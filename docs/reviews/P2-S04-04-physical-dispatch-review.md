# P2-S04-04 real physical dispatch review

- Slice: `P2-S04-04`
- Verdict: `PASS`
- Base commit: `cefd5dff62f0f32c00d74b04f67ecf31bc6de6db`
- Implementation commit: `5a1aa06b64ffd81d6e95030a3b113d9b7865f039`
- Evidence commit: `fec06b920fa5e1d1afb524a0535b98f92a1366e1`
- Mechanism version: `physical_dispatch_v1`
- Configuration digest: `8c1ee38a87c699052e055ea936dde573bdc2a22b7b2be7cc2ca39c086d55b9b1`
- Physical-dispatch gate digest: `c572a35ca8cd431da512eaa5db32d6f6581335e160c27c14953c4f72f5aee9b9`
- Readiness report digest: `ef02da429aa0756118bc287a5e84487a54d9e94b6a84aa6b095b4540ca8f5d56`

## 结论

本 slice 已把 P2-S04-03 的 operator、placement、lease 与 attempt 主链接入三类真实物理执行位置：

`PolicyInputSnapshot -> MaAS proposal -> ResourceScheduler placement -> canonical lease -> physical attempt -> local/edge/cloud dispatch -> artifact -> verifier -> PolicyOutcome`

同一最小任务在七个环境条件下形成七条真实 receipt。聚合门的九项检查全部通过，`real_location_count=3`、`privacy_cloud_dispatch_count=0`、`simulated_receipt_count=0`，且 local、edge、cloud 的进程或 provider 身份可以相互区分。模拟 receipt、仅修改 location label 的 receipt 和缺少 credential 的 cloud 尝试均不能关闭门。

## 三类物理位置

- Local 是受认证 loopback HTTP 端点后的真实 device process；receipt 记录 hostname、PID、process generation、endpoint、supervisor 与 failure boundary。
- Edge 是与 orchestrator/local worker 分离的受监督进程；kill 该 edge process 不会终止 orchestrator，并触发 canonical recovery/reroute。
- Cloud 是经既有 Provider Control Plane 发起的真实 DeepSeek `deepseek-v4-pro` 请求；receipt 记录 HTTP status、provider request/attempt ID、模型、usage、token/cost/latency、输入输出 digest 与 marker verification。

Cloud 价格证据固定到 DeepSeek 官方 pricing 文档。凭据只从 `env://DEEPSEEK_API_KEY` 读取；持久化证据只含 credential ref/fingerprint，不含原始 key、原始任务 payload 或 provider 输出正文。

## 条件切换与恢复

七条 lane 使用相同 payload digest `4820c6e5b51c2af423c00117d92356ab0399c86cc6f437a3bd8def18d61b3346`：

| 条件 | 最终位置 | 证明 |
|---|---|---|
| normal | cloud | 真实 provider HTTP 200、request/attempt ID、usage/cost/latency |
| privacy_local_only | local | cloud dispatch count 为 0 |
| local_load | edge | scheduler 根据负载改选隔离 edge process |
| edge_health | local | health fault 进入 RecoveryPlanner 后 reroute |
| edge_disconnect | local | 真实 edge PID 被终止，local/orchestrator 保持存活并恢复 |
| cloud_unavailable | local | credential preflight fail closed，无 provider request，再由 recovery reroute |
| latency_cost | edge | latency/cost 约束使 scheduler 选择 edge |

失败 receipt 绑定原 placement、lease、physical attempt、failure reason、recovery plan 与最终 receipt。进入外部副作用边界后若结果未知，沿用 P2-S04-03 的禁止自动 retry 规则；只有明确无副作用且存在物理 attempt 证据的失败才允许恢复。

## Canonical owner 边界

- MaAS 只拥有 operator/skill/worker/tool/model 候选 proposal。
- `ResourceScheduler` 保持物理 placement 的最终裁决权。
- `WorkerPoolFoundationRuntime` / `WorkerLeaseManager` 保持 lease、fence 和 physical attempt 所有权。
- `DeploymentProcessManager` 保持 local/edge process 生命周期所有权。
- TypeScript `ProviderControlPlane` 保持 provider route、transport、credential resolution 与请求边界所有权；Python adapter 只消费该正式入口并生成 Zyra receipt。
- `RecoveryPlanner` 保持 retry/replan/reroute 所有权。
- permission runtime、artifact store、budget 与 graph/memory owner 均未转移。

`canonical_owner_transfer_count=0`，没有新增平行 lease、placement、recovery、permission、graph 或 memory owner。

## Readiness、来源与训练边界

MaAS 与 `zyra_physical_dispatch` 均推进到 `implementation_validated / deterministic_ready`。该状态只说明从 proposal 到真实三位置 dispatch、验证与 recovery receipt 的实现已验证；`activation_allowed=false`，默认最强组合的最终激活仍留给 `P2-S06-01`。

本 slice 没有策略训练、强化学习、policy/text gradient、随机采样、训练 dataset、训练 checkpoint 或可变 learned parameter。预训练模型只用于真实 cloud execution lane；其输出必须经过 marker/digest 验证，且不能提交 canonical state。OpenClaw 保持排除，运行时不依赖 `long-horizon-systems/`。

## 实现分桶

实现 commit 的新增行：

- production：2,148
- tests：1,025
- configuration、docs、runtime-assets、generated、data、adapter-only、mock/fixture：0
- production share：`0.67696187`

测试、证据 JSON 和文档不计入 production。内部化 ledger 对 implementation commit 报告 `audit_ok=true`、`errors=0`、`blockers=0`、`effective_added=3173`、`effective_deleted=19`。

## 验证

- physical-dispatch focused suite：`10 passed in 40.24s`
- worker pool foundation：`6 passed in 4.05s`
- worker/placement/lease adjacent bundle：`23 passed in 58.95s`
- focused worker API main path：`1 passed in 11.96s`
- deployment profiles：`8 passed in 1.12s`
- policy contracts：`7 passed in 0.20s`
- deployment clean-state retry：`1 passed in 73.92s`
- three-real-profiles dispatch/recover/restart：`1 passed in 5.79s`
- 最终已通过测试合计：`57`
- Python compileall：passed
- `git diff --check`：passed
- internalization ledger：passed
- no-policy-training、OpenClaw 与 source-boundary scan：passed
- evidence secret scan：passed；实际 ignored env credential 未出现在证据中
- Ruff：repository virtual environment 未安装

一次组合 deployment 命令在 short-task checkpoint handoff 处出现一项失败；同一最终 target 上对该 clean-state retry 用例单独重跑后通过。完整 worker API 文件因其前台 TypeScript agent 生命周期超过本 slice 的时限而没有宣称全文件通过；真实主路径对应的 focused API 用例通过。

## 收口

P2-S04-04 与父级 P2-04 均完成。P2-S05-01 是唯一下一候选，但本完成不授权自动执行下一 slice。
