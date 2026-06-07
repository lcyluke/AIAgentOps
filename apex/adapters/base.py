"""RuntimeAdapter — the single boundary to underlying coding agents (INV-1).

Per the delivery spec §4: NO other module may import a runtime SDK or assemble a
runtime CLI command. All runtime differences are confined to implementations of
this Protocol. Verb set mirrors phi's session verbs (prompt/steer/abort/...).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Optional


@dataclass
class SessionHandle:
    name: str          # {agent}-{taskid}
    session_id: str    # runtime session UUID
    runtime: str       # kiro|claude|hermes
    tmux_session: str


@dataclass
class InstanceStatus:
    state: str         # InstanceState
    last_event_ts: str
    detail: Optional[str] = None


@dataclass
class SpawnSpec:
    agent: str
    instance: str
    brief: str
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    max_steps: int = 20


class RuntimeAdapter(Protocol):
    name: str

    def spawn(self, spec: SpawnSpec) -> SessionHandle: ...
    def prompt(self, h: SessionHandle, text: str) -> None: ...
    def steer(self, h: SessionHandle, text: str) -> None: ...
    def abort(self, h: SessionHandle) -> None: ...
    def status(self, h: SessionHandle) -> InstanceStatus: ...
    def dispose(self, h: SessionHandle) -> None: ...
