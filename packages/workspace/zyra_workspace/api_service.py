from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import WorkspaceError, WorkspaceErrorCode
from .manager import WorkspaceManagerRuntime
from .models import WorkspaceKind, WorkspaceOperation, WorkspaceReadMode
from .rebind import WorkspaceRebindRuntime
from .transactions import WorkspaceEditPort


@dataclass(frozen=True, slots=True)
class WorkspaceApiResponse:
    status: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]

    @classmethod
    def ok(cls, body: Mapping[str, Any], *, status: int = 200) -> "WorkspaceApiResponse":
        return cls(
            status=status,
            body=dict(body),
            headers={"Cache-Control": "no-store", "Content-Type": "application/json"},
        )


class WorkspaceApiService:
    """Framework-neutral API boundary with opaque workspace ids and no host paths."""

    def __init__(self, manager: WorkspaceManagerRuntime) -> None:
        self.manager = manager

    def list_workspaces(self, query: Mapping[str, Any] | None = None) -> WorkspaceApiResponse:
        values = query or {}
        projections = self.manager.list_projections(
            run_id=str(values.get("run_id") or ""),
            task_id=str(values.get("task_id") or ""),
            include_deleted=_as_bool(values.get("include_deleted"), default=False),
        )
        return WorkspaceApiResponse.ok(
            {
                "workspaces": [item.to_dict() for item in projections],
                "count": len(projections),
                "state_owner": "WorkspaceManagerRuntime+WorkspaceBindingStore",
                "physical_locations_redacted": True,
            }
        )

    def get_workspace(self, workspace_id: str) -> WorkspaceApiResponse:
        projection = self.manager.project(workspace_id)
        return WorkspaceApiResponse.ok(
            {
                "workspace": projection.to_dict(),
                "receipts": [item.to_dict() for item in self.manager.store.list_receipts(workspace_id, limit=100)],
                "snapshots": [
                    _snapshot_projection(item)
                    for item in self.manager.store.list_snapshots(workspace_id)
                ],
                "recoveries": [
                    item.to_dict()
                    for item in self.manager.store.list_recoveries(workspace_id)
                ],
                "integration": self.manager.integration_store.workspace_summary(workspace_id),
                "integration_receipts": list(
                    self.manager.integration_store.list_receipts(workspace_id)
                )[-100:],
                "merge_conflicts": [
                    item.to_dict()
                    for item in self.manager.integration_store.list_conflicts(workspace_id)
                ],
                "recovery_inputs": [
                    item.to_dict()
                    for item in self.manager.integration_store.list_recovery_inputs(workspace_id)
                ],
            }
        )

    def get_health(self) -> WorkspaceApiResponse:
        health = self.manager.health()
        return WorkspaceApiResponse.ok(health, status=200 if health.get("ok") else 503)

    def list_files(
        self,
        workspace_id: str,
        query: Mapping[str, Any] | None = None,
    ) -> WorkspaceApiResponse:
        values = query or {}
        handle = self.manager.observe_current(
            workspace_id,
            operations=(WorkspaceOperation.LIST,),
        )
        mount_kind = _mount_kind(values.get("mount"))
        entries = self.manager.backend.list_directory(
            handle,
            mount_kind=mount_kind,
            path=str(values.get("path") or "."),
            maximum_entries=_bounded_int(values.get("limit"), default=1000, minimum=1, maximum=10_000),
        )
        return WorkspaceApiResponse.ok(
            {
                "workspace_id": workspace_id,
                "mount": mount_kind.value,
                "path": str(values.get("path") or "."),
                "entries": [item.to_dict() for item in entries],
                "count": len(entries),
                "physical_location_redacted": True,
            }
        )

    def read_file(
        self,
        workspace_id: str,
        query: Mapping[str, Any],
    ) -> WorkspaceApiResponse:
        path = _required_string(query, "path")
        handle = self.manager.observe_current(
            workspace_id,
            operations=(WorkspaceOperation.READ,),
        )
        mode = WorkspaceReadMode(str(query.get("mode") or WorkspaceReadMode.FULL.value))
        length_value = query.get("length")
        result = self.manager.backend.read(
            handle,
            mount_kind=_mount_kind(query.get("mount")),
            path=path,
            mode=mode,
            start=_bounded_int(query.get("start"), default=0, minimum=0, maximum=2**63 - 1),
            length=(
                _bounded_int(length_value, default=0, minimum=0, maximum=64 * 1024 * 1024)
                if length_value is not None
                else None
            ),
        )
        encoding = str(query.get("encoding") or "base64").casefold()
        if encoding == "utf-8":
            try:
                content: Any = result.content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise WorkspaceError(
                    WorkspaceErrorCode.INVALID_ARGUMENT,
                    "The selected workspace file is not valid UTF-8.",
                    workspace_id=workspace_id,
                    operation="api_read_file",
                    path=path,
                ) from error
        elif encoding == "base64":
            content = base64.b64encode(result.content).decode("ascii")
        else:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace file API encoding must be utf-8 or base64.",
                workspace_id=workspace_id,
                operation="api_read_file",
                path=path,
                actual=encoding,
            )
        return WorkspaceApiResponse.ok(
            {
                "workspace_id": workspace_id,
                "path": path,
                "encoding": encoding,
                "content": content,
                "read": result.to_public_dict(),
                "write_precondition_available": result.record.fully_read or result.record.file_identity == "absent",
                "physical_location_redacted": True,
            }
        )

    def write_file(self, workspace_id: str, payload: Mapping[str, Any]) -> WorkspaceApiResponse:
        path = _required_string(payload, "path")
        content = _decode_content(payload)
        binding = self.manager.store.require_binding(workspace_id)
        handle = self.manager.acquire_for_worker(
            task_id=binding.task_id,
            session_id=binding.session_id,
            worker_id="workspace-api",
        )
        port = WorkspaceEditPort(
            self.manager,
            handle,
            worker_id="workspace-api",
            run_id=binding.run_id,
            task_id=binding.task_id,
        )
        mount_kind = _mount_kind(payload.get("mount"))
        result = port.write_bytes(
            path,
            content,
            mount_kind=mount_kind,
            publish_artifact=False,
            idempotency_key=str(payload.get("idempotency_key") or ""),
            causation_id=str(payload.get("causation_id") or ""),
        )
        path_result = result.transaction.path_results[-1]
        response = WorkspaceApiResponse.ok(
            {
                "write": {
                    "workspace_id": workspace_id,
                    "mount_kind": mount_kind.value,
                    "path": path,
                    "bytes_written": path_result.bytes_after,
                    "prior_bytes": path_result.bytes_before,
                    "created": path_result.disposition in {"created", "download_externalized", "temp_externalized"},
                    "content_hash": path_result.after_hash,
                    "owner_epoch": result.access.owner_epoch,
                    "transaction_id": result.transaction.transaction_id,
                    "receipt_id": result.receipt.receipt_id,
                },
                "workspace_access": result.access.to_public_dict(),
                "physical_location_redacted": True,
            },
            status=201 if path_result.disposition == "created" else 200,
        )
        return WorkspaceApiResponse(
            status=response.status,
            body=response.body,
            headers={
                **response.headers,
                # Typed transport receipt identities use the public receipt_
                # namespace; preserve the durable workspace receipt as the
                # suffix so retries remain traceable to the canonical commit.
                "X-Zyra-Receipt-Id": f"receipt_{result.receipt.receipt_id}",
            },
        )

    def rebind(self, workspace_id: str, payload: Mapping[str, Any]) -> WorkspaceApiResponse:
        target_endpoint_id = _required_string(payload, "target_endpoint_id")
        runtime = WorkspaceRebindRuntime(self.manager)
        target_relative_root = str(payload.get("target_relative_root") or "").strip()
        if target_relative_root:
            runtime.register_endpoint(
                target_endpoint_id,
                relative_root=target_relative_root,
                capability_revision=_bounded_int(
                    payload.get("capability_revision"),
                    default=1,
                    minimum=1,
                    maximum=2**31 - 1,
                ),
            )
        binding = self.manager.store.require_binding(workspace_id)
        access = self.manager.acquire_for_worker(
            task_id=binding.task_id,
            session_id=binding.session_id,
            worker_id="workspace-api",
        )
        result = runtime.rebind(
            access,
            target_endpoint_id=target_endpoint_id,
            worker_id="workspace-api",
            idempotency_key=str(payload.get("idempotency_key") or ""),
            causation_id=str(payload.get("causation_id") or ""),
            artifact_refs=tuple(str(item) for item in payload.get("artifact_refs") or ()),
            event_refs=tuple(str(item) for item in payload.get("event_refs") or ()),
        )
        return WorkspaceApiResponse.ok(result.to_public_dict())

    def snapshot(self, workspace_id: str, payload: Mapping[str, Any] | None = None) -> WorkspaceApiResponse:
        values = payload or {}
        snapshot = self.manager.snapshot(
            workspace_id,
            causation_id=str(values.get("causation_id") or ""),
            include_dirty_state=_as_bool(values.get("include_dirty_state"), default=True),
        )
        return WorkspaceApiResponse.ok(
            {
                "snapshot": _snapshot_projection(snapshot),
                "workspace": self.manager.project(workspace_id).to_dict(),
            },
            status=201,
        )

    def restore(self, workspace_id: str, payload: Mapping[str, Any]) -> WorkspaceApiResponse:
        snapshot_id = _required_string(payload, "snapshot_id")
        result = self.manager.restore(
            workspace_id,
            snapshot_id,
            causation_id=str(payload.get("causation_id") or ""),
        )
        return WorkspaceApiResponse.ok(
            {
                "restore": result.to_public_dict(),
                "workspace": self.manager.project(workspace_id).to_dict(),
            }
        )

    def cleanup(self, workspace_id: str, payload: Mapping[str, Any] | None = None) -> WorkspaceApiResponse:
        values = payload or {}
        receipt = self.manager.cleanup(
            workspace_id,
            causation_id=str(values.get("causation_id") or ""),
            archive=_as_bool(values.get("archive"), default=True),
        )
        return WorkspaceApiResponse.ok(
            {
                "receipt": receipt.to_dict(),
                "workspace": self.manager.project(workspace_id).to_dict(),
            }
        )

    def route_get(self, path_parts: tuple[str, ...], query: Mapping[str, Any]) -> WorkspaceApiResponse | None:
        if path_parts == ("workspaces",):
            return self.list_workspaces(query)
        if path_parts == ("workspaces", "health"):
            return self.get_health()
        if len(path_parts) == 2 and path_parts[0] == "workspaces":
            return self.get_workspace(path_parts[1])
        if len(path_parts) == 3 and path_parts[0] == "workspaces" and path_parts[2] == "files":
            if query.get("path") and str(query.get("read") or "").casefold() in {"1", "true", "yes"}:
                return self.read_file(path_parts[1], query)
            return self.list_files(path_parts[1], query)
        return None

    def route_post(
        self,
        path_parts: tuple[str, ...],
        payload: Mapping[str, Any],
    ) -> WorkspaceApiResponse | None:
        if len(path_parts) != 3 or path_parts[0] != "workspaces":
            return None
        workspace_id = path_parts[1]
        action = path_parts[2]
        if action == "files":
            return self.write_file(workspace_id, payload)
        if action == "snapshot":
            return self.snapshot(workspace_id, payload)
        if action == "restore":
            return self.restore(workspace_id, payload)
        if action == "cleanup":
            return self.cleanup(workspace_id, payload)
        if action == "rebind":
            return self.rebind(workspace_id, payload)
        return None


