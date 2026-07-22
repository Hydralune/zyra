from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping, Sequence

from .errors import StoreConflict, WorkerPoolError, WorkerPoolErrorCode
from .integration_models import (
    AdmissionPhase,
    ControlCommand,
    ControlPhase,
    IntegrationCheckpoint,
    LeaseRenewalRecord,
    PhysicalDispatchBinding,
    RecoveryEvidence,
    TypedYieldReceipt,
)
from .models import PoolJournalRecord, canonical_json, stable_digest, utc_iso, utc_now
from .store import WorkerPoolStore


class WorkerPoolIntegrationRepository:
    """Transactional extension of ``WorkerPoolStore`` for 07A integration.

    This repository never accepts a path and never opens its own database.  It
    uses the canonical store transaction, revision and journal, so process
    restart cannot create a second task, attempt, lease or checkpoint owner.
    """

    SCHEMA_VERSION = 1

    def __init__(self, store: WorkerPoolStore) -> None:
        self.store = store
        self.initialize()

    def initialize(self) -> None:
        with self.store.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS worker_dispatch_bindings (
                    binding_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    owner_session_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL UNIQUE,
                    lease_id TEXT NOT NULL UNIQUE,
                    worker_id TEXT NOT NULL,
                    backend_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    edge_only INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    request_digest TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_dispatch_binding_task
                    ON worker_dispatch_bindings(task_id, version DESC);
                CREATE INDEX IF NOT EXISTS idx_dispatch_binding_run_phase
                    ON worker_dispatch_bindings(run_id, phase, updated_at);
                CREATE INDEX IF NOT EXISTS idx_dispatch_binding_session_phase
                    ON worker_dispatch_bindings(owner_session_id, phase, updated_at);
                CREATE INDEX IF NOT EXISTS idx_dispatch_binding_worker_phase
                    ON worker_dispatch_bindings(worker_id, phase, updated_at);

                CREATE TABLE IF NOT EXISTS worker_control_commands (
                    command_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    claim_owner TEXT NOT NULL,
                    claim_deadline_at TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_control_pending
                    ON worker_control_commands(phase, claim_deadline_at, updated_at);
                CREATE INDEX IF NOT EXISTS idx_control_task
                    ON worker_control_commands(task_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_control_worker
                    ON worker_control_commands(worker_id, updated_at);

                CREATE TABLE IF NOT EXISTS worker_lease_renewals (
                    renewal_id TEXT PRIMARY KEY,
                    lease_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(lease_id, observed_at, disposition)
                );

                CREATE INDEX IF NOT EXISTS idx_renewal_lease
                    ON worker_lease_renewals(lease_id, observed_at DESC);

                CREATE TABLE IF NOT EXISTS worker_typed_yields (
                    yield_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    binding_id TEXT NOT NULL UNIQUE,
                    attempt_id TEXT NOT NULL UNIQUE,
                    lease_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_typed_yield_task
                    ON worker_typed_yields(task_id, created_at);

                CREATE TABLE IF NOT EXISTS worker_recovery_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    lease_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    signal_kind TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_recovery_task
                    ON worker_recovery_evidence(task_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_recovery_worker
                    ON worker_recovery_evidence(worker_id, created_at);

                CREATE TABLE IF NOT EXISTS worker_integration_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    pool_revision INTEGER NOT NULL,
                    journal_sequence INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_integration_checkpoint_run
                    ON worker_integration_checkpoints(run_id, created_at DESC);
                """
            )
            connection.execute(
                """
                INSERT INTO worker_pool_meta(key, value, updated_at)
                VALUES('integration_schema_version', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (str(self.SCHEMA_VERSION), utc_iso()),
            )

    def insert_binding(
        self,
        binding: PhysicalDispatchBinding,
        *,
        request: Mapping[str, Any],
        connection: Any | None = None,
    ) -> PhysicalDispatchBinding:
        if connection is None:
            with self.store.transaction() as current:
                return self.insert_binding(binding, request=request, connection=current)
        row = connection.execute(
            "SELECT request_digest, payload_json FROM worker_dispatch_bindings WHERE idempotency_key=?",
            (binding.idempotency_key,),
        ).fetchone()
        if row is not None:
            if str(row["request_digest"]) != stable_digest(request):
                raise WorkerPoolError(
                    WorkerPoolErrorCode.IDEMPOTENCY_CONFLICT,
                    "dispatch admission idempotency key was reused with another request",
                    operation="insert_dispatch_binding",
                    task_id=binding.task_id,
                )
            return PhysicalDispatchBinding.from_dict(self.store._decode(row["payload_json"]))
        connection.execute(
            """
            INSERT INTO worker_dispatch_bindings(
                binding_id, task_id, run_id, owner_session_id, attempt_id, lease_id,
                worker_id, backend_id, phase, execution_mode, edge_only, version,
                request_digest, idempotency_key, payload_json, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                binding.binding_id,
                binding.task_id,
                binding.run_id,
                binding.owner_session_id,
                binding.attempt_id,
                binding.lease_id,
                binding.worker_id,
                binding.backend_id,
                binding.phase.value,
                binding.execution_mode.value,
                int(binding.edge_only),
                binding.version,
                stable_digest(request),
                binding.idempotency_key,
                canonical_json(binding.to_dict()),
                binding.created_at,
                binding.updated_at,
            ),
        )
        self._journal(
            connection,
            aggregate_type="dispatch_binding",
            aggregate_id=binding.binding_id,
            operation="dispatch_admitted",
            run_id=binding.run_id,
            task_id=binding.task_id,
            causation_id=str(request.get("causation_id") or binding.lease_id),
            correlation_id=str(request.get("correlation_id") or binding.owner_session_id),
            payload={
                "attempt_id": binding.attempt_id,
                "lease_id": binding.lease_id,
                "worker_id": binding.worker_id,
                "backend_id": binding.backend_id,
                "edge_only": binding.edge_only,
                "foreign_refs": binding.foreign_refs.to_dict(),
            },
        )
        return binding

    def update_binding(
        self,
        binding: PhysicalDispatchBinding,
        *,
        expected_version: int,
        operation: str,
        payload: Mapping[str, Any] | None = None,
    ) -> PhysicalDispatchBinding:
        with self.store.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE worker_dispatch_bindings SET
                    phase=?, worker_id=?, backend_id=?, version=?, payload_json=?, updated_at=?
                WHERE binding_id=? AND version=?
                """,
                (
                    binding.phase.value,
                    binding.worker_id,
                    binding.backend_id,
                    binding.version,
                    canonical_json(binding.to_dict()),
                    binding.updated_at,
                    binding.binding_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreConflict(
                    "dispatch binding compare-and-swap failed",
                    operation=operation,
                    task_id=binding.task_id,
                    attempt_id=binding.attempt_id,
                    lease_id=binding.lease_id,
                    metadata={"expected_version": expected_version},
                )
            self._journal(
                connection,
                aggregate_type="dispatch_binding",
                aggregate_id=binding.binding_id,
                operation=operation,
                run_id=binding.run_id,
                task_id=binding.task_id,
                causation_id=binding.lease_id,
                correlation_id=binding.owner_session_id,
                payload={
                    "phase": binding.phase.value,
                    "version": binding.version,
                    "attempt_id": binding.attempt_id,
                    "lease_id": binding.lease_id,
                    **dict(payload or {}),
                },
            )
        return binding

    def get_binding(self, binding_id: str) -> PhysicalDispatchBinding | None:
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM worker_dispatch_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
        return None if row is None else PhysicalDispatchBinding.from_dict(self.store._decode(row["payload_json"]))

    def binding_for_attempt(self, attempt_id: str) -> PhysicalDispatchBinding | None:
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM worker_dispatch_bindings WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
        return None if row is None else PhysicalDispatchBinding.from_dict(self.store._decode(row["payload_json"]))

    def binding_for_lease(self, lease_id: str) -> PhysicalDispatchBinding | None:
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM worker_dispatch_bindings WHERE lease_id=?",
                (lease_id,),
            ).fetchone()
        return None if row is None else PhysicalDispatchBinding.from_dict(self.store._decode(row["payload_json"]))

    def latest_binding(self, task_id: str) -> PhysicalDispatchBinding | None:
        with self.store.transaction() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM worker_dispatch_bindings
                WHERE task_id=? ORDER BY updated_at DESC, version DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        return None if row is None else PhysicalDispatchBinding.from_dict(self.store._decode(row["payload_json"]))

    def list_bindings(
        self,
        *,
        task_id: str = "",
        run_id: str = "",
        owner_session_id: str = "",
        worker_id: str = "",
        phases: Sequence[AdmissionPhase] = (),
    ) -> tuple[PhysicalDispatchBinding, ...]:
        clauses = ["1=1"]
        params: list[Any] = []
        for column, value in {
            "task_id": task_id,
            "run_id": run_id,
            "owner_session_id": owner_session_id,
            "worker_id": worker_id,
        }.items():
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        if phases:
            clauses.append(f"phase IN ({','.join('?' for _ in phases)})")
            params.extend(item.value for item in phases)
        with self.store.transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT payload_json FROM worker_dispatch_bindings
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at, binding_id
                """,  # noqa: S608
                params,
            ).fetchall()
        return tuple(PhysicalDispatchBinding.from_dict(self.store._decode(row["payload_json"])) for row in rows)

    def enqueue_control(self, command: ControlCommand) -> ControlCommand:
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM worker_control_commands WHERE idempotency_key=?",
                (command.idempotency_key,),
            ).fetchone()
            if row is not None:
                prior = ControlCommand.from_dict(self.store._decode(row["payload_json"]))
                comparable = {
                    "kind": prior.kind.value,
                    "task_id": prior.task_id,
                    "worker_id": prior.worker_id,
                    "lease_id": prior.lease_id,
                    "reason": prior.reason,
                }
                requested = {
                    "kind": command.kind.value,
                    "task_id": command.task_id,
                    "worker_id": command.worker_id,
                    "lease_id": command.lease_id,
                    "reason": command.reason,
                }
                if comparable != requested:
                    raise WorkerPoolError(
                        WorkerPoolErrorCode.IDEMPOTENCY_CONFLICT,
                        "control idempotency key was reused for another command",
                        operation="enqueue_worker_control",
                        task_id=command.task_id,
                        worker_id=command.worker_id,
                    )
                return prior
            connection.execute(
                """
                INSERT INTO worker_control_commands(
                    command_id, kind, phase, task_id, run_id, worker_id, lease_id,
                    claim_owner, claim_deadline_at, version, idempotency_key,
                    payload_json, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    command.command_id,
                    command.kind.value,
                    command.phase.value,
                    command.task_id,
                    command.run_id,
                    command.worker_id,
                    command.lease_id,
                    command.claim_owner,
                    command.claim_deadline_at,
                    command.version,
                    command.idempotency_key,
                    canonical_json(command.to_dict()),
                    command.created_at,
                    command.updated_at,
                ),
            )
            self._journal(
                connection,
                aggregate_type="worker_control",
                aggregate_id=command.command_id,
                operation="control_enqueued",
                run_id=command.run_id,
                task_id=command.task_id,
                causation_id=command.binding_id or command.lease_id,
                correlation_id=command.actor_id,
                payload={
                    "kind": command.kind.value,
                    "worker_id": command.worker_id,
                    "lease_id": command.lease_id,
                    "phase": command.phase.value,
                },
            )
        return command

    def claim_controls(
        self,
        claim_owner: str,
        *,
        limit: int = 32,
        claim_ttl_seconds: float = 30.0,
    ) -> tuple[ControlCommand, ...]:
        now = utc_now()
        deadline = utc_iso(now + timedelta(seconds=max(1.0, claim_ttl_seconds)))
        claimed: list[ControlCommand] = []
        with self.store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM worker_control_commands
                WHERE phase=? OR (phase=? AND claim_deadline_at<=?)
                ORDER BY created_at, command_id LIMIT ?
                """,
                (ControlPhase.PENDING.value, ControlPhase.CLAIMED.value, utc_iso(now), max(1, min(1000, limit))),
            ).fetchall()
            for row in rows:
                current = ControlCommand.from_dict(self.store._decode(row["payload_json"]))
                updated = current.advance(
                    ControlPhase.CLAIMED,
                    claim_owner=claim_owner,
                    claim_deadline_at=deadline,
                    attempts=current.attempts + 1,
                    error="",
                )
                cursor = connection.execute(
                    """
                    UPDATE worker_control_commands SET phase=?, claim_owner=?, claim_deadline_at=?,
                        version=?, payload_json=?, updated_at=?
                    WHERE command_id=? AND version=?
                    """,
                    (
                        updated.phase.value,
                        updated.claim_owner,
                        updated.claim_deadline_at,
                        updated.version,
                        canonical_json(updated.to_dict()),
                        updated.updated_at,
                        updated.command_id,
                        current.version,
                    ),
                )
                if cursor.rowcount == 1:
                    claimed.append(updated)
                    self._journal(
                        connection,
                        aggregate_type="worker_control",
                        aggregate_id=updated.command_id,
                        operation="control_claimed",
                        run_id=updated.run_id,
                        task_id=updated.task_id,
                        causation_id=updated.binding_id or updated.lease_id,
                        correlation_id=claim_owner,
                        payload={"kind": updated.kind.value, "attempts": updated.attempts},
                    )
        return tuple(claimed)

    def update_control(
        self,
        command: ControlCommand,
        *,
        expected_version: int,
        operation: str,
    ) -> ControlCommand:
        with self.store.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE worker_control_commands SET phase=?, claim_owner=?, claim_deadline_at=?,
                    version=?, payload_json=?, updated_at=?
                WHERE command_id=? AND version=?
                """,
                (
                    command.phase.value,
                    command.claim_owner,
                    command.claim_deadline_at,
                    command.version,
                    canonical_json(command.to_dict()),
                    command.updated_at,
                    command.command_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StoreConflict(
                    "control command compare-and-swap failed",
                    operation=operation,
                    task_id=command.task_id,
                    worker_id=command.worker_id,
                    lease_id=command.lease_id,
                )
            self._journal(
                connection,
                aggregate_type="worker_control",
                aggregate_id=command.command_id,
                operation=operation,
                run_id=command.run_id,
                task_id=command.task_id,
                causation_id=command.binding_id or command.lease_id,
                correlation_id=command.actor_id,
                payload={
                    "kind": command.kind.value,
                    "phase": command.phase.value,
                    "effect": dict(command.effect),
                    "error": command.error,
                },
            )
        return command

    def get_control(self, command_id: str) -> ControlCommand | None:
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM worker_control_commands WHERE command_id=?",
                (command_id,),
            ).fetchone()
        return None if row is None else ControlCommand.from_dict(self.store._decode(row["payload_json"]))

    def list_controls(
        self,
        *,
        task_id: str = "",
        worker_id: str = "",
        phases: Sequence[ControlPhase] = (),
    ) -> tuple[ControlCommand, ...]:
        clauses = ["1=1"]
        params: list[Any] = []
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if worker_id:
            clauses.append("worker_id=?")
            params.append(worker_id)
        if phases:
            clauses.append(f"phase IN ({','.join('?' for _ in phases)})")
            params.extend(item.value for item in phases)
        with self.store.transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT payload_json FROM worker_control_commands
                WHERE {' AND '.join(clauses)} ORDER BY created_at, command_id
                """,  # noqa: S608
                params,
            ).fetchall()
        return tuple(ControlCommand.from_dict(self.store._decode(row["payload_json"])) for row in rows)

    def append_renewal(self, record: LeaseRenewalRecord) -> LeaseRenewalRecord:
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM worker_lease_renewals WHERE renewal_id=?",
                (record.renewal_id,),
            ).fetchone()
            if row is not None:
                return LeaseRenewalRecord.from_dict(self.store._decode(row["payload_json"]))
            connection.execute(
                """
                INSERT INTO worker_lease_renewals(
                    renewal_id, lease_id, task_id, attempt_id, worker_id,
                    disposition, observed_at, payload_json
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    record.renewal_id,
                    record.lease_id,
                    record.task_id,
                    record.attempt_id,
                    record.worker_id,
                    record.disposition.value,
                    record.observed_at,
                    canonical_json(record.to_dict()),
                ),
            )
            self._journal(
                connection,
                aggregate_type="lease_renewal",
                aggregate_id=record.renewal_id,
                operation=f"renewal_{record.disposition.value}",
                run_id="",
                task_id=record.task_id,
                causation_id=record.lease_id,
                correlation_id=record.worker_id,
                payload=record.to_dict(),
            )
        return record

    def renewals_for_lease(self, lease_id: str) -> tuple[LeaseRenewalRecord, ...]:
        with self.store.transaction() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM worker_lease_renewals
                WHERE lease_id=? ORDER BY observed_at, renewal_id
                """,
                (lease_id,),
            ).fetchall()
        return tuple(LeaseRenewalRecord.from_dict(self.store._decode(row["payload_json"])) for row in rows)

    def append_typed_yield(
        self,
        receipt: TypedYieldReceipt,
        *,
        binding: PhysicalDispatchBinding | None = None,
        expected_binding_version: int | None = None,
    ) -> TypedYieldReceipt:
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM worker_typed_yields WHERE binding_id=?",
                (receipt.binding_id,),
            ).fetchone()
            if row is not None:
                prior = TypedYieldReceipt.from_dict(self.store._decode(row["payload_json"]))
                if prior.digest != receipt.digest:
                    raise WorkerPoolError(
                        WorkerPoolErrorCode.IDEMPOTENCY_CONFLICT,
                        "physical attempt tried to commit a second typed yield",
                        operation="commit_typed_yield",
                        task_id=receipt.task_id,
                        attempt_id=receipt.attempt_id,
                        lease_id=receipt.lease_id,
                    )
                return prior
            connection.execute(
                """
                INSERT INTO worker_typed_yields(
                    yield_id, task_id, binding_id, attempt_id, lease_id,
                    kind, sequence, digest, payload_json, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    receipt.yield_id,
                    receipt.task_id,
                    receipt.binding_id,
                    receipt.attempt_id,
                    receipt.lease_id,
                    receipt.kind.value,
                    receipt.sequence,
                    receipt.digest,
                    canonical_json(receipt.to_dict()),
                    receipt.created_at,
                ),
            )
            if binding is not None:
                if expected_binding_version is None:
                    raise ValueError("expected binding version is required for atomic typed yield")
                cursor = connection.execute(
                    """
                    UPDATE worker_dispatch_bindings SET
                        phase=?, worker_id=?, backend_id=?, version=?, payload_json=?, updated_at=?
                    WHERE binding_id=? AND version=?
                    """,
                    (
                        binding.phase.value,
                        binding.worker_id,
                        binding.backend_id,
                        binding.version,
                        canonical_json(binding.to_dict()),
                        binding.updated_at,
                        binding.binding_id,
                        expected_binding_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StoreConflict(
                        "typed yield binding compare-and-swap failed",
                        operation="commit_typed_yield",
                        task_id=binding.task_id,
                        attempt_id=binding.attempt_id,
                        lease_id=binding.lease_id,
                    )
            self._journal(
                connection,
                aggregate_type="typed_yield",
                aggregate_id=receipt.yield_id,
                operation="typed_yield_committed",
                run_id=receipt.run_id,
                task_id=receipt.task_id,
                causation_id=receipt.lease_id,
                correlation_id=receipt.binding_id,
                payload={
                    "attempt_id": receipt.attempt_id,
                    "kind": receipt.kind.value,
                    "artifact_refs": list(receipt.artifact_refs),
                    "digest": receipt.digest,
                },
            )
            if binding is not None:
                self._journal(
                    connection,
                    aggregate_type="dispatch_binding",
                    aggregate_id=binding.binding_id,
                    operation="dispatch_typed_yield_bound",
                    run_id=binding.run_id,
                    task_id=binding.task_id,
                    causation_id=receipt.yield_id,
                    correlation_id=binding.owner_session_id,
                    payload={
                        "yield_id": receipt.yield_id,
                        "version": binding.version,
                        "phase": binding.phase.value,
                    },
                )
        return receipt

    def typed_yield_for_binding(self, binding_id: str) -> TypedYieldReceipt | None:
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM worker_typed_yields WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
        return None if row is None else TypedYieldReceipt.from_dict(self.store._decode(row["payload_json"]))

    def append_recovery(self, evidence: RecoveryEvidence) -> RecoveryEvidence:
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM worker_recovery_evidence WHERE evidence_id=?",
                (evidence.evidence_id,),
            ).fetchone()
            if row is not None:
                prior = RecoveryEvidence.from_dict(self.store._decode(row["payload_json"]))
                if prior.digest != evidence.digest:
                    raise WorkerPoolError(
                        WorkerPoolErrorCode.IDEMPOTENCY_CONFLICT,
                        "recovery evidence id was reused with different content",
                        operation="append_recovery_evidence",
                        task_id=evidence.task_id,
                    )
                return prior
            connection.execute(
                """
                INSERT INTO worker_recovery_evidence(
                    evidence_id, task_id, run_id, attempt_id, lease_id, worker_id,
                    disposition, signal_kind, digest, payload_json, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    evidence.evidence_id,
                    evidence.task_id,
                    evidence.run_id,
                    evidence.attempt_id,
                    evidence.lease_id,
                    evidence.worker_id,
                    evidence.disposition.value,
                    evidence.signal_kind,
                    evidence.digest,
                    canonical_json(evidence.to_dict()),
                    evidence.created_at,
                ),
            )
            self._journal(
                connection,
                aggregate_type="worker_recovery",
                aggregate_id=evidence.evidence_id,
                operation="worker_recovery_evidence",
                run_id=evidence.run_id,
                task_id=evidence.task_id,
                causation_id=evidence.lease_id,
                correlation_id=evidence.worker_id,
                payload=evidence.to_dict(),
            )
        return evidence

    def list_recovery(self, *, task_id: str = "", worker_id: str = "") -> tuple[RecoveryEvidence, ...]:
        clauses = ["1=1"]
        params: list[Any] = []
        if task_id:
            clauses.append("task_id=?")
            params.append(task_id)
        if worker_id:
            clauses.append("worker_id=?")
            params.append(worker_id)
        with self.store.transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT payload_json FROM worker_recovery_evidence
                WHERE {' AND '.join(clauses)} ORDER BY created_at, evidence_id
                """,  # noqa: S608
                params,
            ).fetchall()
        return tuple(RecoveryEvidence.from_dict(self.store._decode(row["payload_json"])) for row in rows)

    def save_checkpoint(self, checkpoint: IntegrationCheckpoint) -> IntegrationCheckpoint:
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT digest, payload_json FROM worker_integration_checkpoints WHERE checkpoint_id=?",
                (checkpoint.checkpoint_id,),
            ).fetchone()
            if row is not None:
                if str(row["digest"]) != checkpoint.digest:
                    raise WorkerPoolError(
                        WorkerPoolErrorCode.IDEMPOTENCY_CONFLICT,
                        "integration checkpoint id was reused with another state",
                        operation="save_integration_checkpoint",
                    )
                return IntegrationCheckpoint.from_dict(self.store._decode(row["payload_json"]))
            connection.execute(
                """
                INSERT INTO worker_integration_checkpoints(
                    checkpoint_id, run_id, pool_revision, journal_sequence,
                    digest, payload_json, created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    checkpoint.checkpoint_id,
                    checkpoint.run_id,
                    checkpoint.pool_revision,
                    checkpoint.journal_sequence,
                    checkpoint.digest,
                    canonical_json(checkpoint.to_dict()),
                    checkpoint.created_at,
                ),
            )
            self._journal(
                connection,
                aggregate_type="integration_checkpoint",
                aggregate_id=checkpoint.checkpoint_id,
                operation="integration_checkpoint_saved",
                run_id=checkpoint.run_id,
                task_id="",
                causation_id=checkpoint.previous_checkpoint_id,
                correlation_id=checkpoint.run_id,
                payload={
                    "pool_revision": checkpoint.pool_revision,
                    "journal_sequence": checkpoint.journal_sequence,
                    "active_binding_ids": list(checkpoint.active_binding_ids),
                    "pending_control_ids": list(checkpoint.pending_control_ids),
                    "digest": checkpoint.digest,
                },
            )
        return checkpoint

    def get_checkpoint(self, checkpoint_id: str) -> IntegrationCheckpoint | None:
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM worker_integration_checkpoints WHERE checkpoint_id=?",
                (checkpoint_id,),
            ).fetchone()
        return None if row is None else IntegrationCheckpoint.from_dict(self.store._decode(row["payload_json"]))

    def latest_checkpoint(self, run_id: str) -> IntegrationCheckpoint | None:
        with self.store.transaction() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM worker_integration_checkpoints
                WHERE run_id=? ORDER BY created_at DESC, checkpoint_id DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return None if row is None else IntegrationCheckpoint.from_dict(self.store._decode(row["payload_json"]))

    def integrity_report(self) -> Mapping[str, Any]:
        active = self.list_bindings(
            phases=(
                AdmissionPhase.ADMITTED,
                AdmissionPhase.DISPATCHED,
                AdmissionPhase.PARKED,
                AdmissionPhase.DRAINING,
                AdmissionPhase.LOST,
            )
        )
        mismatches: list[str] = []
        for binding in active:
            lease = self.store.get_lease(binding.lease_id)
            attempt = self.store.get_attempt(binding.attempt_id)
            if lease is None:
                mismatches.append(f"binding {binding.binding_id} references missing lease {binding.lease_id}")
            if attempt is None:
                mismatches.append(f"binding {binding.binding_id} references missing attempt {binding.attempt_id}")
            if lease is not None and lease.attempt_id != binding.attempt_id:
                mismatches.append(f"binding {binding.binding_id} lease/attempt mismatch")
            if lease is not None and lease.worker_id != binding.worker_id:
                mismatches.append(f"binding {binding.binding_id} lease/worker mismatch")
        pending = self.list_controls(phases=(ControlPhase.PENDING, ControlPhase.CLAIMED))
        return {
            "ok": not mismatches,
            "schema_version": self.SCHEMA_VERSION,
            "active_binding_count": len(active),
            "pending_control_count": len(pending),
            "mismatches": mismatches,
            "canonical_store": str(self.store.path),
            "separate_state_owner": False,
        }

    def _journal(
        self,
        connection: Any,
        *,
        aggregate_type: str,
        aggregate_id: str,
        operation: str,
        run_id: str,
        task_id: str,
        causation_id: str,
        correlation_id: str,
        payload: Mapping[str, Any],
    ) -> PoolJournalRecord:
        return self.store.append_journal_record(
            PoolJournalRecord(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                operation=operation,
                run_id=run_id,
                task_id=task_id,
                causation_id=causation_id,
                correlation_id=correlation_id,
                payload=dict(payload),
            ),
            connection=connection,
        )


__all__ = ["WorkerPoolIntegrationRepository"]
