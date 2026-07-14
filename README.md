# grok-free-register

`grok-free-register` 是 **Python + Go** 为主的一体化注册工具（hybrid Turnstile 另含 Rust 看门狗 + C++）：浏览器编排与控制面用 Python；代理测活、HTTP 注册、**账号库存 / SSO→OAuth 协议转换 / CPA·sub2api** 用 Go。

> **硬性条件**：Python + Go 核心二进制必须可用，否则 `start.sh` / `setup.sh` / 控制面板 **拒绝启动**。  
> 自检：`bash scripts/polyglot_gate.sh check`

运行结果写入 `keys/`（legacy / sub2api / CPA）。

## 架构分工

| 语言 | 组件 | 职责 |
|------|------|------|
| **Python** | `grok_register/*` · 面板 · Turnstile · enroller | 编排、浏览器 CSP、配置、控制面 |
| **Go** | `proxy-worker` · `register-worker` · **`inventory-worker`** | 代理测活、协议注册、库存扫描、CPA↔sub2api、协议 SSO 转换 |
| **Rust / C++** | `solver-watchdog` · `solver-util` | Hybrid Turnstile 内存看门狗与压力检测（非库存） |

```text
start.sh ──► ensure_runtime ──► polyglot_gate (Python+Go [+hybrid])
                 │
                 ├─ Python register / dashboard
                 ├─ Go     proxy-worker / register-worker
                 └─ Go     inventory-worker (库存 · 协议转换 · 成品包)
```

## 快速开始

**前置工具链：**

- Python 3.10+（`python3` + `venv`）
- Go 1.21+（`go`）— **必须**（含 inventory-worker）
- Rust + g++ — hybrid Turnstile 需要（`solver-watchdog` / `solver-util`）

```bash
git clone https://github.com/hechuyi/grok-free-register.git
cd grok-free-register
bash setup.sh                 # 安装 Python 依赖并编译 Go (+ hybrid)
bash scripts/polyglot_gate.sh check
bash start.sh
```

首次运行会创建 `.venv`、编译原生二进制，并引导生成 `.env`。原生组件也可单独构建：

```bash
bash scripts/build-native.sh  # Go proxy/register/inventory + hybrid
```

需要完整说明时，按用途查看：

- [注册教程](docs/guides/registration.md)
- [本地认证服务](docs/guides/auth-service.md)
- [凭据库存与取用](docs/guides/credential-inventory.md)
- [运行状态与排障](docs/guides/runtime-troubleshooting.md)

常用命令：

```bash
bash start.sh               # 按当前 .env 前台运行（先过 polyglot 门禁）
bash start.sh --dashboard   # Web 控制面板（中英切换 · 成品下载）
bash start.sh --target 100  # 成功 100 个后停止
bash start.sh --max-mem 6G  # 自动估算并发时最多使用 6G 内存
bash start.sh --reconfig    # 重新选择邮箱模式
bash scripts/polyglot_gate.sh check   # 栈自检
```

`start.sh` 直接在当前终端显示状态。按 `Ctrl-C` 停止，再次执行同一命令即可重启；不需要额外的会话管理或守护进程依赖。

需要全局代理时，在 `.env` 中加入：

```env
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
```

## 邮箱模式

`tempmail` 是默认模式，不需要额外配置，适合快速试跑：

```env
EMAIL_MODE=tempmail
```

`moemail` 使用 MoeMail OpenAPI，适合已有 MoeMail 实例和 API Key 的场景：

```env
EMAIL_MODE=moemail
MOEMAIL_API=https://moemail.app
MOEMAIL_API_KEY=mk_xxx
# MOEMAIL_DOMAIN=moemail.app
```

`moemail` 模式只使用 MoeMail，不会 fallback 到其它临时邮箱 provider。

需要 Grok/xAI 代理池时，在项目目录创建 `代理.txt`，一行一个代理即可。未显式设置 `PROXY_POOL_FILE` 时，也兼容读取 `proxy.txt`。这个代理池只用于 xAI 注册页、发码、提交和自动 OAuth 转换，MoeMail 等邮箱 HTTP 默认直连：

```text
http://user:pass@host:port
socks5://user:pass@host:port
vless://...
trojan://...
```

`代理.txt` 也支持 `vmess`、`vless`、`trojan`、`ss`、`hy2`、`hysteria2`、`tuic`、`anytls` 分享链接。程序内置了 sing-box relay，会把分享链接转换成本地 `http://127.0.0.1:端口` 后再测试 Grok/xAI 连通性；如果本机已有外部 proxy-relay，也兼容 `http://127.0.0.1:18080` 的 `/api/state` 和 `/api/nodes/import`。

需要自动节点池时开启本地开关：

