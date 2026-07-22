from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


FAULT_SIGNAL_SCHEMA = "zyra.watchdog-fault-signal/v1"
FAULT_OBSERVATION_SCHEMA = "zyra.watchdog-observation/v1"
FAULT_INJECTION_SCHEMA = "zyra.fault-injection/v1"
RECOVERY_HANDOFF_SCHEMA = "zyra.watchdog-recovery-handoff/v1"
OBSERVER_DESCRIPTOR_SCHEMA = "zyra.watchdog-observer/v1"

_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,511}$")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def runtime_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:24]}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def stable_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def require_identity(name: str, value: str, *, optional: bool = False) -> str:
    selected = str(value or "").strip()
    if not selected and optional:
        return ""
    if not _IDENTITY.fullmatch(selected):
        raise ValueError(f"{name} is not a valid bounded identity")
    return selected


def require_non_negative(name: str, value: int | float) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be numeric")
    if not math.isfinite(float(value)) or float(value) < 0:
        raise ValueError(f"{name} must be finite and non-negative")


def _mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return {str(key): item for key, item in dict(value or {}).items()}


def _strings(value: Sequence[Any] | None) -> tuple[str, ...]:
    return tuple(str(item) for item in value or ())


class ObservationCategory(StrEnum):
    TOOL = "tool"
    WORKER = "worker"
    BROWSER = "browser"
    PERMISSION = "permission"
    PROVIDER = "provider"
    SCHEMA = "schema"
    WORKSPACE = "workspace"
    MCP = "mcp"
    PROCESS = "process"
    REQUIREMENT_CHANGE = "requirement_change"


class FaultKind(StrEnum):
    TOOL_TIMEOUT = "tool_timeout"
    WORKER_UNAVAILABLE = "worker_unavailable"
    BROWSER_CRASH = "browser_crash"
    BROWSER_DISCONNECT = "browser_disconnect"
    PERMISSION_DENIED = "permission_denied"
    MODEL_FAILURE = "model_failure"
    MODEL_RATE_LIMIT = "model_rate_limit"
    MODEL_QUOTA_EXHAUSTED = "model_quota_exhausted"
    SCHEMA_FAILURE = "schema_failure"
    WORKSPACE_CORRUPT = "workspace_corrupt"
    MCP_DISCONNECTED = "mcp_disconnected"
    PROCESS_EXITED = "process_exited"
    SUBAGENT_FAILED = "subagent_failed"
    UNKNOWN = "unknown"


class FaultSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class FaultDisposition(StrEnum):
    IGNORE = "ignore"
    RECORD = "record"
    DEGRADE = "degrade"
    BLOCK = "block"
    RECOVER = "recover"


class SignalOrigin(StrEnum):
    OBSERVER = "observer"
    INJECTION = "injection"


class ObserverMaturity(StrEnum):
    ACTIVE_REAL = "active_real"
    EXPERIMENTAL = "experimental"
    SOURCE_INACTIVE = "source_inactive"
    INJECTION_ONLY = "injection_only"


class ObserverLifecycle(StrEnum):
    REGISTERED = "registered"
    ATTACHED = "attached"
    RUNNING = "running"
    STOPPED = "stopped"
    DISABLED = "disabled"
    FAILED = "failed"

    @property
    def accepts_observations(self) -> bool:
        return self is ObserverLifecycle.RUNNING


class InjectionKind(StrEnum):
    WORKER_LOST = "worker_lost"
    TOOL_TIMEOUT = "tool_timeout"
    BROWSER_CRASH = "browser_crash"
    MODEL_FAILURE = "model_failure"
    WORKSPACE_CORRUPT = "workspace_corrupt"


class InjectionPhase(StrEnum):
    REQUESTED = "requested"
    ARMED = "armed"
    TRIGGERED = "triggered"
    OBSERVED = "observed"
    PROJECTED = "projected"
    CONTINUED = "continued"
    HANDED_OFF = "handed_off"
    REJECTED = "rejected"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {
            InjectionPhase.CONTINUED,
            InjectionPhase.HANDED_OFF,
            InjectionPhase.REJECTED,
            InjectionPhase.FAILED,
        }


