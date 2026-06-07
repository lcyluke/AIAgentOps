"""Instance Registry — name↔session_id mapping + state machine projection.

Per design §6.1: maintains bidirectional name↔session_id registry,
projects state from events (INV-4), and enforces name uniqueness (INV-2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from apex.protocol import ApexEvent, InstanceState


@dataclass
class RegistryEntry:
    """A single registered instance."""
    name: str
    session_id: str
    agent: str
    task_id: str = ""
    runtime: str = ""
    cwd: str = ""
    tmux_session: str = ""
    state: InstanceState = InstanceState.PENDING
    spawned_at: str = ""
    last_event_ts: str = ""


class InstanceRegistry:
    """Name↔session_id mapping with state projection from events."""

    def __init__(self):
        self._by_name: dict[str, RegistryEntry] = {}
        self._by_session: dict[str, RegistryEntry] = {}
        self._event_log: list[ApexEvent] = []  # local cache; persistent in db.py

    # ── Registration ─────────────────────────────────────────

    def register(
        self, agent: str, task_id: str, session_id: str,
        runtime: str = "", cwd: str = "", tmux_session: str = "",
    ) -> str:
        """Register a new instance. Returns the assigned name.
        Name collision → append -2, -3, etc.
        """
        base = f"{agent}-{task_id}" if task_id else agent
        name = base
        n = 2
        while name in self._by_name:
            name = f"{base}-{n}"
            n += 1

        entry = RegistryEntry(
            name=name, session_id=session_id,
            agent=agent, task_id=task_id,
            runtime=runtime, cwd=cwd, tmux_session=tmux_session,
            state=InstanceState.PENDING,
            spawned_at=datetime.now().isoformat(),
        )
        self._by_name[name] = entry
        self._by_session[session_id] = entry

        self._log_event(ApexEvent(
            id=f"evt-{session_id}-reg",
            session_id=session_id,
            type="instance.registered",
            data={"name": name, "agent": agent, "task_id": task_id},
        ))
        return name

    # ── Resolution ───────────────────────────────────────────

    def resolve(self, name: str) -> Optional[RegistryEntry]:
        """Name → entry."""
        return self._by_name.get(name)

    def reverse(self, session_id: str) -> Optional[str]:
        """Session ID → name."""
        e = self._by_session.get(session_id)
        return e.name if e else None

    # ── State projection ─────────────────────────────────────

    def project_state(self, session_id: str) -> InstanceState:
        """Project current state from events (INV-4)."""
        entry = self._by_session.get(session_id)
        if not entry:
            return InstanceState.UNKNOWN

        # Walk events for this session to determine state
        session_events = [e for e in self._event_log if e.session_id == session_id]
        if not session_events:
            return InstanceState.PENDING

        state = InstanceState.PENDING
        for e in session_events:
            t = e.type
            if "registered" in t:
                state = InstanceState.PENDING
            elif "heartbeat" in t or "running" in t:
                state = InstanceState.RUNNING
            elif "completed" in t or "done" in t:
                state = InstanceState.COMPLETED
            elif "failed" in t:
                state = InstanceState.FAILED
            elif "stopped" in t or "disposed" in t:
                state = InstanceState.STOPPED

        entry.state = state
        entry.last_event_ts = session_events[-1].timestamp if session_events else ""
        return state

    def update_state(self, session_id: str, new_state: InstanceState):
        """Explicitly set state (e.g. from daemon)."""
        entry = self._by_session.get(session_id)
        if entry:
            entry.state = new_state

    # ── Listing ──────────────────────────────────────────────

    def list(self, runtime: str = "", state: Optional[InstanceState] = None) -> list[RegistryEntry]:
        """List entries, optionally filtered."""
        entries = list(self._by_name.values())
        if runtime:
            entries = [e for e in entries if e.runtime == runtime]
        if state:
            entries = [e for e in entries if e.state == state]
        return entries

    def count(self) -> int:
        return len(self._by_name)

    # ── Removal ──────────────────────────────────────────────

    def unregister(self, session_id: str):
        """Remove an instance from the registry."""
        entry = self._by_session.pop(session_id, None)
        if entry:
            self._by_name.pop(entry.name, None)
            self._log_event(ApexEvent(
                id=f"evt-{session_id}-unreg",
                session_id=session_id,
                type="instance.disposed",
            ))

    # ── Internal ─────────────────────────────────────────────

    def _log_event(self, event: ApexEvent):
        if not event.timestamp:
            event.timestamp = datetime.now().isoformat()
        self._event_log.append(event)
