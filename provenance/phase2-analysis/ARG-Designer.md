# ARG-Designer 深入阅读记录

## 1. 项目定位与结论

ARG-Designer 对应论文 **Assemble Your Crew: Automatic Multi-agent Communication Topology Design via Autoregressive Graph Generation**。论文发表于 AAAI 2026，并被项目 README 标注为 oral 工作。

这个项目专精于一个边界很清楚的问题：**针对当前任务，从角色池中自动选择 Agent，并同时生成这些 Agent 之间的有向通信拓扑**。它不是通用 Agent 运行时，也不直接解决长期记忆、动态调度、故障恢复或工具权限等完整系统问题。

本记录使用以下标记区分结论性质：

- **[论文事实]**：来自正式论文的定义、算法或实验结果。
- **[代码事实]**：来自当前仓库源码、数据和配置的可核对事实。
- **[分析判断]**：基于论文与实现对应关系得出的判断。
- **[迁移建议]**：面向 Zyra 第二阶段的复用或改造建议。

综合结论如下：

1. **[分析判断] 研究思想具有较高参考价值。** 它把传统的“在固定模板上改图”改写为“按任务条件生成完整角色序列和边序列”，让团队组成和通信结构成为可学习、可扩展的策略输出。
2. **[分析判断] 当前开源仓库的工程成熟度明显低于论文方法的完整度。** 核心模型可以辨认，但存在公式实现偏差、生成状态未完整回写、角色扩展接口不闭合、实验路径缺失或损坏、成本统计未接通、硬编码路径、无依赖清单和无测试等问题。
3. **[迁移建议] 不应把该仓库整体作为第二阶段的图运行时或 canonical topology owner。** 更合适的定位是：把 ARG 方法裁剪成 Zyra 自有动态拓扑系统中的一个**任务/阶段级拓扑提案策略**；其输出必须经过能力、权限、资源、可达性和故障域约束验证，再以 canonical topology mutation 事务提交。
4. **[迁移建议] 值得代码级吸收的是核心生成模型、角色语义索引和课程数据构造思想；需要重写的是数据协议、约束投影、训练目标、状态持久化与运行时接入；不建议迁移其 legacy MAS runtime、LLM wrapper 和不安全代码执行器。**

## 2. 阅读范围与版本

### 2.1 本地版本

- 仓库：`long-horizon-systems/ARG-Designer`
- 分支：`main`
- 阅读时提交：`c4de09317ab31f7f5438a93d954e089ecf302dd6`
- 该提交日期：2026-05-11
- 远程仓库：`https://github.com/Shiy-Li/ARG-Designer.git`
- 源码与配置未修改；当前 Git 状态只有用户新增的 `2507.18224v4.pdf` 为未跟踪文件，没有 tracked source diff

### 2.2 已完整阅读和核对的材料

- 本地论文 `ARG-Designer/2507.18224v4.pdf`：arXiv v4，10 页，SHA-256 `F73928F7C172FABFE2D9D4391733920A309AA1EA06768634CFCECB6A966D089E`；已完整阅读正文、算法、图表和参考文献，并对方法页、实验页与案例图做渲染核验。
- 正式 AAAI 论文与 arXiv 版本，包括正文、算法、实验、消融、鲁棒性、扩展性讨论和参考文献。
- 仓库 `README.md`、`template.env`、全部 81 个 Python 源文件、3 个 JSONL 数据文件和任务切分 JSON。
- `experiment/model.py`、图转换、模型训练、采样、冷启动、微调和评测链路。
- MMLU、GSM8K、HumanEval、AQuA 的数据适配器与实验脚本。
- `mas_framework` 中图运行时、Agent、提示词、LLM、代码执行器、Reader、Search 等模块。
- 数据集记录数、关键字段、首尾记录、HumanEval 固化切分和 MMLU 下载方式。
- 提示词副本的文件哈希：`experiment/prompt` 与 `mas_framework/prompt` 中除 MMLU 外的对应文件内容一致；MMLU 两份实现存在差异。
- 81/81 个 Python 文件完成 AST 解析；仓库内无语法解析失败。

仓库静态规模为 81 个 `.py` 文件、约 11,831 行 Python；同时跟踪了 33 个 `.pyc` 文件。根目录没有 `requirements.txt`、`pyproject.toml`、lockfile 或环境文件，仓库中也没有测试文件。

本次没有安装依赖、调用付费模型或执行整条实验流水线。原因不是阅读范围不足，而是仓库没有锁定依赖和模型 checkpoint，部分实验脚本静态上已经存在无法解析的导入、未定义符号或硬编码路径；在这种状态下直接发起外部 API 实验不能形成可靠复现证据。

### 2.3 论文来源

- AAAI 文章页：<https://ojs.aaai.org/index.php/AAAI/article/view/39481>
- AAAI PDF：<https://ojs.aaai.org/index.php/AAAI/article/download/39481/43442>
- arXiv：<https://arxiv.org/abs/2507.18224>
- arXiv HTML：<https://arxiv.org/html/2507.18224>

## 3. 它具体解决什么问题

### 3.1 传统方法的限制

**[论文事实]** 论文认为既有多 Agent 拓扑设计大多依赖以下两类方式：

- 人工选择 Chain、Star、Mesh、Full-connected 等固定模板；
- 先给定一个模板，再通过增删节点或边进行离散搜索或优化。

