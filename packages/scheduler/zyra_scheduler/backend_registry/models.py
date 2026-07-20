from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence


class BackendKind(StrEnum):
    LOCAL_PROCESS = "local_process"
    DOCKER = "docker"
    EDGE_HTTP = "edge_http"
    CLOUD_HTTP = "cloud_http"


class BackendLocation(StrEnum):
    LOCAL = "local"
    EDGE = "edge"
    CLOUD = "cloud"


class BackendHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    QUARANTINED = "quarantined"
    DISABLED = "disabled"


class BackendFailureKind(StrEnum):
    BACKEND_UNAVAILABLE = "backend_unavailable"
    BACKEND_TIMEOUT = "backend_timeout"
    BACKEND_CAPACITY = "backend_capacity"
    BACKEND_PROTOCOL = "backend_protocol"
    WORKSPACE_UNAVAILABLE = "workspace_unavailable"
    WORKSPACE_CORRUPT = "workspace_corrupt"
    LEASE_CONFLICT = "lease_conflict"
    LEASE_EXPIRED = "lease_expired"
    TURN_TIMEOUT = "turn_timeout"
    EXECUTION_FAILED = "execution_failed"
    DISPATCH_ABORTED = "dispatch_aborted"
    PROVIDER_FAILURE = "provider_failure"


class BackendRecoveryIntent(StrEnum):
    NONE = "none"
    RETRY_BACKEND = "retry_backend"
    CHANGE_BACKEND = "change_backend"
    REBUILD_WORKSPACE = "rebuild_workspace"
    CHANGE_PROVIDER_ROUTE = "change_provider_route"
    RECONCILE = "reconcile"


@dataclass(frozen=True, slots=True)
class BackendResourceLimits:
    maximum_concurrency: int = 1
    turn_timeout_seconds: float = 300.0
    connect_timeout_seconds: float = 10.0
    health_timeout_seconds: float = 3.0
    memory_megabytes: int | None = None
    cpu_millicores: int | None = None


@dataclass(frozen=True, slots=True)
class WorkspacePolicy:
    scope: str = "task"
    read_only: bool = False
    artifact_only: bool = False
    require_existing: bool = True
    require_writable: bool = True
    isolation: str = "workspace"


