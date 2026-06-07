"""Hermes Runtime Adapter — subprocess-based integration with Hermes CLI.

Implements the 6-method RuntimeAdapter Protocol:
  - spawn  → create Hermes profile + start gateway
  - prompt → hermes chat -q via subprocess
  - steer  → hermes chat -q with steering prefix
  - abort  → kill the running chat subprocess
  - status → check gateway health endpoint
  - dispose → stop gateway + remove profile

Hermes CLI is expected at ~/.local/bin/hermes (v0.14.0).
Gateway health: GET http://localhost:8765/health
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib import request
from urllib.error import URLError

logger = logging.getLogger(__name__)

# ── Hermes paths and defaults ─────────────────────────────────────────────

DEFAULT_HERMES_BIN = os.path.expanduser("~/.local/bin/hermes")
DEFAULT_GATEWAY_PORT = 8765
DEFAULT_GATEWAY_HOST = "127.0.0.1"
GATEWAY_HEALTH_URL = f"http://{DEFAULT_GATEWAY_HOST}:{DEFAULT_GATEWAY_PORT}/health"
HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))


# ── Adapter types (matching apex-orchestrator RuntimeAdapter) ────────────


@dataclass
class SessionHandle:
    """Opaque handle to a Hermes runtime session."""
    name: str
    session_id: str
    runtime: str = "hermes"
    tmux_session: str = ""


@dataclass
class InstanceStatus:
    """Status snapshot for a Hermes session."""
    state: str  # "running" | "disposed" | "stopped" | "failed" | "unknown"
    last_event_ts: str
    detail: Optional[str] = None


@dataclass
class SpawnSpec:
    """Input specification for spawning a Hermes agent session."""
    agent: str
    instance: str
    brief: str
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    max_steps: int = 20


# ── HermesAdapter ────────────────────────────────────────────────────────


class HermesAdapter:
    """Hermes CLI adapter implementing the RuntimeAdapter protocol.

    Design:
      - Session = Hermes profile session
      - spawn:   ``hermes profile create`` + ``hermes gateway start``
      - prompt:  ``hermes chat -q "..."`` via subprocess
      - steer:   same as prompt but with a steering prefix
      - status:  check gateway health endpoint (GET /health)
      - dispose: stop gateway + delete profile
    """

    name = "hermes"

    def __init__(
        self,
        hermes_bin: str = DEFAULT_HERMES_BIN,
        gateway_port: int = DEFAULT_GATEWAY_PORT,
        check_binary: bool = True,
    ):
        """Args:
            hermes_bin: Path to the hermes CLI binary.
            gateway_port: Port for the Hermes gateway REST API.
            check_binary: If True (default), verify hermes binary exists on spawn.
        """
        self._hermes = hermes_bin
        self._port = gateway_port
        self._check_binary = check_binary
        self._health_url = f"http://{DEFAULT_GATEWAY_HOST}:{gateway_port}/health"

        # Session registry: session_id → dict with handle, spec, profile_name,
        # chat_process, spawned_at, prompts
        self._sessions: dict[str, dict] = {}

    # ── RuntimeAdapter implementation ──────────────────────────────────

    def spawn(self, spec: SpawnSpec) -> SessionHandle:
        """Spawn a new Hermes session.

        1. Create a Hermes profile for the session.
        2. Start the Hermes gateway for that profile.
        3. Return a SessionHandle.

        Args:
            spec: SpawnSpec with agent name, instance id, brief, cwd, etc.

        Returns:
            SessionHandle tracking the spawned session.

        Raises:
            RuntimeError: If profile creation or gateway start fails.
        """
        sid = str(uuid.uuid4())[:8]
        session_name = f"{spec.agent}-{sid}"
        profile_name = f"apex-{session_name}"

        self._ensure_hermes_installed()

        # Step 1 — create profile
        try:
            subprocess.run(
                [self._hermes, "profile", "create", profile_name],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            logger.info("HermesAdapter: created profile %s", profile_name)
        except subprocess.CalledProcessError as exc:
            msg = f"hermes profile create failed: {exc.stderr.strip()}"
            logger.error(msg)
            raise RuntimeError(msg) from exc

        # Step 2 — start gateway
        try:
            subprocess.run(
                [
                    self._hermes, "gateway", "start",
                    profile_name,
                    "--port", str(self._port),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            logger.info(
                "HermesAdapter: gateway started for profile %s on port %d",
                profile_name, self._port,
            )
        except subprocess.CalledProcessError as exc:
            msg = f"hermes gateway start failed: {exc.stderr.strip()}"
            logger.error(msg)
            # Try to clean up the profile
            self._delete_profile(profile_name)
            raise RuntimeError(msg) from exc

        # Wait briefly for the gateway to become ready
        self._wait_for_gateway(timeout=10)

        h = SessionHandle(
            name=session_name,
            session_id=sid,
            runtime="hermes",
            tmux_session="",  # Hermes doesn't use tmux
        )

        self._sessions[sid] = {
            "handle": h,
            "spec": spec,
            "profile_name": profile_name,
            "chat_process": None,
            "spawned_at": datetime.now(timezone.utc).isoformat(),
            "prompts": 0,
        }

        return h

    def prompt(self, h: SessionHandle, text: str) -> None:
        """Send a prompt to the Hermes agent session.

        Executes: ``hermes chat -q "<text>" --profile <profile_name>``
        This is a synchronous subprocess call (headless execution).

        Args:
            h: SessionHandle from spawn().
            text: The prompt text.

        Raises:
            KeyError: If session not found.
        """
        entry = self._sessions.get(h.session_id)
        if entry is None:
            raise KeyError(f"Unknown session: {h.session_id}")

        profile_name = entry["profile_name"]

        try:
            result = subprocess.run(
                [
                    self._hermes, "chat",
                    "-q", text,
                    "--profile", profile_name,
                ],
                capture_output=True,
                text=True,
                timeout=300,
                cwd=entry["spec"].cwd,
            )
            if result.returncode != 0:
                logger.warning(
                    "HermesAdapter: prompt returned non-zero: %s",
                    result.stderr.strip(),
                )
            entry["prompts"] += 1
        except subprocess.TimeoutExpired:
            logger.error("HermesAdapter: prompt timed out after 300s")
            raise

    def steer(self, h: SessionHandle, text: str) -> None:
        """Send steering guidance to the Hermes agent session.

        This wraps the steering text in a specific prefix and sends it
        via the same ``hermes chat -q`` mechanism. Semantically similar
        to ``prompt`` but used for mid-flight guidance.

        Args:
            h: SessionHandle from spawn().
            text: The steering text.

        Raises:
            KeyError: If session not found.
        """
        entry = self._sessions.get(h.session_id)
        if entry is None:
            raise KeyError(f"Unknown session: {h.session_id}")

        # Prefix to distinguish steering from regular prompts
        steer_text = f"[STEER] {text}"
        self.prompt(h, steer_text)

    def abort(self, h: SessionHandle) -> None:
        """Abort the current operation.

        If a chat subprocess is running, kill it.

        Args:
            h: SessionHandle from spawn().

        Raises:
            KeyError: If session not found.
        """
        entry = self._sessions.get(h.session_id)
        if entry is None:
            raise KeyError(f"Unknown session: {h.session_id}")

        proc = entry.get("chat_process")
        if proc is not None and proc.poll() is None:
            logger.info("HermesAdapter: aborting chat process for session %s", h.session_id)
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass  # process already dead or stuck
            entry["chat_process"] = None

    def status(self, h: SessionHandle) -> InstanceStatus:
        """Return the current status of the Hermes session.

        Checks gateway health endpoint (GET /health).
        Falls back to process-based checks if gateway is unreachable.

        Args:
            h: SessionHandle from spawn().

        Returns:
            InstanceStatus with state and detail.
        """
        entry = self._sessions.get(h.session_id)
        if entry is None:
            return InstanceStatus(
                state="disposed",
                last_event_ts=datetime.now(timezone.utc).isoformat(),
                detail="session not found",
            )

        # Check gateway health
        gateway_alive = self._gateway_healthy()
        if gateway_alive:
            state = "running"
        else:
            # Gateway down — check if chat process is still alive
            proc = entry.get("chat_process")
            if proc is not None and proc.poll() is None:
                state = "running"
            else:
                state = "stopped"

        detail = (
            f"hermes | profile: {entry['profile_name']} | "
            f"gateway: {'up' if gateway_alive else 'down'} | "
            f"prompts: {entry.get('prompts', 0)}"
        )

        return InstanceStatus(
            state=state,
            last_event_ts=datetime.now(timezone.utc).isoformat(),
            detail=detail,
        )

    def dispose(self, h: SessionHandle) -> None:
        """Dispose the Hermes session.

        Stops gateway and deletes profile. Cleans up any running processes.

        Args:
            h: SessionHandle from spawn().
        """
        entry = self._sessions.pop(h.session_id, None)
        if entry is None:
            return

        # Abort any running chat process
        if entry.get("chat_process") is not None:
            try:
                self.abort(h)
            except Exception:
                pass

        # Stop gateway
        profile_name = entry["profile_name"]
        try:
            subprocess.run(
                [self._hermes, "gateway", "stop", profile_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            logger.info("HermesAdapter: stopped gateway for %s", profile_name)
        except Exception as exc:
            logger.warning("HermesAdapter: gateway stop error: %s", exc)

        # Delete profile
        self._delete_profile(profile_name)

    # ── Asynchronous chat ───────────────────────────────────────────────

    def chat_async(self, h: SessionHandle, text: str) -> subprocess.Popen:
        """Start a non-blocking chat subprocess.

        The subprocess runs in the background; use ``chat_result()`` or
        ``abort()`` to manage it.

        Args:
            h: SessionHandle from spawn().
            text: The prompt text.

        Returns:
            The subprocess.Popen object.

        Raises:
            KeyError: If session not found.
        """
        entry = self._sessions.get(h.session_id)
        if entry is None:
            raise KeyError(f"Unknown session: {h.session_id}")

        proc = subprocess.Popen(
            [
                self._hermes, "chat",
                "-q", text,
                "--profile", entry["profile_name"],
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=entry["spec"].cwd,
        )
        entry["chat_process"] = proc
        entry["prompts"] += 1
        return proc

    def chat_result(self, h: SessionHandle, timeout: float = 300) -> tuple[int, str, str]:
        """Wait for and return the result of an async chat.

        Args:
            h: SessionHandle from spawn().
            timeout: Max seconds to wait.

        Returns:
            (exit_code, stdout, stderr)

        Raises:
            KeyError: If session not found.
            RuntimeError: If no chat process is running.
        """
        entry = self._sessions.get(h.session_id)
        if entry is None:
            raise KeyError(f"Unknown session: {h.session_id}")

        proc = entry.get("chat_process")
        if proc is None:
            raise RuntimeError("No async chat process running")

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
            exit_code = -1
        finally:
            entry["chat_process"] = None

        return exit_code, stdout or "", stderr or ""

    # ── Bulk operations ────────────────────────────────────────────────

    def list_sessions(self) -> list[dict]:
        """List all managed Hermes sessions."""
        return [
            {
                "session_id": sid,
                "name": s["handle"].name,
                "profile": s["profile_name"],
                "prompts": s["prompts"],
            }
            for sid, s in self._sessions.items()
        ]

    def dispose_all(self) -> int:
        """Dispose all managed sessions. Returns count disposed."""
        count = 0
        for sid in list(self._sessions.keys()):
            h = self._sessions[sid]["handle"]
            try:
                self.dispose(h)
                count += 1
            except Exception:
                pass
        return count

    # ── Internal helpers ───────────────────────────────────────────────

    def _ensure_hermes_installed(self) -> None:
        """Verify that the hermes CLI binary exists and is executable.

        Skipped when ``check_binary=False`` (useful for testing).
        """
        if not self._check_binary:
            return
        if not Path(self._hermes).exists():
            raise RuntimeError(
                f"Hermes CLI not found at {self._hermes}. "
                f"Install it or set the hermes_bin parameter."
            )
        if not os.access(self._hermes, os.X_OK):
            raise RuntimeError(f"Hermes CLI at {self._hermes} is not executable.")

    def _gateway_healthy(self, timeout: float = 2) -> bool:
        """Check if the Hermes gateway health endpoint responds.

        Args:
            timeout: Seconds to wait for the HTTP response.

        Returns:
            True if the gateway returns HTTP 200.
        """
        try:
            req = request.Request(self._health_url, method="GET")
            resp = request.urlopen(req, timeout=timeout)
            return resp.status == 200
        except (URLError, OSError, ValueError):
            return False

    def _wait_for_gateway(self, timeout: float = 10, interval: float = 0.5) -> None:
        """Block until the gateway health endpoint responds or timeout.

        Args:
            timeout: Max seconds to wait.
            interval: Poll interval in seconds.

        Raises:
            RuntimeError: If gateway does not become healthy within timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._gateway_healthy(timeout=1):
                logger.info("HermesAdapter: gateway is healthy")
                return
            time.sleep(interval)
        raise RuntimeError(
            f"Gateway at {self._health_url} did not become healthy "
            f"within {timeout}s"
        )

    def _delete_profile(self, profile_name: str) -> None:
        """Delete a Hermes profile, swallowing errors."""
        try:
            subprocess.run(
                [self._hermes, "profile", "delete", profile_name, "--force"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as exc:
            logger.warning(
                "HermesAdapter: failed to delete profile %s: %s",
                profile_name, exc,
            )
