from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4


RECOVERY_SIGNAL_SCHEMA = "zyra.recovery-signal/v1"
RECOVERY_PLAN_SCHEMA = "zyra.recovery-plan/v1"
RECOVERY_CHECKPOINT_SCHEMA = "zyra.recovery-checkpoint/v1"
CHECKPOINT_RECEIPT_SCHEMA = "zyra.checkpoint-receipt/v1"
LAYERED_ROUTE_SCHEMA = "zyra.layered-route-decision/v1"
RECOVERY_FEEDBACK_SCHEMA = "zyra.routing-memory-feedback/v1"
OMP_RECEIPT_SCHEMA = "zyra.omp-recovery-receipt/v1"

_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/\-]{0,511}$")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def recovery_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def require_identity(name: str, value: str, *, optional: bool = False) -> str:
    candidate = str(value or "").strip()
    if optional and not candidate:
        return ""
    if not candidate:
        raise ValueError(f"{name} is required")
    if not _IDENTITY.fullmatch(candidate):
        raise ValueError(f"{name} contains unsupported characters")
    return candidate


def require_revision(name: str, value: int, *, allow_zero: bool = True) -> int:
    revision = int(value)
    if revision < (0 if allow_zero else 1):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'}")
    return revision


def immutable_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(copy.deepcopy(dict(value or {})))


def immutable_strings(values: Iterable[Any] | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values or () if str(value)))


def _enum(enum_type: type[StrEnum], value: Any, default: StrEnum | None = None) -> Any:
    if isinstance(value, enum_type):
        return value
    if value in {None, ""} and default is not None:
        return default
    return enum_type(str(value))


class RecoverySignalKind(StrEnum):
    PERMISSION_DENIED = "permission_denied"
    PERMISSION_PENDING = "permission_pending"
    PERMISSION_ASK = "permission_ask"
    TOOL_ERROR = "tool_error"
    TOOL_TIMEOUT = "tool_timeout"
    MCP_AUTH_REQUIRED = "mcp_auth_required"
    MCP_DISCONNECTED = "mcp_disconnected"
    API_RETRY_EXHAUSTED = "api_retry_exhausted"
    RATE_LIMITED = "rate_limited"
    STREAM_STALL = "stream_stall"
    STREAM_INTERRUPTED_AFTER_OUTPUT = "stream_interrupted_after_output"
    PROMPT_TOO_LONG = "prompt_too_long"
    COMPACT_NEEDED = "compact_needed"
    SUBAGENT_FAILED = "subagent_failed"
    SUBAGENT_CANCELLED = "subagent_cancelled"
    WORKER_HEARTBEAT_STALE = "worker_heartbeat_stale"
    WORKER_LOST = "worker_lost"
    WORKER_LEASE_EXPIRED = "worker_lease_expired"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    CREDENTIAL_EXHAUSTED = "credential_exhausted"
    WORKSPACE_CONFLICT = "workspace_conflict"
    CHECKPOINT_DIVERGED = "checkpoint_diverged"
    REQUIREMENT_CHANGED = "requirement_changed"
    UNKNOWN_FAILURE = "unknown_failure"


class RecoverySource(StrEnum):
    WATCHDOG_HANDOFF = "watchdog_handoff"
    WORKER_HANDOFF = "worker_handoff"
    PERMISSION_RUNTIME = "permission_runtime"
    SESSION_RUNTIME = "session_runtime"
    COMPACT_RUNTIME = "compact_runtime"
    TOOL_RUNTIME = "tool_runtime"
    MCP_RUNTIME = "mcp_runtime"
    PROVIDER_RUNTIME = "provider_runtime"
    API_RUNTIME = "api_runtime"
    SUBAGENT_RUNTIME = "subagent_runtime"
    BACKEND_RUNTIME = "backend_runtime"
    WORKSPACE_RUNTIME = "workspace_runtime"
    CONTROL_RUNTIME = "control_runtime"
    CHECKPOINT_RUNTIME = "checkpoint_runtime"
    OMP_SUPPLEMENT = "omp_supplement"


class RecoveryAction(StrEnum):
    RETRY = "retry"
    REROUTE = "reroute"
    DEGRADE_MODEL = "degrade_model"
    SWITCH_BACKEND = "switch_backend"
    SWITCH_PROVIDER = "switch_provider"
    REPLAN = "replan"
    ASK_PERMISSION = "ask_permission"
    AUTHENTICATE_MCP = "authenticate_mcp"
    COMPACT = "compact"
    RESUME_CHECKPOINT = "resume_checkpoint"
    ABORT = "abort"
    NOOP = "noop"


class RecoveryPlanStatus(StrEnum):
    PLANNED = "planned"
    CLAIMED = "claimed"
    APPLYING = "applying"
    WAITING_PERMISSION = "waiting_permission"
    WAITING_AUTH = "waiting_auth"
    WAITING_BACKOFF = "waiting_backoff"
    APPLIED = "applied"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"

    @property
    def terminal(self) -> bool:
        return self in {
            RecoveryPlanStatus.SUCCEEDED,
            RecoveryPlanStatus.FAILED,
            RecoveryPlanStatus.CANCELLED,
            RecoveryPlanStatus.SUPERSEDED,
        }


class RecoveryAttemptStatus(StrEnum):
    STARTED = "started"
    APPLIED = "applied"
    DEFERRED = "deferred"
    REJECTED = "rejected"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


class RecoveryOutcomeKind(StrEnum):
    RECOVERED = "recovered"
    PARTIAL = "partial"
    FAILED = "failed"
    WAITING = "waiting"
    BYPASSED = "bypassed"


class DecisionMode(StrEnum):
    INTERACTIVE = "interactive"
    SEALED_AUTONOMOUS = "sealed_autonomous"


class RouteLayer(StrEnum):
    GRAPH = "graph"
    WORKER = "worker"
    BACKEND = "backend"
    WORKSPACE = "workspace"
    PROVIDER = "provider"
    MODEL = "model"
    CREDENTIAL = "credential"
    TRANSPORT = "transport"


class CheckpointPhase(StrEnum):
    PREPARED = "prepared"
    COMMITTED = "committed"
    INTERRUPTED = "interrupted"
    RESUMING = "resuming"
    RESUMED = "resumed"
    COMPLETED = "completed"
    REJECTED = "rejected"


class PendingWriteState(StrEnum):
    PENDING = "pending"
    COMMITTED = "committed"
    DISCARDED = "discarded"


class SideEffectState(StrEnum):
    RESERVED = "reserved"
    STARTED = "started"
    COMMITTED = "committed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DeltaOperation(StrEnum):
    SET = "set"
    DELETE = "delete"
    APPEND_UNIQUE = "append_unique"
    INCREMENT = "increment"
    MERGE_MAPPING = "merge_mapping"


