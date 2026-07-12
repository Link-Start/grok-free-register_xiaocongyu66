# grok-free-register

`grok-free-register` 是一个命令行注册工具。程序会启动本机浏览器，完成页面操作、邮箱验证码处理和结果保存。

运行结果写入 `keys/` 目录。

## 快速开始

```bash
git clone https://github.com/hechuyi/grok-free-register.git
cd grok-free-register
bash start.sh
```

首次运行会自动创建 `.venv`、安装依赖，并引导生成 `.env`。

需要完整说明时，按用途查看：

- [注册教程](docs/guides/registration.md)
- [本地认证服务](docs/guides/auth-service.md)
- [凭据库存与取用](docs/guides/credential-inventory.md)
- [运行状态与排障](docs/guides/runtime-troubleshooting.md)

常用命令：

```bash
bash start.sh               # 按当前 .env 前台运行
bash start.sh --target 100  # 成功 100 个后停止
bash start.sh --max-mem 6G  # 自动估算并发时最多使用 6G 内存
bash start.sh --reconfig    # 重新选择邮箱模式
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

`代理.txt` 也支持 `vmess`、`vless`、`trojan`、`ss`、`hy2`、`hysteria2`、`tuic`、`anytls` 分享链接。使用分享链接时需先启动本机 proxy-relay 服务，默认调用 `http://127.0.0.1:18080` 的 `/api/state` 和 `/api/nodes/import`，导入后会自动使用对应本地端口。

需要自动节点池时开启本地开关：

```env
PROXY_AUTO_FETCH_ENABLED=1
```

开启后程序会多线程拉取订阅、多线程测试节点，每 20 分钟刷新一次，只保留能访问 `PROXY_AUTO_TEST_URLS` 的代理；上一轮可用的自动代理会在下一轮继续复测，避免源站临时失败时直接清空。自动代理会和 `代理.txt` 里的手动代理混合轮换使用。拉取订阅时会用当前已有代理做轮换请求，避免所有源站请求都走同一个出口。可在 `proxy-sources.txt` 里追加订阅源，一行一个 URL；带 `*` 前缀表示这个 URL 返回的是“订阅源列表”。自动导出文件在 `logs/` 下，支持 raw、base64、sub2api/cpa 导入 JSON。

注册成功后默认会把 SSO 会话交给本项目内置的 `xai_enroller` 自动换成 Grok OAuth 凭据，并写入 `keys/sub2api/`。需要 CPA 文件时设置：

```env
KEY_EXPORT_FORMATS=legacy,sub2api,cpa
```

只想保留某一种最终格式也可以设置为 `sub2api`、`cpa` 或 `legacy`。`sub` 会作为 `sub2api` 的别名处理。

外部邮箱接口如果偶发 Cloudflare 拦截，可开启 CF-Ares 兜底。`cf-ares` 已随默认依赖安装：

```env
CF_ARES_EMAIL=fallback
```

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
| `KEY_EXPORT_FORMATS` | `legacy,sub2api` | 输出格式，支持 `legacy`、`sub2api`/`sub`、`cpa` |
| `KEY_EXPORT_ENROLLER` | `1` | 是否自动调用 `xai_enroller` 把 SSO 转 OAuth 后导出 |
| `PROXY_POOL_FILE` | `代理.txt` | 可选 Grok/xAI 代理池文件，一行一个 `http`/`socks5` 代理或节点分享链接 |
| `PROXY_POOL_STRATEGY` | `round_robin` | 代理选择方式，支持 `round_robin` 和 `random` |
| `PROXY_RELAY_ENABLED` | `1` | 是否把节点分享链接交给本机 proxy-relay 转成本地代理 |
| `PROXY_RELAY_URL` | `http://127.0.0.1:18080` | proxy-relay 管理 API 地址 |
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
| `CF_ARES_EMAIL` | `0` | 可选邮箱 HTTP 兜底，`fallback` 遇到 Cloudflare 拦截时重试，`always` 始终使用 |
| `CF_ARES_BROWSER_ENGINE` | `auto` | CF-Ares 浏览器引擎，支持 `auto`、`undetected`、`seleniumbase` |
| `CF_ARES_HEADLESS` | `1` | CF-Ares 浏览器是否无头运行 |
| `CF_ARES_PROXY` | 空 | CF-Ares 代理，留空沿用 `HTTPS_PROXY`/`HTTP_PROXY` |
| `CF_ARES_PATH` | 空 | 可选本地 CF-Ares 源码目录，不设置则只使用 pip 包 |
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
| `PAGE_BLOCK_STATIC_ASSETS` | `0` | 可选：阻断部分静态资源，降低页面准备成本 |
| `C_HOT_PAGE_POOL` | `0` | 可选：复用消费阶段页面，减少页面重建开销 |

不确定怎么设置时，先保持默认值。性能压测时优先观察 `PHYSICAL_CAP` 和内存，不建议先改 Worker 数量。

## 运行日志

直接运行 `bash start.sh` 时，终端只输出任务开始、成功或失败、本次运行平均速度、累计数量和限流等待：

```text
[→] 开始注册 #38
[✓] 注册成功 #38 | 运行平均 9.9/分 | 累计 38
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

成功结果写入 `keys/`。默认包括：

```text
keys/accounts.txt
keys/grok.txt
keys/auth-sessions.jsonl
keys/sub2api/accounts.sub2api.json
keys/sub2api/xai-*.sub2api.json
```

`accounts.txt` 每行格式：

```text
email:password:sso_token
```

`keys/` 目录包含运行结果，默认不会提交到 Git。

当 `KEY_EXPORT_FORMATS` 包含 `cpa` 时，还会写入：

```text
keys/cpa/xai-*.json
```

## 项目结构

```text
grok_register/              注册核心与 custom 邮箱服务
xai_enroller/               OAuth 认证服务
cloudflare/email-worker.js  Cloudflare Email Routing Worker 示例
start.sh                    首次配置和运行
auth-service.sh             认证服务入口
setup.sh                    安装依赖
.env.example                配置模板
tools/                      运行日志分析工具
tests/                      自动化测试
docs/architecture.md        并发架构说明
```

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

## xAI OAuth Enroller

`xai_enroller/` 用于把已有 xAI 账号的 SSO 会话转换为 OAuth 凭据，并导入 CPA
凭据库。它独立于注册流程运行：注册机不需要启动，已有账号也不需要重新注册。

### 默认同机模式

注册和认证在同一个项目目录运行时，无需配置来源：

```bash
bash auth-service.sh
```

认证服务默认读取本项目的 `keys/auth-sessions.jsonl` 和历史
`keys/accounts.txt`，每 30 秒生成一次经过校验的原子快照。注册仍可同时运行；认证服务
只读取完整记录，不会读取正在追加的半行。

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
