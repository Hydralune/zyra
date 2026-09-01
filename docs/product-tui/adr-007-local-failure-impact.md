# ADR-007：局部失败与任务终态分层

状态：Accepted
日期：2026-09-01

## 决策

1. `zyra.product-presentation/v1` 的 failure presentation 使用显式 `impact: local | task`。tool、node、subagent、backend dispatch/failover 和 recovery 属于局部执行事实；task execution error 属于任务级问题。
2. tool/worker 局部失败不得写入或推导 `task.failed`。产品 TUI 的整体状态只由 canonical task detail 的 terminal/status 决定。
3. CLI 保留失败 tool/worker 的稳定 identity、code、retryable/recovery，并结合 canonical task 状态显示“任务仍在运行 / 已完成 / 最终失败”，不把单点错误伪装成整体结果。
4. recovery requested/planned/completed 使用有界 activity presentation；原始错误 payload、credential、绝对路径和内部拓扑仍不进入产品状态。
5. Web ingress 对 `impact` 使用白名单校验；未知 impact fail closed，避免后端新增枚举被静默解释。

## 证据

- Python 契约覆盖 tool、node、recovery 和 task execution error 的 scope、边界与脱敏。
- CLI reducer/renderer fixture 覆盖 local failure、recovery 和 canonical completed 同时存在，且不产生 `task.failed`。
- 真实集成通过 `CodeWorkerRuntimeEventIngress` 提交一次 tool failure、recovery signal 和成功 retry，经真实 runtime event spine、HTTP event-ingress、CLI typed client 和产品 projector 后，canonical task 完成且 TUI 明示局部失败。

## 后果

单个工具、worker 或调度尝试失败可以高可见展示，但不会改变任务整体语义。若局部失败最终导致任务失败，后续 canonical task terminal 仍会独立显示整体失败及恢复入口。
