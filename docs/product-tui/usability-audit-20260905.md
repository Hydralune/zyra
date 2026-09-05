# Web / CLI 真实使用审查（2026-09-05）

本次按用户要求实际启动产品 CLI、daemon 和 Web，使用真实 ConPTY 及 Playwright Chromium 操作。组件测试、真实界面操作和真实外部模型执行分别记录；不把成功构建等同于体验验收。前一提交为 `a6a8f2ec fix(product): repair session continuity and interactive UX`，本文也记录其后的浏览器复验与修复。

## 文档调整

工作区根目录 `G:\agent-zoo\PRODUCT_TUI_TASK.zh-CN.md` 从历史 Phase A～L 执行清单改为七部分：问题、参考方法、工作方式、用户旅程、优先级、Web/CLI 共同约束、完成标准。删除把旧 milestone、发布认证和人工盲测当作本次修复前置条件的写法。

参考本地 Codex 的 `chat_composer.rs`、`textarea.rs`、`tui.rs`、`insert_history.rs`、`styles.md`、`history_cell.rs` 和选择器源码：重点是草稿、返回路径、内容高度、增量绘制和信息层次，不要求移植 Rust。实际启动的是 Zyra；没有声称本轮启动官方 Codex CLI 做实机对照。

明确要求同尺寸真实终端复验、真实两轮问答、停止后等待迟到结果、队列逐条编辑、已结束任务的信息查看、真实 PTY 审批与输入，以及连续记忆查询。根文档不属于 Zyra Git 仓库，不能随本仓库 commit 提交。

## 已修复的用户问题

| 范围 | 原来表现 | 修复后的行为 |
|---|---|---|
| CLI 输入 | Esc 丢补全草稿；多行/历史上下键不正确；提示不存在的 `/restore` | Esc 先关闭菜单；恢复草稿和光标；补全随选择滚动；支持实际换行序列 |
| CLI 绘制与详情 | 输入重复整屏擦写；空白撑到底行；长行详情被截断 | 相同帧不输出；按变更区域绘制；按显示宽度分页；End 可到末尾 |
| CLI 会话 | 结束后不能恢复；追问收不到上一轮内容；工具详情为空 | 可恢复最新终态任务；真实历史进入模型请求；工具凭据正确投影 |
| CLI 新建与检查 | `/new` 仍留着旧任务引用；结束后记忆/MCP/技能等不可读 | 清除旧任务和权限上下文；允许只读检查，修改动作仍检查运行状态 |
| Web 启动 | API 跨源、事件流/PTY 连接和深链接刷新不可靠 | 正式 `zyra ui` 启动固定同源 HTTP/WS 代理；深链接资源使用绝对路径 |
| Web 会话 | 创建时长时间没有任务；新任务继承旧会话；刷新丢回答或重复答案 | 创建回执立即可见，再异步执行；新会话隔离；详情缓存及历史水合；迟到流式片段不重复答案 |
| Web 导航 | 交付物入口回到对话；面板和通知拥挤 | 独立交付物页面；13 类证据面板按需展开；通知不挡任务按钮 |
| Web 命令 | 完整命令多按一次 Enter；旧参数补全残留 | 完整命令可直接提交；补全只对应当前输入；全局状态无需任务 |
| Web 队列 | 第二条排队覆盖 busy；编辑取走全部条目并丢草稿；换轮切走草稿 | 保持当前执行状态；只编辑选中条目；同会话草稿贯穿换轮并可刷新恢复 |
| 停止与恢复 | 停止不可用或只中止浏览器请求；之后被迟到成功覆盖 | 后端先提交取消状态；图执行检查取消；迟到结果不能覆盖取消；显式继续可以重开；输入及时恢复 |
| Web 权限/PTY | 权限绑定反复重建；响应版本不匹配；需手工搬 permit；输入失败丢内容 | 稳定绑定、兼容 v2 的单次响应；自动关联精确请求凭据；输入哈希绑定审批字节；失败恢复输入；正常退出不报连接错误 |
| Web 记忆 | 查询参数被当搜索词；排序超时；重复查询因时间戳变化丢结果 | 使用解析后的词/层/限额；分词一次并增量计算相似度；未变记录保持时间；索引版本覆盖时间字段；结果摘要优先显示 |
| 导出和场景 | 结束后导出被禁；场景错误成缺字段异常；只提供正式模式；失败原因隐藏 | 终态可导出；显示后端错误；明确选择既有交互/正式模式并按后端规则启用按钮；展示失败原因 |
| 场景身份与证据 | 场景 session ID 使任务列表整体被 typed client 拒绝；交互结果仍套正式纯净检查 | 精确支持既有 scenario session 命名；证据按 canonical mode 校验，交互结果不声明正式有效，默认正式校验拒收交互结果 |

