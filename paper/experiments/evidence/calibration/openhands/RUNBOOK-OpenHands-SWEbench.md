# OpenHands SWE-bench 校准运行手册（可复现）

本手册记录已验证的 OpenHands 官方单智能体基线在 SWE-bench Verified 上的完整可复现流程。

## 环境

- WSL Ubuntu，Docker `DOCKER_HOST=tcp://127.0.0.1:2375`
- OpenHands 工作目录：`/home/ylon/zyra-experiments/openhands-benchmarks`（固定提交 `405bae71`，SDK `43376f1`）
- venv：`.venv/bin/python`（Python 3.12.14）
- 模型：`deepseek/deepseek-v4-flash`，temperature 0，配置文件 `/home/ylon/zyra-experiments/.private/openhands-deepseek.json`（不读内容）

## 关键前置：镜像预拉取（绕过 Docker Hub 认证超时）

Docker Hub 认证端点经代理不稳定，必须先预拉取所有镜像到本地 store，让 buildx 命中本地缓存：

```bash
export DOCKER_HOST=tcp://127.0.0.1:2375
docker pull python:3.13-bookworm
docker pull docker/dockerfile:1.7
docker pull ghcr.io/astral-sh/uv:0.11.6
docker pull swebench/sweb.eval.x86_64.{repo}_1776_{instance}:latest   # 每个任务一个
```

大镜像（swebench 基础镜像 3-4GB）需多次重试（CloudFront 传输易 EOF）。

## 构建 agent-server 镜像

```bash
cd /home/ylon/zyra-experiments/openhands-benchmarks
export DOCKER_HOST=tcp://127.0.0.1:2375
export OPENHANDS_SUPPRESS_BANNER=1
export OPENHANDS_BUILDKIT_CACHE_MODE=min
export IMAGE_TAG_PREFIX=43376f1

.venv/bin/python vendor/software-agent-sdk/openhands-agent-server/openhands/agent_server/docker/build.py \
  --base-image "docker.io/swebench/sweb.eval.x86_64.{repo}_1776_{instance}:latest" \
  --custom-tags "sweb.eval.x86_64.{repo}_1776_{instance}" \
  --image "ghcr.io/openhands/eval-agent-server" \
  --target source-minimal --load \
  --sdk-project-root /home/ylon/zyra-experiments/openhands-benchmarks/vendor/software-agent-sdk
```

## 运行（关键：必须用 empty_patch_critic）

**根因**：`deepseek-v4-flash` 在 OpenHands 默认 agent 下，完成任务后不发 `finish` 工具，触发 `MaxIterationsReached`，导致 patch 未入 output.jsonl。这是停止条件设计问题（OpenHands 只认"模型说 finish"），不是代码错误。

**解法**：用 `--critic empty_patch_critic`（官方提供，只要求 git_patch 非空，不要求 finish）。最终判分仍由官方 SWE-bench 评分器独立完成（隐藏测试），不改变评测公平性。

```bash
.venv/bin/python -m benchmarks.swebench.run_infer \
  /home/ylon/zyra-experiments/.private/openhands-deepseek.json \
  --dataset /home/ylon/zyra-experiments/swe-bench-verified-test.jsonl \
  --split test --workspace docker --max-iterations 30 --num-workers 1 \
  --select <选择文件> --critic empty_patch_critic \
  --note <note> \
  --output-dir /mnt/c/Users/Ylon/Desktop/挑战杯2026/zyra/paper/experiments/evidence/calibration/openhands
```

## 提取 patch + 官方判分

```bash
# 1. 从 output.critic_attempt_1.jsonl 提取 git_patch 构造 predictions.jsonl
#    {instance_id, model_patch, model_name_or_path}

# 2. 官方 SWE-bench 评分器独立判分
cd /home/ylon/zyra-experiments/openhands-benchmarks
.venv/bin/python -m swebench.harness.run_evaluation \
  --dataset_name /home/ylon/zyra-experiments/swe-bench-verified-test.jsonl \
  --predictions_path <predictions.jsonl> \
  --max_workers 1 --run_id <run_id> --split test --timeout 1800

# 3. 结果在 openhands-benchmarks/deepseek__deepseek-v4-flash.<run_id>.json
#    详细 report 在 logs/run_evaluation/<run_id>/deepseek__deepseek-v4-flash/<instance>/report.json
```

## 已验证结果

| 任务 | 官方判分 | 备注 |
|---|---|---|
| pallets__flask-5014 | **resolved=true** (1/1) | FAIL_TO_PASS + 72 PASS_TO_PASS 全过 |
| pylint-dev__pylint-8898 | 运行中 | — |

## 注意

1. `output.jsonl` 仍为空是正常的：`error` 非空的结果写到 `output_errors.jsonl`（且存在路径 bug），但 `output.critic_attempt_1.jsonl` 里的 `git_patch` 可直接用于判分。
2. 判分器会读完整 dataset（500 行），`submitted_instances=1` 只判分提交的那一个。
