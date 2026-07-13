#!/bin/sh
# HF Space / Docker entrypoint for grok-free-register
set -eu

cd /app

# HF Space injects PORT (default 7860). Dashboard follows it.
export PORT="${PORT:-7860}"
export HOST="${HOST:-0.0.0.0}"
export DASHBOARD_PORT="${PORT}"
export KEY_EXPORT_DIR="${KEY_EXPORT_DIR:-/data/keys}"
export PROJECT_ROOT="/app"
export PYTHONPATH="/app${PYTHONPATH:+:$PYTHONPATH}"
export SOLVER_PYTHON="${SOLVER_PYTHON:-python}"
export TURNSTILE_API_URL="${TURNSTILE_API_URL:-http://127.0.0.1:5080}"
export TURNSTILE_SOLVER="${TURNSTILE_SOLVER:-hybrid}"
export TURNSTILE_SOLVER_ON_DEMAND="${TURNSTILE_SOLVER_ON_DEMAND:-1}"
export TURNSTILE_SOLVER_HEADLESS="${TURNSTILE_SOLVER_HEADLESS:-1}"
# 新版 hybrid：默认 auto（按 CPU/内存定 worker；请求进队列）
export SOLVER_GATEWAY_WORKERS="${SOLVER_GATEWAY_WORKERS:-auto}"
export SOLVER_GATEWAY_WORKERS_MAX="${SOLVER_GATEWAY_WORKERS_MAX:-8}"
export SOLVER_WORKER_CONCURRENCY="${SOLVER_WORKER_CONCURRENCY:-0}"
export REGISTER_ENGINE="${REGISTER_ENGINE:-protocol}"
export CONTROL_PLANE_ALLOW_ACTIONS="${CONTROL_PLANE_ALLOW_ACTIONS:-1}"
# HF: start protocol register with the panel (no UI click). Set AUTO_START_REGISTER=0 to disable.
export AUTO_START_REGISTER="${AUTO_START_REGISTER:-1}"
export AUTO_RESTART_REGISTER="${AUTO_RESTART_REGISTER:-1}"
export AUTO_START_DELAY_SEC="${AUTO_START_DELAY_SEC:-3}"
# Panel auth: set DASHBOARD_PASSWORD (and optional DASHBOARD_USER) via Space Secrets
export DASHBOARD_USER="${DASHBOARD_USER:-${CONTROL_PLANE_USER:-admin}}"
export DASHBOARD_PASSWORD="${DASHBOARD_PASSWORD:-${CONTROL_PLANE_PASSWORD:-${PANEL_PASSWORD:-}}}"
export CONTROL_PLANE_TOKEN="${CONTROL_PLANE_TOKEN:-${DASHBOARD_TOKEN:-${PANEL_TOKEN:-}}}"

mkdir -p /data/keys /data/logs /app/logs

# Seed .env from example if missing (secrets should come from Space Secrets / -e)
if [ ! -f /app/.env ]; then
  if [ -f /app/.env.example ]; then
    cp /app/.env.example /app/.env
  else
    touch /app/.env
  fi
fi

# Append runtime defaults without clobbering existing keys
grep -q '^REGISTER_ENGINE=' /app/.env 2>/dev/null || echo "REGISTER_ENGINE=${REGISTER_ENGINE}" >> /app/.env
grep -q '^TURNSTILE_SOLVER=' /app/.env 2>/dev/null || echo "TURNSTILE_SOLVER=${TURNSTILE_SOLVER}" >> /app/.env
grep -q '^TURNSTILE_SOLVER_ON_DEMAND=' /app/.env 2>/dev/null || echo "TURNSTILE_SOLVER_ON_DEMAND=${TURNSTILE_SOLVER_ON_DEMAND}" >> /app/.env
grep -q '^TURNSTILE_API_URL=' /app/.env 2>/dev/null || echo "TURNSTILE_API_URL=${TURNSTILE_API_URL}" >> /app/.env
grep -q '^SOLVER_GATEWAY_WORKERS=' /app/.env 2>/dev/null || echo "SOLVER_GATEWAY_WORKERS=${SOLVER_GATEWAY_WORKERS}" >> /app/.env
grep -q '^SOLVER_GATEWAY_WORKERS_MAX=' /app/.env 2>/dev/null || echo "SOLVER_GATEWAY_WORKERS_MAX=${SOLVER_GATEWAY_WORKERS_MAX}" >> /app/.env
grep -q '^SOLVER_WORKER_CONCURRENCY=' /app/.env 2>/dev/null || echo "SOLVER_WORKER_CONCURRENCY=${SOLVER_WORKER_CONCURRENCY}" >> /app/.env
grep -q '^KEY_EXPORT_DIR=' /app/.env 2>/dev/null || echo "KEY_EXPORT_DIR=${KEY_EXPORT_DIR}" >> /app/.env
grep -q '^CONTROL_PLANE_ALLOW_ACTIONS=' /app/.env 2>/dev/null || echo "CONTROL_PLANE_ALLOW_ACTIONS=${CONTROL_PLANE_ALLOW_ACTIONS}" >> /app/.env
grep -q '^AUTO_START_REGISTER=' /app/.env 2>/dev/null || echo "AUTO_START_REGISTER=${AUTO_START_REGISTER}" >> /app/.env
grep -q '^AUTO_RESTART_REGISTER=' /app/.env 2>/dev/null || echo "AUTO_RESTART_REGISTER=${AUTO_RESTART_REGISTER}" >> /app/.env

