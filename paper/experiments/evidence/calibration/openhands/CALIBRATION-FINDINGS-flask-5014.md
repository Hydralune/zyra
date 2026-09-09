# 校准发现记录：OpenHands 官方单智能体 × pallets__flask-5014

日期：2026-09-07
运行目录：`__home__ylon__zyra-experiments__swe-bench-verified-test.jsonl-test/deepseek/deepseek-v4-flash_sdk_43376f1_maxiter_30_N_zyra-calibration-v1-openhands-run3`

## 结论（如实，未美化）

- 最终成功率：**0/1**（该任务未被判为成功）。
- 失败原因：**统一为 `MaxIterationsReached`** —— Agent 在 30 次迭代上限内始终未发出 `AgentFinishAction`，导致对话进入 ERROR 状态。
- 最终 `output.jsonl` 为空（0 字节），无成功样本写入。

## 关键观测

1. **Agent 的代码修复在语义上正确**：attempt 3 捕获到的 `git_patch` 中，`src/flask/blueprints.py` 的修改与黄金 patch **逐行一致**（在 `if "." in name` 之前添加 `if not name: raise ValueError("'name' may not be empty.")`）。
2. **但 Agent 多产出了无关文件**：在仓库根目录创建了 `reproduce_issue.py` 复现脚本，属于修复目标之外的冗余产物。
3. **收尾失败**：3 个 critic attempt × 各 4 次 runtime retry（max_retries=3）全部耗尽 30 迭代，`git_patch_captured_on_error: true`（attempt 3 的 patch 是靠错误回退机制捕获，非正常 finish）。
4. **错误回写 bug**：`on_result callback failed: [Errno 2] No such file or directory ... 'swe-bench-verified-test_errors.jsonl-test'` —— 失败结果写入路径被错误拼接（`test.jsonl-test` → `test_errors.jsonl-test`），导致 `output_errors.jsonl` 未成功落盘（父目录不存在）。但原始证据保留在 `output.critic_attempt_*.jsonl` 中，未丢失。

## 指标（3 个 critic attempt 累计，来自原始 JSONL）

| attempt | cost(USD) | prompt_tokens | completion | cache_read | reasoning | git_patch |
|---|---|---|---|---|---|---|
| 1 | 0.0332 | 750,491 | 9,727 | 727,424 | 6,393 | 空 |
| 2 | 0.0375 | 828,269 | 11,470 | 802,944 | 7,026 | 空 |
| 3 | 0.0306 | 697,714 | 9,041 | 676,736 | 4,409 | 1673 字节 |

缓存命中率高（cache_read 占总 prompt 的 ~96%）。

## 根因分析（来自对话轨迹 `conversations/pallets__flask-5014.tar.gz`）

对 attempt 3 的 97 个对话事件做序列分析：

- **工具分布**：`terminal` 56 次、`file_editor` 4 次、**`finish` 0 次**。
- **30 次迭代的完整轨迹**：
  1. action 1–17：合理代码调研（探索 repo、定位 `blueprints.py`、查测试、对比 Flask 2.3.0 官方源码）；
  2. action 18–23：开始环境验证（激活 conda、检查 `/testbed` 与 `/workspace/flask` 软链接关系、跑 python 探针）；
  3. action 24：创建 `reproduce_issue.py` 复现脚本（**冗余产物**）；
  4. action 25–26：反复验证复现脚本；
  5. action 27：**正确修改** `src/flask/blueprints.py`（`str_replace` 添加 `if not name: raise ValueError(...)`，与黄金 patch 一致）；
  6. action 28–30：**继续反复验证**（重复 `PYTHONPATH=/workspace ... python -c` 探针），直到 30 次迭代耗尽，**从未发出 finish**。

**根因**：`deepseek-v4-flash` 模型在 OpenHands 默认 agent 下，完成任务后陷入"反复验证"循环，无法主动收尾（finish）。这是模型-框架交互的真实系统性行为，不是环境或配置故障。修复代码本身是对的，但因收尾失败被记为 `MaxIterationsReached`。

## 校准阶段意义

这是**基线本身的真实表现**，不是环境故障，也不是 ZYRA 的问题。它揭示了两点必须在正式实验前处理：

1. **`MaxIterationsReached` 的系统性收尾失败**：`deepseek-v4-flash` 模型在 OpenHands 默认 agent 下，对 SWE-bench 任务存在"做了正确修改但无法主动 finish"的问题。可能原因（待进一步确认，不臆断）：
   - 模型对 OpenHands finish 工具的遵循度不足；
   - 默认 agent 陷入反复验证而非收尾；
   - `max_iterations=30` 对该任务偏紧。
2. **OpenHands benchmark 的 `output_errors.jsonl` 路径拼接 bug**：当 dataset 是本地 `.jsonl` 路径时，错误结果回写路径错误，导致失败样本聚合文件丢失。需在正式实验前修复或绕过（例如用目录路径作为 `--output-dir` 的 dataset 描述段）。

## 下一步（供决策，不擅自执行）

