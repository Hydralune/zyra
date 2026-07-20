from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .query_contract import IndexCheckpointRef, RetrievalSnapshotRef
from .retrieval_models import stable_digest, stable_identifier
from .retrieval_store import SQLiteRetrievalIndex


class AdmissionState(StrEnum):
    ADMITTED = "admitted"
    ENQUEUED = "enqueued"
    PUBLISHED = "published"
    REJECTED = "rejected"


class DeliveryState(StrEnum):
    CLAIMED = "claimed"
    COMMITTED = "committed"
    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class SourceAdmission:
    admission_id: str
    task_id: str
    source_kind: str
    source_id: str
    source_revision: str
    content_digest: str
    event_id: str
    causation_id: str
    state: AdmissionState
    job_id: str = ""
    generation: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    error_code: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "admission_id": self.admission_id,
            "task_id": self.task_id,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "content_digest": self.content_digest,
            "event_id": self.event_id,
            "causation_id": self.causation_id,
            "state": self.state.value,
            "job_id": self.job_id,
            "generation": self.generation,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error_code": self.error_code,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QueryProvenanceRecord:
    query_id: str
    task_id: str
    consumer: str
    request_id: str
    query_fingerprint: str
    index_scope: str
    index_generation: int
    index_revision: str
    mode: str
    source_ref_count: int
    result_digest: str
    vector_status: str
    created_at: float
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "task_id": self.task_id,
            "consumer": self.consumer,
            "request_id": self.request_id,
            "query_fingerprint": self.query_fingerprint,
            "index_scope": self.index_scope,
            "index_generation": self.index_generation,
            "index_revision": self.index_revision,
            "mode": self.mode,
            "source_ref_count": self.source_ref_count,
            "result_digest": self.result_digest,
            "vector_status": self.vector_status,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
            "raw_query_persisted": False,
        }


@dataclass(frozen=True, slots=True)
class ContextDeliveryRecord:
    delivery_id: str
    worker_request_id: str
    run_id: str
    task_id: str
    session_id: str
    state: DeliveryState
    query_ids: tuple[str, ...]
    message_ids: tuple[str, ...]
    source_digest: str
    claimed_at: float
    updated_at: float
    terminal_event_ids: tuple[str, ...] = ()
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "worker_request_id": self.worker_request_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "state": self.state.value,
            "query_ids": list(self.query_ids),
            "message_ids": list(self.message_ids),
            "source_digest": self.source_digest,
            "claimed_at": self.claimed_at,
            "updated_at": self.updated_at,
            "terminal_event_ids": list(self.terminal_event_ids),
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


