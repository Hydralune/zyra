# M2-01A-01 至 M2-01B-02 数字阶段聚合严格审查

审查日期：2026-07-24

最终结论：**修复后通过**

## 1. 审查范围与冻结提交

本次审查严格覆盖以下四个 slice：

1. `M2-S01A-01` typed API client / transport；
2. `M2-S01A-02` workbench shell / command input；
3. `M2-S01B-01` canonical event stream / cursor ingestion；
4. `M2-S01B-02` canonical state store / projection recovery。

冻结边界如下：

| 边界 | Commit |
|---|---|
| M2-01 数字阶段基线 | `f53bf78287a2b2a6eec74102e7218d664307c137` |
| 原聚合 evidence target | `087a5afce5cc12b265733489d54a7214d8d938a5` |
| 原最终 implementation target | `7446f4cb57c4a75f416a5d6707ccf47c03a67cc3` |
| 本次 review-fix target | `0f460bb5fd33d8a47b45e9cf724bb9a58ff50d59` |

四个原实现提交分别是：

| Slice | Implementation commit |
|---|---|
| `M2-S01A-01` | `b4e6b5b554ff6ee8ebd786bddc239d00efa9cbc2` |
| `M2-S01A-02` | `4b7f5f8bd5bc3e2819921bc62d27daf36ed8e682` |
| `M2-S01B-01` | `6cd3f3fc259a5d39e96f81c79917ce3159d46efe` |
| `M2-S01B-02` | `7446f4cb57c4a75f416a5d6707ccf47c03a67cc3` |

审查开始时 Zyra 工作树干净。所有缺陷均归因到原实现或原 evidence
提交；没有把本次 review-fix 产生的问题倒算给原 slice。

## 2. 审查总结

原完成状态不能直接判定为通过。审查确认了七类实质问题：

1. `M2-S01A-01` readiness 固定声明全部 owner 可用，没有真实探针；
2. `M2-S01B-02` projection 并发持久化可能发生旧 revision 覆盖新 revision；
3. `M2-S01B-02` primary/fallback 双副本恢复会优先返回陈旧 primary；
4. `M2-S01B-02` IndexedDB 在 transaction commit 前就确认写入成功；
5. `M2-S01B-02` active route 在首个 event 到达前无法 pin；
6. `M2-S01A-02` Claude supplementary ledger 使用了不存在的 source commit；
7. 原聚合 evidence 缺少强制的 source-language custody，且 source audit
   将 target 硬编码为修复前提交。

以上问题均已在 `0f460bb` 修复，并增加了能在旧实现上失败的对抗测试。
`M2-S01B-01` 未发现需要修复的 slice 内实现缺陷。

## 3. 对实现的独立理解

### 3.1 M2-S01A-01

该 slice 的生产主链不是一个 fetch wrapper，而是：

`Typed request identity -> auth/version/correlation -> idempotency fence ->
retry/cancellation/circuit breaker -> receipt normalization -> Python canonical
task/checkpoint/event owners`。

`opencode` 是 TypeScript typed transport 的 primary implementation source；
`OpenHands` 补 task lifecycle，并把 Python API owner extension 接到 Zyra 的
SQLite task/checkpoint、runtime event spine、artifact 和 control runtime。
`oh-my-pi`、Agent Framework、AgentScope 只承担 conformance。

本次修复前，`/runtime/readiness` 绕过了上述 owner 事实，直接返回全部
`true`，属于任务书明确排除的固定 health 假证据。

### 3.2 M2-S01A-02

该 slice 将 workbench shell、router、task list/detail、command parser、
completion、history、draft、priority queue、overlay、focus 和 route loader
接到真实 typed API。OpenCode 保持 shell/router primary；Claude 只补成熟的
prompt/queue/focus 机制；OpenHands 补 conversation route/retry/状态视图。

command queue 的 `now > next > later` 与同优先级 FIFO 会真实改变 dispatch
顺序；disable path 会在提交前阻断，不生成假 task。

### 3.3 M2-S01B-01

该 slice 的 canonical ingress 链是：

`subscribe-before-snapshot -> identity/digest validation -> cursor ledger ->
ordered buffer -> gap recovery -> reconnect/SSE/long-poll -> canonical batch`。

它不拥有 UI entity state，只拥有 event ingress、ordering、cursor 与 transport
health。OpenCode 是 primary；OpenHands 仅补 SSE/event store 与 Python API
边界。对 malformed frame、sequence conflict、same-sequence/different-digest、
gap、orphan、tombstone 和 disable 均为 fail-closed。

### 3.4 M2-S01B-02

该 slice 的唯一 canonical UI state owner 是
`typescript.CanonicalProjectionStore`。Reducer、settlement、orphan、
history fold、causality、selectors、retention、snapshot migration 和
persistence 都围绕该 owner 工作；React panels 只消费 selectors。

浏览器关闭只关闭本地 projection/binding，不取消 backend task。active route
的 pin 是 retention 保护事实，IndexedDB/localStorage 是 projection snapshot
副本，不是第二 canonical backend owner。

