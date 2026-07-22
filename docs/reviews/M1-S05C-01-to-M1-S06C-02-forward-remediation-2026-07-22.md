# M1-S05C-01 至 M1-S06C-02 前向修复记录

- 修复日期：2026-07-22
- 历史审查：`G:\agent-zoo\.tmp\M1-S05C-01-to-M1-S06C-02-audit.md`
- 修复 baseline：`6f99f7ec24c5617e21e768fea47d2f70617887af`
- 06A implementation：`60af927d4b74ae9785ca5176fdd505fcce8b318f`
- 05D implementation：`eb16281ec422ab3b0e3b8472434d3037c3d5c694`
- 相邻生命周期回归修复：`4c838265a90fb1059a9390c5d37ba6b52a47f114`
- 裁决：**FORWARD_REMEDIATION_PASS**

## 1. 结论与非追溯边界

本轮已关闭历史只读审查中仍未关闭的两个可实施 blocker：

1. 05D 不再依赖 type-only wire declaration 跨越有效生产代码最低线。新增 `3,826` 行 conservative executable TypeScript，并进入真实 provider dispatch 主路径。
2. 06A 的 OMP/Mnemopi supplementary intent、temporal、RRF/polyphonic 和 MMR 机制恢复为 TypeScript，由严格 typed protocol 接入 Python primary；禁用 TypeScript 模块时 retrieval 主路径 fail closed，不存在 Python ranking fallback。

本记录是 `6f99f7e..4c83826` 的前向补救证据，不修改原 slice implementation/evidence commit，也不把 2026-07-21 历史裁决追溯改写成 PASS。原历史结论继续保持：05C-01 FAIL、05C-02 FAIL、05D-01 BLOCKER、05D-02 FAIL、06A-01 BLOCKER、06A-02 BLOCKER、06B-01 PASS、06B-02 FAIL、06C-01 PASS、06C-02 FAIL。后来存在的修复和本轮新增实现只说明当前代码状态已经改善。

## 2. 来源角色、语言和 owner

| 能力域 | 来源角色与原语言 | 本轮落位 | owner 边界 |
|---|---|---|---|
| 05D provider control plane | opencode primary / TypeScript；OMP supplementary / TypeScript；Hermes resolver primary / Python；OpenHands conformance/reference | `packages/runtime/provider-control-plane/src/**` 新增 catalog reconcile、credential pool/refresh、dispatch lifecycle/cancellation、route health 和 executable message/provider/response codecs | TypeScript Provider Control Plane 持有 provider catalog、credential selection、route health 和 dispatch attempt；Python backend registry 继续持有 backend lifecycle。未产生第二 canonical owner |
| 06A retrieval/index | AgentScope primary / Python；OMP Mnemopi supplementary / TypeScript | `packages/memory/retrieval-algorithms/**` 与 `retrieval_typescript_port.py` | Python `MemoryIndexRuntime`、canonical memory store、job/generation/lease 继续持有状态；TypeScript 只执行无持久化副作用的 query analysis/ranking，不写 canonical DB |
| 05C/06B/06C | 沿用既有裁决 | 保留后续已存在的修复，并为组合验证发现的竞态/replay 问题补测试和小范围修复 | 未转移 event、curator、compact/restore 或 worker state owner |

05D-01 曾把 OpenHands 错列为 separate backend primary，这是历史事实；本轮只按 05D-02 起的前向口径将其作为 backend lifecycle conformance/reference，不修改旧 evidence。`Zyra-owned` 在本轮明确不等于 Python-only。

## 3. 05D 修复

### 3.1 有效行数

修复前严格重分桶把 `openai-responses.ts` 等 type-only wire declaration 排除在 executable production 之外，05D-01 距离 `9,000` 最低线的严格缺口为 `3,294`。本轮新增如下内容：

| 分桶 | 语言 | Git additions | Conservative effective | 计入生产最低线 |
|---|---:|---:|---:|---|
| 9 个新 executable production modules | TypeScript | 4,169 | 3,602 | 是 |
| 既有真实主路径集成 | TypeScript | 224 | 224 | 是 |
| remediation behavior tests | TypeScript | 525 | 0 | 否 |
| notice/docs | Markdown | 8 | 0 | 否 |
| 历史 type-only wire declarations | TypeScript | 0（未新增） | 0 | 否 |

本轮有效回补为 `3,826 > 3,294`。严格口径下 05D-01 当前为 `5,706 + 3,826 = 9,532 >= 9,000`；05D 父级为 `9,532 + 9,752 = 19,284 >= 18,000`。这里没有把 test、声明型 DTO、ledger、锁文件、文档或 generated/data 算入 production。

### 3.2 真实行为

- catalog discovery 使用 revision/epoch 做原子 reconcile，并拒绝 stale snapshot。
- credential pool 有 eligibility、rotation、cooldown、refresh version fence；401 会切换 sibling credential 并发起第二次真实请求。
- dispatch lifecycle 使用 request digest/idempotency claim、结果缓存、stale reconciliation 和 settlement fence。
- cancellation 是持久状态；在传输前取消时发送字节数为 0。
- route health/circuit/half-open/concurrency permit 在 `fetch` 前生效；429/capacity fallback 改变真实 route。
- OpenAI Responses 与 Anthropic codec 保持 tool call/result identity，拒绝 duplicate/orphan/synthetic pairing，并解码真实 non-stream response。
- stdio RPC 暴露上述 lifecycle 操作；这些机制不是 ledger-only 或 test-only。

