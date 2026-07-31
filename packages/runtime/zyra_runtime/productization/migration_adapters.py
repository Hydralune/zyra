from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .migration_journal import MigrationBackupRecord, checksum_path


class MigrationAdapterError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        adapter_id: str,
        owner: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.adapter_id = adapter_id
        self.owner = owner
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class MigrationProbe:
    adapter_id: str
    owner: str
    path: str
    source_version: int
    target_version: int
    exists: bool
    migration_required: bool
    mutable: bool
    state_digest: str
    details: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.migration-probe/v1",
            "adapter_id": self.adapter_id,
            "owner": self.owner,
            "path": self.path,
            "source_version": self.source_version,
            "target_version": self.target_version,
            "exists": self.exists,
            "migration_required": self.migration_required,
            "mutable": self.mutable,
            "state_digest": self.state_digest,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class MigrationMutation:
    adapter_id: str
    owner: str
    source_version: int
    target_version: int
    before_digest: str
    after_digest: str
    changed: bool
    attributes: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.migration-mutation/v1",
            "adapter_id": self.adapter_id,
            "owner": self.owner,
            "source_version": self.source_version,
            "target_version": self.target_version,
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "changed": self.changed,
            "attributes": dict(self.attributes),
        }

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


class MigrationAdapter(Protocol):
    adapter_id: str
    owner: str
    target_version: int
    dependencies: tuple[str, ...]

    def probe(self) -> MigrationProbe: ...

    def preflight(self, probe: MigrationProbe) -> Mapping[str, Any]: ...

    def backup(
        self,
        *,
        transaction_id: str,
        backup_root: Path,
    ) -> tuple[MigrationBackupRecord, ...]: ...

    def apply(self, probe: MigrationProbe) -> MigrationMutation: ...

    def verify(self, mutation: MigrationMutation) -> Mapping[str, Any]: ...

    def restore(
        self,
        backups: Sequence[MigrationBackupRecord],
    ) -> Mapping[str, Any]: ...


def _sqlite_state_digest(connection: sqlite3.Connection) -> tuple[int, str]:
    """Hash a consistent logical SQLite image, including uncheckpointed WAL."""

    try:
        payload = connection.serialize()
    except (AttributeError, sqlite3.Error):
        snapshot = sqlite3.connect(":memory:")
        try:
            connection.backup(snapshot)
            payload = snapshot.serialize()
        finally:
            snapshot.close()
    return len(payload), "sha256:" + hashlib.sha256(payload).hexdigest()


