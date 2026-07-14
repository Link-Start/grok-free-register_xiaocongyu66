#!/usr/bin/env bash
# 用 accounts.txt 的 email:password 重登，刷新 keys/sso.txt（email:sso）
# 成功时删除该邮箱旧 SSO 行，写入新行；并刷新 grok.txt / 追加 auth-sessions
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
