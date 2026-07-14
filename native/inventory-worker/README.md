# inventory-worker (Go)

账号库存扫描、CPA / sub2api 合并包，以及 **SSO → OAuth 协议转换**。  
原 Rust 实现已用 Go 重写；CLI 与 JSON 契约兼容（`engine` 字段为 `go`）。

## 构建

```bash
cd native/inventory-worker
go build -trimpath -ldflags="-s -w" -o inventory-worker .
# 或: bash scripts/build-native.sh
```

## 命令

```bash
./inventory-worker version
./inventory-worker check --keys-dir keys
./inventory-worker scan --keys-dir keys --json
./inventory-worker rebuild --keys-dir keys
./inventory-worker convert --keys-dir keys --formats cpa,sub2api
./inventory-worker convert --keys-dir keys --formats cpa,sub2api --pending --enroll --workers 4
```

| 命令 | 作用 |
|------|------|
| `scan` | 扫描 `accounts.txt` / sub2api / cpa，输出库存 JSON |
| `rebuild` | 合并 `accounts.sub2api.json`，清理 CPA 合并包残留 |
| `convert` | OAuth 文件互转；`--enroll` 时用协议 SSO→OAuth |
| `check` | 门禁自检（确保 keys 目录可用） |

## 转换路径

1. **oauth_copy**：已有 CPA 或 sub2api → 互转（无网络）
2. **protocol_enroll**（`--enroll`）：对齐 [grok2api `sso_build`](https://github.com/chenyme/grok2api) / ZhuCe `vault_oauth`：
   ```
   GET accounts.x.ai → POST device/code → GET verify URL
   → POST device/verify → POST device/approve(action=allow) → poll token
   ```
   写出 CPA `xai-*.json` 与 sub2api。SSO 失效或被拒时返回明确错误。

纯协议**注册**仍由 `native/register-worker` 负责；本二进制负责库存与转换。

## 环境变量

| 变量 | 说明 |
|------|------|
| `KEY_EXPORT_DIR` | 默认 keys 目录 |
| `keys/sso.txt` | 规范 SSO 源：`email:sso`（convert 默认读取） |
| `XAI_ENROLLER_SOURCE_SALT` | 文件名 HMAC salt（可选，缺则自动生成） |
| `HTTPS_PROXY` / `HTTP_PROXY` | 协议 enroll 代理 |

## 与 Python

`grok_register.polyglot` / `account_inventory.ensure_bundles` / `account_convert` 默认调用本二进制。  
`INVENTORY_ENGINE=python` 可强制走 Python 回退。
