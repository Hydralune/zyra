from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from threading import RLock
from typing import Any, Iterable, Mapping, Sequence

from .attachments import SkillAttachmentRuntime, estimate_tokens
from .compact_bridge import SkillCompactReference, compact_reference_from_dict
from .digests import arguments_digest, digest_object
from .errors import SkillBudgetExceeded, SkillCompactRestoreError
from .integration_errors import (
    SkillCheckpointConflict,
    SkillCheckpointCorrupt,
    SkillRestoreMessageRejected,
    SkillRestoreReferenceRejected,
    SkillSessionClosed,
    SkillSessionConflict,
    SkillSessionOwnershipError,
)
from .models import (
    InvokedSkillState,
    SkillAttachment,
    SkillInvocationMode,
    SkillInvocationPlan,
    SkillInvocationStatus,
    SkillMessageDelta,
    SkillVersionRef,
    utc_now,
)


class SkillDisclosureKind(StrEnum):
    INLINE_INITIAL = "inline_initial"
    INLINE_RESTORE = "inline_restore"
    FORK_HANDOFF = "fork_handoff"
    TERMINAL_OUTCOME = "terminal_outcome"


class SkillDisclosureStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    CONSUMED = "consumed"
    INVALIDATED = "invalidated"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {
            SkillDisclosureStatus.CONSUMED,
            SkillDisclosureStatus.INVALIDATED,
            SkillDisclosureStatus.FAILED,
        }


