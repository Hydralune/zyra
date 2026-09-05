# 模型计算切分补缺：最终验证记录

验证日期：2026-09-06。实现与复现命令见 [partitioned-inference.zh-CN.md](partitioned-inference.zh-CN.md)。详细缺口、执行计划、失败记录位于工作区根目录 `../ZYRA_赛题补缺分析与执行计划.md`，该文件不属于本仓库。

## 最终真实任务

使用用户配置的 DeepSeek API，经 Zyra 正常 CLI `run --sealed` 入口，识别 256 张 MNIST 官方测试集图像，并由 Agent 编写分析程序、预测 CSV、质量指标和报告。任务预置独立 unittest；操作者没有代写或修改 Agent 的交付物。所有失败尝试分别保留，最终记录来自全新目录 `attempt05`。

| 项目 | 结果 |
|---|---|
| 任务 ID | `task_1d97fa5e49b5` |
| CLI / 最终任务状态 | 退出码 0 / completed |
| 独立交付复核 | passed；没有任务执行期间的人工介入 |
| Provider | deepseek / deepseek-v4-flash |
| 真实 Provider 请求 | 19 次，19 次 HTTP 200 |
| 最终轮 tokens | 475,554：输入 462,633，输出 12,921 |
| 缓存用量 | 输入命中 422,656，未命中 39,977 |
| 模型与数据 | ONNX Model Zoo 预训练 MNIST CNN；CVDF 官方测试集镜像；偏移 137 的 256 张图像 |
| 正确数量 / 准确率 | 254 / 256；99.21875% |
| 分段与原始完整模型 logits 最大绝对误差 | 0.0 |
| 真实分段回执 | 512 条；每个样本 front、back 各一次 |
| 进程与主机 | 2 个独立进程；1 个自报主机身份，本机测试 |
| Agent 工具结果记录 | 25 次：成功 17、拒绝 3、失败 5；Agent 自主恢复后交付 |
| 模型工具许可 | 1 次真实 `model_inference` 调用，消费一次性权限许可 |
| 任务内行为验收 | 3 项 unittest 通过，包含更换 logits 与标签后重跑分析脚本 |

两处分类错误为样本 110（预测 2，标签 4）、样本 129（预测 0，标签 8）。这是模型本身的领域结果，不是分段数值差异。独立复核重新执行原始下载模型、逐项比较数值、CSV 和标签，还核验输入和验收脚本未被修改、各段张量摘要、模型身份、实际工具许可和 Provider 用量。

`agent_reasoning_step_count=0` 是本次模型推理工具报告不生成 Agent 推理步数的口径，不表示整个 DeepSeek 任务没有规划或推理。19 次 Provider 请求、25 次工具结果、256 个样本、512 段计算属于不同统计，不能相加或替代千步 Agent 推理证据。

本次为了触发自动分段，device 节点仅准入前段大小的模型文件，不能加载完整模型。此预算验证的是模型文件准入规则，不是实测硬件显存、峰值 RAM 或操作系统资源隔离。两个节点均在同一台机器运行。

## 本机证据索引

以下文件在运行时目录中，不提交 Git；包含真实运行产物，重新复现会生成新的任务 ID 和用量。

- `.tmp/inference-remediation/attempt05/independent-verification.json`：独立复核结果、模型与输入摘要、数值结果、权限和 19 次 Provider 调用记录。
- `.tmp/inference-remediation/attempt05/execution.json`：真实 CLI 命令、退出码、无人为介入标记。
- `.tmp/inference-remediation/attempt05/task/REPORT.md`：Agent 交付的报告；同目录包含 `analyze.py`、`predictions.csv`、`metrics.json`、`inference.json`。
- `.tmp/inference-remediation/numeric-validation/verification.json`：脱离 Agent 的独立进程数值验证，不能计作 Agent 工具调用。
- `.tmp/inference-remediation/usage-summary.json`：授权预检与全部尝试合计 111 次 Provider 请求、2,739,666 tokens。包含未成功交付的轮次，未将其消耗隐去。