def workspace_error_status(error: WorkspaceError) -> int:
    code = error.detail.code
    if code in {
        WorkspaceErrorCode.INVALID_ARGUMENT,
        WorkspaceErrorCode.INVALID_IDENTIFIER,
        WorkspaceErrorCode.INVALID_LOCATION,
        WorkspaceErrorCode.PATH_TRAVERSAL,
        WorkspaceErrorCode.ABSOLUTE_PATH_REJECTED,
        WorkspaceErrorCode.UNC_PATH_REJECTED,
        WorkspaceErrorCode.DEVICE_PATH_REJECTED,
        WorkspaceErrorCode.RESERVED_NAME_REJECTED,
        WorkspaceErrorCode.READ_REQUIRED,
        WorkspaceErrorCode.READ_INCOMPLETE,
    }:
        return 400
    if code in {WorkspaceErrorCode.NOT_FOUND, WorkspaceErrorCode.SNAPSHOT_NOT_FOUND}:
        return 404
    if code in {
        WorkspaceErrorCode.BINDING_STALE,
        WorkspaceErrorCode.STORE_REVISION_CONFLICT,
        WorkspaceErrorCode.OWNER_EPOCH_STALE,
        WorkspaceErrorCode.READ_EPOCH_STALE,
        WorkspaceErrorCode.BASE_HASH_STALE,
        WorkspaceErrorCode.BASE_MTIME_STALE,
        WorkspaceErrorCode.FILE_IDENTITY_CHANGED,
        WorkspaceErrorCode.IDEMPOTENCY_CONFLICT,
        WorkspaceErrorCode.DIRTY_STATE_CONFLICT,
        WorkspaceErrorCode.RESTORE_CONFLICT,
        WorkspaceErrorCode.CLEANUP_CONFLICT,
    }:
        return 409
    if code in {
        WorkspaceErrorCode.LEASE_EXPIRED,
        WorkspaceErrorCode.LEASE_REVOKED,
        WorkspaceErrorCode.LEASE_OWNER_MISMATCH,
        WorkspaceErrorCode.FENCE_TOKEN_MISMATCH,
        WorkspaceErrorCode.MOUNT_BOUNDARY_VIOLATION,
        WorkspaceErrorCode.SUBMISSION_BOUNDARY_VIOLATION,
        WorkspaceErrorCode.PATH_OUTSIDE_ROOT,
        WorkspaceErrorCode.SYMLINK_ESCAPE,
        WorkspaceErrorCode.REPARSE_POINT_ESCAPE,
    }:
        return 403
    if code in {
        WorkspaceErrorCode.QUOTA_EXCEEDED,
        WorkspaceErrorCode.FILE_COUNT_EXCEEDED,
        WorkspaceErrorCode.SINGLE_FILE_LIMIT_EXCEEDED,
    }:
        return 413
    if code in {WorkspaceErrorCode.DISABLED, WorkspaceErrorCode.BACKEND_UNAVAILABLE}:
        return 503
    if code in {WorkspaceErrorCode.WRITE_FROZEN, WorkspaceErrorCode.OPERATION_IN_PROGRESS}:
        return 423
    return 500


