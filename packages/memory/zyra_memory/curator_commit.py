from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, Protocol

from zyra_core import EventRecord, EventType, now_iso

from .curator_evidence import EvidenceResolver
from .curator_models import (
    CandidateState,
    CommitDisposition,
    DecisionCode,
    DecisionStatus,
    MemoryCandidate,
    MemoryCommitReceipt,
    MemoryDecision,
    OutboxKind,
    OutboxMessage,
    OutboxState,
    canonical_json,
    stable_digest,
    stable_id,
)
from .curator_store import (
    CandidateConflictError,
    CuratorCandidateStore,
    CuratorLeaseLostError,
)
from .models import MemoryLayer, MemoryRecord


class MemoryEventSink(Protocol):
    def append_event(self, event: EventRecord) -> None: ...


class MemoryIndexSink(Protocol):
    def synchronize_task(
        self,
        task_id: str,
        *,
        process: bool = True,
        causation_id: str = "",
        force: bool = False,
        records: Sequence[MemoryRecord] | None = None,
    ) -> Any: ...


class CommitConflictError(RuntimeError):
    pass


class CommitInvariantError(RuntimeError):
    pass


class MemoryCommitRuntime:
    """Single deterministic author for curated canonical MemoryRecord writes."""

    def __init__(
        self,
        *,
        store: CuratorCandidateStore,
        canonical_store: Any,
        clock: Any | None = None,
    ) -> None:
        self.store = store
        self.canonical_store = canonical_store
        self.clock = clock or time.time
        self.evidence_resolver = EvidenceResolver(store)
        self.canonical_store.initialize()
        self.store.initialize()

    def commit(
        self,
        candidate: MemoryCandidate,
        decision: MemoryDecision,
    ) -> MemoryCommitReceipt:
        value = candidate.validated()
        verdict = decision.validated()
        if verdict.candidate_id != value.candidate_id:
            raise CommitInvariantError("decision does not belong to candidate")
        if verdict.candidate_digest != value.semantic_digest:
            raise CommitInvariantError("decision candidate digest is stale")
        if verdict.evidence_digest != value.evidence_digest:
            raise CommitInvariantError("decision evidence digest is stale")
        existing = self.store.receipt_for_candidate(value.candidate_id)
        if existing is not None:
            if existing.decision_id != verdict.decision_id:
                raise CommitConflictError("candidate already settled by another decision")
            return replace(existing, idempotent_replay=True)
        stored_candidate = self.store.require_candidate(value.candidate_id)
        if stored_candidate.semantic_digest != value.semantic_digest:
            raise CommitInvariantError("stored candidate changed after validation")
        stored_decision = self.store.decision(verdict.decision_id)
        if stored_decision is None:
            raise CommitInvariantError("decision must be durably recorded before commit")
        if stored_decision.to_dict() != verdict.to_dict():
            raise CommitInvariantError("stored decision differs from commit decision")
        bundle, _documents, evidence_issues = self.evidence_resolver.resolve_candidate(
            candidate_id=value.candidate_id
        )
        if bundle is None or evidence_issues:
            detail = ",".join(evidence_issues or ("bundle_missing",))
            raise CommitInvariantError(f"candidate evidence changed after validation: {detail}")
        if verdict.status not in {DecisionStatus.ACCEPT, DecisionStatus.SUPERSEDE}:
            return self._settle_without_write(value, verdict)
        return self._commit_memory(value, verdict)

    def commit_many(
        self,
        pairs: Sequence[tuple[MemoryCandidate, MemoryDecision]],
    ) -> tuple[MemoryCommitReceipt, ...]:
        receipts: list[MemoryCommitReceipt] = []
        for candidate, decision in pairs:
            receipts.append(self.commit(candidate, decision))
        return tuple(receipts)

    def _settle_without_write(
        self,
        candidate: MemoryCandidate,
        decision: MemoryDecision,
    ) -> MemoryCommitReceipt:
        if decision.status is DecisionStatus.NOOP:
            disposition = CommitDisposition.NOOP
        elif decision.status is DecisionStatus.MERGE_REQUIRED:
            disposition = CommitDisposition.MERGE_REQUIRED
        elif decision.status is DecisionStatus.SUPERSEDE:
            disposition = CommitDisposition.SUPERSEDED
        else:
            disposition = CommitDisposition.REJECTED
        reason = ",".join(issue.code.value for issue in decision.issues) or decision.status.value
        receipt = MemoryCommitReceipt(
            receipt_id=stable_id("memory_receipt", candidate.candidate_id, decision.decision_id),
            candidate_id=candidate.candidate_id,
            decision_id=decision.decision_id,
            disposition=disposition,
            memory_id=decision.target_memory_id,
            memory_revision=decision.target_revision or 0,
            outbox_message_ids=(),
            committed_at=now_iso(),
            reason=reason,
            metadata={"canonical_write": False, "decision_status": decision.status.value},
        )
        now = float(self.clock())
        with self.store.transaction(immediate=True) as connection:
            self._assert_unsettled(connection, candidate.candidate_id)
            self._insert_receipt(connection, receipt, stored_at=now)
            self.store._audit(
                connection,
                event_type="memory.candidate.settled_without_write",
                run_id=candidate.run_id,
                task_id=candidate.task_id,
                candidate_id=candidate.candidate_id,
                decision_id=decision.decision_id,
                memory_id=decision.target_memory_id,
                causation_id=decision.decision_id,
                payload={
                    "disposition": disposition.value,
                    "reason": reason,
                    "canonical_write": False,
                },
                created_at=now,
            )
        return receipt

    def _commit_memory(
        self,
        candidate: MemoryCandidate,
        decision: MemoryDecision,
    ) -> MemoryCommitReceipt:
        memory_id = decision.target_memory_id or candidate.expected_memory_id or stable_id(
            "memory",
            candidate.task_id,
            candidate.scope.value,
            candidate.proposed_layer,
            candidate.subject,
        )
        now = float(self.clock())
        committed_at = now_iso()
        with self.store.transaction(immediate=True) as connection:
            self._assert_unsettled(connection, candidate.candidate_id)
            self._assert_candidate_and_decision(connection, candidate, decision)
            existing_record = connection.execute(
                "SELECT * FROM memory_records WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            revision_row = connection.execute(
                "SELECT revision, content_digest FROM memory_record_revisions WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            current_revision = int(revision_row["revision"]) if revision_row is not None else 0
            expected_revision = candidate.expected_revision
            if existing_record is not None and not candidate.expected_memory_id:
                raise CommitConflictError(
                    "canonical memory already exists; explicit target id and revision are required"
                )
            if candidate.expected_memory_id and candidate.expected_memory_id != memory_id:
                raise CommitInvariantError("decision target differs from candidate expected memory")
            if expected_revision is not None and expected_revision != current_revision:
                raise CommitConflictError(
                    f"memory revision changed: expected {expected_revision}, actual {current_revision}"
                )
            if existing_record is None and current_revision != 0:
                raise CommitInvariantError("revision row exists without canonical memory")
            if existing_record is not None and revision_row is None:
                raise CommitInvariantError("canonical curated memory lacks revision row")
            next_revision = current_revision + 1
            record = self._memory_record(
                candidate=candidate,
                decision=decision,
                memory_id=memory_id,
                revision=next_revision,
                existing_record=existing_record,
                committed_at=committed_at,
            )
            self._write_memory_record(connection, record)
            content_digest = stable_digest(
                {
                    "summary": record.summary,
                    "content": record.content,
                    "keywords": record.keywords,
                    "artifact_ids": record.artifact_ids,
                    "evidence_ids": record.evidence_ids,
                    "metadata": record.metadata,
                }
            )
            connection.execute(
                """
                INSERT INTO memory_record_revisions(
                    memory_id, revision, candidate_id, decision_id, content_digest, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    revision = excluded.revision,
                    candidate_id = excluded.candidate_id,
                    decision_id = excluded.decision_id,
                    content_digest = excluded.content_digest,
                    updated_at = excluded.updated_at
                WHERE memory_record_revisions.revision = ?
                """,
                (
                    memory_id,
                    next_revision,
                    candidate.candidate_id,
                    decision.decision_id,
                    content_digest,
                    now,
                    current_revision,
                ),
            )
            if self._changes(connection) != 1:
                raise CommitConflictError("memory revision CAS failed")
            event_payload = self._event_payload(candidate, decision, record, next_revision)
            event_message_id = self.store.enqueue_outbox(
                connection,
                kind=OutboxKind.EVENT,
                aggregate_id=candidate.candidate_id,
                task_id=candidate.task_id,
                run_id=candidate.run_id,
                payload=event_payload,
            )
            index_payload = {
                "schema": "zyra.memory-index-admission.v1",
                "task_id": candidate.task_id,
                "run_id": candidate.run_id,
                "memory_id": memory_id,
                "memory_revision": next_revision,
                "candidate_id": candidate.candidate_id,
                "decision_id": decision.decision_id,
                "content_digest": content_digest,
                "canonical_owner": "SQLiteStore.memory_records",
                "derived_index_owner": "MemoryIndexRuntime",
            }
            index_message_id = self.store.enqueue_outbox(
                connection,
                kind=OutboxKind.INDEX_SYNC,
                aggregate_id=candidate.candidate_id,
                task_id=candidate.task_id,
                run_id=candidate.run_id,
                payload=index_payload,
            )
            receipt = MemoryCommitReceipt(
                receipt_id=stable_id("memory_receipt", candidate.candidate_id, decision.decision_id),
                candidate_id=candidate.candidate_id,
                decision_id=decision.decision_id,
                disposition=CommitDisposition.COMMITTED,
                memory_id=memory_id,
                memory_revision=next_revision,
                outbox_message_ids=(event_message_id, index_message_id),
                committed_at=committed_at,
                reason="validated_candidate_committed",
                metadata={
                    "canonical_write": True,
                    "content_digest": content_digest,
                    "scope": candidate.scope.value,
                    "layer": candidate.proposed_layer,
                },
            )
            self._insert_receipt(connection, receipt, stored_at=now)
            connection.execute(
                """
                UPDATE memory_candidates
                SET state = ?, updated_at_text = ?, metadata_json = ?
                WHERE candidate_id = ? AND state = ?
                """,
                (
                    CandidateState.COMMITTED.value,
                    committed_at,
                    canonical_json(
                        {
                            **dict(candidate.metadata),
                            "decision_id": decision.decision_id,
                            "memory_id": memory_id,
                            "memory_revision": next_revision,
                        }
                    ),
                    candidate.candidate_id,
                    CandidateState.ACCEPTED.value,
                ),
            )
            if self._changes(connection) != 1:
                raise CommitConflictError("candidate accepted state changed before commit")
            self.store._audit(
                connection,
                event_type="memory.committed",
                run_id=candidate.run_id,
                task_id=candidate.task_id,
                candidate_id=candidate.candidate_id,
                decision_id=decision.decision_id,
                memory_id=memory_id,
                causation_id=decision.decision_id,
                payload={
                    "memory_revision": next_revision,
                    "content_digest": content_digest,
                    "outbox_message_ids": [event_message_id, index_message_id],
                },
                created_at=now,
            )
        return receipt

    def _memory_record(
        self,
        *,
        candidate: MemoryCandidate,
        decision: MemoryDecision,
        memory_id: str,
        revision: int,
        existing_record: sqlite3.Row | None,
        committed_at: str,
    ) -> MemoryRecord:
        existing_created_at = (
            str(existing_record["created_at"])
            if existing_record is not None
            else committed_at
        )
        keywords = self._keywords(
            candidate.subject,
            decision.validated_summary,
            decision.validated_content,
        )
        evidence_ids = list(candidate.evidence_ids)
        artifact_ids = list(candidate.artifact_ids)
        if existing_record is not None:
            evidence_ids = list(
                dict.fromkeys(
                    [
                        *self._json_list(existing_record["evidence_ids_json"]),
                        *evidence_ids,
                    ]
                )
            )
            artifact_ids = list(
                dict.fromkeys(
                    [
                        *self._json_list(existing_record["artifact_ids_json"]),
                        *artifact_ids,
                    ]
                )
            )
        return MemoryRecord(
            memory_id=memory_id,
            run_id=candidate.run_id,
            task_id=candidate.task_id,
            layer=MemoryLayer(candidate.proposed_layer),
            source_type="memory_curator",
            source_id=candidate.candidate_id,
            summary=decision.validated_summary,
            content=dict(decision.validated_content),
            keywords=keywords,
            artifact_ids=artifact_ids,
            evidence_ids=evidence_ids,
            score=candidate.confidence,
            created_at=existing_created_at,
            updated_at=committed_at,
            metadata={
                "canonical_owner": "SQLiteStore.memory_records",
                "curator_candidate_id": candidate.candidate_id,
                "curator_candidate_digest": candidate.semantic_digest,
                "curator_decision_id": decision.decision_id,
                "curator_policy_digest": decision.policy_digest,
                "curator_evidence_digest": candidate.evidence_digest,
                "curator_subject": candidate.subject,
                "curator_scope": candidate.scope.value,
                "curator_kind": candidate.kind.value,
                "curator_revision": revision,
                "model_assisted_proposal": candidate.model_assisted,
                "validated_deterministically": True,
            },
        )

    @staticmethod
    def _write_memory_record(connection: sqlite3.Connection, record: MemoryRecord) -> None:
        connection.execute(
            """
            INSERT INTO memory_records(
                memory_id, run_id, task_id, layer, source_type, source_id, node_id,
                summary, content_json, keywords_json, artifact_ids_json,
                evidence_ids_json, score, created_at, updated_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                run_id = excluded.run_id,
                task_id = excluded.task_id,
                layer = excluded.layer,
                source_type = excluded.source_type,
                source_id = excluded.source_id,
                node_id = excluded.node_id,
                summary = excluded.summary,
                content_json = excluded.content_json,
                keywords_json = excluded.keywords_json,
                artifact_ids_json = excluded.artifact_ids_json,
                evidence_ids_json = excluded.evidence_ids_json,
                score = excluded.score,
                updated_at = excluded.updated_at,
                metadata_json = excluded.metadata_json
            """,
            (
                record.memory_id,
                record.run_id,
                record.task_id,
                record.layer.value,
                record.source_type,
                record.source_id,
                record.node_id,
                record.summary,
                canonical_json(record.content),
                canonical_json(record.keywords),
                canonical_json(record.artifact_ids),
                canonical_json(record.evidence_ids),
                record.score,
                record.created_at,
                record.updated_at,
                canonical_json(record.metadata),
            ),
        )

    @staticmethod
    def _event_payload(
        candidate: MemoryCandidate,
        decision: MemoryDecision,
        record: MemoryRecord,
        revision: int,
    ) -> Mapping[str, Any]:
        return {
            "schema": "zyra.memory-curator-event.v1",
            "event_type": "memory_curator_committed",
            "candidate_id": candidate.candidate_id,
            "decision_id": decision.decision_id,
            "memory_id": record.memory_id,
            "memory_revision": revision,
            "layer": record.layer.value,
            "scope": candidate.scope.value,
            "subject": candidate.subject,
            "evidence_digest": candidate.evidence_digest,
            "evidence_ids": list(candidate.evidence_ids),
            "artifact_ids": list(candidate.artifact_ids),
            "confidence": candidate.confidence,
            "model_assisted_proposal": candidate.model_assisted,
            "canonical_owner": "SQLiteStore.memory_records",
            "causation_id": decision.decision_id,
        }

    def _assert_unsettled(self, connection: sqlite3.Connection, candidate_id: str) -> None:
        row = connection.execute(
            "SELECT decision_id FROM memory_commit_receipts WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is not None:
            raise CommitConflictError("candidate was concurrently settled")

    @staticmethod
    def _assert_candidate_and_decision(
        connection: sqlite3.Connection,
        candidate: MemoryCandidate,
        decision: MemoryDecision,
    ) -> None:
        candidate_row = connection.execute(
            "SELECT semantic_digest, evidence_digest, state FROM memory_candidates WHERE candidate_id = ?",
            (candidate.candidate_id,),
        ).fetchone()
        if candidate_row is None:
            raise CommitInvariantError("candidate disappeared before commit")
        if str(candidate_row["semantic_digest"]) != candidate.semantic_digest:
            raise CommitInvariantError("candidate digest changed before commit")
        if str(candidate_row["evidence_digest"]) != candidate.evidence_digest:
            raise CommitInvariantError("candidate evidence changed before commit")
        if CandidateState(str(candidate_row["state"])) is not CandidateState.ACCEPTED:
            raise CommitConflictError("candidate is not in accepted state")
        decision_row = connection.execute(
            "SELECT candidate_digest, evidence_digest, policy_digest, status FROM memory_decisions WHERE decision_id = ?",
            (decision.decision_id,),
        ).fetchone()
        if decision_row is None:
            raise CommitInvariantError("decision disappeared before commit")
        if str(decision_row["candidate_digest"]) != decision.candidate_digest:
            raise CommitInvariantError("decision candidate digest changed")
        if str(decision_row["evidence_digest"]) != decision.evidence_digest:
            raise CommitInvariantError("decision evidence digest changed")
        if str(decision_row["policy_digest"]) != decision.policy_digest:
            raise CommitInvariantError("decision policy digest changed")
        if DecisionStatus(str(decision_row["status"])) not in {
            DecisionStatus.ACCEPT,
            DecisionStatus.SUPERSEDE,
        }:
            raise CommitConflictError("stored decision is not commit-authorizing")

    @staticmethod
    def _insert_receipt(
        connection: sqlite3.Connection,
        receipt: MemoryCommitReceipt,
        *,
        stored_at: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO memory_commit_receipts(
                receipt_id, candidate_id, decision_id, disposition, memory_id,
                memory_revision, outbox_message_ids_json, committed_at_text,
                reason, metadata_json, stored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt.receipt_id,
                receipt.candidate_id,
                receipt.decision_id,
                receipt.disposition.value,
                receipt.memory_id,
                receipt.memory_revision,
                canonical_json(receipt.outbox_message_ids),
                receipt.committed_at,
                receipt.reason,
                canonical_json(receipt.metadata),
                stored_at,
            ),
        )

    @staticmethod
    def _keywords(subject: str, summary: str, content: Mapping[str, Any]) -> list[str]:
        raw = f"{subject}\n{summary}\n{canonical_json(content)}"
        values: list[str] = []
        seen: set[str] = set()
        for token in __import__("re").findall(r"[A-Za-z0-9_./:-]{2,}", raw.casefold()):
            if len(token) < 3 or token in seen:
                continue
            seen.add(token)
            values.append(token)
            if len(values) >= 64:
                break
        return values

    @staticmethod
    def _json_list(value: object) -> list[str]:
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item) for item in parsed if str(item)]

    @staticmethod
    def _changes(connection: sqlite3.Connection) -> int:
        return int(connection.execute("SELECT changes() AS count").fetchone()["count"])


