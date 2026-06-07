"""
Claude Code RuntimeAdapter — manages Claude Code sessions via tmux + subprocess.

Uses the official Claude Code CLI at /usr/local/bin/claude (v2.1.143).
Each spawn creates an isolated tmux session running `claude` in a project directory.
Prompts are sent via `tmux send-keys` or subprocess stdin.

Design:
  - Subprocess-based: run `claude -p "prompt" --json` for JSON output.
  - `claude --resume <session_id>` to resume sessions.
  - Session isolation: each spawn creates a new tmux window.
  - Parse JSON output from claude for status/events.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from apex.adapters.base import SessionHandle, SpawnSpec
from apex.protocol import InstanceState, Runtime


# ── Constants ────────────────────────────────────────────────────────────────

CLAUDE_BIN = "/usr/local/bin/claude"
TMUX_BIN = "tmux"
TMUX_SESSION_PREFIX = "apex-claude-"
DEFAULT_TIMEOUT = 300  # seconds


# ── Adapter-level data classes ──────────────────────────────────────────────


@dataclass
class ClaudeSession:
    """Internal tracking of a spawned Claude Code session."""

    session_id: str
    tmux_session: str  # tmux session name
    profile: str = ""
    agent: str = ""
    workdir: str = ""
    pid: int = 0
    created_at: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)
    last_output: str = ""
    state: InstanceState = InstanceState.PENDING

    @property
    def handle(self) -> SessionHandle:
        return SessionHandle(
            id=self.session_id,
            name=f"claude-{self.agent or self.profile}",
            runtime=Runtime.SHELL,  # Claude runs as shell subprocess
            status=self.state.value,
            created_at=str(self.created_at),
            metadata=self.metadata,
        )


# ── Helper functions ─────────────────────────────────────────────────────────


def _run(cmd: list[str], timeout: int = 30, cwd: str = "", capture: bool = True
         ) -> tuple[int, str, str]:
    """Run a command, return (exit_code, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=timeout,
            cwd=cwd or None,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"
    except FileNotFoundError:
        return -2, "", f"command not found: {cmd[0]}"
    except Exception as exc:
        return -3, "", str(exc)


def _tmux_has_session(name: str) -> bool:
    """Check if a tmux session exists."""
    code, stdout, _ = _run([TMUX_BIN, "has-session", "-t", name], timeout=5)
    return code == 0


def _tmux_send_keys(session: str, text: str, enter: bool = True) -> bool:
    """Send keys to a tmux session."""
    args = [TMUX_BIN, "send-keys", "-t", session]
    if enter:
        args.append("Enter")
    # We pipe the text via a shell echo to avoid escaping nightmares
    escaped = shlex.quote(text + ("\n" if enter else ""))
    cmd_str = f"{TMUX_BIN} send-keys -t {shlex.quote(session)} {escaped}"
    code, _, _ = _run(["bash", "-c", cmd_str], timeout=10)
    return code == 0


def _tmux_new_session(name: str, workdir: str, command: str) -> bool:
    """Create a new detached tmux session running a command."""
    code, stdout, stderr = _run([
        TMUX_BIN, "new-session", "-d", "-s", name,
        "-c", workdir,
        *shlex.split(command),
    ], timeout=15)
    return code == 0


def _tmux_kill_session(name: str) -> bool:
    """Kill a tmux session."""
    code, _, _ = _run([TMUX_BIN, "kill-session", "-t", name], timeout=10)
    return code == 0


def _tmux_capture_pane(session: str) -> str:
    """Capture the current content of a tmux pane."""
    code, stdout, _ = _run(
        [TMUX_BIN, "capture-pane", "-t", session, "-p"],
        timeout=10,
    )
    return stdout if code == 0 else ""


def _parse_claude_json_output(text: str) -> dict:
    """Try to extract the last valid JSON object from Claude output."""
    # Claude --json mode outputs one JSON object per line.
    # We look for the last non-empty line that parses as JSON.
    lines = text.strip().split("\n")
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
    # Fallback: try to find a JSON object anywhere
    import re
    # Look for JSON objects (curly braces)
    matches = re.findall(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    for match in reversed(matches):
        try:
            return json.loads(match)
        except (json.JSONDecodeError, ValueError):
            continue
    return {}


# ── ClaudeCodeAdapter ────────────────────────────────────────────────────────


class ClaudeCodeAdapter:
    """RuntimeAdapter implementation for Claude Code CLI.

    Manages Claude Code sessions via tmux for isolation,
    using subprocess for command execution.

    Constructor:
        ClaudeCodeAdapter(apex_home: Path | None = None)
    """

    def __init__(self, apex_home: Path | str | None = None):
        """Initialize the Claude Code adapter.

        Args:
            apex_home: Root directory for Apex data (used as base workdir).
        """
        self._apex_home = Path(apex_home) if apex_home else Path.home() / ".apex"
        self._sessions: dict[str, ClaudeSession] = {}
        self._workdir_base = self._apex_home / "claude-sessions"
        self._workdir_base.mkdir(parents=True, exist_ok=True)

    # ── Public API (6 methods) ───────────────────────────────────────────

    def spawn(self, profile: str = "", agent: str = "",
              metadata: dict | None = None) -> str:
        """Spawn a new Claude Code session in an isolated tmux window.

        Args:
            profile: Profile name for the agent.
            agent: Agent identifier.
            metadata: Optional metadata dict.

        Returns:
            session_id: Unique session identifier string.
        """
        metadata = metadata or {}
        session_id = str(uuid.uuid4())[:8]
        tmux_name = f"{TMUX_SESSION_PREFIX}{session_id}"

        # Create a dedicated workdir for this session
        workdir = self._workdir_base / session_id
        workdir.mkdir(parents=True, exist_ok=True)

        # Start Claude in the tmux session (interactive mode, no initial prompt)
        # We launch claude in a shell that stays alive.
        launch_cmd = f"{shlex.quote(CLAUDE_BIN)}"
        ok = _tmux_new_session(tmux_name, str(workdir), launch_cmd)

        if not ok:
            # Fallback: try without tmux, track as simple subprocess session
            pass

        session = ClaudeSession(
            session_id=session_id,
            tmux_session=tmux_name,
            profile=profile,
            agent=agent,
            workdir=str(workdir),
            metadata=metadata,
            state=InstanceState.RUNNING,
        )
        self._sessions[session_id] = session
        return session_id

    def prompt(self, session_id: str, text: str, **kwargs) -> dict:
        """Send a prompt to an existing Claude Code session.

        Uses `claude -p "text" --json` in a subprocess with --resume
        to maintain session continuity, or falls back to tmux send-keys.

        Args:
            session_id: Session identifier.
            text: Prompt text to send.
            **kwargs: Additional options (model, temperature, etc.).

        Returns:
            dict with keys: response, session_id, status, etc.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return {"error": f"session not found: {session_id}", "ok": False}

        # Strategy: Use `claude -p "prompt" --json --resume <internal_id>`
        # For now, we use a standalone subprocess call with --json output.
        # In a full implementation, --resume ties back to Claude's own
        # session management.

        json_flag = kwargs.pop("json_output", True)
        timeout = kwargs.pop("timeout", DEFAULT_TIMEOUT)

        cmd = [CLAUDE_BIN, "-p", text]
        if json_flag:
            cmd.append("--json")
        # If Claude supports --resume, we could pass a stored resume id
        resume_id = kwargs.pop("resume_id", None)
        if resume_id:
            cmd.extend(["--resume", resume_id])

        # Add any extra flags from kwargs
        for key, val in kwargs.items():
            if val is True:
                cmd.append(f"--{key}")
            elif val is not False and val is not None:
                cmd.append(f"--{key}")
                cmd.append(str(val))

        exit_code, stdout, stderr = _run(cmd, timeout=timeout, cwd=session.workdir)

        response_data: dict = {
            "session_id": session_id,
            "ok": exit_code == 0,
            "exit_code": exit_code,
            "raw_stdout": stdout[:5000] if stdout else "",
            "raw_stderr": stderr[:2000] if stderr else "",
        }

        # Parse JSON output if available
        if stdout:
            parsed = _parse_claude_json_output(stdout)
            if parsed:
                response_data["json_output"] = parsed
                # Extract text content from common Claude JSON fields
                response_data["response"] = (
                    parsed.get("content") or
                    parsed.get("text") or
                    parsed.get("result") or
                    parsed.get("message", {}).get("content") or
                    str(parsed)
                )
            else:
                response_data["response"] = stdout.strip()
        elif stderr:
            response_data["response"] = stderr.strip()
        else:
            response_data["response"] = ""

        # Update session state
        session.last_output = stdout
        if exit_code != 0:
            session.state = InstanceState.FAILED
        else:
            session.state = InstanceState.RUNNING

        return response_data

    def steer(self, session_id: str, instruction: str, **kwargs) -> dict:
        """Send a steering instruction to modify Claude's behavior mid-session.

        Steering sends a high-priority system-level instruction that modifies
        the agent's current trajectory without restarting.

        Args:
            session_id: Session identifier.
            instruction: Steering instruction text.
            **kwargs: Additional options.

        Returns:
            dict with status and response.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return {"error": f"session not found: {session_id}", "ok": False}

        # Steering is implemented as an interrupt + re-prompt with system context.
        # We send Ctrl+C to interrupt any running process, then send the steer
        # instruction as a system message.

        # First, interrupt any running process in the tmux session
        _tmux_send_keys(session.tmux_session, "\x03", enter=False)  # Ctrl+C
        time.sleep(0.3)

        # Then send the steering instruction as a prefixed system message
        steer_text = f"/system {instruction}"
        sent = _tmux_send_keys(session.tmux_session, steer_text, enter=True)

        return {
            "session_id": session_id,
            "ok": sent,
            "steer_instruction": instruction,
            "method": "tmux_send_keys",
        }

    def abort(self, session_id: str) -> dict:
        """Abort the currently running task in a session.

        Sends interrupt signal (Ctrl+C) and returns status.

        Args:
            session_id: Session identifier.

        Returns:
            dict with status.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return {"error": f"session not found: {session_id}", "ok": False}

        # Send Ctrl+C to tmux session
        _tmux_send_keys(session.tmux_session, "\x03", enter=False)
        time.sleep(0.2)

        session.state = InstanceState.STOPPED

        return {
            "session_id": session_id,
            "ok": True,
            "aborted": True,
            "state": session.state.value,
        }

    def status(self, session_id: str) -> dict:
        """Query the status of a Claude Code session.

        Checks tmux session existence and parses latest JSON output
        from the pane for status/events.

        Args:
            session_id: Session identifier.

        Returns:
            dict with status information.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return {
                "session_id": session_id,
                "exists": False,
                "error": f"session not found: {session_id}",
                "state": InstanceState.UNKNOWN.value,
            }

        # Check if tmux session is alive
        tmux_alive = _tmux_has_session(session.tmux_session)
        pane_content = ""

        if tmux_alive:
            pane_content = _tmux_capture_pane(session.tmux_session)
            session.state = InstanceState.RUNNING
        else:
            if session.state == InstanceState.RUNNING:
                session.state = InstanceState.STOPPED

        # Parse latest JSON from pane output
        parsed_output = {}
        if pane_content:
            parsed_output = _parse_claude_json_output(pane_content)

        uptime = time.time() - session.created_at

        return {
            "session_id": session_id,
            "tmux_session": session.tmux_session,
            "tmux_alive": tmux_alive,
            "state": session.state.value,
            "profile": session.profile,
            "agent": session.agent,
            "workdir": session.workdir,
            "uptime_seconds": round(uptime, 1),
            "last_output_length": len(session.last_output),
            "pane_content_preview": pane_content[-500:] if pane_content else "",
            "parsed_output": parsed_output,
        }

    def dispose(self, session_id: str) -> dict:
        """Dispose (destroy) a Claude Code session.

        Kills the tmux session and removes local tracking data.
        This is a best-effort operation — errors are logged but not raised.

        Args:
            session_id: Session identifier.

        Returns:
            dict with disposal status.
        """
        session = self._sessions.pop(session_id, None)
        if session is None:
            return {
                "session_id": session_id,
                "ok": False,
                "error": f"session not found: {session_id}",
            }

        # Kill tmux session (best-effort)
        tmux_killed = _tmux_kill_session(session.tmux_session)

        # Clean up workdir if it was created by us
        workdir = Path(session.workdir)
        if workdir.exists() and str(workdir).startswith(str(self._workdir_base)):
            try:
                import shutil
                shutil.rmtree(workdir, ignore_errors=True)
            except Exception:
                pass

        return {
            "session_id": session_id,
            "ok": True,
            "disposed": True,
            "tmux_killed": tmux_killed,
            "uptime_seconds": round(time.time() - session.created_at, 1),
        }

    # ── Protocol-compatible aliases ─────────────────────────────────────

    def stop(self, handle: SessionHandle) -> bool:
        """Stop the instance (Protocol compatibility)."""
        result = self.abort(handle.id)
        return result.get("ok", False)

    def exec(self, handle: SessionHandle, command: str) -> tuple[int, str, str]:
        """Execute a command inside the instance (Protocol compatibility).

        Returns (exit_code, stdout, stderr).
        """
        session = self._sessions.get(handle.id)
        if session is None:
            return (-1, "", "session not found")
        return _run(["bash", "-c", command], cwd=session.workdir, timeout=60)

    # ── Utility ─────────────────────────────────────────────────────────

    def list_sessions(self) -> list[dict]:
        """List all tracked Claude Code sessions."""
        result = []
        for sid, sess in self._sessions.items():
            result.append({
                "session_id": sid,
                "tmux_session": sess.tmux_session,
                "state": sess.state.value,
                "profile": sess.profile,
                "agent": sess.agent,
                "uptime_seconds": round(time.time() - sess.created_at, 1),
            })
        return result
