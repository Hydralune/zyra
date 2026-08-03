# CARD 项目深入阅读记录

> 项目：`G:\agent-zoo\long-horizon-systems\CARD`  
> 论文：*CARD: Towards Conditional Design of Multi-agent Topological Structures*  
> 论文状态：ICLR 2026，arXiv:2603.01089  
> 本地论文：`CARD/2603.01089v1.pdf`，arXiv v1，28 页，SHA-256 `F622752C9C1539660EE2234F1F71253CA8D2C0AB2E247136E78A7C465E2F78A1`  
> 阅读基线：本地 `main` 分支，commit `d5d1f682a459074c0225b78eca94ab1ea92476e7`（2026-03-28）  
> 记录日期：2026-07-17

本文使用以下标签区分证据性质：

- `[论文事实]`：论文正文、附录或算法中明确陈述的内容。
- `[代码事实]`：当前本地仓库的源码、配置、数据或版本状态直接呈现的内容。
- `[分析判断]`：基于论文—代码映射作出的解释、边界判断或风险判断。
- `[迁移建议]`：面向 Zyra 第二阶段的吸收、改造或拒绝建议。

## 1. 项目定位与先给结论

### 1.1 CARD 真正解决的问题

`[论文事实]` CARD 的专精方向不是一般意义上的多 Agent 框架，也不是完整的长程任务运行时，而是：**在 Agent 集合、模型能力、工具和知识源发生变化时，根据任务与环境条件生成新的 Agent 通信拓扑**。

论文先提出 AMACP（Adaptive Multi-Agent Communication Protocol），要求通信图同时满足：

1. 有效性：能解决当前查询；
2. 成本效率：减少模型、API、Token 和通信边使用；
3. 适应性：环境条件改变时，拓扑随之改变。

CARD 是 AMACP 的一个学习式实例：把 Agent 的静态 profile 与动态 condition 分开编码，再预测通信边。

### 1.2 对本项目的总体判断

`[分析判断]` CARD 最有价值的贡献是把“拓扑应该随环境变化”从口头原则推进为一个可训练策略：

```text
任务 + Agent 能力画像 + 当前资源条件
                 ↓
       条件化通信图生成器
                 ↓
       成本受约束的通信拓扑
```

它提供了一个很适合长程系统的思想：**拓扑不能只由任务类型决定，还应受当前模型质量、工具可用性、检索质量、成本和故障状态约束**。这比固定 DAG、固定角色链或只在 prompt 中描述环境更接近真实系统。

但当前公开实现是研究原型，论文和代码之间存在明显距离。源码实际实现更接近：

```text
角色描述 + 查询描述 ──静态 GCN──┐
                                 ├─ 固定 MLP ─ 相似度矩阵 ─ Bernoulli 采样 ─ DAG 过滤
模型描述 + 工具描述 ──动态 GCN──┘
```

它没有完整实现常规意义上的条件变分图生成器，也没有真实运行时遥测、持久拓扑状态、拓扑变更事务、回滚、稳定性控制或长程恢复。

### 1.3 面向 Zyra 的一句话裁决

`[迁移建议]` 应把 CARD 定位为 **Condition-Aware Topology Proposal Policy（条件感知拓扑提案策略）**，而不是 Zyra 的执行运行时、调度器、通信图状态 owner 或拓扑提交器。

- 值得吸收：profile/condition 双通道、任务条件化边评分、成本正则、环境切换评测、局部条件更新、拓扑消融方法。
- 需要重写：方向性边解码、约束投影、真实环境快照、训练目标、在线更新协议、拓扑事件与 checkpoint 接入。
- 不建议迁移：现有 LLM wrapper、搜索/文件工具、宿主机代码执行器、静态模型描述字典和实验脚本主控逻辑。

## 2. 阅读范围与仓库状态

### 2.1 已阅读材料

`[代码事实]` 本次完整读取和核对了：

- 任务文档 `project-analysis-notes/深入阅读任务.md`；
- 本地 `2603.01089v1.pdf` 的 ICLR/arXiv 论文 28 页，包括正文、限制、分析、算法、prompt 和配置附录；
- `README.md`、`requirements.txt`、`template.env`、`.gitignore`；
- 仓库全部 64 个 Python 文件，共 6,361 行；
- `CARD/config/**` 下全部 21 个 JSON 配置；
- `experiments/**` 下全部训练、评测和运行入口；
- GSM8K 1,319 条 JSONL 与 HumanEval 衍生数据 161 条 JSONL；
- Git 版本、跟踪文件、测试、权重和结果材料状态。

静态检查结果：

- 64/64 个 Python 文件可被 AST 解析；
- 21/21 个配置 JSON 可解析；
- 仓库没有正式测试目录或测试文件；
- 仓库没有提交模型 checkpoint、论文结果文件或 LICENSE 文件；
- 源码与配置保持未修改；当前 Git 状态只有本地论文 `2603.01089v1.pdf` 为未跟踪文件，没有 tracked source diff。

### 2.2 版本信息

`[代码事实]`

- 远端：`https://github.com/Warma10032/CARD.git`
- 分支：`main`
- 当前 commit：`d5d1f682a459074c0225b78eca94ab1ea92476e7`
- 历史只有两个提交：2026-03-01 的 `first commit` 和 2026-03-28 的 README 更新。

`[分析判断]` 当前仓库基本可以视为论文发布时的一次性代码快照，而不是持续维护、版本化发布的工程项目。

### 2.3 论文来源

