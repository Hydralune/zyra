from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


class DeploymentProfile(StrEnum):
    DEVICE = "device"
    EDGE = "edge"
    CLOUD = "cloud"


class NetworkMode(StrEnum):
    OFFLINE = "offline"
    LIMITED = "limited"
    PRIVATE = "private"
    PUBLIC = "public"


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class LifecycleStatus(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    CRASHED = "crashed"
    STOPPING = "stopping"
    UNKNOWN = "unknown"


class ProbeStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class DispatchStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    MIGRATED = "migrated"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class ResourceEnvelope:
    cpu_percent: int
    memory_mb: int
    max_concurrency: int
    disk_mb: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProfilePolicy:
    profile: DeploymentProfile
    host: str
    port: int
    network_mode: NetworkMode
    latency_budget_ms: int
    resource: ResourceEnvelope
    allowed_sensitivity: tuple[Sensitivity, ...]
    capabilities: tuple[str, ...]
    providers: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    credential_environment: tuple[str, ...] = ()
    allow_checkpoint_export: bool = True
    allow_checkpoint_import: bool = True
    required: bool = True

    def public_dict(self, *, credential_presence: Mapping[str, bool] | None = None) -> dict[str, Any]:
        return {
            "profile": self.profile.value,
            "host": self.host,
            "port": self.port,
            "endpoint": f"http://{self.host}:{self.port}",
            "network_mode": self.network_mode.value,
            "latency_budget_ms": self.latency_budget_ms,
            "resource": self.resource.to_dict(),
            "allowed_sensitivity": [item.value for item in self.allowed_sensitivity],
            "capabilities": list(self.capabilities),
            "providers": list(self.providers),
            "models": list(self.models),
            "credential_environment": list(self.credential_environment),
            "credential_presence": dict(credential_presence or {}),
            "allow_checkpoint_export": self.allow_checkpoint_export,
            "allow_checkpoint_import": self.allow_checkpoint_import,
            "required": self.required,
        }

    @property
    def configuration_digest(self) -> str:
        return digest(self.public_dict())


@dataclass(frozen=True, slots=True)
class Workload:
    workload_id: str
    task_id: str
    run_id: str
    operation: str
    payload: Mapping[str, Any]
    sensitivity: Sensitivity
    complexity: int
    latency_sla_ms: int
    cpu_units: int
    memory_mb: int
    required_capabilities: tuple[str, ...] = ()
    provider_required: bool = False
    preferred_provider: str = ""
    preferred_model: str = ""
    checkpoint_ref: str = ""
    idempotency_key: str = ""
    created_at: str = field(default_factory=now_iso)

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "workload_id": self.workload_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "operation": self.operation,
            "payload": dict(self.payload),
            "sensitivity": self.sensitivity.value,
            "complexity": self.complexity,
            "latency_sla_ms": self.latency_sla_ms,
            "cpu_units": self.cpu_units,
            "memory_mb": self.memory_mb,
            "required_capabilities": list(self.required_capabilities),
            "provider_required": self.provider_required,
            "preferred_provider": self.preferred_provider,
            "preferred_model": self.preferred_model,
            "checkpoint_ref": self.checkpoint_ref,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at,
        }

    @property
    def workload_digest(self) -> str:
        return digest(self.semantic_dict())


@dataclass(frozen=True, slots=True)
class PlacementCandidate:
    profile: DeploymentProfile
    admitted: bool
    score: int
    reasons: tuple[str, ...]
    blockers: tuple[str, ...]
    model_split: Mapping[str, Any]
    observed_latency_ms: int
    available_memory_mb: int
    credential_ready: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.value,
            "admitted": self.admitted,
            "score": self.score,
            "reasons": list(self.reasons),
            "blockers": list(self.blockers),
            "model_split": dict(self.model_split),
            "observed_latency_ms": self.observed_latency_ms,
            "available_memory_mb": self.available_memory_mb,
            "credential_ready": self.credential_ready,
        }


@dataclass(frozen=True, slots=True)
class PlacementDecision:
    decision_id: str
    workload_id: str
    selected_profile: DeploymentProfile
    candidates: tuple[PlacementCandidate, ...]
    policy_version: str
    policy_digest: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.deployment-placement/v1",
            "decision_id": self.decision_id,
            "workload_id": self.workload_id,
            "selected_profile": self.selected_profile.value,
            "candidates": [item.to_dict() for item in self.candidates],
            "policy_version": self.policy_version,
            "policy_digest": self.policy_digest,
            "created_at": self.created_at,
            "fallback": False,
        }


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    component_id: str
    profile: str
    pid: int
    generation_id: str
    command_digest: str
    endpoint: str
    status: LifecycleStatus
    started_at: str
    observed_at: str
    exit_code: int | None = None
    restart_count: int = 0
    log_path: str = ""
    configuration_digest: str = ""
    process_create_time: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value