# Public URL: SPACE_ID=owner/name → https://owner-name.hf.space
# Prefer explicit SPACE_HOST / DASHBOARD_PUBLIC_URL when set by the platform.
# Export SPACE_HOST so Python dashboard logs the same clickable URL.
if [ -z "${SPACE_HOST:-}" ] && [ -n "${SPACE_ID:-}" ]; then
  # Murasame52/open-webui → Murasame52-open-webui
  SPACE_HOST=$(echo "${SPACE_ID}" | tr '/' '-')
  export SPACE_HOST
fi
if [ -n "${DASHBOARD_PUBLIC_URL:-}${PUBLIC_URL:-}${SPACE_URL:-}" ]; then
  _pub="${DASHBOARD_PUBLIC_URL:-${PUBLIC_URL:-${SPACE_URL}}}"
  case "$_pub" in
    http://*|https://*) echo "✅ Public dashboard: ${_pub%/}/  (bind ${HOST}:${PORT})" ;;
    *) echo "✅ Public dashboard: https://${_pub%/}/  (bind ${HOST}:${PORT})" ;;
  esac
elif [ -n "${SPACE_HOST:-}" ]; then
  case "${SPACE_HOST}" in
    *.hf.space) echo "✅ HF Space: https://${SPACE_HOST}/  (bind ${HOST}:${PORT})" ;;
    *) echo "✅ HF Space: https://${SPACE_HOST}.hf.space/  (bind ${HOST}:${PORT})" ;;
  esac
else
  echo "✅ Dashboard will bind ${HOST}:${PORT}"
fi

# Sanity: native binaries
for b in \
  /app/native/solver-gateway/solver-gateway \
  /app/native/register-worker/register-worker \
  /app/native/inventory-worker/inventory-worker
do
  if [ -x "$b" ]; then
    echo "  native ok: $b"
  else
    echo "  ⚠️ missing or not executable: $b (protocol may still run via Python)"
  fi
done

# Prefer CapSolver if key present (no browser pressure)
if [ -n "${CAPSOLVER_API_KEY:-}${CAPSOLVER_KEY:-}" ]; then
  echo "✅ CapSolver key detected — protocol path can skip heavy browser when solver API used"
fi

if [ -n "${DASHBOARD_PASSWORD}" ] || [ -n "${CONTROL_PLANE_TOKEN}" ]; then
  echo "🔒 Panel auth enabled (DASHBOARD_PASSWORD / CONTROL_PLANE_TOKEN)"
else
  echo "⚠️  Panel auth OFF — set DASHBOARD_PASSWORD secret for public Spaces"
fi

if [ "${AUTO_START_REGISTER}" = "1" ] || [ "${AUTO_START_REGISTER}" = "true" ]; then
  echo "▶ AUTO_START_REGISTER=1 — protocol register will launch ~${AUTO_START_DELAY_SEC}s after panel is up"
  echo "  (set AUTO_START_REGISTER=0 to only use the UI Start button)"
fi

echo "🚀 Starting grok-free-register dashboard..."
# Dashboard is the control plane; register auto-starts when AUTO_START_REGISTER=1
exec python -m grok_register.dashboard --host "${HOST}" --port "${PORT}"