- arXiv 摘要页：<https://arxiv.org/abs/2603.01089>
- arXiv PDF：<https://arxiv.org/pdf/2603.01089>
- OpenReview PDF：<https://openreview.net/pdf?id=JgvJdICc6P>
- GitHub：<https://github.com/Warma10032/CARD>

## 3. 研究问题与核心贡献

### 3.1 研究问题

`[论文事实]` 论文认为已有多 Agent 拓扑设计主要有两类：

- 人工固定流程，例如预定义链、树、辩论或软件工作流；
- 自动优化但环境静态的图，例如通过文本梯度、可微边参数或启发式搜索学出的固定拓扑。

这些方法在模型升级、工具变化、API 可靠性改变或知识源质量波动时容易失效。CARD 的目标不是为每个任务重新手写图，而是学习：

\[
G_{com}=f(Q,P,C)
\]

其中 `Q` 是任务，`P` 是 Agent profile，`C` 是环境条件。

### 3.2 AMACP 的三个目标

`[论文事实]` AMACP 将目标写为：

\[
\min_{G\in\mathcal G}\mathcal L_{AMACP}(G;Q,C)
=-u(G(Q\mid C))+\beta w(G;C)
\]

- `u`：任务效用；
- `w`：给定条件下的通信成本；
- `β`：准确性与成本之间的权衡。

这一定义的意义在于：同一任务在不同环境下未必应使用同一张图。例如弱模型可能需要更密集协作，强模型则可能减少冗余通信。

### 3.3 论文声称的主要贡献

`[论文事实]`

1. 提出 AMACP，明确自适应多 Agent 通信协议的有效性、成本和适应性目标；
2. 提出 CARD，用 profile 与 condition 双通道生成通信拓扑；
3. 在模型、工具、知识源变化和节点受攻击的模拟环境中验证适应性；
4. 通过拓扑矩阵、条件消融、Agent 数量、攻击和成本分析解释条件信号的作用。

`[分析判断]` 其中最重要的研究增量是第 2 项：把环境条件注入“图生成器”而不是只附加到 Agent prompt。论文的消融也专门证明，prompt-level condition 有时会造成负收益，而结构层条件化更稳定。

## 4. 论文方法深入拆解

### 4.1 Agent 表示

`[论文事实]` 每个 Agent `v_i` 包含两类表示：

\[
P_i=[T_p(Base_i), Role_i, T_p(Plugin_i)]
\]

\[
C_i=T_c(Condition_i)
\]

- `P_i` 描述基础模型、角色、工具等相对静态属性；
- `C_i` 描述可用性、Token 成本、API 可靠性等运行时状态；
- 离散属性被转成自然语言，再由统一预训练语言模型嵌入到共享空间。

论文还把条件写为多特征笛卡尔积：

\[
C=F_1\times F_2\times\cdots\times F_k
\]

其动机是让模型对训练时未见过的特征组合产生一定组合泛化。

### 4.2 条件图生成

`[论文事实]` CARD 从 anchor topology `A` 出发，分别用 profile encoder 与 condition encoder 得到：

\[
H^p=\phi_p(X^p,A;\Theta_p)
\]

\[
H^c=\phi_c(X^c,A;\Theta_c)
\]

查询被视为一个与所有 Agent 相连的辅助节点。decoder 对任意有向节点对预测：

\[
S_{ij}=\psi(h_i^p,h_i^c,h_j^p,h_j^c,h_Q;\Theta_d)
\]

再以阈值 `τ` 得到执行图：

\[
E_{com}=\{(v_i,v_j)\mid S_{ij}>\tau\}
\]

`[分析判断]` 这个设计在概念上将三件事分离得很好：

- Agent 是谁、能做什么；
- 当前环境怎样；
- 当前任务需要怎样通信。

这三者不应被混成一个固定 prompt 或一个静态 role graph。

### 4.3 多轮通信

`[论文事实]` 通信图是有向图，并通过拓扑排序生成执行次序。每轮 `t`，Agent 接收任务、系统 prompt 和本轮上游 Agent 的输出；经过 `K` 轮后，由投票、选择或总结器汇总最终答案。

通信复杂度的主要外部成本与 `K|E_com|` 成正比，而全对边评分本身至少需要 `O(N²)` 的 pairwise decoder 计算。

### 4.4 环境感知训练

`[论文事实]` 论文的 CARD loss 为：

\[
\mathcal L_{CARD}
=-u(\alpha^{(K)})+\beta w(G_{com};C)
\]

软图上的成本项为：

\[
w(\tilde G_{com},C)=\sum_{(i,j)}Cost_{ij}p_{ij}
\]

其中 `p_ij=S_ij`，`Cost_ij` 是该边预期产生的 Token/推理成本。

`[分析判断]` 论文没有充分说明离散 LLM 输出形成的任务效用如何对离散边采样求梯度。正文写成“通过 `S` 反向传播”，但没有给出 REINFORCE、Gumbel-Softmax、straight-through estimator、baseline 或方差控制细节。公开代码采用了 REINFORCE 风格的 `-log_prob * utility`，这应被视为实际训练机制，而不是论文公式中显然可直接微分的一步。

### 4.5 运行时适应

`[论文事实]` 部署时更新 condition embedding 后，一次前向即可重新解码拓扑，不要求重新训练：

\[
G^{new}_{com}=\psi(\phi_p(X^p),\phi_c(X_c^{new}),A)
\]

`[分析判断]` 论文所称“runtime adaptation”是**基于新条件重新生成边**，并不包含真实系统里的条件采集、异常确认、拓扑事务、正在执行任务的安全切换、回滚或防抖。这些仍需宿主系统负责。

## 5. 公开代码的真实实现

