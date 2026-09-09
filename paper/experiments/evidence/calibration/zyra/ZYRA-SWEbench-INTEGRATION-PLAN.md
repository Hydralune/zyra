# ZYRA 对接 SWE-bench Verified 的可执行路径（已源码级确认）

## 结论

ZYRA **已经实现了**对接 Docker 化 benchmark 的能力（无需从零写适配器），通过以下机制：

1. **`DockerCliSandboxConnector`**（`packages/runtime/zyra_runtime/sandbox_gateway/docker_cli_connector.py`）：
   - 在"预先存在的 Docker 容器"里通过 `docker exec` 执行命令
   - 容器生命周期由外部 benchmark harness 拥有
   - 这是专门为 benchmark 场景设计的

2. **`_benchmark_docker_binding`**（`code_worker_adapter.py:3105`）：
   - 通过环境变量 `ZYRA_BENCHMARK_DOCKER_CONTAINER` / `ZYRA_BENCHMARK_DOCKER_WORKDIR` / `ZYRA_BENCHMARK_HOST_WORKSPACE` 配置
   - 配置后，CodeWorker 在容器内改代码，通过 `_BenchmarkWorkspaceMirror` 同步回宿主机

## 对接 SWE-bench 的可执行步骤

1. 启动 flask SWE-bench 容器（已有镜像 `swebench/sweb.eval.x86_64.pallets_1776_flask-5014:latest`，4.23GB）
2. 设置环境变量：
   - `ZYRA_BENCHMARK_DOCKER_CONTAINER=<container_name>`
   - `ZYRA_BENCHMARK_DOCKER_WORKDIR=/workspace/flask`（容器内 flask 路径）
   - `ZYRA_BENCHMARK_HOST_WORKSPACE=<宿主机工作区>`
   - `DEEPSEEK_API_KEY`、`ZYRA_DEEPSEEK_ENABLED=true`、`ZYRA_MODEL=deepseek-v4-flash`
3. 启动 ZYRA API：`python scripts/dev_api.py`（复用 `run_zyra_inference_validation.py` 的启动链路）
4. 提交任务：CLI `node apps/cli/dist/zyra.js run <problem_statement> --sealed` 或 API `createPendingTask`
5. CodeWorker 通过 `docker exec` 在 flask 容器改代码
6. 提取 `git diff` → 官方 SWE-bench 评分

## 已就绪的基础设施（实证）

- Python 3.12.7 / node v22 / bun 1.2.15 可用
- ZYRA 核心 Python 包可 import（sys.path 方式）
- `apps/cli/dist/zyra.js` 编译版已存在
- deepseek 模型链路已打通（`dispatch_marker` 200 OK，prompt 109 / completion 26 tokens）
- CodeWorker TS health check 通过（`ok:true`, worker=CodeWorkerRuntime）
- node stdio-server（provider-control-plane）可启动

## 剩余工作（未完成）

1. 写一个 SWE-bench harness orchestrator（启动 flask 容器 + 设环境变量 + 启动 API + 提交任务 + 收 patch），参考 `run_zyra_inference_validation.py` 的启动链路
2. 验证 CodeWorker 在 flask 容器里能正确改代码（当前只验证到 health check，未验证实际改代码）
3. 产出 patch → 官方判分 → 与 OpenHands 公平对照

## 关键风险

- ZYRA 完整启动链路（API + worker + provider-control-plane + CodeWorker TS）组件多，需精确配置，调试周期长
- CodeWorker 实际改代码的行为未经验证（只验证了 health check 和模型 marker dispatch）
