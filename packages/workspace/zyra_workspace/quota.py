from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from .atomic import atomic_write_json, read_json_object
from .errors import WorkspaceErrorCode, WorkspaceQuotaError
from .models import WorkspaceQuota, WorkspaceUsage, new_workspace_id, utc_now
from .store import WorkspaceBindingStore


@dataclass(frozen=True, slots=True)
class QuotaReservation:
    reservation_id: str
    workspace_id: str
    lease_id: str
    owner_epoch: int
    path: str
    reserved_bytes: int
    creates_file: bool
    state: str = "active"
    created_at: str = field(default_factory=utc_now)
    expires_at: str = ""
    settled_bytes: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "workspace_id": self.workspace_id,
            "lease_id": self.lease_id,
            "owner_epoch": self.owner_epoch,
            "path": self.path,
            "reserved_bytes": self.reserved_bytes,
            "creates_file": self.creates_file,
            "state": self.state,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "settled_bytes": self.settled_bytes,
            "metadata": dict(self.metadata),
        }


class QuotaReservationStore:
    def __init__(self, path: str | Path, *, disabled: bool = False) -> None:
        self.path = Path(path).resolve()
        self.disabled = bool(disabled)
        self._lock = threading.RLock()
        if not self.disabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                atomic_write_json(
                    self.path,
                    {
                        "schema": "zyra.workspace-quota-reservations.v1",
                        "revision": 0,
                        "reservations": {},
                        "updated_at": utc_now(),
                    },
                )

    def list(self, workspace_id: str = "", *, active_only: bool = False) -> tuple[QuotaReservation, ...]:
        with self._lock:
            state = self._load()
        values = [
            _reservation_from_dict(value)
            for value in state["reservations"].values()
            if not workspace_id or str(value.get("workspace_id") or "") == workspace_id
        ]
        if active_only:
            values = [item for item in values if item.state == "active"]
        return tuple(sorted(values, key=lambda item: (item.created_at, item.reservation_id)))

    def create(self, reservation: QuotaReservation) -> QuotaReservation:
        with self._lock:
            state = self._load()
            existing = state["reservations"].get(reservation.reservation_id)
            if existing is not None:
                restored = _reservation_from_dict(existing)
                if restored.to_dict() == reservation.to_dict():
                    return restored
                raise WorkspaceQuotaError(
                    WorkspaceErrorCode.IDEMPOTENCY_CONFLICT,
                    "Workspace quota reservation id was reused.",
                    workspace_id=reservation.workspace_id,
                    operation="quota_reserve",
                    path=reservation.path,
                )
            state["reservations"][reservation.reservation_id] = reservation.to_dict()
            self._commit(state)
            return reservation

    def update(self, reservation: QuotaReservation) -> QuotaReservation:
        with self._lock:
            state = self._load()
            if reservation.reservation_id not in state["reservations"]:
                raise WorkspaceQuotaError(
                    WorkspaceErrorCode.RESERVATION_NOT_FOUND,
                    "Workspace quota reservation was not found.",
                    workspace_id=reservation.workspace_id,
                    operation="quota_update",
                    path=reservation.path,
                )
            state["reservations"][reservation.reservation_id] = reservation.to_dict()
            self._commit(state)
            return reservation

    def get(self, reservation_id: str) -> QuotaReservation | None:
        with self._lock:
            value = self._load()["reservations"].get(reservation_id)
        return _reservation_from_dict(value) if value is not None else None

    def reap_expired(self, now: datetime | None = None) -> tuple[QuotaReservation, ...]:
        selected_now = now or datetime.now(UTC)
        changed: list[QuotaReservation] = []
        with self._lock:
            state = self._load()
            for key, value in list(state["reservations"].items()):
                reservation = _reservation_from_dict(value)
                if reservation.state != "active" or not reservation.expires_at:
                    continue
                try:
                    expires = datetime.fromisoformat(reservation.expires_at)
                except ValueError:
                    expires = selected_now - timedelta(seconds=1)
                if expires > selected_now:
                    continue
                expired = replace(reservation, state="expired")
                state["reservations"][key] = expired.to_dict()
                changed.append(expired)
            if changed:
                self._commit(state)
        return tuple(changed)

    def _load(self) -> dict[str, Any]:
        if self.disabled:
            raise WorkspaceQuotaError(
                WorkspaceErrorCode.DISABLED,
                "Workspace quota reservation store is disabled.",
                operation="quota_store",
            )
        value = read_json_object(self.path)
        if str(value.get("schema") or "") != "zyra.workspace-quota-reservations.v1":
            raise WorkspaceQuotaError(
                WorkspaceErrorCode.STORE_CORRUPT,
                "Workspace quota reservation schema is invalid.",
                operation="quota_store",
            )
        reservations = value.get("reservations")
        if not isinstance(reservations, dict):
            raise WorkspaceQuotaError(
                WorkspaceErrorCode.STORE_CORRUPT,
                "Workspace quota reservations are invalid.",
                operation="quota_store",
            )
        return value

    def _commit(self, state: dict[str, Any]) -> None:
        state["revision"] = int(state.get("revision") or 0) + 1
        state["updated_at"] = utc_now()
        atomic_write_json(self.path, state)


