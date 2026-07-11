# 本地认证服务（暂缓）

注册机与认证服务保持分离：AWS 只持续注册，并保留原始
`email:password:sso` 账户文件；本地服务通过 SSH 读取时只接收邮箱和 SSO，
绝不传输或落盘密码。

本地服务复用 `xai_enroller` 的既有 Device Flow、浏览器确认与 CPA sink。它是
常驻交互终端，内部轮询可以静默进行；仅在发现新账号、Device Flow 进入新阶段、
需要人工确认、认证完成、投递成功或失败时输出事件。计划提供单次状态、暂停、
恢复、取消当前批次和优雅退出命令。

实现前置条件：先稳定修复注册机当前的失败问题。恢复此项工作时，应先完成
`EnrollmentCoordinator.run_records(records)` 的最小重构，再接入 SSH 导出器和
终端控制层；不得同步密码或在日志输出 SSO/OAuth 凭证。
