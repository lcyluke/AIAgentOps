"""Runnable unit tests for APEX core logic (no external deps beyond stdlib)."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from apex.security.redactor import redact, redact_dict
from apex.core.tool_risk import evaluate, tool_allowed_for_role, normalize_role
from apex.core.auditor import StagnationGuard, Auditor


def test_redactor_masks_secrets():
    assert "sk-" not in redact("key=sk-ABCDEFGHIJKLMNOPQRSTUV")
    assert "REDACTED:openai_key" in redact("sk-ABCDEFGHIJKLMNOPQRSTUV")
    assert "REDACTED:email" in redact("contact a@b.com")
    assert "REDACTED:home_path_linux" in redact("/home/luke/secret")
    # same secret -> same fingerprint (auditable), different secret -> different
    a = redact("sk-AAAAAAAAAAAAAAAAAAAAAA")
    b = redact("sk-AAAAAAAAAAAAAAAAAAAAAA")
    c = redact("sk-BBBBBBBBBBBBBBBBBBBBBB")
    assert a == b and a != c
    print("✓ redactor")


def test_redact_dict_recursive():
    out = redact_dict({"cmd": "export TOKEN=ghp_aaaaaaaaaaaaaaaaaaaaaa", "n": 3,
                       "nested": ["x@y.com"]})
    assert "ghp_" not in out["cmd"]
    assert out["n"] == 3
    assert "REDACTED:email" in out["nested"][0]
    print("✓ redact_dict")


def test_role_normalization_and_whitelist():
    assert normalize_role("Senior Developer") == "coder"
    assert normalize_role("Security Auditor") == "security-auditor"
    assert tool_allowed_for_role("write", "coder") is True
    assert tool_allowed_for_role("write", "reviewer") is False  # read-only role
    print("✓ role whitelist")


def test_tool_risk_gate():
    # reviewer trying to write -> blocked (not in role)
    d, _ = evaluate("write", "reviewer", {}, auto_approve_state_changing=True)
    assert d == "block"
    # dangerous shell -> blocked regardless of role
    d, rule = evaluate("run_shell", "coder", {"command": "rm -rf /"}, True)
    assert d == "block" and "dangerous" in rule
    # read always allowed
    d, _ = evaluate("read", "coder", {}, False)
    assert d == "allow"
    # destructive (in-whitelist) always asks regardless of auto-approve toggle
    d, rule = evaluate("run_shell", "coder", {"command": "make deploy"}, True)
    assert d == "ask" and "destructive" in rule
    # destructive tool NOT in role -> blocked by whitelist before risk tier
    d, rule = evaluate("delete_file", "coder", {}, True)
    assert d == "block" and "tool_not_in_role" in rule
    # state-changing follows the toggle
    assert evaluate("create_file", "coder", {}, True)[0] == "allow"
    assert evaluate("create_file", "coder", {}, False)[0] == "ask"
    print("✓ tool risk gate")


def test_stagnation_guard():
    g = StagnationGuard(repeat_threshold=2)
    g.record("read", {"p": "a"}, success=False)
    assert not g.is_repeated_failure("read", {"p": "a"})
    g.record("read", {"p": "a"}, success=False)
    assert g.is_repeated_failure("read", {"p": "a"})       # 2nd fail trips it
    assert g.effective_max_steps(20) < 20                  # consecutive penalty
    g.record("read", {"p": "a"}, success=True)             # success clears
    assert not g.is_repeated_failure("read", {"p": "a"})
    print("✓ stagnation guard")


def test_auditor_block_loop():
    a = Auditor(block_cap=2)
    crit = [{"kind": "file_exists", "path": "definitely_missing_xyz.txt"}]
    # unmet -> block, until cap exceeded
    assert a.decide_stop("t1", crit, cwd="/tmp").get("decision") == "block"
    assert a.decide_stop("t1", crit, cwd="/tmp").get("decision") == "block"
    assert a.decide_stop("t1", crit, cwd="/tmp") == {}     # cap exceeded -> give up
    assert a.block_exceeded("t1")
    # met criterion -> allow stop
    assert a.decide_stop("t2", [], cwd="/tmp") == {}
    print("✓ auditor block loop")


if __name__ == "__main__":
    test_redactor_masks_secrets()
    test_redact_dict_recursive()
    test_role_normalization_and_whitelist()
    test_tool_risk_gate()
    test_stagnation_guard()
    test_auditor_block_loop()
    print("\nALL TESTS PASSED")
