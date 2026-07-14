from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, MutableMapping, TypeVar

from .atomic import KeyedLockPool, atomic_write_json, fsync_directory, read_json_object
from .errors import WorkspaceErrorCode, WorkspaceStoreError
from .models import (
    FileReadRecord,
    WorkspaceBinding,
    WorkspaceLease,
    WorkspaceLeaseState,
    WorkspaceMount,
    WorkspaceOperationReceipt,
    WorkspaceRecoveryRecord,
    WorkspaceSnapshot,
    WorkspaceUsage,
    binding_from_dict,
    lease_from_dict,
    mount_from_dict,
    read_record_from_dict,
    recovery_from_dict,
    snapshot_from_dict,
    usage_from_dict,
    utc_now,
)


SCHEMA = "zyra.workspace-binding-store.v1"
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class WorkspaceStoreSnapshot:
    revision: int
    bindings: Mapping[str, WorkspaceBinding]
    leases: Mapping[str, WorkspaceLease]
    mounts: Mapping[str, tuple[WorkspaceMount, ...]]
    read_records: Mapping[str, FileReadRecord]
    snapshots: Mapping[str, WorkspaceSnapshot]
    recoveries: Mapping[str, WorkspaceRecoveryRecord]
    usages: Mapping[str, WorkspaceUsage]
    receipts: tuple[WorkspaceOperationReceipt, ...]
    idempotency: Mapping[str, Mapping[str, Any]]
    recovered_from_backup: bool = False
    updated_at: str = ""

    def binding_for_task(self, task_id: str, *, kind: str = "task") -> WorkspaceBinding | None:
        candidates = [
            item
            for item in self.bindings.values()
            if item.task_id == task_id and item.workspace_kind.value == kind and item.lifecycle_state.value != "deleted"
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item.owner_epoch, item.binding_revision, item.updated_at))


