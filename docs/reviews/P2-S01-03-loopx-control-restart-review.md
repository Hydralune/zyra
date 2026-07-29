# P2-S01-03 LoopX 控制面、状态面板与重启恢复增量自审

- Slice：`P2-S01-03`
- 基线 commit：`5b5da14511d4615282a4f969321eb368c5a8d8d6`
- 实现 commit：`a9e3dec92bee73cb6405c3bad80e4da181899008`
- 自审日期：`2026-07-29`
- 结论：`PASS`

## 1. 交付结论

本 slice 已把 S01-02 的 durable outbox/ACK bridge 接入真实产品控制面，并完成独立 OS 进程重启恢复。CLI、版本化 API 和既有长程任务工作台共用同一个 Zyra-owned control runtime；所有变更操作先经过 permission 和 `GraphStateCustody`，再由 bridge outbox 投递到固定版本 LoopX，并以真实 ACK、dead-letter、事件和 artifact receipt 作为结果。

LoopX 私有 goal、todo、claim、quota、history 没有进入 Zyra canonical task/lease/budget owner。Zyra 只保存任务 continuation 投影、bridge cursor 和 receipt。任务图在物理 worker dispatch 前执行 continuation gate，能用 mutation/disable 测试证明该机制改变了实际 worker 与 budget 行为，而不是只增加展示状态。

## 2. 实现范围

### 2.1 CLI 与 API

- 注册 `/loopx-connect`、`/loopx-status`、`/loopx-disconnect`、`/loopx-todos`、`/loopx-todo`、`/loopx-claim`、`/loopx-release`、`/loopx-quota`、`/loopx-interaction`、`/loopx-sync`、`/loopx-retry`。
- 新增 `GET /tasks/{task_id}/loopx` 和 `POST /tasks/{task_id}/loopx/commands`。
- 变更命令经过 `loopx.control` permission、typed API 幂等 receipt、canonical graph metadata commit、durable outbox、LoopX dispatcher 和 ACK。
- API 同时返回 typed transport receipt 与 `sync_receipt`，避免把 API 提交成功误报为 LoopX ACK。
- `expected_cursor` 在 canonical mutation 前检查；旧 cursor 返回冲突且不产生 claim、lease 或 budget 副作用。

### 2.2 控制状态与 owner 边界

- 新增版本化 `zyra.loopx-control-state/v1` 投影。
- 私有面：goal、todo、claim、quota、history、connected。
- Zyra canonical 面：task revision/status、worker lease、execution budget。
- 同步面：pending、acked、dead-letter、cursor、最后 receipt、degraded reason。
- 生命周期显式区分 `enabled`、`degraded`、`disabled`；损坏和版本不匹配不会返回伪成功。

### 2.3 真实任务语义

- `DynamicTaskGraph` 在物理 dispatch 前读取 LoopX continuation。
- continuation 被拒绝时，节点进入 `BLOCKED`，产生 `zyra.loopx-continuation-blocked/v1`，不分配 worker、不消耗 tool/budget，并走确定性 recovery。
- continuation 被接受时，goal/todo/obligation 进入 worker constraints。
- disable 路径恢复原有 baseline；测试证明启用/断开机制时 worker 行为发生可解释差异。

### 2.4 既有工作台

- LoopX 面板挂载到既有 `TaskDetail` 长程任务工作台，没有创建第二套 console。
- 面板分别展示私有控制状态与 canonical task/lease/budget。
- connect、interaction、claim/release、retry、disconnect 全部调用真实 typed backend；浏览器本地状态不冒充 canonical mutation。
- 前端提交协调器合并重复点击，并绑定 `expected_cursor` 和稳定 idempotency key。

### 2.5 重启与恢复

- 集成测试启动两个相互独立的 Python OS 进程，使用同一持久化目录和环境。
- 第二个进程恢复 workspace、goal、todo、claim、quota、history、outbox cursor 和 continuation。
- 对私有投影写入未知 schema version 后，状态返回 `degraded`。
- `sync retry` 在 single-writer fence 下重放最新 ACKed command，重建私有投影；重复 retry 不产生额外语义效果。
- 固定版本运行时加载时禁止写 bytecode，避免 immutable 安装目录被 `__pycache__` 改写而导致重启完整性校验失败。

