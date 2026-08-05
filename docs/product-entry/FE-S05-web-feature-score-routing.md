# FE-S05 Web 功能域与评分证据路由

## 结论

FE-S05 没有删除或复制任何既有 Web 功能域。默认 `/tasks` 与 `/tasks/{task_id}`
负责“提出目标、观察进展、取得交付物”，显式 `/tasks/{task_id}/evidence` 复用同一
`CanonicalProjectionStore`、cursor ingress、typed API 和 `TaskDetail`，把评分证据组织为
可导航的 drill-down。`/settings` 保留系统、scenario 和 experiment 工作台。

Web 只持有投影和瞬态交互状态。task、session、event、permission、graph、dispatch、artifact
和 scenario 的 canonical owner 均未改变。

## `apps/web/src/features` 完整盘点

| feature | 产品/评分职责 | canonical source / owner | FE-S05 入口与默认可见性 | 原始证据可追踪性 |
|---|---|---|---|---|
| `artifacts` | 最终交付物、catalog、内容验证和 custody | artifact API / `LocalArtifactStore` | 产品页默认展示交付物；证据页 `#evidence-artifacts` 展开完整 viewer | artifact/producer/receipt/digest 保留 |
| `browser` | 浏览器 worker 行为、下载和 viewer control | browser worker/session API | 证据页复用 `TaskDetail`；产品页高级抽屉仍可达 | action/event/artifact refs 保留 |
| `commands` | typed control command 与 ACK | `@zyra/commands` + runtime control dispatcher | 产品 composer 与证据页共用；不复制 queue | request/command/revision/receipt 保留 |
| `diff-review` | patch、review、apply/rollback custody | diff review API | 证据页复用高级 task detail | transaction/receipt/artifact refs 保留 |
| `evidence` | FE-S05 证据索引与真实性状态 | 只组合 canonical selectors | 新增显式 evidence route；不是 store | 所有卡片只链接既有证据出口 |
| `experiments` | 对照、指标与 evidence bundle | experiment API | `/settings` 的评审入口保持可达 | matrix/score/statistics/bundle refs 保留 |
| `long-horizon` | LoopX goal/todo/claim/history 插件状态 | LoopX bridge / private plugin owner | evidence `#evidence-long-horizon`；产品页高级抽屉 | plugin state 与 canonical task mapping 保留 |
| `mcp` | MCP server/tool 状态和调用 | MCP coordinator | 证据页复用 task detail | invocation/permission/event refs 保留 |
| `memory` | memory query、provenance、continuity | `MemoryFabric` 与 continuity receipts | evidence `#evidence-continuity-placement` | checkpoint/compact/restore/downstream refs 保留 |
| `permissions` | 待决、决策、custody 与审计 | permission API / `PermissionStateStore` | 产品页显示权限摘要；evidence `#evidence-controls` | request/challenge/decision/receipt 保留 |
| `placement` | device/edge/cloud 候选与实际 placement | scheduler projection + dispatch receipt | evidence continuity/placement 区域 | selected 与 physical attempt 分开展示 |
| `providers` | provider/model、credential presence 与 fallback | provider control plane | evidence continuity/placement 区域 | provider attempt/fallback refs 保留 |
| `scenarios` | software-delivery 与 cross-source-research sealed evidence | scenario runner API | `/settings` 与 evidence 双域区均可达 | definition/run/manifest/verifier refs 保留 |
| `session` | task-backed session、checkpoint、compact 与恢复 | task/session API + memory continuity | evidence continuity/placement 区域 | session/checkpoint/context receipt 保留 |
| `skills` | skill registry、供应链和 invocation | skill runtime/API | 证据页复用 task detail | registry revision/permission/invocation refs 保留 |
| `subagents` | 层级、scope、budget、heartbeat 与控制 | subagent API | 证据页复用 task detail | task/run/worker/checkpoint refs 保留 |
| `terminal` | browser terminal session 与 FE-S04 可观察性 | terminal registry/runtime | evidence `#evidence-terminal` | session/frame/control receipt 保留 |
| `timeline` | worker、fault、recovery、requirement change 因果线 | canonical event projection | evidence `#evidence-recovery` | event/cause/correlation refs 保留 |
| `topology` | graph、policy、AgentPrune、神经符号、placement receipt | graph/policy evidence API | evidence `#evidence-topology` | revision/delta/verdict/dispatch refs 保留 |
| `trace` | 跨域 causal trace 与 typed navigation | canonical event projection | evidence `#evidence-causal-trace` | event/span/tool/artifact refs 保留 |

## 评分追踪门

| requirement / score | 原入口和 owner | FE-S05 新入口 | 默认可见性变化 | 原始 receipt | 验证 |
|---|---|---|---|---|---|
| `SCORE-ORG` | topology workbench / graph custody | evidence index → `#evidence-topology` | 从产品默认面折叠到显式证据面；未删除 | graph revision、delta、commit 均保留 | topology projection/interaction + evidence layering tests |
| `SCORE-NOISE` | policy evidence / policy API | evidence index → topology policy panel | 折叠；无静态成功摘要 | prune reason、density、duplicate metric 保留 | 2,105 transition policy test；missing/degraded test |
| `REQ-TRACE-01` | policy + causal trace / event owners | evidence index → topology + `#evidence-causal-trace` | 折叠；无数据即 missing | proposal、verdict、delta、commit、event ref 保留 | policy/causal-trace tests |
| `REQ-EDGE-01` | placement/provider/topology / scheduler + dispatch | evidence continuity/placement + physical receipt card | 折叠；selected 不再冒充 executed | `physical_identity.location`、attempt、lease、permission、failover 保留 | LOCAL-not-EDGE 与 configured-not-real tests |
| `SCORE-ROBUST` | recovery timeline / event + recovery owners | evidence index → `#evidence-recovery` | 折叠；失败仍默认可见于产品状态 | failure/cancel/failover/recovery refs 保留 | recovery timeline、cursor/disconnect tests |

没有评分唯一出口被删除。`TaskDetail` 仍是既有高级面板的单一组合点；产品页高级抽屉和
evidence route 都复用它，因此不会产生第二份数据 store 或第二套 reducer。

## 真实性与退化规则

- layer source 状态只允许 `live`、`degraded`、`missing`、`stale`；断线优先显示
  `degraded`，lag/integrity warning 显示 `stale`，没有 canonical 记录显示 `missing`。
- physical dispatch 卡片的执行真值只读 policy transition 中显式
  `details.physical_identity.location` 和 execution/integrity 字段；不从 backend 名称、provider
  配置或候选 placement 猜测。
- `terminal_id + location=local` 显示 `TERMINAL · LOCAL`；edge 仅在 receipt 明确
  `location=edge` 时显示 `EDGE RUNTIME`。
- `real`、`simulated`、`degraded`、`missing`、`stale` 均有独立标签；缺字段不会升级为成功。
- policy transition 保持 cursor-based incremental load，窗口只挂载 80 行；2,105 条真实 admission
  测试验证完整导出和因果引用不因窗口化丢失。
