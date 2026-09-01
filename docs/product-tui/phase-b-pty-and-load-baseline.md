# Phase B 真实 PTY 与长程负载基线

日期：2026-09-01
状态：Zyra 基线已采集并完成后续硬化；Codex 登录前实机基线已采集，会话基线等待人工认证

## 1. Zyra 真实 PTY 基线

执行入口：

```powershell
Set-Location G:\agent-zoo\zyra
node .\apps\cli\dist\zyra.js
```

观察环境：Windows PowerShell PTY，约 80 列。观察结果：

- 产品界面出现，包含 Zyra header、workspace、连接状态、composer 和快捷键提示；
- 使用 inline screen 控制序列，没有观察到 alternate-screen `1049` 序列；
- bracketed paste 被启用；
- 首次 `Ctrl+C` 后界面执行恢复绘制并关闭 bracketed paste；
- 进程没有在合理时间内退出，后续 `Ctrl+C`、EOF 和文本输入也没有使进程收敛；
- 只读确认本次进程命令行后，精确终止 PID `90448`，未影响其他 Node 进程。

这段结果是 Phase B 初始缺陷样本，不是当前产品状态。后续 `TERM-04` 已通过进程内正常/异常退出和 Windows `TerminateProcess` 各 100 次验证；`SESS-03` 已通过退出不取消、同 task 两次真实 ConPTY attach/resume 和 canonical identity 对账。详见 `phase-f-hardening-evidence-20260901.md` 和 Phase G 报告。

## 2. Codex 真实入口探测

执行入口：

```powershell
node G:\agent-zoo\codex\codex-cli\bin\codex.js --version
node G:\agent-zoo\codex\codex-cli\bin\codex.js --help
```

仓库 launcher 仍因 checkout 内缺少 `@openai/codex-win32-x64` 而失败。后续定位到本机官方 standalone：

```text
C:\Users\libin\.codex\packages\standalone\releases\0.142.0-x86_64-pc-windows-msvc\bin\codex.exe
```

`codex.exe --version` 返回 `codex-cli 0.142.0`。`scripts/product-tui/codex_reference_conpty_probe.py` 已用真实 Windows ConPTY 采集 100×32 inline 启动与退出：登录页可见，bracketed-paste 启用/关闭成对出现，cursor 恢复，无 alternate screen，进程正常退出，且不持久化 raw transcript。机器报告为 `.tmp/codex-reference-conpty-0.142.0-auth-required.json`。

`codex login status` 当前返回 `Not logged in`，因此 `/status`/`/exit` 的认证后会话序列仍不能记为通过。登录属于官方 Codex 的人工身份操作；在完成该操作并重跑同一探针前，审计继续以本地源码、测试、snapshot 和上述登录前实机记录为证据，不伪称完整会话基线已经采集。

## 3. 历史真实长程 CLI 轨迹统计

统计对象是 `competition-tasks` 已保存的真实 `zyra.cli-record.v1` JSONL。这里只读聚合 record/schema/event type；没有读取 Evaluator secret 或私有真值。

| 负载 | 运行标识 | CLI task/run | 记录 | 时间范围（UTC） | 代表性前端状态 |
|---|---|---|---:|---|---|
| T1 | `t1formal20260816r03` | `task_1294ffa4141f` / `run_f573ac3cf2b6` | 47 | 02:13:25–02:18:34 | task create、5 node create、11 dispatch、11 agent message、6 artifact、4 audit |
| T2 | `run_t2_v20_long_horizon_deepseek_high_20260823a` | `task_ebf771a3a932` / `run_482903e12900` | 3,616 | 14:01:33–次日 00:10:43 | 3,013 agent message、503 dispatch、24 node update、1 node failure、1 recovery request、5 route、17 artifact、32 audit、terminal result |
| T3 | `run_t3formal_v214_showcase_deepseek_20260823b` | `task_490b2f8fd0f6` / `run_2d1a390b763d` | 920 | 12:39:27–13:04:06 | 797 agent message、42 dispatch、19 node update、1 node failure、3 route、12 artifact、27 audit、terminal result |

## 4. 映射到前端测试的状态覆盖

现有轨迹可以提供：

- 高频 agent activity 聚合与有界呈现；
- node created/updated/failed 和 topology route 的多代理状态；
- artifact commit 和 audit finding 的摘要/详情分层；
- recovery requested、局部失败和最终终态的严重性区分；
- 10 小时级 transcript 的重放和性能输入。

现有轨迹不能单独证明：

- 产品 `ZyraUiEvent/v2` 的 text delta、permission、diff 和 verification 语义；旧 trace 的大部分记录是 `zyra.cli-task-event.v1`；
- CLI detach/crash、SSE gap、daemon generation change 的 UI 恢复；
- 真实 PTY 中 composer 与高频输出并发；
- CLI/Web canonical 对账。

这些缺口必须由 Phase C～G 的新 fault fixture 和真实运行补齐，不能从历史任务成功倒推前端通过。

## 5. Fixture 转换规则

- 只抽取产品投影所需字段，默认删除 goal 正文、绝对路径、token、credential、capability、custody 和 Evaluator 私有字段。
- 保留稳定 ID 的不可逆替身、相对顺序、时间差、事件类别、状态变化和 artifact 数量。
- fixture 标注来源 run、抽取脚本版本和脱敏摘要；fixture 本身不是“新真实运行”。
- 性能 harness 可按原分布倍增事件，但报告必须把“真实采样”和“合成放大”分开。
