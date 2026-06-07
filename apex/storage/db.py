"""SQLite Persistence Layer — agentops.db

Stores sessions, events, tasks, and instances using the canonical protocol types
from apex.protocol and apex.adapters.base.

Thread-safe via WAL mode.  Default path: ~/.apex/agentops.db (override with
$APEX_DB_PATH).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from apex.adapters.base import SessionHandle, SpawnSpec
from apex.protocol import (
    ApexCommand,
    ApexEvent,
    CommandStatus,
    InstanceInfo,
    InstanceState,
    Runtime,
    TaskSpec,
    TaskStatus,
)


# ── Configuration ────────────────────────────────────────────────────────────

_DEFAULT_DB_PATH = Path.home() / ".apex" / "agentops.db"
DB_PATH = Path(os.environ.get("APEX_DB_PATH", str(_DEFAULT_DB_PATH)))


# ── Connection management (thread-safe) ──────────────────────────────────────

# Each thread gets its own sqlite3 connection.  sqlite3 objects can only be
# used by the thread that created them, even with check_same_thread=False
# (that flag only skips the *check*; it doesn't make the C library thread-safe).
# WAL mode allows concurrent readers + one writer across connections.
_local = threading.local()
_init_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """Return a thread-local connection.  Lazily opens + migrates."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    # Serialise initialisation so the schema is only created once.
    with _init_lock:
        conn = getattr(_local, "conn", None)
        if conn is not None:
            return conn
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        _init_db(conn)
        _local.conn = conn
        return conn


def _init_db(conn: sqlite3.Connection) -> None:
    """Create tables and indexes if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id          TEXT PRIMARY KEY,
            name        TEXT    DEFAULT '',
            runtime     TEXT    DEFAULT 'python',
            status      TEXT    DEFAULT 'active',
            created_at  TEXT    DEFAULT '',
            metadata    TEXT    DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS events (
            id          TEXT PRIMARY KEY,
            session_id  TEXT    NOT NULL,
            type        TEXT    NOT NULL,
            data        TEXT    DEFAULT '{}',
            timestamp   TEXT    DEFAULT '',
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
        CREATE INDEX IF NOT EXISTS idx_events_type    ON events(type);

        CREATE TABLE IF NOT EXISTS tasks (
            id          TEXT PRIMARY KEY,
            name        TEXT    NOT NULL,
            description TEXT    DEFAULT '',
            runtime     TEXT    DEFAULT 'python',
            input_data  TEXT    DEFAULT '{}',
            priority    INTEGER DEFAULT 0,
            depends_on  TEXT    DEFAULT '[]',
            timeout     INTEGER DEFAULT 300,
            retries     INTEGER DEFAULT 0,
            tags        TEXT    DEFAULT '[]',
            status      TEXT    DEFAULT 'pending',
            created_at  TEXT    DEFAULT '',
            completed_at TEXT   DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

        CREATE TABLE IF NOT EXISTS instances (
            session_id  TEXT    NOT NULL,
            instance_id TEXT    PRIMARY KEY,
            runtime     TEXT    DEFAULT 'python',
            state       TEXT    DEFAULT 'pending',
            image       TEXT    DEFAULT '',
            command     TEXT    DEFAULT '',
            spawn_args  TEXT    DEFAULT '{}',
            created_at  TEXT    DEFAULT '',
            completed_at TEXT   DEFAULT '',
            error       TEXT    DEFAULT '',
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE INDEX IF NOT EXISTS idx_instances_session ON instances(session_id);
        CREATE INDEX IF NOT EXISTS idx_instances_state   ON instances(state);
    """)


def close_db() -> None:
    """Close the thread-local connection (useful for tests and clean shutdown)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


# ── Session CRUD ─────────────────────────────────────────────────────────────

def insert_session(session: SessionHandle) -> None:
    """Persist a session handle."""
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO sessions (id, name, runtime, status, created_at, metadata)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            session.id,
            session.name,
            session.runtime.value if isinstance(session.runtime, Runtime) else session.runtime,
            session.status,
            session.created_at,
            json.dumps(session.metadata, ensure_ascii=False),
        ),
    )
    conn.commit()


def get_session(session_id: str) -> Optional[SessionHandle]:
    """Retrieve a session by id."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    return _row_to_session_handle(row)


