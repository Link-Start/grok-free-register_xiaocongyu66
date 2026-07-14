#!/usr/bin/env bash
# 认证服务：Go 协议 SSO→CPA（grok2api sso_build / Chrome TLS 多并发）
#
#   bash auth-service.sh
#   bash auth-service.sh --once --limit 200 --workers 16
#   bash auth-service.sh --sso-file batch.txt --proxy-file 代理.txt
#
set -euo pipefail
cd "$(dirname "$0")"

. scripts/ensure_runtime.sh
ensure_runtime

echo "[*] 认证服务: protocol Go sso_build (SSO→CPA)"
echo "    输出: keys/cpa/xai-*.json（access + refresh）"
# preferred module path; shim grok_register.auth_service_protocol still works
exec .venv/bin/python -m grok_register.sso.auth_service "$@"
