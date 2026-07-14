from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping

from .errors import WorkspaceError, WorkspaceErrorCode, WorkspaceStoreError


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def fsync_directory(path: str | Path) -> None:
    directory = Path(path)
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write_bytes(
    target: str | Path,
    content: bytes,
    *,
    mode: int | None = None,
    expected_parent_identity: str = "",
) -> Path:
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    before_identity = file_identity(path.parent)
    if expected_parent_identity and before_identity != expected_parent_identity:
        raise WorkspaceStoreError(
            WorkspaceErrorCode.FILE_IDENTITY_CHANGED,
            "Workspace parent directory identity changed before atomic write.",
            operation="atomic_write",
            path=str(path.parent),
            expected=expected_parent_identity,
            actual=before_identity,
        )
    temporary = path.with_name(f".{path.name}.zyra-{os.getpid()}-{threading.get_ident()}-{secrets.token_hex(6)}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(temporary, flags, 0o600 if mode is None else mode)
        try:
            offset = 0
            view = memoryview(content)
            while offset < len(view):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("short atomic workspace write")
                offset += written
            os.fsync(descriptor)
            if mode is not None:
                try:
                    os.fchmod(descriptor, mode)
                except (AttributeError, OSError):
                    pass
        finally:
            os.close(descriptor)
        after_identity = file_identity(path.parent)
        if before_identity != after_identity:
            raise WorkspaceStoreError(
                WorkspaceErrorCode.FILE_IDENTITY_CHANGED,
                "Workspace parent directory identity changed while staging an atomic write.",
                operation="atomic_write",
                path=str(path.parent),
                expected=before_identity,
                actual=after_identity,
            )
        os.replace(temporary, path)
        fsync_directory(path.parent)
        return path
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(target: str | Path, value: Mapping[str, Any] | list[Any]) -> Path:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    return atomic_write_bytes(target, payload)


def read_json_object(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WorkspaceStoreError(
            WorkspaceErrorCode.STORE_CORRUPT,
            "Workspace state is not valid durable JSON.",
            operation="read_state",
            path=str(target),
            metadata={"exception_type": type(error).__name__},
        ) from error
    if not isinstance(value, dict):
        raise WorkspaceStoreError(
            WorkspaceErrorCode.STORE_CORRUPT,
            "Workspace state must be a JSON object.",
            operation="read_state",
            path=str(target),
        )
    return value


def file_identity(path: str | Path, *, follow_symlinks: bool = False) -> str:
    target = Path(path)
    try:
        info = target.stat() if follow_symlinks else target.lstat()
    except FileNotFoundError:
        return "absent"
    return f"{int(info.st_dev):x}:{int(info.st_ino):x}:{stat.S_IFMT(info.st_mode):x}"


def file_metadata(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    info = target.lstat()
    return {
        "identity": file_identity(target),
        "mode": int(info.st_mode),
        "size": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
        "nlink": int(info.st_nlink),
        "is_file": stat.S_ISREG(info.st_mode),
        "is_directory": stat.S_ISDIR(info.st_mode),
        "is_symlink": stat.S_ISLNK(info.st_mode),
    }


@dataclass(frozen=True, slots=True)
class OpenedFileIdentity:
    path: Path
    descriptor: int
    identity: str
    size: int
    mtime_ns: int
    mode: int


@contextmanager
def open_regular_file_no_follow(path: str | Path, *, write: bool = False) -> Iterator[OpenedFileIdentity]:
    target = Path(path)
    flags = os.O_RDWR if write else os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(target, flags)
    except OSError as error:
        raise WorkspaceError(
            WorkspaceErrorCode.SYMLINK_ESCAPE,
            "Workspace files must be regular files opened without following links.",
            operation="open_file",
            path=str(target),
            metadata={"errno": error.errno},
        ) from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise WorkspaceError(
                WorkspaceErrorCode.PATH_OUTSIDE_ROOT,
                "Workspace file operations reject devices, sockets, pipes, and directories.",
                operation="open_file",
                path=str(target),
            )
        identity = f"{int(info.st_dev):x}:{int(info.st_ino):x}:{stat.S_IFMT(info.st_mode):x}"
        path_identity = file_identity(target)
        if path_identity != identity:
            raise WorkspaceError(
                WorkspaceErrorCode.FILE_IDENTITY_CHANGED,
                "Workspace file identity changed while opening the file.",
                operation="open_file",
                path=str(target),
                expected=path_identity,
                actual=identity,
            )
        yield OpenedFileIdentity(
            path=target,
            descriptor=descriptor,
            identity=identity,
            size=int(info.st_size),
            mtime_ns=int(info.st_mtime_ns),
            mode=int(info.st_mode),
        )
    finally:
        os.close(descriptor)


def read_descriptor(descriptor: int, *, start: int = 0, length: int | None = None) -> bytes:
    if start < 0 or (length is not None and length < 0):
        raise ValueError("read offsets and lengths must be non-negative")
    os.lseek(descriptor, start, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = length
    while remaining is None or remaining > 0:
        size = 1024 * 1024 if remaining is None else min(1024 * 1024, remaining)
        chunk = os.read(descriptor, size)
        if not chunk:
            break
        chunks.append(chunk)
        if remaining is not None:
            remaining -= len(chunk)
    return b"".join(chunks)


class KeyedLockPool:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def lock_for(self, key: str) -> threading.RLock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
            return lock

    @contextmanager
    def acquire_many(self, keys: list[str] | tuple[str, ...]) -> Iterator[None]:
        ordered = sorted(set(str(item) for item in keys))
        locks = [self.lock_for(item) for item in ordered]
        for lock in locks:
            lock.acquire()
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()


def safe_copy_stream(source: BinaryIO, destination: BinaryIO, *, limit: int, chunk_size: int = 1024 * 1024) -> int:
    total = 0
    while True:
        chunk = source.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise WorkspaceError(
                WorkspaceErrorCode.SINGLE_FILE_LIMIT_EXCEEDED,
                "Workspace stream exceeded the configured single-file limit.",
                operation="copy_stream",
                expected=limit,
                actual=total,
            )
        destination.write(chunk)
    destination.flush()
    try:
        os.fsync(destination.fileno())
    except (AttributeError, OSError):
        pass
    return total