### 5.1 核心目录

| 路径 | 实际职责 |
|---|---|
| `CARD/graph/graph.py` | 条件特征构建、GCN 前向、边采样、DAG 过滤、多轮执行和最终聚合 |
| `CARD/graph/node.py` | 节点连接、同轮上游消息、上一轮 temporal memory、同步/异步执行接口 |
| `CARD/gnn/gcn.py` | 两层 GCN、两层 MLP 和可学习双特征融合 |
| `CARD/dynamic/llm_information.py` | 手写模型能力与成本描述 |
| `CARD/dynamic/search.py` / `rag.py` | 手写工具和知识源描述 |
| `CARD/agents/**` | 数学、代码、分析、对抗和最终决策 Agent |
| `CARD/prompt/**` | MMLU、GSM8K、HumanEval 角色描述与聚合 prompt |
| `experiments/**` | 数据装载、策略梯度训练、评测、权重保存和结果记录 |
| `CARD/config/**` | 模型/角色/搜索工具组合的预枚举环境配置 |

### 5.2 特征链路

`[代码事实]` `Graph.prepare_feature_cache_for_all_combinations()` 会在初始化阶段遍历 JSON 中的每一个组合并缓存三类特征：

1. `features`：角色描述的 all-MiniLM-L6-v2 embedding；
2. `llm_feature`：`Dyllm` 手写模型描述的 embedding；
3. `external_feature`：工具模式与知识源描述的 embedding。

`SentenceTransformer("all-MiniLM-L6-v2")` 的输出为 384 维。

任务执行时：

```text
role_embedding(384) + repeated_query_embedding(384)
                        ↓ concat
                    GCN 768→16→384

llm_embedding(384) + external_embedding(384)
                        ↓ αx+βy
                    GCN 384→16→384

两路输出 concat(768) → MLP 768→16→16 → Z·Zᵀ
```

`[代码事实]` 查询并没有作为一个真正的虚拟图节点加入 anchor graph，而是复制到每个 Agent 后与角色 embedding 拼接。

`[代码事实]` profile 中没有历史状态；condition 中也没有在线采集的可用性、延迟、错误率或实际 Token 价格。当前“动态条件”主要来自预写 JSON 组合和静态自然语言描述。

### 5.3 Anchor graph 与可执行边

`[代码事实]`

- GCN 的 `edge_index` 来自各 prompt set 的 `ROLE_CONNECTION`，即手写角色先验图；
- CLI 的 FullConnected、Chain、Star 等模式主要生成 `spatial_masks`，限制哪些候选边可被执行；
- encoder anchor 与执行候选 mask 是两套不同结构；
- `init_spatial_logit` 被计算，但原始可训练 `spatial_logits` 参数已被注释；真正的 spatial logits 每次由 GCN/MLP 前向产生。

### 5.4 边生成与 DAG 化

`[代码事实]` 边打分为：

```python
logits = self.mlp(torch.cat([logits_static, logits_dynamic], dim=1))
self.spatial_logits = logits @ logits.t()
self.spatial_logits = min_max_norm(torch.flatten(self.spatial_logits))
```

随后 `construct_spatial_connection()` 对候选边执行 Bernoulli 采样，并用递归 `check_cycle()` 阻止成环。

这里有四个非常关键的实现性质：

1. `Z·Zᵀ` 必然是对称矩阵，因此原始 `S_ij=S_ji`；代码不能直接学习有向边的不同方向概率。
2. 有向性主要由候选边遍历顺序和 cycle filter 产生，而不是 decoder 学出。
3. min-max 后 logits 被限制在 `[-1,1]`，再经过 sigmoid，采样概率只能落在约 `[0.269,0.731]`；无法形成接近 0/1 的确定边。
4. 论文定义的阈值 `τ` 在主 `arun()` 路径中没有传入；默认一直随机采样。

`[分析判断]` 这意味着公开实现的核心更准确地说是“条件化无向相似度 + 顺序敏感 DAG 投影”，并不等价于论文公式中的任意有向 pairwise decoder。

### 5.5 多轮执行链

`[代码事实]` `Graph.arun()` 的主链为：

1. 随机选择一个预配置组合，或由 `fixed_group` 选择一个组合；
2. 重建该组合的 Agent 节点；
3. 计算条件边 logits；
4. 每轮重新采样 spatial graph；
5. 按零入度队列顺序逐个 `await` 节点；
6. 将输出保存为下一轮 temporal memory；
7. 最后一轮后把全部 Agent 接到 decision node 并聚合。

`[代码事实]` 批次中的不同问题通过 `asyncio.gather()` 并发，但单张图中同时零入度的 Agent 并未并发执行，而是逐个等待。

`[代码事实]` temporal edge 是另一套参数，但实验优化器没有包含 `graph.temporal_logits`。即使开启 `--optimized_temporal`，该参数也不会被训练，只会保持初始化概率并随机采样。

### 5.6 训练机制

`[代码事实]` MMLU 的单样本准确性损失为：

```text
accuracy_loss = -log_prob * binary_correctness
```

成本项为：

```text
0.01 × hard_coded_group_price × Σ sigmoid(edge_logit) × mask
```

其中 group price 是 `{40,20,35,60,30}`，不是根据实际 Token、请求模型和输入输出长度计算的 `Cost_ij`。

`[代码事实]` 训练没有负奖励、baseline、优势归一化或熵项。答错样本的 accuracy reward 为 0，因此仅成本项可能提供梯度；答对样本才强化采样路径。