## 3. 失败与恢复覆盖

| 场景 | 失败证据 | 可执行恢复 | 结果 |
|---|---|---|---|
| 旧 cursor | HTTP 409，mutation 前拒绝 | 刷新状态后使用新 cursor | 无 claim/lease/budget 副作用 |
| claim 冲突 | bridge dead-letter/冲突结果 | 正确 claimant release 后 `sync retry` | dead-letter 清零，新 claimant ACK |
| quota 耗尽 | applied spend 为 0，状态保持 exhausted | reconnect 调整私有 limit | quota 恢复可用 |
| sync degraded | lifecycle/dead-letter 显式展示 | `sync retry` | ACK cursor 前移或保持幂等 |
| 私有状态损坏/版本不匹配 | lifecycle=`degraded` | 重放最新 ACKed bridge command | 精确重建并恢复 `enabled` |
| disconnect | continuation 关闭 | reconnect | baseline 与 LoopX gate 行为可区分 |
| sealed permission | mutation 确定性拒绝 | 由外部改变 permission 状态后重试 | 零伪 ACK、零人工等待伪装 |

## 4. 状态 owner 自审

- `GraphStateCustody` 仍是 task graph metadata mutation 的唯一提交者。
- LoopX bridge 只在 canonical commit 之后 enqueue，未直接修改共享 graph object。
- LoopX claim 没有创建或释放 Zyra worker lease。
- LoopX quota 没有覆盖 Zyra execution/provider/tool budget。
- durable outbox、single-writer、ACK/dead-letter 和 restart cursor 仍由 Zyra bridge 拥有。
- 固定版本 LoopX 包仍来自 S01-01 的仓库内 runtime-assets；没有 `../long-horizon-systems` 运行依赖，也没有引入 OpenClaw。

## 5. 增量有效代码审计

实现 commit 共 `2542` 行新增、`6` 行删除：

| 分类 | 新增 | 删除 | 说明 |
|---|---:|---:|---|
| production/config | 1721 | 6 | API、CLI、typed client、control runtime、bridge、task graph、工作台与测试命令配置 |
| test | 821 | 0 | Python 集成/重启测试与 Web feature 测试 |
| runtime-assets | 0 | 0 | 复用 S01-01 固定版本包 |
| generated | 0 | 0 | 无生成物计入实现 |
| data | 0 | 0 | 无 dataset/checkpoint |
| docs | 0 | 0 | 本自审与证据在后续独立 docs commit |
| adapter-only/mock/fixture | 0 | 0 | 没有以 adapter、mock 或固定成功轨迹冒充主路径 |

## 6. 验证结果

- `pytest` LoopX control/restart/bridge 与 task graph 相邻回归：`18 passed`。
- Web typed client 与 LoopX feature：`21 passed`。
- Web TypeScript typecheck：通过。
- Web production build：通过，`375 modules`。
- Python compileall：通过。
- `git diff --check`：通过。
- Phase 2 policy：`valid=true`，目标 commit 为实现 commit；`phase2_strongest_v1` 仍按既定门禁保持未激活。
- internalization ledger：`audit_ok=true`，`errors=0`，`blockers=0`。

全量 `test:web` 为 `274 passed, 2 failed`。两个失败都位于既有 `apps/web/test/mcp-panel-integration.test.ts`，其 elicitation fixture 固定在 `2026-07-25T13:00:00Z` 过期，而执行日期为 `2026-07-29`；LoopX 定向测试在同一命令中全部通过。该问题不属于本 slice，未追溯修改。

尝试运行整个既有 `tests/integration/test_api_control_commands.py` 时在 304 秒超时；拆分的相邻用例中 4 个通过，既有 `/help` 用例因 TypeScript E02 响应不再包含旧 Python `groups` 字段而失败。该行为与 LoopX 路由无关，本 slice 未扩大范围修复。

## 7. 最终裁决

本 slice 的控制、状态、真实 ACK、permission、幂等、冲突、quota、degraded、corruption、restart 和任务语义要求均有生产代码与行为测试。未发现当前 slice blocker，裁决为 `PASS`。

根目录 `G:\agent-zoo\docs\phase2\slice-01-03-loopx-control-restart.md` 及其他 Zyra 仓库外文档未修改，不会随 Zyra commit 自动提交。
