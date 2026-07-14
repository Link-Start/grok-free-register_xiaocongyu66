# solver-gateway-cpp

Hybrid Turnstile **控制面纯 C++**（oneTBB + mimalloc）。  
**唯一非 C++ 部分**：`native/solver-hybrid/browser_worker.py`（Chromium 拿 token）。

| 层 | 实现 |
|----|------|
| HTTP API / 队列 / 调度 | C++ + **oneTBB** `concurrent_queue` |
| 内存分配 | **mimalloc**（`operator new` 覆盖） |
| 压力 / RSS / recycle | 内联 `solver_util.hpp` + 内置 watchdog 线程 |
| Turnstile token | Python `browser_worker.py`（line JSON IPC） |

## 依赖

```bash
# Debian/Ubuntu
sudo apt install g++ libtbb-dev libmimalloc-dev
```

## 构建

```bash
cd native/solver-gateway-cpp
make -j$(nproc)
./solver-gateway version   # solver-gateway 1.0.0-cpp
```

或：`bash scripts/build-native.sh`

## 运行

```bash
export TURNSTILE_SOLVER=hybrid
export SOLVER_GATEWAY_PORT=5080
export SOLVER_GATEWAY_WORKERS=auto
export PROJECT_ROOT=/path/to/grok-free-register
export SOLVER_PYTHON=$PROJECT_ROOT/.venv/bin/python

./native/solver-gateway-cpp/solver-gateway
# 或
python -m grok_register.turnstile_solver start
```

`turnstile_solver.hybrid_gateway_bin()` **优先**选择本二进制，不存在时回退 Go `native/solver-gateway`。

## API（与 Go/Theyka 兼容）

```
GET  /turnstile?url=&sitekey=&action=&cdata=  → {"task_id":"..."}
GET  /result?id=
GET  /health  /stats  /v1/memory  /
```

## 环境变量

与旧 Go gateway 相同：`SOLVER_GATEWAY_PORT`、`SOLVER_GATEWAY_WORKERS`、`SOLVER_WATCHDOG_SOFT_MB`、`SOLVER_WORKER_MAX_SOLVES`、`SOLVER_API_TOKEN` 等。

`/stats` 会报告：

```json
{"allocator":"mimalloc","scheduler":"oneTBB","engine":"hybrid-cpp", ...}
```
