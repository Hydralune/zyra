# ADR-009：集中终端会话恢复与异常边界

## 背景

产品 composer 过去直接切换 raw mode 和 bracketed paste。正常提交会恢复，但 idle 状态收到进程信号时，主流程已 abort、composer 仍阻塞在输入 Promise，终端清理无法到达。顶层未捕获异常也没有统一的终端恢复入口。

Codex 的 `custom_terminal.rs`、`tui.rs` 和 startup replay 测试表明，终端能力启停应由生命周期对象拥有，并在启动时重放安全状态，而不是散落在按键处理代码中。

## 决定

1. `TerminalSessionGuard` 统一拥有 raw mode、stdin pause/resume、光标和 bracketed paste 生命周期；composer 只调用 `enter()` / `restore()`。
2. 所有活动 guard 登记到进程内 registry。CLI 顶层在正常 `finally`、`exit` 和 `uncaughtExceptionMonitor` 路径执行幂等的 emergency cleanup。
3. signal abort 主动关闭 composer input，使 idle TUI 不再永久等待；未绑定 task 的中断返回 `CANCELLED/cancelled`，不伪装为正常退出。
4. 每次进入交互模式先输出 reset、show-cursor、paste-off 的安全基线，再启用 bracketed paste。这样上一次进程若被不可捕获强杀，下次 Zyra 启动可自愈陈旧 escape-mode 状态。
5. 外部编辑器启动前复用同一 restore，返回后重新 enter，避免形成第二套终端状态逻辑。

## 验证

- 组件测试验证 abort 后 Promise 收敛、退出码正确、raw/paste/cursor 恢复，以及 emergency cleanup 重复调用幂等。
- Windows 真实 ConPTY fixture 在进入 raw/bracketed-paste 后抛出未捕获顶层异常；验证非零退出、崩溃前安全基线、崩溃后 paste-off/show-cursor、未使用 alternate screen。
- 既有异步 resize/Unicode ConPTY 用例与 CLI 全量测试作为回归门。

## 不可消除的平台边界

`SIGKILL`、Windows `TerminateProcess`、断电和终端宿主崩溃不会执行目标进程的 `finally`、exit hook 或 JavaScript handler。任何用户态 CLI 都不能诚实保证在这些事件发生前发送恢复 escape sequence。本实现提供两层可验证保护：可捕获退出在原进程内恢复；不可捕获退出后由终端宿主 teardown 及下一次 Zyra 启动的安全基线重放恢复。

因此，当前证据关闭受控 fatal crash 缺口，但不把它冒充为 OS 强杀。TERM-04 在完成独立 100 轮 Windows 宿主/强杀恢复门前仍标记为部分实现。