`[代码事实]` MMLU 与 HumanEval 的优化器包含静态 GCN、动态 GCN 和 feature fusion，但不包含 `graph.mlp`。MMLU 会保存这个未训练 MLP；HumanEval 甚至不保存 MLP，评测时会重新随机初始化。

### 5.7 固定图模式

`[代码事实]` 当 `optimized_spatial=False` 时，执行路径直接按 `spatial_masks` 建边，忽略 GNN 打分。换言之，只有显式传入 `--optimized_spatial`，CARD 学出的条件拓扑才进入执行主路径。README 示例包含该参数，但代码默认值是关闭。

## 6. 论文实验与结论

### 6.1 任务、模型与基线

`[论文事实]` 论文评测三类任务：

- HumanEval：代码生成；
- MATH：数学推理；
- MMLU：多领域知识与推理。

基线包括 Vanilla、CoT、Random Graph、LLM-Debate、GPTSwarm、AFlow 和 G-Designer。

主要模型为：

- 训练及域内测试：GPT-4o-mini、DeepSeek-V3、Llama3-70B；
- 仅域外测试：GPT-4o、Qwen-72B。

附录还分析 Qwen 7B/14B/72B、推理蒸馏模型、Google/DuckDuckGo/WikiSearch 和 Wikipedia/Tutorialspoint/Quora 等条件。

### 6.2 主结果

`[论文事实]` Table 1 的平均准确率如下：

| 方法 | HumanEval | MATH | MMLU |
|---|---:|---:|---:|
| Vanilla | 85.50 | 63.33 | 81.04 |
| CoT | 87.66 | 64.33 | 84.18 |
| Random Graph | 86.00 | 64.67 | 83.27 |
| LLM-Debate | 86.00 | 66.83 | 83.92 |
| GPTSwarm | 86.66 | 70.16 | 84.05 |
| AFlow | 89.83 | 73.83 | 82.87 |
| G-Designer | 86.50 | 72.66 | 84.44 |
| **CARD** | **90.50** | **74.50** | **86.67** |

CARD 在 15 个模型—任务组合中有 13 个取得或并列最高结果。

`[论文事实]` CARD 在域外模型上的收益更明显。例如 MATH 从 DeepSeek-V3 切换到 Qwen-72B 时，G-Designer 从 91.66 降到 79.16，CARD 从 91.66 降到 82.50。

### 6.3 条件注入消融

`[论文事实]` 论文比较：

- `w/o Cond.`：无条件图生成；
- `w/ Cond.p`：只把条件追加到 prompt；
- CARD：条件进入图生成模块。

prompt conditioning 在部分组合上会下降，最高出现 MATH `-12.50` 个百分点；CARD 在报告的所有模型—任务对上均为非负增益。

`[分析判断]` 这是论文最有说服力的一组证据：条件不是给 Agent “看见”就够了，条件需要改变系统级通信结构。不过该结果仍来自模拟、预枚举条件，不等同于真实线上遥测闭环。

### 6.4 拓扑解释

`[论文事实]`

- GPT-4o-mini + Google 的边权整体高于 GPT-4o + Google，论文解释为弱模型需要更密集协作；
- GPT-4o-mini 从 Google 换到 Wiki 时，整体矩阵相关性 `r=0.9797, p=0.0006`，但局部知识流变化；
- Llama3-70B + Wiki 产生更密图，表示模型和检索同时较弱时更依赖协作。

### 6.5 规模、攻击与成本

`[论文事实]`

- Agent 数从 5 扩到 10 时，CARD 在 MMLU 上持续优于 G-Designer，域外最大增益约 `+1.99` 个百分点；
- 节点受攻击时，HumanEval 上 CARD 87.50、G-Designer 86.66；恢复节点后 CARD 回升 `+3.33` 个百分点；
- 只更新单个节点条件时，头节点扰动后为 88.33，中间节点扰动后为 85.00；
- 论文报告 CARD 能以约 `4×10^-3` 美元/样本达到约 94% 准确率，位于更优的成本—性能区域。

### 6.6 论文主动承认的限制

`[论文事实]`

- 只调整通信拓扑，不动态更新 prompt、角色 profile 或工具选择策略；
- 图抽象可能无法表示软件工程中的程序约束、工具依赖和执行语义；
- 更真实的系统需要符号先验、人类经验或混合适应机制；
- 未来才考虑更大 Agent 集合、在线强化学习和真实应用验证。

## 7. 论文—代码对应关系

| 论文机制 | 公开代码 | 对应判断 |
|---|---|---|
| profile/condition 双通道 | 角色+查询 GCN；模型+工具描述 GCN | 部分实现 |
| 查询作为辅助图节点 | 查询 embedding 复制并拼接到每个节点 | 近似实现，不是虚拟节点 |
| 可配置 anchor `A` | `ROLE_CONNECTION` 作为 GCN anchor；CLI mode 作为 mask | 分裂为两套结构 |
| 有向 pairwise decoder `S_ij` | `Z·Zᵀ` | 不等价；原始得分对称 |
| 阈值 `τ` 形成图 | 主路径 Bernoulli 采样，未传 threshold | 不一致 |
| 条件变分图编码器 | 两个确定性 GCN + 随机边采样 | 未见标准 VAE 机制 |
| `Cost_ij p_ij` 条件成本 | MMLU 中硬编码 group price × 期望边数 | 部分、粗粒度实现 |
| 新条件下无需重训 | 切换预缓存 `fixed_group` 或随机组合 | 仅支持预枚举组合 |
| 局部节点条件更新 | 无独立在线 API；需重建/重选配置 | 论文能力未产品化 |
| 多轮通信 | spatial 同轮消息 + temporal 上轮消息 | 已实现，但 temporal 不受条件模型训练 |
| 域外组合泛化 | 配置包含多组模型 | 实验入口存在训练/测试泄漏风险 |

