# M2-02A / M2-02B 独立聚合审查（2026-07-24）

## 结论

**修复后通过。**

本次审查覆盖 `M2-S02A-01`、`M2-S02A-02`、`M2-S02B-01`、`M2-S02B-02`，审查基线为
`f7fff49be91fb0f797260c03ff9cca76c06c5974`，包含全部修复的最终审查目标为
`ef467642364a4eb2108160e16d25ab156d59d37c`。审查发现来源角色门禁失效、语言字段过宽、ledger
同步顺序不稳定，以及三处真实控制语义缺口；这些问题均已在 review-fix commit
`ef467642364a4eb2108160e16d25ab156d59d37c` 中修复并补回归测试。最终目标在主工作区和基于该
精确提交创建的 archive cleanroom 中通过适用回归、生产构建、source-to-target 审计和同步幂等
检查。未发现仍未解决的当前范围阻断问题；正式跨领域 live 任务、2,000 有效 transition、真实
端边云与多模型证据仍由后续里程碑 owner 关闭，本审查不提前宣称这些赛题门禁完成。

## 审查中发现并已修复的问题

| 严重度 | 原问题、首次出现提交与影响 | 修复与测试 |
| --- | --- | --- |
| P1 | `M2-S02B-01` 在 `636ba5dc9887c9d563fb0ba2949f50b6d0fcbe42` 把 OpenHands、browser-use、oh-my-pi 同时登记为三个 supplementary；原聚合审计只警告，违反每域最多两个 supplementary 和父级 source-role 裁决。 | OMP 改为 `conformance_only`，其 855 行目标实现重新归属 Zyra 自有 production，不再领取 OMP 迁移额度；聚合审计改为独立 `ROLE_POLICY` fail-closed。新增第三 supplementary mutation 测试。 |
| P1 | 四个 ledger 同步器从 `6b15c5d8924a113553eac4d21922bc28239696dc` 起无法同时稳定 `--check`：任一同步器都会删除再追加自己的记录并改变跨 slice 顺序。 | 四个同步器均改为原位替换 owned rows；新增任意顺序重复同步测试，四个 `--check` 可同时通过。 |
| P1 | A01/A02 ledger 缺少精确语言，A02 历史 evidence 用 `typescript/python` 组合值把 TypeScript view primary 与 Python owner glue 合并；B01/B02 conformance 也存在斜杠组合语言。首次分别出现在 `7ea026f1de635197d84b3179d5494e96ca687577`、`6b15c5d8924a113553eac4d21922bc28239696dc`、`636ba5dc9887c9d563fb0ba2949f50b6d0fcbe42`、`fdf82f2059153b917ca2486cdcbe1b8d68b790b9`。 | 当前 ledger 为每个 role 写入单一 `source_language` / `target_language`；A02 primary 仅绑定 TypeScript，Python owner glue 不领取来源迁移额度。审计拒绝 `mixed`、`unknown` 和斜杠组合值，并校验 same-language 与目标扩展名。 |
| P1 | A02 的 checkpoint control 在 `6b15c5d8924a113553eac4d21922bc28239696dc` 可因历史 `resumeCount > 0` 错把当前 `/rewind` 或 `/resume` receipt 判为已观察。 | 删除无 command/event 关联的宽松条件；测试证明历史 resume 不会提交当前命令，只有当前 command id 或 observed event overlap 才能提交。 |
| P1 | B02 的 recovery panel 在 `fdf82f2059153b917ca2486cdcbe1b8d68b790b9` 把 recovery-chain attempt id 当成 worker-pool 物理 attempt id，真实 kill/reassign 可被后端 owner fence 错拒。 | `recoveryControlOwnerExpectation` 仅在 task worker-pool 快照与当前 worker/lease 投影完全一致时携带物理 `attempt_id`；不一致时省略可选 fence。测试同时构造逻辑恢复 attempt 与物理 attempt，证明发送后者。 |
| P1 | B02 后端 `/retry` 在 `fdf82f2059153b917ca2486cdcbe1b8d68b790b9` 只信任前端归一化，直接 API 可提交超界或显式 unbounded retry。 | 后端在 owner/action 前拒绝布尔、非整数、`<1`、`>8` 及 `bounded_retry=false`；真实 API 集成测试证明 `maximum_attempts=9` 返回 409 且不产生 `control_mutations`。 |

