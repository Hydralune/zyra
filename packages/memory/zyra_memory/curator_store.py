from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .curator_models import (
    CandidateRelation,
    CandidateRelationKind,
    CandidateState,
    CuratorJob,
    CuratorJobLease,
    CuratorJobState,
    CuratorRunRequest,
    CuratorTrigger,
    EvidenceBundle,
    EvidenceDocument,
    MemoryCandidate,
    MemoryCommitReceipt,
    MemoryDecision,
    OutboxKind,
    OutboxMessage,
    OutboxState,
    canonical_json,
    mapping,
    stable_id,
)


ACTIVE_JOB_STATES = (
    CuratorJobState.CLAIMED,
    CuratorJobState.EXTRACTING,
    CuratorJobState.DECIDING,
    CuratorJobState.VALIDATING,
    CuratorJobState.COMMITTING,
)


class CuratorStoreError(RuntimeError):
    pass


class CuratorLeaseLostError(CuratorStoreError):
    pass


class CandidateConflictError(CuratorStoreError):
    pass


class CuratorCandidateStore:
    """Durable non-canonical state for evidence, candidates, jobs and outbox.

    The database path is the existing canonical SQLiteStore path.  Sharing one
    database enables a commit transaction to update canonical memory and create
    event/index outbox intents atomically.  The tables in this class remain
    non-canonical for task events, artifacts and retrieval documents.
    """

    def __init__(self, path: str | Path, *, clock: Any | None = None) -> None:
        self.path = Path(path)
        self.clock = clock or time.time
        self.initialize()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction(immediate=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS curator_evidence_documents (
                    evidence_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    trust TEXT NOT NULL,
                    sequence_number INTEGER NOT NULL,
                    created_at_text TEXT NOT NULL,
                    artifact_id TEXT NOT NULL DEFAULT '',
                    event_id TEXT NOT NULL DEFAULT '',
                    node_id TEXT NOT NULL DEFAULT '',
                    producer TEXT NOT NULL DEFAULT '',
                    media_type TEXT NOT NULL,
                    char_count INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    text_value TEXT NOT NULL,
                    normalized_json TEXT NOT NULL,
                    redacted INTEGER NOT NULL DEFAULT 0,
                    secret_fingerprints_json TEXT NOT NULL DEFAULT '[]',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    stored_at REAL NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_curator_evidence_source_revision
                    ON curator_evidence_documents(task_id, kind, source_id, source_revision);
                CREATE INDEX IF NOT EXISTS idx_curator_evidence_task_sequence
                    ON curator_evidence_documents(task_id, sequence_number, evidence_id);
                CREATE INDEX IF NOT EXISTS idx_curator_evidence_digest
                    ON curator_evidence_documents(content_digest);

                CREATE TABLE IF NOT EXISTS curator_evidence_bundles (
                    bundle_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    start_sequence INTEGER NOT NULL,
                    end_sequence INTEGER NOT NULL,
                    start_event_id TEXT NOT NULL DEFAULT '',
                    end_event_id TEXT NOT NULL DEFAULT '',
                    evidence_ids_json TEXT NOT NULL,
                    bundle_digest TEXT NOT NULL,
                    extractor_version TEXT NOT NULL,
                    created_at_text TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    stored_at REAL NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_curator_bundle_digest
                    ON curator_evidence_bundles(task_id, bundle_digest);
                CREATE INDEX IF NOT EXISTS idx_curator_bundle_task_range
                    ON curator_evidence_bundles(task_id, start_sequence, end_sequence);

                CREATE TABLE IF NOT EXISTS memory_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    proposed_layer TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    evidence_bundle_id TEXT NOT NULL,
                    evidence_digest TEXT NOT NULL,
                    evidence_range_json TEXT NOT NULL,
                    evidence_ids_json TEXT NOT NULL,
                    artifact_ids_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    ttl_seconds INTEGER,
                    extractor_version TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    semantic_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    expected_memory_id TEXT NOT NULL DEFAULT '',
                    expected_revision INTEGER,
                    model_assisted INTEGER NOT NULL DEFAULT 0,
                    proposer TEXT NOT NULL,
                    created_at_text TEXT NOT NULL,
                    updated_at_text TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    stored_at REAL NOT NULL,
                    FOREIGN KEY(evidence_bundle_id) REFERENCES curator_evidence_bundles(bundle_id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_candidates_idempotency
                    ON memory_candidates(idempotency_key);
                CREATE INDEX IF NOT EXISTS idx_memory_candidates_task_state
                    ON memory_candidates(task_id, state, created_at_text, candidate_id);
                CREATE INDEX IF NOT EXISTS idx_memory_candidates_subject
                    ON memory_candidates(task_id, scope, proposed_layer, subject);
                CREATE INDEX IF NOT EXISTS idx_memory_candidates_semantic
                    ON memory_candidates(task_id, semantic_digest);

                CREATE TABLE IF NOT EXISTS candidate_relations (
                    relation_id TEXT PRIMARY KEY,
                    source_candidate_id TEXT NOT NULL,
                    target_candidate_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    evidence_digest TEXT NOT NULL,
                    created_at_text TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(source_candidate_id) REFERENCES memory_candidates(candidate_id),
                    FOREIGN KEY(target_candidate_id) REFERENCES memory_candidates(candidate_id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_relation_unique
                    ON candidate_relations(source_candidate_id, target_candidate_id, kind);
                CREATE INDEX IF NOT EXISTS idx_candidate_relation_target
                    ON candidate_relations(target_candidate_id, kind);

                CREATE TABLE IF NOT EXISTS memory_decisions (
                    decision_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    candidate_digest TEXT NOT NULL,
                    evidence_digest TEXT NOT NULL,
                    policy_digest TEXT NOT NULL,
                    validated_summary TEXT NOT NULL,
                    validated_content_json TEXT NOT NULL,
                    issues_json TEXT NOT NULL,
                    target_memory_id TEXT NOT NULL DEFAULT '',
                    target_revision INTEGER,
                    relation_id TEXT NOT NULL DEFAULT '',
                    created_at_text TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    stored_at REAL NOT NULL,
                    FOREIGN KEY(candidate_id) REFERENCES memory_candidates(candidate_id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_decision_inputs
                    ON memory_decisions(candidate_id, candidate_digest, evidence_digest, policy_digest);
                CREATE INDEX IF NOT EXISTS idx_memory_decisions_candidate
                    ON memory_decisions(candidate_id, created_at_text);

                CREATE TABLE IF NOT EXISTS curator_jobs (
                    job_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    trigger_kind TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    state TEXT NOT NULL,
                    input_watermark INTEGER NOT NULL,
                    last_success_watermark INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT NOT NULL DEFAULT '',
                    ownership_token TEXT NOT NULL DEFAULT '',
                    lease_epoch INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at REAL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    retry_remaining INTEGER NOT NULL DEFAULT 3,
                    retry_at REAL,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    candidate_count INTEGER NOT NULL DEFAULT 0,
                    committed_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_curator_jobs_idempotency
                    ON curator_jobs(idempotency_key);
                CREATE INDEX IF NOT EXISTS idx_curator_jobs_claim
                    ON curator_jobs(state, retry_at, created_at, job_id);
                CREATE INDEX IF NOT EXISTS idx_curator_jobs_task_watermark
                    ON curator_jobs(task_id, input_watermark DESC, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_curator_jobs_lease
                    ON curator_jobs(lease_expires_at, state);

                CREATE TABLE IF NOT EXISTS curator_job_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    lease_epoch INTEGER NOT NULL,
                    worker_id TEXT NOT NULL,
                    ownership_token_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    finished_at REAL,
                    input_watermark INTEGER NOT NULL,
                    candidate_count INTEGER NOT NULL DEFAULT 0,
                    committed_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(job_id) REFERENCES curator_jobs(job_id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_curator_attempt_epoch
                    ON curator_job_attempts(job_id, lease_epoch);

                CREATE TABLE IF NOT EXISTS memory_record_revisions (
                    memory_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    candidate_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_commit_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL UNIQUE,
                    decision_id TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    memory_id TEXT NOT NULL DEFAULT '',
                    memory_revision INTEGER NOT NULL DEFAULT 0,
                    outbox_message_ids_json TEXT NOT NULL,
                    committed_at_text TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    stored_at REAL NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_receipt_decision
                    ON memory_commit_receipts(decision_id);

                CREATE TABLE IF NOT EXISTS memory_curator_outbox (
                    message_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL,
                    claimed_by TEXT NOT NULL DEFAULT '',
                    claim_token TEXT NOT NULL DEFAULT '',
                    claim_expires_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    delivered_at REAL,
                    error TEXT NOT NULL DEFAULT ''
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_outbox_aggregate_kind
                    ON memory_curator_outbox(aggregate_id, kind);
                CREATE INDEX IF NOT EXISTS idx_memory_outbox_delivery
                    ON memory_curator_outbox(state, available_at, created_at, message_id);

                CREATE TABLE IF NOT EXISTS memory_curator_audit (
                    audit_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    run_id TEXT NOT NULL DEFAULT '',
                    task_id TEXT NOT NULL DEFAULT '',
                    job_id TEXT NOT NULL DEFAULT '',
                    candidate_id TEXT NOT NULL DEFAULT '',
                    decision_id TEXT NOT NULL DEFAULT '',
                    memory_id TEXT NOT NULL DEFAULT '',
                    causation_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memory_curator_audit_task
                    ON memory_curator_audit(task_id, created_at, audit_id);
                """
            )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save_evidence_documents(self, documents: Sequence[EvidenceDocument]) -> tuple[str, ...]:
        values = tuple(document.validated() for document in documents)
        if not values:
            return ()
        now = float(self.clock())
        with self.transaction(immediate=True) as connection:
            for document in values:
                ref = document.ref
                existing = connection.execute(
                    "SELECT content_digest FROM curator_evidence_documents WHERE evidence_id = ?",
                    (ref.evidence_id,),
                ).fetchone()
                if existing is not None and str(existing["content_digest"]) != ref.content_digest:
                    raise CandidateConflictError("evidence identity was reused with different content")
                connection.execute(
                    """
                    INSERT INTO curator_evidence_documents(
                        evidence_id, run_id, task_id, kind, source_id, source_revision,
                        content_digest, trust, sequence_number, created_at_text,
                        artifact_id, event_id, node_id, producer, media_type, char_count,
                        title, text_value, normalized_json, redacted,
                        secret_fingerprints_json, metadata_json, stored_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(evidence_id) DO UPDATE SET
                        title = excluded.title,
                        text_value = excluded.text_value,
                        normalized_json = excluded.normalized_json,
                        redacted = excluded.redacted,
                        secret_fingerprints_json = excluded.secret_fingerprints_json,
                        metadata_json = excluded.metadata_json,
                        stored_at = excluded.stored_at
                    """,
                    (
                        ref.evidence_id,
                        ref.run_id,
                        ref.task_id,
                        ref.kind.value,
                        ref.source_id,
                        ref.source_revision,
                        ref.content_digest,
                        ref.trust.value,
                        ref.sequence,
                        ref.created_at,
                        ref.artifact_id,
                        ref.event_id,
                        ref.node_id,
                        ref.producer,
                        ref.media_type,
                        ref.char_count,
                        document.title,
                        document.text,
                        canonical_json(document.normalized),
                        int(document.redacted),
                        canonical_json(document.secret_fingerprints),
                        canonical_json(ref.metadata),
                        now,
                    ),
                )
            self._audit(
                connection,
                event_type="memory.curator.evidence_stored",
                run_id=values[0].ref.run_id,
                task_id=values[0].ref.task_id,
                payload={"evidence_ids": [item.ref.evidence_id for item in values]},
                created_at=now,
            )
        return tuple(document.ref.evidence_id for document in values)

    def evidence_document(self, evidence_id: str) -> EvidenceDocument | None:
        self.initialize()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM curator_evidence_documents WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
        return None if row is None else self._evidence_document(row)

    def evidence_documents(self, evidence_ids: Sequence[str]) -> tuple[EvidenceDocument, ...]:
        ids = tuple(dict.fromkeys(str(value) for value in evidence_ids if str(value)))
        if not ids:
            return ()
        placeholders = ",".join("?" for _ in ids)
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM curator_evidence_documents WHERE evidence_id IN ({placeholders})",
                ids,
            ).fetchall()
        by_id = {str(row["evidence_id"]): self._evidence_document(row) for row in rows}
        return tuple(by_id[value] for value in ids if value in by_id)

    def task_evidence(
        self,
        task_id: str,
        *,
        start_sequence: int = 0,
        end_sequence: int | None = None,
        limit: int = 10_000,
    ) -> tuple[EvidenceDocument, ...]:
        clauses = ["task_id = ?", "sequence_number >= ?"]
        parameters: list[Any] = [task_id, max(0, int(start_sequence))]
        if end_sequence is not None:
            clauses.append("sequence_number <= ?")
            parameters.append(int(end_sequence))
        parameters.append(max(0, min(int(limit), 100_000)))
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM curator_evidence_documents
                WHERE {' AND '.join(clauses)}
                ORDER BY sequence_number ASC, evidence_id ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(self._evidence_document(row) for row in rows)

    def save_bundle(self, bundle: EvidenceBundle) -> EvidenceBundle:
        value = bundle.validated()
        now = float(self.clock())
        with self.transaction(immediate=True) as connection:
            rows = connection.execute(
                f"SELECT evidence_id FROM curator_evidence_documents WHERE evidence_id IN ({','.join('?' for _ in value.evidence_refs)})",
                tuple(item.evidence_id for item in value.evidence_refs),
            ).fetchall()
            stored = {str(row["evidence_id"]) for row in rows}
            required = {item.evidence_id for item in value.evidence_refs}
            if stored != required:
                raise CandidateConflictError("bundle references evidence that is not stored")
            existing = connection.execute(
                "SELECT bundle_digest FROM curator_evidence_bundles WHERE bundle_id = ?",
                (value.bundle_id,),
            ).fetchone()
            if existing is not None and str(existing["bundle_digest"]) != value.bundle_digest:
                raise CandidateConflictError("bundle identity was reused with different content")
            connection.execute(
                """
                INSERT INTO curator_evidence_bundles(
                    bundle_id, run_id, task_id, start_sequence, end_sequence,
                    start_event_id, end_event_id, evidence_ids_json, bundle_digest,
                    extractor_version, created_at_text, metadata_json, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bundle_id) DO UPDATE SET
                    metadata_json = excluded.metadata_json,
                    stored_at = excluded.stored_at
                """,
                (
                    value.bundle_id,
                    value.run_id,
                    value.task_id,
                    value.evidence_range.start_sequence,
                    value.evidence_range.end_sequence,
                    value.evidence_range.start_event_id,
                    value.evidence_range.end_event_id,
                    canonical_json([item.evidence_id for item in value.evidence_refs]),
                    value.bundle_digest,
                    value.extractor_version,
                    value.created_at,
                    canonical_json(value.metadata),
                    now,
                ),
            )
            self._audit(
                connection,
                event_type="memory.curator.bundle_stored",
                run_id=value.run_id,
                task_id=value.task_id,
                causation_id=value.bundle_id,
                payload={
                    "bundle_id": value.bundle_id,
                    "bundle_digest": value.bundle_digest,
                    "evidence_count": len(value.evidence_refs),
                },
                created_at=now,
            )
        return value

    def bundle(self, bundle_id: str) -> EvidenceBundle | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM curator_evidence_bundles WHERE bundle_id = ?",
                (bundle_id,),
            ).fetchone()
            if row is None:
                return None
            evidence_ids = self._json_list(row["evidence_ids_json"])
            placeholders = ",".join("?" for _ in evidence_ids)
            evidence_rows = connection.execute(
                f"SELECT * FROM curator_evidence_documents WHERE evidence_id IN ({placeholders})",
                evidence_ids,
            ).fetchall()
        refs_by_id = {
            str(item["evidence_id"]): self._evidence_document(item).ref
            for item in evidence_rows
        }
        value = {
            "bundle_id": row["bundle_id"],
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "evidence_range": {
                "start_sequence": row["start_sequence"],
                "end_sequence": row["end_sequence"],
                "start_event_id": row["start_event_id"],
                "end_event_id": row["end_event_id"],
            },
            "evidence_refs": [
                refs_by_id[evidence_id].to_dict()
                for evidence_id in evidence_ids
                if evidence_id in refs_by_id
            ],
            "bundle_digest": row["bundle_digest"],
            "extractor_version": row["extractor_version"],
            "created_at": row["created_at_text"],
            "metadata": self._json_mapping(row["metadata_json"]),
        }
        return EvidenceBundle.from_dict(value)

    def save_candidate(self, candidate: MemoryCandidate) -> tuple[MemoryCandidate, bool]:
        value = candidate.validated()
        now = float(self.clock())
        with self.transaction(immediate=True) as connection:
            bundle = connection.execute(
                "SELECT bundle_digest FROM curator_evidence_bundles WHERE bundle_id = ?",
                (value.evidence_bundle_id,),
            ).fetchone()
            if bundle is None:
                raise CandidateConflictError("candidate bundle is not stored")
            if str(bundle["bundle_digest"]) != value.evidence_digest:
                raise CandidateConflictError("candidate bundle digest mismatch")
            existing = connection.execute(
                "SELECT * FROM memory_candidates WHERE idempotency_key = ?",
                (value.idempotency_key,),
            ).fetchone()
            if existing is not None:
                stored = self._candidate(existing)
                if stored.semantic_digest != value.semantic_digest:
                    raise CandidateConflictError(
                        "candidate idempotency key was reused with different semantics"
                    )
                return stored, False
            connection.execute(
                """
                INSERT INTO memory_candidates(
                    candidate_id, run_id, task_id, kind, proposed_layer, scope,
                    subject, summary, content_json, evidence_bundle_id,
                    evidence_digest, evidence_range_json, evidence_ids_json,
                    artifact_ids_json, confidence, ttl_seconds, extractor_version,
                    idempotency_key, semantic_digest, state, expected_memory_id,
                    expected_revision, model_assisted, proposer, created_at_text,
                    updated_at_text, metadata_json, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._candidate_values(value, now),
            )
            self._audit(
                connection,
                event_type="memory.candidate.proposed",
                run_id=value.run_id,
                task_id=value.task_id,
                candidate_id=value.candidate_id,
                causation_id=value.evidence_bundle_id,
                payload={
                    "kind": value.kind.value,
                    "scope": value.scope.value,
                    "layer": value.proposed_layer,
                    "semantic_digest": value.semantic_digest,
                },
                created_at=now,
            )
        return value, True

    def candidate(self, candidate_id: str) -> MemoryCandidate | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM memory_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return None if row is None else self._candidate(row)

    def require_candidate(self, candidate_id: str) -> MemoryCandidate:
        value = self.candidate(candidate_id)
        if value is None:
            raise KeyError(f"memory candidate not found: {candidate_id}")
        return value

    def candidates(
        self,
        *,
        task_id: str = "",
        states: Sequence[CandidateState] = (),
        subject: str = "",
        limit: int = 1000,
    ) -> tuple[MemoryCandidate, ...]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if states:
            clauses.append(f"state IN ({','.join('?' for _ in states)})")
            parameters.extend(item.value for item in states)
        if subject:
            clauses.append("subject = ?")
            parameters.append(subject)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(0, min(int(limit), 100_000)))
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM memory_candidates
                {where}
                ORDER BY created_at_text ASC, candidate_id ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(self._candidate(row) for row in rows)

    def update_candidate_state(
        self,
        candidate_id: str,
        *,
        expected: Sequence[CandidateState],
        target: CandidateState,
        metadata: Mapping[str, Any] | None = None,
    ) -> MemoryCandidate:
        if not expected:
            raise ValueError("expected candidate state set cannot be empty")
        now_text = self._timestamp_text()
        now = float(self.clock())
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM memory_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise KeyError(candidate_id)
            current = CandidateState(str(row["state"]))
            if current is target:
                return self._candidate(row)
            if current not in expected:
                raise CandidateConflictError(
                    f"candidate state {current.value} cannot transition to {target.value}"
                )
            merged_metadata = self._json_mapping(row["metadata_json"])
            merged_metadata.update(dict(metadata or {}))
            connection.execute(
                """
                UPDATE memory_candidates
                SET state = ?, updated_at_text = ?, metadata_json = ?, stored_at = ?
                WHERE candidate_id = ? AND state = ?
                """,
                (
                    target.value,
                    now_text,
                    canonical_json(merged_metadata),
                    now,
                    candidate_id,
                    current.value,
                ),
            )
            if self._changes(connection) != 1:
                raise CandidateConflictError("candidate state changed concurrently")
            updated = connection.execute(
                "SELECT * FROM memory_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return self._candidate(updated)

    def save_relation(self, relation: CandidateRelation) -> CandidateRelation:
        value = relation.validated()
        with self.transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT candidate_id FROM memory_candidates WHERE candidate_id IN (?, ?)",
                (value.source_candidate_id, value.target_candidate_id),
            ).fetchall()
            if {str(row["candidate_id"]) for row in rows} != {
                value.source_candidate_id,
                value.target_candidate_id,
            }:
                raise CandidateConflictError("candidate relation endpoint is missing")
            connection.execute(
                """
                INSERT INTO candidate_relations(
                    relation_id, source_candidate_id, target_candidate_id, kind,
                    reason, evidence_digest, created_at_text, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_candidate_id, target_candidate_id, kind) DO UPDATE SET
                    reason = excluded.reason,
                    evidence_digest = excluded.evidence_digest,
                    metadata_json = excluded.metadata_json
                """,
                (
                    value.relation_id,
                    value.source_candidate_id,
                    value.target_candidate_id,
                    value.kind.value,
                    value.reason,
                    value.evidence_digest,
                    value.created_at,
                    canonical_json(value.metadata),
                ),
            )
        return value

    def relations(
        self,
        candidate_id: str,
        *,
        incoming: bool = True,
        outgoing: bool = True,
    ) -> tuple[CandidateRelation, ...]:
        if not incoming and not outgoing:
            return ()
        if incoming and outgoing:
            condition = "source_candidate_id = ? OR target_candidate_id = ?"
            parameters = (candidate_id, candidate_id)
        elif incoming:
            condition = "target_candidate_id = ?"
            parameters = (candidate_id,)
        else:
            condition = "source_candidate_id = ?"
            parameters = (candidate_id,)
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM candidate_relations WHERE {condition} ORDER BY created_at_text, relation_id",
                parameters,
            ).fetchall()
        return tuple(self._relation(row) for row in rows)

    def save_decision(self, decision: MemoryDecision) -> tuple[MemoryDecision, bool]:
        value = decision.validated()
        now = float(self.clock())
        with self.transaction(immediate=True) as connection:
            candidate = connection.execute(
                "SELECT semantic_digest, evidence_digest FROM memory_candidates WHERE candidate_id = ?",
                (value.candidate_id,),
            ).fetchone()
            if candidate is None:
                raise CandidateConflictError("decision candidate is not stored")
            if str(candidate["semantic_digest"]) != value.candidate_digest:
                raise CandidateConflictError("decision candidate digest mismatch")
            if str(candidate["evidence_digest"]) != value.evidence_digest:
                raise CandidateConflictError("decision evidence digest mismatch")
            existing = connection.execute(
                """
                SELECT * FROM memory_decisions
                WHERE candidate_id = ? AND candidate_digest = ?
                    AND evidence_digest = ? AND policy_digest = ?
                """,
                (
                    value.candidate_id,
                    value.candidate_digest,
                    value.evidence_digest,
                    value.policy_digest,
                ),
            ).fetchone()
            if existing is not None:
                stored = self._decision(existing)
                if stored.to_dict() != value.to_dict():
                    raise CandidateConflictError("deterministic decision inputs changed output")
                return stored, False
            connection.execute(
                """
                INSERT INTO memory_decisions(
                    decision_id, candidate_id, status, candidate_digest,
                    evidence_digest, policy_digest, validated_summary,
                    validated_content_json, issues_json, target_memory_id,
                    target_revision, relation_id, created_at_text, metadata_json, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    value.decision_id,
                    value.candidate_id,
                    value.status.value,
                    value.candidate_digest,
                    value.evidence_digest,
                    value.policy_digest,
                    value.validated_summary,
                    canonical_json(value.validated_content),
                    canonical_json([issue.to_dict() for issue in value.issues]),
                    value.target_memory_id,
                    value.target_revision,
                    value.relation_id,
                    value.created_at,
                    canonical_json(value.metadata),
                    now,
                ),
            )
            self._audit(
                connection,
                event_type="memory.candidate.validated",
                candidate_id=value.candidate_id,
                decision_id=value.decision_id,
                causation_id=value.candidate_id,
                payload={
                    "status": value.status.value,
                    "issue_codes": [issue.code.value for issue in value.issues],
                },
                created_at=now,
            )
        return value, True

    def decision(self, decision_id: str) -> MemoryDecision | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM memory_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        return None if row is None else self._decision(row)

    def candidate_decisions(self, candidate_id: str) -> tuple[MemoryDecision, ...]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM memory_decisions
                WHERE candidate_id = ? ORDER BY created_at_text ASC, decision_id ASC
                """,
                (candidate_id,),
            ).fetchall()
        return tuple(self._decision(row) for row in rows)

    def enqueue_job(
        self,
        request: CuratorRunRequest,
        *,
        retry_limit: int = 3,
    ) -> tuple[CuratorJob, bool]:
        value = request.validated()
        if retry_limit < 0 or retry_limit > 100:
            raise ValueError("retry_limit is out of range")
        now = float(self.clock())
        job_id = stable_id("curator_job", value.request_id, value.idempotency_key)
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM curator_jobs WHERE idempotency_key = ?",
                (value.idempotency_key,),
            ).fetchone()
            if existing is not None:
                stored = self._job(existing)
                stored_request = stored.request.to_dict()
                incoming_request = value.to_dict()
                stored_request.pop("created_at", None)
                incoming_request.pop("created_at", None)
                if stored_request != incoming_request:
                    raise CandidateConflictError("job idempotency key was reused with another request")
                return stored, False
            prior = connection.execute(
                "SELECT MAX(last_success_watermark) AS watermark FROM curator_jobs WHERE task_id = ?",
                (value.task_id,),
            ).fetchone()
            last_success = int(prior["watermark"] or 0)
            connection.execute(
                """
                INSERT INTO curator_jobs(
                    job_id, request_id, run_id, task_id, trigger_kind,
                    requested_by, request_json, idempotency_key, state,
                    input_watermark, last_success_watermark, retry_remaining,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    value.request_id,
                    value.run_id,
                    value.task_id,
                    value.trigger.value,
                    value.requested_by,
                    canonical_json(value.to_dict()),
                    value.idempotency_key,
                    CuratorJobState.QUEUED.value,
                    value.input_watermark,
                    last_success,
                    retry_limit,
                    now,
                    now,
                ),
            )
            self._audit(
                connection,
                event_type="memory.curator.queued",
                run_id=value.run_id,
                task_id=value.task_id,
                job_id=job_id,
                causation_id=value.request_id,
                payload={
                    "trigger": value.trigger.value,
                    "input_watermark": value.input_watermark,
                    "last_success_watermark": last_success,
                },
                created_at=now,
            )
            row = connection.execute(
                "SELECT * FROM curator_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return self._job(row), True

    def job(self, job_id: str) -> CuratorJob | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM curator_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return None if row is None else self._job(row)

    def jobs(
        self,
        *,
        task_id: str = "",
        states: Sequence[CuratorJobState] = (),
        limit: int = 100,
    ) -> tuple[CuratorJob, ...]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if states:
            clauses.append(f"state IN ({','.join('?' for _ in states)})")
            parameters.extend(item.value for item in states)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(0, min(int(limit), 10_000)))
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM curator_jobs {where} ORDER BY created_at DESC, job_id DESC LIMIT ?",
                parameters,
            ).fetchall()
        return tuple(self._job(row) for row in rows)

    def claim_job(
        self,
        *,
        worker_id: str,
        lease_seconds: float,
        task_id: str = "",
        job_id: str = "",
    ) -> tuple[CuratorJob, CuratorJobLease] | None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = float(self.clock())
        with self.transaction(immediate=True) as connection:
            clauses = ["state IN (?, ?)", "(retry_at IS NULL OR retry_at <= ?)", "retry_remaining >= 0"]
            parameters: list[Any] = [
                CuratorJobState.QUEUED.value,
                CuratorJobState.RETRY_WAIT.value,
                now,
            ]
            if task_id:
                clauses.append("task_id = ?")
                parameters.append(task_id)
            if job_id:
                clauses.append("job_id = ?")
                parameters.append(job_id)
            row = connection.execute(
                f"""
                SELECT * FROM curator_jobs
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at ASC, job_id ASC LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is None:
                return None
            if int(row["input_watermark"]) <= int(row["last_success_watermark"]):
                connection.execute(
                    """
                    UPDATE curator_jobs
                    SET state = ?, finished_at = ?, updated_at = ?,
                        error_code = '', error_message = ''
                    WHERE job_id = ? AND state IN (?, ?)
                    """,
                    (
                        CuratorJobState.SUCCEEDED.value,
                        now,
                        now,
                        row["job_id"],
                        CuratorJobState.QUEUED.value,
                        CuratorJobState.RETRY_WAIT.value,
                    ),
                )
                return None
            token = secrets.token_urlsafe(32)
            epoch = int(row["lease_epoch"]) + 1
            attempt = int(row["attempt"]) + 1
            expires = now + float(lease_seconds)
            connection.execute(
                """
                UPDATE curator_jobs
                SET state = ?, lease_owner = ?, ownership_token = ?,
                    lease_epoch = ?, lease_expires_at = ?, attempt = ?,
                    started_at = COALESCE(started_at, ?), finished_at = NULL,
                    retry_at = NULL, error_code = '', error_message = '', updated_at = ?
                WHERE job_id = ? AND state = ? AND lease_epoch = ?
                """,
                (
                    CuratorJobState.CLAIMED.value,
                    worker_id,
                    token,
                    epoch,
                    expires,
                    attempt,
                    now,
                    now,
                    row["job_id"],
                    row["state"],
                    row["lease_epoch"],
                ),
            )
            if self._changes(connection) != 1:
                return None
            attempt_id = stable_id("curator_attempt", row["job_id"], epoch, worker_id)
            connection.execute(
                """
                INSERT INTO curator_job_attempts(
                    attempt_id, job_id, lease_epoch, worker_id,
                    ownership_token_digest, state, started_at, input_watermark
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    row["job_id"],
                    epoch,
                    worker_id,
                    stable_id("token", token),
                    CuratorJobState.CLAIMED.value,
                    now,
                    row["input_watermark"],
                ),
            )
            self._audit(
                connection,
                event_type="memory.curator.claimed",
                run_id=str(row["run_id"]),
                task_id=str(row["task_id"]),
                job_id=str(row["job_id"]),
                causation_id=str(row["request_id"]),
                payload={
                    "worker_id": worker_id,
                    "lease_epoch": epoch,
                    "attempt": attempt,
                    "input_watermark": int(row["input_watermark"]),
                },
                created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM curator_jobs WHERE job_id = ?",
                (row["job_id"],),
            ).fetchone()
        job = self._job(updated)
        lease = CuratorJobLease(
            job_id=job.job_id,
            task_id=job.request.task_id,
            worker_id=worker_id,
            ownership_token=token,
            lease_epoch=epoch,
            input_watermark=job.input_watermark,
            expires_at=expires,
            attempt=attempt,
        ).validated()
        return job, lease

    def heartbeat_job(
        self,
        lease: CuratorJobLease,
        *,
        lease_seconds: float,
    ) -> CuratorJobLease:
        value = lease.validated()
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = float(self.clock())
        expires = now + float(lease_seconds)
        with self.transaction(immediate=True) as connection:
            placeholders = ",".join("?" for _ in ACTIVE_JOB_STATES)
            connection.execute(
                f"""
                UPDATE curator_jobs
                SET lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND task_id = ? AND lease_owner = ?
                    AND ownership_token = ? AND lease_epoch = ?
                    AND input_watermark = ? AND lease_expires_at > ?
                    AND state IN ({placeholders})
                """,
                (
                    expires,
                    now,
                    value.job_id,
                    value.task_id,
                    value.worker_id,
                    value.ownership_token,
                    value.lease_epoch,
                    value.input_watermark,
                    now,
                    *(item.value for item in ACTIVE_JOB_STATES),
                ),
            )
            if self._changes(connection) != 1:
                raise CuratorLeaseLostError("curator heartbeat was fenced or lease expired")
        return CuratorJobLease(
            job_id=value.job_id,
            task_id=value.task_id,
            worker_id=value.worker_id,
            ownership_token=value.ownership_token,
            lease_epoch=value.lease_epoch,
            input_watermark=value.input_watermark,
            expires_at=expires,
            attempt=value.attempt,
        ).validated()

    def transition_job(
        self,
        lease: CuratorJobLease,
        *,
        before: CuratorJobState,
        after: CuratorJobState,
        metadata: Mapping[str, Any] | None = None,
    ) -> CuratorJob:
        if before not in ACTIVE_JOB_STATES or after not in ACTIVE_JOB_STATES:
            raise ValueError("transition_job only handles active states")
        now = float(self.clock())
        with self.transaction(immediate=True) as connection:
            row = self._validate_lease(
                connection,
                lease,
                states=(before,),
                require_unexpired=True,
            )
            connection.execute(
                """
                UPDATE curator_jobs SET state = ?, updated_at = ?
                WHERE job_id = ? AND state = ? AND lease_owner = ?
                    AND ownership_token = ? AND lease_epoch = ?
                    AND lease_expires_at > ?
                """,
                (
                    after.value,
                    now,
                    lease.job_id,
                    before.value,
                    lease.worker_id,
                    lease.ownership_token,
                    lease.lease_epoch,
                    now,
                ),
            )
            if self._changes(connection) != 1:
                raise CuratorLeaseLostError("curator active transition was fenced")
            connection.execute(
                """
                UPDATE curator_job_attempts SET state = ?
                WHERE job_id = ? AND lease_epoch = ?
                """,
                (after.value, lease.job_id, lease.lease_epoch),
            )
            self._audit(
                connection,
                event_type=f"memory.curator.{after.value}",
                run_id=str(row["run_id"]),
                task_id=str(row["task_id"]),
                job_id=lease.job_id,
                causation_id=str(row["request_id"]),
                payload={
                    "state_before": before.value,
                    "state_after": after.value,
                    **dict(metadata or {}),
                },
                created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM curator_jobs WHERE job_id = ?",
                (lease.job_id,),
            ).fetchone()
        return self._job(updated)

    def succeed_job(
        self,
        lease: CuratorJobLease,
        *,
        candidate_count: int,
        committed_count: int,
    ) -> CuratorJob:
        now = float(self.clock())
        with self.transaction(immediate=True) as connection:
            row = self._validate_lease(
                connection,
                lease,
                states=ACTIVE_JOB_STATES,
                require_unexpired=True,
            )
            connection.execute(
                """
                UPDATE curator_jobs
                SET state = ?, last_success_watermark = input_watermark,
                    lease_owner = '', ownership_token = '', lease_expires_at = NULL,
                    finished_at = ?, updated_at = ?, error_code = '', error_message = '',
                    candidate_count = ?, committed_count = ?
                WHERE job_id = ? AND ownership_token = ? AND lease_epoch = ?
                """,
                (
                    CuratorJobState.SUCCEEDED.value,
                    now,
                    now,
                    max(0, int(candidate_count)),
                    max(0, int(committed_count)),
                    lease.job_id,
                    lease.ownership_token,
                    lease.lease_epoch,
                ),
            )
            if self._changes(connection) != 1:
                raise CuratorLeaseLostError("curator completion was fenced")
            connection.execute(
                """
                UPDATE curator_job_attempts
                SET state = ?, finished_at = ?, candidate_count = ?, committed_count = ?
                WHERE job_id = ? AND lease_epoch = ?
                """,
                (
                    CuratorJobState.SUCCEEDED.value,
                    now,
                    max(0, int(candidate_count)),
                    max(0, int(committed_count)),
                    lease.job_id,
                    lease.lease_epoch,
                ),
            )
            self._audit(
                connection,
                event_type="memory.curator.succeeded",
                run_id=str(row["run_id"]),
                task_id=str(row["task_id"]),
                job_id=lease.job_id,
                causation_id=str(row["request_id"]),
                payload={
                    "input_watermark": int(row["input_watermark"]),
                    "candidate_count": max(0, int(candidate_count)),
                    "committed_count": max(0, int(committed_count)),
                },
                created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM curator_jobs WHERE job_id = ?",
                (lease.job_id,),
            ).fetchone()
        return self._job(updated)

    def fail_job(
        self,
        lease: CuratorJobLease,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
        retry_delay_seconds: float = 1.0,
    ) -> CuratorJob:
        if not error_code.strip():
            raise ValueError("error_code is required")
        now = float(self.clock())
        with self.transaction(immediate=True) as connection:
            row = self._validate_lease(
                connection,
                lease,
                states=ACTIVE_JOB_STATES,
                require_unexpired=False,
            )
            retries = int(row["retry_remaining"])
            can_retry = bool(retryable and retries > 0)
            target = CuratorJobState.RETRY_WAIT if can_retry else CuratorJobState.FAILED
            retry_at = now + max(0.0, float(retry_delay_seconds)) if can_retry else None
            next_retries = retries - 1 if can_retry else retries
            connection.execute(
                """
                UPDATE curator_jobs
                SET state = ?, lease_owner = '', ownership_token = '',
                    lease_expires_at = NULL, retry_remaining = ?, retry_at = ?,
                    error_code = ?, error_message = ?, finished_at = ?, updated_at = ?
                WHERE job_id = ? AND ownership_token = ? AND lease_epoch = ?
                """,
                (
                    target.value,
                    next_retries,
                    retry_at,
                    error_code[:120],
                    error_message[:1000],
                    now,
                    now,
                    lease.job_id,
                    lease.ownership_token,
                    lease.lease_epoch,
                ),
            )
            if self._changes(connection) != 1:
                raise CuratorLeaseLostError("curator failure settlement was fenced")
            connection.execute(
                """
                UPDATE curator_job_attempts
                SET state = ?, finished_at = ?, error_code = ?
                WHERE job_id = ? AND lease_epoch = ?
                """,
                (target.value, now, error_code[:120], lease.job_id, lease.lease_epoch),
            )
            self._audit(
                connection,
                event_type="memory.curator.retry_wait" if can_retry else "memory.curator.failed",
                run_id=str(row["run_id"]),
                task_id=str(row["task_id"]),
                job_id=lease.job_id,
                causation_id=str(row["request_id"]),
                payload={
                    "error_code": error_code,
                    "retryable": retryable,
                    "retry_remaining": next_retries,
                    "retry_at": retry_at,
                },
                created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM curator_jobs WHERE job_id = ?",
                (lease.job_id,),
            ).fetchone()
        return self._job(updated)

    def sweep_expired_jobs(self, *, limit: int = 100) -> tuple[str, ...]:
        now = float(self.clock())
        stale_ids: list[str] = []
        with self.transaction(immediate=True) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM curator_jobs
                WHERE state IN ({','.join('?' for _ in ACTIVE_JOB_STATES)})
                    AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                ORDER BY lease_expires_at ASC, job_id ASC LIMIT ?
                """,
                (*(item.value for item in ACTIVE_JOB_STATES), now, max(0, int(limit))),
            ).fetchall()
            for row in rows:
                retries = int(row["retry_remaining"])
                target = CuratorJobState.RETRY_WAIT if retries > 0 else CuratorJobState.STALE
                retry_at = now if retries > 0 else None
                connection.execute(
                    f"""
                    UPDATE curator_jobs
                    SET state = ?, lease_owner = '', ownership_token = '',
                        lease_expires_at = NULL, retry_at = ?,
                        retry_remaining = CASE WHEN retry_remaining > 0 THEN retry_remaining - 1 ELSE 0 END,
                        error_code = 'lease_expired',
                        error_message = 'worker heartbeat expired before settlement',
                        finished_at = ?, updated_at = ?
                    WHERE job_id = ? AND lease_epoch = ?
                        AND state IN ({','.join('?' for _ in ACTIVE_JOB_STATES)})
                        AND lease_expires_at <= ?
                    """,
                    (
                        target.value,
                        retry_at,
                        now,
                        now,
                        row["job_id"],
                        row["lease_epoch"],
                        *(item.value for item in ACTIVE_JOB_STATES),
                        now,
                    ),
                )
                if self._changes(connection) != 1:
                    continue
                stale_ids.append(str(row["job_id"]))
                connection.execute(
                    """
                    UPDATE curator_job_attempts
                    SET state = ?, finished_at = ?, error_code = 'lease_expired'
                    WHERE job_id = ? AND lease_epoch = ?
                    """,
                    (target.value, now, row["job_id"], row["lease_epoch"]),
                )
                self._audit(
                    connection,
                    event_type="memory.curator.lease_expired",
                    run_id=str(row["run_id"]),
                    task_id=str(row["task_id"]),
                    job_id=str(row["job_id"]),
                    causation_id=str(row["request_id"]),
                    payload={
                        "lease_epoch": int(row["lease_epoch"]),
                        "worker_id": str(row["lease_owner"]),
                        "requeued": retries > 0,
                    },
                    created_at=now,
                )
        return tuple(stale_ids)

    def receipt_for_candidate(self, candidate_id: str) -> MemoryCommitReceipt | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM memory_commit_receipts WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return None if row is None else self._receipt(row)

    def memory_revision(self, memory_id: str) -> int:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT revision FROM memory_record_revisions WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
        return int(row["revision"]) if row is not None else 0

    def enqueue_outbox(
        self,
        connection: sqlite3.Connection,
        *,
        kind: OutboxKind,
        aggregate_id: str,
        task_id: str,
        run_id: str,
        payload: Mapping[str, Any],
        available_at: float | None = None,
    ) -> str:
        now = float(self.clock())
        message_id = stable_id("memory_outbox", aggregate_id, kind.value)
        connection.execute(
            """
            INSERT INTO memory_curator_outbox(
                message_id, kind, aggregate_id, task_id, run_id, payload_json,
                state, attempt, available_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            ON CONFLICT(aggregate_id, kind) DO NOTHING
            """,
            (
                message_id,
                kind.value,
                aggregate_id,
                task_id,
                run_id,
                canonical_json(payload),
                OutboxState.PENDING.value,
                now if available_at is None else float(available_at),
                now,
                now,
            ),
        )
        return message_id

    def claim_outbox(
        self,
        *,
        worker_id: str,
        lease_seconds: float,
        kinds: Sequence[OutboxKind] = (),
    ) -> OutboxMessage | None:
        if not worker_id.strip():
            raise ValueError("outbox worker_id is required")
        if lease_seconds <= 0:
            raise ValueError("outbox lease_seconds must be positive")
        now = float(self.clock())
        with self.transaction(immediate=True) as connection:
            clauses = ["state IN (?, ?)", "available_at <= ?"]
            parameters: list[Any] = [
                OutboxState.PENDING.value,
                OutboxState.RETRY_WAIT.value,
                now,
            ]
            if kinds:
                clauses.append(f"kind IN ({','.join('?' for _ in kinds)})")
                parameters.extend(item.value for item in kinds)
            row = connection.execute(
                f"""
                SELECT * FROM memory_curator_outbox
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at ASC, message_id ASC LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(24)
            expires = now + float(lease_seconds)
            connection.execute(
                """
                UPDATE memory_curator_outbox
                SET state = ?, attempt = attempt + 1, claimed_by = ?,
                    claim_token = ?, claim_expires_at = ?, updated_at = ?, error = ''
                WHERE message_id = ? AND state = ?
                """,
                (
                    OutboxState.DELIVERING.value,
                    worker_id,
                    token,
                    expires,
                    now,
                    row["message_id"],
                    row["state"],
                ),
            )
            if self._changes(connection) != 1:
                return None
            updated = connection.execute(
                "SELECT * FROM memory_curator_outbox WHERE message_id = ?",
                (row["message_id"],),
            ).fetchone()
        return self._outbox(updated)

    def deliver_outbox(self, message: OutboxMessage) -> OutboxMessage:
        now = float(self.clock())
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE memory_curator_outbox
                SET state = ?, delivered_at = ?, updated_at = ?,
                    claimed_by = '', claim_token = '', claim_expires_at = NULL, error = ''
                WHERE message_id = ? AND state = ? AND claimed_by = ?
                    AND claim_token = ? AND claim_expires_at > ?
                """,
                (
                    OutboxState.DELIVERED.value,
                    now,
                    now,
                    message.message_id,
                    OutboxState.DELIVERING.value,
                    message.claimed_by,
                    message.claim_token,
                    now,
                ),
            )
            if self._changes(connection) != 1:
                raise CuratorLeaseLostError("outbox delivery settlement was fenced")
            row = connection.execute(
                "SELECT * FROM memory_curator_outbox WHERE message_id = ?",
                (message.message_id,),
            ).fetchone()
        return self._outbox(row)

    def fail_outbox(
        self,
        message: OutboxMessage,
        *,
        error: str,
        retry_delay_seconds: float = 1.0,
        max_attempts: int = 10,
    ) -> OutboxMessage:
        now = float(self.clock())
        dead = message.attempt >= max(1, int(max_attempts))
        target = OutboxState.DEAD if dead else OutboxState.RETRY_WAIT
        available = now if dead else now + max(0.0, float(retry_delay_seconds))
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE memory_curator_outbox
                SET state = ?, available_at = ?, updated_at = ?, error = ?,
                    claimed_by = '', claim_token = '', claim_expires_at = NULL
                WHERE message_id = ? AND state = ? AND claimed_by = ? AND claim_token = ?
                """,
                (
                    target.value,
                    available,
                    now,
                    error[:1000],
                    message.message_id,
                    OutboxState.DELIVERING.value,
                    message.claimed_by,
                    message.claim_token,
                ),
            )
            if self._changes(connection) != 1:
                raise CuratorLeaseLostError("outbox failure settlement was fenced")
            row = connection.execute(
                "SELECT * FROM memory_curator_outbox WHERE message_id = ?",
                (message.message_id,),
            ).fetchone()
        return self._outbox(row)

    def sweep_expired_outbox(self, *, limit: int = 100) -> tuple[str, ...]:
        now = float(self.clock())
        with self.transaction(immediate=True) as connection:
            rows = connection.execute(
                """
                SELECT message_id FROM memory_curator_outbox
                WHERE state = ? AND claim_expires_at IS NOT NULL AND claim_expires_at <= ?
                ORDER BY claim_expires_at ASC, message_id ASC LIMIT ?
                """,
                (OutboxState.DELIVERING.value, now, max(0, int(limit))),
            ).fetchall()
            ids = tuple(str(row["message_id"]) for row in rows)
            for message_id in ids:
                connection.execute(
                    """
                    UPDATE memory_curator_outbox
                    SET state = ?, available_at = ?, updated_at = ?,
                        claimed_by = '', claim_token = '', claim_expires_at = NULL,
                        error = 'delivery lease expired'
                    WHERE message_id = ? AND state = ? AND claim_expires_at <= ?
                    """,
                    (
                        OutboxState.RETRY_WAIT.value,
                        now,
                        now,
                        message_id,
                        OutboxState.DELIVERING.value,
                        now,
                    ),
                )
        return ids

    def outbox_messages(
        self,
        *,
        states: Sequence[OutboxState] = (),
        task_id: str = "",
        limit: int = 1000,
    ) -> tuple[OutboxMessage, ...]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if states:
            clauses.append(f"state IN ({','.join('?' for _ in states)})")
            parameters.extend(item.value for item in states)
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(0, min(int(limit), 100_000)))
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM memory_curator_outbox {where}
                ORDER BY created_at ASC, message_id ASC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(self._outbox(row) for row in rows)

    def audit_events(self, *, task_id: str = "", limit: int = 1000) -> tuple[Mapping[str, Any], ...]:
        parameters: list[Any] = []
        where = ""
        if task_id:
            where = "WHERE task_id = ?"
            parameters.append(task_id)
        parameters.append(max(0, min(int(limit), 100_000)))
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM memory_curator_audit {where}
                ORDER BY created_at ASC, audit_id ASC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(
            {
                "audit_id": row["audit_id"],
                "event_type": row["event_type"],
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "job_id": row["job_id"],
                "candidate_id": row["candidate_id"],
                "decision_id": row["decision_id"],
                "memory_id": row["memory_id"],
                "causation_id": row["causation_id"],
                "payload": self._json_mapping(row["payload_json"]),
                "created_at": float(row["created_at"]),
            }
            for row in rows
        )

    def health(self) -> Mapping[str, Any]:
        with self.connection() as connection:
            candidate_rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM memory_candidates GROUP BY state"
            ).fetchall()
            job_rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM curator_jobs GROUP BY state"
            ).fetchall()
            outbox_rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM memory_curator_outbox GROUP BY state"
            ).fetchall()
            counts = {
                "evidence": int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM curator_evidence_documents"
                    ).fetchone()["count"]
                ),
                "bundles": int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM curator_evidence_bundles"
                    ).fetchone()["count"]
                ),
                "decisions": int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM memory_decisions"
                    ).fetchone()["count"]
                ),
                "receipts": int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM memory_commit_receipts"
                    ).fetchone()["count"]
                ),
            }
        return {
            "state_owner": "CuratorCandidateStore",
            "canonical_memory_owner": "SQLiteStore.memory_records",
            "candidate_state": {str(row["state"]): int(row["count"]) for row in candidate_rows},
            "job_state": {str(row["state"]): int(row["count"]) for row in job_rows},
            "outbox_state": {str(row["state"]): int(row["count"]) for row in outbox_rows},
            **counts,
        }

    def _validate_lease(
        self,
        connection: sqlite3.Connection,
        lease: CuratorJobLease,
        *,
        states: Sequence[CuratorJobState],
        require_unexpired: bool,
    ) -> sqlite3.Row:
        value = lease.validated()
        row = connection.execute(
            "SELECT * FROM curator_jobs WHERE job_id = ?",
            (value.job_id,),
        ).fetchone()
        if row is None:
            raise CuratorLeaseLostError("curator job no longer exists")
        if str(row["task_id"]) != value.task_id:
            raise CuratorLeaseLostError("curator lease task changed")
        if str(row["lease_owner"]) != value.worker_id:
            raise CuratorLeaseLostError("curator lease owner changed")
        if str(row["ownership_token"]) != value.ownership_token:
            raise CuratorLeaseLostError("curator ownership token changed")
        if int(row["lease_epoch"]) != value.lease_epoch:
            raise CuratorLeaseLostError("curator lease epoch changed")
        if int(row["input_watermark"]) != value.input_watermark:
            raise CuratorLeaseLostError("curator input watermark changed")
        if CuratorJobState(str(row["state"])) not in states:
            raise CuratorLeaseLostError("curator job is not in an allowed state")
        if require_unexpired:
            expires = row["lease_expires_at"]
            if expires is None or float(expires) <= float(self.clock()):
                raise CuratorLeaseLostError("curator lease expired")
        return row

    def _audit(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        run_id: str = "",
        task_id: str = "",
        job_id: str = "",
        candidate_id: str = "",
        decision_id: str = "",
        memory_id: str = "",
        causation_id: str = "",
        payload: Mapping[str, Any] | None = None,
        created_at: float | None = None,
    ) -> str:
        timestamp = float(self.clock()) if created_at is None else float(created_at)
        audit_id = stable_id(
            "curator_audit",
            event_type,
            run_id,
            task_id,
            job_id,
            candidate_id,
            decision_id,
            memory_id,
            causation_id,
            canonical_json(payload or {}),
            timestamp,
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO memory_curator_audit(
                audit_id, event_type, run_id, task_id, job_id, candidate_id,
                decision_id, memory_id, causation_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                event_type,
                run_id,
                task_id,
                job_id,
                candidate_id,
                decision_id,
                memory_id,
                causation_id,
                canonical_json(payload or {}),
                timestamp,
            ),
        )
        return audit_id

    def _candidate_values(self, value: MemoryCandidate, stored_at: float) -> tuple[Any, ...]:
        return (
            value.candidate_id,
            value.run_id,
            value.task_id,
            value.kind.value,
            value.proposed_layer,
            value.scope.value,
            value.subject,
            value.summary,
            canonical_json(value.content),
            value.evidence_bundle_id,
            value.evidence_digest,
            canonical_json(value.evidence_range.to_dict()),
            canonical_json(value.evidence_ids),
            canonical_json(value.artifact_ids),
            value.confidence,
            value.ttl_seconds,
            value.extractor_version,
            value.idempotency_key,
            value.semantic_digest,
            value.state.value,
            value.expected_memory_id,
            value.expected_revision,
            int(value.model_assisted),
            value.proposer,
            value.created_at,
            value.updated_at,
            canonical_json(value.metadata),
            stored_at,
        )

    @classmethod
    def _evidence_document(cls, row: sqlite3.Row) -> EvidenceDocument:
        return EvidenceDocument.from_dict(
            {
                "ref": {
                    "evidence_id": row["evidence_id"],
                    "kind": row["kind"],
                    "run_id": row["run_id"],
                    "task_id": row["task_id"],
                    "source_id": row["source_id"],
                    "source_revision": row["source_revision"],
                    "content_digest": row["content_digest"],
                    "trust": row["trust"],
                    "sequence": row["sequence_number"],
                    "created_at": row["created_at_text"],
                    "artifact_id": row["artifact_id"],
                    "event_id": row["event_id"],
                    "node_id": row["node_id"],
                    "producer": row["producer"],
                    "media_type": row["media_type"],
                    "char_count": row["char_count"],
                    "metadata": cls._json_mapping(row["metadata_json"]),
                },
                "title": row["title"],
                "text": row["text_value"],
                "normalized": cls._json_mapping(row["normalized_json"]),
                "redacted": bool(row["redacted"]),
                "secret_fingerprints": cls._json_list(row["secret_fingerprints_json"]),
            }
        )

    @classmethod
    def _candidate(cls, row: sqlite3.Row) -> MemoryCandidate:
        return MemoryCandidate.from_dict(
            {
                "candidate_id": row["candidate_id"],
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "kind": row["kind"],
                "proposed_layer": row["proposed_layer"],
                "scope": row["scope"],
                "subject": row["subject"],
                "summary": row["summary"],
                "content": cls._json_mapping(row["content_json"]),
                "evidence_bundle_id": row["evidence_bundle_id"],
                "evidence_digest": row["evidence_digest"],
                "evidence_range": cls._json_mapping(row["evidence_range_json"]),
                "evidence_ids": cls._json_list(row["evidence_ids_json"]),
                "artifact_ids": cls._json_list(row["artifact_ids_json"]),
                "confidence": row["confidence"],
                "ttl_seconds": row["ttl_seconds"],
                "extractor_version": row["extractor_version"],
                "idempotency_key": row["idempotency_key"],
                "state": row["state"],
                "expected_memory_id": row["expected_memory_id"],
                "expected_revision": row["expected_revision"],
                "model_assisted": bool(row["model_assisted"]),
                "proposer": row["proposer"],
                "created_at": row["created_at_text"],
                "updated_at": row["updated_at_text"],
                "metadata": cls._json_mapping(row["metadata_json"]),
            }
        )

    @classmethod
    def _relation(cls, row: sqlite3.Row) -> CandidateRelation:
        return CandidateRelation(
            relation_id=str(row["relation_id"]),
            source_candidate_id=str(row["source_candidate_id"]),
            target_candidate_id=str(row["target_candidate_id"]),
            kind=CandidateRelationKind(str(row["kind"])),
            reason=str(row["reason"]),
            evidence_digest=str(row["evidence_digest"]),
            created_at=str(row["created_at_text"]),
            metadata=cls._json_mapping(row["metadata_json"]),
        ).validated()

    @classmethod
    def _decision(cls, row: sqlite3.Row) -> MemoryDecision:
        return MemoryDecision.from_dict(
            {
                "decision_id": row["decision_id"],
                "candidate_id": row["candidate_id"],
                "status": row["status"],
                "candidate_digest": row["candidate_digest"],
                "evidence_digest": row["evidence_digest"],
                "policy_digest": row["policy_digest"],
                "validated_summary": row["validated_summary"],
                "validated_content": cls._json_mapping(row["validated_content_json"]),
                "issues": cls._json_list(row["issues_json"]),
                "target_memory_id": row["target_memory_id"],
                "target_revision": row["target_revision"],
                "relation_id": row["relation_id"],
                "created_at": row["created_at_text"],
                "metadata": cls._json_mapping(row["metadata_json"]),
            }
        )

    @classmethod
    def _request(cls, value: object) -> CuratorRunRequest:
        data = cls._json_mapping(value)
        return CuratorRunRequest(
            request_id=str(data.get("request_id") or ""),
            run_id=str(data.get("run_id") or ""),
            task_id=str(data.get("task_id") or ""),
            trigger=CuratorTrigger(str(data.get("trigger") or CuratorTrigger.MANUAL.value)),
            requested_by=str(data.get("requested_by") or ""),
            input_watermark=int(data.get("input_watermark", 0)),
            evidence_start=int(data.get("evidence_start", 0)),
            evidence_end=(
                int(data["evidence_end"])
                if data.get("evidence_end") is not None
                else None
            ),
            allow_model_assist=bool(data.get("allow_model_assist", True)),
            max_candidates=int(data.get("max_candidates", 64)),
            lease_seconds=float(data.get("lease_seconds", 30.0)),
            idempotency_key=str(data.get("idempotency_key") or ""),
            created_at=str(data.get("created_at") or ""),
            metadata=mapping(data.get("metadata")),
        ).validated()

    @classmethod
    def _job(cls, row: sqlite3.Row) -> CuratorJob:
        return CuratorJob(
            job_id=str(row["job_id"]),
            request=cls._request(row["request_json"]),
            state=CuratorJobState(str(row["state"])),
            input_watermark=int(row["input_watermark"]),
            last_success_watermark=int(row["last_success_watermark"]),
            lease_owner=str(row["lease_owner"]),
            ownership_token=str(row["ownership_token"]),
            lease_epoch=int(row["lease_epoch"]),
            lease_expires_at=(
                float(row["lease_expires_at"])
                if row["lease_expires_at"] is not None
                else None
            ),
            attempt=int(row["attempt"]),
            retry_remaining=int(row["retry_remaining"]),
            retry_at=float(row["retry_at"]) if row["retry_at"] is not None else None,
            error_code=str(row["error_code"]),
            error_message=str(row["error_message"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            started_at=float(row["started_at"]) if row["started_at"] is not None else None,
            finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
            candidate_count=int(row["candidate_count"]),
            committed_count=int(row["committed_count"]),
        )

    @classmethod
    def _receipt(cls, row: sqlite3.Row) -> MemoryCommitReceipt:
        return MemoryCommitReceipt.from_dict(
            {
                "receipt_id": row["receipt_id"],
                "candidate_id": row["candidate_id"],
                "decision_id": row["decision_id"],
                "disposition": row["disposition"],
                "memory_id": row["memory_id"],
                "memory_revision": row["memory_revision"],
                "outbox_message_ids": cls._json_list(row["outbox_message_ids_json"]),
                "committed_at": row["committed_at_text"],
                "reason": row["reason"],
                "metadata": cls._json_mapping(row["metadata_json"]),
            }
        )

    @classmethod
    def _outbox(cls, row: sqlite3.Row) -> OutboxMessage:
        return OutboxMessage(
            message_id=str(row["message_id"]),
            kind=OutboxKind(str(row["kind"])),
            aggregate_id=str(row["aggregate_id"]),
            task_id=str(row["task_id"]),
            run_id=str(row["run_id"]),
            payload=cls._json_mapping(row["payload_json"]),
            state=OutboxState(str(row["state"])),
            attempt=int(row["attempt"]),
            available_at=float(row["available_at"]),
            claimed_by=str(row["claimed_by"]),
            claim_token=str(row["claim_token"]),
            claim_expires_at=(
                float(row["claim_expires_at"])
                if row["claim_expires_at"] is not None
                else None
            ),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            delivered_at=(
                float(row["delivered_at"])
                if row["delivered_at"] is not None
                else None
            ),
            error=str(row["error"]),
        )

    @staticmethod
    def _json_mapping(value: object) -> dict[str, Any]:
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}

    @staticmethod
    def _json_list(value: object) -> list[Any]:
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return list(parsed) if isinstance(parsed, list) else []

    @staticmethod
    def _changes(connection: sqlite3.Connection) -> int:
        return int(connection.execute("SELECT changes() AS count").fetchone()["count"])

    @staticmethod
    def _timestamp_text() -> str:
        from datetime import UTC, datetime

        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = [
    "ACTIVE_JOB_STATES",
    "CandidateConflictError",
    "CuratorCandidateStore",
    "CuratorLeaseLostError",
    "CuratorStoreError",
]
