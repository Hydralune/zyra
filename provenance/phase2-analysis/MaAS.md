# MaAS 深入阅读记录

## 0. 结论先行

### 0.1 项目定位

**项目事实**：MaAS 的论文题目是 *Multi-agent Architecture Search via Agentic Supernet*，论文版本为 arXiv:2502.04180v2，并发表于 ICML 2025。项目试图把“为一个数据集搜索单一固定 Agent 工作流”改写为“学习一个按查询条件分配不同工作流的概率分布”。论文把这个分布称为 **agentic supernet**。

**分析判断**：MaAS 最有价值的贡献不是一个可直接作为长程 Agent 内核的完整运行时，而是一个清晰的调度思想：根据任务内容、难度和历史选择，在质量与成本约束下动态选择 operator 组合和推理深度。这更接近 query-conditioned contextual routing、稀疏 MoE 路由和离散策略学习，而不是具备长期状态、故障恢复、动态增删节点和 durable execution 的完整多 Agent 系统。

**代码结论**：当前仓库不能被视为论文结果的完整可复现实现。核心控制器和三个 benchmark 工作流确实存在，但数据集、预期格式的 controller checkpoint、结果日志和真正的测试套件均缺失；训练、文本梯度和测试主路径还存在多处确定性接口错误或语义偏差。仓库中的 277 个 Python 文件大部分来自 MetaGPT 0.8.1 派生代码，MaAS 专属的 `maas/ext/maas` 只有 57 个 Python 文件。

**迁移建议**：对 Zyra 第二阶段，MaAS 应作为 scheduler/router 子域的 supplementary implementation source，而不应取得 canonical graph runtime、长期状态、恢复协议或动态拓扑的 owner。优先内化其“operator 能力描述 + 查询条件路由 + 成本质量联合反馈 + 可学习 early-exit”思想；不应整体搬入 MetaGPT 派生框架、生成式 workflow 模板、共享累计成本器、无沙箱代码执行或当前文本梯度实现。

## 1. 阅读范围与证据边界

本记录完整阅读了以下材料：

| 证据类型 | 路径 | 用途 |
| --- | --- | --- |
| 论文 | `MaAS/2502.04180v2.pdf` | 研究问题、形式化方法、算法、实验与附录 |
| 项目介绍 | `MaAS/README.md` | 对外定位、数据准备和运行命令 |
| 安装配置 | `MaAS/setup.py`、`MaAS/requirements.txt`、`MaAS/config/**`、`MaAS/.maas/config2.yaml` | 包边界、依赖、模型配置与运行前提 |
| 入口 | `MaAS/examples/maas/optimize.py` | CLI 参数和训练/测试分流 |
| 控制器 | `MaAS/maas/ext/maas/models/controller.py`、`models/utils.py` | 查询编码、operator 打分、采样和 early-stop |
| 优化主链 | `MaAS/maas/ext/maas/scripts/optimizer.py`、`evaluator.py`、`optimizer_utils/**` | 数据加载、训练、checkpoint 与结果写入 |
| 评测 | `MaAS/maas/ext/maas/benchmark/**` | 数据集评分、策略梯度、成本惩罚和并发执行 |
| 工作流 | `MaAS/maas/ext/maas/scripts/optimized/{HumanEval,GSM8K,MATH}/**` | 三个数据集的实际 operator 和执行链 |
| 文本梯度 | `MaAS/maas/ext/maas/scripts/textgrad/**` | prompt 更新和候选 operator 生成 |
| 框架边界 | `MaAS/maas/actions/action_node.py`、`maas/provider/**`、`maas/utils/cost_manager.py` | 结构化 LLM 调用、provider 和成本累计 |

本次没有执行训练、测试或外部 LLM 调用。有关“当前提交会在何处失败”的结论来自静态调用链、函数签名和资产路径核对，不声称已经完成运行复现。

文中的标签含义如下：

| 标签 | 含义 |
| --- | --- |
| 项目事实 | 论文、源码、配置或仓库资产可直接证明 |
| 分析判断 | 基于多个项目事实形成的技术判断 |
| 迁移建议 | 面向 Zyra 第二阶段的设计建议，不是 MaAS 已实现能力 |

## 2. MaAS 真正解决的问题

### 2.1 论文针对的两个矛盾

**项目事实**：现有自动 Agent 设计方法通常为整个数据集搜索一个固定工作流。论文指出两个问题：

1. 同一数据集内，简单题和困难题使用同样复杂的 workflow，会浪费 token、LLM call、工具调用和时间。
2. 跨领域 benchmark 中，不同任务需要不同能力，例如文件读取、网页搜索、代码执行和数学推理无法由同一个固定结构同时最优地覆盖。

