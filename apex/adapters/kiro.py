"""Kiro Runtime Adapter — ACP (primary) + tmux (fallback) integration.

Implements RuntimeAdapter Protocol from apex.adapters.base.
ACP (Agent Communication Protocol) is the primary channel; tmux is retained
as a backward-compatible fallback when ACP is unavailable.

§4.2 Upgrade: spawn/prompt/steer/abort mapped to ACP methods with streaming
events from the ACP subprocess parsed into ApexEvent objects.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from apex.adapters.base import (
    InstanceStatus,
    RuntimeAdapter,
    SessionHandle,
    SpawnSpec,
)

logger = logging.getLogger(__name__)


class KiroAdapter(RuntimeAdapter):
    """Kiro adapter with ACP protocol support and tmux fallback.

    Priority:
    1. ACP (JSON-RPC 2.0 over subprocess stdio) — primary channel
    2. tmux headless session — fallback when ACP binary not found

    Usage::

        adapter = KiroAdapter()
        adapter.connect_acp()          # pre-establish ACP session (optional)
        h = adapter.spawn(spec)        # uses ACP if available, else tmux
        adapter.prompt(h, "write hello world")
        adapter.dispose(h)
    """

    name = "kiro"

    def __init__(
        self,
        kiro_bin: str = "kiro",
        tmux_bin: str = "tmux",
        use_acp: bool = True,
        acp_bin: Optional[str] = None,
    ):
        """Args:
            kiro_bin: Path to the kiro binary (for tmux fallback and ACP).
            tmux_bin: Path to tmux binary.
            use_acp: Whether to prefer ACP protocol when available.
            acp_bin: Path to ACP-capable binary (defaults to *kiro_bin*).
        """
        self._kiro = kiro_bin
        self._tmux = tmux_bin
        self._acp_bin = acp_bin or kiro_bin
        self._prefer_acp = use_acp
        self._acp_available: Optional[bool] = None  # lazy probe

        # Session registry: sid → {handle, spec, spawned_at, prompts, mode, acp_client?}
        self._sessions: dict[str, dict] = {}

    # ── RuntimeAdapter implementation ──────────────────────────────────

    def spawn(self, spec: SpawnSpec) -> SessionHandle:
        """Spawn a new agent session.

        Tries ACP first; falls back to tmux if ACP is unavailable or
        ``use_acp=False``.
        """
        sid = str(uuid.uuid4())[:8]
        session_name = f"{spec.agent}-{sid}"

        # Probe ACP lazily
        if self._prefer_acp and self._acp_available is None:
            self._acp_available = self._probe_acp()

        acp_client = None
        spawned_mode = "tmux"

        if self._prefer_acp and self._acp_available:
            try:
                # Lazy import to keep the dependency optional
                from apex.adapters.acp import AcpClient

                acp_client = AcpClient(
                    acp_bin=self._acp_bin,
                    startup_timeout=30.0,
                    request_timeout=120.0,
                )
                acp_client.connect(
                    cwd=spec.cwd,
                    env=spec.env,
                )
                # Send initial prompt as the "spawn brief"
                acp_client.prompt(spec.brief, timeout=30.0)
                spawned_mode = "acp"
                logger.info("KiroAdapter: spawned %s via ACP (session=%s)", session_name, acp_client.session_id)
            except Exception as exc:
                logger.warning("ACP spawn failed (%s), falling back to tmux", exc)
                if acp_client:
                    try:
                        acp_client.dispose()
                    except Exception:
                        pass
                acp_client = None
                # Fall through to tmux
                self._acp_available = False  # don't retry ACP this session

        tmux_session = ""
        if acp_client is None:
            # ── tmux fallback ──
            tmux_session = f"apex-{session_name}"
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

        entry = {
            "handle": h,
            "spec": spec,
            "spawned_at": datetime.now(timezone.utc).isoformat(),
            "prompts": 0,
            "mode": spawned_mode,
        }
        if acp_client:
            entry["acp_client"] = acp_client
        self._sessions[sid] = entry

        return h

    def prompt(self, h: SessionHandle, text: str) -> None:
        """Send a prompt to the agent session."""
        entry = self._sessions.get(h.session_id)
        if entry is None:
            raise KeyError(f"Unknown session: {h.session_id}")

        if entry.get("mode") == "acp":
            acp = entry.get("acp_client")
            if acp is None:
                raise RuntimeError("ACP session missing client")
            acp.prompt(text)
        else:
            self._send_keys(h, text)

        entry["prompts"] += 1

    def steer(self, h: SessionHandle, text: str) -> None:
        """Send steering guidance to the agent session."""
        entry = self._sessions.get(h.session_id)
        if entry is None:
            raise KeyError(f"Unknown session: {h.session_id}")

        if entry.get("mode") == "acp":
            acp = entry.get("acp_client")
            if acp is None:
                raise RuntimeError("ACP session missing client")
            acp.steer(text)
        else:
            self._send_keys(h, text)

    def abort(self, h: SessionHandle) -> None:
        """Abort the current agent operation."""
        entry = self._sessions.get(h.session_id)
        if entry is None:
            raise KeyError(f"Unknown session: {h.session_id}")

        if entry.get("mode") == "acp":
            acp = entry.get("acp_client")
            if acp is None:
                raise RuntimeError("ACP session missing client")
            try:
                acp.abort(timeout=5.0)
            except Exception:
                # Force-kill on abort failure
                try:
                    acp.dispose()
                except Exception:
                    pass
        else:
            self._send_keys(h, "\x03")  # Ctrl+C

    def status(self, h: SessionHandle) -> InstanceStatus:
        """Return instance status, including ACP stream state when applicable."""
        entry = self._sessions.get(h.session_id)
        if entry is None:
            return InstanceStatus(
                state="disposed",
                last_event_ts=datetime.now(timezone.utc).isoformat(),
                detail="session not found",
            )

        mode = entry.get("mode", "tmux")

        if mode == "acp":
            acp = entry.get("acp_client")
            if acp is None:
                state = "disposed"
                detail = "acp client missing"
            elif acp.is_alive():
                acp_status = acp.status()
                state = "running"
                detail = (
                    f"acp | prompts: {entry.get('prompts', 0)} | "
                    f"events_buffered: {acp_status.get('events_buffered', 0)} | "
                    f"pid: {acp_status.get('pid')}"
                )
            else:
                state = "disposed"
                returncode = None
                if acp._process:  # noqa: SLF001 (internal for diagnostics)
                    returncode = acp._process.returncode  # noqa: SLF001
                detail = f"acp exited rc={returncode} | prompts: {entry.get('prompts', 0)}"
        else:
            alive = self._session_alive(h.tmux_session)
            state = "running" if alive else "disposed"
            detail = f"tmux | prompts: {entry.get('prompts', 0)}"

        return InstanceStatus(
            state=state,
            last_event_ts=datetime.now(timezone.utc).isoformat(),
            detail=detail,
        )

    def dispose(self, h: SessionHandle) -> None:
        """Dispose the session (ACP disconnect or tmux kill)."""
        entry = self._sessions.pop(h.session_id, None)
        if entry is None:
            return

        if entry.get("mode") == "acp":
            acp = entry.get("acp_client")
            if acp:
                try:
                    acp.dispose()
                except Exception:
                    pass
        else:
            subprocess.run(
                [self._tmux, "kill-session", "-t", h.tmux_session],
                capture_output=True, timeout=5,
            )

    # ── ACP explicit management ────────────────────────────────────────

    def connect_acp(self) -> bool:
        """Pre-establish an ACP connection (probe + warm-up).

        This can be called before ``spawn()`` to check ACP availability and
        warm up resources. Individual ``spawn()`` calls still create their
        own ACP sessions.

        Returns:
            True if ACP is available and responding.
        """
        if self._acp_available is None:
            self._acp_available = self._probe_acp()
        return self._acp_available

    def acp_events_for(self, h: SessionHandle) -> list:
        """Return buffered ApexEvents for an ACP-backed session.

        Returns an empty list for tmux-backed sessions.
        """
        from apex.protocol import ApexEvent  # noqa: F811 (local for safety)

        entry = self._sessions.get(h.session_id)
        if entry is None:
            return []
        acp = entry.get("acp_client")
        if acp is None:
            return []
        return acp.flush_events()

    # ── Internal helpers ───────────────────────────────────────────────

    def _probe_acp(self) -> bool:
        """Check if the ACP binary is available and responsive."""
        try:
            from apex.adapters.acp import check_acp_available

            return check_acp_available(self._acp_bin)
        except ImportError:
            logger.debug("apex.adapters.acp module not importable")
            return False
        except Exception:
            return False

    def _send_keys(self, h: SessionHandle, text: str) -> None:
        """Send text to tmux session."""
        escaped = text.replace('"', '\\"')
        subprocess.run(
            [self._tmux, "send-keys", "-t", h.tmux_session, escaped, "Enter"],
            capture_output=True, timeout=5,
        )

    def _session_alive(self, name: str) -> bool:
        """Check if tmux session exists."""
        if not name:
            return False
        r = subprocess.run(
            [self._tmux, "has-session", "-t", name],
            capture_output=True, timeout=3,
        )
        return r.returncode == 0

    # ── Bulk operations ────────────────────────────────────────────────

    def list_sessions(self) -> list[dict]:
        """List all managed sessions."""
        return [
            {
                "session_id": sid,
                "name": s["handle"].name,
                "prompts": s["prompts"],
                "mode": s.get("mode", "tmux"),
            }
            for sid, s in self._sessions.items()
        ]

    def dispose_all(self) -> int:
        """Kill all managed sessions. Returns count disposed."""
        count = 0
        for sid in list(self._sessions.keys()):
            h = self._sessions[sid]["handle"]
            try:
                self.dispose(h)
                count += 1
            except Exception:
                pass
        return count
