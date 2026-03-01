#!/usr/bin/env python3
"""
GoodWe ESA Monitor - Web App
FastAPI + WebSockets live monitor and settings
"""
import asyncio
import json
import os
import zoneinfo
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
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


def _load_config() -> dict:
    cfg = dict(CONFIG_DEFAULTS)
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

    # Load
    'total_load_power': (35171, 2, 1, 'W', 'House Load'),

    # BMS data
    'battery_soc': (37007, 1, 1, '%', 'Battery SOC'),
    'bms_temperature': (37003, 1, 0.1, '°C', 'Battery Temperature'),

    # Today's energy
    'pv_energy_day': (35193, 2, 0.1, 'kWh', 'Solar Today'),
    'battery_charge_day': (35208, 1, 0.1, 'kWh', 'Charged Today'),
    'battery_discharge_day': (35211, 1, 0.1, 'kWh', 'Discharged Today'),
}

SIGNED_REGISTERS = {'ibattery', 'pbattery', 'ac_active_power', 'total_load_power'}

history: deque = deque(maxlen=500)
active_connections: list[WebSocket] = []
update_count: int = 0


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


async def read_inverter_data():
    client = AsyncModbusTcpClient(
        host=config['inverter_ip'],
        port=config['modbus_port'],
        timeout=10,
        retries=3,
    )
    try:
        if not await client.connect():
            return None

        data = {}
        for name, (address, count, scale, unit, _) in MONITOR_REGISTERS.items():
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
                pass

        return data if data else None
    except Exception:
        return None
    finally:
        client.close()


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
                    if ws in active_connections:
                        active_connections.remove(ws)
        except Exception:
            pass

        await asyncio.sleep(config['poll_interval'])


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(inverter_poller())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


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
    active_connections.append(websocket)

    last_50 = list(history)[-50:]
    if last_50:
        await websocket.send_json({"type": "history", "readings": last_50})

    try:
        while True:
            await websocket.receive_text()
    except Exception:
        pass
    finally:
        if websocket in active_connections:
            active_connections.remove(websocket)


async def read_register_raw(address: int):
    client = AsyncModbusTcpClient(
        host=config['inverter_ip'],
        port=config['modbus_port'],
        timeout=10,
    )
    try:
        if not await client.connect():
            return None
        result = await client.read_holding_registers(
            address=address,
            count=1,
            slave=config['slave_id'],
        )
        if not result.isError():
            return result.registers[0]
        return None
    except Exception:
        return None
    finally:
        client.close()


async def write_register_raw(address: int, raw_value: int) -> bool:
    client = AsyncModbusTcpClient(
        host=config['inverter_ip'],
        port=config['modbus_port'],
        timeout=10,
    )
    try:
        if not await client.connect():
            return False
        result = await client.write_register(
            address=address,
            value=raw_value,
            slave=config['slave_id'],
        )
        return not result.isError()
    except Exception:
        return False
    finally:
        client.close()


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
    register: int
    value: int


@app.post("/api/settings")
async def post_settings(body: SettingsWrite):
    register = body.register
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

    return {"success": True}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    print("GoodWe ESA Monitor")
    print(f"Opening on http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
