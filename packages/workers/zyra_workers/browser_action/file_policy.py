from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
import unicodedata
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO, Callable

from .models import digest_value, stable_id


class BrowserFilePolicyError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


class FileIntent(StrEnum):
    UPLOAD = "upload"
    DOWNLOAD = "download"
    PDF = "pdf"
    SCREENSHOT = "screenshot"
    TRACE = "trace"


class FileKind(StrEnum):
    REGULAR = "regular"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    DEVICE = "device"
    OTHER = "other"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class FileIdentity:
    path: str
    real_path: str
    kind: FileKind
    size: int
    modified_ns: int
    device: int
    inode: int
    mode: int

    @property
    def stable_digest(self) -> str:
        return digest_value(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "real_path": self.real_path,
            "kind": str(self.kind),
            "size": self.size,
            "modified_ns": self.modified_ns,
            "device": self.device,
            "inode": self.inode,
            "mode": self.mode,
        }


@dataclass(frozen=True, slots=True)
class FilePolicyConfig:
    upload_roots: tuple[str, ...]
    download_root: str
    artifact_root: str
    max_upload_bytes: int = 256 * 1024 * 1024
    max_download_bytes: int = 512 * 1024 * 1024
    max_files_per_action: int = 32
    allow_hidden_uploads: bool = False
    allow_symlinks: bool = False
    allowed_upload_suffixes: frozenset[str] = frozenset()
    denied_upload_suffixes: frozenset[str] = frozenset({".lnk", ".url", ".scf"})

    def __post_init__(self) -> None:
        roots = tuple(str(Path(item).resolve(strict=False)) for item in self.upload_roots)
        if not roots:
            raise ValueError("browser file policy requires an upload root")
        object.__setattr__(self, "upload_roots", roots)
        object.__setattr__(self, "download_root", str(Path(self.download_root).resolve(strict=False)))
        object.__setattr__(self, "artifact_root", str(Path(self.artifact_root).resolve(strict=False)))
        object.__setattr__(
            self,
            "allowed_upload_suffixes",
            frozenset(normalize_suffix(item) for item in self.allowed_upload_suffixes),
        )
        object.__setattr__(
            self,
            "denied_upload_suffixes",
            frozenset(normalize_suffix(item) for item in self.denied_upload_suffixes),
        )
        if self.max_upload_bytes < 1 or self.max_download_bytes < 1 or self.max_files_per_action < 1:
            raise ValueError("browser file policy limits must be positive")

    @property
    def digest(self) -> str:
        return digest_value(
            {
                "upload_roots": self.upload_roots,
                "download_root": self.download_root,
                "artifact_root": self.artifact_root,
                "max_upload_bytes": self.max_upload_bytes,
                "max_download_bytes": self.max_download_bytes,
                "max_files_per_action": self.max_files_per_action,
                "allow_hidden_uploads": self.allow_hidden_uploads,
                "allow_symlinks": self.allow_symlinks,
                "allowed_upload_suffixes": sorted(self.allowed_upload_suffixes),
                "denied_upload_suffixes": sorted(self.denied_upload_suffixes),
            }
        )


@dataclass(frozen=True, slots=True)
class UploadCandidate:
    requested_path: str
    identity: FileIdentity
    root: str

    @property
    def basename(self) -> str:
        return Path(self.identity.real_path).name

    def public_dict(self) -> dict[str, Any]:
        return {
            "requested_path_hash": digest_value(self.requested_path),
            "identity": self.identity.to_dict(),
            "root": self.root,
            "basename": self.basename,
        }


@dataclass(frozen=True, slots=True)
class FileReceipt:
    receipt_id: str
    action_id: str
    intent: FileIntent
    policy_digest: str
    uploads: tuple[UploadCandidate, ...] = ()
    destination_path: str = ""
    destination_root: str = ""
    expected_name: str = ""
    quota_bytes: int = 0

    @property
    def binding_digest(self) -> str:
        return digest_value(self.public_dict())

    def public_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "action_id": self.action_id,
            "intent": str(self.intent),
            "policy_digest": self.policy_digest,
            "uploads": [item.public_dict() for item in self.uploads],
            "destination_path": self.destination_path,
            "destination_root": self.destination_root,
            "expected_name": self.expected_name,
            "quota_bytes": self.quota_bytes,
        }


