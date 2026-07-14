# M1-S05B-01 Sandbox Gateway Foundation 增量批判式自审

## 1. 审查结论

- Slice：M1-S05B-01
- 审查对象：sandbox lifecycle、gateway command policy、permission relay、process budget/tree、file/archive/provenance/quarantine、workspace patch/artifact port、TypeScript control supplement
- 结论：PASS
- 父级 M1-05B：NOT COMPLETE
- 下一入口：slice-05b-02-sandbox-gateway-integration.md

本切片建立一个 Zyra-owned SandboxGatewayRuntime，没有按来源仓库复制多套 gateway。OpenHands 继续作为 Python sandbox lifecycle 的 primary source；OpenClaw 的 command/approval/timeout/session queue 机制保留在 TypeScript supplement；Oh My Pi 的 Hashline、credential-free envelope、dirty-baseline isolation 和 patch preservation 按原语言与 Zyra Python workspace owner 分工落位。

## 2. 独立工作流完成情况

1. 来源裁决：完成。
2. Zyra schema/state/port：完成。
3. Python primary lifecycle/backend：完成。
4. TypeScript supplementary command/approval/Hashline/control：完成。
5. Permission 与 workspace 既有 owner 接入：完成。
6. 行为、失败、可达性、禁用即失败验证：完成。
7. 增量路径、依赖、有效行数分桶：完成。
8. 父级累计收口：未执行，留给 M1-S05B-02。

## 3. Source-to-target 裁决

| Source | Role | Source mechanisms | Zyra target | Language custody | Production owner |
|---|---|---|---|---|---|
| OpenHands | primary | lifecycle、ready wait、process state、cleanup/recovery、archive/file store event boundary | zyra_runtime.sandbox_gateway.lifecycle/backends/state_store/archive_policy/event_port | Python 保留 | SandboxGatewayRuntime |
| OpenClaw | supplementary | allow/ask/deny、unknown fail-closed、exact approval、allow-once、timeout、session queue | sandbox-gateway-control/src/command-policy.ts/approval-ledger.ts/timeout.ts/session-queue.ts | TypeScript 保留 | 无第二 owner |
| Oh My Pi | supplementary | dirty baseline、patch preservation、preflight、Hashline、credential-free、redaction、process cleanup | Python isolation/patch_port/credential_relay/redaction + TypeScript hashline/credential-envelope | 按来源机制保留 | WorkspaceManagerRuntime 仍是 workspace owner |
| AgentScope | conformance_only | workspace lifecycle comparison | tests | Python | none |
| Hermes | reference_only | deployment/sandbox negative comparison | 本审查 | docs | none |
| opencode | conformance_only | typed protocol comparison | TypeScript contract tests | TypeScript | none |
| claude-code-best | reference_only | permission identity compatibility | existing ToolPermissionRuntime port | 不新增迁移 | ToolPermissionRuntime |

可执行 custody manifest：

~~~powershell
$env:PYTHONPATH='packages\runtime;packages\workspace;packages\core'
.\.venv\Scripts\python.exe -m zyra_runtime.sandbox_gateway source-custody
~~~

结果：PASS。Manifest 明确：

- gateway_count=1
- permission_owner=ToolPermissionRuntime
- workspace_owner=WorkspaceManagerRuntime
- artifact_write_owner=WorkspaceEditPort
- vendor_runtime_required=false
- cross_language_bulk_rewrite=false
- typescript_supplement_is_second_gateway=false

## 4. Zyra-owned 模块边界

### 4.1 Python primary

- models.py：command envelope、budget、permission binding、session/lease/event、artifact/provenance、patch/receipt schema。
- state_store.py：跨进程锁、原子 JSON replace、generation CAS、permission single-use durable binding、event sequence、receipt/quarantine/lease custody。
- lifecycle.py：OpenHands-derived explicit lifecycle、ready wait、lease/fence、interrupted recovery、cleanup。
- backends.py：结构化 executable/argv/cwd/env、shell=False、隔离 root、timeout/output/cancel/process-tree cleanup。
- command_policy.py：统一 evidence aggregation，调用既有 shell analyzer，不取得最终 permission authority。
- permission_relay.py：使用既有 canonical request fingerprint，精确绑定 ToolPermissionRuntime grant，持久化 digest 而非 secret/grant capability。
- artifact_port.py / patch_port.py：所有 workspace write/delete 经 WorkspaceEditPort。
- file_policy.py / archive_policy.py / provenance.py / quarantine.py：content type、active content、archive traversal/bomb、untrusted provenance/control-write、quarantine。
- isolation.py：dirty baseline、symlink rejection、content digest delta、failed patch preservation。
- redaction.py / credential_relay.py：secret-free event/artifact projection 和 audience-bound ephemeral credential。
- runtime.py：唯一 command/file gateway orchestration。

