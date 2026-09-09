# django-15930 校准失败根因诊断

日期：2026-09-09

## 1. 结论（一句话）

`django__django-15930` 校准以严格失败告终：agent 精确复现并定位了 `~Q(pk__in=[])` 缺陷，但在 46 分钟内**从未把修复写入生产源码**（只留下复现脚本 `repro_case_q.py`）。收口修复已生效（canonical record 完整、terminal state 完整、strict_success=false 正确记录），但核心问题仍未解决：**上下文压缩机制在密封基准下一次未触发，agent 在长上下文里反复探索、迟迟不进入修复+验证+收口**。

## 2. 运行事实

| 项 | 值 |
|---|---|
| instance | django__django-15930 |
| run label | postfix-django15930-cal1 |
| task_status | failed |
| strict_success | false |
| 结束原因 | `worker.code-worker...preserved an incomplete physical layer 1 for recovery`（retryable=false）|
| elapsed | 2759.6s（约 46 分钟，跑满 deadline）|
| token | input 392,415 + output 60,960 = 453,375（未超 60 万）|
| provider_calls / tool_calls | 33 / 32 |
| **context_compactions** | **0** |
| workspace_deltas | 仅 `repro_case_q.py`（created），0 生产修改、0 测试修改 |
| 失败 dispatch | 2 次，均为 `partial_response_observed → reconcile_partial_response` |

## 3. 关键根因：压缩机制在密封基准下失效

provider 请求体大小随时间变化（`provider_dispatch_attempts.requestBytes`）：

| 阶段 | request bytes |
|---|---|
| #0 | 25,328 B |
| #10 | 52,983 B |
| #15 | 41,017 B |
| #25 | 56,125 B |
| #30 | 46,098 B |
| #37 | 18,166 B |

- 请求体**始终稳定在 25–56 KB**，从未接近 96,000 字符（约 24–96 KB，取决于编码）的压缩阈值；
- 因此 `context_compactions = 0`，一次压缩都没发生；
- 但累计 token 因「每轮重放完整历史 + cache read 计费」涨到 453,375，deadline 与预算先于「进入修复」耗尽。

这印证交接文档 3.2/3.3 节「压缩触发条件与累计计费错位」的**同一根因的新变体**：96k 字符工作上下文限制本意是让压缩提前介入，但实际效果是把单次请求压到阈值以下、压缩永远不触发，而计费仍按累计 token 扣减，导致 agent 在一个缓慢膨胀、却从不压缩的长上下文里反复探索。

## 4. 与既往失败的模式对比

- Flask 5014（run25）：成功闭环（唯一）。
- Pylint 8898：run4 上下文压缩 0 次、累计 97 万 token 耗尽。
- Scikit-learn 13439 / Matplotlib 13989：补丁正确但未 canonical closeout（FreeType 归因已修）。
- **django 15930（本次）：连生产补丁都没写**，复现后即陷入反复探索，压缩 0 次。

共同点：**密封基准下压缩机制从未真正介入**，agent 缺少「长上下文里强制收束到修复」的机制，预算/时间耗尽先于交付。

## 5. 下一步方向（待决策，不擅自动手）

1. 修正压缩触发逻辑：使压缩依据「累计 token 或轮次」触发，而非「单次请求字节」；或在密封基准下对轮次/预算进度设置强制收束点。
2. 复核 96k 字符限制是否真正传播到 provider 请求路径（本次 request bytes 上限仅 56KB，但压缩仍 0 次，说明限制与压缩阈值之间的衔接有问题）。
3. 重建 agent 最后 13 轮的工具轨迹，确认「复现成功却迟迟不改源码」的决策链断裂点（是 nudge 逻辑、验证门、还是缺「复现→修复」的强制跃迁）。

以上均为离线诊断，未修改任何生产代码、未改动容器补丁、未重跑任何任务。
