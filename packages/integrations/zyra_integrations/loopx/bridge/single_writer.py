from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

import psutil

from .contracts import (
    SingleWriterConflictError,
    SingleWriterFenceLostError,
    stable_digest,
)


WRITER_LOCK_SCHEMA = "zyra.loopx-single-writer-lock/v1"
WRITER_EPOCH_SCHEMA = "zyra.loopx-single-writer-epoch/v1"


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SingleWriterConflictError(
            "LoopX writer lock metadata is unreadable; refusing unsafe takeover.",
            code="loopx_writer_lock_corrupt",
            details={"path": str(path), "error": str(error)},
        ) from error
    if not isinstance(value, dict):
        raise SingleWriterConflictError(
            "LoopX writer lock metadata is not an object.",
            code="loopx_writer_lock_corrupt",
            details={"path": str(path)},
        )
    return value


def _process_identity(pid: int) -> tuple[bool, float | None]:
    try:
        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return False, None
        return True, float(process.create_time())
    except (psutil.NoSuchProcess, ProcessLookupError):
        return False, None
    except (psutil.AccessDenied, PermissionError):
        return True, None


def workspace_identity(workspace_root: Path) -> str:
    normalized = os.path.normcase(str(workspace_root.resolve()))
    return f"workspace:{stable_digest(normalized)[:24]}"


@dataclass(slots=True)
class WriterFence:
    owner: "LoopXSingleWriter"
    writer_id: str
    fencing_epoch: int
    fencing_token: str
    pid: int
    process_started_at: float
    acquired_at: float
    _released: bool = False

    def assert_owned(self) -> None:
        if self._released:
            raise SingleWriterFenceLostError(
                "LoopX writer fence has already been released.",
                code="loopx_writer_fence_lost",
                details={"writer_id": self.writer_id, "fencing_epoch": self.fencing_epoch},
            )
        self.owner._assert_fence(self)

    def heartbeat(self) -> None:
        self.assert_owned()
        self.owner._heartbeat(self)

    def release(self) -> None:
        if self._released:
            return
        try:
            self.owner._release(self)
        finally:
            self._released = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": WRITER_LOCK_SCHEMA,
            "writer_id": self.writer_id,
            "workspace_id": self.owner.workspace_id,
            "pid": self.pid,
            "process_started_at": self.process_started_at,
            "fencing_epoch": self.fencing_epoch,
            "fencing_token": self.fencing_token,
            "acquired_at_epoch": self.acquired_at,
        }

    def __enter__(self) -> "WriterFence":
        self.assert_owned()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.release()


