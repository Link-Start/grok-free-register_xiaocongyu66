#!/usr/bin/env bash
# Hard requirement: Python + Go native stack (inventory + hybrid gateway)
# + Rust watchdog + C++ util for old hybrid Turnstile.
# Sourced by ensure_runtime / start / setup. Exit non-zero blocks startup.

polyglot_root() {
    if [ -n "${POLYGLOT_ROOT:-}" ]; then
        printf '%s\n' "$POLYGLOT_ROOT"
        return
    fi
    # scripts/ -> project root
    cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd
}

polyglot_paths() {
    local root
    root="$(polyglot_root)"
    POLYGLOT_PY="${POLYGLOT_PY:-$root/.venv/bin/python}"
    POLYGLOT_GO_PROXY="${POLYGLOT_GO_PROXY:-$root/native/proxy-worker/proxy-worker}"
    POLYGLOT_GO_REGISTER="${POLYGLOT_GO_REGISTER:-$root/native/register-worker/register-worker}"
    # Old hybrid: Go gateway is primary
    POLYGLOT_GO_SOLVER="${POLYGLOT_GO_SOLVER:-$root/native/solver-gateway/solver-gateway}"
    POLYGLOT_GO_INVENTORY="${POLYGLOT_GO_INVENTORY:-$root/native/inventory-worker/inventory-worker}"
    # back-compat alias
    POLYGLOT_RUST_INVENTORY="${POLYGLOT_RUST_INVENTORY:-$POLYGLOT_GO_INVENTORY}"
    POLYGLOT_RUST_WATCHDOG="${POLYGLOT_RUST_WATCHDOG:-$root/native/solver-watchdog/solver-watchdog}"
    POLYGLOT_CPP_UTIL="${POLYGLOT_CPP_UTIL:-$root/native/solver-util/solver-util}"
    POLYGLOT_PY_SOLVER_WORKER="${POLYGLOT_PY_SOLVER_WORKER:-$root/native/solver-hybrid/browser_worker.py}"
    if [ ! -x "$POLYGLOT_GO_INVENTORY" ] && [ -x "$root/native/inventory-worker/target/release/inventory-worker" ]; then
        # legacy rust binary still accepted if present
        POLYGLOT_GO_INVENTORY="$root/native/inventory-worker/target/release/inventory-worker"
        POLYGLOT_RUST_INVENTORY="$POLYGLOT_GO_INVENTORY"
    fi
    if [ ! -x "$POLYGLOT_RUST_WATCHDOG" ] && [ -x "$root/native/solver-watchdog/target/release/solver-watchdog" ]; then
        POLYGLOT_RUST_WATCHDOG="$root/native/solver-watchdog/target/release/solver-watchdog"
    fi
}

polyglot_missing() {
    polyglot_paths
    local miss=()
    if [ ! -x "$POLYGLOT_PY" ]; then
        miss+=("python:.venv/bin/python")
    fi
    if [ ! -x "$POLYGLOT_GO_PROXY" ]; then
        miss+=("go:native/proxy-worker/proxy-worker")
    fi
    if [ ! -x "$POLYGLOT_GO_REGISTER" ]; then
        miss+=("go:native/register-worker/register-worker")
    fi
    if [ ! -x "$POLYGLOT_GO_INVENTORY" ]; then
        miss+=("go:native/inventory-worker/inventory-worker")
    fi
    # Hybrid Turnstile: Go gateway + Rust watchdog + C++ util + Python browser
    if [ "${POLYGLOT_REQUIRE_HYBRID:-1}" = "1" ]; then
        if [ ! -x "$POLYGLOT_GO_SOLVER" ]; then
            miss+=("go:native/solver-gateway/solver-gateway")
        fi
        if [ ! -x "$POLYGLOT_RUST_WATCHDOG" ]; then
            miss+=("rust:native/solver-watchdog/solver-watchdog")
        fi
        if [ ! -x "$POLYGLOT_CPP_UTIL" ]; then
            miss+=("cpp:native/solver-util/solver-util")
        fi
        if [ ! -f "$POLYGLOT_PY_SOLVER_WORKER" ]; then
            miss+=("python:native/solver-hybrid/browser_worker.py")
        fi
    fi
    if [ "${#miss[@]}" -gt 0 ]; then
        printf '%s\n' "${miss[@]}"
        return 1
    fi
    return 0
}