## 4. 缺陷与修复

| ID | 严重度 | 原始责任提交 | 问题 | 修复 |
|---|---|---|---|---|
| `M2-01-RF-001` | High | `b4e6b5b` | readiness 固定为全部成功 | 对 task/checkpoint/receipt table、artifact write、control runtime 和隔离的 TypeScript event owner 做真实探针；缺项 fail-closed |
| `M2-01-RF-002` | High | `7446f4c` | 并发 save 可发生旧 snapshot 最后落盘 | `ProjectionPersistenceCoordinator` 串行 revision write；clear 先 flush |
| `M2-01-RF-003` | High | `7446f4c` | 陈旧 primary 遮蔽更新 fallback | 校验两副本，按 revision 选择；同 revision 不同 checksum 拒绝恢复 |
| `M2-01-RF-004` | Medium | `7446f4c` | IndexedDB request success 被误当作 transaction commit | 只在 `transaction.oncomplete` resolve；error/abort reject |
| `M2-01-RF-005` | Medium | `7446f4c` | 首 event 前 pin 被静默丢弃 | 为规范化 task id 建立零事件 pinned runtime，并由后续 reduction 保留 |
| `M2-01-RF-006` | Medium | `4b7f5f8` | ledger source commit 不存在 | 修正为 `c57f5a29e88e9a814bea47abeb9a0a6f725dc102`，在该 commit 读取 source blobs |
| `M2-01-RF-007` | High | `087a5af` evidence | 缺语言托管；audit target 硬编码 | 新增聚合 custody evidence；source/target 均从固定 commit 审计 |

主要修复路径：

- `apps/api/zyra_api/main.py`
- `apps/api/zyra_api/typed_transport.py`
- `apps/web/src/state/persistence.ts`
- `apps/web/src/state/store.ts`
- `apps/web/test/canonical-projection-store.test.ts`
- `tests/integration/test_m2_typed_api_client_transport.py`
- `scripts/audit_m2_01_source_to_target.py`
- `packages/integrations/zyra_integrations/data/internalization_ledger_seed.json`

## 5. 真实行为、失败路径与动态可达性

已验证的真实主路径包括：

| 能力 | 动态入口 | 语义效果 |
|---|---|---|
| Typed transport | real embedded HTTP server | create/resume/cancel、receipt replay、auth/version/correlation、disable |
| Workbench | default React entry + route loader | route/task projection、command dispatch、cancel/resume 后端调用 |
| Event ingress | runtime event spine API | SSE reconnect、long-poll fallback、cursor/gap recovery |
| Projection | `WorkbenchRuntime -> CanonicalProjectionStore` | event 改变 task/worker/tool/artifact/panel selectors |
| Persistence | store persist/restore | committed cursor、revision、replica recovery |
| Disable | transport/ingress/projection kill switch | 明确报错，不走隐藏 fallback |
| Browser close | projection close | 后续本地 mutation 被拒绝，backend task 不被 cancel |

新增的四个 projection 对抗测试分别证明：

1. 首 event 前 active route pin 可达且后续 reducer 不会丢 pin；
2. 慢旧写入不能覆盖新 revision；
3. fallback 中的新 revision 能战胜陈旧 primary；
4. IndexedDB transaction abort 不能被 request success 掩盖。

这些测试在原 `7446f4c` 语义上会失败，因此满足“断开即失败”和语义效果要求。

## 6. 来源、语言托管与严格内化

### 6.1 固定来源抽样

实际读取并对照了至少三条 source-to-target 链：

1. `opencode@adf178a6.../packages/protocol/src/middleware/authorization.ts`
   的 `Authorization`，对照 typed client 的 auth/version/idempotency/correlation
   路径；
2. `claude-code-best@c57f5a29.../src/utils/messageQueueManager.ts`，
   对照 Zyra command queue/coordinator 的 priority/FIFO dispatch；
3. `OpenHands@c105a823.../openhands/app_server/event/event_store.py`，
   对照 ordered ingress、canonical projection 与 durable restore。

聚合 source-to-target 审计结果：

- 17 条 ledger entries；
- 14 条 production implementation entries；
- 3 条 conformance-only entries；
- 60 个固定 source files；
- 81 个 target bindings；
- 0 个 blocking findings；
- 唯一非阻断项是 Agent Framework 的 AG-UI prose conformance descriptor。

### 6.2 语言托管

`scripts/verify_source_language_custody.py` 已在
`f53bf782..0f460bb` 上 fail-closed 通过：

- OpenCode 与 Claude 的生产路径保留 TypeScript/TSX；
- OpenHands 的 same-language Python owner 与 TypeScript UI/ingress 路径均存在；
- `M2-S01A-01` Python owner extension 有显式 decision id；
- `M2-S01B-02` OpenHands Python-to-TypeScript bounded behavior port 有显式
  cross-language decision id；
- 0 violations。

### 6.3 非伪内化判断