@dataclass(frozen=True, slots=True)
class SkillDisclosureEnvelope:
    disclosure_id: str
    invocation_id: str
    run_id: str
    task_id: str
    session_id: str
    agent_id: str
    version_ref: SkillVersionRef
    kind: SkillDisclosureKind
    status: SkillDisclosureStatus
    parent_tool_use_id: str
    argument_digest: str
    arguments: dict[str, Any]
    selected_resource_refs: tuple[str, ...]
    attachment_refs: tuple[str, ...]
    message_delta_refs: tuple[str, ...]
    policy_snapshot_digest: str
    disclosure_epoch: int
    registry_generation: int
    claim_token_digest: str = ""
    claimed_by_request_id: str = ""
    claimed_at: str = ""
    consumed_turn_id: str = ""
    consumed_at: str = ""
    invalidation_reason: str = ""
    revision: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        required = {
            "disclosure_id": self.disclosure_id,
            "invocation_id": self.invocation_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "parent_tool_use_id": self.parent_tool_use_id,
            "argument_digest": self.argument_digest,
            "policy_snapshot_digest": self.policy_snapshot_digest,
        }
        missing = sorted(name for name, value in required.items() if not str(value).strip())
        if missing:
            raise ValueError(f"skill disclosure envelope is incomplete: {missing}")
        if self.disclosure_epoch < 1:
            raise ValueError("skill disclosure epoch must be positive")
        if self.registry_generation < 1:
            raise ValueError("skill disclosure registry generation must be positive")
        if self.status is SkillDisclosureStatus.CLAIMED and not self.claim_token_digest:
            raise ValueError("claimed skill disclosure requires a claim token digest")
        if self.status is SkillDisclosureStatus.CONSUMED and not self.consumed_turn_id:
            raise ValueError("consumed skill disclosure requires a turn id")

    @property
    def immutable_ref(self) -> str:
        return f"skill-disclosure://{self.session_id}/{self.disclosure_id}@{self.disclosure_epoch}"

    @property
    def can_disclose_body(self) -> bool:
        return self.kind in {
            SkillDisclosureKind.INLINE_INITIAL,
            SkillDisclosureKind.INLINE_RESTORE,
        }

    def to_dict(self, *, include_arguments: bool = False) -> dict[str, Any]:
        value = {
            "disclosure_id": self.disclosure_id,
            "invocation_id": self.invocation_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "version_ref": self.version_ref.to_dict(),
            "kind": str(self.kind),
            "status": str(self.status),
            "parent_tool_use_id": self.parent_tool_use_id,
            "argument_digest": self.argument_digest,
            "selected_resource_refs": list(self.selected_resource_refs),
            "attachment_refs": list(self.attachment_refs),
            "message_delta_refs": list(self.message_delta_refs),
            "policy_snapshot_digest": self.policy_snapshot_digest,
            "disclosure_epoch": self.disclosure_epoch,
            "registry_generation": self.registry_generation,
            "claim_token_digest": self.claim_token_digest,
            "claimed_by_request_id": self.claimed_by_request_id,
            "claimed_at": self.claimed_at,
            "consumed_turn_id": self.consumed_turn_id,
            "consumed_at": self.consumed_at,
            "invalidation_reason": self.invalidation_reason,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "immutable_ref": self.immutable_ref,
            "body_in_checkpoint": False,
        }
        if include_arguments:
            value["arguments"] = copy.deepcopy(self.arguments)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SkillDisclosureEnvelope":
        raw = dict(value)
        version = raw.get("version_ref")
        if not isinstance(version, Mapping):
            raise SkillCheckpointCorrupt("skill disclosure is missing version_ref")
        arguments = raw.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, Mapping):
            raise SkillCheckpointCorrupt("skill disclosure arguments are not an object")
        try:
            return cls(
                disclosure_id=str(raw.get("disclosure_id") or ""),
                invocation_id=str(raw.get("invocation_id") or ""),
                run_id=str(raw.get("run_id") or ""),
                task_id=str(raw.get("task_id") or ""),
                session_id=str(raw.get("session_id") or ""),
                agent_id=str(raw.get("agent_id") or ""),
                version_ref=SkillVersionRef.from_dict(version),
                kind=SkillDisclosureKind(str(raw.get("kind") or "")),
                status=SkillDisclosureStatus(str(raw.get("status") or "")),
                parent_tool_use_id=str(raw.get("parent_tool_use_id") or ""),
                argument_digest=str(raw.get("argument_digest") or ""),
                arguments=dict(arguments),
                selected_resource_refs=tuple(str(item) for item in raw.get("selected_resource_refs") or ()),
                attachment_refs=tuple(str(item) for item in raw.get("attachment_refs") or ()),
                message_delta_refs=tuple(str(item) for item in raw.get("message_delta_refs") or ()),
                policy_snapshot_digest=str(raw.get("policy_snapshot_digest") or ""),
                disclosure_epoch=int(raw.get("disclosure_epoch") or 0),
                registry_generation=int(raw.get("registry_generation") or 0),
                claim_token_digest=str(raw.get("claim_token_digest") or ""),
                claimed_by_request_id=str(raw.get("claimed_by_request_id") or ""),
                claimed_at=str(raw.get("claimed_at") or ""),
                consumed_turn_id=str(raw.get("consumed_turn_id") or ""),
                consumed_at=str(raw.get("consumed_at") or ""),
                invalidation_reason=str(raw.get("invalidation_reason") or ""),
                revision=int(raw.get("revision") or 0),
                created_at=str(raw.get("created_at") or utc_now()),
                updated_at=str(raw.get("updated_at") or utc_now()),
            )
        except (TypeError, ValueError) as error:
            raise SkillCheckpointCorrupt("skill disclosure checkpoint is invalid") from error


@dataclass(frozen=True, slots=True)
class SkillDisclosureClaim:
    disclosure_id: str
    claim_token: str
    request_id: str
    envelope_revision: int
    claimed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "disclosure_id": self.disclosure_id,
            "claim_token": self.claim_token,
            "request_id": self.request_id,
            "envelope_revision": self.envelope_revision,
            "claimed_at": self.claimed_at,
        }


@dataclass(frozen=True, slots=True)
class SkillDisclosureRender:
    disclosure_id: str
    invocation_id: str
    version_ref: SkillVersionRef
    message: SkillMessageDelta
    attachments: tuple[SkillAttachment, ...]
    token_estimate: int
    render_digest: str
    claim: SkillDisclosureClaim

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        value = {
            "disclosure_id": self.disclosure_id,
            "invocation_id": self.invocation_id,
            "version_ref": self.version_ref.to_dict(),
            "attachments": [item.to_dict() for item in self.attachments],
            "token_estimate": self.token_estimate,
            "render_digest": self.render_digest,
            "claim": self.claim.to_dict(),
        }
        if include_content:
            value["message"] = self.message.to_dict()
        else:
            value["message_ref"] = f"skill-message://{self.invocation_id}/{self.disclosure_id}"
        return value


