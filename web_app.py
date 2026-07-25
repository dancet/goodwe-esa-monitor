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

CONFIG_PATH = Path(os.environ.get("DATA_DIR", ".")) / "inverter_config.json"
POLL_INTERVAL_MIN = 5
_TZ = zoneinfo.ZoneInfo(os.environ.get("TZ", "Australia/Brisbane"))

CONFIG_DEFAULTS = {
    "inverter_ip":    os.environ.get("INVERTER_IP", "192.168.1.x"),
    "modbus_port":    int(os.environ.get("MODBUS_PORT", "502")),
    "slave_id":       int(os.environ.get("SLAVE_ID", "247")),
    "poll_interval":  max(POLL_INTERVAL_MIN, int(os.environ.get("POLL_INTERVAL", "5"))),
}

SOC_LIMIT_REGISTER = 47760
GRID_EXPORT_REGISTER = 47510

EVERY_DAY = -1  # schedule entry day value meaning "repeat daily"

# Default schedule template: raise to 100% Saturday morning, restore to 90% Sunday evening.
# Disabled by default until the user enables it via settings.
_SOC_SCHEDULE_TEMPLATE: dict = {
    "enabled": False,
    "entries": [
        {"day": 5, "time": "07:00", "value": 100, "enabled": True},
        {"day": 6, "time": "22:00", "value": 90,  "enabled": True},
    ],
}

# Default export schedule: throttle export during the evening peak, restore overnight.
# Disabled by default until the user enables it via settings.
_EXPORT_SCHEDULE_TEMPLATE: dict = {
    "enabled": False,
    "entries": [
        {"day": EVERY_DAY, "time": "17:00", "value": 250,  "enabled": True},
        {"day": EVERY_DAY, "time": "22:00", "value": 5000, "enabled": True},
    ],
}


def _load_config() -> dict:
    cfg = dict(CONFIG_DEFAULTS)
    cfg["soc_schedule"] = copy.deepcopy(_SOC_SCHEDULE_TEMPLATE)
    cfg["export_schedule"] = copy.deepcopy(_EXPORT_SCHEDULE_TEMPLATE)
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


_TIME_RE = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')
_WEEK_MINUTES = 7 * 24 * 60


def _week_minute(day: int, time_str: str) -> int:
    hh, mm = time_str.split(':')
    return day * 1440 + int(hh) * 60 + int(mm)


def _expand_slots(entries: list) -> list[tuple]:
    """Expand schedule entries into concrete week slots, sorted by time-of-week.

    An entry with day == EVERY_DAY expands to one slot per weekday. Slots are
    (week_minute, value, key) where key identifies the originating entry.
    Where a daily entry and a specific-day entry collide on the same minute the
    specific-day entry sorts later, so it wins as the active slot.
    """
    slots = []
    for entry in entries or []:
        if not entry.get("enabled", True):
            continue
        try:
            day = int(entry["day"])
            value = int(entry["value"])
            time_str = str(entry["time"])
        except (KeyError, TypeError, ValueError):
            continue
        if not _TIME_RE.match(time_str):
            continue
        if day == EVERY_DAY:
            days = range(7)
        elif 0 <= day <= 6:
            days = [day]
        else:
            continue
        key = (day, time_str)
        for d in days:
            slots.append((_week_minute(d, time_str), value, key))
    slots.sort(key=lambda s: (s[0], 0 if s[2][0] == EVERY_DAY else 1))
    return slots


def _now_week_minute(now: datetime) -> int:
    return now.weekday() * 1440 + now.hour * 60 + now.minute


def _active_slot(entries: list, now: datetime):
    """The slot in force right now: the most recent one at or before now.

    If nothing has fired yet this week it wraps to the final slot of the week,
    which is the one that was applied before the week rolled over.
    """
    slots = _expand_slots(entries)
    if not slots:
        return None
    now_min = _now_week_minute(now)
    active = None
    for slot in slots:
        if slot[0] <= now_min:
            active = slot
        else:
            break
    return active if active is not None else slots[-1]


