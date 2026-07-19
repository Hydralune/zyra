# M1-05A / M1-05B 四切片聚合审查与修复报告

## 1. 结论

- 审查范围：`M1-S05A-01`、`M1-S05A-02`、`M1-S05B-01`、`M1-S05B-02`。
- 审查依据：`docs/执行单元完成后通用审查任务书.md`、四份 slice 文档、父级 unit、第一阶段权威计划、比赛要求追踪矩阵、source graph 重排裁决及当前 `execution-state.yaml`。
- 当前基线：`6242f33870beaa241145bf54a29c475e678797ba`。
- 审查修复提交：`59a0581f8b5063f904a368c49cf7121f95cd67e1`。
- 最终裁决：**四个 slice 在修复提交上均为 PASS**。
- 阻断项：`0`。
- 进度处理：本次是已完成 slice 的聚合复审与前向修复，不改变第一阶段唯一状态源；下一入口仍为 `M1-S05C-01`。

原四份自审只能证明各自历史提交当时的行为。其后 `M1-R01` 重构了 runtime、permission 与 TypeScript canonical owner，因此本次以当前默认主路径重新做动态可达性、状态托管、并发、断开即失败和 clean source tree 审查，不沿用历史 PASS 作为当前 PASS。

## 2. 审查对象与历史证据

| Slice | 实现提交 | 历史 evidence 提交 | 修复后裁决 |
| --- | --- | --- | --- |
| `M1-S05A-01` | `7881864` | `5e232ca` | PASS |
| `M1-S05A-02` | `afb5654` | `e7b204d` | PASS |
| `M1-S05B-01` | `9dc178d` | `7880aaa` | PASS |
| `M1-S05B-02` | `4497e21` | `c34535a` | PASS |

历史 evidence 文档继续保留为历史事实；本报告记录当前代码上的增量对抗审查、修复和复验结果，不倒写原提交内容。

## 3. 发现与修复

### 3.1 WorkspaceManager：M1-S05A-01 / M1-S05A-02

| ID | 严重度 | 审查发现 | 修复与动态效果 |
| --- | --- | --- | --- |
| A-01 | blocker | 本地 backend 的授权检查与 owner epoch 轮换不共用协调锁，旧 worker 可在检查后写入。 | `LocalWorkspaceBackend` 的授权、epoch 与写操作进入同一协调边界；旧 epoch 写入确定性失败。 |
| A-02 | blocker | transaction 删除没有完整 read precondition，提交前文件被替换仍可能删除新内容。 | 删除与写入统一记录并校验 read-set/base hash；陈旧 transaction 不能提交。 |
| A-03 | blocker | full-snapshot rollback 会删除 transaction 期间由其它 owner 新建的无关文件。 | rollback 改为 write-set restore，并保留/报告并发冲突，不再用全目录回卷覆盖别人的提交。 |
| A-04 | high | `create_for_task` 的“查询后创建”不是原子的，同一 task 可产生多个 active workspace。 | 增加 task 级互斥和持久化唯一性检查。 |
| A-05 | high | handoff 的检查与消费分离，并发 consumer 可重复消费。 | handoff claim 在 task/workspace 锁内原子完成，消费保持 exactly-once。 |
| A-06 | high | idempotency key 未绑定 task/请求语义，可能跨 task 返回错误 mount 或留下 orphan mount。 | key 绑定 logical fingerprint；冲突在创建 filesystem state 前失败。 |
| A-07 | high | rebind 只迁移 task mount，artifact/download/temp mount 与相关引用仍指向旧 owner。 | snapshot/restore/rebind 覆盖全部 mount kind 和 migration reference，并校验新 epoch。 |
| A-08 | high | API runtime event spine 的全局 SQLite 连接无法可靠 reset，测试与重启可继承残留状态。 | 增加显式 close/reset，并接入 API lifecycle；干净状态不再依赖先前 SQLite。 |

修复后唯一 workspace state owner 仍是 `WorkspaceManagerRuntime` / `WorkspaceBindingStore`；所有文件变更通过 `WorkspaceEditPort`、transaction、snapshot 和 owner epoch fencing 完成。没有把 workspace 状态转移给 gateway、worker 或外部来源 runtime。

### 3.2 SandboxGateway：M1-S05B-01 / M1-S05B-02

