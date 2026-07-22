# M1-S08-01 main-path hardening foundation 批判式自审

日期：2026-07-22

baseline：`44da53ad8ea909147709857e358b7d16e39f6313`

实施前冻结：`3a16f182745dd2a3506bd7d07b8e8ef9f77b58a5`

implementation：`6717ccc177139046a60a5d831c1d9b6129df315a`

## 结论

本 foundation slice 完成，但父级 `M1-08` 和 M1 均未关闭。Zyra 新增了可从真实 HTTP API 和产品 CLI
触发的 M1 硬化运行时：它逐项审计 43 个 source-to-target 裁决、12 个 canonical state family，检查
默认路径依赖与 state custody，执行动态 LangGraph 反模式探针、可逆模块断开探针、长程有效进度计数、
低熵/自治/端边云/provider maturity/cross-cutting gate，并把结果写入带 digest chain 的原子报告存储。

真实 HTTP 场景已通过任务创建、control export artifact、CodeWorker permission suspension、requirement
change、CodeWorker runtime disable 和 task/event 回读。`GraphStateStore` 默认断开探针也已真实执行：baseline
初始化 canonical graph store 成功，切断 `GraphStateStore.initialize` 后得到稳定
`graph_custody_disabled`，无 fallback，随后恢复并再次通过。foundation policy 接受该报告只表示本切片
基础门禁成立，不表示最终 2,000 transitions、sealed autonomy、real edge/cloud 或双真实 provider 已完成。

保守逐文件审计得到 **9,193 行有效 production**，超过本 slice 最低 **9,000 行**，余量 193。
测试、文档、import、Protocol、DTO/schema、签名续行、空白、docstring/literal 与 pass-only 均未计入；
vendor/source-pool/generated/data-as-code/adapter-only 为 0。机器证据见
`docs/reviews/evidence/M1-S08-01-main-path-hardening-foundation.json`。

## 目标覆盖矩阵

| 目标 | 生产落位 | 动态证据 | 裁决 |
|---|---|---|---|
| role-aware source completion | `catalog.py`、`coverage.py` | 43 items、38 capabilities；role/maturity 分列；primary 最多 1、supplementary 最多 2 | 通过 |
| dependency/clean-room 增量检查 | `dependency.py` | 扫描 import/package/process/symlink/path/cache/opaque asset/OpenClaw forward boundary | 通过；完整 cleanroom 后移 08-02/退出审查 |
| state custody | `custody.py` | 12 families 的 schema/store/write/read/restore/event/revision/idempotency/correlation 解析 | 通过 |
| disable matrix foundation | `disable.py`、`probe_catalog.py` | GraphStateStore 可逆断开 + live CodeWorker TypeScript runtime 断开；fallback masking 检测 | foundation 通过；全矩阵保留 08-02 |
| LangGraph negative boundary | `langgraph.py` | topology mutation、nested alias isolation、deterministic permutation、write conflict、pending/committed、side-effect fence、CodeWorker cohesion | 7/7 通过 |
| long-horizon accounting | `progress.py` | 2,000 semantic transitions / 1,000 action 单测，heartbeat/no-op 排除 | 计数器通过；非正式 benchmark evidence |
| low entropy / sealed autonomy | `entropy.py`、`autonomy.py` | budget/baseline 与 zero-human/recovery policy 的 fail-closed gate | foundation 可执行；正式 run 保留 08-02 |
| tier/provider maturity | `deployment.py` | real handshake/lease/route/artifact 与 distinct wire-dialect maturity；loopback 不能晋升 active_real | gate 可执行；真实部署证据保留 08-02 |
| Patch/Git、deny、secret/injection、code index | `cross_cutting.py` | 当前 task/event 与生产入口扫描，缺失语义效果时失败 | foundation 通过 |
| causal evidence / report custody | `evidence_graph.py`、`reporting.py`、`store.py` | revision/causation chain、atomic JSON/Markdown、idempotent persist、tamper detection | 通过 |
| API/CLI 主路径 | `api.py`、`cli.py`、`service.py`、`apps/api/zyra_api/main.py` | real HTTP scenario、GET status、persist/load/verify；CLI status | 通过 |
| effective code gate | `line_audit.py` | Git hunk + Python AST/token conservative buckets | 9,193/9,000，通过 |

## 来源裁决与唯一 owner

本 slice 的 `migration_mode` 是 `audit_and_hardening_only`：没有取得任何上游控制流迁移配额，也没有
替换 02A–07C 已有 owner。新增 Python 代码属于 Zyra evaluation/control product boundary；它消费公开
port、挑战默认路径并保管 derivative report，不写 canonical runtime state。

