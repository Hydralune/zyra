# FE-S06 自动化、真实场景与发布收口：增量自审

## 当前结论

FE-S06 已完成 CLI/automation/release 代码建设和本地真实路径回归。用户在 2026-08-05
明确授权本次官方双域 sealed 场景使用三个已配置 provider，并将冻结任务内容发送到智谱、
DeepSeek、Kimi 及场景指定的 RFC/IANA/MDN 公共来源；绑定候选 `90fd8e0` 的两个 run 均由
独立 verifier 判定通过。

用户同时明确选择按 Codex CLI 环境收口：浏览器控制运行时没有可连接实例，因此浏览器点击/
截图保持 `unavailable`，不宣称 browser PASS；Web 全量测试、production build、真实 HTTP
产品/证据路由和 CLI/Web 跨入口集成作为可执行替代证据。该限制不再阻断 FE-S06，但会保留在
最终 verdict 中。

`9be019e` 的全新 clean worktree 已通过 release policy 使用的完整 Python 门：1,398 passed、
15 deselected、80 subtests passed。当前只剩本 evidence 更新提交形成的最终 HEAD release
admission 与 sealed 复验；在其完成前本文仍不把 FE-S06 写成 completed。

## 实现与修复

- `zyra run` 支持 goal、UTF-8 文件或 non-TTY stdin 三选一；stdin 上限为 256 KiB，空输入、
  TTY 缺失输入、超限和文件失败统一映射 usage exit 2。
- stdout 继续只由一个 JSONL writer 持有；capability path、URL userinfo、credential/token 字段、
  内嵌 Windows/Unix 绝对路径和 ANSI escape 在公开输出前脱敏。
- 裸 `help`、`version` 隐藏子命令已移除；标准 `--help/-h`、`--version/-V` 保留，help receipt
  与 Bun/Node release probe 都要求 `command_count=8`。
- 新增 `scripts/verify_product_entry_release.py`：连续两次构建 Node artifact，比较字节 digest，
  执行 Bun direct/Node built 双入口，核对八命令面、JSONL、依赖闭包和无 DOM/React/TUI/
  `claude-code-best` runtime。
- productization cleanroom 和 Windows/Linux platform plan 均调用该 verifier；release doctor 增加
  Node 前置检查。分发范围仍是离线 bundle，不发布 npm、不做 installer 或 code signing。
- legacy-source release gate 原先把 `provenance/**` 冻结证据副本误判为 runtime source copy；
  现在只把该路径排除出 blob runtime 判定，forbidden source-pool path 检查保持不变，并有负向
  回归测试。
- sealed runner 以进程内 reservation 原子保留端口块，避免并行双域场景获得相同 endpoint；
  deployment reset 会关闭自己拥有的真实节点，Windows venv launcher/runtime PID 分离也由
  authenticated health identity 重新绑定。
- CLI daemon 通过 generation-bound handoff 文件和 `/health` process identity 绑定真实 Python
  runtime PID；status/stop 不再把短命 venv launcher 当作 daemon。
- pytest API cleanup 纳入 deployment owner，workspace import 测试恢复原模块对象；测试 bootstrap
  覆盖 pyproject 声明的全部源码根，并断言关键包来自当前 worktree，消除主仓库 editable 包混入。

## 八入口与输出契约

| 入口 | owner 与最终行为 | 自动化证据 |
|---|---|---|
| `zyra` | 当前目录交互、terminal listener 临时生命周期 | CLI full suite + terminal integration |
| `zyra "goal"` | 创建真实 task，snapshot/SSE/cursor 观察 | real API integration |
| `zyra run` | goal/file/stdin，strict JSONL，0..5 exit | unit + real piped-stdin API integration + release probe |
| `zyra resume` | task-backed session/cursor/snapshot 恢复 | CLI recovery + real API integration |
| `zyra ls` | canonical task/session projection | CLI contract tests |
| `zyra scenario` | daemon scenario lifecycle/evidence | 双域官方 sealed：2 passed / 0 failed，独立 verifier 通过 |
| `zyra ui` | 复用 daemon，启动既有 Web 产品路由 | launcher、HTTP route、跨入口 integration；浏览器实例 unavailable（用户接受 CLI 环境限制） |
| `zyra daemon` | pid/generation/readiness 与 active-task stop guard | daemon integration |

退出码固定为 0 success、1 task/scenario failure、2 usage/input、3 daemon/API、4 cancel/signal/
permission wait、5 verifier/completion gate。pipe consumer 离开不会取消服务端 task；CLI listener
退出不会停止 daemon/task。

## 回归与候选 release 证据

- CLI 全量：`42 passed / 298 assertions`（包含无隐藏命令、stdin/file、JSONL、EPIPE、
  terminal、UI launcher 回归）。
- Web 全量：`309 passed / 1,874 assertions`。
- 全仓 TypeScript typecheck：通过；Web production build：通过。
- `9be019e` 全新 clean worktree、release policy 原样 Python 门：`1,398 passed / 15 deselected /
  80 subtests passed`，72 分 3 秒；三项 collection warning 不影响结果。
- 已知污染源后接历史失败用例：`237 passed`；生产调度/恢复/provider/backend 级联复验：
  `63 passed`；clean-worktree CLI daemon 与 product supervisor 复验：`3 passed`。