| ID | 严重度 | 审查发现 | 修复与动态效果 |
| --- | --- | --- | --- |
| B-01 | blocker | productized Browser live run 可绕过 gateway plan preflight。 | `BrowserWorkerRuntime` 强制 preflight；required gateway 缺失或拒绝时 fail closed；执行前再做 owner-epoch fence。 |
| B-02 | blocker | Browser download 和部分 artifact 可直接写 `LocalArtifactStore`。 | `BrowserActionArtifactPort` 先经 `BrowserGatewayBoundary` 做 quarantine/provenance/commit，再发布 artifact ref。 |
| B-03 | blocker | 文件 artifact 在 permission 与最终 commit 之间缺少 workspace/owner epoch 复核。 | `FileArtifactRequest` 携带 expected workspace/epoch，artifact port 在最后提交点向 live manager 复核。 |
| B-04 | high | Browser upload 只授权原始 host path；授权后内容替换不能被发现。 | preflight 通过 `WorkspaceEditPort` 导出授权字节并绑定 content digest；执行 fence 再导出并比较。 |
| B-05 | blocker | R01 后的 CodeWorker TypeScript host bridge 仍直接 `Popen`，绕过 gateway process lifecycle。 | 改由 `GatewayHostProcessRuntime.start_interactive` 启动；使用结构化 argv、环境 allowlist、process-group/tree 终止和 finally pipe cleanup。 |
| B-06 | high | bounded command output 被截断后没有完整 spill artifact。 | collector 保留经脱敏的 overflow；router 写入 `command-output/<tool_call_id>.log` 并返回真实 artifact ref；超限稳定映射 `OUTPUT_LIMIT`。 |
| B-07 | high | factory 实际只有 local backend，未形成可注入的 sandbox/backend contract。 | 新增 connector contract 以及 connector/docker/simulated backend；local 保持默认，远端实现不取得第二 canonical owner。 |
| B-08 | high | 05B 描述符仍指向 R01 前的 permission owner。 | Python/TypeScript custody 和 receipt 统一声明 `typescript.PermissionCoordinator`；旧 adapter 类名仅作协议兼容。 |
| B-09 | medium | source-custody 缺少来源语言、迁移模式、符号、主路径和行为测试落位。 | 扩展 custody entry，并修正 AgentScope 对照测试目标。 |
| B-10 | medium | submission verifier 会扫描 `.tmp`、`node_modules`、`dist` 和审计 provenance 数据，产生误报并显著拖慢。 | 排除构建/缓存与明确的 audit-only provenance；正式源码边界仍完整扫描。 |
| B-11 | high | host/backend 脱敏没有使用 runtime 注入的 known-secret redactor。 | retained output 与 overflow 统一使用实例 redactor，secret 不进入 terminal/event/artifact。 |
| B-12 | high | gateway-owned artifact 写入推进 epoch 后，Browser plan tracker 未同步，后续 action 会自我 fencing。 | 每次 gateway commit 后推进 plan epoch，仍拒绝真正的外部 stale writer。 |

修复后唯一 gateway owner 是 `SandboxGatewayRuntime`，唯一 permission owner 是 `typescript.PermissionCoordinator`。Workspace 文件状态仍归 05A；gateway 只持有 policy/lifecycle/receipt/backend execution 状态。TypeScript query/session 状态仍归 `TypeScriptClaudeQueryEngine`。artifact 只有在 gateway/WorkspaceManager 提交成功后才由 `LocalArtifactStore` 发布。

## 4. Source-to-target 与去重裁决

### 4.1 M1-05A

- Primary：OpenHands workspace lifecycle。
- Supplementary：AgentScope workspace backend；oh-my-pi PAL/worktree/isolation、Hashline fencing。
- Claude、opencode、OpenClaw、Hermes：只保留接缝、对照或边界参考，不取得 workspace owner。
- 目标落位：`packages/workspace/zyra_workspace/**`、API workspace route、worker/gateway 的 `WorkspaceEditPort` 接缝。

### 4.2 M1-05B

- Primary：OpenHands sandbox lifecycle、action execution、backend failure 与 artifact transfer。
- Supplementary：OpenClaw gateway policy/approval/control/remote receipt；oh-my-pi structured argv、Hashline、credential isolation、bounded host/RPC result。
- claude-code-best：conformance-only；其既有 QueryEngine/permission owner 由 R01 产品化结果承担，不在 05B 复制控制流。
- AgentScope、Hermes、opencode：reference-only。
- 目标落位：`packages/runtime/zyra_runtime/sandbox_gateway/**`、`packages/runtime/sandbox-gateway-control/**`、CodeWorker/BrowserWorker 的真实执行入口。

本次修复没有改变上述 source role，也没有新增第二套 WorkspaceManager、SandboxGateway、permission reducer 或 runtime session owner，因此无需回写根目录分析索引与总计划。

