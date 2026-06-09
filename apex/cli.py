"""APEX CLI — cross-runtime multi-agent orchestrator.

Entry point for the `apex` command (pyproject.toml → apex.cli:app).

Provides deploy / status / audit / list / daemon subcommands.
Reads config from ~/.apex/config.yaml.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import typer

# ── Rich: optional pretty output ──────────────────────────────────────────
_console = None
_Table = None
_Panel = None
_box = None
_HAS_RICH = False

try:
    from rich.console import Console as _RichConsole
    from rich.table import Table as _RichTable
    from rich.panel import Panel as _RichPanel
    from rich import box as _rich_box

    _console = _RichConsole()
    _Table = _RichTable
    _Panel = _RichPanel
    _box = _rich_box
    _HAS_RICH = True
except ImportError:
    pass

# ── Version ───────────────────────────────────────────────────────────────
VERSION = "1.0.0"

# ── Paths ─────────────────────────────────────────────────────────────────
APEX_DIR = Path.home() / ".apex"
CONFIG_PATH = APEX_DIR / "config.yaml"
SESSIONS_PATH = APEX_DIR / "sessions.json"
APEX_DIR.mkdir(parents=True, exist_ok=True)


# ── Config helpers ────────────────────────────────────────────────────────
def load_config() -> dict:
    """Load configuration from ~/.apex/config.yaml (YAML)."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        import yaml as _yaml
    except ImportError:
        # fallback: try to parse as JSON
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    try:
        with open(CONFIG_PATH) as f:
            data = _yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(data: dict) -> None:
    """Persist configuration to ~/.apex/config.yaml."""
    APEX_DIR.mkdir(parents=True, exist_ok=True)
    try:
        import yaml as _yaml

        with open(CONFIG_PATH, "w") as f:
            _yaml.safe_dump(data, f, default_flow_style=False)
    except ImportError:
        with open(CONFIG_PATH, "w") as f:
            json.dump(data, f, indent=2)


# ── Session store (simple JSON file) ──────────────────────────────────────
def _load_sessions() -> dict[str, dict]:
    """Return {session_id: session_data} from persistent store."""
    if not SESSIONS_PATH.exists():
        return {}
    try:
        with open(SESSIONS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_sessions(sessions: dict[str, dict]) -> None:
    """Persist sessions to JSON."""
    APEX_DIR.mkdir(parents=True, exist_ok=True)
    with open(SESSIONS_PATH, "w") as f:
        json.dump(sessions, f, indent=2, default=str)


# ── Pretty-print helpers ──────────────────────────────────────────────────
def _print_table(title: str, columns: list[str], rows: list[list[str]]) -> None:
    """Print a table with Rich (if available) or plain text."""
    if _HAS_RICH:
        tbl = _Table(title=title, box=_box.ROUNDED)
        for col in columns:
            tbl.add_column(col)
        for row in rows:
            tbl.add_row(*[str(c) for c in row])
        _console.print(tbl)
    else:
        # Plain-text fallback
        width = max(len(c) for c in columns) if columns else 10
        header = "  ".join(c.ljust(width) for c in columns)
        sep = "  ".join("-" * width for _ in columns)
        typer.echo(f"\n{title}")
        typer.echo(sep)
        typer.echo(header)
        typer.echo(sep)
        for row in rows:
            typer.echo("  ".join(str(c).ljust(width) for c in row))
        typer.echo(sep)


def _print_panel(message: str, style: str = "") -> None:
    """Print a highlighted panel."""
    if _HAS_RICH:
        _console.print(_Panel(message))
    else:
        typer.echo(f"\n{message}\n")


# ── Adapter registry (discovery) ─────────────────────────────────────────
def _discover_adapters() -> dict[str, object]:
    """Discover all registered RuntimeAdapter implementations.

    Scans apex.adapters submodules for classes implementing RuntimeAdapter.
    Returns {runtime_name: adapter_instance}.
    """
    adapters: dict[str, object] = {}
    try:
        from apex.adapters.base import RuntimeAdapter

        import importlib
        import pkgutil

        # Try to import known adapter modules
        adapter_modules = [
            "apex.adapters.kiro",
            "apex.adapters.claude",
            "apex.adapters.hermes",
        ]
        for mod_name in adapter_modules:
            try:
                mod = importlib.import_module(mod_name)
                for attr_name in dir(mod):
                    attr = getattr(mod, attr_name)
                    if isinstance(attr, type) and attr is not RuntimeAdapter and hasattr(attr, "name"):
                        # Check if it's a RuntimeAdapter implementation
                        try:
                            inst = attr()
                            if hasattr(inst, "spawn") and hasattr(inst, "name"):
                                adapters[inst.name] = inst
                        except Exception:
                            pass
            except ImportError:
                pass
    except ImportError:
        pass
    return adapters


def _get_adapter(runtime: str) -> Optional[object]:
    """Get a specific adapter by runtime name."""
    return _discover_adapters().get(runtime)


# ── Typer app ─────────────────────────────────────────────────────────────
app = typer.Typer(
    name="apex",
    help="APEX — cross-runtime multi-agent orchestrator for Kiro/Claude Code/Hermes.",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"APEX v{VERSION}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        help="Show version and exit.",
        is_eager=True,
    ),
    config: Optional[str] = typer.Option(
        None,
        "--config",
        help="Path to config file (default: ~/.apex/config.yaml).",
        hidden=True,
    ),
) -> None:
    """APEX — cross-runtime multi-agent orchestrator.

    Deploy agents, monitor sessions, audit completions, and run the daemon.

    \b
    Quick start:
        apex deploy --agent coder --task "write a hello-world script"
        apex status
        apex audit <session_id>
        apex list
        apex daemon
    """
    pass


