#!/usr/bin/env bash
# Build mandatory native stack: Go (proxy + register) + Rust (inventory).
# Failure is fatal — project requires Python + Go + Rust.
set -euo pipefail
cd "$(dirname "$0")/.."

ROOT="$(pwd)"
FAIL=0

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[✗] 缺少命令: $1" >&2
    FAIL=1
    return 1
  fi
  return 0
}

echo "=== 构建原生组件 (Go + Rust + C++) ==="

need_cmd go || true
need_cmd cargo || true
need_cmd rustc || true
need_cmd g++ || true

if [ "$FAIL" -ne 0 ]; then
  echo "[✗] 请先安装 Go / Rust / g++ 工具链后再构建。" >&2
  echo "    Go:   https://go.dev/dl/   或 apt install golang-go" >&2
  echo "    Rust: https://rustup.rs/   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh" >&2
  echo "    C++:  apt install g++" >&2
  exit 1
fi

export GOSUMDB="${GOSUMDB:-off}"
if [ -z "${GOPROXY:-}" ]; then
  if [ "${GO_OFFLINE:-0}" = "1" ]; then
    export GOPROXY=off
  else
    export GOPROXY="https://proxy.golang.org,direct"
  fi
fi

echo "[1/6] Go proxy-worker ..."
(
  cd native/proxy-worker
  go build -trimpath -ldflags="-s -w" -o proxy-worker .
)
test -x native/proxy-worker/proxy-worker
echo "    [✓] native/proxy-worker/proxy-worker"

echo "[2/6] Go register-worker ..."
(
  cd native/register-worker
  go build -trimpath -ldflags="-s -w" -o register-worker .
)
test -x native/register-worker/register-worker
echo "    [✓] native/register-worker/register-worker"

echo "[3/6] Go solver-gateway (hybrid Turnstile) ..."
(
  cd native/solver-gateway
  go build -trimpath -ldflags="-s -w" -o solver-gateway .
)
test -x native/solver-gateway/solver-gateway
echo "    [✓] native/solver-gateway/solver-gateway"
./native/solver-gateway/solver-gateway version

echo "[4/6] Rust inventory-worker ..."
(
  cd native/inventory-worker
  cargo build --release
  # stable path copy for gate / PATH-less callers
  cp -f target/release/inventory-worker ./inventory-worker
  chmod +x ./inventory-worker
)
test -x native/inventory-worker/inventory-worker
test -x native/inventory-worker/target/release/inventory-worker
echo "    [✓] native/inventory-worker/inventory-worker"
./native/inventory-worker/inventory-worker version

echo "[5/6] Rust solver-watchdog ..."
(
  cd native/solver-watchdog
  cargo build --release
  cp -f target/release/solver-watchdog ./solver-watchdog
  chmod +x ./solver-watchdog
)
test -x native/solver-watchdog/solver-watchdog
echo "    [✓] native/solver-watchdog/solver-watchdog"
./native/solver-watchdog/solver-watchdog version

echo "[6/6] C++ solver-util ..."
(
  cd native/solver-util
  g++ -O2 -std=c++17 -Wall -Wextra -o solver-util solver_util.cpp
  chmod +x ./solver-util
)
test -x native/solver-util/solver-util
echo "    [✓] native/solver-util/solver-util"
./native/solver-util/solver-util pressure | head -c 200 || true
echo ""

# smoke check
./native/proxy-worker/proxy-worker 2>/dev/null | head -1 || true
./native/register-worker/register-worker 2>/dev/null | head -1 || true

echo ""
echo "[✓] 原生组件构建完成 (Go + Rust + C++ hybrid Turnstile)"
echo "    下一步: bash scripts/polyglot_gate.sh check"
echo "    Hybrid solver: TURNSTILE_SOLVER=hybrid python -m grok_register.turnstile_solver start"
