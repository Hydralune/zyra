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

凡是被要求读取的项目文档，都必须从开头到结尾完整阅读整个文件；不允许只看摘要、目录、搜索命中、片段或局部上下文。如果文件较长，应分段读取直到 EOF，再开始执行、判断或引用该文档。

如果总工程计划、执行单元文档和分析文档不一致，应先更新这些文档，再继续实现。

## 目录结构约定

- `zyra` 的当前目录结构不是最终形态，也不是只能包含现有几个目录。后续为了完成正式内化，可以新增 `apps/**`、`packages/**`、`skills/**`、`scripts/**`、`tests/**` 以及必要的运行时、配置、资源或工具目录。
- 新目录必须对应清晰的 Zyra-owned 模块边界、运行责任和主路径入口；不能只是把上游目录换名放入 `packages/**`、`apps/**` 或其它看似正式的位置。
- 目录扩展应服务于结构内化、语义内化、裁剪内化、改造内化和维护内化：新增目录要能说明 schema/event/artifact/permission/memory/scheduler/recovery/API/UI 或 worker runtime 边界。
- `vendor/**`、`vendor-runtimes/**`、`source-pool/**`、`runtime-sources/**` 等目录只能作为历史债务、临时抽取材料、source-pool 证据或外部依赖追溯，不得作为第一阶段完成落位或有效新增代码来源。

## 里程碑编号重置

- 用户已明确：此前完成的原 M0-M5 现在统一合并为新的 `M0：基础接入与主路径打通`，不能再被称为六个独立完成的里程碑。
- 原 M0-M5 的代码仍保留为基础接入成果：schema、event log、task graph、worker adapter、control command、symbolic control、MemoryFabric 和第一版 scheduler/fault path。
- 这些成果不满足用户要求的“重型代码仓库内化”标准。后续不得继续按旧 M5/M6 顺序推进；新的下一步是新 M1：重型 Runtime/Memory/Scheduler/Fault 内化回补。
- 文档中如果出现“原 M2/M4/M5”字样，应理解为历史能力标签或来源索引，不是当前里程碑编号。

## 代码复用约定

- 本项目建议优先复用、代码级迁移、裁剪改造或封装接入 `../claude-code-best`、`../browser-use`、`../langgraph`、`../agentscope`、`../OpenHands`、`../openclaw`、`../hermes-agent`、`../agent-framework` 中成熟的代码、模块和架构。主来源仓库不是“仅供参考”的资料库；默认应先判断哪些源码可以进入 Zyra 正式模块。
- 不要替用户做“是否复用代码”的保守决策；默认先评估直接复用、大模块接入或 adapter 封装，再判断是否需要重写。
- 只有在接口冲突、运行环境不适配、耦合成本过高、维护成本明显高于重写，或与赛题目标明显不匹配时，才应选择重写；选择重写时需要说明原因。
- 代码级复用和大模块级接入都应作为优先选项；接入时应说明来源、用途、边界和替换方式。
- 对 `claude-code-best` 要按高价值编码 Agent 运行时重点复用来源对待，尤其关注 QueryEngine、tool loop、permission runtime、context/compact、MCP、plugins、SkillTool、AgentTool、TUI/session commands。
- 不要把新的 M0 基础接入实现误解为后续阶段的执行策略。从新的 M1 开始必须明显转向成熟代码迁移、大模块复用和 sidecar/adapter 接入；不能用小规模手写闭环、mock 或占位模块替代第一阶段完整系统目标。
- `../` 下的其它仓库只是来源仓库，`zyra` 才是最终提交项目。凡是最终运行依赖的复用代码、skills、配置、前端组件或 sidecar runtime，都必须迁移、裁剪、改造或封装进 `zyra/packages/**`、`zyra/apps/**`、`zyra/skills/**`、`zyra/scripts/**` 等正式模块，不能让 `zyra` 在提交后依赖 `../claude-code-best`、`../browser-use`、`../OpenHands` 等相对路径。`vendor`、`vendor-runtimes`、`source-pool`、`runtime-sources` 等目录只能作为历史债务或临时抽取材料，不能作为第一阶段完成落位或有效行数来源。

## 严格内化定义