这类方法把搜索空间限制在预定义模板附近，并且通常把 Agent 选择和通信结构优化拆开处理。对任务语义差异较大的输入，它们不能自然地产生全新的团队构成与拓扑。

### 3.2 ARG-Designer 的重写方式

**[论文事实]** ARG-Designer 把团队设计建模为条件自回归图生成：给定任务 `Q` 与候选角色池 `R`，依次生成节点角色，再为每个新节点生成来自既有节点的入边。

其概率分解为：

```text
P(G | Q, R)
= Π_i P(v_i | G_<i, Q, R)
  · Π_i Π_{j<i} P(e_{j,i} | v_i, G_<i, Q)
```

这里的关键不是单纯“预测一张图”，而是把图的构造顺序显式化：

1. 根据任务和已生成历史选择下一个角色；
2. 决定哪些已有节点向该节点发送消息；
3. 遇到 END 后停止生成；
4. 把生成的 DAG 交给实际多 Agent 执行器运行。

**[分析判断]** 这种分解的真正价值是把“团队成员是谁”和“谁应听谁”放进同一条件策略中，同时保留可变节点数和可变边数。它比对固定图做局部打分更适合作为开放角色池下的拓扑提案器。

## 4. 核心方法

### 4.1 图与协作语义

**[论文事实]** 多 Agent 系统被表示为有向无环图 `G=(V,E)`：

- 节点 `v_i` 对应一个具有角色 `r_i` 和内部状态 `s_i` 的 Agent；
- 边 `e_{j,i}` 表示 Agent `j` 的输出可以作为 Agent `i` 的上下文；
- 每轮按拓扑序执行；
- 最终由 summarizer 汇聚各 Agent 的输出。

论文用 `K` 轮协作描述 Agent 状态更新，并在实验设置中报告 `K=3`。

**[论文事实]** 论文公式 (2) 更具体地把第 `k` 轮 Agent `i` 的邻居上下文写成前驱 Agent 的**上一轮输出** `{m_j^(k-1)}`，而不是同一轮拓扑序中刚生成的 `{m_j^k}`。这使论文中的有向边同时带有明确的跨轮信息传递语义；论文未单独展开第 1 轮该集合的初始化方式。

### 4.2 任务与角色表征

**[论文事实]** 任务描述和角色描述使用 `all-MiniLM-L6-v2` 编码为 384 维语义向量。角色矩阵既是候选空间，也是节点生成器输出匹配的度量空间。

**[代码事实]** 当前实现会：

- 用 SentenceTransformer 生成任务嵌入与角色描述嵌入；
- 在 `ARGDesigner` 中用前馈层处理任务嵌入；
- 将角色嵌入拼入 `full_embedding_matrix`；
- 用节点隐藏状态与角色矩阵点积后 softmax，得到角色分布。

### 4.3 历史—任务门控

**[论文事实]** 论文使用一个历史 GRU 汇总已生成节点，再通过任务相关门控融合历史：

```text
g_i = sigmoid(f_hist · f_Q / sqrt(d))
```

融合结果进入节点 GRU，作为选择下一角色的条件。

**[代码事实]** `experiment/model.py` 的实现把点积除以 `self.embedding_dim`，即除以 `d`，而不是论文公式的 `sqrt(d)`。在 `d=384` 时，两者分母约为 384 与 19.6。

**[分析判断]** 这不是无关紧要的书写差异。除以 `d` 会显著压小门控 logit，使门值更容易停留在 0.5 附近，可能削弱任务对历史状态的条件调制。迁移时应以论文公式为准并重新做消融。

### 4.4 节点生成器

**[论文事实]** 节点生成器按顺序选择角色，END 是可学习的终止嵌入。角色选择通过隐藏状态与投影后的角色向量做度量匹配，因此论文把“增加新角色”描述为可通过扩展角色池实现，而不必重构一个固定类别分类头。

**[代码事实]** 当前实现的角色输出确实采用嵌入匹配，但 START/END 都由全零 tensor 构造，并通过 `register_buffer` 固化在完整嵌入矩阵中，不是可学习参数。采样时还会在达到最少节点数前屏蔽 END。

**[分析判断]** 度量式角色选择保留了角色扩展的研究思路，但当前代码没有完整实现论文所称的开放扩展能力：新增角色仍必须进入 `role_to_id`、候选矩阵和 checkpoint 兼容路径，不能只追加一段自然语言描述就稳定工作。

### 4.5 边生成器

**[论文事实]** 对每个新节点，边 GRU 依次判断每个已有节点是否向它连边。节点生成顺序天然规定边方向，因此可以保持 DAG。

**[代码事实]** 本地数据处理统一设置 `max_prev_node=3`，模型只编码和生成距离当前节点最近的至多 3 个前驱候选，而不是论文算法伪代码所表示的全部历史节点。

**[分析判断]** 全历史入边生成的边决策数量是 `O(N²)`；本地三节点窗口将其约束为 `O(Nw)`，其中 `w=3`。这个工程选择提高了可扩展性，但也会让跨较远生成位置的直接依赖不可表达。它应当被明确写成模型配置，而不是隐藏在数据处理脚本中。

### 4.6 课程学习

**[论文事实]** 训练数据分两阶段：