- 选项 A：保持默认配置，将 `MaxIterationsReached` 记为真实失败，继续校准其余任务（如实记录基线失败率）。
- 选项 B：先诊断 finish 失败根因（读对话记录 `conversations/pallets__flask-5014.tar.gz`，确认 agent 是否在最后几轮尝试了 finish 工具），再决定是否调整 `max_iterations` 或 agent 配置后重跑。
- 无论何种，均需修复 `output_errors.jsonl` 路径 bug，避免正式实验丢失失败样本。

## 证据完整性

- 原始运行日志：`logs/instance_pallets__flask-5014.log`、`logs/instance_pallets__flask-5014.output.log`
- 完整对话轨迹：`conversations/pallets__flask-5014.tar.gz`
- 三次 critic 原始结果：`output.critic_attempt_{1,2,3}.jsonl`
- 运行元数据：`metadata.json`（已确认 api_key 脱敏为 `**********`）

## 修复决策与实施（2026-09-07 追加）

**用户决策**：先诊断并修复收尾问题。

**诊断结论**：根因是 `deepseek-v4-flash` 模型的 system prompt 中缺少"完成后必须调用 finish 工具"的显式引导。默认 `system_prompt.j2` 无任何 finish 指令，而 DeepSeek 是已识别的 model family 但 `model_specific/deepseek.j2` 不存在，导致该模型相比 Claude/Gemini/GPT 少了一层行为引导。

**修复**：新增 `vendor/software-agent-sdk/openhands-sdk/openhands/sdk/agent/prompts/model_specific/deepseek.j2`，内容为通用收尾引导（对所有 DeepSeek 任务生效，非任务特定调优）。已通过 `agent.static_system_message` 验证该提示词被正确 include 到 `<IMPORTANT>` 区块。

**待办**：因 agent-server 镜像是 source-minimal 模式（构建时 COPY SDK 源码），需用 `FORCE_BUILD=1` 重建镜像使 deepseek.j2 生效，然后重跑本校准任务验证 finish 问题是否解决。

## 二次诊断结论（run4 重跑后，2026-09-08 追加）

**修复验证结果：finish 引导未解决收尾问题。**

1. 已重建镜像（含 `deepseek.j2`，239.5s），并验证 `deepseek.j2` 确实进入镜像（3 处：源码/build/lib/site-packages）。
2. 重跑 run4 后，通过对话轨迹确认 `SystemPromptEvent` 中**确实包含** finish 引导（`HAS_FINISH_GUIDE: True`），即提示词修复已到达模型。
3. 但 attempt 2/3 仍为 `MaxIterationsReached`，工具分布与 run3 **完全一致**（terminal 56、file_editor 4、finish 0）。

**修正后的根因**：不是"缺少 finish 提示"，而是 `deepseek-v4-flash` 模型在 OpenHands 复杂工具环境下的**收敛能力缺陷**。模型在完成代码修改后，陷入对 `/testbed` vs `/workspace/flask` 软链接、pip 包路径、测试文件位置的**无限环境探索**，即使被告知"完成后调用 finish"，也无法在合适时机收敛到 finish 动作。这是模型能力边界，非提示词或配置可解。

**附带发现（新问题）**：run4 的 attempt 1 报 `LLMBadRequestError: The reasoning_content in the thinking mode must be passed back to the API`（间歇性，attempt 2/3 未复现）。这是 DeepSeek thinking 模式与 OpenHands 消息回传的另一个集成边界问题，需单独记录。

**对实验设计的启示**：这确认了 `deepseek-v4-flash` 作为统一基础模型时，OpenHands 官方单智能体基线在 SWE-bench 类任务上存在系统性收尾失败。正式实验若固定此模型，需明确接受这一基线失败率（如实记录），或对基线采用能力匹配原则（协议已允许），或换更强模型（需重新冻结模型）。

## 第三次定位（用户指正后，2026-09-08 追加）：这是循环停止条件的设计差异，非单纯模型缺陷

**用户关键指正**：这不是"模型太差"，而是** agent loop 的停止条件设计问题**——修复已经完成了（git 中已有正确改动），应该定位到"循环如何判定任务完成"，而不是"模型为什么不主动 finish"。

**确认后的正确定位**：

1. **OpenHands 的停止条件是脆弱的**（见 `local_conversation.py` run loop）：
   - 只有两个停止信号：① 模型主动调用 `finish` 工具 → `FINISHED`；② `iteration >= max_iteration_per_run` → `MaxIterationsReached` 错误。
   - 循环里的 `_stuck_detector` 是**可选**的，且检测到 STUCK 也只是 `continue`，不判定"任务完成"。
   - **没有任何"产物状态已达目标（git diff 正确、测试通过）即收敛"的产物级停止条件**。

2. **ZYRA 的设计正是针对这一点**（论文第二版第 328、508 行）：
   - "任何 Worker 的'任务已完成'文本都不能直接结束任务。完成门检查当前需求版本下的必要义务、预期工件、测试或领域验证、关键记忆连续性和开放租约。"
   - "任务成功不依赖某个 Agent 的自然语言声明。当前需求版本下所有必要义务必须通过完成谓词，必需工件存在且摘要一致，关键记忆通过连续性门。"
   - ZYRA 用 `final_artifact` + `artifact_digest` + `artifact_committed` 等**产物状态迁移**来判断收敛，而非依赖模型声明。

