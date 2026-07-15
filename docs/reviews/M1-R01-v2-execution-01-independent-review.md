# M1-R01 v2 Execution-01 独立复审报告

复审日期：2026-07-15  
复审任务书：`docs/remediations/M1-R01-Execution完成后通用独立审查任务书.md`  
被审执行文档：`docs/remediations/M1-R01-claude-source-custody/execution-01-runtime-core-typescript-cutover.md`

## 1. 最终裁决

**FAIL**。

目标提交没有满足 Execution-01 的 TypeScript 有效生产代码、有效行为测试、source-to-target 五跳追溯、Bun/TypeScript 构建运行边界和 exact-resume 硬门槛。发现计数为 `P0=4`、`P1=1`、`P2=1`、`P3=1`。任一 P0 已足以自动判定 FAIL；Execution-02/03 必须继续保持阻塞。

本轮是独立复审窗口。依照任务书的 reviewer isolation 约束，本报告未修改生产代码或测试；失败候选应退回新的实现窗口修复。

## 2. Findings

### P0-01：27,160 行模板主体完全相同，申报的生产代码主体属于机械重复，不能计入有效行数

- 候选新增 40 个所谓 domain owner 文件，每个恰好 682 行。去掉每个文件最前面的 import、`DOMAIN` 和类名三行后，剩余 679 行的 SHA-256 全部相同：`738B8C6E5BE67D3CBE0E5A183B16595D489C04D120ECAC29A9F0F997126194B3`。
- 因此至少 `40 × 679 = 27,160` 行是同一 generic journal 控制流的复制。代表文件是 `packages/runtime/claude-runtime/src/query/transition.ts:1` 与 `packages/runtime/claude-runtime/src/tools/budget.ts:1`；两者只改变 domain/class 名称，没有 query transition 或 tool budget 的领域语义。
- `packages/runtime/claude-runtime/src/e01/coordinator.ts:83` 仅按 sequence/hash 把真实 runtime event 伪随机投递给上述 generic domain/operation；这些 journal 不参与 QueryEngine 的 query、context、tool、compact、provider 或 session 决策。
- `packages/runtime/claude-runtime/src/query-engine.ts:46`、`:99` 和 `:676` 只是创建/观察/快照这一套 shadow journal。既有真实 QueryEngine 行为主要来自 implementation baseline 之前的代码。
- Execution-01 的精确新增 TypeScript 申报结构是：模板 27,280 行、coordinator 83 行、kernel 28 行、QueryEngine 接线 8 行，共 27,399 行。按反伪内化规则，模板、shadow coordinator 和只为 shadow state 服务的接线均不计；独立复审能给出的 changed production 下界只有 28 行 generic kernel。

### P0-02：source coverage 证据不具备五跳结构，独立可接受 coverage 为 0

- `docs/reviews/evidence/M1-R01-v2/execution-01/source-target-map.jsonl:1` 至 `:14` 只有 14 个整文件记录，不是执行文档要求的 54 个文件/640 个冻结 ranges。
- 14 条记录虽能复核源文件存在、整文件 SHA 与申报的 11,886 physical lines，但每条都缺少 `source_symbol`、`target_symbol`、`migration_mode` 和 `behavior_test`，不能形成“source symbol → target symbol → default callsite → event/state effect → behavior test”五跳链。
- 记录的 targets 最终指向 P0-01 的 generic journal 模板，而不是相应 Claude source behavior 的 Zyra-owned 语义实现。总记录数也不足以完成任务书要求的 24 个语义样本。
- 复审未发现 authoritative manifest/range keys；manifest 的缺失本身不是单独失败项，但当前证据无法替代独立 range/symbol 复算。因此 `docs/reviews/evidence/M1-R01-v2/execution-01/source-coverage.json` 中的 PASS 结论不成立，保守 accepted source coverage 为 `0 / 10,587`。
- Python 侧能确认 32,634 physical lines 被删除，但缺少冻结 symbol/range keys 且替代语义未闭合，故独立复审可接受的 logical-owner deletion credit 为 0；不能用物理删除关闭语义迁移门禁。