# ═══════════════════════════════════════════════════════════════════════════
# deploy — spawn an agent session
# ═══════════════════════════════════════════════════════════════════════════
@app.command()
def deploy(
    agent: str = typer.Option(
        ...,
        "--agent",
        "-a",
        help="Agent name or role (e.g. coder, reviewer, architect).",
    ),
    task: str = typer.Option(
        ...,
        "--task",
        "-t",
        help="Task description / brief for the agent to execute.",
    ),
    cwd: str = typer.Option(
        ".",
        "--cwd",
        "-d",
        help="Working directory for the agent session.",
    ),
    runtime: str = typer.Option(
        "hermes",
        "--runtime",
        "-r",
        help="Target runtime: kiro, claude, hermes.",
    ),
    max_steps: int = typer.Option(
        20,
        "--max-steps",
        help="Maximum tool-call steps allowed.",
    ),
) -> None:
    """Spawn an agent session to execute a task.

    Creates a new session on the specified runtime, registers it
    in the local session store, and prints the session handle.

    \b
    Examples:
        apex deploy -a coder -t "add unit tests for auth.py"
        apex deploy -a reviewer -t "audit auth.py" --runtime claude
        apex deploy -a architect -t "design DB schema" --cwd ~/projects/myapp --max-steps 10
    """
    import uuid

    # Generate a unique session ID
    session_id = str(uuid.uuid4())[:8]
    instance = f"{agent}-{session_id}"
    resolved_cwd = str(Path(cwd).resolve())

    # Build spawn spec from protocol
    try:
        from apex.adapters.base import SpawnSpec
    except ImportError:
        typer.echo("ERROR: Cannot import SpawnSpec — is apex installed?", err=True)
        raise typer.Exit(code=1)

    spec = SpawnSpec(
        agent=agent,
        instance=instance,
        brief=task,
        cwd=resolved_cwd,
        max_steps=max_steps,
    )

    # Attempt to spawn via the appropriate adapter
    adapter = _get_adapter(runtime)
    if adapter is None:
        typer.echo(
            f"WARNING: No adapter found for runtime '{runtime}'. "
            f"Recording session locally only."
        )

    handle = None
    if adapter is not None:
        try:
            handle = adapter.spawn(spec)
        except Exception as exc:
            typer.echo(f"ERROR: Spawn failed — {exc}", err=True)
            raise typer.Exit(code=1)

    # Persist session record
    sessions = _load_sessions()
    sessions[session_id] = {
        "session_id": session_id,
        "instance": instance,
        "agent": agent,
        "task": task,
        "cwd": resolved_cwd,
        "runtime": runtime,
        "max_steps": max_steps,
        "state": "running",
        "created_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "handle": {
            "name": handle.name if handle else instance,
            "runtime": runtime,
            "tmux_session": handle.tmux_session if handle else "",
        } if handle else None,
    }
    _save_sessions(sessions)

    # Pretty output
    rows = [
        ["Session ID", session_id],
        ["Agent", agent],
        ["Runtime", runtime],
        ["Working Dir", resolved_cwd],
        ["Task", task],
        ["Max Steps", str(max_steps)],
        ["State", "running"],
    ]
    if _HAS_RICH:
        tbl = _Table(title="🚀 Agent Deployed", box=_box.ROUNDED, show_header=False)
        tbl.add_column("Key", style="cyan")
        tbl.add_column("Value", style="green")
        for k, v in rows:
            tbl.add_row(k, v)
        _console.print(tbl)
        _console.print(
            f"\n[dim]Monitor with:[/dim] apex status   |   [dim]Audit:[/dim] apex audit {session_id}"
        )
    else:
        typer.echo("\n🚀 Agent Deployed")
        for k, v in rows:
            typer.echo(f"  {k}: {v}")
        typer.echo(f"\nMonitor with: apex status   |   Audit: apex audit {session_id}")