@dataclass(frozen=True, slots=True)
class NodeObservation:
    node_id: str
    profile: DeploymentProfile
    generation_id: str
    pid: int
    endpoint: str
    status: LifecycleStatus
    capabilities: tuple[str, ...]
    heartbeat_sequence: int
    resource: Mapping[str, Any]
    network: Mapping[str, Any]
    credential_presence: Mapping[str, bool]
    observed_at: str
    nonce_window_size: int = 0
    active_dispatches: int = 0
    completed_dispatches: int = 0
    configuration_digest: str = ""
    semantic_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "profile": self.profile.value,
            "generation_id": self.generation_id,
            "pid": self.pid,
            "endpoint": self.endpoint,
            "status": self.status.value,
            "capabilities": list(self.capabilities),
            "heartbeat_sequence": self.heartbeat_sequence,
            "resource": dict(self.resource),
            "network": dict(self.network),
            "credential_presence": dict(self.credential_presence),
            "observed_at": self.observed_at,
            "nonce_window_size": self.nonce_window_size,
            "active_dispatches": self.active_dispatches,
            "completed_dispatches": self.completed_dispatches,
            "configuration_digest": self.configuration_digest,
            "semantic_digest": self.semantic_digest,
        }


@dataclass(frozen=True, slots=True)
class DispatchReceipt:
    dispatch_id: str
    attempt_id: str
    workload_id: str
    task_id: str
    run_id: str
    profile: DeploymentProfile
    node_id: str
    status: DispatchStatus
    started_at: str
    completed_at: str
    result: Mapping[str, Any]
    artifact_refs: tuple[str, ...]
    checkpoint_ref: str
    predecessor_attempt_id: str = ""
    failure_code: str = ""
    degraded: bool = False
    result_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema": "zyra.deployment-dispatch-receipt/v1",
            "dispatch_id": self.dispatch_id,
            "attempt_id": self.attempt_id,
            "workload_id": self.workload_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "profile": self.profile.value,
            "node_id": self.node_id,
            "status": self.status.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result": dict(self.result),
            "artifact_refs": list(self.artifact_refs),
            "checkpoint_ref": self.checkpoint_ref,
            "predecessor_attempt_id": self.predecessor_attempt_id,
            "failure_code": self.failure_code,
            "degraded": self.degraded,
            "result_digest": self.result_digest,
            "fallback": False,
        }
        return value


@dataclass(frozen=True, slots=True)
class ProbeResult:
    probe_id: str
    status: ProbeStatus
    summary: str
    started_at: str
    completed_at: str
    latency_ms: int
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    observations: Mapping[str, Any] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "status": self.status.value,
            "summary": self.summary,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "latency_ms": self.latency_ms,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "observations": dict(self.observations),
            "dependencies": list(self.dependencies),
        }


@dataclass(frozen=True, slots=True)
class SemanticHealthReport:
    report_id: str
    ready: bool
    status: ProbeStatus
    profile_digest: str
    target_commit: str
    probes: tuple[ProbeResult, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    started_at: str
    completed_at: str
    fresh_state: bool
    short_task_id: str = ""
    report_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.semantic-health-report/v1",
            "report_id": self.report_id,
            "ready": self.ready,
            "status": self.status.value,
            "profile_digest": self.profile_digest,
            "target_commit": self.target_commit,
            "probes": [item.to_dict() for item in self.probes],
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "fresh_state": self.fresh_state,
            "short_task_id": self.short_task_id,
            "report_digest": self.report_digest,
            "fallback": False,
        }


def require_non_empty_sequence(values: Sequence[str], name: str) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))
    if not normalized:
        raise ValueError(f"{name} cannot be empty")
    return normalized


__all__ = [
    "DeploymentProfile",
    "DispatchReceipt",
    "DispatchStatus",
    "LifecycleStatus",
    "NetworkMode",
    "NodeObservation",
    "PlacementCandidate",
    "PlacementDecision",
    "ProbeResult",
    "ProbeStatus",
    "ProcessRecord",
    "ProfilePolicy",
    "ResourceEnvelope",
    "SemanticHealthReport",
    "Sensitivity",
    "Workload",
    "canonical_json",
    "digest",
    "new_id",
    "now_iso",
    "require_non_empty_sequence",
]
