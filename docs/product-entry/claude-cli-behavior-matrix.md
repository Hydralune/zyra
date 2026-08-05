# FE-G00 Claude Code CLI 行为参考矩阵

## 1. “学习”的执行含义与参考边界

这里的“学习 Claude Code CLI”不是阅读结束即完成，而是：

1. 读取交互输入、队列、流式渲染、权限等待、长会话、headless I/O 和失败恢复的实际源码；
2. 抽取可测试的产品行为和不变量；
3. 映射到 Zyra 已有 API/event/permission/command owner；
4. 对每项明确 `ADOPT`、`ADAPT` 或 `REJECT`；
5. 将采纳项写入后续 slice 的测试 contract，并通过真实 Zyra 主路径验证。

参考仓库：`G:/agent-zoo/claude-code-best`，Git 基线
`c57f5a29e88e9a814bea47abeb9a0a6f725dc102`，package
`claude-js@1.0.0`。

该仓库的 `CLAUDE.md` 明确说明它是逆向/反编译整理版本，存在约 1341 个 TypeScript 错误、缺少完整测试且含 stub/feature flag。故它只能是**行为参考和风险样本**，不能成为 Zyra runtime dependency、源码复制来源或 contract authority。

裁决术语：

- `ADOPT`：行为不变量可原样采用，但仍由 Zyra owner 实现；
- `ADAPT`：采用用户体验目标，按 Zyra contract/state owner 改写；
- `REJECT`：与 Zyra owner、安全、输出或产品形态冲突；
- 本 Gate 不复制 Claude 代码，不新增依赖，不改 API/schema。

## 2. 源码证据索引

| 行为 | Claude 参考位置 | 观察到的机制 |
|---|---|---|
| 队列 | `src/utils/messageQueueManager.ts:45-79,126-183,216-241` | frozen snapshot；`now > next > later`；同优先级 FIFO；user input 默认 next、notification 默认 later |
| React queue bridge | `src/hooks/useCommandQueue.ts:1-15` | `useSyncExternalStore` 消费稳定 snapshot |
| 队列展示 | `src/components/PromptInput/PromptInputQueuedCommands.tsx:1-116` | idle 过滤、任务通知折叠、可编辑输入与系统通知区分 |
| 输入 | `src/components/PromptInput/PromptInput.tsx:23-117,254-366` 及键盘处理区 | 多行、history/search、slash/typeahead、paste/image/ref、queued draft 回填、external editor |
| 主交互 | `src/screens/REPL.tsx:893-1150,1281-1510,2588-2635,3321-3540,4201-4560` | query guard、stream preview、permission footer、scroll/re-pin、远程流、bounded transcript、search、queue while busy |
| 非交互 | `src/main.tsx:797-883,968-1000,1818-1862` | `-p` 或 stdout 非 TTY 进入 headless；text/json/stream-json；stdin pipe 与格式组合校验 |
| stdout guard | `src/utils/streamJsonStdoutGuard.ts:14-109` | 每行 JSON 校验；非 JSON stdout 转 stderr；shutdown 处理 partial line |
| pipe | `src/utils/process.ts:1-33,45-67` | EPIPE 安静销毁；stdin data/end 与 bounded timeout |
| headless lifecycle | `src/cli/print.ts:589-595` 及 stream/permission/shutdown 分支 | stream-json guard、permission wait 中继续发事件、结构化失败和退出 |

行号只用于本次基线审计；后续不应在运行时依赖工作区外参考仓库。

## 3. 七项行为矩阵