def workspace_error_response(error: WorkspaceError) -> WorkspaceApiResponse:
    return WorkspaceApiResponse(
        status=workspace_error_status(error),
        body={
            "error": error.detail.to_dict(),
            "state_owner": "WorkspaceManagerRuntime+WorkspaceBindingStore",
            "physical_location_redacted": True,
        },
        headers={"Cache-Control": "no-store", "Content-Type": "application/json"},
    )


def _snapshot_projection(snapshot: Any) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "workspace_id": snapshot.workspace_id,
        "owner_epoch": snapshot.owner_epoch,
        "state": snapshot.state.value,
        "manifest_hash": snapshot.manifest_hash,
        "root_tree_hash": snapshot.root_tree_hash,
        "parent_snapshot_id": snapshot.parent_snapshot_id,
        "baseline_id": snapshot.baseline_id,
        "created_at": snapshot.created_at,
        "committed_at": snapshot.committed_at,
        "total_bytes": snapshot.total_bytes,
        "file_count": snapshot.file_count,
        "directory_count": snapshot.directory_count,
        "dirty_path_count": len(snapshot.dirty_paths),
        "repository_count": len(snapshot.repository_refs),
        "entry_content_redacted": True,
    }


def _required_string(value: Mapping[str, Any], name: str) -> str:
    selected = str(value.get(name) or "").strip()
    if not selected:
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_ARGUMENT,
            f"Workspace API field '{name}' is required.",
            operation="workspace_api_validate",
            metadata={"field": name},
        )
    return selected


