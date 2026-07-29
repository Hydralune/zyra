from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence


BRIDGE_COMMAND_SCHEMA = "zyra.loopx-bridge-command/v1"
BRIDGE_MAPPING_VERSION = "zyra.loopx-state-mapping/v1"
BRIDGE_APPLY_RECEIPT_SCHEMA = "zyra.loopx-bridge-apply-receipt/v1"
BRIDGE_SYNC_RECEIPT_SCHEMA = "zyra.loopx-bridge-sync-receipt/v1"
COMMITTED_GRAPH_STATUSES = frozenset({"committed", "rebased", "replayed"})


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def stable_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def compact_text(value: Any, *, limit: int = 1000) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_dict"):
        converted = value.to_dict()
        if isinstance(converted, Mapping):
            return dict(converted)
    raise BridgeContractError(
        f"{label} must be a mapping or expose to_dict().",
        code="loopx_bridge_contract_invalid",
        details={"field": label, "type": type(value).__name__},
    )


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise BridgeContractError(
            f"{label} must be an array.",
            code="loopx_bridge_contract_invalid",
            details={"field": label},
        )
    return value


class BridgeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = bool(retryable)
        self.details = dict(details or {})


class BridgeContractError(BridgeError):
    pass


class BridgeDisabledError(BridgeError):
    pass


class LoopXUnavailableError(BridgeError):
    pass


class LoopXClaimConflictError(BridgeError):
    pass


class OutboxConflictError(BridgeError):
    pass


class OutboxLeaseError(BridgeError):
    pass


class SingleWriterConflictError(BridgeError):
    pass


class SingleWriterFenceLostError(BridgeError):
    pass


class SyncStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    SYNC_DEGRADED = "sync_degraded"
    CLAIM_CONFLICT = "claim_conflict"
    QUOTA_EXHAUSTED = "quota_exhausted"
    DEAD_LETTER = "dead_letter"


class OutboxState(StrEnum):
    PENDING = "pending"
    INFLIGHT = "inflight"
    ACKED = "acked"
    DEAD_LETTER = "dead_letter"


