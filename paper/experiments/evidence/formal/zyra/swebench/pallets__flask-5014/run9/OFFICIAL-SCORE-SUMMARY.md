# ZYRA SWE-bench 官方评分摘要：pallets__flask-5014

## 可复核结论

ZYRA 运行轨迹产出的冻结预测补丁，经独立的 SWE-bench 2.1.5 官方 harness 评分，结果为 `resolved: true`。

| 项目 | 结果 |
| --- | --- |
| 任务 | `pallets__flask-5014` |
| 冻结预测 | `predictions.jsonl` |
| 数据集记录 | `official-instance.json`（从本地冻结的 SWE-bench Verified `test.parquet` 单题提取） |
| 评分器 | SWE-bench 2.1.5 官方 harness |
| 补丁应用 | 成功 |
| FAIL_TO_PASS | 1 / 1 通过 |
| PASS_TO_PASS | 59 / 59 通过 |
| 测试失败 | 0 |
| 官方 resolved | `true` |
| 完成 / 提交 / 错误 | 1 / 1 / 0 |

## 权威产物

- 总报告：`official-swebench-report/ZYRA-deepseek-v4-flash-run9.zyra-run9.json`
- 单题判分：`official-swebench-report/logs/run_evaluation/zyra-run9/ZYRA-deepseek-v4-flash-run9/pallets__flask-5014/report.json`
- 官方测试输出：`official-swebench-report/logs/run_evaluation/zyra-run9/ZYRA-deepseek-v4-flash-run9/pallets__flask-5014/test_output.txt`
- 评分使用的 patch：`official-swebench-report/logs/run_evaluation/zyra-run9/ZYRA-deepseek-v4-flash-run9/pallets__flask-5014/patch.diff`

## 解释边界

本记录证明：本次 ZYRA 运行实际生成的补丁可通过该题的独立官方判分。

run9 的自主执行未正常收尾，原因是当时尚未强制总模型轮次上限；该运行被人工停止以避免无效额度消耗。因此该单元不得直接计入“系统端到端任务成功率”或与基线的成功率比较。后续正式横向实验必须采用固定轮次、固定时限、固定模型与相同官方评分器，并把本记录作为适配与评分链路已打通的证据。
