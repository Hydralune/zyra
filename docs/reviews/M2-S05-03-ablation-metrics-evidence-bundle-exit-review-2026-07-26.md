# M2-S05-03 baseline/ablation、metrics、evidence bundle 与 M2 退出审查

## 1. 结论

- Slice：`M2-S05-03`
- 结论：`PASS_WITH_FROZEN_BASELINE_RESIDUALS`
- baseline：`b3387c626fc587936c24777ce108eac6f962283e`
- 父级 M2-05 baseline：`92537deca86376e147feb6c85248b0d3ff2298a6`
- M2 baseline：`f53bf78287a2b2a6eec74102e7218d664307c137`
- 实施前决策：`5d50f78`
- 最终 implementation target：`a9f7ff816822533c395ee2c28ca1fcd03721d35c`
- ledger 对齐提交：`5debc29`
- 退出状态：`ready_for_m3_reverification`

本结论同时收口 M2-S05-03、父级 M2-05 数字阶段和 M2 里程碑退出。它不重写已保护的历史 slice，不把旧测试或旧静态门禁债务冒充本切片通过项，也不声称本次实验重新调用了外部模型或已认证 provider CLI。

## 2. 实际交付

### 2.1 后端正式 owner

`packages/evaluation/zyra_evaluation/experiment_runtime/**` 已形成 Zyra-owned 的独立实验与证据域：

- `ExperimentMatrixRuntime`：固定并校验 7 个 variant、重复 seed、controlled comparison envelope。
- `ControlledWorkloadExecutor`：逐个消费真实 live causal archive 中的 canonical event，并生成 route、message、memory、compact、topology、fault/recovery 等可验证语义效果。
- `MetricAggregationRuntime` / `DistributionAggregator`：输出 raw samples、P50、P95、variance、standard deviation、MAD、IQR、CV 和 bootstrap confidence interval。
- `ExperimentStore`：用 SQLite 持久化 experiment lifecycle、cell、observation、sample、summary、comparison、receipt 和 bundle。
- `RequirementEvidenceMapper`：把 18 个 requirement/score ID 映射到具体 bundle member 和 source ID。
- `EvidenceBundleBuilder` / `EvidenceBundleVerifier`：生成确定性 ZIP、成员哈希、root digest、manifest digest，并对 missing、duplicate、tamper、scope 和计数失配 fail closed。
- `M2ExitPortfolioBuilder` / `M2ExitPortfolioVerifier`：把两个实验 bundle、冻结的 M1 dispatch/model evidence 和 M2 dual-domain live evidence组合成一个可独立导航的退出包。
- `SourceRoleExitAuditor`：执行来源角色、OpenClaw 前向排除、父仓库运行依赖和 LangGraph 窄域边界审计。

现有 task/event/worker/scheduler/memory/permission/fault/recovery/artifact owner 没有被迁移或复制。实验域保存 observation 和验证 receipt；原始 canonical task state 仍由原 owner 持有。

### 2.2 API 与 Web 主路径

- API 增加 registry、create、start、cancel、status、report、samples、source、requirements、bundle、archive 和 verify 路由。
- typed protocol/client 增加全部 experiment endpoint、normalizer 和 fail-closed admission。
- `Experiment Evidence Workbench` 展示 matrix、P50/P95、离散度、置信区间、variant comparison、requirement/score、bundle integrity 和 reviewer navigation。
- 浏览器关闭/面板 detach 只释放前端 projection，不会取消后台 experiment。
- production Web build 可从 `/settings` 到达 Experiment Evidence Workbench；本地 API 与 production Web 的真实 HTTP 可达性检查均返回 `200`。

## 3. 正式双领域实验

正式运行命令只使用嵌入式 canonical API owner 和冻结的 M2-S05-02 causal archive；没有启动 provider/model CLI、没有发出外部模型请求、没有启动监听器或后台服务。

| 领域 | experiment | cells | raw samples | canonical events | faults | human |
|---|---:|---:|---:|---:|---:|---:|
| software delivery | `experiment_b9dc2c58ea164b49b2581e1461ab7609` | 21 | 672 | 2,158 | 5 | 0 |
| cross-source research | `experiment_44f2dc2d08a345ddbfa6a68b6f778637` | 21 | 672 | 7,163 | 5 | 0 |
| 合计 | 2 domains | 42 | 1,344 | 9,321 | 10 | 0 |

每个领域都包含：