@dataclass(frozen=True, slots=True)
class SkillDisclosureSnapshot:
    schema_version: int
    session_id: str
    generation: int
    envelopes: tuple[SkillDisclosureEnvelope, ...]
    snapshot_digest: str
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self, *, include_arguments: bool = True) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "generation": self.generation,
            "envelopes": [
                item.to_dict(include_arguments=include_arguments) for item in self.envelopes
            ],
            "snapshot_digest": self.snapshot_digest,
            "updated_at": self.updated_at,
            "body_in_checkpoint": False,
        }


class SkillDisclosureLedger:
    """Session-scoped, one-shot body disclosure custody.

    This store is serialized inside the existing 02B/02D session aggregate.
    It is not a second global session store.  Bodies and resource contents are
    never serialized; exact content is rendered through the 03C loader only
    after a consumer atomically claims an envelope.
    """

    SCHEMA_VERSION = 1

    def __init__(self, *, session_id: str, snapshot: Mapping[str, Any] | None = None) -> None:
        self.session_id = str(session_id).strip()
        if not self.session_id:
            raise ValueError("SkillDisclosureLedger requires session_id")
        self._lock = RLock()
        self._generation = 0
        self._envelopes: dict[str, SkillDisclosureEnvelope] = {}
        self._by_invocation: dict[str, list[str]] = {}
        self._epochs: dict[str, int] = {}
        if snapshot:
            self.restore(snapshot)

    def append_plan(self, plan: SkillInvocationPlan) -> SkillDisclosureEnvelope:
        state = plan.state
        if state.session_id != self.session_id:
            raise SkillSessionOwnershipError(
                "skill plan belongs to a different disclosure session",
                detail={"expected": self.session_id, "actual": state.session_id},
            )
        kind = (
            SkillDisclosureKind.FORK_HANDOFF
            if plan.revision.metadata.invocation.mode is SkillInvocationMode.FORK
            else SkillDisclosureKind.INLINE_INITIAL
        )
        epoch = self._next_epoch(state.invocation_id)
        envelope = SkillDisclosureEnvelope(
            disclosure_id=digest_object(
                {
                    "invocation_id": state.invocation_id,
                    "epoch": epoch,
                    "kind": str(kind),
                    "version_ref": state.version_ref.immutable_ref,
                }
            )[:28],
            invocation_id=state.invocation_id,
            run_id=state.run_id,
            task_id=state.task_id,
            session_id=state.session_id,
            agent_id=state.agent_id,
            version_ref=state.version_ref,
            kind=kind,
            status=SkillDisclosureStatus.PENDING,
            parent_tool_use_id=plan.request.parent_tool_use_id,
            argument_digest=arguments_digest(plan.request.arguments),
            arguments=copy.deepcopy(plan.request.arguments),
            selected_resource_refs=tuple(item.descriptor.immutable_ref for item in plan.resources),
            attachment_refs=tuple(item.immutable_ref for item in plan.attachments),
            message_delta_refs=state.message_delta_refs,
            policy_snapshot_digest=plan.policy_snapshot.policy_digest,
            disclosure_epoch=epoch,
            registry_generation=state.version_ref.registry_generation,
        )
        with self._lock:
            if envelope.disclosure_id in self._envelopes:
                raise SkillSessionConflict(
                    "skill disclosure id collision",
                    detail={"disclosure_id": envelope.disclosure_id},
                )
            self._envelopes[envelope.disclosure_id] = envelope
            self._by_invocation.setdefault(envelope.invocation_id, []).append(envelope.disclosure_id)
            self._generation += 1
        return envelope

    def append_restore(
        self,
        reference: SkillCompactReference,
        *,
        run_id: str,
        task_id: str,
        parent_tool_use_id: str,
        policy_snapshot_digest: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> SkillDisclosureEnvelope:
        if reference.session_id != self.session_id:
            raise SkillSessionOwnershipError(
                "compact reference belongs to a different disclosure session",
                detail={"expected": self.session_id, "actual": reference.session_id},
            )
        if reference.status.terminal:
            raise SkillRestoreReferenceRejected(
                "terminal skill references cannot create body disclosure envelopes",
                detail={"status": str(reference.status)},
            )
        epoch = self._next_epoch(reference.invocation_id)
        values = dict(arguments or {})
        envelope = SkillDisclosureEnvelope(
            disclosure_id=digest_object(
                {
                    "invocation_id": reference.invocation_id,
                    "epoch": epoch,
                    "kind": str(SkillDisclosureKind.INLINE_RESTORE),
                    "version_ref": reference.version_ref.immutable_ref,
                }
            )[:28],
            invocation_id=reference.invocation_id,
            run_id=run_id,
            task_id=task_id,
            session_id=reference.session_id,
            agent_id=reference.agent_id,
            version_ref=reference.version_ref,
            kind=SkillDisclosureKind.INLINE_RESTORE,
            status=SkillDisclosureStatus.PENDING,
            parent_tool_use_id=parent_tool_use_id,
            argument_digest=arguments_digest(values),
            arguments=values,
            selected_resource_refs=(),
            attachment_refs=(),
            message_delta_refs=(),
            policy_snapshot_digest=policy_snapshot_digest,
            disclosure_epoch=epoch,
            registry_generation=reference.version_ref.registry_generation,
        )
        with self._lock:
            self._envelopes[envelope.disclosure_id] = envelope
            self._by_invocation.setdefault(envelope.invocation_id, []).append(envelope.disclosure_id)
            self._generation += 1
        return envelope

    def claim(
        self,
        disclosure_id: str,
        *,
        request_id: str,
        agent_id: str,
    ) -> SkillDisclosureClaim:
        request_id = str(request_id).strip()
        if not request_id:
            raise ValueError("skill disclosure claim requires request_id")
        with self._lock:
            current = self._require(disclosure_id)
            if current.agent_id != agent_id:
                raise SkillSessionOwnershipError(
                    "skill disclosure targets another agent",
                    detail={"expected": current.agent_id, "actual": agent_id},
                )
            if not current.can_disclose_body:
                raise SkillRestoreMessageRejected(
                    "fork and terminal disclosures cannot render body messages",
                    detail={"kind": str(current.kind)},
                )
            if current.status is SkillDisclosureStatus.CONSUMED:
                raise SkillSessionConflict(
                    "skill disclosure was already consumed",
                    detail={"consumed_turn_id": current.consumed_turn_id},
                )
            if current.status in {SkillDisclosureStatus.INVALIDATED, SkillDisclosureStatus.FAILED}:
                raise SkillSessionClosed(
                    "skill disclosure is no longer available",
                    detail={"status": str(current.status), "reason": current.invalidation_reason},
                )
            if current.status is SkillDisclosureStatus.CLAIMED:
                if current.claimed_by_request_id != request_id:
                    raise SkillCheckpointConflict(
                        "skill disclosure is claimed by another worker request",
                        detail={"claimed_by": current.claimed_by_request_id},
                    )
                claim_token = digest_object(
                    {
                        "disclosure_id": current.disclosure_id,
                        "request_id": request_id,
                        "revision": current.revision,
                        "claim_token_digest": current.claim_token_digest,
                    }
                )
                return SkillDisclosureClaim(
                    disclosure_id=current.disclosure_id,
                    claim_token=claim_token,
                    request_id=request_id,
                    envelope_revision=current.revision,
                    claimed_at=current.claimed_at,
                )
            claimed_at = utc_now()
            revision = current.revision + 1
            claim_token = digest_object(
                {
                    "disclosure_id": current.disclosure_id,
                    "request_id": request_id,
                    "revision": revision,
                    "claimed_at": claimed_at,
                    "version_ref": current.version_ref.immutable_ref,
                }
            )
            updated = replace(
                current,
                status=SkillDisclosureStatus.CLAIMED,
                claim_token_digest=digest_object({"claim_token": claim_token}),
                claimed_by_request_id=request_id,
                claimed_at=claimed_at,
                revision=revision,
                updated_at=claimed_at,
            )
            self._envelopes[current.disclosure_id] = updated
            self._generation += 1
            return SkillDisclosureClaim(
                disclosure_id=updated.disclosure_id,
                claim_token=claim_token,
                request_id=request_id,
                envelope_revision=updated.revision,
                claimed_at=updated.claimed_at,
            )

    def consume(
        self,
        claim: SkillDisclosureClaim,
        *,
        turn_id: str,
    ) -> SkillDisclosureEnvelope:
        turn_id = str(turn_id).strip()
        if not turn_id:
            raise ValueError("skill disclosure consumption requires turn_id")
        with self._lock:
            current = self._require(claim.disclosure_id)
            if current.status is SkillDisclosureStatus.CONSUMED:
                if current.consumed_turn_id == turn_id:
                    return current
                raise SkillSessionConflict(
                    "skill disclosure was consumed by another turn",
                    detail={"consumed_turn_id": current.consumed_turn_id},
                )
            if current.status is not SkillDisclosureStatus.CLAIMED:
                raise SkillCheckpointConflict(
                    "skill disclosure must be claimed before consumption",
                    detail={"status": str(current.status)},
                )
            if current.claimed_by_request_id != claim.request_id:
                raise SkillCheckpointConflict("skill disclosure claim request mismatch")
            if current.revision != claim.envelope_revision:
                raise SkillCheckpointConflict(
                    "skill disclosure claim revision is stale",
                    detail={"expected": current.revision, "actual": claim.envelope_revision},
                )
            expected_digest = digest_object({"claim_token": claim.claim_token})
            if expected_digest != current.claim_token_digest:
                raise SkillCheckpointConflict("skill disclosure claim token is invalid")
            now = utc_now()
            updated = replace(
                current,
                status=SkillDisclosureStatus.CONSUMED,
                consumed_turn_id=turn_id,
                consumed_at=now,
                revision=current.revision + 1,
                updated_at=now,
            )
            self._envelopes[current.disclosure_id] = updated
            self._generation += 1
            return updated

    def release(self, claim: SkillDisclosureClaim, *, reason: str) -> SkillDisclosureEnvelope:
        with self._lock:
            current = self._require(claim.disclosure_id)
            if current.status is not SkillDisclosureStatus.CLAIMED:
                return current
            if current.claimed_by_request_id != claim.request_id:
                raise SkillCheckpointConflict("cannot release another request's disclosure claim")
            now = utc_now()
            updated = replace(
                current,
                status=SkillDisclosureStatus.PENDING,
                claim_token_digest="",
                claimed_by_request_id="",
                claimed_at="",
                invalidation_reason=reason,
                revision=current.revision + 1,
                updated_at=now,
            )
            self._envelopes[current.disclosure_id] = updated
            self._generation += 1
            return updated

    def invalidate_invocation(self, invocation_id: str, *, reason: str) -> tuple[SkillDisclosureEnvelope, ...]:
        updated: list[SkillDisclosureEnvelope] = []
        with self._lock:
            for disclosure_id in self._by_invocation.get(invocation_id, ()):
                current = self._envelopes[disclosure_id]
                if current.status.terminal:
                    continue
                now = utc_now()
                value = replace(
                    current,
                    status=SkillDisclosureStatus.INVALIDATED,
                    invalidation_reason=reason,
                    claim_token_digest="",
                    revision=current.revision + 1,
                    updated_at=now,
                )
                self._envelopes[disclosure_id] = value
                updated.append(value)
            if updated:
                self._generation += 1
        return tuple(updated)

    def pending(self, *, agent_id: str | None = None) -> tuple[SkillDisclosureEnvelope, ...]:
        with self._lock:
            values = [
                item
                for item in self._envelopes.values()
                if item.status is SkillDisclosureStatus.PENDING
                and item.can_disclose_body
                and (agent_id is None or item.agent_id == agent_id)
            ]
        return tuple(sorted(values, key=lambda item: (item.disclosure_epoch, item.disclosure_id)))

    def get(self, disclosure_id: str) -> SkillDisclosureEnvelope:
        with self._lock:
            return self._require(disclosure_id)

    def for_invocation(self, invocation_id: str) -> tuple[SkillDisclosureEnvelope, ...]:
        with self._lock:
            return tuple(
                self._envelopes[item]
                for item in self._by_invocation.get(invocation_id, ())
            )

    def snapshot(self) -> SkillDisclosureSnapshot:
        with self._lock:
            values = tuple(sorted(self._envelopes.values(), key=lambda item: item.disclosure_id))
            payload = {
                "schema_version": self.SCHEMA_VERSION,
                "session_id": self.session_id,
                "generation": self._generation,
                "envelopes": [item.to_dict(include_arguments=True) for item in values],
            }
            return SkillDisclosureSnapshot(
                schema_version=self.SCHEMA_VERSION,
                session_id=self.session_id,
                generation=self._generation,
                envelopes=values,
                snapshot_digest=digest_object(payload),
            )

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        raw = dict(snapshot)
        if int(raw.get("schema_version") or 0) != self.SCHEMA_VERSION:
            raise SkillCheckpointCorrupt("unsupported skill disclosure checkpoint schema")
        if str(raw.get("session_id") or "") != self.session_id:
            raise SkillSessionOwnershipError("skill disclosure checkpoint session mismatch")
        items = raw.get("envelopes")
        if not isinstance(items, list):
            raise SkillCheckpointCorrupt("skill disclosure checkpoint envelopes must be an array")
        values = tuple(SkillDisclosureEnvelope.from_dict(item) for item in items if isinstance(item, Mapping))
        if len(values) != len(items):
            raise SkillCheckpointCorrupt("skill disclosure checkpoint contains a non-object envelope")
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "session_id": self.session_id,
            "generation": int(raw.get("generation") or 0),
            "envelopes": [item.to_dict(include_arguments=True) for item in sorted(values, key=lambda item: item.disclosure_id)],
        }
        expected = digest_object(payload)
        supplied = str(raw.get("snapshot_digest") or expected)
        if supplied != expected:
            raise SkillCheckpointCorrupt("skill disclosure checkpoint digest mismatch")
        envelopes: dict[str, SkillDisclosureEnvelope] = {}
        by_invocation: dict[str, list[str]] = {}
        epochs: dict[str, int] = {}
        for item in values:
            if item.disclosure_id in envelopes:
                raise SkillCheckpointCorrupt("duplicate skill disclosure id")
            envelopes[item.disclosure_id] = item
            by_invocation.setdefault(item.invocation_id, []).append(item.disclosure_id)
            epochs[item.invocation_id] = max(epochs.get(item.invocation_id, 0), item.disclosure_epoch)
        with self._lock:
            self._envelopes = envelopes
            self._by_invocation = by_invocation
            self._epochs = epochs
            self._generation = int(raw.get("generation") or 0)

    def _next_epoch(self, invocation_id: str) -> int:
        with self._lock:
            value = self._epochs.get(invocation_id, 0) + 1
            self._epochs[invocation_id] = value
            return value

    def _require(self, disclosure_id: str) -> SkillDisclosureEnvelope:
        value = self._envelopes.get(str(disclosure_id))
        if value is None:
            raise SkillSessionConflict(
                "skill disclosure envelope was not found",
                detail={"disclosure_id": disclosure_id},
            )
        return value


