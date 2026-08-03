# LangGraph Source Graph

日期：2026-07-08

本文件是 `G:\agent-zoo\langgraph` 的持续更新 source graph。当前已完成 Batch00 入口盘点、Batch01 checkpoint/store/cache 持久化底座、Batch02 StateGraph/channel 编译链路、Batch03 Pregel runtime/loop/runner、Batch04 streaming/debug/runtime/remote 和 Batch05 prebuilt agent/tool；后续 batch 会继续补全 CLI/SDK 的调用链、状态模型、测试证据和 Zyra 迁移裁决。

> **2026-07-13 角色纠偏：** 本文第 1-11 节保留源码事实与调用链。production 来源角色只以第 12 节和 `docs/milestones/source-graph-realignment-2026-07-08.md` 的 2026-07-13 前向纠偏为准；此前把 StateGraph/channel/Pregel/stream/ToolNode/SDK 列为完整主链迁移的判断均已失效。

## 1. 仓库形态

`langgraph` 是 Python monorepo，核心库都在 `libs/` 下：

```text
libs/
  checkpoint/
  checkpoint-conformance/
  checkpoint-postgres/
  checkpoint-sqlite/
  cli/
  langgraph/
  prebuilt/
  sdk-js/
  sdk-py/
```

仓库自己的依赖说明给出的生产依赖图：

```text
checkpoint
├── checkpoint-postgres
├── checkpoint-sqlite
├── prebuilt
└── langgraph

prebuilt
└── langgraph

sdk-py
├── langgraph
└── cli

sdk-js (standalone)
```

## 2. 初始主链路假设

LangGraph 的核心运行链路预期为：

```text
Graph API / StateGraph / functional API
  -> compile into Pregel graph
  -> channels + managed values + node specs + branch specs
  -> Pregel superstep loop
  -> task planning / runner / retry / interrupt / cache
  -> checkpoint/store/cache writes
  -> streaming/debug/callback projection
  -> resume/time travel/update_state/subgraph persistence
```

`prebuilt` 在这条链路上构造高级 agent/tool graph；`cli` 和 `sdk-py` 提供部署与远端 API 合约。

## 3. 初始文件地图

### 3.1 Core `libs/langgraph`

高权重文件：

- `langgraph/pregel/main.py`：核心 Pregel 公开 runtime，约 176 KB。
- `langgraph/pregel/_loop.py`：Pregel loop，约 82 KB。
- `langgraph/graph/state.py`：StateGraph 编译，约 76 KB。
- `langgraph/pregel/_algo.py`：superstep/task/channel 算法，约 52 KB。
- `langgraph/pregel/remote.py`：remote graph，约 49 KB。
- `langgraph/stream/transformers.py`：stream transformers，约 41 KB。
- `langgraph/pregel/_runner.py`：task runner，约 37 KB。
- `langgraph/types.py`：Command/interrupt/retry/cache 等公共类型，约 33 KB。
- `langgraph/_internal/_runnable.py`：Runnable glue，约 33 KB。
- `langgraph/pregel/_retry.py`：retry 机制，约 33 KB。

目录分组：

- `channels/**`：channel/reducer/value semantics。
- `graph/**`：Graph/StateGraph/node/branch/message/ui compile surface。
- `managed/**`：managed runtime values。
- `pregel/**`：主 runtime、loop、runner、checkpoint、stream/read/write/debug/remote。
- `stream/**`：stream mux/convert/channel/transformers/run stream。
- `_internal/**`：config、runnable、retry、serde、scratchpad、timeout、queue 等内部基础设施。

### 3.2 Persistence `checkpoint*`

- `libs/checkpoint/langgraph/checkpoint/base/**`：base checkpoint saver contract。
- `libs/checkpoint/langgraph/checkpoint/memory/**`：memory checkpoint saver。
- `libs/checkpoint/langgraph/checkpoint/serde/**`：serde、jsonplus、msgpack、encrypted、event hooks。
- `libs/checkpoint/langgraph/store/base/**`：store base、batch、embedding/search contract。
- `libs/checkpoint/langgraph/store/memory/**`：in-memory long-term store。
- `libs/checkpoint/langgraph/cache/**`：cache base/memory/redis。
- `libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/**`：SQLite checkpoint saver。
- `libs/checkpoint-sqlite/langgraph/store/sqlite/**`：SQLite store。
- `libs/checkpoint-postgres/langgraph/checkpoint/postgres/**`：Postgres checkpoint saver。
- `libs/checkpoint-postgres/langgraph/store/postgres/**`：Postgres store。
- `libs/checkpoint-conformance/langgraph/checkpoint/conformance/**`：portable conformance spec。

### 3.3 Prebuilt

- `chat_agent_executor.py`：prebuilt React/chat agent graph。
- `tool_node.py`：tool execution node。
- `tool_validator.py`：tool call validation。
- `interrupt.py`：human interrupt helpers。
- `_tool_call_stream.py`、`_tool_call_transformer.py`：tool call streaming adapters。

### 3.4 CLI / SDK

- `libs/cli/langgraph_cli/**`：CLI entry, config, docker, deploy, dependency tracking, archive, host backend。
- `libs/sdk-py/langgraph_sdk/**`：client, schema, SSE, stream controllers/transports, async/sync resources for runs/threads/assistants/store/cron。
- `libs/sdk-js/README.md`：本仓库中未发现 JS SDK 源码。

## 4. 测试地图

关键测试主题：

- `libs/langgraph/tests/test_pregel.py`、`test_pregel_async.py`、`test_large_cases.py`、`test_large_cases_async.py`
- `test_interruption.py`、`test_interrupt_migration.py`
- `test_time_travel.py`、`test_time_travel_async.py`
- `test_subgraph_persistence.py`、`test_subgraph_persistence_async.py`
- `test_stream_events_v3*.py`、`test_stream_*`
- `test_checkpoint_migration.py`
- `test_state.py`、`test_channels.py`、`test_messages_state.py`
- `libs/checkpoint/tests/test_memory.py`、`test_store.py`、`test_jsonplus.py`、`test_encrypted.py`
- `libs/checkpoint-conformance/langgraph/checkpoint/conformance/spec/test_*.py`
- `libs/prebuilt/tests/test_react_agent*.py`、`test_tool_node.py`、`test_validation_node.py`

## 5. 初始 Zyra 迁移判断

- 高价值：checkpoint/store/cache contract、Pregel loop 的 task/channel/checkpoint 语义、stream/event projection、人机 interrupt/resume/time-travel、ToolNode 的 state/store injection 和 command handling。
- 需谨慎：LangChain Core / Runnable / LangGraph Cloud/Server 耦合较强的 API；迁移时应抽取运行语义而不是原样搬 cloud/deploy surface。
- 暂不作为优先：CLI docker/deploy 产品层、SDK 客户端完整资源 API、`sdk-js` README-only 内容。

后续 batch 必须用源码调用链和测试证据验证或推翻这些判断。

## 6. Batch01 后补充：Checkpoint / Store / Cache

### 6.1 持久化主链路

```text
Pregel runtime / graph execution
  -> BaseCheckpointSaver
       -> CheckpointTuple
       -> checkpoint metadata
       -> channel_versions / versions_seen
       -> parent_config lineage
       -> pending_writes
       -> delta channel history
  -> BaseStore
       -> namespace/key long-term memory
       -> search / vector search / TTL
       -> runtime/tool/node context injection
  -> BaseCache
       -> namespace/key cache
       -> TTL
```

### 6.2 Checkpoint contract

核心数据模型：