| # / 问题 | Claude 参考行为 | 必须保留的不变量 | Zyra owner / contract | Zyra CLI 行为 | TTY / non-TTY | 失败与恢复 | 裁决 / 原因 | slice / 测试 |
|---|---|---|---|---|---|---|---|---|
| 1. 丰富输入：如何在长程任务中输入多行 goal、命令和大段上下文而不误提交？ | PromptInput 支持多行、首/末行 history、slash/typeahead、paste 归一化、大 paste/image ref、queued draft 回填与 external editor | 输入编辑状态是本地临时状态；提交后以 request/task receipt 为准；大内容用 artifact/ref，不把整块文本反复塞入 event | CLI input controller；提交走 `POST /tasks` 或 `POST /tasks/{id}/commands`；artifact 走 artifact contract | 多行编辑、history/search、slash completion、paste 归一化、artifact ref、可选 `$EDITOR`；未提交 draft 可恢复 | TTY 启用；non-TTY 从 arg/`-f`/stdin 读取，不启动 editor，也不输出提示符 | paste/read/editor 失败保留 draft；malformed ref 不提交；断线时未提交 draft 仍在，已提交以 receipt 重查 | `ADAPT`：采用交互能力；拒绝 Claude 本地 message/history 成为 canonical state、拒绝专属 image/paste wire format | FE-S02；multiline/history/large-paste/artifact-ref/editor-failure/queued-draft tests |
| 2. 输入队列：运行中继续输入时如何保证优先级、FIFO 和可见性？ | module-level queue + frozen snapshot；`now > next > later`，同优先级 FIFO；editable user input 与 notification 分离 | 已提交 queue 的顺序/状态/receipt 只由服务端拥有；本地只可保存尚未提交意图；取消必须走 API | `@zyra/commands` + `POST .../commands`、`GET .../command-queue`、`POST .../cancel` | 显示 now/next/later；提交后用服务端 request_id/receipt 替换本地 item；可将尚未提交 editable item 弹回 draft | TTY 显示队列；non-TTY 每个 admission/receipt 为 JSONL，不做隐式重排 | 409 revision conflict 显式 rebase/retry；网络未知结果先按 idempotency 查 receipt；取消冲突不本地删除 | `ADAPT`：采用优先级/FIFO/稳定 snapshot UX；拒绝 module-level queue 成为 canonical owner | FE-S03；priority/FIFO/replay/409/cancel/unknown-outcome tests |
| 3. 流式输出：如何保持可读、低熵，又不因重绘破坏日志/管道？ | REPL 增量 preview、ephemeral progress replacement、tool output 收敛；headless stream-json | event 顺序、cursor、generation 和 canonical state 来自 event ingress；transcript 只投影；结构化 ref 优先于重复正文 | event-ingress capabilities/snapshot/delta/SSE；runtime-event-spine；artifact refs | line transcript + 单行 bottom status；progress 原位语义更新但历史有效事件不丢；工具结果显示摘要+artifact/evidence ref | TTY 行式 transcript；non-TTY stdout 纯 JSONL、diagnostic 到 stderr | gap/generation change 取 snapshot；malformed event 为 contract error；不能用 repaint/heartbeat 计有效 step | `ADAPT`：采用 incremental/low-entropy；`REJECT` Ink/React fullscreen、alternate screen、逐字符动画和 Claude event schema | FE-S02/S05；ordering/progress-replace/artifact-ref/JSONL-purity/gap tests |
| 4. 权限等待：如何让用户明确知道“为什么停住”，又不让前端夺取裁决权？ | sticky permission request/footer；query 流可在等待时继续输出；交互 answer 回到 query | permission runtime/custody token 是唯一 owner；身份、revision、deadline 均需 echo；sealed/competition fail closed | `POST /permissions/sessions/open`、`POST /permissions/sessions/{session_id}/resume` 及 summary/request/resolve endpoints | transcript 显示 tool/action/risk/reason/deadline；可打开详情；allow/deny 提交后等待 canonical decision receipt | TTY 可交互裁决；non-TTY 遇人工请求输出 pending permission JSONL 并 exit 4，除非已有合法自动规则 | 401/403/408/409/410/422 原样映射；断线恢复 pending request；过期不得重新本地批准 | `ADAPT`：采用 sticky wait；`REJECT` `--dangerously-skip-permissions`、本地 permission owner、fail-open | FE-S03/S05；allow/deny/timeout/expired/conflict/reconnect/sealed tests |
| 5. 长会话：数十/数百行后如何搜索、滚动、恢复并保持“仍在运行”的感知？ | user-scroll window 防自动 re-pin；unread/sticky bottom；bounded live transcript；search n/N；将旧内容转 native scrollback/外部查看 | 不能因内存裁剪丢 canonical event；搜索/滚动是 projection；恢复依赖 cursor/snapshot；heartbeat 不计有效 step | event ingress + task store + artifact/event persistence；CLI viewport 只拥有 UI state | 默认 follow；用户滚动后暂停跟随并显示 unread；home/end/search；只虚拟化渲染，旧事件仍可按 cursor/ref 取回 | TTY 启用 scrollback/search；non-TTY 不裁剪 stdout JSONL 流 | resize/search cancel 不影响 task；断线保留 viewport/draft，重连后 cursor 补齐；cursor gap snapshot | `ADAPT`：采用 follow/unread/search/virtualization；`REJECT` 本地 transcript 作为恢复源、用 UI heartbeat 假装进度 | FE-S02/S05/S06；80/120 列、long-run、search、scrollback、reconnect tests |
| 6. TTY / non-TTY / pipe / JSONL / exit：同一命令如何既适合人也适合自动化？ | `-p` 或 stdout 非 TTY headless；text/json/stream-json；input/output 组合校验；stdout JSON guard；EPIPE handler；Claude 多数只用 0/1 | stdout machine contract 绝不混入非 JSON；stderr 承载诊断；pipe 断开不污染/伪失败服务端 task；退出码稳定 | CLI I/O adapter + typed API；服务端 task 独立存在 | `zyra run` 默认 JSONL；可从 stdin/file；每行 event/receipt/error 有 schema/type/task/cursor；SIGINT 先取消本地订阅，显式选择才发 task cancel | TTY human；stdout 非 TTY 自动 strict JSONL；stdin 非 TTY bounded 等待并可显式禁用 | EPIPE 安静停写；partial JSON 不输出；连接失败 exit 2、runtime 3、permission 4、contract 5 | `ADOPT` stdout/stderr 隔离和 EPIPE；`ADAPT` 格式与信号；`REJECT` Claude 0/1 退出语义和 workspace-trust bypass | FE-S01/S06；pipe/head/tee/slow-reader/EPIPE/SIGINT/exit-code tests |
| 7. 失败与重连：网络断开、远端继续运行、cursor 失效时前端如何不撒谎？ | REPL 有 remote stream、loading/disconnected 状态和 query guard；流/队列可恢复；失败显式呈现 | API timeout 不等于请求未执行；task 与 daemon 独立于 listener；已确认 cursor 才可前移；gap 后 snapshot；draft 不丢 | typed client receipt/idempotency journal + event ingress + daemon process supervisor | 显示 connected/degraded/disconnected/recovering；指数退避有上限；未知写结果先查 receipt；恢复后标明补齐区间 | TTY 状态行+system transcript；non-TTY JSONL connection/recovery records，stderr 提示 | generation change、invalid/expired cursor、401/403、server 503、daemon restart 分别处理；不得从屏幕文本重建 state | `ADAPT`：采用显式状态和保留 draft；`REJECT` fail-open、本地重放为事实、断线即取消 task | FE-S03/S05/S06；disconnect-mid-write/cursor-gap/generation/503/restart tests |