### 4.2 TypeScript supplement

- contracts.ts / canonical.ts：typed command、approval、Hashline、credential、RPC contract。
- command-policy.ts / git-policy.ts：OpenClaw-derived fail-closed typed command evidence。
- approval-ledger.ts：exact request/command/grant binding 和 CAS allow-once；final authority 固定为 ToolPermissionRuntime。
- hashline.ts：Oh My Pi-derived snapshot/line hash/range/preflight binding。
- credential-envelope.ts：secret-free public envelope、audience/scope/command binding、single-use ephemeral secret。
- session-queue.ts / timeout.ts：per-session serialization、abort/deadline cleanup。
- rpc.ts：固定 method registry；无 dynamic import、无 process spawn、无 canonical state ownership。

## 5. 严格内化与反伪内化回答

### 5.1 外部机制被拆成哪些 Zyra 模块

来源机制没有保留上游目录结构。Lifecycle、policy、permission relay、state store、process budget、artifact、archive、provenance、isolation、patch、credential 和 event 被拆入独立 Zyra runtime boundary，并统一使用 Zyra session/run/task/tool-use/workspace/event/receipt identity。

### 5.2 哪些机制被裁剪或改造

- OpenHands app-server/API/file-store 外壳未迁移，只保留 lifecycle、ready、cleanup、archive/atomic storage 机制。
- OpenClaw gateway 未作为第二 daemon/port/service 迁移，只保留 TypeScript typed policy/approval/queue/timeout 机制。
- Oh My Pi worktree owner 被移除，patch commit 改由 Zyra WorkspaceEditPort。
- Credential material 不进入 command envelope、state JSON、event 或 receipt。
- TypeScript RPC 只是进程内 typed service contract，不启动 Node sidecar，也不取得 canonical state。

### 5.3 Zyra 数据结构、状态与错误边界

- Canonical gateway state：GatewayStateStore
- Session lifecycle owner：SandboxGatewayRuntime
- Permission decision/grant owner：ToolPermissionRuntime
- Workspace binding/epoch/fence owner：WorkspaceManagerRuntime
- Workspace mutation owner：WorkspaceEditPort
- Gateway artifact transfer owner：GatewayFileArtifactPort
- Structured failures：GatewayErrorCode / SandboxGatewayError
- Events：canonical redacted GatewayEvent
- Receipts：content/permission/policy-bound CommandReceipt / PatchReceipt

### 5.4 断开即失败

- SandboxGatewayConfig.enabled=false：session creation fails closed；不会启动 backend。
- GatewayFileArtifactPort.enabled=false：artifact commit fails；不会 raw-write。
- GatewayPatchPort.enabled=false：patch apply fails；不会 raw-write。
- Permission port 不可用或 grant mismatch：command 在 backend start 前失败。
- Workspace epoch/fence stale：patch preflight/commit 失败。
- Archive traversal、untrusted control write、active content：拒绝或 quarantine。

### 5.5 动态可达性与语义效果

真实 integration test 执行：

~~~text
GatewayCommandEnvelope
  -> StructuredCommandPolicy
  -> GatewayPermissionRelay
  -> SandboxLifecycle lease/fence
  -> LocalProcessSandboxBackend
  -> IsolationWorkspace delta
  -> GatewayPatchPort
  -> WorkspaceEditPort transaction
  -> gateway_patch_committed event
  -> CommandReceipt
~~~

子进程真实创建 generated.txt；文件通过 workspace transaction 合并，存在 transaction id 和 owner epoch rotation。禁用 gateway 时不会出现 backend session root。