1. `single_agent`
2. `static_full_connect_multi_agent`
3. `dynamic_heterogeneous_swarm`
4. `no_scheduler`
5. `no_memory_compact`
6. `no_recovery`
7. `no_low_entropy_communication`

每个 variant 使用 3 个固定 seed；每个领域的 32 个 metric 均保存逐 cell raw sample。两个报告各含 224 个 P50 和 224 个 P95 统计项。

对照语义测试证明：

- dynamic swarm 的 fault recovery rate 高于 `no_recovery`；
- dynamic swarm 的 memory write count 高于 `no_memory_compact`；
- `no_low_entropy_communication` 的 communication delivery count 高于 dynamic swarm；
- 关闭 ablation verifier 或 metric aggregator 会显式失败，不存在静默 fallback。

## 4. Competition evidence bundle

- 文件：`docs/reviews/evidence/M2-S05-03/M2-exit-competition-evidence-bundle.zip`
- portfolio id：`m2-exit_6793777422e64397aabaa010aa0fedb4`
- SHA-256：`213a06b2e2af113e1fefbb1ffc1edc1e14c99e6fc19f6f87bc008f8fcb94b30b`
- manifest digest：`31c7d66e6f4683bb91ce056f1020a40bca3d3e3329ac7afacaff32167a69aa0d`
- root digest：`b81020c7bf472ecb82a177c1d04dd7c39a3f1acd604732e6d15b8219f4c2f4b1`
- requirement/score rows：18
- score mapping：`100/100`
- verifier：`valid=true`
- reviewer backend log required：`false`

两个 nested experiment bundle 都包含：

- final report、algorithm/pseudocode 与 complexity entry；
- reviewer navigation；
- raw samples、distribution summaries、variant comparisons；
- canonical event/span/mutation、fault、checkpoint、recovery、route、message、memory、artifact/checksum；
- controlled config、source archive metadata、source-role disposition；
- requirement/score index；
- screenshot index；
- manifest、成员哈希和 tamper-evident root。

screenshot index 是可验证成员且明确记录 `capture_required_for_runtime_correctness=false`。本轮曾启动本地 production Web/API 尝试像素截图，但会话没有可用浏览器实例；因此 screenshot 数量如实为 0，没有伪造图片或把 HTTP/单元测试冒充截图。M3 可在有浏览器运行时的提交流程中补拍，不影响当前 bundle 的运行时正确性和 reviewer navigation。

## 5. 来源角色与严格内化

### 5.1 裁决

- `zyra`：新实验/统计/evidence 状态域唯一 `primary_implementation`。
- `opencode`、`claude-code-best`、`OpenHands`、`browser-use`、`oh-my-pi`、`Hermes`：`reference_only`，不产生本 slice 生产迁移配额。
- Agent Framework、AgentScope、LangGraph：`conformance_only`；LangGraph 仅保留 checkpoint/exact-resume 窄域对照，不取得 StateGraph/Pregel/channel/ToolNode/server owner。
- OpenClaw：`excluded_forward_only`，无源码读取、运行依赖、迁移、对照或 ledger entry。

### 5.2 为什么不是伪内化

- 生产 owner 位于正式 `packages/evaluation`、`apps/api` 和 `apps/web` 模块，不存在 vendor/source-pool 或父仓库入口。
- Python runtime、SQLite store、API、typed client 和 Web projection 使用 Zyra schema、identity、receipt、event、artifact、error 和 verification 语义。
- 删除/关闭 matrix verifier、metric aggregator、bundle verifier 或 workbench admission，会导致行为测试明确失败。
- 主路径通过真实 HTTP API、durable lifecycle、报告、下载和校验触发；不是 manifest/import smoke。
- exact-commit cleanroom 的 import origin 均指向 cleanroom 内源码；绝对父仓库路径匹配为 0。

## 6. 有效行数与三组 numstat

### 6.1 严格分桶

| 范围 | effective | minimum | 结果 |
|---|---:|---:|---|
| M2-S05-03 | 11,453 | 6,500 | PASS |
| M2-05 | 30,411 | 20,000 | PASS |
| M2 | 180,166 | 135,000 | PASS |

M2-S05-03 分桶：

- raw additions：16,926
- production runtime：11,370
- UI behavior：83
- UI presentation：334
- type declaration：873
- schema/DTO/data：795
- adapter-only：1,400
- generated：311
- tests/mock/fixture：931
- docs/comments/blank：829
- vendor-like/source-pool：0

测试、报告文本、截图、ledger/data、生成式审计脚本、typed wire/API composition 和 JSX/CSS presentation 均未计入 effective production。

