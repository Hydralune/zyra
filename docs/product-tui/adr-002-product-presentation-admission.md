# ADR-002：产品事件准入与 `ZyraUiEvent/v2`

- 状态：Accepted
- 日期：2026-09-01
- 决策范围：Phase D～H

## 背景

真实 task ingress 表明，`runtime.agent.message` 同时承载助手内容、workspace 通知、worker-pool 兼容记录和 task execution 状态。事件名本身不能证明消息面向用户；把 generic summary 直接显示为助手回答会泄露内部实现，也会错误表达任务状态。

## 决策

1. canonical runtime spine 继续拥有原始事实，event ingress 保持只读。
2. event ingress 可为经过白名单准入的 canonical event 附加 `zyra.product-presentation/v1`；该字段不是新的执行真相，也不能回写 runtime。
3. 准入规则只复制明确 schema 中的低熵字段，并进行长度限制、credential redaction 和物理路径脱敏。
4. generic `runtime.agent.message`、route、lease、heartbeat 和未知新事件默认不进入产品流。
5. CLI 只消费版本匹配的 presentation；未知 kind 被忽略，而不是降级为 raw summary。
6. 产品 reducer 使用 `zyra.ui-event/v2`。v2 增加 activity category/severity、tool duration/artifact、worker summary 和 task issue；旧 task 仍从 canonical task + runtime frames 重新投影，不依赖持久化的 v1 UI state。
7. task terminal state、final answer、permission custody、workspace delivery 和 verifier 仍分别以现有 canonical API/snapshot 为准，presentation 不覆盖这些所有权。
8. `stdout` / `stderr` 正文继续由 runtime artifact owner 持有；presentation 只准入 artifact identity、stream、脱敏标题、media type 和有上限的字节数。CLI 通过既有 server-redacted range API 按需读取最多 64 KiB，不把原始输出复制进 transcript 或 UI event state。

## 当前准入集合

- `zyra.task-execution-started/v1` → execution activity；
- `zyra.task-execution-error/v1` → redacted user issue；
- 具有稳定 stream/message identity 的 `runtime.text.*` → assistant started/delta/completed；正文只来自显式 `presentation_text`，缺失时不从 byte count、digest 或 summary 推断；
- `runtime.backend.dispatch.requested` 和明确 worker-attempt 状态 → 聚合 worker；
- 具有稳定 `tool_call_id` 的 `runtime.tool.*` → tool lifecycle；其中 source path 明确标注为 stdout/stderr 的 canonical artifact refs → 有界 output descriptors；
- 其他事件保持 developer-only，后续必须通过契约、测试和本 ADR 的扩展才能准入。

## 后果

- 产品 TUI 不再按 `runtime.*` 名称猜测用户语义。
- 新增 runtime 内部事件不会自动污染 transcript。
- runtime-event-spine 已有正式 assistant presentation 准入，但当前物理 CodeWorker 主路径仍不把私有 provider token delta 接入可重放 API ingress；因此真实 provider 运行仍以 canonical final answer 收敛。完整 verification command 和所有 provider/tool 的细粒度 presentation 也仍未完成；覆盖矩阵必须继续标记为部分实现，不能因 v2 契约存在而宣称 Phase D 完成。
