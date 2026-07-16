from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
for relative in (
    "packages/core",
    "packages/runtime",
    "packages/workers",
    "packages/workspace",
    "packages/skills",
    "packages/integrations",
    "packages/orchestration",
):
    path = str(REPO_ROOT / relative)
    if path not in sys.path:
        sys.path.insert(0, path)

from zyra_runtime.executor import ToolExecutor  # noqa: E402
from zyra_runtime.workers import WorkerRequest  # noqa: E402
from zyra_workers import CodeWorkerRuntime  # noqa: E402
from zyra_workers.typescript_claude_runtime import (  # noqa: E402
    TypeScriptClaudeQueryEngine,
)


def request(run_id: str, *, session_id: str, turns: list[object]) -> WorkerRequest:
    return WorkerRequest(
        run_id=run_id,
        task_id="e01-default-path-probe",
        worker_name="CodeWorkerRuntime",
        constraints={
            "query_turns": turns,
            "permission_mode": "sealed",
            "session_id": session_id,
        },
    )


def runtime(root: Path) -> CodeWorkerRuntime:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return CodeWorkerRuntime(
        project_root=REPO_ROOT,
        workspace_root=workspace,
        artifact_root=root / "artifacts",
    )


def persisted_result(run: object) -> dict[str, object]:
    worker_result = getattr(run, "worker_result")
    checkpoint_path = Path(worker_result.metadata["runtime_state_checkpoint_path"])
    return json.loads(checkpoint_path.read_text(encoding="utf-8"))


def checkpoint_probe(root: Path) -> dict[str, object]:
    selected = runtime(root)
    run = selected.run(
        request(
            "e01-default-checkpoint-probe",
            session_id="e01-default-checkpoint-session",
            turns=[],
        )
    )
    checkpoint_files = list(
        (root / "artifacts" / ".runtime-checkpoints").glob("typescript-e01-*.json")
    )
    if not run.worker_result.ok or len(checkpoint_files) != 1:
        raise RuntimeError("default path did not produce one durable incremental checkpoint")
    checkpoint = json.loads(checkpoint_files[0].read_text(encoding="utf-8"))
    return {
        "ok": True,
        "canonical_owner": checkpoint.get("canonical_owner"),
        "session_id": checkpoint.get("session_id"),
        "checkpoint_phase": checkpoint.get("checkpointPhase"),
        "checkpoint_event_sequence": checkpoint.get("checkpointEventSequence"),
        "revision": checkpoint.get("revision"),
    }


def concurrency_probe(root: Path) -> dict[str, object]:
    selected = runtime(root)
    workspace = root / "workspace"
    (workspace / "one.txt").write_text("one", encoding="utf-8")
    (workspace / "two.txt").write_text("two", encoding="utf-8")
    original_execute = ToolExecutor.execute
    intervals: dict[str, list[float]] = {}

    def delayed_execute(self: ToolExecutor, call: object, **kwargs: object) -> object:
        interval = intervals.setdefault(str(getattr(call, "tool_call_id")), [])
        interval.append(time.perf_counter())
        time.sleep(0.2)
        result = original_execute(self, call, **kwargs)
        interval.append(time.perf_counter())
        return result

    with mock.patch.object(ToolExecutor, "execute", delayed_execute):
        run = selected.run(
            request(
                "e01-default-concurrency-probe",
                session_id="e01-default-concurrency-session",
                turns=[
                    [
                        {"tool_name": "file_read", "arguments": {"path": "one.txt"}},
                        {"tool_name": "file_read", "arguments": {"path": "two.txt"}},
                    ]
                ],
            )
        )
    if not run.worker_result.ok or len(intervals) != 2:
        raise RuntimeError("default path did not execute the two-call read-only batch")
    starts = [interval[0] for interval in intervals.values()]
    ends = [interval[1] for interval in intervals.values()]
    overlap = max(starts) < min(ends)
    snapshot = persisted_result(run)["session_snapshot"]
    if not isinstance(snapshot, dict):
        raise RuntimeError("default path returned an invalid durable session snapshot")
    evidence = snapshot.get("tool_batch_evidence")
    if not isinstance(evidence, dict) or not overlap:
        raise RuntimeError("read-only calls did not overlap on the real stdio path")
    return {
        "ok": True,
        "overlap": overlap,
        "execution_mode": evidence.get("execution_mode"),
        "request_count": evidence.get("request_count"),
        "result_order": evidence.get("result_order"),
        "elapsed_ms": evidence.get("elapsed_ms"),
    }


def lost_ack_probe(root: Path) -> dict[str, object]:
    selected = runtime(root)
    workspace = root / "workspace"
    (workspace / "proof.txt").write_text("durable receipt", encoding="utf-8")
    original_execute = ToolExecutor.execute
    original_write_frame = TypeScriptClaudeQueryEngine._write_frame
    executions = 0
    dropped = False

    def counting_execute(self: ToolExecutor, call: object, **kwargs: object) -> object:
        nonlocal executions
        executions += 1
        return original_execute(self, call, **kwargs)

    def drop_first_result(*args: object, **kwargs: object) -> object:
        nonlocal dropped
        if kwargs.get("kind") == "tool.batch.result" and not dropped:
            dropped = True
            raise OSError("simulated lost tool batch acknowledgement")
        return original_write_frame(*args, **kwargs)

    selected_request = request(
        "e01-default-lost-ack-probe",
        session_id="e01-default-lost-ack-session",
        turns=[
            [{"tool_name": "file_read", "arguments": {"path": "proof.txt"}}]
        ],
    )
    with (
        mock.patch.object(ToolExecutor, "execute", counting_execute),
        mock.patch.object(
            TypeScriptClaudeQueryEngine,
            "_write_frame",
            autospec=True,
            side_effect=drop_first_result,
        ),
    ):
        first = selected.run(selected_request)
    with mock.patch.object(ToolExecutor, "execute", counting_execute):
        resumed = selected.run(selected_request)
    if first.worker_result.ok or not resumed.worker_result.ok or executions != 1:
        raise RuntimeError("lost-ACK recovery re-executed or failed to resume the tool call")
    snapshot = persisted_result(resumed)["session_snapshot"]
    if not isinstance(snapshot, dict):
        raise RuntimeError("resumed path returned an invalid durable session snapshot")
    receipts = snapshot.get("tool_effect_receipts")
    return {
        "ok": True,
        "first_run_failed_closed": not first.worker_result.ok,
        "resumed": resumed.worker_result.ok,
        "executor_invocations": executions,
        "receipt_count": len(receipts) if isinstance(receipts, dict) else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="zyra-e01-default-path-") as temporary:
        root = Path(temporary)
        evidence = {
            "schema": "zyra.m1-r01.e01-default-path-probe.v1",
            "checkpoint": checkpoint_probe(root / "checkpoint"),
            "concurrent_read_only": concurrency_probe(root / "concurrency"),
            "lost_tool_ack": lost_ack_probe(root / "lost-ack"),
        }
    encoded = json.dumps(evidence, ensure_ascii=True, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