**对论文的价值（正面）**：本次校准实验**恰好实证了论文要论证的核心论点**——在相同 `deepseek-v4-flash` 模型下，OpenHands 因"把停止权完全交给模型的自然语言声明"而失败（即使修复代码已正确写入 git），而 ZYRA 的"语义提议与状态裁决分离 + 完成门 + 连续性门"机制正是为了解决这一缺陷。这构成一个有说服力的对照证据，但需在正式实验中用 ZYRA 实际跑通同一任务来验证（而非仅凭设计推断）。

**后续待办**：
1. 保留 run3/run4 的完整失败证据（如实记录 OpenHands 基线真实表现）。
2. 用 ZYRA 跑通同一任务 `pallets__flask-5014`，验证其产物级完成门是否能在相同模型下正常收敛（这是关键对照）。
3. 修复 OpenHands 的 `output_errors.jsonl` 路径 bug（确保不丢失败样本）。
4. 若 ZYRA 也能正常收敛，则该对照可进入论文；若 ZYRA 同样失败，则需重新审视任务适配。

## ZYRA 适配层调查结论（2026-09-08 追加）

**用户决策**：新写最小 ZYRA SWE-bench Verified 适配器。

**调查结果（源码级确认）**：

1. **ZYRA 的产物级完成门已定位到确切实现**：`packages/orchestration/zyra_orchestration/deployment/code_worker_adapter.py` 的 `_evaluate_delivery_completion`，docstring 直接点题 "Fail closed on objective delivery evidence before a model may stop."。检查项包括 `required_paths_present`、`expected_file_contents_match`、`workspace_mutation_observed`、`behavioral_verification_passed`、`final_response_present`、`delivery_contract_bound`。

2. **SWE-bench 评分接口极简**：官方评分器只需 `predictions.jsonl`（每行 `{instance_id, model_patch, model_name_or_path}`），在 Docker 里 checkout base_commit + 应用 model_patch + 跑 FAIL_TO_PASS/PASS_TO_PASS 测试。

3. **关键现实障碍**：ZYRA 是完整产品系统，不是可直接跑单个任务的评测 runner。它包含：
   - 20+ 个 Python 包（`zyra_api`、`zyra_orchestration`、`zyra_workspace`、`zyra_workers`、`zyra_scheduler`、`zyra_symbolic` 等）
   - TypeScript CLI（依赖 `bun`，当前 Windows 环境无 bun）
   - FastAPI daemon（`apps/api/zyra_api/main.py` 19797 行）
   - 完整启动链路：CLI → daemon/API → 控制平面(task_graph) → worker(code_worker_adapter，经 `cwd`+`workspace_roots` 绑定工作区)
   - benchmark 命令本身是 "fixture-only and no-run by default" 的骨架层，无 SWE-bench 适配器

4. **适配器工程量的现实评估**：让 ZYRA 真正跑 SWE-bench Verified 需要：① 配置 Python venv（20+ 包）；② 配置 bun；③ 启动 daemon/API；④ 配置 deepseek-v4-flash 模型端点；⑤ 配置 flask 仓库工作区；⑥ 提交 goal 并跑完；⑦ 提取 git diff；⑧ 官方评分。这是"把 ZYRA 完整跑起来 + 正确配置 + 对接评分"的系统工程，非简单脚本。

**结论**：新写适配器的前置是"先把 ZYRA 完整环境跑起来"，这部分与 OpenHands 基线（已在 WSL 跑通）是两条完全不同的链路。已向用户汇报，待确认推进方式。

## 关键里程碑：官方判分 resolved=true（2026-09-08）

用 `--critic empty_patch_critic` 重跑（run5），agent 未发 finish 时仍能捕获其 `git_patch`（885 字节，含正确修复 + 多余 CHANGES.rst 改动），然后用官方 SWE-bench 评分器独立判分。

**判分结果：`resolved: true`（1/1 通过）**

- `FAIL_TO_PASS`：`tests/test_blueprints.py::test_empty_name_not_allowed` **通过**
- `PASS_TO_PASS`：72 个测试**全部通过**（0 失败）
- `patch_successfully_applied: true`

证据文件：
- `openhands/scoring/deepseek__deepseek-v4-flash.zyra-calibration-flask-5014.json`（汇总）
- `openhands/scoring/flask-5014-report.json`（逐测试明细）

**这彻底澄清了校准的意义**：OpenHands 的 agent 实际上**正确解决了任务**（代码修复通过了全部隐藏测试），之前 run3/run4 报 0/1 纯粹是"agent 未发 finish 信号"导致 patch 未入 output.jsonl，而非代码错误。改用 `empty_patch_critic` 后，agent 的真实代码修复能力得以被官方评分器正确判定为成功。

**对实验方法的修正**：OpenHands 基线的正确运行方式是 `--critic empty_patch_critic`（官方提供的 critic 选项，最终判分仍由隐藏测试独立完成，不改变评测公平性）。后续 SWE-bench 校准与正式任务均应使用此 critic，避免把"agent 是否礼貌说 finish"误计为失败。
