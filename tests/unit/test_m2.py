"""Unit tests for M2: blackboard.py, claims.py, injector.py.

Run standalone:  python3 tests/unit/test_m2.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

# ── Bootstrap: add project root to sys.path so imports resolve ──────────────
import sys

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Override DB_PATH *before* importing the modules so they use a temp file.
_TMP_DB = Path(tempfile.gettempdir()) / "test_m2_apex.db"
os.environ["APEX_DB_PATH"] = str(_TMP_DB)

# Now import the modules under test.
from apex.core.blackboard import Blackboard, _close_conn as _bb_close  # noqa: E402
from apex.core.claims import ClaimsRegistry, _close_conn as _cr_close  # noqa: E402
from apex.core.injector import inject  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fresh_db() -> None:
    """Remove the test DB file and close cached connections."""
    p = Path(os.environ["APEX_DB_PATH"])
    if p.exists():
        p.unlink()
    try:
        _bb_close()
    except Exception:
        pass
    try:
        _cr_close()
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════════
# Blackboard tests
# ═════════════════════════════════════════════════════════════════════════════

class TestBlackboardWriteRead(unittest.TestCase):
    """Basic write, digest, query, verify, and delete."""

    def setUp(self):
        _fresh_db()
        self.bb = Blackboard()

    def test_write_returns_uuid(self):
        eid = self.bb.write("Integration tests pass", "tester")
        self.assertTrue(len(eid) > 20)  # UUID-like

    def test_digest_empty(self):
        d = self.bb.digest()
        self.assertIn("empty", d.lower())

    def test_digest_includes_entry(self):
        self.bb.write("System boots correctly", "architect", verified=True)
        d = self.bb.digest()
        self.assertIn("architect", d)
        self.assertIn("verified", d)
        self.assertIn("System boots correctly", d)

    def test_digest_excludes_author(self):
        self.bb.write("authored by alice", "alice")
        self.bb.write("authored by bob", "bob")
        d = self.bb.digest(exclude_author="alice")
        self.assertIn("bob", d)
        self.assertNotIn("alice", d)

    def test_digest_token_cap_truncation(self):
        long_text = "word " * 500
        self.bb.write(long_text, "bot")
        d = self.bb.digest(max_tokens=50)
        self.assertLessEqual(
            len(d), 50 * 4 + len("\n... [truncated for token budget]") + 10
        )

    def test_query_by_author(self):
        self.bb.write("alpha", "alice")
        self.bb.write("beta", "bob")
        results = self.bb.query(filter_author="alice")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["author"], "alice")

    def test_query_by_text(self):
        self.bb.write("database migration complete", "coder")
        self.bb.write("frontend refactor done", "coder")
        results = self.bb.query(what="migration")
        self.assertEqual(len(results), 1)
        self.assertIn("migration", results[0]["conclusion"])

    def test_query_verified_only(self):
        self.bb.write("v1", "a", verified=True)
        self.bb.write("v2", "b", verified=False)
        results = self.bb.query(verified_only=True)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["verified"])

    def test_query_by_session(self):
        self.bb.write("sess1-work", "a", session_id="s1")
        self.bb.write("sess2-work", "b", session_id="s2")
        results = self.bb.query(session_id="s1")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["session_id"], "s1")

    def test_mark_verified(self):
        eid = self.bb.write("needs verification", "reviewer")
        self.assertTrue(self.bb.mark_verified(eid))
        results = self.bb.query(verified_only=True)
        self.assertEqual(len(results), 1)

    def test_mark_verified_nonexistent(self):
        self.assertFalse(self.bb.mark_verified("no-such-id"))

    def test_delete_entry(self):
        eid = self.bb.write("to be deleted", "janitor")
        self.assertTrue(self.bb.delete_entry(eid))
        self.assertEqual(self.bb.count(), 0)

    def test_delete_nonexistent(self):
        self.assertFalse(self.bb.delete_entry("ghost-id"))

    def test_count(self):
        self.assertEqual(self.bb.count(), 0)
        self.bb.write("a", "x")
        self.bb.write("b", "x", verified=True)
        self.assertEqual(self.bb.count(), 2)
        self.assertEqual(self.bb.count(verified_only=True), 1)

    def test_write_with_metadata(self):
        self.bb.write("data", "bot", metadata={"confidence": 0.95, "source": "test"})
        results = self.bb.query(filter_author="bot")
        self.assertEqual(len(results), 1)
        meta = json.loads(results[0]["metadata"])
        self.assertEqual(meta["confidence"], 0.95)
        self.assertEqual(meta["source"], "test")


# ═════════════════════════════════════════════════════════════════════════════
# Claims tests
# ═════════════════════════════════════════════════════════════════════════════

class TestClaimsRegistry(unittest.TestCase):
    """Claim, release, query, and digest."""

    def setUp(self):
        _fresh_db()
        self.cr = ClaimsRegistry()

    def test_claim_success(self):
        result = self.cr.claim("task-1", "coder")
        self.assertTrue(result["success"])
        self.assertEqual(result["holder"], "coder")
        self.assertEqual(result["status"], "active")

    def test_claim_already_claimed(self):
        self.cr.claim("task-1", "coder")
        result = self.cr.claim("task-1", "tester")
        self.assertFalse(result["success"])
        self.assertEqual(result["holder"], "coder")

    def test_claim_with_criteria(self):
        criteria = {"language": "python", "difficulty": "hard"}
        result = self.cr.claim("task-2", "coder", criteria=criteria)
        self.assertTrue(result["success"])
        claim = self.cr.get_claim("task-2")
        self.assertIsNotNone(claim)
        assert claim is not None  # type guard
        stored = json.loads(claim["criteria_json"])
        self.assertEqual(stored, criteria)

    def test_release_own_claim(self):
        self.cr.claim("task-3", "coder")
        self.assertTrue(self.cr.release("task-3", "coder"))
        self.assertIsNone(self.cr.get_claim("task-3"))

    def test_release_wrong_holder(self):
        self.cr.claim("task-3", "coder")
        self.assertFalse(self.cr.release("task-3", "tester"))
        self.assertIsNotNone(self.cr.get_claim("task-3"))

    def test_force_release(self):
        self.cr.claim("task-4", "coder")
        self.assertTrue(self.cr.force_release("task-4"))
        self.assertIsNone(self.cr.get_claim("task-4"))

    def test_force_release_nonexistent(self):
        self.assertFalse(self.cr.force_release("no-such-task"))

    def test_update_findings(self):
        self.cr.claim("task-5", "coder")
        findings = {"tests_passed": 12, "tests_failed": 0}
        self.assertTrue(self.cr.update_findings("task-5", findings, verified=True))
        claim = self.cr.get_claim("task-5")
        self.assertIsNotNone(claim)
        assert claim is not None  # type guard
        self.assertEqual(json.loads(claim["findings_json"]), findings)
        self.assertTrue(claim["verified"])

    def test_update_findings_nonexistent(self):
        self.assertFalse(self.cr.update_findings("no-such-task", {"x": 1}))

    def test_update_status(self):
        self.cr.claim("task-6", "coder")
        self.assertTrue(self.cr.update_status("task-6", "completed"))
        claim = self.cr.get_claim("task-6")
        self.assertIsNotNone(claim)
        assert claim is not None  # type guard
        self.assertEqual(claim["status"], "completed")

    def test_update_status_nonexistent(self):
        self.assertFalse(self.cr.update_status("ghost", "completed"))

    def test_get_active_claims(self):
        self.cr.claim("active-1", "alice")
        self.cr.claim("active-2", "bob")
        self.cr.claim("done-1", "charlie")
        self.cr.update_status("done-1", "completed")

        active = self.cr.get_active_claims()
        self.assertEqual(len(active), 2)
        ids = {c["task_id"] for c in active}
        self.assertIn("active-1", ids)
        self.assertIn("active-2", ids)
        self.assertNotIn("done-1", ids)

    def test_get_claim(self):
        self.cr.claim("task-7", "coder")
        c = self.cr.get_claim("task-7")
        self.assertIsNotNone(c)
        assert c is not None  # type guard
        self.assertEqual(c["task_id"], "task-7")
        self.assertEqual(c["claimed_by"], "coder")

    def test_get_claim_nonexistent(self):
        self.assertIsNone(self.cr.get_claim("missing"))

    def test_query_by_claimed_by(self):
        self.cr.claim("t-a", "alice")
        self.cr.claim("t-b", "bob")
        results = self.cr.query(claimed_by="alice")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["task_id"], "t-a")

    def test_query_by_status(self):
        self.cr.claim("t-x", "alice")
        self.cr.claim("t-y", "bob")
        self.cr.update_status("t-x", "completed")
        completed = self.cr.query(status="completed")
        active = self.cr.query(status="active")
        self.assertEqual(len(completed), 1)
        self.assertGreaterEqual(len(active), 1)

    def test_claims_digest_empty(self):
        d = self.cr.claims_digest()
        self.assertIn("available", d.lower())

    def test_claims_digest_with_entries(self):
        self.cr.claim("backend-api", "coder")
        self.cr.claim("frontend-ui", "designer")
        d = self.cr.claims_digest()
        self.assertIn("backend-api", d)
        self.assertIn("coder", d)
        self.assertIn("frontend-ui", d)
        self.assertIn("designer", d)
        self.assertIn("do not duplicate", d.lower())

    def test_claims_digest_with_criteria(self):
        self.cr.claim("api-v2", "coder", criteria={"method": "REST"})
        d = self.cr.claims_digest()
        self.assertIn("api-v2", d)
        self.assertIn("REST", d)

    def test_count(self):
        self.assertEqual(self.cr.count(), 0)
        self.cr.claim("c1", "a")
        self.cr.claim("c2", "b")
        self.assertEqual(self.cr.count(), 2)
        self.assertEqual(self.cr.count(status="active"), 2)
        self.cr.update_status("c1", "completed")
        self.assertEqual(self.cr.count(status="completed"), 1)


# ═════════════════════════════════════════════════════════════════════════════
# Injector tests
# ═════════════════════════════════════════════════════════════════════════════

class TestInjector(unittest.TestCase):
    """Context injection end-to-end tests."""

    def setUp(self):
        _fresh_db()
        self.bb = Blackboard()
        self.cr = ClaimsRegistry()

    def test_inject_empty_state(self):
        result = inject("architect")
        self.assertIn("CROSS-AGENT CONTEXT", result)
        self.assertIn("empty", result.lower())
        self.assertIn("available", result.lower())

    def test_inject_excludes_self(self):
        self.bb.write("architect found a bug", "architect")
        self.bb.write("tester validated", "tester", verified=True)
        result = inject("architect")
        self.assertNotIn("architect found a bug", result)
        self.assertIn("tester validated", result)

    def test_inject_includes_claims(self):
        self.cr.claim("task-alpha", "coder")
        result = inject("architect")
        self.assertIn("task-alpha", result)
        self.assertIn("coder", result)

    def test_inject_no_claims_flag(self):
        self.cr.claim("task-beta", "coder")
        result = inject("architect", include_claims=False)
        self.assertNotIn("task-beta", result)
        self.assertNotIn("Active Claims", result)

    def test_inject_no_blackboard_flag(self):
        self.bb.write("something happened", "bot")
        result = inject("architect", include_blackboard=False)
        self.assertNotIn("Blackboard Digest", result)
        self.assertNotIn("something happened", result)

    def test_inject_custom_exclude_author(self):
        self.bb.write("secret by alice", "alice")
        self.bb.write("public by bob", "bob")
        result = inject("charlie", exclude_author="alice")
        self.assertNotIn("secret by alice", result)
        self.assertIn("public by bob", result)

    def test_inject_header_footer_present(self):
        result = inject("agent-x")
        self.assertIn("BEGIN APEX CROSS-AGENT CONTEXT", result)
        self.assertIn("END APEX CROSS-AGENT CONTEXT", result)


# ═════════════════════════════════════════════════════════════════════════════
# Concurrency / edge-case tests
# ═════════════════════════════════════════════════════════════════════════════

class TestConcurrency(unittest.TestCase):
    """Thread-safety for blackboard and claims."""

    def setUp(self):
        _fresh_db()

    def test_concurrent_claims(self):
        """Multiple threads attempting to claim the same task — only one wins."""
        cr = ClaimsRegistry()
        errors: list[Exception] = []
        winners: list[str] = []

        def worker(name: str):
            try:
                result = cr.claim("shared-task", name)
                if result["success"]:
                    winners.append(name)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(f"agent-{i}",))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        self.assertEqual(
            len(winners), 1, f"Only one agent should win. Winners: {winners}"
        )
        claim = cr.get_claim("shared-task")
        self.assertIsNotNone(claim)
        assert claim is not None  # type guard
        self.assertEqual(claim["claimed_by"], winners[0])

    def test_concurrent_blackboard_writes(self):
        """Multiple threads writing to blackboard should all succeed."""
        bb = Blackboard()
        errors: list[Exception] = []

        def worker(prefix: str):
            try:
                for i in range(20):
                    bb.write(f"{prefix}-entry-{i}", prefix)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(f"writer-{t}",))
            for t in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0)
        self.assertEqual(bb.count(), 100)

    def test_claim_release_reclaim(self):
        """Full lifecycle: claim, release, re-claim by another agent."""
        cr = ClaimsRegistry()
        r1 = cr.claim("lifecycle-task", "agent-a")
        self.assertTrue(r1["success"])
        r2 = cr.claim("lifecycle-task", "agent-b")
        self.assertFalse(r2["success"])
        self.assertEqual(r2["holder"], "agent-a")
        self.assertTrue(cr.release("lifecycle-task", "agent-a"))
        r3 = cr.claim("lifecycle-task", "agent-b")
        self.assertTrue(r3["success"])
        self.assertEqual(r3["holder"], "agent-b")


# ═════════════════════════════════════════════════════════════════════════════
# DB isolation: each module uses the same physical DB
# ═════════════════════════════════════════════════════════════════════════════

class TestCrossModuleDB(unittest.TestCase):
    """Verify blackboard and claims coexist in the same SQLite DB."""

    def setUp(self):
        _fresh_db()

    def test_both_tables_exist(self):
        bb = Blackboard()
        cr = ClaimsRegistry()
        bb.write("cross-test", "bot")
        cr.claim("cross-task", "tester")

        import sqlite3 as _s

        conn = _s.connect(os.environ["APEX_DB_PATH"])
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        conn.close()
        table_names = [t[0] for t in tables]
        self.assertIn("blackboard", table_names)
        self.assertIn("claims", table_names)


# ═════════════════════════════════════════════════════════════════════════════
# JSON-field round-trip fidelity
# ═════════════════════════════════════════════════════════════════════════════

class TestJSONRoundTrip(unittest.TestCase):
    """Nested data survives JSON serialize/deserialize in metadata and criteria."""

    def setUp(self):
        _fresh_db()

    def test_blackboard_metadata_roundtrip(self):
        bb = Blackboard()
        meta = {
            "nested": {"list": [1, 2, 3], "bool": True},
            "null_val": None,
            "float": 3.14,
        }
        bb.write("test", "bot", metadata=meta)
        results = bb.query(filter_author="bot")
        self.assertEqual(len(results), 1)
        roundtripped = json.loads(results[0]["metadata"])
        self.assertEqual(roundtripped, meta)

    def test_claims_criteria_roundtrip(self):
        cr = ClaimsRegistry()
        criteria = {
            "tags": ["urgent", "backend"],
            "estimate_hours": 4.5,
            "depends_on": ["task-1", "task-2"],
        }
        cr.claim("json-task", "coder", criteria=criteria)
        claim = cr.get_claim("json-task")
        self.assertIsNotNone(claim)
        assert claim is not None  # type guard
        stored = json.loads(claim["criteria_json"])
        self.assertEqual(stored, criteria)


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