@dataclass(slots=True)
class _MutableStoreState:
    revision: int = 0
    bindings: dict[str, dict[str, Any]] = field(default_factory=dict)
    leases: dict[str, dict[str, Any]] = field(default_factory=dict)
    mounts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    read_records: dict[str, dict[str, Any]] = field(default_factory=dict)
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    recoveries: dict[str, dict[str, Any]] = field(default_factory=dict)
    usages: dict[str, dict[str, Any]] = field(default_factory=dict)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    idempotency: dict[str, dict[str, Any]] = field(default_factory=dict)
    updated_at: str = field(default_factory=utc_now)
    recovered_from_backup: bool = False

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "_MutableStoreState":
        if str(value.get("schema") or "") != SCHEMA:
            raise WorkspaceStoreError(
                WorkspaceErrorCode.STORE_CORRUPT,
                "Workspace store schema is missing or unsupported.",
                operation="load_store",
                expected=SCHEMA,
                actual=value.get("schema"),
            )
        return cls(
            revision=int(value.get("revision") or 0),
            bindings=_dict_of_dict(value.get("bindings")),
            leases=_dict_of_dict(value.get("leases")),
            mounts=_dict_of_list(value.get("mounts")),
            read_records=_dict_of_dict(value.get("read_records")),
            snapshots=_dict_of_dict(value.get("snapshots")),
            recoveries=_dict_of_dict(value.get("recoveries")),
            usages=_dict_of_dict(value.get("usages")),
            receipts=[dict(item) for item in _list(value.get("receipts")) if isinstance(item, Mapping)],
            idempotency=_dict_of_dict(value.get("idempotency")),
            updated_at=str(value.get("updated_at") or utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "revision": self.revision,
            "bindings": self.bindings,
            "leases": self.leases,
            "mounts": self.mounts,
            "read_records": self.read_records,
            "snapshots": self.snapshots,
            "recoveries": self.recoveries,
            "usages": self.usages,
            "receipts": self.receipts,
            "idempotency": self.idempotency,
            "updated_at": self.updated_at,
        }


class WorkspaceBindingStore:
    """Durable canonical workspace owner with atomic CAS updates.

    The store deliberately persists backend/location/binding/lease state outside
    TaskState.  TaskState may retain an opaque workspace id for projection, but
    this store is the only writer of workspace ownership and fencing epochs.
    """

    def __init__(self, root: str | Path, *, disabled: bool = False, max_receipts: int = 4096) -> None:
        self.root = Path(root).resolve()
        self.state_path = self.root / "workspace-state.json"
        self.backup_path = self.root / "workspace-state.backup.json"
        self.disabled = bool(disabled)
        self.max_receipts = max(128, int(max_receipts))
        self._guard = threading.RLock()
        self._workspace_locks = KeyedLockPool()
        if not self.disabled:
            self.root.mkdir(parents=True, exist_ok=True)
            if not self.state_path.exists():
                self._commit_state(_MutableStoreState())

    def snapshot(self) -> WorkspaceStoreSnapshot:
        state = self._load_state()
        bindings = {key: binding_from_dict(value) for key, value in state.bindings.items()}
        leases = {key: lease_from_dict(value) for key, value in state.leases.items()}
        mounts = {
            key: tuple(mount_from_dict(item) for item in values)
            for key, values in state.mounts.items()
        }
        reads = {key: read_record_from_dict(value) for key, value in state.read_records.items()}
        snapshots = {key: snapshot_from_dict(value) for key, value in state.snapshots.items()}
        recoveries = {key: recovery_from_dict(value) for key, value in state.recoveries.items()}
        usages = {key: usage_from_dict(value) for key, value in state.usages.items()}
        receipts = tuple(_receipt_from_dict(value) for value in state.receipts)
        return WorkspaceStoreSnapshot(
            revision=state.revision,
            bindings=bindings,
            leases=leases,
            mounts=mounts,
            read_records=reads,
            snapshots=snapshots,
            recoveries=recoveries,
            usages=usages,
            receipts=receipts,
            idempotency={key: dict(value) for key, value in state.idempotency.items()},
            recovered_from_backup=state.recovered_from_backup,
            updated_at=state.updated_at,
        )

    def get_binding(self, workspace_id: str) -> WorkspaceBinding | None:
        value = self._load_state().bindings.get(workspace_id)
        return binding_from_dict(value) if value is not None else None

    def require_binding(self, workspace_id: str) -> WorkspaceBinding:
        binding = self.get_binding(workspace_id)
        if binding is None:
            raise WorkspaceStoreError(
                WorkspaceErrorCode.NOT_FOUND,
                "Workspace binding was not found.",
                workspace_id=workspace_id,
                operation="get_binding",
            )
        return binding

    def find_binding(
        self,
        *,
        task_id: str,
        session_id: str = "",
        workspace_kind: str = "task",
    ) -> WorkspaceBinding | None:
        candidates = [
            binding_from_dict(value)
            for value in self._load_state().bindings.values()
            if str(value.get("task_id") or "") == task_id
            and str(value.get("workspace_kind") or "") == workspace_kind
            and (not session_id or str(value.get("session_id") or "") == session_id)
            and str(value.get("lifecycle_state") or "") != "deleted"
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item.owner_epoch, item.binding_revision, item.updated_at))

    def list_bindings(
        self,
        *,
        run_id: str = "",
        task_id: str = "",
        backend_id: str = "",
        include_deleted: bool = False,
    ) -> tuple[WorkspaceBinding, ...]:
        result: list[WorkspaceBinding] = []
        for value in self._load_state().bindings.values():
            binding = binding_from_dict(value)
            if run_id and binding.run_id != run_id:
                continue
            if task_id and binding.task_id != task_id:
                continue
            if backend_id and binding.backend_id != backend_id:
                continue
            if not include_deleted and binding.lifecycle_state.value == "deleted":
                continue
            result.append(binding)
        return tuple(sorted(result, key=lambda item: (item.task_id, item.workspace_kind.value, item.workspace_id)))

    def create_binding(self, binding: WorkspaceBinding, *, idempotency_key: str = "") -> WorkspaceBinding:
        with self._workspace_locks.acquire_many((binding.workspace_id, f"task:{binding.task_id}")):
            def mutate(state: _MutableStoreState) -> WorkspaceBinding:
                existing = state.bindings.get(binding.workspace_id)
                if existing is not None:
                    restored = binding_from_dict(existing)
                    if restored.to_dict() == binding.to_dict():
                        return restored
                    raise WorkspaceStoreError(
                        WorkspaceErrorCode.ALREADY_EXISTS,
                        "Workspace id is already bound to a different record.",
                        workspace_id=binding.workspace_id,
                        operation="create_binding",
                    )
                if idempotency_key:
                    prior = state.idempotency.get(idempotency_key)
                    if prior is not None:
                        prior_id = str(prior.get("workspace_id") or "")
                        prior_binding = state.bindings.get(prior_id)
                        if prior_binding is None:
                            raise WorkspaceStoreError(
                                WorkspaceErrorCode.STORE_CORRUPT,
                                "Workspace idempotency record references a missing binding.",
                                operation="create_binding",
                            )
                        return binding_from_dict(prior_binding)
                state.bindings[binding.workspace_id] = binding.to_dict()
                state.usages.setdefault(binding.workspace_id, WorkspaceUsage().to_dict())
                if idempotency_key:
                    state.idempotency[idempotency_key] = {
                        "operation": "create_binding",
                        "workspace_id": binding.workspace_id,
                        "fingerprint": _binding_fingerprint(binding),
                        "created_at": utc_now(),
                    }
                return binding

            return self._transaction(mutate)

    def compare_and_swap_binding(
        self,
        workspace_id: str,
        *,
        expected_revision: int,
        expected_owner_epoch: int,
        update: Callable[[WorkspaceBinding], WorkspaceBinding],
    ) -> WorkspaceBinding:
        with self._workspace_locks.acquire_many((workspace_id,)):
            def mutate(state: _MutableStoreState) -> WorkspaceBinding:
                value = state.bindings.get(workspace_id)
                if value is None:
                    raise WorkspaceStoreError(
                        WorkspaceErrorCode.NOT_FOUND,
                        "Workspace binding was not found.",
                        workspace_id=workspace_id,
                        operation="binding_cas",
                    )
                current = binding_from_dict(value)
                if current.binding_revision != expected_revision:
                    raise WorkspaceStoreError(
                        WorkspaceErrorCode.STORE_REVISION_CONFLICT,
                        "Workspace binding revision changed before commit.",
                        retryable=True,
                        workspace_id=workspace_id,
                        operation="binding_cas",
                        expected=expected_revision,
                        actual=current.binding_revision,
                    )
                if current.owner_epoch != expected_owner_epoch:
                    raise WorkspaceStoreError(
                        WorkspaceErrorCode.OWNER_EPOCH_STALE,
                        "Workspace owner epoch changed before commit.",
                        workspace_id=workspace_id,
                        operation="binding_cas",
                        expected=expected_owner_epoch,
                        actual=current.owner_epoch,
                    )
                updated = update(current)
                if updated.workspace_id != current.workspace_id:
                    raise WorkspaceStoreError(
                        WorkspaceErrorCode.INVALID_ARGUMENT,
                        "Workspace binding updates cannot change workspace identity.",
                        workspace_id=workspace_id,
                        operation="binding_cas",
                    )
                if updated.binding_revision <= current.binding_revision:
                    updated = replace(updated, binding_revision=current.binding_revision + 1, updated_at=utc_now())
                state.bindings[workspace_id] = updated.to_dict()
                return updated

            return self._transaction(mutate)

    def put_lease(self, lease: WorkspaceLease) -> WorkspaceLease:
        with self._workspace_locks.acquire_many((lease.workspace_id,)):
            def mutate(state: _MutableStoreState) -> WorkspaceLease:
                binding_value = state.bindings.get(lease.workspace_id)
                if binding_value is None:
                    raise WorkspaceStoreError(
                        WorkspaceErrorCode.NOT_FOUND,
                        "Cannot attach a lease to a missing workspace.",
                        workspace_id=lease.workspace_id,
                        operation="put_lease",
                    )
                binding = binding_from_dict(binding_value)
                if lease.owner_epoch != binding.owner_epoch:
                    raise WorkspaceStoreError(
                        WorkspaceErrorCode.OWNER_EPOCH_STALE,
                        "Workspace lease owner epoch does not match the binding.",
                        workspace_id=lease.workspace_id,
                        operation="put_lease",
                        expected=binding.owner_epoch,
                        actual=lease.owner_epoch,
                    )
                state.leases[lease.lease_id] = lease.to_dict()
                return lease

            return self._transaction(mutate)

    def get_lease(self, lease_id: str) -> WorkspaceLease | None:
        value = self._load_state().leases.get(lease_id)
        return lease_from_dict(value) if value is not None else None

    def list_leases(self, workspace_id: str, *, active_only: bool = False) -> tuple[WorkspaceLease, ...]:
        leases = [
            lease_from_dict(value)
            for value in self._load_state().leases.values()
            if str(value.get("workspace_id") or "") == workspace_id
        ]
        if active_only:
            leases = [item for item in leases if item.state is WorkspaceLeaseState.ACTIVE]
        return tuple(sorted(leases, key=lambda item: (item.issued_at, item.lease_id)))

    def update_lease(self, lease: WorkspaceLease) -> WorkspaceLease:
        with self._workspace_locks.acquire_many((lease.workspace_id,)):
            def mutate(state: _MutableStoreState) -> WorkspaceLease:
                if lease.lease_id not in state.leases:
                    raise WorkspaceStoreError(
                        WorkspaceErrorCode.LEASE_NOT_FOUND,
                        "Workspace lease was not found.",
                        workspace_id=lease.workspace_id,
                        operation="update_lease",
                    )
                state.leases[lease.lease_id] = lease.to_dict()
                return lease

            return self._transaction(mutate)

    def revoke_workspace_leases(self, workspace_id: str, *, state_value: WorkspaceLeaseState) -> tuple[WorkspaceLease, ...]:
        with self._workspace_locks.acquire_many((workspace_id,)):
            def mutate(state: _MutableStoreState) -> tuple[WorkspaceLease, ...]:
                changed: list[WorkspaceLease] = []
                for lease_id, value in list(state.leases.items()):
                    lease = lease_from_dict(value)
                    if lease.workspace_id != workspace_id or lease.state is not WorkspaceLeaseState.ACTIVE:
                        continue
                    updated = replace(lease, state=state_value, renewed_at=utc_now())
                    state.leases[lease_id] = updated.to_dict()
                    changed.append(updated)
                return tuple(changed)

            return self._transaction(mutate)

    def put_mounts(self, workspace_id: str, mounts: Iterable[WorkspaceMount]) -> tuple[WorkspaceMount, ...]:
        values = tuple(mounts)
        if any(item.workspace_id != workspace_id for item in values):
            raise WorkspaceStoreError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace mount records cannot cross workspace identities.",
                workspace_id=workspace_id,
                operation="put_mounts",
            )
        with self._workspace_locks.acquire_many((workspace_id,)):
            return self._transaction(lambda state: self._replace_mounts(state, workspace_id, values))

    @staticmethod
    def _replace_mounts(
        state: _MutableStoreState,
        workspace_id: str,
        mounts: tuple[WorkspaceMount, ...],
    ) -> tuple[WorkspaceMount, ...]:
        state.mounts[workspace_id] = [item.to_dict() for item in mounts]
        return mounts

    def get_mounts(self, workspace_id: str) -> tuple[WorkspaceMount, ...]:
        return tuple(mount_from_dict(item) for item in self._load_state().mounts.get(workspace_id, ()))

    def put_read_record(self, record: FileReadRecord) -> FileReadRecord:
        key = _read_key(record.workspace_id, record.path)
        with self._workspace_locks.acquire_many((record.workspace_id, key)):
            def mutate(state: _MutableStoreState) -> FileReadRecord:
                current_value = state.read_records.get(key)
                if current_value is not None:
                    current = read_record_from_dict(current_value)
                    if record.read_epoch <= current.read_epoch:
                        raise WorkspaceStoreError(
                            WorkspaceErrorCode.READ_EPOCH_STALE,
                            "Workspace read epoch must advance monotonically.",
                            workspace_id=record.workspace_id,
                            operation="put_read_record",
                            path=record.path,
                            expected=current.read_epoch + 1,
                            actual=record.read_epoch,
                        )
                state.read_records[key] = record.to_dict()
                return record

            return self._transaction(mutate)

    def get_read_record(self, workspace_id: str, path: str) -> FileReadRecord | None:
        value = self._load_state().read_records.get(_read_key(workspace_id, path))
        return read_record_from_dict(value) if value is not None else None

    def invalidate_read_records(self, workspace_id: str, *, paths: Iterable[str] | None = None) -> int:
        selected = None if paths is None else {_read_key(workspace_id, path) for path in paths}
        with self._workspace_locks.acquire_many((workspace_id,)):
            def mutate(state: _MutableStoreState) -> int:
                keys = [
                    key
                    for key in state.read_records
                    if key.startswith(f"{workspace_id}\0") and (selected is None or key in selected)
                ]
                for key in keys:
                    state.read_records.pop(key, None)
                return len(keys)

            return self._transaction(mutate)

    def put_snapshot(self, snapshot: WorkspaceSnapshot) -> WorkspaceSnapshot:
        with self._workspace_locks.acquire_many((snapshot.workspace_id, snapshot.snapshot_id)):
            def mutate(state: _MutableStoreState) -> WorkspaceSnapshot:
                existing = state.snapshots.get(snapshot.snapshot_id)
                if existing is not None:
                    restored = snapshot_from_dict(existing)
                    if restored.manifest_hash == snapshot.manifest_hash and restored.state == snapshot.state:
                        return restored
                    raise WorkspaceStoreError(
                        WorkspaceErrorCode.IDEMPOTENCY_CONFLICT,
                        "Snapshot id is already associated with a different manifest.",
                        workspace_id=snapshot.workspace_id,
                        operation="put_snapshot",
                    )
                state.snapshots[snapshot.snapshot_id] = snapshot.to_dict()
                return snapshot

            return self._transaction(mutate)

    def update_snapshot(self, snapshot: WorkspaceSnapshot) -> WorkspaceSnapshot:
        with self._workspace_locks.acquire_many((snapshot.workspace_id, snapshot.snapshot_id)):
            def mutate(state: _MutableStoreState) -> WorkspaceSnapshot:
                if snapshot.snapshot_id not in state.snapshots:
                    raise WorkspaceStoreError(
                        WorkspaceErrorCode.SNAPSHOT_NOT_FOUND,
                        "Workspace snapshot was not found.",
                        workspace_id=snapshot.workspace_id,
                        operation="update_snapshot",
                    )
                state.snapshots[snapshot.snapshot_id] = snapshot.to_dict()
                return snapshot

            return self._transaction(mutate)

    def get_snapshot(self, snapshot_id: str) -> WorkspaceSnapshot | None:
        value = self._load_state().snapshots.get(snapshot_id)
        return snapshot_from_dict(value) if value is not None else None

    def list_snapshots(self, workspace_id: str) -> tuple[WorkspaceSnapshot, ...]:
        values = [
            snapshot_from_dict(value)
            for value in self._load_state().snapshots.values()
            if str(value.get("workspace_id") or "") == workspace_id
        ]
        return tuple(sorted(values, key=lambda item: (item.created_at, item.snapshot_id)))

    def put_recovery(self, recovery: WorkspaceRecoveryRecord) -> WorkspaceRecoveryRecord:
        with self._workspace_locks.acquire_many((recovery.workspace_id, recovery.recovery_id)):
            def mutate(state: _MutableStoreState) -> WorkspaceRecoveryRecord:
                state.recoveries[recovery.recovery_id] = recovery.to_dict()
                return recovery

            return self._transaction(mutate)

    def get_recovery(self, recovery_id: str) -> WorkspaceRecoveryRecord | None:
        value = self._load_state().recoveries.get(recovery_id)
        return recovery_from_dict(value) if value is not None else None

    def list_recoveries(self, workspace_id: str) -> tuple[WorkspaceRecoveryRecord, ...]:
        values = [
            recovery_from_dict(value)
            for value in self._load_state().recoveries.values()
            if str(value.get("workspace_id") or "") == workspace_id
        ]
        return tuple(sorted(values, key=lambda item: (item.detected_at, item.recovery_id)))

    def put_usage(self, workspace_id: str, usage: WorkspaceUsage) -> WorkspaceUsage:
        with self._workspace_locks.acquire_many((workspace_id,)):
            def mutate(state: _MutableStoreState) -> WorkspaceUsage:
                state.usages[workspace_id] = usage.to_dict()
                return usage

            return self._transaction(mutate)

    def get_usage(self, workspace_id: str) -> WorkspaceUsage:
        value = self._load_state().usages.get(workspace_id)
        return usage_from_dict(value) if value is not None else WorkspaceUsage()

    def append_receipt(self, receipt: WorkspaceOperationReceipt) -> WorkspaceOperationReceipt:
        with self._workspace_locks.acquire_many((receipt.workspace_id,)):
            def mutate(state: _MutableStoreState) -> WorkspaceOperationReceipt:
                state.receipts.append(receipt.to_dict())
                if len(state.receipts) > self.max_receipts:
                    del state.receipts[: len(state.receipts) - self.max_receipts]
                return receipt

            return self._transaction(mutate)

    def list_receipts(
        self,
        workspace_id: str,
        *,
        operation: str = "",
        limit: int = 200,
    ) -> tuple[WorkspaceOperationReceipt, ...]:
        result = [
            _receipt_from_dict(value)
            for value in self._load_state().receipts
            if str(value.get("workspace_id") or "") == workspace_id
            and (not operation or str(value.get("operation") or "") == operation)
        ]
        return tuple(result[-max(1, min(1000, int(limit))):])

    def idempotency_record(self, key: str) -> Mapping[str, Any] | None:
        value = self._load_state().idempotency.get(key)
        return dict(value) if value is not None else None

    def put_idempotency_record(self, key: str, value: Mapping[str, Any]) -> Mapping[str, Any]:
        if not key:
            raise ValueError("idempotency key is required")
        fingerprint = json.dumps(dict(value), sort_keys=True, separators=(",", ":"))

        def mutate(state: _MutableStoreState) -> Mapping[str, Any]:
            existing = state.idempotency.get(key)
            if existing is not None:
                prior = json.dumps(existing, sort_keys=True, separators=(",", ":"))
                if prior != fingerprint:
                    raise WorkspaceStoreError(
                        WorkspaceErrorCode.IDEMPOTENCY_CONFLICT,
                        "Workspace idempotency key was reused with a different payload.",
                        operation="put_idempotency_record",
                    )
                return dict(existing)
            state.idempotency[key] = dict(value)
            return dict(value)

        return self._transaction(mutate)

    def health(self) -> dict[str, Any]:
        if self.disabled:
            return {
                "ok": False,
                "disabled": True,
                "schema": SCHEMA,
                "state_owner": type(self).__name__,
                "error": WorkspaceErrorCode.DISABLED.value,
            }
        try:
            state = self._load_state()
        except WorkspaceStoreError as error:
            return {
                "ok": False,
                "disabled": False,
                "schema": SCHEMA,
                "state_owner": type(self).__name__,
                "error": error.code.value,
            }
        return {
            "ok": True,
            "disabled": False,
            "schema": SCHEMA,
            "state_owner": type(self).__name__,
            "revision": state.revision,
            "binding_count": len(state.bindings),
            "lease_count": len(state.leases),
            "snapshot_count": len(state.snapshots),
            "recovery_count": len(state.recoveries),
            "recovered_from_backup": state.recovered_from_backup,
        }

    def _transaction(self, mutation: Callable[[_MutableStoreState], T]) -> T:
        self._ensure_enabled()
        with self._guard:
            state = self._load_state_locked()
            original_revision = state.revision
            result = mutation(state)
            state.revision = original_revision + 1
            state.updated_at = utc_now()
            self._commit_state(state)
            return result

    def _load_state(self) -> _MutableStoreState:
        self._ensure_enabled()
        with self._guard:
            return self._load_state_locked()

    def _load_state_locked(self) -> _MutableStoreState:
        try:
            return _MutableStoreState.from_dict(read_json_object(self.state_path))
        except (FileNotFoundError, WorkspaceStoreError) as primary_error:
            try:
                state = _MutableStoreState.from_dict(read_json_object(self.backup_path))
            except (FileNotFoundError, WorkspaceStoreError) as backup_error:
                raise WorkspaceStoreError(
                    WorkspaceErrorCode.STORE_CORRUPT,
                    "Workspace state and backup are unavailable or corrupt.",
                    operation="load_store",
                    path=str(self.state_path),
                    metadata={
                        "primary_error": type(primary_error).__name__,
                        "backup_error": type(backup_error).__name__,
                    },
                ) from backup_error
            state.recovered_from_backup = True
            self._commit_state(state)
            state.recovered_from_backup = True
            return state

    def _commit_state(self, state: _MutableStoreState) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists():
            backup_temporary = self.backup_path.with_suffix(".json.tmp")
            try:
                shutil.copyfile(self.state_path, backup_temporary)
                with backup_temporary.open("r+b") as handle:
                    os.fsync(handle.fileno())
                os.replace(backup_temporary, self.backup_path)
                fsync_directory(self.root)
            except Exception:
                backup_temporary.unlink(missing_ok=True)
                raise
        atomic_write_json(self.state_path, state.to_dict())
        if not self.backup_path.exists():
            shutil.copyfile(self.state_path, self.backup_path)
            fsync_directory(self.root)

    def _ensure_enabled(self) -> None:
        if self.disabled:
            raise WorkspaceStoreError(
                WorkspaceErrorCode.DISABLED,
                "Workspace binding store is disabled.",
                operation="workspace_store",
            )


