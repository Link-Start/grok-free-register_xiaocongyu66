#!/usr/bin/env bash
# 用 accounts.txt 里的 email:password 重新登录，刷新 SSO
# 写入 accounts.txt / grok.txt / auth-sessions.jsonl
#
#   bash scripts/sso-relogin.sh --limit 10 --workers 2
#   bash scripts/sso-relogin.sh --only-without-cpa --limit 50
#   bash scripts/sso-relogin.sh --limit 20 --convert   # 登录成功后再转 CPA
#
set -euo pipefail
cd "$(dirname "$0")/.."
. scripts/ensure_runtime.sh
ensure_runtime
exec .venv/bin/python -m grok_register.sso.relogin "$@"
