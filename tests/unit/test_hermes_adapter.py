"""Unit tests for HermesAdapter — subprocess-based Hermes CLI integration.

Tests use mock subprocess to simulate hermes CLI commands and
mock urlopen for gateway health checks — no external deps required.

Run standalone:  python3 -m pytest tests/unit/test_hermes_adapter.py -v
Or:             python3 tests/unit/test_hermes_adapter.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

# ── Bootstrap: add project root to sys.path ─────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from apex.adapters.hermes import (
    DEFAULT_GATEWAY_PORT,
    GATEWAY_HEALTH_URL,
    HermesAdapter,
    InstanceStatus,
    SessionHandle,
    SpawnSpec,
)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _make_spawn_spec(
    agent: str = "test-agent",
    instance: str = "inst-001",
    brief: str = "Write a hello world function",
    cwd: str = "/tmp/test-hermes",
) -> SpawnSpec:
    return SpawnSpec(
        agent=agent,
        instance=instance,
        brief=brief,
        cwd=cwd,
    )


def _make_session_handle(
    name: str = "test-agent-abc12345",
    session_id: str = "abc12345",
) -> SessionHandle:
    return SessionHandle(
        name=name,
        session_id=session_id,
        runtime="hermes",
    )


def _mock_subprocess_run(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> mock.MagicMock:
    """Create a mock for subprocess.run returning the given values."""
    completed = mock.MagicMock()
    completed.returncode = returncode
    completed.stdout = stdout
    completed.stderr = stderr
    return completed


# ═══════════════════════════════════════════════════════════════════════════
# Initialization Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestHermesAdapterInit(unittest.TestCase):
    """Basic instantiation and attribute tests."""

    def test_name_attribute(self):
        adapter = HermesAdapter()
        self.assertEqual(adapter.name, "hermes")

    def test_default_hermes_bin(self):
        adapter = HermesAdapter()
        self.assertIn("hermes", adapter._hermes)

    def test_custom_gateway_port(self):
        adapter = HermesAdapter(gateway_port=9876)
        self.assertEqual(adapter._port, 9876)
        self.assertIn("9876", adapter._health_url)

    def test_session_registry_empty_initially(self):
        adapter = HermesAdapter()
        self.assertEqual(len(adapter._sessions), 0)

    def test_list_sessions_empty(self):
        adapter = HermesAdapter()
        self.assertEqual(adapter.list_sessions(), [])


# ═══════════════════════════════════════════════════════════════════════════
# Spawn Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestSpawn(unittest.TestCase):
    """spawn() creates a profile, starts gateway, and returns a handle."""

    def setUp(self):
        self.adapter = HermesAdapter(hermes_bin="/fake/hermes", check_binary=False)
        # Pre-seed the adapter with an already-ran subprocess mock
        self._mock_run_patcher = mock.patch("subprocess.run")
        self.mock_run = self._mock_run_patcher.start()
        self.mock_run.return_value = _mock_subprocess_run(returncode=0)

        # Mock urlopen for gateway health
        self._mock_urlopen_patcher = mock.patch("urllib.request.urlopen")
        self.mock_urlopen = self._mock_urlopen_patcher.start()
        self.mock_urlopen.return_value.status = 200

    def tearDown(self):
        self._mock_run_patcher.stop()
        self._mock_urlopen_patcher.stop()
        # Clean up sessions
        self.adapter.dispose_all()

    def test_spawn_returns_session_handle(self):
        spec = _make_spawn_spec()
        h = self.adapter.spawn(spec)

        self.assertIsInstance(h, SessionHandle)
        self.assertEqual(h.runtime, "hermes")
        self.assertTrue(len(h.session_id) > 0)
        self.assertIn(spec.agent, h.name)

    def test_spawn_creates_profile(self):
        spec = _make_spawn_spec()
        self.adapter.spawn(spec)

        # Verify hermes profile create was called
        calls = self.mock_run.call_args_list
        profile_create_call = None
        for call in calls:
            args = call[0][0] if call[0] else []
            if len(args) >= 3 and args[1] == "profile" and args[2] == "create":
                profile_create_call = call
                break

        self.assertIsNotNone(
            profile_create_call,
            "hermes profile create should have been called",
        )

    def test_spawn_starts_gateway(self):
        spec = _make_spawn_spec()
        self.adapter.spawn(spec)

        # Verify hermes gateway start was called
        calls = self.mock_run.call_args_list
        gateway_start_call = None
        for call in calls:
            args = call[0][0] if call[0] else []
            if len(args) >= 3 and args[1] == "gateway" and args[2] == "start":
                gateway_start_call = call
                break

        self.assertIsNotNone(
            gateway_start_call,
            "hermes gateway start should have been called",
        )

    def test_spawn_registers_session(self):
        spec = _make_spawn_spec()
        h = self.adapter.spawn(spec)

        self.assertIn(h.session_id, self.adapter._sessions)
        entry = self.adapter._sessions[h.session_id]
        self.assertEqual(entry["handle"], h)
        self.assertEqual(entry["spec"], spec)
        self.assertIn("profile_name", entry)
        self.assertEqual(entry["prompts"], 0)

    def test_spawn_raises_on_profile_create_failure(self):
        self.mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd=["hermes", "profile", "create", "x"],
            stderr="profile already exists",
        )

        spec = _make_spawn_spec()
        with self.assertRaises(RuntimeError) as ctx:
            self.adapter.spawn(spec)
        self.assertIn("profile", str(ctx.exception).lower())

    def test_spawn_raises_on_missing_hermes_binary(self):
        adapter = HermesAdapter(hermes_bin="/nonexistent/hermes")
        spec = _make_spawn_spec()
        with self.assertRaises(RuntimeError) as ctx:
            adapter.spawn(spec)
        self.assertIn("not found", str(ctx.exception).lower())

    def test_spawn_unique_session_ids(self):
        spec1 = _make_spawn_spec(agent="agent-a")
        spec2 = _make_spawn_spec(agent="agent-b")

        # Need to reset mock to return success for each call
        h1 = self.adapter.spawn(spec1)
        h2 = self.adapter.spawn(spec2)

        self.assertNotEqual(h1.session_id, h2.session_id)
        self.assertNotEqual(h1.name, h2.name)


# ═══════════════════════════════════════════════════════════════════════════
# Prompt / Steer / Abort Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestPromptSteerAbort(unittest.TestCase):
    """prompt(), steer(), and abort() operations."""

    def setUp(self):
        self.adapter = HermesAdapter(hermes_bin="/fake/hermes", check_binary=False)

        self._mock_run_patcher = mock.patch("subprocess.run")
        self.mock_run = self._mock_run_patcher.start()
        self.mock_run.return_value = _mock_subprocess_run(returncode=0)

        self._mock_urlopen_patcher = mock.patch("urllib.request.urlopen")
        self.mock_urlopen = self._mock_urlopen_patcher.start()
        self.mock_urlopen.return_value.status = 200

        # Spawn a session to use in tests
        self.spec = _make_spawn_spec()
        self.handle = self.adapter.spawn(self.spec)

    def tearDown(self):
        self._mock_run_patcher.stop()
        self._mock_urlopen_patcher.stop()
        self.adapter.dispose_all()

    def test_prompt_calls_hermes_chat(self):
        self.adapter.prompt(self.handle, "What is Python?")

        # Find the chat call
        chat_calls = [
            c for c in self.mock_run.call_args_list
            if len(c[0][0]) >= 4
            and c[0][0][1] == "chat"
            and c[0][0][2] == "-q"
        ]
        self.assertGreater(len(chat_calls), 0, "hermes chat -q should have been called")
        # Check the prompt text was passed
        last_chat = chat_calls[-1]
        self.assertEqual(last_chat[0][0][3], "What is Python?")

    def test_prompt_increments_counter(self):
        self.adapter.prompt(self.handle, "question 1")
        self.adapter.prompt(self.handle, "question 2")

        entry = self.adapter._sessions[self.handle.session_id]
        self.assertEqual(entry["prompts"], 2)

    def test_prompt_raises_keyerror_for_unknown_session(self):
        bogus = SessionHandle(
            name="ghost",
            session_id="no-such-session",
        )
        with self.assertRaises(KeyError):
            self.adapter.prompt(bogus, "hello")

    def test_steer_calls_prompt_with_prefix(self):
        self.adapter.steer(self.handle, "focus on testing")

        chat_calls = [
            c for c in self.mock_run.call_args_list
            if len(c[0][0]) >= 4
            and c[0][0][1] == "chat"
            and c[0][0][2] == "-q"
        ]
        self.assertGreater(len(chat_calls), 0)
        last_chat = chat_calls[-1]
        self.assertIn("[STEER]", last_chat[0][0][3])
        self.assertIn("focus on testing", last_chat[0][0][3])

    def test_steer_increments_prompt_counter(self):
        initial = self.adapter._sessions[self.handle.session_id]["prompts"]
        self.adapter.steer(self.handle, "guidance")
        entry = self.adapter._sessions[self.handle.session_id]
        self.assertEqual(entry["prompts"], initial + 1)

    def test_abort_no_running_process(self):
        # Should not raise when no chat process is running
        self.adapter.abort(self.handle)

    def test_abort_kills_running_process(self):
        # Simulate a running chat process
        mock_proc = mock.MagicMock()
        mock_proc.poll.return_value = None  # Still running
        self.adapter._sessions[self.handle.session_id]["chat_process"] = mock_proc

        self.adapter.abort(self.handle)

        mock_proc.terminate.assert_called_once()

    def test_abort_force_kills_on_timeout(self):
        mock_proc = mock.MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="hermes", timeout=5)
        self.adapter._sessions[self.handle.session_id]["chat_process"] = mock_proc

        self.adapter.abort(self.handle)

        mock_proc.kill.assert_called_once()

    def test_abort_clears_chat_process(self):
        mock_proc = mock.MagicMock()
        mock_proc.poll.return_value = None
        self.adapter._sessions[self.handle.session_id]["chat_process"] = mock_proc

        self.adapter.abort(self.handle)

        self.assertIsNone(
            self.adapter._sessions[self.handle.session_id]["chat_process"],
        )


# ═══════════════════════════════════════════════════════════════════════════
# Status Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestStatus(unittest.TestCase):
    """status() returns InstanceStatus based on gateway health."""

    def setUp(self):
        self.adapter = HermesAdapter(hermes_bin="/fake/hermes", check_binary=False)

        self._mock_run_patcher = mock.patch("subprocess.run")
        self.mock_run = self._mock_run_patcher.start()
        self.mock_run.return_value = _mock_subprocess_run(returncode=0)

        self._mock_urlopen_patcher = mock.patch("urllib.request.urlopen")
        self.mock_urlopen = self._mock_urlopen_patcher.start()
        self.mock_urlopen.return_value.status = 200

        # Spawn a session
        self.spec = _make_spawn_spec()
        self.handle = self.adapter.spawn(self.spec)

    def tearDown(self):
        self._mock_run_patcher.stop()
        self._mock_urlopen_patcher.stop()
        self.adapter.dispose_all()

    def test_status_returns_instance_status(self):
        st = self.adapter.status(self.handle)

        self.assertIsInstance(st, InstanceStatus)
        self.assertIn(st.state, ("running", "stopped", "disposed"))
        self.assertTrue(len(st.last_event_ts) > 0)

    def test_status_running_when_gateway_healthy(self):
        self.mock_urlopen.return_value.status = 200

        st = self.adapter.status(self.handle)

        self.assertEqual(st.state, "running")
        self.assertIsNotNone(st.detail)
        self.assertIn("up", st.detail)  # type: ignore[arg-type]

    def test_status_stopped_when_gateway_unhealthy(self):
        from urllib.error import URLError

        self.mock_urlopen.side_effect = URLError("connection refused")

        st = self.adapter.status(self.handle)

        self.assertEqual(st.state, "stopped")
        self.assertIsNotNone(st.detail)
        self.assertIn("down", st.detail)  # type: ignore[arg-type]

    def test_status_disposed_for_unknown_session(self):
        bogus = SessionHandle(
            name="ghost",
            session_id="no-such-session",
        )
        st = self.adapter.status(bogus)

        self.assertEqual(st.state, "disposed")
        self.assertIsNotNone(st.detail)
        self.assertIn("not found", st.detail)  # type: ignore[arg-type]

    def test_status_includes_prompts_count(self):
        self.adapter.prompt(self.handle, "test prompt")
        st = self.adapter.status(self.handle)

        self.assertIsNotNone(st.detail)
        self.assertIn("prompts: 1", st.detail)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════
# Dispose Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestDispose(unittest.TestCase):
    """dispose() stops gateway and deletes profile."""

    def setUp(self):
        self.adapter = HermesAdapter(hermes_bin="/fake/hermes", check_binary=False)

        self._mock_run_patcher = mock.patch("subprocess.run")
        self.mock_run = self._mock_run_patcher.start()
        self.mock_run.return_value = _mock_subprocess_run(returncode=0)

        self._mock_urlopen_patcher = mock.patch("urllib.request.urlopen")
        self.mock_urlopen = self._mock_urlopen_patcher.start()
        self.mock_urlopen.return_value.status = 200

        # Spawn a session
        self.spec = _make_spawn_spec()
        self.handle = self.adapter.spawn(self.spec)

    def tearDown(self):
        self._mock_run_patcher.stop()
        self._mock_urlopen_patcher.stop()
        self.adapter.dispose_all()

    def test_dispose_removes_session(self):
        sid = self.handle.session_id
        self.assertIn(sid, self.adapter._sessions)

        self.adapter.dispose(self.handle)

        self.assertNotIn(sid, self.adapter._sessions)

    def test_dispose_stops_gateway(self):
        self.adapter.dispose(self.handle)

        calls = self.mock_run.call_args_list
        stop_calls = [
            c for c in calls
            if len(c[0][0]) >= 3
            and c[0][0][1] == "gateway"
            and c[0][0][2] == "stop"
        ]
        self.assertGreater(
            len(stop_calls), 0,
            "hermes gateway stop should have been called",
        )

    def test_dispose_deletes_profile(self):
        self.adapter.dispose(self.handle)

        calls = self.mock_run.call_args_list
        delete_calls = [
            c for c in calls
            if len(c[0][0]) >= 3
            and c[0][0][1] == "profile"
            and c[0][0][2] == "delete"
        ]
        self.assertGreater(
            len(delete_calls), 0,
            "hermes profile delete should have been called",
        )

    def test_dispose_idempotent(self):
        self.adapter.dispose(self.handle)
        # Should not raise
        self.adapter.dispose(self.handle)

    def test_dispose_all(self):
        spec2 = _make_spawn_spec(agent="agent-2")
        self.adapter.spawn(spec2)

        count = self.adapter.dispose_all()

        self.assertEqual(count, 2)
        self.assertEqual(len(self.adapter._sessions), 0)


# ═══════════════════════════════════════════════════════════════════════════
# Async Chat Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestAsyncChat(unittest.TestCase):
    """chat_async() and chat_result()."""

    def setUp(self):
        self.adapter = HermesAdapter(hermes_bin="/fake/hermes", check_binary=False)

        self._mock_run_patcher = mock.patch("subprocess.run")
        self.mock_run = self._mock_run_patcher.start()
        self.mock_run.return_value = _mock_subprocess_run(returncode=0)

        self._mock_popen_patcher = mock.patch("subprocess.Popen")
        self.mock_popen = self._mock_popen_patcher.start()

        self._mock_urlopen_patcher = mock.patch("urllib.request.urlopen")
        self.mock_urlopen = self._mock_urlopen_patcher.start()
        self.mock_urlopen.return_value.status = 200

        self.spec = _make_spawn_spec()
        self.handle = self.adapter.spawn(self.spec)

    def tearDown(self):
        self._mock_run_patcher.stop()
        self._mock_popen_patcher.stop()
        self._mock_urlopen_patcher.stop()
        self.adapter.dispose_all()

    def test_chat_async_starts_subprocess(self):
        mock_proc = mock.MagicMock()
        self.mock_popen.return_value = mock_proc

        proc = self.adapter.chat_async(self.handle, "async question")

        self.assertEqual(proc, mock_proc)
        self.mock_popen.assert_called_once()
        # Check the stored process
        self.assertEqual(
            self.adapter._sessions[self.handle.session_id]["chat_process"],
            mock_proc,
        )

    def test_chat_async_increments_prompts(self):
        self.mock_popen.return_value = mock.MagicMock()
        self.adapter.chat_async(self.handle, "query")
        entry = self.adapter._sessions[self.handle.session_id]
        self.assertEqual(entry["prompts"], 1)

    def test_chat_result_returns_output(self):
        mock_proc = mock.MagicMock()
        mock_proc.communicate.return_value = ("Hello World", "")
        mock_proc.returncode = 0
        self.adapter._sessions[self.handle.session_id]["chat_process"] = mock_proc

        exit_code, stdout, stderr = self.adapter.chat_result(self.handle)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout, "Hello World")
        self.assertEqual(stderr, "")

    def test_chat_result_clears_process(self):
        mock_proc = mock.MagicMock()
        mock_proc.communicate.return_value = ("ok", "")
        mock_proc.returncode = 0
        self.adapter._sessions[self.handle.session_id]["chat_process"] = mock_proc

        self.adapter.chat_result(self.handle)

        self.assertIsNone(
            self.adapter._sessions[self.handle.session_id]["chat_process"],
        )

    def test_chat_result_raises_when_no_process(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.adapter.chat_result(self.handle)
        self.assertIn("No async", str(ctx.exception))

    def test_chat_result_kills_on_timeout(self):
        mock_proc = mock.MagicMock()
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(
            cmd="hermes", timeout=1,
        )
        self.adapter._sessions[self.handle.session_id]["chat_process"] = mock_proc

        exit_code, stdout, stderr = self.adapter.chat_result(self.handle, timeout=0.1)

        mock_proc.kill.assert_called_once()
        self.assertEqual(exit_code, -1)


# ═══════════════════════════════════════════════════════════════════════════
# Gateway Health Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestGatewayHealth(unittest.TestCase):
    """_gateway_healthy() and _wait_for_gateway()."""

    def setUp(self):
        self.adapter = HermesAdapter(hermes_bin="/fake/hermes", check_binary=False)

    def test_gateway_healthy_true_on_200(self):
        with mock.patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value.status = 200
            self.assertTrue(self.adapter._gateway_healthy())

    def test_gateway_healthy_false_on_connection_refused(self):
        from urllib.error import URLError

        with mock.patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = URLError("connection refused")
            self.assertFalse(self.adapter._gateway_healthy())

    def test_gateway_healthy_false_on_non_200(self):
        with mock.patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value.status = 500
            self.assertFalse(self.adapter._gateway_healthy())

    def test_wait_for_gateway_success(self):
        with mock.patch.object(self.adapter, "_gateway_healthy") as mock_health:
            mock_health.return_value = True
            # Should not raise
            self.adapter._wait_for_gateway(timeout=5)

    def test_wait_for_gateway_timeout(self):
        with mock.patch.object(self.adapter, "_gateway_healthy") as mock_health:
            mock_health.return_value = False
            with self.assertRaises(RuntimeError) as ctx:
                self.adapter._wait_for_gateway(timeout=0.5)
            self.assertIn("healthy", str(ctx.exception).lower())


# ═══════════════════════════════════════════════════════════════════════════
# Types Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestTypes(unittest.TestCase):
    """Verify adapter-level types."""

    def test_session_handle_defaults(self):
        h = SessionHandle(name="test", session_id="sid-001")
        self.assertEqual(h.runtime, "hermes")
        self.assertEqual(h.tmux_session, "")

    def test_instance_status_optional_detail(self):
        st = InstanceStatus(
            state="running",
            last_event_ts="2025-01-01T00:00:00Z",
        )
        self.assertIsNone(st.detail)

    def test_spawn_spec_defaults(self):
        spec = SpawnSpec(
            agent="a",
            instance="i",
            brief="b",
            cwd="/tmp",
        )
        self.assertEqual(spec.env, {})
        self.assertEqual(spec.allowed_tools, [])
        self.assertEqual(spec.max_steps, 20)


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