class FileBackupRuntime:
    def __init__(self, backup_root: Path | str) -> None:
        self.backup_root = Path(backup_root).resolve()

    def backup_path(
        self,
        source: Path,
        *,
        transaction_id: str,
        adapter_id: str,
    ) -> MigrationBackupRecord:
        source = source.resolve()
        if not source.exists():
            raise FileNotFoundError(source)
        destination_root = (
            self.backup_root
            / _safe_segment(transaction_id)
            / _safe_segment(adapter_id)
        )
        destination_root.mkdir(parents=True, exist_ok=True)
        backup_id = "backup-" + hashlib.sha256(
            str(source).encode("utf-8")
        ).hexdigest()[:24]
        if source.is_file():
            destination = destination_root / f"{backup_id}.file"
            self._copy_file(source, destination)
            size, checksum = checksum_path(destination)
            source_kind = "file"
        elif source.is_dir():
            destination = destination_root / f"{backup_id}.directory"
            self._copy_directory(source, destination)
            size, checksum = checksum_tree(destination)
            source_kind = "directory"
        else:
            raise MigrationAdapterError(
                "migration_backup_source_unsupported",
                "backup source must be a regular file or directory",
                adapter_id=adapter_id,
                owner="FileBackupRuntime",
                details={"path": str(source)},
            )
        return MigrationBackupRecord(
            transaction_id=transaction_id,
            adapter_id=adapter_id,
            backup_id=backup_id,
            source_path=str(source),
            backup_path=str(destination),
            source_kind=source_kind,
            size_bytes=size,
            checksum=checksum,
            created_at_ns=time.time_ns(),
            restored_at_ns=None,
        )

    def restore(self, record: MigrationBackupRecord) -> dict[str, Any]:
        source = Path(record.source_path).resolve()
        backup = Path(record.backup_path).resolve()
        if not backup.exists():
            raise MigrationAdapterError(
                "migration_backup_missing",
                "migration backup is missing",
                adapter_id=record.adapter_id,
                owner="FileBackupRuntime",
                details={"backup_path": str(backup)},
            )
        if record.source_kind == "file":
            size, checksum = checksum_path(backup)
            if size != record.size_bytes or checksum != record.checksum:
                raise MigrationAdapterError(
                    "migration_backup_checksum_mismatch",
                    "migration file backup checksum does not match the journal",
                    adapter_id=record.adapter_id,
                    owner="FileBackupRuntime",
                    details={
                        "backup_path": str(backup),
                        "expected": record.checksum,
                        "actual": checksum,
                    },
                )
            source.parent.mkdir(parents=True, exist_ok=True)
            temporary = source.with_name(
                f".{source.name}.restore-{os.getpid()}-{time.time_ns()}.tmp"
            )
            self._copy_file(backup, temporary)
            os.replace(temporary, source)
            restored_size, restored_checksum = checksum_path(source)
        elif record.source_kind == "directory":
            size, checksum = checksum_tree(backup)
            if size != record.size_bytes or checksum != record.checksum:
                raise MigrationAdapterError(
                    "migration_backup_checksum_mismatch",
                    "migration directory backup checksum does not match the journal",
                    adapter_id=record.adapter_id,
                    owner="FileBackupRuntime",
                    details={
                        "backup_path": str(backup),
                        "expected": record.checksum,
                        "actual": checksum,
                    },
                )
            temporary = source.with_name(
                f".{source.name}.restore-{os.getpid()}-{time.time_ns()}"
            )
            displaced = source.with_name(
                f".{source.name}.displaced-{os.getpid()}-{time.time_ns()}"
            )
            self._copy_directory(backup, temporary)
            if source.exists():
                os.replace(source, displaced)
            try:
                os.replace(temporary, source)
            except BaseException:
                if displaced.exists() and not source.exists():
                    os.replace(displaced, source)
                raise
            else:
                if displaced.exists():
                    shutil.rmtree(displaced)
            restored_size, restored_checksum = checksum_tree(source)
        else:
            raise MigrationAdapterError(
                "migration_backup_kind_unsupported",
                f"unsupported backup source kind: {record.source_kind}",
                adapter_id=record.adapter_id,
                owner="FileBackupRuntime",
            )
        if (
            restored_size != record.size_bytes
            or restored_checksum != record.checksum
        ):
            raise MigrationAdapterError(
                "migration_restore_verification_failed",
                "restored data does not match the migration backup",
                adapter_id=record.adapter_id,
                owner="FileBackupRuntime",
                details={
                    "expected": record.checksum,
                    "actual": restored_checksum,
                },
            )
        return {
            "backup_id": record.backup_id,
            "source_path": str(source),
            "size_bytes": restored_size,
            "checksum": restored_checksum,
        }

    @staticmethod
    def _copy_file(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                while True:
                    block = reader.read(1024 * 1024)
                    if not block:
                        break
                    writer.write(block)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _copy_directory(source: Path, destination: Path) -> None:
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            source,
            destination,
            copy_function=shutil.copy2,
            symlinks=False,
            ignore_dangling_symlinks=False,
        )
        for path in destination.rglob("*"):
            if path.is_symlink():
                raise MigrationAdapterError(
                    "migration_backup_symlink_forbidden",
                    "migration directory backup contains a symbolic link",
                    adapter_id="file-backup",
                    owner="FileBackupRuntime",
                    details={"path": str(path)},
                )


