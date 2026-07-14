# M1-S05A-02 Workspace Manager Integration 增量批判式自审

## 1. 结论

- Slice：`M1-S05A-02`
- 基线提交：`5e232ca3a9c9185ea7c8ec557610094b1fa7af06`
- 实现提交：`afb5654488f285a8e3fa549bcfc74d12e8b5095c`
- 父级：`M1-05A`
- 自审结论：**当前 integration slice 完成，父级 M1-05A 已累计收口；下一入口只能是 `M1-S05B-01`。**
- 高风险处置：本 slice 把默认 CodeWorker/BrowserWorker mutation path 改为 WorkspaceEditPort，并实现 workspace rebind/lease CAS 语义；已执行匹配风险的 permission/subagent/API 邻接回归，以及精确实现提交的 source-free cleanroom。数字阶段 M1-05 聚合审查仍不得省略。

## 2. Slice 目标覆盖

| 目标 | Zyra-owned 落位 | 行为证据 | 结论 |
| --- | --- | --- | --- |
| worker mutation gateway | `integration_models.py`、`transactions.py`、`executor.py` | file read/write/edit 通过 lease、read identity、quota、snapshot、journal 与 rollback；缺少 port 时 fail closed | 完成 |
| shell child workspace 与三方合并 | `isolation.py`、`tree_state.py` | shell 在真实隔离副本执行，按 base/current/child merge；保留用户 WIP，冲突持久化 | 完成 |
| transaction journal 与重启恢复 | `integration_store.py`、`recovery.py` | operation/step/rollback/merge/recovery durable；restart 可识别并恢复未完成事务 | 完成 |
| workspace rebind | `rebind.py`、`manager.py` | freeze→snapshot→dirty check→materialize/verify→ref migration→binding CAS→resume；CAS 前回滚、CAS 后目标权威 | 完成 |
| signed path-free handoff | `handoff.py` | envelope 只含 opaque ref、lease/capability、expiry/signature；不泄漏 physical path | 完成 |
| Browser 下载与 workspace URI | `browser_worker.py`、`transactions.py` | `workspace:///` 读取经 port；下载先写 DOWNLOAD mount，再由 artifact owner 接管 | 完成 |
| API 主路径 | `api_service.py`、`apps/api/zyra_api/main.py` | file write 使用 transaction gateway；新增真实 rebind route 和 typed failures | 完成 |
| 幂等、stale handle 与安全失败 | `integration_store.py`、`transactions.py`、`rebind.py` | stable request digest；in-flight 重放拒绝；旧 token/epoch/revision、越界路径、冲突均 fail closed | 完成 |

## 3. 来源角色、裁剪和正式落位

| 来源 | 角色 | 保留机制 | Zyra-owned 裁剪结果 |
| --- | --- | --- | --- |
| OpenHands | primary implementation | workspace transaction、snapshot/rebind、失败恢复 | 拆入 `transactions.py`、`integration_store.py`、`rebind.py`、`recovery.py`；未迁入 OpenHands server/database/runtime owner |
| AgentScope | supplementary implementation | backend refresh、binding refresh/rebind | 收敛为 `rebind.py` 的 backend-neutral materialize/verify 与 binding CAS；不创建第二套 workspace store |
| oh-my-pi | supplementary implementation | isolated child edit、dirty-aware merge、nested outcome | 收敛为 `isolation.py`、`tree_state.py` 的 immutable tree/delta/merge；不透传上游执行器或状态模型 |
| claude-code-best | conformance only | tool context、result/worker handoff 行为对照 | 不产生 production owner 或运行依赖 |
| opencode | reference only | durable session/file-edit 对照 | 不产生 production owner 或运行依赖 |

三条新增 source-role decision 已由同步脚本写入 internalization ledger；与 M1-S05A-01 合计八条且旧事实未被改写。正式运行不依赖根目录来源仓库、vendor/source-pool、editable path、外部进程、MCP server、Docker、本地辅助端口或动态 import。

## 4. 状态 custody

