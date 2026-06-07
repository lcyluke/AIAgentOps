"""Cost Tracker — multi-agent operation cost tracking.

Reads token usage from Hermes state.db (sessions table) and produces
JSON-serializable cost reports for dashboard consumption.

Pricing: DeepSeek V4 Pro ($1/M input, $4/M output), with fallbacks
for other known models.

Output:
  - get_session_cost(session_id) -> dict
  - get_project_cost(project_name, days=30) -> dict
  - get_agent_cost(agent_name, days=30) -> dict
  - get_daily_cost(days=7) -> list[dict]
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── Configuration ────────────────────────────────────────────────────────────

HERMES_HOME = Path.home() / ".hermes"
STATE_DB = HERMES_HOME / "state.db"

# USD per 1M tokens
PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4-pro": {"input": 1.0, "output": 4.0},
    "deepseek-chat": {"input": 0.14, "output": 0.28},
    "deepseek-r1": {"input": 0.55, "output": 2.19},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0},
    "claude-opus-4-7": {"input": 15.0, "output": 75.0},
    "gpt-4o": {"input": 2.5, "output": 10.0},
}

# Default pricing when model not in PRICING
DEFAULT_PRICING = PRICING["deepseek-v4-pro"]

# Keyword-based project detection (heuristic)
PROJECT_KEYWORDS: dict[str, list[str]] = {
    "badminton-coach-ai": [
        "badminton", "羽球", "羽毛球", "动作", "训练", "coach", "batting",
    ],
    "apex": [
        "apex", "dashboard", "fleet", "bridge", "orchestrat",
    ],
    "finopsai": [
        "finops", "云成本", "SaaS", "billing", "cost",
    ],
    "shenzhen-badminton": [
        "深圳", "地图", "场馆", "venue", "court",
    ],
    "autodl": [
        "autodl", "gpu", "GPU", "算力",
    ],
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a connection to the state database (default: ~/.hermes/state.db)."""
    path = db_path or STATE_DB
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _calculate_cost(
    input_tokens: int,
    output_tokens: int,
    model: str | None = None,
) -> float:
    """Estimate USD cost from token counts using pricing table.

    Falls back to DeepSeek V4 Pro pricing for unknown models.
    """
    pricing = PRICING.get(model or "", DEFAULT_PRICING)
    inp_cost = (input_tokens / 1_000_000.0) * pricing["input"]
    out_cost = (output_tokens / 1_000_000.0) * pricing["output"]
    return round(inp_cost + out_cost, 6)


def _detect_project(title: str, system_prompt: str, session_id: str) -> Optional[str]:
    """Return the project key if the session matches known project keywords.

    Uses a scoring system: each matching keyword adds points equal to its
    length. The project with the highest score wins (ties go to the first
    encountered in PROJECT_KEYWORDS order).
    """
    text = f"{title or ''} {system_prompt or ''} {session_id}".lower()
    best_project: Optional[str] = None
    best_score: int = 0
    for key, keywords in PROJECT_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw.lower() in text:
                score += len(kw)
        if score > best_score:
            best_score = score
            best_project = key
    return best_project


# ── Public API ───────────────────────────────────────────────────────────────

def get_session_cost(session_id: str, db_path: Optional[Path] = None) -> dict:
    """Return cost details for a single session.

    Returns a dict with keys: session_id, model, source, title,
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
    reasoning_tokens, estimated_cost_usd, started_at, ended_at,
    project (heuristic), and agent (handoff_platform or source).

    If the session is not found, returns {"error": "session not found"}.
    """
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            """SELECT id, model, source, title, system_prompt,
                      input_tokens, output_tokens,
                      cache_read_tokens, cache_write_tokens, reasoning_tokens,
                      started_at, ended_at,
                      handoff_platform, estimated_cost_usd
               FROM sessions WHERE id = ?""",
            (session_id,),
        ).fetchone()

        if row is None:
            return {"error": "session not found"}

        inp = row["input_tokens"] or 0
        out = row["output_tokens"] or 0
        model = row["model"] or "unknown"

        computed_cost = _calculate_cost(inp, out, model)
        # Prefer calculated cost, but include stored cost for reference
        stored_cost = row["estimated_cost_usd"]

        return {
            "session_id": row["id"],
            "model": model,
            "source": row["source"],
            "title": row["title"],
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_tokens": row["cache_read_tokens"] or 0,
            "cache_write_tokens": row["cache_write_tokens"] or 0,
            "reasoning_tokens": row["reasoning_tokens"] or 0,
            "estimated_cost_usd": computed_cost,
            "stored_cost_usd": stored_cost,
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "project": _detect_project(
                row["title"] or "", row["system_prompt"] or "", row["id"]
            ),
            "agent": row["handoff_platform"] or row["source"] or "unknown",
        }
    finally:
        conn.close()


