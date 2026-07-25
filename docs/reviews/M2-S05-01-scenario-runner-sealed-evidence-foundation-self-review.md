# M2-S05-01 Scenario Runner / Sealed Evidence Foundation 自审

## 1. 冻结边界与结论

- slice baseline：`92537deca86376e147feb6c85248b0d3ff2298a6`
- preimplementation decision：`ba96726e7b6570f1df948d43972c17505f817750`
- implementation commits：`aa67451d56a8b3a6d94d293d646fe08c65d062bf`、
  `e1de9bfef16fc6edc022f47781f5bba21b31d11a`、
  `198087a83102e0457e201114d14549373bea9924`
- final implementation target：`198087a83102e0457e201114d14549373bea9924`
- 结论：M2-S05-01 通过；M2-05 父级和 M2 不在本 slice 完成。

实现冻结后，精确有效行审计先得到 6,448 行。审查没有放宽分桶，而是发现证据验证只验证
manifest 总摘要，没有验证攻击者重算总摘要后的 preflight/policy/owner/metric 跨收据绑定。
`198087a...` 增加跨收据 owner binding 与伪造摘要失败测试。最终审计进一步把两个
`__init__` 导出清单全部降入 schema/data 桶，仍为 6,551 / 6,500。

## 2. 模块边界与状态责任

| 状态域 | canonical owner | 本 slice 落位 |
| --- | --- | --- |
| scenario definition/profile/policy | `ScenarioRegistry` | `packages/evaluation/zyra_evaluation/scenario_runner/registry.py` |
| run lifecycle/revision/journal/receipt | `ScenarioRunStore` | `scenario_runner/store.py` |
| admission/execution/cancel/archive/restart | `ScenarioRunnerService` | `scenario_runner/runtime.py` |
| sealed allow/deny/replan | `SealedPolicyRuntime` | `scenario_runner/sealed_policy.py` |
| effective canonical transition | `EffectiveStepClassifier` | `scenario_runner/effective_steps.py` |
| causal DAG/path receipt | `CausalEvidenceValidator` | `scenario_runner/causal.py` |
| metrics/raw samples | `ScenarioMetricCollector` | `scenario_runner/metrics.py` |
| artifact/event/manifest verification | `EvidenceCollector` | `scenario_runner/evidence.py` |
| HTTP route composition | existing API owners + `ScenarioRunnerApi` | `apps/api/zyra_api/scenario_api.py` |
| CLI | same HTTP API, no local fallback | `scripts/run_first_stage_scenarios.py` |
| frontend projection/control | disposable `ScenarioProjectionStore` / `ScenarioWorkbenchRuntime` | `apps/web/src/features/scenarios/**` |

Task、canonical event、worker、scheduler、memory、permission、fault、recovery 和 artifact custody
仍由既有 Zyra owner 持有。Scenario runner 只持有新的 evaluation run/evidence 状态，不复制这些
状态域。关闭或卸载浏览器 panel 只 detach 轮询和视图；不会调用 cancel，也不会停止后台 run。

## 3. 来源裁决

- production primary 只有 Zyra。该 slice 使用 Python 与 TypeScript/TSX 建立新的 Zyra-owned
  scenario/evidence owner，并组合既有 M1/M2 owners。
- OpenCode 只做 split protocol/app/TUI/web/desktop、session/event、permission/question、
  terminal/review/diff 分类审计。
- `claude-code-best` 的 backend command/session/permission 与 CLI/TUI/REPL/prompt queue/local
  JSX/dialog/Ink 分开审计，不产生本 slice 生产迁移配额。
- Oh My Pi 分成 AgentLoop/TaskTool/PAL/Mnemopi、provider/RPC/ACP/Hashline/RoboOmp、
  TUI/native/Snapcompact 三组审计，不取得 owner。
- Agent Framework、AgentScope、LangGraph 只承担 conformance；browser-use 只承担
  close/restore/watchdog reference。
- OpenClaw 保持 `excluded_forward_only`；运行时 audit 中有确定性排除断言，但 ledger 不新增
  OpenClaw 条目、无源码路径、无运行依赖。

ledger 同步得到 11 条当前 slice 记录、missing target 0；只有 Zyra 一条为 production primary。
source-role runtime audit 有 13 行、2 active、11 inactive、0 finding。

## 4. 真实主路径与语义效果

真实 Web/CLI 调用进入同一 `/scenarios/**` HTTP API。create 对 registry 版本、definition digest、
profile、seed、faults、preflight targets、sealed policy digest 和全新输入作 admission；dirty
database/cache/index/artifact/build 或 replayed input 确定性拒绝。start 通过 API composition
依次触发真实 task、worker routing、memory curator、permission、fault/recovery 与 artifact owners，
而不是调用旧 `m2_scenarios.py` 或固定 fixture。

正式 sealed policy 把 ask、unknown 和 high-risk 转为 deny + replan，不存在 human wait。
operator steering/cancel attempt 被持久化为 rejected attempt，并使正式 run 失败，但
`human_intervention_count` 保持 0，不能伪装成自治成功。

effective-step classifier 排除 heartbeat、log、token、repaint、poll、replay、no-op 和 duplicate；
admitted step 必须绑定 exact run/task、semantic effect 和 causal parent。证据层验证 DAG 无环、
parent 顺序、root-to-leaf reachability、fault/recovery/artifact/verification effect path，
并持久化 raw metric samples、dimension receipt、canonical event chain、artifact bytes checksum。

