# 本地认证服务

认证服务把已有的 **SSO 会话** 转为 CPA 可直接使用的 OAuth 凭据（`keys/cpa/xai-*.json`）。  
**仅协议路径**（Go sso_build）；旧版 Playwright 浏览器确认已移除。

## 启动

```bash
bash auth-service.sh
```

流程与 [chenyme/grok2api](https://github.com/chenyme/grok2api) 的 **SSO→Build** 相同：

```text
keys/sso.txt          # 规范 SSO：email:sso（一行一个邮箱，重登会删旧换新）
keys/accounts.txt     # 账密：email:password（供重登）
  → Go inventory-worker 读 sso.txt，多并发 + 可选多 IP
  → POST device/code（scope 含 offline_access）
  → verify + approve
  → POST oauth2/token → access_token + refresh_token
  → keys/cpa/xai-*.json
```

### 刷新 token 从哪来？

| 字段 | 来源 |
|------|------|
| `access_token` | `POST https://auth.x.ai/oauth2/token` 的 device_code 换票响应 |
| **`refresh_token`** | **同上响应**（申请 scope 时带了 `offline_access`，xAI 才会下发） |
| 之后续期 | `grant_type=refresh_token` + 已有 `refresh_token`（`cliproxyapi` 自动做） |

**不是**从 SSO cookie 里“解析”出 refresh；也不是浏览器 enroller。  
SSO 只用来自动完成 Device Flow 的 verify/approve；真正的 OAuth 票在 **token 端点** 一次性拿到。

常用参数：

```bash
bash auth-service.sh --once --limit 200 --workers 16
bash auth-service.sh --sso-file /path/to/old_sso.txt --workers 16
bash auth-service.sh --proxy-file 代理.txt --interval 30
```

环境变量：`CONVERT_WORKERS` / `AUTH_PROTOCOL_WORKERS`、`AUTH_PROTOCOL_LIMIT`、`SSO_CONVERT_PROXY_FILE`。

**Turnstile 不在此阶段**：注册时已解过验证码拿到 SSO；协议授权只靠 SSO cookie。

## 同机运行

注册与认证可同目录并行：注册写 **`keys/sso.txt`**（`email:sso`）；认证默认读该文件中尚未出 CPA 的号（**最新优先**）。

### SSO 过期：重登

账密在 `keys/accounts.txt`（`email:password`）。重登成功后**删除该邮箱旧 SSO 行**，写入新的 `email:sso`：

```bash
bash scripts/sso-relogin.sh --limit 50 --workers 2
bash scripts/sso-relogin.sh --only-without-cpa --limit 100 --workers 2 --convert
```

## 配置远端同步

先把无密码导出器放到服务器项目目录：

```bash
scp scripts/export_registered_sessions.py user@server.example:/opt/grok-free-register/scripts/
```

在本地终端设置连接信息：

可以直接 `export`，也可以把 `.env.example` 复制为 `.env` 后填写；认证入口会自动读取 `.env`。

```bash
export XAI_AUTH_SERVICE_SSH_HOST=user@server.example
export XAI_AUTH_SERVICE_SSH_IDENTITY=/path/to/key.pem
export XAI_AUTH_SERVICE_REMOTE_ROOT=/opt/grok-free-register
```

使用 `ssh-agent` 时可省略 `XAI_AUTH_SERVICE_SSH_IDENTITY`。

设置了 `XAI_AUTH_SERVICE_SSH_HOST` 后，默认的 `auto` 模式会选择 SSH。需要明确覆盖时使用：

```bash
export XAI_AUTH_SERVICE_SOURCE=local  # 强制读取同机注册结果
export XAI_AUTH_SERVICE_SOURCE=ssh    # 强制使用 SSH，必须配置主机
```

## 运行

```bash
bash auth-service.sh              # 守护轮转
bash auth-service.sh --once       # 只转一轮
```

首次运行会自动安装项目依赖（含 Go inventory-worker）。

- 实时进度条（与 `python -m grok_register.sso.export convert` 相同）
- 默认 SSO 源：`keys/sso.txt`（`email:sso`）
- `Ctrl-C` 完成本轮后退出
- 成功：`keys/cpa/xai-*.json`（含 `refresh_token`），并镜像到 Downloads `authenticated/`
