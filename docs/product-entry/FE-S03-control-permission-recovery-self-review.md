# FE-S03 命令、权限、幂等与断线恢复闭环：增量自审

## 结论

FE-S03 在 `62d11091527dd092f3d18acf6fd4d2871fa43130` 基线上完成，三个实现/硬化
commit 为 `bea4c566b734eb9b4533808b664565d8fe7af9f8`、
`21cfbdb3876fc0a58d01adf4b0a0f089d9fdba5d`、
`c8892f86f4aac29d1bd64a246fbc4ca00db6fc0f`。验收结论为 **PASS**。

运行中的交互 CLI 现在可以通过既有 command/permission contract 执行 queue、now/next/later、
cancel、continue、interrupt、redirect、retry、approve 和 deny；断线从最后确认 cursor 有界恢复，
gap/generation/cursor 冲突改走 canonical snapshot。daemon 默认保护活动任务，force stop 留下持久
audit。FE-S04 terminal transport、FE-S05 `zyra ui` 与 FE-S06 cleanroom 不在本片范围。

## Owner 与实现自审

| 检查项 | 结果 | 证据 |
|---|---|---|
| command owner | PASS | `CliControlSession` 复用 `@zyra/commands`；已发送 queue 只读 `PromptQueueRuntime` snapshot，无本地 canonical queue |
| priority/FIFO | PASS | now/next/later 进入真实 request；真实 API 阻塞 dispatcher 后读取服务端顺序 |
| idempotency | PASS | 同一 request 只产生一个 succeeded effect；permission-gated 重放返回同一 executionId 且 `replayed=true` |
| revision | PASS | mutation 前用只读 `/status` 取得 revision；409 显示 actual 且 `automatic_retry=false` |
| permission owner | PASS | custody、request、challenge、decision、permit 和 receipt 均由 `typescript.PermissionCoordinator` 拥有 |
| custody 安全 | PASS | token 仅存进程内并进入 Authorization header；不写 transcript、daemon audit 或 CLI state；重启需显式重新提供 |
| human intervention | PASS | 人工 allow/deny decision 累加 1；policy/system channel 保持 0；sealed 拒绝保持 0；未修改 allow/deny policy |
| cancel/continue/redirect/retry | PASS | task endpoint、command cancel endpoint、`/change` control request 和 canonical retry_of 均真实接通 |
| event recovery | PASS | 普通断流 cursor resume；gap/generation/cursor/order/binding 问题 snapshot replacement；6 次有界重试，无 polling |
| renderer 边界 | PASS | snapshot replay 以单一最大 canonical sequence 去重，没有无界 event-id 去重集合或第二 transcript owner |
| restart/parity | PASS | 新 Bun 进程以同一 task/session/custody 重查 pending；CLI 与 Web 看到相同 settled request/revision |
| daemon lifecycle | PASS | 活动 task 默认 stop 被拒并审计；force 返回 committed audit；pid/generation owner 仍只在 CLI |
| sealed | PASS | 高风险动作 5 秒内拒绝，human wait=false、human count=0，不自动批准 |
| terminal 边界 | PASS | 未新增 terminal listener/transport、未启用 terminal node、未声明真实 terminal dispatch |

## 必要 contract 兼容修正

真实主路径测试暴露了四个已存在但此前未被 CLI/Web 同时踩到的兼容点：

1. command identity 原用 `controlreq_`，但冻结 typed client 只接受 `request_`；统一为后者。
2. 服务端 revision 曾把 requested/validated/started 审计事件计入 revision，导致 mutation 在执行前
   使自己的 expected revision 失效；改为只读 canonical `session_control_revision`。
3. session open 实际返回 `zyra.permission-api.v1`，typed normalizer 只接受 v2 与 slash-v1；补入
   已部署 dot-v1，不新增 schema。
4. session open 实际字段为 `bearer_token`，CLI/Web 过去只覆盖 `custody_token`；两者兼容读取，
   token 仍不持久化。

这些修正没有转移 command、permission、session 或 event owner，也没有修改 permission policy。

## Claude CLI 参考转化

