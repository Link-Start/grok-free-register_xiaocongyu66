#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "[*] 首次运行，安装依赖..."
    bash setup.sh
fi

exec .venv/bin/python -m xai_enroller.service "$@"
