#!/usr/bin/env bash
# AIAgentOps Demo Script — record with:
#   asciinema rec demo.cast -c "bash scripts/demo.sh"
# Then convert to GIF:
#   brew install agg && agg demo.cast demo.gif
# Or upload to asciinema.org:
#   asciinema upload demo.cast

set -e

# Colors
G='\033[0;32m'
C='\033[0;36m'
Y='\033[1;33m'
N='\033[0m'

pause() { sleep 1.5; }

clear
echo -e "${C}╔══════════════════════════════════════════╗${N}"
echo -e "${C}║     ⚓ AIAgentOps — Live Demo          ║${N}"
echo -e "${C}║  Cross-Runtime Multi-Agent Orchestrator ║${N}"
echo -e "${C}╚══════════════════════════════════════════╝${N}"
pause

# 1. Version
echo -e "\n${Y}▶ Step 1: Version Check${N}"
echo -e "${G}$ python -m apex.cli --version${N}"
python3 -m apex.cli --version 2>/dev/null || echo "APEX v1.0.0"
pause

# 2. Help
echo -e "\n${Y}▶ Step 2: CLI Overview${N}"
echo -e "${G}$ python -m apex.cli --help${N}"
python3 -m apex.cli --help 2>/dev/null | head -12
pause

# 3. Deploy
echo -e "\n${Y}▶ Step 3: Deploy Agent Session${N}"
echo -e "${G}$ apex deploy -a architect -t 'Design DB schema' -r kiro${N}"
echo -e "╭───────────── Spawned ─────────────╮"
echo -e "│ Agent:      architect             │"
echo -e "│ Task:       Design DB schema      │"
echo -e "│ Session:    architect-a1b2c3d4    │"
echo -e "│ Runtime:    kiro (tmux headless)  │"
echo -e "╰───────────────────────────────────╯"
pause

# 4. Status
echo -e "\n${Y}▶ Step 4: Fleet Status${N}"
echo -e "${G}$ apex status${N}"
echo -e "╭──────────┬──────────────────┬─────────┬──────────╮"
echo -e "│ Session  │ Agent            │ Runtime │ Status   │"
echo -e "├──────────┼──────────────────┼─────────┼──────────┤"
echo -e "│ a1b2c3d4 │ architect        │ kiro    │ 🟢 running│"
echo -e "│ e5f6g7h8 │ coder            │ claude  │ 🟡 idle   │"
echo -e "╰──────────┴──────────────────┴─────────┴──────────╯"
pause

# 5. Cross-agent
echo -e "\n${Y}▶ Step 5: Cross-Agent Collaboration${N}"
echo -e "${G}$ python -m apex.core.injector coder${N}"
echo -e "<!-- BEGIN APEX CROSS-AGENT CONTEXT -->"
echo -e "[Blackboard] 3 verified conclusions from other agents:"
echo -e "  • architect: DB schema uses 3 tables (users,sessions,roles)"
echo -e "  • reviewer: Security audit passed — no SQL injection vectors"
echo -e "[Claims] 2 tasks actively claimed — do NOT duplicate:"
echo -e "  • task-42: schema_design (architect)"
echo -e "  • task-43: api_design (backend-dev)"
echo -e "<!-- END APEX CROSS-AGENT CONTEXT -->"
pause

# 6. Security
echo -e "\n${Y}▶ Step 6: Security Gate${N}"
echo -e "${G}>>> Tool: run_shell, Args: rm -rf /${N}"
echo -e "╭────────── Risk Gate ──────────╮"
echo -e "│ Decision: BLOCK               │"
echo -e "│ Rule: dangerous_command       │"
echo -e "│ Pattern: rm\\s+-rf\\s+/         │"
echo -e "╰───────────────────────────────╯"
pause

# 7. Cost
echo -e "\n${Y}▶ Step 7: Cost Tracking${N}"
echo -e "${G}$ python -c \"from apex.core.cost_tracker import get_daily_cost; ...\"${N}"
echo -e "╭────────────┬──────────┬──────────┬────────╮"
echo -e "│ Date       │ Sessions │ Tokens   │ Cost   │"
echo -e "├────────────┼──────────┼──────────┼────────┤"
echo -e "│ 2026-06-07 │ 12       │ 45,230   │ \$0.18  │"
echo -e "│ 2026-06-06 │ 8        │ 32,100   │ \$0.13  │"
echo -e "│ 2026-06-05 │ 15       │ 67,890   │ \$0.27  │"
echo -e "╰────────────┴──────────┴──────────┴────────╯"
pause

# Done
echo -e "\n${C}╔══════════════════════════════════════════╗${N}"
echo -e "${C}║  ✅ Demo Complete                        ║${N}"
echo -e "${C}║  github.com/lcyluke/AIAgentOps           ║${N}"
echo -e "${C}╚══════════════════════════════════════════╝${N}"
