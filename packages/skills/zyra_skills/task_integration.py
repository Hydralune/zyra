from __future__ import annotations

"""Task-checkpoint and CodeWorker handoff for the integrated skill runtime.

The worker tool projection owns invocations that originate inside the query
loop.  The task API owns durable task metadata and may also carry an inline
skill invoked before a worker turn.  This module is the explicit boundary
between those owners.  It deliberately stores references, leases, outcomes,
and runtime checkpoints, never Markdown bodies or permission authority.
"""

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

from .compact_bridge import SkillCompactReference, compact_reference_from_dict
from .digests import digest_object
from .integration_errors import (
    SkillCheckpointConflict,
    SkillCheckpointCorrupt,
    SkillEventCausalityError,
    SkillRestoreMessageRejected,
    SkillSessionOwnershipError,
)
from .models import SkillInvocationMode, SkillInvocationStatus, utc_now


def _parse_time(value: str) -> datetime:
    normalized = str(value or "").strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise SkillCheckpointCorrupt("skill task metadata contains invalid timestamp") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class SkillWorkerDisclosure:
    disclosure_id: str
    invocation_id: str
    session_id: str
    agent_id: str
    immutable_ref: str
    qualified_name: str
    source_kind: str
    trust_tier: str
    body: str
    body_digest: str
    token_estimate: int
    policy_digest: str
    worker_request_id: str
    lease_revision: int
    prepared_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.disclosure_id or not self.invocation_id or not self.worker_request_id:
            raise ValueError("worker skill disclosure identity is incomplete")
        if not self.session_id or not self.agent_id or not self.immutable_ref:
            raise ValueError("worker skill disclosure scope is incomplete")
        if not self.body_digest or self.token_estimate < 0 or self.lease_revision < 1:
            raise ValueError("worker skill disclosure body metadata is invalid")

    def reference_dict(self) -> dict[str, Any]:
        return {
            "disclosure_id": self.disclosure_id,
            "invocation_id": self.invocation_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "immutable_ref": self.immutable_ref,
            "qualified_name": self.qualified_name,
            "source_kind": self.source_kind,
            "trust_tier": self.trust_tier,
            "body_digest": self.body_digest,
            "token_estimate": self.token_estimate,
            "policy_digest": self.policy_digest,
            "worker_request_id": self.worker_request_id,
            "lease_revision": self.lease_revision,
            "prepared_at": self.prepared_at,
            "body_persisted": False,
            "permission_authority": False,
        }

    def to_message_projection(self) -> dict[str, Any]:
        return {
            "content": self.body,
            "summary": f"Invoked skill {self.qualified_name}",
            "message_budget_chars": max(2_000, len(self.body)),
            "metadata": {
                "source": "M1-03C.SkillTaskIntegrationRuntime",
                "skill_disclosure_id": self.disclosure_id,
                "skill_invocation_id": self.invocation_id,
                "skill_ref": self.immutable_ref,
                "skill_source_kind": self.source_kind,
                "skill_trust_tier": self.trust_tier,
                "skill_body_digest": self.body_digest,
                "skill_policy_digest": self.policy_digest,
                "skill_worker_request_id": self.worker_request_id,
                "permission_authority": False,
            },
        }


@dataclass(frozen=True, slots=True)
class SkillWorkerDisclosureBatch:
    batch_id: str
    worker_request_id: str
    run_id: str
    task_id: str
    disclosures: tuple[SkillWorkerDisclosure, ...]
    skipped: tuple[dict[str, Any], ...]
    metadata_revision: int
    total_tokens: int
    created_at: str = field(default_factory=utc_now)

    @property
    def empty(self) -> bool:
        return not self.disclosures

    def to_dict(self, *, include_bodies: bool = False) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for disclosure in self.disclosures:
            value = disclosure.reference_dict()
            if include_bodies:
                value["body"] = disclosure.body
            items.append(value)
        return {
            "schema": "zyra.skill-worker-disclosure-batch.v1",
            "batch_id": self.batch_id,
            "worker_request_id": self.worker_request_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "disclosures": items,
            "skipped": [copy.deepcopy(item) for item in self.skipped],
            "metadata_revision": self.metadata_revision,
            "total_tokens": self.total_tokens,
            "created_at": self.created_at,
            "body_persisted": False,
        }


