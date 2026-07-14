# M1-R01 Claude Source-Custody Remediation Review Plan

状态：in progress。`execution-01` 与 `execution-02` 已完成代码、行为验证、cleanroom 和独立审查；`execution-03` 是唯一下一入口。本文只记录前向 remediation 事实，不回写受保护历史 slice 的完成事实。

## 历史保护

M1-02A 到 M1-03D 的 Python 行为闭环、测试、行数和提交事实保持不变。R01 只判断 Claude TypeScript source custody、默认调用路径和 canonical owner 是否完成前向切换，不用新结论回写历史事实。

## 三个固定执行文档的连续审查范围

| Execution document | Source-custody scope | Required behavior evidence | Status |
| --- | --- | --- | --- |
| `execution-01-runtime-core-typescript-cutover.md` | Query/session/tool/context/compact TypeScript core | default CodeWorker、budget、compact/restore、exact resume、disable failure | completed in `4df4ba614f18d2fb211132027f27311f9ee7d305` |
| `execution-02-permission-mcp-skill-cutover.md` | permission/MCP/SkillTool/plugin/command TypeScript runtime | allow/deny/ask、MCP lifecycle/auth/tools/resources/prompts、skill invoke/update/disable | completed in the execution-02 evidence commit; review: `docs/reviews/M1-R01-execution-02-permission-mcp-skill-cutover-review.md` |
| `execution-03-agenttool-control-final-cutover.md` | AgentTool/subagent/control cutover | child execution、authority/budget、cancel/resume、fanout/fanin、command owner、cleanroom | pending |

三个执行文档是固定的连续工作包，不得再拆成新的审批型 slice。文档内部阶段不是审批边界；局部跨语言 DTO/adapter 记录技术理由和等价证据即可，只有实质或累计改变 primary/canonical owner 时才写一条 R01 级决策记录，不进行逐 port 用户审批。

## Execution-02 已关闭的 source-to-target

| Upstream source area | Zyra target | Migration mode | Explicitly cut or rejected |
| --- | --- | --- | --- |
| `claude-code-best/src/hooks/toolPermission/**`、`src/utils/permissions/**`、tool-use permission context | `packages/runtime/claude-runtime/src/permission/**`、`src/capability-host.ts` | TypeScript 语义迁移；使用 Zyra request fingerprint、full tool identity、mode/rule snapshot | 不迁移上游品牌 UI、遥测、私有服务依赖；不调用 Python evaluator 作为 fallback |
| `claude-code-best/src/services/mcp/client.ts` 及 MCP tools/resources/prompts 生命周期 | `packages/integrations/claude-mcp/**` | TypeScript 模块级重构；stdio/HTTP JSON-RPC、auth handle、list-change、reconnect、elicitation | 不依赖根来源仓库、npm link、外部 sidecar 源码池；不把 legacy Python in-process peer 接回 CodeWorker 主路径 |
| `claude-code-best` SkillTool、Markdown skill、commands/plugin discovery 机制 | `packages/runtime/claude-runtime/src/skills/**`、`src/capabilities.ts` | TypeScript 裁剪内化；安全 root、frontmatter、resource、command/plugin catalog | 不复制 marketplace/cache 整体目录，不允许 symlink/path escape，不保留 Python SkillTool 为默认 owner |
| Claude tool loop 的 permission-before-execution 与 tool-result settlement | `packages/runtime/claude-runtime/src/capability-host.ts`、`src/stdio.ts`、`packages/workers/zyra_workers/typescript_claude_runtime.py` | TypeScript 决策和能力执行；Python 仅做 durable commit、builtin side effect、event/checkpoint projection | 不允许权限提交即提前完成 continuation；不允许 TypeScript 能力失败后静默改由 Python 执行 |

## Execution-02 state custody

