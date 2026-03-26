"""Arroyo TEC Gateway — FastAPI application.

Phase 1: read-only dashboard with SSE live updates.
See blueprint §6 for API specification.
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

from .audit import AuditStore
from .config import Config, load_config
from .driver import SimulatedDriver, ArroyoDriver, DriverProtocol

logger = logging.getLogger("arroyo.gateway")

# ── Global state ───────────────────────────────────────────────────

_config: Config
_drivers: dict[str, DriverProtocol] = {}
_audit: AuditStore

STATIC_DIR = Path(__file__).parent.parent / "static"


# ── Lifespan ───────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    global _config, _audit

    # Load config
    config_path = Path(__file__).parent.parent / "config.yaml"
    _config = load_config(config_path)
    project_dir = config_path.parent

    # Audit store — anchored next to config.yaml
    _audit = AuditStore(project_dir / "audit.db")
    await _audit.open()

    # Determine driver mode from config
    use_simulator = _config.gateway.driver_mode == "simulator"

    # Audit callback for driver connection events
    async def _on_driver_event(device_id: str, event_type: str, detail: str) -> None:
        await _audit.log(
            action=f"connection_{event_type}",
            device_id=device_id,
            notes=detail,
        )

    # Start drivers
    for dev_cfg in _config.devices:
        DriverClass = SimulatedDriver if use_simulator else ArroyoDriver
        drv = DriverClass(
            device_id=dev_cfg.id,
            name=dev_cfg.name,
            ip=dev_cfg.ip,
            port=dev_cfg.port,
            num_channels=dev_cfg.channels,
            poll_rate_hz=_config.gateway.poll_rate_hz,
            failure_threshold=_config.gateway.poll_failure_threshold,
            on_event=_on_driver_event,
        )
        _drivers[dev_cfg.id] = drv
        await drv.start()
        await _audit.log(
            action="device_start",
            device_id=dev_cfg.id,
            notes=f"Driver started ({'simulated' if use_simulator else 'TCP'})",
        )
        logger.info("Started driver for %s (%s)", dev_cfg.id, dev_cfg.name)

    yield

    # Shutdown
    for drv in _drivers.values():
        await drv.stop()
    await _audit.close()


# ── App ────────────────────────────────────────────────────────────

app = FastAPI(
    title="Arroyo TEC Gateway",
    version="0.1.0",
    lifespan=lifespan,
)


# ── API routes ─────────────────────────────────────────────────────

@app.get("/api/v1/devices")
async def get_devices():
    """List all devices with summary status."""
    result = []
    for dev_id, drv in _drivers.items():
        s = drv.status
        result.append({
            "id": s.device_id,
            "name": s.name,
            "ip": s.ip,
            "port": s.port,
            "channels": len(s.channels),
            "connected": s.connected,
            "connection_quality": s.connection_quality,
            "lock_state": s.lock_state,
            "lock_level": s.lock_level,
        })
    return result


@app.get("/api/v1/devices/{device_id}/status")
async def get_device_status(device_id: str):
    """Full device status including all channel data."""
    drv = _drivers.get(device_id)
    if not drv:
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
    return _serialise_device(drv)


@app.get("/api/v1/devices/{device_id}/channels/{ch}/status")
async def get_channel_status(device_id: str, ch: int):
    """Single channel status with primary and diagnostic fields."""
    drv = _drivers.get(device_id)
    if not drv:
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' not found")
    if ch < 1 or ch > len(drv.status.channels):
        raise HTTPException(status_code=400, detail=f"Invalid channel {ch} (device has {len(drv.status.channels)} channels)")
    ch_status = drv.status.channels[ch - 1]
    dev_cfg = _get_device_config(device_id)
    sw_limits = dev_cfg.software_limits.get(ch, None) if dev_cfg else None
    return _serialise_channel(device_id, ch_status, sw_limits)


# ── SSE endpoint for live updates ──────────────────────────────────

@app.get("/api/v1/events")
async def sse_events(request: Request):
    """Server-Sent Events stream: pushes full device state at ~1 Hz."""

    async def event_generator() -> AsyncGenerator[str, None]:
        while True:
            if await request.is_disconnected():
                break
            payload = {
                "devices": [_serialise_device(drv) for drv in _drivers.values()],
                "timestamp": time.time(),
            }
            yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Audit endpoint (Phase 1: read-only, no auth yet) ──────────────

@app.get("/api/v1/audit/log")
async def get_audit_log(limit: int = 100):
    """Recent audit log entries."""
    return await _audit.recent(limit=limit)


# ── Static files & index ──────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = STATIC_DIR / "index.html"
    return index_path.read_text(encoding="utf-8")


# ── Helpers ────────────────────────────────────────────────────────

def _get_device_config(device_id: str):
    for d in _config.devices:
        if d.id == device_id:
            return d
    return None


def _serialise_device(drv: DriverProtocol) -> dict:
    s = drv.status
    dev_cfg = _get_device_config(s.device_id)
    channels = []
    for ch_s in s.channels:
        sw_limits = dev_cfg.software_limits.get(ch_s.channel, None) if dev_cfg else None
        channels.append(_serialise_channel(s.device_id, ch_s, sw_limits))
    return {
        "id": s.device_id,
        "name": s.name,
        "ip": s.ip,
        "port": s.port,
        "connected": s.connected,
        "connection_quality": s.connection_quality,
        "lock_state": s.lock_state,
        "lock_level": s.lock_level,
        "channels": channels,
        "last_poll_time": s.last_poll_time,
        "cache_age_ms": int((time.time() - s.last_poll_time) * 1000) if s.last_poll_time else None,
    }


def _serialise_channel(device_id: str, ch, sw_limits) -> dict:
    return {
        "device_id": device_id,
        "channel": ch.channel,
        "primary": {
            "actual_temp": ch.actual_temp,
            "setpoint": ch.setpoint,
            "current": ch.current,
            "voltage": ch.voltage,
            "output_state": ch.output_state,
            "alarm_summary": ch.alarm_summary,
        },
        "diagnostic": {
            "current_limit": ch.current_limit,
            "voltage_limit": ch.voltage_limit,
            "alarm_raw": ch.alarm_raw,
            "sensor_raw": ch.sensor_raw,
            "fan_state": ch.fan_state,
            "control_mode": ch.control_mode,
        },
        "software_limits": {
            "temp_min": sw_limits.temp_min if sw_limits else None,
            "temp_max": sw_limits.temp_max if sw_limits else None,
            "current_max": sw_limits.current_max if sw_limits else None,
            "voltage_max": sw_limits.voltage_max if sw_limits else None,
        },
        "contested_params": [],
    }
