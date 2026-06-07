<p align="center">
  <h1>AIAgentOps</h1>
</p>

> Cross-runtime multi-agent orchestrator — secure, observable, and runtime-agnostic agent operations for Kiro, Claude Code, Hermes, and beyond.

## What is AIAgentOps?

AIAgentOps is the operational backbone for multi-agent systems. It provides:

- **Runtime Adapters** — single SDK boundary for Kiro, Claude Code, Hermes (INV-1)
- **Security** — fail-closed redaction (INV-5), 3-tier risk gate, role whitelists, dangerous command blocking
- **Auditing** — stagnation guard (anti-loop), completion verification, block ring checks
- **Protocol** — typed, versioned ApexEvent / ApexCommand / TaskSpec (Pydantic)

## Implemented (runnable, tested)

| Module | Description |
|--------|-------------|
| `apex/protocol.py` | Typed events, commands, task specs (Pydantic, versioned) |
| `apex/adapters/base.py` | RuntimeAdapter Protocol — single SDK boundary |
| `apex/security/redactor.py` | Fail-closed sensitive data redactor |
| `apex/core/tool_risk.py` | 3-tier risk gate + role whitelist + alias normalization |
| `apex/core/auditor.py` | StagnationGuard + Auditor (completion verification) |

## Roadmap

| Milestone | Deliverables |
|-----------|-------------|
| M1 | `daemon.py`, `storage/db.py`, `adapters/kiro.py`, `cli.py`, `registry.py`, `event_bus.py` |
| M2 | `blackboard.py`, `claims.py`, userPromptSubmit injection |
| M3 | `ops-mcp/` (TypeScript), ACP upgrade, installer skill |
| M4 | `adapters/claude.py`, `adapters/hermes.py`, cost attribution |
| M5 | Release scripts, TUI |

## Quick Start

```bash
pip install pydantic
python3 tests/unit/test_core.py
```

## License

MIT
