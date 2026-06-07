# AIAgentOps — User Manual

Practical workflows, examples, and debugging for day-to-day multi-agent operations.

## Table of Contents

1. [Getting Started](#getting-started)
2. [Spawning Agent Sessions](#spawning-agent-sessions)
3. [Monitoring & Status](#monitoring--status)
4. [Cross-Agent Collaboration](#cross-agent-collaboration)
5. [Security & Risk Gates](#security--risk-gates)
6. [Cost Tracking](#cost-tracking)
7. [Troubleshooting](#troubleshooting)
8. [Adapter Setup](#adapter-setup)

---

## Getting Started

### Prerequisites

```bash
# Python 3.12+
python3 --version

# Install core deps
pip install typer rich

# Verify installation
python -m apex.cli --version
# → APEX v1.0.0
```

### Directory Layout

```
~/.apex/
├── agentops.db        # SQLite database (sessions, events, tasks)
├── agentops.sock      # Daemon Unix socket
├── claude-sessions/   # Claude Code workdirs
└── config.yaml        # Runtime configuration
```

### Start the Daemon

```bash
# In one terminal:
python -m apex.cli daemon
# → APEX daemon listening on ~/.apex/agentops.sock

# In another terminal:
python -m apex.cli status
```

---

## Spawning Agent Sessions

### Basic Spawn

```bash
# Deploy an architect agent to a task
python -m apex.cli deploy \
  -a architect \
  -t "Design database schema for user service" \
  -d ~/projects/myapp

# Output:
# ╭───────────── Spawned ─────────────╮
# │ Agent:      architect             │
# │ Task:       Design database...    │
# │ Session:    architect-d4e8f2a1    │
# │ Runtime:    kiro                  │
# ╰───────────────────────────────────╯
```

### Spawn with Different Runtimes

```bash
# Kiro (headless tmux)
python -m apex.cli deploy -a coder -t "Fix login bug" -r kiro

# Claude Code
python -m apex.cli deploy -a architect -t "Review PR #42" -r claude

# Hermes
python -m apex.cli deploy -a ops-engineer -t "Health check" -r hermes

# ACP (Kiro advanced protocol)
# Uses ACP JSON-RPC if kiro --acp is available, falls back to tmux
```

### Manual Session Management

```bash
# Attach to a tmux session (Kiro/Claude adapters)
tmux attach -t apex-architect-d4e8f2a1

# Send a prompt to a running session
python -m apex.cli prompt architect-d4e8f2a1 "Also add indexing for email column"

# Stop a session
python -m apex.cli stop architect-d4e8f2a1
```

---

## Monitoring & Status

### CLI Status

```bash
# List all active sessions
python -m apex.cli status

# Detailed view
python -m apex.cli status --detail

# Filter by runtime
python -m apex.cli status -r kiro
```

### TUI Dashboard

```bash
pip install rich
python -m apex.tui
```

The TUI shows four panels refreshing every 2 seconds:

```
┌─ ⚓ APEX Fleet TUI ──────────────────────────┐
│                    │                          │
│  📡 Sessions       │  📋 Tasks                │
│  (active list)     │  (pipeline with status)  │
│                    │                          │
│  💵 Cost & Resources│                         │
│  (7-day, instances)│                          │
└────────────────────┴──────────────────────────┘
```

### Web Dashboard

The [Apex Command Center](http://localhost:8080) provides a full web dashboard with 14 views including Project War Room, AI Fleet, Cost Center, and GPU Resources. Select "AIAgentOps" from the project dropdown to see all modules and tasks.

---

## Cross-Agent Collaboration

### Shared Blackboard

The blackboard prevents information silos between agents:

```python
from apex.core.blackboard import Blackboard

bb = Blackboard()

# Agent A completes a task, writes conclusion
bb.write(
    conclusion="User service DB schema: 3 tables (users, sessions, roles). "
               "Indexed on email, session_token. No partitions needed at <1M users.",
    author="architect-d4e8f2a1",
    verified=True,
    session_id="architect-d4e8f2a1",
)

# Agent B starts — injector feeds this into context
digest = bb.digest(exclude_author="coder-b7e3c9d2")
# → "Other agents have confirmed: User service uses 3 tables..."
```

### Task Claims (Prevent Duplicate Work)

```python
from apex.core.claims import ClaimsRegistry

claims = ClaimsRegistry()

# Agent tries to claim a task
result = claims.claim("task-42", "architect", {"kind": "schema_design"})
if result["success"]:
    print("Task claimed — proceed")
else:
    print(f"Already claimed by: {result['holder']}")
    # → Don't duplicate work
```

### Context Injection

The injector feeds blackboard digest + active claims into each agent's context:

```bash
# Manual injection for debugging
python -m apex.core.injector architect
# → <!-- BEGIN APEX CROSS-AGENT CONTEXT -->
#   [Blackboard] 3 verified conclusions available...
#   [Claims] 2 tasks currently claimed, do NOT duplicate...
# → <!-- END APEX CROSS-AGENT CONTEXT -->
```

---

## Security & Risk Gates

### Tool Risk Evaluation

```python
from apex.core.tool_risk import evaluate

# Read-only tool → auto-allow
decision, rule = evaluate("read_file", "architect", {}, False)
# → ("allow", "read_only")

# Destructive tool → requires approval
decision, rule = evaluate("run_shell", "coder", {"command": "rm file.txt"}, False)
# → ("ask", "destructive_requires_approval")

# Dangerous command → hard block
decision, rule = evaluate("run_shell", "coder",
    {"command": "rm -rf /"}, False)
# → ("block", "dangerous_command:rm\s+-rf\s+/")
```

### Role Whitelists

```python
from apex.core.tool_risk import normalize_role, tool_allowed_for_role

# Architect can read but not execute shells
assert tool_allowed_for_role("read_file", "architect")    # True
assert not tool_allowed_for_role("run_shell", "architect") # False

# Role aliases are normalized
assert normalize_role("backend developer") == "backend-dev"
```

### Secret Redaction

```python
from apex.security.redactor import redact

# API keys masked with auditable fingerprints
redact("key=sk-ABC123...XYZ")
# → "key=REDACTED:openai_key:e4d909c2"

# Same secret → same fingerprint (trackable without exposing)
a = redact("sk-AAA")
b = redact("sk-AAA")
assert a == b  # Same fingerprint
```

---

## Cost Tracking

```python
from apex.core.cost_tracker import (
    get_session_cost, get_project_cost, get_agent_cost, get_daily_cost
)

# Per-session cost
session = get_session_cost("architect-d4e8f2a1")
print(f"Tokens: {session['input_tokens']} in / {session['output_tokens']} out")
print(f"Cost: ${session['estimated_cost_usd']:.4f}")

# Per-project (last 30 days)
project = get_project_cost("AIAgentOps", days=30)
print(f"Total: ${project['total_cost']:.2f} across {project['session_count']} sessions")

# Per-agent
agent = get_agent_cost("architect", days=30)

# Daily breakdown (last 7 days)
daily = get_daily_cost(days=7)
for d in daily:
    print(f"{d['date']}: ${d['total_cost_usd']:.4f}")
```

### Pricing Reference

| Model | Input ($/1M tokens) | Output ($/1M tokens) |
|-------|---------------------|----------------------|
| DeepSeek V4 Pro | $1.00 | $4.00 |
| Claude Sonnet | $3.00 | $15.00 |
| GPT-4o | $2.50 | $10.00 |

---

## Troubleshooting

### Daemon won't start

```bash
# Check if socket file is stale
ls -la ~/.apex/agentops.sock
rm ~/.apex/agentops.sock  # Remove stale socket

# Check port conflicts
lsof -i :8765
```

### Sessions not showing

```bash
# Verify database
sqlite3 ~/.apex/agentops.db "SELECT session_id, name, status FROM sessions;"

# Check tmux sessions
tmux list-sessions | grep apex-
```

### Adapter failures

```bash
# Kiro: check tmux + kiro binary
which kiro && which tmux

# Claude: check Claude Code installation
which claude && claude --version

# Hermes: check gateway health
curl http://localhost:8765/health
```

### Stagnation guard blocking

If an agent is repeatedly failing on the same tool:

```python
from apex.core.stagnation import StagnationGuard

g = StagnationGuard(repeat_threshold=3)  # Increase threshold
# Or reset:
g.reset()
```

### Auditor blocking too aggressively

```python
from apex.core.auditor import Auditor

a = Auditor(block_cap=5)  # Increase from default 3
```

---

## Adapter Setup

### Kiro (tmux headless)

```bash
# Requirements
brew install tmux
# Kiro CLI must be in PATH

# Verify
kiro --version
```

### Claude Code

```bash
# Install Claude Code
npm install -g @anthropic-ai/claude-code

# Verify
claude --version
```

### Hermes

```bash
# Hermes must be installed and configured
hermes --version

# Gateway must be running for REST API
hermes gateway start
```

### ACP (Kiro Advanced Protocol)

```bash
# ACP mode requires kiro >= 0.12 with --acp --stdio support
kiro --acp --stdio
```

---

## MCP Server (IDE Integration)

### Setup for Kiro

```json
// ~/.kiro/mcp.json
{
  "mcpServers": {
    "apex-ops": {
      "command": "node",
      "args": ["~/AIAgentOps/ops-mcp/dist/index.js"]
    }
  }
}
```

### Setup for Cursor

```json
// .cursor/mcp.json
{
  "mcpServers": {
    "apex-ops": {
      "command": "node",
      "args": ["dist/index.js"]
    }
  }
}
```

### Available Tools

| Tool | Description |
|------|-------------|
| `apex_spawn` | Spawn agent session (agent, task, cwd) |
| `apex_status` | Query session status |
| `apex_blackboard` | Search shared knowledge |
| `apex_claims` | List active task claims |
| `apex_stop` | Stop a session |
| `apex_doctor` | Health check |

---

## Workflow Examples

### Example 1: Bug Fix Pipeline

```bash
# 1. Architect designs fix
python -m apex.cli deploy -a architect -t "Design fix for login timeout"

# 2. Coder implements
python -m apex.cli deploy -a coder -t "Implement login timeout fix"

# 3. Injector feeds architect's design into coder's context automatically
# 4. Auditor verifies: tests pass, file changed
# 5. Both sessions complete → blackboard has verified conclusions
```

### Example 2: Multi-Agent Research

```bash
# Spawn 3 agents in parallel
python -m apex.cli deploy -a architect -t "Research DB options" &
python -m apex.cli deploy -a coder -t "Research API frameworks" &
python -m apex.cli deploy -a reviewer -t "Research security patterns" &

# Monitor all
python -m apex.tui

# Each agent writes to blackboard → others see conclusions
# Claims prevent duplicate research topics
```

### Example 3: Cost Monitoring

```bash
# Check today's spend
python -c "
from apex.core.cost_tracker import get_daily_cost
d = get_daily_cost(days=1)
print(f'Today: \${d[0][\"total_cost_usd\"]:.4f}' if d else 'No data')
"

# Weekly report
python -c "
from apex.core.cost_tracker import get_daily_cost
for d in get_daily_cost(days=7):
    print(f'{d[\"date\"]}: \${d[\"total_cost_usd\"]:.4f}')
"
```