class WorkspaceQuotaRuntime:
    def __init__(
        self,
        binding_store: WorkspaceBindingStore,
        reservation_store: QuotaReservationStore,
        *,
        reservation_ttl_seconds: int = 300,
    ) -> None:
        self.binding_store = binding_store
        self.reservation_store = reservation_store
        self.reservation_ttl_seconds = max(5, int(reservation_ttl_seconds))
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    def reserve(
        self,
        *,
        workspace_id: str,
        lease_id: str,
        owner_epoch: int,
        path: str,
        requested_bytes: int,
        creates_file: bool,
        metadata: Mapping[str, Any] | None = None,
    ) -> QuotaReservation:
        if requested_bytes < 0:
            raise WorkspaceQuotaError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace quota reservations cannot be negative.",
                workspace_id=workspace_id,
                operation="quota_reserve",
                path=path,
            )
        lock = self._lock_for(workspace_id)
        with lock:
            self._reconcile_expired_locked(workspace_id)
            binding = self.binding_store.require_binding(workspace_id)
            if binding.owner_epoch != owner_epoch:
                raise WorkspaceQuotaError(
                    WorkspaceErrorCode.OWNER_EPOCH_STALE,
                    "Workspace quota reservation used a stale owner epoch.",
                    workspace_id=workspace_id,
                    operation="quota_reserve",
                    path=path,
                    expected=binding.owner_epoch,
                    actual=owner_epoch,
                )
            usage = self.binding_store.get_usage(workspace_id)
            active = self.reservation_store.list(workspace_id, active_only=True)
            active_bytes = sum(item.reserved_bytes for item in active)
            active_files = sum(1 for item in active if item.creates_file)
            quota = binding.quota
            if requested_bytes > quota.max_single_file_bytes:
                raise WorkspaceQuotaError(
                    WorkspaceErrorCode.SINGLE_FILE_LIMIT_EXCEEDED,
                    "Workspace write exceeds the single-file quota.",
                    workspace_id=workspace_id,
                    operation="quota_reserve",
                    path=path,
                    expected=quota.max_single_file_bytes,
                    actual=requested_bytes,
                )
            projected_bytes = usage.used_bytes + active_bytes + requested_bytes
            if projected_bytes > quota.max_bytes:
                raise WorkspaceQuotaError(
                    WorkspaceErrorCode.QUOTA_EXCEEDED,
                    "Workspace byte quota would be exceeded.",
                    workspace_id=workspace_id,
                    operation="quota_reserve",
                    path=path,
                    expected=quota.max_bytes,
                    actual=projected_bytes,
                    metadata={"used_bytes": usage.used_bytes, "reserved_bytes": active_bytes},
                )
            projected_files = usage.file_count + active_files + (1 if creates_file else 0)
            if projected_files > quota.max_files:
                raise WorkspaceQuotaError(
                    WorkspaceErrorCode.FILE_COUNT_EXCEEDED,
                    "Workspace file-count quota would be exceeded.",
                    workspace_id=workspace_id,
                    operation="quota_reserve",
                    path=path,
                    expected=quota.max_files,
                    actual=projected_files,
                )
            expires_at = (datetime.now(UTC) + timedelta(seconds=self.reservation_ttl_seconds)).isoformat()
            reservation = QuotaReservation(
                reservation_id=new_workspace_id("quota"),
                workspace_id=workspace_id,
                lease_id=lease_id,
                owner_epoch=owner_epoch,
                path=path,
                reserved_bytes=requested_bytes,
                creates_file=creates_file,
                expires_at=expires_at,
                metadata=dict(metadata or {}),
            )
            self.reservation_store.create(reservation)
            self.binding_store.put_usage(
                workspace_id,
                replace(
                    usage,
                    reserved_bytes=active_bytes + requested_bytes,
                    revision=usage.revision + 1,
                    scanned_at=utc_now(),
                ),
            )
            return reservation

    def settle(
        self,
        reservation_id: str,
        *,
        actual_bytes: int,
        prior_bytes: int,
        file_created: bool,
    ) -> QuotaReservation:
        reservation = self.reservation_store.get(reservation_id)
        if reservation is None:
            raise WorkspaceQuotaError(
                WorkspaceErrorCode.RESERVATION_NOT_FOUND,
                "Workspace quota reservation was not found.",
                operation="quota_settle",
            )
        lock = self._lock_for(reservation.workspace_id)
        with lock:
            current = self.reservation_store.get(reservation_id)
            if current is None:
                raise WorkspaceQuotaError(
                    WorkspaceErrorCode.RESERVATION_NOT_FOUND,
                    "Workspace quota reservation was not found.",
                    workspace_id=reservation.workspace_id,
                    operation="quota_settle",
                    path=reservation.path,
                )
            if current.state == "settled":
                if current.settled_bytes != actual_bytes:
                    raise WorkspaceQuotaError(
                        WorkspaceErrorCode.IDEMPOTENCY_CONFLICT,
                        "Workspace quota reservation was settled with different bytes.",
                        workspace_id=current.workspace_id,
                        operation="quota_settle",
                        path=current.path,
                    )
                return current
            if current.state != "active":
                raise WorkspaceQuotaError(
                    WorkspaceErrorCode.RESERVATION_EXPIRED,
                    "Workspace quota reservation is no longer active.",
                    workspace_id=current.workspace_id,
                    operation="quota_settle",
                    path=current.path,
                    actual=current.state,
                )
            binding = self.binding_store.require_binding(current.workspace_id)
            if binding.owner_epoch != current.owner_epoch:
                raise WorkspaceQuotaError(
                    WorkspaceErrorCode.OWNER_EPOCH_STALE,
                    "Workspace quota settlement used a stale owner epoch.",
                    workspace_id=current.workspace_id,
                    operation="quota_settle",
                    path=current.path,
                    expected=binding.owner_epoch,
                    actual=current.owner_epoch,
                )
            usage = self.binding_store.get_usage(current.workspace_id)
            delta = int(actual_bytes) - int(prior_bytes)
            used_bytes = max(0, usage.used_bytes + delta)
            file_count = max(0, usage.file_count + (1 if file_created else 0))
            if used_bytes > binding.quota.max_bytes or file_count > binding.quota.max_files:
                raise WorkspaceQuotaError(
                    WorkspaceErrorCode.QUOTA_EXCEEDED,
                    "Workspace quota settlement exceeds the binding quota.",
                    workspace_id=current.workspace_id,
                    operation="quota_settle",
                    path=current.path,
                )
            settled = replace(current, state="settled", settled_bytes=actual_bytes)
            self.reservation_store.update(settled)
            remaining = sum(
                item.reserved_bytes
                for item in self.reservation_store.list(current.workspace_id, active_only=True)
                if item.reservation_id != current.reservation_id
            )
            self.binding_store.put_usage(
                current.workspace_id,
                replace(
                    usage,
                    used_bytes=used_bytes,
                    file_count=file_count,
                    reserved_bytes=remaining,
                    revision=usage.revision + 1,
                    scanned_at=utc_now(),
                ),
            )
            return settled

    def abort(self, reservation_id: str, *, reason: str = "") -> QuotaReservation:
        reservation = self.reservation_store.get(reservation_id)
        if reservation is None:
            raise WorkspaceQuotaError(
                WorkspaceErrorCode.RESERVATION_NOT_FOUND,
                "Workspace quota reservation was not found.",
                operation="quota_abort",
            )
        with self._lock_for(reservation.workspace_id):
            current = self.reservation_store.get(reservation_id) or reservation
            if current.state != "active":
                return current
            aborted = replace(
                current,
                state="aborted",
                metadata={**dict(current.metadata), "abort_reason": reason},
            )
            self.reservation_store.update(aborted)
            usage = self.binding_store.get_usage(current.workspace_id)
            remaining = sum(
                item.reserved_bytes
                for item in self.reservation_store.list(current.workspace_id, active_only=True)
                if item.reservation_id != current.reservation_id
            )
            self.binding_store.put_usage(
                current.workspace_id,
                replace(usage, reserved_bytes=remaining, revision=usage.revision + 1, scanned_at=utc_now()),
            )
            return aborted

    def reconcile_usage(self, workspace_id: str, scanned: WorkspaceUsage) -> WorkspaceUsage:
        with self._lock_for(workspace_id):
            active = self.reservation_store.list(workspace_id, active_only=True)
            usage = replace(
                scanned,
                reserved_bytes=sum(item.reserved_bytes for item in active),
                revision=self.binding_store.get_usage(workspace_id).revision + 1,
                scanned_at=utc_now(),
            )
            binding = self.binding_store.require_binding(workspace_id)
            self._validate_usage(binding.quota, usage, workspace_id=workspace_id)
            return self.binding_store.put_usage(workspace_id, usage)

    @staticmethod
    def _validate_usage(quota: WorkspaceQuota, usage: WorkspaceUsage, *, workspace_id: str) -> None:
        if usage.accounted_bytes > quota.max_bytes:
            raise WorkspaceQuotaError(
                WorkspaceErrorCode.QUOTA_EXCEEDED,
                "Workspace reconciled usage exceeds the byte quota.",
                workspace_id=workspace_id,
                operation="quota_reconcile",
                expected=quota.max_bytes,
                actual=usage.accounted_bytes,
            )
        if usage.file_count > quota.max_files:
            raise WorkspaceQuotaError(
                WorkspaceErrorCode.FILE_COUNT_EXCEEDED,
                "Workspace reconciled usage exceeds the file-count quota.",
                workspace_id=workspace_id,
                operation="quota_reconcile",
                expected=quota.max_files,
                actual=usage.file_count,
            )
        if usage.directory_count > quota.max_directories:
            raise WorkspaceQuotaError(
                WorkspaceErrorCode.QUOTA_EXCEEDED,
                "Workspace reconciled usage exceeds the directory-count quota.",
                workspace_id=workspace_id,
                operation="quota_reconcile",
                expected=quota.max_directories,
                actual=usage.directory_count,
            )

    def _reconcile_expired_locked(self, workspace_id: str) -> None:
        changed = [item for item in self.reservation_store.reap_expired() if item.workspace_id == workspace_id]
        if not changed:
            return
        usage = self.binding_store.get_usage(workspace_id)
        remaining = sum(item.reserved_bytes for item in self.reservation_store.list(workspace_id, active_only=True))
        self.binding_store.put_usage(
            workspace_id,
            replace(usage, reserved_bytes=remaining, revision=usage.revision + 1, scanned_at=utc_now()),
        )

    def _lock_for(self, workspace_id: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(workspace_id, threading.RLock())


def _reservation_from_dict(value: Mapping[str, Any]) -> QuotaReservation:
    return QuotaReservation(
        reservation_id=str(value.get("reservation_id") or ""),
        workspace_id=str(value.get("workspace_id") or ""),
        lease_id=str(value.get("lease_id") or ""),
        owner_epoch=int(value.get("owner_epoch") or 0),
        path=str(value.get("path") or ""),
        reserved_bytes=int(value.get("reserved_bytes") or 0),
        creates_file=bool(value.get("creates_file", False)),
        state=str(value.get("state") or "active"),
        created_at=str(value.get("created_at") or utc_now()),
        expires_at=str(value.get("expires_at") or ""),
        settled_bytes=int(value.get("settled_bytes") or 0),
        metadata=dict(value.get("metadata") or {}) if isinstance(value.get("metadata"), Mapping) else {},
    )