- `PendingWrite = tuple[str, str, Any]`，即 `(task_id, channel, value)`。
- `CheckpointMetadata` 包含 `source`、`step`、`parents`、`run_id`、`counters_since_delta_snapshot`。
- `Checkpoint` 包含 `v`、`id`、`ts`、`channel_values`、`channel_versions`、`versions_seen`、`updated_channels`。
- `CheckpointTuple` 包含 `config`、`checkpoint`、`metadata`、`parent_config`、`pending_writes`。

主键与恢复：

- `thread_id` 是可恢复运行状态的第一主键。
- `checkpoint_ns` 隔离 namespace，尤其适合子图或分层执行。
- `checkpoint_id` 定位具体快照；缺失时获取最新 checkpoint。
- `parent_config` 串起 lineage，支撑 history/time travel/delta reconstruction。

### 6.3 Pending writes / control writes

`put_writes` 不是附属日志，而是执行状态的一部分。`WRITES_IDX_MAP` 用负索引让特殊 channel 可 upsert：

```text
ERROR      -> -1
SCHEDULED  -> -2
INTERRUPT  -> -3
RESUME     -> -4
```

普通 writes 使用 `(task_id, idx)` 幂等去重；特殊 writes 可替换旧值。这对 Zyra 的权限、interrupt、resume、fault recovery、worker scheduling 都有直接参考价值。

### 6.4 InMemorySaver 结构

`InMemorySaver` 把 checkpoint 拆成三类数据：

```text
storage[thread_id][checkpoint_ns][checkpoint_id]
writes[(thread_id, checkpoint_ns, checkpoint_id)][(task_id, idx)]
blobs[(thread_id, checkpoint_ns, channel, version)]
```

可迁移语义：

- checkpoint row、pending writes、channel version blobs 分离。
- `put` 只保存 `new_versions` 对应的 channel value。
- `get_tuple` 通过 `channel_versions` 回填 channel values。
- `list` 支持 thread/namespace/checkpoint/filter/before/limit。
- `delete_thread` 同时清理 checkpoint、writes、blobs。

### 6.5 DeltaChannel history

`get_delta_channel_history` 从目标 checkpoint 的 parent 开始回溯，不包含目标 checkpoint 自己的 pending writes。它为每个 channel 独立寻找 nearest seed，并按 oldest -> newest 返回 ancestor writes。

关键语义：

- 普通 blob value 可作为 seed。
- `_DeltaSnapshot` 不等同于完整 seed，不能错误截断 ancestor writes。
- pre-delta blob 会终止更早 writes 的 replay。
- prune/copy/delete 若不了解 delta lineage，会造成 silent corruption。

SQLite 与 Postgres 都为该能力实现了两阶段查询：先找 ancestry，再按 channel 查询 writes/seed，避免全量反序列化。

### 6.6 Store / Cache

Store 是长期 namespace memory，不是 checkpoint：

- `Item`、`SearchItem`、`GetOp`、`SearchOp`、`PutOp`、`ListNamespacesOp`。
- `PutOp.value=None` 表示 delete。
- Search 支持 namespace prefix、filter、pagination、semantic query、TTL refresh。
- `InMemoryStore` 支持 `_data` + `_vectors`，向量字段按 JSON path 抽取。
- `AsyncBatchedBaseStore` 在同一 event loop tick 合并/去重 ops，`Put` last-write-wins。

Cache 是可失效运行缓存：

- `InMemoryCache`：RLock + expiry timestamp。
- `RedisCache`：external redis + key prefix，但存在 silent failure，不适合作为核心状态。
- `SqliteCache`：SQLite WAL + TTL purge。

### 6.7 Serde safety

`JsonPlusSerializer` 默认 msgpack，可选 pickle fallback，并提供 strict/allowlist：

- `LANGGRAPH_STRICT_MSGPACK=true` 启用严格模式。
- `allowed_msgpack_modules` / `allowed_json_modules` 限制类型复活。
- Method call 只允许 safe method。
- 未注册或被阻断类型通过 serde event 记录。
- `EncryptedSerializer` 用 AES EAX 包裹 typed serializer，并保留 allowlist 语义。

Zyra 迁移时应吸收 strict allowlist、event hook、encrypted envelope，但不要直接扩大反序列化类型面。

### 6.8 Conformance

`checkpoint-conformance` 把 saver 能力分为：

- Base：`PUT`、`PUT_WRITES`、`GET_TUPLE`、`LIST`、`DELETE_THREAD`。
- Extended：`DELETE_FOR_RUNS`、`COPY_THREAD`、`PRUNE`、`DELTA_CHANNEL_HISTORY`。

这是比 DB adapter 更值得迁移的资产。Zyra 可裁剪成自己的 `CheckpointConformanceSuite`，用于验证 checkpoint store 的恢复、writes、filter、namespace、copy/delete/prune、delta history 行为。

### 6.9 Batch01 source-to-target 裁决

| Source | 裁决 | Zyra target |
| --- | --- | --- |
| `BaseCheckpointSaver` contract | `zyra_module_migrated` | CheckpointRuntime / StatePersistenceRuntime |
| `CheckpointTuple` / metadata / parent_config | `zyra_module_migrated` | Zyra checkpoint schema |
| `WRITES_IDX_MAP` special writes | `zyra_module_migrated` | control-write channel |
| InMemorySaver 三表结构 | `active port` | local/test saver |
| DeltaChannel history | `zyra_module_migrated` | trajectory/memory replay |
| SQLite saver/store | `active port` | local persistence strategy |
| Postgres saver/store | `reference + later port` | production persistence gateway |
| `BaseStore` / search / TTL | `zyra_module_migrated` | LongTermStoreRuntime |
| `AsyncBatchedBaseStore` | `active port` | batched store gateway |
| Cache base/memory/sqlite | `active port` | runtime cache |
| `RedisCache` | `reference-only` | non-critical cache backend |
| `JsonPlusSerializer` | `active port with rewrite` | secure payload serde |
| `EncryptedSerializer` | `active port` | encrypted persistence wrapper |
| `checkpoint-conformance` | `zyra_module_migrated` | checkpoint behavior test suite |
| `ShallowPostgresSaver` | `reference-only` | latest-only pattern, not mainline |

### 6.10 下一步验证点

Batch02/03 需要继续确认：

- StateGraph 编译出的 state/channel/reducer 如何影响 checkpoint `channel_versions`。
- Pregel loop 在 retry/interrupt/resume/cache 中调用 `put`、`put_writes`、`get_delta_channel_history` 的顺序。
- Store 如何注入 runtime/node/tool。
- Cache 在 graph runtime 中的真实可达路径。

## 7. Batch02 后补充：StateGraph / Channels / Managed Values

### 7.1 编译主链路

```text
StateGraph(state_schema, input_schema, output_schema, context_schema)
  -> _add_schema()
       -> _get_channels()
          -> Annotated field -> BaseChannel / BinaryOperatorAggregate / ManagedValue
          -> fallback -> LastValue
  -> add_node()
       -> StateNodeSpec
       -> coerce_to_runnable()
       -> infer input_schema / Command[Literal] ends
  -> add_edge() / add_conditional_edges()
       -> normal edge / waiting join / BranchSpec
  -> compile()
       -> validate graph
       -> strict msgpack allowlist from schemas + channels
       -> CompiledStateGraph(Pregel)
       -> attach_node()
       -> attach_edge()
       -> attach_branch()
  -> PregelNode
       -> read channels
       -> invoke runnable with runtime-injected kwargs
       -> ChannelWrite state updates + control writes
```

### 7.2 Schema 到 channel

`StateGraph` 通过 `_get_channels` 将字段转成运行时 channel：

