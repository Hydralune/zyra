from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import ContractError, stable_digest, validate_relative_path


class CleanStateError(ContractError):
    pass


@dataclass(frozen=True, slots=True)
class StateRoot:
    root_id: str
    path: Path
    kind: str
    writable: bool = True
    allowed_outputs: tuple[str, ...] = ()
    maximum_files: int = 50_000
    maximum_bytes: int = 2 * 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        if not self.root_id.strip() or not self.kind.strip():
            raise CleanStateError("state root requires root_id and kind")
        if self.maximum_files < 0 or self.maximum_bytes < 0:
            raise CleanStateError("state-root budgets cannot be negative")
        object.__setattr__(
            self,
            "allowed_outputs",
            tuple(
                sorted(
                    {
                        validate_relative_path(item, field_name="allowed output")
                        for item in self.allowed_outputs
                    }
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class StateFile:
    relative_path: str
    kind: str
    size_bytes: int
    digest: str
    modified_ns: int
    mode: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "digest": self.digest,
            "modified_ns": self.modified_ns,
            "mode": self.mode,
        }


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    root_id: str
    kind: str
    exists: bool
    files: tuple[StateFile, ...]
    total_bytes: int
    unreadable_paths: tuple[str, ...]
    escaped_symlinks: tuple[str, ...]
    sqlite_integrity: Mapping[str, str]
    digest: str

    def file_map(self) -> dict[str, StateFile]:
        return {item.relative_path: item for item in self.files}

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_id": self.root_id,
            "kind": self.kind,
            "exists": self.exists,
            "file_count": len(self.files),
            "total_bytes": self.total_bytes,
            "files": [item.to_dict() for item in self.files],
            "unreadable_paths": list(self.unreadable_paths),
            "escaped_symlinks": list(self.escaped_symlinks),
            "sqlite_integrity": dict(sorted(self.sqlite_integrity.items())),
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class StateDelta:
    root_id: str
    created: tuple[str, ...]
    modified: tuple[str, ...]
    deleted: tuple[str, ...]
    metadata_only: tuple[str, ...]
    allowed_changes: tuple[str, ...]
    pollution: tuple[str, ...]
    before_digest: str
    after_digest: str

    @property
    def clean(self) -> bool:
        return not self.pollution

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_id": self.root_id,
            "clean": self.clean,
            "created": list(self.created),
            "modified": list(self.modified),
            "deleted": list(self.deleted),
            "metadata_only": list(self.metadata_only),
            "allowed_changes": list(self.allowed_changes),
            "pollution": list(self.pollution),
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
        }


@dataclass(frozen=True, slots=True)
class CleanStateReport:
    snapshots_before: tuple[StateSnapshot, ...]
    snapshots_after: tuple[StateSnapshot, ...]
    deltas: tuple[StateDelta, ...]
    environment_leaks: tuple[str, ...]
    generated_input_digest: str
    fresh_state: bool
    digest: str

    @property
    def valid(self) -> bool:
        return (
            self.fresh_state
            and not self.environment_leaks
            and all(item.clean for item in self.deltas)
            and all(not item.unreadable_paths for item in self.snapshots_after)
            and all(not item.escaped_symlinks for item in self.snapshots_after)
            and all(
                result == "ok"
                for item in self.snapshots_after
                for result in item.sqlite_integrity.values()
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.m3-clean-state-report/v1",
            "valid": self.valid,
            "fresh_state": self.fresh_state,
            "generated_input_digest": self.generated_input_digest,
            "environment_leaks": list(self.environment_leaks),
            "before": [item.to_dict() for item in self.snapshots_before],
            "after": [item.to_dict() for item in self.snapshots_after],
            "deltas": [item.to_dict() for item in self.deltas],
            "digest": self.digest,
        }


class StateFingerprinter:
    def __init__(
        self,
        *,
        content_hash_limit: int = 64 * 1024 * 1024,
        sqlite_check_limit: int = 256 * 1024 * 1024,
    ) -> None:
        self.content_hash_limit = int(content_hash_limit)
        self.sqlite_check_limit = int(sqlite_check_limit)

    def snapshot(self, root: StateRoot) -> StateSnapshot:
        path = root.path.resolve(strict=False)
        if not path.exists():
            material = {
                "root_id": root.root_id,
                "kind": root.kind,
                "exists": False,
                "files": [],
            }
            return StateSnapshot(
                root_id=root.root_id,
                kind=root.kind,
                exists=False,
                files=(),
                total_bytes=0,
                unreadable_paths=(),
                escaped_symlinks=(),
                sqlite_integrity={},
                digest=stable_digest(material),
            )
        files: list[StateFile] = []
        unreadable: list[str] = []
        escaped: list[str] = []
        sqlite_integrity: dict[str, str] = {}
        total_bytes = 0
        for candidate in sorted(path.rglob("*")):
            relative = candidate.relative_to(path).as_posix()
            try:
                stat_result = candidate.lstat()
            except OSError:
                unreadable.append(relative)
                continue
            if stat.S_ISLNK(stat_result.st_mode):
                try:
                    target = candidate.resolve(strict=False)
                except OSError:
                    escaped.append(relative)
                    continue
                if not target.is_relative_to(path):
                    escaped.append(relative)
                files.append(
                    StateFile(
                        relative_path=relative,
                        kind="symlink",
                        size_bytes=0,
                        digest=stable_digest("symlink", os.readlink(candidate)),
                        modified_ns=stat_result.st_mtime_ns,
                        mode=stat_result.st_mode,
                    )
                )
                continue
            if not stat.S_ISREG(stat_result.st_mode):
                continue
            if len(files) >= root.maximum_files:
                raise CleanStateError(
                    f"{root.root_id} exceeds {root.maximum_files} files"
                )
            total_bytes += stat_result.st_size
            if total_bytes > root.maximum_bytes:
                raise CleanStateError(
                    f"{root.root_id} exceeds {root.maximum_bytes} bytes"
                )
            try:
                digest = self._digest(candidate, stat_result.st_size)
            except OSError:
                unreadable.append(relative)
                continue
            files.append(
                StateFile(
                    relative_path=relative,
                    kind=self._kind(candidate),
                    size_bytes=stat_result.st_size,
                    digest=digest,
                    modified_ns=stat_result.st_mtime_ns,
                    mode=stat_result.st_mode,
                )
            )
            if (
                self._looks_like_sqlite(candidate)
                and stat_result.st_size <= self.sqlite_check_limit
            ):
                sqlite_integrity[relative] = self._sqlite_integrity(candidate)
        material = {
            "root_id": root.root_id,
            "kind": root.kind,
            "exists": True,
            "files": [item.to_dict() for item in files],
            "unreadable": unreadable,
            "escaped": escaped,
            "sqlite": sqlite_integrity,
        }
        return StateSnapshot(
            root_id=root.root_id,
            kind=root.kind,
            exists=True,
            files=tuple(files),
            total_bytes=total_bytes,
            unreadable_paths=tuple(unreadable),
            escaped_symlinks=tuple(escaped),
            sqlite_integrity=sqlite_integrity,
            digest=stable_digest(material),
        )

    def compare(
        self,
        root: StateRoot,
        before: StateSnapshot,
        after: StateSnapshot,
    ) -> StateDelta:
        if before.root_id != root.root_id or after.root_id != root.root_id:
            raise CleanStateError("state snapshot root identity mismatch")
        before_files = before.file_map()
        after_files = after.file_map()
        created = sorted(set(after_files) - set(before_files))
        deleted = sorted(set(before_files) - set(after_files))
        modified: list[str] = []
        metadata_only: list[str] = []
        for path in sorted(set(before_files) & set(after_files)):
            left = before_files[path]
            right = after_files[path]
            if left.digest != right.digest or left.size_bytes != right.size_bytes:
                modified.append(path)
            elif left.modified_ns != right.modified_ns or left.mode != right.mode:
                metadata_only.append(path)
        allowed: list[str] = []
        pollution: list[str] = []
        for path in (*created, *modified, *deleted, *metadata_only):
            if self._is_allowed(path, root.allowed_outputs):
                allowed.append(path)
            else:
                pollution.append(path)
        pollution.extend(f"symlink_escape:{item}" for item in after.escaped_symlinks)
        pollution.extend(f"unreadable:{item}" for item in after.unreadable_paths)
        pollution.extend(
            f"sqlite_integrity:{path}:{result}"
            for path, result in after.sqlite_integrity.items()
            if result != "ok"
        )
        return StateDelta(
            root_id=root.root_id,
            created=tuple(created),
            modified=tuple(modified),
            deleted=tuple(deleted),
            metadata_only=tuple(metadata_only),
            allowed_changes=tuple(sorted(set(allowed))),
            pollution=tuple(sorted(set(pollution))),
            before_digest=before.digest,
            after_digest=after.digest,
        )

    def _digest(self, path: Path, size: int) -> str:
        digest = hashlib.sha256()
        if size <= self.content_hash_limit:
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        else:
            with path.open("rb") as stream:
                head = stream.read(1024 * 1024)
                stream.seek(max(0, size - 1024 * 1024))
                tail = stream.read(1024 * 1024)
            digest.update(str(size).encode("ascii"))
            digest.update(head)
            digest.update(tail)
        return "sha256:" + digest.hexdigest()

    @staticmethod
    def _kind(path: Path) -> str:
        lowered = path.name.casefold()
        suffix = path.suffix.casefold()
        if suffix in {".sqlite", ".sqlite3", ".db"}:
            return "sqlite"
        if lowered in {"package-lock.json", "bun.lock", "bun.lockb", "uv.lock"}:
            return "lock"
        if "__pycache__" in path.parts or suffix in {".pyc", ".pyo"}:
            return "cache"
        if lowered.endswith((".index", ".idx")) or "index" in lowered:
            return "index"
        if suffix in {".log", ".jsonl"}:
            return "log"
        return "file"

    @staticmethod
    def _looks_like_sqlite(path: Path) -> bool:
        if path.suffix.casefold() not in {".sqlite", ".sqlite3", ".db"}:
            return False
        try:
            with path.open("rb") as stream:
                return stream.read(16) == b"SQLite format 3\x00"
        except OSError:
            return False

    @staticmethod
    def _sqlite_integrity(path: Path) -> str:
        database: sqlite3.Connection | None = None
        try:
            uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
            database = sqlite3.connect(uri, uri=True, timeout=2.0)
            row = database.execute("PRAGMA quick_check").fetchone()
            return str(row[0] if row else "missing_result").casefold()
        except (sqlite3.Error, OSError) as error:
            return f"error:{type(error).__name__}"
        finally:
            if database is not None:
                database.close()

    @staticmethod
    def _is_allowed(path: str, patterns: Sequence[str]) -> bool:
        candidate = Path(path)
        return any(candidate.match(pattern) for pattern in patterns)


class CleanStateGuard(AbstractContextManager["CleanStateGuard"]):
    """Captures declared roots around one generated-input scenario."""

    def __init__(
        self,
        roots: Iterable[StateRoot],
        *,
        generated_input: Any,
        prohibited_environment_paths: Iterable[str | Path] = (),
        fingerprinter: StateFingerprinter | None = None,
    ) -> None:
        self.roots = tuple(roots)
        if len({item.root_id for item in self.roots}) != len(self.roots):
            raise CleanStateError("clean-state roots require unique identities")
        self.generated_input_digest = stable_digest(generated_input)
        self.prohibited_environment_paths = tuple(
            Path(item).resolve(strict=False)
            for item in prohibited_environment_paths
        )
        self.fingerprinter = fingerprinter or StateFingerprinter()
        self.before: tuple[StateSnapshot, ...] = ()
        self.report: CleanStateReport | None = None

    def __enter__(self) -> "CleanStateGuard":
        self.before = tuple(
            self.fingerprinter.snapshot(root)
            for root in self.roots
        )
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        after = tuple(
            self.fingerprinter.snapshot(root)
            for root in self.roots
        )
        before_by_id = {item.root_id: item for item in self.before}
        after_by_id = {item.root_id: item for item in after}
        deltas = tuple(
            self.fingerprinter.compare(
                root,
                before_by_id[root.root_id],
                after_by_id[root.root_id],
            )
            for root in self.roots
        )
        environment_leaks = self._environment_leaks()
        fresh = all(
            not snapshot.exists or not snapshot.files
            for snapshot in self.before
            if self._root(snapshot.root_id).writable
        )
        material = {
            "before": [item.to_dict() for item in self.before],
            "after": [item.to_dict() for item in after],
            "deltas": [item.to_dict() for item in deltas],
            "environment_leaks": environment_leaks,
            "generated_input_digest": self.generated_input_digest,
            "fresh_state": fresh,
        }
        self.report = CleanStateReport(
            snapshots_before=self.before,
            snapshots_after=after,
            deltas=deltas,
            environment_leaks=environment_leaks,
            generated_input_digest=self.generated_input_digest,
            fresh_state=fresh,
            digest=stable_digest(material),
        )
        return False

    def require_report(self) -> CleanStateReport:
        if self.report is None:
            raise CleanStateError("clean-state guard has not completed")
        return self.report

    def _root(self, root_id: str) -> StateRoot:
        return next(item for item in self.roots if item.root_id == root_id)

    def _environment_leaks(self) -> tuple[str, ...]:
        leaks: list[str] = []
        variables = (
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "TMP",
            "TEMP",
            "PYTHONPYCACHEPREFIX",
        )
        declared = tuple(root.path.resolve(strict=False) for root in self.roots)
        for name in variables:
            value = os.environ.get(name)
            if not value:
                continue
            path = Path(value).resolve(strict=False)
            if any(path == root or path.is_relative_to(root) for root in declared):
                continue
            if any(
                path == prohibited or path.is_relative_to(prohibited)
                for prohibited in self.prohibited_environment_paths
            ):
                leaks.append(f"{name}:{stable_digest(path.as_posix())}")
        return tuple(sorted(leaks))


class IsolatedStateFactory:
    """Creates and validates the cache/store/index/artifact/build layout."""

    ROOT_KINDS = ("cache", "sqlite", "index", "artifacts", "build", "workspace")

    def __init__(self, parent: str | Path | None = None) -> None:
        self.parent = Path(parent).resolve(strict=False) if parent else None
        self.root: Path | None = None

    def create(self, *, prefix: str = "zyra-m3-regression-") -> Path:
        if self.root is not None:
            raise CleanStateError("isolated state root already exists")
        value = tempfile.mkdtemp(
            prefix=prefix,
            dir=str(self.parent) if self.parent else None,
        )
        self.root = Path(value).resolve()
        for kind in self.ROOT_KINDS:
            (self.root / kind).mkdir(parents=True, exist_ok=False)
        return self.root

    def state_roots(
        self,
        *,
        allowed: Mapping[str, Sequence[str]] | None = None,
    ) -> tuple[StateRoot, ...]:
        if self.root is None:
            raise CleanStateError("isolated state root has not been created")
        patterns = allowed or {}
        return tuple(
            StateRoot(
                root_id=kind,
                path=self.root / kind,
                kind=kind,
                allowed_outputs=tuple(patterns.get(kind, ())),
            )
            for kind in self.ROOT_KINDS
        )

    def environment(self) -> dict[str, str]:
        if self.root is None:
            raise CleanStateError("isolated state root has not been created")
        return {
            "ZYRA_STATE_ROOT": str(self.root),
            "ZYRA_CACHE_ROOT": str(self.root / "cache"),
            "ZYRA_INDEX_ROOT": str(self.root / "index"),
            "ZYRA_ARTIFACT_ROOT": str(self.root / "artifacts"),
            "ZYRA_BUILD_ROOT": str(self.root / "build"),
            "TMP": str(self.root / "cache"),
            "TEMP": str(self.root / "cache"),
            "PYTHONPYCACHEPREFIX": str(self.root / "cache" / "pycache"),
        }

    def cleanup(self) -> None:
        if self.root is None:
            return
        target = self.root.resolve(strict=False)
        if self.parent and not target.is_relative_to(self.parent):
            raise CleanStateError(f"refusing to clean unexpected state root: {target}")
        if target.name.startswith("zyra-m3-regression-") and target.is_dir():
            shutil.rmtree(target)
        self.root = None


__all__ = [
    "CleanStateError",
    "CleanStateGuard",
    "CleanStateReport",
    "IsolatedStateFactory",
    "StateDelta",
    "StateFile",
    "StateFingerprinter",
    "StateRoot",
    "StateSnapshot",
]
