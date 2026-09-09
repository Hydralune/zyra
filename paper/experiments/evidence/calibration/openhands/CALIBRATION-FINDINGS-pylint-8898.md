# 校准发现记录：OpenHands 官方单智能体 × pylint-dev__pylint-8898

日期：2026-09-08
运行目录：`__home__ylon__zyra-experiments__swe-bench-verified-test.jsonl-test/deepseek/deepseek-v4-flash_sdk_43376f1_maxiter_30_N_zyra-calibration-v1-openhands-run6-pylint`

## 结论（如实，未美化）

- 最终成功率：**0/1**（未解决）。
- 3 个 critic attempt 均为 `git_patch_len=0`（空 patch），agent 未产出任何代码修改。
- 失败模式：与 flask 任务一致，`MaxIterationsReached`（30 次迭代未发 finish）。

## 与 flask-5014 的关键差异

| 维度 | flask-5014 | pylint-8898 |
|---|---|---|
| 任务 | 空 name 校验（简单，`<15 min`） | bad-names-rgxs 正则逗号处理（`1-4 hours`） |
| agent 是否改对代码 | ✅ 改对（patch 与黄金一致） | ❌ 空 patch（未改任何代码） |
| 官方判分 | resolved=true | 未提交（空 patch 无法判分） |

**含义**：flask-5014 的失败是"停止条件问题"（改对了但没 finish），而 pylint-8898 的失败是"任务难度超出 agent 能力"（agent 在 30 次迭代内根本没找到正确解法，也未产出任何代码）。这两个任务揭示了 OpenHands 基线在 `deepseek-v4-flash` 下的两类不同失败模式。

## 关键指标（pylint-8898，3 critic attempts）

- 全部 attempt 空 patch（0 字节）
- 失败统一为 `MaxIterationsReached`
- 运行时长约 32 分钟（00:54:29 → 01:27:09）
- 期间多次 `git fetch origin` 超时（用缓存版本，非致命）

## 校准结论

1. 校准方法可复现：flask 和 pylint 都用 `empty_patch_critic` 成功跑通并产出可判分/可记录的完整证据。
2. OpenHands 基线在简单任务（flask `<15 min`）能正确解决，在中等任务（pylint `1-4 hours`）无法解决——这是真实的能力边界，符合预期。
3. 两个任务都暴露出 `MaxIterationsReached` 的系统性收尾问题（即使 flask 改对了代码也没 finish），需在正式实验用 `empty_patch_critic` 规避"误计失败"。
