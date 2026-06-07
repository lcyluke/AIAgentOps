"""Auditor — trustworthy completion verification (§6.5).

Verifies CompletionCriteria against the filesystem, emits block decisions
when unmet. Blocks are fed back as new user messages so the agent continues.
Per-task block cap prevents infinite spend. Integrates with EventBus for
event-driven lifecycle and StagnationGuard for anti-loop protection.
"""

from __future__ import annotations

import os
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from apex.protocol import ApexEvent


# ── Data types ──────────────────────────────────────────────

@dataclass
class AuditResult:
    passed: bool
    failures: list[str] = field(default_factory=list)
    verified_at: str = ""


# ── Auditor ─────────────────────────────────────────────────

class Auditor:
    """Verify completion criteria; emit block decisions.

    Per design §6.5:
    - Triggered by stop event with task_id
    - Checks criteria_json from claims table
    - Block reason fed back as new user message for agent to continue
    - Block cap (default 3) prevents infinite spend
    """

    def __init__(self, block_cap: int = 3, event_bus=None):
        self.block_cap = block_cap
        self._blocks: dict[str, int] = defaultdict(int)
        self._event_bus = event_bus  # optional EventBus for event publishing

    # ── Verification ─────────────────────────────────────────

    def verify(self, criteria: list[dict], cwd: str) -> AuditResult:
        """Run all completion criteria against the filesystem."""
        failures: list[str] = []
        for c in criteria:
            kind = c.get("kind", "")
            try:
                if kind == "shell_exit_zero":
                    rc = subprocess.run(
                        c["cmd"], shell=True, cwd=cwd,
                        capture_output=True, timeout=120,
                    ).returncode
                    if rc != 0:
                        failures.append(
                            f"shell_exit_zero failed (rc={rc}): {c['cmd']}"
                        )
                elif kind == "file_exists":
                    path = os.path.join(cwd, c["path"])
                    if not os.path.exists(path):
                        failures.append(f"file_exists failed: {c['path']}")
                elif kind in ("grep_present", "grep_absent"):
                    rc = subprocess.run(
                        ["grep", "-rq", c["pattern"], cwd],
                        capture_output=True, timeout=60,
                    ).returncode
                    found = rc == 0
                    if kind == "grep_present" and not found:
                        failures.append(f"grep_present failed: {c['pattern']}")
                    if kind == "grep_absent" and found:
                        failures.append(f"grep_absent failed: {c['pattern']}")
            except Exception as e:
                failures.append(f"{kind} errored: {e}")

        return AuditResult(
            passed=not failures,
            failures=failures,
            verified_at=datetime.now().isoformat(),
        )

    # ── Stop decision ────────────────────────────────────────

    def decide_stop(
        self, task_id: str, criteria: list[dict], cwd: str,
        session_id: str = "",
    ) -> dict:
        """Return stop-hook decision dict. {} = allow stop.

        Integrates with EventBus: publishes task.verified or task.blocked events.
        """
        res = self.verify(criteria, cwd)
        if res.passed:
            self._publish(session_id, "task.verified", {
                "task_id": task_id, "passed": True,
            })
            return {}  # allow stop

        self._blocks[task_id] += 1
        over_cap = self._blocks[task_id] > self.block_cap

        self._publish(session_id, "task.blocked" if not over_cap else "task.failed", {
            "task_id": task_id,
            "block_count": self._blocks[task_id],
            "block_cap": self.block_cap,
            "failures": res.failures,
            "over_cap": over_cap,
        })

        if over_cap:
            return {
                "decision": "allow",  # give up blocking; caller marks failed
                "reason": f"Block cap exceeded ({self.block_cap}x)",
            }
        return {
            "decision": "block",
            "reason": "; ".join(res.failures),
        }

    # ── Queries ──────────────────────────────────────────────

    def block_count(self, task_id: str) -> int:
        return self._blocks[task_id]

    def block_exceeded(self, task_id: str) -> bool:
        return self._blocks[task_id] > self.block_cap

    def reset(self, task_id: str):
        """Reset block count for a task (e.g. on task restart)."""
        self._blocks.pop(task_id, None)

    # ── Internal ─────────────────────────────────────────────

    def _publish(self, session_id: str, event_type: str, data: dict):
        if self._event_bus:
            self._event_bus.publish(ApexEvent(
                id=f"audit-{session_id}-{int(datetime.now().timestamp())}",
                session_id=session_id,
                type=event_type,
                data=data,
                timestamp=datetime.now().isoformat(),
            ))