**分析判断**：MaAS 解决的是“推理时资源配置和 workflow 结构应当随输入变化”的问题。它不是在解决 session lifecycle、长期记忆、分布式 worker、任务依赖图、故障恢复或多人协作一致性。

### 2.2 从单一架构搜索到条件分布

论文定义的基本对象如下：

1. Agentic operator `O`：一组 LLM 调用、prompt、温度和工具调用构成的复合操作。CoT、Self-Consistency、Debate、ReAct 都可以被视为 operator。
2. Multi-agent system `G={V,E}`：从 operator 集中选择节点并组成 DAG。
3. Agentic supernet `A={π,O}`：包含 `L` 层 operator 概率分布的搜索空间。
4. Controller `Q_φ`：接收查询、operator 和前层选择，生成该查询对应的 `G`。

论文的核心目标可概括为：

```text
max E[ U(G; q, a) - λ C(G; q) ],  G ~ P(G | q)
```

其中 `U` 是质量或正确率，`C` 是 token 等成本，`λ` 控制质量与成本权衡。

**分析判断**：论文称 supernet 是“probabilistic, continuous distribution”，但实际被采样的是离散 operator 子集和离散深度。所谓 continuous 主要指 controller 参数和概率空间连续可优化，不代表执行图本身连续。

## 3. 论文方法

### 3.1 Operator space

论文附录列出八类初始 operator：CoT、LLM-Debate、Self-Consistency、Self-Refine、Ensemble、Testing、ReAct 和 Early Exit。operator 被允许包含多个模型调用和工具调用，所以“多 Agent”在这里是宽泛定义，不要求每个节点有独立身份、mailbox、状态或生命周期。

**分析判断**：这种 operator 抽象适合做能力级调度。它把复杂推理模式封装成可计价、可选择的动作，便于 router 在粗粒度上控制预算；但它也会掩盖 operator 内部的状态、权限和副作用，不能直接替代 Zyra 的可审计 runtime boundary。

### 3.2 Query-conditioned controller

论文使用轻量文本编码器 `v(·)` 对 query 和 operator profile 编码。每层 controller 根据 query、候选 operator 和前层 operator 产生分数，再根据阈值激活若干 operator。Early-exit operator 使实际深度小于最大层数 `L`。

论文公式描述的是把分数降序排列，依次激活，直到累计分数超过阈值。这样每层激活数可随查询变化。

### 3.3 成本约束优化

论文提出两类更新：

1. 对概率分布 `π`：使用 Monte Carlo 近似不可微执行环境下的梯度，并以质量和成本归一化后的 importance weight 更新 controller。
2. 对 operator `O`：使用 textual gradient 修改 prompt、temperature 和 operator node structure。

论文算法 1 表达的是一个交替过程：为训练 query 逐层采样、执行工作流、取得环境反馈、更新分布和 operator。

### 3.4 复杂度直觉

**分析判断**：controller 本身很轻。若每层有 `|O|` 个 operator，固定 embedding 维度下，打分大致随 `L × |O|` 线性增长。实际成本几乎完全由被选 operator 的 LLM 和工具调用决定。训练成本则近似为 `训练样本数 × Monte Carlo 样本数 × 每个采样工作流成本`。因此 MaAS 的价值来自减少昂贵调用，而不是减少 router 计算。

## 4. 论文实验及其含义

### 4.1 实验设置

**项目事实**：论文最终报告六个 benchmark：

| 领域 | Benchmark |
| --- | --- |
| 数学推理 | GSM8K、MATH、MultiArith |
| 代码生成 | HumanEval、MBPP |
| 工具使用 | GAIA |

数据按约 `1:4` 划分训练和测试。MATH 使用 617 个 level-5 问题。论文主要设置为 `gpt-4o-mini-0718`，并报告 Qwen 2.5 72B 和 Llama 3.1 70B 的迁移；`L=4`、`K=4`、阈值为 `0.3`，`λ` 在 `{1e-3, 5e-3, 1e-2}` 中选择。论文声称所有模型 API 温度设为 1。

### 4.2 主要结果

**项目事实**：表 1 中 MaAS 在五个数学/代码 benchmark 的平均分是 83.59，AFlow 是 82.25。MaAS 的分项结果为 GSM8K 92.30、MATH 51.82、MultiArith 98.80、HumanEval 92.85、MBPP 82.17。

**项目事实**：GAIA 平均分为 20.69，高于表中 AgentSquare 的 16.34、GPTSwarm 的 16.33 和 AFlow 的 8.00，但绝对正确率仍较低，尤其 Level 3 为 6.25。

