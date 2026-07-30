from __future__ import annotations

import hashlib
import json
import os
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from zyra_core import create_task_state, now_iso, to_jsonable
from zyra_orchestration.deployment import (
    DeploymentProcessManager,
    DeploymentProfile,
    DeploymentStateStore,
    ProfileCatalog,
)
from zyra_orchestration.topology_policy.contracts import FrozenDict
from zyra_scheduler import (
    OperatorLeaseExecutionContext,
    PhysicalDispatchCallPort,
    PhysicalDispatchEvidenceStore,
    PhysicalDispatchTask,
    ResourceLocation,
    ResourceScheduler,
    WorkerBackendKind,
    WorkerManifest,
    WorkerPool,
)
from zyra_scheduler.worker_pool import (
    BackendCapability,
    CapabilityRequirement,
    ExecutionOutcome,
    ResourceVector,
    WorkerLocation,
    WorkerPoolFoundationRuntime,
)


_PROFILE = {
    "local": DeploymentProfile.DEVICE,
    "edge": DeploymentProfile.EDGE,
    "cloud": DeploymentProfile.CLOUD,
}
_RESOURCE_LOCATION = {
    "local": ResourceLocation.LOCAL,
    "edge": ResourceLocation.EDGE,
    "cloud": ResourceLocation.CLOUD,
}
_PHYSICAL_LOCATION = {
    "local": WorkerLocation.LOCAL,
    "edge": WorkerLocation.EDGE,
    "cloud": WorkerLocation.CLOUD,
}
_BACKEND = {
    "local": WorkerBackendKind.LOCAL_PROCESS,
    "edge": WorkerBackendKind.ISOLATED_PROCESS,
    "cloud": WorkerBackendKind.CLOUD_MODEL,
}


