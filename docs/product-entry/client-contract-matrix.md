# FE-G00 客户端契约矩阵

## 1. 文档身份与结论

- Gate：`FE-G00 Contract / Reference Baseline`
- 初始源码基线：`931deeff0cedd19a23da65f2c9f58be22c956315`
- 三份规格提交：`a9bc7c1aa1805059b25e713c50abef34598f7b8e`
- FE-G00R 实现验证提交：`fd272ba`（核心实现提交 `843ef9c`）
- 基线时间：`2026-08-04T08:45:42+08:00`
- 观测工具链：Node.js `v22.17.0`、Python `3.13.9`、Bun `1.2.15`（通过 `npx bun@1.2.15`；Bun 未加入 PATH）
- 包版本：`@zyra/typed-api-client@0.1.0`、`@zyra/commands@0.1.0`、`@zyra/web@0.1.0`
- 目标 CLI：TypeScript + Bun，位置 `apps/cli`，可执行名 `zyra`
- 当前事实：FE-S01 已建立非交互入口与 daemon process owner，FE-S02 已建立行式交互
  snapshot/SSE 观察，FE-S03 已接入 runtime-owned command queue、permission custody、
  revision/idempotency 与 cursor/gap 恢复；task-backed session resolver 继续由 FE-G00R 提供。
- Gate 结论：**PASS（contract/reference baseline）**；FE-S01 也已按用户独立授权
  完成。CLI 只新增客户端 transport、writer 和 pid/generation 状态，不改变
  task/event/scenario/command/permission/runtime canonical owner。FE-S04 仅为未授权的下一候选。

本文只冻结客户端如何使用既有 Zyra contract，不新增 API、schema 或状态 owner。

## 2. 权威来源与复用边界

| 来源 | 用途 | 裁决 |
|---|---|---|
| `packages/core/typed-api-client/src/protocol.ts` | HTTP method、path、成功状态、输入/输出 contract | 客户端 HTTP 唯一静态清单，直接复用 |
| `apps/web/src/api/client.ts` | receipt、cursor journal、retry、typed client 使用方式 | 抽为共享能力或复用；不得复制成第二套协议 |
| `packages/commands/src/**` | command queue、priority、receipt、recovery | runtime 为 canonical owner；CLI 只投递和投影 |
| `apps/api/zyra_api/main.py` 及分域 API | task、event、permission、artifact、scenario 状态 owner | 后端事实，不由 CLI/Web 重建 |
| `packages/runtime/runtime-event-spine/**` | 事件语义与 projection | CLI/Web 只消费，不成为 event owner |
| 根目录 `docs/phase2/README.md`、`docs/比赛要求追踪矩阵.md`、`docs/milestones/execution-state.yaml` | 已冻结赛题事实与当前授权 | 只读；Phase 2 已完成，当前 P2 slice 为空 |

固定规则：

1. CLI、Web、daemon 都是同一 control plane 的客户端，不拥有 task、session、permission、event、artifact 或 scenario canonical state。
2. 所有写请求都携带服务端 contract 要求的 idempotency/revision/custody 信息；409 不得自动覆盖。
3. 断线恢复以 signed cursor + generation 为主；gap/expired/invalid cursor 必须取 snapshot 后继续，禁止从本地 transcript 反推 canonical state。
4. interactive 与 non-interactive 共用同一 API adapter；区别只在输入、渲染、stdout/stderr 和退出码。
5. Web 降级为状态面、证据面和复杂审批面；CLI 是主入口，但不得隐藏 Web 已有的真实能力。

### 2.1 API 版本、schema 与 event type 绑定

所有请求沿用 typed client 的 `X-Zyra-Api-Version: 1.0`、`X-Zyra-Operation`、
`X-Zyra-Contract`、request/correlation/causation identity，以及 mutation 的
`Idempotency-Key` / receipt headers。客户端不得自行发明同义 schema 名。