| 状态域 | canonical owner | 本 slice 责任 |
| --- | --- | --- |
| task/checkpoint | `SQLiteStore/TaskState` | 只持有 opaque `workspace_ref`；不复制 binding、lease、mount 或 transaction state |
| canonical events | `EventLog` | API/worker 的真实 workspace mutation、artifact、rebind 和 recovery receipt 可追溯 |
| workspace binding/mount/lease/usage | `WorkspaceManagerRuntime + WorkspaceBindingStore` | 继续是唯一 canonical workspace owner |
| integration operation journal | `WorkspaceIntegrationStore` | 记录 transaction/step/idempotency/rollback/merge/recovery；从属于 binding owner，不能提交第二份 canonical binding |
| physical task bytes | `LocalWorkspaceBackend` | 执行 typed mount、path containment 与 fenced mutation |
| file read identity/precondition | `WorkspaceFileStateStore` | mutation 前的 worker+path identity；rebind/restore/lease change 后失效 |
| dirty ownership | `WorkspaceRepositoryOwnershipStore` | 区分用户 WIP 与 agent-owned change；rollback 恢复 ownership |
| snapshots | `WorkspaceSnapshotRuntime` | transaction/rebind 的 committed restore point 与 verified materialization |
| artifact bytes | `LocalArtifactStore` | download/artifact 最终 canonical owner；workspace journal 只保存 artifact ref |
| permission/session | M1-03A `PermissionStateStore` | permission 先于 mutation gateway；本 slice 不复制审批状态 |
| physical worker resource lease | 后续 `M1-07A` | workspace lease 仅做访问 fence，不伪装成 scheduler/edge/cloud resource lease |

## 5. 真实主路径、动态可达性和断开即失败

| 入口 | 运行链 | 语义效果 |
| --- | --- | --- |
| CodeWorker file read/write/edit | tool loop → `ToolExecutor` → required `WorkspaceEditPort` → transaction coordinator | 真实改变 task mount；read identity、lease、quota、snapshot、journal 与 rollback 同时生效 |
| CodeWorker shell | permission → child isolation → real shell handler → three-way merge | shell 不直接污染 canonical tree；用户 WIP 保留，冲突进入 durable recovery |
| BrowserWorker workspace/download | browser action → workspace port / DOWNLOAD mount → artifact store | workspace URL 受同一 fence；download 产生真实文件与 artifact ref |
| Workspace HTTP API | files/rebind route → manager integration runtime | API 不绕过 gateway；typed transaction/rebind response 进入主路径 |
| restart | integration store scan → recovery coordinator | 未完成 operation 不被当成成功；按 commit phase 回滚或继续恢复 |

断开 `WorkspaceEditPort` 后 CodeWorker/BrowserWorker mutation 直接失败，不回退 legacy global directory；关闭 local backend、断开 path policy、binding store 或 integration journal 后对应真实行为测试失败。因而本 slice 的 transaction、merge、rebind、recovery 不是 import smoke、ledger、固定 ACK 或薄 adapter。

## 6. 安全、失败与恢复语义

- transaction 在 mutation 前校验 task/workspace/binding、owner epoch、lease、capability revision、logical mount/path、read identity 与 idempotency digest；未知或 stale handle 确定性拒绝。
- file mutation 使用 transaction snapshot 与 step journal；失败恢复 bytes、file-state 和 dirty ownership，不用 fallback 掩盖 gateway 失效。
- shell child tree 不包含 `.git`，只允许 branch-local delta 回到 canonical workspace；three-way merge 不依赖进程完成顺序，冲突以 typed durable outcome 暂停提交。
- idempotency key 与稳定 request digest 绑定；已提交请求重放返回同一 outcome，不轮换 owner epoch；尚未提交的同 key 请求返回 operation-in-progress/conflict。
- rebind 在 CAS 前保持旧 binding 权威并可清理 target；CAS 成功后 target 成为权威，旧 ref/lease 被撤销，source cleanup 只能作为后续阶段，不允许回滚 canonical binding。
- handoff envelope 使用签名、过期时间、opaque workspace ref 和 capability fence；public/API/event projection 不携带 physical host path。
- path traversal、absolute/drive/UNC、reserved control path、symlink/reparse containment 继续由 M1-S05A-01 policy fail closed；本 slice 没有新增绕过入口。

## 7. 有效行数分桶与父级收口

基于 `git diff --numstat 5e232ca3 afb56544 -- apps packages skills scripts tests`：

| 桶 | 新增 | 删除 | 是否计 production |
| --- | ---: | ---: | --- |
| workspace integration runtime（排除 package exports） | 7,644 | 11 | 是 |
| API/runtime/BrowserWorker 真实主路径改造 | 282 | 10 | 是 |
| 本 slice 保守有效 production | **7,926** | **21** | **是，超过 6,000** |
| package export `__init__.py` | 109 | 0 | 否 |
| tests | 714 | 0 | 否 |
| ledger sync script | 146 | 11 | 否 |
| ledger seed data | 358 | 9 | 否 |
| generated/data-as-code/mock-only/fixture-only/adapter-only | 0 | 0 | 否 |
| vendor/vendor-runtimes/source-pool | 0 | 0 | 否 |

M1-S05A-01 保守 production 为 `8,116`，本 slice 为 `7,926`，父级累计 **16,042**，超过 M1-05A 的 `14,000` 下限。通用 ledger verifier 报告的 `effective_added=8,895` 会包含测试/脚本，本自审没有用该数字抵扣 production 下限。