# ═══════════════════════════════════════════════════════════════════════════
# status — show all active sessions
# ═══════════════════════════════════════════════════════════════════════════
@app.command()
def status(
    detail: bool = typer.Option(
        False,
        "--detail",
        "-v",
        help="Show full session details.",
    ),
    runtime: Optional[str] = typer.Option(
        None,
        "--runtime",
        "-r",
        help="Filter by runtime (kiro, claude, hermes).",
    ),
) -> None:
    """Show all active sessions.

    Lists every session stored in the local session registry
    with its current state, agent, runtime, and task brief.

    \b
    Examples:
        apex status
        apex status --runtime hermes
        apex status --detail
    """
    sessions = _load_sessions()
    if not sessions:
        typer.echo("No active sessions.")
        return

    # Filter by runtime if requested
    filtered = sessions
    if runtime:
        filtered = {
            k: v for k, v in sessions.items()
            if v.get("runtime", "").lower() == runtime.lower()
        }

    if not filtered:
        typer.echo(f"No sessions found for runtime '{runtime}'.")
        return

    if detail:
        for sid, s in sorted(filtered.items()):
            if _HAS_RICH:
                _console.print(
                    _Panel(
                        f"[bold cyan]{sid}[/bold cyan]\n"
                        f"  Agent:    {s.get('agent', '?')}\n"
                        f"  Runtime:  {s.get('runtime', '?')}\n"
                        f"  State:    {s.get('state', '?')}\n"
                        f"  CWD:      {s.get('cwd', '?')}\n"
                        f"  Task:     {s.get('task', '?')}\n"
                        f"  Created:  {s.get('created_ts', '?')}",
                        title=f"Session {sid}",
                    )
                )
            else:
                typer.echo(f"\n── Session {sid} ──")
                typer.echo(f"  Agent:    {s.get('agent', '?')}")
                typer.echo(f"  Runtime:  {s.get('runtime', '?')}")
                typer.echo(f"  State:    {s.get('state', '?')}")
                typer.echo(f"  CWD:      {s.get('cwd', '?')}")
                typer.echo(f"  Task:     {s.get('task', '?')}")
                typer.echo(f"  Created:  {s.get('created_ts', '?')}")
    else:
        columns = ["Session ID", "Agent", "Runtime", "State", "Task"]
        rows = [
            [
                sid,
                s.get("agent", "?"),
                s.get("runtime", "?"),
                s.get("state", "?"),
                (s.get("task", "")[:50] + "...")
                if len(s.get("task", "")) > 50
                else s.get("task", ""),
            ]
            for sid, s in sorted(filtered.items())
        ]
        _print_table("Active Sessions", columns, rows)
        typer.echo(f"\n{len(filtered)} session(s). Use --detail for full info.")