- release/product-entry Python 单元组：`54 passed`；legacy retirement：`7 passed`。
- CLI real piped stdin -> API/runtime：`1 passed`。
- CLI/Web/terminal/registry/failover/sealed-exclusion 真实集成组：`31 passed`。
- recovery/topology 邻接新路径：`4 passed`；两个旧 M2 dual-domain 测试因冻结 harness 与当前
  formal owner/work-unit contract 不一致而失败，没有追溯改写第一阶段测试。
- Bun/Node 双入口 Node artifact：字节双构建一致、依赖闭包只有 `@zyra/cli`、
  `@zyra/commands`、`@zyra/typed-api-client`，Windows passed；Linux/macOS host unavailable。
- `90fd8e0` release graph 生成两份字节一致的 46,195,645-byte zip，SHA-256
  `a5f43a7f635420ff8afaceb6d75574410cccd3b5984b288f537bec684d83908b`；14 个 mandatory gate
  中 13 个通过，唯一失败为当时的 Python regression，随后稳定 contract 与测试隔离根因均已修复。
  该历史 candidate 不冒充最终 admission PASS，最终 evidence HEAD 必须重跑。
- 官方双域 sealed 候选 `90fd8e0`：2 runs、0 failed、human intervention 0；software-delivery
  3,232 个有效 transition，cross-source-research 7,437 个有效 transition，二者 invalid=0、
  unsafe commit=0，local/edge/cloud real gate 均关闭，三个 cloud provider 均返回 HTTP 200；
  index digest `57b360a521708554d174216ec3792841fc53eb5db111ac92e0133306229aaad5`。

## 十个最终场景状态

| # | 场景 | 当前证据 | 判定 |
|---:|---|---|---|
| 1 | CLI 创建，Web 观察同 task/run | real cross-entry HTTP integration | PASS |
| 2 | Web/CLI 控制 revision/permission/receipt 一致 | command/permission parity integrations | PASS |
| 3 | CLI 退出，daemon/task 继续，terminal 真实 failover | daemon + terminal cross-language integration | PASS |
| 4 | 重开从 cursor/snapshot 恢复，无重复副作用 | recovery/idempotency integration | PASS |
| 5 | software-delivery clean state + verifier artifact | 3,232 valid、0 invalid、artifact/verifier digest 绑定 | PASS（最终 HEAD 待复验） |
| 6 | cross-source-research clean state + verifier artifact | 7,437 valid、0 invalid、三个 provider 真实 200 | PASS（最终 HEAD 待复验） |
| 7 | sealed human=0、terminal dispatch=0 | human=0；官方 runner 不启动产品 terminal listener；既有 terminal exclusion regression 通过 | PASS（最终 HEAD 待复验） |
| 8 | interactive terminal 真实 dispatch，LOCAL | TypeScript listener -> Python registry/router real HTTP | PASS |
| 9 | 异常/需求变化/节点失效后自主恢复 | 两域均观察 edge loss、safe fail-closed recovery 和 artifact continuity | PASS（最终 HEAD 待复验） |
| 10 | Web drill-down 至六类关键证据 | production/evidence HTTP route、Web full test、cross-entry 通过；浏览器实例为空 | PASS_WITH_CODEX_CLI_BROWSER_UNAVAILABLE |

## Claude CLI 学习完成门

`claude-cli-behavior-matrix.md` 的七类行为均已有 `ADOPT`、`ADAPT` 或 `REJECT` 裁决，
adopted/adapted 项均指向 Zyra production path 与测试，rejected 项有 owner/security 原因和
替代交互。80/120 列、长 transcript、tool stream、permission、reconnect、search、scrollback、
pipe/tee 已进入 CLI/真实 API 测试。生产依赖闭包没有参考仓库、React、DOM 或 TUI。

## 分桶

| bucket | 增量 | 说明 |
|---|---:|---|
| production | CLI 4 modified；release 3 modified/new；audit 1 modified | stdin、脱敏、八入口、Node artifact verifier、cleanroom 与 evidence audit |
| test | TS 2 modified；Python 4 modified/new | CLI input/output、real stdin、release/cleanroom、legacy evidence boundary |
| docs | README/contract/matrix/self-review/evidence | 最终命令面、参考转化、发布与 blocker 记录 |
| runtime-assets | 0 | 无 vendor/runtime 源码加入 |
| generated | 0 tracked | dist、cleanroom、pytest 和 release outputs 不提交 |
| data | 0 | 无 dataset、label、checkpoint |
| adapter-only | 0 | 关键结论由真实 API/process/HTTP/build/receipt 路径支撑 |
| mock/fixture | release 纯审计单测和 CLI fake streams | 只锁定失败边界；不替代 sealed、browser、terminal 或 release receipt |

## 剩余收口边界

外部 provider/公共来源授权已取得并完成候选验证；浏览器限制已由用户明确选择按 Codex CLI
环境记为 `unavailable`。FE-S06 仅在 evidence commit 后的最终 HEAD 再次通过双域 sealed 与
完整 release admission 后才能置为 completed。不得复用候选 receipt 冒充最终 HEAD 结果。
