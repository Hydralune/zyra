from __future__ import annotations

from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from typing import Any, Iterator

import pytest


ROOT = Path(__file__).resolve().parents[2]
BUN = ROOT / "node_modules" / ".bin" / ("bun.exe" if os.name == "nt" else "bun")
PROBE = ROOT / "apps" / "web" / "test" / "product-live-cross-view-probe.ts"
LOCAL_FAILURE_PROBE = ROOT / "apps" / "cli" / "test" / "product-local-failure-probe.ts"


@contextmanager
def _real_api(tmp_path: Path) -> Iterator[tuple[str, Any]]:
    environment = {
        "ZYRA_SQLITE_PATH": str(tmp_path / "api.sqlite3"),
        "ZYRA_EVENT_LOG": str(tmp_path / "events.jsonl"),
        "ZYRA_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        "ZYRA_TOOL_WORKSPACE": str(tmp_path / "tool-workspace"),
        "ZYRA_PERMISSION_STATE": str(tmp_path / "permissions.json"),
        "ZYRA_WORKER_POOL_STORE": str(tmp_path / "worker-pool.sqlite3"),
        "ZYRA_GRAPH_STATE_STORE": str(tmp_path / "graph.sqlite3"),
        "ZYRA_WORKSPACE_STATE_ROOT": str(tmp_path / "workspace-state"),
        "ZYRA_WORKSPACE_DATA_ROOT": str(tmp_path / "workspace-data"),
        "ZYRA_CONTROL_STATE": str(tmp_path / "control"),
        "ZYRA_SUBAGENT_STATE": str(tmp_path / "subagents"),
    }
    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    for path in [
        ROOT,
        ROOT / "apps" / "api",
        *[
            ROOT / "packages" / name
            for name in (
                "core",
                "commands",
                "orchestration",
                "memory",
                "runtime",
                "integrations",
                "workers",
                "symbolic",
                "scheduler",
                "evaluation",
                "workspace",
                "code_index",
            )
        ],
    ]:
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from apps.api.zyra_api import main as api_main

    api_main = importlib.reload(api_main)
    api_main.reset_runtime_event_spine_bridge()
    api_main.reset_worker_pool_api()
    server = ThreadingHTTPServer(("127.0.0.1", 0), api_main.ZyraRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", api_main
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=30)
        api_main.reset_runtime_event_spine_bridge()
        api_main.reset_worker_pool_api()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _line_reader(stream: Any, output: Queue[str]) -> None:
    for line in stream:
        output.put(line.rstrip("\r\n"))


def _next_line(lines: Queue[str], label: str, timeout: float) -> str:
    try:
        return lines.get(timeout=timeout)
    except Empty as error:
        raise AssertionError(f"Timed out waiting for {label}.") from error


