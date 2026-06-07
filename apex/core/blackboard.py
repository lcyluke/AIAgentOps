"""Blackboard — shared cross-agent knowledge exchange (§6.3).

Provides a persistent, queryable store for conclusions written by agents and
verified by the Auditor.  Agents only "request write" via stop events; the
Auditor (apexd) is the single-writer (INV-3).

Table: blackboard  (stored in the same SQLite DB as apex.storage.db)
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
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
    """Create blackboard table and indexes if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS blackboard (
            id          TEXT PRIMARY KEY,
            conclusion  TEXT    NOT NULL,
            author      TEXT    NOT NULL DEFAULT '',
            verified    INTEGER NOT NULL DEFAULT 0,
            session_id  TEXT    DEFAULT '',
            created_at  TEXT    DEFAULT '',
            metadata    TEXT    DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_blackboard_author
            ON blackboard(author);
        CREATE INDEX IF NOT EXISTS idx_blackboard_verified
            ON blackboard(verified);
        CREATE INDEX IF NOT EXISTS idx_blackboard_created
            ON blackboard(created_at);
    """)


def _close_conn() -> None:
    """Close the thread-local connection (useful for tests)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


# ── Blackboard ───────────────────────────────────────────────────────────────

class Blackboard:
    """Shared blackboard for cross-agent knowledge exchange.

    Only the Auditor (apexd) writes directly.  Agents request writes via the
    stop-event protocol and the Auditor commits verified conclusions.
    """

    # ------------------------------------------------------------------
    # write
    # ------------------------------------------------------------------

    def write(
        self,
        conclusion: str,
        author: str,
        verified: bool = False,
        session_id: str = "",
        metadata: dict | None = None,
    ) -> str:
        """Store a conclusion on the blackboard.

        Args:
            conclusion: The knowledge claim / conclusion text.
            author: Agent or role that authored the conclusion.
            verified: Whether the Auditor has verified this conclusion.
            session_id: Associated session (optional).
            metadata: Arbitrary JSON-serializable extra data.

        Returns:
            The entry id (UUID).
        """
        entry_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        conn = _get_conn()
        conn.execute(
            """INSERT INTO blackboard
               (id, conclusion, author, verified, session_id, created_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                entry_id,
                conclusion,
                author,
                int(verified),
                session_id,
                created_at,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
        return entry_id

    # ------------------------------------------------------------------
    # digest
    # ------------------------------------------------------------------

    def digest(
        self,
        exclude_author: str | None = None,
        max_tokens: int = 800,
        limit: int = 50,
    ) -> str:
        """Produce a human-readable summary of recent conclusions.

        Designed for injection into other agents' context windows so they
        can benefit from cross-agent knowledge without re-discovering it.

        Args:
            exclude_author: If set, omit entries from this author (to avoid
                            an agent seeing its own conclusions as new info).
            max_tokens: Soft cap on output length (~4 chars/token for English).
            limit: Max number of entries to include.

        Returns:
            A Markdown-ish digest string ready for prompt injection.
        """
        conn = _get_conn()
        if exclude_author:
            rows = conn.execute(
                """SELECT * FROM blackboard
                   WHERE author != ?
                   ORDER BY created_at DESC LIMIT ?""",
                (exclude_author, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM blackboard ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

        if not rows:
            return "[Blackboard is empty — no conclusions recorded yet.]"

        lines: list[str] = [
            "## Blackboard Digest (cross-agent knowledge)",
            "",
        ]
        for r in rows:
            verified_mark = " [verified]" if r["verified"] else ""
            entry = f"- **{r['author']}**{verified_mark}: {r['conclusion']}"
            if r["session_id"]:
                entry += f"  _(session: {r['session_id']})_"
            lines.append(entry)

        result = "\n".join(lines)

        # Rough token cap: ~4 chars per token for English prose.
        char_limit = max_tokens * 4
        if len(result) > char_limit:
            result = result[:char_limit] + "\n... [truncated for token budget]"
        return result

    # ------------------------------------------------------------------
    # query
    # ------------------------------------------------------------------

    def query(
        self,
        what: str = "",
        filter_author: str | None = None,
        verified_only: bool = False,
        session_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Query the blackboard with optional filters.

        Args:
            what: Substring to search in conclusion text.
            filter_author: Only return entries from this author.
            verified_only: Only return verified entries.
            session_id: Only return entries for a specific session.
            limit: Max entries to return.
            offset: Pagination offset.

        Returns:
            List of entry dicts (all columns).
        """
        conn = _get_conn()
        where: list[str] = []
        params: list = []

        if filter_author:
            where.append("author = ?")
            params.append(filter_author)
        if verified_only:
            where.append("verified = 1")
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if what:
            where.append("conclusion LIKE ?")
            params.append(f"%{what}%")

        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = conn.execute(
            f"SELECT * FROM blackboard {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # verify / mark
    # ------------------------------------------------------------------

    def mark_verified(self, entry_id: str) -> bool:
        """Mark a conclusion as verified (Auditor action).

        Returns True if a row was updated.
        """
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE blackboard SET verified = 1 WHERE id = ?", (entry_id,)
        )
        conn.commit()
        return cur.rowcount > 0

    def delete_entry(self, entry_id: str) -> bool:
        """Remove an entry from the blackboard.

        Returns True if a row was deleted.
        """
        conn = _get_conn()
        cur = conn.execute("DELETE FROM blackboard WHERE id = ?", (entry_id,))
        conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # stats / count
    # ------------------------------------------------------------------

    def count(self, verified_only: bool = False) -> int:
        """Return total number of entries."""
        conn = _get_conn()
        if verified_only:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM blackboard WHERE verified = 1"
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) as cnt FROM blackboard").fetchone()
        return row["cnt"] if row else 0