| 来源 / 角色 | 审计范围 | 保留的 production owner |
|---|---|---|
| `claude-code-best` primary（8 rows） | QueryEngine/session/tool budget/permission/MCP/skill/subagent/compact/control | Claude-derived TypeScript runtime owners |
| `opencode` primary/supplementary（2 rows） | durable runtime event、provider control | RuntimeEventSpine、ProviderControlPlane |
| browser-use primary（3 rows） | message/context、watchdog、fault observation | browser message/watchdog/fault owners |
| OpenHands primary/supplementary（3 rows） | workspace、recovery、code index | WorkspaceManager、RecoveryApplication、CodeIndexService |
| AgentScope primary（1 row） | worker lifecycle/resource pool | WorkerPoolIntegrationRuntime |
| Oh My Pi（24 rows） | individually adjudicated supplementary/conformance/reference/experimental/deferred M1/M2/M3 entries | only existing Zyra task/provider/memory/patch/worker owners; inactive rows create no migration quota |
| LangGraph conformance（1 row） | checkpoint identity/lineage/pending/committed/exact resume semantics | GraphStateCustody and RecoveryPlanStore; no StateGraph/Pregel owner |
| Zyra-owned primary（1 row） | dynamic graph custody | GraphStateStore / GraphStateCustody |

Catalog totals are 18 primary, 6 supplementary, 2 conformance, 7 reference, 2 experimental and 8 deferred;
maturity totals are 24 `active_real`, 9 `conformance_verified`, 2 experimental and 8 deferred. OpenClaw is
`excluded_forward_only`: no source read, clone, package, process, source graph or runtime path was introduced.

## 状态 custody 与报告边界

`M1StateCustodyMap` resolves task/session、runtime event、permission、memory、worker lease、provider/backend、
graph topology、recovery、workspace、skill invocation、browser session 和 code index 共 12 个 state family。
每项均记录 canonical owner、schema/store、write/read/restore entries、event types、revision/idempotency/
correlation fields 与 derivative consumers。真实场景额外要求 task/session、runtime event、permission 和
workspace 在事件中可动态识别，缺一即阻断。

新增 canonical state 只有 derivative hardening report，由 `M1HardeningReportStore` 管理：写入采用临时文件
原子替换，report digest 与 append-only chain 可验证，同 report 重入保持幂等，tamper 后 load/chain verify
失败。它不修复或覆盖任何 runtime owner 的状态。

## 主路径、语义效果与断开即失败

生产入口包括：

- GET `/hardening/m1/status`、`/hardening/m1/reports`、`/hardening/m1/reports/{id}`、`/hardening/m1/chain`；
- POST `/hardening/m1/audit`、`/hardening/m1/reports/{id}/verify`、`/tasks/{id}/hardening/m1/foundation`；
- `python -m zyra_evaluation.m1_hardening` 的 `audit`、`scenario`、`status`、`reports`、`show`、`verify`、`compare`。

真实 HTTP scenario 不是 fixture replay：它创建新 task，经 control command 写 artifact，CodeWorker file-write
进入真实 permission suspension，`/change` 改变同一 task/session，禁用 TypeScript runtime 后请求显式失败，
最后回读 task/events 并确认未产生未授权 workspace side effect。scenario event、artifact、task revision 和
causation进入同一个 hardening report。

第二条动态失败证据使用 fresh `GraphStateStore` 实例和真实 SQLite schema。禁用 class-level initializer 后，
fresh owner 无法被旧数据库、mock 或 alternate owner 掩盖；restore 后相同 exercise 恢复。若断开 hardening
service/API，则上述 route、17-gate execution DAG、report artifact 和 chain verification 均不可用；对应
integration test 会失败。

## 批判式缺陷发现与修复

1. 初版 decision 写入了错误的 baseline full hash；实施提交已校正为实际 `44da53ad...6313`。
2. provider extractor 最初把普通 `local-sandbox` backend 当 provider，导致假 blocker；现只接受明确
   provider id 或 provider/model wire semantics，不能用 label 推断 maturity。
3. long-horizon foundation 最初把正式 benchmark revision/action 完整性当即时 blocker；现 foundation
   保留可见 finding，只有 final completion 才强制 1,000/2,000 和完整 pair。
4. runtime custody 最初要求 live foundation 场景同时产生全部 12 类状态；现静态 map 仍验证全部 owner，
   live scenario 只强制实际触达的 task/event/permission/workspace，不伪造其它事件。