### 7.1 “变分”实现缺口

`[代码事实]` 全仓没有 `μ/logσ`、重参数化、KL divergence、prior/posterior 或 ELBO 相关实现。

`[分析判断]` 当前代码不能按常规 Conditional VAE 来理解。它的随机性来自 Bernoulli 边采样，训练通过路径 log-prob 传递。论文正文同样没有展开 KL 或后验设计，因此“conditional variational graph encoder”在论文与代码层面都缺乏可复现的标准变分定义。

### 7.2 方向性缺口

`[代码事实]` 论文把 `(v_i,v_j)` 定义为有向通信边；代码 decoder 却使用对称内积。

`[分析判断]` 这是比普通工程 bug 更核心的方法边界。对于“Planner→Executor”和“Executor→Planner”这类语义完全不同的边，当前 decoder 无法直接赋予不同概率。Zyra 若吸收此方法，必须改成非对称 decoder，例如：

\[
S_{ij}=q_i^T W k_j+b_{ij}
\]

或分别学习 source/head 与 target/tail 表示。

### 7.3 环境信号缺口

`[代码事实]` `Dyllm`、`Dysearch` 和 `Dyrag` 都是手写文本字典。代码没有观测：

- 最近成功率与领域能力；
- 当前延迟、排队和限流；
- 工具健康、错误率和断连；
- 真实价格、剩余预算和上下文容量；
- 数据源新鲜度、可信度和检索质量；
- Agent 当前负载、lease、sandbox 或权限状态。

`[分析判断]` 因而当前实现是“配置条件化”，不是“运行状态条件化”。

## 8. 可复现性与工程断点

### 8.1 论文任务与仓库任务不一致

`[代码事实]`

- 论文主任务是 HumanEval、MATH、MMLU；
- 仓库没有 MATH 数据集或 MATH runner；
- 仓库提供的是 GSM8K 数据和 `run_gsm8k.py`；
- `CARD/config/math/**` 虽名为 math，但角色 prompt 与公开运行入口默认连接 GSM8K 1,319 条数据。

`[分析判断]` 当前仓库无法直接复现论文 Table 1 的 MATH 结果。

### 8.2 训练/域外配置口径不一致

`[论文事实]` 附录把 GPT-4o-mini、DeepSeek-V3、Llama3-70B 列为训练及测试配置，把 GPT-4o、Qwen-72B 列为只测试配置。

`[代码事实]`

- MMLU 默认 JSON 确实包含这五组；
- README 的训练命令使用 `--eval_group cycle`；
- `train_mmlu.py` 的 `cycle` 会在一个 batch 内循环 `model_group_1` 到 `model_group_5`，因此训练会访问论文声称仅测试的第 4、5 组；
- HumanEval 训练路径 `allow_random_combination=True`，会从所给 JSON 的所有组合随机抽样；
- HumanEval 默认根配置的模型组又与论文附录不一致，其中包含 GPT-3.5、GPT-4 和重复的 GPT-4 组合。

`[分析判断]` 公开训练入口不能直接支撑论文的 in-domain/out-domain 结论，至少需要重新划分可见配置并记录严格 split。

### 8.3 评测 checkpoint 断裂

`[代码事实]`

- `train_mmlu.py` 保存 `trained_gcn_beta0.01.pt`；
- `evaluate_mmlu.py` 加载 `trained_gcn_beta0.005.pt`；
- 两个路径不一致，仓库也没有任何权重文件；
- HumanEval 保存 GCN、dynamic GCN 和 feature fusion，但不保存 MLP；
- HumanEval eval 新建 Graph 后使用随机 MLP；
- GSM8K 没有完整 train/eval checkpoint 流程。

### 8.4 入口默认阻塞

`[代码事实]` `run_mmlu.py` 和 `run_humaneval.py` 在模块加载时监听本地 debug 端口并调用 `debugpy.wait_for_client()`。没有 debugger 连接时会一直等待，`try/except` 不会把正常等待转换为超时。

### 8.5 环境配置不能按 README 直接闭合

`[代码事实]`

- `template.env` 只提供 `BASE_URL` 和 `API_KEY`；
- `LLMRegistry` 还依赖未记录的 `SERVER`；
- 对多数模型，`SERVER` 缺失会导致 `MODEL_NAME_MAP.get(None).get(...)` 访问空值；
- `GPTChat.agen()` 接受 max tokens、temperature、completion 数，但底层请求没有转发这些参数；
- `GPTChat.gen()` 和 `TogetherChat.gen()` 都是空实现；
- Together 的异步路径内部调用同步 SDK，会阻塞事件循环。

### 8.6 依赖清单不可靠

`[代码事实]`

- `requirements.txt` 有 281 个严格 pin，包含大量与核心实验无关的 notebook、GUI、TensorFlow、Office、Gradio 和 Windows 依赖；
- 源码直接 import 的 `together`、`duckduckgo_search`、`baidusearch` 和 `googlesearch` 未列入 requirements；
- 没有 `pyproject.toml`、lockfile 或最小安装集合；
- README 宣称 Python 3.10+，但未提供经过验证的平台矩阵。

### 8.7 本地数据口径

`[代码事实]`

