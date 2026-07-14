# M1-S05A-01 Workspace Manager Foundation 增量批判式自审

## 1. 结论

- Slice：`M1-S05A-01`
- 基线提交：`dbff7f19e2b34b03fc598584891c7a78cc88b01b`
- 实现提交：`788186401e69fdc87fb7b472f1cfc7fbd1644272`
- 父级：`M1-05A`
- 自审结论：**当前 foundation slice 完成，但父级 M1-05A 尚未收口；下一入口只能是 `M1-S05A-02`。**
- 高风险处置：本 slice 改变默认 CodeWorker/BrowserWorker task workspace 路径，已按匹配风险执行相邻 permission/skill 回归和精确实现提交的 source-free cleanroom；未用普通 slice 结论替代 M1-05 数字阶段聚合审查。

## 2. Slice 目标覆盖

| 目标 | Zyra-owned 落位 | 行为证据 | 结论 |
| --- | --- | --- | --- |
| task workspace durable binding 与 ready barrier | `models.py`、`store.py`、`manager.py` | task 创建前完成 requested/creating/ready/open；失败时 task API fail closed | 完成 |
| backend-aware local workspace 与显式 disabled | `local_backend.py`、`manager.py` | local backend disabled 返回 503，且不回退 legacy global directory | 完成 |
| task/artifact/download/temp typed mounts | `mounts.py`、`local_backend.py` | 默认四类 mount、只读策略、公开投影不暴露 physical root | 完成 |
| 路径、symlink、quota 与原子写边界 | `paths.py`、`quota.py`、`atomic.py`、`local_backend.py` | traversal/symlink 拒绝；byte/file/directory quota 在 mutation 前拒绝；temp+fsync+replace | 完成 |
| read-before-write、base hash 与 stale worker fence | `file_state.py`、`store.py`、`manager.py` | 未读写拒绝、stale hash 拒绝、lease transfer 后旧 token/epoch 拒绝 | 完成 |
| Git read-only 边界、dirty baseline 与 nested repo | `git_boundary.py`、`dirty_state.py` | HEAD/index/worktree 不变；任意 subcommand/config injection 拒绝；用户 dirt 不可被 agent 认领 | 完成 |
| committed snapshot/restore 与 archive-before-delete | `snapshots.py`、`manager.py` | corrupt blob 在 live mutation 前 fail closed；restore 后 epoch 轮换；cleanup 先 archive | 完成 |
| 重启恢复 | `store.py`、`manager.py` | binding/mount/usage 恢复，内存中不可恢复的 fence token 不被伪造，首次 acquire 轮换 lease | 完成 |
| 真实 API/CodeWorker/BrowserWorker 主路径 | `api_service.py`、`apps/api/zyra_api/main.py`、`browser_worker.py` | task 自动创建 workspace；CodeWorker 写入 task mount；BrowserWorker 读取 `workspace:///`；workspace events 进入 EventLog | 完成 |
| permission custody 与 task workspace 一致 | `main.py`、`action_gate.py` | query session 轮换不迁移 workspace owner；custody 使用持久化 session binding 的 exact root；审批恢复测试通过 | 完成 |

## 3. 来源角色、裁剪和正式落位

| 来源 | 角色 | 保留机制 | Zyra-owned 裁剪结果 |
| --- | --- | --- | --- |
| OpenHands | primary implementation | start-task workspace lifecycle、ready barrier、durable backend/location、atomic persistence、archive-before-delete | 拆入 `packages/workspace/zyra_workspace/{manager,store,local_backend,snapshots,atomic}.py`；未迁入 OpenHands server/database/runtime owner |
| AgentScope | supplementary implementation | backend-aware manager、local workspace、mount/concurrency/TTL | 拆入 `manager.py`、`local_backend.py`、`mounts.py`、`quota.py`；未迁入 Docker/E2B/MCP gateway 或第二套状态 owner |
| oh-my-pi | supplementary implementation | dirty baseline、nested repository boundary、Hashline read epoch/base hash/stale preflight | 拆入 `git_boundary.py`、`dirty_state.py`、`file_state.py`；Git 只读 allowlist，不接受任意 subcommand/config |
| claude-code-best | conformance only | worker session/tool context 与 workspace handoff 对照 | 不产生 production owner 或运行依赖 |
| opencode | reference only | durable session/file-edit behavior 对照 | 不产生 production owner 或运行依赖 |

正式运行不依赖 `../OpenHands`、`../agentscope`、`../oh-my-pi`、`../claude-code-best`、`../opencode`。没有新增 pip/npm dependency、MCP server、插件、sidecar、Docker、本地辅助端口、动态 import、vendor 或 source-pool。

## 4. 状态 custody