## 4. 06A 修复

### 4.1 同语言 supplementary 恢复

新 TypeScript package 实现并导出：query intent、显式时间范围解析、半开 UTC boundary、polyphonic/RRF fusion、MMR、多级预算和确定性 tie-break。协议为 `zyra.retrieval-algorithms.v1`，使用有界 stdin/stdout JSON frame。

Python port 校验 protocol/version、request identity、candidate identity/content/voices、预算和 evidence digest，并设置请求/响应大小与 timeout 上限。TypeScript runtime 缺失、输出畸变或证据不一致时直接失败；不会回落到原 Python intent/temporal/RRF/MMR 实现。

| 分桶 | 语言 | Git additions | Conservative effective | 计入来源语言证据 |
|---|---:|---:|---:|---|
| retrieval algorithms production | TypeScript | 939 | 851 | 是 |
| validated process port | Python | 670 | adapter bucket | 否 |
| Python real-path integration/export | Python | 81 | production integration | 不替代 TS quota |
| behavior/mutation/disable tests | TS + Python | 333 | 0 | 否 |
| package/config/lock | JSON + lock | 29 | 0 | 否 |
| sync script/ledger data/notices | Python + JSON + Markdown | 42 | 0 | 否 |

OMP supplementary 没有数值型生产配额；`851` 行是裁剪后的可执行机制，不包含 type-only declaration、测试或协议 adapter。AgentScope Python primary 的 canonical store/index/job/lease 责任保持不变。

### 4.2 主路径与断开即失败

`MemoryIndexRuntime.retrieve()` 在真实检索路径中先调用 TypeScript analysis，再用结果约束 SQL/vector candidate hydration，最后调用 TypeScript ranking。调用方显式 intent 和 temporal filters 会被保留。测试替换 TypeScript 输出 digest、candidate identity 或 budget 即失败；禁用 TypeScript module 后检索失败，从而证明该模块不是仅由 import、fixture 或 ledger 触发。

## 5. 相邻回归修复

- API runtime-event bridge reset 现在持有 API lock 直到 registry release/close 完成，避免并发 getter 获得正在关闭的 bridge；新增确定性并发测试。
- 06C skill-memory 对已 APPLIED projection 的 replayed release 返回既有 applied receipt/event，不再与历史 worker request 产生错误冲突。
- worker-pool 与 graph-custody SQLite connection 的 context manager 现在在 commit/rollback 后真实关闭；workspace reset 同时释放 worker-pool API registry。

这些修复由 `4c83826` 独立提交，不计入 05D 或 06A 的生产有效行数。

## 6. 验证证据

| 范围 | 命令/运行时 | 结果 |
|---|---|---|
| 全 TypeScript workspace | `bun run typecheck` | PASS |
| 05D 原回归 | `bun test packages/runtime/provider-control-plane/test/provider-control-plane.test.ts` | 11/11 PASS |
| 05D remediation | `bun test packages/runtime/provider-control-plane/test/remediation-provider-control-plane.test.ts` | 9/9 PASS |
| 05D 双运行时 | `node --experimental-strip-types --test` 运行两个 provider test files | 20/20 PASS |
| 06A TypeScript | `bun test packages/memory/retrieval-algorithms/test/retrieval-algorithms.test.ts` | 6/6 PASS |
| 06A Python foundation/integration/TS port | focused pytest | 12/12 PASS，包含既有 64-concurrent retrieval case |
| 06A API + CodeWorker | focused pytest | 8/8 PASS |
| 05D Python port/backend/API/failover | focused pytest | 12/12 PASS |
| 生命周期组合 | event spine API、skill-memory compact/restore、worker pool、graph custody、worker-pool API | 24/24 PASS，189.53s |
| ledger | `sync_retrieval_index_source_ledger.py --check` | PASS |
| 静态边界 | `git diff --check`；parent-source runtime path 搜索 | PASS；0 hits |

关键 sensitivity cases 包括：predispatch admission/cancel 的 zero-byte 断言、idempotency digest drift、401 credential rotation、tool pairing mutation、TypeScript runtime disable、协议输出 mutation、bridge reset 并发和 replayed APPLIED release。

## 7. 限制与未声称事项

- BrowserWorker 本轮没有新增经 Provider Control Plane 发起真实 LLM 请求的独立路径；不能把 CodeWorker/API 的 PCP 证据冒充 Browser provider dispatch 证据。Browser 仍不取得第二 provider owner。
- 本轮不重新评价或改写历史 slice 的 baseline..implementation 结论，也不把后续补丁计入原实现。
- 本轮没有执行里程碑退出级 sealed 2,000-transition、真实 local/edge/cloud、完整 cleanroom 或全仓长回归；这些仍属于相应数字阶段聚合/里程碑退出门禁。
- 没有新增对 `../opencode`、`../oh-my-pi`、`../claude-code-best`、`../OpenHands`、`../browser-use` 或已删除 OpenClaw 的运行时依赖。

## 8. 最终裁决

对“当前代码是否按历史报告完成前向修复”的裁决为 **PASS**：05D 的真实 executable production 缺口已补足，06A 的 TypeScript supplementary 语言/迁移边界已恢复，关键失败、mutation 和 disable 路径均有回归证据。对“原十个 slice 是否因此追溯通过”的裁决仍为 **NO**；历史审查结论原样保留。