def test_web_consumes_live_product_text_and_reconciles_cli_canonical_final(
    tmp_path: Path,
) -> None:
    with _real_api(tmp_path) as (base_url, api_main):
        from zyra_core import ArtifactKind, PlanNodeStatus, now_iso
        from zyra_runtime.runtime_events import (
            CodeWorkerRuntimeEventIngress,
            WorkerIngressIdentity,
        )

        state = api_main.create_task_state(
            user_goal="Stream one exact answer and converge CLI and Web facts."
        )
        state.status = PlanNodeStatus.RUNNING
        state.plan_nodes[state.root_node_id].status = PlanNodeStatus.RUNNING
        state.metadata["query_session_id"] = f"task:{state.task_id}"
        state.metadata["goal_contract"] = {"kind": "direct_response"}
        manager = api_main.get_workspace_manager()
        artifacts = api_main.LocalArtifactStore(api_main.artifact_root_path())
        workspace = manager.create_for_task(
            run_id=state.run_id,
            task_id=state.task_id,
            session_id=f"task:{state.task_id}",
            worker_id="web-cross-view-diff-setup",
            idempotency_key="web-cross-view-workspace",
        )
        access = workspace.access
        edit = api_main.WorkspaceEditPort(
            manager,
            access,
            worker_id="web-cross-view-diff-setup",
            run_id=state.run_id,
            task_id=state.task_id,
            artifact_store=artifacts,
        )
        edit.write_text(
            "src/cross-view.txt",
            "before\n",
            idempotency_key="web-cross-view-diff-seed",
        )
        diff_artifact = artifacts.write_text(
            run_id=state.run_id,
            task_id=state.task_id,
            content=(
                "diff --git a/src/cross-view.txt b/src/cross-view.txt\n"
                "--- a/src/cross-view.txt\n"
                "+++ b/src/cross-view.txt\n"
                "@@ -1,1 +1,1 @@\n"
                "-before\n"
                "+after\n"
            ),
            title="cross-view.patch",
            kind=ArtifactKind.TEXT,
            extension=".patch",
            producer_node_id=state.root_node_id,
            metadata={
                "media_type": "text/x-diff",
                "content_family": "text",
                "encoding": "utf-8",
                "security_label": "internal",
                "trust_disposition": "trusted",
                "download_policy": "allow",
            },
        )
        state.artifacts.append(diff_artifact)
        api_main.get_store().save_checkpoint(state)

        ingress = CodeWorkerRuntimeEventIngress(
            api_main.get_runtime_event_spine_bridge(),
            WorkerIngressIdentity(
                run_id=state.run_id,
                task_id=state.task_id,
                session_id=f"task:{state.task_id}",
                worker_request_id="request-web-live-cross-view",
            ),
        )
        process = subprocess.Popen(
            [
                str(BUN),
                str(PROBE),
                base_url,
                state.task_id,
                diff_artifact.artifact_id,
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert process.stdout is not None
        assert process.stderr is not None
        lines: Queue[str] = Queue()
        reader = threading.Thread(
            target=_line_reader,
            args=(process.stdout, lines),
            daemon=True,
        )
        reader.start()
        try:
            assert _next_line(lines, "Web ingress readiness", 30) == "READY"
            ingress.admit_query(sequence=0)
            ingress.emit_payload(
                {
                    "schema": "zyra.provider-assistant-presentation/v1",
                    "phase": "assistant_text_started",
                    "sequence": 1,
                    "delta_kind": "assistant_text",
                    "stream_id": "provider:web-live-cross-view",
                    "assistant_message_id": "answer-web-live-cross-view",
                }
            )
            ingress.emit_payload(
                {
                    "schema": "zyra.provider-assistant-presentation/v1",
                    "phase": "assistant_text_delta",
                    "sequence": 2,
                    "delta_kind": "assistant_text",
                    "stream_id": "provider:web-live-cross-view",
                    "assistant_message_id": "answer-web-live-cross-view",
                    "content": "实时",
                    "segment_index": 1,
                }
            )
            ingress.emit_payload(
                {
                    "schema": "zyra.provider-assistant-presentation/v1",
                    "phase": "assistant_text_delta",
                    "sequence": 3,
                    "delta_kind": "assistant_text",
                    "stream_id": "provider:web-live-cross-view",
                    "assistant_message_id": "answer-web-live-cross-view",
                    "content": "回答\n",
                    "segment_index": 2,
                }
            )
            state.status = PlanNodeStatus.COMPLETED
            state.plan_nodes[state.root_node_id].status = PlanNodeStatus.COMPLETED
            state.metadata["final_answer"] = "实时回答\n"
            state.metadata["verification"] = {
                "final_verifier": {"passed": True, "label": "cross-view verified"},
                "completion_gate": {"passed": True},
            }
            state.updated_at = now_iso()
            api_main.get_store().save_checkpoint(state)
            ingress.emit_payload(
                {
                    "schema": "zyra.provider-assistant-presentation/v1",
                    "phase": "assistant_text_completed",
                    "sequence": 4,
                    "delta_kind": "assistant_text",
                    "stream_id": "provider:web-live-cross-view",
                    "assistant_message_id": "answer-web-live-cross-view",
                    "content": "实时回答\n",
                }
            )

            result = json.loads(_next_line(lines, "cross-view result", 60))
            return_code = process.wait(timeout=30)
            stderr = process.stderr.read()
            assert return_code == 0, stderr
            assert result["liveText"] == "实时回答\n"
            assert result["transientCleared"] is True
            assert result["verificationSame"] is True
            assert result["cli"] == result["web"]
            assert result["cli"]["status"] == "completed"
            assert result["cli"]["finalAnswer"] == "实时回答\n"
            assert result["ingress"]["phase"] == "live"
            assert result["ingress"]["projectedSequence"] >= 1
            assert result["diff"]["same"] is True
            assert result["diff"]["diffId"]
            assert result["diff"]["files"] == 1
            assert result["diff"]["physicalPathDisclosed"] is False
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)
            reader.join(timeout=5)


def test_real_tool_failure_remains_local_when_task_recovers_and_completes(
    tmp_path: Path,
) -> None:
    with _real_api(tmp_path) as (base_url, api_main):
        from zyra_core import PlanNodeStatus, now_iso
        from zyra_runtime.runtime_events import (
            CodeWorkerRuntimeEventIngress,
            WorkerIngressIdentity,
        )

        state = api_main.create_task_state(
            user_goal="Recover from one physical tool failure and complete."
        )
        state.status = PlanNodeStatus.RUNNING
        state.plan_nodes[state.root_node_id].status = PlanNodeStatus.RUNNING
        state.metadata["query_session_id"] = f"task:{state.task_id}"
        state.metadata["goal_contract"] = {"kind": "code_change"}
        api_main.get_store().save_checkpoint(state)

        ingress = CodeWorkerRuntimeEventIngress(
            api_main.get_runtime_event_spine_bridge(),
            WorkerIngressIdentity(
                run_id=state.run_id,
                task_id=state.task_id,
                session_id=f"task:{state.task_id}",
                worker_request_id="request-local-failure-recovery",
            ),
        )
        ingress.admit_query(sequence=0)
        ingress.emit_payload(
            {
                "phase": "tool_call_started",
                "sequence": 1,
                "tool_call_id": "tool-failed-once",
                "tool_name": "tests",
                "arguments": {"suite": "physical"},
            }
        )
        ingress.emit_payload(
            {
                "phase": "tool_call_completed",
                "sequence": 2,
                "tool_call_id": "tool-failed-once",
                "tool_name": "tests",
                "tool_result": {
                    "tool_call_id": "tool-failed-once",
                    "tool_name": "tests",
                    "ok": False,
                    "error": "exit_1",
                },
            }
        )
        ingress.emit_payload(
            {
                "phase": "tool_failure_signal",
                "sequence": 3,
                "tool_call_id": "tool-failed-once",
                "error": "exit_1",
            }
        )
        ingress.emit_payload(
            {
                "phase": "tool_call_started",
                "sequence": 4,
                "tool_call_id": "tool-retry",
                "tool_name": "tests",
                "arguments": {"suite": "physical", "retry": 1},
            }
        )
        ingress.emit_payload(
            {
                "phase": "tool_call_completed",
                "sequence": 5,
                "tool_call_id": "tool-retry",
                "tool_name": "tests",
                "tool_result": {
                    "tool_call_id": "tool-retry",
                    "tool_name": "tests",
                    "ok": True,
                    "output": "passed",
                },
            }
        )

        state.status = PlanNodeStatus.COMPLETED
        state.plan_nodes[state.root_node_id].status = PlanNodeStatus.COMPLETED
        state.metadata["final_answer"] = "Recovered after a local tool failure."
        state.updated_at = now_iso()
        api_main.get_store().save_checkpoint(state)

        process = subprocess.run(
            [str(BUN), str(LOCAL_FAILURE_PROBE), base_url, state.task_id],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert process.returncode == 0, process.stderr
        result = json.loads(process.stdout)
        assert result["taskStatus"] == "completed"
        assert result["taskFailedEvent"] is False
        assert result["renderedLocalFailure"] is True
        assert result["renderedCompleted"] is True
        assert result["failedTools"][0]["impact"] == "local"
        assert result["failedTools"][0]["code"] == "exit_1"
        assert len(result["completedTools"]) == 1
        assert result["recoveryActivities"][0]["impact"] == "local"
        assert "runtime.tool.failed" in result["frameTypes"]
        assert "runtime.recovery.requested" in result["frameTypes"]
        local_presentations = [
            item for item in result["presentations"] if item.get("impact") == "local"
        ]
        assert any(item["kind"] == "tool" for item in local_presentations)
        assert any(item.get("category") == "recovery" for item in local_presentations)
