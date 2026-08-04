from __future__ import annotations

import importlib
import json
import os
import subprocess
import tempfile
import threading
import urllib.request
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from apps.api.zyra_api.provider_backend_api import ProviderBackendApi
from zyra_scheduler.backend_registry import (
    BackendRegistry,
    BackendRegistryActionDispatchPort,
    BackendRegistryStore,
    backend_registry_path,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BUN = REPOSITORY_ROOT / "node_modules" / "bun" / "bin" / "bun.exe"
FIXTURE = (
    REPOSITORY_ROOT
    / "apps"
    / "cli"
    / "test"
    / "fixtures"
    / "terminal-node-process.ts"
)
LIFECYCLE_FIXTURE = (
    REPOSITORY_ROOT
    / "apps"
    / "cli"
    / "test"
    / "fixtures"
    / "terminal-lifecycle-process.ts"
)


@dataclass
class TerminalProcess:
    process: subprocess.Popen[str]
    projection: dict[str, Any]

    @classmethod
    def start(cls, root: Path) -> "TerminalProcess":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [str(BUN), str(FIXTURE), str(root)],
            cwd=REPOSITORY_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            creationflags=flags,
        )
        assert process.stdout is not None
        line = process.stdout.readline()
        if not line:
            stderr = "" if process.stderr is None else process.stderr.read()
            raise AssertionError(f"TypeScript terminal process did not start: {stderr}")
        projection = json.loads(line)
        assert projection["schema"] == "zyra.test-terminal-process/v1"
        assert projection["endpoint"].startswith("http://127.0.0.1:")
        return cls(process=process, projection=projection)

    def stop(self, *, abrupt: bool = False) -> None:
        if self.process.poll() is not None:
            return
        if abrupt:
            self.process.terminate()
        else:
            assert self.process.stdin is not None
            self.process.stdin.write("stop\n")
            self.process.stdin.flush()
        self.process.wait(timeout=10)


def _route() -> dict[str, object]:
    return {
        "route_id": "provider-route-typescript-terminal",
        "route_checksum": "sha256:typescript-terminal-route",
        "catalog_revision": 2,
        "credential_version": 3,
        "credential_fingerprint": "sha256:typescript-terminal-credential",
        "transport_id": "typescript-terminal-transport",
        "turn_id": "turn-typescript-terminal",
    }


def _revision(api: ProviderBackendApi) -> int:
    response = api.handle_get(("backends", "health"), {})
    assert response is not None and int(response.status) == 200
    return int(response.body["result"]["registry_revision"])


def _register(
    api: ProviderBackendApi,
    terminal: TerminalProcess,
    *,
    priority: int,
    enabled: bool = True,
) -> dict[str, Any]:
    projection = terminal.projection
    response = api.handle_post(
        ("backends",),
        {
            "expected_revision": _revision(api),
            "terminal_registration": {
                "generation": projection["generation"],
                "owner_id": projection["owner_id"],
                "capability_token": projection["capability_token"],
            },
            "backend": {
                "backend_id": projection["backend_id"],
                "display_name": "TypeScript CLI terminal integration",
                "kind": "edge_http",
                "location": "local",
                "runtime_worker": "CodeWorkerRuntime",
                "capabilities": ["artifact", "checkpoint", "code-change", "shell"],
                "endpoint": projection["endpoint"],
                "health_endpoint": f"{projection['endpoint']}/health",
                "command": [],
                "docker_image": None,
                "workspace_policy": {
                    "scope": "task",
                    "read_only": False,
                    "artifact_only": False,
                    "require_existing": True,
                    "require_writable": True,
                    "isolation": "workspace-manager",
                },
                "limits": {
                    "maximum_concurrency": 4,
                    "turn_timeout_seconds": 30,
                    "connect_timeout_seconds": 2,
                    "health_timeout_seconds": 1,
                    "memory_megabytes": None,
                    "cpu_millicores": None,
                },
                "enabled": enabled,
                "priority": priority,
                "cost_weight": 0,
                "latency_weight": 0,
                "metadata": {
                    "execution_mode": "terminal_http",
                    "terminal_protocol": "zyra.backend-transport-request/v1",
                    "real_terminal_dispatch_claimed": True,
                },
            },
        },
    )
    assert response is not None, "POST /backends was not routed"
    assert int(response.status) == 200, response.body
    assert response.body["schema"] == "zyra.provider-backend-api/v1"
    return dict(response.body["result"])