1. `D_exp`：在少量基础任务上运行较复杂、较稠密的团队配置，只保留成功图，用于学习有效协作模式；
2. `D_eff = D_simple ∪ D_pruned ∪ D_replay`：加入简单拓扑、由成功图剪枝得到的图以及 replay 样本，继续优化效率与泛化。

训练损失为：

```text
L_total = α L_node + (1-α) L_edge
```

论文和代码均使用 `α=0.2`。

**[代码事实]** 仓库的所谓 pruning 实际只随机移除一部分边，没有系统逐节点/逐边评估，也没有删除节点。代码用已经 softmax 的节点/边概率与 one-hot/binary 目标计算 `binary_cross_entropy`，而不是直接实现论文文字所描述的标准 categorical node NLL 与 Bernoulli edge NLL。

**[分析判断]** 两阶段课程的方向有价值，但当前 `D_pruned` 更接近随机稀疏增强，而不是因果意义上的“找出最小充分团队”。成功样本过滤还会引入选择偏差：模型只看到成功图，却不直接学习哪些拓扑为什么失败。

## 5. 代码结构与真实执行链

### 5.1 关键模块

| 路径 | 实际职责 | 阅读判断 |
|---|---|---|
| `experiment/model.py` | 图张量化、ARG 模型、训练损失、采样与 NetworkX 图恢复 | 研究核心，最值得裁剪吸收 |
| `experiment/process_dataset.py` | 按数据集选择 adapter、图编码窗口等统计量 | 配置分散，存在缺失 adapter |
| `experiment/*/*_adapter.py` | 冷启动图加载、角色嵌入、训练样本生成、随机剪边 | 可提取数据思想，不宜原样迁移 |
| `experiment/*/cold_start_*.py` | 生成基础/微调任务切分，执行人工拓扑并保存成功图 | 课程数据入口 |
| `experiment/*/finetune_*.py` | 预训练、生成候选图、剪边、replay、微调 | 数据闭环入口，但各数据集实现漂移明显 |
| `experiment/*/evaluate_*.py` | 加载模型、生成拓扑、运行 Agent 图、评估答案 | 实验评测入口 |
| `mas_framework/graph/graph.py` | legacy Graph 与实验实际使用的 `TestGraph` | 运行时与模型耦合弱，工程问题较多 |
| `mas_framework/graph/node.py` | 节点、空间/时间前驱、上下文拼接、Agent 调用 | 多轮状态语义的基础实现 |
| `mas_framework/agents/**` | 任务 Agent 与 FinalDecision Agent | 主要是提示词包装和少量工具调用 |
| `mas_framework/llm/gpt_chat.py` | OpenAI 异步调用封装 | 参数和成本统计未接通 |
| `mas_framework/tools/coding/**` | HumanEval/Python 执行 | 直接在宿主进程 `exec/eval`，不可作为安全运行时 |

### 5.2 冷启动到评测的预期流程

结合 README 与源码，项目预期主链是：

```text
少量训练任务
  -> 运行 FullConnected / Mesh 等复杂人工拓扑
  -> 只保存答对的图
  -> 将图、角色描述和任务嵌入编码为训练样本
  -> 预训练 ARG
  -> ARG 生成候选图
  -> 加入 Chain / Star / Layered、随机剪边图和 replay 图
  -> 微调 ARG
  -> 对测试问题采样任务特定 DAG
  -> TestGraph 调用 LLM Agent
  -> FinalDecision 汇总并计算准确率
```

**[分析判断]** 这是一个“离线收集—离线训练—在线一次生成—外部执行”的拓扑策略闭环。它不是在单次任务运行中持续观察状态并修改拓扑的动态系统。

### 5.3 图执行器的真实行为

**[代码事实]** 实验路径主要把 ARG 生成的 NetworkX DAG 转为 `TestGraph`：

- 按入度维护队列，异步执行当前可运行节点；
- 前驱输出作为后继节点的空间消息；
- 失败后按 `max_tries` 重试；
- 全部 Agent 完成后，把它们接到 FinalDecision 节点；
- 默认 `num_rounds=1`。

`TestGraph` 没有构造跨轮 temporal edges。虽然 `Node` 支持 `temporal_predecessors` 和 `last_memory`，但实验生成的 TestGraph 没有把这些连接建立起来。因此把 `num_rounds` 调大，只是重复运行同一空间图并更新节点内存，不等于完整实现论文中显式的跨轮消息关系。

**[分析判断]** 这里还存在比“默认轮数不同”更直接的论文—代码错位：论文公式 (2) 要求边 `(v_j,v_i)` 在第 `k` 轮传递 `m_j^(k-1)`，而 TestGraph 主路径主要把本轮已经完成的前驱输出作为 spatial message 传给后继。代码的 `last_memory` 也没有按生成图的边建立对应 temporal predecessor。因而公开执行器实现的是同轮 DAG 流水线，不是论文公式定义的跨轮邻接消息协议。

另一个 legacy `Graph.run()` 会调用 `construct_spatial_connection()`，但构造函数中 `self.spatial_logits` 的初始化被注释；除非走其它先行赋值路径，同步运行接口不能独立成立。论文实验主链使用 `TestGraph`，所以这更说明仓库中同时保留了未收束的旧运行时代码。

## 6. 论文实验与本地材料