## 4. 采纳项的 Zyra 化规则

### 4.1 输入不是 runtime

Claude 的输入体验值得参考，但 Zyra 的 goal、command、artifact 和 permission 已有各自 owner。Zyra 只保留以下客户端临时数据：

- 当前 draft、光标、history view、未提交 queue item；
- artifact/ref 的本地选择结果；
- viewport、search 和 unread 计数。

一旦提交，客户端必须用服务端 task/request/receipt identity 替换本地临时 identity。

### 4.2 两层队列，单一事实源

```text
local unsent intent queue
  -> POST /tasks/{task_id}/commands
  -> canonical runtime command queue
  -> receipt/event projection
```

本地层可借鉴优先级与可编辑交互；服务端层由 `@zyra/commands` contract 和 runtime 决定 admission、ordering、revision、cancel 和 recovery。两层不得用相同字段暗示同一 owner。

### 4.3 Line transcript，不采用 alternate screen

Zyra 要求可审计、可复制、适配 Windows 和长程证据，因此固定采用普通终端 scrollback：

- 一条有效 event/receipt 对应稳定 transcript record；
- 只有 bottom status 和尚未形成事实的 progress 可重绘；
- tool stdout/stderr 大块内容折叠为摘要 + artifact ref；
- 不清屏、不占用 alternate buffer、不逐字符动画；
- 终端关闭不影响 daemon/API/task。

