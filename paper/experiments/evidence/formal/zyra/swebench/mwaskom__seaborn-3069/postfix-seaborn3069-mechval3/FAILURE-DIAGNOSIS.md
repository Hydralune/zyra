# seaborn-3069 机制验证校准运行诊断

日期：2026-09-10
run label：`postfix-seaborn3069-mechval3`
task id：`task_3f02d0f149ad`

## 结论

本运行暴露完成门 fail-open 的**第二个残留**（比 v8 更隐蔽）：

- `task_status = completed`，但容器里只有 `_probe.py` 诊断探针，**零生产代码改动**。
- agent 的 `final_answer` 诚实承认 "No patch was applied. The tree remains as delivered"，系统却放行 completed。
- 根因：`workspace_mutation_observed` 只认「有没有任何文件变更」，不认「变更的是不是仓库已跟踪源码」。`_probe.py`（created，探针）被当成了交付。

## 数据铁证（全题 workspace_delta 对比）

| 运行 | 结果 | created | modified | 判定 |
|---|---|---|---|---|
| flask-5014 | ✅ strict=True | 空 | `src/flask/blueprints.py`... | 正确 |
| astropy-7606 | failed | 空 | `astropy/units/core.py`... | 正确 |
| **seaborn-3069** | ❌ completed(误放行) | `_probe.py` | 空 | 错误 |
| django-15930 | failed | `repro_case_q.py` | 空 | 错误 |
| pytest-6202 | failed | `_probe_dotbracket/test_x.py` | `src/_pytest/python.py` | 正确 |

规律：`modified`/`deleted` 只能作用于 git 已跟踪源码；`created` 是探针散文件的温床。sealed benchmark 的交付应只认 `modified`+`deleted`。

## 运行事实

| 项 | 值 |
|---|---|
| instance | mwaskom__seaborn-3069 |
| difficulty | 15 min - 1 hour |
| provider calls | 24 |
| total tokens | 303,408 / 600,000（50.6%）|
| tool calls | 26 |
| 压缩 | 8 次 `context_compacted`（turn 3/4/8/10/12/13/14/15）|
| 委派 steer | **0 次**（见下）|
| task status | completed（误放行）|
| strict success | false |

## 委派 steer 未触发的原因（非 bug）

累计 token 在最后一次 dispatch（turn 15 附近）才跨过 50%（303,408），此时已进入 `execution_closeout`（turn 15），steer 注入点来不及在后续轮次执行。这是**时机竞争**，不是机制缺陷。对比 v9（turn 20/22 跨过 50%，后面还有 turn 21 继续跑）steer 有机会注入。

## 修复

`goal_contracts.py::validate_goal_delivery` 的 `workspace_mutation_observed`：sealed 场景（`require_workspace_mutation=True`）下只认 `modified`+`deleted`，排除 `created` 探针散文件。新增测试 `test_sealed_container_rejects_probe_only_delivery`。

## 后续

本运行不重跑。下一题验证：探针文件不再满足 sealed 交付 + 委派链路完整落地（若该题在 deadline 前烧过 50%）。
