#!/usr/bin/env python3
"""APEX Fleet TUI — terminal dashboard for multi-agent operations.

Requires: pip install rich
Usage: python -m apex.tui
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    RICH_OK = True
except ImportError:
    RICH_OK = False


APEX_HOME = Path(os.path.expanduser("~/.apex"))
HERMES_HOME = Path(os.path.expanduser("~/.hermes"))


# ── Data fetchers ───────────────────────────────────────────

def fetch_sessions() -> list[dict]:
    """Get active sessions from apexd or storage."""
    sessions = []
    try:
        from apex.storage.db import list_sessions as db_list
        for s in db_list(limit=50):
            sessions.append({
                "id": getattr(s, "session_id", "?"),
                "name": getattr(s, "name", "?"),
                "runtime": getattr(s, "runtime", "?"),
                "status": getattr(s, "status", "idle"),
            })
    except Exception:
        pass
    return sessions


def fetch_tasks() -> list[dict]:
    """Get tasks from Apex task manager."""
    tasks = []
    try:
        from apex.orchestration.task_manager import TaskManager
        tm = TaskManager()
        for t in tm.list_tasks(limit=30):
            tasks.append({
                "id": getattr(t, "id", "?")[:13],
                "title": getattr(t, "title", "?")[:40],
                "status": getattr(t, "status", "?"),
                "assignee": getattr(t, "assignee", "?") or "-",
            })
    except Exception:
        pass
    return tasks


def fetch_cost() -> dict:
    """Get cost summary."""
    try:
        from apex.core.cost_tracker import get_daily_cost
        days = get_daily_cost(days=7)
        total = sum(d.get("total_cost_usd", 0) for d in days)
        return {"7day_total": round(total, 4), "days": len(days)}
    except Exception:
        return {"7day_total": 0, "days": 0}


def fetch_instances() -> int:
    """Count running agent processes."""
    try:
        r = subprocess.run(
            ["pgrep", "-f", "hermes -p|claude -p|kiro --acp"],
            capture_output=True, text=True, timeout=3,
        )
        return len(r.stdout.strip().split("\n")) if r.stdout.strip() else 0
    except Exception:
        return 0


# ── Render functions ────────────────────────────────────────

def make_header() -> Panel:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return Panel(
        f"[bold cyan]⚓ APEX Fleet TUI[/bold cyan]  |  {now}  |  Multi-Agent OS",
        style="blue",
    )


def make_sessions_table(sessions: list[dict]) -> Panel:
    table = Table(title="Active Sessions", box=None, padding=(0, 1))
    table.add_column("ID", style="dim", width=10)
    table.add_column("Name", width=20)
    table.add_column("Runtime", width=10)
    table.add_column("Status", width=10)

    for s in sessions[:15]:
        status_color = {"running": "green", "idle": "yellow", "stopped": "red"}.get(
            s["status"], "white"
        )
        table.add_row(
            s["id"], s["name"], s["runtime"],
            f"[{status_color}]{s['status']}[/{status_color}]",
        )

    if not sessions:
        table.add_row("-", "No active sessions", "-", "-")

    return Panel(table, title="📡 Sessions")


def make_tasks_table(tasks: list[dict]) -> Panel:
    table = Table(title="Recent Tasks", box=None, padding=(0, 1))
    table.add_column("ID", style="dim", width=14)
    table.add_column("Title", width=42)
    table.add_column("Status", width=12)
    table.add_column("Assignee", width=12)

    for t in tasks[:15]:
        sc = {"completed": "green", "in_progress": "yellow", "blocked": "red"}.get(
            t["status"], "white"
        )
        table.add_row(t["id"], t["title"], f"[{sc}]{t['status']}[/{sc}]", t["assignee"])

    if not tasks:
        table.add_row("-", "No tasks", "-", "-")

    return Panel(table, title="📋 Tasks")


def make_cost_panel(cost: dict, instances: int) -> Panel:
    text = Text()
    text.append(f"💰 7-Day Cost: ", style="bold")
    text.append(f"${cost['7day_total']:.2f}", style="bold yellow")
    text.append(f"\n📊 Days tracked: {cost['days']}")
    text.append(f"\n🖥  Running instances: {instances}")
    return Panel(text, title="💵 Cost & Resources")


# ── Main loop ───────────────────────────────────────────────

def run_tui():
    if not RICH_OK:
        print("❌ rich not installed. Run: pip install rich")
        return

    console = Console()
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
    )
    layout["body"].split_row(
        Layout(name="left"),
        Layout(name="right"),
    )
    layout["left"].split_column(
        Layout(name="sessions"),
        Layout(name="cost"),
    )

    def refresh():
        sessions = fetch_sessions()
        tasks = fetch_tasks()
        cost = fetch_cost()
        instances = fetch_instances()

        layout["header"].update(make_header())
        layout["sessions"].update(make_sessions_table(sessions))
        layout["cost"].update(make_cost_panel(cost, instances))
        layout["right"].update(make_tasks_table(tasks))

    with Live(layout, console=console, refresh_per_second=0.5, screen=True) as live:
        while True:
            try:
                refresh()
                time.sleep(2)
            except KeyboardInterrupt:
                console.print("\n[bold]👋 Goodbye.[/bold]")
                break


if __name__ == "__main__":
    run_tui()