- 没有 annotation 的非结构 schema 变成 `__root__`。
- `Annotated[T, BaseChannelInstance]` 直接使用该 channel。
- `Annotated[T, BaseChannelSubclass]` 实例化 channel。
- `Annotated[T, reducer]` 且 reducer 签名为 `(a, b) -> c` 时生成 `BinaryOperatorAggregate`。
- `Annotated[T, ManagedValue]` 生成 managed value。
- fallback 为 `LastValue`。

Input/Output schema 不允许 managed value；managed value 只能存在于 runtime state 输入侧，不作为普通输出字段。

### 7.3 Node / edge / branch attach

`attach_node` 为每个普通 node 创建 `branch:to:{node}` 触发 channel。node 读取 input schema 对应 channels，输出通过两个 writer 处理：

- `_get_updates` / `_get_root`：从 node return value 提取 state updates。
- `_control_branch`：从 `Command` / `Send` 提取 control routing。

`attach_edge`：

- 单 edge 写目标 `branch:to:{end}`。
- 多 start edge 创建 `join:{start1+start2}:{end}`，由 `NamedBarrierValue` 等待所有 start。

`attach_branch`：

- 读取 fresh state。
- branch path 返回目标 node 或 `Send`。
- node target 转成 `branch:to:{node}`。
- `Send` 转成 `TASKS` pushed task。

### 7.4 Channel semantics

| Channel | 语义 |
| --- | --- |
| `LastValue` | 默认单写 channel；同一 superstep 多写抛错。 |
| `BinaryOperatorAggregate` | reducer 合并多写；支持 `Overwrite` 绕过 reducer。 |
| `DeltaChannel` | checkpoint 不保存完整值；依靠 ancestor writes + snapshot 重建。 |
| `EphemeralValue` | 上一步值；空 update 清空；适合 branch trigger。 |
| `UntrackedValue` | 保存临时值但不 checkpoint。 |
| `AnyValue` | 取最后值，假设多写相等；空 update 清空。 |
| `NamedBarrierValue` | join barrier；全部 names 到齐才可用，consume 后清空。 |
| `Topic` | PubSub topic；可跨 step accumulate。 |

`BaseChannel.update` 明确说明同一 step values 顺序是 arbitrary。因此 reducer/channel 必须显式定义并发语义。Zyra 不应让多个 worker 随意写同一字段而没有 reducer 或冲突规则。

### 7.5 Command / Send / Interrupt / Overwrite

- `Send(node, arg, timeout=...)` 表示下一 step 给指定 node 发送一个自定义输入，典型用于 map-reduce fan-out。
- `Command(update=..., goto=..., resume=..., graph=...)` 同时支持 state update、routing、resume 和 parent graph jump。
- `Command(graph=Command.PARENT)` 在子图中抛 `ParentCommand`，由父图接管 routing。
- `interrupt(value)` 使用 scratchpad interrupt order 匹配 resume；没有 resume 时抛 `GraphInterrupt`，并要求 checkpointer 支撑恢复。
- `Overwrite(value)` 让 reducer channel 直接替换值；JSON-erased 形态通过 `type="__overwrite__"` 保持语义。

### 7.6 Runtime injection

`Runtime` 向 node 暴露：

- `context`
- `store`
- `stream_writer`
- `heartbeat`
- `previous`
- `execution_info`
- `server_info`
- `control`

`RunnableCallable` 根据函数签名注入：

- `config`
- `writer`
- `store`
- `previous`
- `runtime`
- `error`

这使 node/tool 能通过明确参数拿到 memory、stream、heartbeat、run control，而不必直接依赖全局状态。Zyra 可以迁移这个模式，但应替换 LangChain `RunnableConfig` / callback 依赖。

### 7.7 Message / UI reducers

`add_messages`：

- 按 message id merge/update/remove。
- 缺失 id 自动 UUID。
- `RemoveMessage` 删除，`REMOVE_ALL_MESSAGES` 清空。
- 可输出 OpenAI message format。

`_messages_delta_reducer`：

- `DeltaChannel` 专用 batching-invariant reducer。
- 支持 message id update/remove 和 raw dict/string/tuple coercion。

`graph/ui.py`：

- `push_ui_message` 写 custom stream 并可写 state key。
- `delete_ui_message` 写 remove-ui event 并可写 state key。
- `ui_message_reducer` 按 id merge/delete，支持 props merge。

### 7.8 Batch02 source-to-target 裁决

| Source | 裁决 | Zyra target |
| --- | --- | --- |
| `StateGraph` schema -> channel compiler | `active port` | workflow/state compiler |
| `CompiledStateGraph.attach_node/edge/branch` | `zyra_module_migrated` | graph-to-runtime compiler |
| `BaseChannel` contract | `zyra_module_migrated` | state channel runtime |
| `LastValue` | `zyra_module_migrated` | default single-writer field |
| `BinaryOperatorAggregate` | `zyra_module_migrated` | reducer state field |
| `Overwrite` | `zyra_module_migrated` | reducer bypass / state reset |
| `DeltaChannel` | `zyra_module_migrated` | long trajectory / message/file delta state |
| `EphemeralValue` | `active port` | trigger channel |
| `NamedBarrierValue` | `zyra_module_migrated` | join barrier / multi-worker sync |
| `Topic` | `active port` | event/topic channel |
| `UntrackedValue` | `active port` | non-persistent runtime state |
| `ManagedValue` | `zyra_module_migrated` | derived runtime fields |
| `Runtime` injection | `zyra_module_migrated` | node/tool execution context |
| `Command` / `Send` | `zyra_module_migrated` | control command / dynamic task |
| `interrupt()` | `active port` | human-in-loop pause/resume |
| `add_messages` / `_messages_delta_reducer` | `active port with rewrite` | conversation memory reducer |
| `push_message` / `graph/ui.py` | `reference + active port` | event/UI stream helpers |
| full LangChain `Runnable` surface | `reference-only` | none |

### 7.9 Batch03 闭合点

Batch03 已确认：

- Pregel loop 通过 `tick -> runner.tick -> after_tick` 按 superstep 调用 channel `update/consume/finish`。
- `put_writes` 持久化 task pending writes，`apply_writes` 更新 channel versions，`_put_checkpoint` 再提交 checkpoint。
- `RetryPolicy`、`CachePolicy`、`TimeoutPolicy` 都有真实执行路径和测试覆盖。
- `Interrupt`、`Command(resume/update/goto)` 通过 pending writes、scratchpad 和 task planning 进入 checkpoint 与下一轮执行。

## 8. Batch03 后补充：Pregel Runtime / Loop / Scheduler / Runner

### 8.1 Runtime 主链路

```text
Pregel.invoke / ainvoke
  -> Pregel.stream / astream
     -> _defaults()
     -> Runtime(context, store, stream_writer, heartbeat, previous, server_info, control)
     -> SyncPregelLoop / AsyncPregelLoop
        -> load checkpoint tuple or synthetic empty checkpoint
        -> channels_from_checkpoint / achannels_from_checkpoint
        -> _first(input / Command / resume / replay / fork)
        -> while tick()
           -> prepare_next_tasks()
           -> match cached writes
           -> PregelRunner.tick / atick
              -> run_with_retry / arun_with_retry
              -> commit writes / interrupts / errors
              -> schedule PUSH child tasks or error-handler tasks
           -> after_tick()
              -> apply_writes()
              -> emit values/debug
              -> _put_checkpoint(source="loop")
        -> _suppress_interrupt()
```

`Pregel.stream/astream` 是主路径；`invoke/ainvoke` 只是收集 stream 输出。Zyra 若迁移 LangGraph runtime，应把 streaming run loop 作为主实现，再给同步 API 做外壳。

### 8.2 Loop lifecycle

`SyncPregelLoop/AsyncPregelLoop` 进入时：

