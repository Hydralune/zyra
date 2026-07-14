from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .artifact_port import GatewayFileArtifactPort
from .backends import BackendSession
from .canonical import canonical_logical_path, content_digest, digest
from .errors import GatewayErrorCode, SandboxGatewayError
from .models import (
    GatewayPatchSet,
    PatchMutation,
    PatchOperation,
)


@dataclass(frozen=True, slots=True)
class TreeManifestEntry:
    logical_path: str
    content_digest: str
    size: int
    mode: int
    modified_ns: int
    file_identity: tuple[int, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_path": self.logical_path,
            "content_digest": self.content_digest,
            "size": self.size,
            "mode": self.mode,
            "modified_ns": self.modified_ns,
            "file_identity": list(self.file_identity),
        }


@dataclass(frozen=True, slots=True)
class TreeManifest:
    root_digest: str
    entries: Mapping[str, TreeManifestEntry]
    created_at_ns: int
    total_bytes: int
    manifest_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_digest": self.root_digest,
            "entries": {
                key: value.to_dict()
                for key, value in sorted(self.entries.items())
            },
            "created_at_ns": self.created_at_ns,
            "total_bytes": self.total_bytes,
            "manifest_digest": self.manifest_digest,
        }


@dataclass(frozen=True, slots=True)
class IsolationDelta:
    created: tuple[str, ...]
    modified: tuple[str, ...]
    deleted: tuple[str, ...]
    unchanged: tuple[str, ...]
    before_digest: str
    after_digest: str

    @property
    def changed(self) -> bool:
        return bool(self.created or self.modified or self.deleted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": list(self.created),
            "modified": list(self.modified),
            "deleted": list(self.deleted),
            "unchanged": list(self.unchanged),
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "changed": self.changed,
        }


