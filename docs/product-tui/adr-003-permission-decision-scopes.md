# ADR-003：权限决定范围与精确持久规则

- 状态：Accepted
- 日期：2026-09-01
- 决策范围：产品 CLI、HTTP 转发层、TypeScript permission canonical owner

## 背景

Codex 的权限交互允许用户区分“本次允许”和更长范围的允许。Zyra 此前只有一次性 `permission.respond`，另有独立的管理级 rules mutation API。由 CLI 先批准、再单独修改规则会产生竞态；只在前端增加“会话允许”或“始终允许”按钮则会显示后端并未正式兑现的能力。

现有 permission rule matcher 支持通配模式。把原始参数 JSON直接写为模式并不能证明精确匹配，因为参数值本身可能包含 `*` 或 `?`；规则也不应持久保存 credential 或其他敏感参数原文。

## 决策

1. `permission.respond` 正式支持 `once`、`session` 和 `workspace` 三种 `decision_scope`。拒绝决定只允许 `once`。
2. 公开 request 逐项声明 `supported_decision_scopes`；CLI 只显示该请求实际声明的选项，不自行推断能力。
3. `zyra.permission-response/v2` 把 `decision_scope` 纳入响应证明。v1 只作为 `once` 兼容格式接收，不能用于会话或工作区授权。
4. `session` 和 `workspace` 只创建窄范围 allow rule：同一 workspace、tool、namespace、server、command/resource、operation 以及 canonical arguments 必须完全一致；`session` 还必须是同一 session。
5. 精确匹配使用上述 canonical binding 的 SHA-256 digest。持久规则不保存参数或 workspace 原文；scope pattern 只用于可解释筛选，digest 是防止通配符扩大权限的最终绑定。
6. 响应证明、request identity、effect、scope、expiry 和 policy revision 全部验证后，TypeScript canonical owner 在同一同步临界段恢复一次性 continuation 并安装预校验规则。规则冲突时在消费 approval 前失败关闭。
7. `workspace` 规则进入既有 E02 原子 checkpoint，daemon 重启后恢复；`session` 规则也可随 checkpoint 恢复，但永远受原 session identity 约束。
8. 回执明确返回 scope、规则 ID、是否新安装、是否持久和 policy commit revision；不返回原始 arguments、workspace root 或 secret。

## 安全与优先级

- managed、policy、user 和 project 规则继续按既有 precedence 覆盖 operator session/workspace allow；持久授权不能越过上级 deny/ask。
- 参数中包含通配字符时仍按 digest 精确匹配。
- custody 丢失、scope 不支持、v1 scope 扩大、证明篡改、expiry、revision drift 或规则 ID 冲突均 fail closed。
- Python API 只转发已认证 operator 的 scope 和证明，不成为 permission decision owner。

## 验证

- v2 scope tamper 与 v1 once-only proof tests；
- session 相同调用复用、跨 session 拒绝以及含 `*` 参数不能扩大匹配；
- workspace rule 在 E02 runtime restart 后仍精确生效；
- CLI 仅呈现后端声明范围并把范围绑定进 proof；
- HTTP custody 路径把 session scope 转发至 TypeScript owner 并返回正式 rule receipt。

## 后果

产品 TUI 可以诚实提供三种范围，而不需要调用独立的管理规则界面。该设计比工具级宽泛 allow 更保守；用户若改变参数仍会再次收到审批，这是有意的安全边界，而不是匹配缺陷。