首次授权预检已成功：HTTP 200、124 tokens、返回预期标记。首次沙箱调用的 `EACCES` 经网络权限放行后解决。随后尝试的真实情况如下：

| 尝试 | 结果与用途 | Provider 请求 / tokens |
|---|---|---|
| attempt01 | 没有预置明确行为验收入口，Agent 持续寻找验证方式；操作者终止专属 CLI，保留介入记录，不计成功 | 44 / 1,114,740 |
| attempt02 | 暴露缺少报告导致重试受阻；Agent 自主增加自检，最终 CLI 0。未使用后续全部修复，作为补充证据 | 25 / 684,859 |
| attempt03 | 部署 profile 端口冲突，HTTP 503、CLI 1；之后为每轮隔离全部端口 | 0 / 0 |
| attempt04 | 缺报告修复已真实生效、3 项行为验收通过；外层将重试误计为新深度，任务终态 blocked，旧 CLI 未退出。保留失败和介入记录 | 22 / 464,389 |
| attempt05 | 装载全部修复后无人为介入完成；CLI 0，独立复核通过 | 19 / 475,554 |

所有尝试的状态独立保存，未通过删改历史失败、降低行为验收标准或人工补产物取得最终成功。验证脚本在结束后关闭其拥有的 API 和模型进程。

## 回归验证

以下命令在仓库根目录执行。Windows 使用工作区内的 basetemp，避免系统临时目录权限问题。

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/integration/test_partitioned_inference.py tests/unit/test_runtime_protocol.py --basetemp .tmp/pytest-inference-final -o cache_dir=.tmp/pytest-cache-inference
.\.venv\Scripts\python.exe -m pytest -q tests/integration/test_phase2_production_policy_main_path.py --basetemp .tmp/pytest-inference-production-final -o cache_dir=.tmp/pytest-cache-inference
.\node_modules\bun\bin\bun.exe test packages/runtime/claude-runtime/test/runtime.test.ts
.\node_modules\bun\bin\bun.exe test ./apps/cli/test/runner-contract.test.ts
node node_modules/typescript/bin/tsc -p packages/runtime/claude-runtime/tsconfig.json
node node_modules/typescript/bin/tsc -p apps/cli/tsconfig.json
```

- 模型切分与运行时协议：33 项通过、5 个子测试通过。
- 生产策略：43 项通过、12 项独立 live-provider 开关控制的既有测试跳过；本记录中的真实 DeepSeek 任务另行执行，不能代替所有跳过场景。
- TypeScript 运行时：132 项通过。
- CLI 契约：19 项通过。
- 合计 227 项通过、5 个子测试通过；两处 TypeScript 类型检查、Bun/Node 工作器构建和 CLI 构建通过。
- 最终真实任务另有 3 项行为验收通过，独立交付复核通过。

覆盖真实分段端点、跨段原输入依赖、数值等价、故障退出与回退、隐私下限、时延、摘要与权限；回归测试也使用受控故障注入，不能将每项测试都表述为远程实机或真实 Provider 测试。

## 尚未完成的赛题专项证据

此次已实现可执行 ONNX 模型切分、真实受控 Agent 调用及约束路由，并修复真实任务暴露的报告重试、重试深度和 CLI 终态问题。但不能据此宣称已完整满足所有赛题要求：

1. 用户未提供两台独立设备。本次仅验证本机两个进程；实际设备/边缘两机 HTTPS 部署、真实带宽变化和硬件资源约束仍需专项实测。
2. 尚未执行可核验的千步 Agent 决策、多领域动态长程任务基准。必须预先定义实质性步骤和领域验收，禁止靠样本循环、心跳、内部迁移或重复校验凑步数。
3. 当前仅支持可推断中间契约的平坦 ONNX 图、预先选定的切点和 full/split 路由；未实现任意大模型分片、所有切点全局优化或正在执行算子的动态迁移。
4. 未运行整个 monorepo 的全部测试。上述回归和真实任务针对本次改动及暴露的失败路径。