### 4.4 Strict JSONL

Claude 的 stdout guard 说明了一个关键风险：任何依赖或 debug 的 `console.log` 都能破坏自动化。Zyra 应在两层防守：

1. architecture：所有 human 输出只写 stderr，machine record 只由一个 JSONL writer 写 stdout；
2. runtime guard：完整行 JSON parse 失败则转 stderr 并产生可测 guard marker；shutdown 不输出 partial JSON。

JSONL record 至少包含 `schema`、`type`、`timestamp`、`task_id/request_id`（适用时）、`cursor/generation`（事件时）、`payload`。不得包含 capability token、custody token、credential、真实 root/cwd。

## 5. 明确拒绝清单

| Claude 机制/形态 | Zyra 裁决 | 原因 |
|---|---|---|
| 整套 Claude query/runtime/session loop | `REJECT` | 会形成第二套 Agent 与 state owner |
| Ink/React fullscreen、alternate screen、字符级重绘 | `REJECT` | 与可审计 scrollback、Windows/pipe 和 D5 长程压力目标冲突 |
| module-level queue 作为已提交命令事实源 | `REJECT` | runtime queue 已是 canonical owner |
| 本地 transcript/session 文件作为恢复真相 | `REJECT` | 必须用 task store + signed cursor/snapshot |
| `--dangerously-skip-permissions` 或 workspace trust bypass | `REJECT` | 违反 permission/zero-human/fail-closed 边界 |
| Claude stream-json/message schema | `REJECT` | Zyra 必须输出自己的 event/receipt schema |
| 仅 0/1 退出码 | `REJECT` | 不足以区分 usage、transport、runtime、permission、contract |
| 直接复制反编译源码或把仓库加入 release dependency | `REJECT` | 来源质量、许可证/维护风险与离线发布边界不成立 |
| 复用其 UX 测试思想 | `ADAPT` | 可转成 Zyra 主路径行为测试，但不能照搬 fixture 证明 |

## 6. D5 长会话压力验收基线

以下测试集由 Gate 冻结。FE-S02 已实现其中的交互输入、服务端 snapshot/SSE、行式
transcript、有界搜索窗口、follow/unread、80/120 列重排、tool progress 折叠、pipe
兼容和 resume 主路径；FE-S03 已实现服务端 command queue、permission custody、cursor/gap
恢复和 daemon stop 保护；FE-S04 已实现真实同机 terminal listener、注册、typed action、
失效转移和退出 disable；FE-S05 已建立 Web 产品/证据分层、跨入口一致性和 `zyra ui`
launcher。FE-S06 的最终 cleanroom 仍保持后续边界。真实 daemon/terminal/Web 证据不能由
mock transcript 或静态成功卡片替代。

