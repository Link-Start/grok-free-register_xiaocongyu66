# 凭据库存与取用

认证成功的凭据保存在本地认证目录，并在 SQLite 库存中维护三种状态：

- `available`：已经认证、尚未取用；
- `claiming`：正在移动到取用批次；
- `claimed`：已经取用。

运行认证服务后输入：

```text
take 100
```

服务会选择最新的 100 个可用凭据，移动到 `claimed/<batch-id>/`，再把对应库存标记为 `claimed`。认证 ledger 中的 `imported` 记录仍然保留，所以这些账号不会被重新认证。

每条库存记录预留 `note` 字段，默认为空。取用操作不要求填写用途；以后需要备注时可直接更新该字段。

若文件移动中断，服务下次启动会恢复 `claiming` 状态。库存不足或凭据文件缺失时，操作失败但认证服务继续运行。

## 控制面板成品与状态

Web 控制面板（`bash start.sh --dashboard`）会扫描 `KEY_EXPORT_DIR`（默认 `keys/`）并汇总：

| 状态 | 含义 |
|------|------|
| `oauth_ready` | 已有 sub2api / CPA OAuth 凭据 |
| `oauth_pending` | 有 SSO（`keys/sso.txt` 或 legacy），尚未转换成 OAuth |
| `legacy_sso` | 仅 SSO，无 OAuth 文件 |

磁盘上的规范文件：

| 文件 | 格式 | 用途 |
|------|------|------|
| `keys/sso.txt` | `email:sso` | convert / auth-service 的 SSO 源（一邮箱一行） |
| `keys/accounts.txt` | `email:password` | 重登账密 |
| `keys/cpa/xai-*.json` | CPA 单账号 | 成品（含 refresh_token） |

成品下载：

- `GET /api/download?format=sub2api` → `accounts.sub2api.json`
- `GET /api/download?format=cpa_zip` → `xai-singles.zip` (xai-*.json only)
- `GET /api/download?format=legacy` → `accounts.txt`（账密；SSO 见 `sso.txt`）

账号列表：`GET /api/accounts`。重建合并包：`POST /api/action` body `{"action":"rebuild_bundles"}`（无需开启 `CONTROL_PLANE_ALLOW_ACTIONS`）。