| 领域 | typed contract/schema |
|---|---|
| health/readiness | `zyra.health.v1`、`zyra.runtime-readiness.v1` |
| task list/detail/mutation | `zyra.task-list.v1`、`zyra.task-detail.v1`、`zyra.task-mutation.v1` |
| session list/detail | `zyra.session-list.v1`、`zyra.session-detail.v1`；owner 固定为 `task_store_projection` |
| event legacy/ingress | `zyra.task-events.v1`、`zyra.event-ingress-capabilities.v1`、`zyra.event-ingress-snapshot.v1`、`zyra.event-ingress-delta.v1`、`zyra.event-ingress-sse.v1` |
| command | `zyra.task-control-command.v1`、`zyra.command-queue.v1`、`zyra.command-cancel.v1`；共享 queue/receipt protocol 另有 `zyra.control/v1`、`zyra.command-queue/v1`、`zyra.command-receipt/v1` |
| permission | `zyra.permission-control.v2` |
| artifact | `zyra.artifact-catalog.v2`、`zyra.artifact-read.v2`、`zyra.artifact-read-audit.v1` |
| scenario | `zyra.scenario-registry.v1`、`zyra.scenario-run.v1`、`zyra.scenario-evidence-manifest.v1`、`zyra.scenario-mutation.v1` |
| error | `zyra.error.v1` / `application/problem+json` |

event `type` 必须来自 `runtime-event-spine` catalog。CLI/Web 至少要原样识别并按
`stateDomain` 投影以下比赛关键族：

- task/session：`runtime.task.created|updated|completed|failed`、
  `runtime.query.admitted|queued|promoted`、`runtime.turn.started|completed|failed`；
- stream/tool：`runtime.text.started|delta|ended`、
  `runtime.tool.called|progress|succeeded|failed|cancelled`；
- topology/group：`runtime.node.created|updated|failed`、`runtime.topology.route`、
  `runtime.subagent.created|dispatched|progress|yield|completed|failed|cancelled`；
- permission/control：`runtime.permission.requested|pending|allowed|denied`、
  `runtime.control.requested|accepted|rejected|completed`；
- continuity/recovery：`runtime.compact.started|completed|restore`、
  `runtime.api.stream.retry|disconnected`、`runtime.recovery.requested|planned|completed`；
- dispatch/evidence：`runtime.backend.dispatch.requested|accepted|failed`、
  `runtime.backend.failover`、`runtime.artifact.committed|quarantined`、`runtime.heartbeat`。

上述竖线表示事件族枚举，不代表另造聚合 event。`runtime.text.delta`、reasoning delta、
heartbeat 等 non-effective/live-only 事件不得被 UI 计为有效长程 step。

## 3. 八个顶层命令的端到端矩阵

图例：`REUSE` 直接复用；`ADAPT` 只新增 CLI/共享 adapter；`CREATE` 需要新增客户端本地 owner；`BLOCKED` 缺少已授权 contract；`NO-EXPOSE` 不应成为用户入口。