```env
PROXY_AUTO_FETCH_ENABLED=1
PROXY_POOL_STRATEGY=random
PROXY_AUTO_FETCH_WORKERS=16
PROXY_AUTO_TEST_WORKERS=32
PROXY_AUTO_REQUIRE_ACTIVE=1
PROXY_POOL_USE_TESTED_ONLY=1
```

开启后程序会在跑账号前先多线程拉取订阅、多线程测试节点，只保留能访问 `PROXY_AUTO_TEST_URLS` 的代理并按延迟优先写入 `logs/proxy-auto-active.txt`；没有可用代理且 `PROXY_AUTO_REQUIRE_ACTIVE=1` 时会直接停止，不会悄悄直连。上一轮可用的自动代理和 `代理.txt` 里的手动代理会一起复测，开启 `PROXY_POOL_USE_TESTED_ONLY=1` 后注册阶段只使用通过 Grok/xAI 连通性测试的代理。后台每 20 分钟刷新一次，拉取订阅时会用当前已有代理做轮换请求，避免所有源站请求都走同一个出口。可在 `proxy-sources.txt` 里追加订阅源，一行一个 URL；带 `*` 前缀表示这个 URL 返回的是“订阅源列表”。自动导出文件在 `logs/` 下，支持 raw、base64、sub2api/cpa 导入 JSON。

### 公共订阅 / 免费代理爬取（可选）

