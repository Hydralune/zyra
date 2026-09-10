# sphinx-9367 校准失败根因诊断

日期：2026-09-10

## 1. 结论（一句话）

`django-15930` 之后第 2 轮机制修复（验证债务持续 nudge + 预算临近强制 closeout）**没有端到端生效**：`sphinx-doc__sphinx-9367` 补丁本身经官方 SWE-bench evaluator 判 **resolved=1**，但系统仍在 45 分钟内反复「incomplete physical layer 1」恢复，累计 token 烧到 584,565（近 60 万上限），最终 `completion_stop_exhausted`，strict_success=false。

## 2. 运行事实

| 项 | 值 |
|---|---|
| instance | sphinx-doc__sphinx-9367 |
| run label | postfix-sphinx9367-cal6 |
| task_status | failed |
| strict_success | false |
| 官方 evaluator | **resolved=1**（补丁正确）|
| 结束原因 | `worker.code-worker.typescript-provider-tool-loop preserved an incomplete physical layer 1 for recovery` |
| elapsed | 2710.3s（约 45 分钟，跑满 deadline）|
| token | input 489,487 + output 95,078 = 584,565（触顶）|
| provider_calls / tool_calls | 40 / 43 |
| **context_compactions** | **0** |
| 修改文件 | `sphinx/pycode/ast.py`（15 行）+ `tests/test_pycode_ast.py`（2 行）|
| 失败 dispatch | 1 次（`partial_response_observed`，后恢复）|

## 3. 补丁本身（官方 resolved=1）

agent 的修复**方向完全正确且更彻底**，超出了最初的最小修复：

1. `visit_Tuple`：单元素 tuple 字面量 `(1,)` 保留尾逗号；
2. **额外发现**：subscript 里的单元素 tuple（`Tuple[int,]`）也有同样缺陷，新增 `unparse_tuple_index` helper 一并修复——这正是问题描述里提到的 vscode test discovery 路径。

官方 evaluator 隐藏测试（FAIL_TO_PASS `test_unparse[(1,)-(1,)]` + 24 个 PASS_TO_PASS）全部通过。

## 4. 根因：控制闭环仍在「改对代码」后反复恢复，未收口

事件流取证（`events.jsonl` 913 条）显示 **4 个 worker attempt 反复「incomplete physical layer 1」**：

- attempt 1：00:06:38 → 00:42:10（约 35 分钟，一次完整 tool-loop）
- attempt 2（未记录完整，attempt_number 跳号）
- attempt 3：00:42:53 → 00:49:49（恢复，`parent_attempt=attempt_1`）
- attempt 4：00:50:21 启动，被外部 deadline 00:51 切断

每个 attempt 都以 `typescript-provider-tool-loop preserved an incomplete physical layer 1 for recovery` 失败，然后 worker-pool 自动重建下一个 attempt。

这暴露一个**新的、独立于第 2 轮修复的缺陷**：

1. **物理层 worker 在长程 provider tool-loop 中自行崩溃/中断**（`incomplete physical layer 1`），而非跑完整个 loop；
2. 恢复机制会**从头重建 attempt**，重新走 provider loop，但没有「把已完成的补丁 + 已验证状态带进新 attempt 并直接收口」的确定性路径；
3. 结果：补丁在 attempt 1 就写对了，但每个新 attempt 都在**重复探索/验证**，把 token 烧光。

这与第 2 轮修复针对的问题（`nudge_verification` 熄火导致 agent 自由探索）**不同**：这次 nudge 逻辑大概率已生效（40 次调用就改对代码 + 补对测试，比 pytest-6202 的 20 分钟空窗好），但**物理 worker 的恢复机制**在 attempt 边界丢失了「已交付」状态，导致每次都从头来。

## 5. 关键对照

| 运行 | 补丁 | 官方 | 控制闭环 |
|---|---|---|---|
| django-15930 | 只写复现脚本 | — | 空转，压缩 0 次 |
| pytest-6202 | 改对代码+补测试 | resolved=1 | 验证收口 nudge 熄火，烧预算 |
| **sphinx-9367** | **改对代码+补测试** | **resolved=1** | **physical worker 反复恢复，未收口** |

第 1 轮修复解决了 django 的「空转不改代码」；第 2 轮修复解决了 pytest 的「验证 nudge 熄火」。但 sphinx 暴露出**第 3 层缺陷**：物理 worker 的 `incomplete physical layer 1` 崩溃 + 恢复机制丢失交付状态，导致「已改对代码」后仍无法 canonical 收口。

## 6. 下一步方向（待决策，不擅自动手）

1. 重建 4 个 attempt 的 provider/tool 轨迹，确认每个 attempt 崩溃的**精确触发点**（是 token 预算、单次请求超时、还是 stream 中断）；
2. 定位 `typescript-provider-tool-loop` 为何在长程 loop 中 `preserved an incomplete physical layer 1`——这可能是**流式请求在某轮崩溃后，physical layer 1（工具副作用提交层）未完整落盘**；
3. 修复恢复机制：attempt 重建时，必须从已提交的 workspace delta + provider snapshot 恢复「已交付 + 已验证」状态，直接进入 closeout，而非重跑 provider loop；
4. 补确定性回归：覆盖「补丁已写对 + 验证已过 → physical worker 崩溃 → 恢复后直接 canonical 收口」的因果链。

以上均为离线诊断，未修改任何生产代码、未改动容器补丁、未重跑任何任务。
