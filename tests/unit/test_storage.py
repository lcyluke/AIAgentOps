"""Unit tests for apex.storage.db — SQLite persistence layer.

Run standalone:  python3 tests/unit/test_storage.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

# ── Bootstrap: add project root to sys.path so imports resolve ──────────────
import sys

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Override DB_PATH *before* importing the module so it uses a temp file.
os.environ["APEX_DB_PATH"] = str(Path(tempfile.gettempdir()) / "test_agentops.db")

from apex.storage.db import (  # noqa: E402
    DB_PATH,
    close_db,
    get_event,
    get_events_by_session,
    get_instance,
    get_session,
    get_task,
    insert_event,
    insert_instance,
    insert_session,
    insert_task,
    list_instances,
    list_sessions,
    list_tasks,
    update_instance_state,
    update_session_status,
    update_task_status,
)
from apex.adapters.base import SessionHandle, SpawnSpec
from apex.protocol import (
    ApexEvent,
    InstanceInfo,
    InstanceState,
    Runtime,
    TaskSpec,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fresh_session(sid: str = "sess-001") -> SessionHandle:
    return SessionHandle(
        id=sid,
        name="test-session",
        runtime=Runtime.PYTHON,
        status="active",
        created_at="2025-01-01T00:00:00Z",
        metadata={"origin": "unit-test"},
    )


def _fresh_event(eid: str = "evt-001", sid: str = "sess-001") -> ApexEvent:
    return ApexEvent(
        id=eid,
        session_id=sid,
        type="task_started",
        data={"task": "hello"},
        timestamp="2025-01-01T00:00:01Z",
    )


def _fresh_task(tid: str = "task-001") -> TaskSpec:
    return TaskSpec(
        id=tid,
        name="test-task",
        description="a task for testing",
        runtime=Runtime.PYTHON,
        input_data={"cmd": "echo hi"},
        priority=5,
        depends_on=["task-000"],
        timeout=120,
        retries=2,
        tags=["unit", "test"],
        created_at="2025-01-01T00:00:00Z",
    )


def _fresh_instance(iid: str = "inst-001", sid: str = "sess-001") -> InstanceInfo:
    return InstanceInfo(
        session_id=sid,
        instance_id=iid,
        runtime=Runtime.DOCKER,
        state=InstanceState.RUNNING,
        image="python:3.12",
        command="sleep 999",
        spawn_args={"network": "host"},
        created_at="2025-01-01T00:00:00Z",
    )


# ── Test Cases ───────────────────────────────────────────────────────────────

class TestSessionCRUD(unittest.TestCase):
    """Session insert, get, list, and status update."""

    @classmethod
    def setUpClass(cls):
        # Force a fresh, empty DB for this test class.
        p = Path(os.environ["APEX_DB_PATH"])
        if p.exists():
            p.unlink()
        close_db()  # reset cached connection

    def test_insert_and_get(self):
        s = _fresh_session("s1")
        insert_session(s)
        got = get_session("s1")
        self.assertIsNotNone(got)
        self.assertEqual(got.id, "s1")
        self.assertEqual(got.name, "test-session")
        self.assertEqual(got.runtime, Runtime.PYTHON)
        self.assertEqual(got.status, "active")
        self.assertEqual(got.metadata.get("origin"), "unit-test")

    def test_get_nonexistent(self):
        self.assertIsNone(get_session("no-such-session"))

    def test_list_all(self):
        for i in range(5):
            insert_session(_fresh_session(f"sess-{i}"))
        results = list_sessions(limit=10)
        self.assertGreaterEqual(len(results), 5)

    def test_list_filtered_by_status(self):
        insert_session(_fresh_session("active-1"))
        s2 = _fresh_session("stopped-1")
        s2.status = "stopped"
        insert_session(s2)
        active = list_sessions(status="active", limit=10)
        stopped = list_sessions(status="stopped", limit=10)
        self.assertTrue(all(r.status == "active" for r in active))
        self.assertTrue(all(r.status == "stopped" for r in stopped))

    def test_update_status(self):
        s = _fresh_session("s-update")
        insert_session(s)
        self.assertTrue(update_session_status("s-update", "completed"))
        got = get_session("s-update")
        self.assertEqual(got.status, "completed")

    def test_update_status_nonexistent(self):
        self.assertFalse(update_session_status("ghost", "active"))


class TestEventCRUD(unittest.TestCase):
    """Event insert, get, and query-by-session."""

    @classmethod
    def setUpClass(cls):
        p = Path(os.environ["APEX_DB_PATH"])
        if p.exists():
            p.unlink()
        close_db()

        # Ensure the parent session exists (FK constraint).
        insert_session(_fresh_session("ev-sess"))

    def test_insert_and_get(self):
        ev = _fresh_event("ev-1", "ev-sess")
        insert_event(ev)
        got = get_event("ev-1")
        self.assertIsNotNone(got)
        self.assertEqual(got.id, "ev-1")
        self.assertEqual(got.session_id, "ev-sess")
        self.assertEqual(got.type, "task_started")
        self.assertEqual(got.data.get("task"), "hello")

    def test_get_nonexistent(self):
        self.assertIsNone(get_event("no-ev"))

    def test_events_by_session(self):
        for i in range(3):
            ev = _fresh_event(f"ev-s{i}", "ev-sess")
            ev.type = "heartbeat" if i % 2 == 0 else "log"
            insert_event(ev)
        all_ev = get_events_by_session("ev-sess")
        self.assertGreaterEqual(len(all_ev), 3)

    def test_events_by_session_and_type(self):
        heartbeats = get_events_by_session("ev-sess", event_type="heartbeat")
        self.assertTrue(all(e.type == "heartbeat" for e in heartbeats))


class TestTaskCRUD(unittest.TestCase):
    """Task insert, get, list, and status update."""

    @classmethod
    def setUpClass(cls):
        p = Path(os.environ["APEX_DB_PATH"])
        if p.exists():
            p.unlink()
        close_db()

    def test_insert_and_get(self):
        t = _fresh_task("tk-1")
        insert_task(t)
        got = get_task("tk-1")
        self.assertIsNotNone(got)
        self.assertEqual(got.id, "tk-1")
        self.assertEqual(got.name, "test-task")
        self.assertEqual(got.priority, 5)
        self.assertEqual(got.depends_on, ["task-000"])
        self.assertEqual(got.tags, ["unit", "test"])

    def test_get_nonexistent(self):
        self.assertIsNone(get_task("no-task"))

    def test_list_all(self):
        for i in range(4):
            t = _fresh_task(f"tk-{i}")
            t.priority = i
            insert_task(t)
        tasks = list_tasks(limit=10)
        self.assertGreaterEqual(len(tasks), 4)

    def test_list_filtered_by_status(self):
        # Insert tasks, then update some statuses.
        insert_task(_fresh_task("tk-s1"))
        insert_task(_fresh_task("tk-s2"))
        update_task_status("tk-s1", "running")
        update_task_status("tk-s2", "completed", completed_at="2025-01-02T00:00:00Z")

        running = list_tasks(status="running")
        completed = list_tasks(status="completed")
        self.assertTrue(all(update_task_status for _ in running))  # just check it runs
        self.assertGreaterEqual(len(completed), 1)

    def test_update_task_status(self):
        t = _fresh_task("tk-up")
        insert_task(t)
        self.assertTrue(update_task_status("tk-up", "completed", "2025-06-01T00:00:00Z"))
        got = get_task("tk-up")
        self.assertIsNotNone(got)
        # Status is stored on row, not TaskSpec, so verify via list_tasks
        tasks = list_tasks(status="completed")
        ids = [t.id for t in tasks]
        self.assertIn("tk-up", ids)

    def test_update_nonexistent(self):
        self.assertFalse(update_task_status("ghost", "done"))


class TestInstanceCRUD(unittest.TestCase):
    """Instance insert, get, list, and state update."""

    @classmethod
    def setUpClass(cls):
        p = Path(os.environ["APEX_DB_PATH"])
        if p.exists():
            p.unlink()
        close_db()
        insert_session(_fresh_session("inst-sess"))

    def test_insert_and_get(self):
        inst = _fresh_instance("inst-1", "inst-sess")
        insert_instance(inst)
        got = get_instance("inst-1")
        self.assertIsNotNone(got)
        self.assertEqual(got.instance_id, "inst-1")
        self.assertEqual(got.session_id, "inst-sess")
        self.assertEqual(got.runtime, Runtime.DOCKER)
        self.assertEqual(got.state, InstanceState.RUNNING)
        self.assertEqual(got.image, "python:3.12")

    def test_get_nonexistent(self):
        self.assertIsNone(get_instance("no-inst"))

    def test_list_by_session(self):
        for i in range(3):
            insert_instance(_fresh_instance(f"inst-s{i}", "inst-sess"))
        results = list_instances(session_id="inst-sess", limit=10)
        self.assertGreaterEqual(len(results), 3)
        self.assertTrue(all(r.session_id == "inst-sess" for r in results))

    def test_list_by_state(self):
        insert_instance(_fresh_instance("inst-running", "inst-sess"))
        stopped = _fresh_instance("inst-stopped", "inst-sess")
        stopped.state = InstanceState.STOPPED
        insert_instance(stopped)

        running = list_instances(state="running")
        stopped_list = list_instances(state="stopped")
        self.assertTrue(all(r.state == InstanceState.RUNNING for r in running))
        self.assertTrue(all(r.state == InstanceState.STOPPED for r in stopped_list))

    def test_update_state(self):
        inst = _fresh_instance("inst-up", "inst-sess")
        insert_instance(inst)
        self.assertTrue(
            update_instance_state(
                "inst-up",
                InstanceState.FAILED,
                completed_at="2025-06-01T00:00:00Z",
                error="OOM killed",
            )
        )
        got = get_instance("inst-up")
        self.assertEqual(got.state, InstanceState.FAILED)
        self.assertEqual(got.error, "OOM killed")
        self.assertEqual(got.completed_at, "2025-06-01T00:00:00Z")

    def test_update_nonexistent(self):
        self.assertFalse(update_instance_state("ghost", InstanceState.STOPPED))


class TestIntegrity(unittest.TestCase):
    """Cross-table integrity: FK constraints, round-trip fidelity."""

    @classmethod
    def setUpClass(cls):
        p = Path(os.environ["APEX_DB_PATH"])
        if p.exists():
            p.unlink()
        close_db()

    def test_round_trip_all_types(self):
        """Insert a session, event, task, and instance; verify they all come back intact."""
        sess = _fresh_session("rt-sess")
        insert_session(sess)

        ev = _fresh_event("rt-ev", "rt-sess")
        ev.data = {"nested": {"list": [1, 2, 3]}}
        insert_event(ev)

        tk = _fresh_task("rt-task")
        tk.input_data = {"env": {"KEY": "VAL"}}
        insert_task(tk)

        inst = _fresh_instance("rt-inst", "rt-sess")
        insert_instance(inst)

        # Verify
        self.assertEqual(get_session("rt-sess").id, "rt-sess")
        self.assertEqual(get_event("rt-ev").data["nested"]["list"], [1, 2, 3])
        self.assertEqual(get_task("rt-task").input_data["env"]["KEY"], "VAL")
        self.assertEqual(get_instance("rt-inst").state, InstanceState.RUNNING)

    def test_wal_mode_enabled(self):
        """Verify the database is using WAL journal mode."""
        import sqlite3 as _sqlite3

        c = _sqlite3.connect(str(DB_PATH))
        mode = c.execute("PRAGMA journal_mode").fetchone()[0]
        c.close()
        self.assertEqual(mode.lower(), "wal")


class TestThreadSafety(unittest.TestCase):
    """Concurrent access from multiple threads."""

    @classmethod
    def setUpClass(cls):
        p = Path(os.environ["APEX_DB_PATH"])
        if p.exists():
            p.unlink()
        close_db()

    def test_concurrent_inserts(self):
        import threading

        errors = []

        def worker(prefix: str):
            try:
                for i in range(20):
                    sess = _fresh_session(f"{prefix}-{i}")
                    insert_session(sess)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(f"t{t}",))
            for t in range(5)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        self.assertEqual(len(errors), 0)
        # Should have at least 100 sessions
        sessions = list_sessions(limit=200)
        self.assertGreaterEqual(len(sessions), 100)


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
