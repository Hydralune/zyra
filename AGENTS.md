# AGENTS.md

`zyra` 是比赛项目本体：面向超长程复杂任务的动态异构群体智能与深度协同推理 Agent 系统。

## 项目目标

- 第一阶段目标是完整集成版系统，不是最小演示或概念验证。
- 系统应覆盖赛题要求：超长程上下文连续性、动态异构拓扑、低熵通信、神经符号协同推理、端-边-云资源调度、动态异常/需求变更/节点失效注入、可视化中间决策与推理轨迹。
- 第二阶段再围绕赛题评分点做定向重写、优化、鲁棒性增强和前端完善。

## 参考文档

- 赛题分析：`../docs/比赛项目开源Agent架构借鉴分析.md`
- 第一阶段权威执行计划：`../docs/第一阶段总工程计划.md`
- 执行单元目录：`../docs/milestones/`
- 旧版工程计划背景：`../docs/第一阶段工程计划（旧版，仅作背景参考）.md`
- 根目录约定：`../AGENTS.md`

正式开发前应优先阅读上述文档，尤其是：

- `../docs/第一阶段总工程计划.md`。
- 当前被用户指定的 `../docs/milestones/**/unit-*.md`。
- `../docs/比赛项目开源Agent架构借鉴分析.md` 的 `0.4 里程碑内化索引`。

如果总工程计划、执行单元文档和分析文档不一致，应先更新这些文档，再继续实现。

## 里程碑编号重置

- 用户已明确：此前完成的原 M0-M5 现在统一合并为新的 `M0：基础接入与主路径打通`，不能再被称为六个独立完成的里程碑。
- 原 M0-M5 的代码仍保留为基础接入成果：schema、event log、task graph、worker adapter、control command、symbolic control、MemoryFabric 和第一版 scheduler/fault path。
- 这些成果不满足用户要求的“重型代码仓库内化”标准。后续不得继续按旧 M5/M6 顺序推进；新的下一步是新 M1：重型 Runtime/Memory/Scheduler/Fault 内化回补。
- 文档中如果出现“原 M2/M4/M5”字样，应理解为历史能力标签或来源索引，不是当前里程碑编号。

## 代码复用约定

- 本项目建议优先复用、迁移或封装接入 `../claude-code-best`、`../browser-use`、`../langgraph`、`../agentscope`、`../OpenHands`、`../openclaw`、`../hermes-agent`、`../agent-framework` 中成熟的代码、模块和架构。
- 不要替用户做“是否复用代码”的保守决策；默认先评估直接复用、大模块接入或 adapter 封装，再判断是否需要重写。
- 只有在接口冲突、运行环境不适配、耦合成本过高、维护成本明显高于重写，或与赛题目标明显不匹配时，才应选择重写；选择重写时需要说明原因。
- 代码级复用和大模块级接入都应作为优先选项；接入时应说明来源、用途、边界和替换方式。
- 对 `claude-code-best` 要按高价值编码 Agent 运行时重点复用来源对待，尤其关注 QueryEngine、tool loop、permission runtime、context/compact、MCP、plugins、SkillTool、AgentTool、TUI/session commands。
- 不要把新的 M0 基础接入实现误解为后续阶段的执行策略。从新的 M1 开始必须明显转向成熟代码迁移、大模块复用和 sidecar/adapter 接入；不能用小规模手写闭环、mock 或占位模块替代第一阶段完整系统目标。
- `../` 下的其它仓库只是来源仓库，`zyra` 才是最终提交项目。凡是最终运行依赖的复用代码、skills、配置、前端组件或 sidecar runtime，都必须迁移、vendor、subtree/submodule 或封装进 `zyra` 内部，不能让 `zyra` 在提交后依赖 `../claude-code-best`、`../browser-use`、`../OpenHands` 等相对路径。

## 重型内化目标

