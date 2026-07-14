# M1 03A-03D 聚合审查记录（2026-07-12）

## 1. 审查结论

审查范围为 `M1-S03A-01` 至 `M1-S03D-02`，聚合基线为 `1a4aa10`，被审查实现头为 `aca0fc5`，审查修复提交为 `3367825`。按《执行单元完成后通用审查任务书》完成文档、代码、主路径、状态托管、测试质量、有效行数、提交边界和干净目录复核后，结论为：**修复后通过，无阻断项**。

本次没有把账本、manifest、source map、runtime asset、测试或 vendor-like 内容当作父单元 production 最低线。03A-D 的保守 production 合计为 `81,676` 行，高于四个父单元合计最低线 `65,000` 行；聚合 diff 中 `vendor/**` 与 `vendor-runtimes/**` 新增为 0。

赛题门禁没有因本次基础设施审查被提前关闭。03D 为 `REQ-TOPO-01`、`SCORE-COMPAT` 提供 logical subagent、动态命令与角色控制基础；03A-C 为 sealed permission、MCP/skill 工具执行、因果事件与恢复提供工程基础，但双跨领域 live run、2,000 有效 transitions、真实端边云、多模型、动态稀疏拓扑对照等仍由后续 owner 单元交付。

## 2. 审查中发现并修复的问题

| 严重度 | 问题 | 语义影响 | 修复与证据 |
| --- | --- | --- | --- |
| 高 | 03D 动态 built-in command registry 漏注册既有 `/scheduler` | API 命令入口返回 400，形成已存在主路径回归 | 在 `packages/commands/zyra_commands/runtime/registry.py` 恢复 descriptor，并在 API read projection 注册 `scheduler.inspect`；`test_scheduler_api` 通过 |
| 高 | 03D canonical MCP owner 只返回嵌套 diagnostics | `/mcp` 丢失 `enabled/health/catalog/owner_slice` 等稳定 control contract，restore/API 消费方失效 | owner 改为调用真实 `McpCommandAdapter`，保留 canonical owner、control receipt 与 live data；MCP restore/API 测试通过 |
| 高 | 03D permission owner 暴露方向偏离 custody-safe 摘要 | `/permissions` 不再满足“详情必须有 custody”的既有安全契约 | owner 返回 task/run scoped 脱敏计数和 structured routes，不返回 rules、requests、session IDs；API 测试通过 |
| 中 | 03C 引入 `zyra_skills` 后，unittest discovery 与两个独立脚本未补 package bootstrap | 全量 discovery 出现多处 `ModuleNotFoundError: zyra_skills`，clean CLI/提取脚本不可独立运行 | 补齐 `tests/integration`、`tests/unit`、productization verifier、source extractor 的 skills package path；相关脚本和全量 discovery 可达 |
| 中 | `verify_m2.py` 仍按旧 Skill DTO、vendor metadata、无权限副作用和 artifact 顺序验证 | 验证脚本与 03A/03C 实际语义冲突，不能作为当前证据 | 改为验证 `SkillRevision` provenance/builtin root、permission-required fail-closed、只读 trace/checkpoint 与 trace artifact 查找；验证通过 |
| 中 | Skill source audit 的禁止路径字面量会被 submission boundary 扫描器自命中 | 审计实现自身导致边界门禁误报 | 禁止片段改为运行时组合，检查语义不变；submission boundary 通过 |
| 中 | tool-loop 测试硬编码 9 个工具 | 03C 增加 3 个 active skill tools 后产生错误回归 | 改为验证最低基础数和 runtime context/registry 精确一致；12 项相关测试通过 |
| 低 | 03D 新语义落地后，旧测试仍期待 `/clear` fail-closed、MCP `runtime_status=live` | 测试与 canonical session owner/stateful command 语义不一致 | 更新断言：`/clear` 必须 checkpoint 后推进 epoch，`/rewind` 在 owner 未接通时继续 fail-closed，命令 envelope 为 `stateful` |