记忆排序回归用朴素全量 MMR 作独立对照，验证不同 lambda 下顺序一致和输入顺序不影响摘要；不是用新的实现反过来生成预期值。

## 真实启动入口

隔离状态位于 `.tmp/ux-audit-20260905/`，没有操作用户既有任务。主 Web 为 `http://127.0.0.1:18741`，API 为 `18740`；额外场景测试使用 `18743` / `18742` 及另一份状态。

```powershell
$env:ZYRA_CLI_STATE_DIR='G:\agent-zoo\zyra\.tmp\ux-audit-20260905\cli'
$env:ZYRA_STATE_ROOT='G:\agent-zoo\zyra\.tmp\ux-audit-20260905\runtime'
node --env-file=.env.deepseek.local apps/cli/dist/zyra.js ui --base-url http://127.0.0.1:18740 --web-port 18741 --open=false
```

现有 `.env.deepseek.local` 只用于已授权的真实模型测试，未打印或更改凭据。最初普通沙箱请求遭 `EACCES`，取得限定现有测试的执行权限后，真实 DeepSeek 请求通过。浏览器原控制工具没有可用浏览器，随后使用独立 Playwright Chromium；不存在仍在等待浏览器授权的阻断。

## CLI 实际操作

- 真实 80×24、120×40 ConPTY 启动、输入、补全筛选、超过一页的选择、模型/推理强度、帮助和返回；每个尺寸十帧，终端状态机还原后 12 条屏幕断言通过。
- 真实两轮：`task_4aa732440e7e` 记住“青松七号”，`task_2dbb812954aa` 不提示标记地追问，答案仍为“青松七号”；同一 `session_01a06fe41b6d000_8bba1ccfe7d9ada30595`，两轮 completed。
- 真实文件任务 `task_78b4eb4ed242` 使用 file_write/file_read 写读隔离目录的 `ux-smoke.txt`，进程外读取确认为 `ZYRA_UX_FILE_OK`；随后实际打开 diff、tools、plan、verification、artifact 列表和完整预览。
- 22 个命令的实际启动/返回检查：`/pwd`、`/doctor`、`/status`、`/model status`、`/mode`、`/plan`、`/tools`、`/verification`、`/agents`、`/context`、`/memory`、`/skills`、`/mcp`、`/permissions status`、`/raw`、`/export cli-audit.md`、`/sessions`、`/resume`、无效命令、`/new`、新会话 `/rename`、无任务 `/context`。45 帧，退出码 0；导出文件实际生成，885 字节。
- 结束任务的只读命令修复后，再次真实启动并检查 `/memory /skills /mcp /context /diff /tools`，13 帧、退出码 0。记忆返回真实数据；技能/MCP 返回真实空状态。
- 首次脚本连续输入曾出现 `^[/tools`。后续 11 帧详情返回、45 帧命令检查、13 帧只读复查未复现；不声称定位了该偶发输入的根因。

原始记录、还原屏幕、隔离文件都在 `.tmp/ux-audit-20260905/cli-*`；未把每一帧都声明为自动断言。输入真实 Unicode 文本不等于人工 IME 候选窗测试。

## Web 实际用户旅程