所有修复均位于 `ef467642364a4eb2108160e16d25ab156d59d37c`，没有 amend 或重写上述历史
implementation/evidence commit。

## 仍未解决的阻断问题

未发现仍未解决的阻断问题。

## 非阻断风险

- 通用 `scripts/verify_internalization_ledger.py` 仍对受保护且未改动的
  `packages/evaluation/zyra_evaluation/m1_hardening/long_horizon_runtime.py` 报三条
  `FORBIDDEN_RELATIVE_SOURCE_DEP`。命中内容是扫描器自身统计禁止字符串的静态字面量，不是 import、
  subprocess、editable path 或运行期依赖；它们早于本审查基线。该命令的非零结果被保留，未伪装为
  green；本范围由严格 M2-02 source audit、路径扫描和 cleanroom 证明。
- in-app browser runtime 的 `browsers.list()` 返回空列表，无法形成真实 viewport 截图。没有换用未授权
  浏览器控制器，也没有声称视觉截图通过；126 个交互测试、2,400-node / 5,000-row 压测、类型检查和
  生产构建覆盖了代码行为。后续有浏览器实例时仍应补人工视觉回归。
- cleanroom 首次 Python 命令因 archive 不包含被忽略的 `.tmp` 父目录，在 pytest setup 前出现 5 个
  `FileNotFoundError`、1 个测试通过。创建空 `.tmp` 后原命令完整重跑，6/6 通过；该基础设施首轮失败
  没有被删除或记成产品断言失败。

## 目标覆盖矩阵

| 目标 | 状态 | 默认主路径与证据 | 阻断 |
| --- | --- | --- | --- |
| A01 动态 topology / route / placement 投影 | 通过 | RuntimeEventSpine → ingress → CanonicalProjectionStore → topology projector；route/placement、pending/committed、requirement impact、disable 测试 | 无 |
| A02 大图交互与 canonical control | 修复后通过 | TaskDetail → TopologyWorkbench；2,400 nodes / 3,100+ edges、虚拟化、稳定增量布局、搜索、a11y、sealed denial、command correlation | 无 |
| B01 worker causal timeline | 修复后通过 | CanonicalProjectionStore → worker epochs / causal graph / recovery chain / browser step → TimelineWorkbench；lease replacement、late/partial、critical path、disable 测试 | 无 |
| B02 recovery control 与大历史交互 | 修复后通过 | RecoveryControlRuntime → typed Task API → RuntimeControlDispatcher → M1 canonical owners；5,000 rows、fold/search/drift、sealed zero-human、owner race、physical attempt fence | 无 |
| M2-02 跨 parent 聚合 | 通过 | 同一 canonical store 同时驱动 topology 与 timeline，control receipt 只在 canonical evidence 后提交；cleanroom 与 source audit 通过 | 无 |

## 内化审查

### 来源与语言合同

| Slice | 来源与 role | 语言 / migration | 抽样来源 symbol 或控制流 | 目标与 canonical owner | 结论 |
| --- | --- | --- | --- | --- | --- |
| A01 | Zyra primary；opencode/OpenHands/OMP reference；LangGraph conformance | primary TS→TS same-language；其余 target=`none` | 直接复核 canonical ingress/store 与 LangGraph pending/committed、interrupt identity 窄合同 | `features/topology/projection/**`；事实 owner 为 `CanonicalProjectionStore`，route/placement owner 仍在 Python scheduler | 生产计入 Zyra primary；refs 不计 |
| A02 | Zyra primary；opencode/OpenHands reference；LangGraph conformance | primary TS→TS；Python API/owner handler 是既有 owner glue，不绑定为 view-source migration | 复核 OpenCode session panel 和 OpenHands planner/task-list 交互模式；目标控制失败分支直接到 typed API | `features/topology/view/**`；graph/worker/checkpoint owner 不转移 | 生产计入 TS view；Python glue 排除 |
| B01 | opencode primary；OpenHands、browser-use supplementary；OMP/Agent Framework/LangGraph conformance | TS→TS cropped；browser-use Python→TS 为实施前已裁决的有界 semantic port | OpenCode `reuseTimelineRows`、`stabilizeContextKey`、`filterVirtualIndexes`；OpenHands `getIndicatorColor`、`getStatusCode`、`handleEventForUI`；browser-use `AgentHistory`、`AgentHistoryList.errors`、`action_history` | `features/timeline/projection/**`；frontend fact owner 为 canonical store，worker/lease/recovery owner 在 M1 Python stores | 修复后仅 2 supplementary；OMP 无生产额度 |
| B02 | opencode primary；OpenHands、browser-use supplementary；Agent Framework/LangGraph conformance；OMP reference | opencode/OpenHands TS→TS；browser-use Python→TS bounded semantic port | OpenCode `useSessionCommands` / `runCommand`；OpenHands stop/start/resume mutation 与 status reconciliation；browser-use pause/resume/reconnect/failure-reset | `features/timeline/control/**`, `scale/**`, view panel 与 API owner integration；control/admission/recovery/worker owners 保持 Python canonical | 深度内化成立，无黑箱 owner |