class SkillDisclosureRenderer:
    """Re-renders exact body/arguments/attachments after an atomic claim."""

    def __init__(
        self,
        *,
        runtime: Any,
        ledger: SkillDisclosureLedger,
        attachment_runtime: SkillAttachmentRuntime | None = None,
    ) -> None:
        self.runtime = runtime
        self.ledger = ledger
        self.attachment_runtime = attachment_runtime or runtime.attachment_runtime

    def render(
        self,
        disclosure_id: str,
        *,
        request_id: str,
        agent_id: str,
        total_token_budget: int,
    ) -> SkillDisclosureRender:
        claim = self.ledger.claim(
            disclosure_id,
            request_id=request_id,
            agent_id=agent_id,
        )
        envelope = self.ledger.get(disclosure_id)
        try:
            revision = self.runtime.registry.resolve(
                envelope.version_ref.qualified_name,
                requested_ref=envelope.version_ref,
            )
            if revision.metadata.invocation.mode is SkillInvocationMode.FORK:
                raise SkillRestoreMessageRejected("forked skill body cannot render into parent session")
            body = self.runtime.body_loader.load_body(revision)
            state = self.runtime.state_store.get(envelope.invocation_id)
            if state.status is SkillInvocationStatus.FORK_PENDING:
                raise SkillRestoreMessageRejected("fork-pending skill cannot render into parent session")
            if state.status.terminal and envelope.kind is not SkillDisclosureKind.INLINE_RESTORE:
                raise SkillRestoreMessageRejected(
                    "terminal skill has no pending initial body disclosure",
                    detail={"status": str(state.status)},
                )
            policy = state.policy_snapshot
            if policy is None:
                raise SkillCompactRestoreError("skill disclosure has no policy snapshot")
            if policy.policy_digest != envelope.policy_snapshot_digest:
                raise SkillRestoreReferenceRejected("skill disclosure policy digest diverged")
            attachments = self.attachment_runtime.invocation_attachments(
                state=state,
                body=body,
                policy_snapshot=policy,
                resource_refs=envelope.selected_resource_refs,
            )
            message = self.attachment_runtime.inline_message(
                body=body,
                attachments=attachments,
                parent_tool_use_id=envelope.parent_tool_use_id,
                base_directory=revision.skill_root,
                arguments=envelope.arguments,
            )
            tokens = estimate_tokens(message.content) + sum(
                max(1, estimate_tokens(str(item.payload))) for item in attachments
            )
            if tokens > total_token_budget:
                raise SkillBudgetExceeded(
                    "rendered skill disclosure exceeds the worker turn budget",
                    detail={"required": tokens, "budget": total_token_budget},
                )
            render_digest = digest_object(
                {
                    "disclosure": envelope.to_dict(include_arguments=True),
                    "message": message.to_dict(),
                    "attachments": [item.to_dict() for item in attachments],
                }
            )
            return SkillDisclosureRender(
                disclosure_id=envelope.disclosure_id,
                invocation_id=envelope.invocation_id,
                version_ref=envelope.version_ref,
                message=message,
                attachments=attachments,
                token_estimate=tokens,
                render_digest=render_digest,
                claim=claim,
            )
        except Exception:
            self.ledger.release(claim, reason="render_failed")
            raise

    def commit(self, rendered: SkillDisclosureRender, *, turn_id: str) -> SkillDisclosureEnvelope:
        return self.ledger.consume(rendered.claim, turn_id=turn_id)

    def abort(self, rendered: SkillDisclosureRender, *, reason: str) -> SkillDisclosureEnvelope:
        return self.ledger.release(rendered.claim, reason=reason)


def disclosure_ledger_from_context(
    context: Mapping[str, Any] | None,
    *,
    session_id: str,
) -> SkillDisclosureLedger:
    snapshot = None
    if isinstance(context, Mapping):
        raw = context.get("disclosure_ledger")
        if isinstance(raw, Mapping):
            snapshot = raw
    return SkillDisclosureLedger(session_id=session_id, snapshot=snapshot)


def compact_reference_disclosure_kind(raw: Mapping[str, Any]) -> SkillDisclosureKind:
    reference = compact_reference_from_dict(dict(raw))
    if reference.status.terminal:
        return SkillDisclosureKind.TERMINAL_OUTCOME
    return SkillDisclosureKind.INLINE_RESTORE
