# M1-R01 Execution-02 Permission/MCP/Skill Cutover Review

日期：2026-07-14  
基线：`4df4ba614f18d2fb211132027f27311f9ee7d305`  
结论：PASS。代码、相关行为测试、失败路径、source-free cleanroom、custody audit 和 evidence commit 内容齐备；`execution-03-agenttool-control-final-cutover.md` 为唯一下一入口。

## 1. 最终 owner 裁决

| Capability | Canonical owner after execution-02 | Python retained responsibility | Forbidden fallback |
| --- | --- | --- | --- |
| permission policy、allow/deny/ask、request binding、recovery alternative | TypeScript | durable mode/rules/approval/decision/grant、CAS、event projection | `permission_runtime.guard()` 不得出现在 TypeScript host 路径 |
| MCP stdio/HTTP transport、initialize、auth、catalog、call、resource、prompt、elicitation、list-change、reconnect | TypeScript | session checkpoint/artifact/event projection | Python MCP handler/in-process peer 不得执行 CodeWorker canonical MCP call |
| SkillTool、Markdown frontmatter/resource、command/plugin discovery/invocation | TypeScript | checkpoint/artifact/event projection | `SkillToolProjectionRuntime.open_for_worker` 只允许非 TypeScript compatibility 路径 |
| builtin file/shell/browser side effect | TypeScript permission gate，Python ToolExecutor side effect | workspace gateway 与真实副作用 | Python 不得重新做 permission policy decision |

## 2. Per-file disposition

| Path or group | Disposition | Evidence |
| --- | --- | --- |
| `packages/integrations/claude-mcp/**` | canonical TypeScript production module | real stdio child、real local HTTP JSON-RPC、auth state、catalog invalidation、reconnect、tool/resource/prompt behavior |
| `packages/runtime/claude-runtime/src/permission/**` | canonical TypeScript permission policy | exact canonical digest、full identity、mode/rule priority、interactive ASK 与 sealed denial |
| `packages/runtime/claude-runtime/src/skills/**` | canonical TypeScript Skill/Command runtime | safe roots、frontmatter、resource boundary、precedence、digest、command/plugin invocation |
| `packages/runtime/claude-runtime/src/capabilities.ts`、`capability-host.ts` | canonical capability registry/execution owner | TypeScript local execution；Python receipt only after TypeScript decision；execution 后 settlement |
| `packages/runtime/zyra_runtime/permission/runtime.py` | durable-state adapter, not policy owner | validates owner/full binding/policy digest；persists decision/grant/continuation；projects recovery and causal events |
| `packages/workers/zyra_workers/typescript_claude_runtime.py` | adapter-only narrow host | JSONL protocol、durable commit、builtin side effect、settlement、checkpoint；不得计作 TypeScript 内化源码 |
| `packages/workers/zyra_workers/code_worker_runtime.py` | routing cutover | TypeScript owner path skips Python MCP and Skill projections；metadata records `python_skill_projection_used=false` |
| legacy Python MCP/Skill modules | compatibility/conformance pending physical retirement | not dynamically reachable from default TypeScript CodeWorker；execution-03 must not promote them back to canonical owner |
| `apps/api/zyra_api/main.py` | durable control-plane binding fix | MCP mutation facade resolves the same task/session workspace root used by custody creation |
| `scripts/verify_typescript_runtime_custody.py` | audit-only, not production line credit | fails on missing TS owners, Python policy fallback, missing settlement, missing default-owner guards, forbidden source paths |
| `vendor/**`、`vendor-runtimes/**`、source-pool/runtime-sources | rejected | staged diff has zero additions and cleanroom does not copy them |

## 3. 对抗审查发现与修复

1. Permission continuation 曾在 Python 提交 grant 后、TypeScript MCP/Skill 尚未执行前被标记完成。已改为 `tool.settle/tool.settle.result` 两阶段协议；能力成功或失败后才结束 continuation，未知、重复和缺失 settlement 失败关闭。
2. MCP elicitation 曾用带 `method` 的伪 JSON-RPC response 返回。已改为标准 `result/error` response，transport 接受完整 JSON-RPC message。
3. TypeScript 同批 deny 的后三个 decision 曾因 Python 把 denial total 当作 policy revision 而被误判 stale。已把 denial counter 保持为 durable execution state，不再改变 policy digest；真实 mode/rule 语义变化仍使旧 decision 失效。
4. sealed deny 的 recovery event 与三次拒绝熔断在切换后缺失。已由 TypeScript提供 recovery alternatives，Python只持久化 denial counter 和事件；第三次 deny 设置 `permission_abort_loop=true`，TypeScript loop 无视 `continue_on_error` 并停止。
5. permission durable component 断开曾只返回笼统 `typescript_runtime_process_failed`。已在启动子进程前失败为 `tool_loop_foundation_disabled`，记录具体 component，且不发生副作用。
6. issued grant 到 consumed grant 的 cause link 曾为空。已把 issued event id 写入 grant context，消费事件显式引用它。
7. MCP API mutation 使用全局 workspace 重验 task-bound custody，合法 bearer 被拒绝。已按 `task_id/session_id` 解析既有 custody workspace；没有放宽 token 或一次性 grant 校验。
8. 旧测试把 `tool_call_started` 误当成副作用已经发生，并要求 Python MCP/permission snapshot。测试已改为验证调度事件可先出现，但 TypeScript decision、grant consumption 必须早于真实 tool result，且 Python projection 不得回到主路径。

