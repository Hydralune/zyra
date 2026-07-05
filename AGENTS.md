# AGENTS.md

`zyra` 是比赛项目本体：面向超长程复杂任务的动态异构群体智能与深度协同推理 Agent 系统。

## 项目目标

- 第一阶段目标是完整集成版系统，不是最小演示或概念验证。
- 系统应覆盖赛题要求：超长程上下文连续性、动态异构拓扑、低熵通信、神经符号协同推理、端-边-云资源调度、动态异常/需求变更/节点失效注入、可视化中间决策与推理轨迹。
- 第二阶段再围绕赛题评分点做定向重写、优化、鲁棒性增强和前端完善。

## 参考文档

- 赛题分析：`../docs/比赛项目开源Agent架构借鉴分析.md`
- 第一阶段工程计划：`../docs/第一阶段工程计划.md`
- 根目录约定：`../AGENTS.md`

正式开发前应优先阅读上述文档，尤其是第一阶段工程计划中的 M0-M7 里程碑。

## 代码复用约定

- 本项目建议优先复用、迁移或封装接入 `../claude-code-best`、`../browser-use`、`../langgraph`、`../agentscope`、`../OpenHands`、`../openclaw`、`../hermes-agent`、`../agent-framework` 中成熟的代码、模块和架构。
- 不要替用户做“是否复用代码”的保守决策；默认先评估直接复用、大模块接入或 adapter 封装，再判断是否需要重写。
- 只有在接口冲突、运行环境不适配、耦合成本过高、维护成本明显高于重写，或与赛题目标明显不匹配时，才应选择重写；选择重写时需要说明原因。
- 代码级复用和大模块级接入都应作为优先选项；接入时应说明来源、用途、边界和替换方式。
- 对 `claude-code-best` 要按高价值编码 Agent 运行时重点复用来源对待，尤其关注 QueryEngine、tool loop、permission runtime、context/compact、MCP、plugins、SkillTool、AgentTool、TUI/session commands。
- 不要把 M0/M1 的轻量骨架实现误解为后续阶段的执行策略。从 M2 开始应明显转向成熟代码迁移、大模块复用和 sidecar/adapter 接入；不能用小规模手写闭环、mock 或占位模块替代第一阶段完整系统目标。
- `../` 下的其它仓库只是来源仓库，`zyra` 才是最终提交项目。凡是最终运行依赖的复用代码、skills、配置、前端组件或 sidecar runtime，都必须迁移、vendor、subtree/submodule 或封装进 `zyra` 内部，不能让 `zyra` 在提交后依赖 `../claude-code-best`、`../browser-use`、`../OpenHands` 等相对路径。

## 重型内化目标

- 第一阶段完成时，`zyra` 应是完整、可运行、可演示、可继续优化的重型 Agent 系统，而不是轻量控制壳、接口样例或单路径 demo。
- 当前 M0-M3 主要建立 schema、runtime 边界、控制协议和神经符号协作主路径；M4-M6 必须明显增加真实运行能力和非 vendor 主体代码，把来源仓库中成熟模块转化为 `zyra` 内部可维护的 package、app、runtime、adapter 或 UI 视图。
- 代码规模不是单独的验收指标，但第一阶段预期会继续向数十万行量级增长。若后续里程碑只新增少量 glue code，却没有把 memory、scheduler、fault recovery、control console、artifact/diff/browser/terminal 等成熟能力内化进 `zyra`，应视为执行偏轻。
- M4 应重点落地长程记忆、context compact、trajectory replay、skill memory、checkpoint/retrieval；M5 应重点落地端边云 resource scheduler、worker manifest、sandbox/gateway、fault injection、recovery；M6 应重点落地正式控制台、事件时间线、拓扑视图、artifact/diff/browser/terminal 面板和运行中需求变更交互。
- 后续每个里程碑计划和自检都应列出本阶段内化的来源仓库模块、目标路径、运行入口、测试或验证命令，以及仍保留在 vendor pool 中的原因。不要只写“参考了某仓库”，必须说明它如何成为 `zyra` 的可运行组成部分。
- 不允许把真正的代码内化继续后移。M2-M4 已经建立主路径，但不能被解释成 runtime、browser、memory、skills、permission、MCP、watchdog 和 UI 的深度内化已经足够；M5 必须从清算这些债务开始，而不是只做一个新的轻量调度器。
- M5 的最低完成形态是后端重型集成：worker pool/lifecycle、resource scheduler、local/docker/cloud 或 simulated backend、sandbox/gateway、watchdog、fault injection、recovery planner、scheduler-to-symbolic、scheduler-to-memory、control command/API 接入都必须进入真实运行路径。
- M6 的最低完成形态是正式控制台：event stream、任务图/拓扑、agent 状态、artifact/diff/terminal/browser viewer、permission/session/context/memory panels、command palette、故障注入和运行中需求变更输入都必须连接真实 API 和 event log。
- 如果一个里程碑只新增少量 schema、简单 if/else、薄 wrapper、mock 数据或静态页面，即使测试通过，也不能视为完成重型目标。阶段自检必须先补齐，或者明确把里程碑保持为未完成。
- M7 只能做冻结、产品化整合、vendor 收束和来源映射；不能把第一次大规模迁移 runtime/scheduler/UI 推迟到 M7。

## 工程执行约定

- 按 `../docs/第一阶段工程计划.md` 的 M0-M7 推进。
- 每完成一个里程碑，应把对应标题从 `[ ]` 改为 `[x]`。
- 每完成一个阶段或里程碑后，应先以批判、审视的视角进行代码审查，再进入下一阶段；发现实际问题时应优先修复，并重新运行相关验证。
- 阶段审查应重点检查 bug、行为回归、架构边界失控、与赛题要求不匹配、缺少必要测试、代码复用模块适配问题和后续扩展风险。
- 阶段审查还必须检查是否出现“为了快而缩小能力面”的倾向；如果里程碑只是 mock、占位实现、单路径 demo 或轻量替代，应视为未充分完成，并优先补齐成熟模块复用或记录明确的补齐计划。
- 阶段审查必须检查 `zyra` 是否仍依赖根目录来源仓库的 `../` 路径；如果存在，应视为开发期临时桥接，必须迁移进 `zyra` 或记录明确的落位计划。
- 阶段审查必须检查本阶段是否产生了足够的可运行能力和代码内化证据。只有薄 wrapper、空 schema、未连接的 API 或未接入 event log/control command/artifact 的模块，不能单独支撑里程碑完成。
- 阶段审查必须附带内化账本：来源仓库、来源模块、目标路径、接入方式、验证命令、仍保留在 vendor pool 的原因、是否进入任务图/event log/artifact/control command/API/UI 主路径。
- M0-M3 已有重型目标自检文档：`docs/plans/M0-M3-heavyweight-self-check.md`；对应脚本：`scripts/audit_m0_m3_internalization.py`。后续进入 M4-M6 前应先读取该文档或运行该脚本，把其中的 internalization debt 作为阶段输入。
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
