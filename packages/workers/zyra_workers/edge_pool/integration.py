from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping

from zyra_scheduler.worker_pool.integration_models import PhysicalDispatchBinding
from zyra_scheduler.worker_pool.models import utc_iso

from .runtime import EdgeGatewayRequest, EdgeWorkerGatewayRuntime, EdgeWorkerRegistrationRuntime


@dataclass(frozen=True, slots=True)
class IntegratedEdgeRequest:
    operation: str
    input_payload: Mapping[str, Any]
    artifact_name: str
    job_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.operation.strip():
            raise ValueError("edge operation is empty")
        if not self.artifact_name.strip():
            raise ValueError("edge artifact name is empty")
        if not self.job_id.strip():
            raise ValueError("edge job id is empty")
        object.__setattr__(self, "input_payload", copy.deepcopy(dict(self.input_payload)))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        binding: PhysicalDispatchBinding,
    ) -> "IntegratedEdgeRequest":
        raw_payload = value.get("input_payload")
        input_payload = (
            dict(raw_payload)
            if isinstance(raw_payload, Mapping)
            else {
                key: copy.deepcopy(item)
                for key, item in value.items()
                if key not in {"operation", "artifact_name", "job_id", "metadata"}
            }
        )
        return cls(
            operation=str(value.get("operation") or "echo_artifact"),
            input_payload=input_payload,
            artifact_name=str(value.get("artifact_name") or f"{binding.attempt_id}.json"),
            job_id=str(
                value.get("job_id")
                or f"edge-job:{binding.attempt_id}:{binding.progress_sequence + 1}"
            ),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class EdgeIntegrationProjection:
    worker_id: str
    endpoint: str
    running: bool
    heartbeat_sequence: int
    manifest_digest: str
    active_lease_ids: tuple[str, ...]
    captured_at: str = field(default_factory=utc_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "endpoint": self.endpoint,
            "running": self.running,
            "heartbeat_sequence": self.heartbeat_sequence,
            "manifest_digest": self.manifest_digest,
            "active_lease_ids": list(self.active_lease_ids),
            "captured_at": self.captured_at,
        }


class IntegratedEdgeExecutionAdapter:
    """Maps 07A bindings to the in-repo edge gateway and child protocol."""

    def __init__(
        self,
        gateway: EdgeWorkerGatewayRuntime,
        registration: EdgeWorkerRegistrationRuntime,
    ) -> None:
        self.gateway = gateway
        self.registration = registration
        self._lock = threading.RLock()
        self._active_jobs: dict[str, tuple[str, str]] = {}

    def execute(
        self,
        binding: PhysicalDispatchBinding,
        payload: Mapping[str, Any],
        *,
        fence_token: str,
    ) -> Mapping[str, Any]:
        self._assert_binding(binding)
        request = IntegratedEdgeRequest.from_mapping(payload, binding)
        before = self._heartbeat()
        gateway_request = EdgeGatewayRequest(
            task_id=binding.task_id,
            run_id=binding.run_id,
            attempt_id=binding.attempt_id,
            lease_id=binding.lease_id,
            worker_id=binding.worker_id,
            fence_token=fence_token,
            fence_epoch=binding.fence_epoch,
            operation=request.operation,
            input_payload=request.input_payload,
            artifact_name=request.artifact_name,
            job_id=request.job_id,
            metadata={
                **dict(request.metadata),
                "integration_binding_id": binding.binding_id,
                "workspace_ref": binding.foreign_refs.workspace.object_id,
                "gateway_ref": binding.foreign_refs.gateway.object_id,
                "backend_route_ref": binding.foreign_refs.backend_route.object_id,
                "graph_ref": binding.foreign_refs.graph.to_dict(),
            },
        )
        with self._lock:
            if binding.binding_id in self._active_jobs:
                raise ValueError("edge binding already has an active gateway job")
            self._active_jobs[binding.binding_id] = (request.job_id, binding.lease_id)
        try:
            receipt = self.gateway.execute(gateway_request)
        finally:
            with self._lock:
                self._active_jobs.pop(binding.binding_id, None)
        after = self._heartbeat()
        if receipt.request.lease_id != binding.lease_id:
            raise ValueError("edge receipt changed the canonical lease identity")
        if receipt.request.attempt_id != binding.attempt_id:
            raise ValueError("edge receipt changed the canonical attempt identity")
        if not receipt.committed or not receipt.artifact_ref:
            raise ValueError("edge gateway did not commit a result artifact")
        return {
            "accepted": receipt.execution.outcome == "succeeded",
            "outcome": receipt.execution.outcome,
            "gateway_action_ref": f"edge-gateway:{receipt.request.job_id}",
            "artifact_refs": [receipt.artifact_ref],
            "artifact_digest": receipt.artifact_digest,
            "artifact_path": receipt.artifact_path,
            "telemetry": {
                "before": before.to_dict(),
                "after": after.to_dict(),
                "worker_process_pid": (
                    self.registration.connector.endpoint.pid
                    if self.registration.connector.endpoint is not None
                    else 0
                ),
                "protocol": "zyra-authenticated-tcp",
            },
            "gateway_receipt": receipt.to_dict(),
            "result": copy.deepcopy(dict(receipt.execution.artifact or {})),
            "causality": {
                "task_id": binding.task_id,
                "attempt_id": binding.attempt_id,
                "lease_id": binding.lease_id,
                "binding_id": binding.binding_id,
                "graph_ref": binding.foreign_refs.graph.to_dict(),
                "gateway_action_ref": f"edge-gateway:{receipt.request.job_id}",
                "artifact_ref": receipt.artifact_ref,
            },
        }

    def cancel(self, binding: PhysicalDispatchBinding, *, reason: str) -> bool:
        self._assert_binding(binding)
        with self._lock:
            active = self._active_jobs.get(binding.binding_id)
        if active is None:
            return False
        job_id, lease_id = active
        return self.gateway.cancel(
            job_id=job_id,
            lease_id=lease_id,
            reason=reason,
        )

    def projection(self) -> EdgeIntegrationProjection:
        worker_id = self.registration.connector.worker_id
        store = self.registration.lifecycle.store
        worker = store.require_worker(worker_id)
        heartbeat = store.latest_heartbeat(worker_id)
        manifest = store.latest_manifest(worker_id)
        active = tuple(item for item in store.list_leases(worker_id=worker_id) if not item.terminal)
        return EdgeIntegrationProjection(
            worker_id=worker_id,
            endpoint=worker.endpoint,
            running=self.registration.connector.running,
            heartbeat_sequence=heartbeat.sequence if heartbeat else 0,
            manifest_digest=manifest.digest if manifest else "",
            active_lease_ids=tuple(item.lease_id for item in active),
        )

    def _heartbeat(self) -> EdgeIntegrationProjection:
        worker_id = self.registration.connector.worker_id
        manifest = self.registration.lifecycle.store.latest_manifest(worker_id)
        if manifest is None:
            raise ValueError("edge worker manifest is unavailable")
        self.registration.observe_heartbeat(manifest.resource_capacity)
        return self.projection()

    def _assert_binding(self, binding: PhysicalDispatchBinding) -> None:
        if not binding.edge_only:
            raise ValueError("integrated edge adapter rejects a non-edge-only binding")
        if binding.worker_id != self.registration.connector.worker_id:
            raise ValueError("integrated edge binding addresses another worker")
        if binding.foreign_refs.gateway.owner != "M1-S05B.SandboxGatewayRuntime":
            raise ValueError("edge binding does not reference the canonical sandbox gateway owner")


__all__ = [
    "EdgeIntegrationProjection",
    "IntegratedEdgeExecutionAdapter",
    "IntegratedEdgeRequest",
]
