"""StagnationGuard — detect and prevent agent looping.

Per design §6.6: records tool calls, detects repeated failures,
applies step penalties, and complements Auditor.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class StagnationStatus:
    """Result of a stagnation check."""
    stalled: bool
    reason: str = ""
    penalty: int = 0
    effective_max_steps: int = 0


class StagnationGuard:
    """Detect repeated tool failures to prevent infinite loops."""

    def __init__(self, repeat_threshold: int = 2):
        self._threshold = repeat_threshold
        self._history: list[dict] = []  # [{tool, args_hash, success, timestamp}]
        self._failure_counts: dict[str, int] = defaultdict(int)
        # {tool:args_hash → consecutive_failures}

    # ── Record ───────────────────────────────────────────────

    def record(self, tool: str, args: dict | None = None, success: bool = True):
        """Record a tool call result."""
        args_key = self._hash_args(args or {})
        entry = {
            "tool": tool,
            "args_hash": args_key,
            "success": success,
        }
        self._history.append(entry)

        key = f"{tool}:{args_key}"
        if not success:
            self._failure_counts[key] += 1
        else:
            self._failure_counts[key] = 0  # reset on success

    # ── Detection ────────────────────────────────────────────

    def is_repeated_failure(self, tool: str, args: dict | None = None) -> bool:
        """Check if this (tool, args) has failed ≥ threshold times."""
        key = f"{tool}:{self._hash_args(args or {})}"
        return self._failure_counts.get(key, 0) >= self._threshold

    def check(self, tool: str, args: dict | None = None,
              max_steps: int = 20) -> StagnationStatus:
        """Full stagnation check. Returns status with penalty if stalled."""
        if self.is_repeated_failure(tool, args):
            key = f"{tool}:{self._hash_args(args or {})}"
            count = self._failure_counts[key]
            penalty = min(count, max_steps - 1)
            return StagnationStatus(
                stalled=True,
                reason=f"Repeated failure: {tool} failed {count}x consecutively",
                penalty=penalty,
                effective_max_steps=max(1, max_steps - penalty),
            )
        return StagnationStatus(stalled=False, effective_max_steps=max_steps)

    # ── Penalty ──────────────────────────────────────────────

    def apply_penalty(self, max_steps: int) -> int:
        """Calculate effective max steps considering all failures."""
        total_failures = sum(
            c for c in self._failure_counts.values() if c >= self._threshold
        )
        penalty = min(total_failures, max_steps - 1)
        return max(1, max_steps - penalty)

    # ── Stats ────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return stagnation statistics."""
        return {
            "total_calls": len(self._history),
            "total_failures": sum(
                1 for h in self._history if not h["success"]
            ),
            "stalled_tools": [
                k for k, v in self._failure_counts.items()
                if v >= self._threshold
            ],
            "active_penalty": self.apply_penalty(20),
        }

    def reset(self):
        """Reset all state."""
        self._history.clear()
        self._failure_counts.clear()

    # ── Internal ─────────────────────────────────────────────

    @staticmethod
    def _hash_args(args: dict) -> str:
        """Stable hash of tool arguments."""
        return str(sorted(str(v) for v in args.values()))