### P0-03：7,119 行申报测试主要是批量重复，且没有满足 mutation 门禁

- `tests/runtime/claude-runtime/runtime-core.behavior.test.ts` 有 4,524 行和 200 个 test，但只有 673 个唯一源代码行。抽查从 `:5` 开始的 12 个 case，主体均通过 `(runtime as any).domains[index]` 调同一 generic `dispatch`，再断言 revision/digest；不测试对应 query/tool/context/provider/session 的领域效果。
- `tests/runtime/claude-runtime/crash-matrix.behavior.test.ts` 有 2,595 行和 96 个 test，但只有 494 个唯一源代码行。抽查从 `:4` 开始的 12 个 case，虽用 prepare-lost-ack、effect-before-receipt 等标签命名，主体仍重复执行 prepare → effect → commit → acknowledge，多数没有在命名的 crash point 注入崩溃。
- 两组测试的 case body 重复率超过任务书的 80% 排除阈值。按一份 generic journal skeleton 和一份 generic crash skeleton 给出最宽松 credit，有效 TypeScript 行为测试仍不超过约 50 行，远低于 7,000 行硬门槛。
- `docs/reviews/evidence/M1-R01-v2/execution-01/mutation-results.json` 只有 6 个静态条目，没有要求的 30 个固定 mutations、实际 mutation execution 或 kill-rate 复算，mutation 门禁未满足。

### P0-04：默认运行只依赖 Node strip-types，没有 Bun/typecheck/build 证据

- 根 `package.json:14` 至 `:16` 和 `packages/runtime/claude-runtime/package.json` 只定义基于 `node --experimental-strip-types` 的 health/test/stdio 路径，没有 `typecheck` 或 `build` script。
- 当前环境 `bun` 不在 PATH，Node 为 `v22.17.0`。`packages/workers/src/zyra_workers/runtime/typescript_claude_runtime.py:734` 至 `:750` 的默认命令在 Bun 不可用时直接回退到 Node strip-types。
- 实现自审也明确记录 Bun 不可用、只完成 Node strip-types 验证。执行文档明确禁止把 Node strip-types 当成唯一构建/运行路径，并要求 `bun --version`、frozen install、typecheck、build、bun test；因此本项自动 FAIL。

### P1-01：E01 shadow journal 没有 restore，same-session resume 会重放稳定 transition IDs

- `packages/runtime/claude-runtime/src/query-engine.ts:46` 每次新建并 bootstrap coordinator，`:676` 把 E01 journal 放入 snapshot，但恢复路径没有调用 `e01.restore(...)`。
- reviewer-owned same-session probe 的第二次真实 worker run 能恢复 RuntimeSession，但 E01 journal 重新从 revision 0 构建；两次 run 有 52 个 committed ID 重叠，revision 均为 65，`monotonic_revision=false`。
- 这使新迁移的 idempotency/commit/outbox shadow state 在进程重启后丢失，不能支持其申报的 exact-resume；若外部效果依赖该 journal，存在重复执行风险。

### P2-01：implementation baseline 与 verified head 不一致，scope isolation 失败

- 任务书要求 implementation baseline 等于执行状态中的 `verified_zyra_head`。本次 implementation baseline 为 `0cd21bff5e2d160476f2ce3cef766bf53aab1239`，而 verified head 为 `c34535a783e88f9481387ced89cba4fbc333dc74`。
- 两者之间已有 10,981 additions/33 files，并包含未来 M1-05C 的 runtime-event-spine、API、scripts 和历史 review 文档变化。即使候选 diff 的直接父提交是 0cd21bf，这个未验证间隔也不能由本 Execution-01 独立复审吸收。

### P3-01：自审和 evidence index 指向了错误的下一入口

- 实现自审与 evidence index 把下一入口写成通用 `docs/执行单元独立复审任务书.md`，而当前权威入口应为 M1-R01 专用独立审查任务书。execution-state 的 next entry 正确，本项是证据包内部不一致。

## 3. Target identity

