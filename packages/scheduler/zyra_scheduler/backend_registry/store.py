from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import (
    BackendControlRequest,
    BackendControlEvent,
    BackendDefinition,
    BackendDispatchAttempt,
    BackendDispatchSession,
    BackendHealthRecord,
    BackendLease,
    BackendRecoveryInput,
    canonical_json,
    checksum,
)


class BackendRegistryStore:
    """Canonical BackendRegistry persistence; provider state is forbidden here."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS backend_registry_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO backend_registry_meta(key, value)
                    VALUES ('registry_revision', '0');

                CREATE TABLE IF NOT EXISTS backend_definitions (
                    backend_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    location TEXT NOT NULL,
                    runtime_worker TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    registry_revision INTEGER NOT NULL,
                    json TEXT NOT NULL,
                    checksum TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS backend_health (
                    backend_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    current_leases INTEGER NOT NULL,
                    json TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    FOREIGN KEY(backend_id) REFERENCES backend_definitions(backend_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS backend_leases (
                    lease_id TEXT PRIMARY KEY,
                    backend_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    provider_route_id TEXT,
                    acquired_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    released_at REAL,
                    release_reason TEXT,
                    json TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    FOREIGN KEY(backend_id) REFERENCES backend_definitions(backend_id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS backend_dispatch_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    envelope_id TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    backend_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    completed_at REAL,
                    json TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    FOREIGN KEY(lease_id) REFERENCES backend_leases(lease_id)
                        ON DELETE RESTRICT,
                    UNIQUE(envelope_id, attempt_number)
                );

                CREATE TABLE IF NOT EXISTS backend_control_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    backend_id TEXT,
                    lease_id TEXT,
                    envelope_id TEXT,
                    provider_route_id TEXT,
                    created_at REAL NOT NULL,
                    payload_digest TEXT NOT NULL,
                    json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS backend_dispatch_sessions (
                    session_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    current_backend_id TEXT,
                    current_lease_id TEXT,
                    current_envelope_id TEXT,
                    provider_route_id TEXT NOT NULL,
                    m0_execution_ref TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    revision INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    json TEXT NOT NULL,
                    checksum TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS backend_recovery_inputs (
                    recovery_input_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    dispatch_session_id TEXT NOT NULL,
                    consumed INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    json TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    FOREIGN KEY(dispatch_session_id)
                        REFERENCES backend_dispatch_sessions(session_id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS backend_control_requests (
                    control_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    turn_id TEXT,
                    dispatch_session_id TEXT,
                    backend_id TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    requested_at REAL NOT NULL,
                    json TEXT NOT NULL,
                    checksum TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS backend_workspace_quarantine (
                    quarantine_id TEXT PRIMARY KEY,
                    workspace_root TEXT NOT NULL,
                    backend_id TEXT NOT NULL,
                    worker_id TEXT,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    released_at REAL,
                    metadata_json TEXT NOT NULL,
                    UNIQUE(workspace_root, backend_id, worker_id, released_at)
                );

                CREATE TABLE IF NOT EXISTS backend_dispatch_journal (
                    journal_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    record_kind TEXT NOT NULL,
                    envelope_id TEXT,
                    backend_id TEXT,
                    provider_route_id TEXT,
                    previous_digest TEXT NOT NULL,
                    record_digest TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    json TEXT NOT NULL,
                    UNIQUE(session_id, sequence),
                    FOREIGN KEY(session_id)
                        REFERENCES backend_dispatch_sessions(session_id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS backend_dispatch_materializations (
                    session_id TEXT PRIMARY KEY,
                    envelope_id TEXT NOT NULL,
                    transport_receipt_id TEXT NOT NULL,
                    result_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    completed_at REAL NOT NULL,
                    json TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    FOREIGN KEY(session_id)
                        REFERENCES backend_dispatch_sessions(session_id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS backend_side_effect_fences (
                    fence_key TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    envelope_id TEXT NOT NULL,
                    owner_epoch INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    acquired_at REAL NOT NULL,
                    completed_at REAL,
                    result_digest TEXT,
                    json TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    FOREIGN KEY(session_id)
                        REFERENCES backend_dispatch_sessions(session_id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS backend_dispatch_outbox (
                    outbox_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    partition_key TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    available_at REAL NOT NULL,
                    delivered_at REAL,
                    delivery_attempts INTEGER NOT NULL,
                    last_error TEXT,
                    json TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    UNIQUE(session_id, topic, sequence),
                    FOREIGN KEY(session_id)
                        REFERENCES backend_dispatch_sessions(session_id)
                        ON DELETE RESTRICT
                );

                CREATE INDEX IF NOT EXISTS idx_backend_definition_worker
                    ON backend_definitions(runtime_worker, enabled);
                CREATE INDEX IF NOT EXISTS idx_backend_health_status
                    ON backend_health(status, current_leases);
                CREATE INDEX IF NOT EXISTS idx_backend_lease_task
                    ON backend_leases(run_id, task_id, acquired_at);
                CREATE INDEX IF NOT EXISTS idx_backend_attempt_envelope
                    ON backend_dispatch_attempts(envelope_id, attempt_number);
                CREATE INDEX IF NOT EXISTS idx_backend_event_task
                    ON backend_control_events(run_id, task_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_backend_session_task
                    ON backend_dispatch_sessions(run_id, task_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_backend_session_phase
                    ON backend_dispatch_sessions(phase, updated_at);
                CREATE INDEX IF NOT EXISTS idx_backend_recovery_task
                    ON backend_recovery_inputs(run_id, task_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_backend_control_task
                    ON backend_control_requests(run_id, task_id, requested_at);
                CREATE INDEX IF NOT EXISTS idx_backend_workspace_quarantine
                    ON backend_workspace_quarantine(workspace_root, backend_id, released_at);
                CREATE INDEX IF NOT EXISTS idx_backend_journal_session
                    ON backend_dispatch_journal(session_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_backend_journal_envelope
                    ON backend_dispatch_journal(envelope_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_backend_outbox_ready
                    ON backend_dispatch_outbox(delivered_at, available_at, outbox_id);
                """
            )

    def append_journal_record(self, record: Mapping[str, Any]) -> dict[str, Any]:
        required = {
            "journal_id",
            "session_id",
            "sequence",
            "record_kind",
            "previous_digest",
            "record_digest",
            "created_at",
        }
        missing = sorted(required - set(record))
        if missing:
            raise ValueError(f"backend journal record missing fields: {', '.join(missing)}")
        body = dict(record)
        with self._lock, self._connection:
            previous = self._connection.execute(
                """
                SELECT sequence, record_digest FROM backend_dispatch_journal
                WHERE session_id = ? ORDER BY sequence DESC LIMIT 1
                """,
                (str(body["session_id"]),),
            ).fetchone()
            expected_sequence = 1 if previous is None else int(previous["sequence"]) + 1
            expected_previous = "genesis" if previous is None else str(previous["record_digest"])
            if int(body["sequence"]) != expected_sequence:
                raise RuntimeError(
                    f"backend journal sequence conflict: expected {expected_sequence}, "
                    f"actual {body['sequence']}"
                )
            if str(body["previous_digest"]) != expected_previous:
                raise RuntimeError("backend journal hash-chain predecessor mismatch")
            digest_body = dict(body)
            supplied_digest = str(digest_body.pop("record_digest"))
            if checksum(digest_body) != supplied_digest:
                raise RuntimeError("backend journal record digest mismatch")
            self._connection.execute(
                """
                INSERT INTO backend_dispatch_journal(
                    journal_id, session_id, sequence, record_kind,
                    envelope_id, backend_id, provider_route_id,
                    previous_digest, record_digest, created_at, json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(body["journal_id"]),
                    str(body["session_id"]),
                    int(body["sequence"]),
                    str(body["record_kind"]),
                    body.get("envelope_id"),
                    body.get("backend_id"),
                    body.get("provider_route_id"),
                    str(body["previous_digest"]),
                    supplied_digest,
                    float(body["created_at"]),
                    canonical_json(digest_body),
                ),
            )
        return {**digest_body, "record_digest": supplied_digest}

    def journal_records(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._all(
            """
            SELECT json, record_digest FROM backend_dispatch_journal
            WHERE session_id = ? ORDER BY sequence
            """,
            (session_id,),
        )
        records: list[dict[str, Any]] = []
        previous = "genesis"
        for expected_sequence, row in enumerate(rows, start=1):
            body = json.loads(row["json"])
            supplied = str(row["record_digest"])
            if int(body.get("sequence", 0)) != expected_sequence:
                raise RuntimeError(f"backend journal sequence corrupt: {session_id}")
            if str(body.get("previous_digest")) != previous:
                raise RuntimeError(f"backend journal chain corrupt: {session_id}")
            if checksum(body) != supplied:
                raise RuntimeError(f"backend journal digest corrupt: {session_id}")
            records.append({**body, "record_digest": supplied})
            previous = supplied
        return records

    def put_dispatch_materialization(self, value: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(value)
        required = {
            "session_id",
            "envelope_id",
            "transport_receipt_id",
            "result_digest",
            "status",
            "completed_at",
            "result",
        }
        missing = sorted(required - set(body))
        if missing:
            raise ValueError(f"dispatch materialization missing fields: {', '.join(missing)}")
        encoded = canonical_json(body)
        digest = checksum(body)
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT json, checksum FROM backend_dispatch_materializations WHERE session_id = ?",
                (str(body["session_id"]),),
            ).fetchone()
            if existing is not None:
                current = json.loads(existing["json"])
                if checksum(current) != str(existing["checksum"]):
                    raise RuntimeError("dispatch materialization checksum mismatch")
                if current != body:
                    raise RuntimeError("dispatch materialization is immutable")
                return current
            self._connection.execute(
                """
                INSERT INTO backend_dispatch_materializations(
                    session_id, envelope_id, transport_receipt_id,
                    result_digest, status, completed_at, json, checksum
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(body["session_id"]),
                    str(body["envelope_id"]),
                    str(body["transport_receipt_id"]),
                    str(body["result_digest"]),
                    str(body["status"]),
                    float(body["completed_at"]),
                    encoded,
                    digest,
                ),
            )
        return body

    def get_dispatch_materialization(self, session_id: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT json, checksum FROM backend_dispatch_materializations WHERE session_id = ?",
            (session_id,),
        )
        if row is None:
            return None
        body = json.loads(row["json"])
        if checksum(body) != str(row["checksum"]):
            raise RuntimeError(f"dispatch materialization checksum mismatch: {session_id}")
        return body

    def acquire_side_effect_fence(self, value: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(value)
        required = {"fence_key", "session_id", "envelope_id", "owner_epoch", "status", "acquired_at"}
        missing = sorted(required - set(body))
        if missing:
            raise ValueError(f"side-effect fence missing fields: {', '.join(missing)}")
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT json, checksum FROM backend_side_effect_fences WHERE fence_key = ?",
                (str(body["fence_key"]),),
            ).fetchone()
            if row is not None:
                existing = json.loads(row["json"])
                if checksum(existing) != str(row["checksum"]):
                    raise RuntimeError("side-effect fence checksum mismatch")
                if (
                    existing.get("status") == "aborted"
                    and int(body["owner_epoch"]) > int(existing["owner_epoch"])
                    and existing.get("session_id") == body.get("session_id")
                ):
                    self._connection.execute(
                        """
                        UPDATE backend_side_effect_fences
                        SET envelope_id = ?, owner_epoch = ?, status = ?, acquired_at = ?,
                            completed_at = NULL, result_digest = NULL, json = ?, checksum = ?
                        WHERE fence_key = ? AND owner_epoch = ? AND status = 'aborted'
                        """,
                        (
                            str(body["envelope_id"]),
                            int(body["owner_epoch"]),
                            str(body["status"]),
                            float(body["acquired_at"]),
                            canonical_json(body),
                            checksum(body),
                            str(body["fence_key"]),
                            int(existing["owner_epoch"]),
                        ),
                    )
                    return body
                return existing
            self._connection.execute(
                """
                INSERT INTO backend_side_effect_fences(
                    fence_key, session_id, envelope_id, owner_epoch, status,
                    acquired_at, completed_at, result_digest, json, checksum
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    str(body["fence_key"]),
                    str(body["session_id"]),
                    str(body["envelope_id"]),
                    int(body["owner_epoch"]),
                    str(body["status"]),
                    float(body["acquired_at"]),
                    canonical_json(body),
                    checksum(body),
                ),
            )
        return body

    def complete_side_effect_fence(
        self,
        fence_key: str,
        *,
        owner_epoch: int,
        status: str,
        completed_at: float,
        result_digest: str | None,
    ) -> dict[str, Any]:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT json, checksum FROM backend_side_effect_fences WHERE fence_key = ?",
                (fence_key,),
            ).fetchone()
            if row is None:
                raise KeyError(f"side-effect fence not found: {fence_key}")
            body = json.loads(row["json"])
            if checksum(body) != str(row["checksum"]):
                raise RuntimeError(f"side-effect fence checksum mismatch: {fence_key}")
            if int(body["owner_epoch"]) != owner_epoch:
                raise RuntimeError(f"side-effect fence epoch conflict: {fence_key}")
            if body["status"] != "active":
                if body["status"] != status or body.get("result_digest") != result_digest:
                    raise RuntimeError(f"side-effect fence already terminal: {fence_key}")
                return body
            body.update(
                status=status,
                completed_at=completed_at,
                result_digest=result_digest,
            )
            self._connection.execute(
                """
                UPDATE backend_side_effect_fences
                SET status = ?, completed_at = ?, result_digest = ?, json = ?, checksum = ?
                WHERE fence_key = ? AND owner_epoch = ?
                """,
                (
                    status,
                    completed_at,
                    result_digest,
                    canonical_json(body),
                    checksum(body),
                    fence_key,
                    owner_epoch,
                ),
            )
            return body

    def enqueue_dispatch_outbox(self, value: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(value)
        required = {
            "outbox_id", "session_id", "topic", "partition_key", "sequence",
            "available_at", "delivery_attempts", "payload",
        }
        missing = sorted(required - set(body))
        if missing:
            raise ValueError(f"dispatch outbox record missing fields: {', '.join(missing)}")
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO backend_dispatch_outbox(
                    outbox_id, session_id, topic, partition_key, sequence,
                    available_at, delivered_at, delivery_attempts, last_error,
                    json, checksum
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)
                """,
                (
                    str(body["outbox_id"]), str(body["session_id"]), str(body["topic"]),
                    str(body["partition_key"]), int(body["sequence"]),
                    float(body["available_at"]), int(body["delivery_attempts"]),
                    canonical_json(body), checksum(body),
                ),
            )
        return body

    def ready_dispatch_outbox(self, *, at: float, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1 or limit > 10_000:
            raise ValueError("dispatch outbox limit must be between 1 and 10000")
        rows = self._all(
            """
            SELECT json, checksum FROM backend_dispatch_outbox
            WHERE delivered_at IS NULL AND available_at <= ?
            ORDER BY available_at, outbox_id LIMIT ?
            """,
            (at, limit),
        )
        values: list[dict[str, Any]] = []
        for row in rows:
            body = json.loads(row["json"])
            if checksum(body) != str(row["checksum"]):
                raise RuntimeError(f"dispatch outbox checksum mismatch: {body.get('outbox_id', '')}")
            values.append(body)
        return values

    def complete_dispatch_outbox(
        self,
        outbox_id: str,
        *,
        delivered_at: float | None,
        next_available_at: float,
        error: str | None,
    ) -> dict[str, Any]:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT json, checksum FROM backend_dispatch_outbox WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"dispatch outbox record not found: {outbox_id}")
            body = json.loads(row["json"])
            if checksum(body) != str(row["checksum"]):
                raise RuntimeError(f"dispatch outbox checksum mismatch: {outbox_id}")
            body["delivery_attempts"] = int(body["delivery_attempts"]) + 1
            body["delivered_at"] = delivered_at
            body["available_at"] = next_available_at
            body["last_error"] = error
            self._connection.execute(
                """
                UPDATE backend_dispatch_outbox
                SET available_at = ?, delivered_at = ?, delivery_attempts = ?,
                    last_error = ?, json = ?, checksum = ? WHERE outbox_id = ?
                """,
                (
                    next_available_at, delivered_at, int(body["delivery_attempts"]), error,
                    canonical_json(body), checksum(body), outbox_id,
                ),
            )
            return body

    def revision(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM backend_registry_meta WHERE key = 'registry_revision'"
            ).fetchone()
            return int(row["value"] if row is not None else 0)

    def put_definition(
        self,
        definition: BackendDefinition,
        *,
        expected_revision: int | None = None,
    ) -> int:
        with self._lock, self._connection:
            current = self.revision()
            if expected_revision is not None and current != expected_revision:
                raise RuntimeError(
                    f"backend registry revision conflict: expected {expected_revision}, actual {current}"
                )
            revision = current + 1
            value = definition.to_dict()
            self._connection.execute(
                """
                INSERT INTO backend_definitions(
                    backend_id, kind, location, runtime_worker, enabled,
                    registry_revision, json, checksum
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(backend_id) DO UPDATE SET
                    kind = excluded.kind,
                    location = excluded.location,
                    runtime_worker = excluded.runtime_worker,
                    enabled = excluded.enabled,
                    registry_revision = excluded.registry_revision,
                    json = excluded.json,
                    checksum = excluded.checksum
                """,
                (
                    definition.backend_id,
                    definition.kind.value,
                    definition.location.value,
                    definition.runtime_worker,
                    int(definition.enabled),
                    revision,
                    canonical_json(value),
                    checksum(value),
                ),
            )
            self._connection.execute(
                "UPDATE backend_registry_meta SET value = ? WHERE key = 'registry_revision'",
                (str(revision),),
            )
            return revision

    def get_definition(self, backend_id: str) -> BackendDefinition | None:
        row = self._one(
            "SELECT json FROM backend_definitions WHERE backend_id = ?",
            (backend_id,),
        )
        return None if row is None else BackendDefinition.from_dict(json.loads(row["json"]))

    def definitions(self, *, runtime_worker: str = "") -> list[BackendDefinition]:
        if runtime_worker:
            rows = self._all(
                "SELECT json FROM backend_definitions WHERE runtime_worker = ? ORDER BY backend_id",
                (runtime_worker,),
            )
        else:
            rows = self._all("SELECT json FROM backend_definitions ORDER BY backend_id")
        return [BackendDefinition.from_dict(json.loads(row["json"])) for row in rows]

    def put_health(self, health: BackendHealthRecord) -> None:
        value = health.to_dict()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO backend_health(
                    backend_id, status, revision, current_leases, json, checksum
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(backend_id) DO UPDATE SET
                    status = excluded.status,
                    revision = excluded.revision,
                    current_leases = excluded.current_leases,
                    json = excluded.json,
                    checksum = excluded.checksum
                """,
                (
                    health.backend_id,
                    health.status.value,
                    health.revision,
                    health.current_leases,
                    canonical_json(value),
                    checksum(value),
                ),
            )

    def get_health(self, backend_id: str) -> BackendHealthRecord | None:
        row = self._one("SELECT json FROM backend_health WHERE backend_id = ?", (backend_id,))
        return None if row is None else BackendHealthRecord.from_dict(json.loads(row["json"]))

    def health_records(self) -> list[BackendHealthRecord]:
        return [
            BackendHealthRecord.from_dict(json.loads(row["json"]))
            for row in self._all("SELECT json FROM backend_health ORDER BY backend_id")
        ]

    def put_lease(self, lease: BackendLease) -> None:
        value = lease.to_dict()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO backend_leases(
                    lease_id, backend_id, run_id, task_id, turn_id,
                    provider_route_id, acquired_at, expires_at, json, checksum
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease.lease_id,
                    lease.backend_id,
                    lease.run_id,
                    lease.task_id,
                    lease.turn_id,
                    lease.provider_route_id,
                    lease.acquired_at,
                    lease.expires_at,
                    canonical_json(value),
                    lease.checksum,
                ),
            )

    def get_lease(self, lease_id: str) -> BackendLease | None:
        row = self._one(
            "SELECT json, checksum FROM backend_leases WHERE lease_id = ? AND released_at IS NULL",
            (lease_id,),
        )
        if row is None:
            return None
        value = json.loads(row["json"])
        return BackendLease(**value)

    def release_lease(self, lease_id: str, *, reason: str, released_at: float) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE backend_leases
                SET released_at = ?, release_reason = ?
                WHERE lease_id = ? AND released_at IS NULL
                """,
                (released_at, reason, lease_id),
            )
            return cursor.rowcount > 0

    def put_attempt(self, attempt: BackendDispatchAttempt) -> None:
        value = attempt.to_dict()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO backend_dispatch_attempts(
                    attempt_id, envelope_id, lease_id, backend_id,
                    attempt_number, outcome, started_at, completed_at,
                    json, checksum
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id) DO UPDATE SET
                    outcome = excluded.outcome,
                    completed_at = excluded.completed_at,
                    json = excluded.json,
                    checksum = excluded.checksum
                """,
                (
                    attempt.attempt_id,
                    attempt.envelope_id,
                    attempt.lease_id,
                    attempt.backend_id,
                    attempt.attempt,
                    attempt.outcome,
                    attempt.started_at,
                    attempt.completed_at,
                    canonical_json(value),
                    checksum(value),
                ),
            )

    def attempts(self, envelope_id: str) -> list[BackendDispatchAttempt]:
        values: list[BackendDispatchAttempt] = []
        for row in self._all(
            """
            SELECT json FROM backend_dispatch_attempts
            WHERE envelope_id = ? ORDER BY attempt_number
            """,
            (envelope_id,),
        ):
            item = json.loads(row["json"])
            if item.get("failure_kind") is not None:
                from .models import BackendFailureKind

                item["failure_kind"] = BackendFailureKind(item["failure_kind"])
            if item.get("recovery_intent") is not None:
                from .models import BackendRecoveryIntent

                item["recovery_intent"] = BackendRecoveryIntent(item["recovery_intent"])
            values.append(BackendDispatchAttempt(**item))
        return values

    def append_event(self, event: BackendControlEvent) -> None:
        value = event.to_dict()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO backend_control_events(
                    event_id, event_type, run_id, task_id, backend_id,
                    lease_id, envelope_id, provider_route_id, created_at,
                    payload_digest, json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.event_type,
                    event.run_id,
                    event.task_id,
                    event.backend_id,
                    event.lease_id,
                    event.envelope_id,
                    event.provider_route_id,
                    event.created_at,
                    event.payload_digest,
                    canonical_json(value),
                ),
            )

    def put_dispatch_session(
        self,
        session: BackendDispatchSession,
        *,
        expected_revision: int | None = None,
    ) -> BackendDispatchSession:
        value = session.to_dict()
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT revision FROM backend_dispatch_sessions WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()
            actual_revision = int(existing["revision"]) if existing is not None else 0
            if expected_revision is not None and actual_revision != expected_revision:
                raise RuntimeError(
                    "backend dispatch session revision conflict: "
                    f"expected {expected_revision}, actual {actual_revision}"
                )
            if existing is None and session.revision != 1:
                raise RuntimeError("new backend dispatch session must start at revision 1")
            if existing is not None and session.revision != actual_revision + 1:
                raise RuntimeError(
                    "backend dispatch session revision must advance exactly once: "
                    f"actual {actual_revision}, next {session.revision}"
                )
            self._connection.execute(
                """
                INSERT INTO backend_dispatch_sessions(
                    session_id, run_id, task_id, turn_id, phase,
                    current_backend_id, current_lease_id, current_envelope_id,
                    provider_route_id, m0_execution_ref, idempotency_key,
                    revision, updated_at, json, checksum
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    phase = excluded.phase,
                    current_backend_id = excluded.current_backend_id,
                    current_lease_id = excluded.current_lease_id,
                    current_envelope_id = excluded.current_envelope_id,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at,
                    json = excluded.json,
                    checksum = excluded.checksum
                """,
                (
                    session.session_id,
                    session.run_id,
                    session.task_id,
                    session.turn_id,
                    session.phase.value,
                    session.current_backend_id,
                    session.current_lease_id,
                    session.current_envelope_id,
                    session.provider_route_id,
                    session.m0_execution_ref,
                    session.idempotency_key,
                    session.revision,
                    session.updated_at,
                    canonical_json(value),
                    checksum(value),
                ),
            )
        return session

    def get_dispatch_session(self, session_id: str) -> BackendDispatchSession | None:
        row = self._one(
            "SELECT json, checksum FROM backend_dispatch_sessions WHERE session_id = ?",
            (session_id,),
        )
        if row is None:
            return None
        value = json.loads(row["json"])
        if checksum(value) != str(row["checksum"]):
            raise RuntimeError(f"backend dispatch session checksum mismatch: {session_id}")
        return BackendDispatchSession.from_dict(value)

    def dispatch_session_for_idempotency(self, idempotency_key: str) -> BackendDispatchSession | None:
        row = self._one(
            "SELECT json, checksum FROM backend_dispatch_sessions WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        if row is None:
            return None
        value = json.loads(row["json"])
        if checksum(value) != str(row["checksum"]):
            raise RuntimeError("backend dispatch idempotency session checksum mismatch")
        return BackendDispatchSession.from_dict(value)

    def dispatch_sessions(
        self,
        *,
        run_id: str = "",
        task_id: str = "",
        active_only: bool = False,
    ) -> list[BackendDispatchSession]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if run_id:
            clauses.append("run_id = ?")
            parameters.append(run_id)
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if active_only:
            clauses.append("phase NOT IN ('succeeded', 'failed', 'cancelled', 'reconcile_required')")
        sql = "SELECT json, checksum FROM backend_dispatch_sessions"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at, session_id"
        values: list[BackendDispatchSession] = []
        for row in self._all(sql, parameters):
            value = json.loads(row["json"])
            if checksum(value) != str(row["checksum"]):
                raise RuntimeError(
                    f"backend dispatch session checksum mismatch: {value.get('session_id', '')}"
                )
            values.append(BackendDispatchSession.from_dict(value))
        return values

    def put_recovery_input(self, value: BackendRecoveryInput) -> BackendRecoveryInput:
        body = value.to_dict()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO backend_recovery_inputs(
                    recovery_input_id, kind, run_id, task_id, turn_id,
                    dispatch_session_id, consumed, created_at, json, checksum
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    value.recovery_input_id,
                    value.kind.value,
                    value.run_id,
                    value.task_id,
                    value.turn_id,
                    value.dispatch_session_id,
                    int(value.consumed),
                    value.created_at,
                    canonical_json(body),
                    checksum(body),
                ),
            )
        return value

    def recovery_inputs(
        self,
        *,
        run_id: str = "",
        task_id: str = "",
        unconsumed_only: bool = False,
    ) -> list[BackendRecoveryInput]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if run_id:
            clauses.append("run_id = ?")
            parameters.append(run_id)
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if unconsumed_only:
            clauses.append("consumed = 0")
        sql = "SELECT json, checksum FROM backend_recovery_inputs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, recovery_input_id"
        result: list[BackendRecoveryInput] = []
        for row in self._all(sql, parameters):
            body = json.loads(row["json"])
            if checksum(body) != str(row["checksum"]):
                raise RuntimeError(
                    f"backend recovery input checksum mismatch: {body.get('recovery_input_id', '')}"
                )
            result.append(BackendRecoveryInput.from_dict(body))
        return result

    def consume_recovery_input(self, recovery_input_id: str) -> BackendRecoveryInput:
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT json, checksum, consumed FROM backend_recovery_inputs WHERE recovery_input_id = ?",
                (recovery_input_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"backend recovery input not found: {recovery_input_id}")
            body = json.loads(row["json"])
            if checksum(body) != str(row["checksum"]):
                raise RuntimeError(f"backend recovery input checksum mismatch: {recovery_input_id}")
            if not bool(row["consumed"]):
                body["consumed"] = True
                self._connection.execute(
                    "UPDATE backend_recovery_inputs SET consumed = 1, json = ?, checksum = ? WHERE recovery_input_id = ?",
                    (canonical_json(body), checksum(body), recovery_input_id),
                )
            return BackendRecoveryInput.from_dict(body)

    def put_control_request(self, request: BackendControlRequest) -> BackendControlRequest:
        body = request.to_dict()
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT json, checksum FROM backend_control_requests WHERE idempotency_key = ?",
                (request.idempotency_key,),
            ).fetchone()
            if existing is not None:
                stored = json.loads(existing["json"])
                if checksum(stored) != str(existing["checksum"]):
                    raise RuntimeError("backend control request checksum mismatch")
                return BackendControlRequest.from_dict(stored)
            self._connection.execute(
                """
                INSERT INTO backend_control_requests(
                    control_id, action, run_id, task_id, turn_id,
                    dispatch_session_id, backend_id, idempotency_key,
                    requested_at, json, checksum
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.control_id,
                    request.action.value,
                    request.run_id,
                    request.task_id,
                    request.turn_id,
                    request.dispatch_session_id,
                    request.backend_id,
                    request.idempotency_key,
                    request.requested_at,
                    canonical_json(body),
                    checksum(body),
                ),
            )
        return request

    def control_requests(
        self,
        *,
        run_id: str = "",
        task_id: str = "",
    ) -> list[BackendControlRequest]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if run_id:
            clauses.append("run_id = ?")
            parameters.append(run_id)
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        sql = "SELECT json, checksum FROM backend_control_requests"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY requested_at, control_id"
        result: list[BackendControlRequest] = []
        for row in self._all(sql, parameters):
            body = json.loads(row["json"])
            if checksum(body) != str(row["checksum"]):
                raise RuntimeError(f"backend control request checksum mismatch: {body.get('control_id', '')}")
            result.append(BackendControlRequest.from_dict(body))
        return result

    def quarantine_workspace(
        self,
        *,
        quarantine_id: str,
        workspace_root: str,
        backend_id: str,
        worker_id: str | None,
        reason: str,
        created_at: float,
        expires_at: float | None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO backend_workspace_quarantine(
                    quarantine_id, workspace_root, backend_id, worker_id,
                    reason, created_at, expires_at, released_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    quarantine_id,
                    str(Path(workspace_root).expanduser().resolve()),
                    backend_id,
                    worker_id,
                    reason,
                    created_at,
                    expires_at,
                    canonical_json(dict(metadata or {})),
                ),
            )

    def workspace_is_quarantined(
        self,
        workspace_root: str,
        *,
        backend_id: str = "",
        worker_id: str = "",
        at: float | None = None,
    ) -> bool:
        clauses = ["workspace_root = ?", "released_at IS NULL"]
        parameters: list[Any] = [str(Path(workspace_root).expanduser().resolve())]
        if backend_id:
            clauses.append("backend_id = ?")
            parameters.append(backend_id)
        if worker_id:
            clauses.append("(worker_id IS NULL OR worker_id = ?)")
            parameters.append(worker_id)
        if at is not None:
            clauses.append("(expires_at IS NULL OR expires_at > ?)")
            parameters.append(at)
        row = self._one(
            "SELECT quarantine_id FROM backend_workspace_quarantine WHERE "
            + " AND ".join(clauses)
            + " LIMIT 1",
            parameters,
        )
        return row is not None

    def release_workspace_quarantine(
        self,
        workspace_root: str,
        *,
        backend_id: str = "",
        released_at: float,
    ) -> int:
        clauses = ["workspace_root = ?", "released_at IS NULL"]
        parameters: list[Any] = [str(Path(workspace_root).expanduser().resolve())]
        if backend_id:
            clauses.append("backend_id = ?")
            parameters.append(backend_id)
        parameters.append(released_at)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE backend_workspace_quarantine SET released_at = ? WHERE "
                + " AND ".join(clauses),
                [released_at, *parameters[:-1]],
            )
            return int(cursor.rowcount)

    def events(self, *, run_id: str = "", task_id: str = "") -> list[BackendControlEvent]:
        if run_id and task_id:
            rows = self._all(
                """
                SELECT json FROM backend_control_events
                WHERE run_id = ? AND task_id = ? ORDER BY created_at, event_id
                """,
                (run_id, task_id),
            )
        else:
            rows = self._all("SELECT json FROM backend_control_events ORDER BY created_at, event_id")
        return [BackendControlEvent(**json.loads(row["json"])) for row in rows]

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for table in (
            "backend_definitions",
            "backend_health",
            "backend_leases",
            "backend_dispatch_attempts",
            "backend_control_events",
            "backend_dispatch_sessions",
            "backend_recovery_inputs",
            "backend_control_requests",
            "backend_workspace_quarantine",
        ):
            row = self._one(f"SELECT COUNT(*) AS count FROM {table}")
            result[table] = int(row["count"] if row else 0)
        return result

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _one(
        self,
        sql: str,
        parameters: Iterable[Any] = (),
    ) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(sql, tuple(parameters)).fetchone()

    def _all(
        self,
        sql: str,
        parameters: Iterable[Any] = (),
    ) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._connection.execute(sql, tuple(parameters)).fetchall())