def _next_slot(entries: list, now: datetime):
    """The next slot due to fire after now, wrapping into next week."""
    slots = _expand_slots(entries)
    if not slots:
        return None
    now_min = _now_week_minute(now)
    for slot in slots:
        if slot[0] > now_min:
            return slot
    return slots[0]


def _describe_slot(slot) -> dict | None:
    if slot is None:
        return None
    week_minute, value, key = slot
    day = week_minute // 1440
    return {
        "day": day,
        "day_name": DAY_NAMES[day],
        "time": key[1],
        "value": value,
        "daily": key[0] == EVERY_DAY,
    }


DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

_export_state: dict = {
    "last_tick": "never",
    "last_action": "not started",
    "last_write": None,
    "last_error": None,
    "override": None,
}

EXPORT_RECONCILE_INTERVAL = 30
_OVERRIDE_MAX_AGE = 24 * 3600


async def _reconcile_export_limit(now: datetime) -> None:
    """Drive the export limit register towards whatever the schedule says it
    should be right now.

    This is deliberately a convergence loop rather than a fire-at-this-minute
    trigger: if the app was down, the tick was late, the write failed, or the
    value was changed elsewhere (e.g. in SolarGo), the next pass still corrects
    it. Missing a transition would otherwise leave the export limit wide open
    through the evening peak.
    """
    schedule = config.get("export_schedule") or {}

    if not schedule.get("enabled"):
        _export_state["override"] = None
        _export_state["last_action"] = "schedule disabled"
        return

    slot = _active_slot(schedule.get("entries", []), now)
    if slot is None:
        _export_state["last_action"] = "no enabled entries"
        return

    _, target, key = slot

    # A manual "Set now" holds until the schedule moves on to a different entry,
    # so a deliberate override isn't stomped 30 seconds later.
    override = _export_state.get("override")
    if override is not None:
        stale = (now - override["set_at"]).total_seconds() > _OVERRIDE_MAX_AGE
        if override["key"] == key and not stale:
            _export_state["last_action"] = (
                f"manual override held at {override['value']}W until next entry"
            )
            return
        _export_state["override"] = None

    current = await read_register_raw(GRID_EXPORT_REGISTER)
    if current is None:
        _export_state["last_error"] = f"{now:%Y-%m-%d %H:%M:%S} read failed"
        _export_state["last_action"] = "read failed — will retry"
        return

    if current == target:
        _export_state["last_action"] = f"in sync at {target}W"
        return

    print(f"[export_scheduler] Correcting export limit {current}W -> {target}W "
          f"(entry {key[0]}@{key[1]})")
    ok = await write_register_raw(GRID_EXPORT_REGISTER, target)
    if not ok:
        _export_state["last_error"] = f"{now:%Y-%m-%d %H:%M:%S} write failed"
        _export_state["last_action"] = f"write to {target}W failed — will retry"
        print("[export_scheduler] Write FAILED, retrying next pass")
        return

    await asyncio.sleep(0.5)
    verified = await read_register_raw(GRID_EXPORT_REGISTER)
    _export_state["last_write"] = (
        f"{now:%Y-%m-%d %H:%M:%S} set {current}W → {target}W "
        f"(verified {verified if verified is not None else '?'}W)"
    )
    if verified is not None and verified != target:
        _export_state["last_action"] = (
            f"wrote {target}W but read back {verified}W — will retry"
        )
        print(f"[export_scheduler] Verify mismatch: wanted {target}, got {verified}")
    else:
        _export_state["last_action"] = f"applied {target}W"
        _export_state["last_error"] = None
        print(f"[export_scheduler] Applied {target}W")


