# M1-05C / M1-05D 四切片聚合审查与修复报告

## 1. 结论

审查范围为 `M1-S05C-01`、`M1-S05C-02`、`M1-S05D-01`、`M1-S05D-02`。按照
`G:\agent-zoo\docs\执行单元完成后通用审查任务书.md` 对目标、DoD、真实主路径、状态托管、
source-to-target、有效行数、失败路径、cleanroom 和赛题证据逐项复核后，最终裁决为：

**PASS_AFTER_REMEDIATION**。

- 审查前目标提交：`22795ee81139bb02fea2d6dcb1dce935fe3dcd0f`。
- 行为基线：`8e0662ff6a5809390817641d05e9d73905022618`，即四切片实现前的
  M1-05A/M1-05B 聚合证据提交。
- 审查修复提交：`11740ae0d618cc580209de51df8213180b926432`。
- 修复后本范围阻断项：`0`。
- M1-05C 父级累计保守有效生产代码：`16,246 >= 16,000`。
- M1-05D 父级累计保守有效生产代码：`23,033 >= 18,000`。
- 下一入口保持为 `M1-S06A-01`；本报告不把工程内化通过冒充赛题 live/edge/cloud/
  2,000-transition 门禁关闭。

## 2. 审查对象与历史证据

| Slice | 实现提交 | 原 evidence 提交 | 原裁决 |
|---|---|---|---|
| M1-S05C-01 | `62d355675c0656c5714636c962a42746d7ad734a` | `439860b222bde3b85b9eefac669b00f1a4314246` | PASS |
| M1-S05C-02 | `156b6e80b11558a11f29328e17191a210299fba0` | `deb2c5a569f118862be1506a5749d4579a47be11` | PASS |
| M1-S05D-01 | `63616b4`、`009c932`、`ef0ea83` | `f0700258840da4514b0090e7851e8abff4879409` | PASS |
| M1-S05D-02 | `dc010d102ca4b74d7f13e42952706014e2fc40e2` | `22795ee81139bb02fea2d6dcb1dce935fe3dcd0f` | PASS |

审查完整读取了四份 slice 文档、两份父级 unit 文档、四份自审与机器证据，以及当前
execution state、赛题追踪矩阵、第一阶段总工程计划、架构内化索引、source-graph 重排裁决和
LangGraph 第 12 节。OpenClaw 仅保留到 `M1-S05D-02` 的历史 provenance；没有恢复已删除仓库，
也没有为后续入口重新引入它。

## 3. 发现与修复

### 3.1 已修复的阻断问题

聚合运行暴露出一个共享进程生命周期竞态：API 测试先关闭进程级 runtime-event port 后，
scheduler/API 后续请求可能复用已经关闭的缓存 bridge，报出 `runtime event port is closed`；
同时，`server_close()` 会在仍有 daemon handler 执行时先关闭 sidecar。单文件、新进程或一次
偶然通过不能证明该问题不存在。

修复提交 `11740ae` 完成了以下调整：

1. 在 Runtime Event Python integration 中新增按对象身份释放的
   `release_runtime_event_spine()`，避免一个 API 实例重置所有进程级 bridge。
2. API 在数据库 key 轮换时从 registry 定向驱逐旧 bridge，避免 A -> B -> A 时返回已关闭的 A。
3. API server 追踪活跃 handler；关停时先关闭 listener，最多等待 30 秒让在途请求排空，再关闭
   runtime-event/provider sidecar。
4. scheduler HTTP 集成测试的客户端超时由 15 秒调整为 30 秒，与同进程冷启动的实测上界匹配；
   这不延长业务 deadline，也不掩盖服务端失败。
5. 新增三个断开即失败测试：定向释放不影响另一 bridge、数据库 key 回切获得健康新实例、
   `server_close()` 在 handler 排空前不关闭 sidecar。

审查修复分桶：production `+64/-9`，test `+130/-1`；generated、data、docs、vendor/source-pool
计数均为 `0`。

### 3.2 修复后未解决阻断项

本次范围内为 `0`。

### 3.3 非阻断风险与既有债务

- 仓库广域 pytest 仍存在六个已由受保护 R01 TypeScript cutover 删除的 Python-only import
  collection 债务；排除这些过期收集入口后，当前失败节点均在行为基线重现，未发现 05C/05D
  新回归。
- 全局 legacy internalization-ledger audit 仍报告受保护历史记录的 blocker/warning。05D owner-filter
  与同步 seed 为 `7` 条、目标均存在且策略为 `0 error/0 warning`；本报告不改写历史事实。
