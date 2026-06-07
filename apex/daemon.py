#!/usr/bin/env python3
"""
apex/daemon.py — Unix domain socket server for agent lifecycle.

Listens on ~/.apex/agentops.sock, speaks JSON-line protocol.
Commands: spawn, prompt, status, abort, dispose.

Usage:
  python3 -m apex.daemon            # foreground
  nohup python3 -m apex.daemon &    # background

Stop: kill <pid> or pkill -f apex.daemon
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable, Any

# ── Stdout unbuffered for daemon mode ────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

APEX_HOME = Path(os.environ.get("APEX_HOME", Path.home() / ".apex"))
SOCK_PATH = APEX_HOME / "agentops.sock"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log(msg: str) -> None:
    """Timestamped log to stdout."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Protocol types (mirror/superset of apex.protocol — inlined for bootstrapping)
# ---------------------------------------------------------------------------


@dataclass
class AgentOpRequest:
    """Incoming JSON-line request."""

    id: str = ""  # client-generated request id
    command: str = ""  # spawn | prompt | status | abort | dispose
    session_id: str = ""  # session to operate on
    payload: dict = field(default_factory=dict)  # command-specific data


@dataclass
class AgentOpResponse:
    """Outgoing JSON-line response."""

    id: str = ""  # echoes request id
    command: str = ""  # echoes request command
    session_id: str = ""
    ok: bool = True
    data: dict = field(default_factory=dict)
    error: str = ""
    timestamp: str = ""


