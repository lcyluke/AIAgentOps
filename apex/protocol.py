"""Apex Protocol Types — shared data models for orchestration and storage.

These are the canonical types used across Apex agents, adapters, and persistence.
Uses dataclasses (stdlib) for zero-dependency compatibility with the sqlite3 layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# ── Enums ────────────────────────────────────────────────────────────────────

class InstanceState(str, Enum):
    """Lifecycle state of a spawned instance."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


class Runtime(str, Enum):
    """Execution runtime environment."""
    PYTHON = "python"
    NODE = "node"
    SHELL = "shell"
    DOCKER = "docker"
    WASM = "wasm"


class CommandStatus(str, Enum):
    """Status of a command executed against a runtime."""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class TaskStatus(str, Enum):
    """Lifecycle status of a task."""
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ── Data Models ──────────────────────────────────────────────────────────────

@dataclass
class ApexEvent:
    """A discrete event emitted by the orchestrator or an agent."""
    id: str
    session_id: str
    type: str  # e.g. "task_started", "instance_spawned", "command_completed"
    data: dict = field(default_factory=dict)
    timestamp: str = ""  # ISO-8601


@dataclass
class ApexCommand:
    """A command issued to a runtime instance."""
    id: str
    session_id: str
    command: str
    status: CommandStatus = CommandStatus.PENDING
    instance_id: str = ""
    result: Optional[dict] = None
    duration_ms: int = 0
    created_at: str = ""
    completed_at: str = ""


@dataclass
class TaskSpec:
    """Specification for a unit of work."""
    id: str
    name: str
    description: str = ""
    runtime: Runtime = Runtime.PYTHON
    input_data: dict = field(default_factory=dict)
    priority: int = 0
    depends_on: list[str] = field(default_factory=list)
    timeout: int = 300
    retries: int = 0
    tags: list[str] = field(default_factory=list)
    created_at: str = ""


@dataclass
class InstanceInfo:
    """Snapshot of a runtime instance for persistence."""
    session_id: str
    instance_id: str
    runtime: Runtime = Runtime.PYTHON
    state: InstanceState = InstanceState.PENDING
    image: str = ""
    command: str = ""
    spawn_args: dict = field(default_factory=dict)
    created_at: str = ""
    completed_at: str = ""
    error: str = ""