**项目事实**：MATH 成本表中，MaAS 的训练 API 成本为 3.38 美元、wall-clock 53 分钟，推理成本为 0.42 美元、wall-clock 19 分钟，准确率 51.82。AFlow 对应为 22.50 美元、184 分钟、1.66 美元、23 分钟和 51.28。

### 4.3 消融与迁移

**项目事实**：HumanEval 上，完整 MaAS 为 92.85；去掉 textual gradient 为 90.17；去掉 early-exit 为 91.44；去掉成本项为 92.94。MATH 上对应为 51.82、48.23、51.53、51.19。

**分析判断**：论文自己的消融支持两个不同结论：textual gradient 主要影响性能；early-exit 和成本项主要影响资源使用，而不是显著提升准确率。这两类机制迁移时不应被混成一个模块。

**项目事实**：论文报告从 GPT-4o-mini 学到的 supernet 能迁移到 Qwen 和 Llama，也能在 MATH、GSM8K、HumanEval 之间迁移；还通过推理期加入训练未见的 Debate operator 展示 operator-level inductive generalization。

### 4.4 实验证据的边界

**分析判断**：主结果表没有给出多随机种子置信区间或显著性检验。代码中的 PyTorch、NumPy controller 初始化和 multinomial sampling 也没有统一随机种子。成本结果依赖外部 API 价格、并发和缓存条件，当前仓库没有随附原始日志来复核。

**分析判断**：这些 benchmark 都是相对短程的单问题求解。它们能证明 query routing 和 inference-time resource allocation 的价值，不能证明长程任务中的 checkpoint、恢复、需求变化、动态团队、权限、跨设备 dispatch 或数千状态转换。

## 5. 仓库结构与真实主执行链

### 5.1 仓库构成

**项目事实**：`setup.py` 仍声明包名 `metagpt`、版本 0.8.1、项目 URL 为 MetaGPT，并保留 `metagpt=metagpt.software_company:app` 的 console entry point；当前仓库文件中没有对应的 `metagpt` 包。`requirements.txt` 固定了大量与 MaAS 研究主链无关的 Web、RAG、多云 provider、GUI、数据处理和 MetaGPT 依赖。

**分析判断**：这是在 MetaGPT 快照上嵌入 MaAS 实验代码，而不是围绕 MaAS 重新裁剪的独立研究包。安装面、依赖面和 MaAS 实际执行面之间差距很大。

### 5.2 CLI 到训练

实际训练链如下：

```text
examples/maas/optimize.py
  -> EXPERIMENT_CONFIGS[dataset]
  -> ModelsConfig.default()
  -> Optimizer(...)
  -> Optimizer.optimize("Graph")
  -> Optimizer._optimize_graph_maas()
  -> GraphUtils.load_graph_maas(.../train/graph.py)
  -> Evaluator.graph_evaluate(..., is_test=False)
  -> BaseBenchmark.run_evaluation()
  -> BaseBenchmark.evaluate_all_problems()
  -> dataset Workflow.__call__()
  -> MultiLayerController.forward()
  -> selected Operator.__call__()
  -> ActionNode.fill()
  -> BaseLLM.aask()
  -> provider API
  -> score/cost/logprob
  -> controller Adam step
  -> torch.save(controller.state_dict())
```

### 5.3 CLI 到测试

```text
examples/maas/optimize.py --is_test True
  -> Optimizer.optimize("Test")
  -> Optimizer.test()
  -> load .../train/round_N/{dataset}_controller_sampleK.pth
  -> load .../test/graph.py
  -> evaluate_all_problems_test()
  -> save score CSV
```

### 5.4 核心源码职责

| 模块 | 关键符号 | 实际职责 |
| --- | --- | --- |
| `models/controller.py` | `OperatorSelector` | query/operator embedding 投影、余弦式打分、softmax |
| `models/controller.py` | `MultiLayerController` | 四层采样、首层 Generate 约束、EarlyStop、log-prob 汇总 |
| `models/utils.py` | `SentenceEncoder` | 冻结的 `all-MiniLM-L6-v2` 文本编码 |
| `scripts/optimizer.py` | `Optimizer` | 加载模板、controller、数据和 checkpoint |
| `benchmark/benchmark.py` | `BaseBenchmark` | 并发执行、评分、成本惩罚、策略梯度、CSV/checkpoint |
| `scripts/optimized/**/graph.py` | `Workflow` | 把 controller 选择翻译成具体 operator 调用 |
| `scripts/optimized/**/template/operator.py` | `Operator` 子类 | 生成、CoT、ensemble、Programmer、Test、SelfRefine |
| `actions/action_node.py` | `ActionNode.fill` | code/XML/single 三类结构化 LLM 输出 |
| `utils/cost_manager.py` | `CostManager` | 累计 prompt token、completion token 和价格 |

