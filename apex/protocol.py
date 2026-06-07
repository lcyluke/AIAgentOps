"""APEX wire protocol — typed, versioned discriminated unions.

This is the single source of truth for all messages crossing module / process
boundaries: hook shim -> apexd (events), Surfaces -> apexd (commands).
Mirrors the design in §5 of the delivery spec. Inspired by phi's single typed
IPC channel: nothing crosses a boundary without a schema.
"""
from __future__ import annotations

from typing import Literal, Optional, Union
from pydantic import BaseModel, Field

PROTOCOL_VERSION = 1

Runtime = Literal["kiro", "claude", "hermes"]
InstanceState = Literal[
    "idle", "running", "waiting_approval", "done", "failed", "disposed"
]


# ---------------------------------------------------------------- Events (up)
class _Ev(BaseModel):
    v: int = PROTOCOL_VERSION
    ts: str


class InstanceRegistered(_Ev):
    type: Literal["instance.registered"] = "instance.registered"
    agent: str
    instance: str
    session_id: str
    runtime: Runtime
    cwd: str


class TurnHeartbeat(_Ev):
    type: Literal["turn.heartbeat"] = "turn.heartbeat"
    session_id: str
    tool: str
    ok: bool


class TurnCompleted(_Ev):
    type: Literal["turn.completed"] = "turn.completed"
    session_id: str
    response_digest: str
    verdict: Literal["pass", "blocked"]
    reason: Optional[str] = None


class TaskClaimed(_Ev):
    type: Literal["task.claimed"] = "task.claimed"
    session_id: str
    task_id: str


class TaskDone(_Ev):
    type: Literal["task.done"] = "task.done"
    session_id: str
    task_id: str
    evidence: str


class GuardBlocked(_Ev):
    type: Literal["guard.blocked"] = "guard.blocked"
    session_id: str
    tool: str
    rule: str


class InstanceFailed(_Ev):
    type: Literal["instance.failed"] = "instance.failed"
    session_id: str
    error: str


ApexEvent = Union[
    InstanceRegistered, TurnHeartbeat, TurnCompleted,
    TaskClaimed, TaskDone, GuardBlocked, InstanceFailed,
]


# ------------------------------------------------------------- Commands (down)
class CompletionCriterion(BaseModel):
    kind: Literal["shell_exit_zero", "file_exists", "grep_absent", "grep_present"]
    cmd: Optional[str] = None      # shell_exit_zero
    path: Optional[str] = None     # file_exists
    pattern: Optional[str] = None  # grep_*


class TaskSpec(BaseModel):
    task_id: str
    description: str
    criteria: list[CompletionCriterion] = Field(default_factory=list)
    context_refs: list[str] = Field(default_factory=list)
    max_steps: int = 20
    depends_on: list[str] = Field(default_factory=list)


class _Cmd(BaseModel):
    v: int = PROTOCOL_VERSION


class SpawnCmd(_Cmd):
    type: Literal["spawn"] = "spawn"
    agent: str
    task: TaskSpec


class AssignCmd(_Cmd):
    type: Literal["assign"] = "assign"
    instance: str
    task: TaskSpec


class SteerCmd(_Cmd):
    type: Literal["steer"] = "steer"
    instance: str
    text: str


class AbortCmd(_Cmd):
    type: Literal["abort"] = "abort"
    instance: str


class DisposeCmd(_Cmd):
    type: Literal["dispose"] = "dispose"
    instance: str


class AlertCmd(_Cmd):
    type: Literal["alert"] = "alert"
    text: str
    level: Literal["info", "warn", "error"]


class QueryCmd(_Cmd):
    type: Literal["query"] = "query"
    what: Literal["status", "blackboard", "claims", "events", "usage"]
    filter: dict = Field(default_factory=dict)


ApexCommand = Union[
    SpawnCmd, AssignCmd, SteerCmd, AbortCmd, DisposeCmd, AlertCmd, QueryCmd,
]
