from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence, TypeVar

from .errors import StoreConflict, WorkerNotFound, WorkerPoolError, WorkerPoolErrorCode
from .models import (
    AttemptState,
    CancellationReceipt,
    CapabilityAttestation,
    ExecutionReceipt,
    InboxEnvelope,
    InboxMessageState,
    LeaseState,
    PoolJournalRecord,
    TaskAttempt,
    WakeupRecord,
    WakeupState,
    WorkerCapabilityManifest,
    WorkerHeartbeat,
    WorkerInstance,
    WorkerLease,
    WorkerLifecycleState,
    WorkerLocation,
    WorkerTelemetry,
    canonical_json,
    stable_digest,
    utc_iso,
)


T = TypeVar("T")


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc, traceback))
        finally:
            self.close()


class WorkerPoolStore:
    """Durable canonical owner for physical worker, attempt, lease, and inbox state.

    The store intentionally does not persist a copy of the logical AgentTask.  Every
    physical attempt references the stable task id owned by the 03D task runtime.
    All state-changing operations use SQLite ``BEGIN IMMEDIATE`` transactions and
    compare-and-swap versions so a restarted API process cannot silently overwrite
    a concurrent lease or worker transition.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                PRAGMA synchronous=FULL;
                PRAGMA busy_timeout=5000;

                CREATE TABLE IF NOT EXISTS worker_pool_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS worker_manifests (
                    worker_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    location TEXT NOT NULL,
                    worker_kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (worker_id, revision),
                    UNIQUE (worker_id, digest)
                );

                CREATE TABLE IF NOT EXISTS worker_attestations (
                    attestation_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    process_identity TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_attestation_worker
                    ON worker_attestations(worker_id, observed_at DESC);

                CREATE TABLE IF NOT EXISTS worker_instances (
                    worker_id TEXT PRIMARY KEY,
                    worker_kind TEXT NOT NULL,
                    location TEXT NOT NULL,
                    backend_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    attestation_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    process_identity TEXT NOT NULL,
                    last_heartbeat_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_worker_state
                    ON worker_instances(state, location, worker_kind);

                CREATE TABLE IF NOT EXISTS task_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(task_id, attempt_number)
                );

                CREATE INDEX IF NOT EXISTS idx_attempt_task
                    ON task_attempts(task_id, attempt_number DESC);
                CREATE INDEX IF NOT EXISTS idx_attempt_worker_state
                    ON task_attempts(worker_id, state);

                CREATE TABLE IF NOT EXISTS worker_leases (
                    lease_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL UNIQUE,
                    worker_id TEXT NOT NULL,
                    owner_session_id TEXT NOT NULL,
                    backend_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    fence_epoch INTEGER NOT NULL,
                    fence_token_hash TEXT NOT NULL,
                    deadline_at TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_lease_worker_state
                    ON worker_leases(worker_id, state, deadline_at);
                CREATE INDEX IF NOT EXISTS idx_lease_task_state
                    ON worker_leases(task_id, state, deadline_at);

                CREATE TABLE IF NOT EXISTS worker_heartbeats (
                    heartbeat_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    sequence INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(worker_id, generation, sequence)
                );

                CREATE INDEX IF NOT EXISTS idx_heartbeat_worker
                    ON worker_heartbeats(worker_id, observed_at DESC);

                CREATE TABLE IF NOT EXISTS worker_telemetry (
                    worker_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    sequence INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(worker_id, generation, sequence)
                );

                CREATE TABLE IF NOT EXISTS execution_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    fence_epoch INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    output_digest TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(attempt_id, output_digest)
                );

                CREATE INDEX IF NOT EXISTS idx_receipt_task
                    ON execution_receipts(task_id, finished_at DESC);

                CREATE TABLE IF NOT EXISTS worker_inbox (
                    envelope_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    message_kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    available_at TEXT NOT NULL,
                    claim_owner TEXT NOT NULL,
                    claim_deadline_at TEXT NOT NULL,
                    delivery_count INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_inbox_claim
                    ON worker_inbox(worker_id, state, available_at, priority, updated_at);

                CREATE TABLE IF NOT EXISTS worker_wakeups (
                    wakeup_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    envelope_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    claim_owner TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_wakeup_claim
                    ON worker_wakeups(state, available_at, updated_at);

                CREATE TABLE IF NOT EXISTS cancellation_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    changed INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS worker_pool_idempotency (
                    scope TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    result_type TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(scope, idempotency_key)
                );

                CREATE TABLE IF NOT EXISTS worker_pool_journal (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    journal_id TEXT NOT NULL UNIQUE,
                    aggregate_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    causation_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_pool_journal_aggregate
                    ON worker_pool_journal(aggregate_type, aggregate_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_pool_journal_task
                    ON worker_pool_journal(task_id, sequence);
                """
            )
            now = utc_iso()
            connection.execute(
                """
                INSERT INTO worker_pool_meta(key, value, updated_at)
                VALUES('schema_version', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (str(self.SCHEMA_VERSION), now),
            )
            connection.execute(
                """
                INSERT INTO worker_pool_meta(key, value, updated_at)
                VALUES('pool_revision', '0', ?)
                ON CONFLICT(key) DO NOTHING
                """,
                (now,),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def register_manifest(
        self,
        manifest: WorkerCapabilityManifest,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> WorkerCapabilityManifest:
        if connection is None:
            with self.transaction() as current:
                return self.register_manifest(manifest, connection=current)
        row = connection.execute(
            "SELECT payload_json FROM worker_manifests WHERE worker_id=? AND digest=?",
            (manifest.worker_id, manifest.digest),
        ).fetchone()
        if row is not None:
            return WorkerCapabilityManifest.from_dict(self._decode(row["payload_json"]))
        latest = connection.execute(
            "SELECT MAX(revision) AS revision FROM worker_manifests WHERE worker_id=?",
            (manifest.worker_id,),
        ).fetchone()
        expected_revision = int(latest["revision"] or 0) + 1
        if manifest.manifest_revision != expected_revision:
            manifest = WorkerCapabilityManifest.from_dict(
                {**manifest.to_dict(), "manifest_revision": expected_revision, "digest": ""}
            )
        connection.execute(
            """
            INSERT INTO worker_manifests(
                worker_id, revision, digest, location, worker_kind, payload_json, created_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                manifest.worker_id,
                manifest.manifest_revision,
                manifest.digest,
                manifest.location.value,
                manifest.worker_kind,
                canonical_json(manifest),
                manifest.created_at,
            ),
        )
        self._journal(
            connection,
            PoolJournalRecord(
                aggregate_type="worker_manifest",
                aggregate_id=manifest.worker_id,
                operation="manifest_registered",
                payload={"revision": manifest.manifest_revision, "digest": manifest.digest},
            ),
        )
        self._bump_revision(connection)
        return manifest

    def latest_manifest(self, worker_id: str) -> WorkerCapabilityManifest | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM worker_manifests
                WHERE worker_id=? ORDER BY revision DESC LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
        return None if row is None else WorkerCapabilityManifest.from_dict(self._decode(row["payload_json"]))

    def manifest_by_digest(self, worker_id: str, digest: str) -> WorkerCapabilityManifest | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM worker_manifests WHERE worker_id=? AND digest=?",
                (worker_id, digest),
            ).fetchone()
        return None if row is None else WorkerCapabilityManifest.from_dict(self._decode(row["payload_json"]))

    def list_manifests(self, *, current_only: bool = True) -> tuple[WorkerCapabilityManifest, ...]:
        with self._connect() as connection:
            if current_only:
                rows = connection.execute(
                    """
                    SELECT m.payload_json
                    FROM worker_manifests AS m
                    JOIN (
                        SELECT worker_id, MAX(revision) AS revision
                        FROM worker_manifests GROUP BY worker_id
                    ) AS latest
                    ON latest.worker_id=m.worker_id AND latest.revision=m.revision
                    ORDER BY m.worker_id
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT payload_json FROM worker_manifests ORDER BY worker_id, revision"
                ).fetchall()
        return tuple(WorkerCapabilityManifest.from_dict(self._decode(row["payload_json"])) for row in rows)

    def save_attestation(
        self,
        attestation: CapabilityAttestation,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> CapabilityAttestation:
        if connection is None:
            with self.transaction() as current:
                return self.save_attestation(attestation, connection=current)
        connection.execute(
            """
            INSERT INTO worker_attestations(
                attestation_id, worker_id, manifest_digest, process_identity, endpoint,
                observed_at, expires_at, payload_json
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                attestation.attestation_id,
                attestation.worker_id,
                attestation.manifest_digest,
                attestation.process_identity,
                attestation.endpoint,
                attestation.observed_at,
                attestation.expires_at,
                canonical_json(attestation),
            ),
        )
        self._journal(
            connection,
            PoolJournalRecord(
                aggregate_type="worker",
                aggregate_id=attestation.worker_id,
                operation="capability_attested",
                payload={
                    "attestation_id": attestation.attestation_id,
                    "manifest_digest": attestation.manifest_digest,
                    "process_identity": attestation.process_identity,
                    "endpoint": attestation.endpoint,
                },
            ),
        )
        self._bump_revision(connection)
        return attestation

    def latest_attestation(self, worker_id: str) -> CapabilityAttestation | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM worker_attestations
                WHERE worker_id=? ORDER BY observed_at DESC LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
        if row is None:
            return None
        data = self._decode(row["payload_json"])
        return CapabilityAttestation(**data)

    def insert_worker(
        self,
        worker: WorkerInstance,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> WorkerInstance:
        if connection is None:
            with self.transaction() as current:
                return self.insert_worker(worker, connection=current)
        existing = connection.execute(
            "SELECT payload_json FROM worker_instances WHERE worker_id=?",
            (worker.worker_id,),
        ).fetchone()
        if existing is not None:
            current = WorkerInstance.from_dict(self._decode(existing["payload_json"]))
            if current.process_identity == worker.process_identity and current.generation == worker.generation:
                return current
            raise WorkerPoolError(
                WorkerPoolErrorCode.WORKER_ALREADY_EXISTS,
                "worker id is already registered by another process generation",
                operation="insert_worker",
                worker_id=worker.worker_id,
            )
        self._write_worker(connection, worker, insert=True)
        self._journal(
            connection,
            PoolJournalRecord(
                aggregate_type="worker",
                aggregate_id=worker.worker_id,
                operation="worker_registered",
                payload=worker.to_dict(),
            ),
        )
        self._bump_revision(connection)
        return worker

    def upsert_worker_generation(
        self,
        worker: WorkerInstance,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> WorkerInstance:
        if connection is None:
            with self.transaction() as current:
                return self.upsert_worker_generation(worker, connection=current)
        row = connection.execute(
            "SELECT payload_json FROM worker_instances WHERE worker_id=?",
            (worker.worker_id,),
        ).fetchone()
        if row is None:
            return self.insert_worker(worker, connection=connection)
        current = WorkerInstance.from_dict(self._decode(row["payload_json"]))
        if worker.generation <= current.generation:
            if worker.process_identity == current.process_identity:
                return current
            raise StoreConflict(
                "replacement worker generation must be greater than the stored generation",
                operation="upsert_worker_generation",
                metadata={"stored_generation": current.generation, "requested_generation": worker.generation},
            )
        self._write_worker(connection, worker, insert=False, expected_version=current.version)
        self._journal(
            connection,
            PoolJournalRecord(
                aggregate_type="worker",
                aggregate_id=worker.worker_id,
                operation="worker_generation_replaced",
                payload={
                    "previous_generation": current.generation,
                    "generation": worker.generation,
                    "process_identity": worker.process_identity,
                },
            ),
        )
        self._bump_revision(connection)
        return worker

    def update_worker(
        self,
        worker: WorkerInstance,
        *,
        expected_version: int,
        operation: str,
        journal_payload: Mapping[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> WorkerInstance:
        if connection is None:
            with self.transaction() as current:
                return self.update_worker(
                    worker,
                    expected_version=expected_version,
                    operation=operation,
                    journal_payload=journal_payload,
                    connection=current,
                )
        self._write_worker(connection, worker, insert=False, expected_version=expected_version)
        self._journal(
            connection,
            PoolJournalRecord(
                aggregate_type="worker",
                aggregate_id=worker.worker_id,
                operation=operation,
                payload=dict(journal_payload or worker.to_dict()),
            ),
        )
        self._bump_revision(connection)
        return worker

    def get_worker(self, worker_id: str) -> WorkerInstance | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM worker_instances WHERE worker_id=?",
                (worker_id,),
            ).fetchone()
        return None if row is None else WorkerInstance.from_dict(self._decode(row["payload_json"]))

    def require_worker(self, worker_id: str, *, connection: sqlite3.Connection | None = None) -> WorkerInstance:
        if connection is None:
            worker = self.get_worker(worker_id)
        else:
            row = connection.execute(
                "SELECT payload_json FROM worker_instances WHERE worker_id=?",
                (worker_id,),
            ).fetchone()
            worker = None if row is None else WorkerInstance.from_dict(self._decode(row["payload_json"]))
        if worker is None:
            raise WorkerNotFound(worker_id, operation="require_worker")
        return worker

    def list_workers(
        self,
        *,
        states: Sequence[WorkerLifecycleState] = (),
        locations: Sequence[WorkerLocation] = (),
    ) -> tuple[WorkerInstance, ...]:
        clauses: list[str] = []
        params: list[Any] = []
        if states:
            clauses.append(f"state IN ({','.join('?' for _ in states)})")
            params.extend(item.value for item in states)
        if locations:
            clauses.append(f"location IN ({','.join('?' for _ in locations)})")
            params.extend(item.value for item in locations)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT payload_json FROM worker_instances{where} ORDER BY worker_id",  # noqa: S608
                params,
            ).fetchall()
        return tuple(WorkerInstance.from_dict(self._decode(row["payload_json"])) for row in rows)

    def insert_attempt(
        self,
        attempt: TaskAttempt,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> TaskAttempt:
        if connection is None:
            with self.transaction() as current:
                return self.insert_attempt(attempt, connection=current)
        row = connection.execute(
            "SELECT payload_json FROM task_attempts WHERE task_id=? AND attempt_number=?",
            (attempt.task_id, attempt.attempt_number),
        ).fetchone()
        if row is not None:
            current = TaskAttempt.from_dict(self._decode(row["payload_json"]))
            if current.attempt_id == attempt.attempt_id:
                return current
            raise WorkerPoolError(
                WorkerPoolErrorCode.ATTEMPT_CONFLICT,
                "logical task attempt number is already assigned",
                operation="insert_attempt",
                task_id=attempt.task_id,
                attempt_id=attempt.attempt_id,
            )
        connection.execute(
            """
            INSERT INTO task_attempts(
                attempt_id, task_id, run_id, attempt_number, state, worker_id,
                lease_id, version, payload_json, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                attempt.attempt_id,
                attempt.task_id,
                attempt.run_id,
                attempt.attempt_number,
                attempt.state.value,
                attempt.worker_id,
                attempt.lease_id,
                attempt.version,
                canonical_json(attempt),
                utc_iso(),
            ),
        )
        self._journal(
            connection,
            PoolJournalRecord(
                aggregate_type="task_attempt",
                aggregate_id=attempt.attempt_id,
                operation="attempt_created",
                run_id=attempt.run_id,
                task_id=attempt.task_id,
                payload={
                    "attempt_number": attempt.attempt_number,
                    "parent_attempt_id": attempt.parent_attempt_id,
                    "recovery_reason": attempt.recovery_reason,
                },
            ),
        )
        self._bump_revision(connection)
        return attempt

    def update_attempt(
        self,
        attempt: TaskAttempt,
        *,
        expected_version: int,
        operation: str,
        journal_payload: Mapping[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> TaskAttempt:
        if connection is None:
            with self.transaction() as current:
                return self.update_attempt(
                    attempt,
                    expected_version=expected_version,
                    operation=operation,
                    journal_payload=journal_payload,
                    connection=current,
                )
        cursor = connection.execute(
            """
            UPDATE task_attempts SET
                state=?, worker_id=?, lease_id=?, version=?, payload_json=?, updated_at=?
            WHERE attempt_id=? AND version=?
            """,
            (
                attempt.state.value,
                attempt.worker_id,
                attempt.lease_id,
                attempt.version,
                canonical_json(attempt),
                utc_iso(),
                attempt.attempt_id,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise StoreConflict(
                "task attempt compare-and-swap failed",
                operation=operation,
                metadata={"attempt_id": attempt.attempt_id, "expected_version": expected_version},
            )
        self._journal(
            connection,
            PoolJournalRecord(
                aggregate_type="task_attempt",
                aggregate_id=attempt.attempt_id,
                operation=operation,
                run_id=attempt.run_id,
                task_id=attempt.task_id,
                payload=dict(journal_payload or attempt.to_dict()),
            ),
        )
        self._bump_revision(connection)
        return attempt

    def get_attempt(self, attempt_id: str) -> TaskAttempt | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM task_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
        return None if row is None else TaskAttempt.from_dict(self._decode(row["payload_json"]))

    def require_attempt(self, attempt_id: str, *, connection: sqlite3.Connection | None = None) -> TaskAttempt:
        if connection is None:
            attempt = self.get_attempt(attempt_id)
        else:
            row = connection.execute(
                "SELECT payload_json FROM task_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            attempt = None if row is None else TaskAttempt.from_dict(self._decode(row["payload_json"]))
        if attempt is None:
            raise WorkerPoolError(
                WorkerPoolErrorCode.ATTEMPT_NOT_FOUND,
                "physical task attempt was not found",
                operation="require_attempt",
                attempt_id=attempt_id,
            )
        return attempt

    def latest_attempt(self, task_id: str) -> TaskAttempt | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM task_attempts
                WHERE task_id=? ORDER BY attempt_number DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        return None if row is None else TaskAttempt.from_dict(self._decode(row["payload_json"]))

    def list_attempts(
        self,
        *,
        task_id: str = "",
        worker_id: str = "",
        states: Sequence[AttemptState] = (),
    ) -> tuple[TaskAttempt, ...]:
        clauses: list[str] = []
        params: list[Any] = []
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if worker_id:
            clauses.append("worker_id=?")
            params.append(worker_id)
        if states:
            clauses.append(f"state IN ({','.join('?' for _ in states)})")
            params.extend(item.value for item in states)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT payload_json FROM task_attempts{where} ORDER BY task_id, attempt_number",  # noqa: S608
                params,
            ).fetchall()
        return tuple(TaskAttempt.from_dict(self._decode(row["payload_json"])) for row in rows)

    def insert_lease(
        self,
        lease: WorkerLease,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> WorkerLease:
        if connection is None:
            with self.transaction() as current:
                return self.insert_lease(lease, connection=current)
        row = connection.execute(
            "SELECT payload_json FROM worker_leases WHERE idempotency_key=?",
            (lease.idempotency_key,),
        ).fetchone()
        if row is not None:
            current = WorkerLease.from_dict(self._decode(row["payload_json"]))
            if current.attempt_id != lease.attempt_id or current.worker_id != lease.worker_id:
                raise WorkerPoolError(
                    WorkerPoolErrorCode.IDEMPOTENCY_CONFLICT,
                    "lease idempotency key was reused for a different acquisition",
                    operation="insert_lease",
                    lease_id=current.lease_id,
                    task_id=lease.task_id,
                )
            return current
        connection.execute(
            """
            INSERT INTO worker_leases(
                lease_id, task_id, run_id, attempt_id, worker_id, owner_session_id,
                backend_id, state, fence_epoch, fence_token_hash, deadline_at,
                version, idempotency_key, payload_json, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                lease.lease_id,
                lease.task_id,
                lease.run_id,
                lease.attempt_id,
                lease.worker_id,
                lease.owner_session_id,
                lease.backend_id,
                lease.state.value,
                lease.fence_epoch,
                stable_digest(lease.fence_token),
                lease.deadline_at,
                lease.version,
                lease.idempotency_key,
                canonical_json(lease),
                utc_iso(),
            ),
        )
        self._journal(
            connection,
            PoolJournalRecord(
                aggregate_type="worker_lease",
                aggregate_id=lease.lease_id,
                operation="lease_acquired",
                run_id=lease.run_id,
                task_id=lease.task_id,
                payload={
                    "attempt_id": lease.attempt_id,
                    "worker_id": lease.worker_id,
                    "owner_session_id": lease.owner_session_id,
                    "backend_id": lease.backend_id,
                    "deadline_at": lease.deadline_at,
                    "fence_epoch": lease.fence_epoch,
                    "resources": lease.resources.to_dict(),
                },
            ),
        )
        self._bump_revision(connection)
        return lease

    def update_lease(
        self,
        lease: WorkerLease,
        *,
        expected_version: int,
        operation: str,
        journal_payload: Mapping[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> WorkerLease:
        if connection is None:
            with self.transaction() as current:
                return self.update_lease(
                    lease,
                    expected_version=expected_version,
                    operation=operation,
                    journal_payload=journal_payload,
                    connection=current,
                )
        cursor = connection.execute(
            """
            UPDATE worker_leases SET
                state=?, fence_epoch=?, fence_token_hash=?, deadline_at=?, version=?,
                payload_json=?, updated_at=?
            WHERE lease_id=? AND version=?
            """,
            (
                lease.state.value,
                lease.fence_epoch,
                stable_digest(lease.fence_token),
                lease.deadline_at,
                lease.version,
                canonical_json(lease),
                utc_iso(),
                lease.lease_id,
                expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise StoreConflict(
                "worker lease compare-and-swap failed",
                operation=operation,
                metadata={"lease_id": lease.lease_id, "expected_version": expected_version},
            )
        self._journal(
            connection,
            PoolJournalRecord(
                aggregate_type="worker_lease",
                aggregate_id=lease.lease_id,
                operation=operation,
                run_id=lease.run_id,
                task_id=lease.task_id,
                payload=dict(journal_payload or lease.to_dict()),
            ),
        )
        self._bump_revision(connection)
        return lease

    def get_lease(self, lease_id: str) -> WorkerLease | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM worker_leases WHERE lease_id=?",
                (lease_id,),
            ).fetchone()
        return None if row is None else WorkerLease.from_dict(self._decode(row["payload_json"]))

    def lease_for_attempt(self, attempt_id: str) -> WorkerLease | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM worker_leases WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
        return None if row is None else WorkerLease.from_dict(self._decode(row["payload_json"]))

    def require_lease(self, lease_id: str, *, connection: sqlite3.Connection | None = None) -> WorkerLease:
        if connection is None:
            lease = self.get_lease(lease_id)
        else:
            row = connection.execute(
                "SELECT payload_json FROM worker_leases WHERE lease_id=?",
                (lease_id,),
            ).fetchone()
            lease = None if row is None else WorkerLease.from_dict(self._decode(row["payload_json"]))
        if lease is None:
            raise WorkerPoolError(
                WorkerPoolErrorCode.LEASE_NOT_FOUND,
                "worker lease was not found",
                operation="require_lease",
                lease_id=lease_id,
            )
        return lease

    def list_leases(
        self,
        *,
        task_id: str = "",
        worker_id: str = "",
        states: Sequence[LeaseState] = (),
    ) -> tuple[WorkerLease, ...]:
        clauses: list[str] = []
        params: list[Any] = []
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if worker_id:
            clauses.append("worker_id=?")
            params.append(worker_id)
        if states:
            clauses.append(f"state IN ({','.join('?' for _ in states)})")
            params.extend(item.value for item in states)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT payload_json FROM worker_leases{where} ORDER BY updated_at, lease_id",  # noqa: S608
                params,
            ).fetchall()
        return tuple(WorkerLease.from_dict(self._decode(row["payload_json"])) for row in rows)

    def save_heartbeat(
        self,
        heartbeat: WorkerHeartbeat,
        *,
        worker: WorkerInstance,
        connection: sqlite3.Connection | None = None,
    ) -> WorkerHeartbeat:
        if connection is None:
            with self.transaction() as current:
                return self.save_heartbeat(heartbeat, worker=worker, connection=current)
        row = connection.execute(
            """
            SELECT payload_json FROM worker_heartbeats
            WHERE worker_id=? AND generation=? ORDER BY sequence DESC LIMIT 1
            """,
            (heartbeat.worker_id, heartbeat.worker_generation),
        ).fetchone()
        if row is not None:
            latest = WorkerHeartbeat.from_dict(self._decode(row["payload_json"]))
            if heartbeat.sequence <= latest.sequence:
                if heartbeat.sequence == latest.sequence and heartbeat.heartbeat_id == latest.heartbeat_id:
                    return latest
                raise StoreConflict(
                    "heartbeat sequence must increase monotonically",
                    operation="save_heartbeat",
                    metadata={"latest_sequence": latest.sequence, "received_sequence": heartbeat.sequence},
                )
        connection.execute(
            """
            INSERT INTO worker_heartbeats(
                heartbeat_id, worker_id, generation, sequence, observed_at, payload_json
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                heartbeat.heartbeat_id,
                heartbeat.worker_id,
                heartbeat.worker_generation,
                heartbeat.sequence,
                heartbeat.observed_at,
                canonical_json(heartbeat),
            ),
        )
        connection.execute(
            """
            INSERT INTO worker_telemetry(worker_id, generation, sequence, observed_at, payload_json)
            VALUES(?,?,?,?,?)
            """,
            (
                heartbeat.worker_id,
                heartbeat.worker_generation,
                heartbeat.sequence,
                heartbeat.telemetry.observed_at,
                canonical_json(heartbeat.telemetry),
            ),
        )
        self._write_worker(connection, worker, insert=False, expected_version=worker.version - 1)
        self._journal(
            connection,
            PoolJournalRecord(
                aggregate_type="worker",
                aggregate_id=heartbeat.worker_id,
                operation="heartbeat_observed",
                payload={
                    "heartbeat_id": heartbeat.heartbeat_id,
                    "sequence": heartbeat.sequence,
                    "active_lease_ids": list(heartbeat.active_lease_ids),
                    "overcommitted": heartbeat.telemetry.overcommitted,
                    "telemetry": heartbeat.telemetry.to_dict(),
                },
            ),
        )
        self._bump_revision(connection)
        return heartbeat

    def latest_heartbeat(self, worker_id: str) -> WorkerHeartbeat | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM worker_heartbeats
                WHERE worker_id=? ORDER BY generation DESC, sequence DESC LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
        return None if row is None else WorkerHeartbeat.from_dict(self._decode(row["payload_json"]))

    def latest_telemetry(self, worker_id: str) -> WorkerTelemetry | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM worker_telemetry
                WHERE worker_id=? ORDER BY generation DESC, sequence DESC LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
        return None if row is None else WorkerTelemetry.from_dict(self._decode(row["payload_json"]))

    def append_execution_receipt(
        self,
        receipt: ExecutionReceipt,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> ExecutionReceipt:
        if connection is None:
            with self.transaction() as current:
                return self.append_execution_receipt(receipt, connection=current)
        row = connection.execute(
            "SELECT payload_json FROM execution_receipts WHERE attempt_id=? AND output_digest=?",
            (receipt.attempt_id, receipt.output_digest),
        ).fetchone()
        if row is not None:
            return ExecutionReceipt.from_dict(self._decode(row["payload_json"]))
        connection.execute(
            """
            INSERT INTO execution_receipts(
                receipt_id, task_id, attempt_id, lease_id, worker_id, fence_epoch,
                outcome, output_digest, finished_at, payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                receipt.receipt_id,
                receipt.task_id,
                receipt.attempt_id,
                receipt.lease_id,
                receipt.worker_id,
                receipt.fence_epoch,
                receipt.outcome.value,
                receipt.output_digest,
                receipt.finished_at,
                canonical_json(receipt),
            ),
        )
        self._journal(
            connection,
            PoolJournalRecord(
                aggregate_type="execution_receipt",
                aggregate_id=receipt.receipt_id,
                operation="execution_receipt_committed",
                run_id=receipt.run_id,
                task_id=receipt.task_id,
                payload=receipt.to_dict(),
            ),
        )
        self._bump_revision(connection)
        return receipt

    def receipts_for_task(self, task_id: str) -> tuple[ExecutionReceipt, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM execution_receipts WHERE task_id=? ORDER BY finished_at",
                (task_id,),
            ).fetchall()
        return tuple(ExecutionReceipt.from_dict(self._decode(row["payload_json"])) for row in rows)

    def enqueue_inbox(
        self,
        envelope: InboxEnvelope,
        *,
        wakeup: WakeupRecord | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[InboxEnvelope, WakeupRecord | None]:
        if connection is None:
            with self.transaction() as current:
                return self.enqueue_inbox(envelope, wakeup=wakeup, connection=current)
        row = connection.execute(
            "SELECT payload_json FROM worker_inbox WHERE idempotency_key=?",
            (envelope.idempotency_key,),
        ).fetchone()
        if row is not None:
            existing = InboxEnvelope.from_dict(self._decode(row["payload_json"]))
            existing_wakeup = self._wakeup_for_envelope(connection, existing.envelope_id)
            return existing, existing_wakeup
        self._write_inbox(connection, envelope, insert=True)
        if wakeup is not None:
            self._write_wakeup(connection, wakeup, insert=True)
        self._journal(
            connection,
            PoolJournalRecord(
                aggregate_type="worker_inbox",
                aggregate_id=envelope.envelope_id,
                operation="inbox_enqueued",
                run_id=envelope.run_id,
                task_id=envelope.task_id,
                causation_id=envelope.causation_id,
                correlation_id=envelope.correlation_id,
                payload={
                    "worker_id": envelope.worker_id,
                    "message_kind": envelope.message_kind,
                    "wakeup_id": wakeup.wakeup_id if wakeup else "",
                },
            ),
        )
        self._bump_revision(connection)
        return envelope, wakeup

    def claim_inbox(
        self,
        worker_id: str,
        *,
        claim_owner: str,
        claim_deadline_at: str,
        limit: int,
        now: str | None = None,
    ) -> tuple[InboxEnvelope, ...]:
        current_time = now or utc_iso()
        claimed: list[InboxEnvelope] = []
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM worker_inbox
                WHERE worker_id=?
                  AND state IN (?,?)
                  AND available_at<=?
                  AND (claim_deadline_at='' OR claim_deadline_at<=?)
                ORDER BY priority ASC, updated_at ASC, envelope_id ASC
                LIMIT ?
                """,
                (
                    worker_id,
                    InboxMessageState.PENDING.value,
                    InboxMessageState.REQUEUED.value,
                    current_time,
                    current_time,
                    max(1, int(limit)),
                ),
            ).fetchall()
            for row in rows:
                envelope = InboxEnvelope.from_dict(self._decode(row["payload_json"]))
                next_state = InboxMessageState.DEAD_LETTERED if envelope.delivery_count >= envelope.max_deliveries else InboxMessageState.CLAIMED
                updated = InboxEnvelope.from_dict(
                    {
                        **envelope.to_dict(),
                        "state": next_state.value,
                        "claimed_at": current_time,
                        "claim_owner": claim_owner,
                        "claim_deadline_at": claim_deadline_at,
                        "delivery_count": envelope.delivery_count + 1,
                        "version": envelope.version + 1,
                        "failure_reason": "delivery limit exceeded" if next_state is InboxMessageState.DEAD_LETTERED else "",
                    }
                )
                self._write_inbox(connection, updated, insert=False, expected_version=envelope.version)
                self._journal(
                    connection,
                    PoolJournalRecord(
                        aggregate_type="worker_inbox",
                        aggregate_id=updated.envelope_id,
                        operation="inbox_dead_lettered" if updated.state is InboxMessageState.DEAD_LETTERED else "inbox_claimed",
                        run_id=updated.run_id,
                        task_id=updated.task_id,
                        payload={
                            "claim_owner": claim_owner,
                            "delivery_count": updated.delivery_count,
                            "claim_deadline_at": updated.claim_deadline_at,
                        },
                    ),
                )
                if updated.state is InboxMessageState.CLAIMED:
                    claimed.append(updated)
            if rows:
                self._bump_revision(connection)
        return tuple(claimed)

    def update_inbox(
        self,
        envelope: InboxEnvelope,
        *,
        expected_version: int,
        operation: str,
        connection: sqlite3.Connection | None = None,
    ) -> InboxEnvelope:
        if connection is None:
            with self.transaction() as current:
                return self.update_inbox(
                    envelope,
                    expected_version=expected_version,
                    operation=operation,
                    connection=current,
                )
        self._write_inbox(connection, envelope, insert=False, expected_version=expected_version)
        self._journal(
            connection,
            PoolJournalRecord(
                aggregate_type="worker_inbox",
                aggregate_id=envelope.envelope_id,
                operation=operation,
                run_id=envelope.run_id,
                task_id=envelope.task_id,
                payload={
                    "state": envelope.state.value,
                    "claim_owner": envelope.claim_owner,
                    "delivery_count": envelope.delivery_count,
                    "failure_reason": envelope.failure_reason,
                },
            ),
        )
        self._bump_revision(connection)
        return envelope

    def get_inbox(self, envelope_id: str) -> InboxEnvelope | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM worker_inbox WHERE envelope_id=?",
                (envelope_id,),
            ).fetchone()
        return None if row is None else InboxEnvelope.from_dict(self._decode(row["payload_json"]))

    def list_inbox(
        self,
        *,
        worker_id: str = "",
        task_id: str = "",
        states: Sequence[InboxMessageState] = (),
    ) -> tuple[InboxEnvelope, ...]:
        clauses: list[str] = []
        params: list[Any] = []
        if worker_id:
            clauses.append("worker_id=?")
            params.append(worker_id)
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if states:
            clauses.append(f"state IN ({','.join('?' for _ in states)})")
            params.extend(item.value for item in states)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT payload_json FROM worker_inbox{where} ORDER BY updated_at",  # noqa: S608
                params,
            ).fetchall()
        return tuple(InboxEnvelope.from_dict(self._decode(row["payload_json"])) for row in rows)

    def claim_wakeups(
        self,
        *,
        claim_owner: str,
        limit: int,
        now: str | None = None,
    ) -> tuple[WakeupRecord, ...]:
        current_time = now or utc_iso()
        claimed: list[WakeupRecord] = []
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM worker_wakeups
                WHERE state IN (?,?) AND available_at<=?
                ORDER BY updated_at ASC, wakeup_id ASC LIMIT ?
                """,
                (
                    WakeupState.QUEUED.value,
                    WakeupState.REQUEUED.value,
                    current_time,
                    max(1, int(limit)),
                ),
            ).fetchall()
            for row in rows:
                current = WakeupRecord.from_dict(self._decode(row["payload_json"]))
                updated = WakeupRecord.from_dict(
                    {
                        **current.to_dict(),
                        "state": WakeupState.CLAIMED.value,
                        "claim_owner": claim_owner,
                        "claimed_at": current_time,
                        "attempts": current.attempts + 1,
                        "version": current.version + 1,
                    }
                )
                self._write_wakeup(connection, updated, insert=False, expected_version=current.version)
                claimed.append(updated)
            if rows:
                self._bump_revision(connection)
        return tuple(claimed)

    def update_wakeup(
        self,
        wakeup: WakeupRecord,
        *,
        expected_version: int,
        operation: str,
    ) -> WakeupRecord:
        with self.transaction() as connection:
            self._write_wakeup(connection, wakeup, insert=False, expected_version=expected_version)
            self._journal(
                connection,
                PoolJournalRecord(
                    aggregate_type="worker_wakeup",
                    aggregate_id=wakeup.wakeup_id,
                    operation=operation,
                    task_id=wakeup.task_id,
                    payload={
                        "worker_id": wakeup.worker_id,
                        "state": wakeup.state.value,
                        "attempts": wakeup.attempts,
                        "claim_owner": wakeup.claim_owner,
                    },
                ),
            )
            self._bump_revision(connection)
        return wakeup

    def enqueue_wakeup(self, wakeup: WakeupRecord) -> WakeupRecord:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM worker_wakeups WHERE idempotency_key=?",
                (wakeup.idempotency_key,),
            ).fetchone()
            if row is not None:
                return WakeupRecord.from_dict(self._decode(row["payload_json"]))
            self._write_wakeup(connection, wakeup, insert=True)
            self._journal(
                connection,
                PoolJournalRecord(
                    aggregate_type="worker_wakeup",
                    aggregate_id=wakeup.wakeup_id,
                    operation="wakeup_enqueued",
                    task_id=wakeup.task_id,
                    payload={
                        "worker_id": wakeup.worker_id,
                        "envelope_id": wakeup.envelope_id,
                        "reason": wakeup.reason,
                        "available_at": wakeup.available_at,
                    },
                ),
            )
            self._bump_revision(connection)
        return wakeup

    def append_cancellation_receipt(self, receipt: CancellationReceipt) -> CancellationReceipt:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM cancellation_receipts WHERE request_id=?",
                (receipt.request_id,),
            ).fetchone()
            if row is not None:
                return self._cancellation_receipt(self._decode(row["payload_json"]))
            connection.execute(
                """
                INSERT INTO cancellation_receipts(
                    receipt_id, request_id, task_id, changed, payload_json, completed_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    receipt.receipt_id,
                    receipt.request_id,
                    receipt.task_id,
                    int(receipt.changed),
                    canonical_json(receipt),
                    receipt.completed_at,
                ),
            )
            self._journal(
                connection,
                PoolJournalRecord(
                    aggregate_type="cancellation",
                    aggregate_id=receipt.request_id,
                    operation="cancellation_completed",
                    task_id=receipt.task_id,
                    payload=receipt.to_dict(),
                ),
            )
            self._bump_revision(connection)
        return receipt

    def cancellation_receipt(self, request_id: str) -> CancellationReceipt | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM cancellation_receipts WHERE request_id=?",
                (request_id,),
            ).fetchone()
        return None if row is None else self._cancellation_receipt(self._decode(row["payload_json"]))

    def idempotency_result(
        self,
        scope: str,
        idempotency_key: str,
        *,
        request: Mapping[str, Any],
    ) -> tuple[str, Mapping[str, Any]] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT request_digest, result_type, result_json
                FROM worker_pool_idempotency WHERE scope=? AND idempotency_key=?
                """,
                (scope, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        digest = stable_digest(request)
        if row["request_digest"] != digest:
            raise WorkerPoolError(
                WorkerPoolErrorCode.IDEMPOTENCY_CONFLICT,
                "idempotency key was reused with a different request",
                operation=scope,
            )
        return str(row["result_type"]), self._decode(row["result_json"])

    def save_idempotency_result(
        self,
        scope: str,
        idempotency_key: str,
        *,
        request: Mapping[str, Any],
        result_type: str,
        result: Mapping[str, Any],
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if connection is None:
            with self.transaction() as current:
                self.save_idempotency_result(
                    scope,
                    idempotency_key,
                    request=request,
                    result_type=result_type,
                    result=result,
                    connection=current,
                )
                return
        connection.execute(
            """
            INSERT INTO worker_pool_idempotency(
                scope, idempotency_key, request_digest, result_type, result_json, created_at
            ) VALUES(?,?,?,?,?,?)
            ON CONFLICT(scope, idempotency_key) DO NOTHING
            """,
            (
                scope,
                idempotency_key,
                stable_digest(request),
                result_type,
                canonical_json(result),
                utc_iso(),
            ),
        )

    def journal(
        self,
        *,
        after_sequence: int = 0,
        task_id: str = "",
        run_id: str = "",
        aggregate_type: str = "",
        aggregate_id: str = "",
        limit: int = 1000,
    ) -> tuple[PoolJournalRecord, ...]:
        clauses = ["sequence>?"]
        params: list[Any] = [max(0, int(after_sequence))]
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if run_id:
            clauses.append("(run_id=? OR run_id='')")
            params.append(run_id)
        if aggregate_type:
            clauses.append("aggregate_type=?")
            params.append(aggregate_type)
        if aggregate_id:
            clauses.append("aggregate_id=?")
            params.append(aggregate_id)
        params.append(max(1, min(10000, int(limit))))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM worker_pool_journal
                WHERE {' AND '.join(clauses)}
                ORDER BY sequence LIMIT ?
                """,  # noqa: S608
                params,
            ).fetchall()
        return tuple(
            PoolJournalRecord(
                journal_id=str(row["journal_id"]),
                sequence=int(row["sequence"]),
                aggregate_type=str(row["aggregate_type"]),
                aggregate_id=str(row["aggregate_id"]),
                operation=str(row["operation"]),
                run_id=str(row["run_id"]),
                task_id=str(row["task_id"]),
                causation_id=str(row["causation_id"]),
                correlation_id=str(row["correlation_id"]),
                payload=self._decode(row["payload_json"]),
                created_at=str(row["created_at"]),
            )
            for row in rows
        )

    def journal_head_sequence(self) -> int:
        """Return the canonical journal head without a bounded page scan."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM worker_pool_journal"
            ).fetchone()
        return int(row["sequence"] if row is not None else 0)

    def append_journal_record(
        self,
        record: PoolJournalRecord,
        *,
        connection: sqlite3.Connection | None = None,
        bump_revision: bool = True,
    ) -> PoolJournalRecord:
        """Append an integration fact through the canonical pool transaction.

        Integration modules deliberately receive this public operation instead
        of opening a second SQLite connection or maintaining another event log.
        The returned sequence is the canonical worker-pool journal position.
        """

        if connection is None:
            with self.transaction() as current:
                return self.append_journal_record(
                    record,
                    connection=current,
                    bump_revision=bump_revision,
                )
        sequence = self._journal(connection, record)
        if bump_revision:
            self._bump_revision(connection)
        return PoolJournalRecord(
            journal_id=record.journal_id,
            sequence=sequence,
            aggregate_type=record.aggregate_type,
            aggregate_id=record.aggregate_id,
            operation=record.operation,
            run_id=record.run_id,
            task_id=record.task_id,
            causation_id=record.causation_id,
            correlation_id=record.correlation_id,
            payload=dict(record.payload),
            created_at=record.created_at,
        )

    @property
    def revision(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM worker_pool_meta WHERE key='pool_revision'"
            ).fetchone()
        return int(row["value"] if row else 0)

    def integrity_report(self) -> Mapping[str, Any]:
        with self._connect() as connection:
            check = connection.execute("PRAGMA integrity_check").fetchone()
            foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
            active_attempts_without_lease = connection.execute(
                """
                SELECT attempt_id FROM task_attempts
                WHERE state IN (?,?) AND lease_id=''
                """,
                (AttemptState.LEASED.value, AttemptState.RUNNING.value),
            ).fetchall()
            active_leases_without_attempt = connection.execute(
                """
                SELECT l.lease_id FROM worker_leases AS l
                LEFT JOIN task_attempts AS a ON a.attempt_id=l.attempt_id
                WHERE l.state IN (?,?) AND a.attempt_id IS NULL
                """,
                (LeaseState.ACTIVE.value, LeaseState.DRAINING.value),
            ).fetchall()
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])  # noqa: S608
                for table in (
                    "worker_instances",
                    "worker_manifests",
                    "worker_attestations",
                    "task_attempts",
                    "worker_leases",
                    "worker_heartbeats",
                    "execution_receipts",
                    "worker_inbox",
                    "worker_wakeups",
                    "worker_pool_journal",
                )
            }
        issues = [str(row[0]) for row in foreign]
        issues.extend(f"active attempt without lease: {row['attempt_id']}" for row in active_attempts_without_lease)
        issues.extend(f"active lease without attempt: {row['lease_id']}" for row in active_leases_without_attempt)
        return {
            "ok": str(check[0]) == "ok" and not issues,
            "sqlite_integrity": str(check[0]),
            "issues": issues,
            "counts": counts,
            "revision": self.revision,
            "store_path": str(self.path),
        }

    def _write_worker(
        self,
        connection: sqlite3.Connection,
        worker: WorkerInstance,
        *,
        insert: bool,
        expected_version: int = 0,
    ) -> None:
        values = (
            worker.worker_kind,
            worker.location.value,
            worker.backend_id,
            worker.state.value,
            worker.generation,
            worker.version,
            worker.manifest_digest,
            worker.attestation_id,
            worker.endpoint,
            worker.process_identity,
            worker.last_heartbeat_at,
            canonical_json(worker),
            utc_iso(),
        )
        if insert:
            connection.execute(
                """
                INSERT INTO worker_instances(
                    worker_id, worker_kind, location, backend_id, state, generation,
                    version, manifest_digest, attestation_id, endpoint, process_identity,
                    last_heartbeat_at, payload_json, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (worker.worker_id, *values),
            )
            return
        cursor = connection.execute(
            """
            UPDATE worker_instances SET
                worker_kind=?, location=?, backend_id=?, state=?, generation=?, version=?,
                manifest_digest=?, attestation_id=?, endpoint=?, process_identity=?,
                last_heartbeat_at=?, payload_json=?, updated_at=?
            WHERE worker_id=? AND version=?
            """,
            (*values, worker.worker_id, expected_version),
        )
        if cursor.rowcount != 1:
            raise StoreConflict(
                "worker compare-and-swap failed",
                operation="write_worker",
                metadata={"worker_id": worker.worker_id, "expected_version": expected_version},
            )

    def _write_inbox(
        self,
        connection: sqlite3.Connection,
        envelope: InboxEnvelope,
        *,
        insert: bool,
        expected_version: int = 0,
    ) -> None:
        values = (
            envelope.worker_id,
            envelope.task_id,
            envelope.run_id,
            envelope.message_kind,
            envelope.state.value,
            envelope.priority,
            envelope.available_at,
            envelope.claim_owner,
            envelope.claim_deadline_at,
            envelope.delivery_count,
            envelope.version,
            envelope.idempotency_key,
            canonical_json(envelope),
            utc_iso(),
        )
        if insert:
            connection.execute(
                """
                INSERT INTO worker_inbox(
                    envelope_id, worker_id, task_id, run_id, message_kind, state,
                    priority, available_at, claim_owner, claim_deadline_at,
                    delivery_count, version, idempotency_key, payload_json, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (envelope.envelope_id, *values),
            )
            return
        cursor = connection.execute(
            """
            UPDATE worker_inbox SET
                worker_id=?, task_id=?, run_id=?, message_kind=?, state=?, priority=?,
                available_at=?, claim_owner=?, claim_deadline_at=?, delivery_count=?,
                version=?, idempotency_key=?, payload_json=?, updated_at=?
            WHERE envelope_id=? AND version=?
            """,
            (*values, envelope.envelope_id, expected_version),
        )
        if cursor.rowcount != 1:
            raise StoreConflict(
                "inbox envelope compare-and-swap failed",
                operation="write_inbox",
                metadata={"envelope_id": envelope.envelope_id, "expected_version": expected_version},
            )

    def _write_wakeup(
        self,
        connection: sqlite3.Connection,
        wakeup: WakeupRecord,
        *,
        insert: bool,
        expected_version: int = 0,
    ) -> None:
        values = (
            wakeup.worker_id,
            wakeup.task_id,
            wakeup.envelope_id,
            wakeup.state.value,
            wakeup.available_at,
            wakeup.claim_owner,
            wakeup.attempts,
            wakeup.version,
            wakeup.idempotency_key,
            canonical_json(wakeup),
            utc_iso(),
        )
        if insert:
            connection.execute(
                """
                INSERT INTO worker_wakeups(
                    wakeup_id, worker_id, task_id, envelope_id, state, available_at,
                    claim_owner, attempts, version, idempotency_key, payload_json, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (wakeup.wakeup_id, *values),
            )
            return
        cursor = connection.execute(
            """
            UPDATE worker_wakeups SET
                worker_id=?, task_id=?, envelope_id=?, state=?, available_at=?,
                claim_owner=?, attempts=?, version=?, idempotency_key=?, payload_json=?,
                updated_at=?
            WHERE wakeup_id=? AND version=?
            """,
            (*values, wakeup.wakeup_id, expected_version),
        )
        if cursor.rowcount != 1:
            raise StoreConflict(
                "worker wakeup compare-and-swap failed",
                operation="write_wakeup",
                metadata={"wakeup_id": wakeup.wakeup_id, "expected_version": expected_version},
            )

    def _wakeup_for_envelope(
        self,
        connection: sqlite3.Connection,
        envelope_id: str,
    ) -> WakeupRecord | None:
        row = connection.execute(
            "SELECT payload_json FROM worker_wakeups WHERE envelope_id=? ORDER BY updated_at DESC LIMIT 1",
            (envelope_id,),
        ).fetchone()
        return None if row is None else WakeupRecord.from_dict(self._decode(row["payload_json"]))

    def _journal(self, connection: sqlite3.Connection, record: PoolJournalRecord) -> int:
        cursor = connection.execute(
            """
            INSERT INTO worker_pool_journal(
                journal_id, aggregate_type, aggregate_id, operation, run_id, task_id,
                causation_id, correlation_id, payload_json, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record.journal_id,
                record.aggregate_type,
                record.aggregate_id,
                record.operation,
                record.run_id,
                record.task_id,
                record.causation_id,
                record.correlation_id,
                canonical_json(record.payload),
                record.created_at,
            ),
        )
        return int(cursor.lastrowid)

    def _bump_revision(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM worker_pool_meta WHERE key='pool_revision'"
        ).fetchone()
        revision = int(row["value"] if row else 0) + 1
        connection.execute(
            """
            INSERT INTO worker_pool_meta(key, value, updated_at) VALUES('pool_revision', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (str(revision), utc_iso()),
        )
        return revision

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _decode(value: str) -> dict[str, Any]:
        decoded = json.loads(value)
        if not isinstance(decoded, dict):
            raise WorkerPoolError(
                WorkerPoolErrorCode.STORE_CORRUPTION,
                "stored payload is not a JSON object",
                operation="decode_store_payload",
            )
        return decoded

    @staticmethod
    def _cancellation_receipt(data: Mapping[str, Any]) -> CancellationReceipt:
        return CancellationReceipt(
            request_id=str(data.get("request_id") or ""),
            task_id=str(data.get("task_id") or ""),
            accepted=bool(data.get("accepted")),
            changed=bool(data.get("changed")),
            cancelled_attempt_ids=tuple(str(item) for item in data.get("cancelled_attempt_ids") or ()),
            cancelled_lease_ids=tuple(str(item) for item in data.get("cancelled_lease_ids") or ()),
            backend_cancelled=tuple(str(item) for item in data.get("backend_cancelled") or ()),
            gateway_cancelled=tuple(str(item) for item in data.get("gateway_cancelled") or ()),
            failed_targets={str(k): str(v) for k, v in dict(data.get("failed_targets") or {}).items()},
            receipt_id=str(data.get("receipt_id") or ""),
            completed_at=str(data.get("completed_at") or utc_iso()),
            metadata=dict(data.get("metadata") or {}),
        )