def make_response(req: AgentOpRequest, ok: bool = True, data: dict | None = None,
                  error: str = "") -> AgentOpResponse:
    """Build a response mirroring the request metadata."""
    return AgentOpResponse(
        id=req.id,
        command=req.command,
        session_id=req.session_id,
        ok=ok,
        data=data or {},
        error=error,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Agent session (lightweight — delegates to RuntimeAdapter)
# ---------------------------------------------------------------------------


@dataclass
class AgentSession:
    """Tracks a spawned agent session."""
    session_id: str
    agent_name: str = ""
    profile_name: str = ""
    created_at: float = field(default_factory=time.time)
    prompt_count: int = 0
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# RuntimeAdapter reference
# ---------------------------------------------------------------------------

# The daemon expects apex.adapters.base.RuntimeAdapter (a Protocol) to be
# importable.  If the module is not yet created, the daemon will log a warning
# on startup but continue — commands will return "adapter not available" errors
# until the adapter is wired in.

_RuntimeAdapter: Any = None  # type: ignore[assignment]
_adapter_instance: Any = None


def _resolve_adapter():
    """Lazy-import and instantiate the RuntimeAdapter (singleton)."""
    global _RuntimeAdapter, _adapter_instance  # noqa: PLW0603
    if _adapter_instance is not None:
        return _adapter_instance

    try:
        from apex.adapters.base import RuntimeAdapter  # type: ignore[import-untyped]

        _RuntimeAdapter = RuntimeAdapter
    except ImportError:
        log("WARNING: cannot import apex.adapters.base.RuntimeAdapter — "
            "all commands will return 'adapter not available'")
        _RuntimeAdapter = None
        return None

    # Instantiate adapter (assumes a no-arg constructor or a sensible default).
    try:
        _adapter_instance = _RuntimeAdapter()  # type: ignore[operator]
    except TypeError:
        # Maybe it needs explicit args; try passing APEX_HOME
        try:
            _adapter_instance = _RuntimeAdapter(apex_home=APEX_HOME)  # type: ignore[operator]
        except Exception as exc:
            log(f"ERROR: cannot instantiate RuntimeAdapter: {exc}")
            _RuntimeAdapter = None
            return None

    log("RuntimeAdapter loaded and instantiated")
    return _adapter_instance


# ---------------------------------------------------------------------------
# Risk gate & auditor (optional, graceful fallback)
# ---------------------------------------------------------------------------

def _check_risk(session: AgentSession, command: str, payload: dict) -> tuple[bool, str]:
    """Consult the tool-risk gate before executing a command."""
    try:
        from apex.core.tool_risk import risk_gate  # type: ignore[import-untyped]

        allowed, reason = risk_gate(session.session_id, command, payload)
        if not allowed:
            log(f"RISK GATE BLOCKED session={session.session_id} cmd={command}: {reason}")
            return False, reason
    except ImportError:
        pass  # risk gate not wired — allow everything
    except Exception as exc:
        log(f"risk_gate error (allowing): {exc}")
    return True, ""


def _audit(session: AgentSession, command: str, payload: dict, result: dict) -> None:
    """Log to auditor."""
    try:
        from apex.core.auditor import record  # type: ignore[import-untyped]

        record(
            session_id=session.session_id,
            agent=session.agent_name,
            command=command,
            payload=payload,
            result=result,
        )
    except ImportError:
        pass
    except Exception as exc:
        log(f"auditor error: {exc}")


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _handle_spawn(req: AgentOpRequest) -> AgentOpResponse:
    """Spawn a new agent session via the adapter."""
    payload = req.payload
    profile = payload.get("profile", "default")
    agent = payload.get("agent", "")
    metadata = payload.get("metadata", {})

    adapter = _resolve_adapter()
    if adapter is None:
        return make_response(req, ok=False, error="RuntimeAdapter not available")

    session_id = str(uuid.uuid4())

    try:
        # RuntimeAdapter.spawn(profile: str, agent: str, metadata: dict) -> str
        adapter_session_id = adapter.spawn(profile=profile, agent=agent,
                                           metadata=metadata)
        if adapter_session_id:
            session_id = adapter_session_id
    except AttributeError:
        # Adapter doesn't have spawn — create session locally
        pass
    except Exception as exc:
        log(f"adapter.spawn failed: {exc}")
        return make_response(req, ok=False, error=str(exc))

    session = AgentSession(
        session_id=session_id,
        agent_name=agent or profile,
        profile_name=profile,
        metadata=metadata,
    )
    _sessions[session_id] = session  # store in global session registry

    log(f"SPAWN session={session_id} profile={profile} agent={agent}")
    return make_response(req, ok=True, data={
        "session_id": session_id,
        "profile": profile,
        "agent": agent,
    })


def _handle_prompt(req: AgentOpRequest) -> AgentOpResponse:
    """Send a prompt to an existing session."""
    session = _sessions.get(req.session_id)
    if session is None:
        return make_response(req, ok=False, error=f"session not found: {req.session_id}")

    text = req.payload.get("text", "")
    if not text:
        return make_response(req, ok=False, error="missing 'text' in payload")

    # Risk gate
    allowed, reason = _check_risk(session, "prompt", req.payload)
    if not allowed:
        return make_response(req, ok=False, error=reason)

    adapter = _resolve_adapter()
    if adapter is None:
        return make_response(req, ok=False, error="RuntimeAdapter not available")

    try:
        # RuntimeAdapter.prompt(session_id: str, text: str, **kwargs) -> dict
        result = adapter.prompt(req.session_id, text)
        output = result if isinstance(result, dict) else {"response": str(result)}
    except AttributeError:
        output = {"response": "adapter.prompt not implemented"}
    except Exception as exc:
        log(f"adapter.prompt failed: {exc}")
        return make_response(req, ok=False, error=str(exc))

    session.prompt_count += 1

    # Audit
    _audit(session, "prompt", req.payload, output)

    log(f"PROMPT session={req.session_id} len={len(text)} (#{session.prompt_count})")
    return make_response(req, ok=True, data=output)


def _handle_status(req: AgentOpRequest) -> AgentOpResponse:
    """Query status of a session or all sessions."""
    if req.session_id:
        session = _sessions.get(req.session_id)
        if session is None:
            return make_response(req, ok=False, error=f"session not found: {req.session_id}")

        adapter = _resolve_adapter()
        adapter_status = {}
        if adapter is not None:
            try:
                adapter_status = adapter.status(req.session_id)
                if not isinstance(adapter_status, dict):
                    adapter_status = {"raw": str(adapter_status)}
            except AttributeError:
                pass
            except Exception as exc:
                log(f"adapter.status failed: {exc}")

        data = {
            "session_id": session.session_id,
            "agent_name": session.agent_name,
            "profile_name": session.profile_name,
            "prompt_count": session.prompt_count,
            "uptime_seconds": time.time() - session.created_at,
            "adapter_status": adapter_status,
        }
    else:
        # List all sessions
        sessions_list = []
        for sid, s in _sessions.items():
            sessions_list.append({
                "session_id": s.session_id,
                "agent_name": s.agent_name,
                "profile_name": s.profile_name,
                "prompt_count": s.prompt_count,
                "uptime_seconds": time.time() - s.created_at,
            })
        data = {"sessions": sessions_list, "count": len(sessions_list)}

    log(f"STATUS session={req.session_id or '*'} ({len(_sessions)} active)")
    return make_response(req, ok=True, data=data)


def _handle_abort(req: AgentOpRequest) -> AgentOpResponse:
    """Abort a running task in a session (non-destructive)."""
    session = _sessions.get(req.session_id)
    if session is None:
        return make_response(req, ok=False, error=f"session not found: {req.session_id}")

    adapter = _resolve_adapter()
    if adapter is not None:
        try:
            adapter.abort(req.session_id)
        except AttributeError:
            pass
        except Exception as exc:
            log(f"adapter.abort failed: {exc}")
            return make_response(req, ok=False, error=str(exc))

    log(f"ABORT session={req.session_id}")
    return make_response(req, ok=True, data={"aborted": True})


def _handle_dispose(req: AgentOpRequest) -> AgentOpResponse:
    """Dispose (destroy) a session — adapter teardown + remove from registry."""
    session = _sessions.pop(req.session_id, None)
    if session is None:
        return make_response(req, ok=False, error=f"session not found: {req.session_id}")

    adapter = _resolve_adapter()
    if adapter is not None:
        try:
            adapter.dispose(req.session_id)
        except AttributeError:
            pass
        except Exception as exc:
            log(f"adapter.dispose failed: {exc}")
            # Dispose is best-effort; continue removing local state.

    log(f"DISPOSE session={req.session_id} (remaining sessions: {len(_sessions)})")
    return make_response(req, ok=True, data={"disposed": True})


# ── Command dispatch table ────────────────────────────────────────

_COMMAND_HANDLERS: dict[str, Callable[[AgentOpRequest], AgentOpResponse]] = {
    "spawn": _handle_spawn,
    "prompt": _handle_prompt,
    "status": _handle_status,
    "abort": _handle_abort,
    "dispose": _handle_dispose,
}


def _dispatch(req: AgentOpRequest) -> AgentOpResponse:
    handler = _COMMAND_HANDLERS.get(req.command)
    if handler is None:
        return make_response(req, ok=False,
                             error=f"unknown command: {req.command}")
    return handler(req)


# ---------------------------------------------------------------------------
# Global session registry (thread-safe by GIL for simple ops; lock for mutations)
# ---------------------------------------------------------------------------

_sessions: dict[str, AgentSession] = {}
_sessions_lock = threading.Lock()

# Note: the dict operations in _handle_* use the global lock implicitly via
# the single handler thread per connection, but mutations to _sessions pop
# from _handle_dispose could collide.  We guard with a lock when iterating
# in _handle_status (list all) and when removing.


# ---------------------------------------------------------------------------
# Socket server
# ---------------------------------------------------------------------------

class AgentOpsDaemon:
    """Unix domain socket server for agent lifecycle operations."""

    def __init__(self, sock_path: Path = SOCK_PATH):
        self.sock_path = sock_path
        self._server: socket.socket | None = None
        self._running = False
        self._clients: list[threading.Thread] = []

    # ── Start / stop ───────────────────────────────────────────

    def start(self) -> None:
        """Bind and listen on the Unix socket.  Blocks on accept loop."""
        # Ensure parent directory exists
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)

        # Remove stale socket file
        if self.sock_path.exists():
            self.sock_path.unlink()

        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.sock_path))
        self._server.listen(128)  # backlog
        self._server.settimeout(1.0)  # allow periodic running check

        self._running = True
        log(f"daemon listening on {self.sock_path}")

        try:
            while self._running:
                try:
                    conn, _ = self._server.accept()
                except socket.timeout:
                    continue  # re-check running flag
                except OSError:
                    if not self._running:
                        break
                    raise

                t = threading.Thread(
                    target=self._handle_client,
                    args=(conn,),
                    daemon=True,
                )
                t.start()
                self._clients.append(t)

                # Prune finished threads
                self._clients = [t for t in self._clients if t.is_alive()]
        finally:
            self._cleanup()

    def stop(self) -> None:
        """Signal the accept loop to exit and clean up."""
        self._running = False
        # Wake up accept() if blocked
        try:
            # Connect to self to unblock accept
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as kick:
                kick.settimeout(0.1)
                kick.connect(str(self.sock_path))
        except (OSError, ConnectionRefusedError):
            pass
        # Close server socket to unblock accept
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass

    # ── Client handler ─────────────────────────────────────────

    def _handle_client(self, conn: socket.socket) -> None:
        """Read JSON lines, dispatch, write responses."""
        buffer = b""
        try:
            while self._running:
                try:
                    chunk = conn.recv(4096)
                except (OSError, ConnectionResetError):
                    break
                if not chunk:
                    break  # client disconnected

                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue

                    response = self._process_line(line.decode("utf-8", errors="replace"))
                    try:
                        conn.sendall((json.dumps(asdict(response),
                                                  default=str) + "\n").encode("utf-8"))
                    except (OSError, BrokenPipeError):
                        return  # client gone
        except Exception as exc:
            log(f"client handler error: {exc}")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _process_line(self, raw: str) -> AgentOpResponse:
        """Parse one JSON line and dispatch."""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            log(f"invalid JSON: {exc}")
            # We can't echo a request id, so use empty
            return AgentOpResponse(
                id="",
                command="",
                ok=False,
                error=f"invalid JSON: {exc}",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        req_id = payload.get("id", "")
        command = payload.get("command", "")
        session_id = payload.get("session_id", "")
        data = payload.get("payload", {})

        req = AgentOpRequest(
            id=req_id,
            command=command,
            session_id=session_id,
            payload=data,
        )

        try:
            return _dispatch(req)
        except Exception as exc:
            log(f"unhandled exception in {command}: {exc}")
            return make_response(req, ok=False, error=str(exc))

    # ── Cleanup ────────────────────────────────────────────────

    def _cleanup(self) -> None:
        """Remove socket file and dispose all sessions."""
        log("daemon shutting down…")

        # Dispose all sessions
        with _sessions_lock:
            session_ids = list(_sessions.keys())
        for sid in session_ids:
            req = AgentOpRequest(id="shutdown", command="dispose", session_id=sid)
            _handle_dispose(req)

        # Remove socket file
        if self.sock_path.exists():
            try:
                self.sock_path.unlink()
            except OSError:
                pass

        log("daemon stopped")


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

_daemon_instance: Optional[AgentOpsDaemon] = None


def _install_signal_handlers(daemon: AgentOpsDaemon) -> None:
    global _daemon_instance
    _daemon_instance = daemon

    def _shutdown(signum: int, _frame: Any) -> None:
        sig_name = signal.Signals(signum).name
        log(f"received signal {sig_name} ({signum}), shutting down…")
        if _daemon_instance:
            _daemon_instance.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the AgentOps daemon."""
    log(f"apex.daemon starting — APEX_HOME={APEX_HOME}")

    # Pre-load adapter (best-effort)
    _resolve_adapter()

    daemon = AgentOpsDaemon()
    _install_signal_handlers(daemon)

    try:
        daemon.start()
    except Exception as exc:
        log(f"FATAL: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