### 6.1 论文设置

**[论文事实]** 论文使用 `GPT-4o-2024-08-06` 作为 Agent 模型，协作轮数 `K=3`，每个数据集只使用 40 或 60 个训练问题，并在六个 benchmark 上报告结果。

| Benchmark | 论文测试规模 | ARG-Designer 准确率 |
|---|---:|---:|
| MMLU | 153 | 89.54 |
| GSM8K | 1,319 | 94.40 |
| AQuA | 254 | 86.45 |
| MultiArith | 600 | 98.93 |
| SVAMP | 1,000 | 95.63 |
| HumanEval | 164 | 91.74 |
| 平均 | - | 92.78 |

论文中 G-Designer 的平均准确率为 90.04，ARG-Designer 为 92.78。GSM8K 上 ARG 使用约 `4.1×10⁶` tokens，论文称相比 G-Designer 约降低 50%。课程微调把 GSM8K token 数从约 `6.25×10⁶` 降到 `4.1×10⁶`，MMLU 也降低近 30%。

消融实验在 MMLU、GSM8K、HumanEval 上的三任务平均结果为：Vanilla 78.02、完整 ARG 91.89、去掉 fine-tune 91.28、去掉 task embedding 89.76、去掉 graph history 90.64。任务嵌入的影响大于历史编码，说明“按任务改变团队结构”是方法的主要收益来源之一。

论文还报告：

- 在 prompt attack 下，ARG 的下降为 2.15%，在所比较方法中最小；
- 通过加入 `Lawyer` 角色展示角色池扩展案例；
- 生成图通常比复杂模板更稀疏，从而减少 token 消耗。

**[分析判断]** 鲁棒性证据应按有限范围理解：论文只展示向单个 Agent 注入系统 prompt 的攻击案例；公式 (18)–(21) 的训练目标是成功图的条件似然，没有显式攻击增强、鲁棒性正则、最坏情形目标或故障约束。论文把较小降幅解释为“训练目标抑制脆弱结构、形成分布式冗余路径”，但该因果解释没有被专门消融验证，更适合作为事后解释而不是已证实机制。

### 6.2 本地数据与论文口径的差异

| 数据集 | 仓库材料 | 与论文评测口径的关系 |
|---|---:|---|
| GSM8K | 1,319 条 JSONL | 冷启动脚本保留前 40 条训练，默认评测其余 1,279 条，并非论文表中的 1,319 |
| AQuA | 254 条 JSONL | 同样保留 40 条后默认只剩 214 条评测；而且当前流水线静态上无法直接运行 |
| HumanEval | 161 条 JSONL | 比标准/论文口径的 164 少 3 条；固化切分只评测索引 40..160，共 121 条 |
| MMLU | 数据未入库 | loader 硬编码读取 `/root/GDesigner/datasets/MMLU/data/...`；val 规模预期为 153 |
| MultiArith | loader 中含 600 条数据 | 没有对应 experiment 目录、adapter、训练或评测脚本 |
| SVAMP | loader 中含 1,000 条数据 | 没有对应 experiment 目录、adapter、训练或评测脚本 |

**[分析判断]** 仓库现有脚本不能直接复现论文六数据集表格，且 GSM8K、AQuA、HumanEval 的默认实际测试规模与论文表不同。论文结果本身应按正式论文事实记录，但不能把“仓库包含某些数据”误判为“开源实现已经提供相同评测路径”。

## 7. 论文—代码对应中的关键问题

### 7.1 核心模型语义问题

#### 1. 门控缩放与论文公式不同

- **[论文事实]** 分母为 `sqrt(d)`。
- **[代码事实]** `experiment/model.py:450` 和 `:639` 除以 `d`。
- **[影响判断]** 可能削弱任务条件门控；迁移时必须修正并重新训练。

#### 2. END 不是可学习向量

- **[论文事实]** END token 使用可学习嵌入。
- **[代码事实]** `experiment/model.py:396-399` 用全零 START/END 组成 buffer。
- **[影响判断]** 停止决策的表达能力和论文描述不一致；零 END 还会与度量空间几何产生特殊偏置。

#### 3. 采样结果没有写回 `x_pred_node`

- **[代码事实]** `sample()` 初始化全零 `x_pred_node`，角色名称被追加到 `generated_roles`，但采样的角色 id 没有写入该数组。后续图恢复又读取 `x_pred_node` 作为 node label 和 END 判断。
- **[影响判断]** Agent 执行仍可依赖 `generated_roles` 得到角色名，但保存图中的节点 label/type 一致性被破坏，END 相关恢复逻辑也不可信。这会污染 replay、可视化和后续训练闭环。

#### 4. 角色扩展链路不闭合

- **[代码事实]** 采样前通过 `selected_role_ids = [role_to_id[r] ...]` 把候选名映射到已有 id；完整嵌入矩阵又是 checkpoint buffer。
- **[影响判断]** 论文的“扩充角色池无需重新训练”在概念上依赖度量学习，但当前代码缺少稳定的动态注册、id 分配、checkpoint 兼容和角色执行能力绑定接口。

#### 5. `most_similar_roles` 计算后未使用

- **[代码事实]** 采样函数根据任务检索 top-5 相似角色，但后续真正使用的候选集合仍由传入的选择路径决定，局部变量没有进入生成循环。
- **[影响判断]** 这段逻辑不能作为“任务先检索候选角色”的有效实现证据。

