"""ACP Protocol Client — Agent Communication Protocol for Kiro.

ACP is phi's native agent protocol. This module upgrades KiroAdapter
from tmux-only headless to ACP-first with tmux fallback.

ACP uses JSON-RPC over stdio to a Kiro subprocess:
- initialize → get capabilities
- tools/list → discover available tools  
- tools/call → execute a tool
- prompts/get → retrieve prompt templates
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from apex.adapters.base import InstanceStatus, SessionHandle, SpawnSpec


@dataclass
class AcpResponse:
    """Parsed ACP JSON-RPC response."""
    ok: bool
    data: dict = field(default_factory=dict)
    error: str = ""
    raw: str = ""


class AcpClient:
    """ACP JSON-RPC client over subprocess stdio."""

    def __init__(self, kiro_bin: str = "kiro", timeout: int = 30):
        self._kiro = kiro_bin
        self._timeout = timeout
        self._process: Optional[subprocess.Popen] = None
        self._session_id: str = ""

    # ── Connection ───────────────────────────────────────────

    def connect(self, cwd: str = ".") -> bool:
        """Launch kiro in ACP mode and perform handshake."""
        try:
            self._process = subprocess.Popen(
                [self._kiro, "--acp", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True, cwd=cwd,
            )
            # Handshake
            resp = self._call("initialize", {
                "protocolVersion": "1.0",
                "clientInfo": {"name": "apex", "version": "1.0.0"},
            })
            if resp.ok:
                self._session_id = resp.data.get("sessionId", str(uuid.uuid4())[:8])
                return True
            return False
        except Exception:
            return False

    def disconnect(self):
        """Terminate the kiro subprocess."""
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                self._process.kill()
            self._process = None

    # ── ACP methods ──────────────────────────────────────────

    def call_tool(self, tool_name: str, args: dict | None = None) -> AcpResponse:
        """Execute a tool via tools/call."""
        return self._call("tools/call", {
            "name": tool_name,
            "arguments": args or {},
        })

    def list_tools(self) -> AcpResponse:
        """List available tools."""
        return self._call("tools/list", {})

    def send_prompt(self, text: str) -> AcpResponse:
        """Send a prompt/steer text to the agent."""
        return self._call("prompts/execute", {
            "prompt": text,
        })

    def cancel(self) -> AcpResponse:
        """Cancel current operation."""
        return self._call("tools/call", {
            "name": "cancel",
            "arguments": {},
        })

    def status(self) -> AcpResponse:
        """Query agent status."""
        return self._call("status", {})

    # ── Internal ─────────────────────────────────────────────

    def _call(self, method: str, params: dict) -> AcpResponse:
        if not self._process or self._process.poll() is not None:
            return AcpResponse(ok=False, error="process not running")

        assert self._process.stdin and self._process.stdout
        request = json.dumps({
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4())[:8],
            "method": method,
            "params": params,
        }) + "\n"

        try:
            self._process.stdin.write(request)
            self._process.stdin.flush()
            raw = self._process.stdout.readline()
            if raw:
                data = json.loads(raw)
                if "error" in data:
                    return AcpResponse(ok=False, error=str(data["error"]), raw=raw)
                return AcpResponse(ok=True, data=data.get("result", {}), raw=raw)
        except Exception as e:
            return AcpResponse(ok=False, error=str(e))

        return AcpResponse(ok=False, error="no response", raw="")


# ── ACP-upgraded spawn helper ────────────────────────────────

def acp_spawn(spec: SpawnSpec, kiro_bin: str = "kiro") -> tuple[Optional[SessionHandle], Optional[AcpClient]]:
    """Spawn an ACP session from a SpawnSpec.
    
    Returns (SessionHandle, AcpClient) on success, (None, None) on failure.
    Caller should fall back to tmux on failure.
    """
    client = AcpClient(kiro_bin=kiro_bin)
    if not client.connect(cwd=spec.cwd):
        return None, None

    # Send initial task brief via prompt
    client.send_prompt(spec.brief)

    h = SessionHandle(
        name=f"{spec.agent}-{client._session_id}",
        session_id=client._session_id,
        runtime="kiro-acp",
        tmux_session="",  # ACP doesn't use tmux
    )

    return h, client