# ═══════════════════════════════════════════════════════════════════════════
# audit — run auditor check on a session
# ═══════════════════════════════════════════════════════════════════════════
@app.command()
def audit(
    session_id: str = typer.Argument(..., help="Session ID to audit."),
    criteria_file: Optional[str] = typer.Option(
        None,
        "--criteria",
        "-c",
        help="Path to a JSON/YAML file with completion criteria (overrides stored criteria).",
    ),
) -> None:
    """Run an auditor completion check against a session.

    Verifies each completion criterion (shell_exit_zero, file_exists,
    grep_present, grep_absent) against the session's working directory.

    \b
    Examples:
        apex audit abc12345
        apex audit abc12345 --criteria ./criteria.yaml
    """
    sessions = _load_sessions()
    if session_id not in sessions:
        typer.echo(f"ERROR: Session '{session_id}' not found.", err=True)
        raise typer.Exit(code=1)

    s = sessions[session_id]
    cwd = s.get("cwd", ".")

    # Load criteria
    criteria: list[dict] = []
    if criteria_file:
        cf_path = Path(criteria_file)
        if not cf_path.exists():
            typer.echo(f"ERROR: Criteria file '{criteria_file}' not found.", err=True)
            raise typer.Exit(code=1)
        try:
            if cf_path.suffix in (".yaml", ".yml"):
                import yaml as _yaml

                with open(cf_path) as f:
                    criteria = _yaml.safe_load(f)
            else:
                with open(cf_path) as f:
                    criteria = json.load(f)
            if not isinstance(criteria, list):
                typer.echo("ERROR: Criteria file must contain a list.", err=True)
                raise typer.Exit(code=1)
        except Exception as exc:
            typer.echo(f"ERROR: Failed to parse criteria file — {exc}", err=True)
            raise typer.Exit(code=1)
    else:
        # Use stored criteria if available, otherwise default empty
        criteria = s.get("criteria", [])
        if not criteria:
            typer.echo(
                "No criteria stored for this session. "
                "Use --criteria <file> to provide criteria.\n"
                "Example criteria.json:\n"
                '  [{"kind": "file_exists", "path": "output.txt"}]'
            )
            raise typer.Exit(code=0)

    # Run auditor
    try:
        from apex.core.auditor import Auditor

        auditor = Auditor()
        result = auditor.verify(criteria, cwd)
    except ImportError:
        typer.echo("ERROR: Cannot import Auditor — is apex installed?", err=True)
        raise typer.Exit(code=1)

    # Output
    if _HAS_RICH:
        if result.passed:
            _console.print(
                _Panel.fit(
                    f"Session {session_id}: [bold green]PASSED[/bold green]",
                    title="✅ Audit Result",
                    border_style="green",
                )
            )
        else:
            _console.print(
                _Panel.fit(
                    f"Session {session_id}: [bold red]FAILED[/bold red]\n\n"
                    + "\n".join(f"  • {f}" for f in result.failures),
                    title="❌ Audit Result",
                    border_style="red",
                )
            )
    else:
        if result.passed:
            typer.echo(f"\n✅ Audit PASSED — session {session_id}")
        else:
            typer.echo(f"\n❌ Audit FAILED — session {session_id}")
            for f in result.failures:
                typer.echo(f"  • {f}")

    # Update session state
    s["state"] = "done" if result.passed else "failed"
    s["audit_failures"] = result.failures
    _save_sessions(sessions)


# ═══════════════════════════════════════════════════════════════════════════
# list — list all registered agents/runtimes
# ═══════════════════════════════════════════════════════════════════════════
@app.command(name="list")
def list_agents(
    runtime: Optional[str] = typer.Option(
        None,
        "--runtime",
        "-r",
        help="Filter by runtime (kiro, claude, hermes).",
    ),
) -> None:
    """List all registered agents and runtimes.

    Discovers available RuntimeAdapter implementations and
    prints their capabilities.

    \b
    Examples:
        apex list
        apex list --runtime hermes
    """
    adapters = _discover_adapters()

    if not adapters:
        typer.echo("No runtime adapters found.")
        typer.echo(
            "Expected adapter modules at apex.adapters.{kiro,claude,hermes}."
        )
        return

    # Filter by runtime
    if runtime:
        if runtime in adapters:
            adapters = {runtime: adapters[runtime]}
        else:
            typer.echo(f"No adapter registered for runtime '{runtime}'.")
            typer.echo(f"Available: {', '.join(sorted(adapters.keys()))}")
            return

    # Build table
    rows = []
    for name, ad in sorted(adapters.items()):
        rows.append([
            name,
            type(ad).__name__,
            "✓" if hasattr(ad, "spawn") else "✗",
            "✓" if hasattr(ad, "prompt") else "✗",
            "✓" if hasattr(ad, "dispose") else "✗",
        ])

    _print_table(
        "Registered Runtimes",
        ["Runtime", "Adapter Class", "Spawn", "Prompt", "Dispose"],
        rows,
    )

    # Also list known agent roles from tool whitelist
    try:
        from apex.core.tool_risk import ROLE_TOOL_WHITELIST

        role_rows = [
            [role, ", ".join(tools[:3]) + ("..." if len(tools) > 3 else "")]
            for role, tools in sorted(ROLE_TOOL_WHITELIST.items())
        ]
        _print_table(
            "Available Agent Roles",
            ["Role", "Allowed Tools (first 3)"],
            role_rows,
        )
    except ImportError:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# daemon — start the daemon server