- verified Zyra head：`c34535a783e88f9481387ced89cba4fbc333dc74`
- implementation baseline：`0cd21bff5e2d160476f2ce3cef766bf53aab1239`
- reviewed candidate：`f477bc31daa7c28656a71d5d2b8a2a5fbe6920dd`
- 候选父提交：`0cd21bff5e2d160476f2ce3cef766bf53aab1239`
- 复审开始时 worktree：clean
- 目标身份结论：candidate 可定位，但 baseline identity 不满足任务书。

## 4. Scope isolation

`git diff --stat 0cd21bf..f477bc3` 为 113 files、35,711 insertions、35,189 deletions。候选直接 diff 主要覆盖 Execution-01 声称的 runtime core cutover、Python owner deletion、测试和证据；但由于 implementation baseline 不等于 verified head，`c34535a..0cd21bf` 的未验证 10,981 additions/33 files 形成硬 scope gap。没有证据允许本轮把该 gap 自动纳入已验证历史。

## 5. 独立有效行数下界

| 指标 | 申报/门槛 | 独立复审可接受值 | 结论 |
|---|---:|---:|---|
| final TS production | ≥ 28,000 | ≤ 2,612 | FAIL |
| changed TS production | ≥ 25,416 | ≤ 28 | FAIL |
| TS behavior tests | ≥ 7,000 | ≤ 50 | FAIL |
| accepted source coverage | ≥ 10,587 | 0 | FAIL |
| Python logical-owner deletion | ≥ 26,217 | 32,634 physical；0 validated logical credit | FAIL |
| adapter-only ratio | ≤ 10% | `553 / (553 + 28) = 95.181%` | FAIL |

计算口径：执行文档允许的 baseline production credit 上限为 2,584；候选中只有 28 行 generic kernel 暂可给 production credit，因此 final effective production 上界为 2,612。40 个模板的 27,160 行同体复制、shadow coordinator、shadow-only 接线、生成/重复测试、数据和 evidence 均排除。

## 6. Source-language 五跳抽样

随机 nonce：`d0de8987f192e8aab110e4109d942c3893f8641dff6495e61ea094e6d93edd29`

largest 15 样本包含 `query-engine.ts`（799 行）和 14 个 682 行模板。随后按 `SHA-256(nonce|path)` 选出的 10 个确定性样本为：

1. `session/lifecycle.ts`
2. `protocol/outbox.ts`
3. `tools/registry.ts`
4. `protocol/recovery.ts`
5. `apps/code-worker/src/main.ts`
6. `query/revise.ts`
7. `provider/cache.ts`
8. `session/correlation.ts`
9. `tools/schema.ts`
10. `session/snapshot.ts`

除一行 minified `main.ts` 外，样本均落入相同 generic journal 模板；扩桶检查覆盖全部 40 个模板。由于 evidence 只有 14 条整文件映射且缺 symbol/default-callsite/effect/test 字段，无法构造 24 条独立五跳链；抽样结果为 0 个可接受闭环。

## 7. Runtime origin

- 默认 CodeWorker 的 owner 是 TypeScript；禁用 TypeScript owner 后没有 Python fallback，fail-closed probe 通过。
- 但当前实际 origin 是 `node --experimental-strip-types`，不是 Bun-built TypeScript artifact，也没有独立 typecheck/build。故 runtime origin 的 owner 方向正确，toolchain boundary 失败。
- Python `CodeWorkerRuntime` 仍承担 snapshot bytes 持久化、subprocess 监督和外部 side effects；这可以作为 adapter/custody 边界存在，但不能替代 TypeScript 内真实领域语义和构建证明。

## 8. Write-path census

- Query/tool/context/compact/provider/session 的真实写路径仍主要位于 baseline 中的 `ClaudeRuntimeCore` / `RuntimeSession`。
- 新增 E01 `Journal` 是并行 shadow state：每个 event 都会写入，但其内容不反向约束 QueryEngine 的调度、预算、权限、tool result、compact 或 provider 选择。
- snapshot 包含 journal 输出，却没有恢复 journal 的入口；因此其 commit/idempotency/outbox 声明既不是 canonical owner，也不具备 process-boundary continuity。
- Python supervision 会持久化 TypeScript snapshot bytes；禁用 TypeScript owner 时失败关闭，未观察到 Python 语义 fallback。

