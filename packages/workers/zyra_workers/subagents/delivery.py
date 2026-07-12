from __future__ import annotations

"""Durable, topology-bounded subagent continuation and result delivery."""

import copy
import hashlib
import json
import os
import random
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from threading import Condition, RLock
from typing import Any, Callable, Iterable, Mapping, Sequence

from zyra_core import new_id, now_iso


class DeliveryError(RuntimeError):
    pass


class DeliveryDisabled(DeliveryError):
    pass


class DeliveryConflict(DeliveryError):
    pass


class DeliveryTopologyViolation(DeliveryError, PermissionError):
    pass


class DeliveryNotFound(DeliveryError):
    pass


class DeliveryDirection(StrEnum):
    PARENT_TO_CHILD = "parent_to_child"
    CHILD_TO_PARENT = "child_to_parent"


class DeliveryIntent(StrEnum):
    CONTINUE = "continue"
    CLARIFY = "clarify"
    CORRECT = "correct"
    CANCEL = "cancel"
    STATUS = "status"
    TYPED_YIELD = "typed_yield"
    HANDOFF = "handoff"
    FAILURE = "failure"


class DeliveryStatus(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @property
    def terminal(self) -> bool:
        return self in {
            DeliveryStatus.ACKNOWLEDGED,
            DeliveryStatus.FAILED,
            DeliveryStatus.CANCELLED,
            DeliveryStatus.EXPIRED,
        }


@dataclass(frozen=True, slots=True)
class DeliveryBudget:
    maximum_summary_chars: int = 8_000
    maximum_payload_chars: int = 64_000
    maximum_artifact_refs: int = 64
    maximum_evidence_refs: int = 128
    maximum_pending_per_child: int = 64
    maximum_attempts: int = 12
    ttl_seconds: int = 86_400

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class LogicalTaskEdge:
    parent_task_id: str
    child_task_id: str
    run_id: str
    parent_session_id: str
    child_session_id: str
    active: bool = True
    revision: int = 0
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        for name in ("parent_task_id", "child_task_id", "run_id", "parent_session_id", "child_session_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if self.parent_task_id == self.child_task_id:
            raise ValueError("logical task edge cannot point to itself")

    @property
    def edge_id(self) -> str:
        return _digest({
            "run_id": self.run_id,
            "parent_task_id": self.parent_task_id,
            "child_task_id": self.child_task_id,
            "parent_session_id": self.parent_session_id,
            "child_session_id": self.child_session_id,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "parent_task_id": self.parent_task_id,
            "child_task_id": self.child_task_id,
            "run_id": self.run_id,
            "parent_session_id": self.parent_session_id,
            "child_session_id": self.child_session_id,
            "active": self.active,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LogicalTaskEdge":
        return cls(
            parent_task_id=str(value.get("parent_task_id") or ""),
            child_task_id=str(value.get("child_task_id") or ""),
            run_id=str(value.get("run_id") or ""),
            parent_session_id=str(value.get("parent_session_id") or ""),
            child_session_id=str(value.get("child_session_id") or ""),
            active=bool(value.get("active", True)),
            revision=max(0, int(value.get("revision") or 0)),
            created_at=str(value.get("created_at") or now_iso()),
            updated_at=str(value.get("updated_at") or now_iso()),
        )


@dataclass(frozen=True, slots=True)
class DurableDelivery:
    delivery_id: str
    edge_id: str
    run_id: str
    parent_task_id: str
    child_task_id: str
    sender_task_id: str
    target_task_id: str
    direction: DeliveryDirection
    intent: DeliveryIntent
    summary: str
    payload: Mapping[str, Any]
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    idempotency_key: str = ""
    status: DeliveryStatus = DeliveryStatus.QUEUED
    revision: int = 0
    attempts: int = 0
    next_attempt_at_ms: int = 0
    claim_token_digest: str = ""
    claimed_by: str = ""
    last_error: str = ""
    response: Mapping[str, Any] = field(default_factory=dict)
    causation_id: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    completed_at: str = ""

    def __post_init__(self) -> None:
        for name in (
            "delivery_id",
            "edge_id",
            "run_id",
            "parent_task_id",
            "child_task_id",
            "sender_task_id",
            "target_task_id",
            "idempotency_key",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        object.__setattr__(self, "summary", str(self.summary))
        object.__setattr__(self, "payload", _safe_mapping(self.payload))
        object.__setattr__(self, "response", _safe_mapping(self.response))
        object.__setattr__(self, "artifact_refs", _unique_strings(self.artifact_refs))
        object.__setattr__(self, "evidence_refs", _unique_strings(self.evidence_refs))

    @property
    def terminal(self) -> bool:
        return self.status.terminal

    @property
    def digest(self) -> str:
        return _digest(self.to_dict(redact_claim=True))

    def to_dict(self, *, redact_claim: bool = True) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "edge_id": self.edge_id,
            "run_id": self.run_id,
            "parent_task_id": self.parent_task_id,
            "child_task_id": self.child_task_id,
            "sender_task_id": self.sender_task_id,
            "target_task_id": self.target_task_id,
            "direction": self.direction.value,
            "intent": self.intent.value,
            "summary": self.summary,
            "payload": _safe_mapping(self.payload),
            "artifact_refs": list(self.artifact_refs),
            "evidence_refs": list(self.evidence_refs),
            "idempotency_key": self.idempotency_key,
            "status": self.status.value,
            "revision": self.revision,
            "attempts": self.attempts,
            "next_attempt_at_ms": self.next_attempt_at_ms,
            "claim_token_digest": "<redacted>" if redact_claim and self.claim_token_digest else self.claim_token_digest,
            "claimed_by": self.claimed_by,
            "last_error": self.last_error,
            "response": _safe_mapping(self.response),
            "causation_id": self.causation_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DurableDelivery":
        return cls(
            delivery_id=str(value.get("delivery_id") or ""),
            edge_id=str(value.get("edge_id") or ""),
            run_id=str(value.get("run_id") or ""),
            parent_task_id=str(value.get("parent_task_id") or ""),
            child_task_id=str(value.get("child_task_id") or ""),
            sender_task_id=str(value.get("sender_task_id") or ""),
            target_task_id=str(value.get("target_task_id") or ""),
            direction=DeliveryDirection(str(value.get("direction") or DeliveryDirection.PARENT_TO_CHILD.value)),
            intent=DeliveryIntent(str(value.get("intent") or DeliveryIntent.CONTINUE.value)),
            summary=str(value.get("summary") or ""),
            payload=_safe_mapping(value.get("payload")),
            artifact_refs=tuple(str(item) for item in value.get("artifact_refs") or ()),
            evidence_refs=tuple(str(item) for item in value.get("evidence_refs") or ()),
            idempotency_key=str(value.get("idempotency_key") or ""),
            status=DeliveryStatus(str(value.get("status") or DeliveryStatus.QUEUED.value)),
            revision=max(0, int(value.get("revision") or 0)),
            attempts=max(0, int(value.get("attempts") or 0)),
            next_attempt_at_ms=max(0, int(value.get("next_attempt_at_ms") or 0)),
            claim_token_digest=str(value.get("claim_token_digest") or ""),
            claimed_by=str(value.get("claimed_by") or ""),
            last_error=str(value.get("last_error") or ""),
            response=_safe_mapping(value.get("response")),
            causation_id=str(value.get("causation_id") or ""),
            created_at=str(value.get("created_at") or now_iso()),
            updated_at=str(value.get("updated_at") or now_iso()),
            completed_at=str(value.get("completed_at") or ""),
        )


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    delivery: DurableDelivery
    claim_token: str


class DurableDeliveryStore:
    schema = "zyra.subagent-delivery-store/v1"

    def __init__(self, path: str | Path, *, budget: DeliveryBudget | None = None, disabled: bool = False) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.budget = budget or DeliveryBudget()
        self.disabled = bool(disabled)
        self._lock = RLock()
        self._changed = Condition(self._lock)
        self._edges: dict[str, LogicalTaskEdge] = {}
        self._deliveries: dict[str, DurableDelivery] = {}
        self._idempotency: dict[str, str] = {}
        self._load()

    def bind_edge(self, edge: LogicalTaskEdge) -> LogicalTaskEdge:
        self._require_enabled()
        with self._lock:
            existing = self._edges.get(edge.edge_id)
            if existing:
                if existing.to_dict() != edge.to_dict():
                    raise DeliveryConflict("logical task edge identity changed")
                return copy.deepcopy(existing)
            self._edges[edge.edge_id] = copy.deepcopy(edge)
            self._persist()
            return copy.deepcopy(edge)

    def deactivate_edge(self, edge_id: str) -> LogicalTaskEdge:
        self._require_enabled()
        with self._lock:
            current = self._edges.get(edge_id)
            if current is None:
                raise DeliveryNotFound(edge_id)
            changed = replace(current, active=False, revision=current.revision + 1, updated_at=now_iso())
            self._edges[edge_id] = changed
            self._persist()
            return copy.deepcopy(changed)

    def enqueue(
        self,
        *,
        edge_id: str,
        sender_task_id: str,
        target_task_id: str,
        intent: DeliveryIntent,
        summary: str,
        payload: Mapping[str, Any] | None,
        artifact_refs: Iterable[str] = (),
        evidence_refs: Iterable[str] = (),
        idempotency_key: str,
        causation_id: str = "",
    ) -> DurableDelivery:
        self._require_enabled()
        with self._lock:
            edge = self._edges.get(edge_id)
            if edge is None or not edge.active:
                raise DeliveryTopologyViolation("logical parent-child edge is missing or inactive")
            direction = self._direction(edge, sender_task_id, target_task_id)
            self._validate_intent(direction, intent)
            existing_id = self._idempotency.get(idempotency_key)
            if existing_id:
                existing = self._deliveries[existing_id]
                candidate = {
                    "edge_id": edge_id,
                    "sender_task_id": sender_task_id,
                    "target_task_id": target_task_id,
                    "intent": intent.value,
                    "summary": summary,
                    "payload": _safe_mapping(payload),
                    "artifact_refs": list(_unique_strings(artifact_refs)),
                    "evidence_refs": list(_unique_strings(evidence_refs)),
                }
                existing_candidate = {
                    "edge_id": existing.edge_id,
                    "sender_task_id": existing.sender_task_id,
                    "target_task_id": existing.target_task_id,
                    "intent": existing.intent.value,
                    "summary": existing.summary,
                    "payload": existing.payload,
                    "artifact_refs": list(existing.artifact_refs),
                    "evidence_refs": list(existing.evidence_refs),
                }
                if _digest(candidate) != _digest(existing_candidate):
                    raise DeliveryConflict("delivery idempotency key reused with different payload")
                return copy.deepcopy(existing)
            pending = sum(
                1
                for item in self._deliveries.values()
                if item.child_task_id == edge.child_task_id and not item.terminal
            )
            if pending >= self.budget.maximum_pending_per_child:
                raise DeliveryConflict("child delivery queue is full")
            selected_summary = str(summary).strip()
            if not selected_summary or len(selected_summary) > self.budget.maximum_summary_chars:
                raise DeliveryConflict("delivery summary is empty or exceeds budget")
            selected_payload = _safe_mapping(payload)
            if _char_size(selected_payload) > self.budget.maximum_payload_chars:
                raise DeliveryConflict("delivery payload exceeds budget; use artifact refs")
            selected_artifacts = _unique_strings(artifact_refs)
            selected_evidence = _unique_strings(evidence_refs)
            if len(selected_artifacts) > self.budget.maximum_artifact_refs:
                raise DeliveryConflict("delivery has too many artifact refs")
            if len(selected_evidence) > self.budget.maximum_evidence_refs:
                raise DeliveryConflict("delivery has too many evidence refs")
            delivery = DurableDelivery(
                delivery_id=new_id("delivery"),
                edge_id=edge.edge_id,
                run_id=edge.run_id,
                parent_task_id=edge.parent_task_id,
                child_task_id=edge.child_task_id,
                sender_task_id=sender_task_id,
                target_task_id=target_task_id,
                direction=direction,
                intent=intent,
                summary=selected_summary,
                payload=selected_payload,
                artifact_refs=selected_artifacts,
                evidence_refs=selected_evidence,
                idempotency_key=idempotency_key,
                next_attempt_at_ms=_now_ms(),
                causation_id=causation_id,
            )
            self._deliveries[delivery.delivery_id] = delivery
            self._idempotency[idempotency_key] = delivery.delivery_id
            self._persist()
            self._changed.notify_all()
            return copy.deepcopy(delivery)

    def claim(
        self,
        *,
        target_task_id: str,
        claimant_id: str,
        limit: int = 16,
    ) -> tuple[DeliveryClaim, ...]:
        self._require_enabled()
        claims = []
        with self._lock:
            now = _now_ms()
            candidates = sorted(
                (
                    item for item in self._deliveries.values()
                    if item.target_task_id == target_task_id
                    and item.status is DeliveryStatus.QUEUED
                    and item.next_attempt_at_ms <= now
                ),
                key=lambda item: (item.created_at, item.delivery_id),
            )[: max(1, min(int(limit), 128))]
            for item in candidates:
                token = new_id("deliveryclaim") + new_id("nonce")
                changed = replace(
                    item,
                    status=DeliveryStatus.CLAIMED,
                    revision=item.revision + 1,
                    attempts=item.attempts + 1,
                    claim_token_digest=_digest_text(token),
                    claimed_by=claimant_id,
                    updated_at=now_iso(),
                )
                self._deliveries[item.delivery_id] = changed
                claims.append(DeliveryClaim(copy.deepcopy(changed), token))
            if claims:
                self._persist()
                self._changed.notify_all()
        return tuple(claims)

    def delivered(self, delivery_id: str, *, claim_token: str, response: Mapping[str, Any] | None = None) -> DurableDelivery:
        return self._claim_transition(
            delivery_id,
            claim_token=claim_token,
            target=DeliveryStatus.DELIVERED,
            response=response,
        )

    def acknowledge(self, delivery_id: str, *, claim_token: str, response: Mapping[str, Any] | None = None) -> DurableDelivery:
        return self._claim_transition(
            delivery_id,
            claim_token=claim_token,
            target=DeliveryStatus.ACKNOWLEDGED,
            response=response,
        )

    def fail(self, delivery_id: str, *, claim_token: str, error: Exception | str, retryable: bool) -> DurableDelivery:
        self._require_enabled()
        with self._lock:
            current = self._deliveries.get(delivery_id)
            if current is None:
                raise DeliveryNotFound(delivery_id)
            self._assert_claim(current, claim_token)
            if retryable and current.attempts < self.budget.maximum_attempts:
                delay = min(30_000, 500 * (2 ** min(max(current.attempts - 1, 0), 8)) + random.randint(0, 200))
                status = DeliveryStatus.QUEUED
                next_attempt = _now_ms() + delay
                completed_at = ""
            else:
                status = DeliveryStatus.FAILED
                next_attempt = current.next_attempt_at_ms
                completed_at = now_iso()
            changed = replace(
                current,
                status=status,
                revision=current.revision + 1,
                next_attempt_at_ms=next_attempt,
                claim_token_digest="",
                claimed_by="",
                last_error=str(error),
                updated_at=now_iso(),
                completed_at=completed_at,
            )
            self._deliveries[delivery_id] = changed
            self._persist()
            self._changed.notify_all()
            return copy.deepcopy(changed)

    def cancel(self, delivery_id: str, *, actor_task_id: str, reason: str) -> DurableDelivery:
        self._require_enabled()
        with self._lock:
            current = self._deliveries.get(delivery_id)
            if current is None:
                raise DeliveryNotFound(delivery_id)
            if actor_task_id not in {current.parent_task_id, current.sender_task_id}:
                raise DeliveryTopologyViolation("only parent or sender may cancel a delivery")
            if current.terminal:
                return copy.deepcopy(current)
            changed = replace(
                current,
                status=DeliveryStatus.CANCELLED,
                revision=current.revision + 1,
                last_error=reason,
                claim_token_digest="",
                claimed_by="",
                completed_at=now_iso(),
                updated_at=now_iso(),
            )
            self._deliveries[delivery_id] = changed
            self._persist()
            self._changed.notify_all()
            return copy.deepcopy(changed)

    def recover_claims(self) -> tuple[DurableDelivery, ...]:
        """Return process-local claims to queue after restart without effects."""

        recovered = []
        with self._lock:
            for delivery_id, current in list(self._deliveries.items()):
                if current.status is not DeliveryStatus.CLAIMED:
                    continue
                changed = replace(
                    current,
                    status=DeliveryStatus.QUEUED,
                    revision=current.revision + 1,
                    claim_token_digest="",
                    claimed_by="",
                    next_attempt_at_ms=_now_ms(),
                    last_error="claim recovered after process restart before delivery commit",
                    updated_at=now_iso(),
                )
                self._deliveries[delivery_id] = changed
                recovered.append(copy.deepcopy(changed))
            if recovered:
                self._persist()
                self._changed.notify_all()
        return tuple(recovered)

    def expire_due(self, *, now_ms: int | None = None) -> tuple[DurableDelivery, ...]:
        selected_now = _now_ms() if now_ms is None else int(now_ms)
        expired = []
        with self._lock:
            for delivery_id, current in list(self._deliveries.items()):
                if current.terminal:
                    continue
                created_ms = _iso_to_epoch_ms(current.created_at)
                if selected_now - created_ms < self.budget.ttl_seconds * 1000:
                    continue
                changed = replace(
                    current,
                    status=DeliveryStatus.EXPIRED,
                    revision=current.revision + 1,
                    claim_token_digest="",
                    claimed_by="",
                    last_error="delivery expired",
                    completed_at=now_iso(),
                    updated_at=now_iso(),
                )
                self._deliveries[delivery_id] = changed
                expired.append(copy.deepcopy(changed))
            if expired:
                self._persist()
                self._changed.notify_all()
        return tuple(expired)

    def get(self, delivery_id: str) -> DurableDelivery:
        self._require_enabled()
        with self._lock:
            value = self._deliveries.get(delivery_id)
            if value is None:
                raise DeliveryNotFound(delivery_id)
            return copy.deepcopy(value)

    def list(
        self,
        *,
        target_task_id: str | None = None,
        parent_task_id: str | None = None,
        include_terminal: bool = True,
    ) -> tuple[DurableDelivery, ...]:
        self._require_enabled()
        with self._lock:
            return tuple(sorted(
                (
                    copy.deepcopy(item)
                    for item in self._deliveries.values()
                    if (target_task_id is None or item.target_task_id == target_task_id)
                    and (parent_task_id is None or item.parent_task_id == parent_task_id)
                    and (include_terminal or not item.terminal)
                ),
                key=lambda item: (item.created_at, item.delivery_id),
            ))

    def wait_terminal(self, delivery_id: str, timeout: float | None = None) -> DurableDelivery:
        with self._changed:
            if delivery_id not in self._deliveries:
                raise DeliveryNotFound(delivery_id)
            self._changed.wait_for(lambda: self._deliveries[delivery_id].terminal, timeout=timeout)
            return copy.deepcopy(self._deliveries[delivery_id])

    def _claim_transition(
        self,
        delivery_id: str,
        *,
        claim_token: str,
        target: DeliveryStatus,
        response: Mapping[str, Any] | None,
    ) -> DurableDelivery:
        self._require_enabled()
        with self._lock:
            current = self._deliveries.get(delivery_id)
            if current is None:
                raise DeliveryNotFound(delivery_id)
            self._assert_claim(current, claim_token)
            allowed = {
                DeliveryStatus.CLAIMED: {DeliveryStatus.DELIVERED, DeliveryStatus.ACKNOWLEDGED},
                DeliveryStatus.DELIVERED: {DeliveryStatus.ACKNOWLEDGED},
            }
            if target not in allowed.get(current.status, set()):
                if current.status is target:
                    return copy.deepcopy(current)
                raise DeliveryConflict(f"invalid delivery transition {current.status.value}->{target.value}")
            changed = replace(
                current,
                status=target,
                revision=current.revision + 1,
                response=_safe_mapping(response),
                last_error="",
                updated_at=now_iso(),
                completed_at=(now_iso() if target.terminal else current.completed_at),
            )
            self._deliveries[delivery_id] = changed
            self._persist()
            self._changed.notify_all()
            return copy.deepcopy(changed)

    @staticmethod
    def _direction(edge: LogicalTaskEdge, sender: str, target: str) -> DeliveryDirection:
        if sender == edge.parent_task_id and target == edge.child_task_id:
            return DeliveryDirection.PARENT_TO_CHILD
        if sender == edge.child_task_id and target == edge.parent_task_id:
            return DeliveryDirection.CHILD_TO_PARENT
        raise DeliveryTopologyViolation(
            "delivery must follow the exact parent-child edge; sibling and broadcast messaging are forbidden"
        )

    @staticmethod
    def _validate_intent(direction: DeliveryDirection, intent: DeliveryIntent) -> None:
        allowed = {
            DeliveryDirection.PARENT_TO_CHILD: {
                DeliveryIntent.CONTINUE,
                DeliveryIntent.CLARIFY,
                DeliveryIntent.CORRECT,
                DeliveryIntent.CANCEL,
                DeliveryIntent.STATUS,
            },
            DeliveryDirection.CHILD_TO_PARENT: {
                DeliveryIntent.TYPED_YIELD,
                DeliveryIntent.HANDOFF,
                DeliveryIntent.FAILURE,
                DeliveryIntent.STATUS,
            },
        }
        if intent not in allowed[direction]:
            raise DeliveryTopologyViolation(
                f"intent {intent.value} is forbidden for direction {direction.value}"
            )

    @staticmethod
    def _assert_claim(delivery: DurableDelivery, token: str) -> None:
        if not token or not delivery.claim_token_digest:
            raise DeliveryConflict("delivery claim token is required")
        if not _constant_time_equal(delivery.claim_token_digest, _digest_text(token)):
            raise DeliveryConflict("delivery claim token mismatch")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            body = {
                "schema": self.schema,
                "owner": "M1-03D DurableDeliveryStore",
                "free_chat_allowed": False,
                "edges": [item.to_dict() for item in sorted(self._edges.values(), key=lambda value: value.edge_id)],
                "deliveries": [item.to_dict(redact_claim=False) for item in sorted(self._deliveries.values(), key=lambda value: value.delivery_id)],
                "idempotency": dict(sorted(self._idempotency.items())),
            }
            return {**body, "checksum": _digest(body)}

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DeliveryError(f"cannot load delivery store: {error}") from error
        if not isinstance(value, Mapping):
            raise DeliveryError("delivery store root must be an object")
        body = {key: copy.deepcopy(item) for key, item in value.items() if key != "checksum"}
        expected = str(value.get("checksum") or "")
        if expected and not _constant_time_equal(expected, _digest(body)):
            raise DeliveryError("delivery store checksum mismatch")
        for item in value.get("edges") or ():
            if isinstance(item, Mapping):
                edge = LogicalTaskEdge.from_dict(item)
                self._edges[edge.edge_id] = edge
        for item in value.get("deliveries") or ():
            if isinstance(item, Mapping):
                delivery = DurableDelivery.from_dict(item)
                self._deliveries[delivery.delivery_id] = delivery
        raw_idempotency = value.get("idempotency")
        if isinstance(raw_idempotency, Mapping):
            self._idempotency = {str(key): str(item) for key, item in raw_idempotency.items()}

    def _persist(self) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)

    def _require_enabled(self) -> None:
        if self.disabled:
            raise DeliveryDisabled("DurableDeliveryStore is disabled")


def _safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for key, item in value.items():
        name = str(key)
        if any(token in name.casefold() for token in ("secret", "token", "password", "authorization", "credential")):
            result[name] = "<redacted>"
        elif isinstance(item, Mapping):
            result[name] = _safe_mapping(item)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            result[name] = [_safe_value(entry) for entry in item]
        else:
            result[name] = _safe_value(item)
    return result


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return _safe_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_safe_value(item) for item in value]
    return str(value)


def _unique_strings(values: Iterable[Any]) -> tuple[str, ...]:
    seen: set[str] = set()
    result = []
    for value in values:
        selected = str(value).strip()
        if not selected or selected in seen:
            continue
        seen.add(selected)
        result.append(selected)
    return tuple(result)


def _char_size(value: Any) -> int:
    return len(json.dumps(_safe_value(value), ensure_ascii=False, sort_keys=True))


def _digest(value: Any) -> str:
    payload = json.dumps(_safe_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _digest_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(str(value).encode('utf-8')).hexdigest()}"


def _constant_time_equal(left: str, right: str) -> bool:
    return hashlib.sha256(left.encode("utf-8")).digest() == hashlib.sha256(right.encode("utf-8")).digest()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso_to_epoch_ms(value: str) -> int:
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return _now_ms()


__all__ = [
    "DeliveryBudget",
    "DeliveryClaim",
    "DeliveryConflict",
    "DeliveryDirection",
    "DeliveryDisabled",
    "DeliveryError",
    "DeliveryIntent",
    "DeliveryNotFound",
    "DeliveryStatus",
    "DeliveryTopologyViolation",
    "DurableDelivery",
    "DurableDeliveryStore",
    "LogicalTaskEdge",
]
