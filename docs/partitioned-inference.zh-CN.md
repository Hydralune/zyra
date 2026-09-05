# 可执行模型切分与真实任务验收

这项能力把一个 ONNX 计算图切成 device 前段与 edge 后段，通过经过认证的 HTTP/TLS 端点传递中间张量，使用 ONNX Runtime 实际计算。云端 DeepSeek 作为 Zyra Agent 规划、调用工具和分析结果；它的远程 API 权重不在本项目中，不能称为 DeepSeek 权重切分。

原有调度器 `model_split` 字段是任务/模型分配建议。是否发生模型计算切分，应以 `zyra.partitioned-inference/v1` 报告及 `zyra.inference-stage/v1` 计算回执为准，不能以字段名称、角色数或位置标签代替执行证据。

## 安装与复现

在项目虚拟环境安装可选依赖，未安装时其他基础工具仍可使用：

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[inference]'
.\.venv\Scripts\python.exe scripts/prepare_inference_validation.py --root .tmp/inference-remediation --count 256 --offset 137
.\.venv\Scripts\python.exe scripts/validate_partitioned_inference.py --root .tmp/inference-remediation
```

准备脚本下载 ONNX Model Zoo 的 MIT 许可预训练 MNIST CNN 和 MNIST 官方测试集的 CVDF 镜像，固定 SHA-256；不使用训练样本或手工构造标签冒充真实验证。完整来源和预处理写入 `task/input-metadata.json`，标签和模型输入分别保存。权重、数据与运行状态不提交 Git。

验收器用**原始下载模型**作为独立数值基准，而非用切分产物反过来证明自身正确。它还核验预测类别、真实标签准确率、节点进程数和计算回执。模型字节预算仅限制单个模型文件是否允许加载，不代表进程 RSS、峰值激活内存或操作系统内存隔离。

真实 Agent 验收通过正常 CLI `run --sealed` 入口。以下命令使用已有 DeepSeek 环境文件，启动独立状态目录、独立端口的 Zyra 和两个模型进程；完成后关闭本次拥有的进程。`--attempt` 必须是未使用过的字母数字目录名，失败尝试也保留。

```powershell
node --env-file=.env.deepseek.local -e "require('child_process').spawn('.venv/Scripts/python.exe',['scripts/run_zyra_inference_validation.py','--root','.tmp/inference-remediation','--attempt','acceptance01'],{stdio:'inherit',env:process.env}).on('exit',code=>process.exit(code??1))"
.\.venv\Scripts\python.exe scripts/verify_zyra_inference_delivery.py --root .tmp/inference-remediation --attempt acceptance01
```

任务提交前预置 `inference_task_acceptance.py`，由 Agent 在真实执行期间运行 unittest。测试包括真实输出一致性，以及改变 logits/标签后重新执行 Agent 编写的分析程序。结束后的独立验收器还使用原始完整模型逐项核验交付 CSV、样本摘要、分段张量回执、权限消费回执和 DeepSeek Provider 用量；仅 CLI 退出成功不能替代这些检查。

## 自定义模型与部署

```powershell
python -m zyra_runtime.inference partition --model model.onnx --output model-bundle --cut-after 3 --model-id my-model
python -m zyra_runtime.inference.node --bundle model-bundle --node-id device-a --location device --parts full,front --port 9101
python -m zyra_runtime.inference.node --bundle model-bundle --node-id edge-a --location edge --parts back --port 9102
```

端点从环境变量 `ZYRA_INFERENCE_TOKEN` 读取至少 32 字符的随机令牌。配置文件不含密钥，客户端同样从该环境变量取令牌。非 loopback 监听必须加 `--cert`、`--key`，非 loopback 客户端 URL 必须使用 HTTPS，并通过系统信任链验证；不提供跳过证书检查的开关。模型文件由可信操作人员部署，节点不接受 Agent 上传任意模型、地址或令牌。连接禁止自动重定向和代理转发。

`ZYRA_INFERENCE_CONFIG` 指向操作人员维护的配置；相对 bundle 路径以该文件目录为基准。例如：

```json
{
  "schema": "zyra.inference-config/v1",
  "minimum_sensitivity": "internal",
  "maximum_samples": 1000,
  "timeout_seconds": 15,
  "models": {"my-model": {"bundle": "model-bundle"}},
  "nodes": {
    "device": {"url": "http://127.0.0.1:9101", "allowed_sensitivity": ["public", "internal", "sensitive", "restricted"], "cold_compute_estimate_ms": 10},
    "edge": {"url": "http://127.0.0.1:9102", "allowed_sensitivity": ["public", "internal"], "cold_compute_estimate_ms": 10}
  }
}
```

在两台机器上分别启动节点，将 edge URL 换成真实 HTTPS 主机地址即可使用同一协议。协调器本身运行在可信的数据拥有方；隐私分类约束的是推理端点路由，不构成整个云端 Agent 的自动信息流隔离。数据、中间激活和输出均应按照原敏感等级管理；禁止宣称中间激活天然匿名。`restricted` 数据只允许完整 device 推理，Agent 无法通过传入 `public` 降低操作人员设置的最低等级。

## Agent 接口与证据

`model_inference(model_id, path, output_path?, mode?, sensitivity?, batch?, latency_sla_ms?)` 读取工作区 NPZ，写入工作区 JSON 报告及不可变 Artifact。工具需要现有 TypeScript 权限所有者的一次性执行许可；托管工作区读取和写入经过现有 WorkspaceEditPort，缺失时明确失败。模型、节点和凭据只能来自操作人员配置。

`auto` 根据健康检查、模型摘要、节点实际已加载分段、隐私等级和时延估计选择可行路由；`full`/`split` 显式限制候选。时延估计是 RTT 加已测分段计算耗时；未测部分使用配置的冷启动估计。它不保证切分更快，HTTP/TLS 建连、排队、序列化和激活传输都会增加成本。执行也受总时限约束。

自动分段执行中节点丢失时，只有此前完整 device 候选也满足约束，才重新计算当前样本并切换完整推理。只适用于无外部副作用的推理；记录回退原因与已成功分段。强制 `split` 不隐藏故障。节点重启后的 generation 与旧回执不匹配时拒绝结果。

每条回执记录实际 PID、主机名、进程 generation、分段与原模型摘要、张量内容摘要/字节数、运算数和耗时。主机身份是经过认证节点的自报信息，不是硬件证明。两个同机进程只能证明进程隔离和传输，不能当作两台物理设备。

## 算法与复杂度

```text
partition(G, cut):
  checker + shape inference; reject unsupported subgraphs/external weights
  boundary = prefix outputs consumed by suffix or required as final outputs
  preserve every original input directly consumed by suffix
  extract prefix -> boundary and (boundary + preserved inputs) -> final outputs
  verify each partition stays within its declared original nodes
  save executable graphs, tensor contracts and SHA-256 manifest