内置 `proxy_scraper` 会从公开目录抓取候选节点（含 [proxifly/free-proxy-list](https://github.com/proxifly/free-proxy-list)、[snakem982/proxypool](https://github.com/snakem982/proxypool)、TheSpeedX、ProxyScrape 等），输出到 `logs/proxy-scraper-candidates.txt`。`proxy_auto` 刷新时会自动读入该文件并做 Grok/xAI 连通性测试。

```bash
bash start.sh --scrape-proxies              # 抓取内置目录 + proxy-scraper-sources.txt
bash start.sh --scrape-proxies --github     # 额外用 GitHub code search 发现 raw 列表（建议 GITHUB_TOKEN）
.venv/bin/python -m grok_register.proxy_scraper sources
```

说明：

- 以 **HTTP 拉取 raw/API/文本** 为主，不跑浏览器 JS；页面若只有前端渲染，应改用其 raw/API 源。
- 抓到的是**候选**，必须经 `PROXY_AUTO_FETCH_ENABLED=1` 测活后才会进入注册池。
- 可在 `proxy-scraper-sources.txt` 追加源；裸 `ip:port` 列表可用 `#scheme=socks5` 标注协议。

### Go 代理测活 + Go 库存 / 协议转换（硬性原生组件）

项目 **必须** 编译并启用下列二进制（`setup.sh` / `start.sh` 会自动构建并门禁校验）：

| 二进制 | 语言 | 作用 |
|--------|------|------|
| `native/proxy-worker/proxy-worker` | Go | 大批量代理测活（goroutine 池） |
| `native/register-worker/register-worker` | Go | 协议 / HTTP 并发注册 |
| `native/inventory-worker/inventory-worker` | Go | 账号扫描 · CPA↔sub2api · 协议 SSO→OAuth |

```bash
bash scripts/build-native.sh
bash scripts/polyglot_gate.sh check
```

```env
PROXY_WORKER_ENGINE=go
# PROXY_WORKER_BIN=native/proxy-worker/proxy-worker
# PROXY_WORKER_URL=http://127.0.0.1:18765
INVENTORY_ENGINE=go
# INVENTORY_WORKER_BIN=native/inventory-worker/inventory-worker
```

```bash
./native/proxy-worker/proxy-worker serve --port 18765
./native/inventory-worker/inventory-worker scan --keys-dir keys --json
./native/inventory-worker/inventory-worker rebuild --keys-dir keys
```

### Web 控制面板

注册进程会周期性写入 `logs/runtime-status.json`。控制面板读取该快照，并提供 **全部 .env 配置编辑**、账号状态、**CPA / sub2api 成品下载**、启停与 Go/Python 引擎切换。

```bash
bash start.sh --dashboard
# 打开 http://127.0.0.1:8787/
```

```env
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8787
# 允许从面板 start/stop/scrape/save_config（默认关闭；重建合并包与下载不需要）
CONTROL_PLANE_ALLOW_ACTIONS=1
```

面板页：

- **Overview** — KPI、限流、引擎、启停（Python / Go）、成品下载入口
- **Accounts** — 账号库存状态（oauth_ready / oauth_pending）、格式过滤、下载 sub2api / CPA / legacy
- **All Config** — 全量配置目录编辑并写回 `.env`
- **Raw JSON** — 运行时快照

成品下载（可直接导入对应面板）：

| 格式 | 文件 | 下载 |
|------|------|------|
| sub2api | `keys/sub2api/accounts.sub2api.json` | `/api/download?format=sub2api` |
| CPA singles ZIP | `keys/cpa/xai-*.json` → zip | `/api/download?format=cpa_zip` |
| legacy | `keys/accounts.txt` | `/api/download?format=legacy` |

面板上的「重建合并包」会从单账号 `xai-*.json` / `*.sub2api.json` 重新生成上述合并文件。

API：

- `GET /api/status` — 总览（含账号 by_status / products 链接）
- `GET /api/accounts` — 账号列表与状态（`?status=&format=&limit=`）
- `GET /api/accounts/summary` — 库存汇总
- `GET /api/download?format=sub2api|cpa|cpa_zip|legacy` — 成品附件下载
- `GET /api/config` — 全量配置（密钥默认掩码）
- `GET /api/status/raw` — 原始 runtime snapshot
- `POST /api/action` — `start` / `stop` / `scrape` / `save_config` / `rebuild_bundles`

**Web 用 Go 还是 Python？** 控制面板保持 **Python**（stdlib `ThreadingHTTPServer`）：配置/库存/enroller 都是 Python 生态，改动快、与 `register.py` 同进程模型。高并发 I/O（代理测活、实验性 HTTP 注册）继续用 **Go worker**。不建议为面板单独上 Go Web 框架——收益小、双端配置同步成本高。

### Go 并发注册（实验）

HTTP 路径注册可交给 Go worker（Turnstile API + MoeMail/custom + grpc-web + signup）。浏览器路径仍用 Python。

```bash
bash scripts/build-native.sh
# 先确保 Turnstile API 可用（d3vin），并 export 配置：
export REGISTER_ENGINE=go
export GO_REGISTER_WORKERS=4
export TURNSTILE_SOLVER=d3vin
export EMAIL_MODE=moemail
export MOEMAIL_API_KEY=...
bash start.sh --target 10
```

Python 会 `fetch_config` 拿到 `SITE_KEY/ACTION_ID/STATE_TREE` 后拉起 `native/register-worker`。  
也可在面板 Overview 点 **Start Go**（需 `CONTROL_PLANE_ALLOW_ACTIONS=1`，并已准备好 SITE_KEY 等配置 JSON）。

注意：Go 路径不跑 Playwright，风控/页面变更时需继续完善；成功账号写入 `keys/accounts.go.txt`。

注册成功后写入：

| 文件 | 格式 | 用途 |
|------|------|------|
| **`keys/sso.txt`** | `email:sso` | **规范 SSO**（convert 默认源；一邮箱一行，重登删旧换新） |
| `keys/accounts.txt` | `email:password` | 重登账密 |
| `keys/grok.txt` | 纯 token | 由 `sso.txt` 生成 |
| `keys/auth-sessions.jsonl` | JSONL | 会话备份 |

最终成品 CPA 用 **grok2api 同款协议授权**（Go `inventory-worker`，读 `sso.txt`）：

```bash
bash scripts/build-native.sh
python -m grok_register.sso.export convert --formats cpa --limit 200 --workers 16
# 或 bash auth-service.sh --once
# 面板「SSO→CPA」；批次结束后自动：SSO_CONVERT_AFTER_REGISTER=1
```

写出 `keys/cpa/xai-*.json`。可选 `SSO_CONVERT_FORMATS=cpa,sub2api`。

外部邮箱接口如果偶发 Cloudflare 拦截，可开启 CF-Ares 兜底。项目已内置 `vendor/CF-Ares`，默认会从本地源码安装和优先导入，不依赖 PyPI wheel：

```env
CF_ARES_EMAIL=fallback
CF_ARES_XAI=fallback
```

### 内置 Turnstile Solver（可选）

| 引擎 | 栈 / 上游 | 默认端口 |
|---|---|---:|
| **`hybrid`（推荐）** | Go 网关 + Rust 看门狗 + C++ 工具 + Python 浏览器 worker，**自动释放内存** | **5080** |
| `d3vin` | [D3-vin/Turnstile-Solver-NEW](https://github.com/D3-vin/Turnstile-Solver-NEW)（`vendor/turnstile-solver/d3vin`） | 5072 |
| `theyka` | [Theyka/Turnstile-Solver](https://github.com/Theyka/Turnstile-Solver)（`vendor/turnstile-solver/theyka`） | 5000 |

API 兼容：`GET /turnstile` → `task_id`，`GET /result` → token。Hybrid 说明见 `native/solver-hybrid/README.md`（构建：`bash scripts/build-native.sh`）。

推荐直接在 `.env` 启用 hybrid（注册进程会自动拉起网关；超 RSS 自动回收 Chromium）：

```env
TURNSTILE_SOLVER=hybrid
SOLVER_GATEWAY_WORKERS=1
SOLVER_WATCHDOG_SOFT_MB=700
SOLVER_WATCHDOG_HARD_MB=1100
SOLVER_WORKER_MAX_SOLVES=8
# 兼容旧引擎：
# TURNSTILE_SOLVER=d3vin
# TURNSTILE_SOLVER=theyka
TURNSTILE_SOLVER_THREADS=1
TURNSTILE_SOLVER_BROWSER=chromium
TURNSTILE_SOLVER_HEADLESS=1
```

也可单独管理：

```bash
bash start.sh --turnstile-solver install   # 安装依赖 + patchright chromium
bash start.sh --turnstile-solver start     # 前台运行 solver
bash start.sh --turnstile-solver status
bash start.sh --turnstile-solver stop
```

运行状态与日志在 `logs/turnstile-solver/<engine>/`。需要给 solver 单独代理时，创建项目根目录 `turnstile-proxies.txt`（一行一个），并设置 `TURNSTILE_SOLVER_PROXY=1`。

仍可用外部已启动的服务：

```env
TURNSTILE_SOLVER=api
TURNSTILE_SOLVER_ENGINE=external
TURNSTILE_API_URL=http://127.0.0.1:5072
```

`d3vin` / `theyka` / `api` 模式下 S_Worker 不占用本机注册浏览器物理并发槽；P/C 阶段仍用本机浏览器。

`custom` 是自建域名邮箱模式，适合长时间运行。需要一个已接入 Cloudflare Email Routing 的域名，并在运行机器上启动本项目的收信服务。

配置步骤：

1. 在 Cloudflare 为域名开启 Email Routing。
2. 部署 `cloudflare/email-worker.js`。
3. 在 Email Routing 中配置 catch-all，动作选择发送到该 Worker。
4. 在运行机器上启动收信服务：

```bash
bash start.sh --email-service
```

5. 在 `.env` 中配置：

```env
EMAIL_MODE=custom
EMAIL_DOMAIN=example.com
EMAIL_API=http://127.0.0.1:8080
```

如果 Worker 需要回调本机服务，`WEBHOOK_URL` 应使用可访问的域名地址。

## 配置

完整模板见 `.env.example`。日常使用通常只需要配置邮箱模式、代理、目标数量和内存预算。

| 配置 | 默认值 | 说明 |
|---|---:|---|
| `EMAIL_MODE` | `tempmail` | 邮箱模式，支持 `tempmail`、`moemail` 和 `custom` |
| `MOEMAIL_API` | `https://moemail.app` | `moemail` 模式使用的 MoeMail 地址 |
| `MOEMAIL_API_KEY` | 空 | `moemail` 模式使用的 API Key |
| `MOEMAIL_DOMAIN` | 空 | `moemail` 模式指定邮箱域名，留空自动读取 |
| `MOEMAIL_EXPIRY_MS` | `3600000` | `moemail` 邮箱有效期，单位毫秒 |
| `KEY_EXPORT_DIR` | `keys` | 注册结果输出目录 |
| `KEY_EXPORT_FORMATS` | `legacy` | 注册阶段固定只写 SSO 包（其它值忽略） |
| `KEY_EXPORT_ENROLLER` | `0`（硬关） | 旧版注册时 live OAuth 已删除；请用 SSO→CPA |
| `SSO_CONVERT_FORMATS` | `cpa` | 协议批量转换目标：`cpa` / `cpa,sub2api` |
| `SSO_CONVERT_AFTER_REGISTER` | `0` | `1`=协议注册结束后自动 SSO→CPA |
| `CONVERT_WORKERS` | `2` | Go 协议授权并发 |
| `PROXY_POOL_FILE` | `代理.txt` | 可选 Grok/xAI 代理池文件，一行一个 `http`/`socks5` 代理或节点分享链接 |
| `PROXY_POOL_STRATEGY` | `round_robin` | 代理选择方式，支持 `round_robin` 和 `random` |
| `PROXY_RELAY_ENABLED` | `1` | 是否把节点分享链接转成本地代理 |
| `PROXY_RELAY_BUILTIN_ENABLED` | `1` | 是否启用内置 sing-box relay |
| `PROXY_RELAY_AUTO_INSTALL` | `1` | 本机没有 `sing-box` 时是否自动下载到 `PROXY_RELAY_WORK_DIR/bin` |
| `PROXY_RELAY_WORK_DIR` | `logs/proxy-relay` | 内置 relay 的运行目录 |
| `PROXY_RELAY_MAX_NODES` | `48` | 单轮最多同时启动的内置 relay 节点数 |
| `PROXY_RELAY_URL` | `http://127.0.0.1:18080` | 可选外部 proxy-relay 管理 API 地址 |
| `PROXY_RELAY_KERNEL` | `auto` | 分享链接导入内核，支持 `auto`、`sing-box`、`xray` |
| `PROXY_RELAY_PROXY_SCHEME` | `auto` | 导入后给 Grok/xAI 链路使用的代理协议，`auto` 下 sing-box 为 `http`、xray 为 `socks5` |
| `PROXY_RELAY_TIMEOUT` | `8` | 调用 proxy-relay 管理 API 的超时秒数 |
| `PROXY_RELAY_RETRY_SEC` | `30` | 分享链接导入失败后的重试间隔 |
| `PROXY_AUTO_FETCH_ENABLED` | `0` | 是否启用自动节点拉取和测试 |
| `PROXY_AUTO_FETCH_URLS` | 内置 NoMoreWalls 订阅 | 逗号或换行分隔的订阅源 URL |
| `PROXY_AUTO_FETCH_SOURCES_FILE` | `proxy-sources.txt` | 本地订阅源文件，一行一个 URL |
| `PROXY_AUTO_FETCH_INTERVAL_SEC` | `1200` | 自动刷新间隔，默认 20 分钟 |
| `PROXY_AUTO_FETCH_WORKERS` | `8` | 拉取订阅的线程数 |
| `PROXY_AUTO_TEST_WORKERS` | `16` | 测试节点的线程数 |
| `PROXY_AUTO_FETCH_TIMEOUT` | `12` | 拉取订阅的 HTTP 超时秒数 |
| `PROXY_AUTO_TEST_TIMEOUT` | `10` | 测试节点的 HTTP 超时秒数 |
| `PROXY_AUTO_TEST_URLS` | `https://accounts.x.ai/sign-up?redirect=grok-com` | 节点可用性测试 URL |
| `PROXY_AUTO_TEST_ACCEPT_STATUS` | `200-399` | 可接受 HTTP 状态码范围 |
| `PROXY_AUTO_EXPORT_FORMATS` | `raw,sub2api` | 自动导出格式，支持 `raw`、`base64`、`sub2api`、`cpa` |
| `PROXY_AUTO_OUTPUT_DIR` | `logs` | 自动节点池输出目录 |
| `PROXY_AUTO_ACTIVE_FILE` | `proxy-auto-active.txt` | 当前可用自动代理列表文件名 |
| `PROXY_AUTO_STATE_FILE` | `proxy-auto-state.json` | 自动节点池状态文件名 |
| `PROXY_AUTO_SOURCE_LIST_DEPTH` | `1` | `*URL` 订阅源列表最多展开层数 |
| `PROXY_AUTO_MAX_CANDIDATES` | `0` | 单轮最多测试候选数，`0` 表示不限 |
| `PROXY_AUTO_MAX_ACTIVE` | `0` | 自动池最多保留可用代理数，`0` 表示不限 |
| `PROXY_AUTO_REQUIRE_ACTIVE` | 跟随 `PROXY_AUTO_FETCH_ENABLED` | 启动前必须筛出至少一个可访问 Grok/xAI 的代理，否则停止 |
| `PROXY_AUTO_INCLUDE_BOOTSTRAP_CANDIDATES` | `1` | 是否把上一轮可用代理和 `代理.txt` 手动代理也纳入本轮连通性测试 |
| `PROXY_POOL_USE_TESTED_ONLY` | 自动代理必需时为 `1` | 启动前测试完成后，注册阶段只使用通过测试的自动池代理 |
| `CF_ARES_EMAIL` | `0` | 可选邮箱 HTTP 兜底，`fallback` 遇到 Cloudflare 拦截时重试，`always` 始终使用 |
| `CF_ARES_XAI` | 跟随 `CF_ARES_EMAIL` | 可选 xAI/Grok HTTP 兜底，用于发码、验码、注册提交和 set-cookie |
| `CF_ARES_IMPERSONATE` | `chrome120` | `cf-ares` wheel 不完整时使用的 `curl_cffi` 浏览器指纹 |
| `CF_ARES_BROWSER_ENGINE` | `auto` | CF-Ares 浏览器引擎，支持 `auto`、`undetected`、`seleniumbase` |
| `CF_ARES_HEADLESS` | `1` | CF-Ares 浏览器是否无头运行 |
| `CF_ARES_PROXY` | 空 | CF-Ares 代理，留空沿用 `HTTPS_PROXY`/`HTTP_PROXY` |
| `CF_ARES_PATH` | 空 | 可选覆盖为其他 CF-Ares 源码目录，不设置则使用项目内置 `vendor/CF-Ares` |
| `EMAIL_DOMAIN` | 空 | `custom` 模式使用的域名 |
| `EMAIL_API` | `http://127.0.0.1:8080` | 本地收信服务地址 |
| `TARGET` | `0` | 成功数量目标，`0` 表示不限 |
| `PHYSICAL_CAP` | `0` | 浏览器并发上限，`0` 表示启动时自动估算 |
| `PHYSICAL_PER_CPU` | `2` | 自动估算时每个 CPU 核心对应的并发参考值 |
| `PHYSICAL_MEM_MB` | `512` | 自动估算时每个浏览器任务的内存预算 |
| `MIN_FREE_MEM_MB` | `500` | 自动估算时保留的内存 |
| `T_SLOT_CAP` | `8` | token 缓冲容量 |
| `Q_SLOT_CAP` | `8` | 验证码缓冲容量 |
| `Q_PENDING_CAP` | `12` | 等待验证码返回的请求上限 |
| `EMAIL_CODE_RESEND_ATTEMPTS` | `2` | 邮箱验证码未收到时重新发送的最大次数 |
| `EMAIL_CODE_RESEND_AFTER_SEC` | `35` | 等待多久仍未收到验证码后重发 |
| `SOLVER_MOUSE_CLICK_RETRIES` | `3` | token 验证框中心点击次数，`0` 表示关闭 |
| `TURNSTILE_SOLVER` | `hybrid` | `hybrid` / `d3vin` / `theyka` / `local` / `api` |
| `SOLVER_GATEWAY_WORKERS` | `1` | hybrid 浏览器 worker 数（内存紧时保持 1） |
| `SOLVER_WATCHDOG_SOFT_MB` / `HARD_MB` | `700` / `1100` | worker RSS 软/硬上限，超限自动回收 |
| `SOLVER_WORKER_MAX_SOLVES` | `8` | 每浏览器最多求解次数后重启 |
| `TURNSTILE_SOLVER_ENGINE` | `d3vin` | `api` 模式下内置引擎：`hybrid` / `d3vin` / `theyka` / `external` |
| `TURNSTILE_API_URL` | 按引擎默认 | hybrid=`:5080`，d3vin=`:5072`，theyka=`:5000` |
| `TURNSTILE_API_MANAGED` | `1` | 是否自动管理内置 solver 子进程 |
| `TURNSTILE_API_TIMEOUT` | `90` | API 单次求解超时（秒） |
| `TURNSTILE_API_POLL_INTERVAL_MS` | `500` | 轮询 `/result` 的间隔（毫秒） |
| `TURNSTILE_API_ACTION` | 空 | 可选，传给 solver 的 `action` |
| `TURNSTILE_API_CDATA` | 空 | 可选，传给 solver 的 `cdata` |
| `TURNSTILE_SOLVER_THREADS` | `2` | 内置 solver 浏览器线程数 |
| `TURNSTILE_SOLVER_BROWSER` | `chromium` | `chromium` / `chrome` / `msedge` / `camoufox` |
| `TURNSTILE_SOLVER_HEADLESS` | `1` | 内置 solver 是否无头 |
| `REGISTER_HEARTBEAT_INTERVAL` | `60` | 普通日志模式下的运行心跳间隔，`0` 表示关闭 |
| `PAGE_BLOCK_STATIC_ASSETS` | `0` | 可选：阻断部分静态资源，降低页面准备成本 |
| `C_HOT_PAGE_POOL` | `0` | 可选：复用消费阶段页面，减少页面重建开销 |
| `C_SET_COOKIE_VIA_REQUEST` | `1` | 优先用浏览器 request 写入登录 cookie，失败再回退页面导航 |

不确定怎么设置时，先保持默认值。性能压测时优先观察 `PHYSICAL_CAP` 和内存，不建议先改 Worker 数量。

## 运行日志

直接运行 `bash start.sh` 时，终端只输出任务开始、成功或失败、本次运行平均速度、累计数量和限流等待：

```text
[→] 开始注册 #38
[✓] 注册成功 #38 | 运行平均 9.9/分 | 累计 38
[*] 运行中 | T:0 Q:0 发码:0 回码:0 开始:0 成功:0
[⏸] 触发限流 | 60秒后恢复探测
[▶] 限流解除 | 实际等待 61秒
```

需要调试并发、库存和阶段耗时时，使用：

```bash
bash start.sh --debug
```

它会在上述任务事件之外，每 8 秒输出一次完整的 T/Q、物理并发和阶段耗时面板。已有自动化环境也可继续使用 `REGISTER_LOG_MODE=debug`。

常用字段：

| 字段 | 含义 |
|---|---|
| `T` | 当前可用 token 数量 |
| `Q` | 当前可用验证码数量 |
| `phys` | 空闲浏览器并发许可 |
| `s_phys` / `p_phys` / `c_phys` | S/P/C 获取浏览器许可的平均等待秒数 / 平均持有秒数 |
| `p_stage` | P 阶段平均耗时：建邮箱 / 准备页面 / 发送请求 |
| `c_stage` | C 阶段平均耗时：拿页面 / 验证码校验 / 注册提交 |
| `c_hot` | C 热页池命中 / 未命中次数 |
| `t_solve_avg` | 平均 token 获取时间 |
| `q_sent` / `q_ret` | 已发送 / 已收到的验证码数量 |
| `pair` | 已配对消费次数 |
| `ok` / `fail` | 成功 / 失败数量 |
| `rate` | 当前累计成功速率 |

简单判断：

- `T` 长期为 `0` 且 `Q` 有库存，通常是 token 获取较慢。
- `Q` 长期为 `0` 且 `T` 有库存，通常是邮箱或验证码链路较慢。
- `phys` 长期为 `0`，说明浏览器并发已经用满。
- `t_solve_avg` 明显升高，通常表示浏览器压力、网络质量或 token 服务响应变慢。

可以用日志分析工具解析已有日志：

```bash
python3 - <<'PY'
from pathlib import Path
from tools.runtime_log_analyzer import analyze_text
print(analyze_text(Path("run.log").read_text()))
PY
```

## 输出文件

成功结果写入 `keys/`（默认不提交 Git）：

```text
keys/sso.txt                 # email:sso          ← convert 唯一 SSO 源
keys/accounts.txt            # email:password     ← 重登
keys/grok.txt                # SSO token 列表（由 sso.txt 生成）
keys/auth-sessions.jsonl
keys/browser-fingerprints.json
keys/cpa/xai-*.json          # 协议授权后的成品 CPA
keys/sub2api/…               # 可选
```

`sso.txt` 每行：

```text
email:sso_token
```

`accounts.txt` 每行（仅账密）：

```text
email:password
```

协议授权（`sso.export convert` / `auth-service` / 面板 SSO→CPA）成功后写入：

```text
keys/cpa/xai-*.json
```

`xai-*.json` 是 **CLIProxyAPI 唯一可识别** 的单账号认证文件（`type: xai`）。  
CPA 仅支持单账号 `keys/cpa/xai-*.json`（已永久移除合并包 `accounts.cpa.json`）。

### CLIProxyAPI 自动导入与过期刷新

```bash
# 一次性：刷新到期 token + 导入 keys/cpa/xai-*.json → CLIPROXYAPI_AUTH_DIR
python3 -m grok_register.cliproxyapi --once

# 仅导入（不打 auth.x.ai）
python3 -m grok_register.cliproxyapi --once --import-only

# 后台 worker（默认 300s 周期）
python3 -m grok_register.cliproxyapi --worker
# 或 bash scripts/sync_cpa_to_cliproxy.sh
```

主要环境变量：`CLIPROXYAPI_ENABLED`、`CLIPROXYAPI_AUTH_DIR`（默认 `/root/CLIProxyAPI/auths`）、`CLIPROXYAPI_AUTO_REFRESH`、`CLIPROXYAPI_INTERVAL_SEC`。  
面板「账号」页可点：同步 CLIProxyAPI / 刷新过期 Token / 启停自动同步。

### xAI 协议注册（逆向参考）

- **主参考**：[grok-build-auth](https://github.com/dongguatanglinux/grok-build-auth) — OAuth PKCE + CreateSession gRPC-web + CLIProxyAPI 导出  
- **模式参考**：KiroX / aBaiAutoplus 是其它产品的协议注册机（并发、邮箱池、导出），**不是** xAI 线格式  
- 本仓库：`grok_register/xai_protocol_oauth.py`（PKCE / 换 token / 导出单文件）；完整无浏览器登录可挂载 `GROK_BUILD_AUTH_ROOT=/tmp/grok-build-auth`

## 项目结构

```text
start.sh / auth-service.sh / setup.sh   用户入口
grok_register/
  register.py / protocol_register.py    注册（浏览器 CSP / 协议）
  dashboard.py                          Web 面板
  sso/                                  SSO 包 → CPA 协议转换
    export.py                           convert / pending CLI
    auth_service.py                     auth-service 守护
    protocol.py                         curl_cffi 协议辅助
  inventory/                            账号扫描 / 文件互转
  proxy/                                代理池 / 中继 / 抓取
  core/                                 CSP 库存与背压
native/
  inventory-worker/                     Go：SSO→CPA + 库存
  solver-gateway/ + solver-hybrid/      Hybrid Turnstile
  proxy-worker/ register-worker/        测活 / 注册 worker
xai_enroller/                           遗留（见 LEGACY.md；非默认入口）
keys/                                   运行产物（SSO / CPA，不入库）
docs/ scripts/ tests/ vendor/
```

模块路径优先用子包（`grok_register.sso.export`）；根下旧名 `sso_export` / `proxy_auto` 等仍为 **兼容 shim**。

## 测试

测试依赖与运行依赖分开安装：

```bash
.venv/bin/pip install -r tests/requirements.txt
```

快速检查：

```bash
python3 -m unittest tests.test_admission_gate tests.test_register_runtime_unittest tests.test_inventory_unittest tests.test_runtime_log_analyzer -v
```

完整测试：

```bash
python3 -m pytest tests -q
```

## xAI 协议认证（SSO→CPA）

`bash auth-service.sh` = **Go 协议**（grok2api `sso_build`，多并发 + 可选多 IP），  
读取 **`keys/sso.txt`**（`email:sso`）转成 `keys/cpa/xai-*.json`（含 **access + refresh**）。

```bash
# 文件分工
# keys/sso.txt       email:sso          ← convert 唯一 SSO 源（一邮箱一行）
# keys/accounts.txt  email:password     ← 重登用
# keys/grok.txt      纯 token 列表      ← 由 sso.txt 生成

bash auth-service.sh --once --limit 500 --workers 16
bash scripts/sso-relogin.sh --limit 20 --workers 2   # 重登：删旧 SSO 写新 email:sso
python -m grok_register.sso.export convert --formats cpa --limit 500 --workers 16
```

**refresh_token 怎么来的**：Device Flow 申请 scope 带 `offline_access`，  
`POST auth.x.ai/oauth2/token`（`grant_type=device_code`）响应里直接下发；  
之后用 `grant_type=refresh_token` 续 access（`cliproxyapi`）。

也可用 CLI：

```bash
python -m grok_register.sso_export convert --formats cpa --limit 500 --workers 16
```

### 分离设备的 SSH 模式

注册机和认证服务分开运行时，服务器继续写入注册结果，本地认证服务
每 30 秒通过一次性 SSH 导出全量 JSONL 快照。快照经逐行校验、`fsync` 后原子替换到
`~/Downloads/grok-free-register-auth/source-snapshot.jsonl`；同步失败会继续使用上一份有效快照。
认证使用本机 CloakBrowser Chromium，
成功结果写入 `~/Downloads/grok-free-register-auth/authenticated/`，运行状态、同步快照与
认证文件分开保存；认证文件格式可以直接供 CPA 使用。

先把导出器同步到注册机的 `scripts/` 目录：

```bash
scp scripts/export_registered_sessions.py user@your-server:/opt/grok-free-register/scripts/
```

然后在本地配置 SSH 连接：

下面这些变量既可在终端 `export`，也可写入由 `.env.example` 复制出的 `.env`；
`auth-service.sh` 会自动读取该文件。

```bash
export XAI_AUTH_SERVICE_SSH_HOST=user@your-server
export XAI_AUTH_SERVICE_SSH_IDENTITY=/path/to/ssh-key.pem  # 使用 ssh-agent 时可省略
export XAI_AUTH_SERVICE_REMOTE_ROOT=/opt/grok-free-register
export XAI_AUTH_SERVICE_SYNC_SEC=30
```

存在 `XAI_AUTH_SERVICE_SSH_HOST` 时会自动选择 SSH。也可显式设置
`XAI_AUTH_SERVICE_SOURCE=local|ssh`；默认值 `auto` 优先兼容已有 SSH 配置，否则使用同机来源。

启动认证服务：

```bash
bash auth-service.sh
```

需要查看队列、重试、节拍和冷却探针时使用：

```bash
bash auth-service.sh --debug
```

默认每次认证至少间隔 10 秒；实测该值比无间隔运行有更高的长期平均成功速率。限流后
每 60 秒只进行一次恢复探测。需要覆盖时使用：

```bash
export XAI_AUTH_SERVICE_MIN_INTERVAL_SEC=10
export XAI_AUTH_SERVICE_RETRY_SEC=60
```

终端只在账号开始、认证结果、限流状态或控制状态变化时输出，并在底部保持 `认证> ` 输入行。`s` 查看状态，`p` 暂停，
`r` 恢复，`c` 取消当前账号，`q` 退出；`Ctrl-C` 同样会退出。在输入行键入 `take 100`
会把最新的 100 个可用凭证登记为已取用，并移动到独立批次目录。认证记录仍保留为
`imported`，不会因为凭证被取出而重新认证。

可用凭证保存在：

```text
~/Downloads/grok-free-register-auth/authenticated/
```

已取用批次保存在：

```text
~/Downloads/grok-free-register-auth/claimed/<batch-id>/
```

库存状态保存在 `enrollment-ledger.db` 的 `credential_inventory` 表中，状态为
`available`、`claiming` 或 `claimed`。每条记录预留 `note` 字段，默认留空。

`auth-service.sh` 首次运行会自动安装项目依赖。正式用户流程只需要这个 Bash 入口；底层 Python 模块保留给开发和测试，不作为另一套使用方式。

## 开发文档

[docs/architecture.md](docs/architecture.md) 记录并发模型、资源生命周期和必须保持的不变量。

## License

MIT
