"""Unit tests for ACP client and KiroAdapter ACP integration.

Uses mock subprocess to simulate an ACP JSON-RPC server — no external deps.
Run standalone:  python3 tests/unit/test_acp.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import uuid
from typing import Optional
from unittest import mock

# ── Bootstrap: add project root to sys.path ─────────────────────────────────
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from apex.adapters.acp import (
    AcpClient,
    AcpError,
    AcpNotConnectedError,
    AcpTimeoutError,
    check_acp_available,
)
from apex.adapters.base import SessionHandle, SpawnSpec
from apex.protocol import ApexEvent


# ═══════════════════════════════════════════════════════════════════════════
# Mock ACP Server (simulates a JSON-RPC 2.0 agent over stdio)
# ═══════════════════════════════════════════════════════════════════════════


class MockAcpProcess:
    """Simulates an ACP agent subprocess with scripted responses.

    stdin.write() triggers responses queued onto stdout.
    stdout.readline() returns queued responses and notifications.
    """

    def __init__(
        self,
        initialize_response: Optional[dict] = None,
        responses: Optional[list[dict]] = None,
        notifications: Optional[list[dict]] = None,
        delay: float = 0,
    ):
        # Use a shared StringIO for stdout so reader thread sees responses
        self._stdout_buf = io.StringIO()
        self.stdin = io.StringIO()
        self.stdout = self._stdout_buf
        self.stderr = io.StringIO()
        self.pid = 12345
        self.returncode: Optional[int] = None
        self._alive = True

        # Scripted responses by method
        self._init_resp = initialize_response or {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "sessionId": "mock-session-001",
                "capabilities": {"tools": True, "streaming": True},
            },
        }
        self._responses: list[dict] = responses or []
        self._response_idx = 0

        # Pre-scripted notifications (emitted via stdout in order)
        self._notifications: list[dict] = notifications or []
        self._notif_idx = 0
        self._notifs_written = False  # write notifications once

        self._delay = delay
        self._lock = threading.Lock()

    # -- subprocess.Popen interface --

    def poll(self) -> Optional[int]:
        return self.returncode if not self._alive else None

    def terminate(self) -> None:
        self._alive = False
        self.returncode = -15

    def kill(self) -> None:
        self._alive = False
        self.returncode = -9

    def wait(self, timeout: Optional[float] = None) -> int:
        return self.returncode or 0

    # -- stdin writes become JSON-RPC requests --

    def write(self, data: str) -> int:
        """Called when client writes to process.stdin."""
        self.stdin.write(data)
        self.stdin.flush()

        line = data.strip()
        if not line:
            return len(data)

        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            return len(data)

        method = req.get("method", "")
        req_id = req.get("id")

        # If notification (no id), do nothing for stdout
        if req_id is None:
            return len(data)

        # Generate response and queue to stdout
        resp = self._make_response(req_id, method, req.get("params", {}))
        if resp is not None:
            with self._lock:
                self._stdout_buf.write(json.dumps(resp) + "\n")
                self._stdout_buf.flush()

        return len(data)

    def flush(self) -> None:
        pass

    # -- stdout reads return responses and notifications --

    def readline(self) -> str:
        """Called by the reader thread in a loop.

        Blocks until at least one full line is available, then returns it.
        Returns '' only when the process is terminated (EOF).
        """
        while self._alive:
            if self._delay:
                time.sleep(self._delay)

            with self._lock:
                # Write notifications on first readline call (after connection)
                if not self._notifs_written and self._notifications:
                    for notif in self._notifications:
                        self._stdout_buf.write(json.dumps(notif) + "\n")
                    self._stdout_buf.flush()
                    self._notifs_written = True

                # Check if there's content
                self._stdout_buf.seek(0)
                content = self._stdout_buf.read()
                self._stdout_buf.seek(0, 2)  # back to end

                if content and "\n" in content:
                    # Extract first line
                    lines = content.split("\n")
                    first_line = lines[0]

                    # Rebuild buffer with remaining lines
                    remaining = "\n".join(lines[1:])
                    self._stdout_buf.truncate(0)
                    self._stdout_buf.seek(0)
                    if remaining.strip():
                        self._stdout_buf.write(remaining)
                    self._stdout_buf.seek(0, 2)

                    return first_line + "\n"

            # No data yet — sleep briefly before retrying
            time.sleep(0.01)

        # Process terminated → EOF
        return ""

    def close(self) -> None:
        self._alive = False

    # -- internal --

    def _make_response(
        self, req_id: int, method: str, params: dict
    ) -> Optional[dict]:
        """Build a JSON-RPC response for the given request."""
        # initialize → return the canned init response (id adjusted)
        if method == "initialize":
            resp = dict(self._init_resp)
            resp["id"] = req_id
            return resp

        # Use scripted responses if available (round-robin)
        if self._response_idx < len(self._responses):
            resp = dict(self._responses[self._response_idx])
            self._response_idx += 1
            resp["id"] = req_id
            return resp

        # Default: generic success
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"status": "ok", "method": method},
        }


def _make_mock_popen(mock_proc: MockAcpProcess):
    """Factory for mock.patch that returns a callable creating *mock_proc*."""
    def factory(*args, **kwargs):
        return mock_proc

    return factory


# ═══════════════════════════════════════════════════════════════════════════
# Test: AcpClient Core
# ═══════════════════════════════════════════════════════════════════════════


def test_acp_client_connect_and_initialize():
    """AcpClient.connect() sends initialize and parses the handshake response."""
    mock_proc = MockAcpProcess()
    with mock.patch("subprocess.Popen", side_effect=_make_mock_popen(mock_proc)):
        client = AcpClient(acp_bin="fake-kiro", startup_timeout=5.0)
        sid = client.connect(cwd="/tmp/test")

        assert sid == "mock-session-001"
        assert client.initialized is True
        assert client.session_id == "mock-session-001"
        assert client.capabilities == {"tools": True, "streaming": True}
        assert client.is_alive()

        client.dispose()
        assert not client.is_alive()

    print("✓ acp connect + initialize + dispose")


def test_acp_client_prompt():
    """prompt() sends a prompts/execute request and returns the result."""
    mock_proc = MockAcpProcess(
        responses=[
            {"jsonrpc": "2.0", "id": 0, "result": {"accepted": True, "taskId": "t1"}}
        ]
    )
    with mock.patch("subprocess.Popen", side_effect=_make_mock_popen(mock_proc)):
        client = AcpClient(acp_bin="fake-kiro", startup_timeout=5.0)
        client.connect(cwd="/tmp/test")

        result = client.prompt("Write a hello world in Python", timeout=2.0)
        assert result == {"accepted": True, "taskId": "t1"}

        client.dispose()
    print("✓ acp prompt")


def test_acp_client_steer():
    """steer() sends prompts/execute with mode=steer."""
    mock_proc = MockAcpProcess(
        responses=[
            {"jsonrpc": "2.0", "id": 0, "result": {"guided": True}}
        ]
    )
    with mock.patch("subprocess.Popen", side_effect=_make_mock_popen(mock_proc)):
        client = AcpClient(acp_bin="fake-kiro", startup_timeout=5.0)
        client.connect()

        result = client.steer("Focus on readability", timeout=2.0)
        assert result == {"guided": True}

        client.dispose()
    print("✓ acp steer")


def test_acp_client_abort():
    """abort() sends notifications/cancel + tools/call cancel."""
    mock_proc = MockAcpProcess(
        responses=[
            {"jsonrpc": "2.0", "id": 0, "result": {"cancelled": True}}
        ]
    )
    with mock.patch("subprocess.Popen", side_effect=_make_mock_popen(mock_proc)):
        client = AcpClient(acp_bin="fake-kiro", startup_timeout=5.0)
        client.connect()

        result = client.abort(timeout=2.0)
        assert result == {"cancelled": True}

        client.dispose()
    print("✓ acp abort")


def test_acp_client_list_tools():
    """list_tools() returns the tools array from the response."""
    mock_proc = MockAcpProcess(
        responses=[
            {
                "jsonrpc": "2.0",
                "id": 0,
                "result": {
                    "tools": [
                        {"name": "read_file", "description": "Read a file"},
                        {"name": "write_file", "description": "Write a file"},
                    ]
                },
            }
        ]
    )
    with mock.patch("subprocess.Popen", side_effect=_make_mock_popen(mock_proc)):
        client = AcpClient(acp_bin="fake-kiro", startup_timeout=5.0)
        client.connect()

        tools = client.list_tools(timeout=2.0)
        assert len(tools) == 2
        assert tools[0]["name"] == "read_file"
        assert tools[1]["name"] == "write_file"

        client.dispose()
    print("✓ acp list_tools")


def test_acp_client_call_tool():
    """call_tool() sends tools/call with name and arguments."""
    mock_proc = MockAcpProcess(
        responses=[
            {"jsonrpc": "2.0", "id": 0, "result": {"content": "Hello, World!"}}
        ]
    )
    with mock.patch("subprocess.Popen", side_effect=_make_mock_popen(mock_proc)):
        client = AcpClient(acp_bin="fake-kiro", startup_timeout=5.0)
        client.connect()

        result = client.call_tool("read_file", {"path": "/tmp/hello.py"}, timeout=2.0)
        assert result == {"content": "Hello, World!"}

        client.dispose()
    print("✓ acp call_tool")


# ═══════════════════════════════════════════════════════════════════════════
# Test: ACP Event Streaming
# ═══════════════════════════════════════════════════════════════════════════


def test_acp_streaming_events():
    """Notifications from the agent are captured as ApexEvent objects."""
    notifications = [
        {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"step": 1}},
        {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"step": 2}},
        {"jsonrpc": "2.0", "method": "notifications/message", "params": {"text": "done"}},
    ]
    mock_proc = MockAcpProcess(notifications=notifications)
    with mock.patch("subprocess.Popen", side_effect=_make_mock_popen(mock_proc)):
        client = AcpClient(acp_bin="fake-kiro", startup_timeout=5.0)
        client.connect()

        # Give the reader thread a moment to process
        time.sleep(0.2)

        events = client.events()
        assert len(events) >= 2  # at least some should be captured

        # Check event types
        types = {e.type for e in events}
        assert "acp.notifications.progress" in types

        # Each event should have proper fields
        for ev in events:
            assert ev.id
            assert ev.session_id == "mock-session-001"
            assert ev.timestamp

        client.dispose()
    print("✓ acp streaming events")


def test_acp_flush_events():
    """flush_events() returns and clears the buffer."""
    notifications = [
        {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"step": 1}},
    ]
    mock_proc = MockAcpProcess(notifications=notifications)
    with mock.patch("subprocess.Popen", side_effect=_make_mock_popen(mock_proc)):
        client = AcpClient(acp_bin="fake-kiro", startup_timeout=5.0)
        client.connect()

        time.sleep(0.2)

        flushed = client.flush_events()
        assert len(flushed) >= 1
        assert len(client.events()) == 0  # cleared

        client.dispose()
    print("✓ acp flush events")


def test_acp_event_callback():
    """on_event() callbacks fire for each incoming event."""
    notifications = [
        {"jsonrpc": "2.0", "method": "notifications/started", "params": {}},
    ]
    mock_proc = MockAcpProcess(notifications=notifications)
    with mock.patch("subprocess.Popen", side_effect=_make_mock_popen(mock_proc)):
        client = AcpClient(acp_bin="fake-kiro", startup_timeout=5.0)

        received: list[ApexEvent] = []
        client.on_event(lambda ev: received.append(ev))

        client.connect()
        time.sleep(0.2)

        assert len(received) >= 1
        assert received[0].type == "acp.notifications.started"

        client.dispose()
    print("✓ acp event callback")


# ═══════════════════════════════════════════════════════════════════════════
# Test: Error Handling
# ═══════════════════════════════════════════════════════════════════════════


def test_acp_not_connected_error():
    """Operations before connect() raise AcpNotConnectedError."""
    client = AcpClient(acp_bin="fake-kiro")
    try:
        client.prompt("hello")
        assert False, "Should have raised"
    except AcpNotConnectedError:
        pass

    try:
        client.steer("guide")
        assert False, "Should have raised"
    except AcpNotConnectedError:
        pass

    try:
        client.abort()
        assert False, "Should have raised"
    except AcpNotConnectedError:
        pass

    print("✓ acp not-connected errors")


def test_acp_already_connected_error():
    """connect() twice raises AcpError."""
    mock_proc = MockAcpProcess()
    with mock.patch("subprocess.Popen", side_effect=_make_mock_popen(mock_proc)):
        client = AcpClient(acp_bin="fake-kiro", startup_timeout=5.0)
        client.connect()
        try:
            client.connect()
            assert False, "Should have raised"
        except AcpError as e:
            assert "Already connected" in str(e)
        client.dispose()
    print("✓ acp double-connect error")


def test_acp_jsonrpc_error_response():
    """ACP error responses are raised as AcpError."""
    mock_proc = MockAcpProcess(
        responses=[
            {
                "jsonrpc": "2.0",
                "id": 0,
                "error": {"code": -32600, "message": "Invalid Request"},
            }
        ]
    )
    with mock.patch("subprocess.Popen", side_effect=_make_mock_popen(mock_proc)):
        client = AcpClient(acp_bin="fake-kiro", startup_timeout=5.0)
        client.connect()
        try:
            client.prompt("bad request", timeout=2.0)
            assert False, "Should have raised"
        except AcpError as e:
            assert "Invalid Request" in str(e)
        client.dispose()
    print("✓ acp JSON-RPC error response")


def test_acp_timeout():
    """Timeout waiting for response raises AcpTimeoutError."""
    # A mock that never writes responses
    silent_proc = MockAcpProcess()
    # Override to never queue responses
    original_make = silent_proc._make_response

    def no_response(req_id, method, params):
        return None  # never respond

    silent_proc._make_response = no_response

    with mock.patch("subprocess.Popen", side_effect=_make_mock_popen(silent_proc)):
        client = AcpClient(acp_bin="fake-kiro", startup_timeout=1.0)
        try:
            client.connect()
        except AcpError:
            pass  # init also times out — expected
        assert not client.initialized

    print("✓ acp timeout")


def test_acp_status_snapshot():
    """status() returns a diagnostic dict."""
    mock_proc = MockAcpProcess()
    with mock.patch("subprocess.Popen", side_effect=_make_mock_popen(mock_proc)):
        client = AcpClient(acp_bin="fake-kiro", startup_timeout=5.0)
        client.connect()

        snap = client.status()
        assert snap["session_id"] == "mock-session-001"
        assert snap["initialized"] is True
        assert snap["alive"] is True
        assert snap["pid"] == 12345
        assert "tools" in snap["capabilities"]

        client.dispose()

        snap2 = client.status()
        assert snap2["alive"] is False

    print("✓ acp status snapshot")


# ═══════════════════════════════════════════════════════════════════════════
# Test: check_acp_available
# ═══════════════════════════════════════════════════════════════════════════


def test_check_acp_available_true():
    """Probe returns True when binary responds to --version."""
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0)
        assert check_acp_available("fake-kiro") is True
        mock_run.assert_called_once()
    print("✓ check_acp_available true")


def test_check_acp_available_false():
    """Probe returns False when binary not found or exits non-zero."""
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        assert check_acp_available("nonexistent") is False
    print("✓ check_acp_available false (not found)")

    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=1)
        assert check_acp_available("broken-kiro") is False
    print("✓ check_acp_available false (non-zero exit)")


# ═══════════════════════════════════════════════════════════════════════════
# Test: KiroAdapter ACP Integration
# ═══════════════════════════════════════════════════════════════════════════


def test_kiro_adapter_acp_spawn_and_prompt():
    """KiroAdapter.spawn() uses ACP when available, routes prompt/steer/abort."""
    from apex.adapters.kiro import KiroAdapter

    mock_proc = MockAcpProcess(
        responses=[
            {"jsonrpc": "2.0", "id": 0, "result": {"accepted": True}},   # spawn prompt
            {"jsonrpc": "2.0", "id": 0, "result": {"accepted": True}},   # prompt
            {"jsonrpc": "2.0", "id": 0, "result": {"guided": True}},     # steer
            {"jsonrpc": "2.0", "id": 0, "result": {"cancelled": True}},  # abort
        ]
    )
    with mock.patch("subprocess.Popen", side_effect=_make_mock_popen(mock_proc)):
        # Also mock the probe
        with mock.patch.object(KiroAdapter, "_probe_acp", return_value=True):
            adapter = KiroAdapter(use_acp=True, acp_bin="fake-kiro", tmux_bin="fake-tmux")

            spec = SpawnSpec(
                agent="coder",
                instance="test-1",
                brief="Write tests",
                cwd="/tmp/test",
                env={},
            )

            h = adapter.spawn(spec)
            assert h.runtime == "kiro"
            assert h.session_id

            # Check session entry
            entry = adapter._sessions[h.session_id]
            assert entry["mode"] == "acp"
            assert entry["acp_client"] is not None
            assert entry["acp_client"].initialized

            # prompt
            adapter.prompt(h, "Add more edge cases")
            assert entry["prompts"] == 1

            # steer
            adapter.steer(h, "Focus on readability")

            # abort
            adapter.abort(h)

            # status
            st = adapter.status(h)
            assert st.state == "running"
            assert "acp" in st.detail
            assert "pid" in st.detail

            # acp_events_for
            events = adapter.acp_events_for(h)
            assert isinstance(events, list)

            # dispose
            adapter.dispose(h)
            assert h.session_id not in adapter._sessions

    print("✓ kiro adapter ACP spawn + prompt + steer + abort + status + dispose")


def test_kiro_adapter_fallback_to_tmux():
    """When ACP probe fails, spawn() falls back to tmux."""
    from apex.adapters.kiro import KiroAdapter

    with mock.patch.object(KiroAdapter, "_probe_acp", return_value=False):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0)

            adapter = KiroAdapter(use_acp=True, acp_bin="fake-kiro", tmux_bin="fake-tmux")

            spec = SpawnSpec(
                agent="coder",
                instance="test-2",
                brief="Write tests",
                cwd="/tmp/test",
            )

            h = adapter.spawn(spec)

            # Should have used tmux
            entry = adapter._sessions[h.session_id]
            assert entry["mode"] == "tmux"
            assert h.tmux_session.startswith("apex-")

            # Verify tmux was called
            assert any("new-session" in str(call) for call in mock_run.call_args_list)

            adapter.dispose(h)

    print("✓ kiro adapter tmux fallback")


def test_kiro_adapter_connect_acp():
    """connect_acp() probes and returns availability."""
    from apex.adapters.kiro import KiroAdapter

    adapter = KiroAdapter(use_acp=True, acp_bin="kiro")
    with mock.patch.object(KiroAdapter, "_probe_acp", return_value=True):
        assert adapter.connect_acp() is True
        # Second call uses cached result
        assert adapter.connect_acp() is True

    adapter2 = KiroAdapter(use_acp=True, acp_bin="kiro")
    with mock.patch.object(KiroAdapter, "_probe_acp", return_value=False):
        assert adapter2.connect_acp() is False

    print("✓ kiro adapter connect_acp")


def test_kiro_adapter_acp_events_for():
    """acp_events_for() returns ApexEvents from ACP-backed sessions."""
    from apex.adapters.kiro import KiroAdapter

    notifications = [
        {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"step": 1}},
    ]
    mock_proc = MockAcpProcess(
        responses=[
            {"jsonrpc": "2.0", "id": 0, "result": {"accepted": True}},  # spawn prompt
        ],
        notifications=notifications,
    )
    with mock.patch("subprocess.Popen", side_effect=_make_mock_popen(mock_proc)):
        with mock.patch.object(KiroAdapter, "_probe_acp", return_value=True):
            adapter = KiroAdapter(use_acp=True, acp_bin="fake-kiro")

            spec = SpawnSpec(
                agent="coder", instance="test-3", brief="Task", cwd="/tmp/test"
            )
            h = adapter.spawn(spec)
            time.sleep(0.2)

            events = adapter.acp_events_for(h)
            assert len(events) >= 1
            assert any(e.type == "acp.notifications.progress" for e in events)

            # Second call clears buffer
            events2 = adapter.acp_events_for(h)
            assert len(events2) == 0

            adapter.dispose(h)

    print("✓ kiro adapter acp_events_for")


def test_session_list_and_dispose_all():
    """list_sessions() and dispose_all() work with mixed modes."""
    from apex.adapters.kiro import KiroAdapter

    adapter = KiroAdapter(use_acp=False, tmux_bin="fake-tmux")

    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value = mock.Mock(returncode=0)

        # Spawn a couple sessions
        s1 = adapter.spawn(SpawnSpec("a", "i1", "brief", "/tmp"))
        s2 = adapter.spawn(SpawnSpec("b", "i2", "brief", "/tmp"))

        sessions = adapter.list_sessions()
        assert len(sessions) == 2
        assert sessions[0]["mode"] == "tmux"

        # dispose_all
        count = adapter.dispose_all()
        assert count == 2
        assert len(adapter._sessions) == 0

    print("✓ session list + dispose_all")


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [
        ("acp connect + initialize + dispose", test_acp_client_connect_and_initialize),
        ("acp prompt", test_acp_client_prompt),
        ("acp steer", test_acp_client_steer),
        ("acp abort", test_acp_client_abort),
        ("acp list_tools", test_acp_client_list_tools),
        ("acp call_tool", test_acp_client_call_tool),
        ("acp streaming events", test_acp_streaming_events),
        ("acp flush events", test_acp_flush_events),
        ("acp event callback", test_acp_event_callback),
        ("acp not-connected errors", test_acp_not_connected_error),
        ("acp double-connect error", test_acp_already_connected_error),
        ("acp JSON-RPC error response", test_acp_jsonrpc_error_response),
        ("acp timeout", test_acp_timeout),
        ("acp status snapshot", test_acp_status_snapshot),
        ("check_acp_available true", test_check_acp_available_true),
        ("check_acp_available false", test_check_acp_available_false),
        ("kiro adapter ACP integration", test_kiro_adapter_acp_spawn_and_prompt),
        ("kiro adapter tmux fallback", test_kiro_adapter_fallback_to_tmux),
        ("kiro adapter connect_acp", test_kiro_adapter_connect_acp),
        ("kiro adapter acp_events_for", test_kiro_adapter_acp_events_for),
        ("session list + dispose_all", test_session_list_and_dispose_all),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"\n✗ {name}")
            import traceback

            traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed, {len(tests)} total")
    if failed == 0:
        print("ALL TESTS PASSED")
    else:
        print(f"{failed} TEST(S) FAILED")
        sys.exit(1)