- 05C 的当前 source-to-target 绑定保存在 slice review/evidence 中，旧父级 seed 的 31 条记录并未
  追溯改写成 `M1-S05C-01/02` owner 行。这是治理可检索性缺口，不影响已验证运行主路径；应在
  M3 的统一 source-map 工具化时纳入，而不是回写受保护历史。
- M1-S05D-01 的 `7,575` 行 wire contract 中包含一个 6,401 行的 OMP direct-port 类型合同。
  该文件进入正式 TypeScript 包、参与编译，顶层响应/流事件类型被真实 provider transport 使用，
  因此按当前直接源码迁移口径计入；若后续冻结审查采用“所有上游 wire declaration 一律按
  vendor-like 排除”的更严格口径，则 D01 为 `6,880 < 9,000`，父级 D 为
  `16,632 < 18,000`。M3 有效行数工具必须明确这一口径，不能静默改变或机械放大计数。
- Docker CLI 可用但 daemon 不可用；外部 cloud dispatch 未执行。本报告只确认 loopback HTTP
  transport 与工程 dispatch 语义，不声称关闭真实 isolated edge/cloud 门禁。

## 4. Source-to-target 与来源去重

### 4.1 M1-05C

- primary：opencode 的 typed durable event/session/message 机制，经裁剪进入
  `packages/runtime/runtime-event-spine`，canonical owner 为 Zyra TypeScript
  `RuntimeEventSqliteStore`。
- supplementary：OpenHands 仅补 event folding/subscription/UI derivative；oh-my-pi 仅补 typed
  RPC/tool/subagent/compact frame correlation，二者均不持有 canonical state。
- conformance/reference：claude-code-best 约束 query/session/permission/compact 连续边界；
  LangGraph 仅用于 identity、pending/committed、replay/exact-resume 对照，没有 StateGraph、
  channel、Pregel、Store 或 ToolNode production owner。
- Python 只承担受监督进程、schema/error/fold、worker ingress 与 API protocol；legacy JSONL
  是 canonical commit 之后的兼容投影。

### 4.2 M1-05D

- provider domain primary：opencode provider/catalog/credential/route 机制，落入
  `packages/runtime/provider-control-plane`；oh-my-pi 和 Hermes 只补 wire transport、SSE 与有界
  resolver/lease handoff。
- backend domain：Zyra `BackendRegistryStore`、`BackendDispatchRuntime`、
  `WorkerDispatchRouter`、`BackendDispatchControl` 持有 canonical backend/dispatch 状态。
  OpenHands 的受保护 D01 历史记录保留；D02 起明确为 conformance/reference，不形成第二 owner。
- provider 失败只旋转 provider route，backend 失败只旋转 backend；两者同时变化必须显式升级。
  worker 不接收 provider 明文密钥，partial/side-effect output 不重放。

OpenClaw 没有运行期路径、包、进程或相对依赖。其 05D 历史 provenance 不因 2026-07-20 的
forward-only 排除而追溯重写。

## 5. 目标、DoD 与真实主路径

| 能力 | 主路径与状态 owner | 动态/失败/断开证据 | 裁决 |
|---|---|---|---|
| canonical runtime event | source admission -> SQLite canonical commit -> durable delivery/projector -> API/worker | duplicate repair、backpressure、catch-up、projector rebuild、bus/projector disable | PASS |
| typed event/RPC correlation | CodeWorker/OMP frame -> ingress -> canonical event/tool/subagent/compact lifecycle | malformed MCP、partial/final、cancel、correlation 与 byte spill | PASS |
| provider route/credential | ProviderControlPlaneStore snapshot -> real OpenAI/Anthropic loopback bytes/SSE | revoke zero-byte、503/429/stall reroute、partial-output reconcile-only | PASS |
| backend dispatch/failover | task graph/API -> WorkerDispatchRouter -> BackendDispatchJournal/Registry -> worker | unreachable primary -> live alternate、cancel/drain/resume、quarantine、replay/hash/outbox | PASS |
| provider/backend 分离 | provider lease ref 进入 dispatch envelope，backend 不持有 credential/provider state | provider-only 与 backend-only failure 互不旋转；双变更需升级 | PASS |
| lifecycle/restart | API listener/handler -> sidecar registry -> targeted release | A/B/A cache、并行 bridge、在途 handler shutdown killer tests | PASS_AFTER_REMEDIATION |

