from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zyra_orchestration.deployment import (
    DeploymentDispatchRuntime,
    DeploymentError,
    DeploymentProcessManager,
    DeploymentProfile,
    DeploymentStateStore,
    DispatchReceipt,
    DispatchStatus,
    LifecycleStatus,
    PlacementCandidate,
    PlacementDecision,
    ProcessRecord,
    ProfileCatalog,
    Sensitivity,
    Workload,
)
from zyra_orchestration.deployment.errors import redact
from zyra_orchestration.topology_policy.contracts import (
    ContractHeader,
    FrozenDict,
    PhysicalDispatchReceipt,
    StableArtifactRef,
    canonical_digest,
    thaw_json,
)

from .operator_policy.integration import (
    OperatorCallResult,
    OperatorLeaseExecutionContext,
    OperatorPlacementError,
)


PHYSICAL_DISPATCH_MECHANISM_ID = "zyra_physical_dispatch"
PHYSICAL_DISPATCH_MECHANISM_VERSION = "physical_dispatch_v1"
PHYSICAL_DISPATCH_VALIDATION_SCHEMA = "zyra.physical-dispatch-validation/v1"
PHYSICAL_REROUTE_VALIDATION_SCHEMA = "zyra.physical-reroute-validation/v1"
PHASE2_OPERATOR_RUNTIME_VERSION = "phase2-operator-execution-v8"

_LOCATION_TO_PROFILE = {
    "local": DeploymentProfile.DEVICE,
    "edge": DeploymentProfile.EDGE,
    "cloud": DeploymentProfile.CLOUD,
}
_PROFILE_TO_LOCATION = {value: key for key, value in _LOCATION_TO_PROFILE.items()}
_PRIVACY_TO_SENSITIVITY = {
    "public": Sensitivity.PUBLIC,
    "internal": Sensitivity.INTERNAL,
    "confidential": Sensitivity.CONFIDENTIAL,
    "restricted": Sensitivity.RESTRICTED,
    "local-only": Sensitivity.RESTRICTED,
}


def _observed_process_snapshot(
    manager: DeploymentProcessManager,
    profile: DeploymentProfile,
) -> dict[str, Any]:
    """Describe one profile's freshly observed process record.

    ``status`` probes liveness, so this is the same observation the dispatch
    preflight gates on.  Recorded for every profile because a rejected
    dispatch must name which runtime it found dead and which it did not.
    """

    try:
        record = manager.status(f"profile:{profile.value}")
    except DeploymentError as error:
        return {"profile": profile.value, "observation_error": str(error)}
    if record is None:
        return {"profile": profile.value, "present": False}
    return {
        "profile": profile.value,
        "present": True,
        "status": record.status.value,
        "pid": record.pid,
        "generation_id": record.generation_id,
        "restart_count": record.restart_count,
        "started_at": record.started_at,
        "observed_at": record.observed_at,
        "exit_code": record.exit_code,
    }


@dataclass(frozen=True, slots=True)
class PhysicalDispatchTask:
    run_id: str
    task_id: str
    payload: FrozenDict
    privacy_class: str
    allowed_placements: tuple[str, ...]
    permission_ref: str
    operation: str = "physical-dispatch-proof"
    provider_id: str = "deepseek"
    model_id: str = "deepseek-v4-flash"
    latency_sla_ms: int = 120_000
    # Placement policy caps ``latency_sla_ms`` at 120s because it selects the
    # device/edge/cloud tier.  A provider reasoning loop legitimately runs far
    # longer than any placement SLA, so the transport deadline is a separate
    # budget. ``None`` keeps marker/probe callers on the placement SLA while
    # ``0`` explicitly means that no dispatch-owned total deadline exists.
    execution_budget_ms: int | None = None
    maximum_cost_usd: float = 0.01
    verifier_id: str = "physical-dispatch-marker-verifier/v1"
    condition: str = "normal"
    condition_signals: FrozenDict = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        placements = tuple(
            sorted(
                {
                    str(item).strip().casefold()
                    for item in self.allowed_placements
                    if str(item).strip()
                }
            )
        )
        if (
            not self.run_id
            or not self.task_id
            or not self.permission_ref
            or not self.operation.strip()
            or not placements
            or any(item not in _LOCATION_TO_PROFILE for item in placements)
        ):
            raise ValueError("physical dispatch task identity or placement policy is invalid")
        privacy = self.privacy_class.strip().casefold()
        if privacy not in _PRIVACY_TO_SENSITIVITY:
            raise ValueError("physical dispatch privacy class is invalid")
        if privacy in {"restricted", "local-only"} and placements != ("local",):
            raise ValueError("restricted/local-only dispatch must be local-only")
        if self.execution_budget_ms is not None and self.execution_budget_ms < 0:
            raise ValueError("physical dispatch execution budget is invalid")
        object.__setattr__(self, "privacy_class", privacy)
        object.__setattr__(self, "operation", self.operation.strip().casefold())
        object.__setattr__(self, "allowed_placements", placements)
        object.__setattr__(self, "payload", FrozenDict(self.payload))
        object.__setattr__(
            self,
            "condition",
            self.condition.strip().casefold() or "normal",
        )
        object.__setattr__(
            self,
            "condition_signals",
            FrozenDict(self.condition_signals),
        )

    @property
    def payload_digest(self) -> str:
        return canonical_digest(dict(self.payload))

    @property
    def dispatch_timeout_seconds(self) -> float | None:
        """Transport deadline for one physical dispatch attempt.

        Falls back to the placement SLA so existing marker-probe callers keep
        their historical deadline.
        """

        if self.execution_budget_ms == 0:
            return None
        budget_ms = self.execution_budget_ms or self.latency_sla_ms
        return max(5.0, budget_ms / 1000)