| 用户旅程 / CLI 命令 | 精确 HTTP / event 路径 | TypeScript owner | 后端 owner | 幂等 / revision | cursor / recovery | permission | TTY / non-TTY | 失败语义 | 最低行为测试 | 暴露裁决 |
|---|---|---|---|---|---|---|---|---|---|---|
| 进入工作台：`zyra` | `GET /health`；`GET /runtime/readiness`；`GET /tasks?cursor&limit&status`；选中任务后使用 event-ingress 四件套 | 新建 `apps/cli` shell；HTTP 复用 `@zyra/typed-api-client` | API readiness、task store、event ingress | 只读；task projection 带服务端 revision | task list cursor；事件 signed cursor/generation；gap 后 snapshot | 展示 pending request；解决动作仍走 permission API | TTY 进入 line transcript；非 TTY 无 goal 时返回用法错误 | API 不可达显示 disconnected；readiness 503 显示 degraded，不伪装空任务 | 默认入口、空列表、readiness 503、cursor gap、非 TTY 空输入 | `ADAPT` |
| 快速发起：`zyra "<goal>"` | `POST /tasks`（`auto_run=true`）；随后 event-ingress capabilities/snapshot/delta/SSE | CLI task adapter | task API + runtime | 客户端生成 idempotency key；服务端 receipt 为准 | 首次 snapshot 后 SSE；断线从最后确认 cursor 恢复 | 运行中出现权限请求时显式等待 | TTY 转交交互 transcript；非 TTY 等价 `run`，stdout JSONL | 创建失败不产生伪 task；409 显示 receipt/conflict；断线不取消 task | task create、duplicate replay、permission wait、reconnect | `ADAPT` |
| 自动化运行：`zyra run <goal 或 -f>` | `POST /tasks`；event-ingress；必要时读 `GET /tasks/{task_id}`、artifact endpoints | CLI non-interactive adapter | task/event/artifact APIs | 创建 idempotency；不重放未知结果写请求 | cursor journal 落客户端运行目录；gap 取 snapshot | 默认不可交互：需要人工的动作返回明确 pending/exit，不自动允许 | stdout 只允许 JSONL；人类诊断到 stderr；支持 pipe/tee/EPIPE | 固定退出码 0..5；协议污染、断线、权限等待分别可判定 | JSONL purity、pipe/tee、EPIPE、SIGINT、exit code、cursor resume | `ADAPT` |
| 恢复：`zyra resume <task-or-session>` | task：`GET /tasks/{id}`、`POST /tasks/{id}/run`、event-ingress；session：`GET /sessions/{session_id}` | task resume 可复用；session 使用 typed resolver | task store；session 是只读 task projection，不是第二 owner | resolver 只读；run 请求需 idempotency；command 需 expected session revision | task 用 event cursor；session 仅在唯一候选时返回 `resume_task_id` | 保留后端 pending permission，不本地清空 | 两种模式均先输出 canonical task/session identity | task/session 不存在 404；多候选为 `ambiguous` 且不返回 resume target | task resume、重复 resume、session ambiguity/not-found/restart | `REUSE + ADAPT` |
| 列表：`zyra ls` | `GET /tasks?cursor&limit&status`；`GET /sessions?cursor&limit&status` | typed client + CLI list projection | task store；session 为 task-backed projection | 只读 | task/session cursor 分域；cursor scope/字段异常 fail closed | 只显示摘要，不把 permission 变成本地状态 | TTY 表格/短列表；非 TTY JSONL | API 失败不输出空成功结果；不能由本地历史补 session | pagination、filters、JSONL、cursor mismatch | `REUSE + ADAPT` |
| 场景闭环：`zyra scenario <ls/create/start/cancel/verify/evidence>` | `GET /scenarios/registry`；`GET/POST /scenarios/runs`；`GET /scenarios/runs/{id}`；`POST .../start`；`POST .../cancel`；`POST .../verify`；`GET .../evidence` | CLI scenario adapter；Web workbench 可继续复用 | `scenario_api` / scenario runner | create/start/cancel/verify 均携带 request identity/idempotency；revision conflict 不覆盖 | run 状态轮询或 task events；evidence 409 表示尚未形成 | sealed 场景沿用 runtime permission/zero-human policy | TTY 显示阶段；非 TTY 每次状态变化一条 JSONL | 409/422/503 保留 error code 与 retryability | lifecycle、duplicate create/start、evidence-not-ready、sealed rejection | `REUSE + ADAPT` |
| 打开状态面：`zyra ui` | 进程启动前 `GET /health` / `GET /runtime/readiness`；Web 自身继续走 typed API；**无 server-side UI lifecycle endpoint** | 新建本地 UI launcher；`@zyra/web` 仍拥有前端 app | API 不拥有浏览器/本地 dev server 进程 | launcher 使用 pid/port generation 防重；不写 runtime state | UI 重启后由 Web 自己用 cursor/snapshot 恢复 | 浏览器复杂审批仍调用 permission API | TTY 可打印 URL；非 TTY 输出结构化 launcher receipt | 端口冲突、子进程早退、API 未就绪需非零退出 | already-running、random port、child exit、API degraded | launcher=`CREATE`；API=`REUSE` |
| 常驻入口：`zyra daemon <start/stop/status>` | daemon 启动后探测 `GET /health` / `GET /runtime/readiness`；**无 daemon lifecycle HTTP contract** | 新建 CLI-owned process supervisor；只拥有本地进程与 pid/generation | Zyra API 仍拥有 runtime state | start/stop 以 pid+generation 幂等；旧 pid 不得杀新进程 | API/task 恢复仍由 server store + event cursor 完成 | daemon 不代替 permission owner | TTY human；非 TTY JSONL receipt | stale pid、zombie、startup timeout、stop timeout fail closed | double start、stale pid、generation fence、crash recovery | process=`CREATE`；runtime=`REUSE` |

## 4. 领域级 contract 矩阵

### 4.1 Health、task 与 session