## 3. 目标覆盖与主路径矩阵

| 父单元 | 核心目标 | Zyra-owned 目标模块与状态 owner | 主路径入口与动态证据 | 结论 |
| --- | --- | --- | --- | --- |
| M1-03A | permission rule/decision/request/session custody、ask/approve/sealed | `packages/runtime/zyra_runtime/permission/**`；`PermissionStateStore`、custody token、canonical events | CodeWorker/BrowserWorker/tool executor、permission API 与 `/permissions`；真实阻断、精确一次恢复、sealed deny | 历史行为闭环完成；Claude TypeScript permission source custody 由 M1-R01-02 前向纠偏 |
| M1-03B | MCP config/transport/catalog/auth/resource/prompt/sampling/elicitation/control/restore | `packages/integrations/zyra_integrations/mcp/**`；`McpClientRuntime` stores、CodeWorker session projection | MCP API、`/mcp`、ToolRegistry、CodeWorker restore；历史 Python 路径通过 catalog/health/notification/restore 行为测试；未使用 Node sidecar 只是实现事实，不能证明 Claude TypeScript MCP 主链已迁移 | 历史行为闭环完成；Claude TypeScript MCP source custody 由 M1-R01-02 前向纠偏 |
| M1-03C | Markdown skill loader/version/provenance/invocation、plugin/hook/command/agent/marketplace/cache | `packages/skills/zyra_skills/**`；registry、invocation/update/plugin state stores | CodeWorker tool loop、skills API、`/skills`、`/hooks`、update suspend/resume；禁用即改变行为 | 历史行为闭环完成；Claude TypeScript Skill/Extension source custody 由 M1-R01-02 前向纠偏 |
| M1-03D | logical subagent lifecycle、authority ceiling、budget、durable background/fanout/fanin、dynamic commands/session controls | `packages/runtime/zyra_runtime/subagents/**`、`packages/commands/zyra_commands/runtime/**`；SubagentTaskStore、ControlRequestStore、SessionControlStore、SQLite canonical task/session owner | `AgentTool`、commands API、`/clear`、`/scheduler`、MCP/permission owner handlers；跨 task 拒绝、取消、恢复、幂等、typed yield | 历史行为闭环完成；Claude TypeScript AgentTool/control source custody 由 M1-R01-03 前向纠偏 |

断开即失败证据覆盖 permission gate、MCP runtime/restore、SkillRuntime disable、subagent/command owner disable、tool-loop registry/context、canonical session mutation。未发现主要模块只能由 import smoke、固定 health 或 fixture replay 触发的情形。

## 4. 来源到目标与内化裁决

主要来源为 `claude-code-best` 的 permission/MCP/SkillTool/AgentTool/commands/session control，`opencode` 的 durable session/event、provider/tool/permission/MCP/skill/command/plugin 模式，以及 browser-use 的 browser message/action permission/watchdog 接口。实现已拆入 Zyra 的 runtime、integrations、skills、commands、API、event、artifact 和测试边界，没有让正式运行依赖工作区根目录来源仓库。

来源 ledger、source sync 和 runtime assets 只用于追溯；核心决策由 Zyra-owned stores、policy、dispatcher、runtime 和 canonical events 承担。聚合 diff 没有新增 vendor/vendor-runtimes 实现。Skill Markdown body 属于 runtime-assets，不计 production 最低线。

## 5. 有效行数与分桶

以各父单元起点和其 slice review 的保守排除口径复核：

| 父单元 | 最低 production 线 | 保守 production | 结果 |
| --- | ---: | ---: | --- |
| M1-03A | 16,000 | 17,679 | 通过 |
| M1-03B | 16,000 | 26,865 | 通过 |
| M1-03C | 16,000 | 18,023 | 通过 |
| M1-03D | 17,000 | 19,109 | 通过 |
| 合计 | 65,000 | 81,676 | 通过 |

