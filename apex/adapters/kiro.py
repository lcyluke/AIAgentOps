"""Kiro Runtime Adapter — headless Kiro integration via tmux.

Implements RuntimeAdapter Protocol from apex.adapters.base.
Kiro is a VS Code-style editor; this adapter runs it in headless
tmux sessions for agent orchestration.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from apex.adapters.base import (
    InstanceStatus,
    RuntimeAdapter,
    SessionHandle,
    SpawnSpec,
)


class KiroAdapter(RuntimeAdapter):
    """Headless Kiro adapter using tmux for session isolation."""

    name = "kiro"

    def __init__(self, kiro_bin: str = "kiro", tmux_bin: str = "tmux"):
        self._kiro = kiro_bin
        self._tmux = tmux_bin
        self._sessions: dict[str, dict] = {}

    # ── RuntimeAdapter implementation ──────────────────────────

    def spawn(self, spec: SpawnSpec) -> SessionHandle:
        sid = str(uuid.uuid4())[:8]
        session_name = f"{spec.agent}-{sid}"
        tmux_session = f"apex-{session_name}"

        # Create tmux session with kiro in the target directory
        cwd = spec.cwd or os.getcwd()
        cmd = (
            f"cd {cwd} && {self._kiro} "
            f"--wait "
            f"'{spec.brief}'"
        )

        subprocess.run(
            [self._tmux, "new-session", "-d", "-s", tmux_session, "bash", "-c", cmd],
            capture_output=True, timeout=10,
        )

        h = SessionHandle(
            name=session_name,
            session_id=sid,
            runtime="kiro",
            tmux_session=tmux_session,
        )

        self._sessions[sid] = {
            "handle": h,
            "spec": spec,
            "spawned_at": datetime.now().isoformat(),
            "prompts": 0,
        }

        return h

    def prompt(self, h: SessionHandle, text: str) -> None:
        self._send_keys(h, text)
        self._sessions[h.session_id]["prompts"] += 1

    def steer(self, h: SessionHandle, text: str) -> None:
        # Steer is same mechanism as prompt in headless mode
        self._send_keys(h, text)

    def abort(self, h: SessionHandle) -> None:
        self._send_keys(h, "\x03")  # Ctrl+C

    def status(self, h: SessionHandle) -> InstanceStatus:
        alive = self._session_alive(h.tmux_session)
        state = "running" if alive else "disposed"
        return InstanceStatus(
            state=state,
            last_event_ts=datetime.now().isoformat(),
            detail=f"prompts: {self._sessions.get(h.session_id, {}).get('prompts', 0)}",
        )

    def dispose(self, h: SessionHandle) -> None:
        subprocess.run(
            [self._tmux, "kill-session", "-t", h.tmux_session],
            capture_output=True, timeout=5,
        )
        self._sessions.pop(h.session_id, None)

    # ── Internal helpers ────────────────────────────────────────

    def _send_keys(self, h: SessionHandle, text: str) -> None:
        """Send text to tmux session."""
        escaped = text.replace('"', '\\"')
        subprocess.run(
            [self._tmux, "send-keys", "-t", h.tmux_session, escaped, "Enter"],
            capture_output=True, timeout=5,
        )

    def _session_alive(self, name: str) -> bool:
        """Check if tmux session exists."""
        r = subprocess.run(
            [self._tmux, "has-session", "-t", name],
            capture_output=True, timeout=3,
        )
        return r.returncode == 0

    def list_sessions(self) -> list[dict]:
        """List all managed sessions."""
        return [
            {"session_id": sid, "name": s["handle"].name, "prompts": s["prompts"]}
            for sid, s in self._sessions.items()
        ]

    def dispose_all(self) -> int:
        """Kill all managed tmux sessions. Returns count disposed."""
        count = 0
        for sid in list(self._sessions.keys()):
            h = self._sessions[sid]["handle"]
            try:
                self.dispose(h)
                count += 1
            except Exception:
                pass
        return count
