# 开发交接说明

本文档给后续维护者使用。它记录当前主分支状态、已验证的性能优化、不要重复投入的方向，以及后续探索边界。

## 当前基线

当前主分支采用 CSP 风格流水线：

- `S_Worker` 生产 `T`。
- `P_Worker` 发送请求并等待 `Q`。
- `C_Worker` 通过 `Inventory.claim_pair()` 原子获取 `T + Q`。

必须遵守 [architecture.md](architecture.md) 中的不变量。性能实验不能引入中心调度器、运行时角色选择或动态并发分配。

## 已合并优化

以下优化已经进入主分支。

### Token 获取默认优化

相关配置：

```env
SOLVER_INITIAL_WAIT_MS=500
SOLVER_FAST_CLICK=1
PAGE_GOTO_WAIT_UNTIL=domcontentloaded
PAGE_POST_WAIT_MS=500
```

作用：

- 缩短 token 注入后的首次固定等待。
- 没有可见验证 frame 时跳过慢点击等待，直接进入轮询。
- `P_Worker` 和 `C_Worker` 的注册页准备只等待 DOM 可用，再保留短固定等待。

这些优化只减少 worker 内部固定耗时，不改变资源所有权和配对语义。

### C 热页池

相关配置：

```env
C_HOT_PAGE_POOL=0
C_HOT_PAGE_POOL_SIZE=0
C_SET_COOKIE_VIA_REQUEST=0
```

默认关闭。开启后，`C_Worker` 可以复用停在注册页的隔离 page context，并在每次消费后清理 cookies、localStorage 和 sessionStorage。

`C_SET_COOKIE_VIA_REQUEST=1` 会优先用 browser context request 访问 set-cookie URL，避免把热页导航离开注册页；拿不到 `sso` 时回退到原导航路径。

方向性证据：

- 关闭热页池样本：吞吐约 `15.5/min`，C 消费平均约 `7.5s`，浏览器 RSS 峰值约 `4.0GB`。
- 开启热页池样本：吞吐约 `22/min`，C 消费平均约 `2.2s`。

注意：

- 仓库默认保持关闭。
- `C_HOT_PAGE_POOL_SIZE` 是机器相关静态 profile，不是通用参数。
- 该优化仍发生在单个 `C_Worker` 的 pair lease 内，不改变 CSP 模型。

### 静态资源阻断

相关配置：

```env
PAGE_BLOCK_STATIC_ASSETS=0
```

默认关闭。开启后，`P_Worker` 和 `C_Worker` 准备注册页时阻断图片、字体、样式、媒体、`/_next/static/` 和 analytics 请求。

方向性证据：

- P 页面准备样本约 `1.6s -> 1.0s`。
- P 传输量样本约 `4.9MB -> 0.26MB`。
- C 页面准备样本约 `1.2s -> 0.9s`。
- C 传输量样本约 `1.4MB -> 0.26MB`。

该开关不作用于 `S_Worker` 的 token 页面，避免影响 token 生产。

## 已知服务器 Profile

曾在一台测试服务器上使用过以下静态 profile：

```env
PHYSICAL_CAP=6
C_HOT_PAGE_POOL=1
C_HOT_PAGE_POOL_SIZE=3
C_SET_COOKIE_VIA_REQUEST=1
```

这只是该服务器的运行 profile。不要把它当成通用默认值，也不要据此加入运行时动态调度。

## 已否定或暂缓的方向

以下方向已经做过方向性验证，短期不要重复投入，除非外部条件明显变化。

### T Pending Solver Parking

真实 A/B 表现更差。不要合并。

### 同页多个 Turnstile Widget

同页多个 widget 没有稳定产出多个 token。不要作为主方向。

### 最小同源 Turnstile 页面

最小页面没有稳定出现 iframe/token。不要作为主方向。

### S 静态资源阻断

阻断 `S_Worker` token 页面静态资源会导致 token 不产出。不要开启。