## 5. 主路径、状态托管与断开即失败

| 能力 | 当前真实入口 | Canonical state owner | 断开后的可观察结果 |
| --- | --- | --- | --- |
| Workspace lifecycle / binding | API、worker workspace factory、gateway edit port | `WorkspaceManagerRuntime` / `WorkspaceBindingStore` | 默认 worker 无法取得合法 binding；旧 epoch 写入失败 |
| Transaction / snapshot / restore / rebind | `WorkspaceEditPort` 与 handoff/rebind flow | Workspace transaction/snapshot stores | 删除、rollback、handoff、rebind 行为失败或冲突被显式暴露 |
| Tool / host process | CodeWorker ToolExecutor、TypeScript host bridge | `SandboxGatewayRuntime` | required 模式返回 gateway unavailable，不回退到 raw process |
| Browser file/network/upload/download | BrowserWorker plan + action artifact port | gateway policy/receipt；文件状态归 WorkspaceManager | preflight、epoch、digest 或 artifact commit 任一缺失均 fail closed |
| Permission | E02 TypeScript API port | `typescript.PermissionCoordinator` | ask/deny/replay/owner mismatch 在副作用前阻断 |
| Artifact | gateway artifact port -> `LocalArtifactStore` | gateway commit receipt + artifact store | 无 gateway commit 不产生可发布 artifact ref |

新增测试不是 import、manifest 或 fixture presence 检查：它们实际制造 epoch 轮换、并发 create/handoff、陈旧 transaction、rollback 冲突、gateway 缺失、上传内容替换、真实子进程 output overflow、artifact 写入和 backend connector 调用。断开对应模块会使这些行为失败或改变。

## 6. 有效行数分桶

历史 slice 归属按各自实现提交计算，避免把后续 R01 或本次修复重复归入原 slice：

| Slice | 历史 raw additions | 保守 production physical lines | Slice 下限 | 结果 |
| --- | ---: | ---: | ---: | --- |
| `M1-S05A-01` | 8,739 | 8,116 | 8,000 | PASS |
| `M1-S05A-02` | 8,393 | 7,926 | 6,000 | PASS |
| M1-05A 合计 | 17,132 | 16,042 | 14,000 | PASS |
| `M1-S05B-01` | 13,838 | 12,034 | 8,000 | PASS |
| `M1-S05B-02` | 7,924 | 7,085 | 6,000 | PASS |
| M1-05B 合计 | 21,762 | 19,119 | 14,000 | PASS |

上述 production 数排除了 test、docs、generated、data、fixture/mock-only、vendor/source-pool、ledger/manifest/source map 与薄 adapter。历史归属源码在当前修复提交上仍存在并可从默认主路径触达。

本次 review-fix 的 numstat 独立分桶为：production `+1,323/-138`、test `+495/-2`、产品边界审计脚本 `+25/-0`。这些修复行不用于补足历史 slice 下限，也没有 vendor/source-pool/generated/data-as-code 增量。

## 7. 验证证据

### 7.1 修复工作树