class LoopXSingleWriter:
    """Cross-process, Windows-safe single writer with monotonic fencing.

    The durable file uses atomic O_EXCL creation instead of LoopX's POSIX-only
    fcntl helper. A lock is never stolen merely because its heartbeat is old:
    takeover requires a dead process or a proven PID-reuse identity mismatch.
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        state_root: Path | None = None,
        writer_id: str | None = None,
        clock: Callable[[], float] = time.time,
        poll_interval_seconds: float = 0.02,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        expected_state_root = (self.workspace_root / ".zyra" / "loopx" / "state").resolve()
        self.state_root = (state_root or expected_state_root).resolve()
        if self.state_root != expected_state_root:
            raise SingleWriterConflictError(
                "LoopX writer state root violates the fixed workspace-local contract.",
                code="loopx_workspace_state_root_mismatch",
                details={
                    "workspace_root": str(self.workspace_root),
                    "expected": str(expected_state_root),
                    "actual": str(self.state_root),
                },
            )
        self.workspace_id = workspace_identity(self.workspace_root)
        self.writer_id = writer_id or f"loopx-writer-{os.getpid()}-{uuid4().hex}"
        self.clock = clock
        self.poll_interval_seconds = max(0.001, float(poll_interval_seconds))
        self.bridge_root = self.state_root / "bridge"
        self.lock_path = self.bridge_root / "writer.lock"
        self.epoch_path = self.bridge_root / "writer-epoch.json"
        self._guard = threading.RLock()
        self._active: WriterFence | None = None

    def acquire(self, *, timeout_seconds: float = 0.0) -> WriterFence:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self._guard:
            if self._active is not None and not self._active._released:
                self._active.assert_owned()
                return self._active
            while True:
                try:
                    fence = self._try_acquire()
                except FileExistsError:
                    stale = self._inspect_existing()
                    if stale is not None:
                        self._retire_stale(stale)
                        continue
                    if time.monotonic() >= deadline:
                        owner = self.current_owner()
                        raise SingleWriterConflictError(
                            "Another live LoopX bridge writer owns this workspace.",
                            code="loopx_writer_conflict",
                            details={
                                "workspace_id": self.workspace_id,
                                "owner": owner,
                            },
                        )
                    time.sleep(self.poll_interval_seconds)
                    continue
                self._active = fence
                return fence

    def current_owner(self) -> dict[str, Any] | None:
        if not self.lock_path.exists():
            return None
        try:
            return _read_json(self.lock_path)
        except SingleWriterConflictError:
            return {"schema": "corrupt", "path": str(self.lock_path)}

    def _next_epoch(self, *, stale_epoch: int = 0) -> int:
        persisted = 0
        if self.epoch_path.exists():
            value = _read_json(self.epoch_path)
            if value.get("schema") != WRITER_EPOCH_SCHEMA:
                raise SingleWriterConflictError(
                    "LoopX writer epoch metadata has an unsupported schema.",
                    code="loopx_writer_epoch_corrupt",
                    details={"path": str(self.epoch_path)},
                )
            persisted = int(value.get("fencing_epoch") or 0)
        return max(persisted, stale_epoch) + 1

    def _try_acquire(self) -> WriterFence:
        self.bridge_root.mkdir(parents=True, exist_ok=True)
        stale_epoch = 0
        if self.lock_path.exists():
            try:
                stale_epoch = int(_read_json(self.lock_path).get("fencing_epoch") or 0)
            except SingleWriterConflictError:
                stale_epoch = 0
        epoch = self._next_epoch(stale_epoch=stale_epoch)
        process_started = float(psutil.Process(os.getpid()).create_time())
        acquired_at = float(self.clock())
        token = f"fence:{epoch}:{uuid4().hex}"
        payload = {
            "schema": WRITER_LOCK_SCHEMA,
            "writer_id": self.writer_id,
            "workspace_id": self.workspace_id,
            "pid": os.getpid(),
            "process_started_at": process_started,
            "fencing_epoch": epoch,
            "fencing_token": token,
            "acquired_at_epoch": acquired_at,
            "heartbeat_at_epoch": acquired_at,
        }
        descriptor = os.open(
            str(self.lock_path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )
        try:
            os.write(
                descriptor,
                (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"),
            )
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass
            raise
        else:
            os.close(descriptor)
        epoch_payload = {
            "schema": WRITER_EPOCH_SCHEMA,
            "workspace_id": self.workspace_id,
            "fencing_epoch": epoch,
            "fencing_token_digest": stable_digest(token),
            "updated_at_epoch": acquired_at,
        }
        _atomic_write_json(self.epoch_path, epoch_payload)
        return WriterFence(
            owner=self,
            writer_id=self.writer_id,
            fencing_epoch=epoch,
            fencing_token=token,
            pid=os.getpid(),
            process_started_at=process_started,
            acquired_at=acquired_at,
        )

    def _inspect_existing(self) -> dict[str, Any] | None:
        value = _read_json(self.lock_path)
        if value.get("schema") != WRITER_LOCK_SCHEMA:
            raise SingleWriterConflictError(
                "LoopX writer lock has an unsupported schema.",
                code="loopx_writer_lock_corrupt",
                details={"path": str(self.lock_path), "schema": value.get("schema")},
            )
        pid = int(value.get("pid") or 0)
        declared_start = float(value.get("process_started_at") or 0.0)
        if pid <= 0 or declared_start <= 0:
            raise SingleWriterConflictError(
                "LoopX writer lock has no verifiable process identity.",
                code="loopx_writer_lock_corrupt",
                details={"path": str(self.lock_path)},
            )
        active, observed_start = _process_identity(pid)
        if active and observed_start is None:
            return None
        if active and abs(float(observed_start) - declared_start) < 0.01:
            return None
        return {
            **value,
            "inactive_confirmed": True,
            "inactive_reason": (
                "process_not_running"
                if not active
                else "pid_reused_process_identity_mismatch"
            ),
            "observed_process_started_at": observed_start,
        }

    def _retire_stale(self, stale: Mapping[str, Any]) -> None:
        token = str(stale.get("fencing_token") or "")
        current = _read_json(self.lock_path)
        if str(current.get("fencing_token") or "") != token:
            return
        tombstone = self.bridge_root / (
            f"writer.stale.{int(stale.get('fencing_epoch') or 0)}.{uuid4().hex}.json"
        )
        try:
            os.replace(self.lock_path, tombstone)
        except FileNotFoundError:
            return
        try:
            tombstone.unlink()
        except FileNotFoundError:
            pass

    def _assert_fence(self, fence: WriterFence) -> None:
        if not self.lock_path.exists():
            raise SingleWriterFenceLostError(
                "LoopX writer lock disappeared.",
                code="loopx_writer_fence_lost",
                details=fence.to_dict(),
            )
        current = _read_json(self.lock_path)
        matches = (
            current.get("schema") == WRITER_LOCK_SCHEMA
            and current.get("workspace_id") == self.workspace_id
            and current.get("writer_id") == fence.writer_id
            and int(current.get("fencing_epoch") or 0) == fence.fencing_epoch
            and current.get("fencing_token") == fence.fencing_token
            and int(current.get("pid") or 0) == fence.pid
            and abs(
                float(current.get("process_started_at") or 0.0)
                - fence.process_started_at
            )
            < 0.01
        )
        if not matches:
            raise SingleWriterFenceLostError(
                "LoopX writer fencing token no longer matches durable ownership.",
                code="loopx_writer_fence_lost",
                details={
                    "expected": fence.to_dict(),
                    "observed": current,
                },
            )

    def _heartbeat(self, fence: WriterFence) -> None:
        with self._guard:
            self._assert_fence(fence)
            value = _read_json(self.lock_path)
            value["heartbeat_at_epoch"] = float(self.clock())
            _atomic_write_json(self.lock_path, value)
            self._assert_fence(fence)

    def _release(self, fence: WriterFence) -> None:
        with self._guard:
            self._assert_fence(fence)
            current = _read_json(self.lock_path)
            if current.get("fencing_token") == fence.fencing_token:
                self.lock_path.unlink()
            if self._active is fence:
                self._active = None


__all__ = [
    "WRITER_EPOCH_SCHEMA",
    "WRITER_LOCK_SCHEMA",
    "LoopXSingleWriter",
    "WriterFence",
    "workspace_identity",
]