- 按 exact `checkpoint_id`、`ReplayState` 子图 checkpoint 或 thread latest checkpoint 加载状态。
- 无 checkpoint 时构造 synthetic `empty_checkpoint()`。
- 记录 `prev_checkpoint_config`、`checkpoint_id_saved`、`checkpoint_pending_writes`。
- 通过 `channels_from_checkpoint` hydrate live channels；DeltaChannel 缺值会走 saver `get_delta_channel_history`。
- 设置 `step = metadata["step"] + 1` 和 recursion stop。
- 调 `_first` 处理输入、resume、time travel、fork、subgraph replay。

`_first` 是恢复语义中枢：

- `Command(resume=...)` 会写 `RESUME`，多个 pending interrupts 时要求 interrupt-id map。
- time travel replay 会清理 stale RESUME writes，必要时创建 source=`fork` checkpoint。
- fresh input 通过 `map_input -> apply_writes -> _put_checkpoint(source="input")` 进入 state。
- resume 会把当前 channel versions 记录到 `versions_seen[INTERRUPT]`，避免立即重复 interrupt。
- 根图会把 `CONFIG_KEY_RESUMING` / `ReplayState` 传播给子图。

`tick` 负责规划：

- `prepare_next_tasks` 生成 PUSH/PULL tasks。
- pending writes 会回填到已成功 task，避免 resume 时重跑。
- `ERROR_SOURCE_NODE` pending write 会触发 error-handler 重排。
- `interrupt_before` 命中时抛 `GraphInterrupt`。

`after_tick` 负责提交：

- 收集 task writes。
- `apply_writes` 更新 channels 和 checkpoint channel versions。
- emit values。
- exit durability 下累积 DeltaChannel writes。
- 清 pending writes。
- `_put_checkpoint(source="loop")`。
- `interrupt_after` 命中时抛 `GraphInterrupt`。

### 8.3 Deterministic writes and task planning

`apply_writes`：

- 按 `task.path[:3]` 排序，保证同一 superstep 写入顺序确定。
- 更新 `versions_seen`。
- 对 trigger channels 调 `consume()`。
- 普通 writes 按 channel 分组后调用 `channel.update(vals)`。
- 未写 channel 也会收到 `update(EMPTY_SEQ)` 作为 step 边界。
- 若没有后续触发，则调用 `channel.finish()`。

`prepare_next_tasks`：

- 先从 `TASKS` Topic 消费 `Send`，生成 PUSH tasks。
- 再根据 `updated_channels + trigger_to_nodes` 或全量 processes 生成 PULL tasks。
- task id 由 checkpoint id、checkpoint namespace、step、node name、triggers 等生成，保证 replay/fork 可稳定定位。

### 8.4 Runner / retry / fault handling

`PregelRunner`：

- 并发执行当前 superstep tasks。
- `commit` 将成功 writes、`NO_WRITES`、`INTERRUPT`、`ERROR`、`ERROR_SOURCE_NODE` 写入 loop/checkpointer。
- 发现有 node-level error handler 时，调 `schedule_error_handler`，并用 handled exception ids 避免全局 panic。
- `_call/_acall` 支撑 functional API 子 task 调度，`__next_tick__` 保证 child task 至少下一 tick 执行。

`run_with_retry` / `arun_with_retry`：

- 每次 attempt 前清空 task.writes，避免失败尝试污染后续尝试。
- retry policy 支持 exception class、sequence、callable。
- `ParentCommand` 可由当前 graph writers 处理，或重写 namespace 后 bubble 给父图。
- async timeout 通过 `_TimedAttemptScope` 包裹 send/stream/call/runtime writer，超时后关闭 scope 并丢弃 stale writes。
- `runtime.heartbeat()` 可刷新 idle timeout；`run_timeout` 不被 heartbeat 刷新。

Error handler 不是普通 callback：

- 原 task 异常会持久化 `ERROR` 和 `ERROR_SOURCE_NODE`。
- handler task 注入 `NodeError(node=..., error=...)`。
- resume 时 `_resume_error_handlers_if_applicable` 根据 pending writes 重排 handler，原失败节点不重跑。

### 8.5 State control and time travel

`get_state` / `get_state_history` 会从 checkpoint tuple 重建 `StateSnapshot`。没有显式 checkpoint_id 的 latest state 会应用成功 pending writes；指定 checkpoint_id 时不会应用 pending writes。

`update_state` / `bulk_update_state`：

- 不运行 node bound runnable，只运行 node writers，把外部 values 作为该 node 的输出写入 state。
- 支持 `INPUT`、`END`、`__copy__`、普通 node update。
- DeltaChannel fresh-thread update 会创建 stub checkpoint 作为 parent anchor。
- 是 time travel/fork/human correction/control console 的核心入口。

测试 `test_time_travel.py` 系统覆盖 replay/fork/interrupt/subgraph/多层嵌套；`test_delta_channel_update_state.py` 覆盖 update_state 与 DeltaChannel 的历史链。

### 8.6 Batch03 source-to-target 裁决

| Source | 裁决 | Zyra target |
| --- | --- | --- |
| `Pregel.stream/astream` BSP run loop | `zyra_module_migrated` | GraphRuntime / CodeWorkerRuntime 主循环 |
| `get_state` / `get_state_history` | `zyra_module_migrated` | runtime state API / control console state view |
| `update_state` / `bulk_update_state` | `zyra_module_migrated` | runtime patch/fork/human correction |
| `SyncPregelLoop` / `AsyncPregelLoop` lifecycle | `zyra_module_migrated` | resumable run session loop |
| `_first` resume/time-travel/input handling | `zyra_module_migrated` | resume/fork/replay state machine |
| `tick` / `after_tick` | `zyra_module_migrated` | superstep scheduler |
| `_put_checkpoint` durability modes | `zyra_module_migrated` | checkpoint commit protocol |
| `apply_writes` | `zyra_module_migrated` | deterministic channel write applier |
| `prepare_next_tasks` / `prepare_single_task` | `zyra_module_migrated` | task planner / worker route planner |
| stable task id / checkpoint namespace | `active port` | task identity / nested run namespace |
| `PregelRunner` | `zyra_module_migrated` | concurrent task runner |
| runner commit semantics | `zyra_module_migrated` | writes/error/interrupt commit protocol |
| node-level error handler | `zyra_module_migrated` | fault recovery handler / recovery planner |
| `RetryPolicy` / `run_with_retry` | `active port` | retry runtime |
| `TimeoutPolicy` / `_TimedAttemptScope` | `active port with rewrite` | watchdog/heartbeat runtime |
| `RunControl.drain_requested` | `active port` | cooperative drain / shutdown |
| `GraphInterrupt` / `Command(resume)` | `zyra_module_migrated` | human-in-loop pause/resume |
| DeltaChannel checkpoint replay | `zyra_module_migrated` | long trajectory / conversation state replay |
| `BackgroundExecutor` / `AsyncBackgroundExecutor` | `active port with rewrite` | task executor abstraction |
| `CachePolicy` task writes cache | `active port` | non-authoritative runtime cache |
| LangChain callback manager integration | `reference-only` | replace with Zyra event log/callbacks |
| v3 `stream_events` mux | `zyra_module_migrated` | streaming/control UI projection |

### 8.7 Batch04 闭合点

Batch04 已确认：

- stream/debug/checkpoint/task/result events 通过 `StreamMux`、`StreamTransformer` 和 `GraphRunStream` 从 Pregel v2 stream 投影到 v3 protocol event/projection surface。
- v3 stream transformers 把 values/messages/lifecycle/subgraph/checkpoints/debug/tasks/custom/updates 拆成 typed run projections。
- remote graph 能代理 stream/state/history/update_state，但 v3 remote 明确不支持本地 `control`、`transformers`、`interrupt_before/after`，因此不能承担 Zyra 核心控制台运行责任。
- `Runtime` / `RunControl` / `ExecutionInfo` 把 context/store/stream_writer/heartbeat/control/checkpoint namespace 暴露到 node/subgraph。