@dataclass(frozen=True, slots=True)
class SkillWorkerEventProjection:
    event_id: str
    phase: str
    invocation_id: str
    session_id: str
    worker_request_id: str
    checkpoint_digest: str
    compact_reference_count: int
    outcome_projection_count: int
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "phase": self.phase,
            "invocation_id": self.invocation_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "checkpoint_digest": self.checkpoint_digest,
            "compact_reference_count": self.compact_reference_count,
            "outcome_projection_count": self.outcome_projection_count,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class SkillWorkerEventIngestReceipt:
    receipt_id: str
    run_id: str
    task_id: str
    worker_request_id: str
    event_count: int
    projections: tuple[SkillWorkerEventProjection, ...]
    runtime_state_digest: str
    compact_reference_count: int
    outcome_projection_count: int
    metadata_revision: int
    changed: bool
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.skill-worker-event-ingest.v1",
            "receipt_id": self.receipt_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_request_id": self.worker_request_id,
            "event_count": self.event_count,
            "projections": [item.to_dict() for item in self.projections],
            "runtime_state_digest": self.runtime_state_digest,
            "compact_reference_count": self.compact_reference_count,
            "outcome_projection_count": self.outcome_projection_count,
            "metadata_revision": self.metadata_revision,
            "changed": self.changed,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class SkillTaskMetadataSnapshot:
    revision: int
    runtime_state: dict[str, Any]
    compact_references: tuple[dict[str, Any], ...]
    outcome_projections: tuple[dict[str, Any], ...]
    consumed_disclosures: tuple[dict[str, Any], ...]
    pending_disclosures: tuple[dict[str, Any], ...]
    latest_worker_event: dict[str, Any]
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema": "zyra.skill-task-metadata.v1",
            "revision": self.revision,
            "runtime_state": copy.deepcopy(self.runtime_state),
            "compact_references": [copy.deepcopy(item) for item in self.compact_references],
            "outcome_projections": [copy.deepcopy(item) for item in self.outcome_projections],
            "consumed_disclosures": [copy.deepcopy(item) for item in self.consumed_disclosures],
            "pending_disclosures": [copy.deepcopy(item) for item in self.pending_disclosures],
            "latest_worker_event": copy.deepcopy(self.latest_worker_event),
            "updated_at": self.updated_at,
            "body_persisted": False,
            "permission_authority_persisted": False,
        }
        payload["snapshot_digest"] = digest_object(payload)
        return payload


