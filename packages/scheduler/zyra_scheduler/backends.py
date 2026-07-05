from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zyra_core import TaskState, new_id, now_iso

from .models import WorkerManifest


@dataclass(slots=True)
class DispatchEnvelope:
    envelope_id: str
    run_id: str
    task_id: str
    node_id: str | None
    manifest_id: str
    runtime_worker: str
    backend: str
    location: str
    sandbox: str
    gateway: str
    workspace_root: str
    artifact_root: str
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)


class WorkerBackendGateway:
    """Backend/sandbox descriptor used before dispatching a selected runtime worker."""

    def __init__(self, workspace_root: str | Path, artifact_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.artifact_root = Path(artifact_root).resolve()

    def envelope(
        self,
        state: TaskState,
        manifest: WorkerManifest,
        *,
        node_id: str | None = None,
        decision_id: str = "",
    ) -> DispatchEnvelope:
        return DispatchEnvelope(
            envelope_id=new_id("dispatch"),
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=node_id,
            manifest_id=manifest.worker_id,
            runtime_worker=manifest.runtime_worker,
            backend=str(manifest.backend),
            location=str(manifest.location),
            sandbox=manifest.sandbox,
            gateway=manifest.gateway,
            workspace_root=str(self.workspace_root),
            artifact_root=str(self.artifact_root),
            metadata={
                "decision_id": decision_id,
                "workspace_scope": manifest.workspace_scope,
                "privacy_level": manifest.privacy_level,
                "source_modules": manifest.source_modules,
            },
        )


def build_dispatch_envelope(
    state: TaskState,
    manifest: WorkerManifest,
    *,
    workspace_root: str | Path,
    artifact_root: str | Path,
    node_id: str | None = None,
    decision_id: str = "",
) -> DispatchEnvelope:
    return WorkerBackendGateway(workspace_root, artifact_root).envelope(
        state,
        manifest,
        node_id=node_id,
        decision_id=decision_id,
    )