- 内化不是把外部代码搬进项目里运行，而是把外部成熟能力拆解、裁剪、改造并融入 Zyra 自身模块体系，使其以 Zyra 的数据结构、事件、权限、状态、错误处理和测试方式工作。
- 如果某项能力的主要实现仍保持上游仓库原始目录结构，并通过单一 adapter/sidecar 作为黑箱调用，则不得计为深度内化；只能计为 vendored dependency、source pool 或 reference runtime。无论目录名是 `vendor`、`vendor-runtimes`、`third_party`、`runtime-sources`、`productized` 或其它名字，原样源码池、inventory、manifest、source map、seed、JSON/YAML/CSV、文档和只扫描源码得到的 contract 都不得计入有效新增代码。
- 真正的内化必须同时满足结构内化、语义内化、裁剪内化、改造内化和维护内化：上游机制要被拆入 `zyra/packages`、`zyra/apps`、`zyra/skills`、`zyra/scripts` 等 Zyra 模块边界，转化为 Zyra schema、event log、artifact、permission、memory、scheduler、recovery、control command、API 或 UI 的一等能力，而不是保留为上游目录形状的黑箱。
- “能被调用”不是充分条件。即使 vendored runtime 能启动、能被 adapter 调用、能通过 smoke test，只要主要实现仍是上游原样目录加薄封装，就不能把其物理行数计为深度内化；最多只能把 Zyra 侧 adapter、port、schema 转换、错误处理、状态接入、观测接入和行为测试计入有效实现。
- 每个执行单元自审必须回答：外部成熟机制被拆成了哪些 Zyra 模块；哪些上游代码被裁剪或重写；哪些 Zyra 数据结构、事件、权限、状态、错误处理和测试边界承担了该能力；如果删除或断开对应 Zyra 模块，哪条真实行为测试会失败。只能证明文件存在、源码被扫描、manifest 可读或 sidecar 返回固定 contract 的，不算内化。

## 反伪内化对抗验收

- 目录位置不能证明内化。把上游整仓、上游主要目录或保持上游模块边界的源码改名放入 `packages/**`、`apps/**`、`runtime/**`、`productized/**`、`third_party/**`、`runtime-sources/**`、`source-pool/**` 等任何目录，只要仍保留上游目录结构、入口、依赖图、状态模型或核心控制流，就只能计为 migration pool/source pool/reference runtime，不得计为深度内化有效代码。
- 多个薄 adapter 不能拆散黑箱。多个 adapter、manager、service、bridge、gateway、panel 或 API route 如果最终都委托同一个上游 CLI、sidecar、Docker 镜像、npm/pip package、外部进程或原样 runtime 执行核心决策，应整体视为一个黑箱依赖；只有 Zyra 侧协议转换、状态接管、错误处理、事件写入、权限裁决、预算控制、测试和主路径接入代码可以计入有效实现。
- 机械改写不是内化。批量改名、改 import、格式化、语言转换、生成式 port、bundle/minify、wheel/tarball 打包、把 JSON/YAML 伪装成 `.py`/`.ts` 常量、把上游示例或品牌 UI 搬入正式目录，都不能证明内化；只要语义边界和运行责任没有被 Zyra 接管，应按原样迁移池或生成物排除。
- 必须做干净目录验证。执行单元验收时应能在不包含根目录来源仓库的干净 `zyra` 副本中运行本单元核心测试；任何运行期依赖 `../claude-code-best`、`../browser-use`、`../OpenHands` 或其它根目录来源仓库、环境变量、npm link、pip editable path、Docker build context 的能力，都不能判定完成。
- 必须做动态可达性验证。声称内化的模块必须能从真实任务流、API route、CLI command、worker runtime、event type、artifact kind、control command 或 UI panel 触发；只被 import smoke、ledger 查询、source map、health 固定返回、示例脚本或 fixture replay 触发的代码，不得计入主路径内化。
- 必须做断开即失败验证。对每个声称完成的核心能力，应有测试或审计说明证明：禁用、删除或断开对应 Zyra 模块后，相关真实行为会失败或明显改变。只证明禁用 vendor/sidecar 后失败，不能证明 Zyra 已经完成深度内化。
- 必须做语义效果验证。permission 必须真实阻断或放行工具，scheduler 必须真实改变 worker/route，memory/compact 必须真实影响后续上下文或决策，watchdog/fault recovery 必须真实中止、恢复、重试或改路由，MCP/SkillTool/AgentTool 必须真实执行调用和边界约束，UI command/approval 必须真实改变后端 session；只写日志、event、ACK、建议值、静态面板或回放流，一律不算完成。
- 必须做有效行数分桶审查。每个执行单元自审必须把新增内容分为 production、test、generated、data、docs、vendor-like/source-pool、adapter-only、mock/fixture 等桶；generated、data-as-code、fixture-only、mock-only、source pool、vendor-like、ledger/source map/manifest 记录、薄 adapter 和未接入样板不得计入有效新增源码。
- 账本不能替代行为。ledger、manifest、inventory、source map、contract 和设计说明只能证明来源与计划，不能作为完成证据；没有真实 API/CLI/runtime/UI 行为测试、动态可达性证据和语义效果证据的来源项，不得标记为完成。