### Browser Flags

未观察到稳定收益，部分 flag 会破坏浏览器行为。不要默认加入。

### Solver 页面隔离 Context

没有看到资源占用或速度收益。不要作为主方向。

### 早期重复点击

已有探针显示，早期点击在单页单并发下看起来有潜力，但并发 2/4/6 下失败，临时生产 run 变差，`t_solve_avg` 明显上升。

结论：不能全局替换现有点击策略。它也不应作为主线优化直接合并。

已有只读材料：

- `.claude/worktrees/solver-multi-widget/tmp_turnstile_click_schedule_probe.py`：比较无点击、早期容器点击、早期重复点击、当前延迟点击。它只覆盖单页串行样本。
- `.claude/worktrees/solver-multi-widget/tmp_turnstile_concurrency_click_probe.py`：比较 early 和 delayed 在并发 1/2/4/6 下的成功情况。它暴露了 early 在并发下不稳定。
- `.claude/worktrees/solver-multi-widget/tmp_turnstile_production_timing_probe.py`：拆解一次 token 生产里的 iframe、input、response 出现时间，用来判断等待到底卡在哪个阶段。
- `.claude/worktrees/solver-multi-widget/tmp_solver_staggered_multi_probe.py`、`tmp_solver_sequence_probe.py`、`tmp_solver_batch_native_probe.py`：验证同页多 widget、顺序 widget 和 staggered widget。结论是不适合作为主方向。
- `.claude/worktrees/click-strategies/`：2026-06-20 A/B 实验。early-click lane 和 staggered 各有完整 10 分钟生产 run 日志。结论见上方"2026-06-20 A/B 验证结论"。

后续如果在授权环境里继续做方向验证，只能把它当作隔离研究项：

- `early-click solver lane`：只验证”少量隔离页面是否能稳定降低单个 token 等待”，不能改变主线 `S_Worker` 的默认路径。
- `分组错峰点击`：只验证”错开发起和点击是否能降低并发冲突”，不能变成中心调度器，也不能动态调整全局并发。

这两个方向的合并门槛很高。必须有真实 A/B 证明并发成功率、失败率、`t_solve_avg`、CPU/RSS 都优于当前默认路径，并且不破坏 [architecture.md](architecture.md) 的所有权和背压不变量。

下一轮方向验证只需要回答两个问题：

1. 低并发隔离 lane 是否能在不拖慢默认 lane 的情况下稳定贡献额外 `T`。
2. 分组错峰是否只是改变日志形态，还是能真实提高 10 分钟窗口 `recent_ok_per_min`。

如果答案不能由真实 A/B 日志支持，就不要继续调参数。

### 2026-06-20 A/B 验证结论

在远程测试服务器（2 vCPU / 7.6GB RAM, PHYSICAL_CAP=6, C_HOT_PAGE_POOL=1）上完成 10 分钟 A/B。

**Early-click lane（SOLVER_EARLY_LANES=1, SOLVER_EARLY_CLICK_MS=750,2000）**：

| 指标 | 基线 | Early-click | 变化 |
|---|---|---|---|
| 成功率 | 22.0/min | 21.3/min | -3.2% |
| 成功数 | 223 | 216 | -3.1% |
| t_solve_avg | 13.0s | 13.4s | +3.1% |
| solver_click | 0.02s | 0.50s | +2400% |
| solver_wait | 12.12s | 11.99s | -1.1% |
| 失败数 | 0 | 0 | — |

Early-click lane 单独看：24/24 成功（100%），平均 17.0s（比默认路径的 ~12s 慢 42%）。

结论：**方向已否定**。早期点击没有降低 solver_wait（瓶颈阶段），反而增加了 solver_click 开销。整体吞吐下降。不合并。

**分组错峰（SOLVER_STAGGER_MS=1000）**：

