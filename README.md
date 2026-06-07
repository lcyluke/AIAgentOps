# AIAgentOps

> Cross-runtime multi-agent orchestrator — secure, observable, runtime-agnostic agent operations for Kiro, Claude Code, Hermes, and beyond.

## What is AIAgentOps?

AIAgentOps is the operational backbone for multi-agent systems. It provides a unified layer for spawning, monitoring, auditing, and coordinating AI coding agents across different runtimes — all from a single CLI, TUI, or MCP-enabled IDE.

### Architecture

```
┌───────────── Surfaces ─────────────────────────────────────┐
│  CLI (Typer)    TUI (Rich)    MCP (TS)    Installer Skill │
└──────────┬─────────────────────────────────────────────────┘
           │ Unix socket / REST
┌──────────▼─── Core (apexd daemon) ─────────────────────────┐
│ Registry  │ Scheduler │ Blackboard │ Claims   │ Auditor    │
│ EventBus  │ Stagnation│ ToolRisk   │ Injector │ CostTracker│
└──────────┬─────────────────────────────────────────────────┘
           │ RuntimeAdapter Protocol (single SDK boundary)
┌──────────▼─── Runtimes ────────────────────────────────────┐
│  KiroAdapter  │  ClaudeAdapter  │  HermesAdapter  │  ACP  │
└────────────────────────────────────────────────────────────┘
```

### 21 Modules

| Layer | Modules |
|-------|---------|
| **Protocol** | `protocol.py` — typed dataclass events, commands, task specs |
| **Core** | `registry` · `event_bus` · `blackboard` · `claims` · `stagnation` · `auditor` · `tool_risk` · `cost_tracker` · `injector` |
| **Storage** | `db.py` — SQLite WAL, 4 tables, thread-safe |
| **Adapters** | `base` · `kiro` · `claude` · `hermes` · `acp` |
| **Operations** | `daemon.py` · `cli.py` · `tui.py` |
| **MCP** | `ops-mcp/` — TypeScript MCP server (6 tools) |
| **Release** | `scripts/release.mjs` |

## Quick Start

### Install

```bash
# Clone
git clone https://github.com/lcyluke/AIAgentOps.git
cd AIAgentOps

# Python deps (minimal — mostly stdlib)
pip install typer rich

# Optional: TypeScript MCP server
cd ops-mcp && npm install && npm run build
```

### Verify

```bash
python3 tests/unit/test_core.py    # Core logic (6 tests)
python3 tests/unit/test_m2.py      # Blackboard + Claims (48 tests)
python3 tests/unit/test_storage.py # Storage layer
```

### First Command

```bash
python -m apex.cli --help
python -m apex.cli --version
python -m apex.cli status
```

## Runtime Support

| Runtime | Adapter | Mode | Status |
|---------|---------|------|--------|
| **Kiro** | `kiro.py` + `acp.py` | tmux headless / ACP JSON-RPC | ✅ |
| **Claude Code** | `claude.py` | tmux + subprocess | ✅ |
| **Hermes** | `hermes.py` | gateway REST + subprocess | ✅ |

## Key Features

### 🔒 Security (INV-5)

- **Fail-closed redaction**: API keys, emails, home paths masked with auditable fingerprints
- **3-tier risk gate**: READ_ONLY (auto) / STATE_CHANGING (configurable) / DESTRUCTIVE (requires approval)
- **Role whitelists**: per-agent tool access control
- **Dangerous command blocking**: `rm -rf /`, `curl | sh`, fork bombs hard-blocked

### 📋 Shared Context

- **Blackboard**: cross-agent knowledge sharing — one agent's conclusions injected into another's context
- **Claims**: optimistic-lock task registry — prevents duplicate work
- **Injector**: `userPromptSubmit` hook that feeds digest into each agent turn

### 🛡 Trustworthy Completion

- **Auditor**: criteria-based completion verification (shell exit, file exists, grep checks)
- **Block feedback loop**: unmet criteria → block → agent continues → re-verify
- **Block cap**: prevents infinite spend (default 3 blocks per task)
- **StagnationGuard**: detects repeated identical failures, applies step penalties

### 📊 Observability

- **EventBus**: append-only SQLite WAL — full audit trail, state projection
- **Cost Tracker**: per-session, per-agent, per-project cost aggregation
- **TUI Dashboard**: live-updating terminal dashboard (Rich)
- **Apex Dashboard**: web-based Command Center integration

## CLI Reference

```bash
python -m apex.cli deploy -a architect -t "Design DB schema"  # Spawn session
python -m apex.cli status                                      # List sessions
python -m apex.cli status --detail                             # Full details
python -m apex.cli audit <session_id>                          # Verify completion
python -m apex.cli list                                        # Agents + runtimes
python -m apex.cli daemon                                      # Start daemon server
```

## TUI Dashboard

```bash
pip install rich
python -m apex.tui
```

Shows live: active sessions · task pipeline · 7-day cost · running instances. 2-second refresh, Ctrl+C to exit.

## MCP Server (Kiro / Cursor)

```bash
cd ops-mcp && npm start
```

6 tools: `apex_spawn` · `apex_status` · `apex_blackboard` · `apex_claims` · `apex_stop` · `apex_doctor`

## Release

```bash
node scripts/release.mjs patch   # 1.0.0 → 1.0.1
node scripts/release.mjs minor   # 1.0.0 → 1.1.0
node scripts/release.mjs major   # 1.0.0 → 2.0.0
```

Bumps version in pyproject.toml, generates changelog from git log, tags and pushes.

## Invariants (Design Guarantees)

| # | Invariant |
|---|-----------|
| INV-1 | Single SDK boundary — only adapters touch runtime CLIs |
| INV-2 | Name ↔ session_id 1:1 mapping, stable for lifetime |
| INV-3 | Blackboard single-writer: only apexd writes |
| INV-4 | Events append-only; state projected from events |
| INV-5 | All secrets redacted before storage |

## Requirements

- Python 3.12+
- Node.js 18+ (ops-mcp only)
- tmux (Kiro/Claude adapters)
- SQLite (built-in)

## License

MIT — see [LICENSE](LICENSE)

---

📖 **[User Manual →](USAGE.md)**
