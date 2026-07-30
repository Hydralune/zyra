# P2-S05-02 增量评审：因果证据可视化与实验台接入

## 结论

P2-S05-02 已按 slice 边界完成，并完成父级 P2-05 的累计收口。实现将 canonical runtime event spine、policy artifact、P2-S05-01 metric report 和 LoopX control receipt 投影到既有 topology workbench 与 experiment workbench；没有新建第二套 graph、trace、experiment、permission 或 execution owner。

UI 可以从 run/task/mechanism/receipt/report 维度下钻，展示 proposal、symbolic constraint、graph diff、commit、operator/placement、lease、physical attempt、artifact、verifier、memory continuity 与 LoopX 因果引用。`real`、`simulated`、`degraded`、`not_applicable` 与 lifecycle/readiness/integrity 标签分离，缺失、断连、游标冲突、schema 不兼容和 permission 风格错误不会显示为空成功。

## 提交身份

- Slice base commit：`ac99ecbfadbf9605040918039dd054cab7568627`
- Implementation commit：`4eb7db194721ce8be9d72e12cf3ba46f68dccf6d`
- Evidence commit：由证据提交和 `execution-state.yaml` 的 verified head 记录
- 固定组合：`phase2_strongest_v1`

## 实现落位

### Canonical read model 与 typed API

- `apps/api/zyra_api/policy_api.py`
  - 从现有 `RuntimeEventSpineBridge` 与 canonical artifact root 读取证据；
  - 重新计算 artifact byte digest、policy contract digest 和 metric report digest；
  - 使用 scope-bound、signature-bound、high-watermark-bound cursor；
  - task drilldown 仍扫描 global sequence space，避免跨任务事件导致游标不终止；
  - API 为 GET-only read model，`canonical_write_allowed=false`。
- `apps/api/zyra_api/main.py`
  - 接入 `GET /policy/evidence`；
  - 复用 runtime event API 与 artifact root；
  - 暴露 typed client 所需的 CORS response headers。
- `packages/core/typed-api-client/**` 与 `apps/web/src/api/policy-api.ts`
  - 注册 policy evidence/metric contracts；
  - 严格验证 owner、schema、identity、counter、lifecycle、readiness、execution 与 integrity；
  - schema/correlation/cursor 错误 fail closed。

### Topology 与 experiment workbench

- `apps/web/src/features/topology/policy/**`
  - bounded incremental store，最多保留 5,000 条 transition；
  - 80 行虚拟窗口，不一次挂载完整 trace；
  - 原始 page evidence digest、snapshot digest、filter digest 与 high watermark 随导出保留；
  - proposal/constraint/graph diff/commit/lease/attempt/artifact/verifier/permission/event 均保留 typed causal reference；
  - simulated dispatch 只显示 simulated，不获得 real badge。
- `apps/web/src/features/topology/view/topology-workbench.tsx`
  - 接入既有 topology workbench；
  - 已挂载对象直接导航，未挂载对象保留 route/digest 并给出显式提示。
- `apps/web/src/features/experiments/policy/**`
  - 展示 P2-S05-01 receipt-derived aggregate、mechanism、anti-gaming 和 source receipt 信息；
  - missing/failed/simulated 不提升为 observed success。
- `apps/web/src/components/tasks/task-detail.tsx`
  - subagent projection 尚未入场时降级显示，不再阻断 topology policy evidence 的真实页面挂载；
  - 不关闭或接管 subagent canonical owner。

## Owner 与安全边界

- `GraphStateCustody`、`MemoryFabric`、`ResourceScheduler`、permission runtime、worker lease、artifact store 和 runtime event spine 仍是 canonical owner。
- 新 API 与 UI 只消费 immutable event/artifact/report；没有 graph mutation、lease grant、permission grant、dispatch 或 LoopX private-state 写入。
- UI 没有 local canonical mutation。既有命令仍经过原 permission/ACK 路径。
- artifact path 必须位于 canonical artifact root 内，byte digest 和 contract declaration 不一致时标为 `inconsistent`。
- physical dispatch 只有 lease、physical attempt、physical identity、worker manifest、call receipt、artifact 和 verifier 全部存在时才可标为 `real`。

## 真实浏览器验证

