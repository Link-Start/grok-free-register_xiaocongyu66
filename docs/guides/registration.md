# 注册教程

## 开始运行

```bash
git clone https://github.com/hechuyi/grok-free-register.git
cd grok-free-register
bash start.sh
```

首次运行会安装 Python、CloakBrowser Chromium 及其系统依赖，然后引导选择邮箱模式。以后再次执行 `bash start.sh` 会直接使用已有配置。

普通模式只显示服务启动、任务开始、注册成功或失败、本次运行平均速率、累计数量和限流状态。查看完整并发、库存和阶段耗时时使用：

```bash
bash start.sh --debug
```

常用参数：

```bash
bash start.sh --target 100
bash start.sh --max-mem 6G
bash start.sh --reconfig
```

未设置 `--target` 时服务持续运行，按 `Ctrl-C` 安全停止。
再次执行 `bash start.sh` 即可重启。程序直接使用当前终端，不需要额外的会话管理工具。

## 配置邮箱

临时邮箱无需额外配置：

```env
EMAIL_MODE=tempmail
```

MoeMail OpenAPI 模式需要 MoeMail 地址和 API Key：

```env
EMAIL_MODE=moemail
MOEMAIL_API=https://moemail.app
MOEMAIL_API_KEY=mk_xxx
# MOEMAIL_DOMAIN=moemail.app
```

`moemail` 模式只使用 MoeMail，不会 fallback 到其它临时邮箱 provider。
如果需要 Grok/xAI 代理池，在项目目录创建 `代理.txt`，一行一个 `http://` 或 `socks5://` 代理即可；未显式设置时也兼容读取 `proxy.txt`。代理池只作用于 xAI 注册、发码、提交和自动 OAuth 转换，MoeMail 等邮箱 HTTP 默认直连。

`代理.txt` 也可以直接写 `vmess`、`vless`、`trojan`、`ss`、`hy2`、`hysteria2`、`tuic`、`anytls` 分享链接。默认会启用内置 sing-box relay，把分享链接转成本地代理；如果你已有外部 proxy-relay，也可以继续使用 `http://127.0.0.1:18080`：

```env
PROXY_RELAY_ENABLED=1
PROXY_RELAY_BUILTIN_ENABLED=1
PROXY_RELAY_AUTO_INSTALL=1
PROXY_RELAY_URL=http://127.0.0.1:18080
# PROXY_RELAY_KERNEL=auto
```

需要自动补充节点时，可以打开本地自动节点池：

```env
PROXY_AUTO_FETCH_ENABLED=1
PROXY_POOL_STRATEGY=random
PROXY_AUTO_FETCH_WORKERS=16
PROXY_AUTO_TEST_WORKERS=32
```

开启后程序会多线程拉取订阅源、多线程测试节点，默认每 20 分钟刷新一次。测试目标默认是 xAI 注册页，只保留可访问测试 URL 的代理；失效代理会在下一轮刷新时从自动池删除，上一轮可用的自动代理也会重新测试，避免源站临时失败时直接清空可用节点。自动池会和 `代理.txt` 里的手动代理混合轮换使用。

订阅源可写在 `PROXY_AUTO_FETCH_URLS`，也可放到项目目录的 `proxy-sources.txt`，一行一个 URL。带 `*` 前缀表示该 URL 返回的是订阅源列表，程序会再展开一层。拉取订阅时会轮换使用当前已有代理和上一轮可用代理，尽量避免所有源站请求走同一个出口。

自动节点池输出在 `logs/` 下：

```text
logs/proxy-auto-active.txt
logs/proxy-auto-state.json
logs/proxy-auto-sub2api.json
logs/proxy-auto-cpa.json
```

`proxy-auto-active.txt` 会自动混入运行时代理池；`sub2api` 和 `cpa` JSON 用于导入支持同类数据格式的面板。

成功注册后，程序会把 SSO 会话交给内置 `xai_enroller` 自动换成 Grok OAuth 凭据。输出格式由 `KEY_EXPORT_FORMATS` 选择：

```env
KEY_EXPORT_DIR=keys
KEY_EXPORT_FORMATS=legacy,sub2api
```

可选值支持 `legacy`、`sub2api`/`sub`、`cpa`，例如 `KEY_EXPORT_FORMATS=sub2api,cpa`。`sub2api` 文件写入 `keys/sub2api/`；CPA 会写入单账号 `xai-*.json`，仅写单账号 `keys/cpa/xai-*.json`（无合并包）。`keys/auth-sessions.jsonl` 会保留为自动认证恢复源。

如果邮箱服务接口或 xAI HTTP 接口偶发 Cloudflare 拦截，可以开启 CF-Ares 兜底；项目已内置 `vendor/CF-Ares`，默认会从本地源码安装和优先导入：

```env
CF_ARES_EMAIL=fallback
CF_ARES_XAI=fallback
```

自建邮箱需要可接收邮件的域名和本项目的收信服务：

```env
EMAIL_MODE=custom
EMAIL_DOMAIN=example.com
EMAIL_API=http://127.0.0.1:8080
```

自建模式还需运行：

```bash
bash start.sh --email-service
```

性能参数默认会根据 CPU 和可用内存估算。除非正在压测，否则保持 `.env.example` 中的默认值即可。

成功结果写入 `keys/`；默认会有 `accounts.txt`、`grok.txt`、`auth-sessions.jsonl` 和 `sub2api/`。启用 `cpa` 时，这些文件默认不提交到 Git。