| 入口 | 实际操作与结果 |
|---|---|
| 首页/命令 | 四类起步入口、中文/emoji/多行、帮助、状态、完整命令 Enter、Esc 保留草稿；新任务立即显示；最近会话搜索/选择 |
| 对话/恢复 | “山茶六号”两轮真实模型问答，`task_59864f81cc8e` 追问正确；刷新后上一轮和最终回答仍在，无重复答案 |
| 停止/继续 | `task_acfa35f9ccaf` 停止后立即恢复输入，等待一分钟刷新仍停止，显式继续后 completed；新回归检查迟到成功和迟到失败均不能覆盖取消 |
| 排队/编辑 | 两条排队后取回甲条并保留中文草稿，乙条仍排队；停止前轮后乙条实际回答 QUEUE_B；最终复验 `task_59b8d7d8142e` 返回 QUEUE_FINAL，编辑草稿在自动换轮、完成、刷新后保留，补全数为 0 |
| 交付物 | 独立页面、回对话、JSON 预览、深链接刷新；证据中搜索、bookmark、pin；实际下载 413 字节 Physical-MaAS-memory-continuity-result.json |
| 拓扑/长程/计划 | 展开、节点搜索/无命中/清空/fit，7 个拓扑节点；计划依赖按顺序显示；长程刷新及没有目标时的禁用状态 |
| 权限/工作区终端 | 实际 Create PTY → 请求 → 允许一次 → 创建成功；再次审批精确输入，PowerShell 输出 `ZYRA_WEB_PTY_OK_中文` 后 exit 0，PTY 输出游标 580。创建和输入用了不同的实际请求 |
| 时间线/因果 | 展开恢复时间线，145 行事件的搜索与过滤；实际 Inspect 因果记录 |
| 子代理/MCP/技能 | 真实空状态、筛选、范围切换、关闭和恢复 viewer；没有伪造子代理或连接器数据 |
| 差异/浏览器记录 | 页面实际展开与空状态；本任务无 Web 可审查 patch、无浏览器 worker 会话，不声称执行 apply/reject 或浏览器控制 |
| 会话/上下文/记忆 | Inspect context、Preview compact；真实记忆查询连续两次找到 12 条，约 1.2～1.3 秒；中文无命中也正常返回；已结束任务 Export run 生成实际 artifact_9d5b17d2103e |
| 系统/场景/实验 | 状态检查、清历史数量立即更新；正式模式的脏状态拒绝显示说明；交互短场景执行及证据结果另见下方；实验没有正式数据，检查真实空状态及刷新 |
| 窄屏/断线/弹层 | 390×844 下导航开关、首页与中文草稿，页面宽度 390 无横向溢出；离线提示、恢复联网和刷新保留草稿；嵌套 Esc 一次只关一层 |

浏览器 `pageerror` 在最后一轮为 `[]`。导航、停止和故意断网产生的取消请求不是成功网络请求，也不被算作应用异常。截图和页面文本保留在 `.tmp/ux-audit-20260905/web/`；最终包括 29 下载、31 记忆结果、32/33 队列、34 对话。

提交后重启保留服务时又复现就绪误报：`/health` 和任务读取成功，完整 `/runtime/readiness` 实测 12.454 秒返回 ready=true，而 Web 的 12 秒截止时间提前报不可用。将就绪探测独立期限设为 30 秒，保留后端真实 ready 判定；真实浏览器刷新后显示“运行时就绪”，QUEUE_FINAL 回答仍可见。对应 workbench/runtime 配置回归 43 pass、0 fail，Web 构建通过。

场景首次真实执行 `scenario_d537347cae684bcb85940fa949dbc275` 因 `preflight_clean_binding_invalid` 在证据阶段失败，不能记作成功。其相关取消仅针对本轮创建的失败任务，随后正式 daemon stop 检查 active_task_ids 为空并正常停止，没有强杀或清理用户记录。

重启复验还发现固定 foundation worker ID 错误复用旧 PID 的身份；改为每个 API 进程注册独立身份，不刷新或冒领旧 worker。最终真实 `scenario_b37534bd368e435dab52b4756cd3c07d`：succeeded，4.6 秒、46 个有效步骤、0 个无效步骤、1 个产物、证据 verified；随后点击 Verify evidence 与 Archive。截图 `35-scenario-success.png` 明确保留“交互检查、不构成正式验收证据”的标识。

该 foundation 场景检查 owner 链路并注入需求变化/故障，关联任务 `task_6966385c6ed9` 保留 `needs_revision`，不能将场景证据通过等同于交付任务已完成。已在真实任务列表确认这个状态；保留记录供查看。

## 自动化验证