#### 6. 图窗口与论文算法不同

- **[论文事实]** Algorithm 1 对此前节点逐一生成入边。
- **[代码事实]** 六个数据集配置都设置 `max_prev_node=3`。
- **[影响判断]** 本地模型是有限历史边解码器；这是合理的可扩展变体，但论文和配置应明确区分。

#### 7. 生成后存在硬修复

- **[代码事实]** 采样会强制每个非首节点至少有一条入边，并在图生成后修复孤立/不连通成分、移除检测到的环。
- **[影响判断]** 最终执行图不完全来自模型分布，而是“模型样本 + 后处理投影”。后处理本身有价值，但训练时没有显式建模该投影造成的分布偏移，也没有产出修复原因。

#### 8. 损失实现与论文 NLL 表述不完全一致

- **[代码事实]** 对 softmax 后的节点和边概率使用逐维 BCE。
- **[影响判断]** 多类角色通常应使用 categorical cross-entropy/NLL；逐维 BCE 会对所有非目标类重复惩罚。迁移时应明确目标分布，而不是沿用当前实现。

#### 9. 定义但未使用的模块

- **[代码事实]** `task_node_attention` 被构造但没有进入 `forward()` 或 `sample()`。
- **[影响判断]** checkpoint 中可能包含无效参数；应在迁移时删除或真正接入并做消融。

#### 10. 小批采样会丢弃余数

- **[代码事实]** 外层循环为 `range(num_samples // batch_size)`。
- **[影响判断]** 当样本数不是 batch size 的整数倍时余数直接丢失；小于一个 batch 时返回空结果。

### 7.2 运行时与实验问题

#### 1. 论文三轮、代码默认一轮

所有 cold-start、finetune、evaluate 脚本的 `num_rounds` 默认值均为 1；论文设置为 3。TestGraph 又没有建立跨轮 temporal edges，因此这不仅是默认参数不同，也是运行语义不完整。

#### 2. AQuA 流水线静态上不可运行

可直接核对到以下问题：

- 导入不存在的 `experiments.cold_start`；
- 导入不存在的 `SimpleAR.*`；
- adapter 导入不存在的 `experiment.aqua_prompt_set_adapter`；
- 使用未定义的 `SimpleARModel`；
- 使用未定义的 `count`；
- 读取未在 CLI 中定义的参数。

因此 README 对“其它数据集”的概括不能替代可执行证据。

#### 3. GSM8K 微调脚本读取未定义参数

`finetune_gsm8k.py` 使用 `args.ablation`，但其参数解析器没有定义该字段，会在正常主路径上触发 `AttributeError`。

#### 4. SVAMP / MultiArith 实验链缺失

`process_dataset.py` 预留了这两个 adapter 的导入，但仓库不存在对应目录和模块。数据 loader 存在不代表训练与评测实现存在。

#### 5. 绝对路径和 cwd 依赖严重

- 多个 finetune 脚本写死 `/root/ARG-Designer/...`；
- MMLU loader 写死 `/root/GDesigner/...`；
- 多处使用无包前缀导入和 `./task_split_*.json`；
- README 命令依赖先切换到特定子目录。

这使复现结果依赖 Linux 用户目录与当前工作目录，无法视为可移植实验包。

#### 6. 成本统计没有接通

`cost_count` 虽被导入，但没有在 LLM 响应路径调用；API usage 也没有被累计。实验进度条和最终报告中的 token/cost 因而会保持零值，不能用来复核论文 token 结论。

#### 7. LLM 配置没有真正贯穿

`GPTChat.agen()` 计算温度、最大 token、completion 数等默认值，但调用 `achat()` 时只传模型名和 messages；同步 `gen()` 为空实现。`template.env` 中的变量也没有被该模块读取，而源码内 `MINE_BASE_URL` 与 `MINE_API_KEY` 是空字符串。

#### 8. 数据元信息不一致

不同数据集保存 pruning/replay 样本时写入的 `is_correct`、question、mode 等元信息不统一。GSM8K 与 AQuA 的部分剪枝保存路径缺少这些字段，使跨阶段筛选和审计不可靠。

#### 9. 提示词存在重复与漂移

仓库同时有 `experiment/prompt/**`、数据集局部 prompt 和 `mas_framework/prompt/**`。大部分是复制文件，但 MMLU 两份通用 prompt 不同，角色数量和内容也并非完全一致。训练角色描述与执行期 Agent 约束缺少单一权威来源。

### 7.3 工程与安全问题

1. **没有锁定依赖。** 当前环境缺少 `torch`、`torch_geometric`、`sentence_transformers`、`openai`、`shortuuid`、`class_registry` 等依赖，而仓库没有版本清单。
2. **没有测试。** 81 个 Python 文件虽然均可被 AST 解析，但没有单元测试、集成测试或论文结果复现脚本。
3. **没有 checkpoint 和冷启动图产物。** 无法在不重新付费生成数据和训练模型的前提下验证论文数字。
4. **包含 33 个被 Git 跟踪的 `.pyc`。** 这增加平台污染和审查噪声。
5. **Python 执行器不安全。** HumanEval 和数学工具直接在宿主进程调用 `exec/eval`，共享 `globals()`；线程超时只停止等待，不能可靠杀死正在运行的恶意或死循环代码。
6. **MMLU 下载使用未校验的 `tar.extractall()`。** 若归档不可信，存在路径穿越风险。
7. **外围工具并非主方法证据。** Reader、Search、VGen、Web 等多为 legacy/copied 代码，部分导入本身不成立，也没有接入 ARG 的核心训练或评测链。