| 指标 | 基线 | Staggered | 变化 |
|---|---|---|---|
| 成功率 | 22.0/min | 22.2/min | +0.9% |
| 成功数 | 223 | 225 | +0.9% |
| t_solve_avg | 13.0s | 12.9s | -0.8% |
| solver_wait | 12.12s | 12.02s | -0.8% |
| 失败数 | 0 | 0 | — |

差异在噪声范围内，不构成改善证据。瓶颈是 Turnstile 服务端 ~12s 延迟（占 solver_wait 的 99%），不是并发冲突。标记为**已否定**。

### 后端请求直接复刻

停止继续推进。该方向越过了当前项目的安全和维护边界。

## 后续探索框架

后续研究只聚焦两件事：

- 降低 token 生产延迟。
- 降低浏览器资源消耗，使同等机器可以承载更高有效并发。

不要把精力投到已经证明不是主瓶颈的模块。

当前最接近“可能有大收益但风险高”的方向是 early-click lane 和分组错峰点击。已有证据不足以进入主分支，只能保留为单独 worktree 的授权实验项。下一位开发者不要把这两个方向误读成“已经证明有效”。

建议按“方向验证”而不是“参数打磨”的方式做实验：

1. 明确假设：它减少哪个阶段的等待或资源占用。
2. 写最小 probe：只验证方向，不追求最优参数。
3. 与当前主分支做 A/B：至少记录 10 分钟真实运行日志，短 probe 只能作为预筛。
4. 只合并低风险、默认关闭或证据充分的变更。
5. 合并前确认不破坏 `docs/architecture.md` 的不变量。

## 关键指标

运行日志中优先看：

- `rate`：累计成功速率。
- `recent_ok_per_min`：最近窗口成功速率，可用 `runtime_log_analyzer.py` 从日志计算。
- `t_solve_avg`：token 获取平均时间。
- `solver_goto` / `solver_inject` / `solver_initial` / `solver_click` / `solver_wait`：token 阶段拆分。
- `T` 和 `Q` 库存：长期 `T:0 Q>0` 通常说明 token 生产慢。
- `phys`：长期为 `0` 说明本地浏览器许可打满。
- 浏览器进程 RSS、CPU：判断继续提高并发是否只会拉高延迟。

示例：

```bash
python3 - <<'PY'
from pathlib import Path
from runtime_log_analyzer import analyze_text
print(analyze_text(Path("run.log").read_text()))
PY
```

## 变更边界

可以优先合并的变更：

- worker 内部固定等待减少；
- 默认关闭的性能开关；
- 只读监控和日志拆分；
- 不改变 `Inventory` 所有权语义的页面复用；
- 有明确回退路径的请求路径优化。

需要单独 worktree 和真实 A/B 的变更：

- token 点击策略；
- 新的浏览器生命周期模型；
- 降低页面保活成本的高风险模式；
- 替换浏览器或替换核心页面流程；
- 修改 `Physical_Sem`、slot、pending 的容量语义。

不要合并的变更：

- 中心化角色调度；
- 运行时动态调并发；
- worker 直接操作底层库存队列；
- `C_Worker` 先拿单边资源再等另一边；
- `Q` 未返回就占用 `Q_Slot_Sem`；
- `P_Worker` 等待 `Q` 时持有 `Physical_Sem`。

## 合并前验证

代码变更合并前至少运行：

```bash
python3 -m py_compile register.py core/observer.py core/inventory.py core/envelope.py core/admission.py
python3 -m unittest tests.test_register_runtime_unittest tests.test_inventory_unittest tests.test_admission_gate tests.test_runtime_log_analyzer -v
```

架构或取消语义变更还要运行：

```bash
python3 -m pytest tests -q
python3 run_tests.py
```

性能变更需要保存 A/B 日志，并在提交说明里写清：

- 对比基线 commit；
- 运行时长；
- `.env` 中与性能相关的配置；
- 最终累计速率和最近窗口速率；
- `t_solve_avg` 和关键阶段耗时；
- 失败数；
- CPU/RSS 观察。