使用实际 `RuntimeEventSpineBridge`、`LocalArtifactStore`、HTTP API、typed client 和生产 Web bundle 建立：

- task：`task_7634cfdbf9c8`
- run：`run_d84597e07636`
- proposal：`proposal-browser-evidence`
- real dispatch：`dispatch-browser-real`
- simulated dispatch：`dispatch-browser-simulated`
- LoopX ACK event：`event-loopx-browser-ack`

浏览器验证确认：

1. topology workbench 成功挂载 `Proposal → symbolic verdict → canonical commit`；
2. proposal、commit、graph、event 和 physical placement/lease/attempt/artifact/verifier/permission 引用可点击；
3. 当前 viewport 未挂载的 canonical target 不伪造跳转，显示“target remains linked”并保留导出链；
4. `real` 与 `simulated` dispatch 同屏严格分标；
5. snapshot/high watermark/page evidence digest 可见，下一页保持同一 snapshot；
6. task detail 的 subagent 未就绪不会再让 policy evidence 页面崩溃。

截图及 SHA-256：

- `browser-policy-evidence.png`：`2c1a138c16c9f267360d71d4e0b258d750e79de2fa9802d07a5fa6b036b6df51`
- `browser-proposal-inspector.png`：`f9e82586fa3dc539d62c45dd2f564b4d31fcc24428d19982904be3b683b3c3fc`

## 验证结果

- P2-05 后端累计回归：35 passed。
- policy evidence API 定向回归：4 passed，包括 2,105 条真实 event-spine transition、11 页增量读取、tamper/disconnect 和跨任务 cursor 终止。
- topology/experiment/policy Web 回归：21 passed。
- Phase 2 no-training/readiness unit：9 passed。
- typed client/commands/web TypeScript：通过。
- production Web build：382 modules，构建通过。
- Phase 2 policy contract verifier：`valid=true`。
- internalization ledger：`audit_ok=true`，0 errors，0 blockers。
- Python `py_compile` 与 `git diff --check`：通过。

一次全量 Web 运行得到 279 passed、2 failed。两项失败都在未修改的 MCP elicitation 测试：其 canonical projection 会使用当前日期判定绝对到期时间，而测试夹具固定为 `2026-07-25T13:00:00Z`，在本次执行日期 2026-07-30 已过期。`git diff --exit-code <BASE_COMMIT> --` 已确认相关 MCP test/projection/elicitation 文件均未被本 slice 修改；该宽域观察没有被重新标记为 slice 通过。

## 负向与反作弊证据

- artifact tamper → `degraded` + `inconsistent`；
- event adapter disconnect → HTTP 503 typed error；
- cursor scope mismatch/ahead/expired/invalid → HTTP 409/400 typed error；
- schema/owner/lifecycle/readiness/execution/integrity 不兼容 → client admission failure；
- permission-style 403 → UI degraded，不显示 empty success；
- simulated/semantic-only dispatch → 永不计入 real；
- 缺 physical causal identity → execution=`degraded`；
- metric digest mismatch → report inconsistent；
- 2,105 transitions 保持分页和 80-row virtual window；
- UI export 保留 original evidence digests，不用 UI 自报状态闭环。

## 有效行分桶

- Production core：1,879 added lines；1,739 nonblank/non-comment effective lines。
- Adapter-only：537 added lines；514 nonblank/non-comment effective lines。
- Test：644 added lines；607 nonblank/non-comment effective lines。
- Runtime-assets/vendor-like：0。
- Generated source：0。
- External data/models：0。
- Mock/fixture counted as formal evidence：0。
- 评审、JSON evidence 与 PNG screenshot 属于 docs/data，不计入 Zyra implementation。

## P2-05 父级累计收口

P2-S05-01 的固定指标口径、receipt lineage、anti-gaming 与 GET-only metric report，已经由本 slice 的 topology/experiment read model 消费。P2-05 现在形成：

```text
canonical receipt / runtime event
  -> P2-S05-01 fixed metric report
  -> P2-S05-02 typed evidence projection
  -> topology / experiment causal drilldown
  -> digest-preserving export and visual evidence
```

父级收口没有改变第一阶段历史事实，也没有授权 P2-S06-01。记录下一候选不等于授权。

