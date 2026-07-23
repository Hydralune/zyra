from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from zyra_scheduler.worker_pool import (
    BackendCapability,
    CapabilityAttestor,
    CapabilityRequirement,
    ExecutionOutcome,
    NativeWorkerCapabilities,
    ResourceVector,
    WorkerHeartbeat,
    WorkerLocation,
    WorkerPoolFoundationRuntime,
    WorkerTelemetry,
)
from zyra_workers.edge_pool import (
    EdgeGatewayRequest,
    EdgeWorkerGatewayRuntime,
    EdgeWorkerProcessConnector,
    EdgeWorkerRegistrationRuntime,
)

from .contracts import utc_now
from .integration_contracts import stable_digest
from .live_evidence import EndpointAttestation, LiveTier
from .managed_provider import ManagedProviderReceipt


class ExecutionTierError(RuntimeError):
    """A real local/edge/cloud execution tier could not be attested."""


@dataclass(frozen=True, slots=True)
class ExecutionTierRunReceipt:
    run_id: str
    attestations: tuple[EndpointAttestation, ...]
    receipt_path: str
    receipt_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "attestations": [item.to_dict() for item in self.attestations],
            "receipt_path": self.receipt_path,
            "receipt_digest": self.receipt_digest,
        }


class ExecutionTierProbeSuite:
    """Exercise local, isolated edge process and cloud-model dispatch for one run."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()

    def execute(
        self,
        *,
        artifact_root: str | Path,
        cloud_provider: ManagedProviderReceipt,
        edge_host: str = "",
        run_id: str = "",
    ) -> ExecutionTierRunReceipt:
        selected_run_id = run_id or f"m1-live-tier-{uuid4().hex}"
        root = Path(artifact_root).resolve() / "execution-tiers" / selected_run_id
        root.mkdir(parents=True, exist_ok=False)
        secret = os.urandom(32)
        pool = WorkerPoolFoundationRuntime(
            root / "worker-pool.sqlite3",
            attestation_secret=secret,
            default_lease_ttl_seconds=120,
        )
        local = self._local(pool, root, selected_run_id)
        edge = self._edge(
            pool,
            root,
            selected_run_id,
            secret=secret,
            host=edge_host or discover_non_loopback_ipv4(),
        )
        cloud = self._cloud(pool, root, cloud_provider, selected_run_id)
        attestations = (local, edge, cloud)
        payload = {
            "schema": "zyra.execution-tier-run/v1",
            "run_id": selected_run_id,
            "attestations": [item.to_dict() for item in attestations],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        path = root / "execution-tier-receipt.json"
        path.write_text(encoded, encoding="utf-8")
        return ExecutionTierRunReceipt(
            run_id=selected_run_id,
            attestations=attestations,
            receipt_path=str(path),
            receipt_digest=hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    @staticmethod
    def _local(
        pool: WorkerPoolFoundationRuntime,
        root: Path,
        run_id: str,
    ) -> EndpointAttestation:
        started_at = utc_now()
        worker_id = "m1-live-local-worker"
        backend = BackendCapability(
            backend_id="m1-live-local-gateway",
            backend_kind="sandbox_gateway",
            enabled=True,
            healthy=True,
            capabilities=("local_execution", "artifact_return", "agent_task"),
            tool_ids=("local.json_transform",),
            constraints={"sealed_capable": True, "gateway_owner": "SandboxGatewayRuntime"},
            labels={"dispatch_location": "local"},
        )
        registration = pool.register_local_worker(
            worker_id=worker_id,
            worker_kind="code-worker",
            backend=backend,
            capabilities=("local_execution", "artifact_return", "agent_task"),
            tool_ids=("local.json_transform",),
            resources=ResourceVector(cpu_cores=1, memory_mb=256, process_slots=1),
            metadata={"m1_live_tier": True},
        )
        pool.heartbeat_local_worker(worker_id, sequence=1)
        task_id = "m1-live-local-task"
        acquisition = pool.acquire_task(
            task_id=task_id,
            run_id=run_id,
            owner_session_id=f"session:{run_id}",
            requirement=CapabilityRequirement(
                required=("local_execution",),
                locations=(WorkerLocation.LOCAL,),
                resources=ResourceVector(memory_mb=32, process_slots=1),
            ),
            preferred_worker_ids=(worker_id,),
        )
        request = {
            "operation": "json_transform",
            "value": {"tier": "local", "run_id": run_id, "task_id": task_id},
        }
        artifact = root / "local" / "result.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        transformed = {
            "value": request["value"],
            "digest": stable_digest(request["value"]),
        }
        artifact.write_text(
            json.dumps(transformed, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        artifact_id = "artifact-local-" + hashlib.sha256(artifact.read_bytes()).hexdigest()[:20]
        pool.leases.start_attempt(
            acquisition.lease.lease_id,
            worker_id=worker_id,
            fence_token=acquisition.lease.fence_token,
            fence_epoch=acquisition.lease.fence_epoch,
            backend_dispatch_id=f"local:{task_id}",
        )
        receipt = pool.leases.complete(
            acquisition.lease.lease_id,
            worker_id=worker_id,
            fence_token=acquisition.lease.fence_token,
            fence_epoch=acquisition.lease.fence_epoch,
            outcome=ExecutionOutcome.SUCCEEDED,
            summary="local JSON transform completed",
            artifact_refs=(artifact_id,),
            gateway_receipt_ref=f"local-gateway:{artifact_id}",
        )
        health = pool.heartbeats.assess(worker_id)
        return EndpointAttestation(
            tier=LiveTier.LOCAL,
            endpoint=f"local://{socket.gethostname()}/{os.getpid()}",
            endpoint_id=f"local-endpoint:{worker_id}",
            runtime_id=worker_id,
            process_id=registration.process_identity,
            host_id=socket.gethostname(),
            isolation_id=f"local-process:{os.getpid()}",
            request_id=f"local-request:{task_id}",
            route_id=acquisition.lease.backend_id,
            lease_id=acquisition.lease.lease_id,
            artifact_ids=(artifact_id,),
            started_at=started_at,
            completed_at=utc_now(),
            request_digest=stable_digest(request),
            response_digest=stable_digest(receipt.to_dict()),
            handshake_ok=registration.manifest is not None,
            heartbeat_ok=health.status.value in {"healthy", "degraded"},
            task_success=receipt.outcome is ExecutionOutcome.SUCCEEDED,
            protocol="zyra-local-worker/v1",
            transport="in-process-terminal",
            simulated=False,
            loopback=False,
            metadata={
                "artifact_path": str(artifact),
                "worker_location": WorkerLocation.LOCAL.value,
                "receipt_id": receipt.receipt_id,
            },
        )

    def _edge(
        self,
        pool: WorkerPoolFoundationRuntime,
        root: Path,
        run_id: str,
        *,
        secret: bytes,
        host: str,
    ) -> EndpointAttestation:
        started_at = utc_now()
        worker_id = "m1-live-edge-worker"
        connector = EdgeWorkerProcessConnector(
            worker_id=worker_id,
            secret=secret,
            package_roots=self._package_roots(),
            host=host,
            startup_timeout_seconds=15,
            request_timeout_seconds=30,
        )
        registration = EdgeWorkerRegistrationRuntime(
            connector,
            pool.lifecycle,
            pool.heartbeats,
            attestor=CapabilityAttestor(secret),
        )
        try:
            endpoint = registration.register(
                backend=BackendCapability(
                    backend_id="m1-live-edge-gateway",
                    backend_kind="sandbox_gateway",
                    enabled=True,
                    healthy=True,
                    capabilities=("edge_execution", "artifact_return", "agent_task"),
                    tool_ids=("edge.json_transform",),
                    constraints={"sealed_capable": True},
                    labels={"dispatch_location": "edge"},
                ),
                resources=ResourceVector(cpu_cores=1, memory_mb=256, process_slots=1),
            )
            task_id = "m1-live-edge-task"
            acquisition = pool.acquire_task(
                task_id=task_id,
                run_id=run_id,
                owner_session_id=f"session:{run_id}",
                requirement=CapabilityRequirement(
                    required=("edge_execution",),
                    locations=(WorkerLocation.EDGE,),
                    resources=ResourceVector(memory_mb=32, process_slots=1),
                ),
                preferred_worker_ids=(worker_id,),
            )
            request_value = {"tier": "edge", "run_id": run_id, "task_id": task_id}
            gateway = EdgeWorkerGatewayRuntime(
                connector,
                pool.leases,
                artifact_root=root / "edge" / "artifacts",
            )
            gateway_receipt = gateway.execute(
                EdgeGatewayRequest(
                    task_id=task_id,
                    run_id=run_id,
                    attempt_id=acquisition.attempt.attempt_id,
                    lease_id=acquisition.lease.lease_id,
                    worker_id=worker_id,
                    fence_token=acquisition.lease.fence_token,
                    fence_epoch=acquisition.lease.fence_epoch,
                    operation="json_transform",
                    input_payload={"value": request_value},
                    artifact_name="edge-result.json",
                    job_id=f"edge-job:{task_id}",
                )
            )
            receipt = gateway.finalize(gateway_receipt)
            heartbeat = registration.observe_heartbeat(
                ResourceVector(cpu_cores=1, memory_mb=256, process_slots=1)
            )
            process_identity = (
                connector.endpoint.process_identity if connector.endpoint is not None else ""
            )
            manifest = pool.store.latest_manifest(worker_id)
            return EndpointAttestation(
                tier=LiveTier.EDGE,
                endpoint=endpoint.endpoint,
                endpoint_id=f"edge-endpoint:{worker_id}",
                runtime_id=worker_id,
                process_id=process_identity or f"pid:{endpoint.pid}",
                host_id=host,
                isolation_id=(
                    f"edge-process:{process_identity or endpoint.pid}:"
                    f"{manifest.digest if manifest is not None else 'unknown'}"
                ),
                request_id=f"edge-request:{task_id}",
                route_id=acquisition.lease.backend_id,
                lease_id=acquisition.lease.lease_id,
                artifact_ids=(gateway_receipt.artifact_ref,),
                started_at=started_at,
                completed_at=utc_now(),
                request_digest=stable_digest(request_value),
                response_digest=stable_digest(gateway_receipt.to_dict()),
                handshake_ok=bool(process_identity),
                heartbeat_ok=heartbeat.sequence > 0,
                task_success=receipt.outcome is ExecutionOutcome.SUCCEEDED,
                protocol="zyra-authenticated-edge-tcp/v1",
                transport="tcp+hmac",
                simulated=False,
                loopback=_is_loopback_host(host),
                metadata={
                    "artifact_path": gateway_receipt.artifact_path,
                    "worker_location": WorkerLocation.EDGE.value,
                    "worker_pid": endpoint.pid,
                    "manifest_digest": manifest.digest if manifest is not None else "",
                    "receipt_id": receipt.receipt_id,
                },
            )
        finally:
            registration.stop()

    @staticmethod
    def _cloud(
        pool: WorkerPoolFoundationRuntime,
        root: Path,
        provider: ManagedProviderReceipt,
        run_id: str,
    ) -> EndpointAttestation:
        item = provider.attestation
        if item.response_status < 200 or item.response_status >= 300:
            raise ExecutionTierError("cloud provider turn did not succeed")
        started_at = item.started_at
        worker_id = f"m1-live-cloud-{item.provider_id}"
        backend = BackendCapability(
            backend_id=f"m1-live-cloud-{item.provider_id}-gateway",
            backend_kind="provider_gateway",
            enabled=True,
            healthy=True,
            capabilities=("cloud_execution", "artifact_return", "provider_tool_roundtrip"),
            tool_ids=("cloud.read_only_tool",),
            constraints={
                "sealed_capable": True,
                "credential_custody": "provider_cli",
                "dialect": item.dialect.value,
            },
            labels={
                "dispatch_location": "cloud",
                "provider_id": item.provider_id,
                "model_id": item.model_id,
            },
        )
        composition = pool.composer.compose(
            NativeWorkerCapabilities(
                worker_id=worker_id,
                worker_kind="provider-worker",
                location=WorkerLocation.CLOUD,
                capabilities=("cloud_execution", "artifact_return", "provider_tool_roundtrip"),
                tool_ids=("cloud.read_only_tool",),
                resources=ResourceVector(cpu_cores=1, memory_mb=256, process_slots=1),
                labels={"transport": "provider-owned-managed-cli"},
            ),
            backends=(backend,),
        )
        manifest = composition.manifest
        challenge = pool.attestor.challenge()
        process_identity = f"provider-cli-{provider.command_id}"
        capability_attestation = pool.attestor.issue(
            worker_id=worker_id,
            process_identity=process_identity,
            manifest_digest=manifest.digest,
            challenge_nonce=challenge,
            endpoint=item.endpoint,
            protocol_version=manifest.protocol_version,
            metadata={
                "provider_id": item.provider_id,
                "model_id": item.model_id,
                "attempt_id": item.attempt_id,
            },
        )
        worker = pool.lifecycle.register(
            manifest,
            capability_attestation,
            attestor=pool.attestor,
            expected_challenge=challenge,
            backend_id=backend.backend_id,
            owner_session_id=f"session:{run_id}",
            metadata={"m1_live_tier": True, "provider_receipt": provider.trace_digest},
        )
        worker = pool.lifecycle.start(worker.worker_id)
        pool.heartbeats.observe(
            WorkerHeartbeat(
                worker_id=worker.worker_id,
                worker_generation=worker.generation,
                sequence=1,
                manifest_digest=manifest.digest,
                active_lease_ids=(),
                telemetry=WorkerTelemetry(
                    worker_id=worker.worker_id,
                    sequence=1,
                    capacity=manifest.resource_capacity,
                    allocated=ResourceVector(),
                    observed=ResourceVector(),
                    metadata={
                        "provider_request_id": item.request_id,
                        "authenticated_session": True,
                    },
                ),
                process_identity=process_identity,
            )
        )
        task_id = f"m1-live-cloud-task-{item.provider_id}"
        acquisition = pool.acquire_task(
            task_id=task_id,
            run_id=run_id,
            owner_session_id=f"session:{run_id}",
            requirement=CapabilityRequirement(
                required=("cloud_execution", "provider_tool_roundtrip"),
                locations=(WorkerLocation.CLOUD,),
                resources=ResourceVector(memory_mb=32, process_slots=1),
            ),
            preferred_worker_ids=(worker_id,),
        )
        cloud_root = root / "cloud"
        cloud_root.mkdir(parents=True, exist_ok=True)
        artifact = cloud_root / f"{item.provider_id}-provider-result.json"
        artifact_payload = {
            "schema": "zyra.cloud-provider-result/v1",
            "run_id": run_id,
            "task_id": task_id,
            "provider_id": item.provider_id,
            "model_id": item.model_id,
            "request_id": item.request_id,
            "attempt_id": item.attempt_id,
            "tool_call_ids": list(item.tool_call_ids),
            "tool_result_ids": list(item.tool_result_ids),
            "trace_digest": provider.trace_digest,
            "response_digest": item.response_digest,
        }
        artifact.write_text(
            json.dumps(artifact_payload, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        artifact_id = "artifact-cloud-" + hashlib.sha256(artifact.read_bytes()).hexdigest()[:20]
        pool.leases.start_attempt(
            acquisition.lease.lease_id,
            worker_id=worker_id,
            fence_token=acquisition.lease.fence_token,
            fence_epoch=acquisition.lease.fence_epoch,
            backend_dispatch_id=f"provider:{item.attempt_id}",
        )
        receipt = pool.leases.complete(
            acquisition.lease.lease_id,
            worker_id=worker_id,
            fence_token=acquisition.lease.fence_token,
            fence_epoch=acquisition.lease.fence_epoch,
            outcome=ExecutionOutcome.SUCCEEDED,
            summary="authenticated provider tool roundtrip completed",
            artifact_refs=(artifact_id,),
            backend_receipt_ref=item.request_id,
            gateway_receipt_ref=f"provider-gateway:{provider.trace_digest}",
        )
        health = pool.heartbeats.assess(worker_id)
        return EndpointAttestation(
            tier=LiveTier.CLOUD,
            endpoint=item.endpoint,
            endpoint_id=f"cloud-endpoint:{item.provider_id}",
            runtime_id=f"cloud-model:{item.model_id}",
            process_id=process_identity,
            host_id=item.provider_id,
            isolation_id=f"provider-attempt:{item.attempt_id}:{manifest.digest}",
            request_id=item.request_id,
            route_id=acquisition.lease.backend_id,
            lease_id=acquisition.lease.lease_id,
            artifact_ids=(artifact_id,),
            started_at=started_at,
            completed_at=item.completed_at,
            request_digest=item.request_digest,
            response_digest=stable_digest(receipt.to_dict()),
            handshake_ok=bool(item.metadata.get("authenticated_session")) and bool(manifest.digest),
            heartbeat_ok=health.status.value in {"healthy", "degraded"},
            task_success=receipt.outcome is ExecutionOutcome.SUCCEEDED,
            protocol=item.dialect.value,
            transport="provider-owned-managed-cli",
            simulated=False,
            loopback=False,
            metadata={
                "provider_id": item.provider_id,
                "model_id": item.model_id,
                "tool_call_count": len(item.tool_call_ids),
                "trace_path": provider.trace_path,
                "run_id": run_id,
                "artifact_path": str(artifact),
                "receipt_id": receipt.receipt_id,
                "worker_location": WorkerLocation.CLOUD.value,
                "provider_route_id": item.route_id,
            },
        )

    def _package_roots(self) -> tuple[Path, ...]:
        packages = self.project_root / "packages"
        return tuple(path for path in packages.iterdir() if path.is_dir())


def discover_non_loopback_ipv4() -> str:
    candidates: list[str] = []
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = str(item[4][0])
            if address not in candidates:
                candidates.append(address)
    except socket.gaierror:
        pass
    for address in candidates:
        if not address.startswith(("127.", "169.254.")) and address != "0.0.0.0":
            return address
    raise ExecutionTierError(
        "no non-loopback IPv4 address is available for the isolated edge endpoint"
    )


def _is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(host, None, socket.AF_UNSPEC)
                if item[4]
            }
        except socket.gaierror:
            return False
        return bool(addresses) and all(ipaddress.ip_address(item).is_loopback for item in addresses)


__all__ = [
    "ExecutionTierError",
    "ExecutionTierProbeSuite",
    "ExecutionTierRunReceipt",
    "discover_non_loopback_ipv4",
]
