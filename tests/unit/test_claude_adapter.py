"""
Unit tests for apex.adapters.claude — ClaudeCodeAdapter.

Run standalone:
  python3 -m pytest tests/unit/test_claude_adapter.py -v

Or:
  python3 tests/unit/test_claude_adapter.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Bootstrap: add project root to sys.path so imports resolve ──────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from apex.adapters.claude import (
    CLAUDE_BIN,
    TMUX_BIN,
    ClaudeCodeAdapter,
    ClaudeSession,
    _parse_claude_json_output,
    _tmux_has_session,
    _tmux_send_keys,
    _tmux_new_session,
    _tmux_kill_session,
    _tmux_capture_pane,
    _run,
)
from apex.adapters.base import SessionHandle
from apex.protocol import InstanceState


# ── Helpers ──────────────────────────────────────────────────────────────────


class TestParseClaudeJsonOutput(unittest.TestCase):
    """Tests for _parse_claude_json_output helper."""

    def test_single_json_line(self):
        output = '{"type": "assistant", "content": "Hello"}'
        result = _parse_claude_json_output(output)
        self.assertEqual(result["type"], "assistant")
        self.assertEqual(result["content"], "Hello")

    def test_multiple_json_lines_returns_last(self):
        output = (
            '{"type": "system", "content": "init"}\n'
            '{"type": "assistant", "content": "Hello"}\n'
            '{"type": "tool_use", "name": "read"}\n'
        )
        result = _parse_claude_json_output(output)
        self.assertEqual(result["type"], "tool_use")
        self.assertEqual(result["name"], "read")

    def test_mixed_text_and_json(self):
        output = (
            "Some preamble text\n"
            '{"type": "assistant", "content": "Hello"}\n'
            "Some trailing text\n"
        )
        result = _parse_claude_json_output(output)
        self.assertEqual(result["type"], "assistant")
        self.assertEqual(result["content"], "Hello")

    def test_no_json_returns_empty_dict(self):
        output = "Just plain text, no JSON here"
        result = _parse_claude_json_output(output)
        self.assertEqual(result, {})

    def test_invalid_json_skipped(self):
        output = (
            '{"type": "assistant", "content": "Hello"}\n'
            '{invalid json}\n'
            '{"type": "tool_use", "name": "write"}\n'
        )
        result = _parse_claude_json_output(output)
        self.assertEqual(result["type"], "tool_use")

    def test_nested_json_braces(self):
        output = '{"type": "assistant", "content": {"text": "nested", "tokens": 42}}'
        result = _parse_claude_json_output(output)
        self.assertEqual(result["type"], "assistant")
        self.assertEqual(result["content"]["text"], "nested")

    def test_empty_string(self):
        result = _parse_claude_json_output("")
        self.assertEqual(result, {})

    def test_whitespace_only(self):
        result = _parse_claude_json_output("   \n  \n   ")
        self.assertEqual(result, {})


# ── ClaudeSession Tests ──────────────────────────────────────────────────────


class TestClaudeSession(unittest.TestCase):
    """Tests for the ClaudeSession dataclass."""

    def test_create_session(self):
        now = time.time()
        session = ClaudeSession(
            session_id="abc123",
            tmux_session="apex-claude-abc123",
            profile="test",
            agent="tester",
            workdir="/tmp/test",
            created_at=now,
        )
        self.assertEqual(session.session_id, "abc123")
        self.assertEqual(session.state, InstanceState.PENDING)

    def test_handle_property(self):
        session = ClaudeSession(
            session_id="abc123",
            tmux_session="apex-claude-abc123",
            profile="test",
            agent="tester",
            workdir="/tmp/test",
            state=InstanceState.RUNNING,
        )
        handle = session.handle
        self.assertIsInstance(handle, SessionHandle)
        self.assertEqual(handle.id, "abc123")
        self.assertEqual(handle.status, "running")
        self.assertEqual(handle.name, "claude-tester")

    def test_handle_no_agent_falls_back_to_profile(self):
        session = ClaudeSession(
            session_id="xyz",
            tmux_session="apex-claude-xyz",
            profile="default",
            workdir="/tmp/test",
        )
        handle = session.handle
        self.assertEqual(handle.name, "claude-default")


# ── ClaudeCodeAdapter Tests (mocked subprocess) ──────────────────────────────


class TestClaudeCodeAdapterInit(unittest.TestCase):
    """Tests for adapter initialization."""

    def test_default_init(self):
        adapter = ClaudeCodeAdapter()
        self.assertIsNotNone(adapter._apex_home)
        self.assertEqual(adapter._sessions, {})

    def test_init_with_apex_home_string(self):
        adapter = ClaudeCodeAdapter(apex_home="/tmp/test-apex")
        self.assertEqual(str(adapter._apex_home), "/tmp/test-apex")

    def test_init_with_apex_home_path(self):
        adapter = ClaudeCodeAdapter(apex_home=Path("/tmp/test-apex-path"))
        self.assertEqual(str(adapter._apex_home), "/tmp/test-apex-path")

    def test_init_creates_workdir(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            expected = Path(tmpdir) / "claude-sessions"
            self.assertTrue(expected.exists())
            self.assertTrue(expected.is_dir())


class TestClaudeCodeAdapterSpawn(unittest.TestCase):
    """Tests for the spawn method."""

    @patch("apex.adapters.claude._tmux_new_session")
    def test_spawn_creates_session(self, mock_tmux_new):
        mock_tmux_new.return_value = True

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn(profile="test", agent="tester", metadata={"key": "val"})

            self.assertIsInstance(session_id, str)
            self.assertGreater(len(session_id), 0)
            self.assertIn(session_id, adapter._sessions)

            session = adapter._sessions[session_id]
            self.assertEqual(session.profile, "test")
            self.assertEqual(session.agent, "tester")
            self.assertEqual(session.metadata, {"key": "val"})
            self.assertEqual(session.state, InstanceState.RUNNING)
            self.assertTrue(session.tmux_session.startswith("apex-claude-"))

    @patch("apex.adapters.claude._tmux_new_session")
    def test_spawn_with_empty_profile(self, mock_tmux_new):
        mock_tmux_new.return_value = True

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn()

            self.assertIn(session_id, adapter._sessions)
            session = adapter._sessions[session_id]
            self.assertEqual(session.profile, "")
            self.assertEqual(session.agent, "")

    @patch("apex.adapters.claude._tmux_new_session")
    def test_spawn_tmux_failure_still_creates_session(self, mock_tmux_new):
        mock_tmux_new.return_value = False

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn(profile="test")

            # Should still have a session even if tmux fails
            self.assertIn(session_id, adapter._sessions)

    @patch("apex.adapters.claude._tmux_new_session")
    def test_spawn_multiple_sessions_unique_ids(self, mock_tmux_new):
        mock_tmux_new.return_value = True

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            ids = set()
            for _ in range(10):
                sid = adapter.spawn()
                ids.add(sid)
            self.assertEqual(len(ids), 10)


class TestClaudeCodeAdapterPrompt(unittest.TestCase):
    """Tests for the prompt method."""

    @patch("apex.adapters.claude._tmux_new_session")
    def test_prompt_session_not_found(self, mock_tmux_new):
        mock_tmux_new.return_value = True
        adapter = ClaudeCodeAdapter()
        result = adapter.prompt("nonexistent", "hello")
        self.assertFalse(result["ok"])
        self.assertIn("not found", result.get("error", ""))

    @patch("apex.adapters.claude._run")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_prompt_successful_json_response(self, mock_tmux_new, mock_run):
        mock_tmux_new.return_value = True
        mock_run.return_value = (0, '{"type":"assistant","content":"Hello, world!"}', "")

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn(profile="test")

            result = adapter.prompt(session_id, "Hi there")
            self.assertTrue(result["ok"])
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["response"], "Hello, world!")
            self.assertIn("json_output", result)

    @patch("apex.adapters.claude._run")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_prompt_non_json_output(self, mock_tmux_new, mock_run):
        mock_tmux_new.return_value = True
        mock_run.return_value = (0, "Plain text response", "")

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn(profile="test")

            result = adapter.prompt(session_id, "Hello")
            self.assertTrue(result["ok"])
            self.assertEqual(result["response"], "Plain text response")

    @patch("apex.adapters.claude._run")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_prompt_claude_error(self, mock_tmux_new, mock_run):
        mock_tmux_new.return_value = True
        mock_run.return_value = (1, "", "Claude error: invalid API key")

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn(profile="test")

            result = adapter.prompt(session_id, "Hello")
            self.assertFalse(result["ok"])
            self.assertEqual(result["exit_code"], 1)
            self.assertIn("invalid API key", result.get("raw_stderr", ""))
            # Session state should be FAILED
            session = adapter._sessions[session_id]
            self.assertEqual(session.state, InstanceState.FAILED)

    @patch("apex.adapters.claude._run")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_prompt_timeout(self, mock_tmux_new, mock_run):
        mock_tmux_new.return_value = True
        mock_run.return_value = (-1, "", "timeout after 300s")

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn(profile="test")

            result = adapter.prompt(session_id, "Long running task", timeout=300)
            self.assertFalse(result["ok"])
            self.assertEqual(result["exit_code"], -1)

    @patch("apex.adapters.claude._run")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_prompt_json_with_content_field(self, mock_tmux_new, mock_run):
        mock_tmux_new.return_value = True
        mock_run.return_value = (0, '{"content": "Direct content field"}', "")

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn()

            result = adapter.prompt(session_id, "test")
            self.assertEqual(result["response"], "Direct content field")

    @patch("apex.adapters.claude._run")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_prompt_json_with_text_field(self, mock_tmux_new, mock_run):
        mock_tmux_new.return_value = True
        mock_run.return_value = (0, '{"text": "Text field content"}', "")

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn()

            result = adapter.prompt(session_id, "test")
            self.assertEqual(result["response"], "Text field content")

    @patch("apex.adapters.claude._run")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_prompt_json_with_result_field(self, mock_tmux_new, mock_run):
        mock_tmux_new.return_value = True
        mock_run.return_value = (0, '{"result": "Result field content"}', "")

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn()

            result = adapter.prompt(session_id, "test")
            self.assertEqual(result["response"], "Result field content")

    @patch("apex.adapters.claude._run")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_prompt_with_resume_id(self, mock_tmux_new, mock_run):
        mock_tmux_new.return_value = True
        mock_run.return_value = (0, '{"content": "ok"}', "")

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn()

            result = adapter.prompt(session_id, "continue", resume_id="claude-sess-123")
            self.assertTrue(result["ok"])
            # Check that --resume was passed (verify via mock)
            call_args = mock_run.call_args[0][0]
            self.assertIn("--resume", call_args)
            self.assertIn("claude-sess-123", call_args)


class TestClaudeCodeAdapterSteer(unittest.TestCase):
    """Tests for the steer method."""

    @patch("apex.adapters.claude._tmux_send_keys")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_steer_session_not_found(self, mock_tmux_new, mock_tmux_send):
        mock_tmux_new.return_value = True
        adapter = ClaudeCodeAdapter()
        result = adapter.steer("nonexistent", "change direction")
        self.assertFalse(result["ok"])
        self.assertIn("not found", result.get("error", ""))

    @patch("apex.adapters.claude._tmux_send_keys")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_steer_sends_ctrl_c_then_instruction(self, mock_tmux_new, mock_tmux_send):
        mock_tmux_new.return_value = True
        mock_tmux_send.return_value = True

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn(profile="test")

            result = adapter.steer(session_id, "focus on tests")
            self.assertTrue(result["ok"])
            self.assertEqual(result["steer_instruction"], "focus on tests")
            self.assertEqual(result["method"], "tmux_send_keys")

            # Check that Ctrl+C was sent first, then the instruction
            self.assertEqual(mock_tmux_send.call_count, 2)
            first_call_args = mock_tmux_send.call_args_list[0][0]
            self.assertEqual(first_call_args[0], adapter._sessions[session_id].tmux_session)

    @patch("apex.adapters.claude._tmux_send_keys")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_steer_tmux_send_fails(self, mock_tmux_new, mock_tmux_send):
        mock_tmux_new.return_value = True
        mock_tmux_send.return_value = False  # send_keys fails

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn()

            result = adapter.steer(session_id, "instruction")
            self.assertFalse(result["ok"])


class TestClaudeCodeAdapterAbort(unittest.TestCase):
    """Tests for the abort method."""

    @patch("apex.adapters.claude._tmux_send_keys")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_abort_session_not_found(self, mock_tmux_new, mock_tmux_send):
        mock_tmux_new.return_value = True
        adapter = ClaudeCodeAdapter()
        result = adapter.abort("nonexistent")
        self.assertFalse(result["ok"])
        self.assertIn("not found", result.get("error", ""))

    @patch("apex.adapters.claude._tmux_send_keys")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_abort_sends_ctrl_c(self, mock_tmux_new, mock_tmux_send):
        mock_tmux_new.return_value = True
        mock_tmux_send.return_value = True

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn()

            result = adapter.abort(session_id)
            self.assertTrue(result["ok"])
            self.assertTrue(result["aborted"])
            self.assertEqual(result["state"], "stopped")

            # Verify Ctrl+C was sent
            mock_tmux_send.assert_called_once()
            call_args = mock_tmux_send.call_args[0]
            self.assertIn("\x03", call_args[1])  # Ctrl+C character

    @patch("apex.adapters.claude._tmux_send_keys")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_abort_updates_session_state(self, mock_tmux_new, mock_tmux_send):
        mock_tmux_new.return_value = True
        mock_tmux_send.return_value = True

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn()

            adapter.abort(session_id)
            session = adapter._sessions[session_id]
            self.assertEqual(session.state, InstanceState.STOPPED)


class TestClaudeCodeAdapterStatus(unittest.TestCase):
    """Tests for the status method."""

    def test_status_session_not_found(self):
        adapter = ClaudeCodeAdapter()
        result = adapter.status("nonexistent")
        self.assertFalse(result["exists"])
        self.assertEqual(result["state"], "unknown")
        self.assertIn("not found", result.get("error", ""))

    @patch("apex.adapters.claude._tmux_capture_pane")
    @patch("apex.adapters.claude._tmux_has_session")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_status_session_running(self, mock_tmux_new, mock_has_session,
                                     mock_capture_pane):
        mock_tmux_new.return_value = True
        mock_has_session.return_value = True
        mock_capture_pane.return_value = '{"type":"assistant","content":"working..."}'

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn(profile="test", agent="tester")

            result = adapter.status(session_id)
            self.assertEqual(result["session_id"], session_id)
            self.assertTrue(result["tmux_alive"])
            self.assertEqual(result["state"], "running")
            self.assertEqual(result["profile"], "test")
            self.assertEqual(result["agent"], "tester")
            self.assertIn("parsed_output", result)
            self.assertEqual(
                result["parsed_output"]["content"],
                "working..."
            )

    @patch("apex.adapters.claude._tmux_capture_pane")
    @patch("apex.adapters.claude._tmux_has_session")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_status_session_stopped(self, mock_tmux_new, mock_has_session,
                                     mock_capture_pane):
        mock_tmux_new.return_value = True
        mock_has_session.return_value = False  # tmux session is dead
        mock_capture_pane.return_value = ""

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn()

            result = adapter.status(session_id)
            self.assertFalse(result["tmux_alive"])
            self.assertEqual(result["state"], "stopped")
            self.assertEqual(result["pane_content_preview"], "")

    @patch("apex.adapters.claude._tmux_capture_pane")
    @patch("apex.adapters.claude._tmux_has_session")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_status_includes_uptime(self, mock_tmux_new, mock_has_session,
                                     mock_capture_pane):
        mock_tmux_new.return_value = True
        mock_has_session.return_value = True
        mock_capture_pane.return_value = ""

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn()

            result = adapter.status(session_id)
            self.assertIn("uptime_seconds", result)
            self.assertGreaterEqual(result["uptime_seconds"], 0)


class TestClaudeCodeAdapterDispose(unittest.TestCase):
    """Tests for the dispose method."""

    def test_dispose_session_not_found(self):
        adapter = ClaudeCodeAdapter()
        result = adapter.dispose("nonexistent")
        self.assertFalse(result["ok"])
        self.assertIn("not found", result.get("error", ""))

    @patch("apex.adapters.claude._tmux_kill_session")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_dispose_removes_session(self, mock_tmux_new, mock_tmux_kill):
        mock_tmux_new.return_value = True
        mock_tmux_kill.return_value = True

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn(profile="test")

            self.assertIn(session_id, adapter._sessions)

            result = adapter.dispose(session_id)
            self.assertTrue(result["ok"])
            self.assertTrue(result["disposed"])
            self.assertNotIn(session_id, adapter._sessions)

    @patch("apex.adapters.claude._tmux_kill_session")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_dispose_kills_tmux(self, mock_tmux_new, mock_tmux_kill):
        mock_tmux_new.return_value = True
        mock_tmux_kill.return_value = True

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn()

            adapter.dispose(session_id)
            mock_tmux_kill.assert_called_once()

    @patch("apex.adapters.claude._tmux_kill_session")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_dispose_tmux_kill_fails_gracefully(self, mock_tmux_new, mock_tmux_kill):
        mock_tmux_new.return_value = True
        mock_tmux_kill.return_value = False  # kill fails

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn()

            result = adapter.dispose(session_id)
            self.assertTrue(result["ok"])  # still ok - best effort
            self.assertFalse(result["tmux_killed"])
            self.assertNotIn(session_id, adapter._sessions)

    @patch("apex.adapters.claude._tmux_kill_session")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_dispose_cleans_workdir(self, mock_tmux_new, mock_tmux_kill):
        mock_tmux_new.return_value = True
        mock_tmux_kill.return_value = True

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn()
            workdir = adapter._sessions[session_id].workdir
            self.assertTrue(Path(workdir).exists())

            adapter.dispose(session_id)
            # Workdir should be removed
            self.assertFalse(Path(workdir).exists())


class TestClaudeCodeAdapterStopExec(unittest.TestCase):
    """Tests for Protocol-compatible stop() and exec() methods."""

    @patch("apex.adapters.claude._tmux_send_keys")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_stop_calls_abort(self, mock_tmux_new, mock_tmux_send):
        mock_tmux_new.return_value = True
        mock_tmux_send.return_value = True

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn()

            session = adapter._sessions[session_id]
            handle = session.handle
            result = adapter.stop(handle)
            self.assertTrue(result)

            session_after = adapter._sessions[session_id]
            self.assertEqual(session_after.state, InstanceState.STOPPED)

    @patch("apex.adapters.claude._tmux_new_session")
    def test_stop_nonexistent_handle(self, mock_tmux_new):
        mock_tmux_new.return_value = True
        adapter = ClaudeCodeAdapter()
        handle = SessionHandle(id="nonexistent")
        result = adapter.stop(handle)
        self.assertFalse(result)

    @patch("apex.adapters.claude._run")
    @patch("apex.adapters.claude._tmux_new_session")
    def test_exec_runs_command_in_session_workdir(self, mock_tmux_new, mock_run):
        mock_tmux_new.return_value = True
        mock_run.return_value = (0, "output", "")

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            session_id = adapter.spawn()

            session = adapter._sessions[session_id]
            handle = session.handle
            exit_code, stdout, stderr = adapter.exec(handle, "echo hello")

            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout, "output")
            # Verify _run was called with correct cwd
            self.assertTrue(any(
                session.workdir in str(arg) for arg in mock_run.call_args[0]
                if isinstance(arg, str)
            ) or mock_run.call_args[1].get("cwd") == session.workdir)

    @patch("apex.adapters.claude._tmux_new_session")
    def test_exec_nonexistent_handle(self, mock_tmux_new):
        mock_tmux_new.return_value = True
        adapter = ClaudeCodeAdapter()
        handle = SessionHandle(id="nonexistent")
        exit_code, stdout, stderr = adapter.exec(handle, "echo hello")
        self.assertNotEqual(exit_code, 0)
        self.assertIn("not found", stderr)


class TestClaudeCodeAdapterListSessions(unittest.TestCase):
    """Tests for list_sessions utility."""

    @patch("apex.adapters.claude._tmux_new_session")
    def test_list_sessions_empty(self, mock_tmux_new):
        mock_tmux_new.return_value = True
        adapter = ClaudeCodeAdapter()
        result = adapter.list_sessions()
        self.assertEqual(result, [])

    @patch("apex.adapters.claude._tmux_new_session")
    def test_list_sessions_with_active(self, mock_tmux_new):
        mock_tmux_new.return_value = True

        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = ClaudeCodeAdapter(apex_home=tmpdir)
            adapter.spawn(profile="a")
            adapter.spawn(profile="b")
            adapter.spawn(profile="c")

            result = adapter.list_sessions()
            self.assertEqual(len(result), 3)
            profiles = {s["profile"] for s in result}
            self.assertEqual(profiles, {"a", "b", "c"})


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