class SqliteOwnerMigrationAdapter:
    def __init__(
        self,
        *,
        adapter_id: str,
        owner: str,
        path: Path | str,
        target_version: int,
        required_tables: tuple[str, ...] = (),
        dependencies: tuple[str, ...] = (),
        migrations: Mapping[int, Callable[[sqlite3.Connection], Any]] | None = None,
        allow_missing: bool = True,
    ) -> None:
        _validate_adapter_identity(adapter_id, owner)
        if target_version < 1:
            raise ValueError("target_version must be positive")
        self.adapter_id = adapter_id
        self.owner = owner
        self.path = Path(path).resolve()
        self.target_version = target_version
        self.required_tables = tuple(required_tables)
        self.dependencies = tuple(dependencies)
        self.migrations = dict(migrations or {})
        self.allow_missing = allow_missing

    def probe(self) -> MigrationProbe:
        if not self.path.exists():
            if not self.allow_missing:
                raise self._error(
                    "migration_owner_store_missing",
                    "required SQLite owner store is missing",
                )
            return MigrationProbe(
                adapter_id=self.adapter_id,
                owner=self.owner,
                path=str(self.path),
                source_version=0,
                target_version=self.target_version,
                exists=False,
                migration_required=True,
                mutable=True,
                state_digest=_absent_digest(self.path),
                details={
                    "clean_default": True,
                    "operation": "initialize_versioned_store",
                },
            )
        if not self.path.is_file():
            raise self._error(
                "migration_owner_store_not_file",
                "SQLite owner store path is not a regular file",
            )
        try:
            connection = self._connect(readonly=True)
            try:
                quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
                if quick != "ok":
                    raise self._error(
                        "migration_sqlite_integrity_failed",
                        "SQLite owner store failed quick_check",
                        details={"quick_check": quick},
                    )
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type='table' AND name NOT LIKE 'sqlite_%'
                        """
                    ).fetchall()
                }
                size, checksum = _sqlite_state_digest(connection)
            finally:
                connection.close()
        except sqlite3.Error as error:
            raise self._error(
                "migration_sqlite_probe_failed",
                f"cannot inspect SQLite owner store: {type(error).__name__}",
            ) from error
        missing_tables = [
            table for table in self.required_tables if table not in tables
        ]
        if missing_tables:
            raise self._error(
                "migration_sqlite_owner_tables_missing",
                "SQLite owner store is missing required tables",
                details={"missing_tables": missing_tables},
            )
        if version > self.target_version:
            raise self._error(
                "migration_sqlite_future_version",
                "SQLite owner store is newer than this runtime",
                details={"source": version, "target": self.target_version},
            )
        return MigrationProbe(
            adapter_id=self.adapter_id,
            owner=self.owner,
            path=str(self.path),
            source_version=version,
            target_version=self.target_version,
            exists=True,
            migration_required=version < self.target_version,
            mutable=version < self.target_version,
            state_digest=checksum,
            details={
                "size_bytes": size,
                "quick_check": "ok",
                "tables": sorted(tables),
            },
        )

    def preflight(self, probe: MigrationProbe) -> Mapping[str, Any]:
        self._assert_probe(probe)
        if not probe.exists:
            return {
                "ready": True,
                "operation": "initialize_clean_versioned_store",
            }
        if not os.access(self.path, os.R_OK):
            raise self._error(
                "migration_sqlite_unreadable",
                "SQLite owner store is not readable",
            )
        if probe.migration_required:
            if not os.access(self.path, os.W_OK):
                raise self._error(
                    "migration_sqlite_unwritable",
                    "SQLite owner store is not writable",
                )
            for source_version in range(
                probe.source_version,
                probe.target_version,
            ):
                if source_version not in self.migrations and source_version != 0:
                    raise self._error(
                        "migration_sqlite_step_missing",
                        "SQLite owner store has no migration for a version step",
                        details={
                            "source_version": source_version,
                            "target_version": source_version + 1,
                        },
                    )
        free = shutil.disk_usage(self.path.parent).free
        required = max(1_048_576, int(self.path.stat().st_size * 2.2))
        if free < required:
            raise self._error(
                "migration_backup_space_insufficient",
                "insufficient free space for SQLite migration backup",
                details={"required_bytes": required, "free_bytes": free},
            )
        return {
            "ready": True,
            "operation": (
                "migrate" if probe.migration_required else "verify_current"
            ),
            "free_bytes": free,
            "required_bytes": required,
        }

    def backup(
        self,
        *,
        transaction_id: str,
        backup_root: Path,
    ) -> tuple[MigrationBackupRecord, ...]:
        if not self.path.exists():
            return ()
        destination_root = (
            backup_root.resolve()
            / _safe_segment(transaction_id)
            / _safe_segment(self.adapter_id)
        )
        destination_root.mkdir(parents=True, exist_ok=True)
        destination = destination_root / "sqlite.snapshot"
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        source_connection = self._connect(readonly=True)
        backup_connection = sqlite3.connect(temporary)
        try:
            source_connection.backup(backup_connection)
            backup_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            backup_connection.commit()
        finally:
            backup_connection.close()
            source_connection.close()
        os.replace(temporary, destination)
        size, checksum = checksum_path(destination)
        return (
            MigrationBackupRecord(
                transaction_id=transaction_id,
                adapter_id=self.adapter_id,
                backup_id="backup-sqlite",
                source_path=str(self.path),
                backup_path=str(destination),
                source_kind="file",
                size_bytes=size,
                checksum=checksum,
                created_at_ns=time.time_ns(),
                restored_at_ns=None,
            ),
        )

    def apply(self, probe: MigrationProbe) -> MigrationMutation:
        self._assert_probe(probe)
        if not probe.migration_required:
            return MigrationMutation(
                adapter_id=self.adapter_id,
                owner=self.owner,
                source_version=probe.source_version,
                target_version=probe.target_version,
                before_digest=probe.state_digest,
                after_digest=probe.state_digest,
                changed=False,
                attributes={"operation": "already_current"},
            )
        connection = self._connect(readonly=False)
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current != probe.source_version:
                raise self._error(
                    "migration_sqlite_version_changed",
                    "SQLite owner store changed after migration planning",
                    details={
                        "planned": probe.source_version,
                        "actual": current,
                    },
                )
            for source_version in range(current, self.target_version):
                migration = self.migrations.get(source_version)
                if migration is not None:
                    migration(connection)
                elif source_version != 0:
                    raise self._error(
                        "migration_sqlite_step_missing",
                        "SQLite owner store migration step is missing",
                        details={"source_version": source_version},
                    )
                connection.execute(f"PRAGMA user_version={source_version + 1}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        digest_connection = self._connect(readonly=True)
        try:
            _, after = _sqlite_state_digest(digest_connection)
        finally:
            digest_connection.close()
        return MigrationMutation(
            adapter_id=self.adapter_id,
            owner=self.owner,
            source_version=probe.source_version,
            target_version=self.target_version,
            before_digest=probe.state_digest,
            after_digest=after,
            changed=True,
            attributes={
                "operation": "sqlite_transaction",
                "version_steps": self.target_version - probe.source_version,
            },
        )

    def verify(self, mutation: MigrationMutation) -> Mapping[str, Any]:
        if mutation.adapter_id != self.adapter_id:
            raise self._error(
                "migration_mutation_adapter_mismatch",
                "migration mutation belongs to another adapter",
            )
        probe = self.probe()
        if not probe.exists:
            return {
                "verified": True,
                "clean_default": True,
                "target_version": self.target_version,
            }
        if probe.source_version != self.target_version:
            raise self._error(
                "migration_sqlite_verify_version_failed",
                "SQLite owner store did not reach the target version",
                details={
                    "expected": self.target_version,
                    "actual": probe.source_version,
                },
            )
        return {
            "verified": True,
            "target_version": self.target_version,
            "state_digest": probe.state_digest,
            "quick_check": probe.details.get("quick_check"),
            "required_tables": list(self.required_tables),
        }

    def restore(
        self,
        backups: Sequence[MigrationBackupRecord],
    ) -> Mapping[str, Any]:
        selected = [
            record
            for record in backups
            if record.adapter_id == self.adapter_id
        ]
        if not selected:
            if self.allow_missing:
                self.path.unlink(missing_ok=True)
                for suffix in ("-wal", "-shm"):
                    Path(str(self.path) + suffix).unlink(missing_ok=True)
                return {
                    "restored": True,
                    "clean_default_removed": True,
                }
            if self.path.exists():
                raise self._error(
                    "migration_sqlite_backup_missing",
                    "SQLite migration cannot roll back without a backup",
                )
            return {"restored": True, "clean_default": True}
        if len(selected) != 1:
            raise self._error(
                "migration_sqlite_backup_ambiguous",
                "SQLite migration has an ambiguous backup set",
                details={"backups": len(selected)},
            )
        runtime = FileBackupRuntime(Path(selected[0].backup_path).parent.parent.parent)
        receipt = runtime.restore(selected[0])
        for suffix in ("-wal", "-shm"):
            Path(str(self.path) + suffix).unlink(missing_ok=True)
        probe = self.probe()
        if probe.source_version != selected_source_version(selected[0]):
            # Generic backups do not encode user_version separately. The
            # checksum verification above is authoritative; report the value
            # for the caller's rollback verification.
            pass
        return {
            "restored": True,
            "receipt": receipt,
            "source_version": probe.source_version,
            "state_digest": probe.state_digest,
        }

    def _connect(self, *, readonly: bool) -> sqlite3.Connection:
        if readonly:
            uri = self.path.as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=30)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _assert_probe(self, probe: MigrationProbe) -> None:
        if probe.adapter_id != self.adapter_id or probe.owner != self.owner:
            raise self._error(
                "migration_probe_identity_mismatch",
                "migration probe belongs to another owner adapter",
            )
        if Path(probe.path).resolve() != self.path:
            raise self._error(
                "migration_probe_path_mismatch",
                "migration probe path changed",
            )
        current = self.probe()
        if current.state_digest != probe.state_digest:
            raise self._error(
                "migration_probe_stale",
                "owner store changed after it was probed",
                details={
                    "planned_digest": probe.state_digest,
                    "actual_digest": current.state_digest,
                },
            )

    def _error(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> MigrationAdapterError:
        return MigrationAdapterError(
            code,
            message,
            adapter_id=self.adapter_id,
            owner=self.owner,
            details=details,
        )


class CallableOwnerMigrationAdapter:
    def __init__(
        self,
        *,
        adapter_id: str,
        owner: str,
        path: Path | str,
        target_version: int,
        probe_version: Callable[[], int],
        apply_migration: Callable[[int, int], Mapping[str, Any]],
        verify_owner: Callable[[int], Mapping[str, Any]],
        dependencies: tuple[str, ...] = (),
        allow_missing: bool = True,
    ) -> None:
        _validate_adapter_identity(adapter_id, owner)
        self.adapter_id = adapter_id
        self.owner = owner
        self.path = Path(path).resolve()
        self.target_version = target_version
        self.dependencies = tuple(dependencies)
        self._probe_version = probe_version
        self._apply_migration = apply_migration
        self._verify_owner = verify_owner
        self.allow_missing = allow_missing

    def probe(self) -> MigrationProbe:
        exists = self.path.exists()
        if not exists and not self.allow_missing:
            raise MigrationAdapterError(
                "migration_owner_store_missing",
                "required owner store is missing",
                adapter_id=self.adapter_id,
                owner=self.owner,
            )
        if exists and not (self.path.is_file() or self.path.is_dir()):
            raise MigrationAdapterError(
                "migration_owner_store_kind_invalid",
                "owner store must be a file or directory",
                adapter_id=self.adapter_id,
                owner=self.owner,
            )
        version = int(self._probe_version()) if exists else self.target_version
        if version > self.target_version:
            raise MigrationAdapterError(
                "migration_owner_future_version",
                "owner store is newer than this runtime",
                adapter_id=self.adapter_id,
                owner=self.owner,
                details={"source": version, "target": self.target_version},
            )
        digest = digest_path(self.path) if exists else _absent_digest(self.path)
        return MigrationProbe(
            adapter_id=self.adapter_id,
            owner=self.owner,
            path=str(self.path),
            source_version=version,
            target_version=self.target_version,
            exists=exists,
            migration_required=exists and version < self.target_version,
            mutable=exists and version < self.target_version,
            state_digest=digest,
            details={"kind": "directory" if self.path.is_dir() else "file"},
        )

    def preflight(self, probe: MigrationProbe) -> Mapping[str, Any]:
        self._assert_probe(probe)
        if not probe.exists:
            return {"ready": True, "operation": "clean_default_no_store"}
        if not os.access(self.path, os.R_OK):
            raise MigrationAdapterError(
                "migration_owner_store_unreadable",
                "owner store is not readable",
                adapter_id=self.adapter_id,
                owner=self.owner,
            )
        if probe.migration_required and not os.access(self.path, os.W_OK):
            raise MigrationAdapterError(
                "migration_owner_store_unwritable",
                "owner store is not writable",
                adapter_id=self.adapter_id,
                owner=self.owner,
            )
        return {
            "ready": True,
            "operation": (
                "migrate" if probe.migration_required else "verify_current"
            ),
        }

    def backup(
        self,
        *,
        transaction_id: str,
        backup_root: Path,
    ) -> tuple[MigrationBackupRecord, ...]:
        if not self.path.exists():
            return ()
        runtime = FileBackupRuntime(backup_root)
        return (
            runtime.backup_path(
                self.path,
                transaction_id=transaction_id,
                adapter_id=self.adapter_id,
            ),
        )

    def apply(self, probe: MigrationProbe) -> MigrationMutation:
        self._assert_probe(probe)
        if not probe.migration_required:
            return MigrationMutation(
                adapter_id=self.adapter_id,
                owner=self.owner,
                source_version=probe.source_version,
                target_version=probe.target_version,
                before_digest=probe.state_digest,
                after_digest=probe.state_digest,
                changed=False,
                attributes={"operation": "already_current"},
            )
        attributes = dict(
            self._apply_migration(probe.source_version, probe.target_version)
        )
        after = digest_path(self.path)
        if after == probe.state_digest:
            raise MigrationAdapterError(
                "migration_owner_no_effect",
                "owner migration reported success without changing its old store",
                adapter_id=self.adapter_id,
                owner=self.owner,
            )
        return MigrationMutation(
            adapter_id=self.adapter_id,
            owner=self.owner,
            source_version=probe.source_version,
            target_version=probe.target_version,
            before_digest=probe.state_digest,
            after_digest=after,
            changed=True,
            attributes=attributes,
        )

    def verify(self, mutation: MigrationMutation) -> Mapping[str, Any]:
        if mutation.adapter_id != self.adapter_id:
            raise MigrationAdapterError(
                "migration_mutation_adapter_mismatch",
                "migration mutation belongs to another adapter",
                adapter_id=self.adapter_id,
                owner=self.owner,
            )
        if not self.path.exists():
            return {
                "verified": True,
                "clean_default": True,
                "target_version": self.target_version,
            }
        version = int(self._probe_version())
        if version != self.target_version:
            raise MigrationAdapterError(
                "migration_owner_verify_version_failed",
                "owner store did not reach the target version",
                adapter_id=self.adapter_id,
                owner=self.owner,
                details={"expected": self.target_version, "actual": version},
            )
        verification = dict(self._verify_owner(self.target_version))
        if verification.get("verified") is not True:
            raise MigrationAdapterError(
                "migration_owner_verification_failed",
                "owner-specific migration verification failed",
                adapter_id=self.adapter_id,
                owner=self.owner,
                details=verification,
            )
        verification.setdefault("state_digest", digest_path(self.path))
        verification.setdefault("target_version", self.target_version)
        return verification

    def restore(
        self,
        backups: Sequence[MigrationBackupRecord],
    ) -> Mapping[str, Any]:
        selected = [
            record
            for record in backups
            if record.adapter_id == self.adapter_id
        ]
        if not selected:
            if self.path.exists():
                raise MigrationAdapterError(
                    "migration_owner_backup_missing",
                    "owner migration cannot roll back without a backup",
                    adapter_id=self.adapter_id,
                    owner=self.owner,
                )
            return {"restored": True, "clean_default": True}
        if len(selected) != 1:
            raise MigrationAdapterError(
                "migration_owner_backup_ambiguous",
                "owner migration has an ambiguous backup set",
                adapter_id=self.adapter_id,
                owner=self.owner,
            )
        runtime = FileBackupRuntime(Path(selected[0].backup_path).parent.parent.parent)
        receipt = runtime.restore(selected[0])
        return {
            "restored": True,
            "receipt": receipt,
            "source_version": int(self._probe_version()),
            "state_digest": digest_path(self.path),
        }

    def _assert_probe(self, probe: MigrationProbe) -> None:
        if (
            probe.adapter_id != self.adapter_id
            or probe.owner != self.owner
            or Path(probe.path).resolve() != self.path
        ):
            raise MigrationAdapterError(
                "migration_probe_identity_mismatch",
                "migration probe does not match this owner adapter",
                adapter_id=self.adapter_id,
                owner=self.owner,
            )
        current = self.probe()
        if current.state_digest != probe.state_digest:
            raise MigrationAdapterError(
                "migration_probe_stale",
                "owner store changed after it was probed",
                adapter_id=self.adapter_id,
                owner=self.owner,
                details={
                    "planned_digest": probe.state_digest,
                    "actual_digest": current.state_digest,
                },
            )


class ArtifactManifestMigrationAdapter:
    MANIFEST_SCHEMA = "zyra.artifact-store-manifest/v1"

    def __init__(
        self,
        root: Path | str,
        *,
        adapter_id: str = "artifact-state",
        owner: str = "LocalArtifactStore",
        target_version: int = 1,
        dependencies: tuple[str, ...] = (),
    ) -> None:
        _validate_adapter_identity(adapter_id, owner)
        self.adapter_id = adapter_id
        self.owner = owner
        self.root = Path(root).resolve()
        self.path = self.root / ".zyra-artifact-store.json"
        self.target_version = target_version
        self.dependencies = tuple(dependencies)

    def probe(self) -> MigrationProbe:
        if not self.root.exists():
            return MigrationProbe(
                adapter_id=self.adapter_id,
                owner=self.owner,
                path=str(self.root),
                source_version=0,
                target_version=self.target_version,
                exists=False,
                migration_required=True,
                mutable=True,
                state_digest=_absent_digest(self.root),
                details={
                    "clean_default": True,
                    "artifact_count": 0,
                    "operation": "initialize_artifact_manifest",
                },
            )
        if not self.root.is_dir():
            raise self._error(
                "migration_artifact_root_invalid",
                "artifact root is not a directory",
            )
        inventory = self._inventory()
        appended_artifact_count = 0
        if self.path.exists():
            manifest = self._read_manifest()
            version = int(manifest.get("schema_version") or 0)
            recorded = str(manifest.get("inventory_digest") or "")
            if version == self.target_version and recorded != inventory["digest"]:
                drift = self._recorded_inventory_drift(
                    manifest.get("inventory_entries"),
                    inventory["entries"],
                )
                if drift:
                    raise self._error(
                        "migration_artifact_inventory_drift",
                        "artifact manifest does not match immutable artifact bytes",
                        details={
                            "recorded": recorded,
                            "actual": inventory["digest"],
                            "drift": drift,
                        },
                    )
                appended_artifact_count = (
                    len(inventory["entries"])
                    - len(manifest["inventory_entries"])
                )
        else:
            version = 0
        if version > self.target_version:
            raise self._error(
                "migration_artifact_future_version",
                "artifact manifest is newer than this runtime",
                details={"source": version, "target": self.target_version},
            )
        return MigrationProbe(
            adapter_id=self.adapter_id,
            owner=self.owner,
            path=str(self.root),
            source_version=version,
            target_version=self.target_version,
            exists=True,
            migration_required=version < self.target_version,
            mutable=version < self.target_version,
            state_digest=digest_path(self.root),
            details={
                "artifact_count": inventory["count"],
                "artifact_bytes": inventory["bytes"],
                "inventory_digest": inventory["digest"],
                "appended_artifact_count": appended_artifact_count,
            },
        )

    def preflight(self, probe: MigrationProbe) -> Mapping[str, Any]:
        if probe.adapter_id != self.adapter_id:
            raise self._error(
                "migration_probe_identity_mismatch",
                "artifact migration probe belongs to another adapter",
            )
        if not probe.exists:
            return {
                "ready": True,
                "operation": "initialize_clean_artifact_manifest",
            }
        for path in self.root.rglob("*"):
            if path.is_symlink():
                raise self._error(
                    "migration_artifact_symlink_forbidden",
                    "artifact root contains a symbolic link",
                    details={"path": str(path)},
                )
            if path.is_file():
                mode = path.stat().st_mode
                if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                    raise self._error(
                        "migration_artifact_executable_forbidden",
                        "artifact root contains an executable file",
                        details={"path": str(path)},
                    )
        return {
            "ready": True,
            "operation": (
                "write_manifest" if probe.migration_required else "verify_current"
            ),
            "artifact_count": probe.details.get("artifact_count", 0),
        }

    def backup(
        self,
        *,
        transaction_id: str,
        backup_root: Path,
    ) -> tuple[MigrationBackupRecord, ...]:
        if not self.path.exists():
            return ()
        runtime = FileBackupRuntime(backup_root)
        return (
            runtime.backup_path(
                self.path,
                transaction_id=transaction_id,
                adapter_id=self.adapter_id,
            ),
        )

    def apply(self, probe: MigrationProbe) -> MigrationMutation:
        if not probe.migration_required:
            return MigrationMutation(
                adapter_id=self.adapter_id,
                owner=self.owner,
                source_version=probe.source_version,
                target_version=probe.target_version,
                before_digest=probe.state_digest,
                after_digest=probe.state_digest,
                changed=False,
                attributes={"operation": "already_current"},
            )
        self.root.mkdir(parents=True, exist_ok=True)
        inventory = self._inventory()
        manifest = {
            "schema": self.MANIFEST_SCHEMA,
            "schema_version": self.target_version,
            "owner": self.owner,
            "inventory_digest": inventory["digest"],
            "artifact_count": inventory["count"],
            "artifact_bytes": inventory["bytes"],
            "inventory_entries": inventory["entries"],
            "created_at_ns": time.time_ns(),
            "source_version": probe.source_version,
        }
        _atomic_json_write(self.path, manifest)
        after = digest_path(self.root)
        return MigrationMutation(
            adapter_id=self.adapter_id,
            owner=self.owner,
            source_version=probe.source_version,
            target_version=self.target_version,
            before_digest=probe.state_digest,
            after_digest=after,
            changed=True,
            attributes={
                "operation": "artifact_manifest_commit",
                "artifact_count": inventory["count"],
                "inventory_digest": inventory["digest"],
            },
        )

    def verify(self, mutation: MigrationMutation) -> Mapping[str, Any]:
        if not self.root.exists():
            return {
                "verified": True,
                "clean_default": True,
                "target_version": self.target_version,
                "artifact_count": 0,
                "state_digest": _absent_digest(self.root),
            }
        manifest = self._read_manifest()
        if (
            manifest.get("schema") != self.MANIFEST_SCHEMA
            or int(manifest.get("schema_version") or 0) != self.target_version
            or manifest.get("owner") != self.owner
        ):
            raise self._error(
                "migration_artifact_manifest_invalid",
                "artifact manifest schema or owner is invalid",
            )
        inventory = self._inventory()
        if manifest.get("inventory_digest") != inventory["digest"]:
            drift = self._recorded_inventory_drift(
                manifest.get("inventory_entries"),
                inventory["entries"],
            )
            if drift:
                raise self._error(
                    "migration_artifact_verify_failed",
                    "artifact inventory changed during migration",
                    details={"drift": drift},
                )
        return {
            "verified": True,
            "target_version": self.target_version,
            "inventory_digest": inventory["digest"],
            "artifact_count": inventory["count"],
            "appended_artifact_count": (
                len(inventory["entries"])
                - len(manifest.get("inventory_entries") or {})
            ),
            "state_digest": digest_path(self.root),
        }

    def restore(
        self,
        backups: Sequence[MigrationBackupRecord],
    ) -> Mapping[str, Any]:
        selected = [
            record
            for record in backups
            if record.adapter_id == self.adapter_id
        ]
        if not selected:
            self.path.unlink(missing_ok=True)
            return {
                "restored": True,
                "manifest_removed": True,
                "artifact_bytes_changed": False,
            }
        if len(selected) != 1:
            raise self._error(
                "migration_artifact_backup_ambiguous",
                "artifact manifest has an ambiguous backup set",
            )
        runtime = FileBackupRuntime(Path(selected[0].backup_path).parent.parent.parent)
        receipt = runtime.restore(selected[0])
        return {
            "restored": True,
            "receipt": receipt,
            "artifact_bytes_changed": False,
        }

    def _inventory(self) -> dict[str, Any]:
        digest = hashlib.sha256()
        count = 0
        size = 0
        entries: dict[str, dict[str, Any]] = {}
        if not self.root.exists():
            return {
                "count": 0,
                "bytes": 0,
                "digest": "sha256:" + digest.hexdigest(),
                "entries": entries,
            }
        for path in sorted(self.root.rglob("*"), key=lambda item: item.as_posix()):
            if path == self.path or not path.is_file():
                continue
            relative_path = path.relative_to(self.root)
            if relative_path.parts and relative_path.parts[0].startswith("."):
                # Operational owner stores share the artifact root for
                # deployment locality, but are migrated by their own canonical
                # adapters and must not perturb immutable artifact inventory.
                continue
            if path.is_symlink():
                raise self._error(
                    "migration_artifact_symlink_forbidden",
                    "artifact inventory contains a symbolic link",
                    details={"path": str(path)},
                )
            relative = relative_path.as_posix()
            file_size, file_checksum = checksum_path(path)
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(file_size).encode("ascii"))
            digest.update(b"\0")
            digest.update(file_checksum.encode("ascii"))
            digest.update(b"\n")
            entries[relative] = {
                "bytes": file_size,
                "sha256": file_checksum,
            }
            count += 1
            size += file_size
        return {
            "count": count,
            "bytes": size,
            "digest": "sha256:" + digest.hexdigest(),
            "entries": entries,
        }

    @staticmethod
    def _recorded_inventory_drift(
        recorded: Any,
        actual: Mapping[str, Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        if not isinstance(recorded, Mapping):
            return [{"reason": "inventory_entries_missing"}]
        drift: list[dict[str, Any]] = []
        for relative, expected in recorded.items():
            if not isinstance(relative, str) or not isinstance(expected, Mapping):
                drift.append(
                    {
                        "path": str(relative),
                        "reason": "inventory_entry_invalid",
                    }
                )
                continue
            observed = actual.get(relative)
            if observed is None:
                drift.append({"path": relative, "reason": "artifact_missing"})
                continue
            try:
                expected_bytes = int(expected.get("bytes"))
            except (TypeError, ValueError):
                expected_bytes = -1
            expected_sha256 = str(expected.get("sha256") or "")
            if (
                expected_bytes != int(observed.get("bytes") or 0)
                or expected_sha256 != str(observed.get("sha256") or "")
            ):
                drift.append({"path": relative, "reason": "artifact_changed"})
        return drift

    def _read_manifest(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise self._error(
                "migration_artifact_manifest_unreadable",
                "artifact manifest cannot be read",
                details={"error": type(error).__name__},
            ) from error
        if not isinstance(value, dict):
            raise self._error(
                "migration_artifact_manifest_invalid",
                "artifact manifest root must be an object",
            )
        return value

    def _error(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> MigrationAdapterError:
        return MigrationAdapterError(
            code,
            message,
            adapter_id=self.adapter_id,
            owner=self.owner,
            details=details,
        )


def checksum_tree(root: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ValueError(f"tree checksum refuses symbolic link: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        file_size, file_checksum = checksum_path(path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(file_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_checksum.encode("ascii"))
        digest.update(b"\n")
        size += file_size
    return size, "sha256:" + digest.hexdigest()


def digest_path(path: Path) -> str:
    if not path.exists():
        return _absent_digest(path)
    if path.is_file():
        return checksum_path(path)[1]
    if path.is_dir():
        return checksum_tree(path)[1]
    raise ValueError(f"unsupported path kind: {path}")


def selected_source_version(record: MigrationBackupRecord) -> int:
    path = Path(record.backup_path)
    if record.source_kind != "file" or not path.exists():
        return 0
    try:
        connection = sqlite3.connect(path)
        try:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()
    except sqlite3.Error:
        return 0


def _absent_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(
        f"absent:{path.resolve()}".encode("utf-8")
    ).hexdigest()


def _safe_segment(value: str) -> str:
    if not value or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in value):
        raise ValueError(f"unsafe path segment: {value!r}")
    return value


def _validate_adapter_identity(adapter_id: str, owner: str) -> None:
    _safe_segment(adapter_id)
    if not owner.strip() or len(owner) > 192:
        raise ValueError("migration owner must be between 1 and 192 characters")


def _atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
