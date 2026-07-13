#!/bin/bash
# Sync single-account CPA xai-*.json into CLIProxyAPI auth-dir + refresh expired tokens.
# Do NOT copy accounts.cpa.json (type=cpa-auth-bundle) — CLIProxyAPI expects one auth per file.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
LOG="$ROOT/logs/cpa-sync.log"
mkdir -p "$ROOT/logs"
echo "[*] cpa-sync (python) started $(date -Iseconds)" >>"$LOG"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"
# Prefer module worker: refresh + import + strip bundles
exec python3 -m grok_register.cliproxyapi --worker >>"$LOG" 2>&1
