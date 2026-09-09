# 机制对照：任务收敛判定（完成门 vs 模型自然语言声明）

日期：2026-09-08
状态：校准阶段证据（非最终统计结论）

## 目的

本文档记录一个**可复核的机制级对照**：OpenHands 与 ZYRA 在"如何判定任务是否完成"这一核心设计上的差异，以及一个实证案例说明该差异导致的后果。本文档**不宣称任何系统在成功率上的统计优势**——样本量（2 个任务、1 个系统跑通）远不足以支撑。

## 一、源码级事实

### OpenHands 的停止条件（`local_conversation.py` run loop）

循环只依赖两个信号终止：
1. `execution_status == FINISHED` —— 仅当模型**主动调用 `finish` 工具**时设置；
2. `iteration >= max_iteration_per_run` —— 硬上限，触发 `MaxIterationsReached` 错误。

其中没有"产物状态达标即收敛"的判定。可选 `_stuck_detector` 检测到卡死也只是 `continue`，不判定完成。

### ZYRA 的完成门（`code_worker_adapter.py::_evaluate_delivery_completion`）

docstring 原文：**"Fail closed on objective delivery evidence before a model may stop."**

检查项（任一不满足即拒绝停止）：
- `delivery_contract_bound`（交付契约绑定）
- `required_paths_present`（必需产物文件存在）
- `expected_file_contents_match`（文件内容匹配）
- `workspace_mutation_observed`（工作区有实际变更）
- `behavioral_verification_passed`（行为验证通过，verification_count > 0 且无未解决验证）
- `final_response_present`（有最终回复）
- `required_skills_invoked` / `successful_executed_paths`（义务证据）

对应论文第二版第 328、508 行的论述："任何 Worker 的'任务已完成'文本都不能直接结束任务"；"任务成功不依赖某个 Agent 的自然语言声明"。

## 二、实证案例：pallets__flask-5014

### 观察到的现象

OpenHands 官方单智能体 + `deepseek-v4-flash` 在该任务上：
- 3 轮运行均报 `MaxIterationsReached`，框架判定 0/1（失败）。
- 但把 agent 实际产出的 `git_patch` 交给**官方 SWE-bench 评分器**独立判分，结果 **`resolved=true`**：
  - `FAIL_TO_PASS`（`test_empty_name_not_allowed`）通过；
  - 全部 72 个 `PASS_TO_PASS` 测试通过；
  - `patch_successfully_applied: true`；
  - agent 的修复与黄金 patch 逐行一致。

### 结论

agent 已经**正确完成任务**，但因未发 finish 信号而被框架记为失败。这正是"把停止权交给模型自然语言声明"的后果。

### 证据位置

- 框架判 0/1 的证据：`evidence/calibration/openhands/.../run3/run4/output.critic_attempt_*.jsonl`（error 字段为 `MaxIterationsReached`）
- 官方判分 resolved=true 的证据：`evidence/calibration/openhands/scoring/flask-5014-report.json` 与 `deepseek__deepseek-v4-flash.zyra-calibration-flask-5014.json`

## 三、pylint-8898 的对照（能力边界）

同一系统、同一模型在更难的 `pylint-dev__pylint-8898`（`1-4 hours`）上，3 个 critic attempt 均 `git_patch_len=0`（空 patch）——agent 在 30 次迭代内未找到解法、未产出任何代码。这是**任务难度超出 agent 能力**的真实失败，与 flask-5014 的"改对了但没收尾"是**两类不同失败模式**。

| 任务 | 难度 | agent 行为 | 官方判分 |
|---|---|---|---|
| flask-5014 | `<15 min` | 改对代码，但未 finish | **resolved=true** |
| pylint-8898 | `1-4 hours` | 空 patch，未解出 | 未提交（0/1）|

## 四、边界与诚实声明

1. **本文档只证明机制差异及其一个后果，不证明 ZYRA 的完成门在实践中优于 OpenHands**。要证明后者，必须让 ZYRA 在**同一任务、同一模型、同一评分器**下实际跑出结果并比较——这一步尚未完成（ZYRA 无现成 SWE-bench 适配器，需新写）。
2. 2 个任务、1 个系统不构成任何统计结论。
3. OpenHands 改用官方 `empty_patch_critic` 后可公平运行（不把"未发 finish"误计为失败），后续公平对比应统一使用该 critic。

## 五、下一步（待办）

- [ ] 新写 ZYRA 的 SWE-bench 适配器：让 ZYRA CodeWorker 在真实 flask 仓库上驱动 LLM 改代码，经官方评分器判分。
- [ ] 在 flask-5014 上形成"OpenHands（做对但记失败，经 empty_patch_critic 后 resolved=true）vs ZYRA（完成门正常收敛且 resolved=true）"的公平对照。
- [ ] 扩充任务样本（≥ 12 个 SWE-bench）后，才能计算成功率与统计检验。