| 状态域 | canonical owner | 本 slice 责任 |
| --- | --- | --- |
| task/checkpoint | `SQLiteStore/TaskState` | 只保存 opaque `workspace_ref`，不复制 backend location/lease/token |
| canonical events | `EventLog` | workspace lifecycle/operation receipt 在 API commit 点落盘 |
| workspace binding/mount/lease/usage/receipt | `WorkspaceManagerRuntime + WorkspaceBindingStore` | 唯一 workspace canonical owner；atomic JSON primary+backup |
| physical task bytes | `LocalWorkspaceBackend` | typed local mount、path/marker/quota/fence enforcement |
| file read epoch/base hash | `WorkspaceFileStateStore` | worker+path precondition，lease transfer/restore 后失效 |
| Git repository/dirty ownership | `WorkspaceRepositoryOwnershipStore` | read-only repository identity、baseline 与 agent-owned path claim |
| workspace snapshot metadata/blob | `WorkspaceSnapshotRuntime` | content-addressed blob、committed manifest、verify-before-restore |
| artifact bytes | 既有 `LocalArtifactStore` | workspace artifact mount 只保留边界，不夺取正式 artifact owner |
| permission/session custody | M1-03A `PermissionStateStore` | workspace 只提供 exact internal root；不复制 permission rule/request state |
| worker placement/physical resource lease | 后续 `M1-07A` | 当前 lease 仅是 workspace access fence，不伪装成 scheduler resource lease |

## 5. 主路径、动态可达性和断开即失败

真实入口从 `POST /tasks` 开始：

1. API 先调用 `WorkspaceManagerRuntime.create_for_task`，只有 workspace 跨过 ready barrier 后才返回成功 task。
2. binding、mount、lease、usage 和 lifecycle receipt 由 workspace store 持久化；公开 TaskState/API 只拿到 opaque/redacted projection。
3. `POST /tasks/{task_id}/workers/code` 按 task binding 获取 fenced root，CodeWorker 的 file/shell 工具真实在该 task mount 中运行。
4. `POST /tasks/{task_id}/workers/browser` 使用同一 task root；静态 backend 只接受经过 permission gate 与 containment 校验的 `workspace:///...`。
5. `GET/POST /workspaces/{workspace_id}/...` 执行 read/list/write/snapshot/restore/cleanup，并把 operation events 回写 EventLog。

关闭 local backend 后，task 创建直接返回 503，legacy `ZYRA_TOOL_WORKSPACE` 不承担 fallback；移除 workspace package 或断开 task binding 后，CodeWorker/BrowserWorker 主路径得到 typed workspace error。相关真实 HTTP、worker、disabled 测试均会失败，因而不是 import smoke、ledger 或静态 projection。

## 6. 安全、失败和恢复语义

- 所有 logical path 先规范化再逐段检查，拒绝 absolute、drive/UNC、`..`、reserved control path 和 symlink traversal；public response 不带 host path。
- 写入先验证 worker lease、owner epoch、capability revision、fence token、read epoch/base hash、mount policy 和 quota，再进入临时文件、fsync、atomic replace；失败不留下目标或新父目录。
- lease transfer 会 revoke 旧 lease、提升 owner epoch 并失效旧 file state；worker query/permission session 可以轮换，但不改变 task workspace canonical binding。
- snapshot 先冻结写入，再 content-address、落 committed manifest；restore 在替换 live tree 前验证 manifest/blob，corrupt snapshot 将 workspace 标记为 recovery required。
- cleanup 必须有 committed archive snapshot receipt，随后把 live root 移入 quarantine 再删除；删除失败返回 typed cleanup failure。
- restart 不尝试从 hash 反推 fence token；binding 保留但首次 worker acquire 必须产生新 lease/token/epoch。
- Git adapter 只允许固定 read-only 命令和显式参数，不透传任意 subcommand、`-c` config 或环境覆写。

## 7. 有效行数分桶

基于 `git diff --numstat dbff7f19 7881864`：

| 桶 | 新增 | 删除 | 是否计 production |
| --- | ---: | ---: | --- |
| workspace runtime modules（排除 `__init__.py`） | 7,817 | 0 | 是 |
| API/permission/BrowserWorker 真实主路径改造 | 299 | 15 | 是 |
| 保守有效 production | **8,116** | **15** | **是，超过 8,000** |
| package export `__init__.py` | 182 | 0 | 否 |
| tests | 759 | 10 | 否 |
| ledger sync script | 293 | 0 | 否 |
| ledger seed data | 441 | 14 | 否 |
| generated/mock-only/fixture-only/data-as-code | 0 | 0 | 否 |
| vendor/vendor-runtimes/source-pool | 0 | 0 | 否 |

父级 M1-05A 的 `14,000` 行累计下限要到 `M1-S05A-02` 收口时重新核对；当前 slice 不提前宣布父级完成。ledger verifier 的通用 `effective_added` 会包含测试/脚本，因此本自审没有用该数字替代生产分桶。

## 8. 验证记录

在实现提交 `7881864` 上完成：

