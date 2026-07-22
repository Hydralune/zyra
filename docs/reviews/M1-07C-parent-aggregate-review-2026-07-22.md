# M1-07C（M1-S07C-01 / M1-S07C-02）父级完成审查

审查日期：2026-07-22

父级 baseline：`aa494526ed7f32b177f9a10fa3a93d8a684e0264`

原 07C-01 implementation target：`0b331ca6f709a24e663136939257aa061010aacf`

原 07C-02 implementation target：`8de780f9410308725859a9994bdc2e27e5ba6042`

原父级 evidence target：`a95a6ce8d8f8e8e3eda35f01ba52fc3aba9c7989`

审查修复 implementation commit：`600ec308926786ea11185a50b4076c019dfbdf55`

## 结论

结论为 **发现 blocker，修复后通过 M1-07C 父级验收**。原完成记录不能原样通过：恢复 action 的 owner
mutation 虽然真实存在，但 post-action continuation 只是写入一条 `AGENT_MESSAGE` 就声称后续执行已改变；实际
`TaskState` 可仍为 `pending`，恢复 route 也没有被后续 graph dispatch 稳定消费。审查修复把 continuation
改为带 prepared/committed/failed fence 的真实 task-graph、worker lease、backend/provider dispatch，并修复
owner dispatch 后陈旧 `TaskState` 回写覆盖新状态的 lost-update。

修复后，`auto_run=false` 的任务经 recovery 必须真实进入 `completed`、产生 artifact、保存 execution event ids、
worker/backend/provider 引用；模拟 API runtime restart 后的 planned recovery 也必须再次完成真实 dispatch。
事件、proof、routing memory 只在该执行事实之后成立。

原冻结有效代码门禁不被此次 remediation 倒填：07C-01 为 8,429/8,000，07C-02 为 7,062/7,000，父级从
`aa494526..8de780f` 直接重算为 15,469/15,000。remediation 另报 389 行 raw production additions、98 行
raw test additions，不计入原父级阈值，也不改写原 implementation targets。

## 审查发现、首次违规提交与修复

### Blocker 1：continuation 是 event ACK，不是真实后续执行

- 首次违规提交：`774c8f87d9cdf193596d20fedf72fb3034bc5e38`。
- 原行为：`_recovery_continuation_owners()` 只持久化 `EventType.AGENT_MESSAGE`，随后返回 `accepted=true`、
  `changed=true`；没有调用 task graph、worker runtime 或 backend dispatch。
- 反证：新增断言在修复前得到 `pending != completed`，但 API 同时声称 feedback accepted、
  changed-later-decision 与 causal trace complete。
- 修复：`600ec308...` 将 continuation 绑定到真实 graph/worker dispatch，要求 non-empty execution events、
  backend final envelope 与 completed TaskState；不满足时 fail closed，并保存失败 fence，不允许把 event 当成功。

### Blocker 2：owner dispatch 后陈旧快照覆盖 canonical TaskState

- 首次违规提交：`774c8f87...` 引入 continuation runtime 与 owner callback 组合时形成该 lost-update 路径。
- 原行为：continuation runtime 在 owner callback 前加载 TaskState；若 callback 持久化了更新状态，runtime 随后
  仍保存调用前快照，从而把真实 `completed` 覆盖回 `pending`。
- 修复：owner 返回后重新加载同 run/task 的最新 canonical TaskState，再合并 continuation projection；跨 run、
  消失状态和不可写 metadata 均拒绝。

### Blocker 3：恢复 route 未成为实际 dispatch 输入

- 07C 首次错误完成声明提交：`774c8f87...`。底层 task graph 的旧选择逻辑更早存在，但在 07C 声称 recovery
  route 已应用前不构成本单元完成事实。
- 原行为：`worker_pool`、`backend_route` 与嵌套 `provider_route` 写入 TaskState 后，
  `_worker_request_metadata()` 仍优先使用旧 resource decision/manifest；replan 时甚至可能让 resource 建议覆盖
  已分配的 physical worker lease，把 code 任务误送到 BrowserWorker。
- 修复：worker lease、backend lease 与 provider route 显式进入 WorkerRequest/dispatch envelope；恢复期间已分配
  的 canonical worker lease 优先于资源建议。新增 unit test 用故意陈旧的 decision 验证六个 route/lease 字段。

### Blocker 4：07C-02 原 machine evidence 缺少语言 custody

- 首次违规提交：`a95a6ce8d8f8e8e3eda35f01ba52fc3aba9c7989`。
- 原行为：文字自审声称 TypeScript 非零，但 JSON 没有必填 `language_custody`；补字段后又暴露
  `source_decisions.migration_mode` 与 ledger 不一致。
- 修复：后续 evidence commit 补入冻结范围 `fabe347..8de780f` 的 TypeScript custody，并统一为 ledger 的
  `cropped_same_language_recovery_integration_receipts`。硬门禁实测 TypeScript production 1,031 行，0 violation。

## 来源裁决、状态 owner 与内化边界

- `oh-my-pi` 仅为 supplementary implementation：同语言裁剪 provider/credential rotation、partial-stream fence、
  MCP breaker、worktree WIP 与 background generation receipts；没有取得 policy、store、route 或 task owner。