# ═══════════════════════════════════════════════════════════════════════════
@app.command()
def daemon(
    port: int = typer.Option(8765, "--port", "-p", help="Daemon TCP port."),
    host: str = typer.Option("127.0.0.1", "--host", "-H", help="Bind address."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of worker threads/processes."),
) -> None:
    """Start the APEX daemon server.

    The daemon listens for ApexCommand messages (per apex.protocol),
    dispatches them to the appropriate runtime adapter, and streams
    ApexEvent messages back to connected clients.

    \b
    Protocol: JSON lines over TCP (one message per line).
    Each line is a JSON-encoded ApexEvent or ApexCommand.

    \b
    Examples:
        apex daemon
        apex daemon --port 9999 --host 0.0.0.0
    """
    typer.echo(f"⚡ APEX Daemon v{VERSION}")
    typer.echo(f"   Listening on {host}:{port} (workers={workers})")
    typer.echo(f"   Config: {CONFIG_PATH}")
    typer.echo(f"   Sessions: {SESSIONS_PATH}")
    typer.echo()

    # Load config
    cfg = load_config()

    # Print startup info
    adapters = _discover_adapters()
    if adapters:
        typer.echo(f"   Loaded adapters: {', '.join(sorted(adapters.keys()))}")
    else:
        typer.echo("   WARNING: No runtime adapters found.")

    if _HAS_RICH:
        _console.print(
            _Panel.fit(
                "\n".join(
                    [
                        f"[bold]Host:[/bold]        {host}:{port}",
                        f"[bold]Workers:[/bold]     {workers}",
                        f"[bold]Adapters:[/bold]    {', '.join(sorted(adapters.keys())) if adapters else 'none'}",
                        f"[bold]Sessions:[/bold]    {SESSIONS_PATH}",
                    ]
                ),
                title="⚡ Daemon Started",
                border_style="blue",
            )
        )

    typer.echo("Press Ctrl+C to stop.")

    # Attempt to start the daemon server
    try:
        _run_daemon(host, port, cfg)
    except KeyboardInterrupt:
        typer.echo("\nDaemon stopped.")
    except Exception as exc:
        typer.echo(f"Daemon error: {exc}", err=True)
        raise typer.Exit(code=1)


def _run_daemon(host: str, port: int, cfg: dict) -> None:
    """Minimal JSON-lines TCP daemon loop.

    In production this would be replaced by a proper asyncio server
    or a FastAPI/Flask HTTP server. This is a synchronous placeholder
    that demonstrates the protocol channel.
    """
    import socket

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(5)
    server.settimeout(1.0)  # allow Ctrl+C check every second

    typer.echo(f"  → Socket open, accepting connections...")

    try:
        while True:
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue  # loop back to check KeyboardInterrupt

            typer.echo(f"  ← Connection from {addr}")
            try:
                with conn:
                    conn.settimeout(5.0)
                    data = b""
                    while True:
                        try:
                            chunk = conn.recv(4096)
                        except socket.timeout:
                            break
                        if not chunk:
                            break
                        data += chunk
                        if b"\n" in data:
                            break

                    if data:
                        try:
                            msg = json.loads(data.decode("utf-8").strip())
                            msg_type = msg.get("type", "?")
                            typer.echo(f"  → Received: {msg_type}")
                            # Echo back a status event
                            import time as _time

                            resp = {
                                "v": 1,
                                "ts": _time.strftime(
                                    "%Y-%m-%dT%H:%M:%SZ", _time.gmtime()
                                ),
                                "type": "turn.heartbeat",
                                "session_id": msg.get("instance", "?"),
                                "tool": "daemon.echo",
                                "ok": True,
                            }
                            conn.sendall(
                                (json.dumps(resp) + "\n").encode("utf-8")
                            )
                        except json.JSONDecodeError:
                            typer.echo(
                                f"  ✗ Invalid JSON from {addr}", err=True
                            )
            except Exception as exc:
                typer.echo(f"  ✗ Connection error: {exc}", err=True)
    finally:
        server.close()


# ── Entry point ───────────────────────────────────────────────────────────

# Register fleet commands
try:
    from apex.cli_fleet import register_fleet
    register_fleet(app)
except ImportError:
    pass

if __name__ == "__main__":
    app()
