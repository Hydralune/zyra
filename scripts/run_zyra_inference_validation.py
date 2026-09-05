"""Start isolated Zyra + real model nodes and submit a normal sealed CLI task.

Provider credentials must already be in the environment, e.g. via Node's
--env-file=.env.deepseek.local. No credentials are printed or persisted here.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

import psutil

from zyra_runtime.inference.local import LocalInferenceCluster


PROJECT = Path(__file__).resolve().parents[1]
TASK = """完成一个真实的手写数字识别与质量分析任务。当前工作区已有 images.npz、labels.json、input-metadata.json。
必须调用 Zyra 内置 model_inference 工具，对 images.npz 做真实推理；如果工具尚未加载，先用 tool_search 搜索。
注册模型 ID 为 mnist-cnn，输入 NPZ 字段 Input3；使用 mode=auto、batch=true、sensitivity=public、latency_sla_ms=600000、output_path=inference.json。
该工具会自动按实际可用模型和资源选择 device/edge 分段，完整数值输出与每一段回执写入 inference.json；不能用 labels.json 代替推理或生成硬编码预测。
推理后编写并实际运行 analyze.py：读取 inference.json 的 outputs.Plus214_Output_0（每张图像的 10 个 logits），argmax 得到预测，再读取 labels.json 独立计算准确率和全部错误样本。报告文件较大，请用 shell 执行 Python 分析，不要把完整回执塞入上下文。
必须交付：
1. predictions.csv，列为 sample_index,prediction,label，每个输入恰好一行；sample_index 从 0 起。
2. metrics.json，包含 sample_count、correct、accuracy、mismatches（对象列表，字段 sample_index,prediction,label）、stage_execution_count、distinct_process_count、distinct_reported_host_count。
3. REPORT.md，中文说明数据来源、预处理、实际路由、真实准确率和错误样本，并准确说明是两个独立进程还是两台物理设备。不能把样本数或分段执行次数当作 Agent 思考步数，不能声称已经证明所有赛题场景或千步复杂任务。
4. analyze.py 和工具产出的 inference.json，供复查。
使用当前工作区相对路径。运行环境是 Windows/PowerShell，Python 可通过 python 调用。提交前实际运行分析并检查 CSV 行数、metrics 和报告一致。无需下载数据或访问任何密钥。完成任务后给出简短交付说明。
工作区预置了不可修改的独立验收脚本 inference_task_acceptance.py。完成全部文件（包括 REPORT.md）后必须运行 `python -B -m unittest -v inference_task_acceptance`，通过后再提交；该测试也会改变 logits 和标签，验证 analyze.py 确实重新计算，不能只针对这一批数据写死结果。若工具 schema 支持，请使用 shell 的 executable=python、argv=["-B","-m","unittest","-v","inference_task_acceptance"] 调用。-B 用于避免托管工作区出现不必要的字节码缓存。不要搜索外部验收器。
"""


def stop_owned(process):
    try:
        descendants = psutil.Process(process.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        descendants = []
    for child in reversed(descendants):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    if process.poll() is None:
        process.terminate()
    process.wait(timeout=10)
    psutil.wait_procs(descendants, timeout=5)


def available_profile_ports():
    # Deployment profiles use base, base+1, base+2. Isolating only the API
    # port is insufficient when another Zyra instance is still running.
    for _ in range(100):
        reserved = []
        try:
            first = socket.socket()
            reserved.append(first)
            first.bind(("127.0.0.1", 0))
            base = first.getsockname()[1]
            if base > 65533:
                continue
            for port in (base + 1, base + 2):
                candidate = socket.socket()
                reserved.append(candidate)
                candidate.bind(("127.0.0.1", port))
            return base
        except OSError:
            continue
        finally:
            for candidate in reserved:
                candidate.close()
    raise RuntimeError("no free consecutive deployment profile ports found")


def run(root: Path, attempt: str):
    output = root / attempt
    output.mkdir(parents=True, exist_ok=False)
    workspace = output / "task"
    workspace.mkdir()
    for name in ("images.npz", "labels.json", "input-metadata.json"):
        shutil.copyfile(root / "task" / name, workspace / name)
    (workspace / "TASK.md").write_text(TASK, encoding="utf-8")
    shutil.copyfile(PROJECT / "scripts/inference_task_acceptance.py", workspace / "inference_task_acceptance.py")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    profile_base_port = available_profile_ports()
    with LocalInferenceCluster(root / "bundle", output / "nodes", constrain_device=True) as cluster:
        environment = dict(os.environ)
        environment.update({"ZYRA_STATE_ROOT": str(output / "state"), "ZYRA_API_HOST": "127.0.0.1",
                            "ZYRA_API_PORT": str(port), "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1",
                            "ZYRA_DEPLOYMENT_PROFILE_BASE_PORT": str(profile_base_port),
                            "ZYRA_CLI_STATE_DIR": str(output / "cli-state"),
                            "ZYRA_DISABLE_LOCAL_PROVIDER_ENV_FILES": "true",
                            "ZYRA_GLM_ENABLED": "false", "ZYRA_KIMI_ENABLED": "false",
                            "ZYRA_DEEPSEEK_ENABLED": "true", "ZYRA_INFERENCE_CONFIG": str(cluster.config_path),
                            "ZYRA_INFERENCE_TOKEN": cluster.token})
        with (output / "api.stdout.log").open("w", encoding="utf-8") as out, (output / "api.stderr.log").open("w", encoding="utf-8") as err:
            api = subprocess.Popen([sys.executable, "scripts/dev_api.py"], cwd=PROJECT, env=environment,
                                   stdout=out, stderr=err, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                opener = build_opener(ProxyHandler({}))
                deadline = time.monotonic() + 90
                while True:
                    if api.poll() is not None:
                        raise RuntimeError("Zyra API exited during startup; see logs")
                    try:
                        with opener.open(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                            health = json.load(response)
                        break
                    except Exception:
                        if time.monotonic() > deadline:
                            raise TimeoutError("Zyra startup health did not become available")
                        time.sleep(.5)
                (output / "startup.json").write_text(json.dumps({"url": f"http://127.0.0.1:{port}", "profile_base_port": profile_base_port, "health": health}, indent=2), encoding="utf-8")
                print(json.dumps({"phase": "zyra_started", "url": f"http://127.0.0.1:{port}", "evidence": str(output)}), flush=True)
                command = ["node", str(PROJECT / "apps/cli/dist/zyra.js"), "run", "--base-url", f"http://127.0.0.1:{port}",
                           "--file", "TASK.md", "--sealed", "--timeout", "0"]
                with (output / "cli.jsonl").open("w", encoding="utf-8") as cli_out, (output / "cli.stderr.log").open("w", encoding="utf-8") as cli_err:
                    cli = subprocess.Popen(command, cwd=workspace, env=environment, stdout=cli_out, stderr=cli_err,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    try:
                        while cli.poll() is None:
                            time.sleep(10)
                            print(json.dumps({"phase": "real_task_running", "cli_log_bytes": (output / "cli.jsonl").stat().st_size}), flush=True)
                    finally:
                        if cli.poll() is None:
                            stop_owned(cli)
                (output / "execution.json").write_text(json.dumps({"cli_exit_code": cli.returncode,
                    "command": command, "workspace": str(workspace), "manual_intervention_during_task": False}, indent=2), encoding="utf-8")
                print(json.dumps({"phase": "real_task_finished", "cli_exit_code": cli.returncode, "evidence": str(output)}), flush=True)
                return cli.returncode
            finally:
                stop_owned(api)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--attempt", required=True)
    args = parser.parse_args()
    if not args.attempt.isalnum():
        parser.error("attempt must be an alphanumeric directory name")
    sys.exit(run(args.root.resolve(), args.attempt))
