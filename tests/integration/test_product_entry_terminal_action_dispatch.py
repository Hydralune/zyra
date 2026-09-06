from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

from zyra_core import create_task_state
from zyra_runtime import LocalArtifactStore
from zyra_runtime.e02_ports import TypeScriptPermissionReceiptPort
from zyra_runtime.executor import ToolCall, ToolExecutionContext, ToolExecutor
from zyra_runtime.sandbox_gateway.integration_factory import build_gateway_runtime_bundle
from zyra_runtime.sandbox_gateway.integration_tools import GatewayToolExecutionRouter
from zyra_scheduler.backend_registry import (
    BackendDefinition,
    BackendKind,
    BackendLocation,
    BackendRegistry,
    BackendRegistryActionDispatchPort,
    BackendRegistryStore,
    BackendResourceLimits,
    BackendSelectionRequest,
    WorkerDispatchRouter,
    WorkspacePolicy,
    WorkspaceAttestationRuntime,
    backend_registry_path,
    ensure_default_backends,
)
from zyra_workers.backend_dispatch_service import (
    BackendDispatchHttpServer,
    BackendDispatchServiceRuntime,
    BackendOperationRegistry,
    BackendServiceConfig,
)
from zyra_workspace import WorkspaceEditPort, WorkspaceManagerConfig, WorkspaceManagerRuntime


def _route() -> dict[str, object]:
    return {
        "route_id": "provider-route-terminal-action",
        "route_checksum": "sha256:provider-route-terminal-action",
        "catalog_revision": 3,
        "credential_version": 2,
        "credential_fingerprint": "sha256:terminal-action-credential",
        "transport_id": "terminal-action-test-transport",
        "turn_id": "turn-terminal-action",
    }


class _TerminalActionFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.artifacts = self.root / "artifacts"
        self.workspace.mkdir()
        self.artifacts.mkdir()
        self.observed: list[dict[str, object]] = []
        operations = BackendOperationRegistry()

        def action(context, payload):
            context.check_cancelled()
            captured = dict(payload)
            self.observed.append(captured)
            return {
                "schema": "zyra.terminal-action-result/v1",
                "tool_call_id": str(payload["tool_call_id"]),
                "ok": True,
                "summary": f"terminal executed {payload['tool_name']}",
                "output": {
                    "execution_location": "terminal",
                    "tool_name": str(payload["tool_name"]),
                },
                "metadata": {"terminal_generation": "test-generation"},
            }

        for tool_name in (
            "file_read",
            "file_write",
            "file_edit",
            "file_delete",
            "web_search",
            "shell",
            "artifact_write",
        ):
            operations.register(f"tool.{tool_name}", action)
        operations.register(
            "worker.run",
            lambda _context, _payload: {
                "unexpected": "terminal worker.run must remain unreachable"
            },
        )
        runtime = BackendDispatchServiceRuntime(
            BackendServiceConfig(
                backend_id="terminal-action-test",
                runtime_worker="CodeWorkerRuntime",
                maximum_concurrency=4,
                capabilities=("code-change", "shell", "artifact", "checkpoint"),
                workspace_roots=(str(self.workspace),),
                artifact_roots=(str(self.artifacts),),
                generation="test-generation",
            ),
            operations,
        )
        self.server = BackendDispatchHttpServer(runtime).start()
        self.registry_path = backend_registry_path(self.artifacts)
        store = BackendRegistryStore(self.registry_path)
        try:
            registry = BackendRegistry(store)
            ensure_default_backends(registry)
            registry.register(
                BackendDefinition(
                    backend_id="terminal-action-test",
                    display_name="Terminal action test",
                    kind=BackendKind.EDGE_HTTP,
                    location=BackendLocation.LOCAL,
                    runtime_worker="CodeWorkerRuntime",
                    capabilities=("code-change", "shell", "artifact", "checkpoint"),
                    endpoint=self.server.endpoint,
                    health_endpoint=f"{self.server.endpoint}/health",
                    workspace_policy=WorkspacePolicy(require_existing=True),
                    limits=BackendResourceLimits(
                        maximum_concurrency=4,
                        turn_timeout_seconds=10,
                        connect_timeout_seconds=2,
                        health_timeout_seconds=2,
                    ),
                    priority=10_000,
                    metadata={
                        "terminal_registration": True,
                        "terminal_generation": "test-generation",
                        "allowed_hosts": ("127.0.0.1",),
                        "execution_mode": "terminal_http",
                    },
                )
            )
        finally:
            store.close()
        self.action_port = BackendRegistryActionDispatchPort(
            registry_path=self.registry_path,
            workspace_root=self.workspace,
            artifact_root=self.artifacts,
            route_resolver=_route,
        )

    def close(self) -> None:
        self.server.close()
        self.temporary.cleanup()


