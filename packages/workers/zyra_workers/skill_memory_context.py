from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from zyra_core import EventRecord, EventType, now_iso, to_jsonable
from zyra_runtime.workers import WorkerRequest


BROWSER_SKILL_MEMORY_CONTEXT_PROTOCOL = "zyra.browser-skill-memory-context/v1"
BROWSER_SKILL_MEMORY_DELIVERY_PROTOCOL = "zyra.browser-skill-memory-delivery/v1"
BROWSER_SKILL_MEMORY_CHECKPOINT_PROTOCOL = "zyra.browser-skill-memory-checkpoint/v1"


class BrowserSkillMemoryContextError(RuntimeError):
    code = "browser_skill_memory_context_error"

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


class BrowserSkillMemoryProjectionCorrupt(BrowserSkillMemoryContextError):
    code = "browser_skill_memory_projection_corrupt"


class BrowserSkillMemoryScopeMismatch(BrowserSkillMemoryContextError):
    code = "browser_skill_memory_scope_mismatch"


class BrowserSkillMemoryEpochRegression(BrowserSkillMemoryContextError):
    code = "browser_skill_memory_epoch_regression"


class BrowserSkillMemoryToolScopeWidened(BrowserSkillMemoryContextError):
    code = "browser_skill_memory_tool_scope_widened"


class BrowserSkillMemoryDeliveryConflict(BrowserSkillMemoryContextError):
    code = "browser_skill_memory_delivery_conflict"


