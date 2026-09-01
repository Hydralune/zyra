# Phase G 真实长程前端负载证据（2026-09-02）

## 判定

Phase G 尚未通过完整退出门。

三个冷启动、隔离状态、干净 Git workspace 的真实 daemon 运行均通过了前端的启动、提交、运行态、控制回执、`needs_revision`、失败态、两次恢复附着、resize、`/exit` 和终端恢复门。三个运行也都与 canonical 终态保持一致。但是，physical provider 在开始文件操作前统一拒绝路由，错误为 `route_policy_rejected: no provider/model route satisfies the request constraints`。因此这批运行不能证明真实 provider 下的文件变更、diff、verification 或 permission 交互，不能据此关闭 Phase G。

## 证据边界

- 产品源码提交：`324040ed68934cedc8403f05e4ea4623d4df93c3`
- 三次运行均显式选择模型，并使用独立 daemon、端口、runtime state 和 Git workspace。
- 三个 workspace 在运行后 `git status --short` 均为空。
- 每次运行的机器可读报告保留在 `.tmp/phase-g-final-324040ed-run{1,2,3}-{launch,observer}.json`；`.tmp` 不进入 Git。
- 本文固化报告中的非敏感汇总；原始 `events.jsonl` 留在对应 `.tmp/phase-g-final-324040ed-run*-runtime`，其中包含 canonical physical failure receipt。

## 运行汇总

| 运行 | 负载 | task / run | composer ready | 提交到 canonical | ingress | 控制 | 第一次退出时状态 | 最终状态 |
|---|---|---|---:|---:|---:|---|---|---|
| 1 | tenant inventory：隔离、幂等、release 安全与验证文档 | `task_5f36780d9979` / `run_30277b588c97` | 427.590 ms | 6815.963 ms | 150 frames，next 154 | `/redirect`，回执 `applied /change` | `needs_revision` | `failed`，稳定观察 2000 ms |
| 2 | durable queue：多租户幂等、排序、取消安全与验证文档 | `task_924966e15517` / `run_79837009fcda` | 388.905 ms | 7097.896 ms | 151 frames，next 155 | `/review`，回执 `applied /change` | `needs_revision` | `failed`，稳定观察 2000 ms |
| 3 | incident report：指标、排序、文档与验证证据 | `task_80dd16cea883` / `run_beeace748e22` | 407.860 ms | 8739.802 ms | 150 frames，next 154 | `/interrupt`，回执 `applied /change` | `needs_revision` | `failed`，稳定观察 2000 ms |

每次运行包含两个独立恢复附着周期，每周期 250 次 resize；合计 6 次恢复附着和 1500 次 resize。两周期均使用真实 `/exit`，退出码为 0；没有开发者事件洪流、alternate screen 或 bracketed-paste enable，且每次都发送 paste disable/终端恢复序列。恢复期间 task/run identity 保持不变。

## 单次周期数据

| 运行 | cycle 1 startup / detach | cycle 2 startup / detach | cycle 2 canonical |
|---|---:|---:|---|
| 1 | 1445.908 / 1361.205 ms | 1364.602 / 753.462 ms | `failed` |
| 2 | 1566.323 / 1810.829 ms | 1589.281 / 753.306 ms | `failed` |
| 3 | 1528.140 / 1120.445 ms | 1469.467 / 753.070 ms | `failed` |

## 未关闭项

1. 修复或配置一个满足显式模型约束的 physical provider route，然后重新执行至少一组能够实际修改文件并运行验证的真实任务。
2. 新运行必须覆盖 canonical changed paths、diff、verification receipt，并至少出现一次真实 permission 请求或提供该任务无需权限的 canonical 证据；不能用组件 fixture 替代真实 provider 运行。
3. 保留本轮三次运行作为失败态、控制和恢复证据；补齐 provider 成功路径后再按任务书 Phase G 的集合覆盖要求判定。

Agent 没有解出任务本身不等于前端失败，但 physical provider 在副作用前拒绝使所需前端状态根本没有产生。这里记录的是验收覆盖缺口，不把后端失败误报成 TUI 缺陷，也不把未发生的状态写成已验证。