def _dispatch(
    registry_path: Path,
    workspace: Path,
    artifacts: Path,
    *,
    tool_call_id: str,
    target: str,
    content: str,
) -> dict[str, Any]:
    port = BackendRegistryActionDispatchPort(
        registry_path=registry_path,
        workspace_root=workspace,
        artifact_root=artifacts,
        route_resolver=_route,
    )
    return port.dispatch_action(
        run_id="run-typescript-terminal",
        task_id="task-typescript-terminal",
        node_id="node-typescript-terminal",
        tool_name="file_write",
        tool_call_id=tool_call_id,
        arguments={"path": target, "content": content},
        metadata={"session_id": "session-typescript-terminal"},
        permission_receipt={
            "receipt_id": f"permission-{tool_call_id}",
            "binding_id": f"binding-{tool_call_id}",
            "tool_call_id": tool_call_id,
            "allowed": True,
            "authority_type": "typescript.PermissionCoordinator",
        },
    )


def _terminal_health(terminal: TerminalProcess) -> dict[str, Any]:
    with urllib.request.urlopen(
        f"{terminal.projection['endpoint']}/health",
        timeout=5,
    ) as response:
        return json.loads(response.read().decode("utf-8"))


def test_typescript_terminal_real_registration_dispatch_and_failover() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        workspace = root / "workspace"
        artifacts = root / "artifacts"
        workspace.mkdir()
        artifacts.mkdir()
        registry_path = backend_registry_path(artifacts)
        api = ProviderBackendApi(project_root=REPOSITORY_ROOT, artifact_root=artifacts)
        primary = TerminalProcess.start(root)
        secondary = TerminalProcess.start(root)
        try:
            primary_receipt = _register(api, primary, priority=20_000)
            secondary_receipt = _register(api, secondary, priority=10_000)
            assert primary_receipt["backend_id"] == primary.projection["backend_id"]
            assert secondary_receipt["backend_id"] == secondary.projection["backend_id"]

            sealed_before = _terminal_health(primary)["terminal_dispatches"]
            sealed_port = BackendRegistryActionDispatchPort(
                registry_path=registry_path,
                workspace_root=workspace,
                artifact_root=artifacts,
                route_resolver=_route,
                terminal_dispatch_enabled=lambda: False,
            )
            assert sealed_port.available_actions() == ()
            with pytest.raises(ValueError, match="excluded by execution mode"):
                sealed_port.dispatch_action(
                    run_id="run-typescript-terminal-sealed",
                    task_id="task-typescript-terminal-sealed",
                    node_id="node-typescript-terminal-sealed",
                    tool_name="file_write",
                    tool_call_id="typescript-terminal-sealed",
                    arguments={"path": "sealed.txt", "content": "must not dispatch"},
                    metadata={},
                    permission_receipt={"receipt_id": "permission-sealed", "allowed": True},
                )
            assert _terminal_health(primary)["terminal_dispatches"] == sealed_before

            direct = _dispatch(
                registry_path,
                workspace,
                artifacts,
                tool_call_id="typescript-terminal-direct",
                target="direct.txt",
                content="real TypeScript HTTP dispatch",
            )
            direct_receipt = direct["dispatch_receipt"]
            assert direct_receipt["backend_id"] == primary.projection["backend_id"]
            assert direct_receipt["backend_kind"] == "edge_http"
            assert direct_receipt["backend_location"] == "local"
            assert direct_receipt["backend_lease_id"]
            assert direct_receipt["transport_receipt_ids"]
            assert direct["tool_result"]["metadata"]["execution_mode"] == "terminal_http"
            assert (workspace / "direct.txt").read_text(encoding="utf-8") == (
                "real TypeScript HTTP dispatch"
            )

            primary.stop(abrupt=True)
            # Model the stale-but-last-known-healthy record that exists between
            # an abrupt process death and the next real health probe.  This
            # deterministically forces the router to observe the connection
            # failure before it can select the second live terminal.
            store = BackendRegistryStore(registry_path)
            try:
                registry = BackendRegistry(store)
                registry.record_success(
                    primary.projection["backend_id"],
                    latency_milliseconds=0,
                )
                pre_failover = {
                    item.backend_id: {
                        "priority": item.priority,
                        "enabled": item.enabled,
                        "health": registry.health(item.backend_id).status.value,
                        "leases": registry.health(item.backend_id).current_leases,
                    }
                    for item in registry.definitions(runtime_worker="CodeWorkerRuntime")
                    if item.metadata.get("terminal_registration") is True
                }
            finally:
                store.close()
            failed_over = _dispatch(
                registry_path,
                workspace,
                artifacts,
                tool_call_id="typescript-terminal-failover",
                target="failover.txt",
                content="BACKEND_UNAVAILABLE to BACKEND_FAILOVER",
            )
            failover_receipt = failed_over["dispatch_receipt"]
            assert failover_receipt["backend_changed"] is True, json.dumps(
                {"receipt": failover_receipt, "pre": pre_failover},
                sort_keys=True,
            )
            assert failover_receipt["backend_id"] == secondary.projection["backend_id"]
            assert len(failover_receipt["attempt_ids"]) == 2
            assert (workspace / "failover.txt").read_text(encoding="utf-8") == (
                "BACKEND_UNAVAILABLE to BACKEND_FAILOVER"
            )

            store = BackendRegistryStore(registry_path)
            try:
                registry = BackendRegistry(store)
                events = store.events(
                    run_id="run-typescript-terminal",
                    task_id="task-typescript-terminal",
                )
                event_types = {event.event_type for event in events}
                assert "backend.dispatch.failed" in event_types
                assert "backend.failover.committed" in event_types
                failed_health = registry.health(primary.projection["backend_id"])
                assert failed_health.failure_count >= 1
                assert failed_health.status.value == "unavailable"
                assert failed_health.metadata["last_failure_kind"] == "backend_unavailable"
            finally:
                store.close()

            _register(api, primary, priority=20_000, enabled=False)
            _register(api, secondary, priority=10_000, enabled=False)
            health = api.handle_get(("backends", "health"), {})
            assert health is not None
            definitions = {
                item["backend_id"]: item for item in health.body["result"]["backends"]
            }
            assert definitions[primary.projection["backend_id"]]["enabled"] is False
            assert definitions[secondary.projection["backend_id"]]["enabled"] is False
            assert projection_token_absent(health.body, primary.projection["capability_token"])
            assert projection_token_absent(health.body, secondary.projection["capability_token"])
        finally:
            primary.stop(abrupt=True)
            secondary.stop()