def get_project_cost(
    project_name: str,
    days: int = 30,
    db_path: Optional[Path] = None,
) -> dict:
    """Aggregate cost for a named project over the last *days* days.

    Project matching uses keyword heuristics on session title + system_prompt.
    Returns a dict with: project, days, total_sessions, total_input_tokens,
    total_output_tokens, total_estimated_cost_usd, by_model, by_source,
    daily_breakdown.
    """
    if project_name not in PROJECT_KEYWORDS:
        return {"error": f"unknown project: {project_name}"}

    keywords = PROJECT_KEYWORDS[project_name]
    cutoff = (datetime.now() - timedelta(days=days)).timestamp()

    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            """SELECT id, model, source, title, system_prompt,
                      input_tokens, output_tokens, started_at
               FROM sessions
               WHERE started_at > ?
               ORDER BY started_at DESC""",
            (cutoff,),
        ).fetchall()

        # Filter by keyword match
        matched = []
        for row in rows:
            text = f"{row['title'] or ''} {row['system_prompt'] or ''} {row['id']}".lower()
            if any(kw.lower() in text for kw in keywords):
                matched.append(row)

        if not matched:
            return {
                "project": project_name,
                "days": days,
                "total_sessions": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_estimated_cost_usd": 0.0,
                "by_model": {},
                "by_source": {},
                "daily_breakdown": [],
            }

        total_inp = 0
        total_out = 0
        total_cost_sum = 0.0
        by_model: dict[str, dict] = {}
        by_source: dict[str, dict] = {}
        daily: dict[str, dict] = {}

        for row in matched:
            inp = row["input_tokens"] or 0
            out = row["output_tokens"] or 0
            total_inp += inp
            total_out += out

            model = row["model"] or "unknown"
            source = row["source"] or "unknown"
            cost = _calculate_cost(inp, out, model)
            total_cost_sum += cost

            # By model
            if model not in by_model:
                by_model[model] = {"sessions": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0}
            by_model[model]["sessions"] += 1
            by_model[model]["input_tokens"] += inp
            by_model[model]["output_tokens"] += out
            by_model[model]["cost"] += cost

            # By source
            if source not in by_source:
                by_source[source] = {"sessions": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0}
            by_source[source]["sessions"] += 1
            by_source[source]["input_tokens"] += inp
            by_source[source]["output_tokens"] += out
            by_source[source]["cost"] += cost

            # Daily breakdown
            ts = row["started_at"]
            if ts:
                day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                if day not in daily:
                    daily[day] = {"sessions": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0}
                daily[day]["sessions"] += 1
                daily[day]["input_tokens"] += inp
                daily[day]["output_tokens"] += out
                daily[day]["cost"] += cost

        total_cost = round(total_cost_sum, 6)

        # Round and sort daily breakdown
        daily_list = [
            {"date": d, **{k: round(v, 6) if k == "cost" else v for k, v in s.items()}}
            for d, s in sorted(daily.items())
        ]

        # Round by_model and by_source costs
        for d in by_model.values():
            d["cost"] = round(d["cost"], 6)
        for d in by_source.values():
            d["cost"] = round(d["cost"], 6)

        return {
            "project": project_name,
            "days": days,
            "total_sessions": len(matched),
            "total_input_tokens": total_inp,
            "total_output_tokens": total_out,
            "total_estimated_cost_usd": round(total_cost, 6),
            "by_model": by_model,
            "by_source": by_source,
            "daily_breakdown": daily_list,
        }
    finally:
        conn.close()


