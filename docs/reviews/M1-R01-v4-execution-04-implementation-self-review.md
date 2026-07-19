# M1-R01 E04 实现累计自审

## 1. 结论与候选绑定

- 状态：`implementation_complete_review_pending`
- 实现候选：`a2c8b8e894f558974018d33416e010b95c7e6f65`
- 实现候选 tree：`93460d639b3b539fd0253be70d488c7fffa41252`
- E04 进入前 Zyra 基线及继续受保护的 `verified_zyra_head`：`299b708d3559da7a5da1f9d6d55d2d1f1b155249`
- G0 tooling/manifest 快照：`509c1a4c6e688e899ba1930cd5779f18c18c1176`
- 最终候选 gate：10/10 checks 通过；这里只证明实现窗口已闭合，不宣告 E04 独立 PASS，也不恢复 M1-S05C-01。

E04-A 至 E04-E 的实现/evidence commit 分别为：

| Slice | 实现 commit | Evidence commit |
|---|---|---|
| 04A | `f69bbd5bb06a4b335e7b4730ec2d5a785c0302ac` | `a3404e210a4390780abc0442d4aa57041e7a7e53` |
| 04B | `01ca151bd717e792a2db512e24cd82dab2b4c577` | `29207737834785daa6e0ef557b5ab04e28fd615a` |
| 04C | `102761e6264849e27d056931db6b1bde4c3dfcc2` | `67a4f570a184ea190f66003a49c0f445d3c470b8` |
| 04D | `4ef6d8c5b68054b3883bc20764165fecfbd0a440` | `22b06895ae2d57e842b6d0ae83bf49d32e8616ce` |
| 04E | `60bfe4984a1b2d2ac1820446b7f532c50e6e2cee` | `4534a9ce9eda7e2b7021deab90793e1ea933bf95` |
| 04F | `a2c8b8e894f558974018d33416e010b95c7e6f65` | 本自审随最终 E04 evidence commit 提交 |

## 2. 源码回收与当前 reconstruction 裁决

G0 冻结了 `claude-code-best` 的 18 个 primary source ranges，覆盖 8 个语义域；18 项均按 `adapted_migration` 取得 recovery credit。没有用目录搬运、vendor 黑箱、机械 TypeScript/Python 转写或精确文本相似度代替控制流回收。

| 语义域 | 上游成熟机制 | Zyra 正式 owner/主路径 |
|---|---|---|
| query loop | `QueryEngine.ask`、`queryLoop` | `QueryLifecycleRuntime`、`ClaudeRuntimeCore`、stdio default entry |
| session/context/compact | `autoCompactIfNeeded`、`processResumedConversation` | `CompactionSourceCustodyRuntime`、`CompactRestoreRuntime`、checkpoint restore |
| tool orchestration | `runTools`、`partitionToolCalls`、`enforceToolResultBudget` | `ToolExecutionRuntime`、`ToolResultRuntime`、permissioned physical tool host |
| permission | coordinator permission handler、rule evaluation | `PermissionHookRuntime`、`PermissionEvaluator`、E02 continuation/permit |
| MCP | connect/reconnect client lifecycle | `McpConnectionRuntime`、`McpClientRuntime`、E02 MCP route |
| skill/plugin/command | skill load/fork、plugin hook load | `TypeScriptSkillRuntime`、`SkillReloadRuntime`、E02 capability coordinator |
| agent/subagent | run/resume background agent | `runAgent`、`AgentBackgroundSupervisor`、E03 durable agent control |
| isolation/control | worktree creation、async task kill | `IsolationRequestRuntime`、`TypeScriptControlRuntime`、SandboxGateway |

保留的 reconstruction 是 Zyra 已有的 typed contracts、E01/E02/E03 coordinator 外壳、event/artifact schema，以及 Python 的进程耐久化和物理副作用 host。它们继续承担 Zyra 数据结构与平台责任，但不再冒充 Claude logical-control source。

被替换或收紧的部分包括：Python 成功 fallback、terminal result 先于最终 checkpoint 的窗口、permission ask 的非精确恢复、multi-tool 失败后的未取消 pending call、foreground agent 空 ACK、并发 registry 非串行 CAS、跨 lease 复用旧 isolation receipt，以及绕过 managed workspace gateway 的隐式文件访问。E04 没有为了维持旧报告而保留这些行为。