def _note_export_override(value: int, now: datetime | None = None) -> None:
    """Record a manual export-limit write so the reconciler backs off until the
    schedule reaches its next entry."""
    schedule = config.get("export_schedule") or {}
    if not schedule.get("enabled"):
        return
    now = now or datetime.now(_TZ)
    slot = _active_slot(schedule.get("entries", []), now)
    if slot is None:
        return
    _export_state["override"] = {"key": slot[2], "value": value, "set_at": now}
    print(f"[export_scheduler] Manual override {value}W held until entry after "
          f"{slot[2][0]}@{slot[2][1]}")


async def export_scheduler():
    while True:
        now = datetime.now(_TZ)
        try:
            await _reconcile_export_limit(now)
        except Exception as e:
            _export_state["last_error"] = f"{now:%Y-%m-%d %H:%M:%S} {e}"
            _export_state["last_action"] = "error — will retry"
            print(f"[export_scheduler] ERROR: {e}")
        _export_state["last_tick"] = now.strftime('%Y-%m-%d %H:%M:%S')
        await asyncio.sleep(EXPORT_RECONCILE_INTERVAL)


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
        asyncio.create_task(export_scheduler()),
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


@app.get("/api/export-schedule")
async def get_export_schedule():
    return config.get("export_schedule", copy.deepcopy(_EXPORT_SCHEDULE_TEMPLATE))


class ExportScheduleEntry(BaseModel):
    day: int    # -1 = every day, else 0=Mon … 6=Sun
    time: str   # HH:MM
    value: int  # Watts, 0–65535
    enabled: bool = True


class ExportScheduleWrite(BaseModel):
    enabled: bool
    entries: list[ExportScheduleEntry]


@app.post("/api/export-schedule")
async def post_export_schedule(body: ExportScheduleWrite):
    for entry in body.entries:
        if entry.day != EVERY_DAY and not (0 <= entry.day <= 6):
            return {"success": False, "error": "Day must be Every day or 0–6"}
        if not _TIME_RE.match(entry.time):
            return {"success": False, "error": f"Invalid time '{entry.time}' (expected HH:MM)"}
        if not (0 <= entry.value <= 65535):
            return {"success": False, "error": "Export limit must be 0–65535 W"}

    enabled_entries = [e for e in body.entries if e.enabled]
    if body.enabled and not enabled_entries:
        return {"success": False, "error": "Enable at least one entry, or turn the schedule off"}

    # Guard against a schedule that can never fully define a day: with a single
    # entry the limit would be pinned to that value forever.
    if body.enabled and len(enabled_entries) < 2:
        return {"success": False, "error": "A schedule needs at least two entries to cycle between"}

    config["export_schedule"] = {
        "enabled": body.enabled,
        "entries": [
            {"day": e.day, "time": e.time, "value": e.value, "enabled": e.enabled}
            for e in body.entries
        ],
    }
    _save_config()
    # Drop any manual override and converge immediately rather than waiting for
    # the next tick, so the UI reflects reality straight after saving.
    _export_state["override"] = None
    now = datetime.now(_TZ)
    try:
        await _reconcile_export_limit(now)
    except Exception as e:
        print(f"[export_scheduler] immediate reconcile failed: {e}")
    return {"success": True}


@app.get("/api/export-schedule/status")
async def get_export_schedule_status():
    now = datetime.now(_TZ)
    schedule = config.get("export_schedule") or {}
    entries = schedule.get("entries", [])
    override = _export_state.get("override")
    return {
        "server_time": now.strftime('%Y-%m-%d %H:%M:%S %Z'),
        "enabled": schedule.get("enabled", False),
        "active": _describe_slot(_active_slot(entries, now)),
        "next": _describe_slot(_next_slot(entries, now)),
        "override": (
            {"value": override["value"], "set_at": override["set_at"].strftime('%H:%M:%S')}
            if override else None
        ),
        "last_tick": _export_state["last_tick"],
        "last_action": _export_state["last_action"],
        "last_write": _export_state["last_write"],
        "last_error": _export_state["last_error"],
    }


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
        if register == GRID_EXPORT_REGISTER:
            _note_export_override(value)
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
