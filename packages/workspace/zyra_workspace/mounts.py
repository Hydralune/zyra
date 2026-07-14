from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import WorkspaceError, WorkspaceErrorCode
from .models import (
    WorkspaceKind,
    WorkspaceMount,
    WorkspaceMountAccess,
    WorkspaceOperation,
    WorkspaceQuota,
    new_workspace_id,
)


DEFAULT_MOUNT_LAYOUT = {
    WorkspaceKind.TASK: "task",
    WorkspaceKind.ARTIFACT: "artifact",
    WorkspaceKind.DOWNLOAD: "download",
    WorkspaceKind.TEMP: "temp",
}


@dataclass(frozen=True, slots=True)
class WorkspaceMountLayout:
    workspace_id: str
    mounts: tuple[WorkspaceMount, ...]

    def require(self, kind: WorkspaceKind) -> WorkspaceMount:
        for mount in self.mounts:
            if mount.kind is kind:
                return mount
        raise WorkspaceError(
            WorkspaceErrorCode.NOT_FOUND,
            "Workspace mount kind was not found.",
            workspace_id=self.workspace_id,
            operation="resolve_mount",
            actual=kind.value,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "mounts": [item.to_dict() for item in self.mounts],
        }


class WorkspaceArtifactMount:
    """Four-layer mount policy without taking artifact byte ownership."""

    def build_default_layout(
        self,
        *,
        workspace_id: str,
        owner_epoch: int,
        task_quota: WorkspaceQuota,
        artifact_quota: WorkspaceQuota | None = None,
        download_quota: WorkspaceQuota | None = None,
        temp_quota: WorkspaceQuota | None = None,
    ) -> WorkspaceMountLayout:
        mounts = (
            WorkspaceMount(
                mount_id=new_workspace_id("mount"),
                workspace_id=workspace_id,
                kind=WorkspaceKind.TASK,
                relative_root=DEFAULT_MOUNT_LAYOUT[WorkspaceKind.TASK],
                access=WorkspaceMountAccess.READ_WRITE,
                owner_epoch=owner_epoch,
                quota=task_quota,
                metadata={"exec_allowed": True, "canonical_artifact_owner": False},
            ),
            WorkspaceMount(
                mount_id=new_workspace_id("mount"),
                workspace_id=workspace_id,
                kind=WorkspaceKind.ARTIFACT,
                relative_root=DEFAULT_MOUNT_LAYOUT[WorkspaceKind.ARTIFACT],
                access=WorkspaceMountAccess.READ_ONLY,
                owner_epoch=owner_epoch,
                quota=artifact_quota or task_quota,
                metadata={
                    "exec_allowed": False,
                    "canonical_artifact_owner": "LocalArtifactStore",
                    "workspace_stores_refs_only": True,
                },
            ),
            WorkspaceMount(
                mount_id=new_workspace_id("mount"),
                workspace_id=workspace_id,
                kind=WorkspaceKind.DOWNLOAD,
                relative_root=DEFAULT_MOUNT_LAYOUT[WorkspaceKind.DOWNLOAD],
                access=WorkspaceMountAccess.APPEND_ONLY,
                owner_epoch=owner_epoch,
                quota=download_quota or task_quota,
                metadata={"exec_allowed": False, "quarantine_required": True, "artifact_import_required": True},
            ),
            WorkspaceMount(
                mount_id=new_workspace_id("mount"),
                workspace_id=workspace_id,
                kind=WorkspaceKind.TEMP,
                relative_root=DEFAULT_MOUNT_LAYOUT[WorkspaceKind.TEMP],
                access=WorkspaceMountAccess.READ_WRITE,
                owner_epoch=owner_epoch,
                quota=temp_quota or task_quota,
                metadata={"exec_allowed": False, "canonical_artifact_allowed": False, "cleanup_with_lease": True},
            ),
        )
        return WorkspaceMountLayout(workspace_id=workspace_id, mounts=mounts)

    def validate_layout(self, layout: WorkspaceMountLayout) -> None:
        expected = set(WorkspaceKind)
        actual = {item.kind for item in layout.mounts}
        if actual != expected:
            raise WorkspaceError(
                WorkspaceErrorCode.MOUNT_BOUNDARY_VIOLATION,
                "Workspace mount layout must define task, artifact, download, and temp roots exactly once.",
                workspace_id=layout.workspace_id,
                operation="validate_mount_layout",
                expected=sorted(item.value for item in expected),
                actual=sorted(item.value for item in actual),
            )
        roots = [item.relative_root.casefold() for item in layout.mounts]
        if len(set(roots)) != len(roots):
            raise WorkspaceError(
                WorkspaceErrorCode.MOUNT_BOUNDARY_VIOLATION,
                "Workspace mount roots collide.",
                workspace_id=layout.workspace_id,
                operation="validate_mount_layout",
            )
        for left in layout.mounts:
            left_parts = tuple(Path(left.relative_root).parts)
            for right in layout.mounts:
                if left.mount_id == right.mount_id:
                    continue
                right_parts = tuple(Path(right.relative_root).parts)
                if left_parts[: len(right_parts)] == right_parts or right_parts[: len(left_parts)] == left_parts:
                    raise WorkspaceError(
                        WorkspaceErrorCode.MOUNT_BOUNDARY_VIOLATION,
                        "Workspace mount roots cannot be nested.",
                        workspace_id=layout.workspace_id,
                        operation="validate_mount_layout",
                    )

    @staticmethod
    def authorize_service_operation(
        mount: WorkspaceMount,
        operation: WorkspaceOperation,
        *,
        service: str,
    ) -> None:
        if mount.kind is WorkspaceKind.ARTIFACT and operation is WorkspaceOperation.WRITE and service != "artifact-store":
            raise WorkspaceError(
                WorkspaceErrorCode.MOUNT_BOUNDARY_VIOLATION,
                "Only the canonical artifact service may populate artifact mounts.",
                workspace_id=mount.workspace_id,
                operation=operation.value,
                path=mount.relative_root,
            )
        if mount.kind is WorkspaceKind.DOWNLOAD and operation is WorkspaceOperation.WRITE and service not in {
            "browser-download",
            "download-quarantine",
        }:
            raise WorkspaceError(
                WorkspaceErrorCode.MOUNT_BOUNDARY_VIOLATION,
                "Only the browser download pipeline may populate download mounts.",
                workspace_id=mount.workspace_id,
                operation=operation.value,
                path=mount.relative_root,
            )
        if mount.kind is WorkspaceKind.TEMP and service == "artifact-store":
            raise WorkspaceError(
                WorkspaceErrorCode.MOUNT_BOUNDARY_VIOLATION,
                "Temporary workspace files must be imported before becoming canonical artifacts.",
                workspace_id=mount.workspace_id,
                operation=operation.value,
                path=mount.relative_root,
            )

    @staticmethod
    def materialize_directories(root: str | Path, mounts: Iterable[WorkspaceMount]) -> tuple[Path, ...]:
        base = Path(root)
        created: list[Path] = []
        for mount in mounts:
            path = base / mount.relative_root
            path.mkdir(parents=True, exist_ok=True)
            created.append(path)
        return tuple(created)
