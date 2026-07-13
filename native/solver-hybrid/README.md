# Hybrid Turnstile Solver (C++ / Rust / Go / Python)

Multi-language stack for higher throughput and automatic memory release.

| Layer | Language | Binary / module | Role |
|-------|----------|-----------------|------|
| Gateway | **Go** | `native/solver-gateway/solver-gateway` | HTTP API, job queue, worker pool, recycle policy |
| Watchdog | **Rust** | `native/solver-watchdog/solver-watchdog` | Host / process RSS monitor, SIGTERM→SIGKILL |
| Util | **C++** | `native/solver-util/solver-util` | Pressure score, token shape, recycle decision |
| Browser | **Python** | `native/solver-hybrid/browser_worker.py` | Patchright/Chromium solve only + GC / malloc_trim |

## Why hybrid

Browser solve cannot leave Chromium. Bottlenecks were:

1. Long-lived browser contexts leaking RSS
2. Python asyncio + SQLite result DB overhead
3. No host-level memory backpressure (this host often sits near OOM)

Go owns concurrency and the Theyka-compatible API. Python workers are disposable. Rust/C++ enforce recycle before OOM.

## API (compatible)

```
GET  /turnstile?url=&sitekey=&action=&cdata=  → {"task_id":"..."}
GET  /result?id=                              → pending|success|fail
GET  /health  /stats  /v1/memory
```

Default port: **5080** (`SOLVER_GATEWAY_PORT`).

## Memory auto-release

1. **Per solve**: page + browser context closed; `gc.collect()`
2. **Soft/hard RSS** (`SOLVER_WATCHDOG_SOFT_MB` / `HARD_MB`): full browser restart + `malloc_trim`
3. **Max solves** (`SOLVER_WORKER_MAX_SOLVES`, default 8): recycle worker
4. **On fail/timeout**: gateway kills worker process
5. **Host pressure ≥ 92%**: C++/Rust policy forces recycle
6. **Watchdog**: optional attach (dry-run on gateway; workers self-recycle)

## Build

```bash
bash scripts/build-native.sh
# or only hybrid pieces:
g++ -O2 -std=c++17 -o native/solver-util/solver-util native/solver-util/solver_util.cpp
(cd native/solver-watchdog && cargo build --release && cp target/release/solver-watchdog ./)
(cd native/solver-gateway && go build -trimpath -ldflags='-s -w' -o solver-gateway .)
```

## Run

```bash
export TURNSTILE_SOLVER=hybrid
export SOLVER_GATEWAY_PORT=5080
export SOLVER_GATEWAY_WORKERS=1          # keep low on small RAM hosts
export SOLVER_WATCHDOG_SOFT_MB=700
export SOLVER_WATCHDOG_HARD_MB=1100
export SOLVER_WORKER_MAX_SOLVES=8

./native/solver-gateway/solver-gateway
# or via control plane:
python -m grok_register.turnstile_solver start
```

## Env

| Variable | Default | Meaning |
|----------|---------|---------|
| `TURNSTILE_SOLVER` | `d3vin` | set `hybrid` to use this stack |
| `SOLVER_GATEWAY_PORT` | `5080` | listen port |
| `SOLVER_GATEWAY_WORKERS` | `1` | browser worker count |
| `SOLVER_WATCHDOG_SOFT_MB` | `700` | recycle threshold |
| `SOLVER_WATCHDOG_HARD_MB` | `1100` | hard recycle |
| `SOLVER_WORKER_MAX_SOLVES` | `8` | max solves per browser life |
| `TURNSTILE_SOLVER_HEADLESS` | `1` | headless chromium |