- GSM8K JSONL：1,319 条，即常见测试集规模；
- HumanEval 衍生 JSONL：161 条，编号覆盖 0–163，但缺少 32、38、50；
- HumanEval runner 使用 `int(len/batch_size)`，默认 batch size 4 时只执行前 160 条；
- 论文 HumanEval/MATH 结果大量以 0.8333 个百分点递增，显示报告口径更像 120 个样本，但公开 runner 没有对应的 120 样本 split；
- MMLU 数据不随仓库提供，运行时下载；eval 固定上限 153，与 MMLU val 聚合规模一致。

### 8.8 缺少测试与结果证据

`[代码事实]` 仓库没有：

- unit/integration tests；
- seed 管理与多次运行统计脚本；
- 论文表格生成脚本；
- topology matrix 导出主路径；
- 已训练 checkpoint；
- 论文结果 JSON/log；
- 环境锁定和 clean setup 验证。

论文写明“结果取多次运行平均且图表由脚本生成”，但这些完整材料未出现在当前公开仓库。

## 9. 具体代码质量与安全边界

### 9.1 图与 prompt 细节

`[代码事实]`

- HumanEval `ROLE_CONNECTION` 中存在 `Promgramming Expert` 拼写错误，导致对应角色先验边不能连接真实 `Programming Expert`；
- MMLU `get_analyze_constraint()` 的条件表达式使已知角色只收到角色描述，不会附加通用 A/B/C/D 输出约束；
- `mmlu_prompt_set_new.py` 同样注册 `mmlu`，但没有被 `CARD.prompt.__init__` 导入，默认主路径实际使用旧版；
- 同步 `Graph.run()` 在计算 `spatial_logits` 前就调用构图，而初始化中的 spatial parameter 已被注释，因此同步主路径不可用；
- `AnalyzeAgent` 和 `AdverarialAgent` 的输入处理是 async，但同步 `_execute()` 按同步函数调用，进一步确认同步链不闭合。

### 9.2 成本统计失真

`[代码事实]` 所有聊天 wrapper 都把 `cost_count()` 的模型名固定传为 `gpt-4`。这会把 Together、DeepSeek、Qwen 等模型按 GPT-4 tokenizer/价格计算。代码内的全局 Cost、PromptTokens、CompletionTokens 因而不能作为论文成本证据。

### 9.3 条件描述的可维护性

`[代码事实]` 模型能力、参数量、价格和适用性直接写在 `llm_information.py` 的自然语言中，没有来源、时间、版本、置信度或更新机制。

`[分析判断]` 这类文本可以作为研究 prompt fixture，但不能成为生产调度条件。模型和 API 状态变化快，静态描述容易产生错误拓扑，而且无法审计“为什么这条边在此时被添加”。

### 9.4 宿主机执行风险

`[代码事实]`

- `PyExecutor` 直接在宿主进程中 `exec/eval` 模型生成代码；
- 超时通过线程 join 实现，超时后线程不能被终止；
- `globals()` 在测试间共享，代码可以污染进程状态；
- `PythonReader` 会执行被读取的 Python 文件；
- `ZipReader` 和 MMLU downloader 直接 `extractall()`，没有路径穿越校验；
- 搜索工具在 async 函数内使用阻塞 `requests`，且缺少统一超时、域名策略和内容隔离。

`[迁移建议]` 这些工具都不应进入 Zyra 正式边界。代码评测必须放入隔离 worker/sandbox，并由权限、资源、网络、文件系统和进程生命周期策略控制。

## 10. 项目的真实优势

### 10.1 把环境变化提升为一等输入

`[分析判断]` CARD 最值得保留的是建模视角：拓扑策略的输入不应只有任务，还应包含当前执行环境。这对长程任务尤其重要，因为长时间运行中模型可用性、预算、工具健康和资源负载必然变化。

### 10.2 profile 与 condition 分离

`[分析判断]` 静态能力与瞬时状态分开编码，有利于：

- 缓存稳定 profile；
- 高频刷新 condition；
- 对环境变化做局部重算；
- 对同一 Agent 的“能力”和“当前是否适合调用”作不同判断。

### 10.3 拓扑级条件化优于 prompt 补丁

`[论文事实]` prompt-only condition 会在部分实验中退化，CARD 的结构级条件化更稳定。

`[分析判断]` 对 Zyra 而言，这支持一个重要架构原则：资源、故障、预算和权限不能只作为 LLM 文本提示；它们必须进入确定性约束与调度/拓扑决策层。

### 10.4 成本—效果联合目标

`[分析判断]` 即便当前代码的成本实现很粗，论文把预期通信成本直接放入图优化目标是正确方向。它比训练出准确图后再做独立剪枝更自然，也适合扩展到延迟、能耗、隐私风险和失败概率。

### 10.5 条件切换评测框架

`[分析判断]` 模型升级、工具更换、知识源退化、节点受攻击、局部条件恢复和域外组合，是很有价值的评测维度。这些场景可以转化为 Zyra 第二阶段的 topology-policy benchmark。

## 11. 对长程复杂任务的适用边界

### 11.1 有价值的部分

CARD 适合回答：

- 当前阶段应该让哪些已存在 Agent 互相通信？
- 弱模型、贵模型或不稳定工具出现时，通信密度如何改变？
- 某个检索节点退化后，哪些边应该绕开它？
- 在性能相近时，能否选择通信成本更低的图？

### 11.2 不能直接回答的部分

`[分析判断]` CARD 目前不能处理：

- 在同一 run 内增加、删除或替换 Agent/node/role/capability；
- 长程任务阶段转换与动态子任务生成；
- lease、并发冲突、不可变 state commit 和 branch merge；
- 工具副作用的幂等 fence；
- checkpoint、exact resume、故障恢复和拓扑回滚；
- 权限、隐私、数据驻留和隔离域约束；
- 节点正在执行时如何安全迁移拓扑；
- 持续观测下的防抖、迟滞和策略退化保护。