固定来源提交：opencode `adf178a6b95c61506ddaadaf4dd062badb4a8fda`、OpenHands
`c105a82387898e744423c8831d412e26495b38a9`、browser-use
`18484f23ac96bb955259a1c54530a7d265dfffdb`、oh-my-pi
`c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca`、Agent Framework
`d50698bb797710bfd1ebf34eb621c905a4009b2d`、LangGraph
`5931a5f0b313feff24e2516a586c55601b868ac1`。所有来源路径均在这些固定提交存在；OpenClaw
条目为 0，保持 `excluded_forward_only`。

最终 source-to-target 审计：21 个唯一 ledger entries，66 个 production target bindings，18 个
conformance/reference bindings，0 missing target，0 warning，0 error，0 forward OpenClaw entry。

### 关键状态归属

| 状态 | system of record / schema | 提交、并发与 lease 边界 | 恢复路径 | clean-state / 行为证据 | 留在上游 |
| --- | --- | --- | --- | --- | --- |
| canonical frontend event state | `CanonicalProjectionStore`，runtime-event / projection snapshot schema | revisioned transaction、cursor/generation、idempotent event id；IndexedDB commit 后才 ACK | checksum/version migration 后 restore，stale generation 拒绝 | canonical store snapshot/reconnect/disable tests；cleanroom Web suite | 否 |
| dynamic graph / checkpoint | `GraphStateCustody` 与既有 checkpoint stores | immutable pending/committed writes、graph revision、conflict/rebase；精确 checkpoint identity | SessionControlRuntime / RecoveryApplication exact resume | topology projection/control Python+TS tests | 否 |
| scheduler route / placement | Python ResourceScheduler/provider control owners | route/placement revision、候选/约束提交边界；frontend 只读投影 | task checkpoint/event replay 后重建 | route/placement integration + topology tests | 否 |
| worker / attempt / lease | `WorkerPoolStore` / `WorkerControlRuntime` | physical attempt、lease fence、worker/lease owner revision | worker-pool checkpoint / re-admission | recovery API integration；physical-attempt regression | 否 |
| recovery plan / command | `RecoveryApplication`、`RuntimeControlDispatcher`、task SQLite state | request/command idempotency、permission admission、owner/checkpoint fences | durable receipt 与 canonical recovery events | sealed, timeout/late, disconnect, race, retry-bound tests | 否 |
| frontend topology/timeline view state | TS derived projections；controller/runtime 仅拥有 selection/window/fold/search | canonical revision 输入；不写回事实 owner | 从 canonical store 重新投影；viewer detach 不取消后端 | large graph/history, cache bounds, disable tests | 否 |
| artifacts / causal refs | 既有 task/event/artifact stores | event/span/tool/artifact identity 与 commit sequence | event replay / projection restore | causal drilldown、hidden-gap、artifact joins | 否 |

删除或禁用 topology projector、timeline projector、typed transport 或 control runtime 时，相应测试明确
失败或 fail closed；没有只证明 vendor/sidecar 断开。

## 有效行数审查

历史 slice 计数严格使用各自冻结区间，不用本次 remediation 反向补数：