## 6. 论文机制与代码实现的对应关系

### 6.1 已经真实实现的部分

**项目事实**：以下机制在源码中存在真实执行路径：

1. Query 和 operator profile 使用 MiniLM 编码。
2. 四个独立层各自有一个 `OperatorSelector`。
3. Controller 对 operator 输出 softmax 概率并采样多个 operator。
4. EarlyStop 可以提前结束后续层采样。
5. 被选择 operator 的 log-prob 参与基于 reward 的 controller 更新。
6. HumanEval、GSM8K、MATH 有各自的 operator registry 和 Workflow。
7. LLM 调用通过统一 provider 和 `CostManager` 记录 token 成本。
8. 训练后试图保存 controller state dict，测试时试图加载 checkpoint。

### 6.2 代码实际没有形成显式 DAG

**项目事实**：`Workflow` 并没有构造 `G={V,E}` 对象。每层返回 operator 名称列表，代码用两层 `for` 循环顺序执行，并通过 `current_solution` 和 `solutions` 两个局部变量传递结果。

**分析判断**：当前实现搜索的是“每层 operator 子集、执行顺序和深度”，而不是通用 DAG 的节点与边。多 operator 同层也按采样返回顺序串行运行，没有显式并行、边概率、分支合并合同或图状态。

### 6.3 Controller 与论文的差异

**项目事实**：`sample_operators` 不是按分数降序选 top operators，而是用 `torch.multinomial` 无放回随机抽样，直到所选概率累计超过阈值。采样前对概率执行 `detach()`，随后通过被选项对应的 log-prob 做 REINFORCE 风格更新。

**项目事实**：非首层虽然接收上一层所有被选 operator embedding，但 `OperatorSelector.forward` 只使用 `prev_operators_embed[0]`，其余 operator 不进入层间条件。

**项目事实**：首层如果抽到 EarlyStop，会被替换为 Generate，并对 log-prob 加 `-1.5` 惩罚，然后停止后续层；如果首层没有 Generate，也会强制插入 Generate。

**分析判断**：这使实现具有实用的“先生成再处理”约束，但它不是论文形式化中无偏的通用 operator 分布。首层策略、层间条件和采样规则都包含未在论文公式中充分表达的手工规则。

### 6.4 训练目标与论文的差异

**项目事实**：公共训练器使用：

```python
utilities = scores_tensor - 3 * costs_tensor
loss = -(logprobs * utilities).mean()
```

它没有实现论文公式 11 中对 `p(a|q,G_k)` 和 `C(G_k;q)` 的归一化 importance weight，也没有从 CLI 接收论文的 `λ`。CLI 的 `--sample` 被用作完整数据集的 repetition 次数，而不是为同一个 query 同时采样 `K` 个 architecture 后进行归一化比较。

**分析判断**：当前代码是一个简化的 on-policy REINFORCE 训练器，不是论文所写 empirical-Bayes Monte Carlo estimator 的直接实现。

### 6.5 成本归因问题

**项目事实**：一个 benchmark evaluation 只实例化一个 Workflow，Workflow 中只有一个共享 LLM 和共享 `CostManager`。每个 query 返回的是当时的累计 `total_cost`，而不是该 query 的独立 cost。训练器再用 `cost - previous_cost` 计算增量，且 `previous_cost` 跨 batch 和 repetition 保留。

**项目事实**：最多 30 个 query 并发调用同一个 workflow 和成本器。异步完成顺序与 gather 返回顺序可能不同，因此累计快照差分不一定对应当前样本，甚至可能出现错误符号或把别的 query 成本归到当前 query。

**分析判断**：这会污染 policy reward。可学习资源分配必须建立在 run/query scoped cost ledger 上，而不是共享可变累计器的事后差分。

## 7. 三类实际 Workflow

### 7.1 HumanEval

可选 operator 是 Generate、GenerateCoT、MultiGenerateCoT、ScEnsemble、Test、SelfRefine 和 EarlyStop。

**项目事实**：Generate 和 GenerateCoT 在实际 `operator.py` 中实现相同，二者都直接把 problem 送入 `code_fill`，GenerateCoT 没有额外 CoT prompt。

**项目事实**：controller 路由结束后，Workflow 无条件再运行一次 `Test`。如果测试失败，还会调用 `CustomCodeGenerate` 使用 `IMPROVE_CODE_PROMPT` 再生成一次。因此 early-exit 只能减少中间层，不会跳过最终测试/补救。

