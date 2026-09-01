# ADR-009：集中终端会话恢复与异常边界

## 背景

产品 composer 过去直接切换 raw mode 和 bracketed paste。正常提交会恢复，但 idle 状态收到进程信号时，主流程已 abort、composer 仍阻塞在输入 Promise，终端清理无法到达。顶层未捕获异常也没有统一的终端恢复入口。

Codex 的 `custom_terminal.rs`、`tui.rs` 和 startup replay 测试表明，终端能力启停应由生命周期对象拥有，并在启动时重放安全状态，而不是散落在按键处理代码中。

## 决定

1. `TerminalSessionGuard` 统一拥有 raw mode、stdin pause/resume、光标和 bracketed paste 生命周期；composer 只调用 `enter()` / `restore()`。
2. 所有活动 guard 登记到进程内 registry。CLI 顶层在正常 `finally`、`exit` 和 `uncaughtExceptionMonitor` 路径执行幂等的 emergency cleanup。
3. signal abort 主动关闭 composer input，使 idle TUI 不再永久等待；未绑定 task 的中断返回 `CANCELLED/cancelled`，不伪装为正常退出。
4. 每次进入交互模式先输出 reset、show-cursor、paste-off 的安全基线。Linux/macOS 路径随后启用 bracketed paste；Windows 产品路径不启用可跨强杀残留的 bracketed-paste 模式，改由 composer 的有界 paste-burst 状态机识别 Windows 终端产生的快速按键流。显式 bracketed-paste 序列仍可解析。
5. 外部编辑器启动前复用同一 restore，返回后重新 enter，避免形成第二套终端状态逻辑。
6. paste-burst 采用 8ms 字符窗口、60ms Windows 空闲 flush 和 120ms Enter 抑制窗口；ASCII 首字符短暂 hold，非 ASCII/IME 首字符立即显示，形成可信 burst 后才回收前缀。输入总量继续受 256KiB composer 上限约束。
7. 启动时进行不消费输入的 bounded capability probe：只读取 TTY flags、窗口尺寸、`WT_SESSION`/`TERM_PROGRAM`/`TERM`/颜色环境与 Node color depth，产出版本化 inline/color/Unicode/paste 能力。Windows 与 Codex 一样避免通过终端应答读取共享输入队列；尺寸和标识均有上限，unknown/dumb/redirected 环境保守降级。

## 验证

- 组件测试验证 abort 后 Promise 收敛、退出码正确、raw/paste/cursor 恢复，以及 emergency cleanup 重复调用幂等。
- Windows 真实 ConPTY fixture 进入 raw mode 后抛出未捕获顶层异常；验证非零退出、崩溃前安全基线、崩溃后 paste-off/show-cursor、未使用 alternate screen。
- Windows 真实 ConPTY 连续 100 次使用 `TerminateProcess` 级强杀；验证产品路径从未开启 bracketed paste、每轮先重放 paste-off、安全关闭 ConPTY、宿主恢复可见光标且未使用 alternate screen。
- 快速 ASCII、非 ASCII/IME、包含换行和超过 256KiB 的无 bracketed paste 流由状态机与 composer 测试覆盖；异步重绘 ConPTY 以 1ms 字节流验证 Unicode 粘贴不丢失、不提前提交。
- 既有异步 resize/Unicode ConPTY 用例与 CLI 全量测试作为回归门。
- capability contract 测试覆盖 Windows Terminal、redirected/dumb、尺寸钳制、颜色 override 和 typeahead 不被消费；`/status` 与首次引导显示实际选择的渲染/paste 路径。

## 不可消除的平台边界

`SIGKILL`、Windows `TerminateProcess`、断电和终端宿主崩溃不会执行目标进程的 `finally`、exit hook 或 JavaScript handler。任何用户态 CLI 都不能诚实保证在这些事件发生前发送恢复 escape sequence。本实现提供三层保护：可捕获退出在原进程内恢复；Windows 主路径不启用可持久残留的 paste mode；不可捕获退出依赖 OS/ConPTY 回收 raw console state，并在下次启动重放安全基线。

这里的 100 轮证据明确覆盖真实 Windows ConPTY 与强杀边界，但不扩张为“断电或终端宿主自身崩溃可由 Zyra 进程清理”。后两者没有仍在运行的 Zyra 控制面，只能由终端/OS 的启动恢复语义处理。
