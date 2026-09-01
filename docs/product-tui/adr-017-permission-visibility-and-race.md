# ADR-017：权限决定必须高可见并核对 canonical winner

状态：已接受

日期：2026-09-01

## 背景

旧主视图把权限请求作为普通 transcript 行，长 diff/verification 可能把它推出视口；界面提示 `[A]/[D]`，控制循环却没有相应决定路径。并发的相反决定还揭示了一个更危险的语义：canonical owner 会把已接受决定作为 replay 返回，若 CLI 只看 HTTP 成功，就可能把用户请求的 deny 错报为成功，而 receipt 实际是 allow。

## 决策

- 权限卡置于 workspace/diff/verification 之后、composer 之前，并用边框显示动作、目标、原因、风险、scope、expiry 和完整 request identity；最多展开 3 个，其余要求进入 picker。
- `[A]+Enter`/`[D]+Enter` 只在产品状态显示权限且 canonical pending 集合恰好有一个 selectable request 时可用，只提交 `once`。零个或多个请求均 fail closed。
- `CliPermissionSession.resolve` 必须验证 receipt `accepted/effect/decision_scope/request_id` 与用户决定完全一致。
- 并发 loser 即使收到 canonical replay 的成功 HTTP 响应，只要 winner 的 effect/scope 不同，就报告 `permission_decision_conflict`，标记 `automatic_retry=false`，不伪报也不重放。

## 验证

- 24 行视口与 200 行 diff 压力下，权限动作和决定快捷键仍可见。
- 单元竞争验证两个相反决定仅一个成功，并验证 shortcut 的单请求绑定与多请求拒绝。
- 真实 daemon、真实 TypeScript permission owner 与两个 CLI custody client 并发提交 allow/deny；canonical pending 收敛为 0，恰好一个客户端接受 winner，另一个识别 replay mismatch 并返回 `permission_decision_conflict`。