**项目事实**：`Test.exec_code` 在当前 Python 进程中使用 `exec(..., globals())` 执行模型代码，没有隔离、权限、文件系统或网络边界。若 entry point 命中若干 hardcoded case，`extract_test_cases_from_jsonl` 返回空字符串，测试循环不执行任何 case，可能直接判定“no error”。

**分析判断**：HumanEval 路径体现了“路由 + 强制验证”的合理产品思路，但当前安全实现和测试可靠性不能进入生产运行时。

### 7.2 MATH

可选 operator 是 Generate、GenerateCoT、MultiGenerateCoT、ScEnsemble、Programmer、SelfRefine 和 EarlyStop。

**项目事实**：在 controller 选择任何 operator 之前，MATH Workflow 无条件执行 Programmer，再用 Generate 把代码结果改写成数学答案。随后才执行可学习路由，并在末尾做 ensemble。

**项目事实**：训练和测试的 selected Programmer 分支不同。训练版直接把 `result['output']` 加入候选；测试版会把 `result['code']` 再送给 Generate 做答案改写。

**分析判断**：MATH 的效果和成本包含一个固定昂贵前缀，controller 只优化额外推理。训练/测试分支不一致还会造成 policy 学到的 operator 效果与推理期不同。

### 7.3 GSM8K

**项目事实**：GSM8K 在路由结束后无条件调用 Programmer 做最终验证或重算，因此也有固定昂贵后缀。

**项目事实**：GSM8K Workflow 使用 `sum_log_prob += log_probs_layers[layer_idx].item()`，把带梯度 Tensor 转为 Python float。公共训练器随后对 batch 中的 log-prob 调用 `torch.stack(logprobs)`，该路径无法接受 float 列表。

**分析判断**：当前 GSM8K 训练主路径在进入 loss 前即会因类型不匹配失败，且即使改为 `torch.tensor` 也已经丢失 controller 梯度。

### 7.4 “多 Agent”与异构性的实际程度

**项目事实**：实际 operator 使用同一个 Workflow 级 LLM instance。MultiGenerateCoT 虽然生成三个答案，但使用三个连续 `await`，没有并行执行。代码中没有 Debate operator，也没有 GAIA 的 Web/File/Multimodal operator。

**分析判断**：当前代码实现的是同一模型上的多次推理模式组合，而不是具有独立身份、异构模型、长期状态和通信协议的 Agent team。MetaGPT provider 层支持多种模型 API，不等于单个 MaAS workflow 在运行中进行异构模型调度。

## 8. Textual gradient 的真实状态

### 8.1 两条互不闭环的实现

**项目事实**：仓库有两条 textual-gradient 相关路径：

1. `benchmark/benchmark.py` 在 repetition 分数下降后，随机选择 `op_prompt.py` 中一个 prompt，让 LLM 重写它。
2. `scripts/textgrad/textual_gradient.py` 使用三份大 archive prompt 生成新的 operator 候选并写入 `output_*.jsonl`。

### 8.2 主路径问题

**项目事实**：CLI 的 `--is_textgrad` 默认是 `False`。公共训练器只有在前一 repetition 分数下降、下一 repetition 开始且 `is_textgrad=True` 时才尝试 prompt 更新。

**项目事实**：`update_prompt_in_file` 定义需要 `(log_path, prompt_name, prompt_content)`，调用处只传 `(prompt_name, response['prompt'])`，会产生参数数量错误。

**项目事实**：独立 `textual_gradient.py` 没有被 `Optimizer` 调用，也没有把生成的 `code` 自动校验、注册、版本化或接入 operator registry。它在模块 import 时就读取配置并创建同步 OpenAI client，即使训练未启用 textual gradient，也增加了配置和导入副作用。

**项目事实**：代码没有实现论文公式 12 所说的 temperature gradient 和 node-structure gradient。主训练路径最多尝试修改一个 prompt 文件。

**分析判断**：论文消融中最重要的 operator textual gradient，在当前仓库里不是可执行的联合优化闭环。它应被视为不完整研究脚本，而不是成熟的自演化 runtime。

## 9. 当前提交的可运行性与复现性

### 9.1 运行前提

按照 README 和源码，至少需要：

1. Python 3.9 到 3.11。
2. 安装庞大的固定依赖集合，包括 CUDA 11.8 版 PyTorch、sentence-transformers、OpenAI SDK 和 MetaGPT 相关依赖。
3. 在 repo config 或 `~/.maas/config2.yaml` 配置模型 API。
4. 下载 MiniLM 模型，或已有本地 Hugging Face cache。
5. 手工放入三个数据集的 `*_train.jsonl` 和 `*_test.jsonl`。
6. 从项目根目录运行，使相对路径和动态 import 正确解析。

