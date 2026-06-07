"""ToolRiskGate + role whitelist — three-tier risk gating (§6.7).

Risk tiers and the role tool whitelist are absorbed verbatim-in-spirit from the
Agent System 0.12.99 reverse-engineering (ROLE_TOOL_WHITELIST / ToolRiskLevel):
mechanism-level isolation is more reliable than prompt-level instruction.
"""
from __future__ import annotations

import re
from typing import Literal

RiskLevel = Literal["read_only", "state_changing", "destructive"]

TOOL_RISK: dict[str, RiskLevel] = {
    "read_file": "read_only", "read_files": "read_only", "list_files": "read_only",
    "list": "read_only", "read": "read_only", "grep": "read_only",
    "apply_edit": "state_changing", "apply_edits": "state_changing",
    "create_file": "state_changing", "write": "state_changing",
    "search_web": "state_changing", "get_web_content": "state_changing",
    "delete_file": "destructive", "run_shell": "destructive", "shell": "destructive",
}

# Per-role tool whitelist (mirrors Agent System's table, mapped to Kiro tools).
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

# Hard-blocked dangerous command patterns (Tirith-equivalent).
_DANGEROUS = [
    re.compile(r"rm\s+-rf\s+/"),
    re.compile(r"rm\s+-rf\s+\*"),
    re.compile(r"curl[^\n|]*\|\s*sh"),
    re.compile(r"wget[^\n|]*\|\s*sh"),
    re.compile(r"git\s+push[^\n]*--force"),
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"),  # fork bomb
]


def normalize_role(role: str) -> str:
    r = (role or "").lower().strip().replace(" ", "-")
    if r in ROLE_TOOL_WHITELIST:
        return r
    for key, alias in [("coder", ("developer", "writer")),
                       ("reviewer", ("review", "audit")),
                       ("tester", ("test", "spec")),
                       ("architect", ("coordinat", "orchestrat", "planner"))]:
        if key in r or any(a in r for a in alias):
            return key
    return "default"


# Canonical <- alias map (Kiro supports both; we normalize to canonical).
_TOOL_ALIASES: dict[str, str] = {
    "read": "read_file", "list": "list_files", "write": "create_file",
    "shell": "run_shell", "grep": "read_file",
}


def canonical_tool(tool: str) -> str:
    return _TOOL_ALIASES.get(tool, tool)


def tool_allowed_for_role(tool: str, role: str) -> bool:
    wl = ROLE_TOOL_WHITELIST[normalize_role(role)]
    # compare on canonical form so 'run_shell' matches whitelist 'shell', etc.
    canon = canonical_tool(tool)
    return canon in wl or tool in wl or any(canonical_tool(w) == canon for w in wl)


def evaluate(tool: str, role: str, args: dict, auto_approve_state_changing: bool
             ) -> tuple[Literal["allow", "ask", "block"], str]:
    """Return (decision, rule). 'block' -> hook exits 2."""
    # 1. dangerous command hard-block (highest precedence)
    if tool in ("run_shell", "shell"):
        cmd = str(args.get("command", ""))
        for pat in _DANGEROUS:
            if pat.search(cmd):
                return "block", f"dangerous_command:{pat.pattern}"
    # 2. role whitelist
    if not tool_allowed_for_role(tool, role):
        return "block", f"tool_not_in_role:{normalize_role(role)}"
    # 3. risk tier policy
    level = TOOL_RISK.get(tool, "state_changing")
    if level == "read_only":
        return "allow", "read_only"
    if level == "destructive":
        return "ask", "destructive_requires_approval"
    # state_changing
    return ("allow", "auto_state_changing") if auto_approve_state_changing else ("ask", "state_changing")