def test_worker_callable_excludes_terminal_while_typed_actions_select_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _TerminalActionFixture()
    try:
        store = BackendRegistryStore(fixture.registry_path)
        try:
            registry = BackendRegistry(store)
            request = BackendSelectionRequest(
                run_id="run-callable-exclusion",
                task_id="task-callable-exclusion",
                node_id="node-callable-exclusion",
                runtime_worker="CodeWorkerRuntime",
                preferred_backend_id="terminal-action-test",
                required_capabilities=("shell",),
                allowed_locations=(BackendLocation.LOCAL,),
                excluded_backend_ids=(),
                workspace_root=str(fixture.workspace),
                artifact_root=str(fixture.artifacts),
                provider_route_id=str(_route()["route_id"]),
                provider_route_checksum=str(_route()["route_checksum"]),
                provider_catalog_revision=int(_route()["catalog_revision"]),
                provider_credential_version=int(_route()["credential_version"]),
                provider_credential_fingerprint=str(_route()["credential_fingerprint"]),
                provider_transport_id=str(_route()["transport_id"]),
                m0_execution_ref="worker_request:callable-exclusion",
                turn_id=str(_route()["turn_id"]),
            )
            callable_outcome = WorkerDispatchRouter(registry).dispatch_callable(
                request,
                lambda envelope: {"execution_location": envelope.backend_kind.value},
                idempotency_key="worker-callable-terminal-exclusion",
            )
        finally:
            store.close()
        assert callable_outcome.final_lease.backend_id != "terminal-action-test"
        assert callable_outcome.final_envelope.backend_kind is BackendKind.LOCAL_PROCESS
        assert fixture.observed == []

        def reject_live_tree_scan(_runtime, root):
            raise AssertionError(f"terminal action enumerated the live workspace: {root}")

        monkeypatch.setattr(WorkspaceAttestationRuntime, "_scan", reject_live_tree_scan)

        for index, (tool_name, arguments) in enumerate(
            (
                ("file_read", {"path": "proof.txt"}),
                ("file_write", {"path": "proof.txt", "content": "proof"}),
                (
                    "file_edit",
                    {"path": "proof.txt", "old": "proof", "new": "updated"},
                ),
                ("file_delete", {"path": "proof.txt"}),
                ("web_search", {"query": "proof", "paths": ["."]}),
                ("shell", {"executable": sys.executable, "argv": ["-V"]}),
                ("artifact_write", {"name": "proof.md", "content": "proof"}),
            ),
            start=1,
        ):
            tool_call_id = f"terminal-action-{index}"
            dispatched = fixture.action_port.dispatch_action(
                run_id="run-terminal-action",
                task_id="task-terminal-action",
                node_id="node-terminal-action",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                arguments=arguments,
                metadata={"safe": "projection", "authorization_token": "must-not-cross"},
                permission_receipt={
                    "receipt_id": f"permission-{index}",
                    "binding_id": f"binding-{index}",
                    "tool_call_id": tool_call_id,
                    "allowed": True,
                    "authority_type": "typescript.PermissionCoordinator",
                },
            )
            receipt = dispatched["dispatch_receipt"]
            assert receipt["backend_id"] == "terminal-action-test"
            assert receipt["backend_kind"] == "edge_http"
            assert receipt["backend_location"] == "local"
            assert receipt["backend_lease_id"]
            assert receipt["envelope_id"]
            assert receipt["transport_receipt_ids"]
            assert dispatched["tool_result"]["output"]["execution_location"] == "terminal"

        assert [item["tool_name"] for item in fixture.observed] == [
            "file_read",
            "file_write",
            "file_edit",
            "file_delete",
            "web_search",
            "shell",
            "artifact_write",
        ]
        assert all(item["permission"]["allowed"] is True for item in fixture.observed)
        assert all("authorization_token" not in item["metadata"] for item in fixture.observed)

        sealed_port = BackendRegistryActionDispatchPort(
            registry_path=fixture.registry_path,
            workspace_root=fixture.workspace,
            artifact_root=fixture.artifacts,
            route_resolver=_route,
            terminal_dispatch_enabled=lambda: False,
        )
        assert sealed_port.available("shell") is False
        with pytest.raises(ValueError, match="excluded by execution mode"):
            sealed_port.dispatch_action(
                run_id="run-terminal-action",
                task_id="task-terminal-action",
                node_id="node-terminal-action",
                tool_name="shell",
                tool_call_id="terminal-action-sealed",
                arguments={"executable": sys.executable, "argv": ["-V"]},
                metadata={},
                permission_receipt={
                    "receipt_id": "permission-sealed",
                    "allowed": True,
                },
            )
    finally:
        fixture.close()


