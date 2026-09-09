# ZYRA 论文实验复现说明

本目录保存论文验证所需的实验程序、原始回执和统计结果。证据分为两类，二者不能混用。

## 在线协作策略先导实验

`run_online_coordination_benchmark.ts` 对五种原生策略进行独立在线执行：单智能体、静态链式、静态星型、全量广播和 ZYRA。每个 Agent 调用都会产生新的模型请求与独立回执，不读取历史因果轨迹。任务要求从含有有效、过期和撤销记录的信息包中合成 12 项版本化要求，独立判分器只有在 12 项全部正确时才判定任务成功。

正式先导批次使用 5 个不同任务、每个任务 3 次重复，并分别执行无故障和成员响应丢失条件，共 150 个实验单元、645 次真实模型调用。原始结果位于 `evidence/online-pilot-20260907/results.json`，统计结果和图表位于同一目录。

复跑命令如下。执行前需要在仓库根目录配置 `.env.deepseek.local`，实验程序不会把凭据写入结果文件。

```powershell
node --env-file=.env.deepseek.local --experimental-strip-types paper\experiments\run_online_coordination_benchmark.ts --tasks 5 --repetitions 3 --conditions no_fault,agent_loss --output .tmp\online-pilot\results.json
python paper\experiments\analyze_online_coordination_benchmark.py .tmp\online-pilot\results.json --output-dir .tmp\online-pilot\analysis
```

这五种方法是为隔离协作策略而实现的原生实验适配器，不是 AutoGen、GPTSwarm、AgentPrune 或 G-Designer 的官方实现。该批次属于在线先导实验，不能替代外部框架复现、开放基准或真实行业试点。

## 受控因果轨迹实验

`evidence/trace-replay-10seed-20260907` 保存软件交付和跨源研究两个场景各 7 个变体、10 个种子的机制实验包，共 140 个实验单元和 4,480 条原始指标样本。执行器对同一封存事件档案应用不同路由、恢复和记忆策略，适合检查机制开关和结构后果。

该实验不是五个系统各自完成任务。质量、Token、成本和路由时延中有一部分由运行时公式推导，因此不得作为端到端性能优势或统计显著性证据。论文只在附录披露其机制用途和证据边界。

## 统计口径

- 严格任务成功要求 12 个字段全部正确；字段级正确率只用于解释接近成功的运行。
- 二元成功率使用配对 McNemar 精确检验；连续指标使用配对随机化检验和 20,000 次 bootstrap 置信区间。
- 同一任务的重复运行存在相关性。本先导实验把任务与重复编号组成配对执行单元，显著性结果只作探索性证据。
- 多个基线同时比较时使用 Holm 方法校正。
- 失败运行全部保留，除非预注册的基础设施损坏使证据无法读取。

