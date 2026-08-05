# FE-S05 Web 产品面、证据面与跨入口连续性：增量自审

## 结论

FE-S05 在 `ce56dc112bbe68686a16879f182abf10c74bfe61` 基线上完成实现和可执行验证。
默认产品路径保持 `/tasks`、`/tasks/{task_id}`，新增显式
`/tasks/{task_id}/evidence`；证据路由复用同一 `CanonicalProjectionStore`、cursor ingress、
typed API、`TaskDetail` 和 scenario owner，没有复制 Web、reducer 或 canonical state。

`zyra ui` 已进入既有 CLI 命令路由：先通过既有 daemon lifecycle 确认 API，再启动既有
`apps/web` 构建和 `scripts/dev_web.py`，最后生成不含 credential 的产品 URL。launcher 只持有
pid、generation、Web/API origin、启动时间和 project identity；不持有 task/session/event。

浏览器插件在本次执行环境返回“无可用浏览器”，因此没有伪报点击或截图验证。真实构建、真实
`zyra ui`、HTTP 产品/证据路由、共享 API 跨入口测试均已通过；该环境限制在机器证据中保持
`unavailable`，而不是写成 `passed`。

## 产品与证据分层

| 检查项 | 结果 | 证据 |
|---|---|---|
| 默认产品面 | PASS | 首页 goal composer、会话导航、task 状态/进度/权限/最终 artifact 保持默认可见；产品测试覆盖未验证结果不冒充完成 |
| 显式证据面 | PASS | evidence route 提供 topology、noise、neuro-symbolic、continuity、placement、recovery、artifact、control 索引并复用既有高级面板 |
| 双域叙事 | PASS | evidence route 直接挂载 scenario workbench，明确 software-delivery 与 cross-source-research；现有双域 admission 测试通过 |
| 单一投影 owner | PASS | route loader/live sync 将 task/evidence 绑定到同一 canonical store；没有新增 localStorage 同步或第二 reducer |
| 因果引用 | PASS | 既有 policy digest、attempt/lease/permission/failover、artifact producer 和 causal trace 出口均保留 |
| 大型运行 | PASS | policy evidence 2,105 transition cursor append/export 通过；view 只挂载固定窗口；2,400-node topology 与 5,000-row timeline 相邻回归通过 |
| 失败真值 | PASS | disconnected→degraded、lag/integrity→stale、无记录→missing；403/adapter-disable 不显示静态成功 |

完整 feature/score/owner 路由见 `FE-S05-web-feature-score-routing.md`。没有
`SCORE-ORG`、`SCORE-NOISE`、`REQ-TRACE-01`、`REQ-EDGE-01` 或 `SCORE-ROBUST`
唯一出口被删除。

## 物理 placement 诚实性

`PhysicalDispatchViewModel` 只读取已 admission transition 的
`details.physical_identity.location`、`terminal_id`、attempt、lease、permission、recovery 和
`physical_validation.real_gate_closed`：

- `terminal_id + location=local` 显示 `TERMINAL · LOCAL`，绝不显示 EDGE；
- edge 只在 receipt 明确 `location=edge` 时显示 `EDGE RUNTIME`；
- configured backend、backend 名称或 selected placement 不能升级为真实执行；
- execution/integrity 显式映射 `real`、`simulated`、`degraded`、`missing`、`stale`；
- FE-S04 真实 TypeScript terminal registration/dispatch/failover 集成在本片重跑通过，UI 单测验证
  同一 receipt 形状的 LOCAL/failover 展示。

本片没有修改 `real_terminal_dispatch_claimed` 或 `real_edge_dispatch_claimed`，也没有用 UI
标签替代 `REQ-EDGE-01` 的物理证据。

## CLI/Web 连续性与 `zyra ui`

真实隔离 API 测试验证：

1. CLI 创建 canonical pending task；Web task list/detail 与 canonical projection 观察到同一
   task/run/session identity 和 event cursor。
2. Web 提交 `/status` 后，CLI 以同一 request/idempotency 再提交，观察到相同
   request/command/revision；runtime 只产生一次 succeeded effect。
3. 当前 control-command HTTP 路由没有设置可选 replay header；测试将其显式记录为 false，
   不把“同 response + 单次副作用”伪写成存在 replay header。

真实 launcher 验证使用构建产物：首次 `--web-port=0` 启动 Web 后，第二次同样调用复用完全相同
的 PID、generation 和随机端口，返回 `already_running=true`。产品路由和 evidence SPA 路由均
返回 HTTP 200 与 `zyra-product-entry` marker。外部/异进程端口、存活但不可用 generation、
子进程早退和启动超时均 fail closed；启动失败会清理 state 并终止本片启动的 Web child。

清理 Web 后尝试正常停止 daemon 时，既有 daemon 安全门发现共享状态中有 active tasks，按设计
拒绝停止；未使用 `--force`，避免影响不属于本片的任务。验证创建的 Web 进程和 ui state 已清理，
daemon 因保护既有活动任务继续运行。

## 验证记录

- 全仓 TypeScript typecheck：通过。
- Web 全量测试：`309 passed` / `1,874 assertions`。
- CLI 全量测试：`40 passed` / `285 assertions`。
- CLI→Web task/event/command receipt 真实 HTTP 连续性：`1 passed`。
- FE-S03 CLI/Web command/permission parity 相邻真实集成：`2 passed`。
- FE-S04 TypeScript terminal real registration/dispatch/failover：`1 passed`。
- Web 与 CLI build：通过；真实 `zyra ui` 首启/随机端口复用：通过。
- 产品与 evidence HTTP route marker：两者均 `200`。
- 浏览器插件级点击/截图：`unavailable`（执行环境无浏览器实例，未降级为伪验证）。
- `git diff --check`：通过。

## 分桶

| bucket | 增量 | 说明 |
|---|---:|---|
| production | CLI 5 modified + 1 new；Web 10 modified + evidence feature | UI launcher、evidence route/index、truthful dispatch card 与现有入口接线 |
| test | 6 modified/new TS/Python files | launcher、routing、product regression、2,105 transition、cross-entry real HTTP |
| docs | 4 modified/new Markdown + 1 JSON evidence | feature/score 路由、contract 状态、自审、机器证据 |
| runtime-assets | 0 | 无 vendor、外部前端或桌面壳 |
| generated | 0 tracked | Web/CLI dist、pytest temp 不提交 |
| data | 0 | 无 dataset、label、checkpoint |
| adapter-only | 0 | 有真实 API、event projection、command effect、launcher process 和 HTTP route 行为 |
| mock/fixture | launcher fake environment + contract transition | 只验证失败边界/纯映射；完成证据由真实 API、SQLite、HTTP、进程和既有 terminal dispatch 提供 |

## 后继边界

FE-S06 可以消费稳定的产品路由、证据 drill-down、跨入口测试、LOCAL/EDGE/CLOUD 真值标签和
dual-domain scenario 入口。本片没有授权 FE-S06、发布打包、cleanroom 或比赛材料冻结。
