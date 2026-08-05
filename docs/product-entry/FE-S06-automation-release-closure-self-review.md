# FE-S06 自动化、真实场景与发布收口：增量自审

## 当前结论

FE-S06 已完成 CLI/automation/release 代码建设和本地真实路径回归，但截至本 evidence commit
仍有两个不能由实现侧自行绕过的最终验收入口：

1. 官方双域 sealed 场景需要读取本机三个已配置 provider 凭据，并向智谱、DeepSeek、Kimi
   以及场景指定的 RFC/IANA/MDN 公共来源发送任务内容；当前没有这项具体外发授权。
2. Web 产品面/证据面的浏览器级验收需要可连接的浏览器实例；本次浏览器运行时枚举结果为空。

因此本文不把 FE-S06 写成 completed，也不继承旧 HEAD 的 sealed/browser 结论。其余已完成项、
失败修复和阻断边界如下。

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

## 八入口与输出契约

| 入口 | owner 与最终行为 | 自动化证据 |
|---|---|---|
| `zyra` | 当前目录交互、terminal listener 临时生命周期 | CLI full suite + terminal integration |
| `zyra "goal"` | 创建真实 task，snapshot/SSE/cursor 观察 | real API integration |
| `zyra run` | goal/file/stdin，strict JSONL，0..5 exit | unit + real piped-stdin API integration + release probe |
| `zyra resume` | task-backed session/cursor/snapshot 恢复 | CLI recovery + real API integration |
| `zyra ls` | canonical task/session projection | CLI contract tests |
| `zyra scenario` | daemon scenario lifecycle/evidence | scenario/dual-domain integration；最终 sealed 外发仍待授权 |
| `zyra ui` | 复用 daemon，启动既有 Web 产品路由 | launcher、HTTP route、跨入口 integration；浏览器实例不可用 |
| `zyra daemon` | pid/generation/readiness 与 active-task stop guard | daemon integration |

退出码固定为 0 success、1 task/scenario failure、2 usage/input、3 daemon/API、4 cancel/signal/
permission wait、5 verifier/completion gate。pipe consumer 离开不会取消服务端 task；CLI listener
退出不会停止 daemon/task。

## 回归与候选 release 证据

- CLI 全量：`42 passed / 298 assertions`（包含无隐藏命令、stdin/file、JSONL、EPIPE、
  terminal、UI launcher 回归）。
- Web 全量：`309 passed / 1,874 assertions`。
- 全仓 TypeScript typecheck：通过；Web production build：通过。
- release/product-entry Python 单元组：`54 passed`；legacy retirement：`7 passed`。
- CLI real piped stdin -> API/runtime：`1 passed`。
- CLI/Web/terminal/registry/failover/sealed-exclusion 真实集成组：`31 passed`。
- recovery/topology 邻接新路径：`4 passed`；两个旧 M2 dual-domain 测试因冻结 harness 与当前
  formal owner/work-unit contract 不一致而失败，没有追溯改写第一阶段测试。
- Bun/Node 双入口 Node artifact：字节双构建一致、依赖闭包只有 `@zyra/cli`、
  `@zyra/commands`、`@zyra/typed-api-client`，Windows passed；Linux/macOS host unavailable。
- candidate release graph 生成了两份字节一致的 46,183,082-byte zip 和五类 admission receipt；
  前台工具会话中断后父进程没有写出最终 `ci-report.json`，因此只记录 bundle digest 与 partial
  receipts，不声称 14-gate admission PASS，最终 HEAD 必须重跑。

## 十个最终场景状态

| # | 场景 | 当前证据 | 判定 |
|---:|---|---|---|
| 1 | CLI 创建，Web 观察同 task/run | real cross-entry HTTP integration | PASS |
| 2 | Web/CLI 控制 revision/permission/receipt 一致 | command/permission parity integrations | PASS |
| 3 | CLI 退出，daemon/task 继续，terminal 真实 failover | daemon + terminal cross-language integration | PASS |
| 4 | 重开从 cursor/snapshot 恢复，无重复副作用 | recovery/idempotency integration | PASS |
| 5 | software-delivery clean state + verifier artifact | 官方 final sealed run 待外发授权 | BLOCKED |
| 6 | cross-source-research clean state + verifier artifact | 官方 final sealed run 待 provider/公共来源外发授权 | BLOCKED |
| 7 | sealed human=0、terminal dispatch=0 | 现有 exclusion regression 通过；同最终 HEAD 官方 sealed run 待授权 | BLOCKED |
| 8 | interactive terminal 真实 dispatch，LOCAL | TypeScript listener -> Python registry/router real HTTP | PASS |
| 9 | 异常/需求变化/节点失效后自主恢复 | recovery/failover integrations | PASS；最终双域 sealed receipt 待授权 |
| 10 | Web drill-down 至六类关键证据 | production/evidence HTTP route 通过；浏览器实例为空 | BLOCKED |

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

## 剩余授权边界

在用户明确授权指定 provider/公共来源外发、并提供一个可连接浏览器实例前，FE-S06 保持
`in_progress`。不得通过跳过 provider、复用旧 sealed receipt、静态 Web 数据、HTTP marker
或源码审阅把这两项改写成通过。
