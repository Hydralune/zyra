from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import StateConflict, StateCorrupt
from .models import (
    DeploymentProfile,
    DispatchReceipt,
    DispatchStatus,
    LifecycleStatus,
    PlacementDecision,
    ProcessRecord,
    canonical_json,
    digest,
    now_iso,
)


DEPLOYMENT_SCHEMA_VERSION = 1
DEPLOYMENT_STORE_SCHEMA = "zyra.deployment-store/v1"


class DeploymentStateStore:
    def __init__(
        self,
        path: Path | str,
        *,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.path = Path(path).resolve()
        self.timeout_seconds = timeout_seconds
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield connection
                if connection.in_transaction:
                    connection.execute("COMMIT")
            except BaseException:
                try:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()

    def initialize(self) -> None:
        with self.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS deployment_meta (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deployment_processes (
                    component_id TEXT PRIMARY KEY,
                    profile TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    generation_id TEXT NOT NULL,
                    command_digest TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    exit_code INTEGER,
                    restart_count INTEGER NOT NULL,
                    log_path TEXT NOT NULL,
                    configuration_digest TEXT NOT NULL,
                    process_create_time REAL NOT NULL,
                    revision INTEGER NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_deployment_process_generation
                    ON deployment_processes(generation_id);
                CREATE TABLE IF NOT EXISTS deployment_placements (
                    decision_id TEXT PRIMARY KEY,
                    workload_id TEXT NOT NULL,
                    selected_profile TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    policy_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_deployment_placement_workload
                    ON deployment_placements(workload_id, created_at);
                CREATE TABLE IF NOT EXISTS deployment_dispatches (
                    attempt_id TEXT PRIMARY KEY,
                    dispatch_id TEXT NOT NULL,
                    workload_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    result_digest TEXT NOT NULL,
                    predecessor_attempt_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_deployment_dispatch_idempotency
                    ON deployment_dispatches(idempotency_key)
                    WHERE idempotency_key <> '';
                CREATE INDEX IF NOT EXISTS idx_deployment_dispatch_workload
                    ON deployment_dispatches(workload_id, started_at);
                CREATE TABLE IF NOT EXISTS deployment_checkpoints (
                    checkpoint_ref TEXT PRIMARY KEY,
                    workload_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    source_profile TEXT NOT NULL,
                    source_attempt_id TEXT NOT NULL,
                    canonical_checkpoint_ref TEXT NOT NULL,
                    checkpoint_json TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    committed INTEGER NOT NULL,
                    consumed_by_attempt_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    consumed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deployment_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    component_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    causation_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_deployment_events_task
                    ON deployment_events(task_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_deployment_events_type
                    ON deployment_events(event_type, sequence);
                CREATE TABLE IF NOT EXISTS deployment_probe_reports (
                    report_id TEXT PRIMARY KEY,
                    target_commit TEXT NOT NULL,
                    ready INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    report_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            existing = connection.execute(
                "SELECT value_json FROM deployment_meta WHERE key = 'schema_version'"
            ).fetchone()
            if existing is None:
                self._write_meta(
                    connection,
                    "schema_version",
                    DEPLOYMENT_SCHEMA_VERSION,
                    expected_revision=0,
                )
                self._write_meta(
                    connection,
                    "store_identity",
                    {
                        "schema": DEPLOYMENT_STORE_SCHEMA,
                        "created_at": now_iso(),
                        "canonical_owner": "DeploymentStateStore",
                    },
                    expected_revision=0,
                )
            else:
                try:
                    version = int(json.loads(existing["value_json"]))
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise StateCorrupt(
                        "deployment_schema_version_corrupt",
                        "deployment store schema version is corrupt",
                        operation="initialize",
                    ) from error
                if version > DEPLOYMENT_SCHEMA_VERSION:
                    raise StateCorrupt(
                        "deployment_schema_future",
                        "deployment store was created by a newer runtime",
                        operation="initialize",
                        details={
                            "actual": version,
                            "supported": DEPLOYMENT_SCHEMA_VERSION,
                        },
                    )
                if version < DEPLOYMENT_SCHEMA_VERSION:
                    self._migrate(
                        connection,
                        current=version,
                        target=DEPLOYMENT_SCHEMA_VERSION,
                    )

    def _migrate(
        self,
        connection: sqlite3.Connection,
        *,
        current: int,
        target: int,
    ) -> None:
        version = current
        while version < target:
            next_version = version + 1
            if next_version != 1:
                raise StateCorrupt(
                    "deployment_schema_migration_missing",
                    "no deployment store migration is registered",
                    operation="migrate",
                    details={"current": version, "target": target},
                )
            version = next_version
        row = connection.execute(
            "SELECT revision FROM deployment_meta WHERE key = 'schema_version'"
        ).fetchone()
        expected = int(row["revision"]) if row else 0
        self._write_meta(
            connection,
            "schema_version",
            target,
            expected_revision=expected,
        )
        self.append_event(
            "deployment.schema_migrated",
            {
                "from_version": current,
                "to_version": target,
                "owner": "DeploymentStateStore",
            },
            connection=connection,
        )

    def _write_meta(
        self,
        connection: sqlite3.Connection,
        key: str,
        value: Any,
        *,
        expected_revision: int | None = None,
    ) -> int:
        row = connection.execute(
            "SELECT revision FROM deployment_meta WHERE key = ?",
            (key,),
        ).fetchone()
        actual = int(row["revision"]) if row else 0
        if expected_revision is not None and actual != expected_revision:
            raise StateConflict(
                "deployment_meta_revision_conflict",
                "deployment metadata revision changed",
                operation="write_meta",
                details={
                    "key": key,
                    "expected_revision": expected_revision,
                    "actual_revision": actual,
                },
            )
        revision = actual + 1
        connection.execute(
            """
            INSERT INTO deployment_meta(key, value_json, revision, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                revision = excluded.revision,
                updated_at = excluded.updated_at
            """,
            (
                key,
                canonical_json(value).decode("utf-8"),
                revision,
                now_iso(),
            ),
        )
        return revision

    def read_meta(self, key: str, default: Any = None) -> Any:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT value_json FROM deployment_meta WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except json.JSONDecodeError as error:
            raise StateCorrupt(
                "deployment_meta_corrupt",
                "deployment metadata contains invalid JSON",
                operation="read_meta",
                details={"key": key},
            ) from error

    def write_meta(
        self,
        key: str,
        value: Any,
        *,
        expected_revision: int | None = None,
    ) -> int:
        with self.transaction() as connection:
            return self._write_meta(
                connection,
                key,
                value,
                expected_revision=expected_revision,
            )

    def save_process(
        self,
        record: ProcessRecord,
        *,
        expected_revision: int | None = None,
    ) -> int:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT revision FROM deployment_processes WHERE component_id = ?",
                (record.component_id,),
            ).fetchone()
            actual = int(row["revision"]) if row else 0
            if expected_revision is not None and expected_revision != actual:
                raise StateConflict(
                    "deployment_process_revision_conflict",
                    "deployment process state changed",
                    operation="save_process",
                    profile=record.profile,
                    details={
                        "component_id": record.component_id,
                        "expected_revision": expected_revision,
                        "actual_revision": actual,
                    },
                )
            revision = actual + 1
            connection.execute(
                """
                INSERT INTO deployment_processes(
                    component_id, profile, pid, generation_id, command_digest,
                    endpoint, status, started_at, observed_at, exit_code,
                    restart_count, log_path, configuration_digest,
                    process_create_time, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(component_id) DO UPDATE SET
                    profile = excluded.profile,
                    pid = excluded.pid,
                    generation_id = excluded.generation_id,
                    command_digest = excluded.command_digest,
                    endpoint = excluded.endpoint,
                    status = excluded.status,
                    started_at = excluded.started_at,
                    observed_at = excluded.observed_at,
                    exit_code = excluded.exit_code,
                    restart_count = excluded.restart_count,
                    log_path = excluded.log_path,
                    configuration_digest = excluded.configuration_digest,
                    process_create_time = excluded.process_create_time,
                    revision = excluded.revision
                """,
                (
                    record.component_id,
                    record.profile,
                    record.pid,
                    record.generation_id,
                    record.command_digest,
                    record.endpoint,
                    record.status.value,
                    record.started_at,
                    record.observed_at,
                    record.exit_code,
                    record.restart_count,
                    record.log_path,
                    record.configuration_digest,
                    record.process_create_time,
                    revision,
                ),
            )
            self.append_event(
                "deployment.process_state_changed",
                {
                    "component_id": record.component_id,
                    "pid": record.pid,
                    "generation_id": record.generation_id,
                    "status": record.status.value,
                    "revision": revision,
                },
                component_id=record.component_id,
                profile=record.profile,
                connection=connection,
            )
            return revision

    def process(self, component_id: str) -> tuple[ProcessRecord, int] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM deployment_processes WHERE component_id = ?",
                (component_id,),
            ).fetchone()
        return self._process_from_row(row) if row else None

    def processes(self) -> list[tuple[ProcessRecord, int]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM deployment_processes ORDER BY component_id"
            ).fetchall()
        return [self._process_from_row(row) for row in rows]

    @staticmethod
    def _process_from_row(row: sqlite3.Row) -> tuple[ProcessRecord, int]:
        try:
            status = LifecycleStatus(str(row["status"]))
        except ValueError as error:
            raise StateCorrupt(
                "deployment_process_status_corrupt",
                "deployment process status is invalid",
                operation="process",
                details={"component_id": row["component_id"]},
            ) from error
        return (
            ProcessRecord(
                component_id=str(row["component_id"]),
                profile=str(row["profile"]),
                pid=int(row["pid"]),
                generation_id=str(row["generation_id"]),
                command_digest=str(row["command_digest"]),
                endpoint=str(row["endpoint"]),
                status=status,
                started_at=str(row["started_at"]),
                observed_at=str(row["observed_at"]),
                exit_code=(
                    int(row["exit_code"]) if row["exit_code"] is not None else None
                ),
                restart_count=int(row["restart_count"]),
                log_path=str(row["log_path"]),
                configuration_digest=str(row["configuration_digest"]),
                process_create_time=float(row["process_create_time"]),
            ),
            int(row["revision"]),
        )

    def delete_process(self, component_id: str) -> bool:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT profile, pid, generation_id FROM deployment_processes WHERE component_id = ?",
                (component_id,),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "DELETE FROM deployment_processes WHERE component_id = ?",
                (component_id,),
            )
            self.append_event(
                "deployment.process_removed",
                {
                    "component_id": component_id,
                    "pid": int(row["pid"]),
                    "generation_id": str(row["generation_id"]),
                },
                component_id=component_id,
                profile=str(row["profile"]),
                connection=connection,
            )
            return True

    def save_placement(self, decision: PlacementDecision) -> None:
        body = decision.to_dict()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO deployment_placements(
                    decision_id, workload_id, selected_profile, decision_json,
                    policy_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.workload_id,
                    decision.selected_profile.value,
                    canonical_json(body).decode("utf-8"),
                    decision.policy_digest,
                    decision.created_at,
                ),
            )
            self.append_event(
                "deployment.placement_decided",
                body,
                profile=decision.selected_profile.value,
                causation_id=decision.workload_id,
                connection=connection,
            )

    def placement(self, decision_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT decision_json FROM deployment_placements WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        return self._json_row(row, "decision_json") if row else None

    def placements_for_workload(self, workload_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT decision_json
                FROM deployment_placements
                WHERE workload_id = ?
                ORDER BY created_at, decision_id
                """,
                (workload_id,),
            ).fetchall()
        return [self._json_row(row, "decision_json") for row in rows]

    def save_dispatch(
        self,
        receipt: DispatchReceipt,
        *,
        idempotency_key: str,
    ) -> DispatchReceipt:
        body = receipt.to_dict()
        with self.transaction() as connection:
            if idempotency_key:
                existing = connection.execute(
                    """
                    SELECT receipt_json
                    FROM deployment_dispatches
                    WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return self._dispatch_from_mapping(
                        self._json_row(existing, "receipt_json")
                    )
            connection.execute(
                """
                INSERT INTO deployment_dispatches(
                    attempt_id, dispatch_id, workload_id, task_id, run_id,
                    profile, node_id, status, receipt_json, idempotency_key,
                    result_digest, predecessor_attempt_id, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.attempt_id,
                    receipt.dispatch_id,
                    receipt.workload_id,
                    receipt.task_id,
                    receipt.run_id,
                    receipt.profile.value,
                    receipt.node_id,
                    receipt.status.value,
                    canonical_json(body).decode("utf-8"),
                    idempotency_key,
                    receipt.result_digest,
                    receipt.predecessor_attempt_id,
                    receipt.started_at,
                    receipt.completed_at,
                ),
            )
            self.append_event(
                "deployment.dispatch_recorded",
                body,
                component_id=receipt.node_id,
                profile=receipt.profile.value,
                task_id=receipt.task_id,
                run_id=receipt.run_id,
                causation_id=receipt.workload_id,
                correlation_id=receipt.dispatch_id,
                connection=connection,
            )
            return receipt

    def dispatch_by_idempotency(self, key: str) -> DispatchReceipt | None:
        if not key:
            return None
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT receipt_json
                FROM deployment_dispatches
                WHERE idempotency_key = ?
                """,
                (key,),
            ).fetchone()
        if row is None:
            return None
        return self._dispatch_from_mapping(self._json_row(row, "receipt_json"))

    def dispatches_for_workload(self, workload_id: str) -> list[DispatchReceipt]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT receipt_json
                FROM deployment_dispatches
                WHERE workload_id = ?
                ORDER BY started_at, attempt_id
                """,
                (workload_id,),
            ).fetchall()
        return [
            self._dispatch_from_mapping(self._json_row(row, "receipt_json"))
            for row in rows
        ]

    @staticmethod
    def _dispatch_from_mapping(value: Mapping[str, Any]) -> DispatchReceipt:
        return DispatchReceipt(
            dispatch_id=str(value.get("dispatch_id") or ""),
            attempt_id=str(value.get("attempt_id") or ""),
            workload_id=str(value.get("workload_id") or ""),
            task_id=str(value.get("task_id") or ""),
            run_id=str(value.get("run_id") or ""),
            profile=DeploymentProfile(str(value.get("profile") or "")),
            node_id=str(value.get("node_id") or ""),
            status=DispatchStatus(str(value.get("status") or "")),
            started_at=str(value.get("started_at") or ""),
            completed_at=str(value.get("completed_at") or ""),
            result=dict(value.get("result") or {}),
            artifact_refs=tuple(str(item) for item in value.get("artifact_refs") or ()),
            checkpoint_ref=str(value.get("checkpoint_ref") or ""),
            predecessor_attempt_id=str(value.get("predecessor_attempt_id") or ""),
            failure_code=str(value.get("failure_code") or ""),
            degraded=value.get("degraded") is True,
            result_digest=str(value.get("result_digest") or ""),
        )

    def save_checkpoint(
        self,
        *,
        checkpoint_ref: str,
        workload_id: str,
        task_id: str,
        run_id: str,
        source_profile: DeploymentProfile,
        source_attempt_id: str,
        canonical_checkpoint_ref: str,
        payload: Mapping[str, Any],
        committed: bool,
    ) -> dict[str, Any]:
        semantic = {
            "schema": "zyra.deployment-checkpoint-handoff/v1",
            "checkpoint_ref": checkpoint_ref,
            "workload_id": workload_id,
            "task_id": task_id,
            "run_id": run_id,
            "source_profile": source_profile.value,
            "source_attempt_id": source_attempt_id,
            "canonical_checkpoint_ref": canonical_checkpoint_ref,
            "payload": dict(payload),
            "committed": committed,
            "created_at": now_iso(),
        }
        checksum = digest(semantic)
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO deployment_checkpoints(
                    checkpoint_ref, workload_id, task_id, run_id, source_profile,
                    source_attempt_id, canonical_checkpoint_ref, checkpoint_json,
                    checksum, committed, consumed_by_attempt_id, created_at,
                    consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, '')
                """,
                (
                    checkpoint_ref,
                    workload_id,
                    task_id,
                    run_id,
                    source_profile.value,
                    source_attempt_id,
                    canonical_checkpoint_ref,
                    canonical_json(semantic).decode("utf-8"),
                    checksum,
                    1 if committed else 0,
                    semantic["created_at"],
                ),
            )
            self.append_event(
                "deployment.checkpoint_staged",
                {**semantic, "checksum": checksum},
                profile=source_profile.value,
                task_id=task_id,
                run_id=run_id,
                causation_id=source_attempt_id,
                connection=connection,
            )
        return {**semantic, "checksum": checksum}

    def checkpoint(self, checkpoint_ref: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM deployment_checkpoints WHERE checkpoint_ref = ?",
                (checkpoint_ref,),
            ).fetchone()
        if row is None:
            return None
        value = self._json_row(row, "checkpoint_json")
        observed = digest(value)
        if observed != str(row["checksum"]):
            raise StateCorrupt(
                "deployment_checkpoint_checksum_mismatch",
                "deployment checkpoint handoff checksum is invalid",
                operation="checkpoint",
                details={"checkpoint_ref": checkpoint_ref},
            )
        value["checksum"] = observed
        value["consumed_by_attempt_id"] = str(row["consumed_by_attempt_id"])
        value["consumed_at"] = str(row["consumed_at"])
        return value

    def consume_checkpoint(
        self,
        checkpoint_ref: str,
        *,
        attempt_id: str,
    ) -> dict[str, Any]:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM deployment_checkpoints WHERE checkpoint_ref = ?",
                (checkpoint_ref,),
            ).fetchone()
            if row is None:
                raise StateConflict(
                    "deployment_checkpoint_missing",
                    "deployment checkpoint handoff does not exist",
                    operation="consume_checkpoint",
                    details={"checkpoint_ref": checkpoint_ref},
                )
            if not bool(row["committed"]):
                raise StateConflict(
                    "deployment_checkpoint_uncommitted",
                    "uncommitted deployment checkpoint cannot be consumed",
                    operation="consume_checkpoint",
                    details={"checkpoint_ref": checkpoint_ref},
                )
            consumed_by = str(row["consumed_by_attempt_id"])
            if consumed_by and consumed_by != attempt_id:
                raise StateConflict(
                    "deployment_checkpoint_already_consumed",
                    "deployment checkpoint was consumed by another attempt",
                    operation="consume_checkpoint",
                    details={
                        "checkpoint_ref": checkpoint_ref,
                        "consumed_by_attempt_id": consumed_by,
                    },
                )
            consumed_at = str(row["consumed_at"]) or now_iso()
            connection.execute(
                """
                UPDATE deployment_checkpoints
                SET consumed_by_attempt_id = ?, consumed_at = ?
                WHERE checkpoint_ref = ?
                """,
                (attempt_id, consumed_at, checkpoint_ref),
            )
            value = self._json_row(row, "checkpoint_json")
            self.append_event(
                "deployment.checkpoint_consumed",
                {
                    "checkpoint_ref": checkpoint_ref,
                    "attempt_id": attempt_id,
                    "checksum": str(row["checksum"]),
                },
                profile=str(row["source_profile"]),
                task_id=str(row["task_id"]),
                run_id=str(row["run_id"]),
                causation_id=str(row["source_attempt_id"]),
                correlation_id=attempt_id,
                connection=connection,
            )
        value["checksum"] = str(row["checksum"])
        value["consumed_by_attempt_id"] = attempt_id
        value["consumed_at"] = consumed_at
        return value

    def append_event(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        event_id: str = "",
        component_id: str = "",
        profile: str = "",
        task_id: str = "",
        run_id: str = "",
        causation_id: str = "",
        correlation_id: str = "",
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        observed_event_id = event_id or (
            "deployment_event_" + digest(
                {
                    "event_type": event_type,
                    "payload": payload,
                    "at": time.time_ns(),
                }
            ).split(":", 1)[1][:20]
        )
        created_at = now_iso()
        body = dict(payload)
        payload_json = canonical_json(body).decode("utf-8")
        payload_digest = digest(body)
        owns_connection = connection is None
        active = connection or self._connect()
        try:
            active.execute(
                """
                INSERT INTO deployment_events(
                    event_id, event_type, component_id, profile, task_id,
                    run_id, causation_id, correlation_id, payload_json,
                    payload_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observed_event_id,
                    event_type,
                    component_id,
                    profile,
                    task_id,
                    run_id,
                    causation_id,
                    correlation_id,
                    payload_json,
                    payload_digest,
                    created_at,
                ),
            )
            row = active.execute("SELECT last_insert_rowid() AS sequence").fetchone()
            return {
                "schema": "zyra.deployment-event/v1",
                "sequence": int(row["sequence"]),
                "event_id": observed_event_id,
                "event_type": event_type,
                "component_id": component_id,
                "profile": profile,
                "task_id": task_id,
                "run_id": run_id,
                "causation_id": causation_id,
                "correlation_id": correlation_id,
                "payload": body,
                "payload_digest": payload_digest,
                "created_at": created_at,
            }
        finally:
            if owns_connection:
                active.close()

    def events(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
        task_id: str = "",
        event_types: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100_000:
            raise ValueError("event limit must be between 1 and 100000")
        where = ["sequence > ?"]
        parameters: list[Any] = [max(0, int(after_sequence))]
        if task_id:
            where.append("task_id = ?")
            parameters.append(task_id)
        normalized_types = tuple(
            dict.fromkeys(str(item) for item in event_types if str(item))
        )
        if normalized_types:
            placeholders = ",".join("?" for _ in normalized_types)
            where.append(f"event_type IN ({placeholders})")
            parameters.extend(normalized_types)
        parameters.append(limit)
        query = f"""
            SELECT *
            FROM deployment_events
            WHERE {' AND '.join(where)}
            ORDER BY sequence
            LIMIT ?
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = self._json_row(row, "payload_json")
            if digest(payload) != str(row["payload_digest"]):
                raise StateCorrupt(
                    "deployment_event_checksum_mismatch",
                    "deployment event payload checksum is invalid",
                    operation="events",
                    details={"event_id": row["event_id"]},
                )
            result.append(
                {
                    "schema": "zyra.deployment-event/v1",
                    "sequence": int(row["sequence"]),
                    "event_id": str(row["event_id"]),
                    "event_type": str(row["event_type"]),
                    "component_id": str(row["component_id"]),
                    "profile": str(row["profile"]),
                    "task_id": str(row["task_id"]),
                    "run_id": str(row["run_id"]),
                    "causation_id": str(row["causation_id"]),
                    "correlation_id": str(row["correlation_id"]),
                    "payload": payload,
                    "payload_digest": str(row["payload_digest"]),
                    "created_at": str(row["created_at"]),
                }
            )
        return result

    def save_probe_report(
        self,
        report: Mapping[str, Any],
        *,
        target_commit: str,
    ) -> None:
        report_id = str(report.get("report_id") or "")
        if not report_id:
            raise ValueError("probe report requires report_id")
        body = dict(report)
        observed_digest = str(body.get("report_digest") or digest(body))
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO deployment_probe_reports(
                    report_id, target_commit, ready, status, report_json,
                    report_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    target_commit,
                    1 if body.get("ready") is True else 0,
                    str(body.get("status") or ""),
                    canonical_json(body).decode("utf-8"),
                    observed_digest,
                    str(body.get("completed_at") or now_iso()),
                ),
            )
            self.append_event(
                "deployment.semantic_health_completed",
                {
                    "report_id": report_id,
                    "target_commit": target_commit,
                    "ready": body.get("ready") is True,
                    "status": str(body.get("status") or ""),
                    "report_digest": observed_digest,
                    "blockers": list(body.get("blockers") or ()),
                },
                connection=connection,
            )

    def latest_probe_report(self) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT report_json
                FROM deployment_probe_reports
                ORDER BY created_at DESC, report_id DESC
                LIMIT 1
                """
            ).fetchone()
        return self._json_row(row, "report_json") if row else None

    def integrity_report(self) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
            counts = {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in (
                    "deployment_processes",
                    "deployment_placements",
                    "deployment_dispatches",
                    "deployment_checkpoints",
                    "deployment_events",
                    "deployment_probe_reports",
                )
            }
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM deployment_events"
                ).fetchone()[0]
            )
        schema_version = self.read_meta("schema_version")
        ready = (
            quick == "ok"
            and not foreign
            and schema_version == DEPLOYMENT_SCHEMA_VERSION
        )
        return {
            "schema": "zyra.deployment-store-integrity/v1",
            "ready": ready,
            "quick_check": quick,
            "foreign_key_violations": len(foreign),
            "schema_version": schema_version,
            "supported_schema_version": DEPLOYMENT_SCHEMA_VERSION,
            "counts": counts,
            "last_event_sequence": sequence,
            "path": str(self.path),
            "canonical_owner": "DeploymentStateStore",
        }

    def reconcile_processes(
        self,
        alive: Mapping[int, float],
    ) -> list[ProcessRecord]:
        changed: list[ProcessRecord] = []
        for record, revision in self.processes():
            create_time = alive.get(record.pid)
            same_process = (
                create_time is not None
                and (
                    record.process_create_time <= 0
                    or abs(create_time - record.process_create_time) < 0.01
                )
            )
            if same_process:
                continue
            if record.status in {
                LifecycleStatus.STOPPED,
                LifecycleStatus.CRASHED,
            }:
                continue
            updated = ProcessRecord(
                **{
                    **record.to_dict(),
                    "status": LifecycleStatus.CRASHED,
                    "observed_at": now_iso(),
                    "exit_code": record.exit_code,
                }
            )
            self.save_process(updated, expected_revision=revision)
            changed.append(updated)
        return changed

    @staticmethod
    def _json_row(row: sqlite3.Row, column: str) -> dict[str, Any]:
        try:
            value = json.loads(str(row[column]))
        except json.JSONDecodeError as error:
            raise StateCorrupt(
                "deployment_json_corrupt",
                "deployment store row contains invalid JSON",
                operation="decode_row",
                details={"column": column},
            ) from error
        if not isinstance(value, dict):
            raise StateCorrupt(
                "deployment_json_shape_invalid",
                "deployment store JSON row is not an object",
                operation="decode_row",
                details={"column": column},
            )
        return value


__all__ = [
    "DEPLOYMENT_SCHEMA_VERSION",
    "DEPLOYMENT_STORE_SCHEMA",
    "DeploymentStateStore",
]
