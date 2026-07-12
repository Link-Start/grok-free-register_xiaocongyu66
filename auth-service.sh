#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

. scripts/ensure_runtime.sh
ensure_runtime

exec .venv/bin/python -m xai_enroller.service "$@"