## 8. 项目的真实优势

### 8.1 联合学习角色与通信结构

**[分析判断]** 许多拓扑优化只改变边，却默认 Agent 集合不变。ARG 同时生成角色序列与边，使“是否需要某类能力”和“该能力应该接收谁的信息”成为同一个决策过程。这是它最重要的研究贡献。

### 8.2 任务条件化而非全局固定图

任务嵌入直接进入节点选择和边选择，消融结果也表明任务条件是主要收益来源。对于问题分布跨度较大的系统，一张全局最优图通常不存在，任务/阶段条件化比固定拓扑更合理。

### 8.3 生成策略与执行器解耦

ARG 输出 NetworkX DAG，再由 TestGraph 执行。尽管当前接口粗糙，这个分层思想是正确的：拓扑策略不应绑定某个 LLM Agent 实现。

### 8.4 正确性到效率的课程思想

先从高成功率的复杂团队学习“怎样协作能答对”，再引入简单图、稀疏图和 replay 学习“怎样少花资源仍然答对”，比一开始同时优化准确率和成本更稳定。这个思想可扩展为更严格的多目标课程。

### 8.5 语义角色空间具有扩展潜力

使用角色描述嵌入而不是固定分类头，理论上可以支持新角色冷启动，也能让相似角色共享统计强度。当前代码接口未完成，但方法方向仍然有价值。

## 9. 适用边界与长程任务局限

### 9.1 它生成的是一次性静态 DAG

**[分析判断]** ARG 根据输入问题一次生成团队，然后整条任务按该图运行。它没有在同一个长程 run 中根据进度、失败、资源变化或能力发现持续新增、删除、替换节点和边。因此它可以支持“任务开始/阶段切换时设计团队”，不能直接充当长程动态拓扑管理器。

### 9.2 角色是提示词标签，不是可验证能力

角色池由自然语言描述和 Agent class 名称组成，没有 capability contract、工具可用性、权限域、模型支持、资源位置、并发上限或健康状态。生成 `Programmer`、`Lawyer` 或 `Economist` 并不证明当前运行环境真的有相应可执行能力。

### 9.3 没有异构资源与放置决策

论文主要在同一种 GPT-4o 模型上改变角色提示和拓扑。模型没有考虑 local/edge/cloud、不同模型、成本、延迟、隐私、数据驻留、GPU/CPU、故障域和网络状况，因此它解决的是逻辑协作图，不是异构执行 placement。

### 9.4 没有长期状态与恢复语义

没有 canonical event log、checkpoint identity、pending/committed topology write、幂等副作用 fence、exact resume、branch-local delta 或故障后的拓扑重规划。NetworkX 图是可变内存对象，不能直接成为长程系统的权威状态。

### 9.5 训练信号过于粗糙

当前样本主要按最终答案正确与否筛选，缺少：

- 每条边是否真正被消费；
- Agent 输出的边际贡献；
- 重复信息、冲突和无效通信；
- 延迟、成本、峰值上下文和工具调用预算；
- 故障恢复能力；
- 权限拒绝、资源不可用和模型降级下的表现。

### 9.6 自回归顺序会引入表示偏差

同构图可以有多个节点生成顺序。当前 BFS/拓扑序编码会使模型学习某个序列化表示，而不是纯图等价类。早期节点采样错误还会累积到后续角色和所有入边，存在 exposure bias。

## 10. 对 Zyra 第二阶段的迁移价值

### 10.1 建议定位

**[迁移建议]** 将 ARG-Designer 定位为：

> `Task/Phase Conditioned Topology Proposal Policy`

它只负责根据当前任务或阶段状态生成一个候选团队和候选通信边，不拥有最终 topology state，也不直接修改运行中的共享图。

建议接入关系为：

```text
任务/阶段状态 + capability registry snapshot + 资源/权限约束
  -> ARG 风格拓扑提案策略
  -> versioned TopologyProposal artifact
  -> 确定性约束验证与投影
  -> topology mutation transaction
  -> canonical event/checkpoint commit
  -> scheduler/worker runtime 执行
  -> 真实 trace、成本、失败和贡献反馈
  -> 训练/评测数据构造
```

这样可以保留 ARG 的学习能力，同时保证 Zyra 自有动态拓扑、事务、恢复和权限机制仍是唯一权威。

### 10.2 建议拆出的 Zyra-owned 模块

