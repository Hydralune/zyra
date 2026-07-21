from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from .procedure_models import (
    PROCEDURE_STORE_PROTOCOL,
    ProcedureConsumer,
    ProcedureMiningDisposition,
    ProcedureMiningReceipt,
    ProcedureSignal,
    ProcedureStoreConflictError,
    ProcedureValidationStatus,
    ReusableProcedure,
    canonical_json,
    stable_digest,
    stable_id,
)


@dataclass(frozen=True, slots=True)
class ProcedureClaim:
    procedure_id: str
    consumer: ProcedureConsumer
    worker_id: str
    claim_token: str
    lease_epoch: int
    claimed_at: float
    expires_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "procedure_id": self.procedure_id,
            "consumer": self.consumer.value,
            "worker_id": self.worker_id,
            "claim_token": self.claim_token,
            "lease_epoch": self.lease_epoch,
            "claimed_at": self.claimed_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class ProcedureProjectionReceipt:
    projection_id: str
    procedure_id: str
    consumer: ProcedureConsumer
    worker_id: str
    state: str
    projected_at: float
    projection_digest: str
    metadata: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "procedure_id": self.procedure_id,
            "consumer": self.consumer.value,
            "worker_id": self.worker_id,
            "state": self.state,
            "projected_at": self.projected_at,
            "projection_digest": self.projection_digest,
            "metadata": dict(self.metadata),
        }