## 6. 安全与运行责任审查

### 6.1 Command

- executable、argv、cwd、environment digest 分字段绑定。
- shell=False。
- cmd/PowerShell/bash nested/dynamic/encoded/substitution/redirection 分析。
- Redirection 因绕过 WorkspaceEditPort 被 deny。
- Git read-only 与 destructive/network/mutating 分流。
- Package install/publish/auth 被 deny；script execution 为 ask。
- Unknown executable fail-to-ask；sealed autonomous mode 将 ask 确定性转 deny。
- Caller permission_bypass / auto_approve 没有 authority。

### 6.2 Permission

- Request fingerprint 使用既有 zyra_runtime.permission.canonical。
- Grant capability 不持久化，只保存 digest。
- Command mutation 在 authority consume 前拒绝。
- Existing ToolPermissionRuntime consume 后，gateway binding CAS single-consume。
- Restart 后 replay 仍被 durable binding 拒绝。

### 6.3 Process

- Per-command timeout、stdout/stderr/combined output budgets。
- Cancellation token + process-group/tree termination。
- Backend root 与 workspace root 分离。
- Base environment allowlist，不继承 credential-like keys。
- Terminal output 在 event/receipt 前 redaction。

### 6.4 File/archive/provenance

- Canonical logical path 拒绝 traversal、UNC、drive、device paths。
- Workspace owner epoch 在 patch prepare 和 commit 两次验证。
- Archive 在内存中遍历，禁止 symlink/device/traversal/encrypted，限制 entry/expanded bytes/ratio。
- Web/MCP/browser/download provenance 不能修改 .codex、policy、profile、credential/config targets。
- Executable/unknown binary/archive 未 release 时 quarantine。
- Quarantine 位于 gateway state root，不暴露为 workspace file。

## 7. 验证矩阵

### 7.1 当前 slice Python

~~~powershell
.\.venv\Scripts\python.exe -m unittest tests.unit.test_sandbox_gateway_policy tests.unit.test_sandbox_gateway_state tests.unit.test_sandbox_gateway_artifacts tests.integration.test_sandbox_gateway_runtime
~~~

结果：20 passed。

覆盖：

- command/git/shell/network/sealed policy
- path/provenance/redaction
- durable lifecycle/CAS/lease/fence/restart
- exact permission binding/single consumption/replay
- per-session serialization
- real WorkspaceEditPort artifact/patch
- archive traversal
- untrusted quarantine
- stale epoch
- real subprocess + transaction merge + gateway event
- disabled gateway

### 7.2 TypeScript supplement

~~~powershell
node --experimental-strip-types --test packages/runtime/sandbox-gateway-control/test/contracts.test.ts packages/runtime/sandbox-gateway-control/test/policy.test.ts packages/runtime/sandbox-gateway-control/test/approval.test.ts packages/runtime/sandbox-gateway-control/test/hashline-control.test.ts
~~~

结果：18 passed。

覆盖：

- typed canonical contract/path
- Git/shell/network/sealed policy
- exact approval + replay/mutation/expiry
- Hashline snapshot/range/overlap
- credential audience/scope/single-use
- nested redaction
- per-session async queue
- deadline propagation
- fixed typed RPC reachability

### 7.3 相邻回归

~~~powershell
.\.venv\Scripts\python.exe -m unittest tests.unit.test_permission_shell_extensions
~~~

结果：15 passed。

~~~powershell
.\.venv\Scripts\python.exe -m unittest tests.unit.test_workspace_manager_integration
~~~

结果：8 passed。

~~~powershell
.\.venv\Scripts\python.exe -m unittest tests.integration.test_workspace_worker_gateway
~~~

结果：2 passed, 1 failed。

失败项：

~~~text
test_code_worker_mutations_use_workspace_transactions_and_missing_gateway_fails_closed
~~~

实际行为：上一切片已切换的 TypeScript permission owner 对 file_write 返回 ask 和 durable pending approval；旧测试仍期待默认模式自动执行写入。失败发生在现有 packages/workers/zyra_workers/typescript_claude_runtime.py / TypeScript permission path，本切片未修改该路径，新 gateway 尚未接入 CodeWorker 默认主路径。

