from __future__ import annotations

import os
import hashlib
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping

from .digests import digest_object
from .errors import SkillPathError
from .models import utc_now


class SkillFileChangeKind(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"
    TYPE_CHANGED = "type_changed"


@dataclass(frozen=True, slots=True)
class SkillFileIdentity:
    relative_path: str
    size: int
    modified_ns: int
    device: int
    inode: int
    is_symlink: bool
    content_digest: str

    @property
    def digest(self) -> str:
        return digest_object(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "size": self.size,
            "modified_ns": self.modified_ns,
            "device": self.device,
            "inode": self.inode,
            "is_symlink": self.is_symlink,
            "content_digest": self.content_digest,
        }


@dataclass(frozen=True, slots=True)
class SkillFileChange:
    root: str
    relative_path: str
    kind: SkillFileChangeKind
    previous: SkillFileIdentity | None
    current: SkillFileIdentity | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "relative_path": self.relative_path,
            "kind": str(self.kind),
            "previous": self.previous.to_dict() if self.previous else None,
            "current": self.current.to_dict() if self.current else None,
        }


@dataclass(frozen=True, slots=True)
class SkillChangeSet:
    generation: int
    changes: tuple[SkillFileChange, ...]
    stable: bool
    snapshot_digest: str
    previous_digest: str
    observed_at: str = field(default_factory=utc_now)

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    @property
    def reload_required(self) -> bool:
        return self.changed and self.stable

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "changes": [change.to_dict() for change in self.changes],
            "changed": self.changed,
            "stable": self.stable,
            "reload_required": self.reload_required,
            "snapshot_digest": self.snapshot_digest,
            "previous_digest": self.previous_digest,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class SkillChangeReloadResult:
    changes: SkillChangeSet
    reload: Any | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "changes": self.changes.to_dict(),
            "reload": self.reload.to_dict() if hasattr(self.reload, "to_dict") else self.reload,
        }