@dataclass(frozen=True, slots=True)
class OpenedUpload:
    candidate: UploadCandidate
    stream: BinaryIO = field(repr=False, compare=False)
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class CompletedFile:
    path: str
    name: str
    size: int
    sha256: str
    receipt_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "size": self.size,
            "sha256": self.sha256,
            "receipt_id": self.receipt_id,
        }


class UploadLease(AbstractContextManager[tuple[OpenedUpload, ...]]):
    def __init__(self, opened: tuple[OpenedUpload, ...]) -> None:
        self.opened = opened

    def __enter__(self) -> tuple[OpenedUpload, ...]:
        return self.opened

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        for item in self.opened:
            item.stream.close()


class DownloadWriter(AbstractContextManager["DownloadWriter"]):
    def __init__(
        self,
        *,
        receipt: FileReceipt,
        final_path: Path,
        temporary_path: Path,
        stream: BinaryIO,
        release: Callable[[], None] | None = None,
    ) -> None:
        self.receipt = receipt
        self.final_path = final_path
        self.temporary_path = temporary_path
        self.stream = stream
        self._hash = hashlib.sha256()
        self._size = 0
        self._completed = False
        self._release = release
        self._released = False

    def __enter__(self) -> "DownloadWriter":
        return self

    def write(self, data: bytes) -> int:
        if self._completed:
            raise BrowserFilePolicyError("download_already_completed", "download writer has already completed")
        if not isinstance(data, bytes):
            raise TypeError("download writer accepts bytes only")
        if self._size + len(data) > self.receipt.quota_bytes:
            raise BrowserFilePolicyError("download_quota_exceeded", "download exceeds the approved byte quota")
        written = self.stream.write(data)
        self._hash.update(data[:written])
        self._size += written
        return written

    def complete(self) -> CompletedFile:
        if self._completed:
            raise BrowserFilePolicyError("download_already_completed", "download writer has already completed")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        assert_contained(self.temporary_path, Path(self.receipt.destination_root), resolve_leaf=False)
        os.replace(self.temporary_path, self.final_path)
        identity = inspect_file(self.final_path, follow_symlinks=False)
        if identity.kind != FileKind.REGULAR or identity.size != self._size:
            raise BrowserFilePolicyError("download_identity_changed", "completed download identity is invalid")
        self._completed = True
        return CompletedFile(
            path=str(self.final_path),
            name=self.final_path.name,
            size=self._size,
            sha256=self._hash.hexdigest(),
            receipt_id=self.receipt.receipt_id,
        )

    def __exit__(self, exc_type: Any, _value: Any, _traceback: Any) -> None:
        if not self.stream.closed:
            self.stream.close()
        if not self._completed and self.temporary_path.exists():
            try:
                self.temporary_path.unlink()
            except OSError:
                pass
        if not self._released and self._release is not None:
            self._released = True
            self._release()


