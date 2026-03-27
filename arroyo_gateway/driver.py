"""Instrument adapter for Arroyo 7154 MultiSource TEC controllers.

Verified against the Arroyo Computer Interfacing Manual (Rev 2021-01).
See blueprint §8.

Key protocol facts (from manual):
  - Multi-channel: use TEC:CHAN <n> to select channel before commands.
    Commands do NOT take a channel argument directly.
  - Terminator: CR (ASCII 13) or LF (ASCII 10) or both.
  - Responses: bare numeric values, no unit suffixes.
  - Commands are case-insensitive; optional lowercase chars can be omitted.
  - Alarm state via TEC:COND? (condition register bitmask, not TEC:ALARM?).
  - Output state via TEC:OUTput / TEC:OUTput? (not TEC:OUT).
  - Numeric substitutions: 0=OFF, 1=ON accepted.
  - Network: Telnet on port 10001 (7000 Series MultiSource).

Each device gets one ArroyoDriver instance with its own asyncio task.
A hung socket never blocks the event loop for other devices.

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
from typing import Optional, Protocol, Callable, Awaitable

logger = logging.getLogger("arroyo.driver")

EventCallback = Optional[Callable[[str, str, str], Awaitable[None]]]

# ── TEC:COND? bitmask definitions (from manual p.12) ──────────────

COND_BITS = {
    0: "CURRENT_LIMIT",
    1: "VOLTAGE_LIMIT",
    2: "RESISTANCE_LIMIT",
    3: "HIGH_TEMP_LIMIT",
    4: "LOW_TEMP_LIMIT",
    5: "SHORTED_SENSOR",
    6: "OPEN_SENSOR",
    7: "OPEN_TEC",
    # 8: unused
    9: "OUT_OF_TOLERANCE",
    10: "OUTPUT_ON",
    # 11: unused
    12: "THERMAL_RUNAWAY",
}

def _decode_cond(cond: int) -> str:
    """Decode TEC:COND? bitmask into human-readable alarm summary.

    Returns 'NONE' if no alarm bits are set (ignoring bit 10 = OUTPUT_ON
    and bit 9 = OUT_OF_TOLERANCE, which are status, not alarms).
    """
    alarm_bits = cond & ~((1 << 10) | (1 << 9))  # mask out status-only bits
    if alarm_bits == 0:
        return "NONE"
    names = []
    for bit, name in COND_BITS.items():
        if bit in (9, 10):
            continue  # skip status bits for alarm summary
        if cond & (1 << bit):
            names.append(name)
    return ", ".join(names) if names else "NONE"


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
    sensor_raw: float = 0.0       # TEC:R? — sensor resistance
    control_mode: str = "T"       # TEC:MODE? — T, R, or ITE
    fan_state: bool = True        # TEC:FAN? — fan voltage > 0


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


# ── Real TCP driver ────────────────────────────────────────────────

class ArroyoDriver:
    """Async TCP driver for one Arroyo 7154 MultiSource device.

    Commands verified against the Arroyo Computer Interfacing Manual.
    Channel selection via TEC:CHAN <n> before each query/write group.
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
        self._poll_period = 1.0 / poll_rate_hz
        self._failure_threshold = failure_threshold
        self._on_event = on_event
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def _emit(self, event_type: str, detail: str = "") -> None:
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
        For set commands (no '?'), the instrument may not return a
        response; in that case we return an empty string on success.
        """
        async with self._lock:
            if not self._writer or not self._reader:
                return None
            try:
                self._writer.write((cmd + "\r\n").encode("ascii"))
                await self._writer.drain()
                if "?" in cmd:
                    # Query — expect a response line
                    response = await asyncio.wait_for(
                        self._reader.readline(),
                        timeout=self.RESPONSE_TIMEOUT,
                    )
                    return response.decode("ascii").strip()
                else:
                    # Set command — no response expected.
                    # Brief pause to let instrument process.
                    await asyncio.sleep(0.05)
                    return ""
            except (OSError, asyncio.TimeoutError, ConnectionError) as exc:
                logger.warning("Command failed on %s: %s → %s", self.device_id, cmd, exc)
                return None

    async def _select_channel(self, ch: int) -> bool:
        """Select active TEC channel. Must be called before channel-specific commands."""
        resp = await self._send_command(f"TEC:CHAN {ch}")
        return resp is not None

    def _parse_float(self, resp: str) -> Optional[float]:
        """Parse a bare numeric response from the instrument."""
        try:
            return float(resp.strip())
        except (ValueError, TypeError):
            return None

    # ── Polling ────────────────────────────────────────────────────

    async def _poll_once(self) -> bool:
        """Poll all channels. Returns True on full success."""
        for ch_status in self.status.channels:
            ch = ch_status.channel

            # Select channel first
            if not await self._select_channel(ch):
                return False

            # Query actual temperature
            resp = await self._send_command("TEC:T?")
            if resp is None:
                return False
            val = self._parse_float(resp)
            if val is not None:
                ch_status.actual_temp = val

            # Query setpoint
            resp = await self._send_command("TEC:SET:T?")
            if resp is None:
                return False
            val = self._parse_float(resp)
            if val is not None:
                ch_status.setpoint = val

            # Query TEC current
            resp = await self._send_command("TEC:ITE?")
            if resp is None:
                return False
            val = self._parse_float(resp)
            if val is not None:
                ch_status.current = val

            # Query TEC voltage
            resp = await self._send_command("TEC:V?")
            if resp is None:
                return False
            val = self._parse_float(resp)
            if val is not None:
                ch_status.voltage = val

            # Query output state
            resp = await self._send_command("TEC:OUTput?")
            if resp is None:
                return False
            ch_status.output_state = resp.strip() not in ("0", "OFF", "")

            # Query condition register (alarm bitmask)
            resp = await self._send_command("TEC:COND?")
            if resp is None:
                return False
            try:
                ch_status.alarm_raw = int(resp.strip())
                ch_status.alarm_summary = _decode_cond(ch_status.alarm_raw)
            except ValueError:
                ch_status.alarm_raw = 0
                ch_status.alarm_summary = "PARSE_ERROR"

            # Query current limit
            resp = await self._send_command("TEC:LIMit:ITE?")
            if resp is None:
                return False
            val = self._parse_float(resp)
            if val is not None:
                ch_status.current_limit = val

            # Query voltage limit
            resp = await self._send_command("TEC:LIMit:V?")
            if resp is None:
                return False
            val = self._parse_float(resp)
            if val is not None:
                ch_status.voltage_limit = val

            # Query sensor resistance (Tier 1b)
            resp = await self._send_command("TEC:R?")
            if resp is None:
                return False
            val = self._parse_float(resp)
            if val is not None:
                ch_status.sensor_raw = val

            # Query control mode (Tier 1b)
            resp = await self._send_command("TEC:MODE?")
            if resp is None:
                return False
            ch_status.control_mode = resp.strip()

            # Query fan state (Tier 1b)
            resp = await self._send_command("TEC:FAN?")
            if resp is None:
                return False
            val = self._parse_float(resp)
            if val is not None:
                ch_status.fan_state = val > 0

        self.status.last_poll_time = time.time()
        return True

    # ── Write methods (Phase 2) ────────────────────────────────────

    async def set_setpoint(self, ch: int, value: float) -> tuple[Optional[str], Optional[float]]:
        """Set temperature setpoint with readback verification."""
        if not await self._select_channel(ch):
            return None, None
        cmd = f"TEC:T {value:.3f}"
        resp = await self._send_command(cmd)
        if resp is None:
            return None, None
        # Readback
        rb_resp = await self._send_command("TEC:SET:T?")
        if rb_resp is None:
            return f"TEC:CHAN {ch};{cmd}", None
        rb_val = self._parse_float(rb_resp)
        if rb_val is not None:
            self.status.channels[ch - 1].setpoint = rb_val
        return f"TEC:CHAN {ch};{cmd}", rb_val

    async def set_output(self, ch: int, state: bool) -> tuple[Optional[str], Optional[bool]]:
        """Set output on/off with readback verification."""
        if not await self._select_channel(ch):
            return None, None
        cmd = f"TEC:OUTput {'1' if state else '0'}"
        resp = await self._send_command(cmd)
        if resp is None:
            return None, None
        # Readback
        rb_resp = await self._send_command("TEC:OUTput?")
        if rb_resp is None:
            return f"TEC:CHAN {ch};{cmd}", None
        rb_state = rb_resp.strip() not in ("0", "OFF", "")
        self.status.channels[ch - 1].output_state = rb_state
        return f"TEC:CHAN {ch};{cmd}", rb_state

    async def set_current_limit(self, ch: int, value: float) -> tuple[Optional[str], Optional[float]]:
        """Set current limit with readback verification."""
        if not await self._select_channel(ch):
            return None, None
        cmd = f"TEC:LIMit:ITE {value:.3f}"
        resp = await self._send_command(cmd)
        if resp is None:
            return None, None
        rb_resp = await self._send_command("TEC:LIMit:ITE?")
        if rb_resp is None:
            return f"TEC:CHAN {ch};{cmd}", None
        rb_val = self._parse_float(rb_resp)
        if rb_val is not None:
            self.status.channels[ch - 1].current_limit = rb_val
        return f"TEC:CHAN {ch};{cmd}", rb_val

    async def set_voltage_limit(self, ch: int, value: float) -> tuple[Optional[str], Optional[float]]:
        """Set voltage limit with readback verification."""
        if not await self._select_channel(ch):
            return None, None
        cmd = f"TEC:LIMit:V {value:.3f}"
        resp = await self._send_command(cmd)
        if resp is None:
            return None, None
        rb_resp = await self._send_command("TEC:LIMit:V?")
        if rb_resp is None:
            return f"TEC:CHAN {ch};{cmd}", None
        rb_val = self._parse_float(rb_resp)
        if rb_val is not None:
            self.status.channels[ch - 1].voltage_limit = rb_val
        return f"TEC:CHAN {ch};{cmd}", rb_val

    # ── Poll loop ──────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Main polling loop with reconnection and graceful degradation."""
        backoff_idx = 0
        while self._running:
            if not self.status.connected:
                ok = await self._connect()
                if not ok:
                    delay = self.BACKOFF_STEPS[min(backoff_idx, len(self.BACKOFF_STEPS) - 1)]
                    backoff_idx += 1
                    await asyncio.sleep(delay)
                    continue
                backoff_idx = 0

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


# ── Simulated driver ──────────────────────────────────────────────

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

    # ── Write methods (Phase 2) ────────────────────────────────────

    async def set_setpoint(self, ch: int, value: float) -> tuple[Optional[str], Optional[float]]:
        cmd = f"TEC:CHAN {ch};TEC:T {value:.3f}"
        self.status.channels[ch - 1].setpoint = value
        return cmd, value

    async def set_output(self, ch: int, state: bool) -> tuple[Optional[str], Optional[bool]]:
        cmd = f"TEC:CHAN {ch};TEC:OUTput {'1' if state else '0'}"
        self.status.channels[ch - 1].output_state = state
        return cmd, state

    async def set_current_limit(self, ch: int, value: float) -> tuple[Optional[str], Optional[float]]:
        cmd = f"TEC:CHAN {ch};TEC:LIMit:ITE {value:.3f}"
        self.status.channels[ch - 1].current_limit = value
        return cmd, value

    async def set_voltage_limit(self, ch: int, value: float) -> tuple[Optional[str], Optional[float]]:
        cmd = f"TEC:CHAN {ch};TEC:LIMit:V {value:.3f}"
        self.status.channels[ch - 1].voltage_limit = value
        return cmd, value