| Slice | raw + / - | production runtime | UI behavior | 主要排除桶 | 保守有效 | 独立最低线 | 结果 |
| --- | ---: | ---: | ---: | --- | ---: | ---: | --- |
| M2-S02A-01 | 9,146 / 90 | 6,658 | 0 | types 957；tests 1,292；docs/blank 238 | 6,658 | 6,500 | 通过 |
| M2-S02A-02 | 11,848 / 35 | 6,814 | 555 | presentation 1,444；types 1,141；adapter 153；Python owner glue 235；tests 1,160 | 7,369 | 6,500 | 通过 |
| M2-S02B-01 | 10,999 / 0 | 6,564 | 272 | presentation 1,040；types 913；schema 50；tests 1,760 | 6,836 | 6,000 | 通过 |
| M2-S02B-02 | 9,808 / 24 | 5,707 | 330 | presentation 494；types 823；schema 24；generated/Python owner 694；tests 1,230 | 6,037 | 6,000 | 通过 |

父级 A 合计 14,027 / 13,000，父级 B 合计 12,873 / 12,000；数字阶段精确 slice-accounted
合计 26,900 / 25,000，margin 1,900。B02 的 margin 只有 37，但逐文件 AST/分类器复核后仍满足独立
最低线，没有用 ledger、data、test、schema、DTO、adapter 或 Python owner glue 抵扣。

本次 `44c9f1a..ef467642` remediation 新增 592 行：production candidates 47、tests 218、
ledger data 63、audit/sync tooling 264；删除 177 行。production 中排除 1 个空行后保守有效为 46
（Python retry constraint 18，TypeScript owner/correlation behavior 28）。这 46 行只描述修复增量，
不改变上述四个历史 slice 数字。

## 测试与验证

### 主工作区最终目标

| 命令 | 结果 |
| --- | --- |
| `bun test ./packages/core/typed-api-client/test ./apps/web/test` | 126 passed，0 failed，803 assertions |
| `bun run typecheck:web` | 通过 |
| `bun run build:web` | 通过，172 modules |
| 四个适用 Python integration 文件 | 6 passed；恢复控制文件单独为 3 passed in 95.84s |
| `pytest tests/unit/test_m2_02_source_to_target_audit.py` | 4 passed |
| 四个 `sync_m2_*_source_ledger.py --check` | 全部 aligned |
| `audit_m2_02_source_to_target.py --target ef467642...` | 21 entries，66 production，18 conformance/reference，0 error/warning |
| 四个历史 effective-line auditor | 6,658 / 7,369 / 6,836 / 6,037，全部过独立最低线 |
| `git diff --check` | 通过 |

默认主路径、失败路径、sealed denial、stale owner、disable/fail-closed、viewer detach、late receipt、
large graph/history、pending/committed 与 exact resume 均有行为测试。测试不是固定 health response、
fixture-only 或源码存在性 smoke。

### 精确提交 cleanroom

- source：`git archive ef467642364a4eb2108160e16d25ab156d59d37c`
- frozen install：`bun@1.2.15 install --frozen-lockfile`，25 locked packages；没有 lockfile/package 变更。
- Web/typed：126 passed，0 failed，803 assertions。
- Python：显式 cleanroom `PYTHONPATH` 指向 archive 内 `apps/api` 与 `packages/*`；导入路径预检均解析到
  cleanroom；6 passed in 79.14s。
- source audit unit：4 passed；四个 ledger check 和聚合 source audit 通过。
- typecheck/build：通过，172 modules。
- 边界：archive 顶层不存在 opencode/OpenHands/browser-use/claude-code-best/oh-my-pi/langgraph/
  OpenClaw 来源仓库目录；无 npm link、editable parent project、外部 Docker context、新 dependency、
  subprocess、port、MCP server 或 dynamic import。

主工作区 `.venv` 自身是 Zyra editable install，因此 cleanroom Python 没有依赖其 editable project
finder：显式 `PYTHONPATH` 优先指向 archive，并预检 `zyra_api`、`zyra_commands`、`zyra_scheduler`、
`zyra_runtime` 的 `__file__` 全部位于 cleanroom。

## 赛题需求与评分回归矩阵

