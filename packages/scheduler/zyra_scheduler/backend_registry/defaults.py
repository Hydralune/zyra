from __future__ import annotations

from pathlib import Path

from .models import (
    BackendDefinition,
    BackendKind,
    BackendLocation,
    BackendResourceLimits,
    WorkspacePolicy,
)
from .registry import BackendRegistry


def default_backend_definitions() -> list[BackendDefinition]:
    return [
        BackendDefinition(
            backend_id="local-code-worker",
            display_name="Local Code Worker Host",
            kind=BackendKind.LOCAL_PROCESS,
            location=BackendLocation.LOCAL,
            runtime_worker="CodeWorkerRuntime",
            capabilities=("code-change", "shell", "artifact", "checkpoint"),
            workspace_policy=WorkspacePolicy(scope="task", isolation="workspace-manager"),
            limits=BackendResourceLimits(maximum_concurrency=4, turn_timeout_seconds=600.0),
            priority=100,
            metadata={"execution_mode": "in_process_callable", "real_local_dispatch": True},
        ),
        BackendDefinition(
            backend_id="local-code-worker-failover",
            display_name="Local Code Worker Failover Host",
            kind=BackendKind.LOCAL_PROCESS,
            location=BackendLocation.LOCAL,
            runtime_worker="CodeWorkerRuntime",
            capabilities=("code-change", "shell", "artifact", "checkpoint"),
            workspace_policy=WorkspacePolicy(scope="task", isolation="workspace-manager"),
            limits=BackendResourceLimits(maximum_concurrency=2, turn_timeout_seconds=600.0),
            priority=10,
            metadata={"execution_mode": "in_process_callable", "failover_only": True},
        ),
        BackendDefinition(
            backend_id="edge-browser-worker",
            display_name="Edge Browser Worker Compatibility Host",
            kind=BackendKind.LOCAL_PROCESS,
            location=BackendLocation.LOCAL,
            runtime_worker="BrowserWorker",
            capabilities=("browser-action", "screenshot", "dom-state"),
            workspace_policy=WorkspacePolicy(scope="artifact", isolation="browser-context"),
            limits=BackendResourceLimits(maximum_concurrency=2, turn_timeout_seconds=300.0),
            priority=100,
            metadata={
                "execution_mode": "in_process_callable",
                "edge_transport_pending_slice": "M1-S05D-02",
                "real_edge_dispatch_claimed": False,
            },
        ),
        BackendDefinition(
            backend_id="edge-browser-worker-failover",
            display_name="Edge Browser Worker Local Failover",
            kind=BackendKind.LOCAL_PROCESS,
            location=BackendLocation.LOCAL,
            runtime_worker="BrowserWorker",
            capabilities=("browser-action", "screenshot", "dom-state"),
            workspace_policy=WorkspacePolicy(scope="artifact", isolation="browser-context"),
            limits=BackendResourceLimits(maximum_concurrency=1, turn_timeout_seconds=300.0),
            priority=5,
            metadata={"execution_mode": "in_process_callable", "failover_only": True},
        ),
    ]


def ensure_default_backends(registry: BackendRegistry) -> None:
    existing = {item.backend_id for item in registry.definitions()}
    for definition in default_backend_definitions():
        if definition.backend_id not in existing:
            registry.register(definition)


def backend_registry_path(artifact_root: str | Path) -> Path:
    return Path(artifact_root).expanduser().resolve() / ".backend-registry" / "backend.sqlite3"
