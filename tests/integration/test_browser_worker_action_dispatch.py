from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from zyra_core import EventRecord, EventType, to_jsonable
from zyra_runtime import (
    LocalArtifactStore,
    ToolCall,
    ToolExecutionContext,
    ToolExecutor,
    WorkerResult,
)
from zyra_runtime.e02_ports import TypeScriptPermissionReceiptPort
from zyra_workers import (
    BrowserWorkerActionDispatchPort,
    BrowserWorkerRun,
    find_browser_executable,
)
from zyra_workspace import (
    WorkspaceEditPort,
    WorkspaceManagerConfig,
    WorkspaceManagerRuntime,
)


class _CapturingBrowserRuntime:
    def __init__(self) -> None:
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        event = EventRecord(
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "browser_action": {
                    "action": "snapshot_state",
                    "worker": "BrowserWorkerRuntime",
                },
                "browser_result": {
                    "ok": True,
                    "output": {"url": "http://127.0.0.1:8765/", "title": "Portal"},
                },
            },
        )
        result = WorkerResult(
            request_id=request.request_id,
            ok=True,
            summary="BrowserWorker completed browser plan.",
            events=[to_jsonable(event)],
            metadata={
                "browser_backend": "zyra-browser-productized",
                "browser_session_id": "browser-session-proof",
            },
        )
        return BrowserWorkerRun(worker_result=result, event_records=[event])


def _permit(
    *,
    workspace: Path,
    call: ToolCall,
    worker_request_id: str,
) -> tuple[TypeScriptPermissionReceiptPort, object]:
    authority = TypeScriptPermissionReceiptPort(
        run_id=call.run_id,
        task_id=call.task_id,
        session_id=str(call.metadata["session_id"]),
        worker_request_id=worker_request_id,
        workspace_root=workspace,
    )
    permit = authority.accept(
        {
            "canonicalOwner": "typescript",
            "effect": "allow",
            "decisionId": "decision-browser-worker-dispatch",
            "continuationRequestId": "request-browser-worker-dispatch",
            "requestBinding": {
                "run_id": call.run_id,
                "task_id": call.task_id,
                "session_id": str(call.metadata["session_id"]),
                "session_revision": 1,
                "worker_request_id": worker_request_id,
                "tool_call_id": call.tool_call_id,
                "tool_name": call.tool_name,
                "namespace": "builtin",
                "server_id": "",
                "operation": "execute",
                "workspace_root": str(workspace.resolve()),
                "arguments_digest": "sha256:browser-worker-dispatch",
            },
            "finalArgumentsDigest": "sha256:browser-worker-dispatch",
            "finalArguments": call.arguments,
            "policyRevision": 1,
            "modeRevision": 1,
        },
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
        arguments=call.arguments,
        namespace="builtin",
        server_id="",
        operation="execute",
    )
    return authority, permit