| ID | 场景 | 输入/故障 | 必须观察到的结果 | 禁止结果 | 归属 |
|---|---|---|---|---|---|
| D5-01 | 80 列窄终端 + 长输出 | 80-column TTY；真实 task 产生至少 120 个可见 event | 换行/截断规则稳定、顺序完整、bottom follow、最后 cursor 可恢复 | 横向溢出、丢行、清屏、alternate screen | FE-S02 |
| D5-02 | 120 列终端 + 回看 | 120-column TTY；输出到中段时向上滚，继续产生 event | viewport 不被强制拉回；unread 增长；End 后回到底部 | 用户滚动期间自动 re-pin | FE-S02 |
| D5-03 | 长工具输出 | stdout/stderr 大块输出 + artifact | transcript 低熵摘要，完整内容由 artifact ref 可取 | 终端塞满重复正文、artifact 丢失 | FE-S02 |
| D5-04 | 运行中连续输入 | next/later/now 混合，含可编辑未提交项 | 本地意图和服务端 receipt 可区分；服务端 FIFO/priority 正确 | 本地 UI 重排冒充服务端排序 | FE-S03 |
| D5-05 | permission 等待 | 第 40 行出现请求，之后仍有系统事件 | sticky 等待、详情/截止时间可见；non-TTY exit 4 | 自动 allow、假死、吞掉后续事件 | FE-S03 |
| D5-06 | 中途断线重连 | 断线时服务端继续产生 30 个 event | 从最后确认 cursor 补齐；无重复有效记录；显示 recovering | 断线即取消 task、从屏幕文本猜 state | FE-S03/S05 |
| D5-07 | cursor gap / generation change | 服务端拒绝旧 cursor | 明确 gap，取 snapshot，重建 projection 后继续 | 静默跳过或无限重试旧 cursor | FE-S03 |
| D5-08 | transcript 搜索 | 120 行中多处匹配 | n/N 导航、退出搜索后 viewport 稳定 | 搜索改变 canonical event | FE-S02 |
| D5-09 | pipe / tee / head | 将 `zyra run ...` 管道到 `tee`，或消费者提前关闭 | stdout 全是合法 JSONL；EPIPE 安静；task 不被隐式取消 | banner/debug 混入 stdout、partial JSON | FE-S01/S06 |
| D5-10 | resize / narrow terminal | 长 run 中反复 resize | 布局退化但记录 identity/cursor 不变 | 重复提交、丢 permission、状态 owner 漂移 | FE-S02 |
| D5-11 | daemon/CLI 分离 | CLI listener 退出，daemon/task 继续 | 新 CLI 可 resume；daemon generation 不变 | 退出 interactive 顺带杀 daemon/task | FE-S01/S06 |
| D5-12 | sealed 长程 | permission/terminal exclusion/fault recovery | zero-human、确定性拒绝/replan、真实 receipts | 手工点击补救、terminal 被选作 sealed worker | FE-S04/S06 |

通过标准：所有测试必须走真实 typed API/event/runtime/worker 路径；mock、预录 transcript、fixture 或 UI repaint 不能替代 receipt/cursor/dispatch/permission 证据。若任何 D5 测试失败，应修订对应 parent/slice 的设计约束，不能只调渲染参数掩盖。

### 6.1 FE-S02 实际转化结果（2026-08-04）

| Claude 参考行为 | 裁决 | Zyra 独立实现 | 可执行证据 |
|---|---|---|---|
| PromptInput multiline、首末行 history、长 paste ref、slash/ref completion、external editor | `ADAPT` | `apps/cli/src/input/` 的非权威 `PromptDraft`/`PromptHistory` 与安全 editor 子进程；提交才展开 paste，取消草稿可恢复 | `apps/cli/test/interactive-session.test.ts` multiline/paste/history/completion/cancel tests |
| REPL ephemeral progress replacement | `ADAPT` | 只折叠连续 `runtime.tool.progress`；有效 settled event 仍追加，完整输出以 digest/artifact ref 表示 | tool-progress folding test + 真实 daemon tool called/succeeded snapshot |
| sticky follow、unread、recent search budget、resize 后重排 | `ADAPT` | `SessionProjection` 只持 viewport/search 状态；512 条 recent search budget 不改变 server revision/cursor；resize 清本地 search query | 2,101-transition pressure、follow/unread/search-disable/resize tests |
| native terminal scrollback + small footer | `ADOPT/ADAPT` | `LineTranscriptRenderer` 逐行追加；仅 TTY 当前状态行使用 erase-line；pipe 不输出 footer 控制 | 80/120 列、pipe、tool output、alternate-screen negative tests |
| Ink/React root、alternate screen、进程内 transcript owner | `REJECT` | 未引入相关依赖；renderer 的 `alternateScreenUsed` 恒为 false；resume 从服务端 snapshot/cursor 重建观察投影 | CLI dependency/build audit + real daemon resume test |
| Claude 自有 session/message wire schema | `REJECT` | 只消费 `zyra.event-ingress/v1`、`zyra.runtime-event/v1` 和 task-backed session resolver | strict schema/binding/generation/order/cursor negative tests |