### 11.3 固定节点集合限制

`[论文事实]` CARD 优化的是给定 `V` 上的边。

`[分析判断]` 对真正长程任务，环境变化可能要求新建专家、关闭失败 worker、替换模型或改变角色，而不只是改变连接。CARD 可作为“固定候选池内的边策略”，但不能单独构成动态异构群体智能架构。

### 11.4 模拟条件与真实条件的差距

论文主要通过切换模型名、搜索引擎、知识源或攻击标签模拟环境变化。真实系统还需要处理观测噪声、信号延迟、错误归因和条件冲突。没有可靠 condition pipeline，再好的图生成器也会对错误状态做出自信决策。

## 12. 面向 Zyra 第二阶段的迁移设计

### 12.1 推荐角色：拓扑提案策略，不是状态 owner

`[迁移建议]` CARD 的输出应是不可变的 proposal，而不是直接修改运行图：

```text
EnvironmentSnapshot + Task/Phase Context + AgentCapabilityProfiles
                              ↓
                 ConditionAwareTopologyPolicy
                              ↓
                   TopologyProposalArtifact
                              ↓
              Deterministic Constraint Projector
                              ↓
           Zyra topology mutation transaction / event
```

策略层可以学习，提交层必须由 Zyra 的确定性状态机控制。

### 12.2 建议拆成的 Zyra-owned 模块

#### A. `AgentCapabilityProfile`

保存较稳定属性：

- role/capability；
- provider/model/version；
- tool contracts；
- sandbox 与数据域；
- 上下文容量、模态和领域能力；
- 来源与版本化 benchmark 证据。

不要继续使用无来源的手写宣传文本作为唯一事实。

#### B. `EnvironmentSnapshot`

保存带时间和置信度的动态条件：

- health、error rate、latency、queue depth；
- budget、实际单位价格、剩余 Token；
- tool availability、网络与数据源新鲜度；
- worker load、lease、故障与恢复状态；
- privacy/permission/sandbox 约束；
- observation timestamp、source、confidence。

#### C. `ConditionEncoder`

可以保留 CARD 的双通道思路，但输入应为结构化 schema，再按特征类型组合：

- 数值条件做归一化和时间衰减；
- 类别/文本能力做版本化 embedding；
- 不确定状态显式编码置信度；
- profile 和 condition 分别缓存、分别失效。

#### D. `DirectionalTopologyScorer`

替换 `Z·Zᵀ`，显式区分 source 与 target，并输出：

- edge score/probability；
- expected utility；
- expected token/latency/cost；
- reason codes 或主要特征贡献；
- policy version 与 calibration 信息。

#### E. `TopologyConstraintProjector`

在 learned proposal 后确定性执行：

- DAG/允许循环协议；
- 必达节点与最终聚合可达性；
- 最小/最大入度和出度；
- 权限、隐私与隔离边界；
- 成本、延迟和并发上限；
- 故障节点排除；
- 关键路径冗余与单点故障保护。

这一步不能交给随机 cycle filter。

#### F. `TopologyProposalArtifact`

至少记录：

- base topology version；
- environment snapshot id；
- add/remove/replace edge delta；
- 预期收益和成本；
- 约束通过/拒绝原因；
- policy/checkpoint/version；
- 过期时间、回滚目标和评测标签。

#### G. `TopologyCommitAdapter`

负责把 proposal 转成 Zyra canonical topology mutation event，处理并发、版本冲突、checkpoint 和回滚。策略模型不得直接持有 canonical graph。

#### H. `TopologyPolicyTrainer/Evaluator`

训练和评测应覆盖：

- 离线轨迹与反事实候选图；
- sealed condition splits，防止域外配置泄漏；
- utility/cost/latency/failure/privacy 多目标；
- static topology、prompt condition、rule policy 和 learned policy 对照；
- 稳定性、校准、切换频率和恢复时间。

### 12.3 可直接复用、裁剪复用与应重写内容

| 来源 | 建议 | 原因 |
|---|---|---|
| `CARD/gnn/gcn.py` | 只作原型参考，少量同语言重构 | 代码很小；`log_softmax`、融合和 decoder 均需调整 |
| `Graph` 的双通道前向思路 | 裁剪吸收 | 核心研究价值所在，但必须与执行运行时解耦 |
| `ROLE_CONNECTION` | 作为先验/fixture | 可用于初始化，不应成为唯一 anchor 或 canonical topology |
| JSON 环境组合 | 转成 benchmark scenario | 适合测试模型/工具切换，不适合生产状态 |
| MMLU 成本正则思路 | 重写实现 | 需接真实计量与 edge-specific cost |
| Bernoulli + REINFORCE | 有界实验选项 | 需 baseline、校准、方差控制和确定性评测模式 |
| `llm_information.py` | 不迁移数据 | 静态、无来源、无法实时更新 |
| `graph.py` 执行器 | 不直接迁移 | 图策略、Agent 执行、消息和状态混杂 |
| `llm/**` | 不迁移 | 配置不闭合、同步路径空、参数未转发、成本失真 |
| `tools/**` | 不迁移 | 与核心贡献无关，并存在阻塞、隔离和安全问题 |

### 12.4 为什么这里应选择“重构式内化”

`[迁移建议]` 本项目不适合整模块原样搬入生产路径，原因不是研究价值低，而是核心语义必须改造：