class BrowserFilePolicy:
    """Workspace-owned upload and artifact/download containment policy.

    Preflight only resolves and stats files; it never opens upload contents or
    creates destinations.  ``open_uploads`` and ``open_download`` are intended
    to be called only after the exact 03A grant has been consumed.
    """

    def __init__(self, config: FilePolicyConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._claimed_destinations: set[str] = set()

    def preflight_upload(self, *, action_id: str, paths: Iterable[str]) -> FileReceipt:
        requested = tuple(str(item) for item in paths)
        if not requested:
            raise BrowserFilePolicyError("upload_empty", "upload action requires at least one file")
        if len(requested) > self.config.max_files_per_action:
            raise BrowserFilePolicyError("upload_file_count_exceeded", "upload action contains too many files")
        candidates: list[UploadCandidate] = []
        total = 0
        for raw in requested:
            candidate = self._preflight_upload_candidate(raw)
            total += candidate.identity.size
            if total > self.config.max_upload_bytes:
                raise BrowserFilePolicyError("upload_quota_exceeded", "upload files exceed the approved size limit")
            candidates.append(candidate)
        receipt_id = stable_id(
            "brfile",
            action_id,
            FileIntent.UPLOAD,
            self.config.digest,
            [item.public_dict() for item in candidates],
        )
        return FileReceipt(
            receipt_id=receipt_id,
            action_id=action_id,
            intent=FileIntent.UPLOAD,
            policy_digest=self.config.digest,
            uploads=tuple(candidates),
            quota_bytes=self.config.max_upload_bytes,
        )

    def preflight_destination(
        self,
        *,
        action_id: str,
        filename: str,
        intent: FileIntent = FileIntent.DOWNLOAD,
        expected_bytes: int | None = None,
    ) -> FileReceipt:
        if intent == FileIntent.UPLOAD:
            raise BrowserFilePolicyError("invalid_file_intent", "upload cannot use a destination receipt")
        safe_name = validate_filename(filename)
        root = Path(self.config.artifact_root if intent in {FileIntent.PDF, FileIntent.SCREENSHOT, FileIntent.TRACE} else self.config.download_root)
        destination = root / safe_name
        assert_contained(destination, root, resolve_leaf=False)
        quota = self.config.max_download_bytes
        if expected_bytes is not None and (expected_bytes < 0 or expected_bytes > quota):
            raise BrowserFilePolicyError("download_quota_exceeded", "expected destination size exceeds policy quota")
        with self._lock:
            key = os.path.normcase(str(destination.resolve(strict=False)))
            if key in self._claimed_destinations or destination.exists():
                raise BrowserFilePolicyError("destination_collision", "browser file destination already exists or is claimed")
        receipt_id = stable_id(
            "brfile",
            action_id,
            intent,
            self.config.digest,
            str(destination),
            quota,
        )
        return FileReceipt(
            receipt_id=receipt_id,
            action_id=action_id,
            intent=intent,
            policy_digest=self.config.digest,
            destination_path=str(destination),
            destination_root=str(root),
            expected_name=safe_name,
            quota_bytes=quota,
        )

    def revalidate(self, receipt: FileReceipt) -> None:
        if receipt.policy_digest != self.config.digest:
            raise BrowserFilePolicyError("file_policy_changed", "browser file policy changed after approval")
        if receipt.intent == FileIntent.UPLOAD:
            for candidate in receipt.uploads:
                current = inspect_file(Path(candidate.identity.path), follow_symlinks=self.config.allow_symlinks)
                if current.stable_digest != candidate.identity.stable_digest:
                    raise BrowserFilePolicyError(
                        "upload_identity_changed",
                        "upload file changed after action approval",
                        details={"path_hash": digest_value(candidate.requested_path)},
                    )
                assert_contained(Path(current.real_path), Path(candidate.root), resolve_leaf=True)
        else:
            destination = Path(receipt.destination_path)
            assert_contained(destination, Path(receipt.destination_root), resolve_leaf=False)
            if destination.exists():
                raise BrowserFilePolicyError("destination_collision", "browser file destination appeared after approval")

    def open_uploads(self, receipt: FileReceipt) -> UploadLease:
        if receipt.intent != FileIntent.UPLOAD:
            raise BrowserFilePolicyError("receipt_intent_mismatch", "receipt does not authorize upload file reads")
        self.revalidate(receipt)
        opened: list[OpenedUpload] = []
        try:
            for candidate in receipt.uploads:
                fd = secure_open_read(Path(candidate.identity.path), allow_symlink=self.config.allow_symlinks)
                stream = os.fdopen(fd, "rb", closefd=True)
                current = identity_from_stat(Path(candidate.identity.path), Path(candidate.identity.real_path), os.fstat(stream.fileno()))
                if current.stable_digest != candidate.identity.stable_digest:
                    stream.close()
                    raise BrowserFilePolicyError("upload_identity_changed", "upload file changed while being opened")
                sha256, size = hash_open_stream(stream, receipt.quota_bytes)
                stream.seek(0)
                opened.append(OpenedUpload(candidate, stream, sha256, size))
        except Exception:
            for item in opened:
                item.stream.close()
            raise
        return UploadLease(tuple(opened))

    def open_download(self, receipt: FileReceipt) -> DownloadWriter:
        if receipt.intent == FileIntent.UPLOAD:
            raise BrowserFilePolicyError("receipt_intent_mismatch", "upload receipt cannot create a destination")
        self.revalidate(receipt)
        destination = Path(receipt.destination_path)
        root = Path(receipt.destination_root)
        root.mkdir(parents=True, exist_ok=True)
        assert_contained(root, root, resolve_leaf=True)
        key = os.path.normcase(str(destination.resolve(strict=False)))
        with self._lock:
            if key in self._claimed_destinations:
                raise BrowserFilePolicyError("destination_collision", "browser file destination is already claimed")
            self._claimed_destinations.add(key)
        temporary = root / f".{destination.name}.{receipt.receipt_id}.partial"
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(temporary, flags, 0o600)
            stream = os.fdopen(fd, "wb", closefd=True)
        except Exception:
            with self._lock:
                self._claimed_destinations.discard(key)
            raise
        def release() -> None:
            with self._lock:
                self._claimed_destinations.discard(key)

        return DownloadWriter(
            receipt=receipt,
            final_path=destination,
            temporary_path=temporary,
            stream=stream,
            release=release,
        )

    def _preflight_upload_candidate(self, raw: str) -> UploadCandidate:
        if not raw or "\x00" in raw:
            raise BrowserFilePolicyError("invalid_upload_path", "upload path is empty or contains NUL")
        path = Path(raw).expanduser()
        lexical = path.resolve(strict=False)
        selected_root = next(
            (root for root in map(Path, self.config.upload_roots) if is_contained(lexical, root.resolve(strict=False))),
            None,
        )
        if selected_root is None:
            raise BrowserFilePolicyError("upload_outside_root", "upload path is outside all approved roots")
        identity = inspect_file(path, follow_symlinks=self.config.allow_symlinks)
        if identity.kind == FileKind.SYMLINK and not self.config.allow_symlinks:
            raise BrowserFilePolicyError("upload_symlink_denied", "upload symlinks are prohibited")
        if identity.kind != FileKind.REGULAR:
            raise BrowserFilePolicyError("upload_not_regular", "upload path must identify a regular file")
        assert_contained(Path(identity.real_path), selected_root, resolve_leaf=True)
        if identity.size > self.config.max_upload_bytes:
            raise BrowserFilePolicyError("upload_quota_exceeded", "upload file exceeds size limit")
        suffix = normalize_suffix(path.suffix)
        if suffix in self.config.denied_upload_suffixes:
            raise BrowserFilePolicyError("upload_suffix_denied", f"upload suffix {suffix!r} is denied")
        if self.config.allowed_upload_suffixes and suffix not in self.config.allowed_upload_suffixes:
            raise BrowserFilePolicyError("upload_suffix_not_allowed", f"upload suffix {suffix!r} is not allowed")
        if not self.config.allow_hidden_uploads and any(part.startswith(".") for part in path.parts if part not in {".", ".."}):
            raise BrowserFilePolicyError("hidden_upload_denied", "hidden upload paths are prohibited")
        return UploadCandidate(raw, identity, str(selected_root.resolve(strict=False)))


WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def validate_filename(value: str) -> str:
    raw = unicodedata.normalize("NFC", str(value))
    if not raw or len(raw) > 240 or "\x00" in raw:
        raise BrowserFilePolicyError("invalid_filename", "browser filename is empty, too long or contains NUL")
    if raw in {".", ".."} or raw != Path(raw).name or any(char in raw for char in "/\\"):
        raise BrowserFilePolicyError("filename_traversal", "browser filename must be a single path component")
    if raw.endswith((".", " ")) or raw != raw.strip():
        raise BrowserFilePolicyError("windows_filename_ambiguity", "browser filename cannot end in a dot or space")
    if ":" in raw:
        raise BrowserFilePolicyError("windows_ads_denied", "browser filename cannot contain an alternate-data-stream separator")
    stem = raw.split(".", 1)[0].casefold()
    if stem in WINDOWS_RESERVED:
        raise BrowserFilePolicyError("windows_reserved_name", "browser filename uses a reserved Windows device name")
    if any(ord(char) < 32 for char in raw) or re.search(r"[<>\"|?*]", raw):
        raise BrowserFilePolicyError("invalid_filename_character", "browser filename contains a prohibited character")
    return raw


def normalize_suffix(value: str) -> str:
    suffix = str(value).strip().lower()
    if suffix and not suffix.startswith("."):
        suffix = "." + suffix
    return suffix


def is_contained(path: Path, root: Path) -> bool:
    candidate = os.path.normcase(str(path.resolve(strict=False)))
    base = os.path.normcase(str(root.resolve(strict=False)))
    try:
        return os.path.commonpath((candidate, base)) == base
    except ValueError:
        return False


def assert_contained(path: Path, root: Path, *, resolve_leaf: bool) -> None:
    candidate = path.resolve(strict=resolve_leaf)
    base = root.resolve(strict=False)
    if not is_contained(candidate, base):
        raise BrowserFilePolicyError(
            "file_containment_violation",
            "browser file path escapes its owned root",
            details={"path": str(candidate), "root": str(base)},
        )


def inspect_file(path: Path, *, follow_symlinks: bool) -> FileIdentity:
    try:
        metadata = path.stat(follow_symlinks=follow_symlinks)
        lexical = path.lstat()
    except FileNotFoundError as exc:
        raise BrowserFilePolicyError("file_missing", "browser file path does not exist") from exc
    if stat.S_ISLNK(lexical.st_mode) and not follow_symlinks:
        kind = FileKind.SYMLINK
        selected = lexical
    else:
        selected = metadata
        if stat.S_ISREG(selected.st_mode):
            kind = FileKind.REGULAR
        elif stat.S_ISDIR(selected.st_mode):
            kind = FileKind.DIRECTORY
        elif stat.S_ISCHR(selected.st_mode) or stat.S_ISBLK(selected.st_mode):
            kind = FileKind.DEVICE
        else:
            kind = FileKind.OTHER
    return identity_from_stat(path, path.resolve(strict=follow_symlinks), selected, kind=kind)


def identity_from_stat(path: Path, real_path: Path, metadata: os.stat_result, *, kind: FileKind | None = None) -> FileIdentity:
    selected_kind = kind
    if selected_kind is None:
        selected_kind = FileKind.REGULAR if stat.S_ISREG(metadata.st_mode) else FileKind.OTHER
    return FileIdentity(
        path=str(path.resolve(strict=False)),
        real_path=str(real_path),
        kind=selected_kind,
        size=int(metadata.st_size),
        modified_ns=int(metadata.st_mtime_ns),
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        mode=int(metadata.st_mode),
    )


def secure_open_read(path: Path, *, allow_symlink: bool) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW") and not allow_symlink:
        flags |= os.O_NOFOLLOW
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise BrowserFilePolicyError("upload_open_failed", f"approved upload file could not be opened: {exc}") from exc


def hash_open_stream(stream: BinaryIO, quota: int, *, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        size += len(chunk)
        if size > quota:
            raise BrowserFilePolicyError("upload_quota_exceeded", "upload content exceeds approved size quota")
        hasher.update(chunk)
    return hasher.hexdigest(), size