class ReusableProcedureStore:
    """Durable procedure projection in the canonical MemoryFabric SQLite file.

    The store owns procedure candidates and their delivery state, not canonical
    curator outcomes and not 03C skill versions.  Foreign ids are retained as
    provenance; no copied skill body or executable cache is stored here.
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
            with self.connection() as connection:
                connection.executescript(
                    """
                    PRAGMA journal_mode = WAL;
                    PRAGMA foreign_keys = ON;
                    PRAGMA busy_timeout = 30000;

                    CREATE TABLE IF NOT EXISTS reusable_procedures (
                        procedure_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        state TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        confidence REAL NOT NULL,
                        success_count INTEGER NOT NULL,
                        failure_count INTEGER NOT NULL,
                        curator_outcome_id TEXT NOT NULL,
                        curator_job_id TEXT NOT NULL,
                        curator_decision_id TEXT NOT NULL,
                        evidence_bundle_id TEXT NOT NULL,
                        evidence_digest TEXT NOT NULL,
                        canonical_memory_id TEXT NOT NULL,
                        canonical_memory_revision INTEGER NOT NULL,
                        consumers_json TEXT NOT NULL,
                        procedure_json TEXT NOT NULL,
                        procedure_digest TEXT NOT NULL,
                        created_at_text TEXT NOT NULL,
                        updated_at_text TEXT NOT NULL,
                        stored_at REAL NOT NULL,
                        UNIQUE(curator_outcome_id, evidence_digest)
                    );

                    CREATE INDEX IF NOT EXISTS idx_reusable_procedures_task_state
                        ON reusable_procedures(task_id, state, confidence DESC, updated_at_text DESC);
                    CREATE INDEX IF NOT EXISTS idx_reusable_procedures_outcome
                        ON reusable_procedures(curator_outcome_id, procedure_id);

                    CREATE TABLE IF NOT EXISTS reusable_procedure_mining_receipts (
                        receipt_id TEXT PRIMARY KEY,
                        outcome_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        disposition TEXT NOT NULL,
                        procedure_id TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        evidence_digest TEXT NOT NULL,
                        receipt_json TEXT NOT NULL,
                        receipt_digest TEXT NOT NULL,
                        created_at_text TEXT NOT NULL,
                        stored_at REAL NOT NULL,
                        UNIQUE(outcome_id, evidence_digest, disposition)
                    );

                    CREATE INDEX IF NOT EXISTS idx_procedure_mining_receipts_task
                        ON reusable_procedure_mining_receipts(task_id, stored_at, receipt_id);

                    CREATE TABLE IF NOT EXISTS reusable_procedure_signals (
                        signal_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        procedure_id TEXT NOT NULL,
                        outcome_id TEXT NOT NULL,
                        causation_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        payload_digest TEXT NOT NULL,
                        signal_digest TEXT NOT NULL,
                        created_at_text TEXT NOT NULL,
                        published_at REAL,
                        stored_at REAL NOT NULL,
                        FOREIGN KEY(procedure_id) REFERENCES reusable_procedures(procedure_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_procedure_signals_publish
                        ON reusable_procedure_signals(published_at, stored_at, signal_id);

                    CREATE TABLE IF NOT EXISTS reusable_procedure_claims (
                        procedure_id TEXT NOT NULL,
                        consumer TEXT NOT NULL,
                        worker_id TEXT NOT NULL,
                        claim_token TEXT NOT NULL,
                        lease_epoch INTEGER NOT NULL,
                        claimed_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        PRIMARY KEY(procedure_id, consumer),
                        FOREIGN KEY(procedure_id) REFERENCES reusable_procedures(procedure_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_procedure_claims_expiry
                        ON reusable_procedure_claims(consumer, expires_at, procedure_id);

                    CREATE TABLE IF NOT EXISTS reusable_procedure_projections (
                        projection_id TEXT PRIMARY KEY,
                        procedure_id TEXT NOT NULL,
                        consumer TEXT NOT NULL,
                        worker_id TEXT NOT NULL,
                        state TEXT NOT NULL,
                        projected_at REAL NOT NULL,
                        projection_digest TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        FOREIGN KEY(procedure_id) REFERENCES reusable_procedures(procedure_id),
                        UNIQUE(procedure_id, consumer, projection_digest)
                    );

                    CREATE INDEX IF NOT EXISTS idx_procedure_projections_consumer
                        ON reusable_procedure_projections(consumer, projected_at, procedure_id);

                    CREATE TABLE IF NOT EXISTS reusable_procedure_audit (
                        audit_id TEXT PRIMARY KEY,
                        event_type TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        procedure_id TEXT NOT NULL,
                        outcome_id TEXT NOT NULL,
                        causation_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        payload_digest TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_procedure_audit_task
                        ON reusable_procedure_audit(task_id, created_at, audit_id);
                    """
                )
            self._initialized = True

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def put(
        self,
        procedure: ReusableProcedure,
        *,
        expected_revision: int | None = None,
    ) -> tuple[ReusableProcedure, bool]:
        value = procedure.validated()
        stored_at = self.clock()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT revision, procedure_digest FROM reusable_procedures WHERE procedure_id = ?",
                (value.procedure_id,),
            ).fetchone()
            if row is None:
                if expected_revision not in {None, 0}:
                    raise ProcedureStoreConflictError(
                        f"procedure {value.procedure_id} does not exist at revision {expected_revision}"
                    )
                self._insert(connection, value, stored_at)
                created = True
            else:
                current_revision = int(row["revision"])
                current_digest = str(row["procedure_digest"])
                if current_digest == value.procedure_digest:
                    return self._get(connection, value.procedure_id), False
                if expected_revision is None or current_revision != expected_revision:
                    raise ProcedureStoreConflictError(
                        f"procedure {value.procedure_id} revision conflict: {current_revision}"
                    )
                if value.revision != current_revision + 1:
                    raise ProcedureStoreConflictError(
                        "procedure update must advance exactly one revision"
                    )
                connection.execute(
                    """
                    UPDATE reusable_procedures
                    SET state = ?, revision = ?, name = ?, summary = ?, confidence = ?,
                        success_count = ?, failure_count = ?, curator_outcome_id = ?,
                        curator_job_id = ?, curator_decision_id = ?, evidence_bundle_id = ?,
                        evidence_digest = ?, canonical_memory_id = ?,
                        canonical_memory_revision = ?, consumers_json = ?, procedure_json = ?,
                        procedure_digest = ?, updated_at_text = ?, stored_at = ?
                    WHERE procedure_id = ? AND revision = ?
                    """,
                    (
                        value.state.value,
                        value.revision,
                        value.name,
                        value.summary,
                        value.confidence,
                        value.success_count,
                        value.failure_count,
                        value.provenance.curator_outcome_id,
                        value.provenance.curator_job_id,
                        value.provenance.curator_decision_id,
                        value.provenance.evidence_bundle_id,
                        value.provenance.evidence_digest,
                        value.provenance.memory_id,
                        value.provenance.memory_revision,
                        canonical_json([consumer.value for consumer in value.consumers]),
                        canonical_json(value.to_dict()),
                        value.procedure_digest,
                        value.updated_at,
                        stored_at,
                        value.procedure_id,
                        expected_revision,
                    ),
                )
                if self._changes(connection) != 1:
                    raise ProcedureStoreConflictError("procedure compare-and-swap failed")
                created = False
            self._audit(
                connection,
                event_type="procedure.created" if created else "procedure.updated",
                task_id=value.provenance.task_id,
                procedure_id=value.procedure_id,
                outcome_id=value.provenance.curator_outcome_id,
                causation_id=value.provenance.curator_decision_id,
                payload={
                    "state": value.state.value,
                    "revision": value.revision,
                    "procedure_digest": value.procedure_digest,
                    "created": created,
                },
                created_at=stored_at,
            )
            return self._get(connection, value.procedure_id), created

    def get(self, procedure_id: str) -> ReusableProcedure | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT procedure_id FROM reusable_procedures WHERE procedure_id = ?",
                (procedure_id,),
            ).fetchone()
            return self._get(connection, procedure_id) if row is not None else None

    def by_outcome(self, outcome_id: str) -> ReusableProcedure | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT procedure_id FROM reusable_procedures
                WHERE curator_outcome_id = ?
                ORDER BY revision DESC, stored_at DESC LIMIT 1
                """,
                (outcome_id,),
            ).fetchone()
            return self._get(connection, str(row["procedure_id"])) if row is not None else None

    def list(
        self,
        *,
        task_id: str = "",
        states: Sequence[ProcedureValidationStatus] = (),
        consumers: Sequence[ProcedureConsumer] = (),
        minimum_confidence: float = 0.0,
        limit: int = 1000,
    ) -> tuple[ReusableProcedure, ...]:
        clauses: list[str] = ["confidence >= ?"]
        parameters: list[Any] = [max(0.0, min(float(minimum_confidence), 1.0))]
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            parameters.extend(state.value for state in states)
        parameters.append(max(0, min(int(limit), 100_000)))
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT procedure_id FROM reusable_procedures
                WHERE {' AND '.join(clauses)}
                ORDER BY confidence DESC, success_count DESC, updated_at_text DESC,
                         procedure_id ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            procedures = tuple(
                self._get(connection, str(row["procedure_id"])) for row in rows
            )
        if not consumers:
            return procedures
        required = set(consumers)
        return tuple(
            item for item in procedures if required & set(item.consumers)
        )

    def put_receipt(self, receipt: ProcedureMiningReceipt) -> tuple[ProcedureMiningReceipt, bool]:
        value = receipt.validated()
        with self.connection() as connection:
            existing = connection.execute(
                "SELECT receipt_json FROM reusable_procedure_mining_receipts WHERE receipt_id = ?",
                (value.receipt_id,),
            ).fetchone()
            if existing is not None:
                restored = self._receipt(json.loads(str(existing["receipt_json"])))
                if restored.receipt_digest != value.receipt_digest:
                    raise ProcedureStoreConflictError("procedure mining receipt id conflict")
                return restored, False
            connection.execute(
                """
                INSERT INTO reusable_procedure_mining_receipts(
                    receipt_id, outcome_id, task_id, disposition, procedure_id,
                    reason, evidence_digest, receipt_json, receipt_digest,
                    created_at_text, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    value.receipt_id,
                    value.outcome_id,
                    value.task_id,
                    value.disposition.value,
                    value.procedure_id,
                    value.reason,
                    value.evidence_digest,
                    canonical_json(value.to_dict()),
                    value.receipt_digest,
                    value.created_at,
                    self.clock(),
                ),
            )
            return value, True

    def receipts(self, *, task_id: str = "", limit: int = 1000) -> tuple[ProcedureMiningReceipt, ...]:
        where = "WHERE task_id = ?" if task_id else ""
        parameters: list[Any] = [task_id] if task_id else []
        parameters.append(max(0, min(int(limit), 100_000)))
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT receipt_json FROM reusable_procedure_mining_receipts
                {where}
                ORDER BY stored_at DESC, receipt_id DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
            return tuple(self._receipt(json.loads(str(row["receipt_json"]))) for row in rows)

    def put_signal(self, signal: ProcedureSignal) -> tuple[ProcedureSignal, bool]:
        value = signal
        with self.connection() as connection:
            existing = connection.execute(
                "SELECT signal_digest FROM reusable_procedure_signals WHERE signal_id = ?",
                (value.signal_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["signal_digest"]) != value.signal_digest:
                    raise ProcedureStoreConflictError("procedure signal id conflict")
                return self._signal(connection, value.signal_id), False
            connection.execute(
                """
                INSERT INTO reusable_procedure_signals(
                    signal_id, kind, run_id, task_id, procedure_id, outcome_id,
                    causation_id, payload_json, payload_digest, signal_digest,
                    created_at_text, published_at, stored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    value.signal_id,
                    value.kind.value,
                    value.run_id,
                    value.task_id,
                    value.procedure_id,
                    value.outcome_id,
                    value.causation_id,
                    canonical_json(value.payload),
                    value.payload_digest,
                    value.signal_digest,
                    value.created_at,
                    self.clock(),
                ),
            )
            return value, True

    def pending_signals(self, limit: int = 1000) -> tuple[ProcedureSignal, ...]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT signal_id FROM reusable_procedure_signals
                WHERE published_at IS NULL
                ORDER BY stored_at ASC, signal_id ASC LIMIT ?
                """,
                (max(0, min(int(limit), 100_000)),),
            ).fetchall()
            return tuple(self._signal(connection, str(row["signal_id"])) for row in rows)

    def mark_signal_published(self, signal_id: str) -> bool:
        with self.connection() as connection:
            connection.execute(
                "UPDATE reusable_procedure_signals SET published_at = ? WHERE signal_id = ? AND published_at IS NULL",
                (self.clock(), signal_id),
            )
            return self._changes(connection) == 1

    def claim(
        self,
        procedure_id: str,
        *,
        consumer: ProcedureConsumer,
        worker_id: str,
        lease_seconds: float = 30.0,
    ) -> ProcedureClaim:
        if lease_seconds <= 0:
            raise ValueError("procedure claim lease must be positive")
        now = self.clock()
        expires_at = now + lease_seconds
        token = secrets.token_hex(24)
        with self.connection() as connection:
            procedure = self._get(connection, procedure_id)
            if procedure.state is not ProcedureValidationStatus.VALIDATED:
                raise ProcedureStoreConflictError("only validated procedures can be claimed")
            if consumer not in procedure.consumers:
                raise ProcedureStoreConflictError(
                    f"procedure {procedure_id} is not published to {consumer.value}"
                )
            row = connection.execute(
                "SELECT * FROM reusable_procedure_claims WHERE procedure_id = ? AND consumer = ?",
                (procedure_id, consumer.value),
            ).fetchone()
            if row is not None and float(row["expires_at"]) > now and str(row["worker_id"]) != worker_id:
                raise ProcedureStoreConflictError("procedure consumer claim is already held")
            epoch = int(row["lease_epoch"]) + 1 if row is not None else 1
            connection.execute(
                """
                INSERT INTO reusable_procedure_claims(
                    procedure_id, consumer, worker_id, claim_token,
                    lease_epoch, claimed_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(procedure_id, consumer) DO UPDATE SET
                    worker_id = excluded.worker_id,
                    claim_token = excluded.claim_token,
                    lease_epoch = excluded.lease_epoch,
                    claimed_at = excluded.claimed_at,
                    expires_at = excluded.expires_at
                """,
                (procedure_id, consumer.value, worker_id, token, epoch, now, expires_at),
            )
            return ProcedureClaim(
                procedure_id=procedure_id,
                consumer=consumer,
                worker_id=worker_id,
                claim_token=token,
                lease_epoch=epoch,
                claimed_at=now,
                expires_at=expires_at,
            )

    def project(
        self,
        claim: ProcedureClaim,
        *,
        state: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProcedureProjectionReceipt:
        now = self.clock()
        with self.connection() as connection:
            self._assert_claim(connection, claim, now=now)
            projection_payload = {
                "procedure_id": claim.procedure_id,
                "consumer": claim.consumer.value,
                "worker_id": claim.worker_id,
                "state": state,
                "lease_epoch": claim.lease_epoch,
                "metadata": dict(metadata or {}),
            }
            projection_digest = stable_digest(projection_payload)
            projection_id = stable_id(
                "procedure-projection",
                claim.procedure_id,
                claim.consumer.value,
                projection_digest,
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO reusable_procedure_projections(
                    projection_id, procedure_id, consumer, worker_id, state,
                    projected_at, projection_digest, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    projection_id,
                    claim.procedure_id,
                    claim.consumer.value,
                    claim.worker_id,
                    state,
                    now,
                    projection_digest,
                    canonical_json(metadata or {}),
                ),
            )
            connection.execute(
                "DELETE FROM reusable_procedure_claims WHERE procedure_id = ? AND consumer = ? AND claim_token = ? AND lease_epoch = ?",
                (
                    claim.procedure_id,
                    claim.consumer.value,
                    claim.claim_token,
                    claim.lease_epoch,
                ),
            )
            return ProcedureProjectionReceipt(
                projection_id=projection_id,
                procedure_id=claim.procedure_id,
                consumer=claim.consumer,
                worker_id=claim.worker_id,
                state=state,
                projected_at=now,
                projection_digest=projection_digest,
                metadata=dict(metadata or {}),
            )

    def status(self, task_id: str = "") -> Mapping[str, Any]:
        with self.connection() as connection:
            where = "WHERE task_id = ?" if task_id else ""
            parameters: tuple[Any, ...] = (task_id,) if task_id else ()
            rows = connection.execute(
                f"SELECT state, COUNT(*) AS count FROM reusable_procedures {where} GROUP BY state",
                parameters,
            ).fetchall()
            counts = {str(row["state"]): int(row["count"]) for row in rows}
            pending = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM reusable_procedure_signals WHERE published_at IS NULL"
                ).fetchone()["count"]
            )
            active_claims = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM reusable_procedure_claims WHERE expires_at > ?",
                    (self.clock(),),
                ).fetchone()["count"]
            )
        return {
            "protocol": PROCEDURE_STORE_PROTOCOL,
            "task_id": task_id,
            "state_counts": counts,
            "pending_signals": pending,
            "active_claims": active_claims,
            "canonical_curator_owner": "CuratorIntegrationStore",
            "canonical_memory_owner": "SQLiteStore.memory_records",
            "procedure_projection_owner": "ReusableProcedureStore",
            "skill_version_owner": "03C SkillCoordinator",
            "external_database_dependency": False,
            "static_document_activation": False,
        }

    def audit(self, *, task_id: str = "", limit: int = 1000) -> tuple[Mapping[str, Any], ...]:
        where = "WHERE task_id = ?" if task_id else ""
        parameters: list[Any] = [task_id] if task_id else []
        parameters.append(max(0, min(int(limit), 100_000)))
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM reusable_procedure_audit {where}
                ORDER BY created_at DESC, audit_id DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(
            {
                "audit_id": str(row["audit_id"]),
                "event_type": str(row["event_type"]),
                "task_id": str(row["task_id"]),
                "procedure_id": str(row["procedure_id"]),
                "outcome_id": str(row["outcome_id"]),
                "causation_id": str(row["causation_id"]),
                "payload": json.loads(str(row["payload_json"])),
                "payload_digest": str(row["payload_digest"]),
                "created_at": float(row["created_at"]),
            }
            for row in rows
        )

    def _insert(
        self,
        connection: sqlite3.Connection,
        procedure: ReusableProcedure,
        stored_at: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO reusable_procedures(
                procedure_id, run_id, task_id, state, revision, name, summary,
                confidence, success_count, failure_count, curator_outcome_id,
                curator_job_id, curator_decision_id, evidence_bundle_id,
                evidence_digest, canonical_memory_id, canonical_memory_revision,
                consumers_json, procedure_json, procedure_digest,
                created_at_text, updated_at_text, stored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                procedure.procedure_id,
                procedure.provenance.run_id,
                procedure.provenance.task_id,
                procedure.state.value,
                procedure.revision,
                procedure.name,
                procedure.summary,
                procedure.confidence,
                procedure.success_count,
                procedure.failure_count,
                procedure.provenance.curator_outcome_id,
                procedure.provenance.curator_job_id,
                procedure.provenance.curator_decision_id,
                procedure.provenance.evidence_bundle_id,
                procedure.provenance.evidence_digest,
                procedure.provenance.memory_id,
                procedure.provenance.memory_revision,
                canonical_json([consumer.value for consumer in procedure.consumers]),
                canonical_json(procedure.to_dict()),
                procedure.procedure_digest,
                procedure.created_at,
                procedure.updated_at,
                stored_at,
            ),
        )

    def _get(self, connection: sqlite3.Connection, procedure_id: str) -> ReusableProcedure:
        row = connection.execute(
            "SELECT procedure_json FROM reusable_procedures WHERE procedure_id = ?",
            (procedure_id,),
        ).fetchone()
        if row is None:
            raise KeyError(procedure_id)
        return ReusableProcedure.from_dict(json.loads(str(row["procedure_json"])))

    @staticmethod
    def _receipt(value: Mapping[str, Any]) -> ProcedureMiningReceipt:
        return ProcedureMiningReceipt(
            receipt_id=str(value.get("receipt_id") or ""),
            outcome_id=str(value.get("outcome_id") or ""),
            task_id=str(value.get("task_id") or ""),
            disposition=ProcedureMiningDisposition(str(value.get("disposition"))),
            procedure_id=str(value.get("procedure_id") or ""),
            reason=str(value.get("reason") or ""),
            evidence_digest=str(value.get("evidence_digest") or ""),
            state_before=str(value.get("state_before") or ""),
            state_after=str(value.get("state_after") or ""),
            created_at=str(value.get("created_at") or ""),
            receipt_digest=str(value.get("receipt_digest") or ""),
            metadata=dict(value.get("metadata") or {}),
        ).validated()

    @staticmethod
    def _signal(connection: sqlite3.Connection, signal_id: str) -> ProcedureSignal:
        row = connection.execute(
            "SELECT * FROM reusable_procedure_signals WHERE signal_id = ?",
            (signal_id,),
        ).fetchone()
        if row is None:
            raise KeyError(signal_id)
        from .procedure_models import ProcedureSignalKind

        return ProcedureSignal(
            signal_id=str(row["signal_id"]),
            kind=ProcedureSignalKind(str(row["kind"])),
            run_id=str(row["run_id"]),
            task_id=str(row["task_id"]),
            procedure_id=str(row["procedure_id"]),
            outcome_id=str(row["outcome_id"]),
            causation_id=str(row["causation_id"]),
            payload=json.loads(str(row["payload_json"])),
            payload_digest=str(row["payload_digest"]),
            created_at=str(row["created_at_text"]),
            signal_digest=str(row["signal_digest"]),
        )

    def _assert_claim(
        self,
        connection: sqlite3.Connection,
        claim: ProcedureClaim,
        *,
        now: float,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM reusable_procedure_claims WHERE procedure_id = ? AND consumer = ?",
            (claim.procedure_id, claim.consumer.value),
        ).fetchone()
        if row is None:
            raise ProcedureStoreConflictError("procedure claim does not exist")
        if str(row["worker_id"]) != claim.worker_id:
            raise ProcedureStoreConflictError("procedure claim owner mismatch")
        if str(row["claim_token"]) != claim.claim_token:
            raise ProcedureStoreConflictError("procedure claim token mismatch")
        if int(row["lease_epoch"]) != claim.lease_epoch:
            raise ProcedureStoreConflictError("procedure claim epoch mismatch")
        if float(row["expires_at"]) <= now:
            raise ProcedureStoreConflictError("procedure claim expired")

    def _audit(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        task_id: str,
        procedure_id: str,
        outcome_id: str,
        causation_id: str,
        payload: Mapping[str, Any],
        created_at: float,
    ) -> None:
        payload_digest = stable_digest(payload)
        audit_id = stable_id(
            "procedure-audit",
            event_type,
            task_id,
            procedure_id,
            outcome_id,
            causation_id,
            payload_digest,
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO reusable_procedure_audit(
                audit_id, event_type, task_id, procedure_id, outcome_id,
                causation_id, payload_json, payload_digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                event_type,
                task_id,
                procedure_id,
                outcome_id,
                causation_id,
                canonical_json(payload),
                payload_digest,
                created_at,
            ),
        )

    @staticmethod
    def _changes(connection: sqlite3.Connection) -> int:
        return int(connection.execute("SELECT changes() AS count").fetchone()["count"])


__all__ = [
    "ProcedureClaim",
    "ProcedureProjectionReceipt",
    "ReusableProcedureStore",
]
