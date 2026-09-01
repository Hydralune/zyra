# ADR-011：权限策略模式由当前 canonical permission session 拥有

状态：已接受  
日期：2026-09-01

## 背景

Codex TUI 通过 `permissions_menu.rs`、`permission_popups.rs` 和相应 snapshot，让审批策略既可发现又与当前会话同步。Zyra 已有更严格的 TypeScript `PermissionCoordinator`，但产品 CLI 先前只处理单次请求，并把 standard/sealed 任务执行形态称作 `/mode`；这不能替代独立的 permission policy mode。

## 决策

- `/mode` 继续只配置后续 task 的 standard/sealed 执行形态；`/permissions mode` 管理当前 task 绑定的 canonical permission session，二者不混用。
- 模式读写必须携带 task/run/session identity 与 bearer custody。CLI 不建立本地权限真相。
- picker 只展示后端已有且语义可准确解释的 `default`、`acceptEdits`、`dontAsk`、`plan` 和 `auto`。
- `bypassPermissions` 仅在 canonical state 明确返回 `bypassAvailable=true` 时显示。它仍不能绕过 immutable deny 或 hook。
- `sealed` 不作为普通 picker 选项；若当前已经 sealed，则只读显示，并明确只能由 managed override 退出，避免把正在运行的 task 锁进不可逆状态。
- 更新携带 `expected_revision`。成功响应必须校验 transition receipt，并回读 canonical mode/revision；响应丢失时只做只读对账，不自动重放 mutation；冲突时展示实际 mode/revision 并要求用户重新选择。
- permission session 在一个 task 的运行与收敛后保持同一 custody claim，使用户在 task 结束后的 composer 中仍可查看模式和待处理请求。

## 结果

权限模式成为可审计的会话控制，而不是本地开关。UI 能解释每种模式的真实决策边界，同时保留 Zyra 对 custody、revision、managed bypass 和 sealed autonomy 的强安全约束。
