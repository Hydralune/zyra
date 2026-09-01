# Phase G 真实长程前端负载证据（2026-09-02）

## 判定

Phase G 已通过退出门。

验收集合包含三次冷启动失败态/控制/恢复运行、两次真实 provider 成功运行，以及最终一次 daemon 重启后的产品结果复验。集合覆盖运行中、控制回执、`needs_revision`、真实文件修改、diff、命令级验证收据、canonical permission policy、完成/失败终态、CLI 退出恢复和终端稳定性。Agent 任务质量未被用作前端通过条件；每项前端结论都与 canonical task 和真实 ConPTY 观察对账。

## 证据边界

- 失败态/控制基线源码：`324040ed68934cedc8403f05e4ea4623d4df93c3`。
- 决定性 provider 任务源码：`f2e383d90f06c511d4df42e8cc901d9ea42f7919`。
- 最终历史恢复与产品标记观察源码：`0babe345295d63ed2082f02e31587654b1c0c006`。
- 所有正式运行使用独立 daemon state、端口和干净 Git workspace；运行期间没有人工修改目标 workspace。
- 机器可读报告保留在 `.tmp/phase-g-final-324040ed-run{1,2,3}-{launch,observer}.json`、`.tmp/phase-g-network-4dc33ef4-run6-observer.json`、`.tmp/phase-g-network-f2e383d9-run9-observer.json` 和 `.tmp/phase-g-network-0babe345-run9-product-evidence-observer.json`。`.tmp` 不进入 Git，但可由仓库 harness 重建。
- 原始 runtime event log 和 artifact 保留在对应隔离 `.tmp` state；文档只固化非敏感汇总。

## 失败态、控制与恢复基线

三次显式模型运行都在 physical side effect 前得到 `route_policy_rejected`。这不是成功路径证据，但真实覆盖了失败投影、控制和恢复：

| 运行 | 负载 | task / run | composer ready | ingress | 控制 | 第一次退出时状态 | 最终状态 |
|---|---|---|---:|---:|---|---|---|
| 1 | tenant inventory | `task_5f36780d9979` / `run_30277b588c97` | 427.590 ms | 150 frames，next 154 | `/redirect` → `applied /change` | `needs_revision` | `failed` |
| 2 | durable queue | `task_924966e15517` / `run_79837009fcda` | 388.905 ms | 151 frames，next 155 | `/review` → `applied /change` | `needs_revision` | `failed` |
| 3 | incident report | `task_80dd16cea883` / `run_beeace748e22` | 407.860 ms | 150 frames，next 154 | `/interrupt` → `applied /change` | `needs_revision` | `failed` |

每次包含两次独立恢复附着、每周期 250 次 resize 和真实 `/exit`。合计 6 次附着、1500 次 resize；task/run identity 未改变，canonical 终态稳定观察 2000 ms，退出码均为 0，没有开发者事件洪流、alternate screen 或 paste 状态泄漏。

## 真实 provider 成功路径

### 成功运行 A：inventory 工作流

- task / run：`task_dc2cca257f8a` / `run_5d507171f08f`
- 源码：`4dc33ef4a2cb3cc79758813bc240ed59b0f9a827`
- provider：真实 DeepSeek provider-code-worker
- canonical：`running` → `completed`，终态稳定 2000 ms
- ingress：619 frames，next 623；包含 481 agent message、12 artifact、25 audit finding、25 backend dispatch、42 text boundary 和 4 control frame
- 两次恢复附着：startup 1423.460 / 1977.460 ms，各 250 次 resize，真实 `/exit`，退出码 0
- 结果：真实修改 inventory fixture，并由目标项目测试 14/14 通过；该轮暴露并促成 child assistant stream 隔离和当前 workspace staging/materialization 修复

### 成功运行 B：durable queue 决定性闭环