## 8. 验证记录

在实现提交 `afb5654488f285a8e3fa549bcfc74d12e8b5095c` 上完成：

1. 最新直接行为组合（workspace integration unit、worker gateway、workspace API）：`14 passed`，30.54s。
2. CodeWorker tool loop/permission continuation、BrowserWorker/permission、subagent command unit/API、workspace foundation/API/dirty 邻接回归：`89 passed, 10 subtests passed`，130.28s。
3. source ledger/unit 回归：`15 passed`；同步检查输出 `workspace_manager_source_ledger_aligned=true`、`decision_count=8`、owner units 为 `M1-S05A-01,M1-S05A-02`。
4. 精确实现提交 source-free cleanroom：从 `git archive afb56544` 生成 `G:\agent-zoo\cleanroom\M1-S05A-02-afb5654`，目录无 `.git`、无根目录来源仓库，清空 workspace 环境变量、禁止 bytecode 与 pytest cache；结果 `115 passed, 10 subtests passed`，172.19s。唯一 warning 是测试 dataclass 的既有 collection warning。
5. internalization ledger verifier：`ok=true`、`blocker_count=0`、`error_count=0`；552 条为全 seed 的既有 warning，不是当前 slice error。
6. 修改/新增 Python 文件 `py_compile`、`git diff --check`：通过；vendor/vendor-runtimes diff 与 dependency manifest 新增均为空。

普通 slice 未无差别重复全仓长期套件。此次默认 worker mutation 与 rebind/lease 高风险已经由精确实现提交 cleanroom 和受影响邻接回归覆盖；M1-05A 到 M1-05D 全部完成后的数字阶段聚合仍必须在同一最终 target commit 执行适用全仓测试、完整 cleanroom、全量 ledger/source-to-target audit 和独立批判式复审。

## 9. 实现期间发现并修复

1. 初版 idempotency digest 混入可轮换 epoch，导致合法重放被误判；改为稳定请求内容摘要，提交重放不再改变 workspace epoch。
2. 初版 idempotency claim 未立即绑定 transaction id，重复请求可能并发穿透；现在 claim 创建时即绑定，未提交重放 fail closed。
3. transaction rollback 首版只恢复 bytes/file state，未恢复 dirty ownership；现在 ownership snapshot 与文件回滚同批恢复。
4. Browser download 首版可能在 journal 建立前产生文件；调整为 transaction 先登记 operation/step，再写 DOWNLOAD mount 并发布 artifact ref。
5. tree scan 首版会把 `.git` 纳入 child delta；明确排除 Git admin tree，避免 repository metadata 被 shell merge。
6. 首次 cleanroom 命令的工作目录错误，实际落到原仓库；该结果被明确作废，随后在无 `.git` 的精确 archive 目录重新执行并通过，最终证据只采用纠正后的结果。

## 10. 批判式剩余风险

1. 当前 production backend 仍是 local；container/edge/cloud backend、真实隔离资源和跨 backend transfer 由 M1-05B/M1-05D/M1-07A 承担，本 slice 不伪装为真实端边云 dispatch。
2. workspace binding store、integration journal、SQLiteStore/EventLog/ArtifactStore 跨 owner 不构成分布式事务；当前以 phase、idempotency、snapshot 和 recovery receipt 显式暴露 partial failure。逐 commit-point crash/fault matrix 必须在 M1-05 数字阶段聚合扩展。
3. 已覆盖真实 dirty WIP、nested outcome、merge conflict、rollback 与 restart；linked worktree/submodule/reparse point、case-fold collision、超大 tree 和高并发 rebind stress 仍需聚合审查。
4. shell isolation 是本地 child workspace，不是 OS/container sandbox；命令权限仍由 M1-03A，进程/网络/credential gateway 由下一父级 M1-05B 收紧。
5. 本父级关闭的是 workspace manager 工程门禁，不关闭赛题 live 双场景、2,000 canonical transitions、真实端边云 dispatch、异常恢复、可视化因果轨迹与最终交付门禁。

## 11. 父级收口与后续强制入口

M1-05A 的 source-to-target、唯一 state custody、默认 CodeWorker/BrowserWorker/API 主路径、失败/恢复路径、动态可达性、断开即失败、source-free cleanroom 与 `14,000` production 下限均已累计核对，父级可以关闭。

下一入口为 `M1-S05B-01 sandbox gateway foundation`。它必须复用当前 WorkspaceEditPort、binding/lease/path 与 permission contracts，不能创建第二套 workspace owner，也不能把本地 child workspace 宣称成 container/edge/cloud sandbox；新增进程、Docker、本地端口或外部依赖时必须按高风险规则升级验证。
