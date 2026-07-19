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
    TypeScriptRuntimeError,
)


def _request(
    run_id: str,
    *,
    session_id: str,
    turns: list[object],
    extra_constraints: dict[str, object] | None = None,
) -> WorkerRequest:
    return WorkerRequest(
        run_id=run_id,
        task_id="e04-candidate-task",
        worker_name="CodeWorkerRuntime",
        constraints={
            "query_turns": turns,
            "permission_mode": "sealed",
            "session_id": session_id,
            **(extra_constraints or {}),
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


FAULT_POINTS = {
    "checkpoint-before-ack": "checkpoint_request_before_ack",
    "final-checkpoint-before-terminal": "final_checkpoint_ack_before_terminal",
    "lost-ack": "terminal_result_ack_lost",
    "host-disconnect": "python_host_terminal_disconnect",
    "typescript-disconnect": "typescript_process_terminal_disconnect",
}


def terminal_fault(root: Path, mode: str) -> dict[str, object]:
    fault_point = FAULT_POINTS[mode]
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "terminal.txt").write_text(f"terminal receipt for {mode}", encoding="utf-8")
    original_execute = ToolExecutor.execute
    executions = 0

    def counting_execute(self: ToolExecutor, call: object, **kwargs: object) -> object:
        nonlocal executions
        executions += 1
        return original_execute(self, call, **kwargs)

    run_id = f"e04-{mode}"
    session_id = f"e04-{mode}-session"
    faulted_request = _request(
        run_id,
        session_id=session_id,
        turns=[[{"tool_name": "file_read", "arguments": {"path": "terminal.txt"}}]],
        extra_constraints={"typescript_fault_injection": fault_point},
    )
    recovery_request = _request(
        run_id,
        session_id=session_id,
        turns=[[{"tool_name": "file_read", "arguments": {"path": "terminal.txt"}}]],
    )
    with (
        mock.patch.object(ToolExecutor, "execute", counting_execute),
        mock.patch(
            "zyra_workers.typescript_claude_runtime.subprocess.Popen",
            wraps=subprocess.Popen,
        ) as runtime_launch,
    ):
        first = _runtime(root).run(faulted_request)
        recovered = _runtime(root).run(recovery_request)
    _assert_typescript_result(recovered)
    checkpoint = _checkpoint(recovered)
    snapshot = checkpoint.get("session_snapshot")
    if not isinstance(snapshot, dict):
        raise RuntimeError("terminal recovery did not persist a session snapshot")
    fault_receipts = list(snapshot.get("runtime_fault_receipts") or [])
    matching_faults = [
        receipt
        for receipt in fault_receipts
        if isinstance(receipt, dict)
        and receipt.get("point") == fault_point
        and receipt.get("real_process_kill") is True
        and isinstance(receipt.get("process_exit_code"), int)
    ]
    terminal_receipts = snapshot.get("terminal_result_receipts")
    protocol_trace = list(snapshot.get("protocol_frame_trace") or [])
    if first.worker_result.ok or not matching_faults:
        raise RuntimeError(f"{mode} did not kill a real TypeScript owner and fail closed")
    if executions != 1:
        raise RuntimeError(f"{mode} re-executed the physical tool effect: {executions}")
    if not isinstance(terminal_receipts, dict) or len(terminal_receipts) != 1:
        raise RuntimeError(f"{mode} did not commit exactly one terminal receipt")
    if not protocol_trace or any(not isinstance(item, dict) for item in protocol_trace):
        raise RuntimeError(f"{mode} did not persist its complete protocol frame sequence")
    trace_indexes = [int(item.get("trace_index") or 0) for item in protocol_trace]
    if trace_indexes != list(range(1, len(protocol_trace) + 1)):
        raise RuntimeError(f"{mode} protocol frame trace has a gap: {trace_indexes}")
    traced_kinds = {str(item.get("kind") or "") for item in protocol_trace}
    required_kinds = {
        "run.start",
        "run.accepted",
        "runtime.checkpoint",
        "runtime.checkpoint.result",
        "tool.batch.request",
        "tool.batch.result",
        "run.result",
    }
    if mode != "lost-ack":
        required_kinds.update({"run.result.ack", "run.closed"})
    missing_kinds = sorted(required_kinds - traced_kinds)
    if missing_kinds:
        raise RuntimeError(f"{mode} protocol frame trace is incomplete: {missing_kinds}")
    traced_sequences: dict[tuple[int, str], list[int]] = {}
    for item in protocol_trace:
        key = (
            int(item.get("runtime_process_epoch") or 0),
            str(item.get("direction") or ""),
        )
        traced_sequences.setdefault(key, []).append(int(item.get("sequence") or 0))
    for key, sequences in traced_sequences.items():
        if sequences != list(range(1, len(sequences) + 1)):
            raise RuntimeError(
                f"{mode} protocol sequence is not contiguous for {key}: {sequences}"
            )
    terminal = next(iter(terminal_receipts.values()))
    if not isinstance(terminal, dict) or not terminal.get("terminal_id"):
        raise RuntimeError(f"{mode} terminal identity is missing")
    process_epochs = {
        int(receipt.get("runtime_process_epoch") or 0)
        for receipt in matching_faults
    }
    recovered_epoch = int(recovered.worker_result.metadata.get("runtime_process_epoch") or 0)
    if recovered_epoch:
        process_epochs.add(recovered_epoch)
    minimum_launches = 1 if mode == "lost-ack" else 2
    if runtime_launch.call_count < minimum_launches:
        raise RuntimeError(
            f"{mode} observed {runtime_launch.call_count} runtime launches; expected {minimum_launches}"
        )
    if mode != "lost-ack" and len(process_epochs) < 2:
        raise RuntimeError(f"{mode} did not cross two runtime process epochs: {process_epochs}")
    with mock.patch(
        "zyra_workers.typescript_claude_runtime.subprocess.Popen",
        wraps=subprocess.Popen,
    ) as terminal_relaunch:
        terminal_recovery = _runtime(root).run(recovery_request)
    terminal_recovery_result = _assert_typescript_result(terminal_recovery)
    repeated_terminal_receipts = _checkpoint(terminal_recovery)["session_snapshot"][
        "terminal_result_receipts"
    ]
    repeated_terminal = next(iter(repeated_terminal_receipts.values()))
    if terminal_relaunch.call_count != 0:
        raise RuntimeError(f"{mode} relaunched after its terminal receipt was durable")
    if terminal_recovery_result.metadata.get("terminal_result_recovered") != "true":
        raise RuntimeError(f"{mode} did not recover the committed terminal receipt")
    if repeated_terminal.get("terminal_id") != terminal.get("terminal_id"):
        raise RuntimeError(f"{mode} changed stable terminal identity")
    return {
        "mode": mode,
        "fault_point": fault_point,
        "passed": True,
        "first_failed_closed": True,
        "real_process_kills": len(matching_faults),
        "runtime_launches": runtime_launch.call_count,
        "restart_epochs": len(process_epochs),
        "process_epochs": sorted(process_epochs),
        "physical_tool_executions": executions,
        "host_checkpoint_revision": int(snapshot.get("host_checkpoint_revision") or 0),
        "terminal_receipts": len(terminal_receipts),
        "stable_terminal_id": terminal.get("terminal_id"),
        "terminal_relaunches_after_commit": terminal_relaunch.call_count,
        "protocol_frame_count": len(protocol_trace),
        "protocol_frame_trace": protocol_trace,
    }


def stale_writer(root: Path) -> dict[str, object]:
    runtime = _runtime(root)
    session_id = "e04-stale-writer-session"
    first = TypeScriptClaudeQueryEngine(runtime.execution_context)
    stale = TypeScriptClaudeQueryEngine(runtime.execution_context)
    first._load_incremental_checkpoint(session_id)
    stale._load_incremental_checkpoint(session_id)
    committed = first._persist_incremental_checkpoint(session_id, {"probe": "first"})
    try:
        stale._persist_incremental_checkpoint(session_id, {"probe": "stale"})
    except TypeScriptRuntimeError as error:
        if error.code != "typescript_runtime_checkpoint_stale_writer":
            raise
    else:
        raise RuntimeError("stale checkpoint writer was not rejected")
    return {
        "mode": "stale-writer",
        "passed": True,
        "committed_revision": committed["host_checkpoint_revision"],
        "stale_writer_rejected": True,
    }


def corrupt_checkpoint(root: Path) -> dict[str, object]:
    runtime = _runtime(root)
    engine = TypeScriptClaudeQueryEngine(runtime.execution_context)
    session_id = "e04-corrupt-checkpoint-session"
    path = engine._checkpoint_path(session_id)
    path.write_text("{not-json", encoding="utf-8")
    try:
        engine._load_incremental_checkpoint(session_id)
    except TypeScriptRuntimeError as error:
        if error.code != "typescript_runtime_checkpoint_corrupt":
            raise
    else:
        raise RuntimeError("corrupt checkpoint was accepted")
    return {"mode": "corrupt-checkpoint", "passed": True, "corrupt_checkpoint_rejected": True}


def runtime_ports(root: Path, *, built: bool) -> dict[str, object]:
    root.mkdir(parents=True, exist_ok=True)
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    command = (
        ["node", str(REPO_ROOT / "dist" / "code-worker-node" / "main.js")]
        if built
        else [
            str(
                os.environ.get("ZYRA_BUN_EXECUTABLE")
                or REPO_ROOT / "node_modules" / "bun" / "bin" / "bun.exe"
            ),
            str(REPO_ROOT / "apps" / "code-worker" / "src" / "main.ts"),
        ]
    )
    e02_frames = [
        {
            "type": "initialize",
            "request_id": "e04-e02-initialize",
            "workspace_root": str(workspace),
            "state_path": str(root / "e02-state.json"),
            "permission_mode": "sealed",
            "sealed_autonomous": True,
        },
        {
            "type": "request",
            "request_id": "e04-e02-health",
            "operation": "health",
            "payload": {},
        },
    ]
    e02 = subprocess.run(
        [*command, "--e02-api"],
        cwd=REPO_ROOT,
        input="".join(json.dumps(frame) + "\n" for frame in e02_frames),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=120,
        check=False,
    )
    e02_output = [json.loads(line) for line in e02.stdout.splitlines() if line.strip()]
    if (
        e02.returncode != 0
        or len(e02_output) != 2
        or e02_output[0].get("type") != "ready"
        or e02_output[1].get("type") != "response"
        or e02_output[1].get("ok") is not True
        or dict(e02_output[1].get("payload") or {}).get("canonical_owner")
        != "typescript"
    ):
        raise RuntimeError(
            "E02 source/built port failed: "
            + json.dumps(
                {"exit_code": e02.returncode, "stdout": e02.stdout, "stderr": e02.stderr}
            )
        )
    e03_frame = {
        "schema_version": "3.0",
        "request_id": "e04-e03-list",
        "idempotency_key": "e04-e03-list-once",
        "run_id": "e04-e03-run",
        "session_id": "e04-e03-session",
        "parent_task_id": "e04-e03-parent",
        "expected_revision": 0,
        "command": "agent.list",
        "body": {},
    }
    environment = dict(os.environ)
    environment["ZYRA_E03_STATE_ROOT"] = str(root / "e03-state")
    e03 = subprocess.run(
        [*command, "--e03-control"],
        cwd=REPO_ROOT,
        input=json.dumps(e03_frame) + "\n",
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=120,
        check=False,
        env=environment,
    )
    e03_output = [json.loads(line) for line in e03.stdout.splitlines() if line.strip()]
    if (
        e03.returncode != 0
        or len(e03_output) != 1
        or e03_output[0].get("ok") is not True
        or e03_output[0].get("command") != "agent.list"
    ):
        raise RuntimeError(
            "E03 source/built port failed: "
            + json.dumps(
                {"exit_code": e03.returncode, "stdout": e03.stdout, "stderr": e03.stderr}
            )
        )
    return {
        "mode": "built-ports" if built else "source-ports",
        "passed": True,
        "entrypoint": command,
        "e02_protocol": e02_output[0].get("protocol"),
        "e02_canonical_owner": dict(e02_output[1].get("payload") or {}).get(
            "canonical_owner"
        ),
        "e03_command": e03_output[0].get("command"),
        "e03_phase": e03_output[0].get("phase"),
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
    "checkpoint-before-ack": lambda root: terminal_fault(root, "checkpoint-before-ack"),
    "final-checkpoint-before-terminal": lambda root: terminal_fault(
        root, "final-checkpoint-before-terminal"
    ),
    "lost-ack": lambda root: terminal_fault(root, "lost-ack"),
    "host-disconnect": lambda root: terminal_fault(root, "host-disconnect"),
    "typescript-disconnect": lambda root: terminal_fault(root, "typescript-disconnect"),
    "duplicate-ack": duplicate_ack,
    "stale-writer": stale_writer,
    "corrupt-checkpoint": corrupt_checkpoint,
    "source-ports": lambda root: runtime_ports(root, built=False),
    "built-ports": lambda root: runtime_ports(root, built=True),
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
