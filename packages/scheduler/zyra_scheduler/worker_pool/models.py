from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence, TypeVar
from uuid import uuid4


TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,191}$")
HEX_PATTERN = re.compile(r"^[0-9a-f]+$")
JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
EnumT = TypeVar("EnumT", bound=StrEnum)


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_iso(value: datetime | None = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("timestamp is required")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def new_pool_id(prefix: str) -> str:
    normalized = require_token(prefix, "id prefix")
    return f"{normalized}_{uuid4().hex}"


def require_token(value: str, label: str) -> str:
    token = str(value or "").strip()
    if not token:
        raise ValueError(f"{label} is required")
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError(f"{label} contains unsupported characters")
    return token


def tokens(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({require_token(value, "token") for value in values if str(value or "").strip()}))


def positive(value: float, label: str, *, allow_zero: bool = True) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    if allow_zero and number < 0:
        raise ValueError(f"{label} must be non-negative")
    if not allow_zero and number <= 0:
        raise ValueError(f"{label} must be positive")
    return number


def canonical_json(value: Any) -> str:
    return json.dumps(to_primitive(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def to_primitive(value: Any) -> JsonValue:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {item.name: to_primitive(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): to_primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_primitive(item) for item in value]
    if isinstance(value, datetime):
        return utc_iso(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


class WorkerLocation(StrEnum):
    LOCAL = "local"
    EDGE = "edge"
    CLOUD = "cloud"


class WorkerLifecycleState(StrEnum):
    REGISTERED = "registered"
    STARTING = "starting"
    IDLE = "idle"
    BUSY = "busy"
    PARKED = "parked"
    DRAINING = "draining"
    STOPPING = "stopping"
    STOPPED = "stopped"
    LOST = "lost"
    FAILED = "failed"


class WorkerHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE = "stale"
    LOST = "lost"
    UNRECOVERABLE = "unrecoverable"


class AttemptState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"
    SUPERSEDED = "superseded"


class LeaseState(StrEnum):
    ACTIVE = "active"
    DRAINING = "draining"
    RELEASED = "released"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    FENCED = "fenced"


class InboxMessageState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    ACKNOWLEDGED = "acknowledged"
    REQUEUED = "requeued"
    DEAD_LETTERED = "dead_lettered"
    CANCELLED = "cancelled"


class WakeupState(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    DISPATCHED = "dispatched"
    REQUEUED = "requeued"
    DISCARDED = "discarded"


class ExecutionOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    FENCED = "fenced"
    REJECTED = "rejected"


class HealthDisposition(StrEnum):
    NONE = "none"
    OBSERVE = "observe"
    EXPIRE_LEASE = "expire_lease"
    REQUEUE_ATTEMPT = "requeue_attempt"
    REPLAN = "replan"
    TERMINATE_WORKER = "terminate_worker"


class CancellationTarget(StrEnum):
    LOGICAL_TASK = "logical_task"
    ATTEMPT = "attempt"
    LEASE = "lease"
    WORKER = "worker"
    BACKEND_DISPATCH = "backend_dispatch"
    GATEWAY_COMMAND = "gateway_command"


@dataclass(frozen=True, slots=True)
class ResourceVector:
    cpu_cores: float = 0.0
    memory_mb: int = 0
    gpu_units: float = 0.0
    disk_mb: int = 0
    network_mbps: float = 0.0
    process_slots: int = 0
    browser_slots: int = 0
    custom: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "cpu_cores", positive(self.cpu_cores, "cpu_cores"))
        object.__setattr__(self, "memory_mb", int(positive(self.memory_mb, "memory_mb")))
        object.__setattr__(self, "gpu_units", positive(self.gpu_units, "gpu_units"))
        object.__setattr__(self, "disk_mb", int(positive(self.disk_mb, "disk_mb")))
        object.__setattr__(self, "network_mbps", positive(self.network_mbps, "network_mbps"))
        object.__setattr__(self, "process_slots", int(positive(self.process_slots, "process_slots")))
        object.__setattr__(self, "browser_slots", int(positive(self.browser_slots, "browser_slots")))
        normalized = {
            require_token(str(key), "custom resource name"): positive(value, f"resource {key}")
            for key, value in dict(self.custom).items()
        }
        object.__setattr__(self, "custom", normalized)

    def fits(self, request: "ResourceVector") -> bool:
        if self.cpu_cores < request.cpu_cores or self.memory_mb < request.memory_mb:
            return False
        if self.gpu_units < request.gpu_units or self.disk_mb < request.disk_mb:
            return False
        if self.network_mbps < request.network_mbps:
            return False
        if self.process_slots < request.process_slots or self.browser_slots < request.browser_slots:
            return False
        return all(self.custom.get(key, 0.0) >= value for key, value in request.custom.items())

    def plus(self, other: "ResourceVector") -> "ResourceVector":
        custom = dict(self.custom)
        for key, value in other.custom.items():
            custom[key] = custom.get(key, 0.0) + value
        return ResourceVector(
            cpu_cores=self.cpu_cores + other.cpu_cores,
            memory_mb=self.memory_mb + other.memory_mb,
            gpu_units=self.gpu_units + other.gpu_units,
            disk_mb=self.disk_mb + other.disk_mb,
            network_mbps=self.network_mbps + other.network_mbps,
            process_slots=self.process_slots + other.process_slots,
            browser_slots=self.browser_slots + other.browser_slots,
            custom=custom,
        )

    def minus(self, other: "ResourceVector") -> "ResourceVector":
        if not self.fits(other):
            raise ValueError("resource subtraction would become negative")
        custom = dict(self.custom)
        for key, value in other.custom.items():
            custom[key] = custom.get(key, 0.0) - value
        return ResourceVector(
            cpu_cores=self.cpu_cores - other.cpu_cores,
            memory_mb=self.memory_mb - other.memory_mb,
            gpu_units=self.gpu_units - other.gpu_units,
            disk_mb=self.disk_mb - other.disk_mb,
            network_mbps=self.network_mbps - other.network_mbps,
            process_slots=self.process_slots - other.process_slots,
            browser_slots=self.browser_slots - other.browser_slots,
            custom=custom,
        )

    def utilization_against(self, capacity: "ResourceVector") -> Mapping[str, float]:
        def ratio(used: float, total: float) -> float:
            return 0.0 if total <= 0 else min(1.0, max(0.0, used / total))

        values = {
            "cpu_cores": ratio(self.cpu_cores, capacity.cpu_cores),
            "memory_mb": ratio(self.memory_mb, capacity.memory_mb),
            "gpu_units": ratio(self.gpu_units, capacity.gpu_units),
            "disk_mb": ratio(self.disk_mb, capacity.disk_mb),
            "network_mbps": ratio(self.network_mbps, capacity.network_mbps),
            "process_slots": ratio(self.process_slots, capacity.process_slots),
            "browser_slots": ratio(self.browser_slots, capacity.browser_slots),
        }
        values.update(
            {
                f"custom:{key}": ratio(value, capacity.custom.get(key, 0.0))
                for key, value in self.custom.items()
            }
        )
        return values

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.cpu_cores,
                self.memory_mb,
                self.gpu_units,
                self.disk_mb,
                self.network_mbps,
                self.process_slots,
                self.browser_slots,
                *self.custom.values(),
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(to_primitive(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "ResourceVector":
        data = dict(value or {})
        return cls(
            cpu_cores=float(data.get("cpu_cores") or 0),
            memory_mb=int(data.get("memory_mb") or 0),
            gpu_units=float(data.get("gpu_units") or 0),
            disk_mb=int(data.get("disk_mb") or 0),
            network_mbps=float(data.get("network_mbps") or 0),
            process_slots=int(data.get("process_slots") or 0),
            browser_slots=int(data.get("browser_slots") or 0),
            custom={str(key): float(item) for key, item in _mapping(data.get("custom")).items()},
        )


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    required: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    tool_ids: tuple[str, ...] = ()
    backend_kinds: tuple[str, ...] = ()
    locations: tuple[WorkerLocation, ...] = ()
    min_protocol_version: int = 1
    resources: ResourceVector = field(default_factory=ResourceVector)
    labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "required", tokens(self.required))
        object.__setattr__(self, "forbidden", tokens(self.forbidden))
        object.__setattr__(self, "tool_ids", tokens(self.tool_ids))
        object.__setattr__(self, "backend_kinds", tokens(self.backend_kinds))
        object.__setattr__(self, "locations", tuple(sorted(set(self.locations), key=lambda item: item.value)))
        object.__setattr__(self, "min_protocol_version", max(1, int(self.min_protocol_version)))
        object.__setattr__(self, "labels", {require_token(k, "label key"): str(v) for k, v in self.labels.items()})

    def to_dict(self) -> dict[str, Any]:
        return dict(to_primitive(self))


@dataclass(frozen=True, slots=True)
class WorkerCapabilityManifest:
    worker_id: str
    worker_kind: str
    location: WorkerLocation
    backend_ids: tuple[str, ...]
    backend_kinds: tuple[str, ...]
    capabilities: tuple[str, ...]
    tool_ids: tuple[str, ...]
    resource_capacity: ResourceVector
    protocol_version: int = 1
    manifest_revision: int = 1
    constraints: Mapping[str, JsonValue] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)
    allowed_agent_definitions: tuple[str, ...] = ()
    denied_agent_definitions: tuple[str, ...] = ()
    allowed_tool_patterns: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_iso)
    digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "worker_id", require_token(self.worker_id, "worker_id"))
        object.__setattr__(self, "worker_kind", require_token(self.worker_kind, "worker_kind"))
        object.__setattr__(self, "backend_ids", tokens(self.backend_ids))
        object.__setattr__(self, "backend_kinds", tokens(self.backend_kinds))
        object.__setattr__(self, "capabilities", tokens(self.capabilities))
        object.__setattr__(self, "tool_ids", tokens(self.tool_ids))
        object.__setattr__(self, "protocol_version", max(1, int(self.protocol_version)))
        object.__setattr__(self, "manifest_revision", max(1, int(self.manifest_revision)))
        object.__setattr__(self, "constraints", copy.deepcopy(dict(self.constraints)))
        object.__setattr__(self, "labels", {require_token(k, "label key"): str(v) for k, v in self.labels.items()})
        object.__setattr__(self, "allowed_agent_definitions", tokens(self.allowed_agent_definitions))
        object.__setattr__(self, "denied_agent_definitions", tokens(self.denied_agent_definitions))
        object.__setattr__(self, "allowed_tool_patterns", tokens(self.allowed_tool_patterns))
        expected = self.compute_digest()
        if self.digest and self.digest != expected:
            raise ValueError("worker capability manifest digest does not match content")
        object.__setattr__(self, "digest", expected)

    def compute_digest(self) -> str:
        data = {
            "worker_id": self.worker_id,
            "worker_kind": self.worker_kind,
            "location": self.location.value,
            "backend_ids": self.backend_ids,
            "backend_kinds": self.backend_kinds,
            "capabilities": self.capabilities,
            "tool_ids": self.tool_ids,
            "resource_capacity": self.resource_capacity,
            "protocol_version": self.protocol_version,
            "manifest_revision": self.manifest_revision,
            "constraints": self.constraints,
            "labels": self.labels,
            "allowed_agent_definitions": self.allowed_agent_definitions,
            "denied_agent_definitions": self.denied_agent_definitions,
            "allowed_tool_patterns": self.allowed_tool_patterns,
        }
        return stable_digest(data)

    def supports(self, requirement: CapabilityRequirement) -> tuple[bool, tuple[str, ...]]:
        failures: list[str] = []
        available = set(self.capabilities)
        if missing := sorted(set(requirement.required) - available):
            failures.append(f"missing capabilities: {', '.join(missing)}")
        if forbidden := sorted(set(requirement.forbidden) & available):
            failures.append(f"forbidden capabilities present: {', '.join(forbidden)}")
        if missing_tools := sorted(set(requirement.tool_ids) - set(self.tool_ids)):
            failures.append(f"missing tools: {', '.join(missing_tools)}")
        if requirement.backend_kinds and not set(requirement.backend_kinds).intersection(self.backend_kinds):
            failures.append("backend kind does not match")
        if requirement.locations and self.location not in requirement.locations:
            failures.append("worker location does not match")
        if self.protocol_version < requirement.min_protocol_version:
            failures.append("protocol version is too old")
        if not self.resource_capacity.fits(requirement.resources):
            failures.append("worker capacity cannot satisfy requested resources")
        for key, value in requirement.labels.items():
            if self.labels.get(key) != value:
                failures.append(f"label mismatch: {key}")
        return not failures, tuple(failures)

    def permits_agent(self, agent_definition_id: str) -> bool:
        token = require_token(agent_definition_id, "agent_definition_id")
        if token in self.denied_agent_definitions:
            return False
        return not self.allowed_agent_definitions or token in self.allowed_agent_definitions

    def to_dict(self) -> dict[str, Any]:
        return dict(to_primitive(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkerCapabilityManifest":
        data = dict(value)
        return cls(
            worker_id=str(data.get("worker_id") or ""),
            worker_kind=str(data.get("worker_kind") or ""),
            location=_enum(WorkerLocation, data.get("location"), WorkerLocation.LOCAL),
            backend_ids=tuple(_strings(data.get("backend_ids"))),
            backend_kinds=tuple(_strings(data.get("backend_kinds"))),
            capabilities=tuple(_strings(data.get("capabilities"))),
            tool_ids=tuple(_strings(data.get("tool_ids"))),
            resource_capacity=ResourceVector.from_dict(_mapping(data.get("resource_capacity"))),
            protocol_version=int(data.get("protocol_version") or 1),
            manifest_revision=int(data.get("manifest_revision") or 1),
            constraints=_mapping(data.get("constraints")),
            labels={str(k): str(v) for k, v in _mapping(data.get("labels")).items()},
            allowed_agent_definitions=tuple(_strings(data.get("allowed_agent_definitions"))),
            denied_agent_definitions=tuple(_strings(data.get("denied_agent_definitions"))),
            allowed_tool_patterns=tuple(_strings(data.get("allowed_tool_patterns"))),
            created_at=str(data.get("created_at") or utc_iso()),
            digest=str(data.get("digest") or ""),
        )


@dataclass(frozen=True, slots=True)
class CapabilityAttestation:
    worker_id: str
    manifest_digest: str
    process_identity: str
    challenge_nonce: str
    response_digest: str
    protocol_version: int
    endpoint: str = ""
    observed_at: str = field(default_factory=utc_iso)
    expires_at: str = ""
    attestation_id: str = field(default_factory=lambda: new_pool_id("attestation"))
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "worker_id", require_token(self.worker_id, "worker_id"))
        if len(self.manifest_digest) != 64 or not HEX_PATTERN.fullmatch(self.manifest_digest):
            raise ValueError("manifest_digest must be a SHA-256 hex digest")
        object.__setattr__(self, "process_identity", require_token(self.process_identity, "process_identity"))
        object.__setattr__(self, "challenge_nonce", require_token(self.challenge_nonce, "challenge_nonce"))
        if len(self.response_digest) != 64 or not HEX_PATTERN.fullmatch(self.response_digest):
            raise ValueError("response_digest must be a SHA-256 hex digest")
        object.__setattr__(self, "protocol_version", max(1, int(self.protocol_version)))
        if not self.expires_at:
            object.__setattr__(self, "expires_at", utc_iso(parse_utc(self.observed_at) + timedelta(minutes=10)))
        if parse_utc(self.expires_at) <= parse_utc(self.observed_at):
            raise ValueError("attestation expiry must follow observation")
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))

    @property
    def expired(self) -> bool:
        return parse_utc(self.expires_at) <= utc_now()

    def to_dict(self) -> dict[str, Any]:
        return dict(to_primitive(self))


@dataclass(frozen=True, slots=True)
class WorkerInstance:
    worker_id: str
    worker_kind: str
    location: WorkerLocation
    backend_id: str
    state: WorkerLifecycleState = WorkerLifecycleState.REGISTERED
    generation: int = 1
    version: int = 1
    manifest_digest: str = ""
    attestation_id: str = ""
    endpoint: str = ""
    process_identity: str = ""
    owner_session_id: str = ""
    registered_at: str = field(default_factory=utc_iso)
    started_at: str = ""
    stopped_at: str = ""
    last_heartbeat_at: str = ""
    drain_requested_at: str = ""
    failure_code: str = ""
    failure_reason: str = ""
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "worker_id", require_token(self.worker_id, "worker_id"))
        object.__setattr__(self, "worker_kind", require_token(self.worker_kind, "worker_kind"))
        object.__setattr__(self, "backend_id", require_token(self.backend_id, "backend_id"))
        object.__setattr__(self, "generation", max(1, int(self.generation)))
        object.__setattr__(self, "version", max(1, int(self.version)))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))

    @property
    def accepting_leases(self) -> bool:
        return self.state in {WorkerLifecycleState.IDLE, WorkerLifecycleState.BUSY}

    @property
    def terminal(self) -> bool:
        return self.state in {WorkerLifecycleState.STOPPED, WorkerLifecycleState.FAILED}

    def advance(self, state: WorkerLifecycleState, **changes: Any) -> "WorkerInstance":
        return replace(self, state=state, version=self.version + 1, **changes)

    def to_dict(self) -> dict[str, Any]:
        return dict(to_primitive(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkerInstance":
        data = dict(value)
        return cls(
            worker_id=str(data.get("worker_id") or ""),
            worker_kind=str(data.get("worker_kind") or ""),
            location=_enum(WorkerLocation, data.get("location"), WorkerLocation.LOCAL),
            backend_id=str(data.get("backend_id") or ""),
            state=_enum(WorkerLifecycleState, data.get("state"), WorkerLifecycleState.REGISTERED),
            generation=int(data.get("generation") or 1),
            version=int(data.get("version") or 1),
            manifest_digest=str(data.get("manifest_digest") or ""),
            attestation_id=str(data.get("attestation_id") or ""),
            endpoint=str(data.get("endpoint") or ""),
            process_identity=str(data.get("process_identity") or ""),
            owner_session_id=str(data.get("owner_session_id") or ""),
            registered_at=str(data.get("registered_at") or utc_iso()),
            started_at=str(data.get("started_at") or ""),
            stopped_at=str(data.get("stopped_at") or ""),
            last_heartbeat_at=str(data.get("last_heartbeat_at") or ""),
            drain_requested_at=str(data.get("drain_requested_at") or ""),
            failure_code=str(data.get("failure_code") or ""),
            failure_reason=str(data.get("failure_reason") or ""),
            metadata=_mapping(data.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class TaskAttempt:
    task_id: str
    run_id: str
    attempt_number: int
    attempt_id: str = field(default_factory=lambda: new_pool_id("attempt"))
    state: AttemptState = AttemptState.PENDING
    worker_id: str = ""
    lease_id: str = ""
    backend_dispatch_id: str = ""
    parent_attempt_id: str = ""
    recovery_reason: str = ""
    created_at: str = field(default_factory=utc_iso)
    started_at: str = ""
    finished_at: str = ""
    version: int = 1
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", require_token(self.task_id, "task_id"))
        object.__setattr__(self, "run_id", require_token(self.run_id, "run_id"))
        object.__setattr__(self, "attempt_number", max(1, int(self.attempt_number)))
        object.__setattr__(self, "attempt_id", require_token(self.attempt_id, "attempt_id"))
        object.__setattr__(self, "version", max(1, int(self.version)))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))

    @property
    def terminal(self) -> bool:
        return self.state in {
            AttemptState.SUCCEEDED,
            AttemptState.FAILED,
            AttemptState.CANCELLED,
            AttemptState.SUPERSEDED,
        }

    def advance(self, state: AttemptState, **changes: Any) -> "TaskAttempt":
        return replace(self, state=state, version=self.version + 1, **changes)

    def to_dict(self) -> dict[str, Any]:
        return dict(to_primitive(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskAttempt":
        data = dict(value)
        return cls(
            task_id=str(data.get("task_id") or ""),
            run_id=str(data.get("run_id") or ""),
            attempt_number=int(data.get("attempt_number") or 1),
            attempt_id=str(data.get("attempt_id") or new_pool_id("attempt")),
            state=_enum(AttemptState, data.get("state"), AttemptState.PENDING),
            worker_id=str(data.get("worker_id") or ""),
            lease_id=str(data.get("lease_id") or ""),
            backend_dispatch_id=str(data.get("backend_dispatch_id") or ""),
            parent_attempt_id=str(data.get("parent_attempt_id") or ""),
            recovery_reason=str(data.get("recovery_reason") or ""),
            created_at=str(data.get("created_at") or utc_iso()),
            started_at=str(data.get("started_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
            version=int(data.get("version") or 1),
            metadata=_mapping(data.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class WorkerLease:
    task_id: str
    run_id: str
    attempt_id: str
    worker_id: str
    owner_session_id: str
    backend_id: str
    deadline_at: str
    resources: ResourceVector
    lease_id: str = field(default_factory=lambda: new_pool_id("lease"))
    state: LeaseState = LeaseState.ACTIVE
    fence_epoch: int = 1
    fence_token: str = field(default_factory=lambda: uuid4().hex)
    acquired_at: str = field(default_factory=utc_iso)
    renewed_at: str = ""
    released_at: str = ""
    cancel_requested_at: str = ""
    drain_requested_at: str = ""
    version: int = 1
    idempotency_key: str = ""
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("task_id", "run_id", "attempt_id", "worker_id", "owner_session_id", "backend_id"):
            object.__setattr__(self, name, require_token(getattr(self, name), name))
        object.__setattr__(self, "fence_epoch", max(1, int(self.fence_epoch)))
        object.__setattr__(self, "version", max(1, int(self.version)))
        if not self.fence_token or len(self.fence_token) < 16:
            raise ValueError("fence_token must contain at least 16 characters")
        if parse_utc(self.deadline_at) <= parse_utc(self.acquired_at):
            raise ValueError("lease deadline must follow acquisition")
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", stable_digest((self.task_id, self.attempt_id, self.worker_id)))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))

    @property
    def terminal(self) -> bool:
        return self.state in {LeaseState.RELEASED, LeaseState.EXPIRED, LeaseState.CANCELLED, LeaseState.FENCED}

    def expired_at(self, now: datetime | None = None) -> bool:
        return parse_utc(self.deadline_at) <= (now or utc_now())

    def assert_fence(self, *, worker_id: str, fence_token: str, fence_epoch: int) -> bool:
        return (
            self.state in {LeaseState.ACTIVE, LeaseState.DRAINING}
            and self.worker_id == worker_id
            and self.fence_token == fence_token
            and self.fence_epoch == fence_epoch
            and not self.expired_at()
        )

    def advance(self, state: LeaseState | None = None, **changes: Any) -> "WorkerLease":
        return replace(self, state=state or self.state, version=self.version + 1, **changes)

    def to_dict(self, *, include_fence_token: bool = False) -> dict[str, Any]:
        value = dict(to_primitive(self))
        if not include_fence_token:
            value["fence_token"] = ""
            value["fence_token_present"] = bool(self.fence_token)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkerLease":
        data = dict(value)
        return cls(
            task_id=str(data.get("task_id") or ""),
            run_id=str(data.get("run_id") or ""),
            attempt_id=str(data.get("attempt_id") or ""),
            worker_id=str(data.get("worker_id") or ""),
            owner_session_id=str(data.get("owner_session_id") or ""),
            backend_id=str(data.get("backend_id") or ""),
            deadline_at=str(data.get("deadline_at") or ""),
            resources=ResourceVector.from_dict(_mapping(data.get("resources"))),
            lease_id=str(data.get("lease_id") or new_pool_id("lease")),
            state=_enum(LeaseState, data.get("state"), LeaseState.ACTIVE),
            fence_epoch=int(data.get("fence_epoch") or 1),
            fence_token=str(data.get("fence_token") or ""),
            acquired_at=str(data.get("acquired_at") or utc_iso()),
            renewed_at=str(data.get("renewed_at") or ""),
            released_at=str(data.get("released_at") or ""),
            cancel_requested_at=str(data.get("cancel_requested_at") or ""),
            drain_requested_at=str(data.get("drain_requested_at") or ""),
            version=int(data.get("version") or 1),
            idempotency_key=str(data.get("idempotency_key") or ""),
            metadata=_mapping(data.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class WorkerTelemetry:
    worker_id: str
    sequence: int
    capacity: ResourceVector
    allocated: ResourceVector
    observed: ResourceVector = field(default_factory=ResourceVector)
    active_attempt_ids: tuple[str, ...] = ()
    queue_depth: int = 0
    process_uptime_ms: int = 0
    load_average: float = 0.0
    temperature_celsius: float | None = None
    observed_at: str = field(default_factory=utc_iso)
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "worker_id", require_token(self.worker_id, "worker_id"))
        object.__setattr__(self, "sequence", max(1, int(self.sequence)))
        object.__setattr__(self, "active_attempt_ids", tokens(self.active_attempt_ids))
        object.__setattr__(self, "queue_depth", int(positive(self.queue_depth, "queue_depth")))
        object.__setattr__(self, "process_uptime_ms", int(positive(self.process_uptime_ms, "process_uptime_ms")))
        object.__setattr__(self, "load_average", positive(self.load_average, "load_average"))
        if self.temperature_celsius is not None and not math.isfinite(float(self.temperature_celsius)):
            raise ValueError("temperature_celsius must be finite")
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))

    @property
    def overcommitted(self) -> bool:
        return not self.capacity.fits(self.allocated)

    def to_dict(self) -> dict[str, Any]:
        return dict(to_primitive(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkerTelemetry":
        data = dict(value)
        temperature = data.get("temperature_celsius")
        return cls(
            worker_id=str(data.get("worker_id") or ""),
            sequence=int(data.get("sequence") or 1),
            capacity=ResourceVector.from_dict(_mapping(data.get("capacity"))),
            allocated=ResourceVector.from_dict(_mapping(data.get("allocated"))),
            observed=ResourceVector.from_dict(_mapping(data.get("observed"))),
            active_attempt_ids=tuple(_strings(data.get("active_attempt_ids"))),
            queue_depth=int(data.get("queue_depth") or 0),
            process_uptime_ms=int(data.get("process_uptime_ms") or 0),
            load_average=float(data.get("load_average") or 0),
            temperature_celsius=float(temperature) if temperature is not None else None,
            observed_at=str(data.get("observed_at") or utc_iso()),
            metadata=_mapping(data.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class WorkerHeartbeat:
    worker_id: str
    worker_generation: int
    sequence: int
    manifest_digest: str
    active_lease_ids: tuple[str, ...]
    telemetry: WorkerTelemetry
    heartbeat_id: str = field(default_factory=lambda: new_pool_id("heartbeat"))
    observed_at: str = field(default_factory=utc_iso)
    process_identity: str = ""
    signature: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "worker_id", require_token(self.worker_id, "worker_id"))
        object.__setattr__(self, "worker_generation", max(1, int(self.worker_generation)))
        object.__setattr__(self, "sequence", max(1, int(self.sequence)))
        object.__setattr__(self, "active_lease_ids", tokens(self.active_lease_ids))
        if self.telemetry.worker_id != self.worker_id:
            raise ValueError("heartbeat telemetry belongs to another worker")

    def to_dict(self) -> dict[str, Any]:
        return dict(to_primitive(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WorkerHeartbeat":
        data = dict(value)
        return cls(
            worker_id=str(data.get("worker_id") or ""),
            worker_generation=int(data.get("worker_generation") or 1),
            sequence=int(data.get("sequence") or 1),
            manifest_digest=str(data.get("manifest_digest") or ""),
            active_lease_ids=tuple(_strings(data.get("active_lease_ids"))),
            telemetry=WorkerTelemetry.from_dict(_mapping(data.get("telemetry"))),
            heartbeat_id=str(data.get("heartbeat_id") or new_pool_id("heartbeat")),
            observed_at=str(data.get("observed_at") or utc_iso()),
            process_identity=str(data.get("process_identity") or ""),
            signature=str(data.get("signature") or ""),
        )


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    task_id: str
    run_id: str
    attempt_id: str
    lease_id: str
    worker_id: str
    fence_epoch: int
    outcome: ExecutionOutcome
    started_at: str
    finished_at: str
    summary: str
    receipt_id: str = field(default_factory=lambda: new_pool_id("execution_receipt"))
    artifact_refs: tuple[str, ...] = ()
    event_refs: tuple[str, ...] = ()
    backend_receipt_ref: str = ""
    gateway_receipt_ref: str = ""
    output_digest: str = ""
    error_code: str = ""
    error_message: str = ""
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("task_id", "run_id", "attempt_id", "lease_id", "worker_id"):
            object.__setattr__(self, name, require_token(getattr(self, name), name))
        object.__setattr__(self, "fence_epoch", max(1, int(self.fence_epoch)))
        object.__setattr__(self, "artifact_refs", tokens(self.artifact_refs))
        object.__setattr__(self, "event_refs", tokens(self.event_refs))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))
        if parse_utc(self.finished_at) < parse_utc(self.started_at):
            raise ValueError("execution receipt finishes before it starts")
        expected = stable_digest(
            {
                "task_id": self.task_id,
                "attempt_id": self.attempt_id,
                "lease_id": self.lease_id,
                "outcome": self.outcome,
                "summary": self.summary,
                "artifact_refs": self.artifact_refs,
                "error_code": self.error_code,
            }
        )
        if self.output_digest and self.output_digest != expected:
            raise ValueError("execution receipt output digest does not match content")
        object.__setattr__(self, "output_digest", expected)

    def to_dict(self) -> dict[str, Any]:
        return dict(to_primitive(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionReceipt":
        data = dict(value)
        return cls(
            task_id=str(data.get("task_id") or ""),
            run_id=str(data.get("run_id") or ""),
            attempt_id=str(data.get("attempt_id") or ""),
            lease_id=str(data.get("lease_id") or ""),
            worker_id=str(data.get("worker_id") or ""),
            fence_epoch=int(data.get("fence_epoch") or 1),
            outcome=_enum(ExecutionOutcome, data.get("outcome"), ExecutionOutcome.FAILED),
            started_at=str(data.get("started_at") or utc_iso()),
            finished_at=str(data.get("finished_at") or utc_iso()),
            summary=str(data.get("summary") or ""),
            receipt_id=str(data.get("receipt_id") or new_pool_id("execution_receipt")),
            artifact_refs=tuple(_strings(data.get("artifact_refs"))),
            event_refs=tuple(_strings(data.get("event_refs"))),
            backend_receipt_ref=str(data.get("backend_receipt_ref") or ""),
            gateway_receipt_ref=str(data.get("gateway_receipt_ref") or ""),
            output_digest=str(data.get("output_digest") or ""),
            error_code=str(data.get("error_code") or ""),
            error_message=str(data.get("error_message") or ""),
            metadata=_mapping(data.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class InboxEnvelope:
    worker_id: str
    task_id: str
    run_id: str
    message_kind: str
    payload: Mapping[str, JsonValue]
    envelope_id: str = field(default_factory=lambda: new_pool_id("inbox"))
    state: InboxMessageState = InboxMessageState.PENDING
    priority: int = 100
    available_at: str = field(default_factory=utc_iso)
    created_at: str = field(default_factory=utc_iso)
    claimed_at: str = ""
    acknowledged_at: str = ""
    claim_owner: str = ""
    claim_deadline_at: str = ""
    delivery_count: int = 0
    max_deliveries: int = 5
    idempotency_key: str = ""
    causation_id: str = ""
    correlation_id: str = ""
    failure_reason: str = ""
    version: int = 1

    def __post_init__(self) -> None:
        for name in ("worker_id", "task_id", "run_id", "message_kind"):
            object.__setattr__(self, name, require_token(getattr(self, name), name))
        object.__setattr__(self, "payload", copy.deepcopy(dict(self.payload)))
        object.__setattr__(self, "priority", int(self.priority))
        object.__setattr__(self, "delivery_count", int(positive(self.delivery_count, "delivery_count")))
        object.__setattr__(self, "max_deliveries", max(1, int(self.max_deliveries)))
        object.__setattr__(self, "version", max(1, int(self.version)))
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", stable_digest((self.worker_id, self.task_id, self.message_kind, self.payload)))

    @property
    def terminal(self) -> bool:
        return self.state in {
            InboxMessageState.ACKNOWLEDGED,
            InboxMessageState.DEAD_LETTERED,
            InboxMessageState.CANCELLED,
        }

    def to_dict(self) -> dict[str, Any]:
        return dict(to_primitive(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InboxEnvelope":
        data = dict(value)
        return cls(
            worker_id=str(data.get("worker_id") or ""),
            task_id=str(data.get("task_id") or ""),
            run_id=str(data.get("run_id") or ""),
            message_kind=str(data.get("message_kind") or ""),
            payload=_mapping(data.get("payload")),
            envelope_id=str(data.get("envelope_id") or new_pool_id("inbox")),
            state=_enum(InboxMessageState, data.get("state"), InboxMessageState.PENDING),
            priority=int(data.get("priority") or 100),
            available_at=str(data.get("available_at") or utc_iso()),
            created_at=str(data.get("created_at") or utc_iso()),
            claimed_at=str(data.get("claimed_at") or ""),
            acknowledged_at=str(data.get("acknowledged_at") or ""),
            claim_owner=str(data.get("claim_owner") or ""),
            claim_deadline_at=str(data.get("claim_deadline_at") or ""),
            delivery_count=int(data.get("delivery_count") or 0),
            max_deliveries=int(data.get("max_deliveries") or 5),
            idempotency_key=str(data.get("idempotency_key") or ""),
            causation_id=str(data.get("causation_id") or ""),
            correlation_id=str(data.get("correlation_id") or ""),
            failure_reason=str(data.get("failure_reason") or ""),
            version=int(data.get("version") or 1),
        )


@dataclass(frozen=True, slots=True)
class WakeupRecord:
    worker_id: str
    reason: str
    wakeup_id: str = field(default_factory=lambda: new_pool_id("wakeup"))
    envelope_id: str = ""
    task_id: str = ""
    state: WakeupState = WakeupState.QUEUED
    available_at: str = field(default_factory=utc_iso)
    created_at: str = field(default_factory=utc_iso)
    claimed_at: str = ""
    dispatched_at: str = ""
    claim_owner: str = ""
    attempts: int = 0
    idempotency_key: str = ""
    version: int = 1
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "worker_id", require_token(self.worker_id, "worker_id"))
        if self.task_id:
            object.__setattr__(self, "task_id", require_token(self.task_id, "task_id"))
        object.__setattr__(self, "attempts", int(positive(self.attempts, "attempts")))
        object.__setattr__(self, "version", max(1, int(self.version)))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", stable_digest((self.worker_id, self.envelope_id, self.reason)))

    def to_dict(self) -> dict[str, Any]:
        return dict(to_primitive(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "WakeupRecord":
        data = dict(value)
        return cls(
            worker_id=str(data.get("worker_id") or ""),
            reason=str(data.get("reason") or ""),
            wakeup_id=str(data.get("wakeup_id") or new_pool_id("wakeup")),
            envelope_id=str(data.get("envelope_id") or ""),
            task_id=str(data.get("task_id") or ""),
            state=_enum(WakeupState, data.get("state"), WakeupState.QUEUED),
            available_at=str(data.get("available_at") or utc_iso()),
            created_at=str(data.get("created_at") or utc_iso()),
            claimed_at=str(data.get("claimed_at") or ""),
            dispatched_at=str(data.get("dispatched_at") or ""),
            claim_owner=str(data.get("claim_owner") or ""),
            attempts=int(data.get("attempts") or 0),
            idempotency_key=str(data.get("idempotency_key") or ""),
            version=int(data.get("version") or 1),
            metadata=_mapping(data.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class HealthAssessment:
    worker_id: str
    status: WorkerHealthStatus
    disposition: HealthDisposition
    reason: str
    assessed_at: str = field(default_factory=utc_iso)
    stale_for_ms: int = 0
    active_lease_ids: tuple[str, ...] = ()
    recoverable: bool = True
    signal_id: str = field(default_factory=lambda: new_pool_id("health_signal"))
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "worker_id", require_token(self.worker_id, "worker_id"))
        object.__setattr__(self, "stale_for_ms", int(positive(self.stale_for_ms, "stale_for_ms")))
        object.__setattr__(self, "active_lease_ids", tokens(self.active_lease_ids))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return dict(to_primitive(self))


@dataclass(frozen=True, slots=True)
class CancellationRequest:
    task_id: str
    run_id: str
    reason: str
    target: CancellationTarget = CancellationTarget.LOGICAL_TASK
    attempt_id: str = ""
    lease_id: str = ""
    worker_id: str = ""
    actor_id: str = "control-plane"
    request_id: str = field(default_factory=lambda: new_pool_id("cancel"))
    requested_at: str = field(default_factory=utc_iso)
    idempotency_key: str = ""
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", require_token(self.task_id, "task_id"))
        object.__setattr__(self, "run_id", require_token(self.run_id, "run_id"))
        object.__setattr__(self, "actor_id", require_token(self.actor_id, "actor_id"))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", stable_digest((self.task_id, self.attempt_id, self.reason)))

    def to_dict(self) -> dict[str, Any]:
        return dict(to_primitive(self))


@dataclass(frozen=True, slots=True)
class CancellationReceipt:
    request_id: str
    task_id: str
    accepted: bool
    changed: bool
    cancelled_attempt_ids: tuple[str, ...] = ()
    cancelled_lease_ids: tuple[str, ...] = ()
    backend_cancelled: tuple[str, ...] = ()
    gateway_cancelled: tuple[str, ...] = ()
    failed_targets: Mapping[str, str] = field(default_factory=dict)
    receipt_id: str = field(default_factory=lambda: new_pool_id("cancel_receipt"))
    completed_at: str = field(default_factory=utc_iso)
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "cancelled_attempt_ids", tokens(self.cancelled_attempt_ids))
        object.__setattr__(self, "cancelled_lease_ids", tokens(self.cancelled_lease_ids))
        object.__setattr__(self, "backend_cancelled", tokens(self.backend_cancelled))
        object.__setattr__(self, "gateway_cancelled", tokens(self.gateway_cancelled))
        object.__setattr__(self, "failed_targets", {str(k): str(v) for k, v in self.failed_targets.items()})
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return dict(to_primitive(self))


@dataclass(frozen=True, slots=True)
class WorkerSelection:
    worker_id: str
    accepted: bool
    score: float
    reasons: tuple[str, ...]
    available: ResourceVector
    manifest_digest: str
    active_lease_count: int

    def to_dict(self) -> dict[str, Any]:
        return dict(to_primitive(self))


@dataclass(frozen=True, slots=True)
class LeaseAcquisition:
    attempt: TaskAttempt
    lease: WorkerLease
    worker: WorkerInstance
    manifest: WorkerCapabilityManifest
    candidates: tuple[WorkerSelection, ...] = ()
    reused: bool = False

    def to_dict(self, *, include_fence_token: bool = False) -> dict[str, Any]:
        return {
            "attempt": self.attempt.to_dict(),
            "lease": self.lease.to_dict(include_fence_token=include_fence_token),
            "worker": self.worker.to_dict(),
            "manifest": self.manifest.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "reused": self.reused,
        }


@dataclass(frozen=True, slots=True)
class PoolStateSnapshot:
    workers: tuple[WorkerInstance, ...]
    manifests: tuple[WorkerCapabilityManifest, ...]
    active_leases: tuple[WorkerLease, ...]
    active_attempts: tuple[TaskAttempt, ...]
    health: tuple[HealthAssessment, ...]
    captured_at: str = field(default_factory=utc_iso)
    revision: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "workers": [item.to_dict() for item in self.workers],
            "manifests": [item.to_dict() for item in self.manifests],
            "active_leases": [item.to_dict() for item in self.active_leases],
            "active_attempts": [item.to_dict() for item in self.active_attempts],
            "health": [item.to_dict() for item in self.health],
            "captured_at": self.captured_at,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class PoolJournalRecord:
    aggregate_type: str
    aggregate_id: str
    operation: str
    payload: Mapping[str, JsonValue]
    run_id: str = ""
    task_id: str = ""
    causation_id: str = ""
    correlation_id: str = ""
    journal_id: str = field(default_factory=lambda: new_pool_id("pool_event"))
    sequence: int = 0
    created_at: str = field(default_factory=utc_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "aggregate_type", require_token(self.aggregate_type, "aggregate_type"))
        object.__setattr__(self, "aggregate_id", require_token(self.aggregate_id, "aggregate_id"))
        object.__setattr__(self, "operation", require_token(self.operation, "operation"))
        object.__setattr__(self, "payload", copy.deepcopy(dict(self.payload)))

    def to_dict(self) -> dict[str, Any]:
        return dict(to_primitive(self))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(str(item) for item in value)


def _enum(enum_type: type[EnumT], value: Any, default: EnumT) -> EnumT:
    try:
        return enum_type(str(value))
    except (TypeError, ValueError):
        return default