def get_agent_cost(
    agent_name: str,
    days: int = 30,
    db_path: Optional[Path] = None,
) -> dict:
    """Aggregate cost for a named agent over the last *days* days.

    Agent matching checks handoff_platform first, then source field.
    Returns a dict with: agent, days, total_sessions, total_input_tokens,
    total_output_tokens, total_estimated_cost_usd, by_model, by_project,
    daily_breakdown.
    """
    cutoff = (datetime.now() - timedelta(days=days)).timestamp()

    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            """SELECT id, model, source, title, system_prompt,
                      input_tokens, output_tokens,
                      handoff_platform, started_at
               FROM sessions
               WHERE started_at > ?
               ORDER BY started_at DESC""",
            (cutoff,),
        ).fetchall()

        # Filter: agent matches handoff_platform or source
        matched = []
        for row in rows:
            agent = row["handoff_platform"] or row["source"] or ""
            if agent.lower() == agent_name.lower():
                matched.append(row)

        if not matched:
            return {
                "agent": agent_name,
                "days": days,
                "total_sessions": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_estimated_cost_usd": 0.0,
                "by_model": {},
                "by_project": {},
                "daily_breakdown": [],
            }

        total_inp = 0
        total_out = 0
        total_cost_sum = 0.0
        by_model: dict[str, dict] = {}
        by_project: dict[str, dict] = {}
        daily: dict[str, dict] = {}

        for row in matched:
            inp = row["input_tokens"] or 0
            out = row["output_tokens"] or 0
            total_inp += inp
            total_out += out

            model = row["model"] or "unknown"
            cost = _calculate_cost(inp, out, model)
            total_cost_sum += cost

            # By model
            if model not in by_model:
                by_model[model] = {"sessions": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0}
            by_model[model]["sessions"] += 1
            by_model[model]["input_tokens"] += inp
            by_model[model]["output_tokens"] += out
            by_model[model]["cost"] += cost

            # By project
            project = _detect_project(
                row["title"] or "", row["system_prompt"] or "", row["id"]
            ) or "other"
            if project not in by_project:
                by_project[project] = {"sessions": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0}
            by_project[project]["sessions"] += 1
            by_project[project]["input_tokens"] += inp
            by_project[project]["output_tokens"] += out
            by_project[project]["cost"] += cost

            # Daily breakdown
            ts = row["started_at"]
            if ts:
                day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                if day not in daily:
                    daily[day] = {"sessions": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0}
                daily[day]["sessions"] += 1
                daily[day]["input_tokens"] += inp
                daily[day]["output_tokens"] += out
                daily[day]["cost"] += cost

        total_cost = round(total_cost_sum, 6)

        daily_list = [
            {"date": d, **{k: round(v, 6) if k == "cost" else v for k, v in s.items()}}
            for d, s in sorted(daily.items())
        ]

        for d in by_model.values():
            d["cost"] = round(d["cost"], 6)
        for d in by_project.values():
            d["cost"] = round(d["cost"], 6)

        return {
            "agent": agent_name,
            "days": days,
            "total_sessions": len(matched),
            "total_input_tokens": total_inp,
            "total_output_tokens": total_out,
            "total_estimated_cost_usd": round(total_cost, 6),
            "by_model": by_model,
            "by_project": by_project,
            "daily_breakdown": daily_list,
        }
    finally:
        conn.close()


def get_daily_cost(days: int = 7, db_path: Optional[Path] = None) -> list[dict]:
    """Return daily cost timeline for the last *days* days.

    Each entry is a dict with: date, sessions, input_tokens, output_tokens,
    estimated_cost_usd, by_model, by_source.
    """
    cutoff = (datetime.now() - timedelta(days=days)).timestamp()

    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            """SELECT model, source, input_tokens, output_tokens, started_at
               FROM sessions
               WHERE started_at > ?
               ORDER BY started_at""",
            (cutoff,),
        ).fetchall()

        daily: dict[str, dict] = {}

        for row in rows:
            inp = row["input_tokens"] or 0
            out = row["output_tokens"] or 0
            model = row["model"] or "unknown"
            source = row["source"] or "unknown"
            cost = _calculate_cost(inp, out, model)
            ts = row["started_at"]

            if not ts:
                continue

            day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            if day not in daily:
                daily[day] = {
                    "sessions": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost": 0.0,
                    "by_model": {},
                    "by_source": {},
                }

            d = daily[day]
            d["sessions"] += 1
            d["input_tokens"] += inp
            d["output_tokens"] += out
            d["cost"] += cost

            if model not in d["by_model"]:
                d["by_model"][model] = 0
            d["by_model"][model] += 1

            if source not in d["by_source"]:
                d["by_source"][source] = 0
            d["by_source"][source] += 1

        result = []
        # Fill in all days in the range (including empty ones)
        for i in range(days - 1, -1, -1):
            day_str = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            if day_str in daily:
                d = daily[day_str]
                result.append({
                    "date": day_str,
                    "sessions": d["sessions"],
                    "input_tokens": d["input_tokens"],
                    "output_tokens": d["output_tokens"],
                    "estimated_cost_usd": round(d["cost"], 6),
                    "by_model": d["by_model"],
                    "by_source": d["by_source"],
                })
            else:
                result.append({
                    "date": day_str,
                    "sessions": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "estimated_cost_usd": 0.0,
                    "by_model": {},
                    "by_source": {},
                })

        return result
    finally:
        conn.close()