@dataclass(frozen=True, slots=True)
class PhysicalDispatchFailureReceipt:
    run_id: str
    task_id: str
    placement_decision_id: str
    lease_id: str
    physical_attempt_id: str
    location: str
    failure_code: str
    failure_boundary_id: str
    node_id: str
    pid: int
    endpoint: str
    side_effect_started: bool
    retry_safe: bool
    observed_at: str
    predecessor_attempt_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.physical-dispatch-failure/v1",
            "run_id": self.run_id,
            "task_id": self.task_id,
            "placement_decision_id": self.placement_decision_id,
            "lease_id": self.lease_id,
            "physical_attempt_id": self.physical_attempt_id,
            "location": self.location,
            "failure_code": self.failure_code,
            "failure_boundary_id": self.failure_boundary_id,
            "node_id": self.node_id,
            "pid": self.pid,
            "endpoint": self.endpoint,
            "side_effect_started": self.side_effect_started,
            "retry_safe": self.retry_safe,
            "observed_at": self.observed_at,
            "predecessor_attempt_id": self.predecessor_attempt_id,
        }


@dataclass(frozen=True, slots=True)
class PhysicalDispatchValidationReport:
    receipt_digest: str
    location: str
    real_gate_closed: bool
    checks: FrozenDict
    blockers: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    schema: str = PHYSICAL_DISPATCH_VALIDATION_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "receipt_digest": self.receipt_digest,
            "location": self.location,
            "real_gate_closed": self.real_gate_closed,
            "checks": dict(self.checks),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class PhysicalRerouteValidationReport:
    previous_receipt_digest: str
    current_receipt_digest: str
    placement_changed: bool
    physical_identity_changed: bool
    recovery_linked: bool
    valid: bool
    blockers: tuple[str, ...]
    schema: str = PHYSICAL_REROUTE_VALIDATION_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "previous_receipt_digest": self.previous_receipt_digest,
            "current_receipt_digest": self.current_receipt_digest,
            "placement_changed": self.placement_changed,
            "physical_identity_changed": self.physical_identity_changed,
            "recovery_linked": self.recovery_linked,
            "valid": self.valid,
            "blockers": list(self.blockers),
        }