**项目事实**：配置未显式设置 temperature，`LLMConfig` 默认是 `0.0`，而论文实验声明温度为 1。MultiGenerateCoT 在默认温度 0 下的多样性会显著受限。

### 9.2 缺失资产

静态资产核对结果：

| 资产 | 当前状态 |
| --- | --- |
| `maas/ext/maas/data` | 不存在 |
| 顶层或项目级 `tests` | 不存在 |
| JSONL/CSV 数据文件 | 0 个 |
| `results.json`、`log.json`、结果 CSV | 不存在 |
| `HumanEval_controller_sample4.pth` | 不存在 |
| `GSM8K_controller_sample4.pth` | 不存在 |
| `MATH_controller_sample4.pth` | 不存在 |

仓库只带有五个 HumanEval 权重：一个 `embedding_reduction_mlp.pth` 和四个 `operator_selection_mlp_*.pth`。当前 `Optimizer.test` 只加载 `{dataset}_controller_sample{sample}.pth`，核心加载路径不引用这五个权重。

### 9.3 确定性代码断点

**项目事实**：`DataUtils.create_result_data` 要求 `round, score, avg_cost, total_cost, token`，但训练和测试调用处只传 `round, score`。昂贵的评测完成后，结果持久化会因缺少参数失败。

**项目事实**：`Optimizer.optimize` 把 `max_retries` 固定为 1，并只执行一次 `_optimize_graph_maas`；局部变量 `round` 从 1 增加后即退出，不构成论文式多轮优化。

**项目事实**：`argparse` 对 `--is_test` 和 `--is_textgrad` 使用 `type=bool`。在 Python 中，命令行字符串 `"False"` 仍会被转换为 `True`，CLI 语义不可靠。

**项目事实**：README 的测试命令依赖不存在的 controller checkpoint，因此干净目录下不能直接按文档完成测试。

### 9.4 测试与证据

**项目事实**：路径名中的 `optimized/*/test` 是测试数据集用 workflow，不是软件测试。仓库没有单元测试、集成测试、固定 provider fake、controller 梯度测试、成本归因测试或 checkpoint round-trip 测试。

**分析判断**：论文结果目前只能由论文表格支持，不能由仓库内原始日志、固定数据、脚本和测试链独立重建。不能把预生成 operator 模板或若干 `.pth` 文件当作复现实验证据。

## 10. 工程优势

### 10.1 值得保留的机制

1. Query-conditioned routing：比为全数据集搜索一个固定 workflow 更符合真实负载差异。
2. Operator capability profile：用自然语言描述能力和接口，再做 embedding 路由，扩展新 operator 的成本较低。
3. 动态 breadth 和 depth：每层可选多个 operator，EarlyStop 控制深度。
4. 质量成本联合 reward：把资源消耗纳入策略学习，而不是只追求正确率。
5. Operator holdout 思路：测试未见 operator 能否根据 profile 被合理路由，适合验证插件/skill 的冷启动泛化。
6. 训练与测试 workflow 分目录：虽然当前有偏差，但意图上区分 policy learning 与 frozen-policy evaluation 是正确的。
7. Dataset benchmark abstraction：统一返回 prediction、score、cost 和 log-prob，适合作为离线 policy evaluation 的起点。

### 10.2 研究价值

**分析判断**：MaAS 把 NAS、MoE 和 Agent workflow automation 三条思路连接起来，提供了比“让 LLM 自己写一个巨大工作流”更可控的搜索空间。尤其是 query-dependent resource allocation，对长程系统中的阶段级 worker/skill/model 选择有直接启发。

## 11. 局限与风险

### 11.1 算法边界

1. 预定义四层和预定义 operator 集，不能在同一个 run 中新增、删除、替换 node、edge、role 或 capability。
2. 没有 canonical graph state、immutable snapshot、read/write set、冲突检测或确定性 commit。
3. 没有 session、memory、artifact、permission、lease、idempotency、checkpoint 或 exact resume。
4. 没有长期 credit assignment，reward 只来自单个 benchmark 问题的最终 0/1 分数。
5. 没有显式 uncertainty、constraint satisfaction 或安全预算，只用单一线性成本惩罚。
6. 没有稳定的 per-query cost attribution。

### 11.2 工程风险

