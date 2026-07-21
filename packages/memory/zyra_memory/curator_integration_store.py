from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from .curator_models import canonical_json, stable_digest, stable_id
from .curator_integration_models import (
    CURATOR_DELIVERY_PROTOCOL,
    CuratorConsumer,
    CuratorContextProof,
    CuratorDeliveryConflictError,
    CuratorDeliveryLease,
    CuratorDeliveryState,
    CuratorDownstreamDelivery,
    CuratorFailureContract,
    CuratorInputBatch,
    CuratorInputSource,
    CuratorIntegrationContractError,
    CuratorIntegrationRun,
    CuratorIntegrationRunState,
    CuratorOutcome,
    CuratorOutcomeKind,
    CuratorOutcomeState,
    CuratorProjectionConflictError,
    RuntimeTraceRef,
)


class CuratorIntegrationStore:
    """Durable projection/inbox state beside, but never instead of, canonical memory.

    Every table in this store is derived coordination state.  The canonical
    fact remains ``SQLiteStore.memory_records``; the same SQLite path is used
    so outcome publication and downstream fan-out can be atomic without a
    second database or an upstream Mnemopi/Hermes state owner.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.path = Path(path)
        self.clock = clock or time.time
        self._initialize_guard = threading.RLock()
        self._initialized = False
        self.initialize()

    def initialize(self) -> None:
        with self._initialize_guard:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as connection:
                connection.executescript(
                    """
                    PRAGMA journal_mode = WAL;
                    PRAGMA foreign_keys = ON;
                    PRAGMA busy_timeout = 30000;

                    CREATE TABLE IF NOT EXISTS memory_curator_input_batches (
                        batch_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        source_watermark INTEGER NOT NULL,
                        next_watermark INTEGER NOT NULL,
                        high_watermark INTEGER NOT NULL,
                        batch_digest TEXT NOT NULL,
                        has_more INTEGER NOT NULL,
                        created_at_text TEXT NOT NULL,
                        legacy_event_count INTEGER NOT NULL,
                        runtime_event_count INTEGER NOT NULL,
                        artifact_count INTEGER NOT NULL,
                        duplicate_source_ids_json TEXT NOT NULL,
                        warnings_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        stored_at REAL NOT NULL,
                        UNIQUE(task_id, source_watermark, next_watermark, batch_digest)
                    );

                    CREATE INDEX IF NOT EXISTS idx_curator_input_batches_task
                        ON memory_curator_input_batches(task_id, next_watermark, stored_at, batch_id);

                    CREATE TABLE IF NOT EXISTS memory_curator_input_refs (
                        ref_id TEXT PRIMARY KEY,
                        batch_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        source TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        source_sequence INTEGER NOT NULL,
                        content_digest TEXT NOT NULL,
                        occurred_at_text TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        aggregate_id TEXT NOT NULL,
                        causation_id TEXT NOT NULL,
                        correlation_id TEXT NOT NULL,
                        producer TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        artifact_ids_json TEXT NOT NULL,
                        trusted_runtime INTEGER NOT NULL,
                        canonical INTEGER NOT NULL,
                        metadata_json TEXT NOT NULL,
                        stored_at REAL NOT NULL,
                        FOREIGN KEY(batch_id)
                            REFERENCES memory_curator_input_batches(batch_id)
                            ON DELETE CASCADE,
                        UNIQUE(task_id, source, source_id, content_digest)
                    );

                    CREATE INDEX IF NOT EXISTS idx_curator_input_refs_task_sequence
                        ON memory_curator_input_refs(task_id, source_sequence, source, ref_id);
                    CREATE INDEX IF NOT EXISTS idx_curator_input_refs_source
                        ON memory_curator_input_refs(source, source_id, task_id);

                    CREATE TABLE IF NOT EXISTS memory_curator_input_cursors (
                        task_id TEXT NOT NULL,
                        source TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        watermark INTEGER NOT NULL,
                        high_watermark INTEGER NOT NULL,
                        batch_id TEXT NOT NULL,
                        batch_digest TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(task_id, source),
                        FOREIGN KEY(batch_id)
                            REFERENCES memory_curator_input_batches(batch_id)
                            ON DELETE RESTRICT
                    );

                    CREATE TABLE IF NOT EXISTS memory_curator_integration_runs (
                        integration_run_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        curator_job_id TEXT NOT NULL UNIQUE,
                        curator_request_id TEXT NOT NULL,
                        state TEXT NOT NULL,
                        input_batch_id TEXT NOT NULL,
                        input_digest TEXT NOT NULL,
                        started_at_text TEXT NOT NULL,
                        updated_at_text TEXT NOT NULL,
                        outcome_ids_json TEXT NOT NULL,
                        failure_ids_json TEXT NOT NULL,
                        delivery_ids_json TEXT NOT NULL,
                        context_proof_ids_json TEXT NOT NULL,
                        model_status TEXT NOT NULL,
                        canonical_commit_count INTEGER NOT NULL,
                        index_publication_count INTEGER NOT NULL,
                        error_code TEXT NOT NULL,
                        error_message TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        state_version INTEGER NOT NULL,
                        stored_at REAL NOT NULL,
                        FOREIGN KEY(input_batch_id)
                            REFERENCES memory_curator_input_batches(batch_id)
                            ON DELETE RESTRICT
                    );

                    CREATE INDEX IF NOT EXISTS idx_curator_integration_runs_task
                        ON memory_curator_integration_runs(task_id, stored_at, integration_run_id);

                    CREATE TABLE IF NOT EXISTS memory_curator_outcomes (
                        outcome_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        curator_job_id TEXT NOT NULL,
                        curator_request_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        state TEXT NOT NULL,
                        candidate_id TEXT NOT NULL,
                        decision_id TEXT NOT NULL,
                        evidence_bundle_id TEXT NOT NULL,
                        evidence_digest TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        target_consumers_json TEXT NOT NULL,
                        outcome_digest TEXT NOT NULL,
                        created_at_text TEXT NOT NULL,
                        memory_id TEXT NOT NULL,
                        memory_revision INTEGER NOT NULL,
                        commit_receipt_id TEXT NOT NULL,
                        causation_id TEXT NOT NULL,
                        model_assisted INTEGER NOT NULL,
                        deterministic_validation INTEGER NOT NULL,
                        canonical_memory_changed INTEGER NOT NULL,
                        index_published INTEGER NOT NULL,
                        supersedes_outcome_id TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        published_at REAL,
                        stored_at REAL NOT NULL,
                        FOREIGN KEY(curator_job_id)
                            REFERENCES memory_curator_integration_runs(curator_job_id)
                            ON DELETE CASCADE,
                        UNIQUE(curator_job_id, candidate_id, kind, outcome_digest)
                    );

                    CREATE INDEX IF NOT EXISTS idx_curator_outcomes_task
                        ON memory_curator_outcomes(task_id, kind, stored_at, outcome_id);
                    CREATE INDEX IF NOT EXISTS idx_curator_outcomes_memory
                        ON memory_curator_outcomes(memory_id, memory_revision, outcome_id);
                    CREATE INDEX IF NOT EXISTS idx_curator_outcomes_candidate
                        ON memory_curator_outcomes(candidate_id, decision_id, outcome_id);

                    CREATE TABLE IF NOT EXISTS memory_curator_failure_contracts (
                        failure_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        curator_job_id TEXT NOT NULL,
                        phase TEXT NOT NULL,
                        code TEXT NOT NULL,
                        message TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        disposition TEXT NOT NULL,
                        retryable INTEGER NOT NULL,
                        attempt INTEGER NOT NULL,
                        causation_id TEXT NOT NULL,
                        created_at_text TEXT NOT NULL,
                        candidate_id TEXT NOT NULL,
                        decision_id TEXT NOT NULL,
                        outcome_id TEXT NOT NULL,
                        evidence_ids_json TEXT NOT NULL,
                        consumer_hints_json TEXT NOT NULL,
                        canonical_memory_changed INTEGER NOT NULL,
                        operator_action_required INTEGER NOT NULL,
                        details_json TEXT NOT NULL,
                        stored_at REAL NOT NULL,
                        FOREIGN KEY(curator_job_id)
                            REFERENCES memory_curator_integration_runs(curator_job_id)
                            ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_curator_failures_task
                        ON memory_curator_failure_contracts(task_id, severity, stored_at, failure_id);
                    CREATE INDEX IF NOT EXISTS idx_curator_failures_outcome
                        ON memory_curator_failure_contracts(outcome_id, failure_id);

                    CREATE TABLE IF NOT EXISTS memory_curator_downstream_deliveries (
                        delivery_id TEXT PRIMARY KEY,
                        outcome_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        consumer TEXT NOT NULL,
                        state TEXT NOT NULL,
                        payload_digest TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        available_at REAL NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        attempt INTEGER NOT NULL,
                        retry_remaining INTEGER NOT NULL,
                        claimed_by TEXT NOT NULL,
                        claim_token TEXT NOT NULL,
                        lease_epoch INTEGER NOT NULL,
                        claim_expires_at REAL,
                        acknowledged_at REAL,
                        error_code TEXT NOT NULL,
                        error_message TEXT NOT NULL,
                        receipt_json TEXT NOT NULL,
                        FOREIGN KEY(outcome_id)
                            REFERENCES memory_curator_outcomes(outcome_id)
                            ON DELETE CASCADE,
                        UNIQUE(outcome_id, consumer)
                    );

                    CREATE INDEX IF NOT EXISTS idx_curator_delivery_claim
                        ON memory_curator_downstream_deliveries(
                            consumer, state, available_at, created_at, delivery_id
                        );
                    CREATE INDEX IF NOT EXISTS idx_curator_delivery_task
                        ON memory_curator_downstream_deliveries(task_id, consumer, state, delivery_id);

                    CREATE TABLE IF NOT EXISTS memory_curator_consumer_checkpoints (
                        consumer TEXT NOT NULL,
                        partition_key TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        last_delivery_id TEXT NOT NULL,
                        last_outcome_id TEXT NOT NULL,
                        outcome_digest TEXT NOT NULL,
                        receipt_digest TEXT NOT NULL,
                        acknowledged_count INTEGER NOT NULL,
                        version INTEGER NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(consumer, partition_key)
                    );

                    CREATE TABLE IF NOT EXISTS memory_curator_context_proofs (
                        proof_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        outcome_id TEXT NOT NULL,
                        memory_id TEXT NOT NULL,
                        memory_revision INTEGER NOT NULL,
                        query_id TEXT NOT NULL,
                        query_digest TEXT NOT NULL,
                        index_scope TEXT NOT NULL,
                        index_generation INTEGER NOT NULL,
                        index_revision TEXT NOT NULL,
                        retrieved_memory_ids_json TEXT NOT NULL,
                        context_digest TEXT NOT NULL,
                        effect TEXT NOT NULL,
                        consumer TEXT NOT NULL,
                        created_at_text TEXT NOT NULL,
                        worker_request_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        message_count INTEGER NOT NULL,
                        total_chars INTEGER NOT NULL,
                        contains_index_dump INTEGER NOT NULL,
                        canonical_owner TEXT NOT NULL,
                        index_owner TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        stored_at REAL NOT NULL,
                        FOREIGN KEY(outcome_id)
                            REFERENCES memory_curator_outcomes(outcome_id)
                            ON DELETE CASCADE,
                        UNIQUE(outcome_id, query_id, index_revision, context_digest)
                    );

                    CREATE INDEX IF NOT EXISTS idx_curator_context_task
                        ON memory_curator_context_proofs(task_id, stored_at, proof_id);
                    CREATE INDEX IF NOT EXISTS idx_curator_context_memory
                        ON memory_curator_context_proofs(memory_id, memory_revision, proof_id);

                    CREATE TABLE IF NOT EXISTS memory_curator_integration_audit (
                        audit_id TEXT PRIMARY KEY,
                        event_type TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        integration_run_id TEXT NOT NULL,
                        outcome_id TEXT NOT NULL,
                        delivery_id TEXT NOT NULL,
                        causation_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_curator_integration_audit_task
                        ON memory_curator_integration_audit(task_id, created_at, audit_id);
                    """
                )
            self._initialized = True

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

    def record_input_batch(
        self,
        batch: CuratorInputBatch,
        *,
        cursor_source: CuratorInputSource = CuratorInputSource.RUNTIME_EVENT_SPINE,
        expected_cursor_version: int | None = None,
    ) -> tuple[CuratorInputBatch, bool]:
        value = batch.validated()
        now = float(self.clock())
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT batch_digest FROM memory_curator_input_batches WHERE batch_id = ?",
                (value.batch_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["batch_digest"]) != value.batch_digest:
                    raise CuratorProjectionConflictError(
                        "input batch identity reused with another digest"
                    )
                stored = self._input_batch_from_connection(connection, value.batch_id)
                return stored, False
            connection.execute(
                """
                INSERT INTO memory_curator_input_batches(
                    batch_id, run_id, task_id, source_watermark, next_watermark,
                    high_watermark, batch_digest, has_more, created_at_text,
                    legacy_event_count, runtime_event_count, artifact_count,
                    duplicate_source_ids_json, warnings_json, metadata_json, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    value.batch_id,
                    value.run_id,
                    value.task_id,
                    value.source_watermark,
                    value.next_watermark,
                    value.high_watermark,
                    value.batch_digest,
                    int(value.has_more),
                    value.created_at,
                    value.legacy_event_count,
                    value.runtime_event_count,
                    value.artifact_count,
                    canonical_json(value.duplicate_source_ids),
                    canonical_json(value.warnings),
                    canonical_json(value.metadata),
                    now,
                ),
            )
            for ref in value.refs:
                self._insert_input_ref(connection, value.batch_id, ref, stored_at=now)
            self._advance_cursor(
                connection,
                batch=value,
                source=cursor_source,
                expected_version=expected_cursor_version,
                updated_at=now,
            )
            self._audit(
                connection,
                event_type="memory.curator.input_batch_recorded",
                run_id=value.run_id,
                task_id=value.task_id,
                causation_id=value.batch_id,
                payload={
                    "batch_id": value.batch_id,
                    "batch_digest": value.batch_digest,
                    "ref_count": len(value.refs),
                    "source_watermark": value.source_watermark,
                    "next_watermark": value.next_watermark,
                    "high_watermark": value.high_watermark,
                    "cursor_source": cursor_source.value,
                },
                created_at=now,
            )
        return value, True

    def _insert_input_ref(
        self,
        connection: sqlite3.Connection,
        batch_id: str,
        ref: RuntimeTraceRef,
        *,
        stored_at: float,
    ) -> None:
        value = ref.validated()
        conflicting = connection.execute(
            """
            SELECT ref_id, content_digest
            FROM memory_curator_input_refs
            WHERE task_id = ? AND source = ? AND source_id = ?
            """,
            (value.task_id, value.source.value, value.source_id),
        ).fetchone()
        if conflicting is not None:
            if str(conflicting["content_digest"]) != value.content_digest:
                raise CuratorProjectionConflictError(
                    "runtime source identity was reused with changed content"
                )
            return
        connection.execute(
            """
            INSERT INTO memory_curator_input_refs(
                ref_id, batch_id, run_id, task_id, source, kind, source_id,
                source_sequence, content_digest, occurred_at_text, event_type,
                aggregate_id, causation_id, correlation_id, producer, summary,
                artifact_ids_json, trusted_runtime, canonical, metadata_json,
                stored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                value.ref_id,
                batch_id,
                value.run_id,
                value.task_id,
                value.source.value,
                value.kind.value,
                value.source_id,
                value.source_sequence,
                value.content_digest,
                value.occurred_at,
                value.event_type,
                value.aggregate_id,
                value.causation_id,
                value.correlation_id,
                value.producer,
                value.summary,
                canonical_json(value.artifact_ids),
                int(value.trusted_runtime),
                int(value.canonical),
                canonical_json(value.metadata),
                stored_at,
            ),
        )

    def _advance_cursor(
        self,
        connection: sqlite3.Connection,
        *,
        batch: CuratorInputBatch,
        source: CuratorInputSource,
        expected_version: int | None,
        updated_at: float,
    ) -> None:
        row = connection.execute(
            """
            SELECT watermark, high_watermark, version
            FROM memory_curator_input_cursors
            WHERE task_id = ? AND source = ?
            """,
            (batch.task_id, source.value),
        ).fetchone()
        if row is None:
            if expected_version not in {None, 0}:
                raise CuratorProjectionConflictError(
                    "input cursor was not initialized at expected version"
                )
            if batch.source_watermark != 0:
                raise CuratorProjectionConflictError(
                    "first input batch must start from watermark zero"
                )
            connection.execute(
                """
                INSERT INTO memory_curator_input_cursors(
                    task_id, source, run_id, watermark, high_watermark,
                    batch_id, batch_digest, version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch.task_id,
                    source.value,
                    batch.run_id,
                    batch.next_watermark,
                    batch.high_watermark,
                    batch.batch_id,
                    batch.batch_digest,
                    1,
                    updated_at,
                ),
            )
            return
        current_version = int(row["version"])
        current_watermark = int(row["watermark"])
        if expected_version is not None and current_version != expected_version:
            raise CuratorProjectionConflictError(
                f"input cursor version changed: expected {expected_version}, got {current_version}"
            )
        if batch.source_watermark != current_watermark:
            if (
                batch.next_watermark == current_watermark
                and batch.high_watermark == int(row["high_watermark"])
            ):
                return
            raise CuratorProjectionConflictError(
                f"input cursor gap: expected {current_watermark}, got {batch.source_watermark}"
            )
        connection.execute(
            """
            UPDATE memory_curator_input_cursors
            SET run_id = ?, watermark = ?, high_watermark = ?, batch_id = ?,
                batch_digest = ?, version = ?, updated_at = ?
            WHERE task_id = ? AND source = ? AND version = ?
            """,
            (
                batch.run_id,
                batch.next_watermark,
                batch.high_watermark,
                batch.batch_id,
                batch.batch_digest,
                current_version + 1,
                updated_at,
                batch.task_id,
                source.value,
                current_version,
            ),
        )
        if self._changes(connection) != 1:
            raise CuratorProjectionConflictError("input cursor CAS failed")

    def input_cursor(
        self,
        task_id: str,
        source: CuratorInputSource = CuratorInputSource.RUNTIME_EVENT_SPINE,
    ) -> Mapping[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM memory_curator_input_cursors WHERE task_id = ? AND source = ?",
                (task_id, source.value),
            ).fetchone()
        if row is None:
            return {
                "task_id": task_id,
                "source": source.value,
                "watermark": 0,
                "high_watermark": 0,
                "batch_id": "",
                "batch_digest": "",
                "version": 0,
                "updated_at": 0.0,
            }
        return dict(row)

    def input_batch(self, batch_id: str) -> CuratorInputBatch | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT batch_id FROM memory_curator_input_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if row is None:
                return None
            return self._input_batch_from_connection(connection, batch_id)

    def input_batches(
        self,
        *,
        task_id: str = "",
        limit: int = 100,
    ) -> tuple[CuratorInputBatch, ...]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(0, min(int(limit), 10_000)))
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT batch_id
                FROM memory_curator_input_batches
                {where}
                ORDER BY stored_at DESC, batch_id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            return tuple(
                self._input_batch_from_connection(connection, str(row["batch_id"]))
                for row in rows
            )

    def save_integration_run(
        self,
        run: CuratorIntegrationRun,
        *,
        expected_state: CuratorIntegrationRunState | None = None,
    ) -> CuratorIntegrationRun:
        value = run.validated()
        now = float(self.clock())
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT state, state_version, input_digest
                FROM memory_curator_integration_runs
                WHERE integration_run_id = ?
                """,
                (value.integration_run_id,),
            ).fetchone()
            if row is None:
                if expected_state is not None:
                    raise CuratorProjectionConflictError(
                        "integration run does not exist at expected state"
                    )
                self._insert_integration_run(connection, value, state_version=1, stored_at=now)
                self._audit(
                    connection,
                    event_type="memory.curator.integration_started",
                    run_id=value.run_id,
                    task_id=value.task_id,
                    integration_run_id=value.integration_run_id,
                    causation_id=value.curator_job_id,
                    payload=value.to_dict(),
                    created_at=now,
                )
                return value
            current_state = CuratorIntegrationRunState(str(row["state"]))
            if str(row["input_digest"]) != value.input_digest:
                raise CuratorProjectionConflictError(
                    "integration run identity reused with another input digest"
                )
            if expected_state is not None and current_state is not expected_state:
                raise CuratorProjectionConflictError(
                    f"integration run state changed: expected {expected_state.value}, got {current_state.value}"
                )
            version = int(row["state_version"])
            self._update_integration_run(
                connection,
                value,
                expected_version=version,
                next_version=version + 1,
                stored_at=now,
            )
            self._audit(
                connection,
                event_type=f"memory.curator.integration_{value.state.value}",
                run_id=value.run_id,
                task_id=value.task_id,
                integration_run_id=value.integration_run_id,
                causation_id=value.curator_job_id,
                payload=value.to_dict(),
                created_at=now,
            )
        return value

    def integration_run(self, integration_run_id: str) -> CuratorIntegrationRun | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM memory_curator_integration_runs WHERE integration_run_id = ?",
                (integration_run_id,),
            ).fetchone()
        return None if row is None else self._integration_run(row)

    def integration_run_for_job(self, job_id: str) -> CuratorIntegrationRun | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM memory_curator_integration_runs WHERE curator_job_id = ?",
                (job_id,),
            ).fetchone()
        return None if row is None else self._integration_run(row)

    def integration_runs(
        self,
        *,
        task_id: str = "",
        states: Sequence[CuratorIntegrationRunState] = (),
        limit: int = 100,
    ) -> tuple[CuratorIntegrationRun, ...]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            parameters.extend(state.value for state in states)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(0, min(int(limit), 10_000)))
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM memory_curator_integration_runs
                {where}
                ORDER BY stored_at DESC, integration_run_id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(self._integration_run(row) for row in rows)

    def publish_projection(
        self,
        *,
        integration_run: CuratorIntegrationRun,
        outcomes: Sequence[CuratorOutcome],
        failures: Sequence[CuratorFailureContract] = (),
        retry_limit: int = 5,
    ) -> tuple[
        CuratorIntegrationRun,
        tuple[CuratorOutcome, ...],
        tuple[CuratorFailureContract, ...],
        tuple[CuratorDownstreamDelivery, ...],
        tuple[str, ...],
    ]:
        run = integration_run.validated()
        if run.state is not CuratorIntegrationRunState.PROJECTING:
            raise CuratorProjectionConflictError(
                "projection publication requires a projecting integration run"
            )
        outcome_values = tuple(item.validated().publish() for item in outcomes)
        failure_values = tuple(item.validated() for item in failures)
        if any(item.curator_job_id != run.curator_job_id for item in outcome_values):
            raise CuratorIntegrationContractError("outcome belongs to another curator job")
        if any(item.curator_job_id != run.curator_job_id for item in failure_values):
            raise CuratorIntegrationContractError("failure belongs to another curator job")
        retry_count = max(0, min(int(retry_limit), 100))
        now = float(self.clock())
        published: list[CuratorOutcome] = []
        recorded_failures: list[CuratorFailureContract] = []
        deliveries: list[CuratorDownstreamDelivery] = []
        duplicates: list[str] = []
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT state, state_version, input_digest
                FROM memory_curator_integration_runs
                WHERE integration_run_id = ?
                """,
                (run.integration_run_id,),
            ).fetchone()
            if row is None:
                raise CuratorProjectionConflictError("integration run disappeared")
            if CuratorIntegrationRunState(str(row["state"])) is not CuratorIntegrationRunState.PROJECTING:
                raise CuratorProjectionConflictError("integration run is no longer projecting")
            if str(row["input_digest"]) != run.input_digest:
                raise CuratorProjectionConflictError("integration input digest changed")
            for outcome in outcome_values:
                inserted = self._insert_outcome(connection, outcome, stored_at=now)
                if not inserted:
                    duplicates.append(outcome.outcome_id)
                stored = self._outcome_from_connection(connection, outcome.outcome_id)
                published.append(stored)
                for consumer in stored.target_consumers:
                    delivery, _ = self._ensure_delivery(
                        connection,
                        stored,
                        consumer,
                        retry_limit=retry_count,
                        created_at=now,
                    )
                    deliveries.append(delivery)
            for failure in failure_values:
                self._insert_failure(connection, failure, stored_at=now)
                recorded_failures.append(
                    self._failure_from_connection(connection, failure.failure_id)
                )
            next_run = run.transition(
                CuratorIntegrationRunState.PUBLISHED,
                outcome_ids=[item.outcome_id for item in published],
                failure_ids=[item.failure_id for item in recorded_failures],
                delivery_ids=[item.delivery_id for item in deliveries],
                canonical_commit_count=sum(
                    1 for item in published if item.canonical_memory_changed
                ),
                index_publication_count=sum(
                    1 for item in published if item.index_published
                ),
                metadata={
                    "duplicate_outcome_ids": duplicates,
                    "publication_transaction": True,
                    "canonical_memory_owner": "SQLiteStore.memory_records",
                },
            )
            version = int(row["state_version"])
            self._update_integration_run(
                connection,
                next_run,
                expected_version=version,
                next_version=version + 1,
                stored_at=now,
            )
            self._audit(
                connection,
                event_type="memory.curator.projection_published",
                run_id=run.run_id,
                task_id=run.task_id,
                integration_run_id=run.integration_run_id,
                causation_id=run.curator_job_id,
                payload={
                    "outcome_ids": [item.outcome_id for item in published],
                    "failure_ids": [item.failure_id for item in recorded_failures],
                    "delivery_ids": [item.delivery_id for item in deliveries],
                    "duplicate_outcome_ids": duplicates,
                    "canonical_commit_count": next_run.canonical_commit_count,
                    "index_publication_count": next_run.index_publication_count,
                },
                created_at=now,
            )
        return (
            next_run,
            tuple(published),
            tuple(recorded_failures),
            tuple(
                {
                    delivery.delivery_id: delivery
                    for delivery in deliveries
                }.values()
            ),
            tuple(duplicates),
        )

    def _insert_outcome(
        self,
        connection: sqlite3.Connection,
        outcome: CuratorOutcome,
        *,
        stored_at: float,
    ) -> bool:
        existing = connection.execute(
            "SELECT outcome_digest FROM memory_curator_outcomes WHERE outcome_id = ?",
            (outcome.outcome_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["outcome_digest"]) != outcome.outcome_digest:
                raise CuratorProjectionConflictError(
                    "outcome identity reused with another digest"
                )
            return False
        if outcome.supersedes_outcome_id:
            target = connection.execute(
                "SELECT task_id, state FROM memory_curator_outcomes WHERE outcome_id = ?",
                (outcome.supersedes_outcome_id,),
            ).fetchone()
            if target is None or str(target["task_id"]) != outcome.task_id:
                raise CuratorProjectionConflictError(
                    "superseding outcome does not identify a same-task target"
                )
            connection.execute(
                """
                UPDATE memory_curator_outcomes
                SET state = ?
                WHERE outcome_id = ? AND state = ?
                """,
                (
                    CuratorOutcomeState.SUPERSEDED.value,
                    outcome.supersedes_outcome_id,
                    CuratorOutcomeState.PUBLISHED.value,
                ),
            )
        connection.execute(
            """
            INSERT INTO memory_curator_outcomes(
                outcome_id, run_id, task_id, curator_job_id, curator_request_id,
                kind, state, candidate_id, decision_id, evidence_bundle_id,
                evidence_digest, subject, summary, payload_json,
                target_consumers_json, outcome_digest, created_at_text,
                memory_id, memory_revision, commit_receipt_id, causation_id,
                model_assisted, deterministic_validation,
                canonical_memory_changed, index_published,
                supersedes_outcome_id, metadata_json, published_at, stored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outcome.outcome_id,
                outcome.run_id,
                outcome.task_id,
                outcome.curator_job_id,
                outcome.curator_request_id,
                outcome.kind.value,
                outcome.state.value,
                outcome.candidate_id,
                outcome.decision_id,
                outcome.evidence_bundle_id,
                outcome.evidence_digest,
                outcome.subject,
                outcome.summary,
                canonical_json(outcome.payload),
                canonical_json([consumer.value for consumer in outcome.target_consumers]),
                outcome.outcome_digest,
                outcome.created_at,
                outcome.memory_id,
                outcome.memory_revision,
                outcome.commit_receipt_id,
                outcome.causation_id,
                int(outcome.model_assisted),
                int(outcome.deterministic_validation),
                int(outcome.canonical_memory_changed),
                int(outcome.index_published),
                outcome.supersedes_outcome_id,
                canonical_json(outcome.metadata),
                stored_at if outcome.state is CuratorOutcomeState.PUBLISHED else None,
                stored_at,
            ),
        )
        return True

    def _insert_failure(
        self,
        connection: sqlite3.Connection,
        failure: CuratorFailureContract,
        *,
        stored_at: float,
    ) -> bool:
        existing = connection.execute(
            "SELECT failure_id FROM memory_curator_failure_contracts WHERE failure_id = ?",
            (failure.failure_id,),
        ).fetchone()
        if existing is not None:
            return False
        connection.execute(
            """
            INSERT INTO memory_curator_failure_contracts(
                failure_id, run_id, task_id, curator_job_id, phase, code,
                message, severity, disposition, retryable, attempt,
                causation_id, created_at_text, candidate_id, decision_id,
                outcome_id, evidence_ids_json, consumer_hints_json,
                canonical_memory_changed, operator_action_required,
                details_json, stored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                failure.failure_id,
                failure.run_id,
                failure.task_id,
                failure.curator_job_id,
                failure.phase,
                failure.code,
                failure.message,
                failure.severity.value,
                failure.disposition.value,
                int(failure.retryable),
                failure.attempt,
                failure.causation_id,
                failure.created_at,
                failure.candidate_id,
                failure.decision_id,
                failure.outcome_id,
                canonical_json(failure.evidence_ids),
                canonical_json([consumer.value for consumer in failure.consumer_hints]),
                int(failure.canonical_memory_changed),
                int(failure.operator_action_required),
                canonical_json(failure.details),
                stored_at,
            ),
        )
        return True

    def _ensure_delivery(
        self,
        connection: sqlite3.Connection,
        outcome: CuratorOutcome,
        consumer: CuratorConsumer,
        *,
        retry_limit: int,
        created_at: float,
    ) -> tuple[CuratorDownstreamDelivery, bool]:
        payload = {
            "protocol": CURATOR_DELIVERY_PROTOCOL,
            "consumer": consumer.value,
            "outcome": outcome.to_dict(),
            "canonical_memory_owner": "SQLiteStore.memory_records",
            "source_state_owner": "CuratorIntegrationStore",
        }
        payload_digest = stable_digest(payload)
        delivery_id = stable_id(
            "curator_delivery",
            outcome.outcome_id,
            consumer.value,
            payload_digest,
        )
        existing = connection.execute(
            """
            SELECT delivery_id, payload_digest
            FROM memory_curator_downstream_deliveries
            WHERE outcome_id = ? AND consumer = ?
            """,
            (outcome.outcome_id, consumer.value),
        ).fetchone()
        if existing is not None:
            if str(existing["payload_digest"]) != payload_digest:
                raise CuratorProjectionConflictError(
                    "downstream delivery content changed for the same outcome/consumer"
                )
            return (
                self._delivery_from_connection(connection, str(existing["delivery_id"])),
                False,
            )
        connection.execute(
            """
            INSERT INTO memory_curator_downstream_deliveries(
                delivery_id, outcome_id, run_id, task_id, consumer, state,
                payload_digest, payload_json, available_at, created_at,
                updated_at, attempt, retry_remaining, claimed_by, claim_token,
                lease_epoch, claim_expires_at, acknowledged_at, error_code,
                error_message, receipt_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delivery_id,
                outcome.outcome_id,
                outcome.run_id,
                outcome.task_id,
                consumer.value,
                CuratorDeliveryState.PENDING.value,
                payload_digest,
                canonical_json(payload),
                created_at,
                created_at,
                created_at,
                0,
                retry_limit,
                "",
                "",
                0,
                None,
                None,
                "",
                "",
                "{}",
            ),
        )
        return self._delivery_from_connection(connection, delivery_id), True

    def outcome(self, outcome_id: str) -> CuratorOutcome | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT outcome_id FROM memory_curator_outcomes WHERE outcome_id = ?",
                (outcome_id,),
            ).fetchone()
            if row is None:
                return None
            return self._outcome_from_connection(connection, outcome_id)

    def outcomes(
        self,
        *,
        task_id: str = "",
        job_id: str = "",
        kinds: Sequence[CuratorOutcomeKind] = (),
        states: Sequence[CuratorOutcomeState] = (),
        canonical_only: bool = False,
        limit: int = 1000,
    ) -> tuple[CuratorOutcome, ...]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if job_id:
            clauses.append("curator_job_id = ?")
            parameters.append(job_id)
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"kind IN ({placeholders})")
            parameters.extend(kind.value for kind in kinds)
        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            parameters.extend(state.value for state in states)
        if canonical_only:
            clauses.append("canonical_memory_changed = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(0, min(int(limit), 100_000)))
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT outcome_id
                FROM memory_curator_outcomes
                {where}
                ORDER BY stored_at DESC, outcome_id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            return tuple(
                self._outcome_from_connection(connection, str(row["outcome_id"]))
                for row in rows
            )

    def failure(self, failure_id: str) -> CuratorFailureContract | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT failure_id FROM memory_curator_failure_contracts WHERE failure_id = ?",
                (failure_id,),
            ).fetchone()
            if row is None:
                return None
            return self._failure_from_connection(connection, failure_id)

    def failures(
        self,
        *,
        task_id: str = "",
        job_id: str = "",
        retryable: bool | None = None,
        limit: int = 1000,
    ) -> tuple[CuratorFailureContract, ...]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if job_id:
            clauses.append("curator_job_id = ?")
            parameters.append(job_id)
        if retryable is not None:
            clauses.append("retryable = ?")
            parameters.append(int(retryable))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(0, min(int(limit), 100_000)))
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT failure_id
                FROM memory_curator_failure_contracts
                {where}
                ORDER BY stored_at DESC, failure_id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            return tuple(
                self._failure_from_connection(connection, str(row["failure_id"]))
                for row in rows
            )

    def claim_delivery(
        self,
        *,
        consumer: CuratorConsumer,
        worker_id: str,
        lease_seconds: float = 30.0,
        task_id: str = "",
        delivery_id: str = "",
    ) -> tuple[CuratorDownstreamDelivery, CuratorDeliveryLease] | None:
        owner = str(worker_id).strip()
        if not owner:
            raise ValueError("delivery worker_id is required")
        ttl = float(lease_seconds)
        if ttl <= 0:
            raise ValueError("delivery lease_seconds must be positive")
        now = float(self.clock())
        expires_at = now + ttl
        claim_token = secrets.token_urlsafe(24)
        with self.transaction(immediate=True) as connection:
            clauses = [
                "consumer = ?",
                "state IN (?, ?)",
                "available_at <= ?",
                "retry_remaining > 0",
            ]
            parameters: list[Any] = [
                consumer.value,
                CuratorDeliveryState.PENDING.value,
                CuratorDeliveryState.RETRY_WAIT.value,
                now,
            ]
            if task_id:
                clauses.append("task_id = ?")
                parameters.append(task_id)
            if delivery_id:
                clauses.append("delivery_id = ?")
                parameters.append(delivery_id)
            row = connection.execute(
                f"""
                SELECT delivery_id, lease_epoch, attempt
                FROM memory_curator_downstream_deliveries
                WHERE {' AND '.join(clauses)}
                ORDER BY available_at ASC, created_at ASC, delivery_id ASC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is None:
                return None
            selected = str(row["delivery_id"])
            epoch = int(row["lease_epoch"]) + 1
            attempt = int(row["attempt"]) + 1
            connection.execute(
                """
                UPDATE memory_curator_downstream_deliveries
                SET state = ?, claimed_by = ?, claim_token = ?, lease_epoch = ?,
                    claim_expires_at = ?, attempt = ?, updated_at = ?,
                    error_code = '', error_message = ''
                WHERE delivery_id = ?
                  AND state IN (?, ?)
                  AND available_at <= ?
                  AND lease_epoch = ?
                """,
                (
                    CuratorDeliveryState.CLAIMED.value,
                    owner,
                    claim_token,
                    epoch,
                    expires_at,
                    attempt,
                    now,
                    selected,
                    CuratorDeliveryState.PENDING.value,
                    CuratorDeliveryState.RETRY_WAIT.value,
                    now,
                    int(row["lease_epoch"]),
                ),
            )
            if self._changes(connection) != 1:
                return None
            delivery = self._delivery_from_connection(connection, selected)
            lease = CuratorDeliveryLease(
                delivery_id=selected,
                consumer=consumer,
                worker_id=owner,
                claim_token=claim_token,
                lease_epoch=epoch,
                attempt=attempt,
                expires_at=expires_at,
            ).validated()
            self._audit(
                connection,
                event_type="memory.curator.delivery_claimed",
                run_id=delivery.run_id,
                task_id=delivery.task_id,
                outcome_id=delivery.outcome_id,
                delivery_id=delivery.delivery_id,
                causation_id=delivery.outcome_id,
                payload=lease.to_dict(),
                created_at=now,
            )
            return delivery, lease

    def renew_delivery(
        self,
        lease: CuratorDeliveryLease,
        *,
        lease_seconds: float = 30.0,
    ) -> CuratorDeliveryLease:
        value = lease.validated()
        ttl = float(lease_seconds)
        if ttl <= 0:
            raise ValueError("delivery lease_seconds must be positive")
        now = float(self.clock())
        expires_at = now + ttl
        with self.transaction(immediate=True) as connection:
            self._assert_delivery_lease(connection, value, now=now)
            connection.execute(
                """
                UPDATE memory_curator_downstream_deliveries
                SET claim_expires_at = ?, updated_at = ?
                WHERE delivery_id = ? AND state = ? AND claim_token = ?
                  AND lease_epoch = ?
                """,
                (
                    expires_at,
                    now,
                    value.delivery_id,
                    CuratorDeliveryState.CLAIMED.value,
                    value.claim_token,
                    value.lease_epoch,
                ),
            )
            if self._changes(connection) != 1:
                raise CuratorDeliveryConflictError("delivery lease renewal CAS failed")
        return CuratorDeliveryLease(
            delivery_id=value.delivery_id,
            consumer=value.consumer,
            worker_id=value.worker_id,
            claim_token=value.claim_token,
            lease_epoch=value.lease_epoch,
            attempt=value.attempt,
            expires_at=expires_at,
        ).validated()

    def acknowledge_delivery(
        self,
        lease: CuratorDeliveryLease,
        *,
        receipt: Mapping[str, Any],
        partition_key: str = "default",
    ) -> CuratorDownstreamDelivery:
        value = lease.validated()
        receipt_value = dict(receipt)
        receipt_digest = stable_digest(receipt_value)
        now = float(self.clock())
        with self.transaction(immediate=True) as connection:
            row = self._assert_delivery_lease(connection, value, now=now)
            connection.execute(
                """
                UPDATE memory_curator_downstream_deliveries
                SET state = ?, acknowledged_at = ?, updated_at = ?,
                    claimed_by = '', claim_token = '', claim_expires_at = NULL,
                    receipt_json = ?, error_code = '', error_message = ''
                WHERE delivery_id = ? AND state = ? AND claim_token = ?
                  AND lease_epoch = ?
                """,
                (
                    CuratorDeliveryState.ACKNOWLEDGED.value,
                    now,
                    now,
                    canonical_json(receipt_value),
                    value.delivery_id,
                    CuratorDeliveryState.CLAIMED.value,
                    value.claim_token,
                    value.lease_epoch,
                ),
            )
            if self._changes(connection) != 1:
                raise CuratorDeliveryConflictError("delivery acknowledgement CAS failed")
            checkpoint = connection.execute(
                """
                SELECT version, acknowledged_count
                FROM memory_curator_consumer_checkpoints
                WHERE consumer = ? AND partition_key = ?
                """,
                (value.consumer.value, partition_key),
            ).fetchone()
            outcome_id = str(row["outcome_id"])
            outcome_digest = str(row["payload_digest"])
            if checkpoint is None:
                connection.execute(
                    """
                    INSERT INTO memory_curator_consumer_checkpoints(
                        consumer, partition_key, task_id, last_delivery_id,
                        last_outcome_id, outcome_digest, receipt_digest,
                        acknowledged_count, version, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        value.consumer.value,
                        partition_key,
                        str(row["task_id"]),
                        value.delivery_id,
                        outcome_id,
                        outcome_digest,
                        receipt_digest,
                        1,
                        1,
                        now,
                    ),
                )
            else:
                version = int(checkpoint["version"])
                connection.execute(
                    """
                    UPDATE memory_curator_consumer_checkpoints
                    SET task_id = ?, last_delivery_id = ?, last_outcome_id = ?,
                        outcome_digest = ?, receipt_digest = ?,
                        acknowledged_count = ?, version = ?, updated_at = ?
                    WHERE consumer = ? AND partition_key = ? AND version = ?
                    """,
                    (
                        str(row["task_id"]),
                        value.delivery_id,
                        outcome_id,
                        outcome_digest,
                        receipt_digest,
                        int(checkpoint["acknowledged_count"]) + 1,
                        version + 1,
                        now,
                        value.consumer.value,
                        partition_key,
                        version,
                    ),
                )
                if self._changes(connection) != 1:
                    raise CuratorDeliveryConflictError("consumer checkpoint CAS failed")
            delivery = self._delivery_from_connection(connection, value.delivery_id)
            self._audit(
                connection,
                event_type="memory.curator.delivery_acknowledged",
                run_id=delivery.run_id,
                task_id=delivery.task_id,
                outcome_id=delivery.outcome_id,
                delivery_id=delivery.delivery_id,
                causation_id=delivery.outcome_id,
                payload={
                    "consumer": value.consumer.value,
                    "partition_key": partition_key,
                    "receipt_digest": receipt_digest,
                    "attempt": value.attempt,
                },
                created_at=now,
            )
            return delivery

    def fail_delivery(
        self,
        lease: CuratorDeliveryLease,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
        retry_delay_seconds: float = 1.0,
    ) -> CuratorDownstreamDelivery:
        value = lease.validated()
        code = str(error_code).strip() or "consumer_failed"
        message = str(error_message)[:2000]
        delay = max(0.0, float(retry_delay_seconds))
        now = float(self.clock())
        with self.transaction(immediate=True) as connection:
            row = self._assert_delivery_lease(connection, value, now=now)
            remaining = max(0, int(row["retry_remaining"]) - 1)
            next_state = (
                CuratorDeliveryState.RETRY_WAIT
                if retryable and remaining > 0
                else CuratorDeliveryState.DEAD
            )
            available_at = now + delay if next_state is CuratorDeliveryState.RETRY_WAIT else now
            connection.execute(
                """
                UPDATE memory_curator_downstream_deliveries
                SET state = ?, available_at = ?, retry_remaining = ?,
                    updated_at = ?, claimed_by = '', claim_token = '',
                    claim_expires_at = NULL, error_code = ?, error_message = ?
                WHERE delivery_id = ? AND state = ? AND claim_token = ?
                  AND lease_epoch = ?
                """,
                (
                    next_state.value,
                    available_at,
                    remaining,
                    now,
                    code,
                    message,
                    value.delivery_id,
                    CuratorDeliveryState.CLAIMED.value,
                    value.claim_token,
                    value.lease_epoch,
                ),
            )
            if self._changes(connection) != 1:
                raise CuratorDeliveryConflictError("delivery failure CAS failed")
            delivery = self._delivery_from_connection(connection, value.delivery_id)
            self._audit(
                connection,
                event_type="memory.curator.delivery_retry_wait"
                if next_state is CuratorDeliveryState.RETRY_WAIT
                else "memory.curator.delivery_dead",
                run_id=delivery.run_id,
                task_id=delivery.task_id,
                outcome_id=delivery.outcome_id,
                delivery_id=delivery.delivery_id,
                causation_id=delivery.outcome_id,
                payload={
                    "consumer": value.consumer.value,
                    "error_code": code,
                    "error_message": message,
                    "retryable": retryable,
                    "retry_remaining": remaining,
                    "attempt": value.attempt,
                },
                created_at=now,
            )
            return delivery

    def sweep_expired_deliveries(
        self,
        *,
        consumer: CuratorConsumer | None = None,
        limit: int = 1000,
        retry_delay_seconds: float = 0.0,
    ) -> tuple[str, ...]:
        now = float(self.clock())
        clauses = [
            "state = ?",
            "claim_expires_at IS NOT NULL",
            "claim_expires_at <= ?",
        ]
        parameters: list[Any] = [CuratorDeliveryState.CLAIMED.value, now]
        if consumer is not None:
            clauses.append("consumer = ?")
            parameters.append(consumer.value)
        parameters.append(max(0, min(int(limit), 100_000)))
        recovered: list[str] = []
        with self.transaction(immediate=True) as connection:
            rows = connection.execute(
                f"""
                SELECT delivery_id, retry_remaining, run_id, task_id,
                       outcome_id, consumer
                FROM memory_curator_downstream_deliveries
                WHERE {' AND '.join(clauses)}
                ORDER BY claim_expires_at ASC, delivery_id ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            for row in rows:
                remaining = max(0, int(row["retry_remaining"]) - 1)
                state = (
                    CuratorDeliveryState.RETRY_WAIT
                    if remaining > 0
                    else CuratorDeliveryState.DEAD
                )
                connection.execute(
                    """
                    UPDATE memory_curator_downstream_deliveries
                    SET state = ?, available_at = ?, retry_remaining = ?,
                        updated_at = ?, claimed_by = '', claim_token = '',
                        claim_expires_at = NULL, error_code = ?, error_message = ?
                    WHERE delivery_id = ? AND state = ? AND claim_expires_at <= ?
                    """,
                    (
                        state.value,
                        now + max(0.0, float(retry_delay_seconds)),
                        remaining,
                        now,
                        "lease_expired",
                        "downstream consumer lease expired before acknowledgement",
                        str(row["delivery_id"]),
                        CuratorDeliveryState.CLAIMED.value,
                        now,
                    ),
                )
                if self._changes(connection) != 1:
                    continue
                delivery_id = str(row["delivery_id"])
                recovered.append(delivery_id)
                self._audit(
                    connection,
                    event_type="memory.curator.delivery_lease_expired",
                    run_id=str(row["run_id"]),
                    task_id=str(row["task_id"]),
                    outcome_id=str(row["outcome_id"]),
                    delivery_id=delivery_id,
                    causation_id=str(row["outcome_id"]),
                    payload={
                        "consumer": str(row["consumer"]),
                        "next_state": state.value,
                        "retry_remaining": remaining,
                    },
                    created_at=now,
                )
        return tuple(recovered)

    def cancel_delivery(
        self,
        delivery_id: str,
        *,
        reason: str,
    ) -> CuratorDownstreamDelivery:
        now = float(self.clock())
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT state FROM memory_curator_downstream_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise KeyError(delivery_id)
            current = CuratorDeliveryState(str(row["state"]))
            if current is CuratorDeliveryState.ACKNOWLEDGED:
                raise CuratorDeliveryConflictError(
                    "acknowledged delivery cannot be cancelled"
                )
            if current is not CuratorDeliveryState.CANCELLED:
                connection.execute(
                    """
                    UPDATE memory_curator_downstream_deliveries
                    SET state = ?, updated_at = ?, claimed_by = '',
                        claim_token = '', claim_expires_at = NULL,
                        error_code = ?, error_message = ?
                    WHERE delivery_id = ? AND state = ?
                    """,
                    (
                        CuratorDeliveryState.CANCELLED.value,
                        now,
                        "cancelled",
                        str(reason)[:2000],
                        delivery_id,
                        current.value,
                    ),
                )
                if self._changes(connection) != 1:
                    raise CuratorDeliveryConflictError("delivery cancellation CAS failed")
            return self._delivery_from_connection(connection, delivery_id)

    def delivery(self, delivery_id: str) -> CuratorDownstreamDelivery | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT delivery_id FROM memory_curator_downstream_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                return None
            return self._delivery_from_connection(connection, delivery_id)

    def deliveries(
        self,
        *,
        task_id: str = "",
        outcome_id: str = "",
        consumer: CuratorConsumer | None = None,
        states: Sequence[CuratorDeliveryState] = (),
        limit: int = 1000,
    ) -> tuple[CuratorDownstreamDelivery, ...]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if outcome_id:
            clauses.append("outcome_id = ?")
            parameters.append(outcome_id)
        if consumer is not None:
            clauses.append("consumer = ?")
            parameters.append(consumer.value)
        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            parameters.extend(state.value for state in states)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(0, min(int(limit), 100_000)))
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT delivery_id
                FROM memory_curator_downstream_deliveries
                {where}
                ORDER BY created_at DESC, delivery_id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            return tuple(
                self._delivery_from_connection(connection, str(row["delivery_id"]))
                for row in rows
            )

    def consumer_checkpoint(
        self,
        consumer: CuratorConsumer,
        *,
        partition_key: str = "default",
    ) -> Mapping[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_curator_consumer_checkpoints
                WHERE consumer = ? AND partition_key = ?
                """,
                (consumer.value, partition_key),
            ).fetchone()
        if row is None:
            return {
                "consumer": consumer.value,
                "partition_key": partition_key,
                "task_id": "",
                "last_delivery_id": "",
                "last_outcome_id": "",
                "outcome_digest": "",
                "receipt_digest": "",
                "acknowledged_count": 0,
                "version": 0,
                "updated_at": 0.0,
            }
        return dict(row)

    def record_context_proof(
        self,
        proof: CuratorContextProof,
    ) -> tuple[CuratorContextProof, bool]:
        value = proof.validated()
        now = float(self.clock())
        with self.transaction(immediate=True) as connection:
            outcome = connection.execute(
                """
                SELECT memory_id, memory_revision, canonical_memory_changed
                FROM memory_curator_outcomes
                WHERE outcome_id = ?
                """,
                (value.outcome_id,),
            ).fetchone()
            if outcome is None:
                raise CuratorProjectionConflictError("context proof outcome is missing")
            if not bool(outcome["canonical_memory_changed"]):
                raise CuratorProjectionConflictError(
                    "context proof cannot bind to a non-canonical outcome"
                )
            if (
                str(outcome["memory_id"]) != value.memory_id
                or int(outcome["memory_revision"]) != value.memory_revision
            ):
                raise CuratorProjectionConflictError(
                    "context proof memory identity differs from outcome"
                )
            existing = connection.execute(
                "SELECT context_digest FROM memory_curator_context_proofs WHERE proof_id = ?",
                (value.proof_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["context_digest"]) != value.context_digest:
                    raise CuratorProjectionConflictError(
                        "context proof identity reused with another digest"
                    )
                return self._context_proof_from_connection(connection, value.proof_id), False
            connection.execute(
                """
                INSERT INTO memory_curator_context_proofs(
                    proof_id, run_id, task_id, outcome_id, memory_id,
                    memory_revision, query_id, query_digest, index_scope,
                    index_generation, index_revision, retrieved_memory_ids_json,
                    context_digest, effect, consumer, created_at_text,
                    worker_request_id, session_id, message_count, total_chars,
                    contains_index_dump, canonical_owner, index_owner,
                    metadata_json, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    value.proof_id,
                    value.run_id,
                    value.task_id,
                    value.outcome_id,
                    value.memory_id,
                    value.memory_revision,
                    value.query_id,
                    value.query_digest,
                    value.index_scope,
                    value.index_generation,
                    value.index_revision,
                    canonical_json(value.retrieved_memory_ids),
                    value.context_digest,
                    value.effect.value,
                    value.consumer.value,
                    value.created_at,
                    value.worker_request_id,
                    value.session_id,
                    value.message_count,
                    value.total_chars,
                    int(value.contains_index_dump),
                    value.canonical_owner,
                    value.index_owner,
                    canonical_json(value.metadata),
                    now,
                ),
            )
            self._audit(
                connection,
                event_type="memory.curator.context_verified"
                if value.verified
                else "memory.curator.context_absent",
                run_id=value.run_id,
                task_id=value.task_id,
                outcome_id=value.outcome_id,
                causation_id=value.query_id,
                payload=value.to_dict(),
                created_at=now,
            )
            return value, True

    def context_proof(self, proof_id: str) -> CuratorContextProof | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT proof_id FROM memory_curator_context_proofs WHERE proof_id = ?",
                (proof_id,),
            ).fetchone()
            if row is None:
                return None
            return self._context_proof_from_connection(connection, proof_id)

    def context_proofs(
        self,
        *,
        task_id: str = "",
        outcome_id: str = "",
        verified_only: bool = False,
        limit: int = 1000,
    ) -> tuple[CuratorContextProof, ...]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if outcome_id:
            clauses.append("outcome_id = ?")
            parameters.append(outcome_id)
        if verified_only:
            clauses.append("effect = 'present'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(0, min(int(limit), 100_000)))
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT proof_id
                FROM memory_curator_context_proofs
                {where}
                ORDER BY stored_at DESC, proof_id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            return tuple(
                self._context_proof_from_connection(connection, str(row["proof_id"]))
                for row in rows
            )

    def audit_events(
        self,
        *,
        task_id: str = "",
        integration_run_id: str = "",
        limit: int = 1000,
    ) -> tuple[Mapping[str, Any], ...]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if integration_run_id:
            clauses.append("integration_run_id = ?")
            parameters.append(integration_run_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(0, min(int(limit), 100_000)))
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM memory_curator_integration_audit
                {where}
                ORDER BY created_at ASC, audit_id ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(
            {
                **dict(row),
                "payload": self._json_mapping(row["payload_json"]),
            }
            for row in rows
        )

    def consistency_report(self, *, task_id: str = "") -> Mapping[str, Any]:
        clauses = "WHERE task_id = ?" if task_id else ""
        parameters: tuple[Any, ...] = (task_id,) if task_id else ()
        with self.connection() as connection:
            outcome_rows = connection.execute(
                f"""
                SELECT outcome_id, state, canonical_memory_changed, memory_id,
                       memory_revision, commit_receipt_id, index_published
                FROM memory_curator_outcomes
                {clauses}
                """,
                parameters,
            ).fetchall()
            missing_memories: list[str] = []
            invalid_outcomes: list[str] = []
            for row in outcome_rows:
                outcome_id = str(row["outcome_id"])
                if bool(row["canonical_memory_changed"]):
                    if (
                        not str(row["memory_id"])
                        or int(row["memory_revision"]) <= 0
                        or not str(row["commit_receipt_id"])
                    ):
                        invalid_outcomes.append(outcome_id)
                        continue
                    memory = connection.execute(
                        "SELECT memory_id FROM memory_records WHERE memory_id = ?",
                        (str(row["memory_id"]),),
                    ).fetchone()
                    if memory is None:
                        missing_memories.append(outcome_id)
            orphan_deliveries = connection.execute(
                f"""
                SELECT d.delivery_id
                FROM memory_curator_downstream_deliveries d
                LEFT JOIN memory_curator_outcomes o ON o.outcome_id = d.outcome_id
                WHERE o.outcome_id IS NULL
                {"AND d.task_id = ?" if task_id else ""}
                """,
                parameters,
            ).fetchall()
            missing_target_deliveries: list[str] = []
            for row in outcome_rows:
                outcome = self._outcome_from_connection(connection, str(row["outcome_id"]))
                if outcome.state is not CuratorOutcomeState.PUBLISHED:
                    continue
                existing = {
                    CuratorConsumer(str(item["consumer"]))
                    for item in connection.execute(
                        "SELECT consumer FROM memory_curator_downstream_deliveries WHERE outcome_id = ?",
                        (outcome.outcome_id,),
                    ).fetchall()
                }
                if set(outcome.target_consumers) - existing:
                    missing_target_deliveries.append(outcome.outcome_id)
            stale_claims = connection.execute(
                f"""
                SELECT delivery_id
                FROM memory_curator_downstream_deliveries
                WHERE state = ? AND claim_expires_at <= ?
                {"AND task_id = ?" if task_id else ""}
                """,
                (
                    CuratorDeliveryState.CLAIMED.value,
                    float(self.clock()),
                    *parameters,
                ),
            ).fetchall()
        findings = {
            "invalid_outcome_ids": invalid_outcomes,
            "missing_canonical_memory_outcome_ids": missing_memories,
            "orphan_delivery_ids": [str(row["delivery_id"]) for row in orphan_deliveries],
            "missing_target_delivery_outcome_ids": missing_target_deliveries,
            "stale_claim_delivery_ids": [str(row["delivery_id"]) for row in stale_claims],
        }
        return {
            "ok": not any(findings.values()),
            "task_id": task_id,
            "outcome_count": len(outcome_rows),
            "findings": findings,
            "canonical_memory_owner": "SQLiteStore.memory_records",
            "projection_store_owner": "CuratorIntegrationStore",
            "projection_store_is_canonical_memory": False,
        }

    def health(self, *, task_id: str = "") -> Mapping[str, Any]:
        filter_sql = "WHERE task_id = ?" if task_id else ""
        parameters: tuple[Any, ...] = (task_id,) if task_id else ()
        with self.connection() as connection:
            table_counts = {
                "input_batches": int(
                    connection.execute(
                        f"SELECT COUNT(*) AS count FROM memory_curator_input_batches {filter_sql}",
                        parameters,
                    ).fetchone()["count"]
                ),
                "integration_runs": int(
                    connection.execute(
                        f"SELECT COUNT(*) AS count FROM memory_curator_integration_runs {filter_sql}",
                        parameters,
                    ).fetchone()["count"]
                ),
                "outcomes": int(
                    connection.execute(
                        f"SELECT COUNT(*) AS count FROM memory_curator_outcomes {filter_sql}",
                        parameters,
                    ).fetchone()["count"]
                ),
                "failures": int(
                    connection.execute(
                        f"SELECT COUNT(*) AS count FROM memory_curator_failure_contracts {filter_sql}",
                        parameters,
                    ).fetchone()["count"]
                ),
                "deliveries": int(
                    connection.execute(
                        f"SELECT COUNT(*) AS count FROM memory_curator_downstream_deliveries {filter_sql}",
                        parameters,
                    ).fetchone()["count"]
                ),
                "context_proofs": int(
                    connection.execute(
                        f"SELECT COUNT(*) AS count FROM memory_curator_context_proofs {filter_sql}",
                        parameters,
                    ).fetchone()["count"]
                ),
            }
            pending = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) AS count
                    FROM memory_curator_downstream_deliveries
                    WHERE state IN (?, ?, ?)
                    {"AND task_id = ?" if task_id else ""}
                    """,
                    (
                        CuratorDeliveryState.PENDING.value,
                        CuratorDeliveryState.RETRY_WAIT.value,
                        CuratorDeliveryState.CLAIMED.value,
                        *parameters,
                    ),
                ).fetchone()["count"]
            )
            dead = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) AS count
                    FROM memory_curator_downstream_deliveries
                    WHERE state = ?
                    {"AND task_id = ?" if task_id else ""}
                    """,
                    (CuratorDeliveryState.DEAD.value, *parameters),
                ).fetchone()["count"]
            )
        consistency = self.consistency_report(task_id=task_id)
        return {
            "ok": consistency["ok"] and dead == 0,
            "task_id": task_id,
            "path": str(self.path),
            "counts": table_counts,
            "pending_deliveries": pending,
            "dead_deliveries": dead,
            "consistency": consistency,
            "canonical_memory_owner": "SQLiteStore.memory_records",
            "projection_store_owner": "CuratorIntegrationStore",
            "projection_store_is_canonical_memory": False,
            "external_database_dependency": False,
        }

    def _assert_delivery_lease(
        self,
        connection: sqlite3.Connection,
        lease: CuratorDeliveryLease,
        *,
        now: float,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM memory_curator_downstream_deliveries WHERE delivery_id = ?",
            (lease.delivery_id,),
        ).fetchone()
        if row is None:
            raise CuratorDeliveryConflictError("delivery does not exist")
        if CuratorConsumer(str(row["consumer"])) is not lease.consumer:
            raise CuratorDeliveryConflictError("delivery lease consumer mismatch")
        if CuratorDeliveryState(str(row["state"])) is not CuratorDeliveryState.CLAIMED:
            raise CuratorDeliveryConflictError("delivery is not claimed")
        if str(row["claimed_by"]) != lease.worker_id:
            raise CuratorDeliveryConflictError("delivery lease owner mismatch")
        if str(row["claim_token"]) != lease.claim_token:
            raise CuratorDeliveryConflictError("delivery claim token mismatch")
        if int(row["lease_epoch"]) != lease.lease_epoch:
            raise CuratorDeliveryConflictError("delivery lease epoch mismatch")
        if row["claim_expires_at"] is None or float(row["claim_expires_at"]) <= now:
            raise CuratorDeliveryConflictError("delivery lease expired")
        return row

    def _insert_integration_run(
        self,
        connection: sqlite3.Connection,
        run: CuratorIntegrationRun,
        *,
        state_version: int,
        stored_at: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO memory_curator_integration_runs(
                integration_run_id, run_id, task_id, curator_job_id,
                curator_request_id, state, input_batch_id, input_digest,
                started_at_text, updated_at_text, outcome_ids_json,
                failure_ids_json, delivery_ids_json, context_proof_ids_json,
                model_status, canonical_commit_count, index_publication_count,
                error_code, error_message, metadata_json, state_version, stored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._integration_run_values(run, state_version=state_version, stored_at=stored_at),
        )

    def _update_integration_run(
        self,
        connection: sqlite3.Connection,
        run: CuratorIntegrationRun,
        *,
        expected_version: int,
        next_version: int,
        stored_at: float,
    ) -> None:
        values = self._integration_run_values(
            run,
            state_version=next_version,
            stored_at=stored_at,
        )
        connection.execute(
            """
            UPDATE memory_curator_integration_runs
            SET run_id = ?, task_id = ?, curator_job_id = ?,
                curator_request_id = ?, state = ?, input_batch_id = ?,
                input_digest = ?, started_at_text = ?, updated_at_text = ?,
                outcome_ids_json = ?, failure_ids_json = ?, delivery_ids_json = ?,
                context_proof_ids_json = ?, model_status = ?,
                canonical_commit_count = ?, index_publication_count = ?,
                error_code = ?, error_message = ?, metadata_json = ?,
                state_version = ?, stored_at = ?
            WHERE integration_run_id = ? AND state_version = ?
            """,
            (
                *values[1:4],
                *values[4:21],
                values[21],
                run.integration_run_id,
                expected_version,
            ),
        )
        if self._changes(connection) != 1:
            raise CuratorProjectionConflictError("integration run CAS failed")

    @staticmethod
    def _integration_run_values(
        run: CuratorIntegrationRun,
        *,
        state_version: int,
        stored_at: float,
    ) -> tuple[Any, ...]:
        return (
            run.integration_run_id,
            run.run_id,
            run.task_id,
            run.curator_job_id,
            run.curator_request_id,
            run.state.value,
            run.input_batch_id,
            run.input_digest,
            run.started_at,
            run.updated_at,
            canonical_json(run.outcome_ids),
            canonical_json(run.failure_ids),
            canonical_json(run.delivery_ids),
            canonical_json(run.context_proof_ids),
            run.model_status,
            run.canonical_commit_count,
            run.index_publication_count,
            run.error_code,
            run.error_message,
            canonical_json(run.metadata),
            state_version,
            stored_at,
        )

    def _input_batch_from_connection(
        self,
        connection: sqlite3.Connection,
        batch_id: str,
    ) -> CuratorInputBatch:
        row = connection.execute(
            "SELECT * FROM memory_curator_input_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise KeyError(batch_id)
        refs = connection.execute(
            """
            SELECT * FROM memory_curator_input_refs
            WHERE batch_id = ?
            ORDER BY source_sequence ASC, source ASC, source_id ASC, ref_id ASC
            """,
            (batch_id,),
        ).fetchall()
        return CuratorInputBatch(
            batch_id=str(row["batch_id"]),
            run_id=str(row["run_id"]),
            task_id=str(row["task_id"]),
            refs=tuple(self._runtime_trace_ref(item) for item in refs),
            source_watermark=int(row["source_watermark"]),
            next_watermark=int(row["next_watermark"]),
            high_watermark=int(row["high_watermark"]),
            batch_digest=str(row["batch_digest"]),
            has_more=bool(row["has_more"]),
            created_at=str(row["created_at_text"]),
            legacy_event_count=int(row["legacy_event_count"]),
            runtime_event_count=int(row["runtime_event_count"]),
            artifact_count=int(row["artifact_count"]),
            duplicate_source_ids=self._json_strings(row["duplicate_source_ids_json"]),
            warnings=self._json_strings(row["warnings_json"]),
            metadata=self._json_mapping(row["metadata_json"]),
        ).validated()

    def _outcome_from_connection(
        self,
        connection: sqlite3.Connection,
        outcome_id: str,
    ) -> CuratorOutcome:
        row = connection.execute(
            "SELECT * FROM memory_curator_outcomes WHERE outcome_id = ?",
            (outcome_id,),
        ).fetchone()
        if row is None:
            raise KeyError(outcome_id)
        return self._outcome(row)

    def _failure_from_connection(
        self,
        connection: sqlite3.Connection,
        failure_id: str,
    ) -> CuratorFailureContract:
        row = connection.execute(
            "SELECT * FROM memory_curator_failure_contracts WHERE failure_id = ?",
            (failure_id,),
        ).fetchone()
        if row is None:
            raise KeyError(failure_id)
        return self._failure(row)

    def _delivery_from_connection(
        self,
        connection: sqlite3.Connection,
        delivery_id: str,
    ) -> CuratorDownstreamDelivery:
        row = connection.execute(
            "SELECT * FROM memory_curator_downstream_deliveries WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        return self._delivery(row)

    def _context_proof_from_connection(
        self,
        connection: sqlite3.Connection,
        proof_id: str,
    ) -> CuratorContextProof:
        row = connection.execute(
            "SELECT * FROM memory_curator_context_proofs WHERE proof_id = ?",
            (proof_id,),
        ).fetchone()
        if row is None:
            raise KeyError(proof_id)
        return self._context_proof(row)

    @staticmethod
    def _runtime_trace_ref(row: sqlite3.Row) -> RuntimeTraceRef:
        return RuntimeTraceRef.from_dict(
            {
                "ref_id": row["ref_id"],
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "source": row["source"],
                "kind": row["kind"],
                "source_id": row["source_id"],
                "source_sequence": row["source_sequence"],
                "content_digest": row["content_digest"],
                "occurred_at": row["occurred_at_text"],
                "event_type": row["event_type"],
                "aggregate_id": row["aggregate_id"],
                "causation_id": row["causation_id"],
                "correlation_id": row["correlation_id"],
                "producer": row["producer"],
                "summary": row["summary"],
                "artifact_ids": CuratorIntegrationStore._json_strings(
                    row["artifact_ids_json"]
                ),
                "trusted_runtime": bool(row["trusted_runtime"]),
                "canonical": bool(row["canonical"]),
                "metadata": CuratorIntegrationStore._json_mapping(row["metadata_json"]),
            }
        )

    @staticmethod
    def _integration_run(row: sqlite3.Row) -> CuratorIntegrationRun:
        return CuratorIntegrationRun.from_dict(
            {
                "integration_run_id": row["integration_run_id"],
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "curator_job_id": row["curator_job_id"],
                "curator_request_id": row["curator_request_id"],
                "state": row["state"],
                "input_batch_id": row["input_batch_id"],
                "input_digest": row["input_digest"],
                "started_at": row["started_at_text"],
                "updated_at": row["updated_at_text"],
                "outcome_ids": CuratorIntegrationStore._json_strings(
                    row["outcome_ids_json"]
                ),
                "failure_ids": CuratorIntegrationStore._json_strings(
                    row["failure_ids_json"]
                ),
                "delivery_ids": CuratorIntegrationStore._json_strings(
                    row["delivery_ids_json"]
                ),
                "context_proof_ids": CuratorIntegrationStore._json_strings(
                    row["context_proof_ids_json"]
                ),
                "model_status": row["model_status"],
                "canonical_commit_count": row["canonical_commit_count"],
                "index_publication_count": row["index_publication_count"],
                "error_code": row["error_code"],
                "error_message": row["error_message"],
                "metadata": CuratorIntegrationStore._json_mapping(row["metadata_json"]),
            }
        )

    @staticmethod
    def _outcome(row: sqlite3.Row) -> CuratorOutcome:
        return CuratorOutcome.from_dict(
            {
                "outcome_id": row["outcome_id"],
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "curator_job_id": row["curator_job_id"],
                "curator_request_id": row["curator_request_id"],
                "kind": row["kind"],
                "state": row["state"],
                "candidate_id": row["candidate_id"],
                "decision_id": row["decision_id"],
                "evidence_bundle_id": row["evidence_bundle_id"],
                "evidence_digest": row["evidence_digest"],
                "subject": row["subject"],
                "summary": row["summary"],
                "payload": CuratorIntegrationStore._json_mapping(row["payload_json"]),
                "target_consumers": CuratorIntegrationStore._json_strings(
                    row["target_consumers_json"]
                ),
                "outcome_digest": row["outcome_digest"],
                "created_at": row["created_at_text"],
                "memory_id": row["memory_id"],
                "memory_revision": row["memory_revision"],
                "commit_receipt_id": row["commit_receipt_id"],
                "causation_id": row["causation_id"],
                "model_assisted": bool(row["model_assisted"]),
                "deterministic_validation": bool(row["deterministic_validation"]),
                "canonical_memory_changed": bool(row["canonical_memory_changed"]),
                "index_published": bool(row["index_published"]),
                "supersedes_outcome_id": row["supersedes_outcome_id"],
                "metadata": CuratorIntegrationStore._json_mapping(row["metadata_json"]),
            }
        )

    @staticmethod
    def _failure(row: sqlite3.Row) -> CuratorFailureContract:
        return CuratorFailureContract.from_dict(
            {
                "failure_id": row["failure_id"],
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "curator_job_id": row["curator_job_id"],
                "phase": row["phase"],
                "code": row["code"],
                "message": row["message"],
                "severity": row["severity"],
                "disposition": row["disposition"],
                "retryable": bool(row["retryable"]),
                "attempt": row["attempt"],
                "causation_id": row["causation_id"],
                "created_at": row["created_at_text"],
                "candidate_id": row["candidate_id"],
                "decision_id": row["decision_id"],
                "outcome_id": row["outcome_id"],
                "evidence_ids": CuratorIntegrationStore._json_strings(
                    row["evidence_ids_json"]
                ),
                "consumer_hints": CuratorIntegrationStore._json_strings(
                    row["consumer_hints_json"]
                ),
                "canonical_memory_changed": bool(row["canonical_memory_changed"]),
                "operator_action_required": bool(row["operator_action_required"]),
                "details": CuratorIntegrationStore._json_mapping(row["details_json"]),
            }
        )

    @staticmethod
    def _delivery(row: sqlite3.Row) -> CuratorDownstreamDelivery:
        return CuratorDownstreamDelivery.from_dict(
            {
                "delivery_id": row["delivery_id"],
                "outcome_id": row["outcome_id"],
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "consumer": row["consumer"],
                "state": row["state"],
                "payload_digest": row["payload_digest"],
                "payload": CuratorIntegrationStore._json_mapping(row["payload_json"]),
                "available_at": row["available_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "attempt": row["attempt"],
                "retry_remaining": row["retry_remaining"],
                "claimed_by": row["claimed_by"],
                "claim_token": row["claim_token"],
                "lease_epoch": row["lease_epoch"],
                "claim_expires_at": row["claim_expires_at"],
                "acknowledged_at": row["acknowledged_at"],
                "error_code": row["error_code"],
                "error_message": row["error_message"],
                "receipt": CuratorIntegrationStore._json_mapping(row["receipt_json"]),
            }
        )

    @staticmethod
    def _context_proof(row: sqlite3.Row) -> CuratorContextProof:
        return CuratorContextProof.from_dict(
            {
                "proof_id": row["proof_id"],
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "outcome_id": row["outcome_id"],
                "memory_id": row["memory_id"],
                "memory_revision": row["memory_revision"],
                "query_id": row["query_id"],
                "query_digest": row["query_digest"],
                "index_scope": row["index_scope"],
                "index_generation": row["index_generation"],
                "index_revision": row["index_revision"],
                "retrieved_memory_ids": CuratorIntegrationStore._json_strings(
                    row["retrieved_memory_ids_json"]
                ),
                "context_digest": row["context_digest"],
                "effect": row["effect"],
                "consumer": row["consumer"],
                "created_at": row["created_at_text"],
                "worker_request_id": row["worker_request_id"],
                "session_id": row["session_id"],
                "message_count": row["message_count"],
                "total_chars": row["total_chars"],
                "contains_index_dump": bool(row["contains_index_dump"]),
                "canonical_owner": row["canonical_owner"],
                "index_owner": row["index_owner"],
                "metadata": CuratorIntegrationStore._json_mapping(row["metadata_json"]),
            }
        )

    def _audit(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        run_id: str,
        task_id: str,
        integration_run_id: str = "",
        outcome_id: str = "",
        delivery_id: str = "",
        causation_id: str,
        payload: Mapping[str, Any],
        created_at: float,
    ) -> None:
        digest = stable_digest(payload)
        audit_id = stable_id(
            "curator_integration_audit",
            event_type,
            run_id,
            task_id,
            integration_run_id,
            outcome_id,
            delivery_id,
            causation_id,
            digest,
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO memory_curator_integration_audit(
                audit_id, event_type, run_id, task_id, integration_run_id,
                outcome_id, delivery_id, causation_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                event_type,
                run_id,
                task_id,
                integration_run_id,
                outcome_id,
                delivery_id,
                causation_id,
                canonical_json(payload),
                created_at,
            ),
        )

    @staticmethod
    def _json_mapping(value: object) -> dict[str, Any]:
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}

    @staticmethod
    def _json_strings(value: object) -> tuple[str, ...]:
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
        if not isinstance(parsed, Sequence) or isinstance(parsed, (str, bytes, bytearray)):
            return ()
        return tuple(dict.fromkeys(str(item) for item in parsed if str(item)))

    @staticmethod
    def _changes(connection: sqlite3.Connection) -> int:
        return int(connection.execute("SELECT changes() AS count").fetchone()["count"])


__all__ = ["CuratorIntegrationStore"]
