# Phase B 成熟度与验收基线

状态：实施中
日期：2026-09-01

## 1. 基线事实

- Codex TUI 参考树：`codex-rs/tui/src` 下约 483 个 Rust 文件、247,568 行（本机 `rg --files` + 行数统计）。行数不是目标，但说明参考产品由多个长期演进的状态机和组件组成。
- Zyra CLI：`apps/cli/src` 下 34 个 TypeScript 文件、约 9,295 行；其中产品 TUI 是既有 CLI/daemon/automation 能力上的 Foundation。
- 当前结构性判定：Phase A 完成；Phase B 尚未退出；Phase C～H 未完成。

## 2. Phase B 冻结的实现顺序

1. 有界、可重放、版本化的 `ProductSessionState`。
2. 长期存活的 `ProductSessionController`，统一 new/submit/queue/redirect/interrupt/resume/detach。
3. 终端 input/layout/viewport/overlay 内核和 PTY harness。
4. command registry、palette、文件引用与 session picker。
5. Markdown/streaming transcript、计划、工具、权限、diff、verification 组件。
6. presentation v2 和缺失后端契约。
7. 故障恢复、性能/安全、安装升级诊断。
8. 真实长程负载和发布演练。

## 3. P0 性能与容量门

沿用任务书第 15 节，不下调：

- 60～200 列 resize 正确；1,000 次 resize 通过。
- 10,000 transcript items 下可输入/滚动/恢复。
- 100,000 presentation events 重放后屏幕状态有界。
- 8 小时 soak 无 crash、terminal corruption 或明显无界 RSS 增长。
- 100 次断线恢复、100 次重复启动/退出通过。
- 本地健康环境启动至可输入 P95 ≤ 2s；输入/局部重绘 P95 ≤ 50ms，长 transcript ≤ 100ms。
- 10,000 条历史稳定 RSS 初始目标 ≤ 512 MiB。

任何硬件/环境导致无法测量的项目必须记录环境和未运行原因，不得改写为通过。

## 4. P0 安全门

- 权限 custody 不可用时 fail closed。
- token、credential、custody secret、capability URL 不进入 UI、诊断包或 Web URL。
- workspace path traversal、symlink escape、OSC/CSI/ANSI 注入都有回归。
- canonical failure、verification failure 和 not-run 不得显示为 success。

## 5. 真实负载状态覆盖

Phase G 至少三次长程运行合计覆盖：

- running + plan/tool/subagent activity；
- permission request/decision/conflict 或 expiry；
- workspace changes + multi-file diff + verification pass/fail/not-run；
- queue/redirect/interrupt/cancel/continue 中至少两类；
- CLI detach/crash 后 resume；
- SSE disconnect/cursor gap/generation change/daemon restart 中至少两类；
- canonical completed、failed、cancelled/blocked 中至少两种终态。

任务是否解决不参与前端评分；UI 的一致性、可控制性、恢复性、响应性和安全性参与评分。

## 6. 证据目录约定

- 设计与矩阵：`docs/product-tui/`
- 自动化 harness：`scripts/product-tui/`
- 确定性 fixture：`apps/cli/test/fixtures/`
- snapshot/golden：`apps/cli/test/snapshots/`
- 真实运行脱敏报告：`docs/product-tui/evidence/<date>/<run-id>/`

真实报告必须包含 commit、命令、环境、task/run/session ID、时间、事件规模、注入动作、canonical 终态、TUI 判定和未运行项。

## 7. Phase B 当前未关闭项

- 本地 Codex checkout 缺 `@openai/codex-win32-x64`，真实 Codex PTY 未运行。
- Zyra 启动/退出真实 PTY 基线已生成，并发现 Ctrl+C 后进程不退出的 P0；完整交互基线等待 Phase C harness。
- T1/T2/T3 历史真实 CLI 轨迹已完成只读状态统计；permission、diff、verification 和 transport fault 仍需新运行补齐。
- P2/N/A 例外尚未逐项批准，Phase H 前必须关闭。
