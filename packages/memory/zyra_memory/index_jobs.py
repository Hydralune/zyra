from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .retrieval_models import (
    IndexJob,
    IndexJobState,
    IndexLease,
    IndexOperation,
    LeaseLostError,
    PublicationFencedError,
    stable_identifier,
)
from .retrieval_store import SQLiteRetrievalIndex


ACTIVE_STATES = (
    IndexJobState.LEASED,
    IndexJobState.BUILDING,
    IndexJobState.PUBLISHING,
)


@dataclass(frozen=True, slots=True)
class SweepResult:
    stale_job_ids: tuple[str, ...]
    requeued_job_ids: tuple[str, ...]
    generations: tuple[int, ...]

    @property
    def count(self) -> int:
        return len(self.stale_job_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stale_job_ids": list(self.stale_job_ids),
            "requeued_job_ids": list(self.requeued_job_ids),
            "generations": list(self.generations),
            "count": self.count,
        }


class IndexBuildQueue:
    """Durable desired-generation queue.

    Queue entries are facts about requested derived work.  Duplicate delivery is
    safe because callers may provide an idempotency key and worker admission is
    guarded by IndexLeaseStore's transaction.  A new request advances the scope
    desired generation in the same transaction that writes the queued job.
    """

    def __init__(self, index: SQLiteRetrievalIndex) -> None:
        self.index = index
        self.initialize()

    def initialize(self) -> None:
        self.index.initialize()
        with self.index.transaction(immediate=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS index_jobs (
                    job_id TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    state TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    source_revision TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_epoch INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at REAL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    published_at REAL,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    causation_id TEXT NOT NULL DEFAULT '',
                    idempotency_key TEXT NOT NULL DEFAULT ''
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_index_jobs_idempotency
                    ON index_jobs(idempotency_key)
                    WHERE idempotency_key <> '';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_index_jobs_scope_generation
                    ON index_jobs(scope_key, generation);
                CREATE INDEX IF NOT EXISTS idx_index_jobs_state_created
                    ON index_jobs(state, created_at, job_id);
                CREATE INDEX IF NOT EXISTS idx_index_jobs_lease_expiry
                    ON index_jobs(lease_expires_at, state);
                CREATE INDEX IF NOT EXISTS idx_index_jobs_scope_state
                    ON index_jobs(scope_key, state, generation DESC);
                """
            )

    def enqueue(
        self,
        *,
        scope_key: str,
        source_revision: str,
        operation: IndexOperation = IndexOperation.REBUILD,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str = "",
        causation_id: str = "",
    ) -> IndexJob:
        if not scope_key.strip():
            raise ValueError("scope_key is required")
        if not source_revision.strip():
            raise ValueError("source_revision is required")
        now = self.index.clock()
        with self.index.transaction(immediate=True) as connection:
            if idempotency_key:
                existing = connection.execute(
                    "SELECT * FROM index_jobs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return self._row_to_job(existing)
            connection.execute(
                """
                INSERT INTO retrieval_scopes(scope_key, desired_generation, updated_at)
                VALUES (?, 0, ?)
                ON CONFLICT(scope_key) DO NOTHING
                """,
                (scope_key, now),
            )
            scope = connection.execute(
                "SELECT desired_generation FROM retrieval_scopes WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
            generation = int(scope["desired_generation"]) + 1
            job_id = stable_identifier(
                "idxjob",
                scope_key,
                generation,
                operation.value,
                source_revision,
                idempotency_key,
            )
            connection.execute(
                """
                UPDATE retrieval_scopes
                SET desired_generation = ?, source_revision = ?, updated_at = ?
                WHERE scope_key = ?
                """,
                (generation, source_revision, now, scope_key),
            )
            connection.execute(
                """
                INSERT INTO index_jobs(
                    job_id, scope_key, operation, state, generation,
                    source_revision, payload_json, created_at, updated_at,
                    causation_id, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    scope_key,
                    operation.value,
                    IndexJobState.QUEUED.value,
                    generation,
                    source_revision,
                    json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True, default=str),
                    now,
                    now,
                    causation_id,
                    idempotency_key,
                ),
            )
            self.index._audit(
                connection,
                event_type="index.queued",
                job_id=job_id,
                scope_key=scope_key,
                generation=generation,
                source_revision=source_revision,
                causation_id=causation_id,
                payload={"operation": operation.value, "idempotency_key": idempotency_key},
                created_at=now,
            )
            row = connection.execute("SELECT * FROM index_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._row_to_job(row)

    def get(self, job_id: str) -> IndexJob | None:
        self.initialize()
        with self.index.connection() as connection:
            row = connection.execute("SELECT * FROM index_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return None if row is None else self._row_to_job(row)

    def require(self, job_id: str) -> IndexJob:
        job = self.get(job_id)
        if job is None:
            raise KeyError(f"index job not found: {job_id}")
        return job

    def list(
        self,
        *,
        states: Sequence[IndexJobState] = (),
        scope_key: str = "",
        limit: int = 100,
    ) -> tuple[IndexJob, ...]:
        where: list[str] = []
        params: list[Any] = []
        if states:
            placeholders = ",".join("?" for _ in states)
            where.append(f"state IN ({placeholders})")
            params.extend(item.value for item in states)
        if scope_key:
            where.append("scope_key = ?")
            params.append(scope_key)
        condition = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(max(0, int(limit)))
        self.initialize()
        with self.index.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM index_jobs
                {condition}
                ORDER BY created_at ASC, job_id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return tuple(self._row_to_job(row) for row in rows)

    def cancel_superseded_queued(self, scope_key: str) -> tuple[str, ...]:
        """Mark older queued jobs stale after a newer desired generation exists."""
        now = self.index.clock()
        changed: list[str] = []
        with self.index.transaction(immediate=True) as connection:
            scope = connection.execute(
                "SELECT desired_generation FROM retrieval_scopes WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
            if scope is None:
                return ()
            desired = int(scope["desired_generation"])
            rows = connection.execute(
                """
                SELECT job_id, generation, source_revision, causation_id
                FROM index_jobs
                WHERE scope_key = ? AND state = ? AND generation < ?
                """,
                (scope_key, IndexJobState.QUEUED.value, desired),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE index_jobs SET state = ?, updated_at = ?, error_code = ? WHERE job_id = ?",
                    (IndexJobState.STALE.value, now, "superseded_generation", row["job_id"]),
                )
                changed.append(str(row["job_id"]))
                self.index._audit(
                    connection,
                    event_type="index.stale",
                    job_id=str(row["job_id"]),
                    scope_key=scope_key,
                    generation=int(row["generation"]),
                    source_revision=str(row["source_revision"]),
                    causation_id=str(row["causation_id"]),
                    payload={"reason": "superseded_generation", "desired_generation": desired},
                    created_at=now,
                )
        return tuple(changed)

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> IndexJob:
        def timestamp(name: str) -> str:
            value = row[name]
            return SQLiteRetrievalIndex._timestamp_iso(float(value)) if value is not None else ""

        return IndexJob(
            job_id=str(row["job_id"]),
            scope_key=str(row["scope_key"]),
            operation=IndexOperation(str(row["operation"])),
            state=IndexJobState(str(row["state"])),
            generation=int(row["generation"]),
            source_revision=str(row["source_revision"]),
            payload=json.loads(str(row["payload_json"])),
            lease_owner=str(row["lease_owner"]),
            lease_token=str(row["lease_token"]),
            lease_epoch=int(row["lease_epoch"]),
            lease_expires_at=timestamp("lease_expires_at"),
            attempt=int(row["attempt"]),
            created_at=timestamp("created_at"),
            updated_at=timestamp("updated_at"),
            started_at=timestamp("started_at"),
            published_at=timestamp("published_at"),
            error_code=str(row["error_code"]),
            error_message=str(row["error_message"]),
            causation_id=str(row["causation_id"]),
            idempotency_key=str(row["idempotency_key"]),
        )


class IndexLeaseStore:
    """Persistent lease, heartbeat, state-transition, and stale-sweep owner."""

    def __init__(self, queue: IndexBuildQueue) -> None:
        self.queue = queue
        self.index = queue.index

    def acquire(
        self,
        *,
        worker_id: str,
        ttl_seconds: float,
        scope_key: str = "",
    ) -> IndexLease | None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = self.index.clock()
        with self.index.transaction(immediate=True) as connection:
            where = "j.state = ?"
            params: list[Any] = [IndexJobState.QUEUED.value]
            if scope_key:
                where += " AND j.scope_key = ?"
                params.append(scope_key)
            row = connection.execute(
                f"""
                SELECT j.* FROM index_jobs j
                JOIN retrieval_scopes s ON s.scope_key = j.scope_key
                WHERE {where} AND j.generation = s.desired_generation
                ORDER BY j.created_at ASC, j.job_id ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            epoch = int(row["lease_epoch"]) + 1
            expires = now + ttl_seconds
            connection.execute(
                """
                UPDATE index_jobs
                SET state = ?, lease_owner = ?, lease_token = ?, lease_epoch = ?,
                    lease_expires_at = ?, attempt = attempt + 1,
                    started_at = COALESCE(started_at, ?), updated_at = ?,
                    error_code = '', error_message = ''
                WHERE job_id = ? AND state = ? AND lease_epoch = ?
                """,
                (
                    IndexJobState.LEASED.value,
                    worker_id,
                    token,
                    epoch,
                    expires,
                    now,
                    now,
                    row["job_id"],
                    IndexJobState.QUEUED.value,
                    row["lease_epoch"],
                ),
            )
            changed = int(connection.execute("SELECT changes() AS count").fetchone()["count"])
            if changed != 1:
                return None
            lease = IndexLease(
                job_id=str(row["job_id"]),
                scope_key=str(row["scope_key"]),
                worker_id=worker_id,
                token=token,
                epoch=epoch,
                generation=int(row["generation"]),
                expires_at=self.index._timestamp_iso(expires),
            )
            self.index._audit(
                connection,
                event_type="index.leased",
                lease=lease,
                source_revision=str(row["source_revision"]),
                causation_id=str(row["causation_id"]),
                payload={"ttl_seconds": ttl_seconds, "attempt": int(row["attempt"]) + 1},
                created_at=now,
            )
            return lease

    def heartbeat(self, lease: IndexLease, *, ttl_seconds: float) -> IndexLease:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = self.index.clock()
        expires = now + ttl_seconds
        with self.index.transaction(immediate=True) as connection:
            connection.execute(
                f"""
                UPDATE index_jobs
                SET lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND scope_key = ? AND generation = ?
                    AND lease_owner = ? AND lease_token = ? AND lease_epoch = ?
                    AND state IN ({','.join('?' for _ in ACTIVE_STATES)})
                    AND lease_expires_at > ?
                """,
                (
                    expires,
                    now,
                    lease.job_id,
                    lease.scope_key,
                    lease.generation,
                    lease.worker_id,
                    lease.token,
                    lease.epoch,
                    *(item.value for item in ACTIVE_STATES),
                    now,
                ),
            )
            changed = int(connection.execute("SELECT changes() AS count").fetchone()["count"])
            if changed != 1:
                raise LeaseLostError("index lease heartbeat was fenced or expired")
            self.index._audit(
                connection,
                event_type="index.heartbeat",
                lease=lease,
                payload={"ttl_seconds": ttl_seconds},
                created_at=now,
            )
        return IndexLease(
            job_id=lease.job_id,
            scope_key=lease.scope_key,
            worker_id=lease.worker_id,
            token=lease.token,
            epoch=lease.epoch,
            generation=lease.generation,
            expires_at=self.index._timestamp_iso(expires),
        )

    def start_build(self, lease: IndexLease) -> None:
        self._transition(lease, IndexJobState.LEASED, IndexJobState.BUILDING, "index.building")

    def start_publish(self, lease: IndexLease) -> None:
        self._transition(lease, IndexJobState.BUILDING, IndexJobState.PUBLISHING, "index.publishing")

    def fail(self, lease: IndexLease, error: BaseException, *, retryable: bool = False) -> None:
        now = self.index.clock()
        message = f"{type(error).__name__}: {str(error).splitlines()[0] if str(error) else type(error).__name__}"
        with self.index.transaction(immediate=True) as connection:
            row = self._validate(connection, lease, states=ACTIVE_STATES, require_unexpired=False)
            connection.execute(
                """
                UPDATE index_jobs
                SET state = ?, error_code = ?, error_message = ?, updated_at = ?,
                    lease_owner = '', lease_token = '', lease_expires_at = NULL
                WHERE job_id = ? AND lease_epoch = ? AND lease_token = ?
                """,
                (
                    IndexJobState.FAILED.value,
                    "retryable" if retryable else "terminal",
                    message[:500],
                    now,
                    lease.job_id,
                    lease.epoch,
                    lease.token,
                ),
            )
            self.index._audit(
                connection,
                event_type="index.failed",
                lease=lease,
                source_revision=str(row["source_revision"]),
                causation_id=str(row["causation_id"]),
                payload={"retryable": retryable, "error": message[:500]},
                created_at=now,
            )

    def release_without_completion(self, lease: IndexLease, *, reason: str) -> None:
        """Return a healthy lease to queued without changing its generation."""
        now = self.index.clock()
        with self.index.transaction(immediate=True) as connection:
            row = self._validate(connection, lease, states=ACTIVE_STATES, require_unexpired=True)
            connection.execute(
                """
                UPDATE index_jobs
                SET state = ?, lease_owner = '', lease_token = '',
                    lease_expires_at = NULL, updated_at = ?, error_code = ?, error_message = ?
                WHERE job_id = ? AND lease_epoch = ? AND lease_token = ?
                """,
                (
                    IndexJobState.QUEUED.value,
                    now,
                    "released",
                    reason[:500],
                    lease.job_id,
                    lease.epoch,
                    lease.token,
                ),
            )
            self.index._audit(
                connection,
                event_type="index.requeued",
                lease=lease,
                source_revision=str(row["source_revision"]),
                causation_id=str(row["causation_id"]),
                payload={"reason": reason, "same_generation": True},
                created_at=now,
            )

    def sweep_expired(self, *, limit: int = 100) -> SweepResult:
        """Atomically stale expired leases and enqueue fenced replacement generations."""
        now = self.index.clock()
        stale: list[str] = []
        requeued: list[str] = []
        generations: list[int] = []
        with self.index.transaction(immediate=True) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM index_jobs
                WHERE state IN ({','.join('?' for _ in ACTIVE_STATES)})
                    AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                ORDER BY lease_expires_at ASC, job_id ASC
                LIMIT ?
                """,
                (*(item.value for item in ACTIVE_STATES), now, max(0, int(limit))),
            ).fetchall()
            for row in rows:
                old_job_id = str(row["job_id"])
                scope_key = str(row["scope_key"])
                old_generation = int(row["generation"])
                connection.execute(
                    """
                    UPDATE index_jobs
                    SET state = ?, updated_at = ?, error_code = ?, error_message = ?,
                        lease_owner = '', lease_token = '', lease_expires_at = NULL
                    WHERE job_id = ? AND state IN (?, ?, ?) AND lease_expires_at <= ?
                    """,
                    (
                        IndexJobState.STALE.value,
                        now,
                        "lease_expired",
                        "worker heartbeat expired; replacement generation queued",
                        old_job_id,
                        *(item.value for item in ACTIVE_STATES),
                        now,
                    ),
                )
                changed = int(connection.execute("SELECT changes() AS count").fetchone()["count"])
                if changed != 1:
                    continue
                scope = connection.execute(
                    "SELECT desired_generation FROM retrieval_scopes WHERE scope_key = ?",
                    (scope_key,),
                ).fetchone()
                desired = max(old_generation, int(scope["desired_generation"] if scope else 0)) + 1
                source_revision = str(row["source_revision"])
                replacement_id = stable_identifier(
                    "idxjob",
                    scope_key,
                    desired,
                    row["operation"],
                    source_revision,
                    "stale-requeue",
                    old_job_id,
                )
                connection.execute(
                    """
                    UPDATE retrieval_scopes
                    SET desired_generation = ?, source_revision = ?, updated_at = ?
                    WHERE scope_key = ?
                    """,
                    (desired, source_revision, now, scope_key),
                )
                connection.execute(
                    """
                    INSERT INTO index_jobs(
                        job_id, scope_key, operation, state, generation,
                        source_revision, payload_json, created_at, updated_at,
                        causation_id, idempotency_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                    """,
                    (
                        replacement_id,
                        scope_key,
                        row["operation"],
                        IndexJobState.QUEUED.value,
                        desired,
                        source_revision,
                        row["payload_json"],
                        now,
                        now,
                        row["causation_id"],
                    ),
                )
                self.index._audit(
                    connection,
                    event_type="index.stale",
                    job_id=old_job_id,
                    scope_key=scope_key,
                    generation=old_generation,
                    lease_epoch=int(row["lease_epoch"]),
                    worker_id=str(row["lease_owner"]),
                    source_revision=source_revision,
                    causation_id=str(row["causation_id"]),
                    payload={"reason": "lease_expired", "replacement_job_id": replacement_id},
                    created_at=now,
                )
                self.index._audit(
                    connection,
                    event_type="index.requeued",
                    job_id=replacement_id,
                    scope_key=scope_key,
                    generation=desired,
                    source_revision=source_revision,
                    causation_id=str(row["causation_id"]),
                    payload={"stale_job_id": old_job_id, "old_generation": old_generation},
                    created_at=now,
                )
                stale.append(old_job_id)
                requeued.append(replacement_id)
                generations.append(desired)
        return SweepResult(tuple(stale), tuple(requeued), tuple(generations))

    def assert_publishable(self, lease: IndexLease) -> None:
        self.index.initialize()
        with self.index.connection() as connection:
            self._validate(
                connection,
                lease,
                states=(IndexJobState.PUBLISHING,),
                require_unexpired=True,
            )
            scope = connection.execute(
                "SELECT desired_generation FROM retrieval_scopes WHERE scope_key = ?",
                (lease.scope_key,),
            ).fetchone()
            if scope is None or int(scope["desired_generation"]) != lease.generation:
                raise PublicationFencedError("lease generation has been superseded")

    def _transition(
        self,
        lease: IndexLease,
        before: IndexJobState,
        after: IndexJobState,
        event_type: str,
    ) -> None:
        now = self.index.clock()
        with self.index.transaction(immediate=True) as connection:
            row = self._validate(connection, lease, states=(before,), require_unexpired=True)
            connection.execute(
                """
                UPDATE index_jobs SET state = ?, updated_at = ?
                WHERE job_id = ? AND state = ? AND generation = ?
                    AND lease_owner = ? AND lease_epoch = ? AND lease_token = ?
                    AND lease_expires_at > ?
                """,
                (
                    after.value,
                    now,
                    lease.job_id,
                    before.value,
                    lease.generation,
                    lease.worker_id,
                    lease.epoch,
                    lease.token,
                    now,
                ),
            )
            changed = int(connection.execute("SELECT changes() AS count").fetchone()["count"])
            if changed != 1:
                raise LeaseLostError(f"cannot transition index job {before.value} -> {after.value}")
            self.index._audit(
                connection,
                event_type=event_type,
                lease=lease,
                source_revision=str(row["source_revision"]),
                causation_id=str(row["causation_id"]),
                payload={"state_before": before.value, "state_after": after.value},
                created_at=now,
            )

    def _validate(
        self,
        connection: sqlite3.Connection,
        lease: IndexLease,
        *,
        states: Sequence[IndexJobState],
        require_unexpired: bool,
    ) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM index_jobs WHERE job_id = ?", (lease.job_id,)).fetchone()
        if row is None:
            raise LeaseLostError("index job no longer exists")
        if str(row["scope_key"]) != lease.scope_key or int(row["generation"]) != lease.generation:
            raise LeaseLostError("index lease scope or generation changed")
        if str(row["lease_owner"]) != lease.worker_id:
            raise LeaseLostError("index lease owner changed")
        if str(row["lease_token"]) != lease.token or int(row["lease_epoch"]) != lease.epoch:
            raise LeaseLostError("index lease fencing token changed")
        if IndexJobState(str(row["state"])) not in states:
            raise LeaseLostError("index job state is not valid for this operation")
        if require_unexpired:
            expires = row["lease_expires_at"]
            if expires is None or float(expires) <= self.index.clock():
                raise LeaseLostError("index lease expired")
        return row