| 领域 | 精确 contract | 成功状态 | canonical owner | 客户端责任 | 结论 |
|---|---|---|---|---|---|
| Health | `GET /health` | 200 | API runtime | 存活探测，不等于 readiness | `REUSE` |
| Readiness | `GET /runtime/readiness?wait_ms=` | 200、503 | API runtime | 503 呈现 degraded 和原因 | `REUSE` |
| Task list | `GET /tasks?cursor=&limit=&status=` | 200 | task store | 翻页、筛选、保留服务端 identity/revision | `REUSE` |
| Task get | `GET /tasks/{task_id}` | 200 | task store | 只投影 | `REUSE` |
| Task create | `POST /tasks`，body `{goal, auto_run, session_id?, worker_pool?}` | 201 | task API/runtime | 生成 idempotency，持有 receipt | `REUSE` |
| Task cancel | `POST /tasks/{task_id}/cancel`，body `{reason}` | 200 | task runtime | 显示 canonical resulting state | `REUSE` |
| Task run/resume | `POST /tasks/{task_id}/run`，body `{requested_by}` | 200 | task runtime | 不将 HTTP 超时解释为未执行 | `REUSE` |
| Session list | `GET /sessions?cursor=&limit=&status=`；`zyra.session-list.v1` | 200 | task store projection | 翻页、筛选、校验 `state_owner=task_store_projection` | `REUSE` |
| Session detail/resolver | `GET /sessions/{session_id}`；`zyra.session-detail.v1` | 200；不存在 404 | task store projection | 仅在 `resolution=resolved` 时消费 `resume_task_id`；`ambiguous` fail closed | `REUSE` |

`session_id` 仍是 task、command、permission、event 中的关联 identity，不是新的
canonical store。session API 只聚合 task state：单一候选返回 `resolved + resume_task_id`；
多候选返回 `ambiguous + resume_task_id=null`；不存在返回 404。客户端禁止用本地历史消除歧义。

### 4.2 Event / state

| 用途 | 精确 contract | owner / recovery |
|---|---|---|
| 能力协商 | `GET /tasks/{task_id}/event-ingress/capabilities` | event ingress；先协商 transport 与 generation |
| 初始/重同步 | `GET /tasks/{task_id}/event-ingress/snapshot?cursor=&generation=&limit=&filter=` | 服务端 canonical snapshot |
| 增量补齐 | `GET /tasks/{task_id}/event-ingress/delta?cursor=&generation=&limit=&wait_ms=&filter=` | signed cursor；有限等待 |
| 在线流 | `GET /tasks/{task_id}/event-ingress/sse?cursor=&generation=&stream_ms=&heartbeat_ms=&filter=` | SSE；断线后从已确认 cursor 恢复 |
| 旧兼容面 | `GET /tasks/{task_id}/events?after=&cursor=&limit=` | 只为兼容；新 CLI 不以 legacy event id 为恢复依据 |

客户端状态机固定为：`capabilities -> snapshot -> SSE/delta -> cursor journal`。遇到 generation 变化、gap、cursor 过期/签名失败或 filter 不兼容时，清除的只是本地 projection，随后重新 snapshot；task 继续由后端运行。

### 4.3 Command queue

| 操作 | 精确 contract | 幂等 / revision / permission |
|---|---|---|
| 投递 | `POST /tasks/{task_id}/commands` | body 包含 `text`、`arguments`、`request_id`、`command_id`、`actor_id`、`session_id`、`expected_session_revision`、`priority`、`delivery_mode`、`retry_of_request_id`、`idempotency_key`，并透传 sealed/competition 标志；成功 200/201/202，拒绝 403，冲突 409 |
| 队列 | `GET /tasks/{task_id}/command-queue` | 服务端 queue snapshot/receipt 是 canonical；本地队列只保存尚未提交的输入意图 |
| 取消 | `POST /tasks/{task_id}/commands/{request_id}/cancel` | 成功 200，已执行/冲突 409；不得从 UI 直接删除 canonical item |

`@zyra/commands` 的 `zyra.control/v1`、`zyra.command-queue/v1`、`zyra.command-receipt/v1` 是共享 contract。Claude 风格的 `now > next > later`、同优先级 FIFO 和稳定 snapshot 只作为 UX 适配，不能取代 runtime queue owner。

FE-S03 实际接入时确认两项既有 contract 兼容修正：共享 command request identity 使用 typed
client 已冻结的 `request_` 前缀；服务端 session revision 只统计 canonical
`session_control_revision`，不再把同一命令的 requested/validated/started 审计事件计为 mutation，
从而避免命令在执行前使自己的 expected revision 失效。两项均不改变 command owner。

