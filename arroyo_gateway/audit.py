"""Audit store for Arroyo TEC Gateway.

Append-only SQLite log for connection events, session events,
and (Phase 2+) write operations.  See blueprint §10.
"""

from __future__ import annotations

import aiosqlite
import datetime
import pathlib
from typing import Optional

_DB_PATH = pathlib.Path("audit.db")

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT    NOT NULL,
    user         TEXT    NOT NULL,
    role         TEXT    NOT NULL,
    device_id    TEXT    NOT NULL,
    channel      INTEGER,
    action       TEXT    NOT NULL,
    parameter    TEXT,
    old_value    TEXT,
    new_value    TEXT,
    raw_command  TEXT,
    readback_ok  BOOLEAN,
    ref_event    INTEGER,
    notes        TEXT
);
"""


class AuditStore:
    """Thin async wrapper around the audit SQLite database."""

    def __init__(self, db_path: str | pathlib.Path = _DB_PATH) -> None:
        self._db_path = pathlib.Path(db_path)
        self._db: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def log(
        self,
        *,
        action: str,
        device_id: str,
        user: str = "system",
        role: str = "system",
        channel: Optional[int] = None,
        parameter: Optional[str] = None,
        old_value: Optional[str] = None,
        new_value: Optional[str] = None,
        raw_command: Optional[str] = None,
        readback_ok: Optional[bool] = None,
        ref_event: Optional[int] = None,
        notes: Optional[str] = None,
    ) -> None:
        """Append one audit entry."""
        if not self._db:
            return
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        await self._db.execute(
            """\
            INSERT INTO audit_log
                (timestamp, user, role, device_id, channel, action,
                 parameter, old_value, new_value, raw_command,
                 readback_ok, ref_event, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts, user, role, device_id, channel, action,
                parameter, old_value, new_value, raw_command,
                readback_ok, ref_event, notes,
            ),
        )
        await self._db.commit()

    async def recent(self, limit: int = 100) -> list[dict]:
        """Return the most recent audit entries (newest first)."""
        if not self._db:
            return []
        cursor = await self._db.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        )
        cols = [d[0] for d in cursor.description]
        rows = await cursor.fetchall()
        return [dict(zip(cols, row)) for row in rows]
