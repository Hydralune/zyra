from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


JOURNAL_SCHEMA_VERSION = 1


class MigrationTransactionState(StrEnum):
    PREPARED = "prepared"
    APPLYING = "applying"
    VERIFYING = "verifying"
    COMMITTED = "committed"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


class MigrationStepState(StrEnum):
    PENDING = "pending"
    BACKED_UP = "backed_up"
    APPLYING = "applying"
    APPLIED = "applied"
    VERIFIED = "verified"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


class MigrationJournalError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        transaction_id: str = "",
        adapter_id: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.transaction_id = transaction_id
        self.adapter_id = adapter_id
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class MigrationLease:
    owner_id: str
    generation: int
    token: str
    acquired_at_ns: int
    expires_at_ns: int

    def expired(self, now_ns: int | None = None) -> bool:
        return (time.time_ns() if now_ns is None else now_ns) >= self.expires_at_ns

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.migration-lease/v1",
            "owner_id": self.owner_id,
            "generation": self.generation,
            "token": self.token,
            "acquired_at_ns": self.acquired_at_ns,
            "expires_at_ns": self.expires_at_ns,
        }


@dataclass(frozen=True, slots=True)
class MigrationTransactionRecord:
    transaction_id: str
    state: MigrationTransactionState
    configuration_digest: str
    process_generation: str
    plan_digest: str
    target_version: int
    created_at_ns: int
    updated_at_ns: int
    committed_at_ns: int | None
    error_json: str

    @property
    def terminal(self) -> bool:
        return self.state in {
            MigrationTransactionState.COMMITTED,
            MigrationTransactionState.ROLLED_BACK,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.migration-transaction/v1",
            "transaction_id": self.transaction_id,
            "state": self.state.value,
            "configuration_digest": self.configuration_digest,
            "process_generation": self.process_generation,
            "plan_digest": self.plan_digest,
            "target_version": self.target_version,
            "created_at_ns": self.created_at_ns,
            "updated_at_ns": self.updated_at_ns,
            "committed_at_ns": self.committed_at_ns,
            "error": json.loads(self.error_json) if self.error_json else None,
        }


@dataclass(frozen=True, slots=True)
class MigrationStepRecord:
    transaction_id: str
    ordinal: int
    adapter_id: str
    owner: str
    source_version: int
    target_version: int
    state: MigrationStepState
    mutation_digest: str
    started_at_ns: int | None
    completed_at_ns: int | None
    error_json: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.migration-step/v1",
            "transaction_id": self.transaction_id,
            "ordinal": self.ordinal,
            "adapter_id": self.adapter_id,
            "owner": self.owner,
            "source_version": self.source_version,
            "target_version": self.target_version,
            "state": self.state.value,
            "mutation_digest": self.mutation_digest,
            "started_at_ns": self.started_at_ns,
            "completed_at_ns": self.completed_at_ns,
            "error": json.loads(self.error_json) if self.error_json else None,
        }


@dataclass(frozen=True, slots=True)
class MigrationBackupRecord:
    transaction_id: str
    adapter_id: str
    backup_id: str
    source_path: str
    backup_path: str
    source_kind: str
    size_bytes: int
    checksum: str
    created_at_ns: int
    restored_at_ns: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.migration-backup/v1",
            "transaction_id": self.transaction_id,
            "adapter_id": self.adapter_id,
            "backup_id": self.backup_id,
            "source_path": self.source_path,
            "backup_path": self.backup_path,
            "source_kind": self.source_kind,
            "size_bytes": self.size_bytes,
            "checksum": self.checksum,
            "created_at_ns": self.created_at_ns,
            "restored_at_ns": self.restored_at_ns,
        }