### 4.4 Permission

| 操作 | 精确 contract | 约束 |
|---|---|---|
| 打开 session | `POST /permissions/sessions/open` | 200/201；也可能 400/401/403/404/409；必须有认证与 identity echo |
| 恢复 session | `POST /permissions/sessions/{session_id}/resume` | 200；400/401/403/404/409 fail closed |
| 摘要/请求 | `GET /permissions`；`GET /permissions/requests`；`GET /permissions/requests/{request_id}` | CLI/Web 只展示，custody token 不进日志/transcript |
| 裁决 | `POST /permissions/requests/{request_id}/resolve` | 200；400/401/403/404/408/409/410/422 保持原 error code；只有 runtime 接受 allow/deny |
| 其他治理 | expire、rules、mode、decisions 的 typed endpoints | CLI 不新增“跳过全部权限”开关；sealed/competition 一律遵循 zero-human/fail-closed policy |

当前部署同时存在 `zyra.permission-api.v2` 控制响应和 session open 返回的
`zyra.permission-api.v1`；typed normalizer 明确接受这两个已部署 schema（以及冻结的 slash-v1
兼容拼写）。CLI/Web session adapter 同时读取 `bearer_token` 与旧 `custody_token` 字段，token
只通过 Authorization header 使用，不进入持久状态或 transcript。

### 4.5 Artifact

| 操作 | 精确 contract | 恢复/安全 |
|---|---|---|
| Catalog | `GET /tasks/{task_id}/artifacts` | cursor/revision 由服务端返回 |
| Metadata | `GET /tasks/{task_id}/artifacts/{artifact_id}` | identity 与 revision 必须匹配 |
| Content | `GET /tasks/{task_id}/artifacts/{artifact_id}/content` | 大内容不得塞进 transcript；保存或按需预览 |
| Receipts | `GET /tasks/{task_id}/artifacts/{artifact_id}/receipts` | 作为证据链，不由 CLI 合成 |

### 4.6 Scenario

| 子命令 | 精确 contract | 成功状态 / 典型冲突 |
|---|---|---|
| `ls` | `GET /scenarios/registry`；`GET /scenarios/runs`；`GET /scenarios/runs/{run_id}` | 200 |
| `create` | `POST /scenarios/runs` | 201；409/422/503 |
| `start` | `POST /scenarios/runs/{run_id}/start` | 200/202；409/422/503 |
| `cancel` | `POST /scenarios/runs/{run_id}/cancel` | 200；409/422/503 |
| `verify` | `POST /scenarios/runs/{run_id}/verify` | 200；409/503 |
| `evidence` | `GET /scenarios/runs/{run_id}/evidence` | 200；409 表示尚不可形成证据，不是空证据成功 |

### 4.7 Daemon 与 UI launcher

daemon/UI lifecycle 是缺失的**客户端本地进程契约**，不是新增后端 task/session schema 的理由。后续实现若获授权，owner 仅可持有：

- pid、generation、启动时间、监听地址、日志位置和 child process 状态；
- start/stop/status 的本地 idempotency fence；
- 对 `/health` 与 `/runtime/readiness` 的探测结果。

它不得持有或复制 task/session/event/permission canonical state。CLI 退出只结束当前 terminal listener；显式 `daemon stop` 才可停止 daemon。

## 5. TTY、非 TTY、输出与退出码

| 模式 | stdin | stdout | stderr | 恢复 |
|---|---|---|---|---|
| TTY interactive | 多行输入、命令、artifact ref | line transcript；不使用 alternate screen | 即时诊断可进入 transcript/status 行 | signed cursor + snapshot |
| `run` / stdout 非 TTY | goal 参数、`-f` 或 pipe | **每行一个合法 JSON object**，禁止 banner/进度文本 | human diagnostics、warning、debug | cursor journal；EPIPE 安静结束 writer |
| JSONL input | 每行结构化 command/input | JSONL event/receipt | 非 JSON 内容 | malformed line 以结构化 error + 非零退出 |
| `tee` / 慢消费者 | 不改变运行语义 | 保序 JSONL；处理 backpressure/EPIPE | 超时/丢帧告警 | 不因 consumer 离开而取消服务端 task |

