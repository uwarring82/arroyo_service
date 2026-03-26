"""Instrument adapter for Arroyo 7154-05-12 TEC controllers.

See blueprint §8.  Each device gets one ArroyoDriver instance running
its own asyncio task.  A hung socket never blocks the event loop for
other devices.

For development without hardware, SimulatedDriver produces plausible
readings with gentle noise.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol

logger = logging.getLogger("arroyo.driver")

# Type alias for optional async event callback.
# Signature: callback(device_id, event_type, detail)
from typing import Callable, Awaitable
EventCallback = Optional[Callable[[str, str, str], Awaitable[None]]]


# ── Data structures ────────────────────────────────────────────────

@dataclass
class ChannelStatus:
    channel: int
    actual_temp: float = 0.0
    setpoint: float = 0.0
    current: float = 0.0
    voltage: float = 0.0
    output_state: bool = False
    alarm_summary: str = "NONE"
    alarm_raw: int = 0
    # Tier 1b
    current_limit: float = 0.0
    voltage_limit: float = 0.0
    sensor_raw: float = 0.0
    control_mode: str = "T"
    fan_state: bool = True


@dataclass
class DeviceStatus:
    device_id: str
    name: str
    ip: str
    port: int
    connected: bool = False
    connection_quality: str = "disconnected"  # "ok" | "degraded" | "disconnected"
    lock_state: str = "DAQ_OWNS"
    lock_level: Optional[str] = None
    channels: list[ChannelStatus] = field(default_factory=list)
    last_poll_time: float = 0.0
    consecutive_failures: int = 0


# ── Driver protocol ────────────────────────────────────────────────

class DriverProtocol(Protocol):
    device_id: str
    status: DeviceStatus

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


# ── Real TCP driver (for production use with actual hardware) ──────

class ArroyoDriver:
    """Async TCP driver for one Arroyo 7154 device.

    Provisional command mnemonics — must be verified against the
    7154-05-12 programming manual before production use.
    """

    BACKOFF_STEPS = [1, 2, 4, 8, 16]
    RESPONSE_TIMEOUT = 2.0

    def __init__(
        self,
        device_id: str,
        name: str,
        ip: str,
        port: int = 10001,
        num_channels: int = 4,
        poll_rate_hz: float = 1.0,
        failure_threshold: int = 3,
        on_event: EventCallback = None,
    ) -> None:
        self.device_id = device_id
        self.status = DeviceStatus(
            device_id=device_id, name=name, ip=ip, port=port,
            channels=[ChannelStatus(channel=ch) for ch in range(1, num_channels + 1)],
        )
        self._ip = ip
        self._port = port
        self._num_channels = num_channels
        self._poll_period = 1.0 / poll_rate_hz  # seconds between polls
        self._failure_threshold = failure_threshold
        self._on_event = on_event
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def _emit(self, event_type: str, detail: str = "") -> None:
        """Fire the event callback if registered."""
        if self._on_event:
            try:
                await self._on_event(self.device_id, event_type, detail)
            except Exception:
                logger.exception("Event callback failed for %s", self.device_id)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name=f"poll-{self.device_id}")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._disconnect()

    async def _connect(self) -> bool:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._ip, self._port),
                timeout=5.0,
            )
            self.status.connected = True
            self.status.connection_quality = "ok"
            self.status.consecutive_failures = 0
            logger.info("Connected to %s at %s:%d", self.device_id, self._ip, self._port)
            await self._emit("connected", f"{self._ip}:{self._port}")
            return True
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning("Connection failed for %s: %s", self.device_id, exc)
            return False

    async def _disconnect(self) -> None:
        was_connected = self.status.connected
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None
        self.status.connected = False
        if was_connected:
            await self._emit("disconnected", f"after {self.status.consecutive_failures} failures")

    async def _send_command(self, cmd: str) -> Optional[str]:
        """Send a command and read one line of response.

        Returns None on failure.  All access serialised through _lock.
        """
        async with self._lock:
            if not self._writer or not self._reader:
                return None
            try:
                self._writer.write((cmd + "\n").encode("ascii"))
                await self._writer.drain()
                response = await asyncio.wait_for(
                    self._reader.readline(),
                    timeout=self.RESPONSE_TIMEOUT,
                )
                return response.decode("ascii").strip()
            except (OSError, asyncio.TimeoutError, ConnectionError) as exc:
                logger.warning("Command failed on %s: %s → %s", self.device_id, cmd, exc)
                return None

    async def _poll_once(self) -> bool:
        """Poll all channels.  Returns True on success."""
        for ch_status in self.status.channels:
            ch = ch_status.channel
            # Provisional commands — verify against manual
            queries = {
                "actual_temp": f"TEC:T? {ch}",
                "setpoint": f"TEC:SET:T? {ch}",
                "current": f"TEC:ITE? {ch}",
                "voltage": f"TEC:V? {ch}",
                "output_state": f"TEC:OUT? {ch}",
                "alarm_raw": f"TEC:ALARM? {ch}",
                "current_limit": f"TEC:LIM:ITE? {ch}",
                "voltage_limit": f"TEC:LIM:V? {ch}",
            }
            for attr, cmd in queries.items():
                resp = await self._send_command(cmd)
                if resp is None:
                    return False
                try:
                    if attr == "output_state":
                        setattr(ch_status, attr, resp.strip() not in ("0", "OFF"))
                    elif attr == "alarm_raw":
                        ch_status.alarm_raw = int(resp, 0)
                        ch_status.alarm_summary = "NONE" if ch_status.alarm_raw == 0 else f"ALARM(0x{ch_status.alarm_raw:02X})"
                    else:
                        # Strip any unit suffix (e.g. "25.00C" → "25.00")
                        numeric = "".join(c for c in resp if c in "0123456789.-+eE")
                        setattr(ch_status, attr, float(numeric))
                except (ValueError, TypeError) as exc:
                    logger.warning("Parse error on %s ch%d %s: %r → %s", self.device_id, ch, attr, resp, exc)
                    return False
        self.status.last_poll_time = time.time()
        return True

    async def _poll_loop(self) -> None:
        """Main polling loop with reconnection and graceful degradation."""
        backoff_idx = 0
        while self._running:
            # Connect if needed
            if not self.status.connected:
                ok = await self._connect()
                if not ok:
                    delay = self.BACKOFF_STEPS[min(backoff_idx, len(self.BACKOFF_STEPS) - 1)]
                    backoff_idx += 1
                    await asyncio.sleep(delay)
                    continue
                backoff_idx = 0

            # Poll
            ok = await self._poll_once()
            if ok:
                self.status.consecutive_failures = 0
                self.status.connection_quality = "ok"
                self.status.connected = True
            else:
                self.status.consecutive_failures += 1
                if self.status.consecutive_failures >= self._failure_threshold:
                    self.status.connection_quality = "disconnected"
                    self.status.connected = False
                    await self._disconnect()
                    logger.warning("Device %s disconnected after %d failures",
                                   self.device_id, self.status.consecutive_failures)
                else:
                    if self.status.connection_quality != "degraded":
                        await self._emit("degraded", f"failure {self.status.consecutive_failures}/{self._failure_threshold}")
                    self.status.connection_quality = "degraded"

            await asyncio.sleep(self._poll_period)


# ── Simulated driver (for development / UI testing) ────────────────

class SimulatedDriver:
    """Drop-in replacement that generates plausible TEC readings.

    No network connection required.  Useful for UI development and
    acceptance testing of the dashboard.
    """

    def __init__(
        self,
        device_id: str,
        name: str,
        ip: str,
        port: int = 10001,
        num_channels: int = 4,
        poll_rate_hz: float = 1.0,
        failure_threshold: int = 3,
        on_event: EventCallback = None,
    ) -> None:
        self.device_id = device_id
        # Realistic setpoints for a multi-channel TEC setup
        setpoints = [22.0, 25.0, 20.0, 20.0]
        channels = []
        for i in range(num_channels):
            sp = setpoints[i] if i < len(setpoints) else 20.0
            channels.append(ChannelStatus(
                channel=i + 1,
                actual_temp=sp,
                setpoint=sp,
                current=round(random.uniform(0.3, 1.5), 2),
                voltage=round(random.uniform(1.5, 5.0), 2),
                output_state=(i != 2),  # CH 3 starts OFF
                current_limit=2.0,
                voltage_limit=8.0,
                sensor_raw=round(random.uniform(8.0, 15.0), 2),
                control_mode="T",
                fan_state=True,
            ))
        self.status = DeviceStatus(
            device_id=device_id, name=name, ip=ip, port=port,
            connected=True, connection_quality="ok",
            channels=channels,
            last_poll_time=time.time(),
        )
        self._poll_period = 1.0 / poll_rate_hz
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._t0 = time.time()

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._simulate_loop(), name=f"sim-{self.device_id}")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _simulate_loop(self) -> None:
        while self._running:
            t = time.time() - self._t0
            for ch in self.status.channels:
                if ch.output_state:
                    # Gentle drift around setpoint
                    drift = 0.05 * math.sin(t * 0.3 + ch.channel) + random.gauss(0, 0.02)
                    ch.actual_temp = round(ch.setpoint + drift, 2)
                    ch.current = round(abs(0.8 + 0.3 * math.sin(t * 0.1 + ch.channel)) + random.gauss(0, 0.02), 2)
                    ch.voltage = round(abs(3.0 + 1.0 * math.sin(t * 0.05 + ch.channel)) + random.gauss(0, 0.05), 2)
                else:
                    ch.actual_temp = round(ch.actual_temp + random.gauss(0, 0.01), 2)
                    ch.current = 0.0
                    ch.voltage = 0.0
                ch.alarm_raw = 0
                ch.alarm_summary = "NONE"
            self.status.last_poll_time = time.time()
            await asyncio.sleep(self._poll_period)