最终验证同时绑定：

- preflight `scenario_run_id` / `input_digest` / clean / new-input；
- sealed policy digest / valid / zero human / zero operator attempt；
- canonical events 与 effective steps 的 owner run/task；
- raw metric sample digest、sample count 以及 scenario/run/task dimensions。

即使攻击者修改 policy receipt 后重新计算 manifest digest，验证仍以
`policy_digest_binding_mismatch` 失败。

## 5. 动态可达性、失败路径与断开即失败

- Python focused 集成从真实 `ThreadingHTTPServer` 启动 API，完成 registry、create、start、poll、
  evidence、verify、CLI lifecycle 和 server restart 后的 durable read。
- Web 测试使用真实 `ZyraApiClient` protocol catalog/normalizer/idempotency/correlation path，
  证明 panel runtime 调用 scenario owner；没有 direct fetch 或 demo fallback。
- store restart 会把遗留 running/cancelling run 恢复到可重试 queued，而不伪造成功。
- invalid definition/version、dirty scratch、policy mismatch、manual intervention、missing causation、
  stale/conflicting projection、artifact tamper、manifest tamper、rebased receipt tamper 均 fail closed。
- classifier、evidence collector 或 workbench 被 disable 时显式失败；不存在 legacy harness、
  local fallback、另一个 SQLite store 或 replay trace 接管。
- 生产文件中父目录 source repo、npm link、editable path、vendor/source-pool 和
  `m2_scenarios` runtime import 命中均为 0。

## 6. 有效行数分桶与大文件复核

机器可读逐文件结果：
`docs/reviews/evidence/M2-S05-01/effective-lines.json`。

| bucket | lines |
| --- | ---: |
| raw additions | 10,750 |
| production runtime | 6,452 |
| UI behavior | 99 |
| UI/static presentation | 231 |
| type declaration | 556 |
| schema / DTO / static data | 882 |
| adapter-only | 610 |
| test / mock / fixture | 1,262 |
| docs / comments / blank | 658 |
| vendor-like / source-pool | 0 |
| **effective production** | **6,551** |
| minimum | 6,500 |

大文件触发逐项复核：

- `effective_steps.py` 710 raw / 651 effective：事件排除、semantic effect、dedupe、identity、
  causation、coverage 全部是被 runtime 调用的算法；静态 schema 行已排除。
- `evidence.py` 573 / 532：artifact bytes、event chain、causal、metric 和跨收据绑定验证由成功及
  tamper 测试触发。
- `registry.py` 718 / 534：default definition/profile/policy 的 115 schema/data 行被排除；其余是
  version conflict、digest、resolution、configuration validation。
- `runtime.py` 695 / 644：durable lifecycle、background execution、cancel/archive、restart 和真实
  owner composition；没有第二 task/event owner。
- `store.py` 662 / 612：SQLite CAS revision、transition graph、journal/receipt、restart reconciliation。
- Web `runtime.ts` 533 / 478：真实 API lifecycle、bounded poll、stale/conflict handling、detach/close；
  不持有 backend truth。
- `models.py` 487 raw 全部归入 schema/DTO，不计有效行；Scenario API/main/app composition 全归
  adapter-only；typed endpoint catalog 全归 schema；TSX 228 行 presentation 和全部 tests 均为零 credit。

审计触发列表还包含小文件中 adapter/type/schema/test 比例超过 30% 的条目；这些条目均已零 credit，
不是以“大文件存在”为由计数。

## 7. 验证结果

- final focused Python：11/11。
- final scenario Web/typed API：9/9，49 assertions。
- typed client + permission sealed + workbench adjacent：76/76，314 assertions。
- Python 较宽增量：44 pass / 1 legacy fail；失败是受保护旧
  `test_m2_demo_scenarios.py` 导入已移除的 Python `parse_slash_command`，本 slice 既不导入也不执行
  该 harness。
- Web typecheck、production build（362 modules）、Python compile/import、`git diff --check`：通过。
- ledger sync/check：11 entries、missing target 0；ledger unit tests 25 pass / 2 historical failures。
  两个失败均查询既有 M1-02B vendored/audit 行；同步前后 44 条历史目标事实一致，本 slice 不追溯改写。

本 slice 没有新增依赖、子进程、端口、MCP server、插件、Docker 或动态 import，也没有转移既有
canonical owner/transaction/lease/idempotency/restore 语义。typed protocol 的五个既有 mutation
receipt 元数据从 `none` 修正为 catalog invariant 要求的 `required`，并以 typed-client、
permission、workbench 全套 76 项及 typecheck/build 作匹配升级验证。普通 slice 不额外执行完整
cleanroom；数字阶段聚合与 M2 exit 仍负责完整 cleanroom 和全仓门禁。

## 8. 赛题触达与明确非声明

本 slice 为 `REQ-CLOSE-01`、`REQ-TRACE-01`、`SCORE-COMPAT`、`SCORE-UX` 建立可复用 evidence
foundation。它没有关闭以下后续门禁：

- 两个及以上高完成度跨领域 live 场景；
- 单 run 至少 2,000 个有效 canonical state transitions；
- 动态稀疏拓扑与低熵对照；
- 真实 local / isolated edge / cloud dispatch；
- 多模型正式兼容；
- M2-05 父级、M2 数字阶段或 M2 exit。

根目录 `docs/milestones/execution-state.yaml` 不属于 Zyra Git；只在最终 Zyra evidence commit
存在后单独更新。
