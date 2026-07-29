# P2-S02-02 Mechanism Registry、只读诊断与 Rollback 增量自审

- Slice：`P2-S02-02`
- Slice 基线 commit：`d478366fda71bf04a2f3625b350c9a1325406dd7`
- 实现 commit：`1fd314a02520f96a033bfea2a7602fb4225b7628`
- 实现 tree：`c6a2cb4a9b6579bbcd9374269c056b11cbd818d5`
- 自审日期：`2026-07-29`
- 结论：`PASS`

## 1. 交付结论

本 slice 已建立 Zyra-owned mechanism registry、run pin、lifecycle transition、
readiness enforcement、只读 diagnostic、strongest activation gate、确定性 rollback、
degraded fallback、outcome attribution 和 no-policy-training validator。

生产配置当前仍把正常 run 固定到
`topology_policy/phase1_deterministic_baseline`。`phase2_strongest_v1`
保持 `validation + blocked_pending_readiness`，其 LoopX、ARG、CARD、AgentPrune 和
MaAS readiness 均为 `unavailable/input_precheck`，因此既不能进入显式 validation，
也不能成为 default。`phase2_diagnostic_v1` 同样因正式 readiness 为
`unavailable` 而 fail closed；测试中的 `evidence_only` diagnostic record 只用于验证
runtime contract，不代表任何正式机制已经可用。

本 slice 没有切换 topology 或 scheduler 默认实现，没有引入策略训练，也没有修改
第一阶段或早期 P2 的冻结事实。

## 2. Registry 与 lifecycle

`MechanismRegistry` 实现：

1. `register`
2. `resolve`
3. `pin`
4. `enter_validation`
5. `activate_default`
6. `rollback`
7. `retire`
8. `check_compatibility` / `resolve_pinned`

每个 registration 绑定 family、version、profile、schema/source/config digest、
implementation/evidence commit、readiness report digest、rollback target、timeout、
hard gate、audit gate 和 no-policy-training declaration。重复 family/version 无论
digest 相同或不同都拒绝。

生命周期与 activation state 组合由显式矩阵约束：

| Lifecycle | 允许状态 | 正常 run 可选 | 说明 |
|---|---|---|---|
| `baseline` | `active` / `standby` | 仅 active baseline | 冻结 fallback 与 rollback target |
| `validation` | `blocked_pending_readiness` / `validation_ready` / `rollback_ready` | 否 | 仅显式隔离 scenario/preflight |
| `default` | `active` | 是 | 唯一允许 `phase2_strongest_v1` |
| `diagnostic` | `read_only` | 否 | proposal/receipt only |
| `retired` | `disabled` | 否 | 历史 pin/receipt 仍可解析 |

构造 registry 时最多允许一个 default，且其 profile 必须是
`phase2_strongest_v1`。正常 resolver 只读 active version；显式传入 validation
version 不能绕过该选择。

## 3. Readiness enforcement

readiness 与 lifecycle 的硬约束为：

- `input_precheck + deterministic_ready` 仍是 development-only，不能进入
  diagnostic、validation 或 default。
- `implementation_validated + deterministic_ready` 才可通过显式隔离 manifest
  进入 validation。
- `activation_ready + deterministic_ready` 才可进入 default。
- `evidence_only` 只能进入 diagnostic。
- `unavailable` 只能执行 baseline。
- `pending:*` readiness ref 只允许 `unavailable`，正式 report ref 必须校验文件、
  report digest、mechanism ID、stage 和 status。

断开 readiness enforcement 的 mutation test 会使非法 default construction 或
activation 测试失败。生产配置的 strongest profile 继续被现有 frozen readiness
阻断。

## 4. Run pin、兼容与 rollback

run 启动时固定：

- family/version/profile；
- pin-time lifecycle；
- schema version/digest；
- source/config/readiness/registration/registry digest；
- registry revision。

registration digest 排除 registry-owned 的可变 lifecycle/activation state，因此
activation、rollback 和 retire 不会破坏历史 pin；schema/source/config/readiness
任一语义漂移仍会使 compatibility fail closed。

行为测试证明：

- activation 前已启动的 baseline run 继续 baseline；
- activation 后只有新 run 选择 strongest default；
- rollback 后只有新 run 恢复 baseline 或显式选择已激活过的 rollback-ready
  version；
- 未激活的任意 version 不能成为 rollback target；
- rollback/retire 后，旧 run 仍按 pin-time version 执行，旧 receipt 可 replay；
- retired version 不可服务新 run。

baseline 在 strongest activation 后进入 `standby`，回滚后恢复 `active`；不会同时
出现两个 normal resolver target。

## 5. Diagnostic 与 baseline outcome

`TopologyPolicyRuntime` 不拥有 session/config/event store。它从
`config/phase2/policies.yaml` 加载 registry，并使用调用方提供的既有：

- baseline executor；
- side-effect-free baseline reference；
- canonical owner probe；
- event admission。

diagnostic 流程先让冻结 baseline 对同一 immutable input 执行一次真实 outcome，
随后向 diagnostic executor 只提供 `execution_allowed=false` 的 invocation 和
baseline reference。diagnostic proposal 的 receipt 固定声明：

- `executed=false`
- `actual_outcome_recorded=false`
- `outcome_statement=not_executed_no_actual_outcome`

owner probe 同时覆盖 graph revision、route、lease 和 side effect。proposal 返回、
抛异常或超时后都必须再次 probe；任何 diff 会写
`phase2.mechanism_degraded`，随后抛出 `DiagnosticMutationError`，不能用 exception
路径掩盖 mutation。