| State domain | In-run canonical owner | Durable owner | Restore/event boundary |
| --- | --- | --- | --- |
| permission policy、rule match、allow/deny/ask、recovery alternatives | TypeScript `TypeScriptPermissionEvaluator` | Python `PermissionStateStore` 只保存 mode/rules/approval/decision/grant | Python 验证 TypeScript exact binding 与 policy digest，投影带 `canonical_policy_owner=typescript` 的事件；不调用 Python policy evaluator |
| permission continuation 与一次性 grant settlement | TypeScript 决定并在真实能力执行后发 `tool.settle` | Python continuation store、CAS claim、grant store | 成功/失败 settlement 后才结束 continuation；未知、重复、缺失 settlement 失败关闭 |
| MCP connection、catalog、tools/resources/prompts、auth/reconnect | TypeScript `@zyra/claude-mcp` | CodeWorker session checkpoint 保存 TypeScript capability snapshot；credential 只以 env handle 进入 transport | 每次 run 从正式配置重开连接；legacy Python API peer 只保留控制面/兼容测试，不进入默认 CodeWorker |
| Skill/command/plugin discovery 与 invocation | TypeScript skill runtime | 文件系统正式 roots 与 CodeWorker session checkpoint | catalog digest、root precedence 和 capability snapshot 进入 TypeScript session；Python projection 在 TypeScript owner 路径不打开 |
| builtin file/shell/browser side effect、event/artifact/checkpoint | TypeScript permission gate 后由 Python narrow host 执行 | Zyra workspace、event log、artifact store、session store | 这是显式语言边界，不取得 permission/MCP/Skill canonical owner |

## Execution-02 默认调用链

`task/API -> CodeWorkerRuntime -> TypeScript code-worker stdio -> TypeScript CapabilityRuntime -> TypeScript permission decision -> Python durable exact commit -> TypeScript MCP/Skill/Command execution or Python builtin ToolExecutor -> tool.settle -> Zyra event/artifact/checkpoint`

该调用链没有 Python QueryEngine、Python permission evaluator、Python MCP projection 或 Python SkillTool 的静默接管。旧 Python 模块尚未物理删除的部分被限制在非 TypeScript/compatibility 路径，物理退役与 AgentTool/control 最终切换由 `execution-03` 完成。

## 必填证据

- source-to-target：上游文件、source language、target package、migration mode、裁剪内容、拒绝内容。
- state custody：Query/session/tool/permission/MCP/skill/subagent/control 的内存 owner、durable owner、event identity、restore owner。
- default call path：API/CLI/task ingress 到 TypeScript runtime，再到 Zyra durable store/event/artifact 的真实 trace。
- semantic parity：正常行为、失败路径、取消、幂等、budget、compact/restore、permission resume、MCP reconnect、skill disable、subagent recovery。
- disable/mutation evidence：断开 TypeScript core 后默认行为失败或明显改变，且 Python compatibility bridge 不静默接管。
- dependency boundary：仓内 workspace/lockfile/build/package；无根来源仓库、vendor/source-pool、npm link、工作区外 Docker context 或缓存依赖。
- vendor hard failure：任何新增或迁入 `vendor/**`、`vendor-runtimes/**`、source-pool/runtime-sources 的交付实现直接失败，其物理行数不得计入有效实现。
- anti-wholesale-copy：逐项证明上游源码已经按 Zyra module/state/event/permission/error/test owner 拆解；把上游整仓、主要目录或原模块边界换名放进 `packages/**`，但仍保留上游入口、依赖图、状态模型或核心控制流，只能标 migration/source pool，不能关闭 R01。
- diff buckets：TypeScript production、Python production removed/demoted、adapter-only、test、generated、data、docs、vendor-like/source-pool。

## 决策记录模板

仅当单项或累计变化会改变 primary 控制流、canonical owner、runtime custody 或 transaction/lease/idempotency/restore 语义时填写一次：变化范围、现有权威边界、可选方案、选择理由、状态迁移、兼容期、失败/回滚路径和受影响测试。不受影响工作继续推进；不存在逐文件或逐模块审批。

## 关闭条件

三个固定执行文档要求的代码、真实行为测试、失败路径、source-free cleanroom、累计批判式自审和 Zyra evidence commit 全部存在后，才可把本计划标为 complete，并把 `execution-state.yaml` 的下一入口恢复为 M1-S05B-01。
