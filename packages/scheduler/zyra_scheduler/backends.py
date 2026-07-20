from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from zyra_core import TaskState, new_id, now_iso

from .models import WorkerManifest
from zyra_runtime.sandbox_gateway.integration_remote import GatewayDispatchAttestor


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
    provider_route_id: str = ""
    gateway_receipt_id: str = ""
    gateway_receipt_digest: str = ""
    gateway_policy_digest: str = ""
    gateway_envelope_digest: str = ""
    gateway_receipt_expires_at: float = 0.0
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)


class WorkerBackendGateway:
    """Backend/sandbox descriptor used before dispatching a selected runtime worker."""

    def __init__(
        self,
        workspace_root: str | Path,
        artifact_root: str | Path,
        *,
        gateway_attestor: GatewayDispatchAttestor | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.artifact_root = Path(artifact_root).resolve()
        self.gateway_attestor = gateway_attestor or GatewayDispatchAttestor.for_workspace(
            workspace_root=self.workspace_root,
            artifact_root=self.artifact_root,
        )

    def envelope(
        self,
        state: TaskState,
        manifest: WorkerManifest,
        *,
        node_id: str | None = None,
        decision_id: str = "",
        provider_route_id: str = "",
    ) -> DispatchEnvelope:
        envelope = DispatchEnvelope(
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
            provider_route_id=str(provider_route_id or ""),
            metadata={
                "decision_id": decision_id,
                "workspace_scope": manifest.workspace_scope,
                "privacy_level": manifest.privacy_level,
                "source_modules": manifest.source_modules,
                "provider_route_is_opaque_reference": bool(provider_route_id),
                "provider_state_embedded": False,
            },
        )
        attestation = self.gateway_attestor.attest(
            {
                **asdict(envelope),
                "decision_id": decision_id,
            }
        )
        envelope.gateway_receipt_id = attestation.receipt.receipt_id
        envelope.gateway_receipt_digest = attestation.receipt.receipt_digest
        envelope.gateway_policy_digest = attestation.receipt.policy_digest
        envelope.gateway_envelope_digest = attestation.envelope_digest
        envelope.gateway_receipt_expires_at = attestation.receipt.expires_at
        envelope.metadata.update(
            {
                "sandbox_gateway_attested": True,
                "gateway_receipt_id": attestation.receipt.receipt_id,
                "gateway_receipt_digest": attestation.receipt.receipt_digest,
                "gateway_policy_digest": attestation.receipt.policy_digest,
                "gateway_envelope_digest": attestation.envelope_digest,
            }
        )
        return envelope


def build_dispatch_envelope(
    state: TaskState,
    manifest: WorkerManifest,
    *,
    workspace_root: str | Path,
    artifact_root: str | Path,
    node_id: str | None = None,
    decision_id: str = "",
    provider_route_id: str = "",
) -> DispatchEnvelope:
    return WorkerBackendGateway(workspace_root, artifact_root).envelope(
        state,
        manifest,
        node_id=node_id,
        decision_id=decision_id,
        provider_route_id=provider_route_id,
    )