真实 owner 测试使用 `GraphStateCustody`/`GraphStateStore`、
`ResourceScheduler`、`TaskState` 和 `LocalArtifactStore`，证明 diagnostic 与
baseline-only 的 graph revision、route、lease、artifact 和 side-effect 结果完全
一致，且 baseline side effect 只执行一次。

## 6. Activation、degraded fallback 与 attribution

`StrongestProfileActivationGate` 同时检查：

- strongest profile identity 与 `validation_ready` source；
- schema/config/source/readiness digest；
- implementation/evidence commit；
- frozen 23 项 Phase 2 hard metric gate 加 success/safety umbrella gate；
- state owner、runtime path、dependency、rollback audit；
- rollback target 存在、同 family、未 retired，且非 baseline target 必须曾激活；
- no-policy-training evidence 与 registry declaration。

缺任一 gate 都不能产生 eligible activation decision。

registry config 缺失/损坏、outer/config digest 不匹配、schema 不兼容、readiness
report 缺失、executor 缺失、mechanism exception 或 timeout 都会：

1. 写显式 `phase2.mechanism_degraded` EventRecord；
2. 标出 failure class、source mechanism/profile、input/config/readiness digest、
   fallback profile 和 owner diff；
3. 执行冻结 baseline；
4. receipt 标记 `degraded=true`、`fallback_executed=true`、
   `strongest_success_eligible=false`。

evaluation attribution 会拒绝把 diagnostic proposal 当作真实 outcome，也会拒绝把
degraded baseline 计入 strongest success。activation/rollback event 通过既有
`zyra_core.append_event` 写入真实 JSONL event log 并可读取。

## 7. No-policy-training

registry registration 和 release input validator 会拒绝：

- train/trainer/training entry 或目录；
- dataset；
- checkpoint；
- mutable learned parameter；
- policy/textual gradient 路径；
- torch、TensorFlow、JAX、TRL、datasets、stable-baselines3 等训练依赖。

当前五个 production/config path 的 release audit 通过，依赖增量为零。未新增训练
dataset、checkpoint、策略梯度、RL、微调或 learned parameter。

## 8. 状态 owner 审计

- registry 只拥有 mechanism metadata/lifecycle。
- run pin 作为 immutable value 交给既有 run/session owner；registry 不持久化
  session。
- registry config 使用既有仓库配置文件；没有第二 config store。
- lifecycle/degraded/diagnostic event 使用既有 `EventRecord` 和 caller event
  admission；没有第二 event store。
- graph commit 仍只属于 `GraphStateCustody`。
- route/placement/lease 仍只属于 `ResourceScheduler` 和既有 lease owner。
- diagnostic runtime 没有 graph/scheduler/artifact write port。
- baseline executor 是既有 deterministic 路径；本 slice 未复制 policy owner。
- OpenClaw 保持排除，无 `../long-horizon-systems` 运行依赖。

## 9. 增量有效代码审计

从 slice 基线到实现 commit 共新增 `4610` 行：

| 分类 | 新增 | 删除 | 说明 |
|---|---:|---:|---|
| production | 3020 | 0 | registry/runtime/activation、outcome attribution 与导出 |
| data | 237 | 0 | `config/phase2/policies.yaml`，不是训练数据 |
| test | 1353 | 0 | lifecycle、readiness、diagnostic、real owner、activation、rollback、degraded、attribution |
| runtime-assets | 0 | 0 | 无 vendor/runtime asset |
| generated | 0 | 0 | 无生成物计入实现 |
| docs | 0 | 0 | 本自审与证据在独立 docs commit |
| adapter-only/mock/fixture | 0 | 0 | production callback port 计 production；mock/fixture 不计实现 |

测试中的 helper record 用于 lifecycle matrix；关键 diagnostic 证据使用真实 graph、
scheduler、artifact 和 event owner。

## 10. 验证结果

- slice + policy contract/projector/replay + GraphStateCustody + 相邻 route regression：
  `50 passed in 6.90s`。
- slice 专属 unit/integration：`29 passed in 1.32s`。
- Python `compileall`：通过。
- `git diff --check`：通过。
- Phase 2 policy contract verifier：`valid=true`；
  `strongest_activation_eligible=false`，blocker 为 LoopX/ARG/CARD/AgentPrune/MaAS。
- internalization ledger：`audit_ok=true`，`errors=0`，`blockers=0`，
  `warnings=734`。
- no-policy-training release-path audit：通过，5 个 path，0 个新增依赖。
- Ruff 与 pyflakes 均未安装于项目 venv，没有伪报成功。

相邻 `tests/integration/test_mechanism_readiness_enforcement.py` 为
`1 failed, 2 passed, 6 errors`，根因是冻结 baseline manifest 中
`package-manifest` SHA 与当前 `package.json` 不一致。该失败在本 slice 修改前的
干净基线 commit 已复现，不由本实现引入；本 slice 没有改写冻结 manifest 或早期
P2 evidence 来隐藏该历史问题。

## 11. 最终裁决

本 slice 的 lifecycle、readiness、validation isolation、diagnostic no-effect、
run pin、activation/rollback、degraded fallback、outcome attribution、
no-policy-training 和 owner boundary 均具备 production code、真实行为测试与
mutation evidence。当前 default 未切换，strongest profile 未激活，裁决为
`PASS`。

根目录 `G:\agent-zoo\docs\phase2\slice-02-02-policy-registry-shadow.md` 及其他
Zyra 仓库外文档未修改，不会随 Zyra commit 自动提交。