5. cross-cutting evaluator 最初因 closure 写法重复执行同一 gate 四次；现每个 evaluator 绑定独立 callable。
6. dependency scan 最初把测试、remediation 和 deny-policy 文本中的路径字面量当运行依赖；现只扫描生产
   入口与真实 dependency/process/path 语义，同时仍保留 OpenClaw forward-only 检查。
7. report idempotency 最初把存储回填字段纳入内容 digest，第二次 persist 不稳定；现 digest 输入剥离
   storage envelope，并由 tamper test 验证。
8. 通用 disable runner 初版没有默认可执行 probe；补入 GraphStateStore real-owner probe，并把 live
   CodeWorker disconnect evidence 合并进同一 gate。final mode 仍会对未注册的完整矩阵 fail closed。

## 有效行数分桶

统计区间固定为 `44da53ad8ea909147709857e358b7d16e39f6313..6717ccc177139046a60a5d831c1d9b6129df315a`。

| 桶 | 行数 | 计入最低线 |
|---|---:|---|
| raw additions | 12,092 | 否 |
| production raw | 11,603 | 否 |
| tests | 375 | 否 |
| docs | 114 | 否 |
| blank | 802 | 否 |
| import | 258 | 否 |
| Protocol | 14 | 否 |
| schema/DTO header + fields | 423 | 否 |
| signature continuation | 888 | 否 |
| comment/docstring/literal/pass-only | 25 | 否 |
| generated/data/vendor/source-pool/adapter-only/mock-only | 0 | 否 |
| **conservative effective production** | **9,193** | **是** |

全部有效代码均为 Python；`apps/api/zyra_api/main.py` 计 61 行 API wiring，hardening package 计 9,132
行产品执行逻辑。测试和 predecision 文档均由 auditor 强制归零。没有修改 `vendor/**` 或
`vendor-runtimes/**`，没有新增 lockfile、package、editable path、npm link、Docker context 或 sibling
source-repository runtime dependency。

## 验证

- 直接 foundation：`7 passed in 8.54s`，覆盖 source/custody、7 个动态 graph probe、generic 与 default
  disable/restore、2,000/1,000 progress accounting、causal revision failure、report tamper。
- 真实 HTTP 与全 hardening service：`1 passed in 83.16s`；17-gate foundation report accepted，report
  persist/load/verify 通过，GraphStateStore 与 CodeWorker 两条 disconnect evidence 均无 fallback。
- 相邻 owner 回归：`11 passed in 20.38s`；覆盖 dynamic graph custody、provider control plane、runtime
  event spine、change command 和 compact/export artifact。
- Python `compileall`、CLI `status --compact`、`git diff --check` 通过；CLI 显示 43 catalog items、38
  capabilities、`disable-graph-state-store` 和有效空 report chain。
- effective line gate：`passed`，0 blocker/error/warning，9,193/9,000。

额外相邻探测发现不在当前 diff owner 内的既有问题：`tests/unit/test_query_session_foundation.py` collection
仍缺少 `CodeWorkerSessionFoundationRuntime` export；`test_code_worker_query_session_foundation.py` 与一个
CodeWorker permission approval 用例仍有原 runtime/default-path 失败。本 slice 没有修改该 runtime owner，
也未用 fallback 掩盖；这些结果记录给 08-02 聚合定位。普通 foundation 未运行全仓测试、完整 cleanroom、
全量 ledger/source-to-target audit；它们由 08-02、M1-08 数字阶段聚合和 M1 退出审查强制执行。

## 未伪报、残留债务与下一入口

本 slice 不声称：完整 QueryEngine/session/tool/permission/workspace/gateway/event/provider/memory/curator/
skill/worker/watchdog/recovery disable matrix 已跑完；两个高完成度跨领域 live task 已完成；单 run 已产生
正式 sealed 的 1,000 actions/2,000 transitions；动态稀疏低熵对照、真实 local/isolated-edge/cloud dispatch、
两种真实 provider wire、多模型、零人工 fault/recovery 或 M1 exit evidence 已关闭。

这些是 `M1-S08-02-main-path-hardening-integration` 的明确 blocker/交付输入，不是可忽略 limitation。因此父级
`M1-08` 仍为进行中，下一入口严格为 `slice-08-02-main-path-hardening-integration.md`。evidence commit 创建后
才更新根目录 `G:/agent-zoo/docs/milestones/execution-state.yaml`；该文件不属于 Zyra Git，最终交付必须
单独说明。