def test_api_code_worker_binding_discovers_registered_terminal_action_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _TerminalActionFixture()
    try:
        monkeypatch.setenv("ZYRA_ARTIFACT_ROOT", str(fixture.artifacts))
        from apps.api.zyra_api.main import _code_worker_backend_action_dispatch_port

        state = create_task_state("Bind the product CodeWorker action lane")
        state.metadata["provider_route"] = _route()
        port = _code_worker_backend_action_dispatch_port(
            state,
            workspace_root=fixture.workspace,
        )
        assert port is not None
        assert "shell" in port.available_actions()
        result = port.dispatch_action(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            tool_name="shell",
            tool_call_id="api-binding-terminal-action",
            arguments={"executable": sys.executable, "argv": ["-V"]},
            metadata={"session_id": f"session-{state.task_id}"},
            permission_receipt={
                "receipt_id": "api-binding-permission",
                "allowed": True,
            },
        )
        assert result["dispatch_receipt"]["backend_id"] == "terminal-action-test"
        assert result["tool_result"]["output"]["execution_location"] == "terminal"
    finally:
        fixture.close()


def test_gateway_permission_then_http_dispatch_and_delegation_mutation() -> None:
    fixture = _TerminalActionFixture()
    manager = WorkspaceManagerRuntime(
        WorkspaceManagerConfig(
            state_root=fixture.root / "workspace-state",
            data_root=fixture.workspace / "managed",
        )
    )
    state = create_task_state("Dispatch an approved command to a terminal backend")
    created = manager.create_for_task(
        run_id=state.run_id,
        task_id=state.task_id,
        session_id=f"session-{state.task_id}",
        worker_id="CodeWorkerRuntime",
        idempotency_key="create-terminal-action-workspace",
    )
    port = WorkspaceEditPort(
        manager,
        created.access,
        worker_id="CodeWorkerRuntime",
        run_id=state.run_id,
        task_id=state.task_id,
        node_id=state.root_node_id,
        artifact_store=LocalArtifactStore(fixture.artifacts),
    )
    managed_workspace = manager.internal_task_root(created.access)
    fixture.action_port.workspace_root = managed_workspace

    class Authority:
        @staticmethod
        def validate_and_consume(_call, _grant, _execution_context):
            return True

    arguments = {
        "executable": sys.executable,
        "argv": [
            "-c",
            "from pathlib import Path; Path('local-marker.txt').write_text('local')",
        ],
    }
    try:
        delegated_bundle = build_gateway_runtime_bundle(
            workspace_root=managed_workspace,
            artifact_root=fixture.artifacts / "delegated",
            worker_id="CodeWorkerRuntime",
            workspace_edit_port=port,
            runtime_services={
                "sandbox_gateway_required": True,
                "backend_action_dispatch_port": fixture.action_port,
            },
        )
        delegated_router = GatewayToolExecutionRouter(delegated_bundle)
        read = delegated_router.execute(
            ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                tool_name="file_read",
                tool_call_id="gateway-terminal-file-read",
                arguments={"path": "remote-proof.txt"},
                metadata={"session_id": f"terminal-action-{state.task_id}"},
            ),
            permission_grant=None,
            permission_authority=None,
            permission_execution_context={},
        )
        assert read.ok, (read.error, read.metadata, read.output)
        assert read.output["execution_location"] == "terminal"
        assert read.output["backend_action_dispatch_receipt"]["backend_lease_id"]
        assert read.output["gateway_receipt"]["permission_consumption_id"]
        assert read.metadata["permission_execution_grant_consumed"] == "true"

        for index, (tool_name, tool_arguments) in enumerate(
            (
                ("file_write", {"path": "remote-proof.txt", "content": "remote"}),
                ("file_edit", {"path": "remote-only.txt", "old": "before", "new": "after"}),
                (
                    "artifact_write",
                    {"name": "remote-proof.md", "content": "remote artifact"},
                ),
            ),
            start=1,
        ):
            action = delegated_router.execute(
                ToolCall(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    tool_name=tool_name,
                    tool_call_id=f"gateway-terminal-{tool_name}-{index}",
                    arguments=tool_arguments,
                    metadata={"session_id": f"terminal-action-{state.task_id}"},
                ),
                permission_grant={"grant_id": f"terminal-{tool_name}-grant"},
                permission_authority=Authority(),
                permission_execution_context={},
            )
            assert action.ok, (action.error, action.metadata, action.output)
            if tool_name == "artifact_write":
                assert action.artifacts
                artifact = action.artifacts[0]
                artifact_store = LocalArtifactStore(fixture.artifacts)
                artifact_store.verify(artifact)
                assert artifact_store.resolve_path(artifact).read_text() == "remote artifact"
            else:
                assert action.output["execution_location"] == "terminal"
                assert action.output["backend_action_dispatch_receipt"]["backend_lease_id"]
            assert action.output["gateway_receipt"]["permission_consumption_id"]
            assert action.output["gateway_receipt"]["outcome"] == "committed"

        delegated = delegated_router.execute(
            ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                tool_name="shell",
                tool_call_id="gateway-terminal-action",
                arguments=arguments,
                metadata={"session_id": f"terminal-action-{state.task_id}"},
            ),
            permission_grant={"grant_id": "terminal-action-grant"},
            permission_authority=Authority(),
            permission_execution_context={},
        )
        assert delegated.ok, (delegated.error, delegated.metadata, delegated.output)
        assert delegated.output["execution_location"] == "terminal"
        receipt = delegated.output["backend_action_dispatch_receipt"]
        assert receipt["permission_receipt_id"]
        assert receipt["backend_lease_id"]
        assert receipt["envelope_id"]
        assert receipt["transport_receipt_ids"]
        assert delegated.output["gateway_receipt"]["permission_consumption_id"]
        assert delegated.output["gateway_receipt"]["outcome"] == "committed"
        assert delegated.metadata["permission_execution_grant_consumed"] == "true"
        assert not (managed_workspace / "local-marker.txt").exists()

        local_bundle = build_gateway_runtime_bundle(
            workspace_root=managed_workspace,
            artifact_root=fixture.artifacts / "local-control",
            worker_id="CodeWorkerRuntime",
            workspace_edit_port=port,
            runtime_services={"sandbox_gateway_required": True},
        )
        local = GatewayToolExecutionRouter(local_bundle).execute(
            ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                tool_name="shell",
                tool_call_id="gateway-local-control",
                arguments=arguments,
                metadata={"session_id": f"local-control-{state.task_id}"},
            ),
            permission_grant={"grant_id": "local-control-grant"},
            permission_authority=Authority(),
            permission_execution_context={},
        )
        assert local.ok, (local.error, local.metadata, local.output)
        assert local.metadata.get("backend_action_dispatch_routed") is None
        assert (managed_workspace / "local-marker.txt").read_text() == "local"
    finally:
        fixture.close()


