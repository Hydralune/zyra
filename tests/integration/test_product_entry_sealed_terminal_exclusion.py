from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from zyra_scheduler.backend_registry import (
    BackendDefinition,
    BackendKind,
    BackendLocation,
    BackendRegistry,
    BackendRegistryStore,
    backend_registry_path,
    dispatch_worker_callable,
)


def test_sealed_dispatch_excludes_registered_terminal_backend() -> None:
    with TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        workspace = root / "workspace"
        artifacts = root / "artifacts"
        workspace.mkdir()
        artifacts.mkdir()
        terminal_backend_id = "terminal_backend_sealed_exclusion"

        store = BackendRegistryStore(backend_registry_path(artifacts))
        try:
            registry = BackendRegistry(store)
            registry.register(
                BackendDefinition(
                    backend_id=terminal_backend_id,
                    display_name="Terminal backend excluded from sealed runs",
                    kind=BackendKind.EDGE_HTTP,
                    location=BackendLocation.LOCAL,
                    runtime_worker="CodeWorkerRuntime",
                    capabilities=("code-change",),
                    endpoint="http://127.0.0.1:43123/capability/opaque-terminal-token",
                    priority=10_000,
                    metadata={"terminal_registration": True},
                )
            )
            assert registry.terminal_backend_ids() == (terminal_backend_id,)
        finally:
            store.close()

        def execute(envelope):
            if envelope.backend_id == terminal_backend_id:
                raise ConnectionError("terminal backend selected")
            return envelope.backend_id

        shared = {
            "run_id": "run-terminal-exclusion",
            "task_id": "task-terminal-exclusion",
            "node_id": "node-terminal-exclusion",
            "runtime_worker": "CodeWorkerRuntime",
            "preferred_backend_id": None,
            "workspace_root": workspace,
            "artifact_root": artifacts,
            "provider_route_id": "provider-route-terminal-exclusion",
            "provider_route_checksum": "route-checksum-terminal-exclusion",
            "provider_catalog_revision": 1,
            "provider_credential_version": 1,
            "provider_credential_fingerprint": "credential-terminal-exclusion",
            "provider_transport_id": "transport-terminal-exclusion",
            "m0_execution_ref": "worker_request:terminal-exclusion",
            "turn_id": "turn-terminal-exclusion",
            "operation": execute,
            "required_capabilities": ("code-change",),
        }

        sealed = dispatch_worker_callable(
            **shared,
            idempotency_key="terminal-exclusion:sealed",
            exclude_terminal_backends=True,
        )
        assert sealed.final_lease.backend_id != terminal_backend_id
        assert terminal_backend_id not in {item.backend_id for item in sealed.attempts}
        assert terminal_backend_id in sealed.final_lease.reason
        assert "excluded backends" in sealed.final_lease.reason

        interactive = dispatch_worker_callable(
            **shared,
            idempotency_key="terminal-exclusion:interactive",
            exclude_terminal_backends=False,
        )
        assert interactive.attempts[0].backend_id == terminal_backend_id
        assert interactive.final_lease.backend_id != terminal_backend_id
        assert interactive.backend_changed is True