固定退出码：

| code | 含义 |
|---:|---|
| 0 | 请求完成且达到命令定义的成功终态 |
| 1 | task/场景执行失败，或读取到不可接受的执行 contract |
| 2 | CLI usage/参数/输入文件错误 |
| 3 | daemon/API 不可达，或启动后未在边界内通过 health contract |
| 4 | 显式取消、signal policy 中断；后续非交互 permission wait 也保留此码 |
| 5 | final verifier 或 completion gate 未达标、缺失，或场景 evidence verify 失败 |

退出码只表达当前命令观察到的结果；不得用 CLI 进程退出推断 daemon/task 已停止。

## 6. Web 降级与能力保留

| Web 区域 | 保留职责 | 从主入口移除的职责 |
|---|---|---|
| Dashboard / task list | 任务状态、筛选、证据摘要、断线状态 | 不作为创建/恢复的唯一入口 |
| Task detail | 长程状态、拓扑、事件、artifact、receipt | 不实现第二套 command/event 状态机 |
| Permission center | 复杂详情、历史与显式裁决 | 不让浏览器本地状态成为 permission owner |
| Scenario workbench | 配置、证据查看、比赛演示 | CLI 仍需覆盖完整 lifecycle |
| Settings / backend views | 只展示经过脱敏的配置和健康 | 不展示 capability token、credential、内部 cwd/root |

Web 与 CLI 必须共享 typed API 和 event recovery 语义。Web 降级不是删功能，而是将“持续工作入口”转到 CLI，将浏览器聚焦于高密度状态和证据。

## 7. 行为测试目录（后续 slice 的冻结验收）

这些是分阶段测试 contract；FE-S01 已实现其中 walking skeleton 对应的真实测试，
完整 TTY/重连/压力向量仍归后继 slice：

| 测试组 | 必测场景 | 归属 slice |
|---|---|---|
| `cli-command-surface` | 八顶层命令 help/parse、默认入口、usage、固定退出码 | FE-S01 |
| `cli-noninteractive-jsonl` | stdout purity、stderr 分流、pipe/tee、EPIPE、SIGINT、JSONL input/output | FE-S01 / FE-S06 |
| `cli-event-recovery` | snapshot、SSE、delta、cursor resume、generation change、gap fallback | FE-S01 / FE-S03 |
| `cli-command-permission` | now/next/later、FIFO、409、取消、permission wait/deny/timeout、sealed fail closed | FE-S03 |
| `cli-session-resolution` | task resume、session ambiguity/not-found/restart；只接受 task-backed owner | FE-S01 / FE-S03 |
| `cli-daemon-process` | double start、stale pid、generation fence、crash/zombie、stop timeout | FE-S01 / FE-S06 |
| `web-downshift-regression` | Web task/event/permission/scenario 能力仍真实可达 | FE-S05 |
| `shared-client-parity` | 同一 request 在 CLI/Web 的 method/path/body/status/cursor 语义一致 | FE-S05 / FE-S06 |

### 7.1 六个后续 slice 的唯一交接行

| slice | contract / owner | 主要失败路径 | 必须测试 | 当前准入 |
|---|---|---|---|---|
| FE-S01 | typed API task/event/scenario；CLI I/O 与 daemon pid/generation 只归 `apps/cli` | usage、API unavailable、dirty preflight、stream disconnect、unknown schema/version、JSONL pollution | command surface、real run/cancel/scenario、daemon survival、pipe/EPIPE/exit | **COMPLETED**；见 FE-S01 evidence/self-review |
| FE-S02 | input/viewport 是本地临时 owner；event ingress 是 transcript 事实源 | paste/editor failure、event gap、resize、用户滚动时误 re-pin | multiline/history/ref、80/120 列、long transcript、search/scrollback | **COMPLETED**；见 FE-S02 evidence/self-review |
| FE-S03 | `@zyra/commands` + permission runtime | 409、取消冲突、permission timeout/expired、disconnect/recovery | priority/FIFO/idempotency、allow/deny、sealed fail closed、cursor gap | **COMPLETED**；见 FE-S03 evidence/self-review |
| FE-S04 | BackendRegistry/Scheduler/attestation；CLI listener 仅拥有本地进程 | capability 泄露、越权注册、digest/sequence、zombie、root escape、sealed self-selection | registration authority、real HTTP dispatch/failover、kill/restart、attestation、exclusion mutation | 下一候选；未授权；T-01..T-04 contract 已收口 |
| FE-S05 | CLI/Web 共用 typed adapter；Web 只拥有表现 state | projection divergence、Web 能力被误删、浏览器本地状态冒充后端 | parity、Web task/event/permission/scenario regression、redaction | 待 S04 |
| FE-S06 | release/automation owner；runtime owners 不变 | offline 包缺依赖、Windows entry 失败、D5/比赛证据断链 | cleanroom install、binary、JSONL、D5、sealed scenario、evidence bundle | 最终候选 |