## 运行责任与验证分层

- M1/M2 当前执行单元必须把第二轮对抗发现的问题作为红线自证，而不是等到 M3 才判断：默认主路径必须使用新能力；新增 `pip/npm` 依赖、MCP server、插件、子进程、本地端口服务、Docker 镜像、动态 import 不能承担核心决策；session、permission、memory、scheduler、recovery、artifact 等状态必须说明由哪个 Zyra schema/store 持久化和恢复；event log/trace 必须能追溯到真实 span、tool call、artifact、worker route 或 state mutation；核心测试不得依赖 `.cache`、SQLite、artifact 残留、构建产物或预录轨迹；LLM 只能参与建议、分类和解释，不能替代 permission、scheduler、fault recovery、compact restore 的 Zyra-owned 约束、状态机或可审计规则；fallback 不能掩盖被验收模块失效。
- 上述红线在 M1/M2 中主要通过自审、针对性测试和证据说明落实；不要求每个执行单元都实现完整自动化审计系统。但只要当前单元已经违反这些红线，就不得以“后续 M3 工具化”作为通过理由。
- M3 负责把这些红线工具化和收束：依赖/进程审计、默认配置主路径 trace、干净缓存/干净目录场景、state custody map、event 因果校验、有效行数分桶报告、opaque bundle/binary 检查、source similarity 或 semantic port 风险提示，都应在 M3 source map、测试评测、打包健康检查和冻结报告中落地。
- 第二阶段再强化为 CI 级质量门禁：更严格的 AST/call graph 相似度审查、mutation/disable 测试、长期依赖治理、鲁棒性矩阵、安全边界和性能回归。第二阶段强化不能替代第一阶段对明显伪内化的即时失败判定。

## 重型内化目标