def test_approved_browser_tool_delegates_to_browser_worker_and_exports_events(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    workspace.mkdir()
    runtime = _CapturingBrowserRuntime()
    port = BrowserWorkerActionDispatchPort(
        project_root=Path.cwd(),
        workspace_root=workspace,
        artifact_root=artifacts,
        state_root=tmp_path / "state",
        workspace_edit_port=object(),
        runtime=runtime,
    )
    call = ToolCall(
        run_id="run-browser-dispatch",
        task_id="task-browser-dispatch",
        node_id="node-browser-dispatch",
        tool_name="browser",
        tool_call_id="browser-call-1",
        arguments={
            "action": "extract_text",
            "url": "http://127.0.0.1:8765/portal.html",
            "allow_network": True,
        },
        metadata={"session_id": "code-worker-session"},
    )
    authority, permit = _permit(
        workspace=workspace,
        call=call,
        worker_request_id="code-worker-request",
    )
    executor = ToolExecutor(
        ToolExecutionContext.for_workspace(
            workspace,
            artifacts,
            runtime_services={"backend_action_dispatch_port": port},
        ),
        permission_authority=authority,
    )

    result = executor.execute(call, permission_grant=permit)

    assert result.ok, (result.error, result.metadata, result.output)
    assert result.output["execution_location"] == "BrowserWorkerRuntime"
    assert result.metadata["backend_action_dispatch_routed"] == "true"
    assert result.metadata["delegated_runtime"] == "BrowserWorkerRuntime"
    assert result.metadata["permission_execution_grant_consumed"] == "true"
    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.worker_name == "BrowserWorker"
    assert request.constraints["browser_backend"] == "zyra-browser-productized"
    assert request.constraints["browser_allow_unsafe_sandbox_bypass"] is (
        os.name == "nt"
    )
    assert request.constraints["browser_allow_loopback"] is True
    assert request.constraints["browser_allow_literal_ip"] is True
    assert request.constraints["browser_plan"] == [
        {
            "action": "open_url",
            "arguments": {"url": "http://127.0.0.1:8765/portal.html"},
        },
        {"action": "extract_text", "arguments": {"max_chars": 100_000}},
        {"action": "snapshot_state", "arguments": {}},
    ]
    exported_events = port.event_records()
    assert len(exported_events) == 1
    assert exported_events[0].payload["browser_action"]["worker"] == (
        "BrowserWorkerRuntime"
    )


def test_browser_worker_dispatch_rejects_unapproved_or_mismatched_receipt(
    tmp_path: Path,
) -> None:
    runtime = _CapturingBrowserRuntime()
    port = BrowserWorkerActionDispatchPort(
        project_root=Path.cwd(),
        workspace_root=tmp_path / "workspace",
        artifact_root=tmp_path / "artifacts",
        state_root=tmp_path / "state",
        workspace_edit_port=object(),
        runtime=runtime,
    )
    with pytest.raises(RuntimeError, match="exact approved browser receipt"):
        port.dispatch_action(
            run_id="run",
            task_id="task",
            node_id="node",
            tool_name="browser",
            tool_call_id="browser-call",
            arguments={"url": "http://127.0.0.1:8765/"},
            metadata={},
            permission_receipt={
                "receipt_id": "receipt",
                "tool_call_id": "different-call",
                "allowed": True,
            },
        )
    assert runtime.requests == []


def test_real_browser_worker_opens_loopback_portal_and_emits_page_state(
    tmp_path: Path,
) -> None:
    executable = find_browser_executable()
    if executable is None:
        pytest.skip("Chrome or Edge is not installed")

    class _PortalHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API.
            content = (
                b"<!doctype html><html><head><title>Supplier Portal</title></head>"
                b"<body><main>Recall status: S07 open incident</main></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _PortalHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    manager = WorkspaceManagerRuntime(
        WorkspaceManagerConfig(
            state_root=tmp_path / "workspace-state",
            data_root=tmp_path / "workspace-data",
        )
    )
    created = manager.create_for_task(
        run_id="run-browser-real",
        task_id="task-browser-real",
        session_id="session-browser-real",
        worker_id="physical-code-worker:test",
        idempotency_key="create-browser-real-workspace",
    )
    workspace = manager.internal_task_root(created.access)
    artifacts = tmp_path / "artifacts"
    edit_port = WorkspaceEditPort(
        manager,
        created.access,
        worker_id="physical-code-worker:test",
        run_id="run-browser-real",
        task_id="task-browser-real",
        node_id="node-browser-real",
        artifact_store=LocalArtifactStore(artifacts),
    )
    port = BrowserWorkerActionDispatchPort(
        project_root=Path.cwd(),
        workspace_root=workspace,
        artifact_root=artifacts,
        state_root=tmp_path / "browser-state",
        workspace_edit_port=edit_port,
    )
    try:
        result = port.dispatch_action(
            run_id="run-browser-real",
            task_id="task-browser-real",
            node_id="node-browser-real",
            tool_name="browser",
            tool_call_id="browser-real-call",
            arguments={
                "action": "extract_text",
                "url": f"http://127.0.0.1:{server.server_port}/portal.html",
                "allow_network": True,
                "allowed_domains": ["127.0.0.1"],
            },
            metadata={},
            permission_receipt={
                "receipt_id": "outer-browser-permission",
                "tool_call_id": "browser-real-call",
                "allowed": True,
            },
        )
    finally:
        port.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    tool_result = result["tool_result"]
    assert tool_result["ok"], (tool_result.get("error"), tool_result)
    assert tool_result["metadata"]["browser_backend"] == (
        "zyra-browser-productized"
    )
    events = port.event_records()
    assert any("browser_session" in item.payload for item in events)
    browser_events = [item for item in events if "browser_action" in item.payload]
    succeeded = [
        item
        for item in browser_events
        if isinstance(item.payload["browser_action"].get("transition"), dict)
        and item.payload["browser_action"]["transition"]["phase"] == "succeeded"
    ]
    assert len(succeeded) == 3
    assert len(
        {
            item.payload["browser_action"]["transition"]["action_id"]
            for item in succeeded
        }
    ) == 3
    serialized = str([to_jsonable(item) for item in events])
    assert "Supplier Portal" in serialized
    assert "Recall status" in serialized