- task / run：`task_141d76cf35ad` / `run_9aee0f813e80`
- provider 配置：`deepseek` / `deepseek-v4-flash`
- 目标 workspace：`.tmp/phase-g-final-b3-workspace`，运行前基线 commit `a348973120672880416fac0b40c5052bfe6079aa`
- canonical：`completed`，确认时间 `2026-09-01T19:03:03.047Z`
- ingress：1439 frames，next 1439；1308 agent message、12 artifact、25 audit finding、26 backend dispatch、5 node created、1 local node failure、19 node update、19 text started/ended、3 topology route
- provider 实际执行：18 turns、23 tool calls、3 workspace mutations
- 交付：修改 `src/queue.js`、`test/queue.test.js`；创建 `VALIDATION.md` 和有界 tool artifact；实际 diff 为 87 insertions / 5 deletions
- 测试演进：基线 6 项中 4 项失败；修复后公开测试 6/6；最终 14/14 两次通过
- 外部独立复验：在 materialized workspace 执行 `npm.cmd test`，14 pass、0 fail、退出码 0

canonical outcome 明确记录：

- `delivery_verifier.passed = true`
- `final_verifier.passed = true`
- completion gate `hard_conditions_passed = true`，无 failed condition
- 三条 `node --test` 收据为 `passed / exit 0`
- 一条 literal `npm test` 在 provider sandbox 中因 launcher 不可用记录为 failed；没有被伪装成成功，外部 Windows `npm.cmd test` 另行通过
- permission receipt schema `zyra.phase2-policy-permission-receipt/v1`，owner `typescript.PermissionCoordinator`，effect `allow`，reason `explicit_allow_rule`，权限为 `graph.write` 与 `worker.dispatch`

## 产品 TUI 可见结果与重启恢复

最终 observer 在任务完成并重启 daemon 后执行两次独立恢复附着，直接断言产品内容，而不只读取后端：

| 周期 | startup | resize | 可见文件状态 | `/verification` | 退出 |
|---|---:|---:|---|---|---|
| 1 | 2810.098 ms | 250 | `4 个文件发生变更`、diff preview | `命令级收据：已记录`、`node --test`、`exit 0` | `/exit`，code 0 |
| 2 | 2854.466 ms | 250 | 同上 | 同上 | `/exit`，code 0 |

两周期都显示 `最终验证通过`，task/run identity 与 canonical `completed` 一致，无 raw `runtime.*` 洪流，无 alternate screen，发送 paste disable，终态稳定观察 2000 ms。报告 `all_passed = true`。

这次 daemon 重启复验还发现历史 task 的 workspace payload 过期会把已可读的完成结果替换为泛化 API contract error。提交 `675d6da4` 将其修为：历史 resume 明确提示“本地文件未由本次恢复验证”并保持结果、diff 和验证收据可检查；新任务首次 materialization 失败仍然 fail closed。两条边界测试与真实 ConPTY 均已通过。

## 运行中发现并关闭的缺陷

| 提交 | 关闭项 |
|---|---|
| `16544c50` | child assistant stream 不再污染父 transcript |
| `4dc33ef4` | 产品任务与当前 workspace 的 staging/materialization 闭环 |
| `6b8cff0f` | executable/argv 结构化验证证据保真 |
| `b28d08a8` | Windows `.cmd/.exe` 验证 shim 与稳定 scope |
| `f2e383d9` | verifier `failed_to_start` 与 `node --test` 分类 |
| `675d6da4` | daemon 重启后历史完成任务的安全降级检查 |
| `0babe345` | 文件/验证/退出码的物理 TUI 标记门稳定化 |

另外两次探索运行暴露 `.cmd` 分类和 `failed_to_start` 缺陷后被显式 `/cancel`，未计入成功样本，也未伪报为完成。

## 剩余边界

- Windows Terminal 人工 IME 候选窗仍是独立人工门；自动 Unicode、组合字符、emoji、paste 和 ConPTY 不能替代它。
- 官方 Codex standalone 未登录，认证后的 `/status`/`/exit` 参考序列仍标记未运行；它不影响 Zyra Phase G 判定。
- Linux/macOS 未进行实机验证。