通过理由不是 ledger 或文件存在，而是：

- 生产模块位于 Zyra-owned `apps/**`、`packages/**`；
- 状态、事件、权限、receipt、cursor、projection、错误和测试均使用 Zyra
  contracts；
- 真实 API/React/runtime 入口可达；
- disable/abort/corruption/replica race 会改变行为测试结果；
- cleanroom 不包含 sibling source repositories 仍可安装、构建和运行；
- 没有 `../opencode`、`../OpenHands`、`../claude-code-best` 运行依赖。

## 7. 有效行数复核

所有数字都从原 implementation interval 重新计算；本次修复行没有用于倒填
原最低线。

| 范围 | Effective | Minimum | 结果 |
|---|---:|---:|---|
| `M2-S01A-01` | 7,002 | 6,000 | PASS |
| `M2-S01A-02` | 6,674 | 6,000 | PASS |
| `M2-01A` parent | 13,625 | 12,000 | PASS |
| `M2-S01B-01` | 9,600 | 7,500 | PASS |
| `M2-S01B-02` | 8,382 | 7,500 | PASS |
| `M2-01B` parent | 18,133 | 15,000 | PASS |
| `M2-01` stage | 29,504 | 27,000 | PASS |

production、test/mock、type/schema、presentation、adapter、docs、generated、
vendor/source-pool 分桶保持分离；test、docs、ledger 和修复 evidence 没有计入
原 slice effective production。

## 8. 最终 target cleanroom

cleanroom：
`G:/agent-zoo/.tmp/m2-01-aggregate-cleanroom-0f460bb`

固定 target：
`0f460bb5fd33d8a47b45e9cf724bb9a58ff50d59`

结果：

| 命令/门禁 | 结果 |
|---|---|
| Bun frozen install | PASS；首次 sandbox cache 权限失败，获得 scoped cache access 后按 lockfile 完成 |
| 全 TypeScript `typecheck` | PASS |
| `test:web` | 88 passed，0 failed，342 assertions |
| `build:web` | PASS |
| cleanroom Python import-origin assertions | PASS；API、memory、runtime 均从 cleanroom 导入 |
| M2-01 相邻 Python integration set | 17 passed |
| source-to-target audit | PASS |
| source-language custody | PASS |

cleanroom 不包含 sibling source repositories，仍完成以上运行，证明当前 M2
能力没有根目录来源仓库运行依赖。

## 9. 全仓测试的独立归因

本次没有把全仓测试缺口隐藏为“未运行”：

1. 原样执行 `pytest tests`，收集阶段被 6 个历史 runtime foundation 测试阻断；
2. 原因是 `packages/runtime/zyra_runtime/__init__.py` 的旧公共 export 只列在
   `__all__`，没有实际 import；
3. 该文件从 M2 基线 `f53bf782` 到 review target `0f460bb` 完全未变化；
4. 排除这 6 个已归因收集阻断后继续运行 30 分钟，在约 27% 触发硬超时且已有
   多项失败；
5. 用 `-x` 取得的首个失败是 browser permission exact-resume；
6. 同一个测试在 M2 基线 `f53bf782` 也失败，确认不是本次 M2 修复引入。

因此：

- 这些结果不能记为全仓通过；
- 它们也不能归因给本次四个 M2 slice；
- 按已完成保护边界，本次审查不越权修改受保护的 M1 runtime/public-export
  和 browser permission owner；
- M2 scoped + adjacent 验证已经在最终 cleanroom target 全部通过。

`verify_submission_boundary.py` 还会把 audit/recovery 代码中用于检测外部路径的
deny-list 字符串本身误报为依赖。相关 scanner 和命中文件同样是 M2 基线前
代码。实际 cleanroom 无 sibling repositories 仍成功运行，是本次范围内更直接
的无外部运行依赖证据。

## 10. 最终结论与剩余风险

最终结论：**M2-S01A-01 至 M2-S01B-02 在 `0f460bb` 修复后通过。**

通过范围包括：

- 四个 slice 的源码、父级和数字阶段有效行数；
- typed API、workbench、event ingress、single canonical projection owner；
- 真实行为、失败路径、disable、replica/transaction recovery；
- 固定来源提交、source-to-target、语言托管与 bounded cross-language 例外；
- 最终 target cleanroom 的安装、typecheck、88 个 Web tests、构建与 17 个
  Python adjacent tests。

未关闭但已独立归因的风险：

1. High：M1 历史 `zyra_runtime` public export 导致 6 个全仓测试无法收集；
2. Medium：排除收集阻断后的全仓 Python 长程套件超过 30 分钟且有基线失败；
3. Low：legacy submission-boundary scanner 对 deny-list 审计字面量产生假阳性。

这些风险不应写成 M2 已修复，也不应阻塞已验证的 M2-01 scoped 结论；后续应在
对应 M1 保护边界复审或数字阶段全仓治理任务中单独处理。

机器可读证据：
`docs/reviews/evidence/M2-01A-01B-aggregate-review-2026-07-24.json`
