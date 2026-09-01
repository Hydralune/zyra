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
9. verification command 由 TypeScript progressive execution 的同一判定函数形成终态回执；后台 spawn 不计为通过，`shell_wait` 绑定原始 shell call。Python custody owner 只在安全投影时从既有私有 tool-call snapshot 补入脱敏命令，task outcome 最多保留 64 条；最终 verifier 通过但没有命令回执时继续明确显示 `not_recorded`。
10. provider transport 解码后的 `text_delta` 在 provider dispatch 完成前进入显式 `zyra.provider-assistant-presentation/v1` 生命周期；兼容流缺少 `response_start` 时由产品边界合成且去重 started，thinking 和 tool argument 帧永不进入该生命周期。
11. assistant delta 只投递给固定 `product-live-ingress` subscription，单块最多 1,024 UTF-8 bytes，不写 durable event store、不推进 SSE durable cursor。started/ended 是 durable boundary；短回答可在 ended 中保留最终 presentation，长回答由 canonical final answer 收敛。
12. API 进程内 live hub、CLI live reducer 和 Web ingress observer 都有独立容量上限。daemon 重启可以丢弃尚未完成的瞬时 delta，但 snapshot、durable ended 和 final answer 必须确定性收敛，不能把瞬时队列冒充新的 canonical truth。

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
- provider 解码帧已接入 typed runtime event；带 `runtime_event_bridge` 的 CodeWorker 路径会继续进入 live-only message bus、API SSE 和 CLI reducer。独立 deployment-node 现在通过认证 HTTP side channel 暴露按 attempt 绑定、带 ordinal/digest 的内存有界 presentation queue；只有 dispatcher 在执行请求中显式订阅时节点才创建队列，未订阅路径不保留 attempt 队列。scheduler 在阻塞的 `/execute` 同时轮询该通道，并把经过 task/run/workload/digest 校验的事件交给 API runtime ingress。通道失败只形成有界告警，不改变任务执行结果；队列溢出显式报告缺口并从仍可用的连续 ordinal 恢复。
- 真实 provider 集成测试已经证明 tool-call round、tool observation、最终文本以及 started/delta/ended 都在 physical worker 返回 receipt 之前产生；真实独立节点测试证明阻塞 `/execute` 时认证 `/runtime-events` 可以并发响应，dispatcher 测试证明 receipt 返回前可向上游转发。完整的比赛 daemon/provider → node → API SSE → CLI PTY 单任务证据仍须在真实长程负载门中取得；在该证据形成前，只能声称各实际边界及其组合已实现并通过测试，不能声称比赛任务的端到端实时输出已经验收。
- Web event ingress 已验证并分发 `kind: live`，但默认 Web 产品 transcript 尚未消费该瞬时 observer；CLI/Web 的最终 canonical 事实仍一致，实时呈现对账保持未完成。
- live delta 不可重放是有意的瞬时语义，不替代 durable cursor/snapshot。CLI 断线或 daemon 重启期间缺失的中间字符只允许由 durable ended/final answer 收敛，不能伪造 replay。
- verification command receipt 已形成正式链路，但 Phase G 真实长程负载、Web 实时视图和发布级稳定性复验仍未完成；不能因该链路成立而宣称 Phase D 或整份任务完成。
