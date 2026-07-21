from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from zyra_scheduler.worker_pool.capabilities import (
    AgentToolConstraints,
    BackendCapability,
    CapabilityAttestor,
    NativeWorkerCapabilities,
    WorkerCapabilityComposer,
)
from zyra_scheduler.worker_pool.heartbeat import WorkerHeartbeatRuntime
from zyra_scheduler.worker_pool.leases import WorkerLeaseManager
from zyra_scheduler.worker_pool.lifecycle import WorkerLifecycleRuntime
from zyra_scheduler.worker_pool.models import (
    CapabilityAttestation,
    ExecutionOutcome,
    ExecutionReceipt,
    ResourceVector,
    WorkerHeartbeat,
    WorkerLocation,
    WorkerTelemetry,
    stable_digest,
    utc_iso,
)

from .connector import EdgeExecutionResult, EdgeProcessEndpoint, EdgeWorkerProcessConnector
from .protocol import PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class EdgeGatewayRequest:
    task_id: str
    run_id: str
    attempt_id: str
    lease_id: str
    worker_id: str
    fence_token: str
    fence_epoch: int
    operation: str
    input_payload: Mapping[str, Any]
    artifact_name: str
    job_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EdgeGatewayReceipt:
    request: EdgeGatewayRequest
    execution: EdgeExecutionResult
    artifact_ref: str
    artifact_path: str
    artifact_digest: str
    committed: bool
    completed_at: str = field(default_factory=utc_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.request.job_id,
            "task_id": self.request.task_id,
            "attempt_id": self.request.attempt_id,
            "lease_id": self.request.lease_id,
            "worker_id": self.request.worker_id,
            "operation": self.request.operation,
            "outcome": self.execution.outcome,
            "artifact_ref": self.artifact_ref,
            "artifact_path": self.artifact_path,
            "artifact_digest": self.artifact_digest,
            "committed": self.committed,
            "completed_at": self.completed_at,
        }


class EdgeWorkerGatewayRuntime:
    """05B-compatible gateway boundary for a real independent edge process.

    The parent process validates the canonical WorkerLease fence before dispatch,
    the edge process echoes the physical identity in its signed response, and only
    then does the parent commit the returned artifact below the Zyra artifact root.
    """

    def __init__(
        self,
        connector: EdgeWorkerProcessConnector,
        leases: WorkerLeaseManager,
        *,
        artifact_root: str | Path,
        policy: Callable[[EdgeGatewayRequest], bool] | None = None,
    ) -> None:
        self.connector = connector
        self.leases = leases
        self.artifact_root = Path(artifact_root).resolve()
        self.policy = policy or (lambda request: request.operation in {"echo_artifact", "hash_artifact", "json_transform", "wait"})

    def execute(self, request: EdgeGatewayRequest) -> EdgeGatewayReceipt:
        if request.worker_id != self.connector.worker_id:
            raise ValueError("edge gateway request addresses another worker")
        if not self.policy(request):
            raise PermissionError("edge gateway policy denied operation")
        lease = self.leases.assert_fence(
            request.lease_id,
            worker_id=request.worker_id,
            fence_token=request.fence_token,
            fence_epoch=request.fence_epoch,
            operation="edge_gateway_execute",
        )
        if lease.attempt_id != request.attempt_id or lease.task_id != request.task_id:
            raise ValueError("edge gateway request differs from canonical lease identity")
        self.leases.start_attempt(
            request.lease_id,
            worker_id=request.worker_id,
            fence_token=request.fence_token,
            fence_epoch=request.fence_epoch,
            backend_dispatch_id=request.job_id,
        )
        result = self.connector.execute(
            job_id=request.job_id,
            task_id=request.task_id,
            attempt_id=request.attempt_id,
            lease_id=request.lease_id,
            fence_epoch=request.fence_epoch,
            fence_token_digest=stable_digest(request.fence_token),
            operation=request.operation,
            input_payload=request.input_payload,
        )
        artifact_ref = ""
        artifact_path = ""
        artifact_digest = ""
        committed = False
        if result.artifact is not None:
            content = self._decode_artifact(result.artifact)
            artifact_digest = hashlib.sha256(content).hexdigest()
            if artifact_digest != str(result.artifact.get("sha256") or ""):
                raise ValueError("edge artifact digest differs from signed response")
            target = self._artifact_target(request)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            artifact_ref = f"artifact://edge/{request.task_id}/{target.name}"
            artifact_path = str(target)
            committed = True
        return EdgeGatewayReceipt(
            request=request,
            execution=result,
            artifact_ref=artifact_ref,
            artifact_path=artifact_path,
            artifact_digest=artifact_digest,
            committed=committed,
        )

    def finalize(self, receipt: EdgeGatewayReceipt) -> ExecutionReceipt:
        outcome = (
            ExecutionOutcome.SUCCEEDED
            if receipt.execution.outcome == "succeeded"
            else ExecutionOutcome.CANCELLED
            if receipt.execution.outcome == "cancelled"
            else ExecutionOutcome.FAILED
        )
        return self.leases.complete(
            receipt.request.lease_id,
            worker_id=receipt.request.worker_id,
            fence_token=receipt.request.fence_token,
            fence_epoch=receipt.request.fence_epoch,
            outcome=outcome,
            summary=f"edge gateway {receipt.request.operation} {receipt.execution.outcome}",
            artifact_refs=(receipt.artifact_ref,) if receipt.artifact_ref else (),
            gateway_receipt_ref=f"edge-gateway:{receipt.request.job_id}",
            metadata={"edge_gateway_receipt": receipt.to_dict()},
        )

    def cancel(self, *, job_id: str = "", lease_id: str = "", reason: str = "") -> bool:
        return self.connector.cancel(job_id=job_id, lease_id=lease_id, reason=reason)

    def _artifact_target(self, request: EdgeGatewayRequest) -> Path:
        safe_name = Path(request.artifact_name).name
        if not safe_name or safe_name in {".", ".."}:
            raise ValueError("edge artifact name is invalid")
        task_root = (self.artifact_root / "edge" / request.task_id).resolve()
        target = (task_root / safe_name).resolve()
        if task_root != target.parent and task_root not in target.parents:
            raise ValueError("edge artifact target escapes artifact root")
        return target

    @staticmethod
    def _decode_artifact(value: Mapping[str, Any]) -> bytes:
        if value.get("encoding") != "base64":
            raise ValueError("edge artifact encoding is unsupported")
        content = base64.b64decode(str(value.get("content") or ""), validate=True)
        if len(content) != int(value.get("size") or -1):
            raise ValueError("edge artifact size differs from response")
        return content