## 8. 赛题证据映射

| 赛题证据 | 客户端必须展示的真实链 |
|---|---|
| 超长程任务与记忆连续性 | task identity、checkpoint/compact/restore/restart/requirement-change event 与因果 receipt；不是“聊天记录很长” |
| 动态异构群体 | 同 run 的 node/edge/role/capability delta、placement、lease、dispatch receipt |
| 低熵通信 | 结构化 message、artifact/evidence ref、state delta、prune reason 和可比较指标 |
| 神经符号协同 | model proposal、symbolic accept/reject、canonical delta、verification、commit |
| 端边云 | local/terminal、edge runtime、cloud provider/model 的物理 dispatch receipt；终端节点 contract 见配套文档 |
| 零人工闭环 | sealed run 中低风险 allowlist 自动执行，高风险/未知确定性拒绝并 recovery/replan |

## 9. 差异收口与 Gate 决定

| ID | 差异 | 影响 | Gate 行为 |
|---|---|---|---|
| C-01 | 初始无通用 session collection/resolver API | FE-G00R 已新增 task-backed list/detail；歧义/不存在 fail closed | **RESOLVED**；真实 API + typed normalizer 测试通过 |
| C-02 | daemon/UI launcher owner、pid/generation/路径初始未形成实现 contract | 可能产生重复 daemon、误杀新进程或将客户端状态冒充 runtime state | **CLI daemon RESOLVED IN FE-S01**；UI launcher 仍归后续 slice，后端 owner 未改 |
| C-03 | terminal-node contract 初始存在 T-01..T-04 | FE-G00R 已收口 projection、authority、sealed exclusion 与 digest | **RESOLVED**；见 `terminal-node-contract.md` |
| C-04 | D5 完整压力测试仍含尚未实现的 TTY 行为 | FE-S01 只能验证 non-TTY JSONL/EPIPE/daemon walking skeleton | **PARTIAL**：FE-S01 向量已通过；FE-S02/S06 执行完整 D5，失败则回修 |

因此，FE-G00 的 contract/reference Gate 与 FE-S01 非交互 walking skeleton 均已通过。
这仍不是完整前端产品完成证据；FE-S04 是下一候选，但记录候选不构成授权。

## 10. 基线验证记录

- 工作树检查时只有用户原有未跟踪目录 `zyra.egg-info/`；本文不触碰它。
- FE-G00R 定向测试：session API、terminal registration/redaction/frame、sealed exclusion 共 `6 passed`。
- typed client：`typecheck:web` 通过；session contract `3 passed`。
- backend/task-graph/provider 相邻回归 `21 passed`；registry/failover/exclusion 重验 `10 passed`。
- 最终 HEAD sealed/recovery 长链回归 `12 passed`（128.20s）。
- FE-S01：CLI + typed-client `28 passed`；真实 API/runtime/daemon 集成 `4 passed`；
  全仓 TypeScript typecheck、CLI build 与 Node 执行通过。
- FE-S02：CLI `21 passed`，真实 daemon/task/session/Web/event-ingress 回归 `5 passed`。
- FE-S03：CLI `29 passed`、commands `29 passed`、typed client `19 passed`、Web permission
  `16 passed`；真实 command/permission/restart/parity `2 passed`、canonical queue `2 passed`、
  permission console `2 passed`、CLI/daemon 邻接回归 `6 passed`。
- D5 仅完成 FE-S01 可执行向量；TTY/长会话/完整压力证据保留给 FE-S02/S06，
  本文不伪报通过。
- FE-S03 只修正 command revision 计算、已部署 permission schema/token 的客户端兼容；未修改
  permission policy、runtime owner、public persistence schema，未启用 terminal node，也未声明
  `real_terminal_dispatch_claimed`。