| 建议模块 | 责任 | 来源与改造方式 |
|---|---|---|
| `TopologyDesignRequest` | 固化 task、phase、已有拓扑、可用角色/能力、预算、故障状态 | Zyra 新协议，不沿用原脚本参数 |
| `RoleCapabilityEmbeddingIndex` | 对角色与真实 capability contract 建索引，支持版本化扩展 | 吸收角色语义矩阵思想，重写注册和版本兼容 |
| `AutoregressiveTopologyPolicy` | 生成角色和候选入边，显式 EOS、置信度和随机种子 | 裁剪 `ARGDesigner`，修正公式、损失和采样状态 |
| `TopologyConstraintProjector` | 验证 DAG、可达性、关键角色、权限、预算、故障域与最大扇入/扇出 | 原仓库仅有弱后处理，需由 Zyra 实现 |
| `TopologyProposalArtifact` | 保存候选图、角色版本、模型版本、seed、log-prob、修复记录 | 新增可回放、可比较的 artifact |
| `TopologyCommitAdapter` | 把通过验证的提案转换为 immutable delta 和 topology mutation event | 接入 Zyra canonical graph state |
| `TopologyExperienceBuilder` | 从真实 trace 计算成功、成本、延迟、通信贡献、恢复结果 | 扩展 `D_exp/D_eff` 思想，不沿用随机剪边脚本 |
| `TopologyPolicyEvaluator` | 和固定、随机、简单、剪枝、无任务条件等策略做 sealed 对照 | 重建可复现实验层 |

### 10.3 可以优先裁剪的源码思想

#### 高价值

- `experiment/model.py` 中节点 GRU、边 GRU和角色度量匹配的整体分解；
- 图到自回归训练张量的转换思路；
- 角色描述嵌入与任务嵌入；
- END 控制可变团队大小的思路；
- 复杂成功图 → 简单/稀疏/replay 图的课程框架；
- 生成策略与实际 Agent 执行解耦。

#### 需要大幅修正后才能吸收

- `Graph_to_Adj_Matrix`：保留 DAG 序列化思想，但要固定 canonical ordering、支持稳定 node id、记录序列化版本，并处理图同构问题；
- `ARGDesigner.sample()`：必须重写角色 id 回写、EOS、batch 余数、动态角色注册、约束修复和 artifact 输出；
- adapter：改为消费 Zyra trace/event/artifact，而不是扫描散落的 `.pt` 文件；
- pruning：改为基于真实边使用、反事实贡献或结构消融，而不是随机删边；
- 多轮执行：由 Zyra 的 task graph/runtime 管理，ARG 只在明确的 topology replan point 产生 delta。

#### 不建议直接迁移

- `mas_framework/graph/Graph` 和 `TestGraph` 作为第二阶段运行时；
- `GPTChat` 与价格统计封装；
- Python `exec/eval` 执行器；
- 硬编码到单数据集的 cold-start/finetune/evaluate 脚本；
- Reader、Search、VGen、Web 等与核心方法无直接关系的外围代码；
- 被跟踪的 `.pyc` 和路径相关配置。

### 10.4 面向长程任务必须新增的条件

原模型输入只有任务语义、图历史和角色语义。第二阶段至少应加入：

- 当前 phase、未完成目标和依赖；
- 当前 canonical topology snapshot 与允许的 mutation scope；
- capability 是否真实可用，以及版本、工具、模型和权限；
- local/edge/cloud 位置、延迟、价格、隐私和数据驻留；
- worker 健康、lease、并发、队列和故障域；
- 当前 token、时间、费用和上下文预算；
- 历史失败、验证结果、恢复次数和信息熵；
- 必须存在或不得共置的角色/能力约束。

模型输出也不能只有角色和边，而应包含：

- 稳定 role/capability id；
- 新增、删除、替换节点和边的 delta；
- 每项变更的置信度或 log-prob；
- 预期收益、成本和风险；
- 依赖的 registry snapshot/version；
- 触发回退的条件；
- policy、checkpoint、seed 和 proposal id。

### 10.5 训练目标应从正确率扩展为多目标

建议把第二阶段的学习信号拆为：

```text
utility
= task_success
- λ1 · token_cost
- λ2 · wall_clock_latency
- λ3 · monetary_cost
- λ4 · permission_or_privacy_risk
- λ5 · redundant_communication
- λ6 · recovery_cost
+ λ7 · verified_information_gain
+ λ8 · robustness_under_failure
```

课程可以分为：

1. 从成功的高冗余图学习基本功能；
2. 用真实 edge/node disable 实验学习最小充分结构；
3. 加入成本、延迟、权限和异构 placement；
4. 加入节点失效、模型降级、网络断连和需求变更；
5. 在长程 run 中按阶段评估重配置收益，避免频繁震荡。

**[迁移建议]** 不应让 LLM 直接替代确定性约束。ARG/LLM 可以提出拓扑，DAG、安全、权限、预算、事务和恢复规则必须由 Zyra-owned runtime 验证和执行。

## 11. 第二阶段最小验收要求

若未来吸收 ARG 方法，至少应通过以下行为验证：