def _read_key(workspace_id: str, path: str) -> str:
    return f"{workspace_id}\0{path.replace('\\', '/')}"


def _binding_fingerprint(binding: WorkspaceBinding) -> str:
    payload = binding.to_dict()
    payload.pop("created_at", None)
    payload.pop("updated_at", None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _receipt_from_dict(value: Mapping[str, Any]) -> WorkspaceOperationReceipt:
    from .models import WorkspaceOperation

    return WorkspaceOperationReceipt(
        receipt_id=str(value.get("receipt_id") or ""),
        workspace_id=str(value.get("workspace_id") or ""),
        operation=WorkspaceOperation(str(value.get("operation") or "open")),
        ok=bool(value.get("ok", False)),
        owner_epoch=int(value.get("owner_epoch") or 0),
        binding_revision=int(value.get("binding_revision") or 0),
        lease_id=str(value.get("lease_id") or ""),
        started_at=str(value.get("started_at") or ""),
        completed_at=str(value.get("completed_at") or ""),
        causation_id=str(value.get("causation_id") or ""),
        idempotency_key=str(value.get("idempotency_key") or ""),
        snapshot_id=str(value.get("snapshot_id") or ""),
        paths=tuple(str(item) for item in _list(value.get("paths"))),
        bytes_changed=int(value.get("bytes_changed") or 0),
        state_before=str(value.get("state_before") or ""),
        state_after=str(value.get("state_after") or ""),
        error_code=str(value.get("error_code") or ""),
        message=str(value.get("message") or ""),
        metadata=dict(value.get("metadata") or {}) if isinstance(value.get("metadata"), Mapping) else {},
    )


def _dict_of_dict(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): dict(item) for key, item in value.items() if isinstance(item, Mapping)}


def _dict_of_list(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): [dict(item) for item in _list(items) if isinstance(item, Mapping)]
        for key, items in value.items()
    }


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []
