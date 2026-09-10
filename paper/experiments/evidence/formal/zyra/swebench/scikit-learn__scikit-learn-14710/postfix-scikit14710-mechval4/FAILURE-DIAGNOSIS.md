# scikit-learn-14710 机制验证校准运行诊断

日期：2026-09-10
run label：`postfix-scikit14710-mechval4`
task id：`task_25f834f14bc6`

## 结论

本运行是 `deepseek-flash`（新模型）首跑，暴露了「验证闭环无法收敛」的底层机制缺陷：

- **补丁完全正确**：官方 SWE-bench 评测 `resolved=true`，FAIL_TO_PASS `test_string_target_early_stopping[None]` 通过，63 个 PASS_TO_PASS 全过，零回归。
- **系统却 fail-closed**：`task_status=failed`、`strict_success=false`。补丁对了，验证没收口。

## 根因（三个耦合机制缺陷，已修 commit `5f44761f`）

1. **正确解释器未注入**：SWE-bench 镜像标准约定 `/opt/miniconda3/envs/testbed/bin/python`（C 扩展已编译），但 harness 从不告诉 agent。agent 用默认 `/opt/miniconda3/bin/python` 跑 pytest → `ModuleNotFoundError: No module named 'sklearn.__check_build._check_build'`。

2. **环境探测扣预算**：agent 识别到 `conda env testbed` 存在、默认 python 不对后，想探测正确解释器，却被 `alternate_verification_budget_exhausted` block（`targeted_repair_inspection_limit=4` 耗尽）。`recent_reasoning` 明确记录："two probes (interpreter/pytest version; site-packages scan) returned error: alternate_verification_budget_exhausted"。

3. **构建哨兵误判为语义债务**：`No module named 'x.__check_build'` 这个「被测库 C 扩展未编译」的环境错误，被记成语义验证债务，deadlock 收口。

## 运行事实

| 项 | 值 |
|---|---|
| instance | scikit-learn__scikit-learn-14710 |
| model | deepseek-flash |
| difficulty | 15 min - 1 hour |
| provider calls | 22 |
| total tokens | 281,797 / 600,000 |
| tool calls | 24 |
| 压缩 | 7 次 `compaction_count` |
| 补丁 | gradient_boosting.py（+10）+ test_gradient_boosting.py（+28）|
| 官方 evaluator | **resolved=1** |
| task status | failed（验证没收口）|
| strict success | false |

## 关键证据（为什么是机制问题不是模型问题）

agent 的 `recent_reasoning`（round 9-14）完整展示了失败链：

- round 9-11：找到正确方向（"conda env `testbed` exists and the default `python` is not the test runner"）
- round 12："Verification budget for alternate probing is exhausted"——被预算掐断
- round 13-14：只能读文件确认 edit 落点，deadline 到，没收口

agent 的**根因定位、代码修改、测试补充全部正确**，卡在「验证收口」这最后一步——而这一步的失败是三个系统机制缺陷叠加，非模型能力问题。

## 修复

见 commit `5f44761f`（三个卡点 + 回归测试）。下一题验证：正确解释器注入后，agent 一开始就跑对测试，闭环收敛到 strict success。

## 后续

本运行不重跑（补丁已暴露 + resolved=true）。用新 seed 选下一题，验证三卡点修复后闭环是否收敛。