- LangGraph 仅为 exact-resume conformance；没有 StateGraph、Pregel、channel/reducer 或 Store production owner。
- `claude-code-best` 在本单元只承担 recovery/permission/session 行为对照，不产生第二 canonical owner。
- Python `RecoverySignalClassifier` / `RecoveryDecisionRuntime` 继续拥有分类与策略；`RecoveryPlanStore` 拥有 plan、
  action receipt、checkpoint、outcome 与 routing-memory durability；`RecoveryIntegrationRuntime` 拥有集成编排。
- `SQLiteStore.TaskState` 是 canonical task projection；GraphStateCustody、WorkerPool、BackendRegistry、
  ProviderControlPlane、PermissionControlPlane 与 MemoryFabric 分别保留既有窄域 owner。
- TypeScript OMP runtime 只产生有界 supplementary receipts；不存在 `../oh-my-pi`、上级源码仓库、外部 CLI、
  sidecar owner、npm link、pip editable、Docker context 或 OpenClaw 前向依赖。

## 主路径、动态可达性、语义效果与断开即失败

真实入口为 `POST /tasks/{task_id}/recovery/signals`、
`POST /tasks/{task_id}/recovery/observations` 与 `POST /recovery/plans/{plan_id}/restart`。主路径现在是：

`typed observation/signal -> fused owner state -> deterministic decision -> canonical action receipt ->`
`fenced continuation -> task graph -> worker/backend/provider dispatch -> applied proof -> routing memory -> causal trace`。

断开或退化验证由测试直接覆盖：

- continuation 只返回 ACK 时，任务仍为 pending，新行为测试失败；
- 不重新加载 latest TaskState 时，真实完成状态被覆盖，新行为测试失败；
- 不消费 recovery worker/backend/provider projection 时，陈旧 route unit test 失败，replan 场景会错误进入 BrowserWorker；
- 不创建 recovery 独立 permission session 时，restart retry 被 custody gate 拒绝；修复没有持久化或绕过明文 token；
- 禁用 recovery component、classifier、route/memory、continuation 或 exact-resume owner 的既有 fail-closed 测试继续通过。

## 有效行数、依赖与提交边界

原冻结门禁：

| 范围 | effective production | 最低值 | 余量 |
| --- | ---: | ---: | ---: |
| M1-S07C-01 | 8,429 | 8,000 | 429 |
| M1-S07C-02 | 7,062 | 7,000 | 62 |
| M1-07C parent direct recompute | 15,469 | 15,000 | 469 |

审查修复 `a95a6ce..600ec30` 分桶：production raw additions 389 / deletions 65；tests raw additions 98 /
deletions 2；generated、data-as-code、vendor/source-pool、mock-only、adapter-only 与 dependency/lockfile additions
均为 0。该 raw remediation 不作为原门禁补量。

`zyra` 是唯一 Git 提交仓库；根目录 `docs/milestones/execution-state.yaml` 不属于该仓库，将在 evidence commit 后
单独更新并在交付说明中明确。

## 验证

- 修复后直接与 task graph：`16 passed, 6 subtests passed`。
- 07A worker pool + 07B watchdog/fault 相邻回归：`51 passed`。
- TypeScript 原语言 watchdog/recovery/recovery-integration：`15 passed`。
- 精确 implementation commit `600ec308...` detached cleanroom：frozen `bun.lock` 安装 17 个锁定包后，Python
  `67 passed, 6 subtests passed`，TypeScript `15 passed`；cleanroom Git HEAD 精确匹配且目录已删除。
- cleanroom 首轮在未安装锁定 Bun 时 fail closed 为 `typescript_runtime_unavailable`，没有 Python fallback；
  frozen install 后通过，证明依赖可从锁文件重建。
- Python compileall、`git diff --check` 通过；diff-scoped forbidden dependency scan 为 0。
- 两个 source-ledger sync 均 aligned，07C-01 与 07C-02 各恰好 1 条 production decision；targeted ledger
  finding 为 0。全局 ledger 仍有受保护历史/未来 planned debt，未错误归入当前单元。
- source-language custody：07C-01 TypeScript 1,146 added production lines；07C-02 TypeScript 1,031；均通过。

没有执行与当前 07C diff 无直接关系的全仓长耗时测试；本次因共享 recovery/task-graph 风险已升级为 07C 全集、
07A/07B 相邻全集与 detached cleanroom。完整 M1-07A/B/C 数字阶段聚合审查仍是下一入口，负责全仓、全 ledger、
完整 cleanroom 与跨 unit 结论，不能由本父级审查替代。

## 最终裁决与下一入口

M1-07C 在 `600ec308...` 实现修复及随后 machine evidence commit 存在后可判定为完成；07C-01/02 原提交历史
不 amend、不 squash，原缺陷与首次违规 commit 保留。下一入口仍为
`docs/执行单元完成后通用审查任务书.md` 对 M1-07A、M1-07B、M1-07C 的数字阶段聚合审查；聚合通过后才进入
`M1-S08-01`。