没有 direct/cropped 类条目。原因不是重新发明同义 runtime，而是 18 个 selected ranges 都必须适配 Zyra 的 durable journal、permission continuation、runtime event spine、artifact、workspace lease 和 terminal ACK 协议；目标控制流保留证据记录在 `target-provenance-report.jsonl`。`reimplementation-exceptions.json` 为空。

Python stdio/事件/API/workspace 接线、候选验证脚本和 cleanroom 编排属于 platform glue 或验证基础设施，不取得 source-recovery credit。line bucket 将两个 Python durable port 文件单列为 `adapter-only`，共新增 489 行。

未选择的 `claude-code-best` 代码、`opencode` 和 `OpenClaw` 快照没有迁入 E04 production。它们没有获得 G0 recovery range；在 8 个冻结域已经存在单一 Claude primary owner 后继续搬入会制造重复 owner 或扩大 E04 边界。它们也没有成为运行时依赖。其余整仓内容仍只可作为后续经 source-role 裁决的来源，不计入本次完成。

## 3. 默认调用链和状态责任

实际默认链为：API task route / task graph → `CodeWorkerRuntime` → Python durable physical host → TypeScript source 或 built stdio entry → `ClaudeRuntimeCore` / query lifecycle → E02 permission、MCP、skill/plugin/command → tool/agent/isolation physical ports → final capability close checkpoint → durable `run.result.ack` → `run.closed`。

- TypeScript 是 query、permission、route、compact、skill/plugin/command、agent logical transition 和 terminal protocol 的 canonical owner。
- Python 只承担进程生命周期、durable checkpoint/receipt、API/event projection、真实工具副作用及 workspace gateway；census 的 1,619 个 Python symbols 中 logical-owner/fallback 记录为 0。
- permission ask 先投影到 Python queue，但恢复时把 exact approval response 返回 E02；logical worker request ID 在 suspend/resume 间稳定，错误 binding fail closed。
- terminal result 必须在 closing checkpoint 的 durable ACK 之后发送；lost/duplicate ACK 恢复不重复物理工具效果，也不产生第二份 terminal receipt。
- managed workspace 请求通过 SandboxGateway/lease；resume 会把 isolation request/receipt 重新绑定到新 lease，不复用旧物理身份。
- 断开 TypeScript runtime、8 个 domain owner 或 workspace gateway 时，对应真实路径失败；没有 Python logical fallback。

## 4. 动态可达性、失败路径和断开即失败

- source Bun、built Bun、built Node 三个 stdio probe 均完成 35 frames、16 closing checkpoints、单一 terminal result/closed，并且 terminal 后请求数为 0。
- Python bridge 实际启动 TypeScript；API route 产生 74 个事件并把 task 推进到 `completed`。
- crash matrix 的 resume、lost ACK、duplicate ACK、disable 四项全部通过。
- 36 项 E04 source-recovery 行为测试观测到 manifest 要求的 32 个 test IDs，8/8 domain closure 全部通过。
- 13 个 G0 mutation operators 全部编译、被 exact killer 杀死、恢复原 hash 后 killer 通过；残留 mutation backup 为 0。
- domain disable tests 直接断开 query、compact、tool、permission、MCP、skill、agent、isolation owner；terminal-order mutation 会让 closing checkpoint 消失并被 source stdio probe 杀死。

## 5. 构建、测试与 cleanroom

最终候选重复验证结果：

- `bun run typecheck`：通过。
- `bun run build`：通过；Bun/Node 各生成约 4.48 MB bundle。
- Claude runtime + MCP 全量 TypeScript：1,257 pass，0 fail。
- E04 专项：36 pass，0 fail。
- 四组 Python candidate integration suites：27 pass，0 fail。
- default-path 五条入口、terminal crash 四类、13 项 mutation：全部通过。
- detached cleanroom：删除 `vendor`、`vendor-runtimes`、node_modules、dist 和缓存后，执行 frozen install、typecheck、build、全量 TS、27 项 Python 与六类 probe；6/6 commands 通过，unexpected dirty paths 为 0，forbidden source roots 为 0。
- dependency audit：forbidden runtime dependency 为 0；没有 `../claude-code-best`、`../opencode`、`../OpenClaw`、npm link、editable parent path 或 source-pool 主路径。

