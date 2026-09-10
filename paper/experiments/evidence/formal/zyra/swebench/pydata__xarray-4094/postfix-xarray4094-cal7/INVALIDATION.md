# xarray-4094 校准运行中断说明

日期：2026-09-10

## 结论

本运行（`postfix-xarray4094-cal7`）的 **agent 补丁经官方 SWE-bench evaluator 判 `resolved=1`**，且 agent 在 26 次 provider dispatch 内**无崩溃循环地改对了生产代码 + 补对了回归测试**（对比 sphinx 的 4 个 attempt 反复「incomplete physical layer 1」）。这证明第一步修复（累计预算驱动压缩 + 恢复续跑）让 agent 从「崩溃循环」进步到「干净交付」。

但运行**被外部环境中断**（后台 runner 观察进程在 12:37 被 kill，连带 API 进程），**非自然终态**，因此：
- `task_status = unknown`（无 task-run.json）
- `strict_success = false`
- 本运行只能作为「补丁质量 + 无崩溃循环」的**诊断证据**，不能作为系统 strict success 或 efficacy 样本。

## 运行事实

| 项 | 值 |
|---|---|
| instance | pydata__xarray-4094 |
| run label | postfix-xarray4094-cal7 |
| 官方 evaluator | **resolved=1** |
| 补丁 sha256 | b742ca9af7e8ee99a754f687e86343d36a4e993a06612d49e7b8f4c0a55a047e |
| 补丁字节 | 1888 |
| 修改文件 | `xarray/core/dataarray.py`（+9）+ `xarray/tests/test_dataset.py`（+10）|
| provider dispatch | 26 次（24 succeeded / 0 failed，运行中断时）|
| 崩溃循环 | **无**（对比 sphinx 的 4 attempt）|
| 终态 | 外部中断，无 task-run.json |

## 补丁内容（官方 resolved=1）

- **根因**：`to_unstacked_dataset` 对单维变量，堆叠维度坐标在 `.squeeze(drop=True)` 后残留为标量坐标，导致后续 `Dataset(data_dict)` 组合时 `MergeError`。
- **修复**：`if dim in data.coords and dim not in data.dims: data = data.drop_vars(dim)` 精确删除冗余标量坐标。
- **测试**：`test_to_stacked_array_to_unstacked_dataset_single_dimension`，精确覆盖单维 roundtrip。

## 与修复目标的对照

| 运行 | 崩溃循环 | 补丁 | 官方 |
|---|---|---|---|
| sphinx-9367（第一步修复前）| 4 attempt 反复崩溃 | 改对 | resolved=1 |
| **xarray-4094（第一步修复后）** | **无** | **改对+补对测试** | **resolved=1** |

第一步 B（恢复续跑）的核心价值在 xarray 上得到验证：agent 不再陷入「physical worker 反复 incomplete」的循环，而是持续线性推进到交付。

## 后续

本运行不重跑（补丁已暴露）。下一步若需验证「第一步 A（累计预算驱动压缩）」在真正烧预算场景下的效果，需用新 seed 选新题，且需保证 runner 观察进程不被外部 kill（如用独立持久会话跑 runner）。