## 4. 行为与失败路径证据

- TypeScript runtime/MCP/permission/skill：`20 passed`。覆盖 protocol ordering/version、multi-turn、budget、compact/restore、invalid schema、abort/model error、permission exact binding、sealed/headless、stdio、HTTP bearer handle、needs-auth、list-change、reconnect、tools/resources/prompts、Skill resource escape。
- Permission continuation：`17 passed`。覆盖 ASK park/resume、exact approval、lease/claim、branch isolation、tampered replay、lost receipt、tombstone、durable mode reconciliation。
- MCP/Skill integration：`18 passed, 2 subtests passed`。真实 stdio peer 进入 CodeWorker，Python MCP/Skill owner 不进入默认调用链。
- MCP API control/restore：`1 passed`。Python legacy in-process peer 仅验证控制面；CodeWorker checkpoint 明确无 Python `mcp_runtime` owner。
- Permission unit/control plane：`87 passed, 279 subtests passed`。
- CodeWorker permission foundation：`23 passed, 7 subtests passed`。覆盖 recovery、denial limit、component disconnect、raw approval ignore、grant causality。
- Custody audit：`typescript runtime custody: PASS`。

未运行全仓无差别测试。原因：本 remediation execution 的增量门禁已覆盖全部改动与相邻 permission/MCP/Skill/CodeWorker/API 路径；无关长耗时套件按现行分层规则留给后续聚合审查。没有跳过当前语义所需的行为或失败路径。

## 5. Source-free cleanroom

最终 cleanroom：`C:\Users\libin\AppData\Local\Temp\zyra-r01-execution-02-final-cleanroom-51389cf7bdf24e7ca9bf7870e03eb887`

只复制以下正式边界：

- `packages/runtime/claude-runtime/**`
- `packages/integrations/claude-mcp/**`
- TypeScript code-worker entrypoint
- Python narrow host、CodeWorker routing、permission durable commit 文件
- standalone fake MCP test peer

未复制根来源仓库、`vendor/**`、`vendor-runtimes/**`、source-pool/runtime-sources、node_modules、构建产物或缓存。使用 Zyra 已有 Python interpreter 作为测试工具链。cleanroom 中 TypeScript 行为测试 `20 passed`，custody audit `PASS`。

## 6. Diff buckets

基于 staged diff、相对 `4df4ba614f18d2fb211132027f27311f9ee7d305`：

| Bucket | Added | Deleted | Counting decision |
| --- | ---: | ---: | --- |
| TypeScript production | 2,634 | 8 | 有效正式实现 |
| Python durable production | 408 | 5 | 有效 Zyra state/API/routing 实现；不取得 TypeScript capability owner |
| Python adapter-only narrow host | 844 | 25 | 单独报告，不用作 TypeScript 深度内化行数证明 |
| Tests | 549 | 91 | 验证证据，不计 production 下限 |
| Package/config and audit script | 129 | 2 | 配置与审计，不计 production 下限 |
| Generated/data/docs/vendor-like/source-pool | 0 | 0 | 无 |

production source 合计为 `3,886` additions / `38` deletions，其中 adapter-only 已单独隔离。没有用 tests、audit、配置、数据或 vendor-like 内容抵扣正式实现。

## 7. Remaining boundary for execution-03

- AgentTool/subagent/background/control command 的最终 TypeScript owner 尚未在本执行文档关闭。
- 旧 Python MCP/Skill/permission compatibility 实现的物理删除或进一步降级留给 execution-03；它们在当前默认 TypeScript CodeWorker 不可达，execution-03 不得将其迁入 vendor 或重新设为 fallback。
- execution-03 必须保持本审查已固定的 permission/MCP/Skill owner 和 `tool.settle` 事务边界，不得用“统一 Python 控制面”覆盖本次切换。