## 9. Batch04 后补充：Streaming / Debug / Remote Graph / Runtime Context

### 9.1 Streaming 主链路

```text
Pregel.stream_events(version="v3")
  -> _pregel_stream_v3 / _apregel_stream_v3
     -> StreamMux(
          factories=[
            ValuesTransformer,
            MessagesTransformer,
            LifecycleTransformer,
            SubgraphTransformer,
            *compiled_factories,
            *extra_factories,
          ],
          scope=parent_ns,
        )
     -> stream / astream(
          version="v2",
          stream_mode=_collect_stream_modes(mux),
          subgraphs=True,
        )
     -> GraphRunStream / AsyncGraphRunStream
        -> caller-driven _pump_next / _apump_next
        -> convert_to_protocol_event
        -> StreamMux.push / apush
        -> StreamChannel projections + raw protocol event log
```

核心判断：v3 streaming 是 caller-driven，不是后台线程推送。调用方消费 `run.values`、`run.messages`、`run.subgraphs`、`run.lifecycle` 或 raw events 时，才驱动底层 Pregel 继续前进。这对 Zyra 控制台很关键，因为它把 backpressure、projection 订阅、abort/cancel 和 UI 按需消费放在同一个运行边界中。

### 9.2 ProtocolEvent / StreamMux / StreamChannel

`ProtocolEvent` envelope：

```text
type="event"
method=<values|updates|messages|tasks|checkpoints|debug|custom|custom:*|lifecycle>
params.namespace=list[str]
params.timestamp=int
params.data=Any
params.interrupts?=tuple[Any, ...]
seq?=monotonic int assigned by root mux
```

`StreamMux` 负责：

- 注册 transformer factories 和 concrete transformers。
- 按 scope 与 `before_builtins` 排序，保证 redaction/filter 这类 mutating transformer 可在 builtins 前运行。
- 维护只读 `extensions` 和 native direct attributes。
- 分配 root `seq`，child mux 不修改 forwarded event。
- 自动把 named `StreamChannel("name")` 的 push 转成 `custom:name` protocol event。
- close/fail 时收束 transformer、scheduled async tasks 和 projection channels。

`StreamChannel` 负责：

- 单订阅 projection queue。
- lazy subscribe：未订阅 projection 不积累历史数据。
- drain-on-consume：消费后从 buffer 移除。
- fail 后先吐出已入队 items，再抛错。
- `tee/atee` fan-out。
- push stamp 支撑 `GraphRunStream.interleave` 跨 projection 排序。

测试覆盖了 event suppression、所有 transformer 都能看到 suppressed event、projection key 冲突、auto-forward seq ordering、close/fail resilience、memory bound、single subscriber、tee/atee。

### 9.3 GraphRunStream / AsyncGraphRunStream

`GraphRunStream`：

- 包装底层 graph iterator 和 mux。
- raw iteration 输出 protocol events。
- projection iteration 也会驱动 pump。
- `output`、`interrupted`、`interrupts` 会驱动 run 到完成。
- `abort()` 关闭底层 generator 和 mux，且幂等。
- `interleave(*names)` 按 push stamp 合并 projection items。

`AsyncGraphRunStream`：

- async `output/interrupted/interrupts`。
- `_apump_next` 使用 condition + `_pumping` single-flight，保证只有一个 task 调用底层 `__anext__()`。
- `abort()` 会取消 in-flight pull，测试覆盖普通子图、深度嵌套子图和正在 pump 中的子图取消。

Zyra 迁移时应把这层视为 run session handle，而不是普通 iterator helper。

### 9.4 Transformers

内置 transformer：

- `ValuesTransformer`：当前 scope 的 state snapshots，native `run.values`。
- `CustomTransformer`：`get_stream_writer()` 的 custom payload。
- `UpdatesTransformer`：node output updates。
- `MessagesTransformer`：按 model call 分组为 `ChatModelStream` / `AsyncChatModelStream` handle；LangChain message protocol 应在 Zyra 中替换。
- `LifecycleTransformer`：基于 tasks events 生成 subgraph lifecycle event，包含 started/completed/failed/interrupted/drained。
- `SubgraphTransformer`：为 direct-child subgraph 创建 child mux 和 `SubgraphRunStream` / `AsyncSubgraphRunStream`。
- `CheckpointsTransformer`：checkpoint projection。
- `DebugTransformer`：debug projection。
- `TasksTransformer`：raw task projection。

关键测试事实：

- async `aprocess` 必须在后续 transformer 看到 event 前完成。
- scheduled task 必须在 `afinalize` 前完成；`on_error="raise"` 会 fail run。
- `afail` 会取消 pending scheduled tasks。
- `LifecycleTransformer` / `SubgraphTransformer` suppress tasks raw event 进入 main log，但后续 transformer 仍能处理同一 event。

### 9.5 Debug projection

`pregel/debug.py` 将 loop 内部状态转成可解释 debug payload：

- `map_debug_tasks`：task id/name/input/triggers/metadata。
- `map_task_result_writes`：聚合同一 channel 多次写入。
- `map_debug_task_results`：task result/error/interrupts。
- `map_debug_checkpoint`：checkpoint config、parent_config、values、metadata、next、tasks。
- `tasks_w_writes`：从 pending writes 反推 result/error/interrupt/state。

这说明 debug stream 不是日志列表，而是 checkpoint/task/pending-writes 的解释性 projection。Zyra 如果迁移，应接入自己的 checkpoint schema 和 event log。

### 9.6 Runtime / RunControl

`Runtime` 注入 node/subgraph：

- `context`
- `store`
- `stream_writer`
- `heartbeat`
- `previous`
- `execution_info`
- `server_info`
- `control`

`RunControl.request_drain(reason)` 是 cooperative drain 控制点。测试证明：

- node 内请求 drain 会阻止未来 step。
- terminal step 请求 drain 会正常完成。
- `durability="exit"` 下 drain 会保存可 resume checkpoint。
- 子图 drain 后可以 resume parent。
- 外部线程或 async task 可在执行中请求 drain。
- 预先 drained 的 control 会阻止第一个 pending task。
- `ExecutionInfo` 在 checkpointer run 中填入 thread_id、task_id、checkpoint_id、checkpoint_ns、node_attempt、node_first_attempt_time；子图 namespace 带嵌套 segment。

Zyra 应把这部分迁为 execution context/control API，并扩展 worker/span/attempt 字段。

### 9.7 RemoteGraph

`RemoteGraph` 是 LangGraph Server SDK 代理层：

- 代理 state/history/update/stream/invoke。
- v3 `stream_events` 返回 `_RemoteGraphRunStream` / `_AsyncRemoteGraphRunStream`。
- `_ProjectionRegistry` 暴露 `values/messages/tool_calls/subgraphs` typed projections，以及 `updates/checkpoints/tasks/custom` decoded projections。
- remote v3 不枚举 `lifecycle/debug`。
- `_sanitize_config` 会剥掉 checkpoint internal keys。
- `Command(resume=...)` 只把 raw resume value 发到 wire；`goto/update` 不支持。
- abort 会 cancel server run 并 close SDK thread stream。

裁决：RemoteGraph 适合作为 Zyra API contract 参考，不适合作为核心 runtime 内化来源。它的 v3 限制反向证明：控制台 transformer/control/breakpoint 必须在 Zyra-owned runtime 内完成，不能交给远端黑箱。

### 9.8 Batch04 source-to-target 裁决