核心能力均能从 API、task graph、worker ingress 或实际 TypeScript/Python runtime 触发，不依赖
fixture replay、固定 health 返回或来源仓库 CLI。删除 canonical store、durable bus/projector、provider
control plane、backend registry/router 或本次 targeted lifecycle release，相关 killer test 会失败或
行为明显改变。

## 6. 状态托管与因果边界

| 状态域 | Canonical owner | 持久化/恢复 | 非 owner 边界 |
|---|---|---|---|
| runtime event facts/global order/route | TypeScript RuntimeEventSqliteStore | SQLite cursor、route decision、event idempotency | Python/API 仅协议与 supervised process |
| delivery/projector | DurableConsumerRuntime / ProjectionDeliveryRuntime | lease、ack、retry、dead-letter、checkpoint | UI/history 为派生投影 |
| provider catalog/credential/routes/attempts | TypeScript ProviderControlPlaneStore | SQLite immutable revisions/snapshots | backend 只保存 opaque provider-route ref |
| backend/dispatch session/journal | Python BackendRegistryStore / BackendDispatchJournal | lease、attempt、recovery、hash、outbox、materialization | provider 不持有 backend health/lease |
| task/workspace/artifact/event | 既有 task graph、workspace、artifact、event owner | 使用原 owner 的持久化与 epoch/fence | dispatch 只引用，不夺取 owner |

event log/trace 能关联 source event、route decision、tool call/result、worker attempt、artifact pointer、
recovery 与 materialization。外部副作用在 observable output 后进入 reconcile-only，不允许 fallback
掩盖已执行副作用。

## 7. 有效行数分桶

`whole commit additions` 为独立 `git numstat` 观测；`raw production candidate` 与 `conservative`
遵循各 slice 已发布的路径/非空行分桶。C01 的主要 event-spine 文件在历史 checkpoint
`0cd21bff` 已写入，最后实现提交只补完主路径，因此同时展示 final-commit additions 与从
`c34535a` 起按 05C owned paths 复核的生产分桶，避免把最后一个提交误当成全部实现，也避免把
期间 R01 改动算入 05C。

| Slice | Whole commit additions | Raw production candidate | 明确排除 | Conservative | 最低线 |
|---|---:|---:|---:|---:|---:|
| M1-S05C-01 | 1,599（final commit） | 9,745（owned-path scope） | adapter 455；exports 89；tests 755 | 9,201 | 9,000 |
| M1-S05C-02 | 8,459 | 7,933 | adapter/API/protocol、governance、test/export/docs 共 888 | 7,045 | 7,000 |
| M1-S05D-01 | 16,160 | 14,224 | adapter 801；exports 136；adjacent 6；tests 996；script/data/manifest | 13,281 | 9,000 |
| M1-S05D-02 | 11,794 | 10,524 | production boundary 772；tests 462；script 86；ledger 368；export/manifest/lock 344 | 9,752 | 9,000 |

父级累计：M1-05C `9,201 + 7,045 = 16,246`；M1-05D
`13,281 + 9,752 = 23,033`。generated、fixture-only、mock-only、source pool、ledger data 与
vendor-like 目录均未计入 conservative production。D01 wire-contract 敏感性见 3.3，不隐藏。

## 8. 验证证据

### 8.1 最终提交 cleanroom

cleanroom：`G:\agent-zoo\.tmp\cleanroom-M1-05CD-11740ae`；精确 HEAD：
`11740ae0d618cc580209de51df8213180b926432`；测试后 `git status --short` 为空。

- `bun install --frozen-lockfile`：通过，14 个安装项、24 个 package，无 lockfile 变化；Windows
  默认 temp ACL 失败后改用工作区 temp，属于环境修正而非业务重跑。
- Runtime Event Spine typecheck：通过；Node behavior `22/22`。
- Provider Control Plane typecheck：通过；Node `11/11`，locked Bun `1.2.15` 为 `11/11`。
- Claude runtime typecheck：通过。
- 四切片 Python 核心聚合集：`29/29 passed in 63.64s`。覆盖 event spine/API/message bus、
  backend registry/provider port/task graph/scheduler、backend failover、provider/backend API、
  scheduler API 与 E02 TypeScript API cutover。
- provider/backend source-ledger sync：`aligned=true`，`7` 条 decision，owner 为
  `M1-S05D-01,M1-S05D-02`。