@dataclass(frozen=True, slots=True)
class BackendDefinition:
    backend_id: str
    display_name: str
    kind: BackendKind
    location: BackendLocation
    runtime_worker: str
    capabilities: Sequence[str] = field(default_factory=tuple)
    endpoint: str | None = None
    health_endpoint: str | None = None
    command: Sequence[str] = field(default_factory=tuple)
    docker_image: str | None = None
    workspace_policy: WorkspacePolicy = field(default_factory=WorkspacePolicy)
    limits: BackendResourceLimits = field(default_factory=BackendResourceLimits)
    enabled: bool = True
    priority: int = 0
    cost_weight: float = 0.0
    latency_weight: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["kind"] = self.kind.value
        value["location"] = self.location.value
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BackendDefinition:
        return cls(
            backend_id=str(value["backend_id"]),
            display_name=str(value["display_name"]),
            kind=BackendKind(str(value["kind"])),
            location=BackendLocation(str(value["location"])),
            runtime_worker=str(value["runtime_worker"]),
            capabilities=tuple(str(item) for item in value.get("capabilities", ())),
            endpoint=(str(value["endpoint"]) if value.get("endpoint") else None),
            health_endpoint=(str(value["health_endpoint"]) if value.get("health_endpoint") else None),
            command=tuple(str(item) for item in value.get("command", ())),
            docker_image=(str(value["docker_image"]) if value.get("docker_image") else None),
            workspace_policy=WorkspacePolicy(**dict(value.get("workspace_policy") or {})),
            limits=BackendResourceLimits(**dict(value.get("limits") or {})),
            enabled=bool(value.get("enabled", True)),
            priority=int(value.get("priority", 0)),
            cost_weight=float(value.get("cost_weight", 0.0)),
            latency_weight=float(value.get("latency_weight", 0.0)),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class BackendHealthRecord:
    backend_id: str
    status: BackendHealthStatus
    revision: int
    consecutive_failures: int = 0
    success_count: int = 0
    failure_count: int = 0
    current_leases: int = 0
    latency_milliseconds: float = 0.0
    reason: str = ""
    last_checked_at: float = 0.0
    last_success_at: float | None = None
    last_failure_at: float | None = None
    quarantine_until: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BackendHealthRecord:
        return cls(
            backend_id=str(value["backend_id"]),
            status=BackendHealthStatus(str(value["status"])),
            revision=int(value["revision"]),
            consecutive_failures=int(value.get("consecutive_failures", 0)),
            success_count=int(value.get("success_count", 0)),
            failure_count=int(value.get("failure_count", 0)),
            current_leases=int(value.get("current_leases", 0)),
            latency_milliseconds=float(value.get("latency_milliseconds", 0.0)),
            reason=str(value.get("reason") or ""),
            last_checked_at=float(value.get("last_checked_at", 0.0)),
            last_success_at=(float(value["last_success_at"]) if value.get("last_success_at") is not None else None),
            last_failure_at=(float(value["last_failure_at"]) if value.get("last_failure_at") is not None else None),
            quarantine_until=(float(value["quarantine_until"]) if value.get("quarantine_until") is not None else None),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class BackendSelectionRequest:
    run_id: str
    task_id: str
    node_id: str | None
    runtime_worker: str
    preferred_backend_id: str | None
    required_capabilities: Sequence[str]
    allowed_locations: Sequence[BackendLocation]
    excluded_backend_ids: Sequence[str]
    workspace_root: str
    artifact_root: str
    provider_route_id: str | None
    turn_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BackendLease:
    lease_id: str
    backend_id: str
    registry_revision: int
    health_revision: int
    run_id: str
    task_id: str
    node_id: str | None
    runtime_worker: str
    workspace_root: str
    artifact_root: str
    provider_route_id: str | None
    turn_id: str
    acquired_at: float
    expires_at: float
    attempt_limit: int
    previous_lease_id: str | None
    reason: str
    checksum: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BackendDispatchEnvelope:
    schema: str
    envelope_id: str
    run_id: str
    task_id: str
    node_id: str | None
    turn_id: str
    runtime_worker: str
    backend_lease_id: str
    backend_id: str
    backend_kind: BackendKind
    backend_location: BackendLocation
    workspace_root: str
    artifact_root: str
    provider_route_id: str | None
    idempotency_key: str
    deadline_at: float
    attempt: int
    previous_envelope_id: str | None
    created_at: float
    metadata: Mapping[str, Any]
    checksum: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["backend_kind"] = self.backend_kind.value
        value["backend_location"] = self.backend_location.value
        return value


@dataclass(frozen=True, slots=True)
class BackendDispatchAttempt:
    attempt_id: str
    envelope_id: str
    lease_id: str
    backend_id: str
    attempt: int
    started_at: float
    completed_at: float | None
    outcome: str
    failure_kind: BackendFailureKind | None
    recovery_intent: BackendRecoveryIntent | None
    retryable: bool
    provider_route_changed: bool
    backend_changed: bool
    output_observed: bool
    reason: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["failure_kind"] = None if self.failure_kind is None else self.failure_kind.value
        value["recovery_intent"] = None if self.recovery_intent is None else self.recovery_intent.value
        return value


@dataclass(frozen=True, slots=True)
class BackendControlEvent:
    event_id: str
    event_type: str
    run_id: str
    task_id: str
    node_id: str | None
    lease_id: str | None
    envelope_id: str | None
    backend_id: str | None
    provider_route_id: str | None
    causation_id: str | None
    correlation_id: str
    created_at: float
    payload: Mapping[str, Any]
    payload_digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BackendDispatchError(RuntimeError):
    def __init__(
        self,
        kind: BackendFailureKind,
        message: str,
        *,
        retryable: bool,
        recovery_intent: BackendRecoveryIntent,
        backend_id: str = "",
        lease_id: str = "",
        provider_route_id: str = "",
        output_observed: bool = False,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.recovery_intent = recovery_intent
        self.backend_id = backend_id
        self.lease_id = lease_id
        self.provider_route_id = provider_route_id
        self.output_observed = output_observed
        self.detail = dict(detail or {})

    def safe_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "message": str(self),
            "retryable": self.retryable,
            "recovery_intent": self.recovery_intent.value,
            "backend_id": self.backend_id,
            "lease_id": self.lease_id,
            "provider_route_id": self.provider_route_id,
            "output_observed": self.output_observed,
            "detail": dict(self.detail),
        }


def new_backend_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def now_timestamp() -> float:
    return time.time()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def checksum(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
