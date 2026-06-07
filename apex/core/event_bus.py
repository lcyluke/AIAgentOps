"""Event Bus — append-only event log over SQLite WAL.

Per design §6.8: all events are append-only (INV-4), state is projected from
events, and the bus supports publish/subscribe for Registry projection,
cron triggers, and audit export.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from apex.protocol import ApexEvent


class EventBus:
    """Append-only event bus backed by SQLite WAL."""

    def __init__(self, db_path: str = ""):
        self._db_path = db_path or str(
            Path.home() / ".apex" / "agentops.db"
        )
        self._local = threading.local()
        self._subscribers: list[Callable[[ApexEvent], None]] = []
        self._init_db()

    # ── Init ─────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                type TEXT NOT NULL,
                data_json TEXT DEFAULT '{}',
                timestamp TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp)")
        conn.commit()

    # ── Publish ──────────────────────────────────────────────

    def publish(self, event: ApexEvent) -> str:
        """Publish an event (append-only, INV-4). Returns event ID."""
        if not event.id:
            event.id = f"evt-{event.session_id}-{int(time.time()*1000)}"
        if not event.timestamp:
            event.timestamp = datetime.now().isoformat()

        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO events (id, session_id, type, data_json, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (event.id, event.session_id, event.type,
             json.dumps(event.data), event.timestamp),
        )
        conn.commit()

        # Notify subscribers
        for sub in self._subscribers:
            try:
                sub(event)
            except Exception:
                pass

        return event.id

    # ── Subscribe ────────────────────────────────────────────

    def subscribe(self, callback: Callable[[ApexEvent], None]):
        """Register a subscriber callback."""
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[ApexEvent], None]):
        """Remove a subscriber."""
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    # ── Query ────────────────────────────────────────────────

    def get_events(
        self, session_id: str = "", event_type: str = "",
        since: str = "", limit: int = 100,
    ) -> list[ApexEvent]:
        """Query events with optional filters."""
        conn = self._get_conn()
        where = []
        params = []

        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if event_type:
            where.append("type = ?")
            params.append(event_type)
        if since:
            where.append("timestamp >= ?")
            params.append(since)

        sql = "SELECT * FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        return [
            ApexEvent(
                id=r["id"], session_id=r["session_id"],
                type=r["type"], data=json.loads(r["data_json"]),
                timestamp=r["timestamp"],
            )
            for r in rows
        ]

    def project_state(self, session_id: str) -> str:
        """Project current state from the latest event for a session."""
        events = self.get_events(session_id=session_id, limit=1)
        if not events:
            return "unknown"
        e = events[0]
        t = e.type
        if "registered" in t: return "idle"
        if "running" in t or "heartbeat" in t: return "running"
        if "completed" in t or "done" in t: return "done"
        if "failed" in t: return "failed"
        if "stopped" in t or "disposed" in t: return "stopped"
        return "unknown"

    def count(self, session_id: str = "") -> int:
        """Count events, optionally per session."""
        conn = self._get_conn()
        if session_id:
            r = conn.execute("SELECT COUNT(*) FROM events WHERE session_id = ?",
                           (session_id,)).fetchone()
        else:
            r = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return r[0] if r else 0

    def export_audit(self, since: str = "") -> list[dict]:
        """Export audit trail as JSON-serializable list."""
        events = self.get_events(since=since, limit=10000)
        return [
            {"id": e.id, "session_id": e.session_id, "type": e.type,
             "timestamp": e.timestamp, "data": e.data}
            for e in events
        ]

    def close(self):
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
