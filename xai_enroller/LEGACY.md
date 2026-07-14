# xai_enroller — 遗留模块

**状态：遗留 / 非默认路径**

当前生产认证入口是：

```bash
bash auth-service.sh
# → python -m grok_register.sso.auth_service
# → Go inventory-worker protocol SSO→CPA
```

本目录仍保留：

- 单元测试（`tests/test_xai_enroller_*`）
- 部分 sinks / models / fingerprints 被库存或其它代码引用

**不要**再通过 `python -m xai_enroller.service` 作为日常认证路径。  
Playwright Device Flow 确认已从 `auth-service.sh` 移除。

若需浏览器指纹工具，见 `xai_enroller.fingerprints`。
