from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import (
    BackendControlEvent,
    BackendDefinition,
    BackendDispatchAttempt,
    BackendHealthRecord,
    BackendLease,
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
                """
            )

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