1. HumanEval 在主进程直接执行模型代码。
2. MATH/GSM8K 的 import 字符串黑名单不是安全沙箱，仍可访问危险 builtin、文件和网络。
3. 外部 API、模型下载和动态 import 都缺乏 cleanroom 封装。
4. 一个模块 import 即可能创建外部 OpenAI client。
5. 通用 MetaGPT provider 包在 import 时加载大量 provider，扩大依赖和失败面。
6. 共享可变 CostManager 与高并发任务存在归因竞争。
7. 训练和测试 workflow 存在行为漂移。

### 11.3 研究表述与代码范围

1. 论文覆盖六个 benchmark，代码只支持 HumanEval、GSM8K、MATH。
2. 论文 operator space 包含 Debate、ReAct、Ensemble 等，当前 registry 只实现较小子集。
3. 论文强调 tool-use/GAIA，仓库没有对应 benchmark 和 Web/File operator 主链。
4. 论文强调 prompt、temperature、node structure 联合 textual gradient，代码主链没有后两者。
5. 论文公式描述归一化 Monte Carlo 更新，代码使用简化 policy gradient。

## 12. 对长程复杂任务的实际价值

### 12.1 可以解决的子问题

**分析判断**：MaAS 适合解决长程系统中的以下局部问题：

1. 为当前 subtask 选择轻量或重型 worker runtime。
2. 在 local、edge、cloud 模型之间进行质量/成本/时延权衡。
3. 选择是否启用检索、浏览器、代码执行、debate、verification 或多样本 ensemble。
4. 在证据足够时提前停止扩展推理。
5. 对新注册 skill/operator 做 profile-based cold-start 路由。

### 12.2 不能直接承担的系统职责

**分析判断**：MaAS 不适合作为以下职责的 primary owner：

1. Zyra canonical dynamic topology。
2. 长期 durable task graph 和 event sourcing。
3. CodeWorker 的连续 reason-tool-observe-revise loop。
4. Permission、sandbox 和 side-effect commit。
5. Fault recovery、lease、retry、resume 和 branch merge。
6. Memory retrieval、compact/restore 和 artifact lineage。

关键原因是 MaAS 只在预声明的四层搜索空间中选择 operator。它能动态“选路”，但不能在运行中改变 canonical graph schema，也不保存可恢复的运行状态。

## 13. Zyra 第二阶段迁移建议

### 13.1 来源角色裁决

| MaAS 子域 | 建议来源角色 | 理由 |
| --- | --- | --- |
| Query-conditioned operator routing | supplementary implementation source | 可补强 Zyra scheduler 的能力级路由 |
| Cost-quality objective | supplementary implementation source | 可扩展多目标 placement/dispatch policy |
| Operator profile embedding | supplementary implementation source | 可用于 skill/worker 冷启动和候选召回 |
| Early-exit policy | supplementary implementation source | 可形成预算与证据驱动的停止策略 |
| Controller benchmark/ablation 思路 | conformance/evaluation source | 适合静态策略对照和消融 |
| Agentic supernet 作为 canonical graph | rejected | 预定义层和 operator 集不满足 Zyra 动态拓扑 |
| MetaGPT 派生运行时整体 | rejected | 依赖面大、职责重叠、缺少 durable state |
| 当前 textual gradient | reference only | 主路径断裂，无法安全自修改生产代码 |
| 当前 code execution | rejected | 无可靠 sandbox 和 permission fence |

### 13.2 建议内化的模块边界

迁移时不要复制 `maas/ext/maas` 的目录形状。建议拆成 Zyra-owned 机制：

1. `OperatorCatalog`：为 skill、worker、model、tool-chain 保存稳定 ID、能力、输入输出合同、风险、placement 和成本画像。
2. `QueryFeatureEncoder`：从当前 subtask、上下文摘要、失败历史、隐私等级和预算生成特征。
3. `ArchitecturePolicy`：为候选 operator/worker 打分，输出带概率、预算和解释的 route proposal。
4. `BudgetedSelector`：在 token、货币、时延、并发、隐私和可靠性约束下选择 breadth/depth。
5. `EarlyExitGate`：根据 verifier 证据、剩余预算和不确定性生成显式停止决定。
6. `OutcomeAttributor`：按 run/route/operator 精确记录质量、成本、时延、失败和副作用。
7. `PolicyTrainer`：只从 sealed evidence 更新离线 policy，并支持 frozen version、shadow evaluation 和 rollback。

### 13.3 必须接入的 Zyra 一等事实

建议把每次路由形成可审计事件，而不是只保存在模型 Tensor 中：