infer(input, constraints):
  authenticate/probe nodes; verify model/part identity and actual availability
  enumerate full(device) and split(device.front, edge.back)
  filter by privacy floor, actual loaded parts and estimated latency
  choose smallest estimated latency among admitted routes
  for each sample:
    dispatch real computation; validate request, generation and tensor digests
    on split fault, recompute locally only if auto and full was admissible
  enforce deadline; persist numeric outputs and evidence counters
```

设节点数 V、依赖边数 E、权重字节 W、每样本跨段张量字节 A、样本数 N。边界识别、图提取和契约枚举为 O(V+E) 加 ONNX 自身 shape inference；读写和哈希约 O(W)，初始化器复制可增加两个分段的总磁盘占用。候选数固定为 2，路由枚举为 O(1)；总推理代价为 O(N × 模型计算量 + N × A) 加传输/序列化时间，报告储存为 O(N × 回执大小 + 输出大小)。当前实现不是在所有切点上搜索最优解，不支持动态迁移一个正在执行的算子。

初版支持可推断中间张量契约的平坦 ONNX DAG；控制流子图和 external-data 权重明确拒绝，允许覆盖的初始化器输入须先冻结。批模式把第 0 维逐样本切片，适合单样本输入模型，不能用来声称支持任意模型的 KV cache、流水并行或大语言模型张量并行。

## 验收口径

必须分别报告真实 Provider 请求、实际 Agent 工具调用、领域样本数、分段计算次数、故障恢复和领域质量。256 个样本、512 段运算、几千条内部事件均不能自动算作 Agent 千步推理。本功能的真实 MNIST 任务只证明该任务及模型切分调用链，不取代赛题中多领域、动态长程任务和物理跨设备的专项验收。

此次真实验收还修复了三个收尾问题：仅缺 Markdown/rst 报告不应要求修改业务源码才允许重跑验收；同一算子的恢复重试不增加计划深度，但必须累计全部成本；CLI 必须识别绑定当前任务的显式失败终态，不能因为最终验证已通过、外层完成门禁却异常而永久等待。这些修复均不将失败结果转换为成功。