`1a4aa10..aca0fc5` 聚合原始变化为 265 files、`+131,885/-4,960`。其中 apps `+5,081/-381`、packages `+104,030/-4,297`、scripts `+1,201`、tests `+19,669/-218`、docs `+1,635/-63`；packages 中 ledger seed/data `+13,289/-3,693` 被排除，skills runtime-assets `+257` 被排除，tests/docs/scripts 不抵扣父单元 production 最低线，vendor/vendor-runtimes 为 0。

四个 `unit-review --fail-on-error` 均返回 `ok=true`、`error_count=0`、`blocker_count=0`。通用审计器仍报告 3 类非阻断 warning（零值信号被当成 missing、lightweight M3 reports、target impact 通用字段）；这些不替代本记录中的父单元逐项行为审查，也不作为赛题门禁关闭证据。

## 6. 验证结果

| 验证 | 结果 |
| --- | --- |
| 03A-D 定向套件 | 287 tests OK，1 skipped（Windows symlink privilege） |
| 修复回归组合 | API/MCP/scheduler/tool-loop 36 tests 中修复项全部通过；后续 cleanroom 关键组合 42 tests OK |
| `verify_m2.py` | 通过（项目 `.venv`） |
| `verify_claude_productization_foundation.py` | 通过 |
| `verify_submission_boundary.py` | 通过 |
| permission/MCP source sync | permission 58 decisions aligned；MCP 62 decisions aligned |
| `compileall apps packages scripts tests` | 通过 |
| `git diff --check` | 通过（仅 Windows LF/CRLF 提示） |
| 全量 discovery | 637 tests：635 passed、1 skipped、1 browser live 时序失败；失败项立即独立复跑通过 |
| cleanroom（`git archive 3367825`） | `verify_m2`、submission boundary、42 项关键 API/MCP/scheduler/tool-loop/subagent 行为全部通过 |

全量唯一失败发生在 browser-use live 页面 readiness timeout 后的 click/search 时序，独立复跑完整通过；同轮 live upload/download 通过。该项不由 03A-D 修改引入，记录为非阻断 flaky 风险，不宣称本轮全量零失败。后续 browser/watchdog 单元应把本地页面 readiness 与 action retry 稳定性纳入长期回归。

## 7. 风险、缺口与赛题状态

- 无本审查范围内的完成阻断项。
- browser-use live test 存在一次可复现性抖动；当前不影响 03A-D 完成，但不能在正式 benchmark 中依赖单次成功。
- `unit-review` 的若干报告仍是 lightweight 通用审计，需按计划在 M3 工具化收束；本次不将其 warning 误判为动态赛题证据。
- 03D logical subagent 是后续动态拓扑/异构角色基础，不等于已经完成稀疏拓扑生成、真实多 provider/model 或端边云 dispatch。
- 03A interactive ask/approve 可用，但正式 sealed benchmark 仍必须保持 `human_intervention_count=0`，高风险/未知动作 deny 后进入 recovery/replan。
- 本次触达 `REQ-TOPO-01`、`REQ-CLOSE-01`、`REQ-FAULT-01`、`REQ-TRACE-01`、`SCORE-COMPAT`、`SCORE-ROBUST` 的工程前置能力；矩阵状态不因本次审查自动提升。

## 8. 提交与状态边界

- 被审查实现头：`aca0fc5`
- 审查修复提交：`3367825`
- 本记录将在 evidence commit 中提交；`execution-state.yaml` 仅在 evidence commit 存在后更新 verified head。
- `G:\agent-zoo\docs/**` 位于 Zyra Git 仓库之外。总工程计划、M1 README 和架构分析的当前入口摘要已校准到“03D-02 完成，下一入口 04A-01”，但这些根目录文档不能随 Zyra commit 提交。
- 下一执行入口保持 `slice-04a-01-browser-session-productization-foundation.md`，本审查没有改动 04 部分实现。
