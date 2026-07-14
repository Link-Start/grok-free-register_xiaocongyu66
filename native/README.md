# native — 多语言二进制

| 目录 | 语言 | 角色 |
|------|------|------|
| `inventory-worker/` | Go | 账号库存、**SSO→CPA 协议授权**（sso_build）、CPA/sub2api 文件 |
| `proxy-worker/` | Go | 代理测活 |
| `register-worker/` | Go | HTTP 协议注册 worker（可选） |
| `solver-gateway/` | Go | Hybrid Turnstile 网关（默认） |
| `solver-hybrid/` | Python | Turnstile browser worker（仅 token） |
| `solver-watchdog/` | Rust | Hybrid 内存看门狗 |
| `solver-util/` | C++ | 压力分 / token 形状 |
| `solver-gateway-cpp/` | C++ | **可选** C++ 网关（非默认） |

构建：

```bash
bash scripts/build-native.sh
```

门禁：`bash scripts/polyglot_gate.sh check`