- 第一阶段完成时，`zyra` 应是完整、可运行、可演示、可继续优化的重型 Agent 系统，而不是轻量控制壳、接口样例或单路径 demo。
- 当前原 M0-M5 只算新的 M0 基础接入：它建立了 schema、runtime 边界、控制协议、神经符号协作、第一版 memory 和第一版 scheduler/fault 主路径，但还没有达到重型代码仓库内化标准。新 M1/M2 必须继续明显增加真实运行能力和非 vendor 主体代码，把来源仓库中成熟模块转化为 `zyra` 内部可维护的 package、app、runtime、adapter 或 UI 视图。
- 代码规模不是单独的验收指标，但第一阶段预期会继续向数十万行量级增长。若后续里程碑只新增少量 glue code，却没有把 memory、scheduler、fault recovery、control console、artifact/diff/browser/terminal 等成熟能力内化进 `zyra`，应视为执行偏轻。
- 新 M1 应重点回补并内化 runtime、permission、MCP、SkillTool/AgentTool/subagent、browser-use message/watchdog、memory retrieval/skill memory、scheduler、sandbox/gateway、fault recovery；新 M2 应重点落地正式控制台、事件时间线、拓扑视图、artifact/diff/browser/terminal 面板和运行中需求变更交互。
- 后续每个里程碑计划和自检都应列出本阶段内化的来源仓库模块、目标路径、运行入口、测试或验证命令，以及仍保留为历史 vendor/source-pool 债务或外部依赖的原因。不要只写“参考了某仓库”，必须说明它如何成为 `zyra` 的可运行组成部分。
- 不允许把真正的代码内化继续后移。原 M0-M5 已经建立主路径，但不能被解释成 runtime、browser、memory、skills、permission、MCP、watchdog、scheduler 和 UI 的深度内化已经足够；新 M1 必须从清算这些债务开始，而不是再做轻量 glue。
- 新 M1 的最低完成形态是后端重型集成：QueryEngine/tool loop、permission/MCP/SkillTool/AgentTool、browser message/watchdog、worker pool/lifecycle、resource scheduler、local/docker/cloud 或 simulated backend、sandbox/gateway、fault injection、recovery planner、scheduler-to-symbolic、scheduler-to-memory、control command/API 接入都必须进入真实运行路径。
- 新 M2 的最低完成形态是正式控制台：event stream、任务图/拓扑、agent 状态、artifact/diff/terminal/browser viewer、permission/session/context/memory panels、command palette、故障注入和运行中需求变更输入都必须连接真实 API 和 event log。
- 如果一个里程碑只新增少量 schema、简单 if/else、薄 wrapper、mock 数据或静态页面，即使测试通过，也不能视为完成重型目标。阶段自检必须先补齐，或者明确把里程碑保持为未完成。
- 新 M3 只能做冻结、产品化整合、历史 vendor/source-pool 债务清理和来源映射；不能把第一次大规模迁移 runtime/scheduler/UI 推迟到新 M3。
- 第一阶段剩余执行单元的最低有效新增代码总量为 `550,000` 行，具体分配见 `../docs/第一阶段总工程计划.md` 和当前执行单元文档。
- 当前 `39` 个 `unit-*.md` 是父级验收单元，M1/M2/M3 分别为 25/9/5 个；它们的 9,000-20,000 行下限是父级预算和失败线，不再表示一次实现应吞下整个单元。正式执行前应继续拆成更小 `slice-*.md`，通常每片 3,000-6,000 行生产内化代码，最高不超过 8,000 行；每片只承接一个语义能力、一个主要 source-to-target 迁移链、一个 Zyra 目标模块和一组真实行为测试。
- 例外：`M1-01A` 和 `M1-01B` 用户已明确不要拆分。后续不得回头把这两个文档拆成 slice；如复审发现问题，应在原单元文档、`docs/plans/M1-01A-ledger-schema-audit.md` 或 `docs/plans/M1-01B-extraction-runtime-scaffold.md` 中回补记录、代码和验证。
- 当前 `M1-01B` 的 `vendor-runtimes/claude-code-runtime/pilot` 只能视为 source-pool 证据，`required_for_main_path=false`，不得计入有效内化代码。`M1-02A` 以后必须继续把 QueryEngine/tool loop/session lifecycle 等主体迁入正式 Zyra 模块，不能把该 pilot 当作产品化 runtime。
- 代码行数下限是失败线，不是完成线。即使超过目标行数，只要执行单元目标、详细任务、主路径接入、验证或批判式审查没有完成，仍然视为失败。
- 低于执行单元行数下限默认失败，除非能给出非常强的工程理由，例如目标上游模块已经完整内化、裁剪、重构并强化，再增加只会制造废代码。
- 文档、注释、mock、死代码、未接入 vendor 堆放、无关上游外壳、原样 vendor/source pool 不得计入有效新增代码。
- 大型 seed、索引、清单、source-to-target 账本记录、JSON/YAML/CSV 数据文件、生成型 inventory、manifest、source map 或原样 vendor/source pool 不能计入“有效新增代码”来证明重型内化；只能单独报告为数据规模、账本覆盖规模或依赖规模。可计入的只限真正让这些数据或依赖参与运行时加载、审计、更新、API/CLI 查询、event log 或测试验证的 Zyra 实现代码。
- 后续执行单元必须以真实源码迁移、封装接入、裁剪产品化和主路径集成为主体。不得用大型数据文件、清单、schema 堆叠、测试体量、薄 wrapper、胶水代码、整仓 vendor 或原样 source pool 来凑行数；如果新增行数主要来自这些内容，应判定为任务缩水或失败。

## 工程执行约定