裁决：

- 不是本 diff 引入的行为回归。
- 不通过回改已完成 R01 permission owner 或伪造 auto-approval 使旧断言变绿。
- M1-S05B-02 接入时应把该测试改为审批 continuation 驱动，或显式使用 sealed policy 预期 deny/recovery。
- 该项不能作为当前 slice 绕过自身测试的理由；当前 slice 自有与直接相邻 workspace/permission 测试均已通过。

## 8. 增量依赖与路径审查

限定扫描：

- 根目录来源仓库相对路径：0
- vendor-runtimes / runtime-sources / source-pool 依赖：0
- npm link / pip editable：0
- dynamic import / createRequire：0
- shell=True / shell: true：0
- TypeScript execSync / spawnSync：0

Artifact/patch port direct-write 扫描只命中：

- Protocol 中声明的 write_bytes
- self.workspace_edit_port.write_bytes
- PurePosixPath 文件名解析

没有 Path.open/write_text/write_bytes/unlink/rmtree 直接写 workspace。

## 9. 有效行数分桶

| Bucket | Files | Lines | 是否计入 8,000 生产门禁 |
|---|---:|---:|---|
| Python production internalization | 30 | 8,961 | 是 |
| TypeScript production supplement | 12 | 3,440 | 是 |
| Production total | 42 | 12,401 | 是 |
| Python tests | 4 | 793 | 否 |
| TypeScript tests | 4 | 458 | 否 |
| Test total | 8 | 1,251 | 否 |
| Generated | 0 | 0 | 否 |
| Data-as-code | 0 | 0 | 否 |
| Docs | 1 | 本文件 | 否 |
| Vendor/source-pool | 0 | 0 | 否 |
| Standalone thin adapter-only files | 0 | 0 | 否 |
| Mock/fixture-only production files | 0 | 0 | 否 |

判定：12,401 >= 8,000，通过 slice 生产有效行数门禁。

注：callback port class 位于 substantive permission/credential production modules 内，只负责连接既有 owner；没有单独以薄 adapter 文件抵扣行数。生产统计没有包含 tests、review、manifest data 或 vendor。

## 10. 对抗问题

### 10.1 是否出现第二个 gateway

否。TypeScript package descriptor 固定：

- role=supplementary
- canonicalGatewayOwner=SandboxGatewayRuntime
- finalAuthority=ToolPermissionRuntime

RPC 不持久化 canonical session/permission/workspace state，不启动 sidecar。

### 10.2 是否把 TypeScript 强制改写成 Python

否。OpenClaw command/approval/timeout/queue 和 Oh My Pi Hashline/credential envelope 保留 TypeScript；OpenHands Python lifecycle 保留 Python。Python 只承担 Zyra canonical orchestration 和既有 Python workspace/permission contract relay。

### 10.3 是否以 ledger/manifest 替代行为

否。Custody manifest 只作来源证据；完成判定由 real subprocess、permission consume、workspace transaction、archive rejection、quarantine、restart/replay 和 disable tests 支撑。

### 10.4 是否依赖上游仓库或 vendor

否。运行时 import/execute 不引用 ../OpenHands、../openclaw、../oh-my-pi 或任何 vendor/source pool。

### 10.5 删除新模块是否改变真实行为

是：

- 删除 runtime/backend：真实 subprocess integration 无法启动。
- 删除 permission relay：grant issue/consume 测试失败。
- 删除 patch/artifact port：workspace merge 与 quarantine 测试失败。
- 删除 lifecycle/state store：restart/lease/CAS 测试失败。
- 删除 TypeScript policy/approval/Hashline：18 项 native tests 失败，RPC descriptor 不可达。

## 11. 未在本 slice 完成的工作

- CodeWorker/BrowserWorker/MCP/remote worker 默认主路径接入。
- 既有 worker permission continuation 与新 command envelope 的统一 handoff。
- Batch workspace transaction 的更强原子 commit/rollback。
- Edge/container/cloud backend adapters。
- 网络 namespace、resource scheduler、05C/05D/07C downstream integration。
- 父级 M1-05B 累计行数、主路径和 cleanroom 收口。

这些属于 M1-S05B-02，本切片不冒充父级完成。