- decoder 方向性错误；
- condition 不是实时状态；
- graph state 没有持久化与事务；
- 训练/评测 split 不可靠；
- 执行器和策略耦合；
- 安全边界不满足生产要求。

合理路径是保留 PyTorch/GNN 的同语言实现能力，但按 Zyra schema、事件、状态、约束和评测边界重新组织，而不是套一个 adapter 调用原始 `Graph.arun()`。

## 13. 最小验证要求

如果第二阶段吸收 CARD 思路，至少应完成以下验证。

### 13.1 算法正确性

- 同一节点对的两个方向可以产生不同分数；
- 约束投影后图满足可达性、权限、预算和故障边界；
- 固定 seed/确定性模式可重放相同 proposal；
- 策略 checkpoint 与 profile/condition schema 有兼容版本；
- 对未见条件组合做真正 sealed evaluation。

### 13.2 语义效果

- 模型能力下降时，拓扑真实改变且任务效果/成本变化可测；
- 工具断连时，受影响边被移除并触发替代路径；
- 预算下降时，实际调用数和 Token 成本下降，而非只记录建议；
- 条件恢复后可安全回切；
- 禁用 topology policy 后，对照结果明显变化。

### 13.3 长程稳定性

- 条件快速抖动时不会频繁重构拓扑；
- topology mutation 能 checkpoint、resume 和 rollback；
- 正在执行的节点不会因图替换丢失消息或副作用状态；
- 2,000+ canonical transitions 中能记录 topology proposal、commit、reject 和 recovery 因果链。

### 13.4 评测矩阵

至少包含：

- fixed dense / fixed sparse；
- task-only learned topology；
- prompt-conditioned topology；
- rule-based condition policy；
- CARD-style learned condition policy；
- directional decoder 与对称 decoder 消融；
- 无成本项、真实成本项与多目标约束消融。

指标除准确率外还应包括：

- 有效边数、Token、真实费用、端到端延迟；
- topology switch count、恢复时间、失败率；
- 条件预测校准与 proposal 拒绝率；
- 域外组合、节点失败、工具退化和断连恢复。

## 14. 最终取舍

### 14.1 应保留

`[迁移建议]`

- AMACP 的 effectiveness/cost/adaptiveness 三目标；
- profile 与 condition 分离；
- 条件进入图策略而不是只进入 prompt；
- 环境更新后单次前向重算拓扑；
- 模型、工具、知识源、攻击和恢复条件的评测设计；
- 将预期通信成本放入训练目标。

### 14.2 必须修正

- 对称内积 decoder；
- `[-1,1]→sigmoid` 导致的窄概率区间；
- 随机采样 + 顺序 cycle filter 代替有向约束解码；
- 静态文本条件代替真实遥测；
- 预枚举组合代替在线 condition snapshot；
- 训练/测试环境泄漏；
- checkpoint、默认入口和依赖断裂；
- 无持久状态、无 topology transaction、无 rollback。

### 14.3 不应高估

`[分析判断]` CARD 证明了“条件化拓扑值得研究”，但当前证据还不能证明它已解决真实长程系统中的在线自适应通信。论文实验是小规模、预定义 Agent 集合和模拟条件；公开代码也没有生产级闭环。

### 14.4 最终结论

`[分析判断]` CARD 对 Zyra 第二阶段有较高的**算法与评测设计价值**，但现有源码的**直接生产复用价值中等偏低**。最佳吸收方式不是迁移整个框架，而是把其核心思想内化为 Zyra 的一个独立拓扑策略模块：读取结构化、可审计的环境快照，生成方向性 topology proposal，经确定性约束投影后再由 Zyra canonical runtime 提交。

## 15. 关键证据索引

### 15.1 论文

- 问题、AMACP 与贡献：论文第 1、3 节；
- profile/condition、encoder/decoder、loss 与 runtime adaptation：第 4 节，公式 (7)–(13)；
- 主结果与条件消融：第 5 节，Table 1、Figure 3、Figure 4；
- 限制：Appendix A；
- 工具、规模、攻击和成本：Appendix B；
- 模型与资源：Appendix C；
- 拓扑矩阵：Appendix D；
- 算法：Appendix E Algorithm 1；
- prompt 与配置：Appendix F、G。

### 15.2 源码

- 条件特征缓存：`CARD/graph/graph.py::prepare_feature_cache_for_all_combinations`
- 角色先验图：`CARD/graph/graph.py::construct_adj_matrix`
- 静态/动态特征：`construct_features`、`construct_dynamic_llm_features`、`construct_dynamic_externel_features`
- 边生成主链：`CARD/graph/graph.py::arun`
- Bernoulli 构图和 cycle filter：`construct_spatial_connection`、`check_cycle`
- GCN/MLP/fusion：`CARD/gnn/gcn.py`
- 条件文本：`CARD/dynamic/llm_information.py`、`CARD/dynamic/search.py`、`CARD/dynamic/rag.py`
- MMLU 策略梯度和成本项：`experiments/train_mmlu.py`
- MMLU checkpoint 断点：`experiments/train_mmlu.py`、`experiments/evaluate_mmlu.py`
- HumanEval 训练/评测：`experiments/run_humaneval.py`
- GSM8K 路径：`experiments/run_gsm8k.py`、`datasets/gsm8k_dataset.py`
- LLM 路由和请求：`CARD/llm/llm_registry.py`、`gpt_chat.py`、`together_chat.py`
- 代码执行风险：`CARD/tools/coding/python_executor.py`、`executor_utils.py`
- 数据下载/解包：`datasets/MMLU/download.py`、`CARD/tools/reader/readers.py`
