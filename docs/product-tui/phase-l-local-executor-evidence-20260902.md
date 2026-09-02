# Phase L：默认本地执行环境证据（2026-09-02）

## 判定

默认产品 CLI 已不再把启动目录扫描、Base64 编码并逐文件上传到 managed workspace。发布构建现在注册一个只监听 loopback、带 generation 与一次性 capability proof 的本地 terminal executor；task 持久化精确的 backend/generation/cwd/workspace-roots 摘要，但不持久化 proof。CodeWorker 的文件读取、写入、编辑、删除、Shell 与命令级验证均经类型化 action 直接作用于启动工作树。

Phase L 的机器可执行实现门已通过：自动契约、10 万文件工作区真实 provider 多轮任务、真实 daemon smoke、Windows ConPTY、性能门、CLI/Web 回归和构建均通过。实际 Zyra 源码仓库的外部 provider 文件读取仍不在无人值守测试中执行；该步骤可能把仓库内容发送给第三方 provider，必须在明确的数据共享授权下单独完成，不能用合成工作区结果冒充。

## 架构与安全边界

- 产品入口把规范化 `cwd`、`workspace_roots`、terminal backend id、terminal generation 和 binding digest 绑定到 task 创建与运行请求。
- API 在 task 创建和运行时分别校验当前存活的精确 terminal、generation、root 与 capability proof；proof 只存在于 CLI 进程内存和请求头/体边界，未写入 task/event/artifact。
- 调度器只向 task 绑定的 terminal backend 派发 action；path 必须位于允许 root，Shell 也以同一 cwd 执行。
- terminal-backed 本地执行只证明 root identity，不枚举整棵目录；managed local/container workspace 仍保留完整 entry manifest attestation。
- “封闭自治”继续使用 managed 隔离边界，API 明确拒绝把 CLI local executor 注入 sealed task；模式 picker/status 已明确显示“不挂载 CLI 当前目录”，避免把尚未实现正式 transfer protocol 的隔离工作区伪装成本地代码会话。
- changed paths、mutation digest、命令退出码与 verification receipt 回到 canonical task/presentation；默认本地路径不再 stage 或 materialize 整个工作树。
- 同一产品 conversation 的连续 task 在前一 task 已进入 terminal 状态、session/run/task 精确匹配且旧 bearer 验证成功时，原子移交 permission custody；旧 task binding 随即失效，明文 token 不落盘也不重新签发。
- daemon 自动启动会避开已占用的三端口 deployment profile，并按 daemon generation 隔离 API/runtime 状态。

## 真实 provider 证据

### 小型隔离工作区

- `task_781c290f20d0`：真实 provider 读取测试输入并直接创建 `proof.txt`。
- 物理结果为 23 bytes 的 `ZYRA_LOCAL_EXECUTOR_OK\n`；canonical delivery verifier 通过，TUI 显示文件变更和最终验证后回到 composer。

### 100,001 文件合成大工作区

工作区 `G:\agent-zoo\.zyra-tui-synthetic-100k` 由测试生成，只包含 100,000 个空文件和一个 `package.json`，没有用户源码或凭据。`package.json` SHA-256 为 `D88816C2534B6927FAF089974DC5874DD377F55FF6838E3EC6A7E6C707AC628F`。

第一次运行成功完成文件写入，但连续 turn 暴露出三个产品缺陷：指代句“只回复其中的内容”被误编译为字面回答、Markdown 吞掉标识符内部下划线、permission custody 仍绑定前一 task。对应失败观测为 `task_4e62103d2761`（首轮成功）和 `task_169bf92809da`（错误续轮）；它们不计作退出门成功证据。

修复后从发布构建重新冷启动并在同一 TUI/同一 canonical session 中连续运行：

| turn | task / run | 结果 |
|---|---|---|
| 创建并回读 | `task_eeb8efc25302` / `run_fb8ed96468b1` | `completed`；直接创建 23-byte `proof.txt`；最终 verifier `decision_a7a2ffaf4fc2` 通过 |
| 读取并精确回复 | `task_217b05d1dbe8` / `run_691178310f5c` | `completed`；canonical/TUI 最终回答均为 `ZYRA_SYNTHETIC_100K_OK`；最终 verifier `decision_f04b40941637` 通过 |

两次 task 的 `query_session_id` 均为 `session_01a06283d58d000_2297712668277f23cdcd`，executor cwd 均精确为合成根目录。两次 canonical workspace usage 均为 `file_count=0`、`used_bytes=0`，证明 task 启动没有复制或上传 100,001 个文件；第二轮没有 permission custody warning，下划线在 TUI 中完整保留。CLI 用 Ctrl+C 正常退出并返回 exit code 0，daemon 随产品生命周期停止。

实际 `G:\agent-zoo\zyra` 中的 `task_2077df5f7f36` 与 `task_d80247fe4043` 另行证明默认自动 daemon 能绑定该 cwd，且 task workspace usage 仍为 `file_count=0`、`used_bytes=0`。这些记录只证明零上传与绑定，不声称完成了第三方 provider 源码读取。

## 当前回归结果

| 门 | 命令 | 结果 |
|---|---|---|
| CLI 全量 | `bun test .\apps\cli\test` | 200 pass，0 fail，959 expect，22 files |
| Python 契约/集成 | 目标化 `pytest`：goal、terminal contract/action、backend registry、CodeWorker、permission console/control | 70 passed，113.84 s |
| Web 对账 | `bun run test:web` | 320 pass，0 fail，1916 expect，29 files |
| 全仓类型 | `bun run typecheck` | pass；runtime、memory、typed client、commands、CLI、Web |
| 产品构建 | `bun run build` | pass；CodeWorker Bun/Node 各 275 modules、CLI 101 modules / 1.0 MB |
| Web 构建 | `bun run build:web` | pass；385 modules |
| 真实产品 smoke | `bun scripts/product_tui_smoke.ts --base-url=http://127.0.0.1:8141 --goal=请只回复ZYRA_SMOKE_FINAL_OK` | pass；真实 provider/canonical final/TUI replay 一致，产品帧无内部 task/run/session id |
| Windows ConPTY | `bun run product-tui:conpty` | pass；1,000 resize，startup 1473.784 ms，exit 268.944 ms，无 alternate screen |
| 性能 | `bun run product-tui:perf` | pass；100,000 events / 10,000 messages，input P95 0.006 ms，repaint P95 22.616 ms，RSS peak 165.516 MiB |

Smoke harness 同步完成产品化修正：不再要求从用户界面抓取内部 `task_*` ID，而是通过 canonical API 以本次新增 task、精确 goal 和精确 executor cwd 定位记录；同时把内部 ID 泄漏作为失败。

## 未由自动化代签的门

- 在实际 Zyra 源码仓库中让外部 provider 读取/修改源码：需要明确第三方数据共享授权；当前仅完成该目录的零上传和 executor 绑定证据。
- Windows Terminal 人工 IME 候选窗验收。
- 两名未参与实现者的 Codex/Zyra 同类任务盲测。
- 当前提交的隔离 clean-install/release archive；必须在实现提交固定后用精确 commit 生成，不能沿用历史候选结果。
- 官方 Codex 登录后的实机参考序列，以及 Linux/macOS 实机兼容结果。

以上项目不否定 Phase L 的机器实现，但在人工/发布门完成前仍不得宣称整份产品 TUI 达到 Codex 等价或最终发布状态。
