#!/usr/bin/env bash

ensure_runtime() {
    local lock_dir=".setup.lock"
    local acquired=0
    local attempt
    local current_requirements=""
    local recorded_requirements=""

    for attempt in {1..300}; do
        if mkdir "$lock_dir" 2>/dev/null; then
            acquired=1
            break
        fi
        sleep 0.2
    done
    if [ "$acquired" -ne 1 ]; then
        echo "[!] 另一个安装进程长时间未结束，请稍后重试。" >&2
        return 1
    fi
    trap 'rmdir .setup.lock 2>/dev/null || true' EXIT INT TERM

    if [ ! -d .venv ]; then
        echo "[*] 首次运行，安装依赖..."
        if ! bash setup.sh; then
            rmdir "$lock_dir"
            trap - EXIT INT TERM
            return 1
        fi
    elif [ -f requirements.txt ]; then
        if command -v sha256sum >/dev/null 2>&1; then
            current_requirements="$(sha256sum requirements.txt | awk '{print $1}')"
        elif command -v shasum >/dev/null 2>&1; then
            current_requirements="$(shasum -a 256 requirements.txt | awk '{print $1}')"
        fi
        recorded_requirements="$(cat .venv/.requirements.sha256 2>/dev/null || true)"
        if [ -n "$current_requirements" ] && [ "$current_requirements" != "$recorded_requirements" ]; then
            echo "[*] requirements.txt 已更新，安装/更新 Python 依赖..."
            .venv/bin/pip install -q -r requirements.txt
            printf '%s\n' "$current_requirements" > .venv/.requirements.sha256
        fi
    fi

    rmdir "$lock_dir"
    trap - EXIT INT TERM
}
