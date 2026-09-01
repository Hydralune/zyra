# ADR-005：产品会话跨 daemon 重启恢复

状态：Accepted

## 背景

产品 TUI 的 canonical task 不属于 CLI 进程，也不能依赖一次 HTTP/SSE 连接。真实 Windows 故障注入发现，仅有 cursor/snapshot 单元状态机仍不足以恢复：typed transport 熔断器会把 circuit-open 的本地拒绝再次记为网络失败，从而不断后移 cooldown；强杀 daemon 还会留下仍处于 30 秒年龄窗口内的 permission 文件锁，使新 daemon 在 bootstrap migration 中退出。

## 决定

1. `observeProductTask` 将 daemon 暂时不可达视为正常恢复状态。连续 outage 使用最多 100 次、120 秒的有界预算，并以 100ms～2s 退避；收到已验证 canonical frame 后清零连续失败窗口。
2. cursor/generation/binding/gap 或服务端 `resyncRequired` 触发 snapshot replacement。旧进程 cursor 失效时，客户端推进 ingress generation，而不是继续使用旧 cursor 猜测增量。
3. snapshot replacement 同时刷新 presentation、canonical task 和 control-session task binding；恢复后的控制操作不得继续使用重启前的 task projection。
4. permission custody 在连接恢复后通过正式 resume API 重建；resume 失败时保持 fail closed，并显示可诊断错误码。
5. circuit 已 open 时被 `beforeRequest` 本地拒绝的请求不再计作新的物理失败，也不更新 `openedAt`。cooldown 到期后只允许正式 half-open probe。
6. permission 锁继续 fail closed，但锁文件中声明的 OS PID 已确定退出时允许立即接管。Windows 使用 `OpenProcess` 与 `GetExitCodeProcess` 判断 owner；无法检查或 access denied 时不删除锁。
7. 托管 daemon 的 stdout/stderr 写入 CLI state 目录内固定、每次启动截断的 `daemon-startup.log`，使启动失败可诊断且不会形成无界文件集合。
8. canonical `cancelled` 在产品入口映射为公开 `CANCELLED` 退出码 4，不再误归类为通用 task failure。

## 结果

- 同一个 Windows ConPTY TUI 可以经历真实 managed-daemon 强制停止、不同 PID/daemon generation 冷启动、旧 cursor 失效、generation 2 snapshot 重建，再从原 composer 执行 `/status` 和 `/cancel`。
- 任务真相始终来自重启前后共享的 canonical store；TUI 不建立本地 task 副本。
- circuit cooldown、permission 锁和 daemon bootstrap 都有独立回归，避免只在 mock observer 中证明恢复。
- 真实门禁为 `tests/integration/test_product_tui_daemon_restart.py`；它还校验最终 projection revision、cancelled 状态以及 bracketed-paste 成对恢复。