def _mount_kind(value: Any) -> WorkspaceKind:
    selected = str(value or WorkspaceKind.TASK.value)
    try:
        return WorkspaceKind(selected)
    except ValueError as error:
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_ARGUMENT,
            "The workspace mount kind is not supported.",
            operation="workspace_api_validate",
            actual=selected,
        ) from error


def _decode_content(payload: Mapping[str, Any]) -> bytes:
    encoding = str(payload.get("encoding") or "utf-8").casefold()
    content = payload.get("content")
    if not isinstance(content, str):
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_ARGUMENT,
            "Workspace API file content must be a string.",
            operation="workspace_api_validate",
        )
    if encoding == "utf-8":
        return content.encode("utf-8")
    if encoding == "base64":
        try:
            return base64.b64decode(content.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as error:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace API base64 content is invalid.",
                operation="workspace_api_validate",
            ) from error
    raise WorkspaceError(
        WorkspaceErrorCode.INVALID_ARGUMENT,
        "Workspace API file encoding must be utf-8 or base64.",
        operation="workspace_api_validate",
        actual=encoding,
    )


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    selected = str(value).strip().casefold()
    if selected in {"1", "true", "yes", "on"}:
        return True
    if selected in {"0", "false", "no", "off"}:
        return False
    raise WorkspaceError(
        WorkspaceErrorCode.INVALID_ARGUMENT,
        "Workspace API boolean query value is invalid.",
        operation="workspace_api_validate",
        actual=value,
    )


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    try:
        selected = int(value)
    except (TypeError, ValueError) as error:
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_ARGUMENT,
            "Workspace API integer value is invalid.",
            operation="workspace_api_validate",
            actual=value,
        ) from error
    if selected < minimum or selected > maximum:
        raise WorkspaceError(
            WorkspaceErrorCode.INVALID_ARGUMENT,
            "Workspace API integer value is outside its allowed range.",
            operation="workspace_api_validate",
            expected={"minimum": minimum, "maximum": maximum},
            actual=selected,
        )
    return selected