@dataclass(frozen=True, slots=True)
class CanonicalCommitRef:
    graph_id: str
    delta_id: str
    commit_id: str
    status: str
    committed_revision: int
    snapshot_signature: str
    committed_at: str = ""
    causation_id: str = ""
    correlation_id: str = ""
    receipt_digest: str = ""

    @classmethod
    def from_value(cls, value: Any) -> "CanonicalCommitRef":
        if hasattr(value, "receipt"):
            value = value.receipt
        raw = _mapping(value, "canonical_commit")
        status = compact_text(raw.get("status"), limit=64).lower()
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
        normalized = {
            "graph_id": compact_text(raw.get("graph_id"), limit=192),
            "delta_id": compact_text(raw.get("delta_id"), limit=192),
            "commit_id": compact_text(raw.get("commit_id"), limit=192),
            "status": status,
            "committed_revision": int(raw.get("committed_revision") or 0),
            "snapshot_signature": compact_text(raw.get("snapshot_signature"), limit=256),
            "committed_at": compact_text(raw.get("created_at"), limit=64),
            "causation_id": compact_text(metadata.get("causation_id"), limit=192),
            "correlation_id": compact_text(metadata.get("correlation_id"), limit=192),
        }
        missing = [
            key
            for key in ("graph_id", "delta_id", "commit_id", "snapshot_signature")
            if not normalized[key]
        ]
        if status not in COMMITTED_GRAPH_STATUSES or normalized["committed_revision"] < 1 or missing:
            raise BridgeContractError(
                "LoopX outbox accepts only a successfully committed Zyra canonical mutation.",
                code="loopx_canonical_commit_required",
                details={
                    "status": status,
                    "committed_revision": normalized["committed_revision"],
                    "missing": missing,
                },
            )
        digest = stable_digest(normalized)
        return cls(**normalized, receipt_digest=digest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "delta_id": self.delta_id,
            "commit_id": self.commit_id,
            "status": self.status,
            "committed_revision": self.committed_revision,
            "snapshot_signature": self.snapshot_signature,
            "committed_at": self.committed_at,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "receipt_digest": self.receipt_digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CanonicalCommitRef":
        raw = dict(value)
        normalized = {
            "graph_id": compact_text(raw.get("graph_id"), limit=192),
            "delta_id": compact_text(raw.get("delta_id"), limit=192),
            "commit_id": compact_text(raw.get("commit_id"), limit=192),
            "status": compact_text(raw.get("status"), limit=64).lower(),
            "committed_revision": int(raw.get("committed_revision") or 0),
            "snapshot_signature": compact_text(
                raw.get("snapshot_signature"),
                limit=256,
            ),
            "committed_at": compact_text(raw.get("committed_at"), limit=64),
            "causation_id": compact_text(raw.get("causation_id"), limit=192),
            "correlation_id": compact_text(raw.get("correlation_id"), limit=192),
        }
        supplied_digest = compact_text(raw.get("receipt_digest"), limit=128)
        expected_digest = stable_digest(normalized)
        missing = [
            key
            for key in ("graph_id", "delta_id", "commit_id", "snapshot_signature")
            if not normalized[key]
        ]
        if (
            normalized["status"] not in COMMITTED_GRAPH_STATUSES
            or normalized["committed_revision"] < 1
            or missing
            or supplied_digest != expected_digest
        ):
            raise BridgeContractError(
                "Persisted canonical commit receipt is invalid or was modified.",
                code="loopx_canonical_receipt_invalid",
                details={
                    "status": normalized["status"],
                    "committed_revision": normalized["committed_revision"],
                    "missing": missing,
                    "digest_matches": supplied_digest == expected_digest,
                },
            )
        return cls(**normalized, receipt_digest=supplied_digest)


@dataclass(frozen=True, slots=True)
class ValidationReceipt:
    validation_passed: bool
    permission_allowed: bool
    lease_valid: bool
    budget_allowed: bool
    validation_receipt_id: str = ""
    permission_receipt_id: str = ""
    lease_receipt_id: str = ""
    budget_receipt_id: str = ""

    @property
    def spend_allowed(self) -> bool:
        return (
            self.validation_passed
            and self.permission_allowed
            and self.lease_valid
            and self.budget_allowed
        )

    @property
    def failed_gates(self) -> tuple[str, ...]:
        checks = {
            "validation": self.validation_passed,
            "permission": self.permission_allowed,
            "lease": self.lease_valid,
            "budget": self.budget_allowed,
        }
        return tuple(key for key, passed in checks.items() if not passed)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ValidationReceipt":
        raw = dict(value or {})
        return cls(
            validation_passed=raw.get("validation_passed") is True,
            permission_allowed=raw.get("permission_allowed") is True,
            lease_valid=raw.get("lease_valid") is True,
            budget_allowed=raw.get("budget_allowed") is True,
            validation_receipt_id=compact_text(raw.get("validation_receipt_id"), limit=192),
            permission_receipt_id=compact_text(raw.get("permission_receipt_id"), limit=192),
            lease_receipt_id=compact_text(raw.get("lease_receipt_id"), limit=192),
            budget_receipt_id=compact_text(raw.get("budget_receipt_id"), limit=192),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "validation_passed": self.validation_passed,
            "permission_allowed": self.permission_allowed,
            "lease_valid": self.lease_valid,
            "budget_allowed": self.budget_allowed,
            "validation_receipt_id": self.validation_receipt_id,
            "permission_receipt_id": self.permission_receipt_id,
            "lease_receipt_id": self.lease_receipt_id,
            "budget_receipt_id": self.budget_receipt_id,
            "spend_allowed": self.spend_allowed,
            "failed_gates": list(self.failed_gates),
        }


@dataclass(frozen=True, slots=True)
class TodoHint:
    todo_id: str
    title: str
    role: str = "agent"
    priority: str = "P2"
    action_kind: str = "advance"
    continuation_policy: str = "independent_handoff"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TodoHint":
        raw = dict(value)
        todo_id = compact_text(raw.get("todo_id"), limit=80).lower()
        title = compact_text(raw.get("title") or raw.get("text"), limit=500)
        role = compact_text(raw.get("role") or "agent", limit=16).lower()
        if not todo_id.startswith("todo_") or not title or role not in {"agent", "user"}:
            raise BridgeContractError(
                "LoopX todo hints require a stable todo_* id, title, and agent/user role.",
                code="loopx_bridge_contract_invalid",
                details={"todo_id": todo_id, "role": role},
            )
        return cls(
            todo_id=todo_id,
            title=title,
            role=role,
            priority=compact_text(raw.get("priority") or "P2", limit=8).upper(),
            action_kind=compact_text(raw.get("action_kind") or "advance", limit=64).lower(),
            continuation_policy=compact_text(
                raw.get("continuation_policy") or "independent_handoff",
                limit=64,
            ).lower(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "todo_id": self.todo_id,
            "title": self.title,
            "role": self.role,
            "priority": self.priority,
            "action_kind": self.action_kind,
            "continuation_policy": self.continuation_policy,
        }


@dataclass(frozen=True, slots=True)
class ClaimRequest:
    todo_id: str
    claimant: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ClaimRequest":
        raw = dict(value)
        todo_id = compact_text(raw.get("todo_id"), limit=80).lower()
        claimant = compact_text(raw.get("claimant") or raw.get("claimed_by"), limit=80).lower()
        if not todo_id.startswith("todo_") or not claimant:
            raise BridgeContractError(
                "LoopX claim requires todo_id and a private LoopX claimant token.",
                code="loopx_bridge_contract_invalid",
            )
        return cls(todo_id=todo_id, claimant=claimant)

    def to_dict(self) -> dict[str, str]:
        return {"todo_id": self.todo_id, "claimant": self.claimant}


@dataclass(frozen=True, slots=True)
class QuotaView:
    limit_slots: int
    requested_spend_slots: int = 0
    window_hours: float = 24.0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "QuotaView":
        raw = dict(value or {})
        limit_slots = max(0, int(raw.get("limit_slots", raw.get("compute", 0)) or 0))
        requested = max(
            0,
            int(raw.get("requested_spend_slots", raw.get("spend_slots", 0)) or 0),
        )
        window = max(0.01, float(raw.get("window_hours", 24.0) or 24.0))
        return cls(
            limit_slots=limit_slots,
            requested_spend_slots=requested,
            window_hours=window,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "limit_slots": self.limit_slots,
            "requested_spend_slots": self.requested_spend_slots,
            "window_hours": self.window_hours,
        }


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    source_event_id: str
    summary: str
    verified: bool = True
    evidence_refs: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "HistoryEntry":
        raw = dict(value)
        event_id = compact_text(raw.get("source_event_id") or raw.get("event_id"), limit=192)
        summary = compact_text(raw.get("summary"), limit=500)
        if not event_id or not summary:
            raise BridgeContractError(
                "LoopX history entries require source_event_id and summary.",
                code="loopx_bridge_contract_invalid",
            )
        refs = tuple(
            compact_text(item, limit=256)
            for item in _sequence(raw.get("evidence_refs"), "history.evidence_refs")
            if compact_text(item, limit=256)
        )
        return cls(
            source_event_id=event_id,
            summary=summary,
            verified=raw.get("verified", True) is True,
            evidence_refs=refs,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_event_id": self.source_event_id,
            "summary": self.summary,
            "verified": self.verified,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True)
class InteractionInput:
    input_ref: str
    feedback_ref: str = ""
    continuation_hint: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "InteractionInput | None":
        if not value:
            return None
        raw = dict(value)
        input_ref = compact_text(raw.get("input_ref"), limit=256)
        if not input_ref:
            raise BridgeContractError(
                "LoopX interaction requires a permission-checked input_ref.",
                code="loopx_bridge_contract_invalid",
            )
        return cls(
            input_ref=input_ref,
            feedback_ref=compact_text(raw.get("feedback_ref"), limit=256),
            continuation_hint=compact_text(raw.get("continuation_hint"), limit=500),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "input_ref": self.input_ref,
            "feedback_ref": self.feedback_ref,
            "continuation_hint": self.continuation_hint,
        }


@dataclass(frozen=True, slots=True)
class LoopXStateUpdate:
    goal_id: str
    objective_ref: str
    requirement_revision: str
    todos: tuple[TodoHint, ...] = ()
    claims: tuple[ClaimRequest, ...] = ()
    quota: QuotaView = field(default_factory=lambda: QuotaView(limit_slots=0))
    validation: ValidationReceipt = field(
        default_factory=lambda: ValidationReceipt(False, False, False, False)
    )
    history: tuple[HistoryEntry, ...] = ()
    interaction: InteractionInput | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LoopXStateUpdate":
        raw = dict(value)
        goal_id = compact_text(raw.get("goal_id"), limit=128)
        objective_ref = compact_text(raw.get("objective_ref"), limit=256)
        requirement_revision = compact_text(raw.get("requirement_revision"), limit=128)
        if not goal_id or any(char in goal_id for char in ("/", "\\")):
            raise BridgeContractError(
                "LoopX goal_id must be a non-empty single path token.",
                code="loopx_bridge_contract_invalid",
                details={"goal_id": goal_id},
            )
        if not objective_ref or not requirement_revision:
            raise BridgeContractError(
                "LoopX goal mapping requires objective_ref and requirement_revision references.",
                code="loopx_bridge_contract_invalid",
            )
        return cls(
            goal_id=goal_id,
            objective_ref=objective_ref,
            requirement_revision=requirement_revision,
            todos=tuple(
                TodoHint.from_mapping(_mapping(item, "todos[]"))
                for item in _sequence(raw.get("todos"), "todos")
            ),
            claims=tuple(
                ClaimRequest.from_mapping(_mapping(item, "claims[]"))
                for item in _sequence(raw.get("claims"), "claims")
            ),
            quota=QuotaView.from_mapping(
                raw.get("quota") if isinstance(raw.get("quota"), Mapping) else None
            ),
            validation=ValidationReceipt.from_mapping(
                raw.get("validation")
                if isinstance(raw.get("validation"), Mapping)
                else None
            ),
            history=tuple(
                HistoryEntry.from_mapping(_mapping(item, "history[]"))
                for item in _sequence(raw.get("history"), "history")
            ),
            interaction=InteractionInput.from_mapping(
                raw.get("interaction")
                if isinstance(raw.get("interaction"), Mapping)
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "objective_ref": self.objective_ref,
            "requirement_revision": self.requirement_revision,
            "todos": [item.to_dict() for item in self.todos],
            "claims": [item.to_dict() for item in self.claims],
            "quota": self.quota.to_dict(),
            "validation": self.validation.to_dict(),
            "history": [item.to_dict() for item in self.history],
            "interaction": self.interaction.to_dict() if self.interaction else None,
        }


@dataclass(frozen=True, slots=True)
class BridgeCommand:
    workspace_id: str
    run_id: str
    task_id: str
    canonical_commit: CanonicalCommitRef
    update: LoopXStateUpdate
    causation_id: str
    correlation_id: str
    idempotency_key: str
    created_at: str
    schema: str = BRIDGE_COMMAND_SCHEMA
    mapping_version: str = BRIDGE_MAPPING_VERSION

    @classmethod
    def create(
        cls,
        *,
        workspace_id: str,
        run_id: str,
        task_id: str,
        canonical_commit: Any,
        update: LoopXStateUpdate | Mapping[str, Any],
        created_at: str,
        causation_id: str = "",
        correlation_id: str = "",
        idempotency_key: str = "",
    ) -> "BridgeCommand":
        commit = CanonicalCommitRef.from_value(canonical_commit)
        state_update = (
            update
            if isinstance(update, LoopXStateUpdate)
            else LoopXStateUpdate.from_mapping(update)
        )
        normalized_workspace = compact_text(workspace_id, limit=192)
        normalized_run = compact_text(run_id, limit=192)
        normalized_task = compact_text(task_id, limit=192)
        normalized_created = compact_text(
            created_at or commit.committed_at,
            limit=64,
        )
        if not all((normalized_workspace, normalized_run, normalized_task, normalized_created)):
            raise BridgeContractError(
                "workspace_id, run_id, task_id, and created_at are required.",
                code="loopx_bridge_contract_invalid",
            )
        effective_causation = compact_text(
            causation_id or commit.causation_id or commit.commit_id,
            limit=192,
        )
        effective_correlation = compact_text(
            correlation_id or commit.correlation_id or normalized_run,
            limit=192,
        )
        identity = {
            "workspace_id": normalized_workspace,
            "run_id": normalized_run,
            "task_id": normalized_task,
            "commit_id": commit.commit_id,
            "mapping_version": BRIDGE_MAPPING_VERSION,
            "goal_id": state_update.goal_id,
            "update_digest": stable_digest(state_update.to_dict()),
        }
        effective_key = compact_text(idempotency_key, limit=256) or (
            f"loopx:{stable_digest(identity)}"
        )
        return cls(
            workspace_id=normalized_workspace,
            run_id=normalized_run,
            task_id=normalized_task,
            canonical_commit=commit,
            update=state_update,
            causation_id=effective_causation,
            correlation_id=effective_correlation,
            idempotency_key=effective_key,
            created_at=normalized_created,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "mapping_version": self.mapping_version,
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "canonical_commit": self.canonical_commit.to_dict(),
            "update": self.update.to_dict(),
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BridgeCommand":
        raw = dict(value)
        if raw.get("schema") != BRIDGE_COMMAND_SCHEMA:
            raise BridgeContractError(
                "Unsupported LoopX bridge command schema.",
                code="loopx_bridge_schema_unsupported",
                details={"schema": raw.get("schema")},
            )
        if raw.get("mapping_version") != BRIDGE_MAPPING_VERSION:
            raise BridgeContractError(
                "Unsupported LoopX mapping version.",
                code="loopx_bridge_mapping_unsupported",
                details={"mapping_version": raw.get("mapping_version")},
            )
        commit = CanonicalCommitRef.from_dict(
            _mapping(raw.get("canonical_commit"), "canonical_commit")
        )
        return cls(
            workspace_id=str(raw["workspace_id"]),
            run_id=str(raw["run_id"]),
            task_id=str(raw["task_id"]),
            canonical_commit=commit,
            update=LoopXStateUpdate.from_mapping(
                _mapping(raw.get("update"), "update")
            ),
            causation_id=str(raw["causation_id"]),
            correlation_id=str(raw["correlation_id"]),
            idempotency_key=str(raw["idempotency_key"]),
            created_at=str(raw["created_at"]),
        )

    @property
    def content_digest(self) -> str:
        return stable_digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    sequence: int
    command: BridgeCommand
    state: OutboxState
    attempts: int
    lease_token: str
    lease_expires_at: float | None
    available_at: float
    last_error_code: str
    last_error_message: str
    apply_receipt: Mapping[str, Any] | None
    writer_epoch: int
    writer_token: str


@dataclass(frozen=True, slots=True)
class ApplyReceipt:
    status: SyncStatus
    goal_id: str
    idempotency_key: str
    applied_event_ids: tuple[str, ...]
    duplicate_event_ids: tuple[str, ...]
    source_event_count: int
    source_checksum: str
    spend_applied_slots: int
    spend_rejected_slots: int
    quota_spent_slots: int
    quota_limit_slots: int
    continuation_allowed: bool
    last_validated_receipt: Mapping[str, Any]
    claim_conflict: Mapping[str, Any] | None = None
    quota_exhausted: bool = False
    degraded_reason: str = ""
    module_origin: str = ""
    schema: str = BRIDGE_APPLY_RECEIPT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "status": self.status.value,
            "goal_id": self.goal_id,
            "idempotency_key": self.idempotency_key,
            "applied_event_ids": list(self.applied_event_ids),
            "duplicate_event_ids": list(self.duplicate_event_ids),
            "source_event_count": self.source_event_count,
            "source_checksum": self.source_checksum,
            "spend_applied_slots": self.spend_applied_slots,
            "spend_rejected_slots": self.spend_rejected_slots,
            "quota_spent_slots": self.quota_spent_slots,
            "quota_limit_slots": self.quota_limit_slots,
            "continuation_allowed": self.continuation_allowed,
            "last_validated_receipt": dict(self.last_validated_receipt),
            "claim_conflict": dict(self.claim_conflict) if self.claim_conflict else None,
            "quota_exhausted": self.quota_exhausted,
            "degraded_reason": self.degraded_reason,
            "module_origin": self.module_origin,
        }


@dataclass(frozen=True, slots=True)
class DispatchReceipt:
    sequence: int
    status: SyncStatus
    idempotency_key: str
    attempts: int
    apply_receipt: Mapping[str, Any] | None = None
    event_id: str = ""
    artifact_id: str = ""
    error_code: str = ""
    degraded_reason: str = ""
    schema: str = BRIDGE_SYNC_RECEIPT_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "sequence": self.sequence,
            "status": self.status.value,
            "idempotency_key": self.idempotency_key,
            "attempts": self.attempts,
            "apply_receipt": dict(self.apply_receipt or {}),
            "event_id": self.event_id,
            "artifact_id": self.artifact_id,
            "error_code": self.error_code,
            "degraded_reason": self.degraded_reason,
        }


__all__ = [
    "BRIDGE_APPLY_RECEIPT_SCHEMA",
    "BRIDGE_COMMAND_SCHEMA",
    "BRIDGE_MAPPING_VERSION",
    "BRIDGE_SYNC_RECEIPT_SCHEMA",
    "ApplyReceipt",
    "BridgeCommand",
    "BridgeContractError",
    "BridgeDisabledError",
    "BridgeError",
    "CanonicalCommitRef",
    "ClaimRequest",
    "DispatchReceipt",
    "HistoryEntry",
    "InteractionInput",
    "LoopXClaimConflictError",
    "LoopXStateUpdate",
    "LoopXUnavailableError",
    "OutboxConflictError",
    "OutboxLeaseError",
    "OutboxRecord",
    "OutboxState",
    "QuotaView",
    "SingleWriterConflictError",
    "SingleWriterFenceLostError",
    "SyncStatus",
    "TodoHint",
    "ValidationReceipt",
    "canonical_json",
    "stable_digest",
]