class ContinuationMode(StrEnum):
    AUTO = "auto"
    CONTINUE = "continue"
    RECOVERY_HANDOFF = "recovery_handoff"


@dataclass(frozen=True, slots=True)
class CorrelationRefs:
    run_id: str
    task_id: str
    observation_id: str
    session_id: str = ""
    node_id: str = ""
    attempt_id: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    worker_id: str = ""
    backend_id: str = ""
    provider_id: str = ""
    workspace_id: str = ""
    browser_session_id: str = ""
    mcp_server_id: str = ""
    subagent_task_id: str = ""
    source_state_revision: int = 0

    def __post_init__(self) -> None:
        for name in ("run_id", "task_id", "observation_id"):
            require_identity(name, getattr(self, name))
        for name in (
            "session_id",
            "node_id",
            "attempt_id",
            "tool_call_id",
            "tool_name",
            "worker_id",
            "backend_id",
            "provider_id",
            "workspace_id",
            "browser_session_id",
            "mcp_server_id",
            "subagent_task_id",
        ):
            require_identity(name, getattr(self, name), optional=True)
        require_non_negative("source_state_revision", self.source_state_revision)

    def with_observation(self, observation_id: str, revision: int | None = None) -> "CorrelationRefs":
        return CorrelationRefs(
            **{
                **self.to_dict(),
                "observation_id": observation_id,
                "source_state_revision": self.source_state_revision if revision is None else revision,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "node_id": self.node_id,
            "attempt_id": self.attempt_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "worker_id": self.worker_id,
            "backend_id": self.backend_id,
            "provider_id": self.provider_id,
            "workspace_id": self.workspace_id,
            "browser_session_id": self.browser_session_id,
            "mcp_server_id": self.mcp_server_id,
            "subagent_task_id": self.subagent_task_id,
            "observation_id": self.observation_id,
            "source_state_revision": self.source_state_revision,
        }


@dataclass(frozen=True, slots=True)
class ObservationProvenance:
    observer_id: str
    source_repo: str
    source_revision: str
    observation_point: str
    maturity: ObserverMaturity
    adapter_version: str = "M1-S07B-01"
    injection_id: str = ""

    def __post_init__(self) -> None:
        require_identity("observer_id", self.observer_id)
        require_identity("source_repo", self.source_repo)
        require_identity("source_revision", self.source_revision)
        require_identity("observation_point", self.observation_point)
        require_identity("adapter_version", self.adapter_version)
        require_identity("injection_id", self.injection_id, optional=True)
        if self.maturity is ObserverMaturity.INJECTION_ONLY and not self.injection_id:
            raise ValueError("injection-only provenance requires injection_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "observer_id": self.observer_id,
            "source_repo": self.source_repo,
            "source_revision": self.source_revision,
            "observation_point": self.observation_point,
            "maturity": self.maturity.value,
            "adapter_version": self.adapter_version,
            "injection_id": self.injection_id,
        }


@dataclass(frozen=True, slots=True)
class StructuredObservation:
    category: ObservationCategory
    code: str
    refs: CorrelationRefs
    provenance: ObservationProvenance
    summary: str
    observed_at: str = field(default_factory=utc_now)
    status: str = ""
    error_type: str = ""
    status_code: int | None = None
    retryable_hint: bool | None = None
    terminal_hint: bool | None = None
    elapsed_ms: int | None = None
    deadline_ms: int | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    evidence_event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_identity("observation code", self.code)
        if not str(self.summary).strip():
            raise ValueError("observation summary must not be empty")
        if self.elapsed_ms is not None:
            require_non_negative("elapsed_ms", self.elapsed_ms)
        if self.deadline_ms is not None:
            require_non_negative("deadline_ms", self.deadline_ms)
        if self.status_code is not None and not 0 <= self.status_code <= 999:
            raise ValueError("status_code must be in range 0..999")
        object.__setattr__(self, "details", _mapping(self.details))
        object.__setattr__(self, "evidence_event_ids", _strings(self.evidence_event_ids))
        if self.provenance.injection_id and self.provenance.maturity is not ObserverMaturity.INJECTION_ONLY:
            raise ValueError("injected observations must use injection_only maturity")

    @property
    def observation_id(self) -> str:
        return self.refs.observation_id

    @property
    def fingerprint(self) -> str:
        return stable_digest(
            {
                "category": self.category.value,
                "code": self.code,
                "refs": self.refs.to_dict(),
                "provenance": self.provenance.to_dict(),
                "status": self.status,
                "error_type": self.error_type,
                "status_code": self.status_code,
                "elapsed_ms": self.elapsed_ms,
                "deadline_ms": self.deadline_ms,
                "details": dict(self.details),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": FAULT_OBSERVATION_SCHEMA,
            "observation_id": self.observation_id,
            "category": self.category.value,
            "code": self.code,
            "refs": self.refs.to_dict(),
            "provenance": self.provenance.to_dict(),
            "summary": self.summary,
            "observed_at": self.observed_at,
            "status": self.status,
            "error_type": self.error_type,
            "status_code": self.status_code,
            "retryable_hint": self.retryable_hint,
            "terminal_hint": self.terminal_hint,
            "elapsed_ms": self.elapsed_ms,
            "deadline_ms": self.deadline_ms,
            "details": dict(self.details),
            "evidence_event_ids": list(self.evidence_event_ids),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class FaultSignal:
    kind: FaultKind
    severity: FaultSeverity
    disposition: FaultDisposition
    refs: CorrelationRefs
    provenance: ObservationProvenance
    summary: str
    origin: SignalOrigin = SignalOrigin.OBSERVER
    retryable: bool = False
    terminal: bool = False
    classification_rule: str = ""
    observed_code: str = ""
    signal_id: str = field(default_factory=lambda: runtime_id("faultsig"))
    created_at: str = field(default_factory=utc_now)
    evidence_event_ids: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identity("signal_id", self.signal_id)
        require_identity("classification_rule", self.classification_rule, optional=True)
        require_identity("observed_code", self.observed_code, optional=True)
        if not str(self.summary).strip():
            raise ValueError("fault signal summary must not be empty")
        object.__setattr__(self, "evidence_event_ids", _strings(self.evidence_event_ids))
        object.__setattr__(self, "details", _mapping(self.details))
        if self.origin is SignalOrigin.INJECTION and not self.provenance.injection_id:
            raise ValueError("injected fault signal requires injection provenance")
        if self.origin is SignalOrigin.OBSERVER and self.provenance.injection_id:
            raise ValueError("observer signal cannot carry injection provenance")

    @property
    def fingerprint(self) -> str:
        return stable_digest(
            {
                "kind": self.kind.value,
                "refs": self.refs.to_dict(),
                "provenance": self.provenance.to_dict(),
                "origin": self.origin.value,
                "observed_code": self.observed_code,
                "terminal": self.terminal,
                "details": dict(self.details),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": FAULT_SIGNAL_SCHEMA,
            "signal_id": self.signal_id,
            "kind": self.kind.value,
            "severity": self.severity.value,
            "disposition": self.disposition.value,
            "origin": self.origin.value,
            "refs": self.refs.to_dict(),
            "provenance": self.provenance.to_dict(),
            "summary": self.summary,
            "retryable": self.retryable,
            "terminal": self.terminal,
            "classification_rule": self.classification_rule,
            "observed_code": self.observed_code,
            "created_at": self.created_at,
            "evidence_event_ids": list(self.evidence_event_ids),
            "details": dict(self.details),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ObserverDescriptor:
    observer_id: str
    display_name: str
    maturity: ObserverMaturity
    attach_owner: str
    lifecycle_owner: str
    observation_point: str
    source_repo: str
    source_revision: str
    emitted_kinds: tuple[FaultKind, ...]
    categories: tuple[ObservationCategory, ...]
    enabled_by_default: bool
    descriptor_revision: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "observer_id",
            "attach_owner",
            "lifecycle_owner",
            "observation_point",
            "source_repo",
            "source_revision",
        ):
            require_identity(name, getattr(self, name))
        if not self.display_name.strip():
            raise ValueError("observer display name must not be empty")
        require_non_negative("descriptor_revision", self.descriptor_revision)
        object.__setattr__(self, "emitted_kinds", tuple(dict.fromkeys(self.emitted_kinds)))
        object.__setattr__(self, "categories", tuple(dict.fromkeys(self.categories)))
        object.__setattr__(self, "metadata", _mapping(self.metadata))
        if self.maturity is ObserverMaturity.SOURCE_INACTIVE and self.enabled_by_default:
            raise ValueError("source_inactive observer cannot be enabled by default")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": OBSERVER_DESCRIPTOR_SCHEMA,
            "observer_id": self.observer_id,
            "display_name": self.display_name,
            "maturity": self.maturity.value,
            "attach_owner": self.attach_owner,
            "lifecycle_owner": self.lifecycle_owner,
            "observation_point": self.observation_point,
            "source_repo": self.source_repo,
            "source_revision": self.source_revision,
            "emitted_kinds": [item.value for item in self.emitted_kinds],
            "categories": [item.value for item in self.categories],
            "enabled_by_default": self.enabled_by_default,
            "descriptor_revision": self.descriptor_revision,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ObserverState:
    descriptor: ObserverDescriptor
    lifecycle: ObserverLifecycle
    revision: int
    attach_count: int = 0
    start_count: int = 0
    stop_count: int = 0
    observation_count: int = 0
    emitted_count: int = 0
    last_observation_id: str = ""
    last_error: str = ""
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in (
            "revision",
            "attach_count",
            "start_count",
            "stop_count",
            "observation_count",
            "emitted_count",
        ):
            require_non_negative(name, getattr(self, name))
        require_identity("last_observation_id", self.last_observation_id, optional=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "descriptor": self.descriptor.to_dict(),
            "lifecycle": self.lifecycle.value,
            "revision": self.revision,
            "attach_count": self.attach_count,
            "start_count": self.start_count,
            "stop_count": self.stop_count,
            "observation_count": self.observation_count,
            "emitted_count": self.emitted_count,
            "last_observation_id": self.last_observation_id,
            "last_error": self.last_error,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class FaultInjectionRequest:
    run_id: str
    task_id: str
    kind: InjectionKind
    target: CorrelationRefs
    requested_by: str
    idempotency_key: str
    continuation: ContinuationMode = ContinuationMode.AUTO
    parameters: Mapping[str, Any] = field(default_factory=dict)
    injection_id: str = field(default_factory=lambda: runtime_id("inject"))
    requested_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in ("run_id", "task_id", "requested_by", "idempotency_key", "injection_id"):
            require_identity(name, getattr(self, name))
        if self.target.run_id != self.run_id or self.target.task_id != self.task_id:
            raise ValueError("injection target does not belong to request run/task")
        object.__setattr__(self, "parameters", _mapping(self.parameters))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": FAULT_INJECTION_SCHEMA,
            "injection_id": self.injection_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "kind": self.kind.value,
            "target": self.target.to_dict(),
            "requested_by": self.requested_by,
            "idempotency_key": self.idempotency_key,
            "continuation": self.continuation.value,
            "parameters": dict(self.parameters),
            "requested_at": self.requested_at,
        }


@dataclass(frozen=True, slots=True)
class InjectionTransition:
    injection_id: str
    phase: InjectionPhase
    revision: int
    reason: str
    transition_id: str = field(default_factory=lambda: runtime_id("injectstep"))
    signal_id: str = ""
    event_id: str = ""
    handoff_id: str = ""
    created_at: str = field(default_factory=utc_now)
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("injection_id", "transition_id"):
            require_identity(name, getattr(self, name))
        for name in ("signal_id", "event_id", "handoff_id"):
            require_identity(name, getattr(self, name), optional=True)
        require_non_negative("revision", self.revision)
        if not self.reason.strip():
            raise ValueError("injection transition reason must not be empty")
        object.__setattr__(self, "details", _mapping(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "injection_id": self.injection_id,
            "phase": self.phase.value,
            "revision": self.revision,
            "reason": self.reason,
            "signal_id": self.signal_id,
            "event_id": self.event_id,
            "handoff_id": self.handoff_id,
            "created_at": self.created_at,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class RecoveryHandoff:
    run_id: str
    task_id: str
    signal_id: str
    fault_kind: FaultKind
    refs: CorrelationRefs
    requested_actions: tuple[str, ...]
    recoverable: bool
    reason: str
    injection_id: str = ""
    handoff_id: str = field(default_factory=lambda: runtime_id("recoveryhandoff"))
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("run_id", "task_id", "signal_id", "handoff_id"):
            require_identity(name, getattr(self, name))
        require_identity("injection_id", self.injection_id, optional=True)
        if self.refs.run_id != self.run_id or self.refs.task_id != self.task_id:
            raise ValueError("recovery handoff refs do not belong to run/task")
        object.__setattr__(self, "requested_actions", _strings(self.requested_actions))
        object.__setattr__(self, "metadata", _mapping(self.metadata))
        if not self.reason.strip():
            raise ValueError("recovery handoff reason must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RECOVERY_HANDOFF_SCHEMA,
            "handoff_id": self.handoff_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "signal_id": self.signal_id,
            "fault_kind": self.fault_kind.value,
            "refs": self.refs.to_dict(),
            "requested_actions": list(self.requested_actions),
            "recoverable": self.recoverable,
            "reason": self.reason,
            "injection_id": self.injection_id,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ProjectionReceipt:
    signal_id: str
    event_id: str
    canonical_event_written: bool
    memory_record_ids: tuple[str, ...]
    scheduler_changed: bool
    scheduler_receipt: Mapping[str, Any]
    task_projection_changed: bool
    receipt_id: str = field(default_factory=lambda: runtime_id("faultprojection"))
    created_at: str = field(default_factory=utc_now)
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("signal_id", "event_id", "receipt_id"):
            require_identity(name, getattr(self, name))
        object.__setattr__(self, "memory_record_ids", _strings(self.memory_record_ids))
        object.__setattr__(self, "scheduler_receipt", _mapping(self.scheduler_receipt))
        object.__setattr__(self, "errors", _strings(self.errors))

    @property
    def ok(self) -> bool:
        return self.canonical_event_written and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "signal_id": self.signal_id,
            "event_id": self.event_id,
            "canonical_event_written": self.canonical_event_written,
            "memory_record_ids": list(self.memory_record_ids),
            "scheduler_changed": self.scheduler_changed,
            "scheduler_receipt": dict(self.scheduler_receipt),
            "task_projection_changed": self.task_projection_changed,
            "created_at": self.created_at,
            "errors": list(self.errors),
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class FaultInjectionReceipt:
    request: FaultInjectionRequest
    phase: InjectionPhase
    signal: FaultSignal | None
    projection: ProjectionReceipt | None
    handoff: RecoveryHandoff | None
    transitions: tuple[InjectionTransition, ...]
    duplicate: bool = False
    same_run: bool = True
    receipt_id: str = field(default_factory=lambda: runtime_id("injectreceipt"))
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        require_identity("receipt_id", self.receipt_id)
        object.__setattr__(self, "transitions", tuple(self.transitions))
        if not self.phase.terminal:
            raise ValueError("fault injection receipt must describe a terminal injection phase")
        if self.duplicate and not self.transitions:
            raise ValueError("duplicate receipt must include prior transitions")

    @property
    def ok(self) -> bool:
        return self.phase in {InjectionPhase.CONTINUED, InjectionPhase.HANDED_OFF}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.fault-injection-receipt/v1",
            "receipt_id": self.receipt_id,
            "request": self.request.to_dict(),
            "phase": self.phase.value,
            "signal": None if self.signal is None else self.signal.to_dict(),
            "projection": None if self.projection is None else self.projection.to_dict(),
            "handoff": None if self.handoff is None else self.handoff.to_dict(),
            "transitions": [item.to_dict() for item in self.transitions],
            "duplicate": self.duplicate,
            "same_run": self.same_run,
            "receipt_id": self.receipt_id,
            "created_at": self.created_at,
            "ok": self.ok,
        }