- `bun test ./apps/web/test ./apps/cli/test ./packages/commands/test ./packages/memory/retrieval-algorithms/test`：555 pass、0 fail、3078 expect，52 文件，93.71 秒。
- 最终输入/场景增量回归：`bun test ./apps/web/test/workbench-shell.test.tsx ./apps/web/test/product-frontstage.test.ts ./apps/web/test/scenario-runner-workbench.test.ts`：61 pass、0 fail。
- `bun test ./packages/core/typed-api-client/test ./packages/core/typed-api-client/src`：31 pass、0 fail。
- 最后场景失败说明与会话身份变更的增量检查：52 pass、0 fail；Web 再次构建通过。
- `bun run build:web`（含 TypeScript 检查）、`bun run typecheck:cli`、`bun run build:cli`：通过。
- Python 记忆、排序端口、索引基础/集成、Web HTTP/WS 代理、PTY 单元回归：42 passed。
- `pytest tests/integration/test_worker_pool_api_main_path.py tests/integration/test_retrieval_api_main_path.py -q ...`：39 passed，290.12 秒；包括真实本地 HTTP 的取消/迟到结果竞争回归。
- `pytest tests/scenarios/test_scenario_runner_foundation.py -q ...`：9 passed，包括交互模式不得通过默认正式证据校验。
- `pytest tests/integration/test_scenario_runner_api_main_path.py -k 'not live_software' -q ...`：8 passed、1 deselected；`pytest tests/integration/test_cli_noninteractive_foundation.py -k 'scenario_calls_existing_http_lifecycle_directly' -q ...`：1 passed、7 deselected，实际执行 CLI/HTTP 的正式短场景链路。
- `node --experimental-strip-types --test packages/runtime/runtime-event-spine/test/runtime-event-spine.test.ts`：20 passed。
- 前一提交的真实 ConPTY 集成：3 passed；会话/历史/物理 worker 集成：23 passed；产品工具投影：8 passed。它们的范围不等于外部 provider 全量测试。

pytest 使用本轮工作区 `--basetemp` 和 `-p no:cacheprovider`；需要 tempfile 的测试将 TMP/TEMP 指向隔离目录。初次默认临时目录遇到访问拒绝，换用工作区后通过。曾出现的新回归断言错误（取消 resume 的 changed 标记、记忆重复查询、可选 degraded 字段、异常详情属性）均修复或按真实契约校正后重跑；没有忽略失败来报通过。

扩展运行 `pytest tests/scenarios/test_scenario_runner_foundation.py tests/scenarios/test_m2_s05_02_live_scenarios.py ...`：15 passed、3 failed。三个失败分别为 software delivery、research delivery、cross-domain comparison，均在 `DualDomainScenarioExecutor` 执行阶段报 `live_analysis_owner_unbound`；旧 `LiveOwnerHarness` 没有提供当前实现要求的 `bindings.analysis`。检查确认这个测试文件和 `dual_domain.py` 相对 HEAD 均无改动，失败发生在本次修改的证据收集之前。本轮没有修改这套正式 2,000 步测试桩，也没有将这三个失败隐藏在通过总数中；该测试桩与正式长程证据校验需要后续专门处理。

## 边界

未执行：正式 2,000+ 步双域场景/消融实验、8 小时 soak、clean-install、其他操作系统、人工 IME 候选窗、无现成数据的 MCP OAuth/技能执行/子代理消息/浏览器 worker 控制/Web patch 应用，以及每一个 CLI 有状态命令的完整后端流程。真实权限已覆盖 Web PTY 的申请、单次允许和执行；没有把 CLI 文件任务的自动放行说成 CLI 审批弹窗测试。

本轮已对现有页面入口进行真实浏览器操作，对可执行的主用户旅程进行真实后端验证；空状态和未配置的能力如实保留。这里的验收不代表已达到 Codex 的全部成熟度，也不代表所有可能后端状态均已穷尽。


## Web 六项体验修复与黑白灰主题（2026-09-05，第二轮）

本轮只修改 Web 前端、Web API 适配及相应测试。现有 API `127.0.0.1:8000` 的进程始终为 PID 87624，启动时间 16:53:15；没有重启服务、改动现有用户任务或修改 CLI。使用独立前端 `127.0.0.1:18745` 代理现有 API，在实际 Chromium 中操作，桌面 1440×1000、手机 390×844。

