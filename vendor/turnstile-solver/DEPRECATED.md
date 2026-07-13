# 旧版 Turnstile Solver（计划删除）

`d3vin/` 与 `theyka/` 为历史 vendored 引擎，**新部署请使用 hybrid**：

| 路径 | 说明 |
|------|------|
| `native/solver-gateway/` | Go 网关 · 多核队列 |
| `native/solver-watchdog/` | Rust 内存看门狗 |
| `native/solver-util/` | C++ 压力/回收策略 |
| `native/solver-hybrid/browser_worker.py` | Python 浏览器 worker |

启用：

```env
TURNSTILE_SOLVER=hybrid
```

本目录仅作兼容回退；确认 hybrid 稳定后可整体删除 `vendor/turnstile-solver/{d3vin,theyka}`。
