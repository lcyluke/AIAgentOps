"""Unit tests for apex.core.cost_tracker — Hermes state.db cost queries.

Run standalone:  python3 tests/unit/test_cost_tracker.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

# ── Bootstrap: add project root to sys.path ─────────────────────────────────
import sys

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ── Test DB path ────────────────────────────────────────────────────────────
_TMP_DB = Path(tempfile.gettempdir()) / "test_cost_tracker_hermes.db"


def _fresh_db() -> sqlite3.Connection:
    """Create a fresh test database with the Hermes sessions schema."""
    if _TMP_DB.exists():
        _TMP_DB.unlink()
    conn = sqlite3.connect(str(_TMP_DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            billing_provider TEXT,
            billing_base_url TEXT,
            billing_mode TEXT,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            cost_status TEXT,
            cost_source TEXT,
            pricing_version TEXT,
            title TEXT,
            api_call_count INTEGER DEFAULT 0,
            handoff_state TEXT,
            handoff_platform TEXT,
            handoff_error TEXT
        )
    """)
    conn.commit()
    return conn


def _now_ts() -> float:
    return datetime.now().timestamp()


def _days_ago_ts(days: int) -> float:
    return (datetime.now() - timedelta(days=days)).timestamp()


def _insert_session(conn: sqlite3.Connection, **kwargs) -> str:
    """Insert a test session row. Returns the session id."""
    sid = kwargs.pop("id", f"test-{int(time.time() * 1_000_000)}")
    defaults = {
        "source": "cli",
        "model": "deepseek-v4-pro",
        "started_at": _now_ts(),
        "input_tokens": 0,
        "output_tokens": 0,
    }
    defaults.update(kwargs)

    conn.execute(
        """INSERT INTO sessions (id, source, model, title, system_prompt,
           input_tokens, output_tokens, started_at, ended_at,
           handoff_platform, estimated_cost_usd, cache_read_tokens,
           cache_write_tokens, reasoning_tokens)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            sid,
            defaults.get("source"),
            defaults.get("model"),
            defaults.get("title"),
            defaults.get("system_prompt"),
            defaults.get("input_tokens", 0),
            defaults.get("output_tokens", 0),
            defaults.get("started_at"),
            defaults.get("ended_at"),
            defaults.get("handoff_platform"),
            defaults.get("estimated_cost_usd"),
            defaults.get("cache_read_tokens", 0),
            defaults.get("cache_write_tokens", 0),
            defaults.get("reasoning_tokens", 0),
        ),
    )
    conn.commit()
    return sid


# ═════════════════════════════════════════════════════════════════════════════
# Test Cases
# ═════════════════════════════════════════════════════════════════════════════


class TestSessionCost(unittest.TestCase):
    """get_session_cost() tests."""

    @classmethod
    def setUpClass(cls):
        cls.conn = _fresh_db()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        if _TMP_DB.exists():
            _TMP_DB.unlink()

    def setUp(self):
        # Clear sessions for each test
        self.conn.execute("DELETE FROM sessions")
        self.conn.commit()

    def test_session_cost_found(self):
        sid = _insert_session(
            self.conn,
            id="sess-001",
            model="deepseek-v4-pro",
            input_tokens=500000,
            output_tokens=200000,
            title="Apex Dashboard setup",
            source="cli",
        )

        from apex.core.cost_tracker import get_session_cost

        result = get_session_cost(sid, db_path=_TMP_DB)
        self.assertEqual(result["session_id"], "sess-001")
        self.assertEqual(result["input_tokens"], 500000)
        self.assertEqual(result["output_tokens"], 200000)
        # Expected: 500k/1M * $1 + 200k/1M * $4 = 0.5 + 0.8 = 1.3
        self.assertAlmostEqual(result["estimated_cost_usd"], 1.3, places=5)
        self.assertEqual(result["source"], "cli")
        self.assertEqual(result["model"], "deepseek-v4-pro")
        self.assertEqual(result["agent"], "cli")  # falls back to source

    def test_session_cost_not_found(self):
        from apex.core.cost_tracker import get_session_cost

        result = get_session_cost("nonexistent", db_path=_TMP_DB)
        self.assertIn("error", result)
        self.assertEqual(result["error"], "session not found")

    def test_session_cost_with_handoff(self):
        sid = _insert_session(
            self.conn,
            id="sess-agent",
            handoff_platform="code-reviewer",
            source="cli",
            input_tokens=10000,
            output_tokens=5000,
            model="deepseek-chat",
        )

        from apex.core.cost_tracker import get_session_cost

        result = get_session_cost(sid, db_path=_TMP_DB)
        self.assertEqual(result["agent"], "code-reviewer")
        # deepseek-chat: $0.14/M input, $0.28/M output
        # 10000/1M*0.14 + 5000/1M*0.28 = 0.0014 + 0.0014 = 0.0028
        self.assertAlmostEqual(result["estimated_cost_usd"], 0.0028, places=6)

    def test_session_cost_with_cache_tokens(self):
        sid = _insert_session(
            self.conn,
            id="sess-cache",
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=200,
            cache_write_tokens=100,
            reasoning_tokens=300,
        )

        from apex.core.cost_tracker import get_session_cost

        result = get_session_cost(sid, db_path=_TMP_DB)
        self.assertEqual(result["cache_read_tokens"], 200)
        self.assertEqual(result["cache_write_tokens"], 100)
        self.assertEqual(result["reasoning_tokens"], 300)

    def test_session_cost_project_detection(self):
        sid = _insert_session(
            self.conn,
            id="sess-proj",
            title="羽球宝AI训练模块",
            system_prompt="你是一个羽毛球教练",
            input_tokens=1000,
            output_tokens=1000,
        )

        from apex.core.cost_tracker import get_session_cost

        result = get_session_cost(sid, db_path=_TMP_DB)
        self.assertEqual(result["project"], "badminton-coach-ai")

    def test_session_cost_zero_tokens(self):
        sid = _insert_session(
            self.conn,
            id="sess-zero",
            input_tokens=0,
            output_tokens=0,
            model="deepseek-v4-pro",
        )

        from apex.core.cost_tracker import get_session_cost

        result = get_session_cost(sid, db_path=_TMP_DB)
        self.assertEqual(result["estimated_cost_usd"], 0.0)


class TestProjectCost(unittest.TestCase):
    """get_project_cost() tests."""

    @classmethod
    def setUpClass(cls):
        cls.conn = _fresh_db()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        if _TMP_DB.exists():
            _TMP_DB.unlink()

    def setUp(self):
        self.conn.execute("DELETE FROM sessions")
        self.conn.commit()

    def test_unknown_project(self):
        from apex.core.cost_tracker import get_project_cost

        result = get_project_cost("no-such-project", db_path=_TMP_DB)
        self.assertIn("error", result)
        self.assertEqual(result["error"], "unknown project: no-such-project")

    def test_empty_project(self):
        from apex.core.cost_tracker import get_project_cost

        result = get_project_cost("apex", days=30, db_path=_TMP_DB)
        self.assertEqual(result["project"], "apex")
        self.assertEqual(result["total_sessions"], 0)
        self.assertEqual(result["total_estimated_cost_usd"], 0.0)

    def test_project_with_sessions(self):
        # Insert sessions matching "apex" project
        _insert_session(
            self.conn,
            id="apex-1",
            title="Apex orchestrator bug fix",
            input_tokens=100000,
            output_tokens=50000,
            model="deepseek-v4-pro",
        )
        _insert_session(
            self.conn,
            id="apex-2",
            title="Dashboard fleet view",
            input_tokens=200000,
            output_tokens=100000,
            model="deepseek-chat",
            source="webui",
        )
        # Non-matching session
        _insert_session(
            self.conn,
            id="other-1",
            title="Something else",
            input_tokens=999999,
            output_tokens=999999,
        )

        from apex.core.cost_tracker import get_project_cost

        result = get_project_cost("apex", days=30, db_path=_TMP_DB)
        self.assertEqual(result["total_sessions"], 2)
        # Session 1 (deepseek-v4-pro): 100k/1M*$1 + 50k/1M*$4 = 0.1 + 0.2 = 0.3
        # Session 2 (deepseek-chat):   200k/1M*$0.14 + 100k/1M*$0.28 = 0.028 + 0.028 = 0.056
        # Total = 0.356
        self.assertAlmostEqual(result["total_estimated_cost_usd"], 0.356, places=5)
        self.assertGreaterEqual(result["total_input_tokens"], 300000)
        self.assertGreaterEqual(result["total_output_tokens"], 150000)
        self.assertIn("deepseek-v4-pro", result["by_model"])
        self.assertIn("deepseek-chat", result["by_model"])
        self.assertIn("cli", result["by_source"])
        self.assertIn("webui", result["by_source"])
        self.assertGreater(len(result["daily_breakdown"]), 0)

    def test_project_time_filter(self):
        # Old session (40 days ago)
        old_ts = _days_ago_ts(40)
        _insert_session(
            self.conn,
            id="old-apex",
            title="Old Apex session",
            started_at=old_ts,
            input_tokens=100000,
            output_tokens=50000,
        )
        # Recent session
        _insert_session(
            self.conn,
            id="recent-apex",
            title="Recent Apex session",
            input_tokens=50000,
            output_tokens=25000,
        )

        from apex.core.cost_tracker import get_project_cost

        result = get_project_cost("apex", days=30, db_path=_TMP_DB)
        self.assertEqual(result["total_sessions"], 1)  # Only recent one
        self.assertEqual(result["total_input_tokens"], 50000)

    def test_badminton_project(self):
        _insert_session(
            self.conn,
            title="羽毛球训练计划生成",
            input_tokens=1000000,
            output_tokens=250000,
            model="deepseek-v4-pro",
        )

        from apex.core.cost_tracker import get_project_cost

        result = get_project_cost("badminton-coach-ai", days=30, db_path=_TMP_DB)
        self.assertEqual(result["total_sessions"], 1)
        # 1M/1M*$1 + 0.25M/1M*$4 = 1.0 + 1.0 = 2.0
        self.assertAlmostEqual(result["total_estimated_cost_usd"], 2.0, places=5)


class TestAgentCost(unittest.TestCase):
    """get_agent_cost() tests."""

    @classmethod
    def setUpClass(cls):
        cls.conn = _fresh_db()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        if _TMP_DB.exists():
            _TMP_DB.unlink()

    def setUp(self):
        self.conn.execute("DELETE FROM sessions")
        self.conn.commit()

    def test_empty_agent(self):
        from apex.core.cost_tracker import get_agent_cost

        result = get_agent_cost("cli", days=30, db_path=_TMP_DB)
        self.assertEqual(result["agent"], "cli")
        self.assertEqual(result["total_sessions"], 0)
        self.assertEqual(result["total_estimated_cost_usd"], 0.0)

    def test_agent_by_source(self):
        _insert_session(
            self.conn,
            id="cli-1",
            source="cli",
            input_tokens=100000,
            output_tokens=50000,
            model="deepseek-v4-pro",
        )
        _insert_session(
            self.conn,
            id="cli-2",
            source="cli",
            input_tokens=200000,
            output_tokens=100000,
            title="Apex fleet management",
            model="deepseek-v4-pro",
        )
        _insert_session(
            self.conn,
            id="webui-1",
            source="webui",
            input_tokens=999999,
            output_tokens=999999,
        )

        from apex.core.cost_tracker import get_agent_cost

        result = get_agent_cost("cli", days=30, db_path=_TMP_DB)
        self.assertEqual(result["total_sessions"], 2)
        self.assertEqual(result["total_input_tokens"], 300000)
        self.assertEqual(result["total_output_tokens"], 150000)
        # 300k/1M*$1 + 150k/1M*$4 = 0.3 + 0.6 = 0.9
        self.assertAlmostEqual(result["total_estimated_cost_usd"], 0.9, places=5)
        self.assertIn("deepseek-v4-pro", result["by_model"])
        # Should detect project
        self.assertIn("apex", result["by_project"])

    def test_agent_by_handoff_platform(self):
        _insert_session(
            self.conn,
            id="handoff-1",
            source="cli",
            handoff_platform="code-reviewer",
            input_tokens=50000,
            output_tokens=25000,
            model="deepseek-chat",
        )
        _insert_session(
            self.conn,
            id="cli-other",
            source="cli",
            input_tokens=999999,
            output_tokens=999999,
        )

        from apex.core.cost_tracker import get_agent_cost

        result = get_agent_cost("code-reviewer", days=30, db_path=_TMP_DB)
        self.assertEqual(result["total_sessions"], 1)
        self.assertIn("deepseek-chat", result["by_model"])
        self.assertIn("other", result["by_project"])

    def test_agent_case_insensitive(self):
        _insert_session(
            self.conn,
            id="case-1",
            source="Slack",
            input_tokens=10000,
            output_tokens=5000,
        )

        from apex.core.cost_tracker import get_agent_cost

        result = get_agent_cost("slack", days=30, db_path=_TMP_DB)
        self.assertEqual(result["total_sessions"], 1)


class TestDailyCost(unittest.TestCase):
    """get_daily_cost() tests."""

    @classmethod
    def setUpClass(cls):
        cls.conn = _fresh_db()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        if _TMP_DB.exists():
            _TMP_DB.unlink()

    def setUp(self):
        self.conn.execute("DELETE FROM sessions")
        self.conn.commit()

    def test_empty_days(self):
        from apex.core.cost_tracker import get_daily_cost

        result = get_daily_cost(days=3, db_path=_TMP_DB)
        self.assertEqual(len(result), 3)
        for entry in result:
            self.assertEqual(entry["sessions"], 0)
            self.assertEqual(entry["estimated_cost_usd"], 0.0)

    def test_daily_aggregation(self):
        today = datetime.now()
        today_ts = today.timestamp()
        yesterday_ts = (today - timedelta(days=1)).timestamp()

        # Two sessions today
        _insert_session(
            self.conn,
            id="today-1",
            started_at=today_ts,
            input_tokens=100000,
            output_tokens=50000,
            model="deepseek-v4-pro",
            source="cli",
        )
        _insert_session(
            self.conn,
            id="today-2",
            started_at=today_ts,
            input_tokens=200000,
            output_tokens=100000,
            model="deepseek-chat",
            source="webui",
        )
        # One session yesterday
        _insert_session(
            self.conn,
            id="yesterday-1",
            started_at=yesterday_ts,
            input_tokens=50000,
            output_tokens=25000,
            model="deepseek-v4-pro",
            source="cli",
        )

        from apex.core.cost_tracker import get_daily_cost

        result = get_daily_cost(days=3, db_path=_TMP_DB)
        self.assertEqual(len(result), 3)

        today_str = today.strftime("%Y-%m-%d")
        yesterday_str = (today - timedelta(days=1)).strftime("%Y-%m-%d")

        today_entry = next(e for e in result if e["date"] == today_str)
        yesterday_entry = next(e for e in result if e["date"] == yesterday_str)

        self.assertEqual(today_entry["sessions"], 2)
        self.assertEqual(today_entry["input_tokens"], 300000)
        self.assertEqual(today_entry["output_tokens"], 150000)
        self.assertIn("cli", today_entry["by_source"])
        self.assertIn("webui", today_entry["by_source"])
        self.assertEqual(today_entry["by_source"]["cli"], 1)
        self.assertEqual(today_entry["by_source"]["webui"], 1)

        self.assertEqual(yesterday_entry["sessions"], 1)
        self.assertEqual(yesterday_entry["input_tokens"], 50000)

    def test_daily_cost_out_of_range(self):
        old_ts = _days_ago_ts(10)
        _insert_session(
            self.conn,
            id="old-session",
            started_at=old_ts,
            input_tokens=999999,
            output_tokens=999999,
        )

        from apex.core.cost_tracker import get_daily_cost

        result = get_daily_cost(days=7, db_path=_TMP_DB)
        for entry in result:
            self.assertEqual(entry["sessions"], 0)

    def test_daily_json_serializable(self):
        _insert_session(
            self.conn,
            input_tokens=1000,
            output_tokens=500,
            model="deepseek-v4-pro",
        )

        from apex.core.cost_tracker import get_daily_cost

        result = get_daily_cost(days=1, db_path=_TMP_DB)
        json_str = json.dumps(result)
        self.assertTrue(len(json_str) > 0)
        parsed = json.loads(json_str)
        self.assertEqual(len(parsed), 1)


class TestPricingCalculation(unittest.TestCase):
    """Verify cost calculations for different models."""

    def setUp(self):
        # Direct import to test the internal helper
        from apex.core.cost_tracker import _calculate_cost

        self.calc = _calculate_cost

    def test_deepseek_v4_pro(self):
        cost = self.calc(1_000_000, 0, "deepseek-v4-pro")
        self.assertAlmostEqual(cost, 1.0, places=5)
        cost = self.calc(0, 1_000_000, "deepseek-v4-pro")
        self.assertAlmostEqual(cost, 4.0, places=5)
        cost = self.calc(500_000, 250_000, "deepseek-v4-pro")
        self.assertAlmostEqual(cost, 1.5, places=5)  # 0.5 + 1.0

    def test_deepseek_chat(self):
        cost = self.calc(1_000_000, 1_000_000, "deepseek-chat")
        self.assertAlmostEqual(cost, 0.42, places=5)  # 0.14 + 0.28

    def test_claude_sonnet(self):
        cost = self.calc(1_000_000, 1_000_000, "claude-sonnet-4")
        self.assertAlmostEqual(cost, 18.0, places=5)  # 3.0 + 15.0

    def test_unknown_model_defaults(self):
        cost = self.calc(1_000_000, 1_000_000, "unknown-model")
        self.assertAlmostEqual(cost, 5.0, places=5)  # $1 + $4

    def test_none_model(self):
        cost = self.calc(1_000_000, 0, None)
        self.assertAlmostEqual(cost, 1.0, places=5)


class TestProjectDetection(unittest.TestCase):
    """Verify keyword-based project detection."""

    def setUp(self):
        from apex.core.cost_tracker import _detect_project

        self.detect = _detect_project

    def test_apex_detection(self):
        self.assertEqual(self.detect("Apex orchestrator fix", "", "sid1"), "apex")
        self.assertEqual(self.detect("Dashboard fleet status", "", "sid2"), "apex")
        self.assertEqual(self.detect("Bridge sync error", "", "sid3"), "apex")

    def test_badminton_detection(self):
        self.assertEqual(self.detect("羽球宝训练", "", "sid4"), "badminton-coach-ai")
        self.assertEqual(self.detect("Badminton coach session", "", "sid5"), "badminton-coach-ai")
        self.assertEqual(self.detect("羽毛球动作分析", "", "sid6"), "badminton-coach-ai")

    def test_finops_detection(self):
        self.assertEqual(self.detect("FinOps billing report", "", "sid7"), "finopsai")
        self.assertEqual(self.detect("云成本优化", "", "sid8"), "finopsai")

    def test_shenzhen_detection(self):
        self.assertEqual(self.detect("深圳羽球地图场馆查询", "", "sid9"), "shenzhen-badminton")
        self.assertEqual(self.detect("venue search Shenzhen", "", "sid10"), "shenzhen-badminton")

    def test_no_match(self):
        self.assertIsNone(self.detect("Random chat", "", "sid11"))
        self.assertIsNone(self.detect("", "", "sid12"))


class TestJSONSerializable(unittest.TestCase):
    """Ensure all public functions return JSON-serializable dicts."""

    @classmethod
    def setUpClass(cls):
        cls.conn = _fresh_db()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        if _TMP_DB.exists():
            _TMP_DB.unlink()

    def setUp(self):
        self.conn.execute("DELETE FROM sessions")
        self.conn.commit()

    def test_get_session_cost_json(self):
        sid = _insert_session(
            self.conn,
            id="json-test",
            input_tokens=5000,
            output_tokens=2000,
            model="deepseek-v4-pro",
        )

        from apex.core.cost_tracker import get_session_cost

        result = get_session_cost(sid, db_path=_TMP_DB)
        json_str = json.dumps(result)
        parsed = json.loads(json_str)
        self.assertEqual(parsed["session_id"], "json-test")

    def test_get_project_cost_json(self):
        _insert_session(
            self.conn,
            title="Apex test",
            input_tokens=10000,
            output_tokens=5000,
        )

        from apex.core.cost_tracker import get_project_cost

        result = get_project_cost("apex", days=30, db_path=_TMP_DB)
        json_str = json.dumps(result)
        parsed = json.loads(json_str)
        self.assertEqual(parsed["project"], "apex")
        self.assertIsInstance(parsed["by_model"], dict)
        self.assertIsInstance(parsed["by_source"], dict)
        self.assertIsInstance(parsed["daily_breakdown"], list)

    def test_get_agent_cost_json(self):
        _insert_session(
            self.conn,
            source="cli",
            input_tokens=10000,
            output_tokens=5000,
        )

        from apex.core.cost_tracker import get_agent_cost

        result = get_agent_cost("cli", days=30, db_path=_TMP_DB)
        json_str = json.dumps(result)
        parsed = json.loads(json_str)
        self.assertEqual(parsed["agent"], "cli")

    def test_get_daily_cost_json(self):
        _insert_session(
            self.conn,
            input_tokens=1000,
            output_tokens=500,
        )

        from apex.core.cost_tracker import get_daily_cost

        result = get_daily_cost(days=1, db_path=_TMP_DB)
        json_str = json.dumps(result)
        parsed = json.loads(json_str)
        self.assertIsInstance(parsed, list)
        self.assertGreater(len(parsed), 0)


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