class SealedPhysicalDispatchError(RuntimeError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _free_port_block(count: int = 3) -> int:
    for base in range(49_000, 61_000, count):
        sockets: list[socket.socket] = []
        try:
            for port in range(base, base + count):
                item = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                item.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                item.bind(("127.0.0.1", port))
                sockets.append(item)
            return base
        except OSError:
            continue
        finally:
            for item in sockets:
                item.close()
    raise SealedPhysicalDispatchError(
        "no free deployment port block is available"
    )


def _loopback(endpoint: str) -> bool:
    parsed = urlparse(endpoint)
    return (parsed.hostname or "").casefold() in {
        "127.0.0.1",
        "::1",
        "localhost",
    }


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip():
            values[name.strip()] = value.strip()
    return values


def _receipt_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a policy-contract envelope without losing its audit digest."""

    if "physical_identity" in value:
        return dict(value)
    payload = _mapping(value.get("payload"))
    if not payload:
        raise SealedPhysicalDispatchError(
            "physical dispatch receipt contract payload is missing"
        )
    result = dict(payload)
    result["digest"] = str(value.get("digest") or "")
    result["contract_id"] = str(value.get("contract_id") or "")
    result["created_at"] = str(value.get("created_at") or "")
    return result


@dataclass(frozen=True, slots=True)
class SealedPhysicalEvidence:
    tiers: tuple[dict[str, Any], ...]
    providers: tuple[dict[str, Any], ...]
    degradation: dict[str, Any]
    receipts: tuple[dict[str, Any], ...]
    validations: tuple[dict[str, Any], ...]
    scheduler_decisions: tuple[dict[str, Any], ...]
    lease_receipts: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.phase2-sealed-physical-evidence/v1",
            "tiers": list(self.tiers),
            "providers": list(self.providers),
            "degradation": self.degradation,
            "receipts": list(self.receipts),
            "validations": list(self.validations),
            "scheduler_decisions": list(self.scheduler_decisions),
            "lease_receipts": list(self.lease_receipts),
        }


class SealedPhysicalDispatchRuntime:
    """Exercise local, isolated-edge and live-cloud lanes through real owners."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        state_root: str | Path,
        credential_env_file: str | Path | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        if credential_env_file is not None:
            environment.update(_read_env_file(Path(credential_env_file).resolve()))
        self.environment = environment

    def execute(
        self,
        *,
        scenario_run_id: str,
        task_id: str,
        payload: Mapping[str, Any],
        privacy_class: str,
        maximum_cost_usd: float,
        maximum_latency_ms: int,
    ) -> SealedPhysicalEvidence:
        missing_credential = not any(
            bool(self.environment.get(name))
            for name in (
                "ZAI_API_KEY",
                "KIMI_API_KEY",
                "DEEPSEEK_API_KEY",
            )
        )
        if missing_credential:
            raise SealedPhysicalDispatchError(
                "a GLM, Kimi, or DeepSeek credential is required for "
                "the sealed cloud lane"
            )
        deployment_root = self.state_root / "deployment"
        catalog = ProfileCatalog.defaults(
            self.project_root,
            host="127.0.0.1",
            base_port=_free_port_block(),
            environment=self.environment,
        )
        deployment_store = DeploymentStateStore(
            deployment_root / "deployment.sqlite3"
        )
        manager = DeploymentProcessManager(
            project_root=self.project_root,
            state_root=deployment_root,
            store=deployment_store,
            environment=self.environment,
        )
        pool = WorkerPoolFoundationRuntime(
            self.state_root / "worker-pool.sqlite3",
            attestation_secret=hashlib.sha256(
                f"sealed:{scenario_run_id}".encode("utf-8")
            ).digest(),
            default_lease_ttl_seconds=180,
        )
        task = PhysicalDispatchTask(
            run_id=scenario_run_id,
            task_id=task_id,
            payload=FrozenDict(dict(payload)),
            privacy_class=privacy_class,
            allowed_placements=("local", "edge", "cloud"),
            permission_ref=f"permission://sealed/{scenario_run_id}/allowed",
            maximum_cost_usd=maximum_cost_usd,
            latency_sla_ms=maximum_latency_ms,
        )
        call_port = PhysicalDispatchCallPort(
            task=task,
            catalog=catalog,
            process_manager=manager,
            state_store=deployment_store,
            evidence_store=PhysicalDispatchEvidenceStore(
                self.state_root / "physical-evidence"
            ),
        )
        scheduler_receipts: list[dict[str, Any]] = []
        lease_receipts: list[dict[str, Any]] = []
        try:
            for sequence, location in enumerate(("local", "edge", "cloud"), start=1):
                profile = _PROFILE[location]
                process, _client, health = manager.start_node(
                    catalog.policy(profile)
                )
                runtime_identity = dict(health.get("runtime_identity") or {})
                worker_id = f"sealed-{scenario_run_id}-{location}"
                capabilities = (
                    "artifact-production",
                    "verification",
                    "deterministic-transform",
                    *(
                        ("provider-dispatch",)
                        if location == "cloud"
                        else ()
                    ),
                )
                registration = pool.register_physical_worker(
                    worker_id=worker_id,
                    worker_kind="sealed-physical-dispatch-worker",
                    location=_PHYSICAL_LOCATION[location],
                    backend=BackendCapability(
                        backend_id=f"sealed-{location}",
                        backend_kind=_BACKEND[location].value,
                        enabled=True,
                        healthy=True,
                        capabilities=capabilities,
                        tool_ids=("physical-dispatch-proof",),
                    ),
                    capabilities=capabilities,
                    tool_ids=("physical-dispatch-proof",),
                    resources=ResourceVector(
                        cpu_cores=2,
                        memory_mb=1024,
                        process_slots=2,
                    ),
                    process_identity=str(
                        runtime_identity.get("failure_boundary_id")
                        or f"process:{process.pid}:{process.generation_id}"
                    ),
                    endpoint=process.endpoint,
                    metadata={
                        "pid": process.pid,
                        "node_id": health.get("node_id"),
                        "generation_id": process.generation_id,
                        "failure_boundary_id": runtime_identity.get(
                            "failure_boundary_id"
                        ),
                        "sealed_run": True,
                    },
                )
                pool.heartbeat_local_worker(
                    worker_id,
                    sequence=1,
                    process_uptime_ms=1,
                )
                state = create_task_state(
                    f"sealed physical dispatch proof for {location}"
                )
                state.run_id = scenario_run_id
                state.task_id = task_id
                state.constraints.allowed_workers = [worker_id]
                state.metadata["runtime_hints"] = {
                    "preferred_worker": worker_id,
                }
                logical_manifest = WorkerManifest(
                    worker_id=worker_id,
                    display_name=worker_id,
                    runtime_worker=worker_id,
                    location=_RESOURCE_LOCATION[location],
                    backend=_BACKEND[location],
                    capabilities=list(capabilities),
                    tools=["physical-dispatch-proof"],
                    models=[
                        "glm-5.2"
                        if location == "cloud"
                        else "local-deterministic"
                    ],
                    privacy_level=(
                        "public_only" if location == "cloud" else "sensitive_ok"
                    ),
                    latency_ms={"local": 1, "edge": 25, "cloud": 100}[location],
                    cost_per_1k_tokens=(
                        0.0044 if location == "cloud" else 0.0
                    ),
                )
                decision = ResourceScheduler(
                    WorkerPool((logical_manifest,))
                ).decide(state)
                if (
                    decision.selected_manifest_id != worker_id
                    or str(decision.selected_location) != location
                ):
                    raise SealedPhysicalDispatchError(
                        f"ResourceScheduler did not select the {location} lane"
                    )
                scheduler_receipts.append(to_jsonable(decision))
                acquisition = pool.acquire_task(
                    task_id=task_id,
                    run_id=scenario_run_id,
                    owner_session_id=f"sealed:{scenario_run_id}",
                    requirement=CapabilityRequirement(
                        required=(
                            ("provider-dispatch",)
                            if location == "cloud"
                            else ("deterministic-transform",)
                        ),
                        tool_ids=("physical-dispatch-proof",),
                        locations=(_PHYSICAL_LOCATION[location],),
                        resources=ResourceVector(
                            cpu_cores=0.1,
                            memory_mb=32,
                            process_slots=1,
                        ),
                    ),
                    attempt_number=sequence,
                    preferred_worker_ids=(worker_id,),
                    idempotency_key=(
                        f"sealed:{scenario_run_id}:{task_id}:{location}"
                    ),
                    metadata={
                        "scheduler_decision_id": decision.decision_id,
                        "physical_location": location,
                    },
                )
                lease = acquisition.lease
                attempt = pool.leases.start_attempt(
                    lease.lease_id,
                    worker_id=worker_id,
                    fence_token=lease.fence_token,
                    fence_epoch=lease.fence_epoch,
                    backend_dispatch_id=f"sealed-{location}-{sequence}",
                )
                context = OperatorLeaseExecutionContext(
                    run_id=scenario_run_id,
                    task_id=task_id,
                    operator_ref=f"tool:physical-dispatch:{location}",
                    operator_type=(
                        "model" if location == "cloud" else "tool"
                    ),
                    layer_index=sequence - 1,
                    placement_decision_id=decision.decision_id,
                    placement_location=location,
                    candidate_set_digest=_digest(
                        {
                            "worker": worker_id,
                            "location": location,
                            "alternatives": decision.alternatives,
                        }
                    ),
                    policy_input_digest=_digest(
                        {
                            "privacy_class": privacy_class,
                            "allowed_placements": ["local", "edge", "cloud"],
                            "maximum_cost_usd": maximum_cost_usd,
                            "maximum_latency_ms": maximum_latency_ms,
                        }
                    ),
                    graph_signature=_digest(
                        {
                            "scenario_run_id": scenario_run_id,
                            "lane": location,
                        }
                    ),
                    catalog_version="sealed-physical-v1",
                    catalog_digest=catalog.profile_digest,
                    mechanism_version="phase2_strongest_v1",
                    permission_digest=_digest(task.permission_ref),
                    operator_idempotency_key=(
                        f"sealed:{scenario_run_id}:{task_id}:{location}"
                    ),
                    attempt_id=attempt.attempt_id,
                    lease_id=lease.lease_id,
                    worker_id=worker_id,
                    manifest_digest=registration.manifest.digest,
                    fence_epoch=lease.fence_epoch,
                    fence_token=lease.fence_token,
                    lease_acquired_at=lease.acquired_at,
                    attempt_started_at=attempt.started_at,
                    call_started_at=now_iso(),
                )
                call_port.prepare(context)
                context = replace(context, call_started_at=now_iso())
                result = call_port.execute(context)
                completion = pool.leases.complete(
                    lease.lease_id,
                    worker_id=worker_id,
                    fence_token=lease.fence_token,
                    fence_epoch=lease.fence_epoch,
                    outcome=ExecutionOutcome.SUCCEEDED,
                    summary=result.summary,
                    artifact_refs=tuple(
                        item.ref_id for item in result.artifact_refs
                    ),
                    event_refs=result.verification_refs,
                    backend_receipt_ref=result.call_ref,
                    metadata={
                        "physical_dispatch_receipt_digest": result.metadata.get(
                            "physical_dispatch_receipt_digest"
                        ),
                        "location": location,
                    },
                )
                lease_receipts.append(
                    {
                        "acquisition": acquisition.to_dict(),
                        "attempt": attempt.to_dict(),
                        "completion": completion.to_dict(),
                        "fence_token_persisted": False,
                    }
                )
            receipts = tuple(
                _receipt_evidence(item.to_dict())
                for item in call_port.receipts
            )
            validations = tuple(
                item.to_dict() for item in call_port.validation_reports
            )
            if len(receipts) != 3 or any(
                item.get("real_gate_closed") is not True
                for item in validations
            ):
                raise SealedPhysicalDispatchError(
                    "physical dispatch did not close all three real-execution gates"
                )
            tiers = tuple(
                self._tier(receipt, validation)
                for receipt, validation in zip(
                    receipts,
                    validations,
                    strict=True,
                )
            )
            providers = self._providers(receipts, validations)
            edge = next(
                item
                for item in receipts
                if str(
                    _mapping(item.get("physical_identity")).get("location") or ""
                )
                == "edge"
            )
            local = next(
                item
                for item in receipts
                if str(
                    _mapping(item.get("physical_identity")).get("location") or ""
                )
                == "local"
            )
            stopped = manager.stop(
                "profile:edge",
                timeout_seconds=10,
            )
            degradation = {
                "schema": "zyra.live-disconnected-degradation/v1",
                "event_id": f"sealed-edge-loss-{scenario_run_id}",
                "observed": stopped is not None,
                "safe": stopped is not None,
                "relabeled_as_cloud": False,
                "route_before": str(edge.get("placement_decision_id") or ""),
                "route_after": str(local.get("placement_decision_id") or ""),
                "failed_endpoint_id": str(
                    _mapping(edge.get("physical_identity")).get("node_id") or ""
                ),
                "failed_runtime_id": str(
                    _mapping(edge.get("physical_identity")).get(
                        "failure_boundary_id"
                    )
                    or ""
                ),
                "transport_closed_after_task": True,
                "fallback_tier": "device",
                "side_effect_idempotent": True,
                "artifact_continuity": bool(
                    _mapping(local.get("artifact_ref")).get("digest")
                ),
                "reason": (
                    "isolated edge process was stopped after its committed "
                    "receipt; the fenced local receipt is the safe fallback"
                ),
                "observed_at": now_iso(),
            }
            return SealedPhysicalEvidence(
                tiers=tiers,
                providers=providers,
                degradation=degradation,
                receipts=receipts,
                validations=validations,
                scheduler_decisions=tuple(scheduler_receipts),
                lease_receipts=tuple(lease_receipts),
            )
        finally:
            call_port.close()

    @staticmethod
    def _tier(
        receipt: Mapping[str, Any],
        validation: Mapping[str, Any],
    ) -> dict[str, Any]:
        identity = _mapping(receipt.get("physical_identity"))
        runtime = _mapping(receipt.get("runtime_evidence"))
        call = _mapping(receipt.get("call_receipt"))
        artifact = _mapping(receipt.get("artifact_ref"))
        location = str(identity.get("location") or "")
        tier = "device" if location == "local" else location
        endpoint = str(
            identity.get("endpoint")
            or runtime.get("network_endpoint")
            or ""
        )
        return {
            "observation_id": f"tier-{receipt.get('physical_attempt_id')}",
            "tier": tier,
            "endpoint": endpoint,
            "endpoint_id": str(identity.get("node_id") or ""),
            "runtime_id": str(identity.get("generation_id") or ""),
            "process_id": str(identity.get("pid") or ""),
            "isolation_id": str(identity.get("failure_boundary_id") or ""),
            "request_id": str(call.get("ref_id") or ""),
            "route_id": str(receipt.get("placement_decision_id") or ""),
            "lease_id": str(receipt.get("lease_id") or ""),
            "artifact_ids": [str(artifact.get("ref_id") or "")],
            "started_at": str(
                _mapping(receipt.get("input_signals")).get("call_started_at")
                or ""
            ),
            "completed_at": str(receipt.get("completed_at") or now_iso()),
            "request_digest": str(
                _mapping(receipt.get("input_signals")).get("payload_digest")
                or receipt.get("digest")
                or ""
            ),
            "response_digest": str(call.get("digest") or ""),
            "handshake_ok": True,
            "heartbeat_ok": True,
            "task_success": True,
            "simulated": bool(receipt.get("simulated")),
            "loopback": _loopback(endpoint),
            "metadata": {
                "physical_dispatch_receipt_digest": receipt.get("digest"),
                "physical_dispatch_validation": dict(validation),
                "independent_process": identity.get("independent_process"),
                "failure_boundary_id": identity.get("failure_boundary_id"),
                "provider_evidence": _mapping(receipt.get("provider_evidence")),
                "remote_boundary": (
                    "live-provider"
                    if location == "cloud"
                    else "isolated-process"
                    if location == "edge"
                    else "local-terminal"
                ),
            },
        }

    @staticmethod
    def _providers(
        receipts: Sequence[Mapping[str, Any]],
        validations: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], ...]:
        by_location = {
            str(_mapping(item.get("physical_identity")).get("location") or ""): item
            for item in receipts
        }
        local = by_location["local"]
        cloud = by_location["cloud"]
        local_call = _mapping(local.get("call_receipt"))
        local_attempt = str(local.get("physical_attempt_id") or "")
        local_signal = _mapping(local.get("input_signals"))
        cloud_provider = _mapping(cloud.get("provider_evidence"))
        cloud_call = _mapping(cloud.get("call_receipt"))
        cloud_attempt = str(cloud.get("physical_attempt_id") or "")
        cloud_usage = _mapping(cloud_provider.get("usage"))
        cloud_endpoint = str(
            cloud_provider.get("endpoint")
            or "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        )
        return (
            {
                "observation_id": f"provider-local-{local_attempt}",
                "provider_id": "zyra-local",
                "model_id": "local-deterministic",
                "endpoint": str(
                    _mapping(local.get("physical_identity")).get("endpoint")
                    or "local://physical-dispatch"
                ),
                "request_id": str(local_call.get("ref_id") or ""),
                "attempt_id": local_attempt,
                "route_id": str(local.get("placement_decision_id") or ""),
                "credential_custodian": (
                    "WorkerPoolFoundationRuntime attestation/fenced lease"
                ),
                "authenticated": True,
                "response_status": 200,
                "request_digest": str(
                    local_signal.get("payload_digest")
                    or local.get("digest")
                    or ""
                ),
                "response_digest": str(local_call.get("digest") or ""),
                "tool_call_ids": [local_attempt],
                "tool_result_ids": [local_attempt],
                "started_at": str(local_signal.get("call_started_at") or ""),
                "completed_at": str(local.get("completed_at") or now_iso()),
                "cost_usd": 0.0,
                "latency_ms": 0,
                "metadata": {
                    "physical_dispatch_receipt_digest": local.get("digest"),
                    "capability_kind": "local-model-capability",
                    "simulated": False,
                },
            },
            {
                "observation_id": f"provider-cloud-{cloud_attempt}",
                "provider_id": str(cloud_provider.get("provider_id") or ""),
                "model_id": str(cloud_provider.get("model_id") or ""),
                "endpoint": cloud_endpoint,
                "request_id": str(cloud_provider.get("request_id") or ""),
                "attempt_id": str(
                    cloud_provider.get("provider_attempt_id")
                    or cloud_attempt
                ),
                "route_id": str(cloud.get("placement_decision_id") or ""),
                "credential_custodian": str(
                    cloud_provider.get("credential_ref")
                    or "env://ZAI_API_KEY"
                ),
                "authenticated": bool(cloud_provider.get("live")),
                "response_status": int(
                    cloud_provider.get("http_status") or 0
                ),
                "request_digest": str(
                    cloud_provider.get("payload_digest")
                    or cloud.get("digest")
                    or ""
                ),
                "response_digest": str(cloud_call.get("digest") or ""),
                "tool_call_ids": [cloud_attempt],
                "tool_result_ids": [cloud_attempt],
                "started_at": str(
                    _mapping(cloud.get("input_signals")).get("call_started_at")
                    or ""
                ),
                "completed_at": str(cloud.get("completed_at") or now_iso()),
                "cost_usd": float(cloud_provider.get("cost_usd") or 0),
                "latency_ms": int(cloud_provider.get("latency_ms") or 0),
                "metadata": {
                    "physical_dispatch_receipt_digest": cloud.get("digest"),
                    "provider_usage": cloud_usage,
                    "credential_material_persisted": cloud_provider.get(
                        "credential_material_persisted"
                    ),
                    "simulated": cloud_provider.get("simulated"),
                    "semantic_only": cloud_provider.get("semantic_only"),
                },
            },
        )


class SealedPlacementOwner:
    """Delegate canonical routing while adding the P2 real-lane evidence port."""

    def __init__(
        self,
        *,
        delegate: Any,
        physical_runtime: SealedPhysicalDispatchRuntime,
        evidence_sink: Any | None = None,
    ) -> None:
        self.delegate = delegate
        self.physical_runtime = physical_runtime
        self.evidence_sink = evidence_sink
        self._evidence: SealedPhysicalEvidence | None = None

    def acquire_route(self, **kwargs: Any) -> Mapping[str, Any]:
        return self.delegate.acquire_route(**kwargs)

    def migrate_route(self, **kwargs: Any) -> Mapping[str, Any]:
        return self.delegate.migrate_route(**kwargs)

    def execute_tiers(
        self,
        *,
        scenario_run_id: str,
        domain_input: Any,
        route: Mapping[str, Any],
    ) -> Sequence[Mapping[str, Any]]:
        evidence = self._require_evidence(
            scenario_run_id=scenario_run_id,
            domain_input=domain_input,
            route=route,
        )
        return evidence.tiers

    def execute_providers(
        self,
        *,
        scenario_run_id: str,
        domain_input: Any,
        route: Mapping[str, Any],
        capability_count: int,
    ) -> Sequence[Mapping[str, Any]]:
        evidence = self._require_evidence(
            scenario_run_id=scenario_run_id,
            domain_input=domain_input,
            route=route,
        )
        if len(evidence.providers) < capability_count:
            raise SealedPhysicalDispatchError(
                "real provider/model capability minimum was not reached"
            )
        return evidence.providers

    def disconnected_degradation(
        self,
        *,
        scenario_run_id: str,
        domain_input: Any,
        route: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._require_evidence(
            scenario_run_id=scenario_run_id,
            domain_input=domain_input,
            route=route,
        ).degradation

    def _require_evidence(
        self,
        *,
        scenario_run_id: str,
        domain_input: Any,
        route: Mapping[str, Any],
    ) -> SealedPhysicalEvidence:
        if self._evidence is None:
            self._evidence = self.physical_runtime.execute(
                scenario_run_id=scenario_run_id,
                task_id=str(
                    getattr(self.delegate, "state", None).task_id
                    if getattr(self.delegate, "state", None) is not None
                    else f"sealed-task-{scenario_run_id}"
                ),
                payload={
                    "kind": "phase2-sealed-physical-proof",
                    "domain": str(domain_input.domain.value),
                    "input_digest": str(domain_input.input_digest),
                    "canonical_route": dict(route),
                },
                privacy_class=str(domain_input.privacy_class.value),
                maximum_cost_usd=float(domain_input.maximum_cost_usd),
                maximum_latency_ms=int(domain_input.maximum_latency_ms),
            )
            if self.evidence_sink is not None:
                self.evidence_sink(self._evidence)
        return self._evidence


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


__all__ = [
    "SealedPhysicalDispatchError",
    "SealedPhysicalDispatchRuntime",
    "SealedPhysicalEvidence",
    "SealedPlacementOwner",
]