class CuratorOutboxDispatcher:
    """Replay commit side effects without replaying the canonical mutation."""

    def __init__(
        self,
        *,
        store: CuratorCandidateStore,
        event_sink: MemoryEventSink | None,
        index_sink: MemoryIndexSink | None,
        worker_id: str = "memory-curator-outbox",
        lease_seconds: float = 30.0,
        retry_delay_seconds: float = 1.0,
        max_attempts: int = 10,
    ) -> None:
        self.store = store
        self.event_sink = event_sink
        self.index_sink = index_sink
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.retry_delay_seconds = retry_delay_seconds
        self.max_attempts = max_attempts

    def drain(self, *, limit: int = 100) -> Mapping[str, Any]:
        self.store.sweep_expired_outbox(limit=limit)
        delivered: list[str] = []
        failed: list[str] = []
        deferred: list[str] = []
        for _ in range(max(0, int(limit))):
            message = self.store.claim_outbox(
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
            )
            if message is None:
                break
            try:
                self._deliver(message)
            except BaseException as error:
                settled = self.store.fail_outbox(
                    message,
                    error=f"{type(error).__name__}: {str(error)[:900]}",
                    retry_delay_seconds=self.retry_delay_seconds,
                    max_attempts=self.max_attempts,
                )
                failed.append(message.message_id)
                if settled.state is OutboxState.RETRY_WAIT:
                    deferred.append(message.message_id)
                continue
            self.store.deliver_outbox(message)
            delivered.append(message.message_id)
        pending = self.store.outbox_messages(
            states=(OutboxState.PENDING, OutboxState.RETRY_WAIT, OutboxState.DELIVERING),
            limit=100_000,
        )
        return {
            "delivered_message_ids": delivered,
            "failed_message_ids": failed,
            "deferred_message_ids": deferred,
            "pending_count": len(pending),
            "event_sink_configured": self.event_sink is not None,
            "index_sink_configured": self.index_sink is not None,
        }

    def _deliver(self, message: OutboxMessage) -> None:
        if message.kind is OutboxKind.EVENT:
            self._deliver_event(message)
            return
        if message.kind is OutboxKind.INDEX_SYNC:
            self._deliver_index(message)
            return
        raise RuntimeError(f"unsupported memory curator outbox kind: {message.kind.value}")

    def _deliver_event(self, message: OutboxMessage) -> None:
        if self.event_sink is None:
            raise RuntimeError("memory curator event sink is unavailable")
        event = EventRecord(
            event_id=message.message_id,
            run_id=message.run_id,
            task_id=message.task_id,
            event_type=EventType.MEMORY_CURATOR_COMMITTED,
            payload={
                **dict(message.payload),
                "outbox_message_id": message.message_id,
                "outbox_attempt": message.attempt,
            },
        )
        self.event_sink.append_event(event)

    def _deliver_index(self, message: OutboxMessage) -> None:
        if self.index_sink is None:
            raise RuntimeError("memory curator index sink is unavailable")
        result = self.index_sink.synchronize_task(
            message.task_id,
            process=True,
            causation_id=message.message_id,
        )
        outcome = getattr(result, "outcome", None)
        if outcome is not None and getattr(outcome, "status", "") not in {"ready", ""}:
            raise RuntimeError(f"memory index synchronization failed: {outcome.status}")


__all__ = [
    "CommitConflictError",
    "CommitInvariantError",
    "CuratorOutboxDispatcher",
    "MemoryCommitRuntime",
    "MemoryEventSink",
    "MemoryIndexSink",
]
