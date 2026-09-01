# Phase F 终端与性能硬化证据（2026-09-01）

## 结论

产品 TUI 的组件级 8 小时稳定性门、10k transcript / 100k presentation event、有界内存与延迟门已经真实执行并通过。该结论证明 renderer/state/composer 的长时有界性，不替代 Phase G 的真实 daemon/provider/长任务验收。

## 8 小时 component soak

源提交：`2aed010bf00e7b1a1a4f1b6def76a297294c0f94`（产品代码相对性能实现提交 `2ff5c482` 无后续变更）。

执行命令：

```powershell
.\node_modules\.bin\bun.exe scripts\product-tui\performance_gate.ts `
  --iterations 200 `
  --soak-seconds 28800
```

真实结果：进程退出码 `0`，`all_passed=true`。

| 指标 | 结果 | 门槛 |
|---|---:|---:|
| retained messages | 10,000 | 精确 10,000 |
| retained activities | 2,000 | 不高于 2,000 |
| replay input | 100,000 events | 100,000 events |
| input P95 | 0.007 ms | ≤ 50 ms |
| repaint P95 | 10.749 ms | ≤ 100 ms |
| soak repaint P95 | 17.238 ms | ≤ 100 ms |
| initial RSS | 144.207 MiB | — |
| stable RSS | 158.066 MiB | — |
| maximum RSS | 281.742 MiB | ≤ 512 MiB |
| measured soak growth | 0 MiB | ≤ 128 MiB |

全部判定项 `retained_messages_exact`、`retained_activities_bounded`、`input_p95`、`repaint_p95`、`rss` 和 `soak_growth` 均为 `true`。运行持续 28,800 秒；不能用较短 smoke 代替该记录。

## Windows ConPTY 与生命周期门

以下门与 8 小时 component soak 互补：

- 1,000 次 60～200 列 resize，同时持续高频 product event，并逐字节输入中文、组合字符和 ZWJ emoji；输入零丢失、零替换；
- 100 次正常启动/退出与 100 次不可捕获 `TerminateProcess` 循环；host raw/cursor/paste 状态恢复，无 alternate screen；
- 100 次真实 daemon 断线/重启恢复，generation 与 cursor gap 通过 snapshot 重建；
- 10,000 次 terminal backpressure 更新只保留最新 frame，不形成无界写队列；
- 256 KiB paste 边界、碎片 UTF-8、Windows paste burst 与异步重绘组合门通过。

对应入口：

```powershell
.\.venv\Scripts\python.exe scripts\product-tui\windows_conpty_gate.py
.\node_modules\.bin\bun.exe scripts\product-tui\performance_gate.ts
```

## 边界

- 本文不把 Agent 是否解出 T1 作为前端通过条件。
- 本文不声称人工 IME 候选窗已验证；该项由 `windows_ime_manual_gate.ps1` 单独验收。
- Linux/macOS 当前没有实机结果，不由 Windows 结果外推。