核心聚合命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_workspace_manager_foundation.py tests\unit\test_workspace_manager_integration.py tests\unit\test_sandbox_gateway_policy.py tests\unit\test_sandbox_gateway_state.py tests\unit\test_sandbox_gateway_artifacts.py tests\unit\test_sandbox_gateway_integration_policy.py tests\integration\test_sandbox_gateway_runtime.py tests\integration\test_sandbox_gateway_worker_integration.py tests\integration\test_workspace_manager_api.py tests\integration\test_browser_session_productization_integration.py -q
.\.venv\Scripts\python.exe -m pytest tests\integration\test_e01_typescript_runtime_cutover.py -q
node --experimental-strip-types --test packages\runtime\sandbox-gateway-control\test\*.test.ts
.\.venv\Scripts\python.exe scripts\verify_submission_boundary.py
.\.venv\Scripts\python.exe scripts\sync_workspace_manager_source_ledger.py --check
git diff --check
```

- WorkspaceManager foundation/integration：`26 passed, 5 subtests passed`。
- 05A/05B 直接受影响组合：`73 passed, 9 subtests passed`，耗时 `90.99s`。
- Browser permission + productized path：分别通过；productized 单组 `11 passed`。
- R01 TypeScript runtime cutover：`23 passed`，耗时 `75.82s`。
- `sandbox-gateway-control` TypeScript：`21/21 passed`。
- Workspace API：`3/3 passed`。
- `python -m compileall`：通过。
- WorkspaceManager source-ledger check：`8 decisions`，通过。
- submission boundary verifier：通过。
- `git diff --check`：通过。

### 7.2 干净源码目录

从 review-fix commit `59a0581f8b5063f904a368c49cf7121f95cd67e1` 使用 `git archive` 创建无 `.git`、无缓存、无 SQLite、无 artifact 残留的临时源码树。源码树复用项目锁定的 `.venv` 与 Bun 1.2.15 工具链，不复用原工作树源码或状态：

```powershell
$env:ZYRA_BUN_EXECUTABLE='G:\agent-zoo\zyra\node_modules\bun\bin\bun.exe'
G:\agent-zoo\zyra\.venv\Scripts\python.exe -m pytest tests\integration\test_e01_typescript_runtime_cutover.py -q --basetemp=G:\agent-zoo\zyra\.tmp\pytest-m1-review-e01-59a0581
G:\agent-zoo\zyra\.venv\Scripts\python.exe -m pytest tests\integration\test_browser_worker_permission_gate.py -q --basetemp=G:\agent-zoo\zyra\.tmp\pytest-m1-review-browser2-59a0581
node --experimental-strip-types --test test\*.test.ts
G:\agent-zoo\zyra\.venv\Scripts\python.exe scripts\verify_submission_boundary.py
```

- 相关 Python 聚合：`73 passed, 9 subtests passed`，耗时 `91.59s`。
- R01 TypeScript runtime cutover：`23 passed`，耗时 `74.14s`。
- Browser permission gate：`17 passed, 2 subtests passed`，耗时 `18.57s`。
- `sandbox-gateway-control`：`21/21 passed`。
- submission boundary verifier：`Submission boundary verification passed`。

第一次 clean-tree E01 运行因 pytest 无权扫描用户级 `%TEMP%/pytest-of-libin`，23 项均在 fixture 建立前报 `WinError 5`；改用工作区内显式 `--basetemp` 后全数通过。第一次 Browser permission 运行未设置 clean-tree 外部的锁定 Bun 路径，9 项均在 E02 port 启动前失败；设置 `ZYRA_BUN_EXECUTABLE` 后全数通过。二者均为可复现的测试宿主配置诊断，不是业务断言失败；临时源码树和 basetemp 已删除。

## 8. 反伪内化与交付边界

- 当前默认路径不执行 `../OpenHands`、`../claude-code-best`、`../browser-use`、`../opencode`、`../AgentScope` 或其它根目录来源仓库。
- 没有 `npm link`、editable source path、外部 Docker build context、source-pool/runtime-sources/vendor-runtimes 运行依赖。
- Docker/remote 本轮验收的是可注入 connector contract、owner/backend receipt 和失败边界；真实 edge/cloud dispatch 仍由 `REQ-EDGE-01` 的 M1-05D/M1-07/M3 冻结证据负责，本报告不虚报为已完成赛题证据。
- 赛题 A 门禁状态不因本次工程审查提升：`REQ-EDGE-01` 仍按比赛要求追踪矩阵保持 planned；本次关闭的是 05A/05B 的工程内化 B 门禁缺陷。
- 根目录 `docs/**` 与 `execution-state.yaml` 未修改；`G:\agent-zoo` 不是 Git 仓库。
- 审查开始前已存在的 `zyra/AGENTS.md` 用户修改未触碰、未暂存、未提交。

## 9. 未运行项与后续层级

本次按高风险匹配原则执行了 WorkspaceManager、SandboxGateway、Browser、permission、artifact、API、R01 TypeScript host 和 clean source tree 的直接/相邻回归；没有无差别运行整个 Zyra 全仓测试。与 05A/05B diff 无直接关系的 memory、scheduler、UI 和后续 M1-05C+ 长耗时套件仍按任务书留在 M1-05 全 sibling 聚合或里程碑退出层。当前已修改的 canonical owner 接缝和默认执行路径均已覆盖，没有用“后续全仓”掩盖已知失败。

## 10. 最终裁决

四个 slice 的历史规模、来源职责和目标能力成立，但原实现存在会破坏 owner fencing、exactly-once、默认 gateway custody、artifact provenance 与 process lifecycle 的真实缺陷。`59a0581` 已将这些缺陷修复到 Zyra-owned 模块，相关行为、失败路径、断开即失败、相邻 R01 回归和干净源码目录复验全部通过。

因此本次聚合审查结论为：**M1-S05A-01、M1-S05A-02、M1-S05B-01、M1-S05B-02 均 PASS；允许保持完成状态，下一入口继续为 M1-S05C-01。**
