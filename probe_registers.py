#!/usr/bin/env python3
"""
Read-only Modbus register probe for GoodWe ESA.

Scans ranges of holding registers looking for a target value (default: 89,
the SOC upper limit set as a test marker). Prints every hit with surrounding
context so we can identify the SOC protection register.

Usage:
    python probe_registers.py                  # scan defaults, target=89
    python probe_registers.py --target 89
    python probe_registers.py --range 45000 50000
    python probe_registers.py --dump 47000 47100   # dump a specific range verbosely
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from pymodbus.client import AsyncModbusTcpClient

# --------------------------------------------------------------------------- #
# Config (mirrors web_app.py)
# --------------------------------------------------------------------------- #
CONFIG_PATH = Path(os.environ.get("DATA_DIR", ".")) / "inverter_config.json"
CONFIG_DEFAULTS = {
    "inverter_ip":  os.environ.get("INVERTER_IP", "192.168.1.x"),
    "modbus_port":  int(os.environ.get("MODBUS_PORT", "502")),
    "slave_id":     int(os.environ.get("SLAVE_ID", "247")),
}

def _load_dotenv():
    """Read key=value pairs from .env in the project root."""
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def load_config() -> dict:
    _load_dotenv()
    # Re-evaluate defaults after .env is loaded
    cfg = {
        "inverter_ip":  os.environ.get("INVERTER_IP", "192.168.1.x"),
        "modbus_port":  int(os.environ.get("MODBUS_PORT", "502")),
        "slave_id":     int(os.environ.get("SLAVE_ID", "247")),
    }
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except Exception:
            pass
    return cfg


# --------------------------------------------------------------------------- #
# Modbus helpers
# --------------------------------------------------------------------------- #
CHUNK = 100  # registers per read (safe well under the 125 max)

async def read_chunk(client, start: int, count: int, slave: int) -> list[int | None]:
    """Read `count` registers from `start`. Returns list with None on error."""
    try:
        result = await client.read_holding_registers(
            address=start, count=count, slave=slave
        )
        if result.isError():
            return [None] * count
        return list(result.registers)
    except Exception as e:
        print(f"  [ERR] reading {start}–{start+count-1}: {e}")
        return [None] * count


async def scan_range(
    client,
    slave: int,
    start: int,
    end: int,
    target: int,
    context_size: int = 5,
    verbose: bool = False,
) -> list[tuple[int, int]]:
    """
    Scan registers [start, end) in chunks.
    Returns list of (address, value) for every register == target.
    Prints context around each hit.
    """
    hits: list[tuple[int, int]] = []
    cache: dict[int, int | None] = {}

    total = end - start
    print(f"\nScanning {start}–{end-1} ({total} registers) …")

    addr = start
    while addr < end:
        count = min(CHUNK, end - addr)
        values = await read_chunk(client, addr, count, slave)
        for i, v in enumerate(values):
            cache[addr + i] = v
        addr += count
        done = addr - start
        print(f"  {done}/{total} ({100*done//total}%)", end="\r", flush=True)

    print()  # newline after progress

    for address in range(start, end):
        v = cache.get(address)
        if v is None:
            if verbose:
                print(f"  {address:6d}  [read error]")
            continue
        if verbose:
            print(f"  {address:6d}  {v:6d}  (0x{v:04X})")
        if v == target:
            hits.append((address, v))

    if hits:
        print(f"\n*** Found target value {target} at {len(hits)} address(es): ***\n")
        for (addr, val) in hits:
            ctx_start = max(start, addr - context_size)
            ctx_end   = min(end,   addr + context_size + 1)
            print(f"  Address {addr} = {val}")
            print(f"  Context ({ctx_start}–{ctx_end-1}):")
            for a in range(ctx_start, ctx_end):
                cv = cache.get(a)
                marker = "  <-- HIT" if a == addr else ""
                val_str = str(cv) if cv is not None else "[err]"
                print(f"    {a:6d}  {val_str:>6}{marker}")
            print()
    else:
        print(f"  (no registers equal to {target} in this range)")

    return hits


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
SCAN_RANGES = [
    # Most GoodWe settings live in the 45000–50000 band
    (45000, 46000, "Battery / BMS settings (lower block)"),
    (46000, 47000, "Battery / BMS settings (mid block)"),
    (47000, 48000, "Inverter / grid settings (incl. known 47120, 47510)"),
    (48000, 49000, "Protection / advanced settings"),
    (49000, 50000, "Extended settings"),
]


async def main():
    parser = argparse.ArgumentParser(description="GoodWe register probe (read-only)")
    parser.add_argument("--target",  type=int, default=89,
                        help="Value to search for (default: 89)")
    parser.add_argument("--range",   type=int, nargs=2, metavar=("START", "END"),
                        help="Custom scan range instead of defaults")
    parser.add_argument("--dump",    type=int, nargs=2, metavar=("START", "END"),
                        help="Dump all register values in range verbosely")
    parser.add_argument("--context", type=int, default=5,
                        help="Registers to print either side of a hit (default: 5)")
    args = parser.parse_args()

    cfg = load_config()
    print(f"Connecting to {cfg['inverter_ip']}:{cfg['modbus_port']}  slave={cfg['slave_id']}")

    client = AsyncModbusTcpClient(
        host=cfg["inverter_ip"],
        port=cfg["modbus_port"],
        timeout=10,
        retries=2,
    )
    connected = await client.connect()
    if not connected:
        print("ERROR: Could not connect to inverter.")
        return

    print("Connected.\n")

    all_hits: list[tuple[int, int]] = []

    try:
        if args.dump:
            # Verbose dump mode — prints every register value
            start, end = args.dump
            print(f"Verbose dump of {start}–{end} (target highlight: {args.target})\n")
            hits = await scan_range(
                client, cfg["slave_id"],
                start, end + 1,
                target=args.target,
                context_size=0,
                verbose=True,
            )
            all_hits.extend(hits)

        elif args.range:
            start, end = args.range
            hits = await scan_range(
                client, cfg["slave_id"],
                start, end,
                target=args.target,
                context_size=args.context,
            )
            all_hits.extend(hits)

        else:
            for (start, end, label) in SCAN_RANGES:
                print(f"\n{'='*60}")
                print(f"Block: {label}")
                print(f"{'='*60}")
                hits = await scan_range(
                    client, cfg["slave_id"],
                    start, end,
                    target=args.target,
                    context_size=args.context,
                )
                all_hits.extend(hits)

        print("\n" + "="*60)
        if all_hits:
            print(f"SUMMARY — all addresses with value {args.target}:")
            for addr, val in all_hits:
                print(f"  {addr}")
        else:
            print(f"No registers found with value {args.target} in scanned ranges.")
        print("="*60)

    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