## 6. 有效行数分桶

以 E04 基线 `299b708…` 到实现候选比较：总新增 9,330 行、删除 549 行。其中 production 5,111/411，test 2,804/111，adapter-only 489/27，docs 923/0，data 3/0；generated 与 vendor-like/source-pool 新增均为 0。E04 没有 LOC 最低线，这些数字只用于阻止把 docs、data、adapter 或 source pool 当成深度回收。

## 7. 自我反驳与剩余停止点

本自审不能证明独立 reviewer 的 source-range 抽样一定认可 retained control flow，也不能替代 reviewer-owned terminal-order probe、dependency/process audit、cleanroom 重放和 mutation 复验。token overlap 仅作为诊断，不是迁移成败的独立裁决。

因此当前只能进入 `implementation_complete_review_pending`：候选 evidence commit 形成后必须停止，由 `docs/remediations/M1-R01-E04完成后独立审查任务书.md` 接管。只有独立 PASS 才能推进 `verified_zyra_head`、关闭 M1-R01 并恢复 M1-S05C-01。

## 8. 2026-07-19 独立审查后修复补充

首次独立审查的历史结论保持 FAIL；其四项发现已在候选 `2340b792d184bc7625ba15fe7e9d958c3daa4c66` 修复。更新后的事实取代本文件第 4-6 节中的旧候选统计：

- G0 Python owner census：`1,619` 条 schema v4 完整记录，schema error `0`，logical owner `0`；
- terminal matrix：五个真实 kill/disconnect point，除 durable terminal receipt 直接恢复外均跨至少两个真实 TypeScript process epochs，完整 protocol frame trace 可审计；
- mutation：`15/15` killed，恢复哈希一致，残留 `0`；
- cleanroom：candidate-local `.venv`、candidate-local npm cache/Bun、fresh installs，`1,257` TypeScript + `34` Python 通过，dirty path `0`；
- line buckets：audit tooling `3,268` 行独立列桶，不计入 production；production `2,432`，test `2,913`，adapter-only `790`。

详细修复映射见 `docs/reviews/M1-R01-v4-execution-04-repair-report.md`。最终候选门禁为 `10/10` PASS，但状态仍是 `implementation_complete_review_pending`，不得用自审替代新的独立审查。

## 9. 2026-07-19 第二次独立审查后修复补充

候选 `2340b792d184bc7625ba15fe7e9d958c3daa4c66` / evidence `2c74e21ec35ab29fe67a11b24a5f4d90de64cbfc` 的重新独立审查结论为 FAIL。历史报告与 reviewer-owned evidence 保持不变。新实现候选为 `3d4a00d62e47264cc4ac8678de41af497be7aec8`，tree 为 `af2f174ae9607fbcaebd33a691f1e1c5451fcadb`。

新候选把 baseline identity 与 G0 tooling identity 分离，冻结 18 个可执行 source ranges，并把每项 provenance 绑定到不同的 qualified candidate method/function body。tool partition、skill discover/validate/register、forked child input 构造和 plugin hook replacement 均已进入真实 production 调用链，不再以 callback passthrough、whole-class anchor 或 symbol-anywhere 取得 recovery credit。

复验得到 E04 专项 `37/37`、mutation `15/15`、candidate gate `10/10` 和 fresh cleanroom PASS；cleanroom 未复用原 node_modules/venv，forbidden dependency 与 dirty path 均为 0。最新 line bucket 把 production、test、adapter-only、audit-tooling、docs 和 data 分开，production 新增 `2,570`、test 新增 `2,961`、adapter-only 新增 `790`、audit-tooling 新增 `3,644`。

由于本轮审查者执行了修复，本节只是 implementation self-review/repair handoff，不能构成独立 PASS。状态必须保持 `implementation_complete_review_pending`，由不同 reviewer 对新 implementation/evidence pair 重跑任务书。
