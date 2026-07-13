#!/usr/bin/env bash
# Ensure Python venv + mandatory Go/Rust native binaries before any entrypoint.

ensure_runtime() {
    local lock_dir=".setup.lock"
    local acquired=0
    local attempt
    local current_requirements=""
    local recorded_requirements=""
    local root
    root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    # shellcheck source=polyglot_gate.sh
    source "$root/scripts/polyglot_gate.sh"
    export POLYGLOT_ROOT="$root"

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
        echo "[*] 首次运行，安装依赖 (Python + Go + Rust)..."
        if ! bash setup.sh; then
            rmdir "$lock_dir" 2>/dev/null || true
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

    # Tests / CI can set POLYGLOT_REQUIRED=0 to skip native build + hard gate
    if [ "${POLYGLOT_REQUIRED:-1}" = "0" ]; then
        rmdir "$lock_dir" 2>/dev/null || true
        trap - EXIT INT TERM
        echo "[*] POLYGLOT_REQUIRED=0 · 跳过多语言硬门禁 (仅测试用)"
        return 0
    fi

    # Auto-build native stack if incomplete (hard requirement)
    # polyglot_missing returns 0 when complete, 1 when components are missing
    if polyglot_missing >/dev/null 2>&1; then
        : # stack complete
    else
        echo "[*] 多语言栈不完整，正在编译 Go + Rust 原生组件..."
        if ! bash "$root/scripts/build-native.sh"; then
            rmdir "$lock_dir" 2>/dev/null || true
            trap - EXIT INT TERM
            require_polyglot_stack || true
            return 1
        fi
    fi

    rmdir "$lock_dir" 2>/dev/null || true
    trap - EXIT INT TERM

    # Hard gate: refuse to continue without full stack
    if ! require_polyglot_stack; then
        return 1
    fi
    return 0
}