class PhysicalDispatchEvidenceStore:
    """Durable, redacted evidence projection; canonical owners remain external."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(
        self,
        *,
        kind: str,
        identity: str,
        payload: Mapping[str, Any],
    ) -> StableArtifactRef:
        safe_payload = redact(dict(payload))
        body = json.dumps(
            safe_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        receipt_digest = _sha256_bytes(body)
        safe_identity = "".join(
            character if character.isalnum() or character in "._-" else "-"
            for character in identity
        )[:120]
        path = (self.root / f"{kind}-{safe_identity}-{receipt_digest[:16]}.json").resolve()
        if self.root not in path.parents:
            raise ValueError("physical evidence path escaped its configured root")
        temporary = path.with_suffix(".tmp")
        if not path.exists():
            with temporary.open("xb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        return StableArtifactRef(
            ref_id=f"{kind}:{identity}",
            uri=f"physical-evidence://{kind}/{safe_identity}/{receipt_digest}",
            digest=receipt_digest,
        )


@dataclass(slots=True)
class _PreparedPhysicalDispatch:
    context: OperatorLeaseExecutionContext
    profile: DeploymentProfile
    location: str
    process: ProcessRecord
    client: Any
    health: dict[str, Any]
    workload: Workload
    decision: PlacementDecision


class PhysicalDispatchReceiptBuilder:
    def __init__(
        self,
        *,
        configuration_digest: str,
        orchestrator_pid: int | None = None,
    ) -> None:
        self.configuration_digest = configuration_digest
        self.orchestrator_pid = int(orchestrator_pid or os.getpid())

    def build(
        self,
        *,
        task: PhysicalDispatchTask,
        prepared: _PreparedPhysicalDispatch,
        dispatch: DispatchReceipt,
        verifier_ref: StableArtifactRef,
        recovery_evidence: Sequence[PhysicalDispatchFailureReceipt],
    ) -> PhysicalDispatchReceipt:
        context = prepared.context
        health = prepared.health
        location = prepared.location
        runtime_identity = _mapping(health.get("runtime_identity"))
        network = _mapping(health.get("network"))
        output = _mapping(dispatch.result.get("output"))
        artifact = _mapping(dispatch.result.get("artifact"))
        provider = _mapping(output.get("provider_call"))
        process_create_time = float(
            runtime_identity.get("process_create_time")
            or prepared.process.process_create_time
            or 0
        )
        physical_identity = {
            "location": location,
            "profile": prepared.profile.value,
            "runtime_kind": str(
                runtime_identity.get("runtime_kind") or "isolated_process"
            ),
            "hostname": str(runtime_identity.get("hostname") or ""),
            "node_id": dispatch.node_id,
            "generation_id": str(
                health.get("generation_id") or prepared.process.generation_id
            ),
            "pid": int(health.get("pid") or prepared.process.pid),
            "process_create_time": process_create_time,
            "terminal_id": str(runtime_identity.get("terminal_id") or ""),
            "terminal_source": str(runtime_identity.get("terminal_source") or ""),
            "failure_boundary_id": str(
                runtime_identity.get("failure_boundary_id") or ""
            ),
            "independent_process": bool(
                runtime_identity.get("independent_process")
            ),
            "orchestrator_pid": self.orchestrator_pid,
            "endpoint": str(health.get("endpoint") or prepared.process.endpoint),
        }
        input_signals = {
            "selected_location": location,
            "resource_scheduler_decision_id": context.placement_decision_id,
            "lease_acquired_at": context.lease_acquired_at,
            "attempt_started_at": context.attempt_started_at,
            "call_started_at": context.call_started_at,
            "call_finished_at": dispatch.completed_at,
            "worker_id": context.worker_id,
            "candidate_set_digest": context.candidate_set_digest,
            "policy_input_digest": context.policy_input_digest,
            "privacy_class": task.privacy_class,
            "maximum_cost_usd": task.maximum_cost_usd,
            "latency_sla_ms": task.latency_sla_ms,
            "condition": task.condition,
            "condition_signals": dict(task.condition_signals),
            "workload_operation": prepared.workload.operation,
            "task_payload_digest": task.payload_digest,
            "operator_ref": str(output.get("operator_ref") or ""),
            "layer_index": int(output.get("layer_index") or 0),
            "operator_execution_digest": str(
                output.get("operator_execution_digest") or ""
            ),
            "operator_adapter_id": str(output.get("operator_adapter_id") or ""),
            "operator_execution_body": _mapping(
                output.get("operator_execution_body")
            ),
            "contract_outputs": _mapping(output.get("contract_outputs")),
            "domain_result": _mapping(output.get("domain_result")),
            "domain_artifact": _mapping(output.get("domain_artifact")),
            "workspace_delta": _mapping(output.get("workspace_delta")),
            "final_text": str(output.get("final_text") or ""),
            "output_contract_fulfilled": (
                output.get("output_contract_fulfilled") is True
            ),
            "domain_effect_performed": (
                output.get("domain_effect_performed") is True
            ),
            "leased_worker_process_identity": str(
                _mapping(task.payload.get("physical_worker_binding")).get(
                    "process_identity"
                )
                or ""
            ),
            "leased_worker_endpoint": str(
                _mapping(task.payload.get("physical_worker_binding")).get(
                    "endpoint"
                )
                or ""
            ),
            "leased_worker_node_id": str(
                _mapping(task.payload.get("physical_worker_binding")).get(
                    "node_id"
                )
                or ""
            ),
            "leased_worker_generation_id": str(
                _mapping(task.payload.get("physical_worker_binding")).get(
                    "generation_id"
                )
                or ""
            ),
        }
        runtime_evidence = {
            "health_status": health.get("status"),
            "health_digest": health.get("semantic_digest"),
            "network_mode": network.get("mode"),
            "network_endpoint": network.get("endpoint"),
            "network_namespace": network.get("namespace"),
            "network_down": network.get("network_down") is True,
            "isolation_kind": "process",
            "failure_boundary_id": physical_identity["failure_boundary_id"],
            "independent_from_orchestrator": (
                int(physical_identity["pid"]) != self.orchestrator_pid
            ),
            "process_create_time": process_create_time,
            "node_configuration_digest": health.get("configuration_digest"),
            "simulated": False,
            "semantic_only": False,
        }
        privacy_evidence = {
            "privacy_class": task.privacy_class,
            "allowed_placements": list(task.allowed_placements),
            "selected_placement": location,
            "permission_ref": task.permission_ref,
            "permission_digest": context.permission_digest,
            "payload_digest": task.payload_digest,
            "payload_redacted": True,
            "credential_material_persisted": False,
            "cloud_dispatch_allowed": "cloud" in task.allowed_placements,
        }
        return PhysicalDispatchReceipt(
            header=ContractHeader(
                contract_id=f"physical-dispatch:{dispatch.dispatch_id}",
                created_at=dispatch.completed_at,
                source_event_id=f"deployment.dispatch:{dispatch.dispatch_id}",
                correlation_id=dispatch.dispatch_id,
                causation_id=context.placement_decision_id,
                mechanism_id=PHYSICAL_DISPATCH_MECHANISM_ID,
                mechanism_version=PHYSICAL_DISPATCH_MECHANISM_VERSION,
                input_version="operator-placement-lease/v1",
                idempotency_key=context.operator_idempotency_key,
                configuration_digest=self.configuration_digest,
            ),
            placement_decision_id=context.placement_decision_id,
            alternatives=tuple(
                _PROFILE_TO_LOCATION[item.profile]
                for item in prepared.decision.candidates
            ),
            input_signals=FrozenDict(input_signals),
            worker_manifest_ref=StableArtifactRef(
                ref_id=f"worker-manifest:{context.worker_id}",
                uri=f"worker-manifest://{context.worker_id}/{context.manifest_digest}",
                digest=_digest_hex(context.manifest_digest),
            ),
            lease_id=context.lease_id,
            physical_attempt_id=context.attempt_id,
            physical_identity=FrozenDict(physical_identity),
            call_receipt=StableArtifactRef(
                ref_id=dispatch.dispatch_id,
                uri=f"deployment-dispatch://{dispatch.dispatch_id}",
                digest=_digest_hex(dispatch.result_digest),
            ),
            artifact_ref=StableArtifactRef(
                ref_id=str(
                    artifact.get("artifact_id")
                    or dispatch.artifact_refs[0]
                ),
                uri=str(
                    artifact.get("artifact_ref")
                    or dispatch.artifact_refs[0]
                ),
                digest=_digest_hex(str(artifact.get("sha256") or "")),
            ),
            verifier_ref=verifier_ref,
            privacy_class=task.privacy_class,
            allowed_placements=task.allowed_placements,
            permission_ref=task.permission_ref,
            simulated=False,
            semantic_only=False,
            placement_reason=(
                "ResourceScheduler selected the leased physical worker before "
                f"dispatch under condition {task.condition}"
            ),
            privacy_evidence=FrozenDict(privacy_evidence),
            runtime_evidence=FrozenDict(runtime_evidence),
            provider_evidence=FrozenDict(provider),
            recovery_evidence=tuple(
                FrozenDict(item.to_dict()) for item in recovery_evidence
            ),
        )


class PhysicalDispatchReceiptValidator:
    def validate(
        self,
        receipt: PhysicalDispatchReceipt,
    ) -> PhysicalDispatchValidationReport:
        identity = dict(receipt.physical_identity)
        runtime = dict(receipt.runtime_evidence)
        provider = dict(receipt.provider_evidence)
        privacy = dict(receipt.privacy_evidence)
        location = str(identity.get("location") or "").casefold()
        timestamps_valid = _ordered_timestamps(
            str(receipt.input_signals.get("lease_acquired_at") or ""),
            str(receipt.input_signals.get("attempt_started_at") or ""),
            str(receipt.input_signals.get("call_started_at") or ""),
            str(receipt.input_signals.get("call_finished_at") or ""),
        )
        checks: dict[str, bool] = {
            "not_simulated": not receipt.simulated,
            "not_semantic_only": not receipt.semantic_only,
            "known_location": location in _LOCATION_TO_PROFILE,
            "placement_allowed": location in receipt.allowed_placements,
            "privacy_projection_matches": (
                privacy.get("selected_placement") == location
                and privacy.get("payload_redacted") is True
            ),
            "permission_present": bool(receipt.permission_ref),
            "lease_precedes_execution": timestamps_valid,
            "physical_attempt_present": bool(receipt.physical_attempt_id),
            "worker_manifest_present": bool(receipt.worker_manifest_ref.digest),
            "call_receipt_present": bool(receipt.call_receipt.digest),
            "artifact_present": bool(receipt.artifact_ref.digest),
            "verifier_present": bool(receipt.verifier_ref.digest),
            "real_process_identity": (
                int(identity.get("pid") or 0) > 0
                and float(identity.get("process_create_time") or 0) > 0
                and bool(identity.get("node_id"))
                and bool(identity.get("generation_id"))
            ),
            "runtime_health_ready": runtime.get("health_status") == "ready",
            "workload_operation_present": bool(
                receipt.input_signals.get("workload_operation")
            ),
            "task_payload_bound": bool(
                receipt.input_signals.get("task_payload_digest")
                and receipt.input_signals.get("task_payload_digest")
                == privacy.get("payload_digest")
            ),
        }
        if (
            receipt.input_signals.get("workload_operation")
            == "phase2-operator-execution"
        ):
            execution_body = _mapping(
                receipt.input_signals.get("operator_execution_body")
            )
            contract_outputs = _mapping(
                receipt.input_signals.get("contract_outputs")
            )
            domain_artifact = _mapping(
                receipt.input_signals.get("domain_artifact")
            )
            domain_result = _mapping(
                receipt.input_signals.get("domain_result")
            )
            execution_digest = str(
                receipt.input_signals.get("operator_execution_digest") or ""
            ).removeprefix("sha256:")
            contract_digest = str(
                execution_body.get("contract_outputs_digest") or ""
            ).removeprefix("sha256:")
            artifact_digest = str(
                domain_artifact.get("content_digest") or ""
            ).removeprefix("sha256:")
            checks.update(
                {
                    "operator_ref_bound": bool(
                        receipt.input_signals.get("operator_ref")
                    ),
                    "operator_layer_bound": (
                        int(receipt.input_signals.get("layer_index") or 0) > 0
                    ),
                    "operator_execution_digest_valid": bool(
                        execution_body
                        and execution_digest == canonical_digest(execution_body)
                    ),
                    "operator_adapter_bound": bool(
                        receipt.input_signals.get("operator_adapter_id")
                        and receipt.input_signals.get("operator_adapter_id")
                        == execution_body.get("operator_adapter_id")
                    ),
                    "operator_contract_outputs_valid": bool(
                        contract_outputs
                        and contract_digest == canonical_digest(contract_outputs)
                        and sorted(execution_body.get("output_contract") or ())
                        == sorted(
                            execution_body.get("fulfilled_output_contract") or ()
                        )
                        == sorted(contract_outputs)
                    ),
                    "operator_domain_artifact_valid": bool(
                        domain_artifact.get("content")
                        and artifact_digest
                        == canonical_digest(domain_artifact.get("content"))
                        and str(execution_body.get("domain_artifact_digest") or "")
                        .removeprefix("sha256:")
                        == artifact_digest
                    ),
                    "operator_domain_result_valid": bool(
                        domain_result
                        and str(execution_body.get("domain_result_digest") or "")
                        .removeprefix("sha256:")
                        == canonical_digest(domain_result)
                    ),
                    "operator_domain_effect_performed": (
                        receipt.input_signals.get("domain_effect_performed") is True
                        and receipt.input_signals.get(
                            "output_contract_fulfilled"
                        )
                        is True
                    ),
                    "leased_process_identity_exact": bool(
                        receipt.input_signals.get(
                            "leased_worker_process_identity"
                        )
                        and receipt.input_signals.get(
                            "leased_worker_process_identity"
                        )
                        == identity.get("failure_boundary_id")
                        == runtime.get("failure_boundary_id")
                    ),
                    "leased_endpoint_exact": bool(
                        receipt.input_signals.get("leased_worker_endpoint")
                        and receipt.input_signals.get("leased_worker_endpoint")
                        == identity.get("endpoint")
                        == runtime.get("network_endpoint")
                    ),
                    "leased_node_generation_exact": bool(
                        receipt.input_signals.get("leased_worker_node_id")
                        == identity.get("node_id")
                        and receipt.input_signals.get(
                            "leased_worker_generation_id"
                        )
                        == identity.get("generation_id")
                    ),
                }
            )
        if receipt.privacy_class in {"restricted", "local-only"}:
            checks["sensitive_local_only"] = (
                receipt.allowed_placements == ("local",) and location == "local"
            )
        if location == "local":
            checks.update(
                {
                    "local_host_identity": bool(identity.get("hostname")),
                    "local_terminal_identity": bool(identity.get("terminal_id")),
                    "local_process_identity": (
                        identity.get("runtime_kind") == "isolated_process"
                    ),
                    "local_provider_request_zero": not provider,
                }
            )
        elif location == "edge":
            checks.update(
                {
                    "edge_independent_process": (
                        identity.get("independent_process") is True
                        and int(identity.get("pid") or 0)
                        != int(identity.get("orchestrator_pid") or 0)
                    ),
                    "edge_failure_boundary": bool(
                        identity.get("failure_boundary_id")
                    ),
                    "edge_network_endpoint": bool(
                        runtime.get("network_endpoint")
                    ),
                    "edge_network_namespace": bool(
                        runtime.get("network_namespace")
                    ),
                    "edge_provider_request_zero": not provider,
                }
            )
        elif location == "cloud":
            usage = _mapping(provider.get("usage"))
            operator_execution = (
                receipt.input_signals.get("workload_operation")
                == "phase2-operator-execution"
            )
            checks.update(
                {
                    "cloud_live_request": (
                        provider.get("live") is True
                        and provider.get("external_model_request") is True
                        and provider.get("simulated") is False
                        and provider.get("semantic_only") is False
                    ),
                    "cloud_provider_model_request": all(
                        bool(provider.get(name))
                        for name in (
                            "provider_id",
                            "model_id",
                            "request_id",
                            "provider_attempt_id",
                        )
                    ),
                    "cloud_http_success": (
                        200 <= int(provider.get("http_status") or 0) < 300
                    ),
                    "cloud_usage_present": (
                        int(usage.get("total_tokens") or 0) > 0
                    ),
                    "cloud_cost_present": (
                        (
                            float(provider.get("cost_usd") or 0) > 0
                            or float(provider.get("cost_amount") or 0) > 0
                        )
                        and bool(provider.get("cost_currency"))
                        and bool(provider.get("pricing_source_ref"))
                    ),
                    "cloud_latency_present": (
                        int(provider.get("latency_ms") or 0) > 0
                    ),
                    "cloud_payload_digest": bool(
                        _digest_hex(str(provider.get("payload_digest") or ""))
                    ),
                    "cloud_credential_redacted": (
                        str(provider.get("credential_ref") or "").startswith(
                            "env://"
                        )
                        and provider.get("credential_material_persisted") is False
                    ),
                    "cloud_execution_verified": (
                        (
                            provider.get("task_execution_verified") is True
                            and provider.get("prompt_goal_bound") is True
                            and provider.get("provider_called") is True
                            and provider.get("synthetic_usage") is False
                            and provider.get("workload_operation")
                            == "phase2-operator-execution"
                        )
                        if operator_execution
                        else provider.get("marker_verified") is True
                    ),
                }
            )
        blockers = tuple(
            sorted(name for name, passed in checks.items() if not passed)
        )
        return PhysicalDispatchValidationReport(
            receipt_digest=receipt.digest,
            location=location,
            real_gate_closed=not blockers,
            checks=FrozenDict(checks),
            blockers=blockers,
        )

    def validate_reroute(
        self,
        previous: PhysicalDispatchReceipt,
        current: PhysicalDispatchReceipt,
    ) -> PhysicalRerouteValidationReport:
        previous_identity = dict(previous.physical_identity)
        current_identity = dict(current.physical_identity)
        previous_location = str(previous_identity.get("location") or "")
        current_location = str(current_identity.get("location") or "")
        placement_changed = previous_location != current_location
        identity_fields = (
            "pid",
            "node_id",
            "generation_id",
            "failure_boundary_id",
        )
        physical_identity_changed = any(
            previous_identity.get(name) != current_identity.get(name)
            for name in identity_fields
        )
        if previous_location == "cloud" or current_location == "cloud":
            previous_provider = dict(previous.provider_evidence)
            current_provider = dict(current.provider_evidence)
            physical_identity_changed = physical_identity_changed or (
                previous_provider.get("request_id")
                != current_provider.get("request_id")
            )
        recovery_linked = any(
            str(item.get("physical_attempt_id") or "")
            == previous.physical_attempt_id
            for item in current.recovery_evidence
        )
        blockers = []
        if not placement_changed:
            blockers.append("placement_label_unchanged")
        if not physical_identity_changed:
            blockers.append("physical_identity_unchanged")
        if not recovery_linked:
            blockers.append("recovery_causation_missing")
        return PhysicalRerouteValidationReport(
            previous_receipt_digest=previous.digest,
            current_receipt_digest=current.digest,
            placement_changed=placement_changed,
            physical_identity_changed=physical_identity_changed,
            recovery_linked=recovery_linked,
            valid=not blockers,
            blockers=tuple(blockers),
        )


class PhysicalDispatchCallPort:
    """Lease-gated operator call port backed by real deployment processes."""

    def __init__(
        self,
        *,
        task: PhysicalDispatchTask,
        catalog: ProfileCatalog,
        process_manager: DeploymentProcessManager,
        state_store: DeploymentStateStore,
        evidence_store: PhysicalDispatchEvidenceStore,
        enabled: bool = True,
        orchestrator_pid: int | None = None,
    ) -> None:
        self.task = task
        self.catalog = catalog
        self.process_manager = process_manager
        self.state_store = state_store
        self.evidence_store = evidence_store
        self.enabled = enabled
        self.orchestrator_pid = int(orchestrator_pid or os.getpid())
        self.dispatch_runtime = DeploymentDispatchRuntime(
            state_store,
            enabled=enabled,
        )
        self.configuration_digest = canonical_digest(
            {
                "mechanism": PHYSICAL_DISPATCH_MECHANISM_VERSION,
                "profiles": catalog.profile_digest,
                "privacy_class": task.privacy_class,
                "allowed_placements": list(task.allowed_placements),
                "operation": task.operation,
                "provider_id": task.provider_id,
                "model_id": task.model_id,
                "condition": task.condition,
                "condition_signals": dict(task.condition_signals),
            }
        )
        self.builder = PhysicalDispatchReceiptBuilder(
            configuration_digest=self.configuration_digest,
            orchestrator_pid=self.orchestrator_pid,
        )
        self.validator = PhysicalDispatchReceiptValidator()
        self._prepared: dict[str, _PreparedPhysicalDispatch] = {}
        self._failures: dict[str, list[PhysicalDispatchFailureReceipt]] = {}
        self._receipts: list[PhysicalDispatchReceipt] = []
        self._validation_reports: list[PhysicalDispatchValidationReport] = []

    @property
    def receipts(self) -> tuple[PhysicalDispatchReceipt, ...]:
        return tuple(self._receipts)

    @property
    def failure_receipts(self) -> tuple[PhysicalDispatchFailureReceipt, ...]:
        return tuple(self._failures.get(self.task.task_id, ()))

    @property
    def validation_reports(self) -> tuple[PhysicalDispatchValidationReport, ...]:
        return tuple(self._validation_reports)

    def prepare(self, context: OperatorLeaseExecutionContext) -> None:
        if not self.enabled:
            raise OperatorPlacementError(
                "physical_dispatch_disabled",
                "physical dispatch is disabled",
            )
        if context.run_id != self.task.run_id or context.task_id != self.task.task_id:
            raise OperatorPlacementError(
                "physical_dispatch_task_identity_mismatch",
                "physical dispatch context does not belong to the configured task",
            )
        location = context.placement_location.casefold()
        profile = _LOCATION_TO_PROFILE.get(location)
        if profile is None:
            raise OperatorPlacementError(
                "physical_dispatch_location_invalid",
                "ResourceScheduler selected an unknown physical location",
            )
        if location not in self.task.allowed_placements:
            raise OperatorPlacementError(
                "physical_dispatch_privacy_rejected",
                "the selected physical location violates the task privacy policy",
                metadata={
                    "selected_location": location,
                    "allowed_placements": list(self.task.allowed_placements),
                    "side_effect_started": False,
                },
            )
        try:
            process = self.process_manager.status(
                f"profile:{profile.value}"
            )
            if process is not None and process.status in {
                LifecycleStatus.BLOCKED,
                LifecycleStatus.CRASHED,
                LifecycleStatus.UNKNOWN,
            }:
                observed = tuple(
                    _observed_process_snapshot(self.process_manager, item)
                    for item in (
                        DeploymentProfile.DEVICE,
                        DeploymentProfile.EDGE,
                        DeploymentProfile.CLOUD,
                    )
                )
                raise OperatorPlacementError(
                    "physical_dispatch_process_unavailable",
                    "the selected physical runtime is unavailable before dispatch",
                    retryable=True,
                    metadata={
                        "process_status": process.status.value,
                        "side_effect_started": False,
                        "physical_attempt_id": context.attempt_id,
                        "selected_location": location,
                        "selected_profile": profile.value,
                        "process_manager_identity": (
                            f"{id(self.process_manager):x}"
                        ),
                        "observed_profiles": list(observed),
                    },
                )
            process, client, health = self.process_manager.start_node(
                self.catalog.policy(profile)
            )
            health = dict(health)
            semantic = dict(client.semantic_readiness())
            operations = tuple(
                str(item) for item in semantic.get("operations") or ()
            )
            runtime_current = (
                self.task.operation != "phase2-operator-execution"
                or semantic.get("runtime_implementation_version")
                == PHASE2_OPERATOR_RUNTIME_VERSION
            )
            if self.task.operation not in operations or not runtime_current:
                # A managed node may predate the active release.  Restart that
                # exact profile once so the operation catalog is loaded from
                # the current immutable source before any side effect begins.
                process, client, health = self.process_manager.start_node(
                    self.catalog.policy(profile),
                    restart=True,
                )
                health = dict(health)
                semantic = dict(client.semantic_readiness())
                operations = tuple(
                    str(item) for item in semantic.get("operations") or ()
                )
                runtime_current = (
                    self.task.operation != "phase2-operator-execution"
                    or semantic.get("runtime_implementation_version")
                    == PHASE2_OPERATOR_RUNTIME_VERSION
                )
            if self.task.operation not in operations or not runtime_current:
                raise OperatorPlacementError(
                    "physical_dispatch_operation_unavailable",
                    "the selected deployment node does not expose the requested operator operation",
                    retryable=True,
                    metadata={
                        "operation": self.task.operation,
                        "side_effect_started": False,
                    },
                )
            blockers = tuple(str(item) for item in health.get("blockers") or ())
            if blockers:
                raise OperatorPlacementError(
                    "physical_dispatch_node_unhealthy",
                    "the selected deployment node failed its pre-dispatch health gate",
                    retryable=True,
                    metadata={
                        "blockers": list(blockers),
                        "side_effect_started": False,
                    },
                )
            if (
                profile is DeploymentProfile.CLOUD
                and not any(
                    bool(value)
                    for value in _mapping(
                        health.get("credential_presence")
                    ).values()
                )
            ):
                raise OperatorPlacementError(
                    "physical_dispatch_cloud_credential_missing",
                    "cloud dispatch has no real provider credential",
                    retryable=True,
                    metadata={"side_effect_started": False},
                )
        except (DeploymentError, OperatorPlacementError) as error:
            self._record_failure(
                context=context,
                location=location,
                code=str(getattr(error, "code", type(error).__name__)),
                process=locals().get("process"),
                health=locals().get("health", {}),
                side_effect_started=False,
                retry_safe=True,
            )
            if isinstance(error, OperatorPlacementError):
                raise
            raise OperatorPlacementError(
                str(getattr(error, "code", "physical_dispatch_preflight_failed")),
                "physical deployment node preflight failed",
                retryable=True,
                metadata={
                    "side_effect_started": False,
                    "physical_attempt_id": context.attempt_id,
                },
            ) from error
        sensitivity = _PRIVACY_TO_SENSITIVITY[self.task.privacy_class]
        provider_required = profile is DeploymentProfile.CLOUD
        workload = Workload(
            workload_id=f"physical-workload:{context.operator_idempotency_key}:{location}",
            task_id=self.task.task_id,
            run_id=self.task.run_id,
            operation=self.task.operation,
            payload=thaw_json(self.task.payload),
            sensitivity=sensitivity,
            complexity=1,
            latency_sla_ms=self.task.latency_sla_ms,
            cpu_units=1,
            memory_mb=64,
            required_capabilities=(
                ("provider-dispatch",)
                if provider_required
                else ("deterministic-transform",)
            ),
            provider_required=provider_required,
            preferred_provider=(self.task.provider_id if provider_required else ""),
            preferred_model=(self.task.model_id if provider_required else ""),
            idempotency_key=(
                f"physical:{context.operator_idempotency_key}:{location}"
            ),
        )
        alternatives = tuple(
            PlacementCandidate(
                profile=item,
                admitted=item is profile,
                score=1_000 if item is profile else 0,
                reasons=(
                    "selected_by_resource_scheduler",
                    f"privacy:{self.task.privacy_class}",
                )
                if item is profile
                else ("not_selected_by_resource_scheduler",),
                blockers=()
                if item is profile
                else ("not_selected",),
                model_split={
                    "provider": self.task.provider_id if item is DeploymentProfile.CLOUD else "",
                    "model": self.task.model_id if item is DeploymentProfile.CLOUD else "",
                },
                observed_latency_ms=int(
                    _mapping(health.get("network")).get("latency_budget_ms")
                    or 0
                )
                if item is profile
                else 0,
                available_memory_mb=int(
                    _mapping(health.get("resource")).get("memory_mb") or 0
                )
                if item is profile
                else 0,
                credential_ready=(
                    any(
                        bool(value)
                        for value in _mapping(
                            health.get("credential_presence")
                        ).values()
                    )
                    if item is DeploymentProfile.CLOUD and item is profile
                    else item is not DeploymentProfile.CLOUD
                ),
            )
            for item in DeploymentProfile
        )
        decision = PlacementDecision(
            decision_id=context.placement_decision_id,
            workload_id=workload.workload_id,
            selected_profile=profile,
            candidates=alternatives,
            policy_version=PHYSICAL_DISPATCH_MECHANISM_VERSION,
            policy_digest=self.configuration_digest,
            created_at=context.call_started_at or context.attempt_started_at,
        )
        self._prepared[context.attempt_id] = _PreparedPhysicalDispatch(
            context=context,
            profile=profile,
            location=location,
            process=process,
            client=client,
            health=health,
            workload=workload,
            decision=decision,
        )

    def execute(self, context: OperatorLeaseExecutionContext) -> OperatorCallResult:
        prepared = self._prepared.pop(context.attempt_id, None)
        if prepared is None:
            raise OperatorPlacementError(
                "physical_dispatch_not_prepared",
                "physical dispatch did not pass preflight before execution",
            )
        prepared.context = context
        try:
            dispatch = self.dispatch_runtime.dispatch(
                prepared.workload,
                prepared.decision,
                prepared.client,
                predecessor_attempt_id=(
                    self.failure_receipts[-1].physical_attempt_id
                    if self.failure_receipts
                    else ""
                ),
                timeout_seconds=self.task.dispatch_timeout_seconds,
                attempt_id=context.attempt_id,
            )
        except DeploymentError as error:
            failure = self._record_failure(
                context=context,
                location=prepared.location,
                code=error.code,
                process=prepared.process,
                health=prepared.health,
                side_effect_started=True,
                retry_safe=False,
            )
            raise OperatorPlacementError(
                "physical_dispatch_outcome_unknown",
                "physical dispatch crossed the node transport boundary without a receipt",
                metadata={
                    "side_effect_started": True,
                    "physical_attempt_id": failure.physical_attempt_id,
                },
            ) from error
        if dispatch.status is not DispatchStatus.SUCCEEDED:
            retry_safe = dispatch.failure_code in {
                "node_network_unavailable",
                "node_provider_credential_missing",
                "node_provider_failure",
                "node_concurrency_exhausted",
            }
            failure = self._record_failure(
                context=context,
                location=prepared.location,
                code=dispatch.failure_code or "physical_dispatch_failed",
                process=prepared.process,
                health=prepared.health,
                side_effect_started=not retry_safe,
                retry_safe=retry_safe,
            )
            raise OperatorPlacementError(
                failure.failure_code,
                "the physical deployment node returned a failed attempt",
                retryable=retry_safe,
                metadata={
                    "side_effect_started": not retry_safe,
                    "physical_attempt_id": failure.physical_attempt_id,
                    "node_error": dict(dispatch.result),
                },
            )
        output = _mapping(dispatch.result.get("output"))
        if self.task.operation == "phase2-operator-execution":
            code_worker_execution = (
                output.get("operator_adapter_id")
                == "worker.code-worker.typescript-provider-tool-loop"
            )
            execution_verified = bool(
                output.get("domain_effect_performed") is True
                and output.get("output_contract_fulfilled") is True
                and output.get("operator_execution_body")
                and output.get("operator_execution_digest")
                and output.get("domain_result")
                and output.get("domain_artifact")
                and (
                    not code_worker_execution
                    or (
                        _mapping(output.get("provider_call")).get(
                            "task_execution_verified"
                        )
                        is True
                        and _mapping(output.get("provider_call")).get(
                            "prompt_goal_bound"
                        )
                        is True
                        and _mapping(output.get("provider_call")).get(
                            "synthetic_usage"
                        )
                        is False
                    )
                )
            )
        else:
            execution_verified = output.get("marker_verified") is True
        if not execution_verified:
            raise OperatorPlacementError(
                "physical_dispatch_verifier_failed",
                "physical dispatch output failed the canonical execution verifier",
            )
        verifier_body = {
            "schema": "zyra.physical-dispatch-verifier/v1",
            "verifier_id": self.task.verifier_id,
            "run_id": self.task.run_id,
            "task_id": self.task.task_id,
            "physical_attempt_id": context.attempt_id,
            "call_ref": dispatch.dispatch_id,
            "artifact_refs": list(dispatch.artifact_refs),
            "task_payload_digest": output.get("task_payload_digest"),
            "verification_kind": (
                "task-bound-provider-execution"
                if self.task.operation == "phase2-operator-execution"
                else "marker-proof"
            ),
            "provider_request_ids": [
                str(item.get("request_id") or "")
                for item in _mapping(output.get("provider_call")).get("calls")
                or ()
                if isinstance(item, Mapping)
                and str(item.get("request_id") or "")
            ],
            "operator_execution_digest": output.get(
                "operator_execution_digest"
            ),
            "operator_adapter_id": output.get("operator_adapter_id"),
            "domain_result_digest": _mapping(
                output.get("operator_execution_body")
            ).get("domain_result_digest"),
            "domain_artifact_digest": _mapping(
                output.get("domain_artifact")
            ).get("content_digest"),
            "passed": execution_verified,
            "verified_at": dispatch.completed_at,
        }
        verifier_ref = self.evidence_store.save(
            kind="verifier",
            identity=context.attempt_id,
            payload=verifier_body,
        )
        receipt = self.builder.build(
            task=self.task,
            prepared=prepared,
            dispatch=dispatch,
            verifier_ref=verifier_ref,
            recovery_evidence=self.failure_receipts,
        )
        validation = self.validator.validate(receipt)
        if not validation.real_gate_closed:
            raise OperatorPlacementError(
                "physical_dispatch_receipt_invalid",
                "physical dispatch receipt failed the real-execution gate",
                metadata={
                    "blockers": list(validation.blockers),
                    "execution_timestamps": {
                        key: receipt.input_signals.get(key)
                        for key in (
                            "lease_acquired_at",
                            "attempt_started_at",
                            "call_started_at",
                            "call_finished_at",
                        )
                    },
                },
            )
        receipt_ref = self.evidence_store.save(
            kind="receipt",
            identity=context.attempt_id,
            payload=receipt.to_dict(),
        )
        self._receipts.append(receipt)
        self._validation_reports.append(validation)
        provider = dict(receipt.provider_evidence)
        usage = _mapping(provider.get("usage"))
        latency_ms = max(
            0,
            int(provider.get("latency_ms") or _duration_ms(
                dispatch.started_at,
                dispatch.completed_at,
            )),
        )
        return OperatorCallResult(
            operator_ref=context.operator_ref,
            call_ref=receipt.call_receipt.uri,
            artifact_refs=(receipt.artifact_ref,),
            verification_refs=(receipt.verifier_ref.ref_id,),
            actual_tokens=int(usage.get("total_tokens") or 0),
            actual_cost_usd=float(provider.get("cost_usd") or 0),
            actual_latency_ms=latency_ms,
            summary=(
                f"real {prepared.location} physical dispatch verified "
                f"for attempt {context.attempt_id}"
            ),
            call_finished_at=dispatch.completed_at,
            metadata=FrozenDict(
                {
                    "physical_dispatch_receipt_ref": receipt_ref.uri,
                    "physical_dispatch_receipt_digest": receipt.digest,
                    "physical_dispatch_validation": validation.to_dict(),
                    "physical_attempt_id": context.attempt_id,
                    "physical_location": prepared.location,
                    "provider_request_id": provider.get("request_id", ""),
                    "simulated": False,
                    "semantic_only": False,
                    "workload_operation": self.task.operation,
                    "task_payload_digest": output.get(
                        "task_payload_digest", ""
                    ),
                    "operator_execution_digest": output.get(
                        "operator_execution_digest", ""
                    ),
                    "execution_output": output,
                }
            ),
        )

    def _record_failure(
        self,
        *,
        context: OperatorLeaseExecutionContext,
        location: str,
        code: str,
        process: ProcessRecord | None,
        health: Mapping[str, Any],
        side_effect_started: bool,
        retry_safe: bool,
    ) -> PhysicalDispatchFailureReceipt:
        runtime = _mapping(health.get("runtime_identity"))
        failures = self._failures.setdefault(self.task.task_id, [])
        receipt = PhysicalDispatchFailureReceipt(
            run_id=context.run_id,
            task_id=context.task_id,
            placement_decision_id=context.placement_decision_id,
            lease_id=context.lease_id,
            physical_attempt_id=context.attempt_id,
            location=location,
            failure_code=code,
            failure_boundary_id=str(
                runtime.get("failure_boundary_id")
                or (
                    f"process:{process.pid}:{process.generation_id}"
                    if process is not None
                    else f"unreachable:{location}"
                )
            ),
            node_id=str(
                health.get("node_id")
                or (process.component_id if process is not None else "")
            ),
            pid=int(
                health.get("pid") or (process.pid if process is not None else 0)
            ),
            endpoint=str(
                health.get("endpoint")
                or (process.endpoint if process is not None else "")
            ),
            side_effect_started=side_effect_started,
            retry_safe=retry_safe,
            observed_at=_now_iso(),
            predecessor_attempt_id=(
                failures[-1].physical_attempt_id if failures else ""
            ),
        )
        failures.append(receipt)
        self.evidence_store.save(
            kind="failure",
            identity=context.attempt_id,
            payload=receipt.to_dict(),
        )
        return receipt

    def close(self) -> None:
        self.process_manager.stop_all()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _digest_hex(value: str) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("sha256:"):
        text = text[7:]
    return (
        text
        if len(text) == 64 and all(character in "0123456789abcdef" for character in text)
        else ""
    )


def _sha256_bytes(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _ordered_timestamps(*values: str) -> bool:
    try:
        parsed = tuple(_parse_time(value) for value in values)
    except (TypeError, ValueError):
        return False
    return all(left <= right for left, right in zip(parsed, parsed[1:]))


def _duration_ms(started_at: str, completed_at: str) -> int:
    try:
        return max(
            0,
            round(
                (_parse_time(completed_at) - _parse_time(started_at)).total_seconds()
                * 1000
            ),
        )
    except (TypeError, ValueError):
        return 0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = [
    "PHYSICAL_DISPATCH_MECHANISM_ID",
    "PHYSICAL_DISPATCH_MECHANISM_VERSION",
    "PhysicalDispatchCallPort",
    "PhysicalDispatchEvidenceStore",
    "PhysicalDispatchFailureReceipt",
    "PhysicalDispatchReceiptBuilder",
    "PhysicalDispatchReceiptValidator",
    "PhysicalDispatchTask",
    "PhysicalDispatchValidationReport",
    "PhysicalRerouteValidationReport",
]