| 原问题 | 修复与实际验证 |
| --- | --- |
| 历史回答恢复时出现误导的完成提示 | 完整回答请求具有加载、失败和就绪状态。浏览器对旧历史 GET 注入延迟时显示“正在加载回答”，注入 503 后显示“回答加载失败”；解除故障并点击“重新加载回答”后恢复两轮真实回答。没有把故障注入记作真实服务故障。 |
| 字号和对比度 | 正文默认 16 px，可选择 14/16/18 px，浏览器保存并即时应用；辅助说明用较小灰字。设置事实标签从 10 px 浅色改为 12 px #666，值为 14 px #303030；实测大字选项刷新保留，手机无横向溢出。 |
| 内部记录伪装成交付物 | 只将明确的 CodeWorker manifest、memory continuity 等已知运行记录移到证据分类；未知产物及 internal 安全标签的用户文件仍显示。真实短对话显示 0 个交付物、2 条运行记录；全部产物面板仍可查看两个原始 JSON。 |
| 日常设置不足 | 默认模型来自 `/providers/models?available_only=true`，思考强度来自该模型实际支持列表；保存后仅影响此浏览器之后提交的任务和追问。真实提交捕获 `deepseek / deepseek-v4-flash / low`，后端 `product_execution_config` 一致。队列回归覆盖固定模型及自动选择均保持提交时设置。权限面板可按任务打开，保留后端权限策略和单次审批机制。连接/历史、场景/实验分到独立页签。 |
| 会话与复制操作缺失 | 真实剪贴板核对回复 Markdown 和代码 `hello`；会话名称通过后端 `/rename` 保存，修复 `task:task_*` owner 键被规范化别名替代的问题。仅对本轮任务重命名为“Web 验收 · 黑白灰主题”，刷新仍保留。归档仅影响当前浏览器列表，刷新保留且恢复成功，不删除记录或停止任务。 |
| 主页面和高级面板割裂 | 统一黑白灰底色、文字、按钮和表单对比度，移除 CSS 绿色及其他彩色强调；主对话、设置、详情、证据使用同一主题。运行标识/事件/计划默认折叠，核心按钮、场景表单、权限和产物入口改为中文；原始协议标识和产物标题保留。手机导航、菜单、Escape 关闭与恢复入口经过实际操作。 |

真实模型样例：

- `task_fbfd1d818146`：通过 Web 提交“请直接回复一句问候，并将 hello 放在 Markdown 围栏中。”，25 秒完成，6/6 步骤，复制回复与代码逐字核对成功。页面刷新后回复保留。
- `task_fdfcf39d0670`：包含“不要读取或修改文件”等否定要求的代码展示样例被现有后端分类为 `workspace_change`，约 4 分 13 秒后物理 worker 返回 incomplete layer 1，任务 failed。浏览器真实显示失败；曾打开停止确认但选择返回，没有执行取消。它不能计作成功样例。原有后端的意图分类与物理执行问题保留在本记录中，本轮未为此重启或修改运行服务。

最终截图在 `.tmp/ux-audit-20260905/`：`web-polish-final-answer.png`、`web-polish-final-settings.png`、`web-polish-final-mobile.png`、`web-polish-final-records.png`；延迟/失败截图为 `web-polish-history-loading.png` 和 `web-polish-history-error.png`。浏览器 `pageerror=[]`，手机文档宽度与视口均为 390。

验证结果：`bun run typecheck:web`、`bun run build:web` 均通过；`bun run test:web` 最终 341 pass、0 fail、1984 expect，31 文件，44.80 秒。早期检查发现并修复了提取公共显示函数时遗漏的常量引用、中文化后的旧文案断言，以及权限组件的显示值误传；最终版本全部重跑通过。新增回归覆盖历史失败重试、队列模型配置固定、存储失败、归档恢复、产物分类、模型目录以及控制命令 session owner 键。构建产物位于被 Git 忽略的 `apps/web/dist`。

本轮未重新执行 CLI、Python 后端全量回归、正式 2,000 步场景、消融实验或长时间 soak；真实场景/实验页检查了目录、模式选择、只读状态和空状态，没有启动正式长程实验。