| Source | 裁决 | Zyra target |
| --- | --- | --- |
| `ProtocolEvent` | `zyra_module_migrated` | runtime event envelope |
| `StreamTransformer` | `zyra_module_migrated` | event projection/filter/redaction |
| `StreamMux` | `zyra_module_migrated` | event mux / console projection registry |
| `StreamChannel` | `zyra_module_migrated` | projection queue / run panel stream |
| `GraphRunStream` | `zyra_module_migrated` | sync run session handle |
| `AsyncGraphRunStream` | `zyra_module_migrated` | async run session handle / cancellation |
| Values/Updates/Custom/Checkpoints/Debug/Tasks transformers | `active port` | typed runtime projections |
| `MessagesTransformer` | `active port with rewrite` | model-call/token stream projection |
| `LifecycleTransformer` | `zyra_module_migrated` | subagent/subgraph lifecycle stream |
| `SubgraphTransformer` | `zyra_module_migrated` | child run handles / nested console |
| `pregel/debug.py` | `active port` | debug checkpoint/task projection |
| `Runtime` / `RunControl` / `ExecutionInfo` | `zyra_module_migrated` | execution context / cooperative drain |
| graph callbacks | `reference-only` | interrupt/resume side event pattern |
| `RemoteGraph` | `reference-only` | remote API contract |
| `_RemoteGraphRunStream` | `reference-only` | remote adapter behavior |

### 9.9 Batch05 闭合点

Batch05 已确认：

- prebuilt React/chat agent graph 通过 `create_react_agent` 编译为 `StateGraph`，再进入 Batch02-04 的 Pregel runtime 和 stream surface。
- `ToolNode` 执行 tool calls、注入 state/store/runtime、处理 errors/Command/output，是高价值迁移对象。
- tool-call stream/transformer 直接接入 Batch04 `StreamTransformer` / `StreamMux`，显式启用后暴露 `run.tool_calls`。
- validation node 和 interrupt helper 已 deprecated，只适合作为 tool-call validation / approval payload shape 参考。

## 10. Batch05 后补充：Prebuilt Agent / ToolNode / Tool Validation

### 10.1 Prebuilt 主链路

```text
create_react_agent(model, tools, ...)
  -> normalize tools / model.bind_tools
  -> ToolNode([...])
  -> StateGraph
     -> optional pre_model_hook
     -> agent node = call_model / acall_model
     -> optional post_model_hook
     -> tools node = ToolNode
     -> optional generate_structured_response
  -> conditional edges
     -> v1: route all tool calls to "tools"
     -> v2: Send("tools", ToolCallWithContext(...)) per tool call
     -> return_direct may route tools -> END
  -> compile(checkpointer, store, interrupt_before/after, debug, name)
```

关键判断：`prebuilt` 不是独立 agent runtime，而是将 LangChain model/tool protocol 编译到 `StateGraph`、`ToolNode`、`Send`、`Command`、`Runtime` 和 `StreamMux` 的高层 factory。

### 10.2 `create_react_agent`

主要机制：

- 默认 state 包含 `messages`、`remaining_steps`，有 structured output 时包含 `structured_response`。
- 用户传 `state_schema` 时会校验必需 key。
- 支持 static model、string model、Runnable、sync dynamic model、async dynamic model。
- `prompt` 被转成 Runnable 并与 model composition。
- `_validate_chat_history` 保证 AIMessages 的 tool_calls 都有对应 ToolMessage。
- `remaining_steps` 阻止已无足够步数时继续 tool call。
- `pre_model_hook` 可提供 `llm_input_messages`。
- `post_model_hook` 可拦截 agent 输出并根据 pending tool calls 继续路由。
- `response_format` 增加 structured response node。
- `return_direct` tool 可让 tool node 后直接 END。

Zyra 迁移判断：`v2 Send per tool call` 和 hook/route 结构值得迁；LangChain model/prompt/bind_tools surface 应替换为 Zyra model gateway 和 tool registry。

### 10.3 `ToolNode`

主执行链：

```text
ToolNode._func / _afunc
  -> _parse_input
  -> _extract_state
  -> ToolRuntime(...)
  -> _run_one / _arun_one
     -> optional wrap_tool_call / awrap_tool_call
     -> _execute_tool_sync / _execute_tool_async
        -> _validate_tool_call
        -> _inject_tool_args
        -> tool.invoke / tool.ainvoke
        -> _normalize_tool_response
  -> _combine_tool_outputs
```

输入形态：

- list messages。
- dict state with `messages_key`。
- BaseModel/dataclass-like state。
- raw list[ToolCall]。
- `ToolCallWithContext` from `Send`。

并发：

- sync path 用 `get_executor_for_config(config).map(...)`。
- async path 用 `asyncio.gather(...)`。

状态读取：

- `ToolCallWithContext` 直接使用 payload state。
- raw list[ToolCall] 会从 `CONFIG_KEY_READ` 读取 channels 还原 state。

### 10.4 ToolRuntime / injection

`ToolRuntime` 字段：

- `state`
- `context`
- `config`
- `stream_writer`
- `tool_call_id`
- `store`
- `tools`
- `execution_info`
- `server_info`

注入机制：

- `InjectedState()` 注入整个 state。
- `InjectedState("field")` 注入单个字段。
- `InjectedStore()` 注入 compile 时传入的 store。
- `ToolRuntime` 参数直接注入 tool-specific runtime。
- `_get_all_injected_args` 同时读取 tool schema annotations 和 function type hints。
- `_inject_tool_args` 会移除 LLM 提供的 injected args，再写入 trusted values。

测试覆盖 dict/Pydantic/dataclass/list state injection、store injection、async all-types injection、缺 store 错误、validation error filtering。

### 10.5 Wrapper / interceptor

`wrap_tool_call` / `awrap_tool_call`：

- 接收 `ToolCallRequest` 和 `execute` callable。
- 可修改 request。
- 可 short-circuit 返回 ToolMessage。
- 可 short-circuit 返回 Command。
- 可多次 execute 实现 retry。
- 可处理 unregistered tool，此时 `request.tool is None`。
- 可 override tool 为 custom implementation 后 execute。

Zyra 迁移判断：这是 permission/cache/retry/tool gateway 的高价值来源，但必须接入 Zyra audit、approval、policy 和 event log。

### 10.6 Error and Command validation

错误处理：

- `handle_tool_errors=True` 默认捕获 Exception 并返回 error ToolMessage。
- `False` 时异常冒泡。
- string/callable/type/tuple 提供不同 error policy。
- `GraphBubbleUp` / `GraphInterrupt` 永远冒泡，不被吞。
- `ToolInvocationError` 会过滤 injected 参数相关 validation errors。

Command validation：

- dict update 只能用于 dict/tool_calls input。
- list update 只能用于 list messages input。
- current graph 的 Command.update 必须包含匹配当前 tool_call_id 的 ToolMessage。
- `Command.PARENT` 不要求当前图终止 ToolMessage。
- `RemoveMessage(REMOVE_ALL_MESSAGES)` 是允许特例。
- `list[Command|ToolMessage]` 必须正好有一个匹配当前 tool_call_id 的终止 ToolMessage。
- 多个 `Command.PARENT` 且 goto 是 `list[Send]` 时会合并为一个 parent command。

这部分适合直接作为 Zyra ToolRuntime 的 contract guard 来源。

### 10.7 Tool-call streaming

```text
ToolRuntime.emit_output_delta(delta)
  -> _tool_call_writer ContextVar
  -> tools stream event: tool-output-delta
  -> ToolCallTransformer(required_stream_modes=("tools",))
  -> ToolCallStream.output_deltas
```

`ToolCallTransformer`：