| 事件/状态 | 至少包含 |
| --- | --- |
| `route_candidates_scored` | query/subtask ID、policy version、operator IDs、scores、feature digest |
| `route_selected` | selected IDs、selection probability、预算、随机种子、约束理由 |
| `operator_started` | worker/placement、input artifact、permission decision、attempt ID |
| `operator_finished` | output artifact、actual cost、latency、verification、failure class |
| `early_exit_decided` | evidence、confidence、remaining budget、decision rule |
| `policy_feedback_recorded` | immutable outcome、credit assignment、training eligibility |

**迁移建议**：route proposal 只能提出选择，canonical task graph 的 mutation、lease 和 commit 仍由 Zyra runtime 负责。这样 MaAS 风格 policy 不会成为第二个状态 owner。

### 13.4 算法改造要求

1. 用 run-scoped cost ledger 替换共享累计 `CostManager`。
2. 用显式 `λ` 或约束优化替换硬编码 `3 * cost`。
3. 给每次采样记录 RNG seed、policy version 和候选快照，保证 replay。
4. 使用所有前层选择的聚合表示，而不是只取第一个 operator。
5. 区分确定性 top-k、随机探索和 sealed benchmark policy。
6. 引入 advantage baseline、off-policy 校正或 contextual-bandit 估计，避免简单稀疏 0/1 reward 的高方差。
7. 把 early-exit 设为 verifier 后的状态转换，不允许仅凭 router 概率跳过安全验证。
8. 把 operator 级 cost、latency、failure、privacy 和 placement 都纳入多目标约束。
9. 训练时冻结生产 policy，先 shadow evaluation，再通过版本化 promotion 上线。
10. 新 operator 必须通过 schema、permission、sandbox、behavior test 和 disable-test，不能由 LLM 生成代码后自动注册。

### 13.5 不建议直接迁移的代码

1. `setup.py` 和整个 MetaGPT provider/runtime 派生层。
2. 三套重复的 `optimized/{dataset}/{train,test}/template` 目录。
3. `ActionNode` 作为 Zyra operator runtime。
4. `sample_operators` 当前随机阈值实现。
5. `BaseBenchmark.evaluate_all_problems` 当前共享成本和并发逻辑。
6. `scripts/textgrad` 的代码生成 archive。
7. HumanEval 当前 `exec` 测试和数学 Programmer 的字符串黑名单。

## 14. 建议的 Zyra 验收实验

若第二阶段内化 MaAS 思想，至少应有以下对抗验证：

1. Static workflow 与 query-conditioned routing 的质量、token、成本、时延对照。
2. 去掉 cost constraint、early-exit、operator profile、history feature 的独立消融。
3. 简单、中等、困难任务上的实际 operator 数和深度分布。
4. 新增未见 skill/worker 后的 cold-start 路由质量。
5. local、edge、cloud 三类真实 dispatch 的预算与隐私约束。
6. Operator 超时、节点失效、模型断连后的 route recovery，而不是仅返回低分。
7. 同一 event log 的 deterministic replay 和 exact-resume。
8. 禁用 router 后真实任务行为必须改变，证明动态可达性。
9. 禁用 verifier 时 early-exit 必须被安全门禁拒绝。
10. 训练 policy 与 frozen production policy 的版本隔离和 rollback。

## 15. 最终评价

| 维度 | 评价 | 说明 |
| --- | --- | --- |
| 研究问题价值 | 高 | 准确抓住固定 workflow 的资源浪费和跨域失配 |
| 方法新意 | 高 | 把 supernet/MoE/NAS 思想引入 query-conditioned Agent workflow |
| 论文实验说服力 | 中高 | 结果、成本、消融和迁移较完整，但缺少仓库内复现资产 |
| 当前代码完整性 | 低 | 只覆盖三类 benchmark，关键路径存在接口和梯度问题 |
| 当前工程成熟度 | 低 | 无测试、无数据、无可加载 controller checkpoint、依赖面过大 |
| 直接代码迁移价值 | 低到中 | 个别 controller/benchmark 思路可裁剪，不能整体迁移 |
| Zyra 架构借鉴价值 | 高 | 适合作为 scheduler 的能力级稀疏路由和预算策略来源 |
| 作为长程 runtime 主干 | 不适合 | 缺少 durable state、动态 canonical graph、恢复和安全边界 |

**最终判断**：MaAS 应被认真吸收，但要吸收的是“查询条件下的预算化 operator 路由”这一算法与产品机制，而不是把当前实验仓库当作可直接运行的多 Agent 基础设施。对 Zyra 最合理的内化方式，是让它成为现有 scheduler、worker catalog 和 evidence system 之上的可替换 policy layer；所有状态、事件、权限、side effect、checkpoint 和 recovery 继续由 Zyra-owned runtime 承担。