def test_tool_executor_delegates_permission_approved_search() -> None:
    fixture = _TerminalActionFixture()
    try:
        run_id = "run-terminal-search"
        task_id = "task-terminal-search"
        session_id = "session-terminal-search"
        worker_request_id = "worker-request-terminal-search"
        call = ToolCall(
            run_id=run_id,
            task_id=task_id,
            node_id="node-terminal-search",
            tool_name="web_search",
            tool_call_id="terminal-search-call",
            arguments={"query": "terminal dispatch", "paths": ["."]},
            metadata={"session_id": session_id},
        )
        authority = TypeScriptPermissionReceiptPort(
            run_id=run_id,
            task_id=task_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            workspace_root=fixture.workspace,
        )
        arguments_digest = "sha256:terminal-search-authorized-arguments"
        permit = authority.accept(
            {
                "canonicalOwner": "typescript",
                "effect": "allow",
                "decisionId": "decision-terminal-search",
                "continuationRequestId": "request-terminal-search",
                "requestBinding": {
                    "run_id": run_id,
                    "task_id": task_id,
                    "session_id": session_id,
                    "session_revision": 1,
                    "worker_request_id": worker_request_id,
                    "tool_call_id": call.tool_call_id,
                    "tool_name": call.tool_name,
                    "namespace": "builtin",
                    "server_id": "",
                    "operation": "execute",
                    "workspace_root": str(fixture.workspace.resolve()),
                    "arguments_digest": arguments_digest,
                },
                "finalArgumentsDigest": arguments_digest,
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
        executor = ToolExecutor(
            ToolExecutionContext.for_workspace(
                fixture.workspace,
                fixture.artifacts,
                runtime_services={
                    "backend_action_dispatch_port": fixture.action_port,
                },
            ),
            permission_authority=authority,
        )
        result = executor.execute(call, permission_grant=permit)
        assert result.ok, (result.error, result.metadata, result.output)
        assert result.output["execution_location"] == "terminal"
        receipt = result.output["backend_action_dispatch_receipt"]
        assert receipt["backend_lease_id"]
        assert receipt["transport_receipt_ids"]
        assert result.metadata["permission_execution_grant_consumed"] == "true"
    finally:
        fixture.close()