class MigrationJournal:
    _TRANSACTION_TRANSITIONS = {
        MigrationTransactionState.PREPARED: {
            MigrationTransactionState.APPLYING,
            MigrationTransactionState.FAILED,
            MigrationTransactionState.ROLLING_BACK,
        },
        MigrationTransactionState.APPLYING: {
            MigrationTransactionState.VERIFYING,
            MigrationTransactionState.FAILED,
            MigrationTransactionState.ROLLING_BACK,
        },
        MigrationTransactionState.VERIFYING: {
            MigrationTransactionState.COMMITTED,
            MigrationTransactionState.FAILED,
            MigrationTransactionState.ROLLING_BACK,
        },
        MigrationTransactionState.FAILED: {
            MigrationTransactionState.ROLLING_BACK,
        },
        MigrationTransactionState.ROLLING_BACK: {
            MigrationTransactionState.ROLLED_BACK,
            MigrationTransactionState.FAILED,
        },
        MigrationTransactionState.COMMITTED: set(),
        MigrationTransactionState.ROLLED_BACK: set(),
    }
    _STEP_TRANSITIONS = {
        MigrationStepState.PENDING: {
            MigrationStepState.BACKED_UP,
            MigrationStepState.APPLYING,
            MigrationStepState.FAILED,
        },
        MigrationStepState.BACKED_UP: {
            MigrationStepState.APPLYING,
            MigrationStepState.ROLLING_BACK,
            MigrationStepState.FAILED,
        },
        MigrationStepState.APPLYING: {
            MigrationStepState.APPLIED,
            MigrationStepState.FAILED,
            MigrationStepState.ROLLING_BACK,
        },
        MigrationStepState.APPLIED: {
            MigrationStepState.VERIFIED,
            MigrationStepState.FAILED,
            MigrationStepState.ROLLING_BACK,
        },
        MigrationStepState.VERIFIED: {
            MigrationStepState.ROLLING_BACK,
        },
        MigrationStepState.FAILED: {
            MigrationStepState.ROLLING_BACK,
        },
        MigrationStepState.ROLLING_BACK: {
            MigrationStepState.ROLLED_BACK,
            MigrationStepState.FAILED,
        },
        MigrationStepState.ROLLED_BACK: set(),
    }

    @classmethod
    def can_transition_transaction(
        cls,
        current: MigrationTransactionState,
        target: MigrationTransactionState,
    ) -> bool:
        return target is current or target in cls._TRANSACTION_TRANSITIONS[current]

    @classmethod
    def can_transition_step(
        cls,
        current: MigrationStepState,
        target: MigrationStepState,
    ) -> bool:
        return target is current or target in cls._STEP_TRANSITIONS[current]

    def __init__(
        self,
        path: Path | str,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock_ns = clock_ns
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=30000")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS product_migration_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at_ns INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS product_migration_lease (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    owner_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    token TEXT NOT NULL,
                    acquired_at_ns INTEGER NOT NULL,
                    expires_at_ns INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS product_migration_transactions (
                    transaction_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    configuration_digest TEXT NOT NULL,
                    process_generation TEXT NOT NULL,
                    plan_digest TEXT NOT NULL,
                    target_version INTEGER NOT NULL,
                    created_at_ns INTEGER NOT NULL,
                    updated_at_ns INTEGER NOT NULL,
                    committed_at_ns INTEGER,
                    error_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS product_migration_steps (
                    transaction_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    adapter_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    source_version INTEGER NOT NULL,
                    target_version INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    mutation_digest TEXT NOT NULL,
                    started_at_ns INTEGER,
                    completed_at_ns INTEGER,
                    error_json TEXT NOT NULL,
                    PRIMARY KEY(transaction_id, adapter_id),
                    UNIQUE(transaction_id, ordinal),
                    FOREIGN KEY(transaction_id)
                        REFERENCES product_migration_transactions(transaction_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS product_migration_backups (
                    transaction_id TEXT NOT NULL,
                    adapter_id TEXT NOT NULL,
                    backup_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    backup_path TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    checksum TEXT NOT NULL,
                    created_at_ns INTEGER NOT NULL,
                    restored_at_ns INTEGER,
                    PRIMARY KEY(transaction_id, adapter_id, backup_id),
                    FOREIGN KEY(transaction_id, adapter_id)
                        REFERENCES product_migration_steps(transaction_id, adapter_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_product_migrations_state
                    ON product_migration_transactions(state, updated_at_ns);
                CREATE INDEX IF NOT EXISTS idx_product_migration_steps_state
                    ON product_migration_steps(transaction_id, state, ordinal);
                """
            )
            now = self._clock_ns()
            row = self._connection.execute(
                "SELECT value FROM product_migration_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._connection.execute(
                    """
                    INSERT INTO product_migration_meta(key, value, updated_at_ns)
                    VALUES('schema_version', ?, ?)
                    """,
                    (str(JOURNAL_SCHEMA_VERSION), now),
                )
            elif int(row["value"]) != JOURNAL_SCHEMA_VERSION:
                raise MigrationJournalError(
                    "migration_journal_schema_incompatible",
                    "migration journal schema is incompatible",
                    details={
                        "expected": JOURNAL_SCHEMA_VERSION,
                        "actual": int(row["value"]),
                    },
                )

    def acquire_lease(
        self,
        owner_id: str,
        *,
        ttl_seconds: int,
    ) -> MigrationLease:
        if not owner_id.strip():
            raise ValueError("migration lease owner_id is required")
        if not 5 <= ttl_seconds <= 3600:
            raise ValueError("migration lease ttl must be between 5 and 3600")
        now = self._clock_ns()
        with self._write_transaction():
            row = self._connection.execute(
                "SELECT * FROM product_migration_lease WHERE singleton=1"
            ).fetchone()
            if row is not None and int(row["expires_at_ns"]) > now:
                if str(row["owner_id"]) != owner_id:
                    prior_owner = str(row["owner_id"])
                    if _lease_owner_alive(prior_owner):
                        raise MigrationJournalError(
                            "migration_lease_busy",
                            "another live process owns the migration lease",
                            details={
                                "owner_id": prior_owner,
                                "generation": int(row["generation"]),
                                "expires_at_ns": int(row["expires_at_ns"]),
                            },
                        )
                generation = int(row["generation"]) + 1
            else:
                generation = (int(row["generation"]) + 1) if row is not None else 1
            lease = MigrationLease(
                owner_id=owner_id,
                generation=generation,
                token="migration-lease-" + secrets.token_urlsafe(24),
                acquired_at_ns=now,
                expires_at_ns=now + ttl_seconds * 1_000_000_000,
            )
            self._connection.execute(
                """
                INSERT INTO product_migration_lease(
                    singleton, owner_id, generation, token,
                    acquired_at_ns, expires_at_ns
                ) VALUES(1, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    owner_id=excluded.owner_id,
                    generation=excluded.generation,
                    token=excluded.token,
                    acquired_at_ns=excluded.acquired_at_ns,
                    expires_at_ns=excluded.expires_at_ns
                """,
                (
                    lease.owner_id,
                    lease.generation,
                    lease.token,
                    lease.acquired_at_ns,
                    lease.expires_at_ns,
                ),
            )
            return lease

    def renew_lease(
        self,
        lease: MigrationLease,
        *,
        ttl_seconds: int,
    ) -> MigrationLease:
        if not 5 <= ttl_seconds <= 3600:
            raise ValueError("migration lease ttl must be between 5 and 3600")
        now = self._clock_ns()
        with self._write_transaction():
            self._assert_lease(lease, allow_expired=False)
            renewed = MigrationLease(
                owner_id=lease.owner_id,
                generation=lease.generation,
                token=lease.token,
                acquired_at_ns=lease.acquired_at_ns,
                expires_at_ns=now + ttl_seconds * 1_000_000_000,
            )
            changed = self._connection.execute(
                """
                UPDATE product_migration_lease
                SET expires_at_ns=?
                WHERE singleton=1 AND owner_id=? AND generation=? AND token=?
                """,
                (
                    renewed.expires_at_ns,
                    lease.owner_id,
                    lease.generation,
                    lease.token,
                ),
            ).rowcount
            if changed != 1:
                raise MigrationJournalError(
                    "migration_lease_lost",
                    "migration lease was lost during renewal",
                )
            return renewed

    def release_lease(self, lease: MigrationLease) -> bool:
        with self._write_transaction():
            changed = self._connection.execute(
                """
                DELETE FROM product_migration_lease
                WHERE singleton=1 AND owner_id=? AND generation=? AND token=?
                """,
                (lease.owner_id, lease.generation, lease.token),
            ).rowcount
            return changed == 1

    def assert_lease(self, lease: MigrationLease) -> None:
        with self._lock:
            self._assert_open()
            self._assert_lease(lease, allow_expired=False)

    def begin(
        self,
        *,
        lease: MigrationLease,
        transaction_id: str,
        configuration_digest: str,
        process_generation: str,
        plan_digest: str,
        target_version: int,
        steps: tuple[Mapping[str, Any], ...],
    ) -> MigrationTransactionRecord:
        if not transaction_id.strip():
            raise ValueError("transaction_id is required")
        if target_version < 1:
            raise ValueError("target_version must be positive")
        now = self._clock_ns()
        with self._write_transaction():
            self._assert_lease(lease, allow_expired=False)
            existing = self._connection.execute(
                """
                SELECT * FROM product_migration_transactions
                WHERE transaction_id=?
                """,
                (transaction_id,),
            ).fetchone()
            if existing is not None:
                record = self._transaction_from_row(existing)
                if (
                    record.configuration_digest != configuration_digest
                    or record.process_generation != process_generation
                    or record.plan_digest != plan_digest
                    or record.target_version != target_version
                ):
                    raise MigrationJournalError(
                        "migration_transaction_identity_conflict",
                        "migration transaction id was reused with different content",
                        transaction_id=transaction_id,
                    )
                return record
            active = self._connection.execute(
                """
                SELECT transaction_id, state
                FROM product_migration_transactions
                WHERE state NOT IN ('committed', 'rolled_back')
                ORDER BY updated_at_ns DESC
                LIMIT 1
                """
            ).fetchone()
            if active is not None:
                raise MigrationJournalError(
                    "migration_transaction_active",
                    "an unfinished migration transaction must be recovered first",
                    transaction_id=str(active["transaction_id"]),
                    details={"state": str(active["state"])},
                )
            self._connection.execute(
                """
                INSERT INTO product_migration_transactions(
                    transaction_id, state, configuration_digest,
                    process_generation, plan_digest, target_version,
                    created_at_ns, updated_at_ns, committed_at_ns, error_json
                ) VALUES(?, 'prepared', ?, ?, ?, ?, ?, ?, NULL, '')
                """,
                (
                    transaction_id,
                    configuration_digest,
                    process_generation,
                    plan_digest,
                    target_version,
                    now,
                    now,
                ),
            )
            ordinals: set[int] = set()
            adapters: set[str] = set()
            for step in steps:
                ordinal = int(step["ordinal"])
                adapter_id = str(step["adapter_id"])
                if ordinal < 0 or ordinal in ordinals:
                    raise MigrationJournalError(
                        "migration_step_ordinal_invalid",
                        "migration step ordinals must be unique non-negative integers",
                        transaction_id=transaction_id,
                        adapter_id=adapter_id,
                    )
                if not adapter_id or adapter_id in adapters:
                    raise MigrationJournalError(
                        "migration_step_adapter_duplicate",
                        "migration adapter ids must be unique",
                        transaction_id=transaction_id,
                        adapter_id=adapter_id,
                    )
                ordinals.add(ordinal)
                adapters.add(adapter_id)
                self._connection.execute(
                    """
                    INSERT INTO product_migration_steps(
                        transaction_id, ordinal, adapter_id, owner,
                        source_version, target_version, state, mutation_digest,
                        started_at_ns, completed_at_ns, error_json
                    ) VALUES(?, ?, ?, ?, ?, ?, 'pending', '', NULL, NULL, '')
                    """,
                    (
                        transaction_id,
                        ordinal,
                        adapter_id,
                        str(step["owner"]),
                        int(step["source_version"]),
                        int(step["target_version"]),
                    ),
                )
            return self.require_transaction(transaction_id)

    def transition_transaction(
        self,
        *,
        lease: MigrationLease,
        transaction_id: str,
        target: MigrationTransactionState,
        error: Mapping[str, Any] | None = None,
    ) -> MigrationTransactionRecord:
        now = self._clock_ns()
        with self._write_transaction():
            self._assert_lease(lease, allow_expired=False)
            current = self.require_transaction(transaction_id)
            if target is current.state:
                return current
            if not self.can_transition_transaction(current.state, target):
                raise MigrationJournalError(
                    "migration_transaction_transition_invalid",
                    f"invalid transaction transition: {current.state.value} -> {target.value}",
                    transaction_id=transaction_id,
                )
            committed_at = (
                now if target is MigrationTransactionState.COMMITTED else None
            )
            error_json = (
                _canonical_json(error)
                if error is not None
                else current.error_json
            )
            self._connection.execute(
                """
                UPDATE product_migration_transactions
                SET state=?, updated_at_ns=?, committed_at_ns=COALESCE(?, committed_at_ns),
                    error_json=?
                WHERE transaction_id=?
                """,
                (target.value, now, committed_at, error_json, transaction_id),
            )
            return self.require_transaction(transaction_id)

    def transition_step(
        self,
        *,
        lease: MigrationLease,
        transaction_id: str,
        adapter_id: str,
        target: MigrationStepState,
        mutation_digest: str = "",
        error: Mapping[str, Any] | None = None,
    ) -> MigrationStepRecord:
        now = self._clock_ns()
        with self._write_transaction():
            self._assert_lease(lease, allow_expired=False)
            current = self.require_step(transaction_id, adapter_id)
            if target is current.state:
                return current
            if not self.can_transition_step(current.state, target):
                raise MigrationJournalError(
                    "migration_step_transition_invalid",
                    f"invalid step transition: {current.state.value} -> {target.value}",
                    transaction_id=transaction_id,
                    adapter_id=adapter_id,
                )
            started = current.started_at_ns
            if target in {
                MigrationStepState.APPLYING,
                MigrationStepState.ROLLING_BACK,
            } and started is None:
                started = now
            completed = (
                now
                if target
                in {
                    MigrationStepState.VERIFIED,
                    MigrationStepState.ROLLED_BACK,
                    MigrationStepState.FAILED,
                }
                else current.completed_at_ns
            )
            selected_digest = mutation_digest or current.mutation_digest
            selected_error = (
                _canonical_json(error)
                if error is not None
                else current.error_json
            )
            self._connection.execute(
                """
                UPDATE product_migration_steps
                SET state=?, mutation_digest=?, started_at_ns=?,
                    completed_at_ns=?, error_json=?
                WHERE transaction_id=? AND adapter_id=?
                """,
                (
                    target.value,
                    selected_digest,
                    started,
                    completed,
                    selected_error,
                    transaction_id,
                    adapter_id,
                ),
            )
            return self.require_step(transaction_id, adapter_id)

    def add_backup(
        self,
        *,
        lease: MigrationLease,
        record: MigrationBackupRecord,
    ) -> MigrationBackupRecord:
        if record.size_bytes < 0:
            raise ValueError("backup size cannot be negative")
        if not record.checksum.startswith("sha256:"):
            raise ValueError("backup checksum must use sha256")
        with self._write_transaction():
            self._assert_lease(lease, allow_expired=False)
            self.require_step(record.transaction_id, record.adapter_id)
            existing = self._connection.execute(
                """
                SELECT * FROM product_migration_backups
                WHERE transaction_id=? AND adapter_id=? AND backup_id=?
                """,
                (record.transaction_id, record.adapter_id, record.backup_id),
            ).fetchone()
            if existing is not None:
                selected = self._backup_from_row(existing)
                if selected != record:
                    raise MigrationJournalError(
                        "migration_backup_identity_conflict",
                        "migration backup id was reused with different metadata",
                        transaction_id=record.transaction_id,
                        adapter_id=record.adapter_id,
                    )
                return selected
            self._connection.execute(
                """
                INSERT INTO product_migration_backups(
                    transaction_id, adapter_id, backup_id, source_path,
                    backup_path, source_kind, size_bytes, checksum,
                    created_at_ns, restored_at_ns
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.transaction_id,
                    record.adapter_id,
                    record.backup_id,
                    record.source_path,
                    record.backup_path,
                    record.source_kind,
                    record.size_bytes,
                    record.checksum,
                    record.created_at_ns,
                    record.restored_at_ns,
                ),
            )
            return record

    def mark_backup_restored(
        self,
        *,
        lease: MigrationLease,
        transaction_id: str,
        adapter_id: str,
        backup_id: str,
    ) -> MigrationBackupRecord:
        now = self._clock_ns()
        with self._write_transaction():
            self._assert_lease(lease, allow_expired=False)
            changed = self._connection.execute(
                """
                UPDATE product_migration_backups
                SET restored_at_ns=COALESCE(restored_at_ns, ?)
                WHERE transaction_id=? AND adapter_id=? AND backup_id=?
                """,
                (now, transaction_id, adapter_id, backup_id),
            ).rowcount
            if changed != 1:
                raise MigrationJournalError(
                    "migration_backup_unknown",
                    "migration backup record does not exist",
                    transaction_id=transaction_id,
                    adapter_id=adapter_id,
                    details={"backup_id": backup_id},
                )
            row = self._connection.execute(
                """
                SELECT * FROM product_migration_backups
                WHERE transaction_id=? AND adapter_id=? AND backup_id=?
                """,
                (transaction_id, adapter_id, backup_id),
            ).fetchone()
            assert row is not None
            return self._backup_from_row(row)

    def require_transaction(self, transaction_id: str) -> MigrationTransactionRecord:
        with self._lock:
            self._assert_open()
            row = self._connection.execute(
                """
                SELECT * FROM product_migration_transactions
                WHERE transaction_id=?
                """,
                (transaction_id,),
            ).fetchone()
            if row is None:
                raise MigrationJournalError(
                    "migration_transaction_unknown",
                    "migration transaction does not exist",
                    transaction_id=transaction_id,
                )
            return self._transaction_from_row(row)

    def require_step(
        self,
        transaction_id: str,
        adapter_id: str,
    ) -> MigrationStepRecord:
        with self._lock:
            self._assert_open()
            row = self._connection.execute(
                """
                SELECT * FROM product_migration_steps
                WHERE transaction_id=? AND adapter_id=?
                """,
                (transaction_id, adapter_id),
            ).fetchone()
            if row is None:
                raise MigrationJournalError(
                    "migration_step_unknown",
                    "migration step does not exist",
                    transaction_id=transaction_id,
                    adapter_id=adapter_id,
                )
            return self._step_from_row(row)

    def active_transaction(self) -> MigrationTransactionRecord | None:
        with self._lock:
            self._assert_open()
            row = self._connection.execute(
                """
                SELECT * FROM product_migration_transactions
                WHERE state NOT IN ('committed', 'rolled_back')
                ORDER BY updated_at_ns DESC
                LIMIT 1
                """
            ).fetchone()
            return self._transaction_from_row(row) if row is not None else None

    def latest_committed(self) -> MigrationTransactionRecord | None:
        with self._lock:
            self._assert_open()
            row = self._connection.execute(
                """
                SELECT * FROM product_migration_transactions
                WHERE state='committed'
                ORDER BY committed_at_ns DESC
                LIMIT 1
                """
            ).fetchone()
            return self._transaction_from_row(row) if row is not None else None

    def steps(self, transaction_id: str) -> tuple[MigrationStepRecord, ...]:
        with self._lock:
            self._assert_open()
            rows = self._connection.execute(
                """
                SELECT * FROM product_migration_steps
                WHERE transaction_id=?
                ORDER BY ordinal
                """,
                (transaction_id,),
            ).fetchall()
            return tuple(self._step_from_row(row) for row in rows)

    def backups(
        self,
        transaction_id: str,
        adapter_id: str | None = None,
    ) -> tuple[MigrationBackupRecord, ...]:
        with self._lock:
            self._assert_open()
            if adapter_id is None:
                rows = self._connection.execute(
                    """
                    SELECT * FROM product_migration_backups
                    WHERE transaction_id=?
                    ORDER BY adapter_id, created_at_ns, backup_id
                    """,
                    (transaction_id,),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT * FROM product_migration_backups
                    WHERE transaction_id=? AND adapter_id=?
                    ORDER BY created_at_ns, backup_id
                    """,
                    (transaction_id, adapter_id),
                ).fetchall()
            return tuple(self._backup_from_row(row) for row in rows)

    def integrity_report(self) -> dict[str, Any]:
        with self._lock:
            self._assert_open()
            quick = str(
                self._connection.execute("PRAGMA quick_check").fetchone()[0]
            )
            invalid_transactions: list[str] = []
            rows = self._connection.execute(
                "SELECT transaction_id, state FROM product_migration_transactions"
            ).fetchall()
            for row in rows:
                try:
                    MigrationTransactionState(str(row["state"]))
                except ValueError:
                    invalid_transactions.append(str(row["transaction_id"]))
            invalid_steps: list[str] = []
            rows = self._connection.execute(
                "SELECT transaction_id, adapter_id, state FROM product_migration_steps"
            ).fetchall()
            for row in rows:
                try:
                    MigrationStepState(str(row["state"]))
                except ValueError:
                    invalid_steps.append(
                        f"{row['transaction_id']}:{row['adapter_id']}"
                    )
            orphan_backups = int(
                self._connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM product_migration_backups b
                    LEFT JOIN product_migration_steps s
                      ON s.transaction_id=b.transaction_id
                     AND s.adapter_id=b.adapter_id
                    WHERE s.adapter_id IS NULL
                    """
                ).fetchone()[0]
            )
            active = self.active_transaction()
            return {
                "schema": "zyra.migration-journal-integrity/v1",
                "healthy": (
                    quick == "ok"
                    and not invalid_transactions
                    and not invalid_steps
                    and orphan_backups == 0
                ),
                "quick_check": quick,
                "invalid_transactions": invalid_transactions,
                "invalid_steps": invalid_steps,
                "orphan_backups": orphan_backups,
                "active_transaction": (
                    active.to_dict() if active is not None else None
                ),
            }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True

    def _assert_lease(
        self,
        lease: MigrationLease,
        *,
        allow_expired: bool,
    ) -> None:
        row = self._connection.execute(
            "SELECT * FROM product_migration_lease WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise MigrationJournalError(
                "migration_lease_missing",
                "migration lease is not held",
            )
        matches = (
            str(row["owner_id"]) == lease.owner_id
            and int(row["generation"]) == lease.generation
            and secrets.compare_digest(str(row["token"]), lease.token)
        )
        if not matches:
            raise MigrationJournalError(
                "migration_lease_stale",
                "migration lease generation or token is stale",
                details={
                    "expected_owner": lease.owner_id,
                    "actual_owner": str(row["owner_id"]),
                    "expected_generation": lease.generation,
                    "actual_generation": int(row["generation"]),
                },
            )
        now = self._clock_ns()
        if not allow_expired and int(row["expires_at_ns"]) <= now:
            raise MigrationJournalError(
                "migration_lease_expired",
                "migration lease expired",
                details={"expires_at_ns": int(row["expires_at_ns"]), "now_ns": now},
            )

    @contextlib.contextmanager
    def _write_transaction(self):
        with self._lock:
            self._assert_open()
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def _assert_open(self) -> None:
        if self._closed:
            raise MigrationJournalError(
                "migration_journal_closed",
                "migration journal is closed",
            )

    @staticmethod
    def _transaction_from_row(row: sqlite3.Row) -> MigrationTransactionRecord:
        return MigrationTransactionRecord(
            transaction_id=str(row["transaction_id"]),
            state=MigrationTransactionState(str(row["state"])),
            configuration_digest=str(row["configuration_digest"]),
            process_generation=str(row["process_generation"]),
            plan_digest=str(row["plan_digest"]),
            target_version=int(row["target_version"]),
            created_at_ns=int(row["created_at_ns"]),
            updated_at_ns=int(row["updated_at_ns"]),
            committed_at_ns=(
                int(row["committed_at_ns"])
                if row["committed_at_ns"] is not None
                else None
            ),
            error_json=str(row["error_json"]),
        )

    @staticmethod
    def _step_from_row(row: sqlite3.Row) -> MigrationStepRecord:
        return MigrationStepRecord(
            transaction_id=str(row["transaction_id"]),
            ordinal=int(row["ordinal"]),
            adapter_id=str(row["adapter_id"]),
            owner=str(row["owner"]),
            source_version=int(row["source_version"]),
            target_version=int(row["target_version"]),
            state=MigrationStepState(str(row["state"])),
            mutation_digest=str(row["mutation_digest"]),
            started_at_ns=(
                int(row["started_at_ns"])
                if row["started_at_ns"] is not None
                else None
            ),
            completed_at_ns=(
                int(row["completed_at_ns"])
                if row["completed_at_ns"] is not None
                else None
            ),
            error_json=str(row["error_json"]),
        )

    @staticmethod
    def _backup_from_row(row: sqlite3.Row) -> MigrationBackupRecord:
        return MigrationBackupRecord(
            transaction_id=str(row["transaction_id"]),
            adapter_id=str(row["adapter_id"]),
            backup_id=str(row["backup_id"]),
            source_path=str(row["source_path"]),
            backup_path=str(row["backup_path"]),
            source_kind=str(row["source_kind"]),
            size_bytes=int(row["size_bytes"]),
            checksum=str(row["checksum"]),
            created_at_ns=int(row["created_at_ns"]),
            restored_at_ns=(
                int(row["restored_at_ns"])
                if row["restored_at_ns"] is not None
                else None
            ),
        )


def _canonical_json(value: Mapping[str, Any] | None) -> str:
    if value is None:
        return ""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _lease_owner_alive(owner_id: str) -> bool:
    """Conservatively identify a product process that still owns its lease."""

    if not owner_id.startswith("pid:"):
        # Old/third-party owner formats cannot be proven dead before expiry.
        return True
    parts = owner_id.split(":", 2)
    if len(parts) != 3:
        return True
    try:
        pid = int(parts[1])
    except ValueError:
        return True
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def checksum_path(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            digest.update(block)
    return size, "sha256:" + digest.hexdigest()