1. workspace foundation/Git/API + CodeWorker/BrowserWorker + scheduler/subagent 邻接组合：`25 passed, 5 subtests passed`，68.66s。
2. CodeWorker、BrowserWorker、skill update、skill invocation 四条独立进程回归：各 `1 passed`；合计 47.2s。
3. Browser permission/action gate 邻接回归：`31 passed, 7 subtests passed`，17.55s。
4. exact-commit source-free cleanroom（仅 `apps packages scripts tests pyproject.toml`，无 vendor/source repo/cache）：核心与主路径 `23 passed, 5 subtests passed`；Code/Browser permission resume 各 `1 passed`。
5. cleanroom 禁止目录计数 `0`，外部来源仓库路径引用命中 `0`，workspace ledger `--check` 通过。
6. internalization ledger verifier：`ok=true`、`blocker_count=0`、`error_count=0`；552 条为全 seed 的既有 warning，不是当前 slice error。5 条当前 source-role decision 均已落账。
7. `compileall/py_compile`、`git diff --check`：通过。Ruff 未安装，因此没有把 lint 伪报为已执行。
8. `vendor` / `vendor-runtimes` diff、dependency manifest diff：空。

普通 slice 未重复 732 项全仓长期套件；M1-05 数字阶段聚合审查必须在全部 sibling units 完成后的同一最终 target commit 上执行适用全仓测试、完整 cleanroom、全量 ledger/source-to-target audit 和独立批判式复审。

## 9. 实现期间发现并修复

1. Windows backup fsync 首版用只读 handle，无法可靠 flush；改为可写 handle 后 fsync。
2. “读取不存在文件”首版不能形成 create precondition；现在 `absent` 是有效 file identity，仍要求先读后写。
3. dirty-state 首版引用错误 hash helper；统一为 `sha256_file`，并用真实 Git repository 测试覆盖。
4. snapshot identity 比较与 Windows `follow_symlinks=False` 时间恢复存在兼容问题；修正 identity compare 并 fail-safe 处理平台限制。
5. directory quota 首版只核对 byte/file；增加 parent creation 前 directory reservation，失败不创建目录。
6. idempotent create 首版可能用 lease transfer 前的旧 binding projection；现在用 acquisition 后最新 binding。
7. BrowserWorker 首次仍只能读 `file://`/network，导致 task mount 无安全 URI；增加受 permission gate 和 containment 校验的 `workspace:///`。
8. CodeWorker permission test 使用自定义 query session 时找不到创建 task 时的 workspace session；明确 task binding 稳定、query session 可轮换，按 task 获取 workspace。
9. permission HTTP facade 首次仍用 legacy global root，导致新 task-root custody token 401；现在优先读取 server-owned persisted custody binding 的 exact root，兼容尚未迁移的 skill session。
10. 既有 CodeWorker/skill 测试直接断言 legacy global directory；改为通过真实 workspace API 写入/读取，并额外断言 legacy path 未被使用。

## 10. 批判式剩余风险

1. 当前 CodeWorker 通过 manager-issued task root 接入既有 file/shell tool executor；manager API 已强制 read-before-write，但既有 file tool handler 尚未全部改造成 `WorkspaceFileStateStore` gateway。`M1-S05A-02` 必须把 tool-level mutation、artifact/download/temp mount 和 worker handoff 收紧到同一 lease/precondition enforcement，而不能把 foundation 当作最终 sandbox。
2. local backend 是本 slice 唯一 production backend；edge/cloud/container backend capability negotiation、resource placement 和 remote snapshot transfer 尚未实现，也不应在本 slice 宣称完成。
3. workspace store 和 SQLiteStore/EventLog/LocalArtifactStore 之间不是分布式事务。当前 ready barrier、typed recovery state 和 idempotency receipt 可审计 partial failure；05A-02 与 M1-05 聚合应增加逐 commit-point crash matrix。
4. Git dirty/nested-repo 只读行为已覆盖真实 Git；submodule/worktree linked admin dir、超大 repository、case-fold collision 与 Windows reparse-point live matrix 仍需在 05A-02/聚合验证扩展。
5. snapshot 采用本地 content-addressed blob；跨 backend dedupe、GC、retention pressure 和并发 snapshot/cleanup stress 尚未关闭。
6. 本 slice 关闭的是 workspace foundation 工程内化门禁，不关闭赛题 live 任务、2,000 transitions、真实端边云 dispatch、故障恢复、可视化因果轨迹或最终交付门禁。

## 11. 后续强制入口

`M1-S05A-02` 必须在本 owner/contract 上继续完成 integration：不得创建第二套 workspace canonical store，不得回退 legacy global directory，不得让 remote/container backend、scheduler lease 或 artifact owner与当前 workspace lease混为一谈；父级最后一片必须累计核对 `14,000` production 下限、跨 slice 主路径、state custody、source-to-target 完整性和 source-free cleanroom。
