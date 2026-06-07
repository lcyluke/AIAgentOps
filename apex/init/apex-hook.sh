#!/usr/bin/env bash
# APEX hook shim — wraps Kiro hook STDIN JSON into ApexEvents, talks to apexd
# over the unix socket. Usage: apex-hook.sh <verb> <agent>
set -euo pipefail
VERB="${1:?verb required}"; AGENT="${2:?agent required}"
SOCK="${APEX_SOCK:-$HOME/.apex/apexd.sock}"
PAYLOAD="$(cat)"
RESP="$(printf '%s' "{\"verb\":\"$VERB\",\"agent\":\"$AGENT\",\"hook\":$PAYLOAD}" | nc -U "$SOCK" 2>/dev/null || true)"
CODE="$(printf '%s' "$RESP" | head -n1)"
BODY="$(printf '%s' "$RESP" | tail -n +2)"
[ -n "$BODY" ] && printf '%s' "$BODY"
exit "${CODE:-0}"
