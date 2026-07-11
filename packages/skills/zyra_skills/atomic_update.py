from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from .digests import digest_object
from .errors import SkillReloadRejected, SkillRollbackRejected
from .models import SkillSourceKind, utc_now
from .path_security import canonical_root, iter_skill_directories
from .sources.filesystem import FilesystemSkillSource


@dataclass(frozen=True, slots=True)
class SkillUpdateReceipt:
    update_id: str
    source_root: str
    target_root: str
    backup_root: str
    generation: int
    installed_refs: tuple[str, ...]
    previous_refs: tuple[str, ...]
    rolled_back: bool
    transaction_digest: str
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "update_id": self.update_id,
            "source_root": self.source_root,
            "target_root": self.target_root,
            "backup_root": self.backup_root,
            "generation": self.generation,
            "installed_refs": list(self.installed_refs),
            "previous_refs": list(self.previous_refs),
            "rolled_back": self.rolled_back,
            "transaction_digest": self.transaction_digest,
            "created_at": self.created_at,
        }


class AtomicSkillPackageUpdater:
    """Local staging -> validation -> backup -> atomic rename -> rollback.

    This foundation updater deliberately accepts only a local, already
    materialized source directory. Network fetch, marketplace resolution, npm
    install, and remote authentication remain deferred. The updater is useful
    for product-owned/project/plugin cache refresh without partial publication.
    """

    def __init__(self, *, staging_root: str | Path, backup_root: str | Path) -> None:
        self.staging_root = Path(staging_root).resolve()
        self.backup_root = Path(backup_root).resolve()
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._generation = 0
        self._receipts: dict[str, SkillUpdateReceipt] = {}

    def install(
        self,
        *,
        source_root: str | Path,
        target_root: str | Path,
        source_kind: SkillSourceKind,
        source_id: str,
        namespace: str,
    ) -> SkillUpdateReceipt:
        source = canonical_root(source_root)
        target = Path(target_root).resolve()
        if target == self.staging_root or target == self.backup_root:
            raise SkillReloadRejected("skill update target cannot be the staging or backup root")
        update_id = digest_object(
            {
                "source": str(source),
                "target": str(target),
                "generation": self._generation + 1,
                "time": utc_now(),
            }
        )[:24]
        staged = self.staging_root / update_id
        backup = self.backup_root / update_id
        if staged.exists() or backup.exists():
            raise SkillReloadRejected("skill update transaction path collision")
        _copy_package_tree(source, staged)
        staged_scan = FilesystemSkillSource(
            root=staged,
            source_kind=source_kind,
            source_id=source_id,
            namespace=namespace,
            strict=True,
        ).scan(generation=self._generation + 1)
        if not staged_scan.ok:
            shutil.rmtree(staged, ignore_errors=True)
            raise SkillReloadRejected(
                "staged skill package validation failed",
                detail={"errors": list(staged_scan.errors)},
            )
        previous_refs: tuple[str, ...] = ()
        if target.exists():
            previous_scan = FilesystemSkillSource(
                root=target,
                source_kind=source_kind,
                source_id=source_id,
                namespace=namespace,
                strict=True,
            ).scan(generation=self._generation)
            if previous_scan.ok:
                previous_refs = tuple(item.version_ref.immutable_ref for item in previous_scan.revisions)
        with self._lock:
            self._generation += 1
            moved_old = False
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    os.replace(target, backup)
                    moved_old = True
                os.replace(staged, target)
                installed_refs = tuple(item.version_ref.immutable_ref for item in staged_scan.revisions)
                payload = {
                    "update_id": update_id,
                    "source": str(source),
                    "target": str(target),
                    "backup": str(backup) if moved_old else "",
                    "generation": self._generation,
                    "installed_refs": installed_refs,
                    "previous_refs": previous_refs,
                }
                receipt = SkillUpdateReceipt(
                    update_id=update_id,
                    source_root=str(source),
                    target_root=str(target),
                    backup_root=str(backup) if moved_old else "",
                    generation=self._generation,
                    installed_refs=installed_refs,
                    previous_refs=previous_refs,
                    rolled_back=False,
                    transaction_digest=digest_object(payload),
                )
                self._receipts[update_id] = receipt
                return receipt
            except Exception as error:
                if target.exists() and moved_old:
                    shutil.rmtree(target, ignore_errors=True)
                if moved_old and backup.exists():
                    os.replace(backup, target)
                shutil.rmtree(staged, ignore_errors=True)
                raise SkillReloadRejected("atomic skill package update failed and was rolled back") from error

    def rollback(self, update_id: str) -> SkillUpdateReceipt:
        with self._lock:
            receipt = self._receipts.get(update_id)
            if receipt is None:
                raise SkillRollbackRejected("skill update receipt was not found")
            if receipt.rolled_back:
                return receipt
            if not receipt.backup_root:
                raise SkillRollbackRejected("skill update has no previous package to restore")
            target = Path(receipt.target_root)
            backup = Path(receipt.backup_root)
            if not backup.exists():
                raise SkillRollbackRejected("skill update backup no longer exists")
            displaced = self.staging_root / f"rollback-{update_id}"
            if displaced.exists():
                shutil.rmtree(displaced)
            if target.exists():
                os.replace(target, displaced)
            try:
                os.replace(backup, target)
            except Exception as error:
                if displaced.exists():
                    os.replace(displaced, target)
                raise SkillRollbackRejected("skill package rollback failed") from error
            shutil.rmtree(displaced, ignore_errors=True)
            rolled_back = SkillUpdateReceipt(
                update_id=receipt.update_id,
                source_root=receipt.source_root,
                target_root=receipt.target_root,
                backup_root=receipt.backup_root,
                generation=receipt.generation,
                installed_refs=receipt.installed_refs,
                previous_refs=receipt.previous_refs,
                rolled_back=True,
                transaction_digest=receipt.transaction_digest,
                created_at=receipt.created_at,
            )
            self._receipts[update_id] = rolled_back
            return rolled_back


def _copy_package_tree(source: Path, target: Path) -> None:
    iter_skill_directories(source)

    def reject_links(path: str, names: list[str]) -> list[str]:
        root = Path(path)
        for name in names:
            candidate = root / name
            if candidate.is_symlink():
                raise SkillReloadRejected(
                    "symlink is forbidden in staged skill package",
                    detail={"path": str(candidate)},
                )
        return []

    shutil.copytree(source, target, symlinks=False, ignore=reject_links)
