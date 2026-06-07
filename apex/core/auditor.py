"""StagnationGuard + Auditor — anti-loop + trustworthy completion (§6.5/§6.6)."""
from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field


# ----------------------------------------------------- StagnationGuard (§6.6)
class StagnationGuard:
    """Block repeated identical failures; penalize consecutive failures.

    Absorbed from Agent System's StagnationDetector: catch '(tool,args) failed
    N times' before execution, and shrink the step budget on consecutive fails.
    """
    def __init__(self, repeat_threshold: int = 2, penalty_per_failure: int = 1):
        self.repeat_threshold = max(2, repeat_threshold)
        self.penalty_per_failure = penalty_per_failure
        self._fail_counts: dict[str, int] = defaultdict(int)
        self._consecutive: int = 0

    @staticmethod
    def _key(tool: str, args: dict) -> str:
        return tool + ":" + json.dumps(args, sort_keys=True, default=str)

    def record(self, tool: str, args: dict, success: bool) -> None:
        k = self._key(tool, args)
        if success:
            self._fail_counts.pop(k, None)
            self._consecutive = 0
        else:
            self._fail_counts[k] += 1
            self._consecutive += 1

    def is_repeated_failure(self, tool: str, args: dict) -> bool:
        return self._fail_counts[self._key(tool, args)] >= self.repeat_threshold

    def effective_max_steps(self, max_steps: int) -> int:
        return max(1, max_steps - self._consecutive * self.penalty_per_failure)


# --------------------------------------------------------------- Auditor (§6.5)
@dataclass
class AuditResult:
    passed: bool
    failures: list[str] = field(default_factory=list)


class Auditor:
    """Verify completion criteria; emit block decision when unmet.

    Block reason is fed back as a new user message so the agent continues
    (the stop-hook feedback loop). A per-task block cap prevents infinite spend.
    """
    def __init__(self, block_cap: int = 3):
        self.block_cap = block_cap
        self._blocks: dict[str, int] = defaultdict(int)

    def verify(self, criteria: list[dict], cwd: str) -> AuditResult:
        failures: list[str] = []
        for c in criteria:
            kind = c.get("kind")
            try:
                if kind == "shell_exit_zero":
                    rc = subprocess.run(
                        c["cmd"], shell=True, cwd=cwd,
                        capture_output=True, timeout=120,
                    ).returncode
                    if rc != 0:
                        failures.append(f"shell_exit_zero failed (rc={rc}): {c['cmd']}")
                elif kind == "file_exists":
                    import os
                    if not os.path.exists(os.path.join(cwd, c["path"])):
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
            except Exception as e:  # treat criterion error as failure (fail-closed)
                failures.append(f"{kind} errored: {e}")
        return AuditResult(passed=not failures, failures=failures)

    def decide_stop(self, task_id: str, criteria: list[dict], cwd: str) -> dict:
        """Return Kiro stop-hook decision dict. {} = allow stop."""
        res = self.verify(criteria, cwd)
        if res.passed:
            return {}  # allow stop
        self._blocks[task_id] += 1
        if self._blocks[task_id] > self.block_cap:
            return {}  # give up blocking; caller marks failed + alerts
        return {"decision": "block", "reason": "; ".join(res.failures)}

    def block_exceeded(self, task_id: str) -> bool:
        return self._blocks[task_id] > self.block_cap
