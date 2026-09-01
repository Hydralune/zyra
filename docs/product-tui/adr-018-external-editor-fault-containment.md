# ADR-018：外部编辑器故障不得终止产品会话

## 决定

`Ctrl+E` 在启动外部编辑器前恢复终端安全基线，并在外部进程结束后重新进入 composer 所需的终端模式。只有退出码为零且草稿文件可读时才替换内存草稿。

编辑器缺失、启动失败或返回非零状态时，TUI 显示可执行错误，保留调用前草稿，并继续接受输入；该局部工具故障不得拒绝产品输入循环或结束远端 task 观察。

## 原因

外部编辑器由用户环境提供，不属于 canonical task owner。把编辑器失败升级为 TUI/session failure 会使长期任务失去控制面，也违反 Codex 中外部编辑器是可恢复 composer 操作的产品语义。

## 证据

- 组件测试覆盖成功替换、失败保留以及失败后的继续输入和提交。
- Windows ConPTY 使用真实 built CLI 与继承 PTY 的 Node 编辑器进程，分别验证成功编辑和退出码 7；两条路径均恢复 composer、正常 `/exit`，无 alternate screen 或残留 bracketed-paste mode。
