"""Maintenance lock manager for Arroyo TEC Gateway.

Per-device lock state with Level A (manual advisory) fallback.
See blueprint §5.

Lock state is held in memory only — never persisted. On restart,
all devices default to DAQ_OWNS (§5.6).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("arroyo.lock")


@dataclass
class LockInfo:
    """Current lock state for one device."""
    state: str = "DAQ_OWNS"          # DAQ_OWNS | MAINT_LOCKED | CONTESTED
    level: Optional[str] = None      # "A" | "B" | "C" | None
    holder: Optional[str] = None     # user who holds the lock
    acquired_at: Optional[float] = None
    expires_at: Optional[float] = None


class LockManager:
    """Manages per-device maintenance locks.

    Phase 2 implementation: Level A (manual advisory) only.
    Level B (cooperative software lock) deferred.
    """

    def __init__(self, timeout_minutes: float = 15.0, on_expire=None) -> None:
        self._locks: dict[str, LockInfo] = {}
        self._timeout_s = timeout_minutes * 60.0
        self._check_task: Optional[asyncio.Task] = None
        self._on_expire = on_expire  # async callback(device_id, holder)

    def register_device(self, device_id: str) -> None:
        """Register a device with default DAQ_OWNS state."""
        if device_id not in self._locks:
            self._locks[device_id] = LockInfo()

    def get(self, device_id: str) -> LockInfo:
        """Get current lock info for a device."""
        return self._locks.get(device_id, LockInfo())

    def acquire(self, device_id: str, user: str) -> LockInfo:
        """Attempt to acquire maintenance lock (Level A).

        Returns updated LockInfo. Raises ValueError if already locked
        by another user.
        """
        lock = self._locks.get(device_id)
        if not lock:
            raise ValueError(f"Unknown device: {device_id}")

        # Check for expired lock
        self._expire_if_stale(device_id)

        if lock.state == "MAINT_LOCKED" and lock.holder != user:
            raise ValueError(
                f"Device {device_id} is locked by {lock.holder} "
                f"until {_fmt_time(lock.expires_at)}"
            )

        now = time.time()
        lock.state = "MAINT_LOCKED"
        lock.level = "A"
        lock.holder = user
        lock.acquired_at = now
        lock.expires_at = now + self._timeout_s
        logger.info("Lock acquired: %s by %s (Level A, expires %s)",
                     device_id, user, _fmt_time(lock.expires_at))
        return lock

    def release(self, device_id: str, user: str) -> LockInfo:
        """Release maintenance lock. Returns updated LockInfo."""
        lock = self._locks.get(device_id)
        if not lock:
            raise ValueError(f"Unknown device: {device_id}")

        if lock.state != "MAINT_LOCKED":
            # Already released or never locked — idempotent
            return lock

        if lock.holder != user:
            raise ValueError(
                f"Lock on {device_id} is held by {lock.holder}, "
                f"not {user}"
            )

        lock.state = "DAQ_OWNS"
        lock.level = None
        lock.holder = None
        lock.acquired_at = None
        lock.expires_at = None
        logger.info("Lock released: %s by %s", device_id, user)
        return lock

    def extend(self, device_id: str, user: str) -> LockInfo:
        """Extend lock timeout. Returns updated LockInfo."""
        lock = self._locks.get(device_id)
        if not lock:
            raise ValueError(f"Unknown device: {device_id}")

        if lock.state != "MAINT_LOCKED" or lock.holder != user:
            raise ValueError(
                f"No active lock on {device_id} held by {user}"
            )

        self._expire_if_stale(device_id)
        if lock.state != "MAINT_LOCKED":
            raise ValueError(f"Lock on {device_id} has expired")

        lock.expires_at = time.time() + self._timeout_s
        logger.info("Lock extended: %s by %s until %s",
                     device_id, user, _fmt_time(lock.expires_at))
        return lock

    def is_locked(self, device_id: str) -> bool:
        """Check if device is in MAINT_LOCKED state (non-expired)."""
        self._expire_if_stale(device_id)
        lock = self._locks.get(device_id)
        return lock is not None and lock.state == "MAINT_LOCKED"

    def require_lock(self, device_id: str, user: str) -> None:
        """Raise ValueError if device is not locked by the given user."""
        self._expire_if_stale(device_id)
        lock = self._locks.get(device_id)
        if not lock or lock.state != "MAINT_LOCKED":
            raise ValueError(f"Device {device_id} is not in maintenance mode")
        if lock.holder != user:
            raise ValueError(
                f"Device {device_id} is locked by {lock.holder}, not {user}"
            )

    def _expire_if_stale(self, device_id: str) -> Optional[str]:
        """Silently expire a lock that has timed out. Returns expired holder or None."""
        lock = self._locks.get(device_id)
        if not lock or lock.state != "MAINT_LOCKED":
            return None
        if lock.expires_at and time.time() > lock.expires_at:
            holder = lock.holder
            logger.info("Lock expired: %s (was held by %s)", device_id, holder)
            lock.state = "DAQ_OWNS"
            lock.level = None
            lock.holder = None
            lock.acquired_at = None
            lock.expires_at = None
            return holder
        return None

    async def start_expiry_checker(self, interval: float = 5.0) -> None:
        """Start a background task that checks for expired locks."""
        self._check_task = asyncio.create_task(
            self._expiry_loop(interval), name="lock-expiry"
        )

    async def stop_expiry_checker(self) -> None:
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass

    async def _expiry_loop(self, interval: float) -> None:
        while True:
            for device_id in list(self._locks):
                expired_holder = self._expire_if_stale(device_id)
                if expired_holder and self._on_expire:
                    try:
                        await self._on_expire(device_id, expired_holder)
                    except Exception:
                        logger.exception("Expiry callback failed for %s", device_id)
            await asyncio.sleep(interval)

    def serialise(self, device_id: str) -> dict:
        """Serialise lock state for API responses."""
        self._expire_if_stale(device_id)
        lock = self.get(device_id)
        remaining = None
        if lock.expires_at:
            remaining = max(0, lock.expires_at - time.time())
        return {
            "state": lock.state,
            "level": lock.level,
            "holder": lock.holder,
            "expires_at": lock.expires_at,
            "remaining_s": round(remaining, 1) if remaining is not None else None,
        }


def _fmt_time(ts: Optional[float]) -> str:
    if ts is None:
        return "—"
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