- submission boundary verifier：通过。
- `apps/packages/skills` 指向根目录来源仓库的相对运行依赖：`0`。
- worker/code-worker/scheduler 直接读取常见 provider API key：`0`。

### 8.2 修复前复现与修复后稳定性

- 原 D02 组合 20 测试曾一次 `20/20` 通过，但在包含前序 API 生命周期的 8 测试顺序中稳定复现
  scheduler 超时和 `runtime event port is closed`，证明它是顺序相关竞态。
- 修复后 lifecycle killer tests `3/3`；相邻 lifecycle/event/scheduler 顺序 `9/9`；原 D02
  组合 `20/20`；最终 cleanroom 聚合 `29/29`。

### 8.3 仓库广域诊断与基线分类

直接运行仓库根 `pytest -q` 会错误递归收集 `.tmp/cleanroom`、cache 和 vendor，产生 224 个
collection error，因此不作为产品结论。`pytest tests` 暴露六个过期 Python-only import 收集入口；
排除它们后对 483 个测试进行分段，以避免 Windows 下长时间无输出的单进程超时：

| 分段 | 当前结果 | 基线 `8e0662f` 对照 |
|---|---|---|
| unit | 272 passed；11 个独立 failure（pytest 展开为 52 failed/179 subtests） | 相同 11 个 failure |
| integration later/direct | 35 passed；4 failed | 相同 4 个 failure |
| browser/control | 58 passed；24 failed；12 subtests；新 backend failover 4 passed | 当前 24 个 failure 均在基线失败集合；基线另有 5 failure/3 setup error |
| codeworker/E01-E04 adjacent | 47 passed；29 failed；8 subtests | 当前失败集合为基线 30 个 failure 的子集 |
| scenarios | 3 passed；1 failed | 同一 M2 `parse_slash_command` baseline failure |

因此广域失败均为受保护基线债务，本次变更没有新增失败节点；本报告不把它们写成绿色全仓测试。
Ruff 在当前 venv 未安装，未声称执行；changed-path `compileall` 与 `git diff --check` 通过。

## 9. 反伪内化与交付边界

- 正式实现位于 `zyra/packages/**`、`zyra/apps/**`，使用 Zyra schema、event、permission、state、
  error、artifact 与 test 边界；没有把整仓改名放入 formal package 后调用原入口。
- cleanroom 未复制根目录来源仓库；没有 `../opencode`、`../OpenHands`、`../claude-code-best`、
  `../browser-use`、`../openclaw` 等运行依赖，也没有 npm link、pip editable source path 或外部
  source CLI 独占核心决策。
- real loopback provider/backend transport 验证了实际 headers/body/SSE/HTTP failover，不是固定
  contract 或 mock-only 健康检查；Docker/external cloud 未验证的部分明确留在赛题门禁。
- ledger、manifest 和报告只承担来源/审查证据，不替代上述 API/runtime/worker 行为。

## 10. 赛题证据与未运行项

本次关闭的是 M1-05C/M1-05D 工程内化、行为和状态 owner 门禁。以下状态没有改变：

- 两个及以上高完成度跨领域 live 任务：未关闭。
- sealed autonomous policy 下单 run 2,000 个有效 canonical transitions：未关闭。
- 动态稀疏拓扑与低熵对照：05C 提供 event/policy 基础，但正式对照门禁未关闭。
- 真实 local/isolated edge/external cloud dispatch：local/loopback 工程路径已测；Docker daemon 与
  external cloud 未测，门禁未关闭。
- 多模型、异常/需求变更/节点失效恢复、可视化因果轨迹、算法伪代码/复杂性与提交材料：仍由
  后续 M1/M2/M3 和里程碑退出审查承担。

普通 slice 的相关测试、数字阶段聚合的 broad baseline 对照及高风险 exact-commit cleanroom 已
完成。没有为了本报告重跑与 05C/05D 无关、且已明确为 baseline debt 的数小时全仓串行套件。

## 11. 最终裁决

M1-S05C-01 至 M1-S05D-02 在修复 runtime-event bridge 生命周期与 API shutdown 竞态后满足当前
父级目标、真实主路径、失败路径、状态托管、来源去重、提交边界和保守有效行数门禁，裁决为
`PASS_AFTER_REMEDIATION`。执行状态应补记 `M1-05D` 父级完成及本次 review-fix/evidence commit，
但 `completed_through` 与下一入口仍分别保持 `M1-S05D-02`、`M1-S06A-01`。
