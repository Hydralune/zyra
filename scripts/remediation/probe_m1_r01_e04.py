from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
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
    candidate = str(REPO_ROOT / relative)
    if candidate not in sys.path:
        sys.path.insert(0, candidate)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from zyra_runtime.executor import ToolExecutor  # noqa: E402
from zyra_runtime.workers import WorkerRequest  # noqa: E402
from zyra_workers import CodeWorkerRuntime  # noqa: E402
from zyra_workers.typescript_claude_runtime import (  # noqa: E402
    TypeScriptClaudeQueryEngine,
)


def _request(run_id: str, *, session_id: str, turns: list[object]) -> WorkerRequest:
    return WorkerRequest(
        run_id=run_id,
        task_id="e04-candidate-task",
        worker_name="CodeWorkerRuntime",
        constraints={
            "query_turns": turns,
            "permission_mode": "sealed",
            "session_id": session_id,
        },
    )


def _runtime(root: Path) -> CodeWorkerRuntime:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return CodeWorkerRuntime(
        project_root=REPO_ROOT,
        workspace_root=workspace,
        artifact_root=root / "artifacts",
    )


def _checkpoint(run: object) -> dict[str, object]:
    worker_result = getattr(run, "worker_result")
    path = Path(worker_result.metadata["runtime_state_checkpoint_path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("runtime checkpoint is not an object")
    return payload


def _assert_typescript_result(run: object) -> Any:
    result = getattr(run, "worker_result")
    if not result.ok:
        raise RuntimeError(f"CodeWorker failed: {result.error}")
    if result.metadata.get("canonical_runtime_owner") != "typescript":
        raise RuntimeError("CodeWorker did not report the TypeScript canonical owner")
    if result.metadata.get("python_query_engine_fallback") != "false":
        raise RuntimeError("CodeWorker reported a Python query fallback")
    return result


def python_bridge(root: Path) -> dict[str, object]:
    runtime = _runtime(root)
    proof = root / "workspace" / "proof.txt"
    proof.write_text("E04 physical tool receipt", encoding="utf-8")
    run = runtime.run(
        _request(
            "e04-python-bridge",
            session_id="e04-python-bridge-session",
            turns=[[{"tool_name": "file_read", "arguments": {"path": "proof.txt"}}]],
        )
    )
    result = _assert_typescript_result(run)
    checkpoint = _checkpoint(run)
    snapshot = checkpoint.get("session_snapshot")
    if not isinstance(snapshot, dict):
        raise RuntimeError("Python bridge did not persist the TypeScript session snapshot")
    receipts = snapshot.get("tool_effect_receipts")
    if not isinstance(receipts, dict) or len(receipts) != 1:
        raise RuntimeError("Python physical tool effect was not receipted exactly once")
    if result.metadata.get("terminal_commit_protocol") != "close-result-ack-closed":
        raise RuntimeError("terminal protocol metadata is not closed")
    return {
        "mode": "python-bridge",
        "passed": True,
        "physical_tool_receipts": len(receipts),
        "checkpoint_revision": checkpoint.get("revision"),
        "terminal_protocol": result.metadata.get("terminal_commit_protocol"),
        "canonical_owner": "typescript",
        "python_fallback": False,
    }


def resume(root: Path) -> dict[str, object]:
    session_id = "e04-cross-process-resume-session"
    request = _request("e04-cross-process-resume", session_id=session_id, turns=[])
    first = _runtime(root).run(request)
    first_result = _assert_typescript_result(first)
    first_checkpoint = _checkpoint(first)
    with mock.patch(
        "zyra_workers.typescript_claude_runtime.subprocess.Popen",
        wraps=subprocess.Popen,
    ) as runtime_launch:
        second = _runtime(root).run(request)
    second_result = _assert_typescript_result(second)
    second_checkpoint = _checkpoint(second)
    first_revision = int(first_checkpoint.get("revision") or 0)
    second_revision = int(second_checkpoint.get("revision") or 0)
    if first_result.metadata["runtime_state_checkpoint_path"] != second_result.metadata[
        "runtime_state_checkpoint_path"
    ]:
        raise RuntimeError("cross-process resume changed the stable checkpoint identity")
    if runtime_launch.call_count != 0:
        raise RuntimeError("cross-process terminal resume relaunched TypeScript")
    if second_result.metadata.get("terminal_result_recovered") != "true":
        raise RuntimeError("cross-process resume did not use the durable terminal receipt")
    return {
        "mode": "resume",
        "passed": True,
        "first_revision": first_revision,
        "second_revision": second_revision,
        "same_checkpoint": True,
        "runtime_relaunches": runtime_launch.call_count,
        "terminal_result_recovered": True,
        "canonical_owner": "typescript",
    }


def lost_ack(root: Path) -> dict[str, object]:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "terminal.txt").write_text("terminal receipt", encoding="utf-8")
    runtime = CodeWorkerRuntime(
        project_root=REPO_ROOT,
        workspace_root=workspace,
        artifact_root=root / "artifacts",
    )
    original_execute = ToolExecutor.execute
    original_write_frame = TypeScriptClaudeQueryEngine._write_frame
    executions = 0
    dropped = False

    def counting_execute(self: ToolExecutor, call: object, **kwargs: object) -> object:
        nonlocal executions
        executions += 1
        return original_execute(self, call, **kwargs)

    def drop_first_terminal_ack(*args: object, **kwargs: object) -> object:
        nonlocal dropped
        if kwargs.get("kind") == "run.result.ack" and not dropped:
            dropped = True
            raise OSError("simulated lost terminal acknowledgement")
        return original_write_frame(*args, **kwargs)

    request = _request(
        "e04-lost-terminal-ack",
        session_id="e04-lost-terminal-ack-session",
        turns=[[{"tool_name": "file_read", "arguments": {"path": "terminal.txt"}}]],
    )
    with (
        mock.patch.object(ToolExecutor, "execute", counting_execute),
        mock.patch.object(
            TypeScriptClaudeQueryEngine,
            "_write_frame",
            autospec=True,
            side_effect=drop_first_terminal_ack,
        ),
    ):
        first = runtime.run(request)
    with (
        mock.patch.object(ToolExecutor, "execute", counting_execute),
        mock.patch(
            "zyra_workers.typescript_claude_runtime.subprocess.Popen",
            wraps=subprocess.Popen,
        ) as runtime_launch,
    ):
        recovered = runtime.run(request)
    _assert_typescript_result(recovered)
    receipts = _checkpoint(recovered)["session_snapshot"]["terminal_result_receipts"]
    if first.worker_result.ok or not dropped or executions != 1:
        raise RuntimeError("lost terminal ACK did not fail then recover exactly once")
    if runtime_launch.call_count != 0 or not isinstance(receipts, dict) or len(receipts) != 1:
        raise RuntimeError("lost terminal ACK recovery relaunched or duplicated the effect")
    return {
        "mode": "lost-ack",
        "passed": True,
        "first_failed_closed": True,
        "runtime_relaunches": runtime_launch.call_count,
        "physical_tool_executions": executions,
        "terminal_receipts": len(receipts),
    }


def duplicate_ack(root: Path) -> dict[str, object]:
    runtime = _runtime(root)
    original_read_frame = TypeScriptClaudeQueryEngine._read_frame
    original_write_frame = TypeScriptClaudeQueryEngine._write_frame
    duplicate: dict[str, object] | None = None
    duplicated = False
    injected_sequence_offset = 0
    terminal_ack_attempts = 0

    def duplicate_terminal_result(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal duplicate, duplicated, injected_sequence_offset
        if duplicate is not None:
            frame = dict(duplicate)
            frame["sequence"] = kwargs["expected_sequence"]
            duplicate = None
            injected_sequence_offset += 1
            return frame
        adjusted = dict(kwargs)
        adjusted["expected_sequence"] = int(kwargs["expected_sequence"]) - injected_sequence_offset
        frame = original_read_frame(*args, **adjusted)
        if frame.get("kind") == "run.result" and not duplicated:
            duplicate = dict(frame)
            duplicated = True
        return frame

    def count_terminal_ack(*args: object, **kwargs: object) -> object:
        nonlocal terminal_ack_attempts
        if kwargs.get("kind") == "run.result.ack":
            terminal_ack_attempts += 1
            if terminal_ack_attempts == 2:
                return None
        return original_write_frame(*args, **kwargs)

    with (
        mock.patch.object(
            TypeScriptClaudeQueryEngine,
            "_read_frame",
            autospec=True,
            side_effect=duplicate_terminal_result,
        ),
        mock.patch.object(
            TypeScriptClaudeQueryEngine,
            "_write_frame",
            autospec=True,
            side_effect=count_terminal_ack,
        ),
    ):
        run = runtime.run(
            _request(
                "e04-duplicate-terminal-delivery",
                session_id="e04-duplicate-terminal-delivery-session",
                turns=[],
            )
        )
    _assert_typescript_result(run)
    receipts = _checkpoint(run)["session_snapshot"]["terminal_result_receipts"]
    if not duplicated or terminal_ack_attempts != 2:
        raise RuntimeError("duplicate terminal delivery was not acknowledged idempotently")
    if not isinstance(receipts, dict) or len(receipts) != 1:
        raise RuntimeError("duplicate terminal delivery produced multiple receipts")
    return {
        "mode": "duplicate-ack",
        "passed": True,
        "ack_attempts": terminal_ack_attempts,
        "terminal_receipts": len(receipts),
    }


def disable(root: Path) -> dict[str, object]:
    with mock.patch.dict(os.environ, {"ZYRA_DISABLE_TYPESCRIPT_RUNTIME": "1"}):
        run = _runtime(root).run(
            _request("e04-disable", session_id="e04-disable-session", turns=[])
        )
    result = run.worker_result
    if result.ok or result.error != "typescript_runtime_disabled":
        raise RuntimeError("disabled TypeScript runtime did not fail closed")
    if result.metadata.get("python_query_engine_fallback") != "false":
        raise RuntimeError("disabled TypeScript runtime attempted Python fallback")
    return {
        "mode": "disable",
        "passed": True,
        "error": result.error,
        "canonical_owner": result.metadata.get("canonical_runtime_owner"),
        "python_fallback": False,
    }


def _post(url: str, path: str, payload: dict[str, object]) -> dict[str, object]:
    request = urllib.request.Request(
        url + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("API response is not an object")
    return result


def api_route(root: Path) -> dict[str, object]:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        {
            "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
            "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
            "ZYRA_TOOL_WORKSPACE": str(workspace),
            "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
            "ZYRA_PERMISSION_STATE": str(root / "permission-state.json"),
        }
    )
    module_name = "apps.api.zyra_api.main"
    module = (
        importlib.reload(sys.modules[module_name])
        if module_name in sys.modules
        else importlib.import_module(module_name)
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.ZyraRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        response = _post(
            base_url,
            "/tasks",
            {"goal": "Execute a deterministic local code task for E04.", "auto_run": True},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        bridge = getattr(module, "_RUNTIME_EVENT_SPINE", None)
        if bridge is not None:
            bridge.close()
        module._RUNTIME_EVENT_SPINE = None
        module._RUNTIME_EVENT_SPINE_KEY = None
        module.reset_runtime_event_spines()
    encoded = json.dumps(response, ensure_ascii=True)
    task = response.get("task")
    events = response.get("events")
    if not isinstance(task, dict) or task.get("status") != "completed":
        failed_nodes = []
        runtime_signals = []
        if isinstance(events, list):
            for event in events:
                payload = event.get("payload") if isinstance(event, dict) else None
                query = payload.get("query_session") if isinstance(payload, dict) else None
                if not isinstance(query, dict):
                    continue
                nested = query.get("payload") if isinstance(query.get("payload"), dict) else {}
                phase = query.get("phase")
                if (
                    phase
                    or query.get("event_type")
                    or query.get("tool_name")
                    or nested.get("decision")
                    or nested.get("error")
                ):
                    runtime_signals.append(
                        {
                            "phase": phase,
                            "event_type": query.get("event_type"),
                            "tool_name": nested.get("tool_name", query.get("tool_name")),
                            "decision": nested.get("decision"),
                            "error": nested.get("error", query.get("error")),
                            "message": nested.get("message", query.get("message")),
                            "ok": nested.get("ok", query.get("ok")),
                            "result": nested.get("result", query.get("result")),
                            "payload": nested if phase in {"tool_call_completed", "tool_failure_signal"} else None,
                            "query": query if phase in {"tool_call_completed", "tool_failure_signal"} else None,
                        }
                    )
        if isinstance(task, dict) and isinstance(task.get("plan_nodes"), dict):
            for node in task["plan_nodes"].values():
                if isinstance(node, dict) and node.get("status") == "failed":
                    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
                    worker_result = (
                        metadata.get("worker_result")
                        if isinstance(metadata.get("worker_result"), dict)
                        else {}
                    )
                    failed_nodes.append(
                        {
                            "node_id": node.get("node_id"),
                            "summary": worker_result.get("summary"),
                            "error": worker_result.get("error"),
                            "worker_metadata": worker_result.get("metadata"),
                        }
                    )
        raise RuntimeError(
            "API task route did not complete: "
            + json.dumps(
                {
                    "task_status": task.get("status") if isinstance(task, dict) else None,
                    "failed_nodes": failed_nodes,
                    "runtime_signals": runtime_signals[-20:],
                    "event_count": len(events) if isinstance(events, list) else None,
                },
                ensure_ascii=True,
                default=str,
            )
        )
    if not isinstance(events, list) or "CodeWorkerRuntime" not in encoded:
        raise RuntimeError("API task route did not enter CodeWorkerRuntime")
    if "canonical_runtime_owner" not in encoded or "typescript" not in encoded:
        raise RuntimeError("API task route did not expose the TypeScript runtime result")
    return {
        "mode": "api-route",
        "passed": True,
        "task_id": task.get("task_id"),
        "task_status": task.get("status"),
        "event_count": len(events),
        "code_worker": True,
        "canonical_owner": "typescript",
    }


MODES = {
    "python-bridge": python_bridge,
    "api-route": api_route,
    "resume": resume,
    "lost-ack": lost_ack,
    "duplicate-ack": duplicate_ack,
    "disable": disable,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=(*MODES, "all"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    local_bun = REPO_ROOT / "node_modules" / ".bin" / "bun.exe"
    if local_bun.is_file():
        os.environ.setdefault("ZYRA_BUN_EXECUTABLE", str(local_bun))
    selected = list(MODES) if args.mode == "all" else [args.mode]
    results: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="zyra-e04-probe-") as temporary:
        base = Path(temporary)
        for mode in selected:
            results.append(MODES[mode](base / mode))
    document = {
        "schema_version": "4.0",
        "execution_id": "E04",
        "mode": args.mode,
        "ok": all(result.get("passed") is True for result in results),
        "results": results,
    }
    encoded = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0 if document["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