- 第一阶段完成时，`zyra` 应是完整、可运行、可演示、可继续优化的重型 Agent 系统，而不是轻量控制壳、接口样例或单路径 demo。
- 当前原 M0-M5 只算新的 M0 基础接入：它建立了 schema、runtime 边界、控制协议、神经符号协作、第一版 memory 和第一版 scheduler/fault 主路径，但还没有达到重型代码仓库内化标准。新 M1/M2 必须继续明显增加真实运行能力和非 vendor 主体代码，把来源仓库中成熟模块转化为 `zyra` 内部可维护的 package、app、runtime、adapter 或 UI 视图。
- 代码规模不是单独的验收指标，但第一阶段预期会继续向数十万行量级增长。若后续里程碑只新增少量 glue code，却没有把 memory、scheduler、fault recovery、control console、artifact/diff/browser/terminal 等成熟能力内化进 `zyra`，应视为执行偏轻。
- 新 M1 应重点回补并内化 runtime、permission、MCP、SkillTool/AgentTool/subagent、browser-use message/watchdog、memory retrieval/skill memory、scheduler、sandbox/gateway、fault recovery；新 M2 应重点落地正式控制台、事件时间线、拓扑视图、artifact/diff/browser/terminal 面板和运行中需求变更交互。
- 后续每个里程碑计划和自检都应列出本阶段内化的来源仓库模块、目标路径、运行入口、测试或验证命令，以及仍保留在 vendor pool 中的原因。不要只写“参考了某仓库”，必须说明它如何成为 `zyra` 的可运行组成部分。
- 不允许把真正的代码内化继续后移。原 M0-M5 已经建立主路径，但不能被解释成 runtime、browser、memory、skills、permission、MCP、watchdog、scheduler 和 UI 的深度内化已经足够；新 M1 必须从清算这些债务开始，而不是再做轻量 glue。
- 新 M1 的最低完成形态是后端重型集成：QueryEngine/tool loop、permission/MCP/SkillTool/AgentTool、browser message/watchdog、worker pool/lifecycle、resource scheduler、local/docker/cloud 或 simulated backend、sandbox/gateway、fault injection、recovery planner、scheduler-to-symbolic、scheduler-to-memory、control command/API 接入都必须进入真实运行路径。
- 新 M2 的最低完成形态是正式控制台：event stream、任务图/拓扑、agent 状态、artifact/diff/terminal/browser viewer、permission/session/context/memory panels、command palette、故障注入和运行中需求变更输入都必须连接真实 API 和 event log。
- 如果一个里程碑只新增少量 schema、简单 if/else、薄 wrapper、mock 数据或静态页面，即使测试通过，也不能视为完成重型目标。阶段自检必须先补齐，或者明确把里程碑保持为未完成。
- 新 M3 只能做冻结、产品化整合、vendor 收束和来源映射；不能把第一次大规模迁移 runtime/scheduler/UI 推迟到新 M3。
- 第一阶段剩余执行单元的最低有效新增代码总量为 `550,000` 行，具体分配见 `../docs/第一阶段总工程计划.md` 和当前执行单元文档。
- 当前执行单元已经细拆为 `39` 个，M1/M2/M3 分别为 25/9/5 个；单个执行单元的最低有效新增代码通常为 9,000-18,000 行，最高 20,000 行。不要把多个执行单元合并成一次执行，也不要恢复成单次六七万行的大任务。
- 代码行数下限是失败线，不是完成线。即使超过目标行数，只要执行单元目标、详细任务、主路径接入、验证或批判式审查没有完成，仍然视为失败。
- 低于执行单元行数下限默认失败，除非能给出非常强的工程理由，例如目标上游模块已经完整内化、裁剪、重构并强化，再增加只会制造废代码。
- 文档、注释、mock、死代码、未接入 vendor 堆放、无关上游外壳不得计入有效新增代码。

## 工程执行约定

- 按 `../docs/第一阶段总工程计划.md` 和 `../docs/milestones/**/unit-*.md` 推进。用户每次会指定一个执行单元；agent 只执行该单元，不自行跨到下一个单元。
- 每完成一个执行单元，应更新对应单元执行记录或自检文档；只有该里程碑全部单元完成后，才可更新里程碑状态。
- 每完成一个阶段或里程碑后，应先以批判、审视的视角进行代码审查，再进入下一阶段；发现实际问题时应优先修复，并重新运行相关验证。
- 阶段审查应重点检查 bug、行为回归、架构边界失控、与赛题要求不匹配、缺少必要测试、代码复用模块适配问题和后续扩展风险。
- 阶段审查还必须检查是否出现“为了快而缩小能力面”的倾向；如果里程碑只是 mock、占位实现、单路径 demo 或轻量替代，应视为未充分完成，并优先补齐成熟模块复用或记录明确的补齐计划。
- 阶段审查必须检查 `zyra` 是否仍依赖根目录来源仓库的 `../` 路径；如果存在，应视为开发期临时桥接，必须迁移进 `zyra` 或记录明确的落位计划。
- 阶段审查必须检查本阶段是否产生了足够的可运行能力和代码内化证据。只有薄 wrapper、空 schema、未连接的 API 或未接入 event log/control command/artifact 的模块，不能单独支撑里程碑完成。
- 阶段审查必须附带内化账本：来源仓库、来源模块、目标路径、接入方式、验证命令、仍保留在 vendor pool 的原因、是否进入任务图/event log/artifact/control command/API/UI 主路径。
- 阶段审查必须附带代码行数审查：执行前 `BASE_COMMIT`，执行后 `git diff --numstat <BASE_COMMIT> HEAD -- apps packages tests scripts vendor-runtimes skills`，有效新增代码行数、排除项和是否达标。
- 原 M0-M5 已有历史记录：`docs/plans/M0-M3-heavyweight-self-check.md`、`docs/plans/M4-memory-compact-trajectory.md`、`docs/plans/M5-scheduler-fault-recovery.md`。后续进入新 M1 前应先读取这些文档或运行审计脚本，把其中的 internalization debt 作为阶段输入。
- 新增模块要优先明确 schema、event、artifact、adapter 边界。
- 不要让 agent 间自由广播长上下文；公共通信层应使用结构化消息、evidence ref、artifact ref 和状态 delta。
- 需求变更必须支持任务运行中直接输入新指令，并记录为 `requirement_change` 事件，而不是停止当前 run 后重开。

## Git 工作流约定

- `zyra` 使用 Git 作为项目内版本控制边界；后续阶段性开发、修复、自检和文档更新完成后，agent 应主动执行必要的 Git 操作，而不是只提示用户手动提交。
- 开始较大改动前，应先查看 `git status --short`，识别已有未提交内容；不要回滚、覆盖或删除用户已有改动。
- 提交前应再次查看状态，并运行与改动范围匹配的验证命令。里程碑级改动至少运行对应 `scripts/verify_m*.py`、相关测试；涉及提交边界时运行 `scripts/verify_submission_boundary.py`。
- 提交时默认使用非交互命令：`git add ...` 和 `git commit -m "..."`。提交信息应简洁说明里程碑或修复主题，例如 `Complete M3 symbolic collaboration milestone`。
- 除非用户明确要求，agent 不应自动执行 `git push`、创建/修改远端仓库、改写历史、rebase、reset、checkout 覆盖文件、clean 删除未跟踪文件或切换分支。
- 如果工作区存在与当前任务无关的用户改动，应避免把它们混入提交；如果无法可靠区分，应先说明风险并征求用户确认。
- 每次提交后应向用户报告 commit hash、提交主题、验证命令和当前工作区是否干净。
