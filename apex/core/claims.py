"""Claims Registry — optimistic locking for task claims (§6.4).

Prevents duplicate work by letting agents claim tasks optimistically.  If a
task is already claimed, the caller receives the current holder's identity.
Active claims are fed into agent context injections so agents know what NOT
to duplicate.

Table: claims  (stored in the same SQLite DB as apex.storage.db)
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Configuration ────────────────────────────────────────────────────────────

_DEFAULT_DB_PATH = Path.home() / ".apex" / "agentops.db"
DB_PATH = Path(os.environ.get("APEX_DB_PATH", str(_DEFAULT_DB_PATH)))


# ── Connection management (thread-safe, follows apex/storage/db.py) ──────────

_local = threading.local()
_init_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """Return a thread-local connection.  Lazily opens + migrates."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    with _init_lock:
        conn = getattr(_local, "conn", None)
        if conn is not None:
            return conn
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        _init_table(conn)
        _local.conn = conn
        return conn


def _init_table(conn: sqlite3.Connection) -> None:
    """Create claims table and indexes if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS claims (
            task_id       TEXT PRIMARY KEY,
            claimed_by    TEXT    NOT NULL,
            claimed_at    TEXT    DEFAULT '',
            status        TEXT    DEFAULT 'active',
            criteria_json TEXT    DEFAULT '{}',
            findings_json TEXT    DEFAULT '{}',
            verified      INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_claims_status
            ON claims(status);
        CREATE INDEX IF NOT EXISTS idx_claims_claimed_by
            ON claims(claimed_by);
        CREATE INDEX IF NOT EXISTS idx_claims_claimed_at
            ON claims(claimed_at);
    """)


def _close_conn() -> None:
    """Close the thread-local connection (useful for tests)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


# ── Claims Registry ──────────────────────────────────────────────────────────

class ClaimsRegistry:
    """Optimistic-locking claims registry for task coordination.

    Agents call ``claim()`` before starting work on a task.  If the task is
    already claimed, the current holder is returned so the caller can move on.
    """

    # ------------------------------------------------------------------
    # claim
    # ------------------------------------------------------------------

    def claim(
        self,
        task_id: str,
        by: str,
        criteria: dict | None = None,
    ) -> dict:
        """Attempt to claim a task (optimistic lock).

        Args:
            task_id: Unique task identifier.
            by: Agent / role name making the claim.
            criteria: Optional JSON-serializable criteria dict.

        Returns:
            A dict with keys:
              - success (bool): True if the claim succeeded.
              - holder (str): Current claim holder.
              - claimed_at (str): ISO-8601 timestamp of the claim.
              - status (str): Claim status ('active', 'completed', etc.).
        """
        conn = _get_conn()
        now = datetime.now(timezone.utc).isoformat()

        # Check if already claimed.
        existing = conn.execute(
            "SELECT * FROM claims WHERE task_id = ?", (task_id,)
        ).fetchone()

        if existing:
            return {
                "success": False,
                "holder": existing["claimed_by"],
                "claimed_at": existing["claimed_at"],
                "status": existing["status"],
            }

        # Optimistic insert — the PRIMARY KEY constraint is the lock.
        try:
            conn.execute(
                """INSERT INTO claims
                   (task_id, claimed_by, claimed_at, status, criteria_json,
                    findings_json, verified)
                   VALUES (?, ?, ?, 'active', ?, '{}', 0)""",
                (
                    task_id,
                    by,
                    now,
                    json.dumps(criteria or {}, ensure_ascii=False),
                ),
            )
            conn.commit()
            return {
                "success": True,
                "holder": by,
                "claimed_at": now,
                "status": "active",
            }
        except sqlite3.IntegrityError:
            # Race: another thread inserted first.
            existing2 = conn.execute(
                "SELECT * FROM claims WHERE task_id = ?", (task_id,)
            ).fetchone()
            if existing2:
                return {
                    "success": False,
                    "holder": existing2["claimed_by"],
                    "claimed_at": existing2["claimed_at"],
                    "status": existing2["status"],
                }
            return {
                "success": False,
                "holder": "unknown",
                "claimed_at": "",
                "status": "error",
            }

    # ------------------------------------------------------------------
    # release
    # ------------------------------------------------------------------

    def release(self, task_id: str, by: str) -> bool:
        """Release a claim (only the holder can release).

        Returns True if the claim was deleted.
        """
        conn = _get_conn()
        cur = conn.execute(
            "DELETE FROM claims WHERE task_id = ? AND claimed_by = ?",
            (task_id, by),
        )
        conn.commit()
        return cur.rowcount > 0

    def force_release(self, task_id: str) -> bool:
        """Force-release a claim regardless of holder (Auditor action).

        Returns True if a row was deleted.
        """
        conn = _get_conn()
        cur = conn.execute("DELETE FROM claims WHERE task_id = ?", (task_id,))
        conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # update findings / status
    # ------------------------------------------------------------------

    def update_findings(
        self, task_id: str, findings: dict, verified: bool = False
    ) -> bool:
        """Update findings for a claimed task.

        Returns True if a row was updated.
        """
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE claims SET findings_json = ?, verified = ? WHERE task_id = ?",
            (json.dumps(findings, ensure_ascii=False), int(verified), task_id),
        )
        conn.commit()
        return cur.rowcount > 0

    def update_status(self, task_id: str, status: str) -> bool:
        """Update the status of a claim (e.g. 'completed', 'failed').

        Returns True if a row was updated.
        """
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE claims SET status = ? WHERE task_id = ?",
            (status, task_id),
        )
        conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # query
    # ------------------------------------------------------------------

    def get_active_claims(self) -> list[dict]:
        """Return all active (unresolved) claims, newest first."""
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM claims WHERE status = 'active' ORDER BY claimed_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_claim(self, task_id: str) -> dict | None:
        """Return claim details for a task, or None if not claimed."""
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM claims WHERE task_id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    def query(
        self,
        claimed_by: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Query claims with optional filters.

        Args:
            claimed_by: Filter by claim holder.
            status: Filter by claim status.
            limit: Max results.
            offset: Pagination offset.

        Returns:
            List of claim dicts.
        """
        conn = _get_conn()
        where: list[str] = []
        params: list = []

        if claimed_by:
            where.append("claimed_by = ?")
            params.append(claimed_by)
        if status:
            where.append("status = ?")
            params.append(status)

        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"SELECT * FROM claims {clause} ORDER BY claimed_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # digest
    # ------------------------------------------------------------------

    def claims_digest(self) -> str:
        """Produce a human-readable summary of active claims.

        Designed for injection into agent context windows so agents know
        which tasks are already being worked on.
        """
        active = self.get_active_claims()
        if not active:
            return "[No active claims — all tasks are available.]"

        lines: list[str] = [
            "## Active Claims (tasks already claimed — do NOT duplicate)",
            "",
        ]
        for c in active:
            verified_mark = " [verified]" if c["verified"] else ""
            lines.append(
                f"- **{c['task_id']}** claimed by "
                f"*{c['claimed_by']}* at {c['claimed_at']}{verified_mark}"
            )
            criteria = json.loads(c.get("criteria_json") or "{}")
            if criteria:
                lines.append(f"  criteria: {json.dumps(criteria)}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # stats
    # ------------------------------------------------------------------

    def count(self, status: str | None = None) -> int:
        """Return number of claims, optionally filtered by status."""
        conn = _get_conn()
        if status:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM claims WHERE status = ?", (status,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) as cnt FROM claims").fetchone()
        return row["cnt"] if row else 0