class EdgeWorkerRegistrationRuntime:
    def __init__(
        self,
        connector: EdgeWorkerProcessConnector,
        lifecycle: WorkerLifecycleRuntime,
        heartbeats: WorkerHeartbeatRuntime,
        *,
        attestor: CapabilityAttestor,
        composer: WorkerCapabilityComposer | None = None,
    ) -> None:
        self.connector = connector
        self.lifecycle = lifecycle
        self.heartbeats = heartbeats
        self.attestor = attestor
        self.composer = composer or WorkerCapabilityComposer()
        self.manifest = None
        self.endpoint = None

    def register(
        self,
        *,
        backend: BackendCapability,
        resources: ResourceVector,
        capabilities: tuple[str, ...] = ("edge_execution", "artifact_return", "agent_task"),
        tool_ids: tuple[str, ...] = ("edge.echo_artifact", "edge.hash_artifact", "edge.json_transform"),
        agent_constraints: AgentToolConstraints | None = None,
        replace_generation: bool = False,
    ) -> EdgeProcessEndpoint:
        endpoint = self.connector.start()
        composition = self.composer.compose(
            NativeWorkerCapabilities(
                worker_id=self.connector.worker_id,
                worker_kind="edge-process",
                location=WorkerLocation.EDGE,
                capabilities=capabilities,
                tool_ids=tool_ids,
                resources=resources,
                protocol_version=PROTOCOL_VERSION,
                labels={"transport": "zyra-authenticated-tcp", "process_isolation": "independent"},
            ),
            backends=(backend,),
            agent_constraints=agent_constraints,
        )
        manifest = composition.manifest
        challenge = self.attestor.challenge()
        response = self.connector.attest(manifest_digest=manifest.digest, challenge_nonce=challenge)
        attestation = CapabilityAttestation(
            worker_id=manifest.worker_id,
            manifest_digest=response.manifest_digest,
            process_identity=response.process_identity,
            challenge_nonce=response.challenge_nonce,
            response_digest=response.response_digest,
            protocol_version=response.protocol_version,
            endpoint=response.endpoint,
            metadata={"pid": response.pid, "platform": response.platform, "transport": "tcp"},
        )
        self.lifecycle.register(
            manifest,
            attestation,
            attestor=self.attestor,
            expected_challenge=challenge,
            backend_id=backend.backend_id,
            metadata={"edge_connector": "EdgeWorkerProcessConnector"},
            replace_generation=replace_generation,
        )
        self.lifecycle.start(manifest.worker_id)
        self.manifest = manifest
        self.endpoint = endpoint
        self.observe_heartbeat(resources)
        return endpoint

    def observe_heartbeat(self, capacity: ResourceVector) -> WorkerHeartbeat:
        if self.manifest is None or self.connector.endpoint is None:
            raise RuntimeError("edge worker is not registered")
        payload = self.connector.heartbeat()
        active_attempt_ids = tuple(str(item) for item in payload.get("active_attempt_ids") or ())
        telemetry = WorkerTelemetry(
            worker_id=self.manifest.worker_id,
            sequence=int(payload.get("sequence") or 1),
            capacity=capacity,
            allocated=ResourceVector(process_slots=len(active_attempt_ids)),
            active_attempt_ids=active_attempt_ids,
            queue_depth=int(payload.get("queue_depth") or 0),
            process_uptime_ms=int(payload.get("process_uptime_ms") or 0),
            load_average=float(payload.get("load_average") or 0),
            metadata={"pid": int(payload.get("pid") or 0), "edge_process": True},
        )
        heartbeat = WorkerHeartbeat(
            worker_id=self.manifest.worker_id,
            worker_generation=self.lifecycle.store.require_worker(self.manifest.worker_id).generation,
            sequence=telemetry.sequence,
            manifest_digest=self.manifest.digest,
            active_lease_ids=tuple(str(item) for item in payload.get("active_lease_ids") or ()),
            telemetry=telemetry,
            process_identity=self.connector.endpoint.process_identity,
        )
        self.heartbeats.observe(heartbeat)
        return heartbeat

    def drain(self) -> None:
        self.lifecycle.begin_drain(self.connector.worker_id, reason="edge connector drain requested")
        self.connector.drain()

    def stop(self) -> None:
        self.connector.stop()
        if self.lifecycle.store.get_worker(self.connector.worker_id) is not None:
            self.lifecycle.stop(self.connector.worker_id, force=True, reason="edge process stopped")