class SkillTaskIntegrationRuntime:
    """Atomic API-task bridge for pre-turn disclosure and worker events."""

    def __init__(self, *, disclosure_lease_seconds: int = 900) -> None:
        if disclosure_lease_seconds < 30:
            raise ValueError("skill disclosure lease must be at least 30 seconds")
        self.disclosure_lease_seconds = disclosure_lease_seconds
        self._lock = RLock()

    def snapshot(self, metadata: Mapping[str, Any]) -> SkillTaskMetadataSnapshot:
        checkpoint = metadata.get("skill_runtime_state")
        context = metadata.get("skill_session_context")
        runtime_state = dict(checkpoint) if isinstance(checkpoint, Mapping) else {}
        context = dict(context) if isinstance(context, Mapping) else {}
        references = self._validated_references(context.get("invoked_skill_refs") or ())
        outcomes = self._validated_outcomes(context.get("worker_projection_outcomes") or ())
        consumed = self._mapping_values(context.get("disclosed_skill_refs"))
        pending = self._mapping_values(context.get("pending_skill_disclosures"))
        latest = context.get("latest_worker_skill_event")
        return SkillTaskMetadataSnapshot(
            revision=int(context.get("skill_metadata_revision") or 0),
            runtime_state=runtime_state,
            compact_references=references,
            outcome_projections=outcomes,
            consumed_disclosures=consumed,
            pending_disclosures=pending,
            latest_worker_event=dict(latest) if isinstance(latest, Mapping) else {},
            updated_at=str(context.get("skill_metadata_updated_at") or ""),
        )

    def worker_constraints(self, metadata: Mapping[str, Any]) -> dict[str, Any]:
        snapshot = self.snapshot(metadata)
        if not snapshot.runtime_state:
            return {}
        session_ids = {
            str(item.get("session_id") or "")
            for item in snapshot.compact_references
            if str(item.get("session_id") or "")
        }
        if len(session_ids) > 1:
            raise SkillSessionOwnershipError(
                "task checkpoint contains skill references from multiple sessions"
            )
        result = {
            "skill_runtime_state": copy.deepcopy(snapshot.runtime_state),
            "invoked_skill_refs": [copy.deepcopy(item) for item in snapshot.compact_references],
            "skill_metadata_revision": snapshot.revision,
        }
        if session_ids:
            result["skill_session_id"] = next(iter(session_ids))
        return result

    def prepare_disclosures(
        self,
        *,
        metadata: MutableMapping[str, Any],
        runtime: Any,
        run_id: str,
        task_id: str,
        worker_request_id: str,
        total_token_budget: int = 25_000,
        maximum: int = 8,
    ) -> SkillWorkerDisclosureBatch:
        if not run_id or not task_id or not worker_request_id:
            raise ValueError("worker disclosure preparation requires run/task/request identity")
        if total_token_budget <= 0 or maximum < 1:
            raise ValueError("worker disclosure budget and maximum must be positive")
        with self._lock:
            context = self._mutable_context(metadata)
            self._reap_stale_pending(context)
            existing = context["pending_skill_disclosures"].get(worker_request_id)
            if isinstance(existing, Mapping):
                return self._rebuild_batch(
                    existing,
                    runtime=runtime,
                    run_id=run_id,
                    task_id=task_id,
                    worker_request_id=worker_request_id,
                )
            references = self._validated_references(context.get("invoked_skill_refs") or ())
            consumed = context["disclosed_skill_refs"]
            disclosures: list[SkillWorkerDisclosure] = []
            skipped: list[dict[str, Any]] = []
            remaining = total_token_budget
            next_revision = int(context.get("skill_metadata_revision") or 0) + 1
            for raw in references:
                if len(disclosures) >= maximum or remaining <= 0:
                    break
                reference = compact_reference_from_dict(dict(raw))
                skip = self._skip_reason(reference, consumed)
                if skip:
                    skipped.append({"invocation_id": reference.invocation_id, "reason": skip})
                    continue
                restored = runtime.compact_bridge.restore(
                    (reference,),
                    session_id=reference.session_id,
                    agent_id=reference.agent_id,
                )
                if len(restored) != 1:
                    skipped.append({"invocation_id": reference.invocation_id, "reason": "not_restored"})
                    continue
                item = restored[0]
                revision = runtime.registry.resolve(
                    item.reference.version_ref.qualified_name,
                    requested_ref=item.reference.version_ref,
                )
                if revision.metadata.invocation.mode is SkillInvocationMode.FORK:
                    skipped.append({"invocation_id": reference.invocation_id, "reason": "fork_child_owned"})
                    continue
                if item.body.token_estimate > remaining:
                    skipped.append({"invocation_id": reference.invocation_id, "reason": "token_budget"})
                    continue
                disclosure_id = digest_object(
                    {
                        "invocation_id": reference.invocation_id,
                        "immutable_ref": reference.version_ref.immutable_ref,
                        "worker_request_id": worker_request_id,
                        "body_digest": item.body.version_ref.body_digest,
                        "lease_revision": next_revision,
                    }
                )[:40]
                disclosure = SkillWorkerDisclosure(
                    disclosure_id=disclosure_id,
                    invocation_id=reference.invocation_id,
                    session_id=reference.session_id,
                    agent_id=reference.agent_id,
                    immutable_ref=reference.version_ref.immutable_ref,
                    qualified_name=revision.qualified_name,
                    source_kind=str(revision.provenance.source_kind),
                    trust_tier=str(revision.provenance.trust_tier),
                    body=item.body.text,
                    body_digest=item.body.version_ref.body_digest,
                    token_estimate=item.body.token_estimate,
                    policy_digest=reference.policy_snapshot.policy_digest,
                    worker_request_id=worker_request_id,
                    lease_revision=next_revision,
                )
                disclosures.append(disclosure)
                remaining -= disclosure.token_estimate
            batch_id = digest_object(
                {
                    "worker_request_id": worker_request_id,
                    "run_id": run_id,
                    "task_id": task_id,
                    "disclosure_ids": [item.disclosure_id for item in disclosures],
                    "revision": next_revision,
                }
            )[:40]
            batch = SkillWorkerDisclosureBatch(
                batch_id=batch_id,
                worker_request_id=worker_request_id,
                run_id=run_id,
                task_id=task_id,
                disclosures=tuple(disclosures),
                skipped=tuple(skipped),
                metadata_revision=next_revision,
                total_tokens=sum(item.token_estimate for item in disclosures),
            )
            if batch.empty:
                return batch
            context["pending_skill_disclosures"][worker_request_id] = batch.to_dict()
            self._advance_revision(context, expected=int(context.get("skill_metadata_revision") or 0))
            return batch

    def commit_disclosures(
        self,
        *,
        metadata: MutableMapping[str, Any],
        batch: SkillWorkerDisclosureBatch,
        worker_event_ids: Sequence[str] = (),
    ) -> None:
        with self._lock:
            context = self._mutable_context(metadata)
            pending = context["pending_skill_disclosures"].get(batch.worker_request_id)
            if not isinstance(pending, Mapping) or str(pending.get("batch_id")) != batch.batch_id:
                raise SkillCheckpointConflict("skill disclosure lease is no longer current")
            consumed = context["disclosed_skill_refs"]
            now = utc_now()
            for item in batch.disclosures:
                consumed[item.invocation_id] = {
                    **item.reference_dict(),
                    "consumed_at": now,
                    "worker_event_ids": [str(value) for value in worker_event_ids if str(value)],
                }
            del context["pending_skill_disclosures"][batch.worker_request_id]
            self._advance_revision(context)

    def abort_disclosures(
        self,
        *,
        metadata: MutableMapping[str, Any],
        batch: SkillWorkerDisclosureBatch,
        reason: str,
    ) -> bool:
        with self._lock:
            context = self._mutable_context(metadata)
            pending = context["pending_skill_disclosures"].get(batch.worker_request_id)
            if not isinstance(pending, Mapping) or str(pending.get("batch_id")) != batch.batch_id:
                return False
            del context["pending_skill_disclosures"][batch.worker_request_id]
            history = context.setdefault("skill_disclosure_abort_history", [])
            if isinstance(history, list):
                history.append(
                    {
                        "batch_id": batch.batch_id,
                        "worker_request_id": batch.worker_request_id,
                        "reason": str(reason)[:512],
                        "aborted_at": utc_now(),
                    }
                )
                del history[:-64]
            self._advance_revision(context)
            return True

    def ingest_worker_events(
        self,
        *,
        metadata: MutableMapping[str, Any],
        events: Iterable[Any],
        run_id: str,
        task_id: str,
        worker_request_id: str,
    ) -> SkillWorkerEventIngestReceipt:
        selected: list[tuple[Any, Mapping[str, Any]]] = []
        for event in events:
            if str(getattr(event, "run_id", "")) != run_id or str(getattr(event, "task_id", "")) != task_id:
                continue
            payload = getattr(event, "payload", None)
            if isinstance(payload, Mapping) and isinstance(payload.get("skill_session_checkpoint"), Mapping):
                selected.append((event, payload))
        with self._lock:
            context = self._mutable_context(metadata)
            if not selected:
                return SkillWorkerEventIngestReceipt(
                    receipt_id=digest_object({"run_id": run_id, "task_id": task_id, "worker_request_id": worker_request_id, "empty": True})[:40],
                    run_id=run_id,
                    task_id=task_id,
                    worker_request_id=worker_request_id,
                    event_count=0,
                    projections=(),
                    runtime_state_digest=digest_object(metadata.get("skill_runtime_state") or {}),
                    compact_reference_count=len(context.get("invoked_skill_refs") or ()),
                    outcome_projection_count=len(context.get("worker_projection_outcomes") or ()),
                    metadata_revision=int(context.get("skill_metadata_revision") or 0),
                    changed=False,
                )
            projections: list[SkillWorkerEventProjection] = []
            prior_checkpoint = metadata.get("skill_runtime_state")
            latest_checkpoint: dict[str, Any] = {}
            latest_references: tuple[dict[str, Any], ...] = ()
            latest_outcomes: tuple[dict[str, Any], ...] = ()
            seen_event_ids: set[str] = set()
            for event, payload in selected:
                event_id = str(getattr(event, "event_id", ""))
                if not event_id or event_id in seen_event_ids:
                    raise SkillEventCausalityError("worker skill events require unique event ids")
                seen_event_ids.add(event_id)
                checkpoint = dict(payload["skill_session_checkpoint"])
                references = self._validated_references(payload.get("skill_compact_references") or ())
                outcomes = self._validated_outcomes(payload.get("skill_outcome_projections") or ())
                self._validate_checkpoint_scope(checkpoint, run_id=run_id, task_id=task_id)
                session_ids = {str(item.get("session_id") or "") for item in references if item.get("session_id")}
                if len(session_ids) > 1:
                    raise SkillSessionOwnershipError("worker emitted references from multiple skill sessions")
                phase = str(payload.get("phase") or "")
                invocation_id = str(payload.get("invocation_id") or "")
                projections.append(
                    SkillWorkerEventProjection(
                        event_id=event_id,
                        phase=phase,
                        invocation_id=invocation_id,
                        session_id=next(iter(session_ids), ""),
                        worker_request_id=str(payload.get("worker_request_id") or worker_request_id),
                        checkpoint_digest=digest_object(checkpoint),
                        compact_reference_count=len(references),
                        outcome_projection_count=len(outcomes),
                        created_at=str(getattr(event, "created_at", "") or utc_now()),
                    )
                )
                latest_checkpoint = checkpoint
                latest_references = references
                latest_outcomes = outcomes
            changed = latest_checkpoint != prior_checkpoint
            metadata["skill_runtime_state"] = latest_checkpoint
            context["invoked_skill_refs"] = [copy.deepcopy(item) for item in latest_references]
            context["worker_projection_outcomes"] = [copy.deepcopy(item) for item in latest_outcomes]
            context["latest_worker_skill_event"] = projections[-1].to_dict()
            context["latest_worker_skill_event_ids"] = [item.event_id for item in projections]
            self._advance_revision(context)
            receipt_id = digest_object(
                {
                    "run_id": run_id,
                    "task_id": task_id,
                    "worker_request_id": worker_request_id,
                    "event_ids": [item.event_id for item in projections],
                    "runtime_state_digest": digest_object(latest_checkpoint),
                }
            )[:40]
            return SkillWorkerEventIngestReceipt(
                receipt_id=receipt_id,
                run_id=run_id,
                task_id=task_id,
                worker_request_id=worker_request_id,
                event_count=len(projections),
                projections=tuple(projections),
                runtime_state_digest=digest_object(latest_checkpoint),
                compact_reference_count=len(latest_references),
                outcome_projection_count=len(latest_outcomes),
                metadata_revision=int(context["skill_metadata_revision"]),
                changed=changed,
            )

    def public_projection(self, metadata: Mapping[str, Any]) -> dict[str, Any]:
        snapshot = self.snapshot(metadata)
        return {
            "schema": "zyra.skill-task-public.v1",
            "revision": snapshot.revision,
            "runtime_state_digest": digest_object(snapshot.runtime_state),
            "active_reference_count": len(snapshot.compact_references),
            "outcome_projection_count": len(snapshot.outcome_projections),
            "consumed_disclosure_count": len(snapshot.consumed_disclosures),
            "pending_disclosure_count": len(snapshot.pending_disclosures),
            "latest_worker_event": copy.deepcopy(snapshot.latest_worker_event),
            "updated_at": snapshot.updated_at,
            "body_in_projection": False,
            "permission_authority_in_projection": False,
        }

    def _mutable_context(self, metadata: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
        value = metadata.setdefault("skill_session_context", {})
        if not isinstance(value, MutableMapping):
            raise SkillCheckpointCorrupt("skill_session_context must be an object")
        for key in ("disclosed_skill_refs", "pending_skill_disclosures"):
            item = value.setdefault(key, {})
            if not isinstance(item, MutableMapping):
                raise SkillCheckpointCorrupt(f"{key} must be an object")
        references = value.setdefault("invoked_skill_refs", [])
        outcomes = value.setdefault("worker_projection_outcomes", [])
        if not isinstance(references, list) or not isinstance(outcomes, list):
            raise SkillCheckpointCorrupt("skill references and outcomes must be arrays")
        return value

    def _skip_reason(self, reference: SkillCompactReference, consumed: Mapping[str, Any]) -> str:
        if reference.invocation_id in consumed:
            return "already_disclosed"
        if reference.status.terminal:
            return "terminal_outcome_only"
        if reference.status is SkillInvocationStatus.FORK_PENDING:
            return "fork_child_owned"
        if reference.status is not SkillInvocationStatus.INLINE_ACTIVE:
            return "not_inline_active"
        return ""

    def _rebuild_batch(
        self,
        value: Mapping[str, Any],
        *,
        runtime: Any,
        run_id: str,
        task_id: str,
        worker_request_id: str,
    ) -> SkillWorkerDisclosureBatch:
        if str(value.get("run_id") or "") != run_id or str(value.get("task_id") or "") != task_id:
            raise SkillCheckpointConflict("worker disclosure request id crossed task custody")
        requested = value.get("disclosures")
        if not isinstance(requested, list):
            raise SkillCheckpointCorrupt("pending skill disclosure batch is invalid")
        snapshot = self.snapshot({"skill_runtime_state": runtime.state_snapshot(), "skill_session_context": {"invoked_skill_refs": []}})
        del snapshot  # runtime state validation is intentional; bodies are reloaded below.
        disclosures: list[SkillWorkerDisclosure] = []
        for raw in requested:
            if not isinstance(raw, Mapping):
                raise SkillCheckpointCorrupt("pending skill disclosure entry is invalid")
            state = runtime.state_store.get(str(raw.get("invocation_id") or ""))
            reference = runtime.compact_bridge.references(
                (state,), session_id=state.session_id, agent_id=state.agent_id
            )
            if len(reference) != 1:
                raise SkillRestoreMessageRejected("pending skill disclosure is no longer inline-active")
            restored = runtime.compact_bridge.restore(
                reference, session_id=state.session_id, agent_id=state.agent_id
            )[0]
            revision = runtime.registry.resolve(
                state.version_ref.qualified_name, requested_ref=state.version_ref
            )
            rebuilt = SkillWorkerDisclosure(
                disclosure_id=str(raw.get("disclosure_id") or ""),
                invocation_id=state.invocation_id,
                session_id=state.session_id,
                agent_id=state.agent_id,
                immutable_ref=state.version_ref.immutable_ref,
                qualified_name=revision.qualified_name,
                source_kind=str(revision.provenance.source_kind),
                trust_tier=str(revision.provenance.trust_tier),
                body=restored.body.text,
                body_digest=restored.body.version_ref.body_digest,
                token_estimate=restored.body.token_estimate,
                policy_digest=state.policy_snapshot.policy_digest if state.policy_snapshot else "",
                worker_request_id=worker_request_id,
                lease_revision=int(raw.get("lease_revision") or value.get("metadata_revision") or 0),
                prepared_at=str(raw.get("prepared_at") or value.get("created_at") or utc_now()),
            )
            if rebuilt.body_digest != str(raw.get("body_digest") or ""):
                raise SkillCheckpointConflict("pending skill disclosure revision changed")
            disclosures.append(rebuilt)
        return SkillWorkerDisclosureBatch(
            batch_id=str(value.get("batch_id") or ""),
            worker_request_id=worker_request_id,
            run_id=run_id,
            task_id=task_id,
            disclosures=tuple(disclosures),
            skipped=tuple(dict(item) for item in value.get("skipped") or () if isinstance(item, Mapping)),
            metadata_revision=int(value.get("metadata_revision") or 0),
            total_tokens=sum(item.token_estimate for item in disclosures),
            created_at=str(value.get("created_at") or utc_now()),
        )

    def _reap_stale_pending(self, context: MutableMapping[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        pending = context["pending_skill_disclosures"]
        stale: list[str] = []
        for request_id, value in pending.items():
            if not isinstance(value, Mapping):
                stale.append(str(request_id))
                continue
            try:
                created = _parse_time(str(value.get("created_at") or ""))
            except SkillCheckpointCorrupt:
                stale.append(str(request_id))
                continue
            if (now - created).total_seconds() > self.disclosure_lease_seconds:
                stale.append(str(request_id))
        for request_id in stale:
            pending.pop(request_id, None)

    def _advance_revision(self, context: MutableMapping[str, Any], *, expected: int | None = None) -> None:
        current = int(context.get("skill_metadata_revision") or 0)
        if expected is not None and current != expected:
            raise SkillCheckpointConflict(
                "skill task metadata revision changed",
                detail={"expected": expected, "actual": current},
            )
        context["skill_metadata_revision"] = current + 1
        context["skill_metadata_updated_at"] = utc_now()

    def _validated_references(self, value: Iterable[Any]) -> tuple[dict[str, Any], ...]:
        if isinstance(value, (str, bytes, Mapping)):
            raise SkillCheckpointCorrupt("skill compact references must be an array")
        result: list[dict[str, Any]] = []
        invocation_ids: set[str] = set()
        for item in value:
            if not isinstance(item, Mapping):
                raise SkillCheckpointCorrupt("skill compact reference must be an object")
            reference = compact_reference_from_dict(dict(item))
            if reference.invocation_id in invocation_ids:
                raise SkillCheckpointCorrupt("skill compact references contain duplicate invocation ids")
            invocation_ids.add(reference.invocation_id)
            result.append(reference.to_dict())
        return tuple(result)

    def _validated_outcomes(self, value: Iterable[Any]) -> tuple[dict[str, Any], ...]:
        if isinstance(value, (str, bytes, Mapping)):
            raise SkillCheckpointCorrupt("skill outcome projections must be an array")
        result: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise SkillCheckpointCorrupt("skill outcome projection must be an object")
            raw = copy.deepcopy(dict(item))
            if not str(raw.get("invocation_id") or ""):
                raise SkillCheckpointCorrupt("skill outcome projection has no invocation id")
            for key in raw:
                if "body" in str(key).lower() or "prompt" in str(key).lower():
                    raise SkillCheckpointCorrupt("skill outcome projection contains body-like data")
            result.append(raw)
        return tuple(result)

    def _mapping_values(self, value: Any) -> tuple[dict[str, Any], ...]:
        if value is None:
            return ()
        if not isinstance(value, Mapping):
            raise SkillCheckpointCorrupt("skill disclosure ledger projection must be an object")
        return tuple(
            copy.deepcopy(dict(item))
            for _, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if isinstance(item, Mapping)
        )

    def _validate_checkpoint_scope(self, checkpoint: Mapping[str, Any], *, run_id: str, task_id: str) -> None:
        states = checkpoint.get("states")
        if states is None and isinstance(checkpoint.get("state_snapshot"), Mapping):
            states = checkpoint["state_snapshot"].get("states")
        if states is None:
            # Empty state snapshots are valid when the model listed skills but
            # did not invoke one.  Unknown non-empty shapes fail closed.
            if checkpoint:
                known = {"revision_lifecycle", "budget_state", "states", "state_snapshot"}
                if not set(checkpoint).issubset(known):
                    raise SkillCheckpointCorrupt("worker skill checkpoint has unknown shape")
            return
        if isinstance(states, Mapping):
            state_values = states.values()
        elif isinstance(states, list):
            state_values = states
        else:
            raise SkillCheckpointCorrupt("worker skill checkpoint states must be an object or array")
        for state in state_values:
            if not isinstance(state, Mapping):
                raise SkillCheckpointCorrupt("worker skill state must be an object")
            if str(state.get("task_id") or "") not in {"", task_id}:
                raise SkillSessionOwnershipError("worker skill state crossed task custody")
            state_run = str(state.get("run_id") or "")
            if state_run and state_run != run_id:
                raise SkillSessionOwnershipError("worker skill state crossed run custody")


__all__ = [
    "SkillTaskIntegrationRuntime",
    "SkillTaskMetadataSnapshot",
    "SkillWorkerDisclosure",
    "SkillWorkerDisclosureBatch",
    "SkillWorkerEventIngestReceipt",
    "SkillWorkerEventProjection",
]
