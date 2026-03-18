#!/usr/bin/env python3
"""
GoodWe ESA Monitor - Web App
FastAPI + WebSockets live monitor and settings
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import zoneinfo
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pymodbus.client import AsyncModbusTcpClient
import uvicorn

CONFIG_PATH = Path(os.environ.get("DATA_DIR", ".")) / "config.json"
POLL_INTERVAL_MIN = 5
_TZ = zoneinfo.ZoneInfo(os.environ.get("TZ", "Australia/Brisbane"))

CONFIG_DEFAULTS = {
    "inverter_ip":    os.environ.get("INVERTER_IP", "192.168.1.x"),
    "modbus_port":    int(os.environ.get("MODBUS_PORT", "502")),
    "slave_id":       int(os.environ.get("SLAVE_ID", "247")),
    "poll_interval":  max(POLL_INTERVAL_MIN, int(os.environ.get("POLL_INTERVAL", "5"))),
}

SOC_LIMIT_REGISTER = 47760

# Default schedule template: raise to 100% Saturday morning, restore to 90% Sunday evening.
# Disabled by default until the user enables it via settings.
_SOC_SCHEDULE_TEMPLATE: dict = {
    "enabled": False,
    "entries": [
        {"day": 5, "time": "07:00", "value": 100, "enabled": True},
        {"day": 6, "time": "22:00", "value": 90,  "enabled": True},
    ],
}


def _load_config() -> dict:
    cfg = dict(CONFIG_DEFAULTS)
    cfg["soc_schedule"] = copy.deepcopy(_SOC_SCHEDULE_TEMPLATE)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except Exception:
            pass
    return cfg


def _save_config():
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


config: dict = _load_config()

MONITOR_REGISTERS = {
    # PV Generation
    'ppv1': (35105, 2, 1, 'W', 'PV1 Power'),
    'ppv2': (35109, 2, 1, 'W', 'PV2 Power'),

    # Battery essentials
    'vbattery': (35180, 1, 0.1, 'V', 'Battery Voltage'),
    'ibattery': (35181, 1, 0.1, 'A', 'Battery Current'),
    'pbattery': (35182, 2, 1, 'W', 'Battery Power'),

    # Grid power
    'ac_active_power': (35139, 2, 1, 'W', 'Grid Power'),

    # Load is derived (see read_inverter_data)

    # BMS data
    'battery_soc': (37007, 1, 1, '%', 'Battery SOC'),
    'bms_temperature': (37003, 1, 0.1, '°C', 'Battery Temperature'),

    # Today's energy
    'pv_energy_day': (35193, 2, 0.1, 'kWh', 'Solar Today'),
    'battery_charge_day': (35208, 1, 0.1, 'kWh', 'Charged Today'),
    'battery_discharge_day': (35211, 1, 0.1, 'kWh', 'Discharged Today'),
}

SIGNED_REGISTERS = {'ibattery', 'pbattery', 'ac_active_power'}

history: deque = deque(maxlen=500)
active_connections: set[WebSocket] = set()
update_count: int = 0
_modbus_client: AsyncModbusTcpClient | None = None


def decode_register_value(registers, count, scale, signed=True):
    if count == 1:
        value = registers[0]
        if signed and value > 32767:
            value = value - 65536
    else:
        value = (registers[0] << 16) + registers[1]
        if signed and value > 2147483647:
            value = value - 4294967296
    return value * scale


def _reset_client():
    global _modbus_client
    if _modbus_client is not None:
        _modbus_client.close()
        _modbus_client = None


async def _get_client() -> AsyncModbusTcpClient | None:
    global _modbus_client
    if _modbus_client is not None and _modbus_client.connected:
        return _modbus_client
    _reset_client()
    client = AsyncModbusTcpClient(
        host=config['inverter_ip'],
        port=config['modbus_port'],
        timeout=10,
        retries=3,
    )
    if await client.connect():
        _modbus_client = client
        return client
    client.close()
    return None


async def read_inverter_data():
    client = await _get_client()
    if client is None:
        return None

    data = {}
    for name, (address, count, scale, unit, _) in MONITOR_REGISTERS.items():
        await asyncio.sleep(0)  # yield between reads so the event loop can handle incoming connections
        try:
            result = await client.read_holding_registers(
                address=address,
                count=count,
                slave=config['slave_id'],
            )
            if not result.isError():
                value = decode_register_value(
                    result.registers, count, scale,
                    signed=(name in SIGNED_REGISTERS),
                )
                # Sanity checks
                if name == 'vbattery' and (value < 40 or value > 600):
                    continue
                if name == 'battery_soc' and (value < 0 or value > 100):
                    continue
                if name == 'bms_temperature' and (value < -20 or value > 80):
                    continue
                data[name] = value
        except Exception:
            _reset_client()
            break

    # Compute house load from energy balance: battery + pv - grid_export
    # Works in both grid-connected and backup modes
    if 'pbattery' in data or 'ppv1' in data or 'ppv2' in data or 'ac_active_power' in data:
        data['total_load_power'] = round(
            data.get('pbattery', 0) +
            data.get('ppv1', 0) +
            data.get('ppv2', 0) -
            data.get('ac_active_power', 0)
        )

    return data if data else None


_scheduler_last_applied: dict[tuple, str] = {}
_scheduler_last_tick: str = "never"


async def soc_scheduler():
    """Apply scheduled SOC limit changes once per configured day/time."""
    global _scheduler_last_tick
    while True:
        try:
            schedule = config.get("soc_schedule", {})
            now = datetime.now(_TZ)
            current_day = now.weekday()  # 0=Mon … 6=Sun
            current_time = now.strftime('%H:%M')
            today = now.strftime('%Y-%m-%d')
            _scheduler_last_tick = now.strftime('%Y-%m-%d %H:%M:%S')
            if schedule.get("enabled"):
                for entry in schedule.get("entries", []):
                    if not entry.get("enabled", True):
                        continue
                    if entry["day"] == current_day and entry["time"] == current_time:
                        key = (entry["day"], entry["time"])
                        if _scheduler_last_applied.get(key) != today:
                            print(f"[soc_scheduler] Firing: day={entry['day']} time={entry['time']} value={entry['value']}")
                            success = await write_register_raw(SOC_LIMIT_REGISTER, entry["value"])
                            print(f"[soc_scheduler] Write result: {success}")
                            _scheduler_last_applied[key] = today
        except Exception as e:
            print(f"[soc_scheduler] ERROR: {e}")
        await asyncio.sleep(30)


async def inverter_poller():
    global update_count
    while True:
        try:
            data = await read_inverter_data()
            if data:
                update_count += 1
                now = datetime.now(_TZ)
                reading = {
                    'type': 'update',
                    'timestamp': now.strftime('%H:%M:%S'),
                    'date': now.strftime('%Y-%m-%d'),
                    'data': data,
                }
                history.append(reading)

                dead = []
                for ws in list(active_connections):
                    try:
                        await ws.send_json(reading)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    active_connections.discard(ws)
        except Exception:
            pass

        await asyncio.sleep(config['poll_interval'])


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(inverter_poller()),
        asyncio.create_task(soc_scheduler()),
    ]
    yield
    for task in tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    _reset_client()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("templates/index.html")


@app.get("/settings")
async def settings_page():
    return FileResponse("templates/settings.html")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.add(websocket)

    last_50 = list(history)[-50:]
    if last_50:
        await websocket.send_json({"type": "history", "readings": last_50})

    try:
        while True:
            await websocket.receive_text()
    except Exception:
        pass
    finally:
        active_connections.discard(websocket)


async def read_register_raw(address: int):
    client = await _get_client()
    if client is None:
        return None
    try:
        result = await client.read_holding_registers(
            address=address,
            count=1,
            slave=config['slave_id'],
        )
        if not result.isError():
            return result.registers[0]
        return None
    except Exception:
        _reset_client()
        return None


async def write_register_raw(address: int, raw_value: int) -> bool:
    client = await _get_client()
    if client is None:
        return False
    try:
        result = await client.write_register(
            address=address,
            value=raw_value,
            slave=config['slave_id'],
        )
        return not result.isError()
    except Exception:
        _reset_client()
        return False


@app.get("/api/debug/schedule")
async def debug_schedule():
    now = datetime.now(_TZ)
    schedule = config.get("soc_schedule", {})
    entries = schedule.get("entries", [])
    current_day = now.weekday()
    current_time = now.strftime('%H:%M')
    today = now.strftime('%Y-%m-%d')
    return {
        "server_time": now.strftime('%Y-%m-%d %H:%M:%S %Z'),
        "current_day": current_day,
        "current_time": current_time,
        "schedule_enabled": schedule.get("enabled", False),
        "entries": entries,
        "last_applied": {f"{k[0]}@{k[1]}": v for k, v in _scheduler_last_applied.items()},
        "scheduler_last_tick": _scheduler_last_tick,
        "matches_right_now": [
            e for e in entries
            if e.get("enabled", True)
            and e["day"] == current_day
            and e["time"] == current_time
            and _scheduler_last_applied.get((e["day"], e["time"])) != today
        ],
    }


@app.get("/api/soc-limit")
async def get_soc_limit():
    raw = await read_register_raw(SOC_LIMIT_REGISTER)
    if raw is None:
        return {"value": None, "register": SOC_LIMIT_REGISTER}
    return {"value": raw, "register": SOC_LIMIT_REGISTER}


class SocLimitWrite(BaseModel):
    value: int


@app.post("/api/soc-limit")
async def post_soc_limit(body: SocLimitWrite):
    if not (0 <= body.value <= 100):
        return {"success": False, "error": "Value must be 0–100"}
    success = await write_register_raw(SOC_LIMIT_REGISTER, body.value)
    if not success:
        return {"success": False, "error": "Write failed"}
    await asyncio.sleep(0.5)
    verified = await read_register_raw(SOC_LIMIT_REGISTER)
    return {"success": True, "verified_value": verified}


@app.get("/api/soc-schedule")
async def get_soc_schedule():
    return config.get("soc_schedule", copy.deepcopy(_SOC_SCHEDULE_TEMPLATE))


class ScheduleEntry(BaseModel):
    day: int    # 0=Mon … 6=Sun
    time: str   # HH:MM
    value: int  # 0–100
    enabled: bool = True


class SocScheduleWrite(BaseModel):
    enabled: bool
    entries: list[ScheduleEntry]


@app.post("/api/soc-schedule")
async def post_soc_schedule(body: SocScheduleWrite):
    for entry in body.entries:
        if not (0 <= entry.day <= 6):
            return {"success": False, "error": "Day must be 0–6"}
        if not re.match(r'^\d{2}:\d{2}$', entry.time):
            return {"success": False, "error": "Time must be HH:MM"}
        if not (0 <= entry.value <= 100):
            return {"success": False, "error": "Limit must be 0–100"}
    config["soc_schedule"] = {
        "enabled": body.enabled,
        "entries": [
            {"day": e.day, "time": e.time, "value": e.value, "enabled": e.enabled}
            for e in body.entries
        ],
    }
    _save_config()
    return {"success": True}


@app.get("/api/settings")
async def get_settings():
    raw_47120 = await read_register_raw(47120)
    raw_47510 = await read_register_raw(47510)

    result = {}

    if raw_47120 is not None:
        signed = raw_47120 if raw_47120 <= 32767 else raw_47120 - 65536
        result['meter_target_power_offset'] = {
            'value': signed,
            'register': 47120,
        }
    else:
        result['meter_target_power_offset'] = None

    if raw_47510 is not None:
        result['grid_export_limit'] = {
            'value': raw_47510,
            'register': 47510,
        }
    else:
        result['grid_export_limit'] = None

    return result


class SettingsWrite(BaseModel):
    register_address: int
    value: int


@app.post("/api/settings")
async def post_settings(body: SettingsWrite):
    register = body.register_address
    value = body.value

    if register not in (47120, 47510):
        return {"success": False, "error": "Unknown register"}

    if register == 47120:
        if not (-32768 <= value <= 32767):
            return {"success": False, "error": "Value out of range (-32768 to 32767)"}
        raw = value if value >= 0 else value + 65536
    else:
        if not (0 <= value <= 65535):
            return {"success": False, "error": "Value out of range (0 to 65535)"}
        raw = value

    success = await write_register_raw(register, raw)

    if success:
        await asyncio.sleep(0.5)
        verify_raw = await read_register_raw(register)
        if verify_raw is not None:
            if register == 47120:
                verified = verify_raw if verify_raw <= 32767 else verify_raw - 65536
            else:
                verified = verify_raw
            return {"success": True, "verified_value": verified}
        return {"success": True, "verified_value": None}
    else:
        return {"success": False, "error": "Write failed"}


@app.get("/api/config")
async def get_config():
    return config


class ConfigWrite(BaseModel):
    inverter_ip: str
    modbus_port: int
    slave_id: int
    poll_interval: int


@app.post("/api/config")
async def post_config(body: ConfigWrite):
    if not body.inverter_ip.strip():
        return {"success": False, "error": "Inverter IP cannot be empty"}
    if not (1 <= body.modbus_port <= 65535):
        return {"success": False, "error": "Port must be 1\u201365535"}
    if not (1 <= body.slave_id <= 247):
        return {"success": False, "error": "Slave ID must be 1\u2013247"}
    if body.poll_interval < POLL_INTERVAL_MIN:
        return {"success": False, "error": f"Poll interval minimum is {POLL_INTERVAL_MIN}s"}

    config['inverter_ip'] = body.inverter_ip.strip()
    config['modbus_port'] = body.modbus_port
    config['slave_id'] = body.slave_id
    config['poll_interval'] = body.poll_interval
    _save_config()
    _reset_client()

    return {"success": True}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    print("GoodWe ESA Monitor")
    print(f"Opening on http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, ws_ping_interval=20, ws_ping_timeout=20)