class SkillChangeDetector:
    """Polling change detector with stable-snapshot debounce.

    It does not execute a watcher thread. A caller explicitly polls at a
    session boundary, then the reload coordinator performs a complete
    validation and atomic registry swap. File content is hashed so timestamp
    preservation cannot hide a change. Symlink changes are surfaced but never
    followed.
    """

    def __init__(
        self,
        roots: Iterable[str | Path],
        *,
        stable_window_seconds: float = 0.05,
        max_files: int = 20_000,
    ) -> None:
        if stable_window_seconds < 0:
            raise ValueError("skill change stable window cannot be negative")
        if max_files < 1:
            raise ValueError("skill change file limit must be positive")
        self.roots = tuple(sorted({str(Path(root).resolve()) for root in roots if str(root)}))
        self.stable_window_seconds = stable_window_seconds
        self.max_files = max_files
        self._lock = RLock()
        self._accepted: dict[str, SkillFileIdentity] = {}
        self._accepted_digest = digest_object({})
        self._candidate_digest = ""
        self._candidate_since = 0.0
        self._generation = 0

    def capture(self) -> SkillChangeSet:
        observed = self._scan()
        digest = self._snapshot_digest(observed)
        with self._lock:
            self._accepted = observed
            self._accepted_digest = digest
            self._candidate_digest = ""
            self._candidate_since = 0.0
            self._generation += 1
            return SkillChangeSet(
                generation=self._generation,
                changes=(),
                stable=True,
                snapshot_digest=digest,
                previous_digest=digest,
            )

    def poll(self, *, now: float | None = None) -> SkillChangeSet:
        observed = self._scan()
        digest = self._snapshot_digest(observed)
        clock = time.monotonic() if now is None else float(now)
        with self._lock:
            changes = self._diff(self._accepted, observed)
            if not changes:
                self._candidate_digest = ""
                self._candidate_since = 0.0
                return SkillChangeSet(
                    generation=self._generation,
                    changes=(),
                    stable=True,
                    snapshot_digest=digest,
                    previous_digest=self._accepted_digest,
                )
            if digest != self._candidate_digest:
                self._candidate_digest = digest
                self._candidate_since = clock
            stable = clock - self._candidate_since >= self.stable_window_seconds
            return SkillChangeSet(
                generation=self._generation,
                changes=changes,
                stable=stable,
                snapshot_digest=digest,
                previous_digest=self._accepted_digest,
            )

    def acknowledge(self, changes: SkillChangeSet) -> SkillChangeSet:
        if not changes.reload_required:
            raise SkillPathError("only a stable skill change set can be acknowledged")
        observed = self._scan()
        digest = self._snapshot_digest(observed)
        if digest != changes.snapshot_digest:
            raise SkillPathError(
                "skill files changed again before reload acknowledgement",
                detail={"expected": changes.snapshot_digest, "actual": digest},
            )
        with self._lock:
            self._accepted = observed
            self._accepted_digest = digest
            self._candidate_digest = ""
            self._candidate_since = 0.0
            self._generation += 1
            return SkillChangeSet(
                generation=self._generation,
                changes=changes.changes,
                stable=True,
                snapshot_digest=digest,
                previous_digest=changes.previous_digest,
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": 1,
                "roots": list(self.roots),
                "generation": self._generation,
                "accepted_digest": self._accepted_digest,
                "files": {key: value.to_dict() for key, value in self._accepted.items()},
                "content_included": False,
            }

    def _scan(self) -> dict[str, SkillFileIdentity]:
        result: dict[str, SkillFileIdentity] = {}
        for root_text in self.roots:
            root = Path(root_text)
            if not root.exists():
                continue
            if root.is_symlink():
                raise SkillPathError("skill change detector root cannot be a symlink", detail={"root": root_text})
            for directory, names, files in os.walk(root, followlinks=False):
                retained_names: list[str] = []
                for name in sorted(names):
                    path = Path(directory) / name
                    if not path.is_symlink():
                        retained_names.append(name)
                        continue
                    relative = path.relative_to(root).as_posix()
                    key = f"{root_text}::{relative}"
                    stat = path.lstat()
                    result[key] = SkillFileIdentity(
                        relative_path=relative,
                        size=int(stat.st_size),
                        modified_ns=int(stat.st_mtime_ns),
                        device=int(stat.st_dev),
                        inode=int(stat.st_ino),
                        is_symlink=True,
                        content_digest="symlink-directory",
                    )
                names[:] = retained_names
                for name in sorted(files):
                    path = Path(directory) / name
                    relative = path.relative_to(root).as_posix()
                    key = f"{root_text}::{relative}"
                    stat = path.lstat()
                    content_digest = "symlink"
                    if not path.is_symlink():
                        digest = hashlib.sha256()
                        with path.open("rb") as handle:
                            for chunk in iter(lambda: handle.read(128 * 1024), b""):
                                digest.update(chunk)
                        content_digest = digest.hexdigest()
                    result[key] = SkillFileIdentity(
                        relative_path=relative,
                        size=int(stat.st_size),
                        modified_ns=int(stat.st_mtime_ns),
                        device=int(stat.st_dev),
                        inode=int(stat.st_ino),
                        is_symlink=path.is_symlink(),
                        content_digest=content_digest,
                    )
                    if len(result) > self.max_files:
                        raise SkillPathError(
                            "skill change detector file limit exceeded",
                            detail={"limit": self.max_files},
                        )
        return result

    @staticmethod
    def _snapshot_digest(snapshot: Mapping[str, SkillFileIdentity]) -> str:
        return digest_object({key: value.to_dict() for key, value in sorted(snapshot.items())})

    @staticmethod
    def _diff(
        previous: Mapping[str, SkillFileIdentity],
        current: Mapping[str, SkillFileIdentity],
    ) -> tuple[SkillFileChange, ...]:
        changes: list[SkillFileChange] = []
        for key in sorted(set(previous) | set(current)):
            before = previous.get(key)
            after = current.get(key)
            root, relative = key.split("::", 1)
            if before is None:
                kind = SkillFileChangeKind.ADDED
            elif after is None:
                kind = SkillFileChangeKind.REMOVED
            elif before.is_symlink != after.is_symlink:
                kind = SkillFileChangeKind.TYPE_CHANGED
            elif before != after:
                kind = SkillFileChangeKind.MODIFIED
            else:
                continue
            changes.append(
                SkillFileChange(
                    root=root,
                    relative_path=relative,
                    kind=kind,
                    previous=before,
                    current=after,
                )
            )
        return tuple(changes)