### 6.2 要求的三组 raw numstat

以 `b3387c6..HEAD` 执行：

- `apps packages skills scripts`：41 files，16,236 additions，9 deletions；
- `tests`：3 files，495 additions，0 deletions；
- `vendor vendor-runtimes`：0 files，0 additions，0 deletions。

## 7. 验证结果

### 7.1 当前 slice 与父级

- exact formal portfolio tamper test：1 passed；重复 ZIP member warning 来自故意追加同名成员的攻击样本。
- slice Python 行为：cleanroom 5 passed / 1 formal-evidence skip；工作树正式 portfolio 1 passed，合计覆盖 6 项。
- M2-05 Python 组合：29 passed；1 个受保护 legacy `m2_scenarios` import failure。
- M2 process-isolated API/投影/终端/浏览器/工件/记忆/场景适用集：66 项最终通过；retrieval 首次 15 秒 HTTP timeout，单独重跑 1 passed in 8.96s。
- TypeScript runtime 全套：1,268 passed，0 failed，1,489 assertions。
- Web/typed-client 全套：271 passed，2 个 MCP elicitation 日期失效测试失败；本 slice experiment workbench 4/4 passed。
- TypeScript typecheck：PASS。
- production Web build：PASS，370 modules。
- ledger/source-language 单元测试：10 passed。
- source-language custody：PASS；Python 11,837 raw production-path additions，TypeScript 1,487，0 violation。
- M2-S05-03 ledger sync：1 entry，0 missing target。

### 7.2 Exact-commit cleanroom

- target：`a9f7ff816822533c395ee2c28ca1fcd03721d35c`
- git archive SHA-256：`FF51C70C1FB831332D68D1421B09D9449352AD13C56A6410E96B475438651661`
- Python import origin：cleanroom 内 `packages/evaluation` 与 `apps/api`
- Python：5 passed / 1 formal-evidence skip
- Experiment Web：4 passed
- typecheck：PASS
- production build：PASS，370 modules
- source-role audit：11 rows，0 findings，OpenClaw `excluded_forward_only`
- absolute parent-source path scan：0

共享的 `node_modules` 仅作为当前冻结 lockfile 的依赖缓存；被构建、类型检查和测试的源码全部来自 exact git archive。

## 8. 基线残留与归因

以下项目没有计为通过，也不由本 slice 修改：

1. Web 全套中 2 个 MCP elicitation 测试使用固定过期时间，当前日期下 fail；`apps/web/src/features/mcp` 和对应测试在 baseline 到 target 间无 diff。
2. `test_m2_demo_scenarios` 的 legacy `m2_scenarios` 仍导入已移除的 `parse_slash_command`；相关文件在 baseline 到 target 间无 diff，生产 experiment/scenario runner 不导入该 legacy 路径。
3. `verify_submission_boundary.py` 仍报告 11 个 M1 前置源码中的审计字面量；本 slice 新增的 5 个 source-role 字面量已在 `a9f7ff8` 修复，余下文件在 baseline 到 target 间无 diff。
4. full internalization-ledger verifier 仍报告 `m1_hardening/long_horizon_runtime.py` 的 3 个同源前置 blocker 和 734 个历史 warning；M2-S05-03 ledger sync、source-to-target tests 和 custody verifier单独通过。
5. execution-state 已登记的 6 个 pre-M2 `zyra_runtime` public-export collection blocker 和 broad pytest 30 分钟历史超时没有在本切片冒充修复或通过。

这些残留均在本 slice baseline 前存在，当前 diff 没有扩大对应边界。它们继续作为 M3 re-verification/packaging 前的显式仓库债务，而不是 M2-S05-03 的隐性通过项。

## 9. 赛题证据与非声明

Portfolio 通过冻结证据映射验证以下门禁：双领域 live task、每领域 2,000+ canonical transitions、动态稀疏/低熵对照、fault/requirement-change/node-loss recovery、因果轨迹、算法伪代码/复杂性、M1 real local/edge/cloud dispatch 和 multi-provider/model compatibility。

本次 M2 实验本身明确不声明：

- 新调用了已认证 provider/model CLI；
- 新发出了外部模型请求；
- 在本轮重新执行了 M1 dispatch/model 场景；
- M3 最终提交重验证已经完成。

M2 于 2026-07-26 退出，距 2026-09-15 官方截止日保留 51 天。M3 只负责 re-verification、打包、部署彩排和提交缓冲，不应再首次实现 M1/M2 的核心 runtime 或 workbench。