- `messageQueueManager` 的稳定 snapshot、priority 和 FIFO 被适配为服务端 queue 的呈现，不复制
  module-level queue。
- sticky permission wait 被适配为 request identity、risk、reason、deadline 与 canonical receipt；
  不采用本地 permission owner 或 skip-permission 开关。
- reconnect 状态和 draft 保留被适配为有界 cursor resume/snapshot replacement；不从屏幕文本恢复。
- `/exit` 仅解除输入监听，daemon/task/event owner 不跟随当前 REPL 退出。

实现落位已写入 `claude-cli-behavior-matrix.md` 6.2 节；完成含义是代码和真实测试，不是读完参考源码。

## 真实行为与负向证据

1. 真实 dispatcher queue 中 now、next、later 顺序由服务端返回；cancel 后 canonical item 为
   cancelled，retry 带原 request 的 `retry_of`。
2. CLI 与 Web 对同一 task 的 command revision 连续；过期 expected revision 被拒，CLI 不覆盖。
3. deny 不产生 permit；allow 产生 permit并实际完成同一 `/e02-reload`；重复请求只得到同一
   execution 的 replay receipt。
4. CLI 与 Web 读取同一 permission settlement；新进程用显式 custody 重新看到并解决 pending。
5. 过期 challenge 在客户端 proof 前拒绝；permission adapter 不可用时 query/resolve fail closed。
6. 中途断流从最后 server cursor 恢复；gap 用 snapshot replacement，已打印 event 不重复。
7. sealed 高风险动作不进入人工等待，确定性返回拒绝并保持 human intervention count 为 0。
8. daemon stop 面对活动 task 默认拒绝并记录 rejected audit；force stop 记录 committed audit。
9. 已有 signal/cancel 集成继续证明显式取消作用于服务端 task，而非只结束本地 renderer。

## 验证记录

- CLI：typecheck、build 通过，`29 passed`。
- `@zyra/commands`：typecheck 通过，`29 passed`。
- typed API client：typecheck 通过，`19 passed`。
- Web permission：typecheck 通过，`16 passed`。
- TypeScript permission runtime：`20 passed`。
- FE-S03 真实 command/permission/restart/parity：`2 passed in 35.90s`。
- canonical command queue：`2 passed in 30.56s`。
- current permission console API：`2 passed in 12.75s`。
- CLI/daemon/FE-S01 邻接回归：`6 passed in 147.80s`。
- `git diff --check`：通过。

旧 `test_api_control_commands.py` 中两条用例仍断言 Gate 00A 前的 Python `result/tool_result`
响应或已退休 shell route；它们在当前基线上与 TypeScript cutover contract 不兼容。本片没有恢复旧
owner/path，而以当前 permission console、runtime behavior 与真实 CLI/Web E2E 作为权威回归。

## 分桶

| bucket | 增量 | 说明 |
|---|---:|---|
| production | 14 files / +1323 -59 | CLI control/permission/recovery/daemon，Web compatibility，API revision 与 permission audit count |
| test | 6 files / +791 | CLI 单测、shared contract、runtime behavior、真实 Python 集成 |
| docs | 5 files | CLI README、两份矩阵、本自审、机器证据 |
| runtime-assets | 0 | 无 vendor/参考仓库复制 |
| generated | 0 tracked | build 产物未入 commit |
| data | 0 | 无 dataset/checkpoint |
| adapter-only | 0 | 无空 adapter 冒充闭环 |
| mock/fixture | unit-only | 完成条件含真实 API/runtime/daemon/Web/独立进程证据 |

## 风险与后继边界

- custody token 不持久化是安全边界；重启后没有显式 token 时仍可观察 task/event，但 permission
  resolve 会 fail closed。
- daemon `--force=true` 是显式破坏性覆盖，audit 只证明谁/何时/哪些 task 被覆盖，不伪装优雅取消。
- FE-S04 仍需把 terminal listener 的取消、失效和注销接到这里的正式 command/daemon 语义。
- FE-S04 只是下一候选，未获得执行授权。

机器可读证据见 `docs/product-entry/evidence/FE-S03-control-permission-recovery.json`。
