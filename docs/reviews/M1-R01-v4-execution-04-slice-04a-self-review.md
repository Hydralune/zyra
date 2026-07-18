# M1-R01 E04-A 增量批判式自审

## 结论

`E04-A Terminal Protocol Closure` 在实现提交
`f69bbd5bb06a4b335e7b4730ec2d5a785c0302ac` 上通过增量验收。该结论只关闭
04A，不完成 E04，也不恢复 `M1-S05C-01`。

## 实际修复

- TypeScript 在关闭 E02 capability runtime 并完成所需 checkpoint 后才提交
  `run.result`。
- terminal result 具备稳定 `terminal_id`；Python 先原子持久化 receipt，再发送
  相关 ACK；TypeScript 收到 durable ACK 后才发送 `run.closed`。
- `run.result` 后不再产生需要 Python 响应的 checkpoint/effect 请求；ACK 丢失时，
  同一 worker request 直接从已提交 terminal receipt 恢复，不启动 TypeScript 进程，
  也不重放工具副作用。
- 同一 terminal result 的重复交付可幂等 ACK；不同 result 复用 request identity
  会失败关闭。
- E02 快照只在原 worker request 上提升 epoch 并恢复。相同 run/task/session 的后续
  worker request 显式建立新的请求级 authority，避免错误继承旧 permit、lease、
  continuation 和 idempotency 记录。
- close 阶段 E02 checkpoint 合并到最新 TypeScript runtime checkpoint，不再覆盖
  E01 canonical snapshot。
- over-wide recent-message preservation 不再把有效 compact 计划收窄为零源消息。

## 对抗式检查

- 若在 Python 持久化 terminal receipt 前 ACK，durability-before-ACK 测试失败。
- 若 ACK 丢失后重新启动 TypeScript 或重放工具，lost-ACK/restart 测试的进程启动计数
  或工具执行计数失败。
- 若重复 terminal delivery 产生第二份 receipt，duplicate-delivery 测试失败。
- 若恢复旧 worker request 的 E02 authority 到新 request，request-scope 测试失败。
- 若禁用 TypeScript canonical runtime，默认任务失败关闭；不存在 Python
  query/policy/capability 决策回退。

## 有效行数分桶

- production：新增 358 行，删除 62 行。TypeScript 负责 terminal 状态机、E02
  restore selection 和 compact 决策；Python 新增内容只承担进程协议、durable
  receipt、side-effect fencing 与 projection。
- test：新增 292 行。
- generated、data、docs、vendor-like/source-pool、mock-only、fixture-only：0 行。
- adapter-only：0 行计入 canonical decision owner；Python 物理 host 代码不取得
  query/permission/compact 的逻辑所有权。

## 验证与风险

验证命令与结果见
`docs/reviews/evidence/M1-R01-v4/execution-04/slice-04a/verification.json`。
本片改变默认跨语言 terminal commit 顺序，按高风险项执行了两份相邻 Python
集成文件、TypeScript protocol/runtime/source-custody/E02 测试、E02 typecheck 和
G0 不可变清单复核。E04 全量 mutation、cleanroom、八域 E2E 与 candidate gate
仍由 04F 强制执行。

## 未关闭事项

- 八个能力域的上游源码恢复与逐 range provenance 尚未开始；由 04B-04E 处理。
- E01 全目录已知的 fake-capability host 兼容回归分配给 04C 的 permission/capability
  默认路径收束，不用本片协议修复掩盖。
- E04 terminal verdict 只能由专用独立审查任务书在最终 target commit 上给出。