1. **任务条件性**：语义不同的任务在控制其它变量后能产生统计上不同且合理的角色/边分布。
2. **角色扩展**：新增 capability 后无需修改分类头即可成为候选，但只有完成 registry 绑定和执行合同后才能被提交。
3. **终止正确性**：EOS 是可学习且可观测的，节点数边界、空图和最大图都有确定行为。
4. **图约束**：所有提案经过 DAG、可达性、关键能力、扇入/扇出、预算和权限校验；修复行为留下事件与 artifact。
5. **回放确定性**：给定 policy version、registry snapshot、checkpoint、seed 和输入，可以重放相同提案；随机采样结果可追溯。
6. **真实成本**：token、延迟、费用、工具调用与通信边均来自真实 trace，不能使用始终为零的占位计数。
7. **动态可达性**：该策略能从真实 task/phase replan 路径触发，而不是只在离线 notebook 或 fixture 中运行。
8. **断开即变化**：禁用策略后回退到确定性基线，真实 topology proposal 和任务行为会发生可验证变化。
9. **故障恢复**：节点失效、provider 不可用、权限拒绝或预算收紧后，能产生合法 delta，而不是重建一个未关联旧状态的新图。
10. **exact resume**：提案尚未提交、已提交但未执行、执行中故障三种状态均能从 checkpoint 恢复，不重复副作用。
11. **反事实贡献**：至少通过 edge/node disable 或有界消融证明稀疏化没有只依赖随机删除。
12. **基线完整**：与固定简单图、固定复杂图、随机图、随机剪枝、无任务条件、无历史条件等策略在相同预算下比较。
13. **干净环境**：Zyra 正式模块不依赖 `../ARG-Designer`、其绝对路径、散落 checkpoint 或当前工作目录。
14. **安全执行**：代码任务必须在可终止、资源受限的隔离 runtime 中验证，不能使用宿主进程 `exec/eval`。

## 12. 最终取舍

| 项目要素 | 价值 | 建议 |
|---|---|---|
| 条件自回归角色—边联合生成 | 高 | 作为 topology proposal policy 的方法核心吸收 |
| 角色描述度量空间 | 高 | 与真实 capability registry 结合后重做 |
| 正确性→效率课程 | 高 | 扩展为真实 trace、多目标与故障课程 |
| 图序列化与有限边窗口 | 中 | 固定 canonical ordering、显式窗口语义后吸收 |
| NetworkX 生成图 | 中 | 只作临时提案表示，不作 canonical state |
| 当前训练/采样实现 | 中低 | 选择性迁移并修正关键语义，不能原样使用 |
| cold-start/finetune 脚本 | 低 | 仅参考流程，按 Zyra 数据协议重写 |
| TestGraph / legacy Graph | 低 | 不作为第二阶段 runtime 来源 |
| LLM wrapper、成本统计、代码执行器 | 低且有风险 | 不迁移 |

最终判断是：**ARG-Designer 是一个专精且值得吸收的“团队拓扑生成算法来源”，但不是可直接产品化的长程多 Agent 系统。** 对 Zyra 第二阶段最有价值的做法，是保留它对任务条件、角色语义、生成顺序和效率课程的建模思想，把核心网络裁剪到 Zyra-owned 模块；同时由 Zyra 接管 capability、权限、资源、异构调度、canonical topology event、checkpoint、故障恢复和真实成本反馈。

如果只是把当前仓库的 `ARGDesigner + TestGraph` 作为 sidecar 启动，得到的仍是一张不可审计、缺乏状态恢复且没有真实资源约束的一次性 DAG，这不构成长程动态拓扑内化。只有当它的输出被降格为版本化提案，并通过 Zyra 的确定性验证、事务提交、运行反馈和 exact-resume 闭环时，才会成为第二阶段的有效能力。

## 13. 关键证据索引

### 13.1 本地源码

- `ARG-Designer/README.md`：项目定位、AAAI 2026 oral 声明、HumanEval/GSM8K/MMLU quick start。
- `ARG-Designer/experiment/model.py`：核心 ARG 网络、门控、角色矩阵、损失、采样和图恢复。
- `ARG-Designer/experiment/process_dataset.py`：各数据集统一的 `max_prev_node=3` 与缺失 adapter 入口。
- `ARG-Designer/experiment/utils.py`：角色嵌入加载和数据集模块映射。
- `ARG-Designer/experiment/*/*_adapter.py`：成功图读取、训练样本转换和随机剪边。
- `ARG-Designer/experiment/*/cold_start_*.py`：训练任务切分、复杂/简单图生成和成功图保存。
- `ARG-Designer/experiment/*/finetune_*.py`：课程微调、replay、剪枝和路径/参数问题。
- `ARG-Designer/experiment/*/evaluate_*.py`：实际测试切分、图执行和指标计算。
- `ARG-Designer/mas_framework/graph/graph.py`：Graph/TestGraph、多轮、连接与 FinalDecision 执行。
- `ARG-Designer/mas_framework/graph/node.py`：空间/时间前驱和节点内存。
- `ARG-Designer/mas_framework/llm/gpt_chat.py`、`price.py`：LLM 参数与未接通的成本统计。
- `ARG-Designer/mas_framework/tools/coding/python_executor.py`、`executor_utils.py`：宿主进程代码执行与线程超时。
- `ARG-Designer/datasets/MMLU/download.py`、`mmlu_dataset.py`：未校验归档解压与硬编码数据路径。
- `ARG-Designer/experiment/humaneval/task_split_humaneval.json`：16 个 cold-start、24 个 fine-tune、121 个 test 索引。

### 13.2 正式论文

- Li et al., *Assemble Your Crew: Automatic Multi-agent Communication Topology Design via Autoregressive Graph Generation*, AAAI 2026, DOI `10.1609/aaai.v40i28.39481`。
- AAAI 页码：23142–23150。
- 论文中的方法公式、Algorithm 1、Table 1–3、Figure 3–5 是本记录中论文方法、主结果、消融、效率、鲁棒性和扩展性结论的依据。
