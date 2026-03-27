"""Post-write stability verification for Arroyo TEC Gateway.

After a write, the stability checker waits N polling cycles then
re-reads the parameter.  If the value has reverted, it logs a
'stability_contested' event; otherwise 'stability_confirmed'.
See blueprint §5.5.

All stability events reference the original write event via ref_event.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .audit import AuditStore

from .policy import READBACK_TOLERANCE

logger = logging.getLogger("arroyo.stability")


@dataclass
class PendingCheck:
    """A write awaiting stability verification."""
    device_id: str
    channel: int
    parameter: str          # "setpoint" | "output_state" | "current_limit" | "voltage_limit"
    expected_value: float | bool
    audit_event_id: int     # ref_event for the follow-up log entry
    check_after: float      # timestamp after which to check
    user: str
    role: str


class StabilityChecker:
    """Background service that verifies write stability."""

    def __init__(
        self,
        audit: AuditStore,
        poll_period_s: float = 1.0,
        stability_cycles: int = 2,
    ) -> None:
        self._audit = audit
        self._check_delay = poll_period_s * stability_cycles
        self._pending: list[PendingCheck] = []
        self._task: Optional[asyncio.Task] = None
        self._drivers: dict = {}  # set from app.py

    def set_drivers(self, drivers: dict) -> None:
        self._drivers = drivers

    def schedule(
        self,
        device_id: str,
        channel: int,
        parameter: str,
        expected_value: float | bool,
        audit_event_id: int,
        user: str = "system",
        role: str = "system",
    ) -> None:
        """Schedule a stability check for a recent write."""
        self._pending.append(PendingCheck(
            device_id=device_id,
            channel=channel,
            parameter=parameter,
            expected_value=expected_value,
            audit_event_id=audit_event_id,
            check_after=time.time() + self._check_delay,
            user=user,
            role=role,
        ))

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="stability-checker")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            now = time.time()
            ready = [p for p in self._pending if now >= p.check_after]
            for check in ready:
                self._pending.remove(check)
                await self._verify(check)
            await asyncio.sleep(0.5)

    async def _verify(self, check: PendingCheck) -> None:
        """Read current value and compare with expected."""
        drv = self._drivers.get(check.device_id)
        if not drv:
            logger.warning("Stability check: driver %s not found", check.device_id)
            return

        ch_status = None
        for ch in drv.status.channels:
            if ch.channel == check.channel:
                ch_status = ch
                break

        if ch_status is None:
            logger.warning("Stability check: channel %d not found on %s",
                           check.channel, check.device_id)
            return

        # Read current value from cached status (most recent poll)
        current = getattr(ch_status, check.parameter, None)
        if current is None:
            logger.warning("Stability check: parameter %s not found", check.parameter)
            return

        # Compare
        if isinstance(check.expected_value, bool):
            stable = current == check.expected_value
        else:
            stable = abs(float(current) - float(check.expected_value)) <= READBACK_TOLERANCE

        action = "stability_confirmed" if stable else "stability_contested"
        notes = None
        if not stable:
            notes = (
                f"Expected {check.parameter}={check.expected_value}, "
                f"found {current} after stability window"
            )
            logger.warning("Stability contested on %s ch%d: %s",
                           check.device_id, check.channel, notes)

        await self._audit.log(
            action=action,
            device_id=check.device_id,
            channel=check.channel,
            parameter=check.parameter,
            old_value=str(check.expected_value),
            new_value=str(current),
            ref_event=check.audit_event_id,
            user=check.user,
            role=check.role,
            notes=notes,
        )