class RetrievalIntegrationStore:
    """Derived consumption, delivery and provenance state.

    The tables live beside the retrieval index so deleting that database also
    deletes these cursors.  They never contain canonical MemoryRecord bodies or
    an FTS/vector dump and can be recreated from MemoryRecordStore plus the 05C
    event stream.
    """

    def __init__(self, index: SQLiteRetrievalIndex) -> None:
        self.index = index
        self.initialize()

    def initialize(self) -> None:
        self.index.initialize()
        with self.index.transaction(immediate=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS retrieval_source_admissions (
                    admission_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    causation_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    job_id TEXT NOT NULL DEFAULT '',
                    generation INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    error_code TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL,
                    UNIQUE(task_id, source_kind, source_id, source_revision, content_digest)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_retrieval_admission_event
                    ON retrieval_source_admissions(event_id)
                    WHERE event_id <> '';
                CREATE INDEX IF NOT EXISTS idx_retrieval_admission_task_state
                    ON retrieval_source_admissions(task_id, state, updated_at);
                CREATE INDEX IF NOT EXISTS idx_retrieval_admission_job
                    ON retrieval_source_admissions(job_id)
                    WHERE job_id <> '';

                CREATE TABLE IF NOT EXISTS retrieval_query_provenance (
                    query_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    consumer TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    query_fingerprint TEXT NOT NULL,
                    index_scope TEXT NOT NULL,
                    index_generation INTEGER NOT NULL,
                    index_revision TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    source_ref_count INTEGER NOT NULL,
                    result_digest TEXT NOT NULL,
                    vector_status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    metadata_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_retrieval_query_task_consumer
                    ON retrieval_query_provenance(task_id, consumer, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_retrieval_query_request
                    ON retrieval_query_provenance(request_id)
                    WHERE request_id <> '';

                CREATE TABLE IF NOT EXISTS retrieval_context_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    worker_request_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    query_ids_json TEXT NOT NULL,
                    message_ids_json TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    claimed_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    terminal_event_ids_json TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_retrieval_delivery_task_state
                    ON retrieval_context_deliveries(task_id, state, updated_at);

                CREATE TABLE IF NOT EXISTS retrieval_checkpoint_refs (
                    task_id TEXT PRIMARY KEY,
                    source_revision TEXT NOT NULL,
                    desired_generation INTEGER NOT NULL,
                    published_generation INTEGER NOT NULL,
                    pending_job_ids_json TEXT NOT NULL,
                    last_query_ids_json TEXT NOT NULL,
                    cursor_refs_json TEXT NOT NULL,
                    checkpoint_digest TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    def admit_source(
        self,
        *,
        task_id: str,
        source_kind: str,
        source_id: str,
        source_revision: str,
        content_digest: str,
        event_id: str = "",
        causation_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[SourceAdmission, bool]:
        required = {
            "task_id": task_id,
            "source_kind": source_kind,
            "source_id": source_id,
            "source_revision": source_revision,
            "content_digest": content_digest,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(f"source admission missing: {', '.join(missing)}")
        admission_id = stable_identifier(
            "admission",
            task_id,
            source_kind,
            source_id,
            source_revision,
            content_digest,
        )
        now = time.time()
        with self.index.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO retrieval_source_admissions(
                    admission_id, task_id, source_kind, source_id,
                    source_revision, content_digest, event_id, causation_id,
                    state, created_at, updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    admission_id,
                    task_id,
                    source_kind,
                    source_id,
                    source_revision,
                    content_digest,
                    event_id,
                    causation_id,
                    AdmissionState.ADMITTED.value,
                    now,
                    now,
                    self._json(metadata or {}),
                ),
            )
            inserted = int(connection.execute("SELECT changes() AS count").fetchone()["count"]) == 1
            row = connection.execute(
                "SELECT * FROM retrieval_source_admissions WHERE admission_id = ?",
                (admission_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("source admission disappeared after write")
        return self._source_admission(row), inserted

    def bind_job(self, admission_id: str, *, job_id: str, generation: int) -> SourceAdmission:
        if not job_id or generation < 1:
            raise ValueError("job binding requires job_id and positive generation")
        now = time.time()
        with self.index.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM retrieval_source_admissions WHERE admission_id = ?",
                (admission_id,),
            ).fetchone()
            if row is None:
                raise KeyError(admission_id)
            state = AdmissionState(str(row["state"]))
            if state is AdmissionState.REJECTED:
                raise RuntimeError("rejected source admission cannot bind a job")
            if row["job_id"] and (
                str(row["job_id"]) != job_id or int(row["generation"]) != generation
            ):
                raise RuntimeError("source admission already references another job generation")
            connection.execute(
                """
                UPDATE retrieval_source_admissions
                SET state = ?, job_id = ?, generation = ?, updated_at = ?
                WHERE admission_id = ?
                """,
                (AdmissionState.ENQUEUED.value, job_id, generation, now, admission_id),
            )
            updated = connection.execute(
                "SELECT * FROM retrieval_source_admissions WHERE admission_id = ?",
                (admission_id,),
            ).fetchone()
        return self._source_admission(updated)

    def mark_published(self, job_id: str, *, generation: int) -> tuple[SourceAdmission, ...]:
        now = time.time()
        with self.index.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE retrieval_source_admissions
                SET state = ?, generation = ?, updated_at = ?, error_code = ''
                WHERE job_id = ? AND generation <= ? AND state <> ?
                """,
                (
                    AdmissionState.PUBLISHED.value,
                    generation,
                    now,
                    job_id,
                    generation,
                    AdmissionState.REJECTED.value,
                ),
            )
            rows = connection.execute(
                "SELECT * FROM retrieval_source_admissions WHERE job_id = ? ORDER BY created_at",
                (job_id,),
            ).fetchall()
        return tuple(self._source_admission(row) for row in rows)

    def reject(self, admission_id: str, *, error_code: str) -> SourceAdmission:
        if not error_code:
            raise ValueError("error_code is required")
        with self.index.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE retrieval_source_admissions
                SET state = ?, error_code = ?, updated_at = ?
                WHERE admission_id = ?
                """,
                (AdmissionState.REJECTED.value, error_code, time.time(), admission_id),
            )
            row = connection.execute(
                "SELECT * FROM retrieval_source_admissions WHERE admission_id = ?",
                (admission_id,),
            ).fetchone()
        if row is None:
            raise KeyError(admission_id)
        return self._source_admission(row)

    def source_admissions(
        self,
        *,
        task_id: str = "",
        state: AdmissionState | None = None,
        limit: int = 1_000,
    ) -> tuple[SourceAdmission, ...]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if state is not None:
            clauses.append("state = ?")
            parameters.append(state.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(0, min(int(limit), 10_000)))
        self.initialize()
        with self.index.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM retrieval_source_admissions
                {where}
                ORDER BY created_at DESC, admission_id DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(self._source_admission(row) for row in rows)

    def record_query(
        self,
        snapshot: RetrievalSnapshotRef,
        *,
        request_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> QueryProvenanceRecord:
        now = time.time()
        with self.index.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO retrieval_query_provenance(
                    query_id, task_id, consumer, request_id,
                    query_fingerprint, index_scope, index_generation,
                    index_revision, mode, source_ref_count, result_digest,
                    vector_status, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(query_id) DO UPDATE SET
                    request_id = excluded.request_id,
                    index_generation = excluded.index_generation,
                    index_revision = excluded.index_revision,
                    mode = excluded.mode,
                    source_ref_count = excluded.source_ref_count,
                    result_digest = excluded.result_digest,
                    vector_status = excluded.vector_status,
                    metadata_json = excluded.metadata_json
                """,
                (
                    snapshot.query_id,
                    snapshot.task_id,
                    snapshot.consumer.value,
                    request_id,
                    snapshot.query_fingerprint,
                    snapshot.index_scope,
                    snapshot.index_generation,
                    snapshot.index_revision,
                    snapshot.mode.value,
                    len(snapshot.source_refs),
                    snapshot.result_digest,
                    snapshot.vector_status,
                    now,
                    self._json(metadata or {}),
                ),
            )
            row = connection.execute(
                "SELECT * FROM retrieval_query_provenance WHERE query_id = ?",
                (snapshot.query_id,),
            ).fetchone()
        return self._query_record(row)

    def query_records(
        self,
        *,
        task_id: str,
        consumer: str = "",
        limit: int = 100,
    ) -> tuple[QueryProvenanceRecord, ...]:
        clauses = ["task_id = ?"]
        parameters: list[Any] = [task_id]
        if consumer:
            clauses.append("consumer = ?")
            parameters.append(consumer)
        parameters.append(max(0, min(int(limit), 10_000)))
        self.initialize()
        with self.index.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM retrieval_query_provenance
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC, query_id DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(self._query_record(row) for row in rows)

    def claim_delivery(
        self,
        *,
        worker_request_id: str,
        run_id: str,
        task_id: str,
        session_id: str,
        query_ids: Sequence[str],
        message_ids: Sequence[str],
        source_digest: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[ContextDeliveryRecord, bool]:
        if not worker_request_id or not run_id or not task_id or not session_id:
            raise ValueError("delivery requires worker/run/task/session identity")
        query_values = tuple(dict.fromkeys(str(value) for value in query_ids if str(value)))
        message_values = tuple(dict.fromkeys(str(value) for value in message_ids if str(value)))
        if not source_digest:
            raise ValueError("delivery requires source_digest")
        delivery_id = stable_identifier("delivery", worker_request_id, source_digest)
        now = time.time()
        with self.index.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM retrieval_context_deliveries WHERE worker_request_id = ?",
                (worker_request_id,),
            ).fetchone()
            if existing is not None:
                record = self._delivery_record(existing)
                if record.source_digest != source_digest:
                    raise RuntimeError("worker request already claimed another retrieval snapshot")
                if record.state is DeliveryState.RELEASED:
                    connection.execute(
                        """
                        UPDATE retrieval_context_deliveries
                        SET state = ?, terminal_event_ids_json = '[]', reason = '',
                            updated_at = ?, metadata_json = ?
                        WHERE worker_request_id = ?
                        """,
                        (
                            DeliveryState.CLAIMED.value,
                            now,
                            self._json(metadata or record.metadata),
                            worker_request_id,
                        ),
                    )
                    refreshed = connection.execute(
                        "SELECT * FROM retrieval_context_deliveries WHERE worker_request_id = ?",
                        (worker_request_id,),
                    ).fetchone()
                    return self._delivery_record(refreshed), False
                return record, False
            connection.execute(
                """
                INSERT INTO retrieval_context_deliveries(
                    delivery_id, worker_request_id, run_id, task_id, session_id,
                    state, query_ids_json, message_ids_json, source_digest,
                    claimed_at, updated_at, terminal_event_ids_json, reason,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)
                """,
                (
                    delivery_id,
                    worker_request_id,
                    run_id,
                    task_id,
                    session_id,
                    DeliveryState.CLAIMED.value,
                    self._json(query_values),
                    self._json(message_values),
                    source_digest,
                    now,
                    now,
                    self._json(()),
                    self._json(metadata or {}),
                ),
            )
            row = connection.execute(
                "SELECT * FROM retrieval_context_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
        return self._delivery_record(row), True

    def complete_delivery(
        self,
        worker_request_id: str,
        *,
        terminal_event_ids: Sequence[str],
    ) -> ContextDeliveryRecord:
        return self._transition_delivery(
            worker_request_id,
            target=DeliveryState.COMMITTED,
            terminal_event_ids=terminal_event_ids,
            reason="provider_runtime_completed",
        )

    def release_delivery(
        self,
        worker_request_id: str,
        *,
        reason: str,
        terminal_event_ids: Sequence[str] = (),
    ) -> ContextDeliveryRecord:
        return self._transition_delivery(
            worker_request_id,
            target=DeliveryState.RELEASED,
            terminal_event_ids=terminal_event_ids,
            reason=reason or "worker_execution_failed",
        )

    def delivery(self, worker_request_id: str) -> ContextDeliveryRecord | None:
        self.initialize()
        with self.index.connection() as connection:
            row = connection.execute(
                "SELECT * FROM retrieval_context_deliveries WHERE worker_request_id = ?",
                (worker_request_id,),
            ).fetchone()
        return None if row is None else self._delivery_record(row)

    def save_checkpoint(self, checkpoint: IndexCheckpointRef) -> Mapping[str, Any]:
        value = checkpoint.validated()
        body = value.to_dict()
        digest = stable_digest(body)
        with self.index.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO retrieval_checkpoint_refs(
                    task_id, source_revision, desired_generation,
                    published_generation, pending_job_ids_json,
                    last_query_ids_json, cursor_refs_json, checkpoint_digest,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    source_revision = excluded.source_revision,
                    desired_generation = excluded.desired_generation,
                    published_generation = excluded.published_generation,
                    pending_job_ids_json = excluded.pending_job_ids_json,
                    last_query_ids_json = excluded.last_query_ids_json,
                    cursor_refs_json = excluded.cursor_refs_json,
                    checkpoint_digest = excluded.checkpoint_digest,
                    updated_at = excluded.updated_at
                """,
                (
                    value.task_id,
                    value.source_revision,
                    value.desired_generation,
                    value.published_generation,
                    self._json(value.pending_job_ids),
                    self._json(value.last_query_ids),
                    self._json(value.cursor_refs),
                    digest,
                    time.time(),
                ),
            )
        return {**body, "checkpoint_digest": digest}

    def load_checkpoint(self, task_id: str) -> IndexCheckpointRef | None:
        self.initialize()
        with self.index.connection() as connection:
            row = connection.execute(
                "SELECT * FROM retrieval_checkpoint_refs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        checkpoint = IndexCheckpointRef(
            task_id=str(row["task_id"]),
            source_revision=str(row["source_revision"]),
            desired_generation=int(row["desired_generation"]),
            published_generation=int(row["published_generation"]),
            pending_job_ids=tuple(self._list(row["pending_job_ids_json"])),
            last_query_ids=tuple(self._list(row["last_query_ids_json"])),
            cursor_refs=tuple(
                dict(value) for value in self._list(row["cursor_refs_json"]) if isinstance(value, Mapping)
            ),
        ).validated()
        if stable_digest(checkpoint.to_dict()) != str(row["checkpoint_digest"]):
            raise RuntimeError("retrieval checkpoint reference digest mismatch")
        return checkpoint

    def health(self) -> Mapping[str, Any]:
        self.initialize()
        with self.index.connection() as connection:
            admission_rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM retrieval_source_admissions GROUP BY state"
            ).fetchall()
            delivery_rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM retrieval_context_deliveries GROUP BY state"
            ).fetchall()
            query_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM retrieval_query_provenance"
                ).fetchone()["count"]
            )
            checkpoint_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM retrieval_checkpoint_refs"
                ).fetchone()["count"]
            )
        return {
            "admissions": {str(row["state"]): int(row["count"]) for row in admission_rows},
            "deliveries": {str(row["state"]): int(row["count"]) for row in delivery_rows},
            "query_provenance_count": query_count,
            "checkpoint_ref_count": checkpoint_count,
            "canonical_records_stored": False,
            "index_dump_stored": False,
            "state_owner": "RetrievalIntegrationStore(derived)",
        }

    def _transition_delivery(
        self,
        worker_request_id: str,
        *,
        target: DeliveryState,
        terminal_event_ids: Sequence[str],
        reason: str,
    ) -> ContextDeliveryRecord:
        event_ids = tuple(dict.fromkeys(str(value) for value in terminal_event_ids if str(value)))
        with self.index.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM retrieval_context_deliveries WHERE worker_request_id = ?",
                (worker_request_id,),
            ).fetchone()
            if row is None:
                raise KeyError(worker_request_id)
            current = DeliveryState(str(row["state"]))
            if current is DeliveryState.COMMITTED and target is not DeliveryState.COMMITTED:
                raise RuntimeError("committed retrieval delivery cannot be released")
            connection.execute(
                """
                UPDATE retrieval_context_deliveries
                SET state = ?, terminal_event_ids_json = ?, reason = ?, updated_at = ?
                WHERE worker_request_id = ?
                """,
                (target.value, self._json(event_ids), reason, time.time(), worker_request_id),
            )
            updated = connection.execute(
                "SELECT * FROM retrieval_context_deliveries WHERE worker_request_id = ?",
                (worker_request_id,),
            ).fetchone()
        return self._delivery_record(updated)

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}

    @staticmethod
    def _list(value: Any) -> list[Any]:
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return list(parsed) if isinstance(parsed, list) else []

    @classmethod
    def _source_admission(cls, row: Any) -> SourceAdmission:
        return SourceAdmission(
            admission_id=str(row["admission_id"]),
            task_id=str(row["task_id"]),
            source_kind=str(row["source_kind"]),
            source_id=str(row["source_id"]),
            source_revision=str(row["source_revision"]),
            content_digest=str(row["content_digest"]),
            event_id=str(row["event_id"]),
            causation_id=str(row["causation_id"]),
            state=AdmissionState(str(row["state"])),
            job_id=str(row["job_id"]),
            generation=int(row["generation"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            error_code=str(row["error_code"]),
            metadata=cls._mapping(row["metadata_json"]),
        )

    @classmethod
    def _query_record(cls, row: Any) -> QueryProvenanceRecord:
        return QueryProvenanceRecord(
            query_id=str(row["query_id"]),
            task_id=str(row["task_id"]),
            consumer=str(row["consumer"]),
            request_id=str(row["request_id"]),
            query_fingerprint=str(row["query_fingerprint"]),
            index_scope=str(row["index_scope"]),
            index_generation=int(row["index_generation"]),
            index_revision=str(row["index_revision"]),
            mode=str(row["mode"]),
            source_ref_count=int(row["source_ref_count"]),
            result_digest=str(row["result_digest"]),
            vector_status=str(row["vector_status"]),
            created_at=float(row["created_at"]),
            metadata=cls._mapping(row["metadata_json"]),
        )

    @classmethod
    def _delivery_record(cls, row: Any) -> ContextDeliveryRecord:
        return ContextDeliveryRecord(
            delivery_id=str(row["delivery_id"]),
            worker_request_id=str(row["worker_request_id"]),
            run_id=str(row["run_id"]),
            task_id=str(row["task_id"]),
            session_id=str(row["session_id"]),
            state=DeliveryState(str(row["state"])),
            query_ids=tuple(str(value) for value in cls._list(row["query_ids_json"])),
            message_ids=tuple(str(value) for value in cls._list(row["message_ids_json"])),
            source_digest=str(row["source_digest"]),
            claimed_at=float(row["claimed_at"]),
            updated_at=float(row["updated_at"]),
            terminal_event_ids=tuple(
                str(value) for value in cls._list(row["terminal_event_ids_json"])
            ),
            reason=str(row["reason"]),
            metadata=cls._mapping(row["metadata_json"]),
        )


__all__ = [
    "AdmissionState",
    "ContextDeliveryRecord",
    "DeliveryState",
    "QueryProvenanceRecord",
    "RetrievalIntegrationStore",
    "SourceAdmission",
]