def list_sessions(
    status: Optional[str] = None,
    runtime: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[SessionHandle]:
    """List sessions, optionally filtered."""
    conn = _get_conn()
    where: list[str] = []
    params: list = []
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if runtime is not None:
        where.append("runtime = ?")
        params.append(runtime)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM sessions {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return [_row_to_session_handle(r) for r in rows]


def update_session_status(session_id: str, status: str) -> bool:
    """Update a session's status. Returns True if a row was changed."""
    conn = _get_conn()
    cur = conn.execute("UPDATE sessions SET status = ? WHERE id = ?", (status, session_id))
    conn.commit()
    return cur.rowcount > 0


def _row_to_session_handle(row: sqlite3.Row) -> SessionHandle:
    return SessionHandle(
        id=row["id"],
        name=row["name"],
        runtime=Runtime(row["runtime"]) if row["runtime"] else Runtime.PYTHON,
        status=row["status"],
        created_at=row["created_at"],
        metadata=json.loads(row["metadata"] or "{}"),
    )


# ── Event CRUD ───────────────────────────────────────────────────────────────

def insert_event(event: ApexEvent) -> None:
    """Persist an ApexEvent."""
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO events (id, session_id, type, data, timestamp)
           VALUES (?, ?, ?, ?, ?)""",
        (
            event.id,
            event.session_id,
            event.type,
            json.dumps(event.data, ensure_ascii=False),
            event.timestamp,
        ),
    )
    conn.commit()


def get_events_by_session(
    session_id: str,
    event_type: Optional[str] = None,
    limit: int = 500,
    offset: int = 0,
) -> list[ApexEvent]:
    """Return events for a session, newest first.  Optionally filter by type."""
    conn = _get_conn()
    if event_type:
        rows = conn.execute(
            """SELECT * FROM events
               WHERE session_id = ? AND type = ?
               ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
            (session_id, event_type, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM events WHERE session_id = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (session_id, limit, offset),
        ).fetchall()
    return [_row_to_event(r) for r in rows]


def get_event(event_id: str) -> Optional[ApexEvent]:
    """Retrieve a single event by id."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        return None
    return _row_to_event(row)


def _row_to_event(row: sqlite3.Row) -> ApexEvent:
    return ApexEvent(
        id=row["id"],
        session_id=row["session_id"],
        type=row["type"],
        data=json.loads(row["data"] or "{}"),
        timestamp=row["timestamp"],
    )


# ── Task CRUD ────────────────────────────────────────────────────────────────

def insert_task(task: TaskSpec) -> None:
    """Persist a TaskSpec."""
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO tasks
           (id, name, description, runtime, input_data, priority, depends_on,
            timeout, retries, tags, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (
            task.id,
            task.name,
            task.description,
            task.runtime.value if isinstance(task.runtime, Runtime) else task.runtime,
            json.dumps(task.input_data, ensure_ascii=False),
            task.priority,
            json.dumps(task.depends_on, ensure_ascii=False),
            task.timeout,
            task.retries,
            json.dumps(task.tags, ensure_ascii=False),
            task.created_at,
        ),
    )
    conn.commit()


def get_task(task_id: str) -> Optional[TaskSpec]:
    """Retrieve a task by id."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    return _row_to_task_spec(row)


def list_tasks(
    status: Optional[str] = None,
    runtime: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[TaskSpec]:
    """List tasks, optionally filtered by status and/or runtime."""
    conn = _get_conn()
    where: list[str] = []
    params: list = []
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if runtime is not None:
        where.append("runtime = ?")
        params.append(runtime)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM tasks {clause} ORDER BY priority DESC, created_at ASC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return [_row_to_task_spec(r) for r in rows]


def update_task_status(task_id: str, status: str, completed_at: str = "") -> bool:
    """Update task status (and optionally set completed_at). Returns True if changed."""
    conn = _get_conn()
    if completed_at:
        cur = conn.execute(
            "UPDATE tasks SET status = ?, completed_at = ? WHERE id = ?",
            (status, completed_at, task_id),
        )
    else:
        cur = conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
    conn.commit()
    return cur.rowcount > 0


def _row_to_task_spec(row: sqlite3.Row) -> TaskSpec:
    return TaskSpec(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        runtime=Runtime(row["runtime"]) if row["runtime"] else Runtime.PYTHON,
        input_data=json.loads(row["input_data"] or "{}"),
        priority=row["priority"],
        depends_on=json.loads(row["depends_on"] or "[]"),
        timeout=row["timeout"],
        retries=row["retries"],
        tags=json.loads(row["tags"] or "[]"),
        created_at=row["created_at"],
        # status/ completed_at are kept on the row but TaskSpec doesn't carry them;
        # we leave them queryable via list_tasks(filter).
    )


# ── Instance CRUD ────────────────────────────────────────────────────────────

def insert_instance(info: InstanceInfo) -> None:
    """Persist an InstanceInfo snapshot."""
    conn = _get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO instances
           (session_id, instance_id, runtime, state, image, command,
            spawn_args, created_at, completed_at, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            info.session_id,
            info.instance_id,
            info.runtime.value if isinstance(info.runtime, Runtime) else info.runtime,
            info.state.value if isinstance(info.state, InstanceState) else info.state,
            info.image,
            info.command,
            json.dumps(info.spawn_args, ensure_ascii=False),
            info.created_at,
            info.completed_at,
            info.error,
        ),
    )
    conn.commit()


def get_instance(instance_id: str) -> Optional[InstanceInfo]:
    """Retrieve an instance snapshot."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM instances WHERE instance_id = ?", (instance_id,)).fetchone()
    if row is None:
        return None
    return _row_to_instance_info(row)


def list_instances(
    session_id: Optional[str] = None,
    state: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[InstanceInfo]:
    """List instances, optionally filtered by session and/or state."""
    conn = _get_conn()
    where: list[str] = []
    params: list = []
    if session_id is not None:
        where.append("session_id = ?")
        params.append(session_id)
    if state is not None:
        where.append("state = ?")
        params.append(state)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM instances {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return [_row_to_instance_info(r) for r in rows]


def update_instance_state(
    instance_id: str,
    state: InstanceState,
    completed_at: str = "",
    error: str = "",
) -> bool:
    """Update an instance's state (and optionally completed_at + error). Returns True if changed."""
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE instances SET state = ?, completed_at = ?, error = ? WHERE instance_id = ?",
        (state.value if isinstance(state, InstanceState) else state, completed_at, error, instance_id),
    )
    conn.commit()
    return cur.rowcount > 0


def _row_to_instance_info(row: sqlite3.Row) -> InstanceInfo:
    return InstanceInfo(
        session_id=row["session_id"],
        instance_id=row["instance_id"],
        runtime=Runtime(row["runtime"]) if row["runtime"] else Runtime.PYTHON,
        state=InstanceState(row["state"]) if row["state"] else InstanceState.PENDING,
        image=row["image"],
        command=row["command"],
        spawn_args=json.loads(row["spawn_args"] or "{}"),
        created_at=row["created_at"],
        completed_at=row["completed_at"],
        error=row["error"],
    )