FE-S02 的参考行为已经转化成 Zyra 自有代码和测试，不以“读过源码”作为完成证据。

### 6.2 FE-S03 实际转化结果（2026-08-04）

| Claude 参考行为 | 裁决 | Zyra 独立实现 | 可执行证据 |
|---|---|---|---|
| `messageQueueManager` 的 `now > next > later`、同优先级 FIFO 和稳定 snapshot | `ADAPT` | `CliControlSession` 复用 `@zyra/commands` 构造 request/receipt，但排序、sequence、取消和 retry target 只读取 `PromptQueueRuntime` 的 canonical queue | CLI queue 单测 + 真实 API priority/FIFO/cancel/retry 集成 |
| permission sticky wait、待决详情和输入区裁决 | `ADAPT` | `CliPermissionSession` 只持进程内 custody，展示 reason/risk/deadline；allow/deny 绑定完整 challenge proof 并等待 `typescript.PermissionCoordinator` receipt | proof/expired/disabled 单测 + CLI/Web deny/allow/restart 真实测试 |
| 断线状态、有限重订阅与草稿保留 | `ADAPT` | SSE 从最后 server cursor 有界退避；gap/generation/cursor conflict 重新 snapshot；renderer 用单一最大 sequence 去重，不保存第二份 transcript | cursor resume 与 gap snapshot replacement 单测 |
| module-level canonical queue、本地 permission owner、断线后从 UI 文本重建 | `REJECT` | 已发送命令、pending permission、revision、cursor 和终态均不落本地 owner；custody token 不持久化 | owner 检查、adapter-disable fail-closed、进程重启真实测试 |
| daemon/task 生命周期绑在当前 REPL | `REJECT` | `/exit` 只 detach 控制输入；daemon stop 查询活动 task，默认拒绝，force 产生持久 audit | 完整 CLI daemon 集成回归 |

FE-S03 采用的是 Claude CLI 的用户问题与交互不变量；没有复制其源码、wire schema、queue、
permission state 或 runtime dependency。

## 7. 赛题映射

| 参考行为的 Zyra 化 | 对赛题的实际贡献 |
|---|---|
| cursor/snapshot/reconnect + long transcript | 让 checkpoint、compact/restore、restart、需求变更证据在超长程运行中可追踪 |
| 低熵流式摘要 + artifact/evidence ref | 展示结构化通信、剪枝理由和证据链，不用日志体量冒充协同 |
| runtime-owned command/permission queue | 保持多 Agent 控制、revision、lease、zero-human 边界 |
| TTY/JSONL 双形态 | 同时支持人工演示和封闭自动化评测 |
| 明确 disconnected/degraded/recovery | 区分前端断线与后端任务终态，避免错误比赛证据 |
| line transcript + search/scrollback | 使动态拓扑、神经符号裁决、端边云 receipt 在同一 run 可审计 |

## 8. Gate 决定

- 是否需要继续学习 Claude Code CLI：**需要，但方式是定向参考 + 转化为 Zyra contract/test，不是继续无边界阅读。**
- 当前已完成七个目标行为的源码追踪、owner 映射和采纳裁决；这足以通过
  contract/reference Gate 并进入实现候选。
- 后续只在某个 slice 出现具体交互/恢复问题时回到对应源码点，不再把 Claude 仓库当总体蓝图。
- client/session 与 terminal contract 的 C-01/T-01..T-04 已由 FE-G00R 在提交
  `fd272ba` 收口；D5 真实执行按 parent 修订保留给 FE-S02/S06。
- FE-G00 的 contract/reference baseline 结论为 **PASS**；该 Gate 当时只确定 FE-S01
  候选，不曾以规格审计冒充 CLI 行为实现。
- FE-S04 已在独立授权下完成：CLI 复用 Zyra typed action 与 BackendRegistry HTTP contract，
  未复制 Claude session/permission owner，也未把 terminal 叙述成 edge。
- FE-S05 已在独立授权下完成：只参考 Claude CLI 的入口/恢复行为并转化为 Zyra contract，
  `zyra ui` 打开既有 Web，CLI 与 Web 仍是同一 runtime 的两个客户端；FE-S06 未被自动授权。