class BrowserSkillMemoryDeliveryState(StrEnum):
    PREPARED = "prepared"
    APPLIED = "applied"
    RELEASED = "released"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class BrowserSkillMemoryScope:
    run_id: str
    task_id: str
    session_id: str

    def __post_init__(self) -> None:
        missing = [
            name
            for name, value in (
                ("run_id", self.run_id),
                ("task_id", self.task_id),
                ("session_id", self.session_id),
            )
            if not str(value).strip()
        ]
        if missing:
            raise BrowserSkillMemoryProjectionCorrupt(
                "browser skill-memory scope is incomplete",
                details={"missing": missing},
            )

    @property
    def digest(self) -> str:
        return stable_digest(self.to_dict())

    def to_dict(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> BrowserSkillMemoryScope:
        return cls(
            run_id=str(value.get("runId") or value.get("run_id") or ""),
            task_id=str(value.get("taskId") or value.get("task_id") or ""),
            session_id=str(value.get("sessionId") or value.get("session_id") or ""),
        )


@dataclass(frozen=True, slots=True)
class BrowserSkillMemoryAttachment:
    candidate_id: str
    kind: str
    name: str
    source_id: str
    source_digest: str
    content: str
    token_estimate: int
    required: bool
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validated(self) -> BrowserSkillMemoryAttachment:
        if not self.candidate_id or not self.source_id or not self.source_digest:
            raise BrowserSkillMemoryProjectionCorrupt(
                "browser skill-memory attachment identity is incomplete",
                details={"candidate_id": self.candidate_id},
            )
        require_sha256(self.source_digest, "attachment source digest")
        if self.token_estimate < 0:
            raise BrowserSkillMemoryProjectionCorrupt(
                "browser skill-memory attachment token estimate is invalid",
                details={"candidate_id": self.candidate_id},
            )
        metadata_text = canonical_json(self.metadata).casefold()
        forbidden = (
            "renderedbody",
            "rendered_body",
            "skill_body",
            "executable_skill_body",
            "registry_snapshot",
            "skill_directory",
        )
        present = [marker for marker in forbidden if marker in metadata_text]
        if present:
            raise BrowserSkillMemoryProjectionCorrupt(
                "browser skill-memory attachment contains executable or owner state",
                details={"candidate_id": self.candidate_id, "forbidden": present},
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        value = self.validated()
        return {
            "candidate_id": value.candidate_id,
            "kind": value.kind,
            "name": value.name,
            "source_id": value.source_id,
            "source_digest": value.source_digest,
            "content": value.content,
            "token_estimate": value.token_estimate,
            "required": value.required,
            "metadata": dict(value.metadata),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> BrowserSkillMemoryAttachment:
        return cls(
            candidate_id=str(value.get("candidateId") or value.get("candidate_id") or ""),
            kind=str(value.get("kind") or "other"),
            name=str(value.get("name") or "restored memory"),
            source_id=str(value.get("sourceId") or value.get("source_id") or ""),
            source_digest=str(value.get("sourceDigest") or value.get("source_digest") or "").removeprefix("sha256:"),
            content=str(value.get("content") or ""),
            token_estimate=safe_integer(value.get("tokenEstimate") or value.get("token_estimate"), minimum=0),
            required=bool(value.get("required")),
            metadata=mapping(value.get("metadata")),
        ).validated()


@dataclass(frozen=True, slots=True)
class BrowserSkillMemoryProjection:
    projection_id: str
    scope: BrowserSkillMemoryScope
    boundary_id: str
    compact_projection_id: str
    context_epoch: int
    preparation_id: str
    retrieval_receipt_id: str
    fidelity_comparison_id: str
    selected_strategy: str
    summary: str
    memory_ids: tuple[str, ...]
    procedure_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    authority_skill_ids: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    denied_tools: tuple[str, ...]
    provider_id: str
    model_id: str
    provider_message: Mapping[str, Any]
    attachments: tuple[BrowserSkillMemoryAttachment, ...]
    current_authority_required: bool
    executable_skill_body_present: bool
    created_at: str
    metadata: Mapping[str, Any]
    projection_digest: str

    def semantic_projection(self) -> dict[str, Any]:
        return {
            "projectionId": self.projection_id,
            "protocol": BROWSER_SKILL_MEMORY_CONTEXT_PROTOCOL,
            "identity": {
                "runId": self.scope.run_id,
                "taskId": self.scope.task_id,
                "sessionId": self.scope.session_id,
                "workerRequestId": str(self.metadata.get("source_worker_request_id") or "browser-export"),
                "epoch": safe_integer(self.metadata.get("source_epoch"), minimum=0),
            },
            "boundaryId": self.boundary_id,
            "compactProjectionId": self.compact_projection_id,
            "contextEpoch": self.context_epoch,
            "preparationId": self.preparation_id,
            "retrievalReceiptId": self.retrieval_receipt_id,
            "fidelityComparisonId": self.fidelity_comparison_id,
            "selectedStrategy": self.selected_strategy,
            "summary": self.summary,
            "memoryIds": list(self.memory_ids),
            "procedureIds": list(self.procedure_ids),
            "evidenceIds": list(self.evidence_ids),
            "artifactIds": list(self.artifact_ids),
            "authoritySkillIds": list(self.authority_skill_ids),
            "allowedTools": list(self.allowed_tools),
            "deniedTools": list(self.denied_tools),
            "providerId": self.provider_id,
            "modelId": self.model_id,
            "providerMessage": to_jsonable(dict(self.provider_message)),
            "attachments": [
                {
                    "candidateId": item.candidate_id,
                    "kind": item.kind,
                    "name": item.name,
                    "sourceId": item.source_id,
                    "sourceDigest": item.source_digest,
                    "content": item.content,
                    "tokenEstimate": item.token_estimate,
                    "selected": True,
                    "required": item.required,
                    "metadata": dict(item.metadata),
                }
                for item in self.attachments
            ],
            "currentAuthorityRequired": self.current_authority_required,
            "executableSkillBodyPresent": self.executable_skill_body_present,
            "createdAt": self.created_at,
            "metadata": dict(self.metadata),
        }

    def validated(self) -> BrowserSkillMemoryProjection:
        required = {
            "projection_id": self.projection_id,
            "boundary_id": self.boundary_id,
            "compact_projection_id": self.compact_projection_id,
            "preparation_id": self.preparation_id,
            "retrieval_receipt_id": self.retrieval_receipt_id,
            "fidelity_comparison_id": self.fidelity_comparison_id,
            "selected_strategy": self.selected_strategy,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "created_at": self.created_at,
        }
        missing = [key for key, value in required.items() if not str(value).strip()]
        if missing:
            raise BrowserSkillMemoryProjectionCorrupt(
                "browser skill-memory projection identity is incomplete",
                details={"missing": missing},
            )
        if self.context_epoch < 1:
            raise BrowserSkillMemoryProjectionCorrupt(
                "browser skill-memory projection requires an applied context epoch",
                details={"context_epoch": self.context_epoch},
            )
        if not self.current_authority_required or self.executable_skill_body_present:
            raise BrowserSkillMemoryProjectionCorrupt(
                "browser skill-memory projection attempted to restore execution authority",
                details={
                    "current_authority_required": self.current_authority_required,
                    "executable_skill_body_present": self.executable_skill_body_present,
                },
            )
        if set(self.allowed_tools) & set(self.denied_tools):
            raise BrowserSkillMemoryToolScopeWidened(
                "browser skill-memory allowed scope intersects denied tools",
                details={"tools": sorted(set(self.allowed_tools) & set(self.denied_tools))},
            )
        for attachment in self.attachments:
            attachment.validated()
        provider_text = canonical_json(self.provider_message).casefold()
        if any(
            marker in provider_text
            for marker in (
                "executable_skill_body",
                "renderedbody",
                "rendered_body",
                "registry_snapshot",
                "skill_directory",
            )
        ):
            raise BrowserSkillMemoryProjectionCorrupt(
                "browser skill-memory provider message contains execution-owner state"
            )
        expected = stable_digest(self.semantic_projection())
        if expected != self.projection_digest:
            raise BrowserSkillMemoryProjectionCorrupt(
                "browser skill-memory projection digest mismatch",
                details={"expected": expected, "actual": self.projection_digest},
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        value = self.validated()
        return {**value.semantic_projection(), "projectionDigest": value.projection_digest}

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        expected_scope: BrowserSkillMemoryScope | None = None,
    ) -> BrowserSkillMemoryProjection:
        protocol = str(value.get("protocol") or "")
        if protocol != BROWSER_SKILL_MEMORY_CONTEXT_PROTOCOL:
            raise BrowserSkillMemoryProjectionCorrupt(
                "unsupported browser skill-memory projection protocol",
                details={"protocol": protocol},
            )
        scope = BrowserSkillMemoryScope.from_mapping(mapping(value.get("identity")))
        if expected_scope and scope != expected_scope:
            raise BrowserSkillMemoryScopeMismatch(
                "browser skill-memory projection belongs to another run/task/session",
                details={"expected": expected_scope.to_dict(), "actual": scope.to_dict()},
            )
        projection = cls(
            projection_id=str(value.get("projectionId") or value.get("projection_id") or ""),
            scope=scope,
            boundary_id=str(value.get("boundaryId") or value.get("boundary_id") or ""),
            compact_projection_id=str(value.get("compactProjectionId") or value.get("compact_projection_id") or ""),
            context_epoch=safe_integer(value.get("contextEpoch") or value.get("context_epoch"), minimum=0),
            preparation_id=str(value.get("preparationId") or value.get("preparation_id") or ""),
            retrieval_receipt_id=str(value.get("retrievalReceiptId") or value.get("retrieval_receipt_id") or ""),
            fidelity_comparison_id=str(value.get("fidelityComparisonId") or value.get("fidelity_comparison_id") or ""),
            selected_strategy=str(value.get("selectedStrategy") or value.get("selected_strategy") or ""),
            summary=str(value.get("summary") or ""),
            memory_ids=strings(value.get("memoryIds") or value.get("memory_ids")),
            procedure_ids=strings(value.get("procedureIds") or value.get("procedure_ids")),
            evidence_ids=strings(value.get("evidenceIds") or value.get("evidence_ids")),
            artifact_ids=strings(value.get("artifactIds") or value.get("artifact_ids")),
            authority_skill_ids=strings(value.get("authoritySkillIds") or value.get("authority_skill_ids")),
            allowed_tools=strings(value.get("allowedTools") or value.get("allowed_tools")),
            denied_tools=strings(value.get("deniedTools") or value.get("denied_tools")),
            provider_id=str(value.get("providerId") or value.get("provider_id") or ""),
            model_id=str(value.get("modelId") or value.get("model_id") or ""),
            provider_message=mapping(value.get("providerMessage") or value.get("provider_message")),
            attachments=tuple(
                BrowserSkillMemoryAttachment.from_mapping(item)
                for item in sequence(value.get("attachments"))
                if isinstance(item, Mapping)
            ),
            current_authority_required=bool(value.get("currentAuthorityRequired", value.get("current_authority_required"))),
            executable_skill_body_present=bool(value.get("executableSkillBodyPresent", value.get("executable_skill_body_present"))),
            created_at=str(value.get("createdAt") or value.get("created_at") or ""),
            metadata=mapping(value.get("metadata")),
            projection_digest=str(value.get("projectionDigest") or value.get("projection_digest") or ""),
        )
        return projection.validated()


@dataclass(frozen=True, slots=True)
class BrowserSkillMemoryDeliveryReceipt:
    delivery_id: str
    projection_id: str
    worker_request_id: str
    state: BrowserSkillMemoryDeliveryState
    context_epoch_before: int
    context_epoch_after: int
    context_block_ids: tuple[str, ...]
    provider_message_count: int
    selected_strategy: str
    provider_id: str
    model_id: str
    provider_switched: bool
    fallback_event_emitted: bool
    memory_ids: tuple[str, ...]
    procedure_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    authority_skill_ids: tuple[str, ...]
    current_authority_required: bool
    created_at: str
    applied_at: str
    reason: str
    metadata: Mapping[str, Any]
    delivery_digest: str

    def semantic_projection(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "protocol": BROWSER_SKILL_MEMORY_DELIVERY_PROTOCOL,
            "projection_id": self.projection_id,
            "worker_request_id": self.worker_request_id,
            "state": self.state.value,
            "context_epoch_before": self.context_epoch_before,
            "context_epoch_after": self.context_epoch_after,
            "context_block_ids": list(self.context_block_ids),
            "provider_message_count": self.provider_message_count,
            "selected_strategy": self.selected_strategy,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "provider_switched": self.provider_switched,
            "fallback_event_emitted": self.fallback_event_emitted,
            "memory_ids": list(self.memory_ids),
            "procedure_ids": list(self.procedure_ids),
            "evidence_ids": list(self.evidence_ids),
            "artifact_ids": list(self.artifact_ids),
            "authority_skill_ids": list(self.authority_skill_ids),
            "current_authority_required": self.current_authority_required,
            "created_at": self.created_at,
            "applied_at": self.applied_at,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }

    def validated(self) -> BrowserSkillMemoryDeliveryReceipt:
        if not self.delivery_id or not self.projection_id or not self.worker_request_id:
            raise BrowserSkillMemoryProjectionCorrupt("browser skill-memory delivery identity is incomplete")
        if self.context_epoch_after < self.context_epoch_before:
            raise BrowserSkillMemoryEpochRegression("browser skill-memory delivery context epoch regressed")
        if not self.current_authority_required:
            raise BrowserSkillMemoryProjectionCorrupt("browser skill-memory delivery removed current authority requirement")
        expected = stable_digest(self.semantic_projection())
        if expected != self.delivery_digest:
            raise BrowserSkillMemoryProjectionCorrupt(
                "browser skill-memory delivery digest mismatch",
                details={"delivery_id": self.delivery_id},
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        value = self.validated()
        return {**value.semantic_projection(), "delivery_digest": value.delivery_digest}


@dataclass(frozen=True, slots=True)
class BrowserSkillMemoryCheckpoint:
    scope: BrowserSkillMemoryScope
    revision: int
    context_epoch: int
    applied_projection_ids: tuple[str, ...]
    deliveries: tuple[BrowserSkillMemoryDeliveryReceipt, ...]
    last_projection_id: str
    created_at: str
    updated_at: str
    checksum: str

    def semantic_projection(self) -> dict[str, Any]:
        return {
            "protocol": BROWSER_SKILL_MEMORY_CHECKPOINT_PROTOCOL,
            "scope": self.scope.to_dict(),
            "revision": self.revision,
            "context_epoch": self.context_epoch,
            "applied_projection_ids": list(self.applied_projection_ids),
            "deliveries": [item.to_dict() for item in self.deliveries],
            "last_projection_id": self.last_projection_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def validated(self) -> BrowserSkillMemoryCheckpoint:
        if self.revision < 0 or self.context_epoch < 0:
            raise BrowserSkillMemoryProjectionCorrupt("browser skill-memory checkpoint revision/epoch is invalid")
        for delivery in self.deliveries:
            delivery.validated()
        actual = stable_digest(self.semantic_projection())
        if actual != self.checksum:
            raise BrowserSkillMemoryProjectionCorrupt(
                "browser skill-memory checkpoint checksum mismatch",
                details={"expected": self.checksum, "actual": actual},
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        value = self.validated()
        return {**value.semantic_projection(), "checksum": value.checksum}

    @classmethod
    def empty(cls, scope: BrowserSkillMemoryScope) -> BrowserSkillMemoryCheckpoint:
        timestamp = now_iso()
        checkpoint = cls(
            scope=scope,
            revision=0,
            context_epoch=0,
            applied_projection_ids=(),
            deliveries=(),
            last_projection_id="",
            created_at=timestamp,
            updated_at=timestamp,
            checksum="",
        )
        return replace(checkpoint, checksum=stable_digest(checkpoint.semantic_projection()))

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        expected_scope: BrowserSkillMemoryScope,
    ) -> BrowserSkillMemoryCheckpoint:
        if str(value.get("protocol") or "") != BROWSER_SKILL_MEMORY_CHECKPOINT_PROTOCOL:
            raise BrowserSkillMemoryProjectionCorrupt("unsupported browser skill-memory checkpoint protocol")
        scope = BrowserSkillMemoryScope.from_mapping(mapping(value.get("scope")))
        if scope != expected_scope:
            raise BrowserSkillMemoryScopeMismatch(
                "browser skill-memory checkpoint belongs to another run/task/session",
                details={"expected": expected_scope.to_dict(), "actual": scope.to_dict()},
            )
        deliveries = tuple(
            delivery_from_mapping(item)
            for item in sequence(value.get("deliveries"))
            if isinstance(item, Mapping)
        )
        checkpoint = cls(
            scope=scope,
            revision=safe_integer(value.get("revision"), minimum=0),
            context_epoch=safe_integer(value.get("context_epoch"), minimum=0),
            applied_projection_ids=strings(value.get("applied_projection_ids")),
            deliveries=deliveries,
            last_projection_id=str(value.get("last_projection_id") or ""),
            created_at=str(value.get("created_at") or ""),
            updated_at=str(value.get("updated_at") or ""),
            checksum=str(value.get("checksum") or ""),
        )
        return checkpoint.validated()


@dataclass(frozen=True, slots=True)
class BrowserSkillMemoryPreparation:
    projection: BrowserSkillMemoryProjection
    checkpoint: BrowserSkillMemoryCheckpoint
    provider_messages: tuple[Mapping[str, Any], ...]
    context_text: str
    fallback_events: tuple[EventRecord, ...]
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection": self.projection.to_dict(),
            "checkpoint": self.checkpoint.to_dict(),
            "provider_messages": [to_jsonable(dict(item)) for item in self.provider_messages],
            "context_text": self.context_text,
            "fallback_events": [to_jsonable(item) for item in self.fallback_events],
            "replayed": self.replayed,
        }


class BrowserSkillMemoryContextRuntime:
    """Apply a TypeScript 06C projection through the existing browser window.

    Python validates and transports the cross-worker projection because the
    BrowserWorker is Python-owned. It does not summarize history, select a
    procedure, resolve a skill, or evaluate a skill policy. Those decisions
    remain in 02D/06A/06B/03C and are referenced by immutable ids.
    """

    def __init__(self, *, disabled: bool = False, maximum_deliveries: int = 2_000) -> None:
        self.disabled = bool(disabled)
        self.maximum_deliveries = max(32, min(int(maximum_deliveries), 100_000))
        self._lock = threading.RLock()
        self._preparations: dict[str, BrowserSkillMemoryPreparation] = {}
        self._deliveries: dict[str, BrowserSkillMemoryDeliveryReceipt] = {}
        self._pending_projection_ids: set[str] = set()
        self._scope_epochs: dict[str, int] = {}
        self._rejections = 0
        self._fallbacks = 0
        self._provider_switches = 0

    def prepare(
        self,
        request: WorkerRequest,
        *,
        context_window: Any,
        checkpoint_value: Mapping[str, Any] | None = None,
    ) -> BrowserSkillMemoryPreparation | None:
        if self.disabled or request.constraints.get("disable_skill_memory_context") is True:
            return None
        raw = request.constraints.get("skill_memory_restore")
        if not isinstance(raw, Mapping) or not raw:
            return None
        scope = BrowserSkillMemoryScope(
            run_id=request.run_id,
            task_id=request.task_id,
            session_id=str(
                request.constraints.get("canonical_session_id")
                or request.constraints.get("session_id")
                or ""
            ),
        )
        try:
            projection = BrowserSkillMemoryProjection.from_mapping(raw, expected_scope=scope)
            checkpoint = (
                BrowserSkillMemoryCheckpoint.from_mapping(
                    checkpoint_value,
                    expected_scope=scope,
                )
                if isinstance(checkpoint_value, Mapping) and checkpoint_value
                else BrowserSkillMemoryCheckpoint.empty(scope)
            )
            self._validate_request_scope(request, projection)
            self._validate_epoch(checkpoint, projection)
            replayed = projection.projection_id in checkpoint.applied_projection_ids
            context_text = self._context_text(projection)
            provider_messages = self._provider_messages(projection, context_text)
            fallback_events = self._fallback_events(request, projection)
            preparation = BrowserSkillMemoryPreparation(
                projection=projection,
                checkpoint=checkpoint,
                provider_messages=provider_messages,
                context_text=context_text,
                fallback_events=fallback_events,
                replayed=replayed,
            )
            with self._lock:
                existing = self._preparations.get(projection.projection_id)
                if (
                    existing
                    and existing.projection.projection_digest
                    != preparation.projection.projection_digest
                ):
                    raise BrowserSkillMemoryDeliveryConflict(
                        "browser skill-memory projection replayed with different preparation",
                        details={"projection_id": projection.projection_id},
                    )
                # Checkpoint/replay state is per delivery attempt; only the
                # immutable TypeScript projection must remain identical.
                if replayed:
                    self._preparations[projection.projection_id] = preparation
                    return preparation
                current = self._deliveries.get(projection.projection_id)
                if current is not None and current.state is BrowserSkillMemoryDeliveryState.PREPARED:
                    if projection.projection_id in self._pending_projection_ids:
                        raise BrowserSkillMemoryDeliveryConflict(
                            "browser skill-memory projection is still seeding context",
                            details={"projection_id": projection.projection_id},
                        )
                    if current.worker_request_id == request.request_id and existing is not None:
                        return existing
                    raise BrowserSkillMemoryDeliveryConflict(
                        "browser skill-memory projection already has an active delivery",
                        details={
                            "projection_id": projection.projection_id,
                            "active_worker_request_id": current.worker_request_id,
                            "worker_request_id": request.request_id,
                        },
                    )
                if current is not None and current.state is BrowserSkillMemoryDeliveryState.APPLIED:
                    raise BrowserSkillMemoryDeliveryConflict(
                        "applied browser skill-memory projection is missing from the checkpoint",
                        details={"projection_id": projection.projection_id},
                    )
                reservation = self._delivery(
                    request,
                    projection,
                    checkpoint,
                    block_ids=(),
                    state=BrowserSkillMemoryDeliveryState.PREPARED,
                    reason="browser context window delivery reserved",
                    fallback_event_emitted=bool(fallback_events),
                )
                # RELEASED attempts may be retried by a new worker request.  A
                # live PREPARED attempt is fenced above before context mutation.
                self._preparations.pop(projection.projection_id, None)
                self._deliveries[projection.projection_id] = reservation
                self._pending_projection_ids.add(projection.projection_id)
            try:
                blocks = context_window.seed_request_messages(provider_messages)
            except Exception:
                with self._lock:
                    current = self._deliveries.get(projection.projection_id)
                    if current is not None and current.delivery_id == reservation.delivery_id:
                        self._deliveries.pop(projection.projection_id, None)
                        self._preparations.pop(projection.projection_id, None)
                    self._pending_projection_ids.discard(projection.projection_id)
                raise
            block_ids = tuple(str(getattr(block, "block_id", "")) for block in blocks)
            prepared_delivery = self._delivery(
                request,
                projection,
                checkpoint,
                block_ids=block_ids,
                state=BrowserSkillMemoryDeliveryState.PREPARED,
                reason="browser context window accepted skill-memory projection",
                fallback_event_emitted=bool(fallback_events),
            )
            with self._lock:
                current = self._deliveries.get(projection.projection_id)
                if current is None or current.delivery_id != reservation.delivery_id:
                    self._pending_projection_ids.discard(projection.projection_id)
                    raise BrowserSkillMemoryDeliveryConflict(
                        "browser skill-memory delivery reservation changed during context seed",
                        details={"projection_id": projection.projection_id},
                    )
                self._preparations[projection.projection_id] = preparation
                self._deliveries[projection.projection_id] = prepared_delivery
                self._pending_projection_ids.discard(projection.projection_id)
            return preparation
        except BrowserSkillMemoryContextError:
            self._rejections += 1
            raise
        except Exception as error:  # noqa: BLE001 - convert cross-language corruption to typed failure.
            self._rejections += 1
            raise BrowserSkillMemoryProjectionCorrupt(
                "browser skill-memory projection could not be prepared",
                details={"exception_type": type(error).__name__, "message": str(error)},
            ) from error

    def commit(
        self,
        request: WorkerRequest,
        preparation: BrowserSkillMemoryPreparation,
        *,
        terminal_event_ids: Sequence[str] = (),
    ) -> tuple[BrowserSkillMemoryCheckpoint, BrowserSkillMemoryDeliveryReceipt, EventRecord]:
        projection = preparation.projection
        with self._lock:
            if projection.projection_id in self._pending_projection_ids:
                raise BrowserSkillMemoryDeliveryConflict(
                    "browser skill-memory context seed is still pending",
                    details={"projection_id": projection.projection_id},
                )
            current = self._deliveries.get(projection.projection_id)
            if preparation.replayed:
                existing = next(
                    (
                        item
                        for item in preparation.checkpoint.deliveries
                        if item.projection_id == projection.projection_id
                    ),
                    None,
                )
                if existing is None:
                    raise BrowserSkillMemoryDeliveryConflict(
                        "replayed browser skill-memory projection lacks prior delivery",
                        details={"projection_id": projection.projection_id},
                    )
                event = self.event_for_delivery(request, existing, replayed=True)
                return preparation.checkpoint, existing, event
            if current is None or current.state is not BrowserSkillMemoryDeliveryState.PREPARED:
                raise BrowserSkillMemoryDeliveryConflict(
                    "browser skill-memory delivery was not prepared",
                    details={"projection_id": projection.projection_id},
                )
            if current.worker_request_id != request.request_id:
                raise BrowserSkillMemoryDeliveryConflict(
                    "browser skill-memory delivery belongs to another worker request",
                    details={
                        "projection_id": projection.projection_id,
                        "active_worker_request_id": current.worker_request_id,
                        "worker_request_id": request.request_id,
                    },
                )
            applied = replace(
                current,
                state=BrowserSkillMemoryDeliveryState.APPLIED,
                applied_at=now_iso(),
                reason="browser context window committed skill-memory projection",
                metadata={
                    **dict(current.metadata),
                    "terminal_event_ids": sorted({str(item) for item in terminal_event_ids if str(item)}),
                },
                delivery_digest="",
            )
            applied = replace(applied, delivery_digest=stable_digest(applied.semantic_projection()))
            self._deliveries[projection.projection_id] = applied
            deliveries = [
                item
                for item in preparation.checkpoint.deliveries
                if item.projection_id != projection.projection_id
            ]
            deliveries.append(applied)
            deliveries = deliveries[-self.maximum_deliveries :]
            checkpoint = BrowserSkillMemoryCheckpoint(
                scope=preparation.checkpoint.scope,
                revision=preparation.checkpoint.revision + 1,
                context_epoch=max(preparation.checkpoint.context_epoch, projection.context_epoch),
                applied_projection_ids=tuple(
                    sorted(
                        {
                            *preparation.checkpoint.applied_projection_ids,
                            projection.projection_id,
                        }
                    )
                ),
                deliveries=tuple(deliveries),
                last_projection_id=projection.projection_id,
                created_at=preparation.checkpoint.created_at,
                updated_at=now_iso(),
                checksum="",
            )
            checkpoint = replace(
                checkpoint,
                checksum=stable_digest(checkpoint.semantic_projection()),
            )
            self._scope_epochs[checkpoint.scope.digest] = checkpoint.context_epoch
            event = self.event_for_delivery(request, applied, replayed=False)
            return checkpoint, applied, event

    def release(
        self,
        request: WorkerRequest,
        preparation: BrowserSkillMemoryPreparation,
        *,
        reason: str,
    ) -> tuple[BrowserSkillMemoryDeliveryReceipt, EventRecord]:
        projection = preparation.projection
        with self._lock:
            if projection.projection_id in self._pending_projection_ids:
                raise BrowserSkillMemoryDeliveryConflict(
                    "browser skill-memory context seed is still pending",
                    details={"projection_id": projection.projection_id},
                )
            current = self._deliveries.get(projection.projection_id)
            if current is not None and current.worker_request_id != request.request_id:
                raise BrowserSkillMemoryDeliveryConflict(
                    "browser skill-memory delivery belongs to another worker request",
                    details={
                        "projection_id": projection.projection_id,
                        "active_worker_request_id": current.worker_request_id,
                        "worker_request_id": request.request_id,
                    },
                )
            if current is None:
                current = self._delivery(
                    request,
                    projection,
                    preparation.checkpoint,
                    block_ids=(),
                    state=BrowserSkillMemoryDeliveryState.RELEASED,
                    reason=reason,
                    fallback_event_emitted=bool(preparation.fallback_events),
                )
            elif current.state is BrowserSkillMemoryDeliveryState.PREPARED:
                current = replace(
                    current,
                    state=BrowserSkillMemoryDeliveryState.RELEASED,
                    applied_at=now_iso(),
                    reason=reason.strip() or "browser skill-memory delivery released",
                    delivery_digest="",
                )
                current = replace(
                    current,
                    delivery_digest=stable_digest(current.semantic_projection()),
                )
            self._deliveries[projection.projection_id] = current
            return current, self.event_for_delivery(request, current, replayed=False)

    def projection_for_result(
        self,
        preparation: BrowserSkillMemoryPreparation | None,
        checkpoint: BrowserSkillMemoryCheckpoint | None,
        delivery: BrowserSkillMemoryDeliveryReceipt | None,
    ) -> dict[str, Any]:
        if preparation is None:
            return {
                "protocol": BROWSER_SKILL_MEMORY_DELIVERY_PROTOCOL,
                "enabled": not self.disabled,
                "applied": False,
                "reason": "no_skill_memory_restore_projection",
                "canonical_skill_owner": "03C SkillCoordinator",
                "canonical_context_owner": "02D BrowserContextTaskIntegrationRuntime",
            }
        return {
            "protocol": BROWSER_SKILL_MEMORY_DELIVERY_PROTOCOL,
            "enabled": True,
            "applied": bool(
                delivery and delivery.state is BrowserSkillMemoryDeliveryState.APPLIED
            ),
            "projection": preparation.projection.to_dict(),
            "delivery": delivery.to_dict() if delivery else {},
            "checkpoint": checkpoint.to_dict() if checkpoint else {},
            "provider_messages": [
                to_jsonable(dict(item)) for item in preparation.provider_messages
            ],
            "replayed": preparation.replayed,
            "canonical_skill_owner": "03C SkillCoordinator",
            "canonical_context_owner": "02D BrowserContextTaskIntegrationRuntime",
            "executable_skill_body_present": False,
            "current_authority_required": True,
        }

    def event_for_delivery(
        self,
        request: WorkerRequest,
        receipt: BrowserSkillMemoryDeliveryReceipt,
        *,
        replayed: bool,
    ) -> EventRecord:
        return EventRecord(
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "skill_memory_browser_context": {
                    **receipt.to_dict(),
                    "replayed": replayed,
                    "changes_browser_worker_context": (
                        receipt.state is BrowserSkillMemoryDeliveryState.APPLIED
                    ),
                    "canonical_skill_owner": "03C SkillCoordinator",
                    "canonical_compact_owner": "02D ContextCompactionRuntime",
                    "canonical_context_owner": "BrowserContextTaskIntegrationRuntime",
                    "current_authority_required": True,
                    "executable_skill_body_present": False,
                }
            },
        )

    def health(self) -> dict[str, Any]:
        with self._lock:
            values = list(self._deliveries.values())
            return {
                "protocol": BROWSER_SKILL_MEMORY_DELIVERY_PROTOCOL,
                "disabled": self.disabled,
                "preparation_count": len(self._preparations),
                "delivery_count": len(values),
                "pending_delivery_count": len(self._pending_projection_ids),
                "applied_count": sum(
                    1
                    for item in values
                    if item.state is BrowserSkillMemoryDeliveryState.APPLIED
                ),
                "released_count": sum(
                    1
                    for item in values
                    if item.state is BrowserSkillMemoryDeliveryState.RELEASED
                ),
                "rejection_count": self._rejections,
                "fallback_count": self._fallbacks,
                "provider_switch_count": self._provider_switches,
                "scope_count": len(self._scope_epochs),
                "canonical_skill_owner": "03C SkillCoordinator",
                "canonical_context_owner": "02D BrowserContextTaskIntegrationRuntime",
                "owns_skill_loader": False,
                "owns_skill_policy": False,
                "owns_compact_boundary": False,
            }

    def _validate_request_scope(
        self,
        request: WorkerRequest,
        projection: BrowserSkillMemoryProjection,
    ) -> None:
        parent_allowed = browser_parent_allowed_tools(request)
        parent_denied = browser_parent_denied_tools(request)
        if not tool_scope_is_subset(parent_allowed, projection.allowed_tools):
            raise BrowserSkillMemoryToolScopeWidened(
                "browser skill-memory projection widens the BrowserWorker tool scope",
                details={
                    "parent_allowed": list(parent_allowed),
                    "projection_allowed": list(projection.allowed_tools),
                },
            )
        forbidden = sorted(set(projection.allowed_tools) & set(parent_denied))
        if forbidden:
            raise BrowserSkillMemoryToolScopeWidened(
                "browser skill-memory projection restores a denied BrowserWorker tool",
                details={"forbidden": forbidden},
            )
        expected_provider = str(
            request.constraints.get("provider_route_id")
            or request.constraints.get("provider_id")
            or projection.provider_id
        )
        if expected_provider and expected_provider != projection.provider_id:
            self._provider_switches += 1

    def _validate_epoch(
        self,
        checkpoint: BrowserSkillMemoryCheckpoint,
        projection: BrowserSkillMemoryProjection,
    ) -> None:
        known = max(
            checkpoint.context_epoch,
            self._scope_epochs.get(checkpoint.scope.digest, 0),
        )
        if projection.context_epoch < known:
            raise BrowserSkillMemoryEpochRegression(
                "browser skill-memory projection context epoch regressed",
                details={"known": known, "projection": projection.context_epoch},
            )

    def _provider_messages(
        self,
        projection: BrowserSkillMemoryProjection,
        context_text: str,
    ) -> tuple[Mapping[str, Any], ...]:
        message = dict(projection.provider_message)
        raw_content = message.get("content")
        content_parts = list(raw_content) if isinstance(raw_content, list) else []
        if not any(
            isinstance(item, Mapping)
            and str(item.get("type") or "") == "text"
            and str(item.get("text") or "").strip()
            for item in content_parts
        ):
            content_parts.insert(0, {"type": "text", "text": context_text})
        message["role"] = "user"
        message["content"] = content_parts
        metadata = dict(mapping(message.get("metadata")))
        metadata.update(
            {
                "schema": BROWSER_SKILL_MEMORY_CONTEXT_PROTOCOL,
                "skill_memory_projection_id": projection.projection_id,
                "compact_projection_id": projection.compact_projection_id,
                "compact_boundary_id": projection.boundary_id,
                "context_epoch": projection.context_epoch,
                "selected_strategy": projection.selected_strategy,
                "memory_ids": list(projection.memory_ids),
                "procedure_ids": list(projection.procedure_ids),
                "evidence_ids": list(projection.evidence_ids),
                "artifact_ids": list(projection.artifact_ids),
                "authority_skill_ids": list(projection.authority_skill_ids),
                "source_provenance": "06C_skill_memory_restore",
                "trust_level": "verified",
                "secret_redaction_state": "not_applicable_reference_only",
                "current_authority_required": True,
                "executable_skill_body_present": False,
                "read_once": False,
            }
        )
        message["metadata"] = metadata
        return (message,)

    def _context_text(self, projection: BrowserSkillMemoryProjection) -> str:
        lines = [
            f"[Zyra browser restored context epoch {projection.context_epoch}; boundary {projection.boundary_id}]",
            projection.summary,
            "Skill outcome memory and procedures below are evidence/context only.",
            "Any skill execution must resolve the current version, trust and policy through 03C SkillCoordinator.",
            "Memory ids: " + (", ".join(projection.memory_ids) or "none"),
            "Procedure ids: " + (", ".join(projection.procedure_ids) or "none"),
            "Evidence ids: " + (", ".join(projection.evidence_ids) or "none"),
            "Artifact ids: " + (", ".join(projection.artifact_ids) or "none"),
            "Authority skill ids: " + (", ".join(projection.authority_skill_ids) or "none"),
            "Effective BrowserWorker tools remain: " + (", ".join(projection.allowed_tools) or "none"),
            "Denied BrowserWorker tools remain: " + (", ".join(projection.denied_tools) or "none"),
        ]
        for attachment in projection.attachments:
            lines.extend(
                (
                    "",
                    f"[{attachment.kind.upper()} {attachment.candidate_id}] {attachment.name}",
                    attachment.content,
                    f"source={attachment.source_id} sha256:{attachment.source_digest}",
                )
            )
        return "\n".join(lines).strip()

    def _fallback_events(
        self,
        request: WorkerRequest,
        projection: BrowserSkillMemoryProjection,
    ) -> tuple[EventRecord, ...]:
        metadata = mapping(projection.provider_message.get("metadata"))
        reasons = strings(metadata.get("fallback_reasons"))
        fallback = str(metadata.get("fallback_strategy") or "")
        provider_switched = bool(metadata.get("provider_switched"))
        if provider_switched:
            reasons = tuple(sorted({*reasons, "provider_switched"}))
            self._provider_switches += 1
        if not reasons and not fallback:
            return ()
        self._fallbacks += 1
        return (
            EventRecord(
                run_id=request.run_id,
                task_id=request.task_id,
                node_id=request.node_id,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "skill_memory_compact_fallback": {
                        "projection_id": projection.projection_id,
                        "boundary_id": projection.boundary_id,
                        "selected_strategy": projection.selected_strategy,
                        "provider_id": projection.provider_id,
                        "model_id": projection.model_id,
                        "provider_switched": provider_switched,
                        "reasons": list(reasons),
                        "fallback_strategy": fallback or "extractive_artifact_refs",
                        "text_reference_baseline_preserved": True,
                        "silent_history_loss": False,
                        "canonical_compact_owner": "02D ContextCompactionRuntime",
                    }
                },
            ),
        )

    def _delivery(
        self,
        request: WorkerRequest,
        projection: BrowserSkillMemoryProjection,
        checkpoint: BrowserSkillMemoryCheckpoint,
        *,
        block_ids: Sequence[str],
        state: BrowserSkillMemoryDeliveryState,
        reason: str,
        fallback_event_emitted: bool,
    ) -> BrowserSkillMemoryDeliveryReceipt:
        created_at = now_iso()
        provider_metadata = mapping(projection.provider_message.get("metadata"))
        receipt = BrowserSkillMemoryDeliveryReceipt(
            delivery_id=stable_id(
                "browser-skill-memory-delivery",
                projection.projection_id,
                request.request_id,
            ),
            projection_id=projection.projection_id,
            worker_request_id=request.request_id,
            state=state,
            context_epoch_before=checkpoint.context_epoch,
            context_epoch_after=max(checkpoint.context_epoch, projection.context_epoch),
            context_block_ids=tuple(sorted({str(item) for item in block_ids if str(item)})),
            provider_message_count=1,
            selected_strategy=projection.selected_strategy,
            provider_id=projection.provider_id,
            model_id=projection.model_id,
            provider_switched=bool(provider_metadata.get("provider_switched")),
            fallback_event_emitted=fallback_event_emitted,
            memory_ids=projection.memory_ids,
            procedure_ids=projection.procedure_ids,
            evidence_ids=projection.evidence_ids,
            artifact_ids=projection.artifact_ids,
            authority_skill_ids=projection.authority_skill_ids,
            current_authority_required=True,
            created_at=created_at,
            applied_at=created_at if state is not BrowserSkillMemoryDeliveryState.PREPARED else "",
            reason=reason,
            metadata={
                "compact_projection_id": projection.compact_projection_id,
                "retrieval_receipt_id": projection.retrieval_receipt_id,
                "fidelity_comparison_id": projection.fidelity_comparison_id,
                "canonical_skill_owner": "03C SkillCoordinator",
                "canonical_context_owner": "02D BrowserContextTaskIntegrationRuntime",
                "action_execution_count": 0,
            },
            delivery_digest="",
        )
        return replace(receipt, delivery_digest=stable_digest(receipt.semantic_projection()))


def delivery_from_mapping(value: Mapping[str, Any]) -> BrowserSkillMemoryDeliveryReceipt:
    receipt = BrowserSkillMemoryDeliveryReceipt(
        delivery_id=str(value.get("delivery_id") or ""),
        projection_id=str(value.get("projection_id") or ""),
        worker_request_id=str(value.get("worker_request_id") or ""),
        state=BrowserSkillMemoryDeliveryState(str(value.get("state") or "prepared")),
        context_epoch_before=safe_integer(value.get("context_epoch_before"), minimum=0),
        context_epoch_after=safe_integer(value.get("context_epoch_after"), minimum=0),
        context_block_ids=strings(value.get("context_block_ids")),
        provider_message_count=safe_integer(value.get("provider_message_count"), minimum=0),
        selected_strategy=str(value.get("selected_strategy") or ""),
        provider_id=str(value.get("provider_id") or ""),
        model_id=str(value.get("model_id") or ""),
        provider_switched=bool(value.get("provider_switched")),
        fallback_event_emitted=bool(value.get("fallback_event_emitted")),
        memory_ids=strings(value.get("memory_ids")),
        procedure_ids=strings(value.get("procedure_ids")),
        evidence_ids=strings(value.get("evidence_ids")),
        artifact_ids=strings(value.get("artifact_ids")),
        authority_skill_ids=strings(value.get("authority_skill_ids")),
        current_authority_required=bool(value.get("current_authority_required")),
        created_at=str(value.get("created_at") or ""),
        applied_at=str(value.get("applied_at") or ""),
        reason=str(value.get("reason") or ""),
        metadata=mapping(value.get("metadata")),
        delivery_digest=str(value.get("delivery_digest") or ""),
    )
    return receipt.validated()


def browser_parent_allowed_tools(request: WorkerRequest) -> tuple[str, ...]:
    constraints = request.constraints
    explicit = constraints.get("browser_allowed_tools")
    if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes)):
        return strings(explicit)
    plan = constraints.get("browser_plan")
    names: list[str] = []
    if isinstance(plan, Sequence) and not isinstance(plan, (str, bytes)):
        for item in plan:
            if not isinstance(item, Mapping):
                continue
            action = str(item.get("action") or item.get("name") or "").strip()
            if action:
                names.append(action)
    return tuple(sorted(set(names or ["browser_read"])))