- native projection：`run.tool_calls`。
- `tool-started` 创建 `ToolCallStream`。
- `tool-output-delta` 写 delta。
- `tool-finished` 设置 output 并 close。
- `tool-error` 设置 error 并 close。
- 只处理当前 scope，subgraph scoped tool events 留给 child mux。
- raw `tools` event pass-through main log。

测试覆盖 sync/async end-to-end、stream modes union includes tools、scope filtering、concurrent tool calls 不串流、error field。

### 10.8 Validation / interrupt helpers

`ValidationNode`：

- 只校验 last AIMessage tool_calls，不执行工具。
- Pydantic/BaseTool/function schema 成功时返回 validated JSON ToolMessage。
- 失败时返回 error ToolMessage，带 `additional_kwargs={"is_error": True}`。
- 已 deprecated，适合作为 validation/repair feedback 参考。

`HumanInterrupt*`：

- 描述 human action request、allow_ignore/respond/edit/accept、response type。
- 已迁移到 `langchain.agents.interrupt`。
- 只作为 approval payload shape 参考。

### 10.9 Batch05 source-to-target 裁决

| Source | 裁决 | Zyra target |
| --- | --- | --- |
| `ToolNode` execution core | `zyra_module_migrated` | ToolRuntime / ToolNode |
| `_parse_input` / `ToolCallWithContext` | `active_port` | tool call input / Send fan-out |
| `ToolRuntime` | `zyra_module_migrated` | tool execution context |
| `InjectedState` / `InjectedStore` | `zyra_module_migrated` | hidden tool args injection |
| `_inject_tool_args` trusted stripping | `zyra_module_migrated` | anti-forged injected args guard |
| `wrap_tool_call` / `awrap_tool_call` | `active_port` | permission/cache/retry gateway |
| tool error handling | `active_port` | tool error policy |
| Command validation | `zyra_module_migrated` | tool result/control contract guard |
| parent Command Send merge | `active_port` | subagent/worker handoff |
| `create_react_agent` graph factory | `active_port with rewrite` | AgentRuntime factory |
| pre/post model hooks | `active_port` | context/policy/approval hooks |
| `remaining_steps` | `reference_plus` | budget guard |
| structured response node | `reference_only` | structured output surface |
| `ToolCallTransformer` | `zyra_module_migrated` | tool/artifact streaming projection |
| `ToolCallStream` | `zyra_module_migrated` | per-tool live stream handle |
| `ValidationNode` | `reference_plus` | validation/repair feedback |
| `HumanInterrupt*` | `reference_only` | approval payload shape |
| LangChain BaseTool/BaseMessage schemas | `replace` | Zyra-owned tool/message schemas |

### 10.10 下一步验证点

Batch06 已确认：

- CLI 如何读取 graph config、构建本地 dev/server/docker/deploy artefacts。
- SDK Python 的 runs/threads/assistants/store/stream API 合同。
- 哪些是 LangGraph Cloud/Server 产品边界，只能作为 Zyra API 参考。
- 哪些 CLI/SDK streaming/control schema 可反向补强 Zyra 控制台/API 设计。

## 11. Batch06 后补充：CLI / SDK / Deployment Boundary

### 11.1 CLI 主链路

```text
langgraph cli
  -> validate_config_file(langgraph.json)
  -> dev / up / build / dockerfile / deploy / validate / new
```

命令裁决：

- `dev`：将 graphs/env/store/auth/http/ui/webhooks/checkpointer 传入 `langgraph_api.cli.run_server`，适合作为 Zyra local API/dev server config handoff 参考。
- `up`：Docker Compose 启动 Redis/Postgres/langgraph-api/debugger，适合作为本地服务编排参考。
- `build` / `dockerfile`：通过 `config_to_docker` 生成 Dockerfile 与 build context，适合作为 deployment artifact helper。
- `deploy`：LangSmith HostBackend orchestration，云后端 API reference-only。
- `validate`：config errors + unknown key warnings，适合作为 Zyra config validation 参考。

### 11.2 Config / path custody

```text
validate_config
  -> infer python/node graph
  -> normalize defaults
  -> validate versions/source/deps/graphs/distro/auth/http/encryption
  -> LocalDeps
  -> rewrite graph/auth/encryption/checkpointer/http local paths
  -> build runtime env vars
```

高价值机制：

- Python >= 3.11，Node >= 20。
- `source.kind="uv"` 与 dependency-based config 互斥。
- `graphs` 必需。
- `auth.path` / `encryption.path` / `http.app` 必须是 `<module>:<attribute>`。
- local file paths 必须被 `dependencies` 覆盖，否则 fail。
- real/faux packages 和 additional Docker contexts 有明确映射。
- reserved package names 防止 shadow runtime deps。

Zyra 判断：`validate_config`、`LocalDeps`、path rewriting 和 secure archive 是 active port；Docker tag/base image 只是 deployment helper。

### 11.3 SDK resource API

```text
get_client / get_sync_client
  -> http
  -> assistants
  -> threads
  -> runs
  -> crons
  -> store
```

核心 API projection：

- `ThreadState` 包含 values/next/checkpoint/metadata/parent_checkpoint/tasks/interrupts。
- `Run` 包含 status、metadata、multitask_strategy。
- `Checkpoint` 暴露 thread_id/checkpoint_ns/checkpoint_id/checkpoint_map。
- `RunsClient` 支持 stream/create/wait/cancel/cancel_many/join/join_stream。
- `ThreadsClient` 支持 get_state/update_state/get_history/stream/join_stream。
- `StoreClient` 支持 namespace KV/search/list_namespaces。
- `AssistantsClient` 支持 graph/schema/subgraph introspection。

Zyra 判断：resource client shape 和 API schemas 是 active port；`CronClient` reference-only。

### 11.4 HTTP / SSE safety

SDK HTTP 层提供：

- typed errors for 400/401/403/404/409/422/429/5xx。
- orjson serialization with model_dump/dict/set handling。
- SSE `Accept: text/event-stream` and content-type check。
- reconnect via same-origin `Location`。
- `Last-Event-ID` support。
- path segment encoding with `_quote_path_param`。

Zyra 判断：typed errors、path encoding、same-origin reconnect 是 `zyra_module_migrated` 级别的安全/可靠性来源。

### 11.5 v3 ThreadStream

```text
ThreadsClient.stream(thread_id?, assistant_id, transport=sse|websocket)
  -> AsyncThreadStream / SyncThreadStream
     -> Protocol transport
     -> StreamController
     -> lifecycle watcher
     -> run.start / input.respond
     -> values/messages/tool_calls/subgraphs/extensions/interleave projections
```

关键机制：

- 进入 context 时启动 lifecycle/input watcher。
- `run.start` 发送 command 并 release run_start_gate。
- `run.respond` 必须匹配 outstanding interrupt id 和 namespace。
- values projection 先开订阅再 REST fetch snapshot。
- messages 按 message_id 路由 stream。
- tool_calls 暴露 per-tool handle、delta、output、error。
- subgraphs/subagents 通过 namespace 和 tasks/lifecycle events 发现 child handle。
- extensions 使用 `custom:<name>` 命名通道。
- interleave projections 用一个 merged subscription 保留 arrival order。

Zyra 判断：`ThreadStream`、`StreamController`、subscription matching、decoders、SSE/WS transport、projection handles 都是控制台/SDK 高价值迁移对象。

### 11.6 Batch06 source-to-target 裁决

