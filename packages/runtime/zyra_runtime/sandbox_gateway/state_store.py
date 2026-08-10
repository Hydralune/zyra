from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, TypeVar

from .canonical import canonical_json, digest
from .constants import DEFAULT_EVENT_HISTORY_LIMIT, STATE_SCHEMA
from .errors import GatewayErrorCode, SandboxGatewayError
from .models import (
    GatewayEvent,
    GatewayEventKind,
    GatewayLease,
    GatewayLifecycleState,
    GatewaySessionRecord,
    PermissionBinding,
    QuarantineRecord,
)

T = TypeVar("T")


class InterProcessFileLock:
    """Small cross-platform advisory lock used around atomic state replacement."""

    def __init__(self, path: Path, *, timeout_seconds: float = 30.0) -> None:
        self.path = Path(path)
        self.timeout_seconds = float(timeout_seconds)
        self._handle: Any = None
        self._thread_lock = threading.RLock()

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            handle = self.path.open("a+b")
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            while True:
                try:
                    self._lock_handle(handle)
                except OSError:
                    if time.monotonic() >= deadline:
                        handle.close()
                        raise TimeoutError(f"timed out acquiring state lock: {self.path.name}")
                    time.sleep(0.025)
                    continue
                self._handle = handle
                return

    def release(self) -> None:
        with self._thread_lock:
            handle = self._handle
            if handle is None:
                return
            self._handle = None
            try:
                self._unlock_handle(handle)
            finally:
                handle.close()

    def __enter__(self) -> "InterProcessFileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()

    @staticmethod
    def _lock_handle(handle: Any) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_handle(handle: Any) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class GatewayStateStore:
    """Durable canonical state for gateway sessions, grants, events, and receipts."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        event_history_limit: int = DEFAULT_EVENT_HISTORY_LIMIT,
        lock_timeout_seconds: float = 30.0,
    ) -> None:
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_root / "sandbox-gateway-state.json"
        self.lock = InterProcessFileLock(
            self.state_root / "sandbox-gateway-state.lock",
            timeout_seconds=lock_timeout_seconds,
        )
        self.event_history_limit = int(event_history_limit)
        self._thread_lock = threading.RLock()
        if not self.state_path.exists():
            with self.lock:
                if not self.state_path.exists():
                    self._write_unlocked(self._empty())

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema": STATE_SCHEMA,
            "revision": 0,
            "sessions": {},
            "permission_bindings": {},
            "events": {},
            "event_replays": {},
            "receipts": {},
            "quarantine": {},
            "idempotency": {},
            "leases": {},
            "metadata": {
                "canonical_owner": "SandboxGatewayRuntime",
                "stores_secret_material": False,
            },
        }

    @contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        with self._thread_lock:
            with self.lock:
                state = self._read_unlocked()
                revision = int(state.get("revision", 0))
                yield state
                state["revision"] = revision + 1
                self._write_unlocked(state)

    def snapshot(self) -> dict[str, Any]:
        with self._thread_lock:
            with self.lock:
                return self._read_unlocked()

    @property
    def revision(self) -> int:
        return int(self.snapshot().get("revision", 0))

    def create_session(self, record: GatewaySessionRecord) -> GatewaySessionRecord:
        with self.transaction() as state:
            sessions = state["sessions"]
            existing = sessions.get(record.session_id)
            if existing is not None:
                candidate = GatewaySessionRecord.from_dict(existing)
                if self._session_identity(candidate) != self._session_identity(record):
                    raise SandboxGatewayError(
                        GatewayErrorCode.STATE_CONFLICT,
                        "session id is already bound to different run or task material",
                        operation="create_session",
                    )
                return candidate
            sessions[record.session_id] = record.to_dict()
            return record

    def require_session(self, session_id: str) -> GatewaySessionRecord:
        state = self.snapshot()
        value = state["sessions"].get(session_id)
        if value is None:
            raise SandboxGatewayError(
                GatewayErrorCode.SESSION_NOT_FOUND,
                f"sandbox gateway session not found: {session_id}",
                operation="require_session",
            )
        return GatewaySessionRecord.from_dict(value)

    def save_session(
        self,
        record: GatewaySessionRecord,
        *,
        expected_generation: int,
    ) -> GatewaySessionRecord:
        with self.transaction() as state:
            value = state["sessions"].get(record.session_id)
            if value is None:
                raise SandboxGatewayError(
                    GatewayErrorCode.SESSION_NOT_FOUND,
                    f"sandbox gateway session not found: {record.session_id}",
                    operation="save_session",
                )
            existing = GatewaySessionRecord.from_dict(value)
            if existing.generation != expected_generation:
                raise SandboxGatewayError(
                    GatewayErrorCode.STATE_CONFLICT,
                    "session generation changed before commit",
                    operation="save_session",
                    retryable=True,
                    metadata={
                        "expected_generation": expected_generation,
                        "actual_generation": existing.generation,
                    },
                )
            state["sessions"][record.session_id] = record.to_dict()
            return record

    def update_session(
        self,
        session_id: str,
        updater: Callable[[GatewaySessionRecord], GatewaySessionRecord],
        *,
        retries: int = 4,
    ) -> GatewaySessionRecord:
        last_error: Exception | None = None
        for _ in range(max(1, retries)):
            existing = self.require_session(session_id)
            updated = updater(existing)
            if updated.session_id != existing.session_id:
                raise ValueError("session updater cannot change session_id")
            try:
                return self.save_session(updated, expected_generation=existing.generation)
            except SandboxGatewayError as error:
                if error.code is not GatewayErrorCode.STATE_CONFLICT:
                    raise
                last_error = error
        assert last_error is not None
        raise last_error

    def list_sessions(
        self,
        *,
        states: set[GatewayLifecycleState] | None = None,
    ) -> tuple[GatewaySessionRecord, ...]:
        state = self.snapshot()
        records = [
            GatewaySessionRecord.from_dict(item)
            for item in state["sessions"].values()
        ]
        if states is not None:
            records = [item for item in records if item.state in states]
        return tuple(sorted(records, key=lambda item: (item.created_at, item.session_id)))

    def save_permission_binding(self, binding: PermissionBinding) -> PermissionBinding:
        with self.transaction() as state:
            values = state["permission_bindings"]
            existing = values.get(binding.binding_id)
            if existing is not None:
                candidate = PermissionBinding.from_dict(existing)
                if candidate.to_dict() != binding.to_dict():
                    raise SandboxGatewayError(
                        GatewayErrorCode.STATE_CONFLICT,
                        "permission binding identity collision",
                        operation="save_permission_binding",
                    )
                return candidate
            values[binding.binding_id] = binding.to_dict()
            return binding

    def require_permission_binding(self, binding_id: str) -> PermissionBinding:
        state = self.snapshot()
        value = state["permission_bindings"].get(binding_id)
        if value is None:
            raise SandboxGatewayError(
                GatewayErrorCode.APPROVAL_MISSING,
                f"permission binding not found: {binding_id}",
                operation="require_permission_binding",
            )
        return PermissionBinding.from_dict(value)

    def consume_permission_binding(
        self,
        binding_id: str,
        *,
        consumption_id: str,
        expected_grant_digest: str,
        expected_command_digest: str,
    ) -> PermissionBinding:
        with self.transaction() as state:
            value = state["permission_bindings"].get(binding_id)
            if value is None:
                raise SandboxGatewayError(
                    GatewayErrorCode.APPROVAL_MISSING,
                    "permission binding not found",
                    operation="consume_permission_binding",
                )
            binding = PermissionBinding.from_dict(value)
            if binding.grant_digest != expected_grant_digest:
                raise SandboxGatewayError(
                    GatewayErrorCode.APPROVAL_MISMATCH,
                    "permission grant material does not match the binding",
                    operation="consume_permission_binding",
                )
            if binding.command_digest != expected_command_digest:
                raise SandboxGatewayError(
                    GatewayErrorCode.COMMAND_MUTATED,
                    "command material changed after permission was issued",
                    operation="consume_permission_binding",
                )
            consumed = binding.consume(consumption_id)
            state["permission_bindings"][binding_id] = consumed.to_dict()
            return consumed

    def append_event(self, event: GatewayEvent) -> GatewayEvent:
        with self.transaction() as state:
            events = state["events"].setdefault(event.session_id, [])
            if events and int(events[-1]["sequence"]) >= event.sequence:
                existing = next(
                    (item for item in events if item["event_id"] == event.event_id),
                    None,
                )
                if existing is not None:
                    return self._event_from_dict(existing)
                raise SandboxGatewayError(
                    GatewayErrorCode.STATE_CONFLICT,
                    "event sequence must increase monotonically",
                    operation="append_event",
                )
            events.append(event.to_dict())
            if len(events) > self.event_history_limit:
                del events[: len(events) - self.event_history_limit]
            return event

    def append_next_event(
        self,
        *,
        kind: GatewayEventKind | str,
        run_id: str,
        task_id: str,
        session_id: str,
        worker_id: str,
        payload: Mapping[str, Any],
        causation_id: str = "",
        correlation_id: str = "",
        idempotency_key: str = "",
    ) -> GatewayEvent:
        """Allocate a sequence and append under one cross-process lock."""

        with self.transaction() as state:
            events = state.setdefault("events", {}).setdefault(session_id, [])
            replays = state.setdefault("event_replays", {})
            replay_key = f"{session_id}:{idempotency_key}" if idempotency_key else ""
            if replay_key:
                existing_id = str(replays.get(replay_key) or "")
                if existing_id:
                    existing = next(
                        (item for item in events if item.get("event_id") == existing_id),
                        None,
                    )
                    if existing is not None:
                        return self._event_from_dict(existing)
                    raise SandboxGatewayError(
                        GatewayErrorCode.STATE_CORRUPT,
                        "event replay index refers to a missing retained event",
                        operation="append_next_event",
                    )
            sequence = int(events[-1]["sequence"]) + 1 if events else 1
            event = GatewayEvent.build(
                kind=kind,
                run_id=run_id,
                task_id=task_id,
                session_id=session_id,
                worker_id=worker_id,
                sequence=sequence,
                payload=payload,
                causation_id=causation_id,
                correlation_id=correlation_id,
            )
            events.append(event.to_dict())
            if replay_key:
                replays[replay_key] = event.event_id
            if len(events) > self.event_history_limit:
                removed = events[: len(events) - self.event_history_limit]
                del events[: len(removed)]
                removed_ids = {str(item.get("event_id") or "") for item in removed}
                for key, value in tuple(replays.items()):
                    if value in removed_ids:
                        del replays[key]
            return event

    def next_event_sequence(self, session_id: str) -> int:
        state = self.snapshot()
        events = state["events"].get(session_id, [])
        return int(events[-1]["sequence"]) + 1 if events else 1

    def list_events(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[GatewayEvent, ...]:
        state = self.snapshot()
        return tuple(
            self._event_from_dict(item)
            for item in state["events"].get(session_id, [])
            if int(item["sequence"]) > after_sequence
        )

    def save_receipt(
        self,
        receipt_id: str,
        value: Mapping[str, Any],
        *,
        idempotency_key: str = "",
    ) -> Mapping[str, Any]:
        with self.transaction() as state:
            existing = state["receipts"].get(receipt_id)
            projected = dict(value)
            if existing is not None:
                if digest(existing) != digest(projected):
                    raise SandboxGatewayError(
                        GatewayErrorCode.STATE_CONFLICT,
                        "receipt identity collision",
                        operation="save_receipt",
                    )
                return dict(existing)
            state["receipts"][receipt_id] = projected
            if idempotency_key:
                current = state["idempotency"].get(idempotency_key)
                if current and current != receipt_id:
                    raise SandboxGatewayError(
                        GatewayErrorCode.STATE_CONFLICT,
                        "idempotency key is already bound to another receipt",
                        operation="save_receipt",
                    )
                state["idempotency"][idempotency_key] = receipt_id
            return projected

    def receipt_for_idempotency(self, idempotency_key: str) -> Mapping[str, Any] | None:
        state = self.snapshot()
        receipt_id = state["idempotency"].get(idempotency_key)
        if not receipt_id:
            return None
        value = state["receipts"].get(receipt_id)
        return dict(value) if value is not None else None

    def save_quarantine(self, record: QuarantineRecord) -> QuarantineRecord:
        with self.transaction() as state:
            existing = state["quarantine"].get(record.quarantine_id)
            if existing is not None:
                candidate = self._quarantine_from_dict(existing)
                if candidate.to_dict() != record.to_dict():
                    raise SandboxGatewayError(
                        GatewayErrorCode.STATE_CONFLICT,
                        "quarantine identity collision",
                        operation="save_quarantine",
                    )
                return candidate
            state["quarantine"][record.quarantine_id] = record.to_dict()
            return record

    def require_quarantine(self, quarantine_id: str) -> QuarantineRecord:
        state = self.snapshot()
        value = state["quarantine"].get(quarantine_id)
        if value is None:
            raise SandboxGatewayError(
                GatewayErrorCode.INVALID_REQUEST,
                f"quarantine record not found: {quarantine_id}",
                operation="require_quarantine",
            )
        return self._quarantine_from_dict(value)

    def release_quarantine(
        self,
        quarantine_id: str,
        *,
        reason: str,
    ) -> QuarantineRecord:
        with self.transaction() as state:
            value = state["quarantine"].get(quarantine_id)
            if value is None:
                raise SandboxGatewayError(
                    GatewayErrorCode.INVALID_REQUEST,
                    "quarantine record not found",
                    operation="release_quarantine",
                )
            record = self._quarantine_from_dict(value)
            if record.released:
                return record
            updated = replace(
                record,
                released=True,
                release_reason=str(reason),
                released_at=time.time(),
            )
            state["quarantine"][quarantine_id] = updated.to_dict()
            return updated

    def save_lease(self, lease: GatewayLease) -> GatewayLease:
        with self.transaction() as state:
            state["leases"][lease.lease_id] = lease.to_dict()
            return lease

    def require_lease(self, lease_id: str) -> GatewayLease:
        state = self.snapshot()
        value = state["leases"].get(lease_id)
        if value is None:
            raise SandboxGatewayError(
                GatewayErrorCode.LEASE_EXPIRED,
                "gateway lease not found",
                operation="require_lease",
            )
        return GatewayLease.from_dict(value)

    def compact(self) -> Mapping[str, int]:
        now = time.time()
        with self.transaction() as state:
            before_bindings = len(state["permission_bindings"])
            before_leases = len(state["leases"])
            state["permission_bindings"] = {
                key: value
                for key, value in state["permission_bindings"].items()
                if (
                    value.get("consumed_at") is None
                    and float(value.get("expires_at", 0)) > now
                )
                or float(value.get("expires_at", 0)) > now - 86_400
            }
            state["leases"] = {
                key: value
                for key, value in state["leases"].items()
                if (
                    value.get("released_at") is None
                    and float(value.get("expires_at", 0)) > now
                )
                or float(value.get("expires_at", 0)) > now - 86_400
            }
            return {
                "permission_bindings_removed": before_bindings - len(state["permission_bindings"]),
                "leases_removed": before_leases - len(state["leases"]),
            }

    def custody_descriptor(self) -> Mapping[str, Any]:
        state = self.snapshot()
        return {
            "schema": state.get("schema"),
            "revision": state.get("revision"),
            "canonical_owner": "SandboxGatewayRuntime",
            "permission_owner": "typescript.PermissionCoordinator",
            "workspace_owner": "WorkspaceManagerRuntime",
            "stores_secret_material": False,
            "sessions": len(state["sessions"]),
            "permission_bindings": len(state["permission_bindings"]),
            "events": sum(len(items) for items in state["events"].values()),
            "receipts": len(state["receipts"]),
            "quarantine": len(state["quarantine"]),
        }

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SandboxGatewayError(
                GatewayErrorCode.STATE_CORRUPT,
                f"sandbox gateway state cannot be read: {type(error).__name__}",
                operation="read_state",
            ) from error
        if value.get("schema") != STATE_SCHEMA:
            raise SandboxGatewayError(
                GatewayErrorCode.STATE_CORRUPT,
                "sandbox gateway state schema mismatch",
                operation="read_state",
            )
        value.setdefault("event_replays", {})
        return value

    def _write_unlocked(self, state: Mapping[str, Any]) -> None:
        temporary = self.state_path.with_suffix(
            f".{os.getpid()}.{threading.get_ident()}.tmp"
        )
        encoded = canonical_json(state) + "\n"
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _session_identity(record: GatewaySessionRecord) -> tuple[str, ...]:
        return (
            record.session_id,
            record.run_id,
            record.task_id,
            record.workspace_id,
            record.worker_id,
            record.backend_id,
        )

    @staticmethod
    def _event_from_dict(value: Mapping[str, Any]) -> GatewayEvent:
        return GatewayEvent(
            event_id=str(value["event_id"]),
            kind=GatewayEventKind(str(value["kind"])),
            run_id=str(value["run_id"]),
            task_id=str(value["task_id"]),
            session_id=str(value["session_id"]),
            worker_id=str(value["worker_id"]),
            sequence=int(value["sequence"]),
            payload=dict(value.get("payload") or {}),
            causation_id=str(value.get("causation_id", "")),
            correlation_id=str(value.get("correlation_id", "")),
            occurred_at=float(value["occurred_at"]),
            schema=str(value.get("schema", "")),
        )

    @staticmethod
    def _quarantine_from_dict(value: Mapping[str, Any]) -> QuarantineRecord:
        return QuarantineRecord(
            quarantine_id=str(value["quarantine_id"]),
            session_id=str(value["session_id"]),
            request_id=str(value["request_id"]),
            content_digest=str(value["content_digest"]),
            logical_path=str(value["logical_path"]),
            reason_codes=tuple(str(item) for item in value.get("reason_codes", ())),
            released=bool(value.get("released", False)),
            release_reason=str(value.get("release_reason", "")),
            created_at=float(value["created_at"]),
            released_at=(
                float(value["released_at"])
                if value.get("released_at") is not None
                else None
            ),
            metadata=dict(value.get("metadata") or {}),
        )