def browser_parent_denied_tools(request: WorkerRequest) -> tuple[str, ...]:
    values = request.constraints.get("browser_denied_tools")
    return strings(values)


def tool_scope_is_subset(parent: Sequence[str], candidate: Sequence[str]) -> bool:
    parent_set = set(parent)
    if "*" in parent_set:
        return True
    return set(candidate).issubset(parent_set)


def stable_id(prefix: str, *parts: Any) -> str:
    return f"{prefix}-{stable_digest(parts)[:32]}"


def stable_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonicalize(value: Any, *, depth: int = 0) -> Any:
    if depth > 64:
        return "[depth-limited]"
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        return int(value) if value.is_integer() else value
    if isinstance(value, Mapping):
        return {
            str(key): canonicalize(item, depth=depth + 1)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if item is not None or item is None
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [canonicalize(item, depth=depth + 1) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return canonicalize(to_dict(), depth=depth + 1)
    return str(value)


def require_sha256(value: str, label: str) -> str:
    normalized = str(value or "").removeprefix("sha256:").lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise BrowserSkillMemoryProjectionCorrupt(
            f"{label} must be a sha256 digest",
            details={"label": label},
        )
    return normalized


def mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    return ()


def strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values = (value,)
    else:
        values = sequence(value)
    return tuple(sorted({str(item).strip() for item in values if str(item).strip()}))


def safe_integer(value: Any, *, minimum: int = 0) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        parsed = 0
    return max(minimum, parsed)


__all__ = [
    "BROWSER_SKILL_MEMORY_CHECKPOINT_PROTOCOL",
    "BROWSER_SKILL_MEMORY_CONTEXT_PROTOCOL",
    "BROWSER_SKILL_MEMORY_DELIVERY_PROTOCOL",
    "BrowserSkillMemoryAttachment",
    "BrowserSkillMemoryCheckpoint",
    "BrowserSkillMemoryContextError",
    "BrowserSkillMemoryContextRuntime",
    "BrowserSkillMemoryDeliveryConflict",
    "BrowserSkillMemoryDeliveryReceipt",
    "BrowserSkillMemoryDeliveryState",
    "BrowserSkillMemoryEpochRegression",
    "BrowserSkillMemoryPreparation",
    "BrowserSkillMemoryProjection",
    "BrowserSkillMemoryProjectionCorrupt",
    "BrowserSkillMemoryScope",
    "BrowserSkillMemoryScopeMismatch",
    "BrowserSkillMemoryToolScopeWidened",
]