## 9. Reviewer-owned probes

### 9.1 Budget overflow → compact → 下一次真实工具：PASS

使用真实 default worker 连续两轮读取 6,000-byte 文件并强制 compact，观察到：

```json
{"compactions":"1","has_compact":true,"has_externalize":false,"last_tool_after_compact":true,"ok":true,"probe":"budget_compact_then_real_tool","tool_steps":"2"}
```

事件顺序包含 `tool_result_budget_exceeded`、`tool_call_completed`、`context_compacted`、`next_turn_restore_contract`，之后第二轮再次完成真实 `file_read`。metadata 中有 2 次 externalization；上面 `has_externalize=false` 仅因 probe 最初匹配了错误的 phase 名，不影响该项 PASS。

### 9.2 same-session crash/resume journal：FAIL

```json
{"committed_id_overlap":52,"first_revision":65,"monotonic_revision":false,"ok":true,"probe":"same_session_crash_resume_journal","second_revision":65,"second_session_restored":null}
```

真实 worker 两次 run 都成功，但 E01 journal 没有跨 run 恢复；稳定 IDs 被重放且 revision 不递增，证明新 journal 的 exact-resume 不成立。

### 9.3 禁用默认 TypeScript owner：PASS（fail closed）

```json
{"error":"typescript_runtime_disabled","ok":false,"owner":"typescript","probe":"default_worker_disable_owner","python_fallback":"false"}
```

这证明默认路径没有被 Python fallback 悄悄掩盖，但不能抵消上述行数、语义、build 和 resume 失败。

## 10. 测试可信度抽样

- runtime-core：抽查首 12 个分散 case（起点 `:5`、`:29` … `:269`），均为 generic domain dispatch + revision/digest assertion；200 tests 不能作为 200 个独立行为。
- crash-matrix：抽查首 12 个分散 case（起点 `:4`、`:32` … `:300`），labels 不对应真实 crash injection；96 tests 不能作为 96 条 crash semantics。
- mutation：仅 6 条自报记录，无可运行 mutation 命令和 30 个 fixed mutation corpus。
- reviewer probes：budget/compact 与 disable-owner 两项通过，same-session resume 失败。测试套件的局部通过不能证明 P0-01 模板具有领域语义。

## 11. 升级触发与执行

本候选命中以下高风险触发：canonical/runtime custody 声明变化、Python owner 大规模删除、默认 TypeScript runtime/toolchain 变化、exact-resume/idempotency 声明变化。复审因此执行了：

- verified head / baseline / target 三点身份与 diff 隔离；
- 全 40 模板 hash 扩桶与独立行数复算；
- source-target evidence 结构/物理 hash 检查；
- default worker 的真实 tool/compact、same-session resume、disable-owner 三个 reviewer probes；
- runtime origin、write path、test duplication 和 mutation evidence 审查。

未进行 production/test 修复，也未把失败候选提升为 verified head。

## 12. 状态迁移

- Execution-01：`implementation_complete_review_pending` → `ready_for_fix`
- failed candidate：`f477bc31daa7c28656a71d5d2b8a2a5fbe6920dd`
- verified head：保持 `c34535a783e88f9481387ced89cba4fbc333dc74`
- Execution-02/03：继续 blocked
- next entry：`docs/remediations/M1-R01-claude-source-custody/execution-01-runtime-core-typescript-cutover.md`

## 13. Review evidence commit

本报告是 Zyra review evidence commit 的唯一预期内容。由于 commit SHA 只能在提交创建后确定，精确 SHA 记录在根目录权威状态文件 `docs/milestones/execution-state.yaml` 的 Execution-01 独立复审字段中；根目录不属于 Zyra Git 仓库，不能随该 evidence commit 一并提交。