class IsolationWorkspace:
    """Oh My Pi-derived dirty-baseline isolation for backend-owned roots."""

    def __init__(
        self,
        session: BackendSession,
        *,
        maximum_files: int = 20_000,
        maximum_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        self.session = session
        self.maximum_files = int(maximum_files)
        self.maximum_bytes = int(maximum_bytes)
        self._baseline: TreeManifest | None = None

    def stage(
        self,
        artifact_port: GatewayFileArtifactPort,
        logical_paths: Iterable[str],
    ) -> TreeManifest:
        for logical_path in logical_paths:
            canonical = canonical_logical_path(logical_path)
            content = artifact_port.read(canonical)
            target = self._resolve(canonical)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._assert_no_links(target.parent)
            target.write_bytes(content)
        self._baseline = self.capture()
        return self._baseline

    def capture(self) -> TreeManifest:
        root = self.session.execution_root.resolve()
        entries: dict[str, TreeManifestEntry] = {}
        total = 0
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise SandboxGatewayError(
                    GatewayErrorCode.LINK_REJECTED,
                    f"sandbox output contains a symbolic link: {relative}",
                    operation="isolation_capture",
                )
            if path.is_dir():
                continue
            if not path.is_file():
                raise SandboxGatewayError(
                    GatewayErrorCode.PATCH_REJECTED,
                    f"sandbox output contains a non-regular file: {relative}",
                    operation="isolation_capture",
                )
            if len(entries) >= self.maximum_files:
                raise SandboxGatewayError(
                    GatewayErrorCode.PATCH_REJECTED,
                    "sandbox output exceeds file count budget",
                    operation="isolation_capture",
                )
            content = path.read_bytes()
            total += len(content)
            if total > self.maximum_bytes:
                raise SandboxGatewayError(
                    GatewayErrorCode.PATCH_REJECTED,
                    "sandbox output exceeds byte budget",
                    operation="isolation_capture",
                )
            entries[relative] = TreeManifestEntry(
                logical_path=relative,
                content_digest=content_digest(content),
                size=len(content),
                mode=info.st_mode,
                modified_ns=info.st_mtime_ns,
                file_identity=(int(info.st_dev), int(info.st_ino)),
            )
        projected = {
            key: value.to_dict()
            for key, value in sorted(entries.items())
        }
        return TreeManifest(
            root_digest=digest({"path": str(root)}),
            entries=entries,
            created_at_ns=time_ns(),
            total_bytes=total,
            manifest_digest=digest({"entries": projected, "total_bytes": total}),
        )

    def delta(
        self,
        before: TreeManifest | None = None,
        after: TreeManifest | None = None,
    ) -> IsolationDelta:
        baseline = before or self._baseline
        if baseline is None:
            raise SandboxGatewayError(
                GatewayErrorCode.INVALID_STATE,
                "isolation baseline has not been captured",
                operation="isolation_delta",
            )
        current = after or self.capture()
        old_paths = set(baseline.entries)
        new_paths = set(current.entries)
        created = tuple(sorted(new_paths - old_paths))
        deleted = tuple(sorted(old_paths - new_paths))
        modified = tuple(
            sorted(
                path
                for path in old_paths & new_paths
                if baseline.entries[path].content_digest
                != current.entries[path].content_digest
            )
        )
        unchanged = tuple(sorted((old_paths & new_paths) - set(modified)))
        return IsolationDelta(
            created=created,
            modified=modified,
            deleted=deleted,
            unchanged=unchanged,
            before_digest=baseline.manifest_digest,
            after_digest=current.manifest_digest,
        )

    def build_patch(
        self,
        *,
        session_id: str,
        base_workspace_id: str,
        base_owner_epoch: int,
        idempotency_key: str,
        command_id: str,
        before: TreeManifest | None = None,
        after: TreeManifest | None = None,
        reason: str = "sandbox command delta",
        provenance_refs: Iterable[str] = (),
    ) -> tuple[IsolationDelta, GatewayPatchSet]:
        baseline = before or self._baseline
        if baseline is None:
            raise SandboxGatewayError(
                GatewayErrorCode.INVALID_STATE,
                "isolation baseline has not been captured",
                operation="isolation_build_patch",
            )
        current = after or self.capture()
        delta = self.delta(baseline, current)
        mutations: list[PatchMutation] = []
        for path in delta.created:
            mutations.append(
                PatchMutation.create(
                    PatchOperation.CREATE,
                    path,
                    content=self._resolve(path).read_bytes(),
                    metadata={"isolation_delta": "created"},
                )
            )
        for path in delta.modified:
            mutations.append(
                PatchMutation.create(
                    PatchOperation.EDIT,
                    path,
                    content=self._resolve(path).read_bytes(),
                    expected_previous_digest=baseline.entries[path].content_digest,
                    metadata={"isolation_delta": "modified"},
                )
            )
        for path in delta.deleted:
            mutations.append(
                PatchMutation.create(
                    PatchOperation.DELETE,
                    path,
                    expected_previous_digest=baseline.entries[path].content_digest,
                    metadata={"isolation_delta": "deleted"},
                )
            )
        patch_set = GatewayPatchSet.build(
            session_id=session_id,
            mutations=mutations,
            base_workspace_id=base_workspace_id,
            base_owner_epoch=base_owner_epoch,
            reason=reason,
            idempotency_key=idempotency_key,
            command_id=command_id,
            provenance_refs=provenance_refs,
            metadata={
                "isolation_before": baseline.manifest_digest,
                "isolation_after": current.manifest_digest,
                "delta": delta.to_dict(),
            },
        )
        return delta, patch_set

    def preserve_failure_patch(
        self,
        *,
        destination: Path,
        delta: IsolationDelta,
        patch_set: GatewayPatchSet,
    ) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / f"{patch_set.patch_set_id}.json"
        temporary = target.with_suffix(".tmp")
        import json

        temporary.write_text(
            json.dumps(
                {
                    "delta": delta.to_dict(),
                    "patch_set": patch_set.to_dict(),
                    "content_in_sandbox_root": True,
                    "execution_root_digest": digest(
                        {"path": str(self.session.execution_root)}
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
        return target

    def _resolve(self, logical_path: str) -> Path:
        canonical = canonical_logical_path(logical_path)
        root = self.session.execution_root.resolve()
        target = root.joinpath(*canonical.split("/")).resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError as error:
            raise SandboxGatewayError(
                GatewayErrorCode.PATH_ESCAPE,
                "isolation path escaped backend execution root",
                operation="isolation_path",
            ) from error
        return target

    @staticmethod
    def _assert_no_links(path: Path) -> None:
        current = path
        while current.parent != current:
            if current.exists() and current.is_symlink():
                raise SandboxGatewayError(
                    GatewayErrorCode.LINK_REJECTED,
                    "isolation staging path contains a symbolic link",
                    operation="isolation_stage",
                )
            current = current.parent


def time_ns() -> int:
    import time

    return time.time_ns()