| Source | 裁决 | Zyra target |
| --- | --- | --- |
| CLI config schema | `active_port` | runtime/deploy config |
| config validation | `active_port` | config validator |
| local deps/path custody | `active_port` | clean-directory import ownership |
| secure archive | `active_port` | remote build/source packaging |
| Dockerfile/compose generation | `reference_only` | deploy helper |
| LangSmith HostBackend deploy | `reference_only` | cloud deploy example |
| SDK schema ThreadState/Run/Checkpoint/StreamPart | `active_port` | API contract |
| Runs/Threads/Store clients | `active_port` | Zyra SDK |
| typed errors/path encoding/SSE reconnect | `zyra_module_migrated` | SDK reliability/safety |
| `AsyncThreadStream` / `SyncThreadStream` | `zyra_module_migrated` | control console stream |
| `StreamController` / `SyncStreamController` | `zyra_module_migrated` | shared stream fanout/reconnect |
| subscription filters / decoders | `zyra_module_migrated` | event projection state machines |
| values/messages/tool_calls/subgraphs/extensions projections | `zyra_module_migrated` | UI/SDK typed projections |
| Auth decorator registry | `active_port with rewrite` | API resource policy |
| Encryption handler types | `reference_plus` | future memory/artifact encryption |
| sdk-js placeholder | `reference_only` | external JS SDK pointer |

### 11.7 下一步

Batch07 已汇总 Batch01-06，给出 LangGraph 对 Zyra M1/M2/M3 的总裁决和与其它来源仓库的边界切分。

## 12. 2026-07-13 Cross-Batch 前向总裁决

Batch01-07 的源码事实继续有效，但原“完整链深度内化”结论已被重裁决。LangGraph 对 Zyra 的核心价值不是全局 state graph runtime，而是 checkpoint commit / exact-resume 的恢复正确性语义。

三项争议的技术判断如下：

- “closed loop”不是准确术语；LangGraph 可以调用工具、读取环境和条件路由。真正成立的风险是 `StateGraph` 通常在 compile 前预声明 node/edge/state/target，容易把开放环境压成封闭世界。只在预编译集合内选边不算 Zyra 所需的运行时动态拓扑。
- 并行共享状态污染风险属实。共享 state/channel/reducer 一旦保留可变对象别名、原地修改嵌套对象或使用非确定 reducer，分支完成顺序就可能改变 canonical 结果。
- 逻辑抽离不是必然反模式，但把连续的 `reason -> tool -> observe -> revise` 循环拆成共享状态微节点会破坏内聚性。durable graph 只接任务交接、权限提交、长等待、artifact、fault/recovery 等粗粒度边界。

### 12.1 唯一推荐 production 链

```text
thread / namespace / checkpoint identity
  -> parent lineage and stable task/request correlation
  -> pending writes separated from committed writes
  -> atomic commit and side-effect idempotency fence
  -> interrupt/resume correlation
  -> exact crash restore and conformance tests
```

这些语义裁剪进入 Zyra `GraphCommitRuntime` / `CheckpointRecoveryRuntime`。Zyra store/schema/event/recovery 承担 canonical owner，不依赖 LangGraph package、server、sidecar、SQLite/Postgres saver 或 RemoteGraph 黑箱。

### 12.2 来源角色

| LangGraph 模块 | 前向角色 | 原因与边界 |
| --- | --- | --- |
| checkpoint tuple/base/serde/conformance | 窄域 `primary_implementation` semantic source | 只吸收 identity/lineage/pending writes/atomic restore/corruption handling；实现必须 Zyra-owned |
| `_loop/_algo/_checkpoint` selected semantics/tests | `reference_only` | 只核对 stable id、commit boundary、interrupt/resume 和 crash recovery；不复制 planner/runner |
| StateGraph compiler、functional API、subgraph | `reference_only` | compile 前预声明图容易形成封闭世界；动态图 topology 由 Zyra runtime 掌握 |
| channels/reducers/managed values/DeltaChannel | `reference_only` / negative conformance | shared mutable alias 和非确定性 reducer 风险；Zyra 使用 immutable snapshot + branch-local delta |
| Pregel planner/runner/retry/executor | `reference_only` | 不得取得 scheduler、worker lease、routing 或 topology policy owner |
| stream/debug/Runtime/RunControl/RemoteGraph | `conformance_only` / `deferred` | canonical event/projector/control plane 已由 Zyra/opencode 子域 owner 承担 |
| Store/search/vector/TTL/cache | `reference_only` | 不得取得 MemoryFabric/index/curator owner |
| ToolNode/ToolRuntime/create_react_agent/prebuilt | `rejected` for default path | 会复制或拆碎 Claude-derived CodeWorker tool/query loop |
| CLI/SDK/server/deploy/Auth hosted surface | `reference_only` / `deferred` | 不得成为默认运行依赖；独立 typed/config/path 机制需另按目标子域裁决 |

### 12.3 Zyra 侧替代责任

- `DynamicTopologyRuntime`：运行中新增、删除、替换 node/edge/role/capability；只在预编译目标集合中选边不算动态。
- `GraphStateCustody`：不可变 snapshot、branch-local delta、显式 read/write-set、stale/conflict detection、deterministic commit；禁止节点原地修改共享对象。
- `GraphCommitRuntime`：pending/committed write journal、atomic visibility、stable ids、side-effect fence。
- `CheckpointRecoveryRuntime`：checkpoint lineage、topology revision restore、interrupt/resume correlation、partial-commit/corruption handling。
- Claude-derived `CodeWorkerRuntime`：完整 reason/tool/observe/revise、permission、tool-result budget、context/compact/session lifecycle；图层只观察粗粒度 durable boundary。
- Zyra EventStore/Projector 和 M2 single reducer/store：唯一 event/UI truth，LangGraph stream 只做 conformance。

### 12.4 必须保留的对抗证据

1. 运行中因新环境事实创建预编译图中不存在的 topology mutation。
2. 并行分支修改嵌套对象时无 alias 污染；打乱完成顺序后 canonical commit/replay 仍确定。
3. write conflict 显式 serialize/rebase/replan，不做隐式 last-writer-wins。
4. 在 pending write、外部副作用前后、commit、topology mutation 位置崩溃，恢复不丢已提交事实、不泄漏未提交状态、不重复副作用。
5. CodeWorker 内聚循环未被 StateGraph/ToolNode 微节点化；禁用 exact-resume 模块会让恢复场景真实失败。
6. clean-room/dependency audit 证明默认主路径不 import/call LangGraph StateGraph、Pregel、channels、ToolNode、stream controller、SDK/server。

### 12.5 工程量守恒与失败判定

LangGraph 降权只删除不合适的上游实现义务，不降低 Zyra 能力、父级最低有效代码或赛题证据。原计划中由 StateGraph/channel/Pregel/stream/ToolNode/SDK 贡献的实现量，必须由 Zyra-owned runtime-evolvable topology、immutable snapshot/branch delta、write-set/conflict detector、deterministic commit、side-effect fence、exact-resume、causality projection 和相应对抗测试回补。

以下任一情况直接失败：把“动态”实现成对预声明 node/edge 的条件选择；节点原地修改共享 dict/list/object；合并结果依赖并行完成顺序；LangGraph Store/checkpointer/stream/server 黑箱持有 canonical state；StateGraph/Pregel/ToolNode/create-react-agent 接管 CodeWorker 内部推理循环；或以 LangGraph 降权为理由删除 checkpoint lineage、pending write、atomic commit、exact resume、fault/recovery 与赛题动态证据。

### 12.6 后续精读顺序

1. checkpoint tuple、lineage、pending writes、serde/corruption 与 conformance tests。
2. selected `_loop/_algo/_checkpoint` 的 stable id、atomic commit、interrupt/resume、crash restore 语义。
3. StateGraph/channel/Pregel/prebuilt/stream 只在对抗测试或 conformance 缺口出现时定点回读。

后续不恢复 `LG-01..06` 这类按完整框架链拆分的迁移顺序；跨仓 owner 与里程碑边界同时服从 `docs/milestones/source-graph-realignment-2026-07-08.md`。
