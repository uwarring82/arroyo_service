"""Arroyo TEC Gateway — FastAPI application.

Phase 2: bounded writes with maintenance lock, readback verification,
and stability checking.  See blueprint §6 for API specification.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .audit import AuditStore
from .config import Config, load_config
from .driver import SimulatedDriver, ArroyoDriver, DriverProtocol
from .lock import LockManager
from .policy import (
    validate_setpoint, validate_current_limit, validate_voltage_limit,
    validate_output_enable, check_readback, check_readback_bool,
)
from .stability import StabilityChecker

logger = logging.getLogger("arroyo.gateway")

# ── Global state ───────────────────────────────────────────────────

_config: Config
_drivers: dict[str, DriverProtocol] = {}
_audit: AuditStore
_locks: LockManager
_stability: StabilityChecker
_default_user: str = "technician"
_default_role: str = "technician"

STATIC_DIR = Path(__file__).parent.parent / "static"


# ── Lifespan ───────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    global _config, _audit, _locks, _stability, _default_user, _default_role

    config_path = Path(__file__).parent.parent / "config.yaml"
    _config = load_config(config_path)
    project_dir = config_path.parent

    # Config-based role bypass (Phase 2)
    auth_raw = _config.raw.get("auth", {})
    _default_user = auth_raw.get("default_user", "technician")
    _default_role = auth_raw.get("default_role", "technician")

    _audit = AuditStore(project_dir / "audit.db")
    await _audit.open()

    # Lock manager with expiry audit callback
    async def _on_lock_expire(device_id: str, holder: str) -> None:
        await _audit.log(action="lock_expired", device_id=device_id,
                         user=holder, role="system",
                         notes=f"Lock expired (was held by {holder})")

    _locks = LockManager(timeout_minutes=_config.gateway.lock_timeout_minutes,
                          on_expire=_on_lock_expire)

    _stability = StabilityChecker(
        audit=_audit,
        poll_period_s=1.0 / _config.gateway.poll_rate_hz,
        stability_cycles=_config.gateway.stability_check_cycles,
    )

    use_simulator = _config.gateway.driver_mode == "simulator"

    async def _on_driver_event(device_id: str, event_type: str, detail: str) -> None:
        await _audit.log(action=f"connection_{event_type}", device_id=device_id, notes=detail)

    for dev_cfg in _config.devices:
        DriverClass = SimulatedDriver if use_simulator else ArroyoDriver
        drv = DriverClass(
            device_id=dev_cfg.id, name=dev_cfg.name, ip=dev_cfg.ip,
            port=dev_cfg.port, num_channels=dev_cfg.channels,
            poll_rate_hz=_config.gateway.poll_rate_hz,
            failure_threshold=_config.gateway.poll_failure_threshold,
            on_event=_on_driver_event,
        )
        _drivers[dev_cfg.id] = drv
        _locks.register_device(dev_cfg.id)
        await drv.start()
        await _audit.log(
            action="device_start", device_id=dev_cfg.id,
            notes=f"Driver started ({'simulated' if use_simulator else 'TCP'})",
        )

    _stability.set_drivers(_drivers)
    await _stability.start()
    await _locks.start_expiry_checker()

    yield

    await _stability.stop()
    await _locks.stop_expiry_checker()
    for drv in _drivers.values():
        await drv.stop()
    await _audit.close()


app = FastAPI(title="Arroyo TEC Gateway", version="0.2.0", lifespan=lifespan)


# ── Request models ─────────────────────────────────────────────────

class SetpointRequest(BaseModel):
    value: float
    confirmed: bool = False

class OutputRequest(BaseModel):
    state: bool
    confirmed: bool = False

class LimitRequest(BaseModel):
    value: float

class LockRequest(BaseModel):
    device_id: str


# ── Internal helpers ───────────────────────────────────────────────

def _get_driver(device_id: str) -> DriverProtocol:
    drv = _drivers.get(device_id)
    if not drv:
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
    return drv

def _get_channel(drv, ch: int):
    if ch < 1 or ch > len(drv.status.channels):
        raise HTTPException(status_code=400, detail=f"Invalid channel {ch}")
    return drv.status.channels[ch - 1]

def _get_device_config(device_id: str):
    for d in _config.devices:
        if d.id == device_id:
            return d
    return None

def _get_limits(device_id: str, ch: int):
    dev_cfg = _get_device_config(device_id)
    return dev_cfg.software_limits.get(ch) if dev_cfg else None

def _require_lock(device_id: str):
    try:
        _locks.require_lock(device_id, _default_user)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

def _require_connected(drv):
    if drv.status.connection_quality == "disconnected":
        raise HTTPException(status_code=503, detail=f"Device '{drv.status.device_id}' is disconnected")


# ── Read endpoints ─────────────────────────────────────────────────

@app.get("/api/v1/devices")
async def get_devices():
    result = []
    for dev_id, drv in _drivers.items():
        s = drv.status
        lock_info = _locks.serialise(dev_id)
        result.append({
            "id": s.device_id, "name": s.name, "ip": s.ip, "port": s.port,
            "channels": len(s.channels), "connected": s.connected,
            "connection_quality": s.connection_quality,
            "lock_state": lock_info["state"], "lock_level": lock_info["level"],
            "lock_holder": lock_info["holder"], "lock_remaining_s": lock_info["remaining_s"],
        })
    return result

@app.get("/api/v1/devices/{device_id}/status")
async def get_device_status(device_id: str):
    return _serialise_device(_get_driver(device_id))

@app.get("/api/v1/devices/{device_id}/channels/{ch}/status")
async def get_channel_status(device_id: str, ch: int):
    drv = _get_driver(device_id)
    return _serialise_channel(device_id, _get_channel(drv, ch), _get_limits(device_id, ch))


# ── Write endpoints ────────────────────────────────────────────────

@app.post("/api/v1/devices/{device_id}/channels/{ch}/setpoint")
async def post_setpoint(device_id: str, ch: int, req: SetpointRequest):
    drv = _get_driver(device_id)
    _require_lock(device_id)
    _require_connected(drv)
    ch_s = _get_channel(drv, ch)
    limits = _get_limits(device_id, ch)

    ok, error, needs_confirm = validate_setpoint(
        req.value, limits, ch_s.setpoint, _config.gateway.large_setpoint_change_threshold,
    )
    if not ok:
        raise HTTPException(status_code=422, detail=error)
    if needs_confirm and not req.confirmed:
        return {"ok": False, "requires_confirmation": True,
                "message": f"Large setpoint change: {ch_s.setpoint:.2f} → {req.value:.2f}°C"}

    old = ch_s.setpoint
    raw_cmd, rb = await drv.set_setpoint(ch, req.value)
    if raw_cmd is None:
        raise HTTPException(status_code=502, detail="Command failed")

    rb_ok = rb is not None and check_readback(req.value, rb)
    eid = await _audit.log(
        action="set_setpoint", device_id=device_id, channel=ch, parameter="setpoint",
        old_value=str(old), new_value=str(rb or req.value), raw_command=raw_cmd,
        readback_ok=rb_ok, user=_default_user, role=_default_role,
    )
    if eid and rb is not None:
        _stability.schedule(device_id, ch, "setpoint", rb, eid, _default_user, _default_role)

    return {"ok": True, "old_value": old, "new_value": rb, "readback_verified": rb_ok,
            "stability_check": "pending"}


@app.post("/api/v1/devices/{device_id}/channels/{ch}/output")
async def post_output(device_id: str, ch: int, req: OutputRequest):
    drv = _get_driver(device_id)
    _require_lock(device_id)
    _require_connected(drv)
    ch_s = _get_channel(drv, ch)
    limits = _get_limits(device_id, ch)

    if req.state:
        ok, error = validate_output_enable(ch_s.setpoint, limits)
        if not ok:
            raise HTTPException(status_code=422, detail=error)
        if ch_s.alarm_summary != "NONE" and not req.confirmed:
            return {"ok": False, "requires_confirmation": True,
                    "message": f"Channel in alarm ({ch_s.alarm_summary})"}

    old = ch_s.output_state
    raw_cmd, rb = await drv.set_output(ch, req.state)
    if raw_cmd is None:
        raise HTTPException(status_code=502, detail="Command failed")

    rb_ok = rb is not None and check_readback_bool(req.state, rb)
    eid = await _audit.log(
        action="set_output", device_id=device_id, channel=ch, parameter="output_state",
        old_value=str(old), new_value=str(rb or req.state), raw_command=raw_cmd,
        readback_ok=rb_ok, user=_default_user, role=_default_role,
    )
    if eid and rb is not None:
        _stability.schedule(device_id, ch, "output_state", rb, eid, _default_user, _default_role)

    return {"ok": True, "old_state": old, "new_state": rb, "readback_verified": rb_ok,
            "stability_check": "pending"}


@app.post("/api/v1/devices/{device_id}/channels/{ch}/current-limit")
async def post_current_limit(device_id: str, ch: int, req: LimitRequest):
    drv = _get_driver(device_id)
    _require_lock(device_id)
    _require_connected(drv)
    ch_s = _get_channel(drv, ch)
    limits = _get_limits(device_id, ch)

    ok, error = validate_current_limit(req.value, limits)
    if not ok:
        raise HTTPException(status_code=422, detail=error)

    old = ch_s.current_limit
    raw_cmd, rb = await drv.set_current_limit(ch, req.value)
    if raw_cmd is None:
        raise HTTPException(status_code=502, detail="Command failed")

    rb_ok = rb is not None and check_readback(req.value, rb)
    await _audit.log(
        action="set_current_limit", device_id=device_id, channel=ch, parameter="current_limit",
        old_value=str(old), new_value=str(rb or req.value), raw_command=raw_cmd,
        readback_ok=rb_ok, user=_default_user, role=_default_role,
    )
    return {"ok": True, "old_value": old, "new_value": rb, "readback_verified": rb_ok}


@app.post("/api/v1/devices/{device_id}/channels/{ch}/voltage-limit")
async def post_voltage_limit(device_id: str, ch: int, req: LimitRequest):
    drv = _get_driver(device_id)
    _require_lock(device_id)
    _require_connected(drv)
    ch_s = _get_channel(drv, ch)
    limits = _get_limits(device_id, ch)

    ok, error = validate_voltage_limit(req.value, limits)
    if not ok:
        raise HTTPException(status_code=422, detail=error)

    old = ch_s.voltage_limit
    raw_cmd, rb = await drv.set_voltage_limit(ch, req.value)
    if raw_cmd is None:
        raise HTTPException(status_code=502, detail="Command failed")

    rb_ok = rb is not None and check_readback(req.value, rb)
    await _audit.log(
        action="set_voltage_limit", device_id=device_id, channel=ch, parameter="voltage_limit",
        old_value=str(old), new_value=str(rb or req.value), raw_command=raw_cmd,
        readback_ok=rb_ok, user=_default_user, role=_default_role,
    )
    return {"ok": True, "old_value": old, "new_value": rb, "readback_verified": rb_ok}


# ── Lock endpoints ─────────────────────────────────────────────────

@app.post("/api/v1/lock/acquire")
async def acquire_lock(req: LockRequest):
    _get_driver(req.device_id)
    try:
        _locks.acquire(req.device_id, _default_user)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await _audit.log(action="lock_acquire", device_id=req.device_id,
                     user=_default_user, role=_default_role)
    return {"ok": True, **_locks.serialise(req.device_id)}

@app.post("/api/v1/lock/release")
async def release_lock(req: LockRequest):
    _get_driver(req.device_id)
    try:
        _locks.release(req.device_id, _default_user)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await _audit.log(action="lock_release", device_id=req.device_id,
                     user=_default_user, role=_default_role)
    return {"ok": True, **_locks.serialise(req.device_id)}

@app.post("/api/v1/lock/extend")
async def extend_lock(req: LockRequest):
    _get_driver(req.device_id)
    try:
        _locks.extend(req.device_id, _default_user)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await _audit.log(action="lock_extend", device_id=req.device_id,
                     user=_default_user, role=_default_role)
    return {"ok": True, **_locks.serialise(req.device_id)}


# ── SSE / Audit / Static ──────────────────────────────────────────

@app.get("/api/v1/events")
async def sse_events(request: Request):
    async def gen() -> AsyncGenerator[str, None]:
        while True:
            if await request.is_disconnected():
                break
            payload = {"devices": [_serialise_device(drv) for drv in _drivers.values()],
                       "timestamp": time.time()}
            yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(1.0)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})

@app.get("/api/v1/audit/log")
async def get_audit_log(limit: int = 100):
    return await _audit.recent(limit=limit)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


# ── Serialisation ──────────────────────────────────────────────────

def _serialise_device(drv) -> dict:
    s = drv.status
    dev_cfg = _get_device_config(s.device_id)
    lock_info = _locks.serialise(s.device_id)
    channels = []
    for ch_s in s.channels:
        sw = dev_cfg.software_limits.get(ch_s.channel) if dev_cfg else None
        channels.append(_serialise_channel(s.device_id, ch_s, sw))
    return {
        "id": s.device_id, "name": s.name, "ip": s.ip, "port": s.port,
        "connected": s.connected, "connection_quality": s.connection_quality,
        "lock_state": lock_info["state"], "lock_level": lock_info["level"],
        "lock_holder": lock_info["holder"], "lock_remaining_s": lock_info["remaining_s"],
        "channels": channels, "last_poll_time": s.last_poll_time,
        "cache_age_ms": int((time.time() - s.last_poll_time) * 1000) if s.last_poll_time else None,
    }

def _serialise_channel(device_id: str, ch, sw) -> dict:
    return {
        "device_id": device_id, "channel": ch.channel,
        "primary": {"actual_temp": ch.actual_temp, "setpoint": ch.setpoint,
                     "current": ch.current, "voltage": ch.voltage,
                     "output_state": ch.output_state, "alarm_summary": ch.alarm_summary},
        "diagnostic": {"current_limit": ch.current_limit, "voltage_limit": ch.voltage_limit,
                        "alarm_raw": ch.alarm_raw, "sensor_raw": ch.sensor_raw,
                        "fan_state": ch.fan_state, "control_mode": ch.control_mode},
        "software_limits": {"temp_min": sw.temp_min if sw else None, "temp_max": sw.temp_max if sw else None,
                            "current_max": sw.current_max if sw else None, "voltage_max": sw.voltage_max if sw else None},
        "contested_params": [],
    }