def projection_token_absent(value: object, token: str) -> bool:
    return token not in json.dumps(value, ensure_ascii=False, sort_keys=True)


def test_cli_terminal_lifecycle_registers_and_disables_through_real_http(
    tmp_path: Path,
) -> None:
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
    from apps.api.zyra_api import main as api_main

    api_main = importlib.reload(api_main)
    api_main.reset_scenario_runner_api(wait=True)
    api_main.reset_runtime_event_spine_bridge()
    api_main.reset_worker_pool_api()
    server = ThreadingHTTPServer(("127.0.0.1", 0), api_main.ZyraRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        completed = subprocess.run(
            [str(BUN), str(LIFECYCLE_FIXTURE), base_url, str(tmp_path)],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert completed.returncode == 0, completed.stderr
        projection = json.loads(completed.stdout.strip())
        assert projection["schema"] == "zyra.test-terminal-lifecycle/v1"
        assert projection["enabled"]["enabled"] is True
        assert projection["status"]["registered"] is True
        assert projection["status"]["real_terminal_dispatch_claimed"] is True
        assert projection["disabled"]["enabled"] is False
        assert projection["enabled"]["backend_id"] == projection["disabled"]["backend_id"]

        with urllib.request.urlopen(f"{base_url}/backends/health", timeout=10) as response:
            registry = json.loads(response.read().decode("utf-8"))["result"]
        definition = next(
            item
            for item in registry["backends"]
            if item["backend_id"] == projection["enabled"]["backend_id"]
        )
        assert definition["kind"] == "edge_http"
        assert definition["location"] == "local"
        assert definition["enabled"] is False
        assert definition["metadata"]["execution_mode"] == "terminal_http"
        assert "real_edge_dispatch_claimed" not in definition["metadata"]
        assert definition["endpoint"] is None
        assert definition["health_endpoint"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=30)
        api_main.reset_scenario_runner_api(wait=True)
        api_main.reset_runtime_event_spine_bridge()
        api_main.reset_worker_pool_api()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