# Print human report; return 0 only when complete.
require_polyglot_stack() {
    local root miss line
    root="$(polyglot_root)"
    polyglot_paths

    if miss="$(polyglot_missing)"; then
        if [ "${POLYGLOT_QUIET:-0}" != "1" ]; then
            echo "[✓] 多语言栈就绪 · Python + Go hybrid + Rust + C++"
            echo "    Python  $($POLYGLOT_PY -c 'import sys; print(sys.version.split()[0])' 2>/dev/null || echo ok)"
            echo "    Go      proxy + register + inventory + solver-gateway"
            echo "    Rust    solver-watchdog"
            echo "    C++     solver-util"
            echo "    Hybrid  browser_worker.py (Python Chromium)"
        fi
        return 0
    fi

    echo "[✗] 硬性条件未满足：本项目必须启用 Python + Go inventory + 旧版 hybrid。" >&2
    echo "    缺少组件：" >&2
    while IFS= read -r line; do
        [ -n "$line" ] && echo "      - $line" >&2
    done <<< "$miss"
    echo "" >&2
    echo "    修复：" >&2
    echo "      1) 安装: python3 + go + cargo/rustc + g++" >&2
    echo "      2) bash setup.sh          # 或 bash scripts/build-native.sh" >&2
    echo "      3) 确认二进制可执行后重试 bash start.sh" >&2
    echo "" >&2
    echo "    期望路径 (相对 $root):" >&2
    echo "      .venv/bin/python" >&2
    echo "      native/proxy-worker/proxy-worker" >&2
    echo "      native/register-worker/register-worker" >&2
    echo "      native/inventory-worker/inventory-worker" >&2
    echo "      native/solver-gateway/solver-gateway" >&2
    echo "      native/solver-watchdog/solver-watchdog" >&2
    echo "      native/solver-util/solver-util" >&2
    echo "      native/solver-hybrid/browser_worker.py" >&2
    return 1
}

# For Python import-time gate
polyglot_status_json() {
    polyglot_paths
    local py_ok=false go_proxy_ok=false go_reg_ok=false inv_ok=false
    local go_solver_ok=false rust_wd_ok=false cpp_ok=false py_worker_ok=false
    [ -x "$POLYGLOT_PY" ] && py_ok=true
    [ -x "$POLYGLOT_GO_PROXY" ] && go_proxy_ok=true
    [ -x "$POLYGLOT_GO_REGISTER" ] && go_reg_ok=true
    [ -x "$POLYGLOT_GO_INVENTORY" ] && inv_ok=true
    [ -x "$POLYGLOT_GO_SOLVER" ] && go_solver_ok=true
    [ -x "$POLYGLOT_RUST_WATCHDOG" ] && rust_wd_ok=true
    [ -x "$POLYGLOT_CPP_UTIL" ] && cpp_ok=true
    [ -f "$POLYGLOT_PY_SOLVER_WORKER" ] && py_worker_ok=true
    local core_ok=false hybrid_ok=false
    [ "$py_ok" = true ] && [ "$go_proxy_ok" = true ] && [ "$go_reg_ok" = true ] && [ "$inv_ok" = true ] && core_ok=true
    # hybrid: Go gateway + Rust watchdog + C++ util + Python browser worker
    if [ "$go_solver_ok" = true ] && [ "$rust_wd_ok" = true ] && [ "$cpp_ok" = true ] && [ "$py_worker_ok" = true ]; then
        hybrid_ok=true
    fi
    local ok=false
    if [ "$core_ok" = true ]; then
        if [ "${POLYGLOT_REQUIRE_HYBRID:-1}" = "1" ]; then
            [ "$hybrid_ok" = true ] && ok=true
        else
            ok=true
        fi
    fi
    printf '{"python":%s,"go_proxy_worker":%s,"go_register_worker":%s,"go_inventory_worker":%s,"rust_inventory_worker":%s,"go_solver_gateway":%s,"rust_solver_watchdog":%s,"cpp_solver_util":%s,"py_browser_worker":%s,"hybrid_ok":%s,"ok":%s}\n' \
        "$py_ok" "$go_proxy_ok" "$go_reg_ok" "$inv_ok" "$inv_ok" \
        "$go_solver_ok" "$rust_wd_ok" "$cpp_ok" "$py_worker_ok" "$hybrid_ok" "$ok"
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    case "${1:-check}" in
        check|require)
            require_polyglot_stack
            ;;
        json|status)
            polyglot_status_json
            ;;
        paths)
            polyglot_paths
            echo "PY=$POLYGLOT_PY"
            echo "GO_PROXY=$POLYGLOT_GO_PROXY"
            echo "GO_REGISTER=$POLYGLOT_GO_REGISTER"
            echo "GO_INVENTORY=$POLYGLOT_GO_INVENTORY"
            echo "GO_SOLVER=$POLYGLOT_GO_SOLVER"
            echo "RUST_WATCHDOG=$POLYGLOT_RUST_WATCHDOG"
            echo "CPP_UTIL=$POLYGLOT_CPP_UTIL"
            ;;
        *)
            echo "usage: polyglot_gate.sh [check|json|paths]" >&2
            exit 2
            ;;
    esac
fi
