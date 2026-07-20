from __future__ import annotations

import json
import secrets
import sqlite3
import time
from typing import Any, Mapping, Sequence

from .job_models import (
    CodeIndexBuildJob,
    CodeIndexBuildLease,
    CodeIndexJobOperation,
    CodeIndexJobState,
    CodeIndexLeaseLost,
    CodeIndexLeaseUnavailable,
    CodeIndexPublicationFenced,
    CodeIndexSweepResult,
)
from .models import WorkspaceIdentity, stable_digest
from .store import CodeIndexStore


class CodeIndexBuildQueue:
    """Durable desired-generation queue for workspace code indexes.

    The generation fence is advanced in the same transaction as job admission.
    A patch arriving while a worker builds therefore makes the older lease
    unable to publish even if its heartbeat is still valid.
    """

    def __init__(self, store: CodeIndexStore, *, clock: Any | None = None) -> None:
        self.store = store
        self.clock = clock or time.time
        self.initialize()

    def initialize(self) -> None:
        self.store.initialize()
        with self.store.transaction(immediate=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS code_index_generation_fences (
                    workspace_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    desired_generation INTEGER NOT NULL,
                    published_generation INTEGER NOT NULL DEFAULT 0,
                    published_revision TEXT NOT NULL DEFAULT '',
                    active_job_id TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS code_index_jobs (
                    job_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    state TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    source_revision TEXT NOT NULL,
                    transaction_id TEXT NOT NULL DEFAULT '',
                    changed_paths_json TEXT NOT NULL,
                    deleted_paths_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_epoch INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at REAL NOT NULL DEFAULT 0,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL NOT NULL DEFAULT 0,
                    published_at REAL NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    causation_id TEXT NOT NULL DEFAULT '',
                    idempotency_key TEXT NOT NULL DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_code_index_jobs_claim
                    ON code_index_jobs(state, workspace_id, generation, created_at);
                CREATE INDEX IF NOT EXISTS idx_code_index_jobs_lease
                    ON code_index_jobs(state, lease_expires_at)
                    WHERE lease_expires_at > 0;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_code_index_jobs_idempotency
                    ON code_index_jobs(workspace_id, idempotency_key)
                    WHERE idempotency_key <> '';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_code_index_jobs_transaction
                    ON code_index_jobs(workspace_id, transaction_id)
                    WHERE transaction_id <> '';

                CREATE TABLE IF NOT EXISTS code_index_job_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    worker_id TEXT NOT NULL DEFAULT '',
                    source_revision TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_code_index_job_audit_workspace
                    ON code_index_job_audit(workspace_id, audit_id);
                """
            )

    def enqueue(
        self,
        identity: WorkspaceIdentity,
        *,
        operation: CodeIndexJobOperation = CodeIndexJobOperation.REBUILD,
        transaction_id: str = "",
        changed_paths: Sequence[str] = (),
        deleted_paths: Sequence[str] = (),
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str = "",
        causation_id: str = "",
    ) -> CodeIndexBuildJob:
        workspace_id = identity.workspace_id.strip()
        source_revision = identity.revision.strip()
        if not workspace_id or not source_revision or not identity.task_id.strip():
            raise ValueError("code index job requires workspace/task/source revision")
        normalized_changed = tuple(sorted({str(path).strip() for path in changed_paths if str(path).strip()}))
        normalized_deleted = tuple(sorted({str(path).strip() for path in deleted_paths if str(path).strip()}))
        key = idempotency_key.strip()
        tx_id = transaction_id.strip()
        now = float(self.clock())
        with self.store.transaction(immediate=True) as connection:
            existing = self._existing_idempotent(
                connection,
                workspace_id=workspace_id,
                idempotency_key=key,
                transaction_id=tx_id,
            )
            if existing is not None:
                expected = stable_digest(
                    operation.value,
                    source_revision,
                    normalized_changed,
                    normalized_deleted,
                    payload or {},
                )
                observed = stable_digest(
                    str(existing["operation"]),
                    str(existing["source_revision"]),
                    self._list(existing["changed_paths_json"]),
                    self._list(existing["deleted_paths_json"]),
                    self._mapping(existing["payload_json"]),
                )
                if expected != observed:
                    raise ValueError("code index idempotency key was reused with another payload")
                return self._row_to_job(existing)
            fence = connection.execute(
                "SELECT * FROM code_index_generation_fences WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            desired = int(fence["desired_generation"] if fence else 0) + 1
            job_id = "codejob_" + stable_digest(
                workspace_id,
                desired,
                source_revision,
                operation.value,
                tx_id,
                key,
                normalized_changed,
                normalized_deleted,
            )[:32]
            connection.execute(
                """
                INSERT INTO code_index_generation_fences(
                    workspace_id, task_id, source_revision, desired_generation,
                    published_generation, published_revision, active_job_id, updated_at
                ) VALUES (?, ?, ?, ?, 0, '', ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    task_id = excluded.task_id,
                    source_revision = excluded.source_revision,
                    desired_generation = excluded.desired_generation,
                    active_job_id = excluded.active_job_id,
                    updated_at = excluded.updated_at
                """,
                (
                    workspace_id,
                    identity.task_id,
                    source_revision,
                    desired,
                    job_id,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE code_index_jobs
                SET state = ?, error_code = ?, error_message = ?, updated_at = ?
                WHERE workspace_id = ? AND state = ? AND generation < ?
                """,
                (
                    CodeIndexJobState.STALE.value,
                    "superseded_before_claim",
                    "a newer desired generation was admitted",
                    now,
                    workspace_id,
                    CodeIndexJobState.QUEUED.value,
                    desired,
                ),
            )
            connection.execute(
                """
                INSERT INTO code_index_jobs(
                    job_id, workspace_id, task_id, operation, state,
                    generation, source_revision, transaction_id,
                    changed_paths_json, deleted_paths_json, payload_json,
                    created_at, updated_at, causation_id, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    workspace_id,
                    identity.task_id,
                    operation.value,
                    CodeIndexJobState.QUEUED.value,
                    desired,
                    source_revision,
                    tx_id,
                    self._json(normalized_changed),
                    self._json(normalized_deleted),
                    self._json(payload or {}),
                    now,
                    now,
                    causation_id,
                    key,
                ),
            )
            self._audit(
                connection,
                event_type="code_index.queued",
                workspace_id=workspace_id,
                job_id=job_id,
                generation=desired,
                source_revision=source_revision,
                payload={
                    "operation": operation.value,
                    "transaction_id": tx_id,
                    "changed_paths": normalized_changed,
                    "deleted_paths": normalized_deleted,
                    "causation_id": causation_id,
                },
                now=now,
            )
            row = connection.execute(
                "SELECT * FROM code_index_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return self._row_to_job(row)

    def claim(
        self,
        *,
        worker_id: str,
        workspace_id: str = "",
        lease_ttl_seconds: float = 30.0,
    ) -> CodeIndexBuildLease | None:
        worker = worker_id.strip()
        if not worker:
            raise ValueError("worker_id is required")
        now = float(self.clock())
        expires = now + max(0.1, float(lease_ttl_seconds))
        with self.store.transaction(immediate=True) as connection:
            where = ["j.state = ?", "j.generation = f.desired_generation"]
            parameters: list[Any] = [CodeIndexJobState.QUEUED.value]
            if workspace_id:
                where.append("j.workspace_id = ?")
                parameters.append(workspace_id)
            row = connection.execute(
                f"""
                SELECT j.* FROM code_index_jobs j
                JOIN code_index_generation_fences f ON f.workspace_id = j.workspace_id
                WHERE {' AND '.join(where)}
                ORDER BY j.created_at, j.job_id LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            epoch = int(row["lease_epoch"]) + 1
            connection.execute(
                """
                UPDATE code_index_jobs
                SET state = ?, lease_owner = ?, lease_token = ?, lease_epoch = ?,
                    lease_expires_at = ?, attempt = attempt + 1,
                    started_at = CASE WHEN started_at = 0 THEN ? ELSE started_at END,
                    updated_at = ?, error_code = '', error_message = ''
                WHERE job_id = ? AND state = ?
                """,
                (
                    CodeIndexJobState.LEASED.value,
                    worker,
                    token,
                    epoch,
                    expires,
                    now,
                    now,
                    str(row["job_id"]),
                    CodeIndexJobState.QUEUED.value,
                ),
            )
            if int(connection.execute("SELECT changes() AS count").fetchone()["count"]) != 1:
                raise CodeIndexLeaseUnavailable(
                    "code_index_claim_race",
                    "code index job changed before lease claim committed",
                )
            self._audit(
                connection,
                event_type="code_index.leased",
                workspace_id=str(row["workspace_id"]),
                job_id=str(row["job_id"]),
                generation=int(row["generation"]),
                worker_id=worker,
                source_revision=str(row["source_revision"]),
                payload={"lease_epoch": epoch, "lease_expires_at": expires},
                now=now,
            )
        return CodeIndexBuildLease(
            job_id=str(row["job_id"]),
            workspace_id=str(row["workspace_id"]),
            worker_id=worker,
            token=token,
            epoch=epoch,
            generation=int(row["generation"]),
            source_revision=str(row["source_revision"]),
            expires_at=expires,
        )

    def start_build(self, lease: CodeIndexBuildLease) -> CodeIndexBuildJob:
        return self._transition(
            lease,
            expected=(CodeIndexJobState.LEASED,),
            target=CodeIndexJobState.BUILDING,
            event_type="code_index.building",
        )

    def begin_publish(self, lease: CodeIndexBuildLease) -> CodeIndexBuildJob:
        return self._transition(
            lease,
            expected=(CodeIndexJobState.BUILDING,),
            target=CodeIndexJobState.PUBLISHING,
            event_type="code_index.publishing",
            require_desired=True,
        )

    def mark_ready(
        self,
        lease: CodeIndexBuildLease,
        *,
        content_digest: str,
        counts: Mapping[str, int],
    ) -> CodeIndexBuildJob:
        now = float(self.clock())
        with self.store.transaction(immediate=True) as connection:
            self._require_live_lease(
                connection,
                lease,
                expected=(CodeIndexJobState.PUBLISHING,),
                require_desired=True,
                now=now,
            )
            connection.execute(
                """
                UPDATE code_index_jobs
                SET state = ?, published_at = ?, updated_at = ?,
                    lease_expires_at = 0, error_code = '', error_message = ''
                WHERE job_id = ?
                """,
                (CodeIndexJobState.READY.value, now, now, lease.job_id),
            )
            connection.execute(
                """
                UPDATE code_index_generation_fences
                SET published_generation = ?, published_revision = ?,
                    active_job_id = ?, updated_at = ?
                WHERE workspace_id = ? AND desired_generation = ?
                    AND source_revision = ?
                """,
                (
                    lease.generation,
                    lease.source_revision,
                    lease.job_id,
                    now,
                    lease.workspace_id,
                    lease.generation,
                    lease.source_revision,
                ),
            )
            if int(connection.execute("SELECT changes() AS count").fetchone()["count"]) != 1:
                raise CodeIndexPublicationFenced(
                    "code_index_ready_fenced",
                    "generation fence changed before ready transition",
                )
            self._audit(
                connection,
                event_type="code_index.ready",
                workspace_id=lease.workspace_id,
                job_id=lease.job_id,
                generation=lease.generation,
                worker_id=lease.worker_id,
                source_revision=lease.source_revision,
                payload={"content_digest": content_digest, **dict(counts)},
                now=now,
            )
            updated = connection.execute(
                "SELECT * FROM code_index_jobs WHERE job_id = ?",
                (lease.job_id,),
            ).fetchone()
        return self._row_to_job(updated)

    def renew(
        self,
        lease: CodeIndexBuildLease,
        *,
        lease_ttl_seconds: float,
    ) -> CodeIndexBuildLease:
        now = float(self.clock())
        expires = now + max(0.1, float(lease_ttl_seconds))
        with self.store.transaction(immediate=True) as connection:
            self._require_live_lease(
                connection,
                lease,
                expected=(
                    CodeIndexJobState.LEASED,
                    CodeIndexJobState.BUILDING,
                    CodeIndexJobState.PUBLISHING,
                ),
                now=now,
            )
            connection.execute(
                """
                UPDATE code_index_jobs SET lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND lease_owner = ? AND lease_token = ? AND lease_epoch = ?
                """,
                (expires, now, lease.job_id, lease.worker_id, lease.token, lease.epoch),
            )
            self._audit(
                connection,
                event_type="code_index.heartbeat",
                workspace_id=lease.workspace_id,
                job_id=lease.job_id,
                generation=lease.generation,
                worker_id=lease.worker_id,
                source_revision=lease.source_revision,
                payload={"lease_epoch": lease.epoch, "lease_expires_at": expires},
                now=now,
            )
        return CodeIndexBuildLease(
            job_id=lease.job_id,
            workspace_id=lease.workspace_id,
            worker_id=lease.worker_id,
            token=lease.token,
            epoch=lease.epoch,
            generation=lease.generation,
            source_revision=lease.source_revision,
            expires_at=expires,
        )

    def fail(
        self,
        lease: CodeIndexBuildLease,
        *,
        error_code: str,
        error_message: str,
    ) -> CodeIndexBuildJob:
        now = float(self.clock())
        with self.store.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM code_index_jobs WHERE job_id = ?",
                (lease.job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(lease.job_id)
            if not self._lease_matches(row, lease):
                raise CodeIndexLeaseLost(
                    "code_index_failure_lease_lost",
                    "worker cannot fail a job after losing its lease",
                )
            connection.execute(
                """
                UPDATE code_index_jobs
                SET state = ?, error_code = ?, error_message = ?,
                    lease_expires_at = 0, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    CodeIndexJobState.FAILED.value,
                    error_code,
                    error_message[:2_000],
                    now,
                    lease.job_id,
                ),
            )
            self._audit(
                connection,
                event_type="code_index.failed",
                workspace_id=lease.workspace_id,
                job_id=lease.job_id,
                generation=lease.generation,
                worker_id=lease.worker_id,
                source_revision=lease.source_revision,
                payload={"error_code": error_code, "error_message": error_message[:1_000]},
                now=now,
            )
            updated = connection.execute(
                "SELECT * FROM code_index_jobs WHERE job_id = ?",
                (lease.job_id,),
            ).fetchone()
        return self._row_to_job(updated)

    def sweep_expired(self) -> CodeIndexSweepResult:
        now = float(self.clock())
        stale: list[str] = []
        requeued: list[str] = []
        generations: list[int] = []
        with self.store.transaction(immediate=True) as connection:
            rows = connection.execute(
                """
                SELECT * FROM code_index_jobs
                WHERE state IN (?, ?, ?) AND lease_expires_at > 0 AND lease_expires_at <= ?
                ORDER BY lease_expires_at, job_id
                """,
                (
                    CodeIndexJobState.LEASED.value,
                    CodeIndexJobState.BUILDING.value,
                    CodeIndexJobState.PUBLISHING.value,
                    now,
                ),
            ).fetchall()
            for row in rows:
                job_id = str(row["job_id"])
                workspace_id = str(row["workspace_id"])
                fence = connection.execute(
                    "SELECT * FROM code_index_generation_fences WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()
                if fence is None:
                    continue
                connection.execute(
                    """
                    UPDATE code_index_jobs
                    SET state = ?, error_code = ?, error_message = ?,
                        lease_owner = '', lease_token = '', lease_expires_at = 0,
                        updated_at = ?
                    WHERE job_id = ? AND lease_epoch = ?
                    """,
                    (
                        CodeIndexJobState.STALE.value,
                        "lease_expired",
                        "worker lease expired before fenced publication",
                        now,
                        job_id,
                        int(row["lease_epoch"]),
                    ),
                )
                if int(connection.execute("SELECT changes() AS count").fetchone()["count"]) != 1:
                    continue
                desired = max(int(fence["desired_generation"]), int(row["generation"])) + 1
                new_job_id = "codejob_" + stable_digest(
                    workspace_id,
                    desired,
                    str(row["source_revision"]),
                    "lease-requeue",
                    job_id,
                )[:32]
                connection.execute(
                    """
                    UPDATE code_index_generation_fences
                    SET desired_generation = ?, active_job_id = ?, updated_at = ?
                    WHERE workspace_id = ?
                    """,
                    (desired, new_job_id, now, workspace_id),
                )
                connection.execute(
                    """
                    INSERT INTO code_index_jobs(
                        job_id, workspace_id, task_id, operation, state,
                        generation, source_revision, transaction_id,
                        changed_paths_json, deleted_paths_json, payload_json,
                        created_at, updated_at, causation_id, idempotency_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, '')
                    """,
                    (
                        new_job_id,
                        workspace_id,
                        str(row["task_id"]),
                        str(row["operation"]),
                        CodeIndexJobState.QUEUED.value,
                        desired,
                        str(row["source_revision"]),
                        str(row["changed_paths_json"]),
                        str(row["deleted_paths_json"]),
                        self._json(
                            {
                                **self._mapping(row["payload_json"]),
                                "requeued_from_job_id": job_id,
                                "requeue_reason": "lease_expired",
                            }
                        ),
                        now,
                        now,
                        str(row["causation_id"] or job_id),
                    ),
                )
                self._audit(
                    connection,
                    event_type="code_index.stale",
                    workspace_id=workspace_id,
                    job_id=job_id,
                    generation=int(row["generation"]),
                    worker_id=str(row["lease_owner"]),
                    source_revision=str(row["source_revision"]),
                    payload={"lease_epoch": int(row["lease_epoch"]), "requeued_job_id": new_job_id},
                    now=now,
                )
                self._audit(
                    connection,
                    event_type="code_index.requeued",
                    workspace_id=workspace_id,
                    job_id=new_job_id,
                    generation=desired,
                    source_revision=str(row["source_revision"]),
                    payload={"stale_job_id": job_id},
                    now=now,
                )
                stale.append(job_id)
                requeued.append(new_job_id)
                generations.append(desired)
        return CodeIndexSweepResult(tuple(stale), tuple(requeued), tuple(generations))

    def get(self, job_id: str) -> CodeIndexBuildJob | None:
        self.initialize()
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM code_index_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return None if row is None else self._row_to_job(row)

    def require(self, job_id: str) -> CodeIndexBuildJob:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def list(
        self,
        *,
        workspace_id: str = "",
        states: Sequence[CodeIndexJobState] = (),
        limit: int = 1_000,
    ) -> tuple[CodeIndexBuildJob, ...]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if workspace_id:
            clauses.append("workspace_id = ?")
            parameters.append(workspace_id)
        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            parameters.extend(state.value for state in states)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(0, min(int(limit), 10_000)))
        self.initialize()
        with self.store.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM code_index_jobs {where}
                ORDER BY created_at, job_id LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(self._row_to_job(row) for row in rows)

    def fence_state(self, workspace_id: str) -> Mapping[str, Any]:
        self.initialize()
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM code_index_generation_fences WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        return {} if row is None else dict(row)

    def audit_events(self, *, workspace_id: str, limit: int = 10_000) -> tuple[Mapping[str, Any], ...]:
        self.initialize()
        with self.store.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM code_index_job_audit
                WHERE workspace_id = ? ORDER BY audit_id LIMIT ?
                """,
                (workspace_id, max(0, min(int(limit), 100_000))),
            ).fetchall()
        return tuple(
            {**dict(row), "payload": self._mapping(row["payload_json"])} for row in rows
        )

    def assert_publishable(self, lease: CodeIndexBuildLease) -> None:
        now = float(self.clock())
        with self.store.connection() as connection:
            self._require_live_lease(
                connection,
                lease,
                expected=(CodeIndexJobState.PUBLISHING,),
                require_desired=True,
                now=now,
            )

    def _transition(
        self,
        lease: CodeIndexBuildLease,
        *,
        expected: Sequence[CodeIndexJobState],
        target: CodeIndexJobState,
        event_type: str,
        require_desired: bool = False,
    ) -> CodeIndexBuildJob:
        now = float(self.clock())
        with self.store.transaction(immediate=True) as connection:
            self._require_live_lease(
                connection,
                lease,
                expected=expected,
                require_desired=require_desired,
                now=now,
            )
            connection.execute(
                "UPDATE code_index_jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                (target.value, now, lease.job_id),
            )
            self._audit(
                connection,
                event_type=event_type,
                workspace_id=lease.workspace_id,
                job_id=lease.job_id,
                generation=lease.generation,
                worker_id=lease.worker_id,
                source_revision=lease.source_revision,
                payload={"lease_epoch": lease.epoch},
                now=now,
            )
            row = connection.execute(
                "SELECT * FROM code_index_jobs WHERE job_id = ?",
                (lease.job_id,),
            ).fetchone()
        return self._row_to_job(row)

    def _require_live_lease(
        self,
        connection: sqlite3.Connection,
        lease: CodeIndexBuildLease,
        *,
        expected: Sequence[CodeIndexJobState],
        require_desired: bool = False,
        now: float,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM code_index_jobs WHERE job_id = ?",
            (lease.job_id,),
        ).fetchone()
        if row is None:
            raise CodeIndexLeaseLost("code_index_job_missing", "leased code index job is missing")
        if not self._lease_matches(row, lease):
            raise CodeIndexLeaseLost(
                "code_index_lease_mismatch",
                "code index worker lease owner/token/epoch/generation does not match",
            )
        if CodeIndexJobState(str(row["state"])) not in set(expected):
            raise CodeIndexLeaseLost(
                "code_index_job_state_changed",
                f"code index job state is {row['state']}, expected {[state.value for state in expected]}",
            )
        if float(row["lease_expires_at"]) <= now:
            raise CodeIndexLeaseLost("code_index_lease_expired", "code index worker lease expired")
        if require_desired:
            fence = connection.execute(
                "SELECT * FROM code_index_generation_fences WHERE workspace_id = ?",
                (lease.workspace_id,),
            ).fetchone()
            if (
                fence is None
                or int(fence["desired_generation"]) != lease.generation
                or str(fence["source_revision"]) != lease.source_revision
                or str(fence["active_job_id"]) != lease.job_id
            ):
                raise CodeIndexPublicationFenced(
                    "code_index_generation_fenced",
                    "a newer workspace source generation superseded this build",
                )
        return row

    @staticmethod
    def _lease_matches(row: sqlite3.Row, lease: CodeIndexBuildLease) -> bool:
        return (
            str(row["workspace_id"]) == lease.workspace_id
            and str(row["lease_owner"]) == lease.worker_id
            and str(row["lease_token"]) == lease.token
            and int(row["lease_epoch"]) == lease.epoch
            and int(row["generation"]) == lease.generation
            and str(row["source_revision"]) == lease.source_revision
        )

    @staticmethod
    def _existing_idempotent(
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        idempotency_key: str,
        transaction_id: str,
    ) -> sqlite3.Row | None:
        if idempotency_key:
            row = connection.execute(
                """
                SELECT * FROM code_index_jobs
                WHERE workspace_id = ? AND idempotency_key = ?
                """,
                (workspace_id, idempotency_key),
            ).fetchone()
            if row is not None:
                return row
        if transaction_id:
            return connection.execute(
                """
                SELECT * FROM code_index_jobs
                WHERE workspace_id = ? AND transaction_id = ?
                """,
                (workspace_id, transaction_id),
            ).fetchone()
        return None

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        event_type: str,
        workspace_id: str,
        job_id: str,
        generation: int,
        source_revision: str,
        payload: Mapping[str, Any],
        now: float,
        worker_id: str = "",
    ) -> None:
        connection.execute(
            """
            INSERT INTO code_index_job_audit(
                event_type, workspace_id, job_id, generation, worker_id,
                source_revision, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                workspace_id,
                job_id,
                generation,
                worker_id,
                source_revision,
                json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                now,
            ),
        )

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def _list(value: Any) -> list[Any]:
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return list(parsed) if isinstance(parsed, list) else []

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        try:
            parsed = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}

    @classmethod
    def _row_to_job(cls, row: sqlite3.Row) -> CodeIndexBuildJob:
        return CodeIndexBuildJob(
            job_id=str(row["job_id"]),
            workspace_id=str(row["workspace_id"]),
            task_id=str(row["task_id"]),
            operation=CodeIndexJobOperation(str(row["operation"])),
            state=CodeIndexJobState(str(row["state"])),
            generation=int(row["generation"]),
            source_revision=str(row["source_revision"]),
            transaction_id=str(row["transaction_id"]),
            changed_paths=tuple(str(value) for value in cls._list(row["changed_paths_json"])),
            deleted_paths=tuple(str(value) for value in cls._list(row["deleted_paths_json"])),
            payload=cls._mapping(row["payload_json"]),
            lease_owner=str(row["lease_owner"]),
            lease_token=str(row["lease_token"]),
            lease_epoch=int(row["lease_epoch"]),
            lease_expires_at=float(row["lease_expires_at"]),
            attempt=int(row["attempt"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            started_at=float(row["started_at"]),
            published_at=float(row["published_at"]),
            error_code=str(row["error_code"]),
            error_message=str(row["error_message"]),
            causation_id=str(row["causation_id"]),
            idempotency_key=str(row["idempotency_key"]),
        )


__all__ = ["CodeIndexBuildQueue"]
