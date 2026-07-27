from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .errors import (
    InstallationFailure,
    MigrationFailure,
    TransactionConflict,
)
from .integrity import (
    ArchiveInspector,
    ChecksumVerifier,
    normalize_relative_path,
    resolve_below,
    sha256_file,
    stable_digest,
)
from .models import InstallReceipt, InstallState, OperationRecord


INSTALL_SCHEMA_VERSION = 1
_TERMINAL_STATES = {
    InstallState.COMMITTED,
    InstallState.ROLLED_BACK,
    InstallState.FAILED,
    InstallState.UNINSTALLED,
}
_TRANSITIONS = {
    InstallState.NEW: {InstallState.VERIFYING, InstallState.FAILED},
    InstallState.VERIFYING: {InstallState.STAGING, InstallState.FAILED},
    InstallState.STAGING: {
        InstallState.MIGRATING,
        InstallState.DOCTORING,
        InstallState.ROLLING_BACK,
        InstallState.FAILED,
    },
    InstallState.MIGRATING: {
        InstallState.DOCTORING,
        InstallState.ROLLING_BACK,
        InstallState.FAILED,
    },
    InstallState.DOCTORING: {
        InstallState.ACTIVATING,
        InstallState.ROLLING_BACK,
        InstallState.FAILED,
    },
    InstallState.ACTIVATING: {
        InstallState.COMMITTED,
        InstallState.ROLLING_BACK,
        InstallState.FAILED,
    },
    InstallState.COMMITTED: {
        InstallState.COMMITTED,
        InstallState.ROLLING_BACK,
        InstallState.UNINSTALLED,
    },
    InstallState.ROLLING_BACK: {
        InstallState.ROLLED_BACK,
        InstallState.FAILED,
    },
    InstallState.ROLLED_BACK: set(),
    InstallState.FAILED: set(),
    InstallState.UNINSTALLED: set(),
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class InstallReceiptStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._migrate_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "InstallReceiptStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextlib.contextmanager
    def transaction(self) -> Any:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def _migrate_schema(self) -> None:
        with self._lock:
            connection = self._connection
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS release_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS install_transactions (
                    transaction_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_digest TEXT NOT NULL,
                    release_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    install_root TEXT NOT NULL,
                    active_path TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    migration_version INTEGER NOT NULL,
                    owned_paths_json TEXT NOT NULL,
                    operations_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error_json TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_install_release
                    ON install_transactions(release_id, updated_at);
                CREATE TABLE IF NOT EXISTS install_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    transaction_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(transaction_id)
                        REFERENCES install_transactions(transaction_id)
                );
                CREATE INDEX IF NOT EXISTS ix_install_events_transaction
                    ON install_events(transaction_id, sequence);
                CREATE TABLE IF NOT EXISTS install_leases (
                    install_root TEXT PRIMARY KEY,
                    transaction_id TEXT NOT NULL,
                    token TEXT NOT NULL,
                    acquired_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    FOREIGN KEY(transaction_id)
                        REFERENCES install_transactions(transaction_id)
                );
                CREATE TABLE IF NOT EXISTS migration_journal (
                    transaction_id TEXT NOT NULL,
                    migration_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    checksum TEXT NOT NULL,
                    state TEXT NOT NULL,
                    rollback_payload_json TEXT NOT NULL,
                    applied_at TEXT NOT NULL,
                    PRIMARY KEY(transaction_id, migration_id),
                    FOREIGN KEY(transaction_id)
                        REFERENCES install_transactions(transaction_id)
                );
                """
            )
            row = connection.execute(
                "SELECT value FROM release_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO release_metadata(key, value) VALUES (?, ?)",
                    ("schema_version", str(INSTALL_SCHEMA_VERSION)),
                )
            elif int(row["value"]) != INSTALL_SCHEMA_VERSION:
                raise TransactionConflict(
                    "Install store schema version is unsupported.",
                    code="install_store_schema_mismatch",
                    details={
                        "expected": INSTALL_SCHEMA_VERSION,
                        "actual": int(row["value"]),
                    },
                )

    def create(
        self,
        *,
        idempotency_key: str,
        request_digest: str,
        release_id: str,
        install_root: Path,
        active_path: Path,
        manifest_digest: str,
    ) -> tuple[InstallReceipt, bool]:
        if not idempotency_key.strip():
            raise TransactionConflict(
                "Install idempotency key is required.",
                code="install_idempotency_key_missing",
            )
        with self.transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM install_transactions
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise TransactionConflict(
                        "Install idempotency key was reused with different inputs.",
                        code="install_idempotency_conflict",
                        details={
                            "idempotency_key": idempotency_key,
                            "transaction_id": existing["transaction_id"],
                        },
                    )
                return self._row_to_receipt(existing), False
            transaction_id = f"install-{uuid.uuid4().hex}"
            now = utc_now()
            connection.execute(
                """
                INSERT INTO install_transactions(
                    transaction_id, idempotency_key, request_digest,
                    release_id, state, revision, install_root, active_path,
                    manifest_digest, migration_version, owned_paths_json,
                    operations_json, created_at, updated_at, error_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transaction_id,
                    idempotency_key,
                    request_digest,
                    release_id,
                    InstallState.NEW.value,
                    1,
                    str(install_root.resolve()),
                    str(active_path.resolve()),
                    manifest_digest,
                    0,
                    "[]",
                    "[]",
                    now,
                    now,
                    None,
                ),
            )
            self._append_event(
                connection,
                transaction_id=transaction_id,
                revision=1,
                kind="install.created",
                state=InstallState.NEW,
                payload={
                    "request_digest": request_digest,
                    "release_id": release_id,
                    "manifest_digest": manifest_digest,
                },
            )
            row = connection.execute(
                "SELECT * FROM install_transactions WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
            assert row is not None
            return self._row_to_receipt(row), True

    def get(self, transaction_id: str) -> InstallReceipt:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM install_transactions WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
        if row is None:
            raise TransactionConflict(
                "Install transaction does not exist.",
                code="install_transaction_missing",
                details={"transaction_id": transaction_id},
            )
        return self._row_to_receipt(row)

    def by_idempotency_key(self, key: str) -> InstallReceipt | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM install_transactions WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        return self._row_to_receipt(row) if row is not None else None

    def latest_committed(self, install_root: Path) -> InstallReceipt | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM install_transactions
                WHERE install_root = ? AND state = ?
                ORDER BY updated_at DESC, revision DESC
                LIMIT 1
                """,
                (str(install_root.resolve()), InstallState.COMMITTED.value),
            ).fetchone()
        return self._row_to_receipt(row) if row is not None else None

    def transition(
        self,
        transaction_id: str,
        *,
        expected_revision: int,
        target: InstallState,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        owned_paths: Sequence[str] | None = None,
        operations: Sequence[OperationRecord] | None = None,
        migration_version: int | None = None,
        active_path: Path | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> InstallReceipt:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM install_transactions WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
            if row is None:
                raise TransactionConflict(
                    "Install transaction does not exist.",
                    code="install_transaction_missing",
                    details={"transaction_id": transaction_id},
                )
            current = self._row_to_receipt(row)
            if current.revision != expected_revision:
                raise TransactionConflict(
                    "Install transaction revision is stale.",
                    code="install_revision_conflict",
                    details={
                        "transaction_id": transaction_id,
                        "expected": expected_revision,
                        "actual": current.revision,
                    },
                )
            allowed = _TRANSITIONS[current.state]
            if target not in allowed:
                raise TransactionConflict(
                    "Install state transition is invalid.",
                    code="install_transition_invalid",
                    details={
                        "transaction_id": transaction_id,
                        "source": current.state.value,
                        "target": target.value,
                        "allowed": sorted(item.value for item in allowed),
                    },
                )
            next_revision = current.revision + 1
            now = utc_now()
            next_owned = tuple(owned_paths) if owned_paths is not None else current.owned_paths
            next_operations = (
                tuple(operations) if operations is not None else current.operations
            )
            next_migration = (
                migration_version
                if migration_version is not None
                else current.migration_version
            )
            next_active = (
                str(active_path.resolve())
                if active_path is not None
                else current.active_path
            )
            connection.execute(
                """
                UPDATE install_transactions
                SET state = ?, revision = ?, active_path = ?,
                    migration_version = ?, owned_paths_json = ?,
                    operations_json = ?, updated_at = ?, error_json = ?
                WHERE transaction_id = ? AND revision = ?
                """,
                (
                    target.value,
                    next_revision,
                    next_active,
                    next_migration,
                    json.dumps(list(next_owned), ensure_ascii=False),
                    json.dumps(
                        [item.to_dict() for item in next_operations],
                        ensure_ascii=False,
                    ),
                    now,
                    (
                        json.dumps(dict(error), ensure_ascii=False)
                        if error is not None
                        else None
                    ),
                    transaction_id,
                    expected_revision,
                ),
            )
            if connection.total_changes < 1:
                raise TransactionConflict(
                    "Install transaction update lost its revision race.",
                    code="install_revision_race",
                    details={"transaction_id": transaction_id},
                )
            self._append_event(
                connection,
                transaction_id=transaction_id,
                revision=next_revision,
                kind=kind,
                state=target,
                payload=dict(payload or {}),
            )
            updated = connection.execute(
                "SELECT * FROM install_transactions WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
            assert updated is not None
            return self._row_to_receipt(updated)

    def acquire_lease(
        self,
        transaction_id: str,
        install_root: Path,
        *,
        ttl_seconds: float = 300.0,
    ) -> str:
        now = time.time()
        token = uuid.uuid4().hex
        root = str(install_root.resolve())
        with self.transaction() as connection:
            transaction = connection.execute(
                "SELECT state FROM install_transactions WHERE transaction_id = ?",
                (transaction_id,),
            ).fetchone()
            if transaction is None:
                raise TransactionConflict(
                    "Cannot lease a missing install transaction.",
                    code="install_lease_transaction_missing",
                    details={"transaction_id": transaction_id},
                )
            existing = connection.execute(
                "SELECT * FROM install_leases WHERE install_root = ?",
                (root,),
            ).fetchone()
            if existing is not None and float(existing["expires_at"]) > now:
                if existing["transaction_id"] != transaction_id:
                    raise TransactionConflict(
                        "Install root is leased by another transaction.",
                        code="install_root_leased",
                        retryable=True,
                        details={
                            "install_root": root,
                            "holder": existing["transaction_id"],
                            "expires_at": existing["expires_at"],
                        },
                    )
                return str(existing["token"])
            connection.execute(
                """
                INSERT INTO install_leases(
                    install_root, transaction_id, token, acquired_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(install_root) DO UPDATE SET
                    transaction_id = excluded.transaction_id,
                    token = excluded.token,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at
                """,
                (root, transaction_id, token, now, now + ttl_seconds),
            )
        return token

    def renew_lease(
        self,
        install_root: Path,
        *,
        transaction_id: str,
        token: str,
        ttl_seconds: float = 300.0,
    ) -> None:
        root = str(install_root.resolve())
        now = time.time()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE install_leases
                SET expires_at = ?
                WHERE install_root = ? AND transaction_id = ? AND token = ?
                    AND expires_at >= ?
                """,
                (now + ttl_seconds, root, transaction_id, token, now),
            )
            if cursor.rowcount != 1:
                raise TransactionConflict(
                    "Install lease cannot be renewed.",
                    code="install_lease_lost",
                    details={
                        "install_root": root,
                        "transaction_id": transaction_id,
                    },
                )

    def release_lease(
        self,
        install_root: Path,
        *,
        transaction_id: str,
        token: str,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                DELETE FROM install_leases
                WHERE install_root = ? AND transaction_id = ? AND token = ?
                """,
                (str(install_root.resolve()), transaction_id, token),
            )

    def record_migration(
        self,
        *,
        transaction_id: str,
        migration_id: str,
        version: int,
        checksum: str,
        state: str,
        rollback_payload: Mapping[str, Any],
    ) -> None:
        with self.transaction() as connection:
            prior = connection.execute(
                """
                SELECT * FROM migration_journal
                WHERE transaction_id = ? AND migration_id = ?
                """,
                (transaction_id, migration_id),
            ).fetchone()
            if prior is not None and (
                prior["checksum"] != checksum
                or int(prior["version"]) != version
            ):
                raise MigrationFailure(
                    "Migration identity was reused with different content.",
                    code="migration_identity_conflict",
                    details={
                        "transaction_id": transaction_id,
                        "migration_id": migration_id,
                    },
                )
            connection.execute(
                """
                INSERT INTO migration_journal(
                    transaction_id, migration_id, version, checksum,
                    state, rollback_payload_json, applied_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(transaction_id, migration_id) DO UPDATE SET
                    state = excluded.state,
                    rollback_payload_json = excluded.rollback_payload_json,
                    applied_at = excluded.applied_at
                """,
                (
                    transaction_id,
                    migration_id,
                    version,
                    checksum,
                    state,
                    json.dumps(dict(rollback_payload), ensure_ascii=False),
                    utc_now(),
                ),
            )

    def migration_journal(self, transaction_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM migration_journal
                WHERE transaction_id = ?
                ORDER BY version, migration_id
                """,
                (transaction_id,),
            ).fetchall()
        return [
            {
                "migration_id": row["migration_id"],
                "version": int(row["version"]),
                "checksum": row["checksum"],
                "state": row["state"],
                "rollback_payload": json.loads(row["rollback_payload_json"]),
                "applied_at": row["applied_at"],
            }
            for row in rows
        ]

    def events(self, transaction_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM install_events
                WHERE transaction_id = ?
                ORDER BY sequence
                """,
                (transaction_id,),
            ).fetchall()
        return [
            {
                "sequence": int(row["sequence"]),
                "transaction_id": row["transaction_id"],
                "revision": int(row["revision"]),
                "kind": row["kind"],
                "state": row["state"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        transaction_id: str,
        revision: int,
        kind: str,
        state: InstallState,
        payload: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO install_events(
                transaction_id, revision, kind, state, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                transaction_id,
                revision,
                kind,
                state.value,
                json.dumps(dict(payload), ensure_ascii=False, sort_keys=True),
                utc_now(),
            ),
        )

    @staticmethod
    def _row_to_receipt(row: sqlite3.Row) -> InstallReceipt:
        operations_raw = json.loads(row["operations_json"])
        operations = tuple(
            OperationRecord(
                sequence=int(item["sequence"]),
                kind=str(item["kind"]),
                target=str(item["target"]),
                before_digest=str(item["before_digest"]),
                after_digest=str(item["after_digest"]),
                reversible=bool(item["reversible"]),
                status=str(item["status"]),
                attributes=dict(item.get("attributes") or {}),
            )
            for item in operations_raw
        )
        return InstallReceipt(
            transaction_id=row["transaction_id"],
            idempotency_key=row["idempotency_key"],
            release_id=row["release_id"],
            state=InstallState(row["state"]),
            revision=int(row["revision"]),
            install_root=row["install_root"],
            active_path=row["active_path"],
            manifest_digest=row["manifest_digest"],
            migration_version=int(row["migration_version"]),
            owned_paths=tuple(json.loads(row["owned_paths_json"])),
            operations=operations,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            error=json.loads(row["error_json"]) if row["error_json"] else None,
        )


class MigrationContext(Protocol):
    transaction_id: str
    release_root: Path
    state_root: Path


class MigrationApply(Protocol):
    def __call__(self, context: MigrationContext) -> Mapping[str, Any]: ...


class MigrationRollback(Protocol):
    def __call__(
        self,
        context: MigrationContext,
        payload: Mapping[str, Any],
    ) -> None: ...


class MigrationVerify(Protocol):
    def __call__(self, context: MigrationContext) -> Mapping[str, Any]: ...


class MigrationStep:
    def __init__(
        self,
        *,
        migration_id: str,
        version: int,
        checksum: str,
        apply: MigrationApply,
        rollback: MigrationRollback,
        verify: MigrationVerify,
        dependencies: Sequence[str] = (),
        reversible: bool = True,
    ) -> None:
        if not migration_id or any(character.isspace() for character in migration_id):
            raise MigrationFailure(
                "Migration id is invalid.",
                code="migration_id_invalid",
                details={"migration_id": migration_id},
            )
        if version < 1:
            raise MigrationFailure(
                "Migration version must be positive.",
                code="migration_version_invalid",
                details={"migration_id": migration_id, "version": version},
            )
        if not checksum:
            raise MigrationFailure(
                "Migration checksum is required.",
                code="migration_checksum_missing",
                details={"migration_id": migration_id},
            )
        self.migration_id = migration_id
        self.version = version
        self.checksum = checksum
        self.apply = apply
        self.rollback = rollback
        self.verify = verify
        self.dependencies = tuple(dependencies)
        self.reversible = reversible


class MigrationRegistry:
    def __init__(self, steps: Iterable[MigrationStep] = ()) -> None:
        self._steps: dict[str, MigrationStep] = {}
        for step in steps:
            self.register(step)

    def register(self, step: MigrationStep) -> None:
        prior = self._steps.get(step.migration_id)
        if prior is not None:
            raise MigrationFailure(
                "Migration id is registered more than once.",
                code="migration_duplicate",
                details={"migration_id": step.migration_id},
            )
        if any(existing.version == step.version for existing in self._steps.values()):
            raise MigrationFailure(
                "Migration version is registered more than once.",
                code="migration_version_duplicate",
                details={"version": step.version},
            )
        self._steps[step.migration_id] = step

    def plan(
        self,
        *,
        current_version: int,
        target_version: int,
    ) -> tuple[MigrationStep, ...]:
        if target_version < current_version:
            raise MigrationFailure(
                "Forward migration target is below current version.",
                code="migration_target_regression",
                details={
                    "current_version": current_version,
                    "target_version": target_version,
                },
            )
        selected = {
            identifier: step
            for identifier, step in self._steps.items()
            if current_version < step.version <= target_version
        }
        for step in selected.values():
            missing = [
                dependency
                for dependency in step.dependencies
                if dependency not in self._steps
            ]
            if missing:
                raise MigrationFailure(
                    "Migration dependency is not registered.",
                    code="migration_dependency_missing",
                    details={
                        "migration_id": step.migration_id,
                        "missing": missing,
                    },
                )
        ordered: list[MigrationStep] = []
        temporary: set[str] = set()
        permanent: set[str] = set()

        def visit(identifier: str) -> None:
            if identifier in permanent:
                return
            if identifier in temporary:
                raise MigrationFailure(
                    "Migration dependency graph contains a cycle.",
                    code="migration_dependency_cycle",
                    details={"migration_id": identifier},
                )
            temporary.add(identifier)
            step = self._steps[identifier]
            for dependency in step.dependencies:
                dependency_step = self._steps[dependency]
                if dependency_step.version > current_version:
                    visit(dependency)
            temporary.remove(identifier)
            permanent.add(identifier)
            if step.version > current_version and step.version <= target_version:
                ordered.append(step)

        for identifier in sorted(
            selected,
            key=lambda item: (selected[item].version, item),
        ):
            visit(identifier)
        versions = [step.version for step in ordered]
        if versions != sorted(versions):
            raise MigrationFailure(
                "Migration dependencies violate version order.",
                code="migration_version_order",
                details={"versions": versions},
            )
        return tuple(ordered)

    def rollback_plan(
        self,
        *,
        current_version: int,
        target_version: int,
    ) -> tuple[MigrationStep, ...]:
        if target_version > current_version:
            raise MigrationFailure(
                "Rollback target exceeds current version.",
                code="rollback_target_forward",
            )
        selected = [
            step
            for step in self._steps.values()
            if target_version < step.version <= current_version
        ]
        selected.sort(key=lambda step: (step.version, step.migration_id), reverse=True)
        irreversible = [step.migration_id for step in selected if not step.reversible]
        if irreversible:
            raise MigrationFailure(
                "Rollback crosses irreversible migrations.",
                code="migration_irreversible",
                details={"migrations": irreversible},
            )
        return tuple(selected)


class MigrationExecutor:
    def __init__(
        self,
        registry: MigrationRegistry,
        store: InstallReceiptStore,
    ) -> None:
        self.registry = registry
        self.store = store

    def apply(
        self,
        context: MigrationContext,
        *,
        current_version: int,
        target_version: int,
    ) -> dict[str, Any]:
        plan = self.registry.plan(
            current_version=current_version,
            target_version=target_version,
        )
        applied: list[dict[str, Any]] = []
        try:
            for step in plan:
                payload = dict(step.apply(context))
                verification = dict(step.verify(context))
                if verification.get("ready") is not True:
                    raise MigrationFailure(
                        "Migration verification did not report ready.",
                        code="migration_verification_failed",
                        details={
                            "migration_id": step.migration_id,
                            "verification": verification,
                        },
                    )
                self.store.record_migration(
                    transaction_id=context.transaction_id,
                    migration_id=step.migration_id,
                    version=step.version,
                    checksum=step.checksum,
                    state="applied",
                    rollback_payload=payload,
                )
                applied.append(
                    {
                        "migration_id": step.migration_id,
                        "version": step.version,
                        "checksum": step.checksum,
                        "rollback_payload": payload,
                        "verification": verification,
                    }
                )
        except BaseException as error:
            rollback_errors = self._rollback_applied(context, applied)
            if isinstance(error, MigrationFailure):
                error.details["rollback_errors"] = rollback_errors
                raise
            raise MigrationFailure(
                "Migration application failed.",
                code="migration_apply_failed",
                details={
                    "error": str(error),
                    "type": type(error).__name__,
                    "rollback_errors": rollback_errors,
                },
            ) from error
        return {
            "schema": "zyra.migration-apply-receipt/v1",
            "ready": True,
            "transaction_id": context.transaction_id,
            "from_version": current_version,
            "to_version": target_version,
            "applied": applied,
            "plan_digest": stable_digest(
                [
                    {
                        "id": step.migration_id,
                        "version": step.version,
                        "checksum": step.checksum,
                    }
                    for step in plan
                ]
            ),
        }

    def rollback(
        self,
        context: MigrationContext,
        *,
        current_version: int,
        target_version: int,
    ) -> dict[str, Any]:
        plan = self.registry.rollback_plan(
            current_version=current_version,
            target_version=target_version,
        )
        journal = {
            item["migration_id"]: item
            for item in self.store.migration_journal(context.transaction_id)
        }
        rolled_back: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for step in plan:
            record = journal.get(step.migration_id)
            if record is None or record["state"] != "applied":
                continue
            try:
                step.rollback(context, record["rollback_payload"])
                self.store.record_migration(
                    transaction_id=context.transaction_id,
                    migration_id=step.migration_id,
                    version=step.version,
                    checksum=step.checksum,
                    state="rolled_back",
                    rollback_payload=record["rollback_payload"],
                )
                rolled_back.append(
                    {
                        "migration_id": step.migration_id,
                        "version": step.version,
                    }
                )
            except BaseException as error:
                errors.append(
                    {
                        "migration_id": step.migration_id,
                        "error": str(error),
                        "type": type(error).__name__,
                    }
                )
        if errors:
            raise MigrationFailure(
                "Migration rollback was incomplete.",
                code="migration_rollback_failed",
                details={"errors": errors, "rolled_back": rolled_back},
            )
        return {
            "schema": "zyra.migration-rollback-receipt/v1",
            "ready": True,
            "transaction_id": context.transaction_id,
            "from_version": current_version,
            "to_version": target_version,
            "rolled_back": rolled_back,
        }

    def _rollback_applied(
        self,
        context: MigrationContext,
        applied: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, str]]:
        errors: list[dict[str, str]] = []
        by_id = self.registry._steps
        for record in reversed(applied):
            identifier = str(record["migration_id"])
            step = by_id[identifier]
            try:
                step.rollback(context, dict(record["rollback_payload"]))
                self.store.record_migration(
                    transaction_id=context.transaction_id,
                    migration_id=identifier,
                    version=step.version,
                    checksum=step.checksum,
                    state="rolled_back",
                    rollback_payload=dict(record["rollback_payload"]),
                )
            except BaseException as error:
                errors.append(
                    {
                        "migration_id": identifier,
                        "error": str(error),
                        "type": type(error).__name__,
                    }
                )
        return errors


class _InstallContext:
    def __init__(
        self,
        *,
        transaction_id: str,
        release_root: Path,
        state_root: Path,
    ) -> None:
        self.transaction_id = transaction_id
        self.release_root = release_root
        self.state_root = state_root


class ReleaseInstaller:
    def __init__(
        self,
        store: InstallReceiptStore,
        *,
        migrations: MigrationExecutor | None = None,
        doctor: Callable[[Path], Mapping[str, Any]] | None = None,
    ) -> None:
        self.store = store
        self.migrations = migrations
        self.doctor = doctor or self._default_doctor

    def install(
        self,
        archive: Path,
        *,
        install_root: Path,
        idempotency_key: str,
        release_id: str,
        manifest_digest: str,
        checksum_manifest: Mapping[str, Any] | None = None,
        target_migration_version: int = 0,
    ) -> InstallReceipt:
        archive = archive.resolve()
        install_root = install_root.resolve()
        active_path = install_root / "current"
        request_digest = stable_digest(
            {
                "archive": str(archive),
                "archive_sha256": sha256_file(archive) if archive.is_file() else "",
                "install_root": str(install_root),
                "release_id": release_id,
                "manifest_digest": manifest_digest,
                "target_migration_version": target_migration_version,
            }
        )
        receipt, created = self.store.create(
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            release_id=release_id,
            install_root=install_root,
            active_path=active_path,
            manifest_digest=manifest_digest,
        )
        if not created:
            if receipt.state is InstallState.COMMITTED:
                return receipt
            if receipt.state not in _TERMINAL_STATES:
                raise TransactionConflict(
                    "Existing install transaction is still in progress.",
                    code="install_idempotent_in_progress",
                    retryable=True,
                    details={
                        "transaction_id": receipt.transaction_id,
                        "state": receipt.state.value,
                    },
                )
            raise TransactionConflict(
                "Existing install transaction reached a non-success terminal state.",
                code="install_idempotent_failed",
                details={
                    "transaction_id": receipt.transaction_id,
                    "state": receipt.state.value,
                },
            )
        token = self.store.acquire_lease(
            receipt.transaction_id,
            install_root,
        )
        stage = install_root / ".staging" / receipt.transaction_id
        prior_active = self._read_active_target(active_path)
        operations: list[OperationRecord] = []
        try:
            receipt = self.store.transition(
                receipt.transaction_id,
                expected_revision=receipt.revision,
                target=InstallState.VERIFYING,
                kind="install.verifying",
                payload={"archive": str(archive)},
            )
            inspection = ArchiveInspector(archive).inspect()
            if inspection["archive_sha256"] == "":
                raise InstallationFailure(
                    "Archive digest is empty.",
                    code="install_archive_digest_empty",
                )
            receipt = self.store.transition(
                receipt.transaction_id,
                expected_revision=receipt.revision,
                target=InstallState.STAGING,
                kind="install.staging",
                payload={"archive_inspection": inspection},
            )
            if stage.exists():
                self._remove_owned_tree(stage, install_root)
            stage.mkdir(parents=True, exist_ok=False)
            ArchiveInspector(archive).safe_extract(stage)
            release_root = self._single_release_root(stage)
            if checksum_manifest is not None:
                ChecksumVerifier(release_root).verify(
                    checksum_manifest,
                    reject_extra=True,
                )
            operations.append(
                OperationRecord(
                    sequence=1,
                    kind="stage.extract",
                    target=str(release_root),
                    before_digest="",
                    after_digest=self._tree_digest(release_root),
                    reversible=True,
                    status="completed",
                    attributes={"archive": str(archive)},
                )
            )
            migration_version = receipt.migration_version
            context = _InstallContext(
                transaction_id=receipt.transaction_id,
                release_root=release_root,
                state_root=install_root / "state",
            )
            if self.migrations is not None and target_migration_version > migration_version:
                receipt = self.store.transition(
                    receipt.transaction_id,
                    expected_revision=receipt.revision,
                    target=InstallState.MIGRATING,
                    kind="install.migrating",
                    operations=operations,
                    payload={
                        "from_version": migration_version,
                        "to_version": target_migration_version,
                    },
                )
                migration_receipt = self.migrations.apply(
                    context,
                    current_version=migration_version,
                    target_version=target_migration_version,
                )
                operations.append(
                    OperationRecord(
                        sequence=len(operations) + 1,
                        kind="schema.migrate",
                        target=str(context.state_root),
                        before_digest=str(migration_version),
                        after_digest=str(target_migration_version),
                        reversible=True,
                        status="completed",
                        attributes={"receipt": migration_receipt},
                    )
                )
                migration_version = target_migration_version
            receipt = self.store.transition(
                receipt.transaction_id,
                expected_revision=receipt.revision,
                target=InstallState.DOCTORING,
                kind="install.doctoring",
                operations=operations,
                migration_version=migration_version,
            )
            doctor_report = dict(self.doctor(release_root))
            if doctor_report.get("ready") is not True:
                raise InstallationFailure(
                    "Staged release doctor did not report ready.",
                    code="install_doctor_failed",
                    details={"doctor": doctor_report},
                )
            operations.append(
                OperationRecord(
                    sequence=len(operations) + 1,
                    kind="stage.doctor",
                    target=str(release_root),
                    before_digest="",
                    after_digest=stable_digest(doctor_report),
                    reversible=False,
                    status="completed",
                    attributes={"ready": True},
                )
            )
            receipt = self.store.transition(
                receipt.transaction_id,
                expected_revision=receipt.revision,
                target=InstallState.ACTIVATING,
                kind="install.activating",
                operations=operations,
            )
            release_destination = install_root / "releases" / release_id
            release_destination.parent.mkdir(parents=True, exist_ok=True)
            if release_destination.exists():
                existing_digest = self._tree_digest(release_destination)
                staged_digest = self._tree_digest(release_root)
                if existing_digest != staged_digest:
                    raise InstallationFailure(
                        "Release id already exists with different content.",
                        code="install_release_id_collision",
                        details={
                            "release_id": release_id,
                            "existing_digest": existing_digest,
                            "staged_digest": staged_digest,
                        },
                    )
                self._remove_owned_tree(stage, install_root)
            else:
                os.replace(release_root, release_destination)
                self._remove_empty_stage(stage)
            self._write_active_target(active_path, release_destination)
            operations.append(
                OperationRecord(
                    sequence=len(operations) + 1,
                    kind="release.activate",
                    target=str(active_path),
                    before_digest=prior_active,
                    after_digest=str(release_destination),
                    reversible=True,
                    status="completed",
                    attributes={"release_id": release_id},
                )
            )
            owned_paths = self._owned_paths(install_root, release_destination, active_path)
            receipt = self.store.transition(
                receipt.transaction_id,
                expected_revision=receipt.revision,
                target=InstallState.COMMITTED,
                kind="install.committed",
                operations=operations,
                owned_paths=owned_paths,
                migration_version=migration_version,
                active_path=active_path,
                payload={
                    "doctor_digest": stable_digest(doctor_report),
                    "release_path": str(release_destination),
                },
            )
            return receipt
        except BaseException as error:
            self._rollback_failed_install(
                receipt,
                install_root=install_root,
                stage=stage,
                active_path=active_path,
                prior_active=prior_active,
                operations=operations,
                target_migration_version=target_migration_version,
                error=error,
            )
            if isinstance(
                error,
                (InstallationFailure, MigrationFailure, TransactionConflict),
            ):
                raise
            raise InstallationFailure(
                "Release installation failed.",
                code="install_unexpected_failure",
                details={"type": type(error).__name__, "error": str(error)},
            ) from error
        finally:
            self.store.release_lease(
                install_root,
                transaction_id=receipt.transaction_id,
                token=token,
            )

    def uninstall(
        self,
        transaction_id: str,
        *,
        purge_state: bool = False,
    ) -> InstallReceipt:
        receipt = self.store.get(transaction_id)
        if receipt.state is not InstallState.COMMITTED:
            raise InstallationFailure(
                "Only a committed installation can be uninstalled.",
                code="uninstall_state_invalid",
                details={"state": receipt.state.value},
            )
        install_root = Path(receipt.install_root).resolve()
        token = self.store.acquire_lease(transaction_id, install_root)
        try:
            active_path = Path(receipt.active_path)
            active_target = self._read_active_target(active_path)
            release_paths = [
                resolve_below(install_root, path)
                for path in receipt.owned_paths
                if path.startswith("releases/")
            ]
            for target in sorted(release_paths, key=lambda path: len(path.parts), reverse=True):
                if target.exists():
                    self._remove_owned_tree(target, install_root)
            if active_target and any(
                active_target == str(path) for path in release_paths
            ):
                active_path.unlink(missing_ok=True)
            removed_state: list[str] = []
            if purge_state:
                for relative in ("state", "operator-data"):
                    target = resolve_below(install_root, relative)
                    if target.exists():
                        self._remove_owned_tree(target, install_root)
                        removed_state.append(relative)
            updated = self.store.transition(
                transaction_id,
                expected_revision=receipt.revision,
                target=InstallState.UNINSTALLED,
                kind="install.uninstalled",
                payload={
                    "purge_state": purge_state,
                    "removed_state": removed_state,
                },
            )
            return updated
        finally:
            self.store.release_lease(
                install_root,
                transaction_id=transaction_id,
                token=token,
            )

    def rollback_committed(
        self,
        transaction_id: str,
        *,
        target_migration_version: int = 0,
    ) -> InstallReceipt:
        receipt = self.store.get(transaction_id)
        if receipt.state is not InstallState.COMMITTED:
            raise InstallationFailure(
                "Only a committed installation can be rolled back.",
                code="rollback_state_invalid",
                details={"state": receipt.state.value},
            )
        if target_migration_version < 0:
            raise InstallationFailure(
                "Rollback migration target cannot be negative.",
                code="rollback_migration_target_invalid",
                details={"target": target_migration_version},
            )
        if target_migration_version > receipt.migration_version:
            raise InstallationFailure(
                "Rollback migration target is newer than the installed schema.",
                code="rollback_migration_target_forward",
                details={
                    "installed": receipt.migration_version,
                    "target": target_migration_version,
                },
            )
        install_root = Path(receipt.install_root).resolve()
        active_path = Path(receipt.active_path).resolve()
        token = self.store.acquire_lease(transaction_id, install_root)
        try:
            activation = next(
                (
                    item
                    for item in reversed(receipt.operations)
                    if item.kind == "release.activate"
                ),
                None,
            )
            if activation is None:
                raise InstallationFailure(
                    "Committed installation has no activation journal entry.",
                    code="rollback_activation_journal_missing",
                    details={"transaction_id": transaction_id},
                )
            release_path = Path(activation.after_digest).resolve()
            try:
                release_path.relative_to(install_root)
            except ValueError as error:
                raise InstallationFailure(
                    "Rollback release path escapes the install root.",
                    code="rollback_release_path_unsafe",
                    details={"path": str(release_path)},
                ) from error
            prior_active = activation.before_digest
            if prior_active:
                prior_path = Path(prior_active).resolve()
                try:
                    prior_path.relative_to(install_root)
                except ValueError as error:
                    raise InstallationFailure(
                        "Rollback prior activation escapes the install root.",
                        code="rollback_prior_path_unsafe",
                        details={"path": str(prior_path)},
                    ) from error
                if not prior_path.is_dir():
                    raise InstallationFailure(
                        "Rollback prior release is unavailable.",
                        code="rollback_prior_release_missing",
                        details={"path": str(prior_path)},
                    )
            current_active = self._read_active_target(active_path)
            if current_active != str(release_path):
                raise TransactionConflict(
                    "Rollback target is no longer the active release.",
                    code="rollback_active_release_changed",
                    retryable=True,
                    details={
                        "expected": str(release_path),
                        "actual": current_active,
                    },
                )
            rolling = self.store.transition(
                transaction_id,
                expected_revision=receipt.revision,
                target=InstallState.ROLLING_BACK,
                kind="install.rollback_requested",
                payload={
                    "release_path": str(release_path),
                    "prior_active": prior_active,
                    "from_migration_version": receipt.migration_version,
                    "to_migration_version": target_migration_version,
                },
            )
            if (
                self.migrations is not None
                and rolling.migration_version > target_migration_version
            ):
                context = _InstallContext(
                    transaction_id=transaction_id,
                    release_root=release_path,
                    state_root=install_root / "state",
                )
                self.migrations.rollback(
                    context,
                    current_version=rolling.migration_version,
                    target_version=target_migration_version,
                )
            if prior_active:
                self._write_active_target(active_path, Path(prior_active))
            else:
                active_path.unlink(missing_ok=True)
            if release_path.exists():
                self._remove_owned_tree(release_path, install_root)
            rollback_operation = OperationRecord(
                sequence=len(rolling.operations) + 1,
                kind="release.rollback",
                target=str(active_path),
                before_digest=str(release_path),
                after_digest=prior_active,
                reversible=False,
                status="completed",
                attributes={
                    "migration_version": target_migration_version,
                },
            )
            return self.store.transition(
                transaction_id,
                expected_revision=rolling.revision,
                target=InstallState.ROLLED_BACK,
                kind="install.rolled_back",
                operations=(*rolling.operations, rollback_operation),
                migration_version=target_migration_version,
                payload={
                    "restored_active": prior_active,
                    "removed_release": str(release_path),
                },
            )
        except BaseException as error:
            current = self.store.get(transaction_id)
            if (
                current.state is InstallState.ROLLING_BACK
                and InstallState.FAILED in _TRANSITIONS[current.state]
            ):
                self.store.transition(
                    transaction_id,
                    expected_revision=current.revision,
                    target=InstallState.FAILED,
                    kind="install.rollback_failed",
                    error={
                        "type": type(error).__name__,
                        "message": str(error),
                    },
                )
            raise
        finally:
            self.store.release_lease(
                install_root,
                transaction_id=transaction_id,
                token=token,
            )

    def migrate_committed(
        self,
        transaction_id: str,
        *,
        target_migration_version: int,
    ) -> InstallReceipt:
        receipt = self.store.get(transaction_id)
        if receipt.state is not InstallState.COMMITTED:
            raise InstallationFailure(
                "Only a committed installation can be migrated.",
                code="migrate_state_invalid",
                details={"state": receipt.state.value},
            )
        if self.migrations is None:
            raise MigrationFailure(
                "No migration registry is configured.",
                code="migration_registry_unavailable",
            )
        if target_migration_version <= receipt.migration_version:
            raise MigrationFailure(
                "Migration target must be newer than the installed schema.",
                code="migration_target_not_forward",
                details={
                    "installed": receipt.migration_version,
                    "target": target_migration_version,
                },
            )
        install_root = Path(receipt.install_root).resolve()
        active_path = Path(receipt.active_path).resolve()
        token = self.store.acquire_lease(transaction_id, install_root)
        try:
            active = self._read_active_target(active_path)
            if not active:
                raise InstallationFailure(
                    "Committed installation has no active release.",
                    code="migration_active_release_missing",
                )
            active_release = Path(active).resolve()
            try:
                active_release.relative_to(install_root)
            except ValueError as error:
                raise InstallationFailure(
                    "Migration active release escapes the install root.",
                    code="migration_active_release_unsafe",
                    details={"path": str(active_release)},
                ) from error
            context = _InstallContext(
                transaction_id=transaction_id,
                release_root=active_release,
                state_root=install_root / "state",
            )
            migration_receipt = self.migrations.apply(
                context,
                current_version=receipt.migration_version,
                target_version=target_migration_version,
            )
            operation = OperationRecord(
                sequence=len(receipt.operations) + 1,
                kind="schema.migrate",
                target=str(context.state_root),
                before_digest=str(receipt.migration_version),
                after_digest=str(target_migration_version),
                reversible=True,
                status="completed",
                attributes={"receipt": migration_receipt},
            )
            return self.store.transition(
                transaction_id,
                expected_revision=receipt.revision,
                target=InstallState.COMMITTED,
                kind="install.migrated",
                operations=(*receipt.operations, operation),
                migration_version=target_migration_version,
                payload={
                    "from_version": receipt.migration_version,
                    "to_version": target_migration_version,
                    "migration_receipt": migration_receipt,
                },
            )
        finally:
            self.store.release_lease(
                install_root,
                transaction_id=transaction_id,
                token=token,
            )

    def _rollback_failed_install(
        self,
        receipt: InstallReceipt,
        *,
        install_root: Path,
        stage: Path,
        active_path: Path,
        prior_active: str,
        operations: list[OperationRecord],
        target_migration_version: int,
        error: BaseException,
    ) -> None:
        current = self.store.get(receipt.transaction_id)
        if current.state in _TERMINAL_STATES:
            return
        rollback_errors: list[dict[str, str]] = []
        try:
            if InstallState.ROLLING_BACK in _TRANSITIONS[current.state]:
                current = self.store.transition(
                    current.transaction_id,
                    expected_revision=current.revision,
                    target=InstallState.ROLLING_BACK,
                    kind="install.rolling_back",
                    operations=operations,
                    error={
                        "type": type(error).__name__,
                        "message": str(error),
                    },
                )
            if self.migrations is not None and current.migration_version > 0:
                context = _InstallContext(
                    transaction_id=current.transaction_id,
                    release_root=stage,
                    state_root=install_root / "state",
                )
                try:
                    self.migrations.rollback(
                        context,
                        current_version=max(
                            current.migration_version,
                            target_migration_version,
                        ),
                        target_version=0,
                    )
                except BaseException as rollback_error:
                    rollback_errors.append(
                        {
                            "kind": "migration",
                            "type": type(rollback_error).__name__,
                            "error": str(rollback_error),
                        }
                    )
            try:
                if stage.exists():
                    self._remove_owned_tree(stage, install_root)
            except BaseException as rollback_error:
                rollback_errors.append(
                    {
                        "kind": "stage",
                        "type": type(rollback_error).__name__,
                        "error": str(rollback_error),
                    }
                )
            try:
                if prior_active:
                    self._write_active_target(active_path, Path(prior_active))
                else:
                    active_path.unlink(missing_ok=True)
            except BaseException as rollback_error:
                rollback_errors.append(
                    {
                        "kind": "active-pointer",
                        "type": type(rollback_error).__name__,
                        "error": str(rollback_error),
                    }
                )
            current = self.store.get(current.transaction_id)
            target = (
                InstallState.ROLLED_BACK
                if not rollback_errors and InstallState.ROLLED_BACK in _TRANSITIONS[current.state]
                else InstallState.FAILED
            )
            self.store.transition(
                current.transaction_id,
                expected_revision=current.revision,
                target=target,
                kind=(
                    "install.rolled_back"
                    if target is InstallState.ROLLED_BACK
                    else "install.rollback_failed"
                ),
                operations=operations,
                error={
                    "type": type(error).__name__,
                    "message": str(error),
                    "rollback_errors": rollback_errors,
                },
            )
        except BaseException:
            current = self.store.get(receipt.transaction_id)
            if (
                current.state not in _TERMINAL_STATES
                and InstallState.FAILED in _TRANSITIONS[current.state]
            ):
                self.store.transition(
                    current.transaction_id,
                    expected_revision=current.revision,
                    target=InstallState.FAILED,
                    kind="install.failed",
                    error={
                        "type": type(error).__name__,
                        "message": str(error),
                        "rollback_errors": rollback_errors,
                    },
                )

    @staticmethod
    def _default_doctor(root: Path) -> Mapping[str, Any]:
        required = ("pyproject.toml", "package.json", "README.md")
        missing = [name for name in required if not (root / name).is_file()]
        return {
            "schema": "zyra.install-doctor/v1",
            "ready": not missing,
            "root": str(root),
            "missing": missing,
        }

    @staticmethod
    def _single_release_root(stage: Path) -> Path:
        current = stage
        for _ in range(3):
            entries = list(current.iterdir())
            if (current / "pyproject.toml").is_file():
                return current
            if len(entries) != 1 or not entries[0].is_dir():
                raise InstallationFailure(
                    "Staged archive does not contain exactly one release root.",
                    code="install_stage_root_invalid",
                    details={
                        "current": str(current),
                        "entries": [item.name for item in entries],
                    },
                )
            current = entries[0]
        if not (current / "pyproject.toml").is_file():
            raise InstallationFailure(
                "Staged archive payload does not contain pyproject.toml.",
                code="install_stage_project_missing",
                details={"current": str(current)},
            )
        return current

    @staticmethod
    def _read_active_target(path: Path) -> str:
        if not path.is_file():
            return ""
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            return ""
        return str(Path(value).resolve())

    @staticmethod
    def _write_active_target(path: Path, target: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        try:
            temporary.write_text(str(target.resolve()) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _tree_digest(root: Path) -> str:
        entries: list[dict[str, Any]] = []
        for path in sorted(
            (item for item in root.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(root).as_posix(),
        ):
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
        return stable_digest(entries)

    @staticmethod
    def _owned_paths(
        install_root: Path,
        release_path: Path,
        active_path: Path,
    ) -> tuple[str, ...]:
        owned = {
            normalize_relative_path(release_path.relative_to(install_root).as_posix()),
            normalize_relative_path(active_path.relative_to(install_root).as_posix()),
        }
        return tuple(sorted(owned))

    @staticmethod
    def _remove_empty_stage(stage: Path) -> None:
        current = stage
        for _ in range(2):
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent

    @staticmethod
    def _remove_owned_tree(target: Path, install_root: Path) -> None:
        root = install_root.resolve()
        resolved = target.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError as error:
            raise InstallationFailure(
                "Refusing to remove a path outside the install root.",
                code="install_cleanup_escape",
                details={"target": str(resolved), "root": str(root)},
            ) from error
        if not relative.parts or len(relative.parts) < 2:
            raise InstallationFailure(
                "Refusing broad install cleanup.",
                code="install_cleanup_too_broad",
                details={"target": str(resolved), "root": str(root)},
            )
        if resolved.is_symlink() or resolved.is_file():
            resolved.unlink(missing_ok=True)
        elif resolved.is_dir():
            shutil.rmtree(resolved)


def default_migration_registry() -> MigrationRegistry:
    def ensure_state_root(context: MigrationContext) -> Mapping[str, Any]:
        existed = context.state_root.exists()
        context.state_root.mkdir(parents=True, exist_ok=True)
        marker = context.state_root / "release-schema.json"
        prior = marker.read_text(encoding="utf-8") if marker.is_file() else ""
        marker.write_text(
            json.dumps(
                {
                    "schema": "zyra.release-state/v1",
                    "version": 1,
                    "transaction_id": context.transaction_id,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return {"existed": existed, "prior": prior, "marker": str(marker)}

    def rollback_state(
        context: MigrationContext,
        payload: Mapping[str, Any],
    ) -> None:
        marker = Path(str(payload["marker"]))
        prior = str(payload.get("prior") or "")
        if prior:
            marker.write_text(prior, encoding="utf-8")
        else:
            marker.unlink(missing_ok=True)
        if not bool(payload.get("existed")):
            with contextlib.suppress(OSError):
                context.state_root.rmdir()

    def verify_state(context: MigrationContext) -> Mapping[str, Any]:
        marker = context.state_root / "release-schema.json"
        if not marker.is_file():
            return {"ready": False, "reason": "marker_missing"}
        value = json.loads(marker.read_text(encoding="utf-8"))
        return {
            "ready": value.get("schema") == "zyra.release-state/v1"
            and value.get("version") == 1,
            "version": value.get("version"),
            "transaction_id": value.get("transaction_id"),
        }

    step = MigrationStep(
        migration_id="release-state-v1",
        version=1,
        checksum=stable_digest(
            {
                "migration": "release-state-v1",
                "behavior": "atomic marker with prior-content rollback",
            }
        ),
        apply=ensure_state_root,
        rollback=rollback_state,
        verify=verify_state,
    )
    return MigrationRegistry((step,))


__all__ = [
    "INSTALL_SCHEMA_VERSION",
    "InstallReceiptStore",
    "MigrationExecutor",
    "MigrationRegistry",
    "MigrationStep",
    "ReleaseInstaller",
    "default_migration_registry",
    "utc_now",
]