| ID / PDF 分值 | 本轮 owner 与证据 | 状态变化 | 阻断 |
| --- | --- | --- | --- |
| REQ-TOPO-01 | M2-02A；动态 add/remove/replace、route/placement、churn、requirement impact 可视投影与 2,400-node 测试 | 推进 contract/trace，不关闭正式对照门禁 | 无当前阻断；正式 static/full-connect 对照后续 |
| REQ-EDGE-01 | M2-02A；device/edge/cloud placement、provider/model split、privacy/cost/latency 投影 | 推进 UI 证据，不把模拟标签当真实 dispatch | 真实三类 dispatch 仍由 M1-08/M2-05/M3 owner |
| REQ-FAULT-01 | M2-02B；worker failure→recovery→replacement/checkpoint/control causal chain | 推进可视与控制证据 | 正式 live fault matrix 后续 |
| REQ-TRACE-01 | M2-02A/B；event/span/tool/artifact/checkpoint/worker/route 因果 drilldown | 推进统一前端真实状态消费 | 里程碑 live trace 尚未关闭 |
| REQ-CLOSE-01 | B02 sealed denial：一次 operator attempt、0 human、无 approval wait、自动恢复或 fail closed | 仅证明控制分支 contract | 2,000-step 与最终交付仍后续 |
| SCORE-ORG（15） | role/capability/top-k route/worker replacement/topology trace | 推进 | 未宣称得分锁定 |
| SCORE-UX（5） | topology/timeline/control/virtualization/a11y 自动行为与生产构建 | 推进 | browser viewport 截图待环境可用 |
| SCORE-ROBUST（5） | stale owner、retry bound、sealed、disconnect、late receipt、recovery tests | 推进 | 重复 live run/MTTR 后续 |
| SCORE-EFF（5） | large graph/history virtual caps、sparse route/placement/SLA overlay | 推进 | 正式 token/time/cost 消融后续 |

## 提交与状态边界

- numeric-stage base：`f7fff49be91fb0f797260c03ff9cca76c06c5974`
- A01 decision / implementation / evidence：
  `870c52d6efe833bb501ac93ed2bd5e631464d7eb` / `7ea026f1de635197d84b3179d5494e96ca687577` / `a2fa483a75ccae4c14bf9cd94aa0cda41346c28e`
- A02 decision / implementation / evidence：
  `dde72a7bb36d6431d4138ff176617687241dc791` / `6b15c5d8924a113553eac4d21922bc28239696dc` / `0980c52e1a0624a06501ccd0c39a723e624c95e3`
- B01 decision / implementation / evidence：
  `9ce8748e05705809e40f581f8761f76dc26d007b` / `636ba5dc9887c9d563fb0ba2949f50b6d0fcbe42` / `46be71eb4ced2da38f5b05fce24907da7ffc8f6c`
- B02 decision / implementation / first aggregate fix / evidence：
  `482cc5f4b582e1b81eeda48c0163e88838f295e8` / `fdf82f2059153b917ca2486cdcbe1b8d68b790b9` /
  `f3302a3c5366218068cb8acf9790c973e4b62033` / `44c9f1ac907f5cd16eeb972d332e8d6a44952ffa`
- 本次 review-fix 与最终 target：`ef467642364a4eb2108160e16d25ab156d59d37c`
- cleanroom target：`ef467642364a4eb2108160e16d25ab156d59d37c`
- review/evidence commit：包含本报告和机器证据的后续独立 commit（证据中记为 `this_commit`）。
- `execution-state.yaml` 只在 review/evidence commit 形成后追加本次独立复审记录，不重写四个原 slice
  的历史 implementation/evidence 事实。
- `G:\agent-zoo` 根目录不是 Git 仓库；`execution-state.yaml` 的更新不属于 Zyra commit，最终交付必须
  单独说明。Zyra 报告和 evidence 位于 `G:\agent-zoo\zyra` Git 仓库。

## 下一步

本轮没有遗留的通过前必修项。权威下一入口仍为
`docs/milestones/M2-console-demo/slice-03a-01-artifact-custody-catalog-viewers.md`；进入前继续以
`execution-state.yaml` 为唯一进度源。browser 实例恢复后补 viewport 视觉回归，但它不阻断本次已
完成的代码、行为、source-role 与 cleanroom 审查。