- 按 `../docs/第一阶段总工程计划.md`、`../docs/milestones/**/unit-*.md` 和拆分后的 `slice-*.md` 推进。用户每次会指定一个父级执行单元或一个执行切片；agent 只执行当前切片或当前单元内明确指定的工作，不自行跨到下一个单元。
- 每完成一个执行单元，应更新对应单元执行记录或自检文档；只有该里程碑全部单元完成后，才可更新里程碑状态。
- 每完成一个阶段或里程碑后，应先以批判、审视的视角进行代码审查，再进入下一阶段；发现实际问题时应优先修复，并重新运行相关验证。
- 阶段审查应重点检查 bug、行为回归、架构边界失控、与赛题要求不匹配、缺少必要测试、代码复用模块适配问题和后续扩展风险。
- 阶段审查还必须检查是否出现“为了快而缩小能力面”的倾向；如果里程碑只是 mock、占位实现、单路径 demo 或轻量替代，应视为未充分完成，并优先补齐成熟模块复用或记录明确的补齐计划。
- 阶段审查必须检查 `zyra` 是否仍依赖根目录来源仓库的 `../` 路径；如果存在，应视为开发期临时桥接，必须迁移进 `zyra` 或记录明确的落位计划。
- 阶段审查必须检查本阶段是否产生了足够的可运行能力和代码内化证据。只有薄 wrapper、空 schema、未连接的 API 或未接入 event log/control command/artifact 的模块，不能单独支撑里程碑完成。
- 阶段审查必须附带内化账本：来源仓库、来源模块、目标路径、接入方式、验证命令、仍保留为历史 vendor/source-pool 债务或外部依赖的原因、是否进入任务图/event log/artifact/control command/API/UI 主路径。
- 阶段审查必须附带代码行数审查：执行前 `BASE_COMMIT`，执行后分层运行 `git diff --numstat <BASE_COMMIT> HEAD -- apps packages skills scripts`、`git diff --numstat <BASE_COMMIT> HEAD -- tests`、`git diff --numstat <BASE_COMMIT> HEAD -- vendor vendor-runtimes`，有效新增代码行数、排除项和是否达标。
- 原 M0-M5 已有历史记录：`docs/plans/M0-M3-heavyweight-self-check.md`、`docs/plans/M4-memory-compact-trajectory.md`、`docs/plans/M5-scheduler-fault-recovery.md`。后续进入新 M1 前应先读取这些文档或运行审计脚本，把其中的 internalization debt 作为阶段输入。
- 新增模块要优先明确 schema、event、artifact、adapter 边界。
- 不要让 agent 间自由广播长上下文；公共通信层应使用结构化消息、evidence ref、artifact ref 和状态 delta。
- 需求变更必须支持任务运行中直接输入新指令，并记录为 `requirement_change` 事件，而不是停止当前 run 后重开。

## Git 工作流约定

- `zyra` 使用 Git 作为项目内版本控制边界；后续阶段性开发、修复、自检和文档更新完成后，agent 应主动执行必要的 Git 操作，而不是只提示用户手动提交。
- `G:\agent-zoo\zyra` 是当前 Git 仓库；`G:\agent-zoo` 根目录本身不一定是同一个 Git 仓库。若同时修改了根目录 `../docs/**` 或 `../AGENTS.md`，提交前必须说明这些文件是否能进入当前 commit；不能让用户误以为根目录非仓库文件已经随 `zyra` commit 提交。
- 开始较大改动前，应先查看 `git status --short`，识别已有未提交内容；不要回滚、覆盖或删除用户已有改动。
- 提交前应再次查看状态，并运行与改动范围匹配的验证命令。里程碑级改动至少运行对应 `scripts/verify_m*.py`、相关测试；涉及提交边界时运行 `scripts/verify_submission_boundary.py`。
- 提交时默认使用非交互命令：`git add ...` 和 `git commit -m "..."`。提交信息应简洁说明里程碑或修复主题，例如 `Complete M3 symbolic collaboration milestone`。
- 除非用户明确要求，agent 不应自动执行 `git push`、创建/修改远端仓库、改写历史、rebase、reset、checkout 覆盖文件、clean 删除未跟踪文件或切换分支。
- 如果工作区存在与当前任务无关的用户改动，应避免把它们混入提交；如果无法可靠区分，应先说明风险并征求用户确认。
- 每次提交后应向用户报告 commit hash、提交主题、验证命令和当前工作区是否干净。
