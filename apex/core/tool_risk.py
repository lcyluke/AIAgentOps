"""ToolRiskGate — three-tier risk gating with role whitelists (§6.7).

Risk tiers absorbed from Agent System reverse-engineering:
- READ_ONLY: auto-allow (read_file, list_files, grep)
- STATE_CHANGING: configurable auto-approve (write, edit, search)
- DESTRUCTIVE: always requires approval (delete, shell)

Dangerous command patterns are hard-blocked regardless of role.
Protocol integration: uses new dataclass types, emits ApexEvents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

from apex.protocol import ApexEvent

# ── Types ────────────────────────────────────────────────────

RiskLevel = Literal["read_only", "state_changing", "destructive"]
Decision = Literal["allow", "ask", "block"]


@dataclass
class RiskDecision:
    decision: Decision
    rule: str
    risk_level: RiskLevel = "state_changing"
    blocked_at: str = ""


# ── Tool risk registry ───────────────────────────────────────

TOOL_RISK: dict[str, RiskLevel] = {
    "read_file": "read_only", "read_files": "read_only",
    "list_files": "read_only", "list": "read_only",
    "read": "read_only", "grep": "read_only",
    "apply_edit": "state_changing", "apply_edits": "state_changing",
    "create_file": "state_changing", "write": "state_changing",
    "search_web": "state_changing", "get_web_content": "state_changing",
    "delete_file": "destructive", "run_shell": "destructive",
    "shell": "destructive",
}

# Per-role tool whitelist
ROLE_TOOL_WHITELIST: dict[str, list[str]] = {
    "architect":        ["read", "list", "grep"],
    "coder":            ["read", "list", "write", "shell"],
    "backend-dev":      ["read", "list", "write", "shell"],
    "tester":           ["read", "list", "write", "shell"],
    "test-runner":      ["read", "list", "write", "shell"],
    "reviewer":         ["read", "list", "grep"],
    "security-auditor": ["read", "list", "grep"],
    "default":          ["read", "list"],
}

# Hard-blocked dangerous command patterns
_DANGEROUS = [
    re.compile(r"rm\s+-rf\s+/"),
    re.compile(r"rm\s+-rf\s+\*"),
    re.compile(r"curl[^\n|]*\|\s*sh"),
    re.compile(r"wget[^\n|]*\|\s*sh"),
    re.compile(r"git\s+push[^\n]*--force"),
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"),  # fork bomb
]

# Canonical tool alias map
_TOOL_ALIASES: dict[str, str] = {
    "read": "read_file", "list": "list_files",
    "write": "create_file", "shell": "run_shell",
    "grep": "read_file",
}


# ── Role helpers ─────────────────────────────────────────────

def normalize_role(role: str) -> str:
    r = (role or "").lower().strip().replace(" ", "-")
    if r in ROLE_TOOL_WHITELIST:
        return r
    for key, aliases in [
        ("coder", ("developer", "writer")),
        ("reviewer", ("review", "audit")),
        ("tester", ("test", "spec")),
        ("architect", ("coordinat", "orchestrat", "planner")),
    ]:
        if key in r or any(a in r for a in aliases):
            return key
    return "default"


def canonical_tool(tool: str) -> str:
    return _TOOL_ALIASES.get(tool, tool)


def tool_allowed_for_role(tool: str, role: str) -> bool:
    wl = ROLE_TOOL_WHITELIST[normalize_role(role)]
    canon = canonical_tool(tool)
    return canon in wl or tool in wl or any(
        canonical_tool(w) == canon for w in wl
    )


# ── Risk gate ────────────────────────────────────────────────

class ToolRiskGate:
    """Three-tier risk gate with role whitelist + dangerous command blocking.

    PreToolUse hook integration: evaluate() → allow/ask/block.
    Block → hook exits 2, emits guard.blocked event.
    """

    def __init__(self, event_bus=None):
        self._event_bus = event_bus

    def evaluate(
        self, tool: str, role: str, args: dict | None = None,
        auto_approve_state_changing: bool = False,
        session_id: str = "",
    ) -> RiskDecision:
        """Evaluate a tool call. Returns RiskDecision with allow/ask/block."""

        # 1. Dangerous command hard-block (highest precedence)
        if tool in ("run_shell", "shell") and args:
            cmd = str(args.get("command", ""))
            for pat in _DANGEROUS:
                if pat.search(cmd):
                    return self._decide(
                        "block", f"dangerous_command:{pat.pattern}",
                        "destructive", session_id,
                    )

        # 2. Role whitelist
        if not tool_allowed_for_role(tool, role):
            return self._decide(
                "block", f"tool_not_in_role:{normalize_role(role)}",
                "state_changing", session_id,
            )

        # 3. Risk tier policy
        level = TOOL_RISK.get(tool, "state_changing")
        if level == "read_only":
            return RiskDecision(decision="allow", rule="read_only", risk_level=level)
        if level == "destructive":
            return RiskDecision(decision="ask", rule="destructive_requires_approval", risk_level=level)

        # state_changing
        if auto_approve_state_changing:
            return RiskDecision(decision="allow", rule="auto_state_changing", risk_level=level)
        return RiskDecision(decision="ask", rule="state_changing", risk_level=level)

    def _decide(self, decision: Decision, rule: str,
                risk_level: RiskLevel, session_id: str) -> RiskDecision:
        """Emit event on block, return decision."""
        if decision == "block" and self._event_bus:
            self._event_bus.publish(ApexEvent(
                id=f"risk-{session_id}-{int(datetime.now().timestamp())}",
                session_id=session_id,
                type="guard.blocked",
                data={"rule": rule, "risk_level": risk_level},
                timestamp=datetime.now().isoformat(),
            ))
        return RiskDecision(
            decision=decision, rule=rule, risk_level=risk_level,
            blocked_at=datetime.now().isoformat() if decision == "block" else "",
        )


# ── Module-level convenience (backward compat) ───────────────

_default_gate = ToolRiskGate()

def evaluate(tool: str, role: str, args: dict,
             auto_approve_state_changing: bool) -> tuple[Decision, str]:
    """Legacy module-level evaluate(). Returns (decision, rule)."""
    result = _default_gate.evaluate(tool, role, args, auto_approve_state_changing)
    return result.decision, result.rule