class ConflictKind(StrEnum):
    WRITE_WRITE = "write_write"
    READ_WRITE = "read_write"
    BASE_REVISION = "base_revision"
    OWNER_MISMATCH = "owner_mismatch"
    SIGNATURE_MISMATCH = "signature_mismatch"


@dataclass(frozen=True, slots=True)
class RecoveryRefs:
    run_id: str
    task_id: str
    session_id: str = ""
    node_id: str = ""
    turn_id: str = ""
    tool_call_id: str = ""
    request_id: str = ""
    response_id: str = ""
    worker_id: str = ""
    attempt_id: str = ""
    worker_lease_id: str = ""
    backend_id: str = ""
    backend_lease_id: str = ""
    provider_id: str = ""
    provider_route_id: str = ""
    credential_id: str = ""
    transport_id: str = ""
    workspace_id: str = ""
    checkpoint_id: str = ""
    permission_request_id: str = ""
    mcp_server_id: str = ""
    subagent_id: str = ""
    graph_id: str = ""
    graph_revision: int = 0

    def __post_init__(self) -> None:
        for name in (
            "run_id", "task_id", "session_id", "node_id", "turn_id", "tool_call_id",
            "request_id", "response_id", "worker_id", "attempt_id", "worker_lease_id",
            "backend_id", "backend_lease_id", "provider_id", "provider_route_id",
            "credential_id", "transport_id", "workspace_id", "checkpoint_id",
            "permission_request_id", "mcp_server_id", "subagent_id", "graph_id",
        ):
            require_identity(name, getattr(self, name), optional=name not in {"run_id", "task_id"})
        require_revision("graph_revision", self.graph_revision)

    def to_dict(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if getattr(self, name) not in {"", 0}
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RecoveryRefs":
        fields = cls.__dataclass_fields__
        return cls(**{name: value[name] for name in fields if name in value})


@dataclass(frozen=True, slots=True)
class RecoverySignal:
    kind: RecoverySignalKind
    source: RecoverySource
    refs: RecoveryRefs
    reason_code: str
    summary: str
    recoverable: bool = True
    retryable: bool = False
    terminal: bool = False
    partial_output: bool = False
    observable_side_effect: bool = False
    auth_required: bool = False
    permission_effect: str = ""
    severity: str = "warning"
    signal_id: str = field(default_factory=lambda: recovery_id("recoverysig"))
    causation_id: str = ""
    correlation_id: str = ""
    observed_at: str = field(default_factory=utc_now)
    attempt_count: int = 0
    retry_after_ms: int = 0
    evidence_event_ids: tuple[str, ...] = ()
    suggested_actions: tuple[RecoveryAction, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("signal_id", "reason_code"):
            require_identity(name, getattr(self, name))
        for name in ("causation_id", "correlation_id"):
            require_identity(name, getattr(self, name), optional=True)
        if not self.summary.strip():
            raise ValueError("recovery signal summary is required")
        require_revision("attempt_count", self.attempt_count)
        require_revision("retry_after_ms", self.retry_after_ms)
        object.__setattr__(self, "evidence_event_ids", immutable_strings(self.evidence_event_ids))
        object.__setattr__(self, "suggested_actions", tuple(dict.fromkeys(self.suggested_actions)))
        object.__setattr__(self, "details", immutable_mapping(self.details))

    @property
    def fingerprint(self) -> str:
        return stable_digest({
            "kind": self.kind.value,
            "source": self.source.value,
            "refs": self.refs.to_dict(),
            "reason_code": self.reason_code,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "partial_output": self.partial_output,
            "observable_side_effect": self.observable_side_effect,
            "details": dict(self.details),
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RECOVERY_SIGNAL_SCHEMA,
            "signal_id": self.signal_id,
            "kind": self.kind.value,
            "source": self.source.value,
            "refs": self.refs.to_dict(),
            "reason_code": self.reason_code,
            "summary": self.summary,
            "recoverable": self.recoverable,
            "retryable": self.retryable,
            "terminal": self.terminal,
            "partial_output": self.partial_output,
            "observable_side_effect": self.observable_side_effect,
            "auth_required": self.auth_required,
            "permission_effect": self.permission_effect,
            "severity": self.severity,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "observed_at": self.observed_at,
            "attempt_count": self.attempt_count,
            "retry_after_ms": self.retry_after_ms,
            "evidence_event_ids": list(self.evidence_event_ids),
            "suggested_actions": [item.value for item in self.suggested_actions],
            "details": copy.deepcopy(dict(self.details)),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RecoverySignal":
        return cls(
            signal_id=str(value.get("signal_id") or recovery_id("recoverysig")),
            kind=_enum(RecoverySignalKind, value.get("kind"), RecoverySignalKind.UNKNOWN_FAILURE),
            source=_enum(RecoverySource, value.get("source"), RecoverySource.API_RUNTIME),
            refs=RecoveryRefs.from_dict(dict(value.get("refs") or {})),
            reason_code=str(value.get("reason_code") or "runtime.unknown"),
            summary=str(value.get("summary") or "runtime recovery signal"),
            recoverable=bool(value.get("recoverable", True)),
            retryable=bool(value.get("retryable", False)),
            terminal=bool(value.get("terminal", False)),
            partial_output=bool(value.get("partial_output", False)),
            observable_side_effect=bool(value.get("observable_side_effect", False)),
            auth_required=bool(value.get("auth_required", False)),
            permission_effect=str(value.get("permission_effect") or ""),
            severity=str(value.get("severity") or "warning"),
            causation_id=str(value.get("causation_id") or ""),
            correlation_id=str(value.get("correlation_id") or ""),
            observed_at=str(value.get("observed_at") or utc_now()),
            attempt_count=int(value.get("attempt_count") or 0),
            retry_after_ms=int(value.get("retry_after_ms") or 0),
            evidence_event_ids=tuple(value.get("evidence_event_ids") or ()),
            suggested_actions=tuple(RecoveryAction(str(item)) for item in value.get("suggested_actions") or ()),
            details=dict(value.get("details") or {}),
        )


@dataclass(frozen=True, slots=True)
class RecoveryBudget:
    maximum_retries: int = 3
    maximum_reroutes: int = 2
    maximum_backend_switches: int = 2
    maximum_provider_switches: int = 2
    maximum_compactions: int = 2
    maximum_resumes: int = 3
    maximum_total_actions: int = 8
    maximum_delay_ms: int = 300_000
    base_delay_ms: int = 500

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            require_revision(name, getattr(self, name))
        if self.maximum_total_actions < 1:
            raise ValueError("maximum_total_actions must be positive")

    def to_dict(self) -> dict[str, int]:
        return {name: int(getattr(self, name)) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "RecoveryBudget":
        source = dict(value or {})
        return cls(**{name: int(source[name]) for name in cls.__dataclass_fields__ if name in source})


@dataclass(frozen=True, slots=True)
class RecoveryContext:
    refs: RecoveryRefs
    mode: DecisionMode = DecisionMode.INTERACTIVE
    session_state: Mapping[str, Any] = field(default_factory=dict)
    permission_state: Mapping[str, Any] = field(default_factory=dict)
    worker_state: Mapping[str, Any] = field(default_factory=dict)
    backend_state: Mapping[str, Any] = field(default_factory=dict)
    provider_state: Mapping[str, Any] = field(default_factory=dict)
    checkpoint_state: Mapping[str, Any] = field(default_factory=dict)
    memory_evidence: tuple[Mapping[str, Any], ...] = ()
    privacy_constraints: tuple[str, ...] = ()
    allowed_locations: tuple[str, ...] = ()
    forbidden_actions: tuple[RecoveryAction, ...] = ()
    budget: RecoveryBudget = field(default_factory=RecoveryBudget)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("session_state", "permission_state", "worker_state", "backend_state", "provider_state", "checkpoint_state", "metadata"):
            object.__setattr__(self, name, immutable_mapping(getattr(self, name)))
        object.__setattr__(self, "memory_evidence", tuple(immutable_mapping(item) for item in self.memory_evidence))
        object.__setattr__(self, "privacy_constraints", immutable_strings(self.privacy_constraints))
        object.__setattr__(self, "allowed_locations", immutable_strings(self.allowed_locations))
        object.__setattr__(self, "forbidden_actions", tuple(dict.fromkeys(self.forbidden_actions)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "refs": self.refs.to_dict(),
            "mode": self.mode.value,
            "session_state": copy.deepcopy(dict(self.session_state)),
            "permission_state": copy.deepcopy(dict(self.permission_state)),
            "worker_state": copy.deepcopy(dict(self.worker_state)),
            "backend_state": copy.deepcopy(dict(self.backend_state)),
            "provider_state": copy.deepcopy(dict(self.provider_state)),
            "checkpoint_state": copy.deepcopy(dict(self.checkpoint_state)),
            "memory_evidence": [copy.deepcopy(dict(item)) for item in self.memory_evidence],
            "privacy_constraints": list(self.privacy_constraints),
            "allowed_locations": list(self.allowed_locations),
            "forbidden_actions": [item.value for item in self.forbidden_actions],
            "budget": self.budget.to_dict(),
            "metadata": copy.deepcopy(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RecoveryContext":
        return cls(
            refs=RecoveryRefs.from_dict(dict(value.get("refs") or {})),
            mode=_enum(DecisionMode, value.get("mode"), DecisionMode.INTERACTIVE),
            session_state=dict(value.get("session_state") or {}),
            permission_state=dict(value.get("permission_state") or {}),
            worker_state=dict(value.get("worker_state") or {}),
            backend_state=dict(value.get("backend_state") or {}),
            provider_state=dict(value.get("provider_state") or {}),
            checkpoint_state=dict(value.get("checkpoint_state") or {}),
            memory_evidence=tuple(dict(item) for item in value.get("memory_evidence") or ()),
            privacy_constraints=tuple(value.get("privacy_constraints") or ()),
            allowed_locations=tuple(value.get("allowed_locations") or ()),
            forbidden_actions=tuple(RecoveryAction(str(item)) for item in value.get("forbidden_actions") or ()),
            budget=RecoveryBudget.from_dict(value.get("budget")),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class RecoveryCandidate:
    action: RecoveryAction
    score: float
    eligible: bool
    policy_rule: str
    reason: str
    route_layer: RouteLayer | None = None
    delay_ms: int = 0
    requirements: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    expected_effects: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identity("policy_rule", self.policy_rule)
        require_revision("delay_ms", self.delay_ms)
        for name in ("requirements", "blockers", "expected_effects"):
            object.__setattr__(self, name, immutable_strings(getattr(self, name)))
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "score": self.score,
            "eligible": self.eligible,
            "policy_rule": self.policy_rule,
            "reason": self.reason,
            "route_layer": self.route_layer.value if self.route_layer else "",
            "delay_ms": self.delay_ms,
            "requirements": list(self.requirements),
            "blockers": list(self.blockers),
            "expected_effects": list(self.expected_effects),
            "metadata": copy.deepcopy(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RecoveryCandidate":
        layer = str(value.get("route_layer") or "")
        return cls(
            action=RecoveryAction(str(value["action"])),
            score=float(value.get("score") or 0.0),
            eligible=bool(value.get("eligible", False)),
            policy_rule=str(value.get("policy_rule") or "policy.unknown"),
            reason=str(value.get("reason") or "recovery candidate"),
            route_layer=RouteLayer(layer) if layer else None,
            delay_ms=int(value.get("delay_ms") or 0),
            requirements=tuple(value.get("requirements") or ()),
            blockers=tuple(value.get("blockers") or ()),
            expected_effects=tuple(value.get("expected_effects") or ()),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    selected: RecoveryCandidate
    candidates: tuple[RecoveryCandidate, ...]
    policy_revision: int
    deterministic_key: str
    rationale: str
    decision_id: str = field(default_factory=lambda: recovery_id("recoverydecision"))
    decided_at: str = field(default_factory=utc_now)
    advisory_notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_identity("decision_id", self.decision_id)
        require_identity("deterministic_key", self.deterministic_key)
        require_revision("policy_revision", self.policy_revision, allow_zero=False)
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "advisory_notes", immutable_strings(self.advisory_notes))
        if self.selected not in self.candidates:
            raise ValueError("selected recovery candidate is absent from candidate set")

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "selected": self.selected.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "policy_revision": self.policy_revision,
            "deterministic_key": self.deterministic_key,
            "rationale": self.rationale,
            "decided_at": self.decided_at,
            "advisory_notes": list(self.advisory_notes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RecoveryDecision":
        candidates = tuple(RecoveryCandidate.from_dict(item) for item in value.get("candidates") or ())
        selected_value = RecoveryCandidate.from_dict(dict(value.get("selected") or {}))
        selected = next((item for item in candidates if item.to_dict() == selected_value.to_dict()), selected_value)
        if selected not in candidates:
            candidates = (*candidates, selected)
        return cls(
            decision_id=str(value.get("decision_id") or recovery_id("recoverydecision")),
            selected=selected,
            candidates=candidates,
            policy_revision=int(value.get("policy_revision") or 1),
            deterministic_key=str(value.get("deterministic_key") or stable_digest(value)[:32]),
            rationale=str(value.get("rationale") or "deterministic recovery policy"),
            decided_at=str(value.get("decided_at") or utc_now()),
            advisory_notes=tuple(value.get("advisory_notes") or ()),
        )


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    signal: RecoverySignal
    decision: RecoveryDecision
    status: RecoveryPlanStatus
    idempotency_key: str
    plan_id: str = field(default_factory=lambda: recovery_id("recoveryplan"))
    revision: int = 1
    action_cursor: int = 0
    claimed_by: str = ""
    claim_expires_at: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("plan_id", "idempotency_key"):
            require_identity(name, getattr(self, name))
        for name in ("claimed_by",):
            require_identity(name, getattr(self, name), optional=True)
        require_revision("revision", self.revision, allow_zero=False)
        require_revision("action_cursor", self.action_cursor)
        object.__setattr__(self, "provenance", immutable_mapping(self.provenance))

    def evolve(self, **changes: Any) -> "RecoveryPlan":
        return replace(self, revision=self.revision + 1, updated_at=utc_now(), **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RECOVERY_PLAN_SCHEMA,
            "plan_id": self.plan_id,
            "signal": self.signal.to_dict(),
            "decision": self.decision.to_dict(),
            "status": self.status.value,
            "idempotency_key": self.idempotency_key,
            "revision": self.revision,
            "action_cursor": self.action_cursor,
            "claimed_by": self.claimed_by,
            "claim_expires_at": self.claim_expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "provenance": copy.deepcopy(dict(self.provenance)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RecoveryPlan":
        return cls(
            plan_id=str(value.get("plan_id") or recovery_id("recoveryplan")),
            signal=RecoverySignal.from_dict(dict(value.get("signal") or {})),
            decision=RecoveryDecision.from_dict(dict(value.get("decision") or {})),
            status=_enum(RecoveryPlanStatus, value.get("status"), RecoveryPlanStatus.PLANNED),
            idempotency_key=str(value.get("idempotency_key") or stable_digest(value)[:32]),
            revision=int(value.get("revision") or 1),
            action_cursor=int(value.get("action_cursor") or 0),
            claimed_by=str(value.get("claimed_by") or ""),
            claim_expires_at=str(value.get("claim_expires_at") or ""),
            created_at=str(value.get("created_at") or utc_now()),
            updated_at=str(value.get("updated_at") or utc_now()),
            provenance=dict(value.get("provenance") or {}),
        )


@dataclass(frozen=True, slots=True)
class RouteLayerChange:
    layer: RouteLayer
    owner: str
    before_ref: Mapping[str, Any]
    after_ref: Mapping[str, Any]
    requested: bool
    applied: bool
    receipt_id: str = ""
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_identity("owner", self.owner)
        require_identity("receipt_id", self.receipt_id, optional=True)
        object.__setattr__(self, "before_ref", immutable_mapping(self.before_ref))
        object.__setattr__(self, "after_ref", immutable_mapping(self.after_ref))
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer.value,
            "owner": self.owner,
            "before_ref": copy.deepcopy(dict(self.before_ref)),
            "after_ref": copy.deepcopy(dict(self.after_ref)),
            "requested": self.requested,
            "applied": self.applied,
            "receipt_id": self.receipt_id,
            "reason": self.reason,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RouteLayerChange":
        return cls(
            layer=RouteLayer(str(value["layer"])),
            owner=str(value.get("owner") or "unknown-owner"),
            before_ref=dict(value.get("before_ref") or {}),
            after_ref=dict(value.get("after_ref") or {}),
            requested=bool(value.get("requested", False)),
            applied=bool(value.get("applied", False)),
            receipt_id=str(value.get("receipt_id") or ""),
            reason=str(value.get("reason") or ""),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class LayeredRouteDecision:
    run_id: str
    task_id: str
    plan_id: str
    action: RecoveryAction
    trigger_signal_id: str
    policy_rule: str
    changes: tuple[RouteLayerChange, ...]
    escalation: str = "none"
    route_decision_id: str = field(default_factory=lambda: recovery_id("layeredroute"))
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("run_id", "task_id", "plan_id", "trigger_signal_id", "policy_rule", "route_decision_id"):
            require_identity(name, getattr(self, name))
        object.__setattr__(self, "changes", tuple(self.changes))
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))
        layers = [item.layer for item in self.changes]
        if len(layers) != len(set(layers)):
            raise ValueError("layered route decision repeats a route layer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": LAYERED_ROUTE_SCHEMA,
            "route_decision_id": self.route_decision_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "action": self.action.value,
            "trigger_signal_id": self.trigger_signal_id,
            "policy_rule": self.policy_rule,
            "changes": [item.to_dict() for item in self.changes],
            "escalation": self.escalation,
            "created_at": self.created_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LayeredRouteDecision":
        return cls(
            route_decision_id=str(value.get("route_decision_id") or recovery_id("layeredroute")),
            run_id=str(value["run_id"]),
            task_id=str(value["task_id"]),
            plan_id=str(value["plan_id"]),
            action=RecoveryAction(str(value["action"])),
            trigger_signal_id=str(value["trigger_signal_id"]),
            policy_rule=str(value.get("policy_rule") or "policy.unknown"),
            changes=tuple(RouteLayerChange.from_dict(item) for item in value.get("changes") or ()),
            escalation=str(value.get("escalation") or "none"),
            created_at=str(value.get("created_at") or utc_now()),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class SideEffectFence:
    fence_key: str
    run_id: str
    task_id: str
    operation: str
    state: SideEffectState
    request_digest: str
    response_digest: str = ""
    receipt_ref: str = ""
    revision: int = 1
    reserved_at: str = field(default_factory=utc_now)
    committed_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("fence_key", "run_id", "task_id", "operation", "request_digest"):
            require_identity(name, getattr(self, name))
        for name in ("response_digest", "receipt_ref"):
            require_identity(name, getattr(self, name), optional=True)
        require_revision("revision", self.revision, allow_zero=False)
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "fence_key": self.fence_key,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "operation": self.operation,
            "state": self.state.value,
            "request_digest": self.request_digest,
            "response_digest": self.response_digest,
            "receipt_ref": self.receipt_ref,
            "revision": self.revision,
            "reserved_at": self.reserved_at,
            "committed_at": self.committed_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SideEffectFence":
        return cls(
            fence_key=str(value["fence_key"]),
            run_id=str(value["run_id"]),
            task_id=str(value["task_id"]),
            operation=str(value["operation"]),
            state=SideEffectState(str(value["state"])),
            request_digest=str(value["request_digest"]),
            response_digest=str(value.get("response_digest") or ""),
            receipt_ref=str(value.get("receipt_ref") or ""),
            revision=int(value.get("revision") or 1),
            reserved_at=str(value.get("reserved_at") or utc_now()),
            committed_at=str(value.get("committed_at") or ""),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class CheckpointWrite:
    write_id: str
    task_key: str
    channel: str
    value: Any
    state: PendingWriteState = PendingWriteState.PENDING
    sequence: int = 0
    writer_id: str = ""
    idempotency_key: str = ""
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("write_id", "task_key", "channel"):
            require_identity(name, getattr(self, name))
        for name in ("writer_id", "idempotency_key"):
            require_identity(name, getattr(self, name), optional=True)
        require_revision("sequence", self.sequence)
        object.__setattr__(self, "value", copy.deepcopy(self.value))
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))

    @property
    def value_digest(self) -> str:
        return stable_digest(self.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "write_id": self.write_id,
            "task_key": self.task_key,
            "channel": self.channel,
            "value": copy.deepcopy(self.value),
            "value_digest": self.value_digest,
            "state": self.state.value,
            "sequence": self.sequence,
            "writer_id": self.writer_id,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CheckpointWrite":
        result = cls(
            write_id=str(value["write_id"]),
            task_key=str(value["task_key"]),
            channel=str(value["channel"]),
            value=copy.deepcopy(value.get("value")),
            state=_enum(PendingWriteState, value.get("state"), PendingWriteState.PENDING),
            sequence=int(value.get("sequence") or 0),
            writer_id=str(value.get("writer_id") or ""),
            idempotency_key=str(value.get("idempotency_key") or ""),
            created_at=str(value.get("created_at") or utc_now()),
            metadata=dict(value.get("metadata") or {}),
        )
        expected = str(value.get("value_digest") or result.value_digest)
        if expected != result.value_digest:
            raise ValueError(f"checkpoint write digest mismatch: {result.write_id}")
        return result


@dataclass(frozen=True, slots=True)
class InFlightMessage:
    message_id: str
    sender_id: str
    receiver_id: str
    correlation_id: str
    payload_ref: Mapping[str, Any]
    delivered: bool = False
    processed: bool = False
    sequence: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("message_id", "sender_id", "receiver_id", "correlation_id"):
            require_identity(name, getattr(self, name))
        require_revision("sequence", self.sequence)
        object.__setattr__(self, "payload_ref", immutable_mapping(self.payload_ref))
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))
        if self.processed and not self.delivered:
            raise ValueError("processed in-flight message must be delivered")

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sender_id": self.sender_id,
            "receiver_id": self.receiver_id,
            "correlation_id": self.correlation_id,
            "payload_ref": copy.deepcopy(dict(self.payload_ref)),
            "delivered": self.delivered,
            "processed": self.processed,
            "sequence": self.sequence,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InFlightMessage":
        return cls(
            message_id=str(value["message_id"]),
            sender_id=str(value["sender_id"]),
            receiver_id=str(value["receiver_id"]),
            correlation_id=str(value["correlation_id"]),
            payload_ref=dict(value.get("payload_ref") or {}),
            delivered=bool(value.get("delivered", False)),
            processed=bool(value.get("processed", False)),
            sequence=int(value.get("sequence") or 0),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class PendingRequestInfo:
    request_id: str
    request_kind: str
    correlation_id: str
    owner: str
    awaiting_response: bool = True
    response_id: str = ""
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("request_id", "request_kind", "correlation_id", "owner"):
            require_identity(name, getattr(self, name))
        require_identity("response_id", self.response_id, optional=True)
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))
        if self.awaiting_response and self.response_id:
            raise ValueError("pending request cannot await and contain a response id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "request_kind": self.request_kind,
            "correlation_id": self.correlation_id,
            "owner": self.owner,
            "awaiting_response": self.awaiting_response,
            "response_id": self.response_id,
            "created_at": self.created_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PendingRequestInfo":
        return cls(
            request_id=str(value["request_id"]),
            request_kind=str(value["request_kind"]),
            correlation_id=str(value["correlation_id"]),
            owner=str(value["owner"]),
            awaiting_response=bool(value.get("awaiting_response", True)),
            response_id=str(value.get("response_id") or ""),
            created_at=str(value.get("created_at") or utc_now()),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class RecoveryCheckpoint:
    run_id: str
    task_id: str
    session_id: str
    workflow_signature: str
    graph_signature: str
    topology_signature: str
    owner_refs: Mapping[str, Any]
    version_refs: Mapping[str, Any]
    committed_refs: tuple[Mapping[str, Any], ...]
    phase: CheckpointPhase = CheckpointPhase.PREPARED
    checkpoint_id: str = field(default_factory=lambda: recovery_id("recoverycheckpoint"))
    parent_checkpoint_id: str = ""
    ancestry: tuple[str, ...] = ()
    iteration: int = 0
    commit_revision: int = 0
    pending_writes: tuple[CheckpointWrite, ...] = ()
    committed_writes: tuple[CheckpointWrite, ...] = ()
    in_flight_messages: tuple[InFlightMessage, ...] = ()
    pending_requests: tuple[PendingRequestInfo, ...] = ()
    completed_step_ids: tuple[str, ...] = ()
    processed_response_ids: tuple[str, ...] = ()
    side_effect_fence_keys: tuple[str, ...] = ()
    state_payload: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    committed_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("run_id", "task_id", "session_id", "workflow_signature", "graph_signature", "topology_signature", "checkpoint_id"):
            require_identity(name, getattr(self, name))
        require_identity("parent_checkpoint_id", self.parent_checkpoint_id, optional=True)
        require_revision("iteration", self.iteration)
        require_revision("commit_revision", self.commit_revision)
        for name in ("owner_refs", "version_refs", "state_payload", "metadata"):
            object.__setattr__(self, name, immutable_mapping(getattr(self, name)))
        object.__setattr__(self, "committed_refs", tuple(immutable_mapping(item) for item in self.committed_refs))
        for name in ("ancestry", "completed_step_ids", "processed_response_ids", "side_effect_fence_keys"):
            object.__setattr__(self, name, immutable_strings(getattr(self, name)))
        object.__setattr__(self, "pending_writes", tuple(self.pending_writes))
        object.__setattr__(self, "committed_writes", tuple(self.committed_writes))
        object.__setattr__(self, "in_flight_messages", tuple(self.in_flight_messages))
        object.__setattr__(self, "pending_requests", tuple(self.pending_requests))
        if self.parent_checkpoint_id and self.parent_checkpoint_id not in self.ancestry:
            raise ValueError("parent checkpoint must appear in ancestry")
        pending_ids = {item.write_id for item in self.pending_writes}
        committed_ids = {item.write_id for item in self.committed_writes}
        if pending_ids & committed_ids:
            raise ValueError("pending and committed checkpoint writes must be disjoint")
        if any(item.state is not PendingWriteState.PENDING for item in self.pending_writes):
            raise ValueError("pending checkpoint write has non-pending state")
        if any(item.state is not PendingWriteState.COMMITTED for item in self.committed_writes):
            raise ValueError("committed checkpoint write has non-committed state")

    @property
    def signature(self) -> str:
        return stable_digest({
            "workflow": self.workflow_signature,
            "graph": self.graph_signature,
            "topology": self.topology_signature,
            "owners": dict(self.owner_refs),
            "versions": dict(self.version_refs),
        })

    @property
    def content_digest(self) -> str:
        return stable_digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": RECOVERY_CHECKPOINT_SCHEMA,
            "checkpoint_id": self.checkpoint_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "workflow_signature": self.workflow_signature,
            "graph_signature": self.graph_signature,
            "topology_signature": self.topology_signature,
            "signature": self.signature,
            "owner_refs": copy.deepcopy(dict(self.owner_refs)),
            "version_refs": copy.deepcopy(dict(self.version_refs)),
            "committed_refs": [copy.deepcopy(dict(item)) for item in self.committed_refs],
            "phase": self.phase.value,
            "parent_checkpoint_id": self.parent_checkpoint_id,
            "ancestry": list(self.ancestry),
            "iteration": self.iteration,
            "commit_revision": self.commit_revision,
            "pending_writes": [item.to_dict() for item in self.pending_writes],
            "committed_writes": [item.to_dict() for item in self.committed_writes],
            "in_flight_messages": [item.to_dict() for item in self.in_flight_messages],
            "pending_requests": [item.to_dict() for item in self.pending_requests],
            "completed_step_ids": list(self.completed_step_ids),
            "processed_response_ids": list(self.processed_response_ids),
            "side_effect_fence_keys": list(self.side_effect_fence_keys),
            "state_payload": copy.deepcopy(dict(self.state_payload)),
            "created_at": self.created_at,
            "committed_at": self.committed_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }
        if include_digest:
            value["content_digest"] = self.content_digest
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RecoveryCheckpoint":
        result = cls(
            checkpoint_id=str(value.get("checkpoint_id") or recovery_id("recoverycheckpoint")),
            run_id=str(value["run_id"]),
            task_id=str(value["task_id"]),
            session_id=str(value["session_id"]),
            workflow_signature=str(value["workflow_signature"]),
            graph_signature=str(value["graph_signature"]),
            topology_signature=str(value["topology_signature"]),
            owner_refs=dict(value.get("owner_refs") or {}),
            version_refs=dict(value.get("version_refs") or {}),
            committed_refs=tuple(value.get("committed_refs") or ()),
            phase=_enum(CheckpointPhase, value.get("phase"), CheckpointPhase.PREPARED),
            parent_checkpoint_id=str(value.get("parent_checkpoint_id") or ""),
            ancestry=tuple(value.get("ancestry") or ()),
            iteration=int(value.get("iteration") or 0),
            commit_revision=int(value.get("commit_revision") or 0),
            pending_writes=tuple(CheckpointWrite.from_dict(item) for item in value.get("pending_writes") or ()),
            committed_writes=tuple(CheckpointWrite.from_dict(item) for item in value.get("committed_writes") or ()),
            in_flight_messages=tuple(InFlightMessage.from_dict(item) for item in value.get("in_flight_messages") or ()),
            pending_requests=tuple(PendingRequestInfo.from_dict(item) for item in value.get("pending_requests") or ()),
            completed_step_ids=tuple(value.get("completed_step_ids") or ()),
            processed_response_ids=tuple(value.get("processed_response_ids") or ()),
            side_effect_fence_keys=tuple(value.get("side_effect_fence_keys") or ()),
            state_payload=dict(value.get("state_payload") or {}),
            created_at=str(value.get("created_at") or utc_now()),
            committed_at=str(value.get("committed_at") or ""),
            metadata=dict(value.get("metadata") or {}),
        )
        if str(value.get("signature") or result.signature) != result.signature:
            raise ValueError("checkpoint signature mismatch")
        if str(value.get("content_digest") or result.content_digest) != result.content_digest:
            raise ValueError("checkpoint content digest mismatch")
        return result


@dataclass(frozen=True, slots=True)
class CheckpointReceipt:
    checkpoint_id: str
    run_id: str
    task_id: str
    commit_revision: int
    phase: CheckpointPhase
    signature: str
    content_digest: str
    applied_write_ids: tuple[str, ...] = ()
    pending_write_ids: tuple[str, ...] = ()
    bypassed_step_ids: tuple[str, ...] = ()
    skipped_response_ids: tuple[str, ...] = ()
    fenced_effect_keys: tuple[str, ...] = ()
    resume_token: str = ""
    receipt_id: str = field(default_factory=lambda: recovery_id("checkpointreceipt"))
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("checkpoint_id", "run_id", "task_id", "signature", "content_digest", "receipt_id"):
            require_identity(name, getattr(self, name))
        require_identity("resume_token", self.resume_token, optional=True)
        require_revision("commit_revision", self.commit_revision)
        for name in ("applied_write_ids", "pending_write_ids", "bypassed_step_ids", "skipped_response_ids", "fenced_effect_keys"):
            object.__setattr__(self, name, immutable_strings(getattr(self, name)))
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CHECKPOINT_RECEIPT_SCHEMA,
            "receipt_id": self.receipt_id,
            "checkpoint_id": self.checkpoint_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "commit_revision": self.commit_revision,
            "phase": self.phase.value,
            "signature": self.signature,
            "content_digest": self.content_digest,
            "applied_write_ids": list(self.applied_write_ids),
            "pending_write_ids": list(self.pending_write_ids),
            "bypassed_step_ids": list(self.bypassed_step_ids),
            "skipped_response_ids": list(self.skipped_response_ids),
            "fenced_effect_keys": list(self.fenced_effect_keys),
            "resume_token": self.resume_token,
            "created_at": self.created_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CheckpointReceipt":
        return cls(
            receipt_id=str(value.get("receipt_id") or recovery_id("checkpointreceipt")),
            checkpoint_id=str(value["checkpoint_id"]),
            run_id=str(value["run_id"]),
            task_id=str(value["task_id"]),
            commit_revision=int(value.get("commit_revision") or 0),
            phase=CheckpointPhase(str(value["phase"])),
            signature=str(value["signature"]),
            content_digest=str(value["content_digest"]),
            applied_write_ids=tuple(value.get("applied_write_ids") or ()),
            pending_write_ids=tuple(value.get("pending_write_ids") or ()),
            bypassed_step_ids=tuple(value.get("bypassed_step_ids") or ()),
            skipped_response_ids=tuple(value.get("skipped_response_ids") or ()),
            fenced_effect_keys=tuple(value.get("fenced_effect_keys") or ()),
            resume_token=str(value.get("resume_token") or ""),
            created_at=str(value.get("created_at") or utc_now()),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class RecoveryActionReceipt:
    plan_id: str
    action: RecoveryAction
    status: RecoveryAttemptStatus
    owner: str
    request_digest: str
    changed_execution: bool
    receipt_id: str = field(default_factory=lambda: recovery_id("recoveryaction"))
    external_receipt_ref: str = ""
    checkpoint_receipt: CheckpointReceipt | None = None
    route_decision: LayeredRouteDecision | None = None
    before: Mapping[str, Any] = field(default_factory=dict)
    after: Mapping[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("plan_id", "owner", "request_digest", "receipt_id"):
            require_identity(name, getattr(self, name))
        for name in ("external_receipt_ref", "error_code"):
            require_identity(name, getattr(self, name), optional=True)
        for name in ("before", "after", "metadata"):
            object.__setattr__(self, name, immutable_mapping(getattr(self, name)))

    @property
    def ok(self) -> bool:
        return self.status in {RecoveryAttemptStatus.APPLIED, RecoveryAttemptStatus.SUCCEEDED, RecoveryAttemptStatus.DEFERRED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "plan_id": self.plan_id,
            "action": self.action.value,
            "status": self.status.value,
            "owner": self.owner,
            "request_digest": self.request_digest,
            "changed_execution": self.changed_execution,
            "external_receipt_ref": self.external_receipt_ref,
            "checkpoint_receipt": self.checkpoint_receipt.to_dict() if self.checkpoint_receipt else None,
            "route_decision": self.route_decision.to_dict() if self.route_decision else None,
            "before": copy.deepcopy(dict(self.before)),
            "after": copy.deepcopy(dict(self.after)),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
            "ok": self.ok,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RecoveryActionReceipt":
        checkpoint = value.get("checkpoint_receipt")
        route = value.get("route_decision")
        return cls(
            receipt_id=str(value.get("receipt_id") or recovery_id("recoveryaction")),
            plan_id=str(value["plan_id"]),
            action=RecoveryAction(str(value["action"])),
            status=RecoveryAttemptStatus(str(value["status"])),
            owner=str(value.get("owner") or "unknown-owner"),
            request_digest=str(value.get("request_digest") or stable_digest(value)),
            changed_execution=bool(value.get("changed_execution", False)),
            external_receipt_ref=str(value.get("external_receipt_ref") or ""),
            checkpoint_receipt=CheckpointReceipt.from_dict(checkpoint) if isinstance(checkpoint, Mapping) else None,
            route_decision=LayeredRouteDecision.from_dict(route) if isinstance(route, Mapping) else None,
            before=dict(value.get("before") or {}),
            after=dict(value.get("after") or {}),
            error_code=str(value.get("error_code") or ""),
            error_message=str(value.get("error_message") or ""),
            created_at=str(value.get("created_at") or utc_now()),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    plan_id: str
    signal_id: str
    kind: RecoveryOutcomeKind
    action: RecoveryAction
    summary: str
    success: bool
    outcome_id: str = field(default_factory=lambda: recovery_id("recoveryoutcome"))
    receipt_ids: tuple[str, ...] = ()
    route_decision_id: str = ""
    checkpoint_id: str = ""
    latency_ms: int = 0
    cost_delta: float = 0.0
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("plan_id", "signal_id", "outcome_id"):
            require_identity(name, getattr(self, name))
        for name in ("route_decision_id", "checkpoint_id"):
            require_identity(name, getattr(self, name), optional=True)
        if not self.summary.strip():
            raise ValueError("recovery outcome summary is required")
        require_revision("latency_ms", self.latency_ms)
        object.__setattr__(self, "receipt_ids", immutable_strings(self.receipt_ids))
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "plan_id": self.plan_id,
            "signal_id": self.signal_id,
            "kind": self.kind.value,
            "action": self.action.value,
            "summary": self.summary,
            "success": self.success,
            "receipt_ids": list(self.receipt_ids),
            "route_decision_id": self.route_decision_id,
            "checkpoint_id": self.checkpoint_id,
            "latency_ms": self.latency_ms,
            "cost_delta": self.cost_delta,
            "created_at": self.created_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RecoveryOutcome":
        return cls(
            outcome_id=str(value.get("outcome_id") or recovery_id("recoveryoutcome")),
            plan_id=str(value["plan_id"]),
            signal_id=str(value["signal_id"]),
            kind=RecoveryOutcomeKind(str(value["kind"])),
            action=RecoveryAction(str(value["action"])),
            summary=str(value.get("summary") or "recovery outcome"),
            success=bool(value.get("success", False)),
            receipt_ids=tuple(value.get("receipt_ids") or ()),
            route_decision_id=str(value.get("route_decision_id") or ""),
            checkpoint_id=str(value.get("checkpoint_id") or ""),
            latency_ms=int(value.get("latency_ms") or 0),
            cost_delta=float(value.get("cost_delta") or 0.0),
            created_at=str(value.get("created_at") or utc_now()),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class RoutingMemoryRecord:
    run_id: str
    task_id: str
    signal_kind: RecoverySignalKind
    action: RecoveryAction
    success: bool
    route_layers: tuple[RouteLayer, ...]
    evidence_refs: tuple[str, ...]
    summary: str
    record_id: str = field(default_factory=lambda: recovery_id("routingmemory"))
    worker_id: str = ""
    backend_id: str = ""
    provider_id: str = ""
    model_id: str = ""
    score_delta: float = 0.0
    penalty_until: str = ""
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("run_id", "task_id", "record_id"):
            require_identity(name, getattr(self, name))
        for name in ("worker_id", "backend_id", "provider_id", "model_id"):
            require_identity(name, getattr(self, name), optional=True)
        object.__setattr__(self, "route_layers", tuple(dict.fromkeys(self.route_layers)))
        object.__setattr__(self, "evidence_refs", immutable_strings(self.evidence_refs))
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": RECOVERY_FEEDBACK_SCHEMA,
            "record_id": self.record_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "signal_kind": self.signal_kind.value,
            "action": self.action.value,
            "success": self.success,
            "route_layers": [item.value for item in self.route_layers],
            "evidence_refs": list(self.evidence_refs),
            "summary": self.summary,
            "worker_id": self.worker_id,
            "backend_id": self.backend_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "score_delta": self.score_delta,
            "penalty_until": self.penalty_until,
            "created_at": self.created_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RoutingMemoryRecord":
        return cls(
            record_id=str(value.get("record_id") or recovery_id("routingmemory")),
            run_id=str(value["run_id"]),
            task_id=str(value["task_id"]),
            signal_kind=RecoverySignalKind(str(value["signal_kind"])),
            action=RecoveryAction(str(value["action"])),
            success=bool(value.get("success", False)),
            route_layers=tuple(RouteLayer(str(item)) for item in value.get("route_layers") or ()),
            evidence_refs=tuple(value.get("evidence_refs") or ()),
            summary=str(value.get("summary") or "routing memory feedback"),
            worker_id=str(value.get("worker_id") or ""),
            backend_id=str(value.get("backend_id") or ""),
            provider_id=str(value.get("provider_id") or ""),
            model_id=str(value.get("model_id") or ""),
            score_delta=float(value.get("score_delta") or 0.0),
            penalty_until=str(value.get("penalty_until") or ""),
            created_at=str(value.get("created_at") or utc_now()),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class DeltaEntry:
    entry_id: str
    branch_id: str
    key: str
    operation: DeltaOperation
    value: Any
    sequence: int
    expected_digest: str = ""
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("entry_id", "branch_id", "key"):
            require_identity(name, getattr(self, name))
        require_identity("expected_digest", self.expected_digest, optional=True)
        require_revision("sequence", self.sequence)
        object.__setattr__(self, "value", copy.deepcopy(self.value))
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))

    @property
    def digest(self) -> str:
        return stable_digest({
            "entry_id": self.entry_id,
            "branch_id": self.branch_id,
            "key": self.key,
            "operation": self.operation.value,
            "value": self.value,
            "sequence": self.sequence,
            "expected_digest": self.expected_digest,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "branch_id": self.branch_id,
            "key": self.key,
            "operation": self.operation.value,
            "value": copy.deepcopy(self.value),
            "sequence": self.sequence,
            "expected_digest": self.expected_digest,
            "created_at": self.created_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DeltaEntry":
        result = cls(
            entry_id=str(value["entry_id"]),
            branch_id=str(value["branch_id"]),
            key=str(value["key"]),
            operation=DeltaOperation(str(value["operation"])),
            value=copy.deepcopy(value.get("value")),
            sequence=int(value.get("sequence") or 0),
            expected_digest=str(value.get("expected_digest") or ""),
            created_at=str(value.get("created_at") or utc_now()),
            metadata=dict(value.get("metadata") or {}),
        )
        if str(value.get("digest") or result.digest) != result.digest:
            raise ValueError(f"delta entry digest mismatch: {result.entry_id}")
        return result


@dataclass(frozen=True, slots=True)
class BranchDelta:
    run_id: str
    task_id: str
    checkpoint_id: str
    base_revision: int
    owner: str
    read_set: tuple[str, ...]
    write_set: tuple[str, ...]
    entries: tuple[DeltaEntry, ...]
    branch_id: str = field(default_factory=lambda: recovery_id("recoverybranch"))
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("run_id", "task_id", "checkpoint_id", "owner", "branch_id"):
            require_identity(name, getattr(self, name))
        require_revision("base_revision", self.base_revision)
        object.__setattr__(self, "read_set", tuple(sorted(set(immutable_strings(self.read_set)))))
        object.__setattr__(self, "write_set", tuple(sorted(set(immutable_strings(self.write_set)))))
        object.__setattr__(self, "entries", tuple(sorted(self.entries, key=lambda item: (item.sequence, item.key, item.entry_id))))
        object.__setattr__(self, "metadata", immutable_mapping(self.metadata))
        if any(item.branch_id != self.branch_id for item in self.entries):
            raise ValueError("branch delta contains an entry owned by another branch")
        actual_writes = {item.key for item in self.entries}
        if actual_writes != set(self.write_set):
            raise ValueError("branch delta write set does not match entries")

    @property
    def digest(self) -> str:
        return stable_digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "branch_id": self.branch_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "checkpoint_id": self.checkpoint_id,
            "base_revision": self.base_revision,
            "owner": self.owner,
            "read_set": list(self.read_set),
            "write_set": list(self.write_set),
            "entries": [item.to_dict() for item in self.entries],
            "created_at": self.created_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }
        if include_digest:
            value["digest"] = self.digest
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BranchDelta":
        result = cls(
            branch_id=str(value["branch_id"]),
            run_id=str(value["run_id"]),
            task_id=str(value["task_id"]),
            checkpoint_id=str(value["checkpoint_id"]),
            base_revision=int(value.get("base_revision") or 0),
            owner=str(value["owner"]),
            read_set=tuple(value.get("read_set") or ()),
            write_set=tuple(value.get("write_set") or ()),
            entries=tuple(DeltaEntry.from_dict(item) for item in value.get("entries") or ()),
            created_at=str(value.get("created_at") or utc_now()),
            metadata=dict(value.get("metadata") or {}),
        )
        if str(value.get("digest") or result.digest) != result.digest:
            raise ValueError(f"branch delta digest mismatch: {result.branch_id}")
        return result


@dataclass(frozen=True, slots=True)
class DeltaConflict:
    kind: ConflictKind
    key: str
    branch_id: str
    conflicting_branch_id: str
    base_revision: int
    current_revision: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "key": self.key,
            "branch_id": self.branch_id,
            "conflicting_branch_id": self.conflicting_branch_id,
            "base_revision": self.base_revision,
            "current_revision": self.current_revision,
            "reason": self.reason,
        }


__all__ = [
    "BranchDelta", "CHECKPOINT_RECEIPT_SCHEMA", "CheckpointPhase", "CheckpointReceipt",
    "CheckpointWrite", "ConflictKind", "DecisionMode", "DeltaConflict", "DeltaEntry",
    "DeltaOperation", "InFlightMessage", "LAYERED_ROUTE_SCHEMA", "LayeredRouteDecision",
    "OMP_RECEIPT_SCHEMA", "PendingRequestInfo", "PendingWriteState", "RECOVERY_CHECKPOINT_SCHEMA",
    "RECOVERY_FEEDBACK_SCHEMA", "RECOVERY_PLAN_SCHEMA", "RECOVERY_SIGNAL_SCHEMA", "RecoveryAction",
    "RecoveryActionReceipt", "RecoveryAttemptStatus", "RecoveryBudget", "RecoveryCandidate",
    "RecoveryCheckpoint", "RecoveryContext", "RecoveryDecision", "RecoveryOutcome",
    "RecoveryOutcomeKind", "RecoveryPlan", "RecoveryPlanStatus", "RecoveryRefs", "RecoverySignal",
    "RecoverySignalKind", "RecoverySource", "RouteLayer", "RouteLayerChange", "RoutingMemoryRecord",
    "SideEffectFence", "SideEffectState", "canonical_json", "immutable_mapping", "immutable_strings",
    "recovery_id", "stable_digest", "utc_now",
]
