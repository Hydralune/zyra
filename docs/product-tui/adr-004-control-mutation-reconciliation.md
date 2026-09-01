# ADR-004：产品控制 mutation 的歧义结果对账

状态：Accepted

## 背景

产品 TUI 的 queue、redirect、interrupt、cancel 和 continue 都会改变 canonical task/session 状态。HTTP mutation 写入服务端后若连接在回包前断开，客户端无法仅凭 transport error 判断 mutation 是否已经提交。Idempotency key 可以识别重复请求，但不能证明第一次请求已经离开服务端临界段；因此自动重发可能与仍在执行的第一次请求并发。

Codex 风格产品需要把这种情况显示为可恢复的控制状态，而不是静默重复操作或猜测成功。

## 决定

1. typed transport 对已经写入 socket 的 mutation disconnect/timeout 保持“不自动重放”。
2. control command 使用稳定的 request ID、command ID 和 idempotency key。服务端 `ControlRequestStore` 是持久 receipt owner。
3. 新增只读 `GET /tasks/{task_id}/commands/{request_id}`。它严格校验 task/run binding，返回 durable request lifecycle；terminal receipt 返回 200，已接收但未终结返回 202，跨 task 查询按不存在处理。
4. CLI 在 command mutation 出现歧义 transport failure 时，只查询该 receipt；不会再次 POST。查询成功后按原 request identity 校验 receipt，并恢复 canonical revision。
5. task cancel/continue 分别通过 task detail 对账；command cancel 通过 canonical queue 对账。只有 canonical 状态明确证明 mutation 已生效时才显示成功。
6. receipt/task/queue 均无法确认时，CLI 返回 `*_outcome_unknown`，携带脱敏 request/task identity、`automatic_retry=false` 和 `mutation_replayed=false`，要求用户先检查 `/queue` 或 `/status`。
7. revision conflict 不自动修改并重发用户意图；CLI更新已观察 revision，要求用户显式审查和再次提交。

## 结果

- lost-ack 不会造成重复 steer、interrupt、cancel 或 continue。
- 用户看到的“已取消/已继续/已应用”都有 canonical read 或 receipt 支持。
- receipt 查询是只读恢复面，不成为第二套控制状态，也不执行 mutation。
- daemon 完全不可达时结果保持 unknown；恢复后用户可以从 canonical task/queue 继续。
