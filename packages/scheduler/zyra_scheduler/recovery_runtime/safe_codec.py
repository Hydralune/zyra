from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePath
from typing import Any

from .contracts import RECOVERY_CHECKPOINT_SCHEMA, RecoveryCheckpoint, canonical_json


class CheckpointCodecError(ValueError):
    """Raised when a checkpoint crosses the versioned JSON allowlist boundary."""


class CheckpointVersionError(CheckpointCodecError):
    pass


class CheckpointCorruptError(CheckpointCodecError):
    pass


class CheckpointPathError(CheckpointCodecError):
    pass


class CheckpointSizeError(CheckpointCodecError):
    pass


_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/\-]{0,511}$")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:[\\/]")
_PICKLE_PREFIXES = (
    b"\x80\x02", b"\x80\x03", b"\x80\x04", b"\x80\x05",
    b"(dp", b"ccopy_reg", b"c__builtin__", b"cos\nsystem",
)


class SafeCheckpointCodec:
    """Strict JSON codec for recovery checkpoints.

    It deliberately does not offer a generic serializer hook. Values crossing
    this boundary are copied into a finite JSON tree, all dictionary keys are
    validated, object depth and collection sizes are bounded, and path-like
    values are rejected unless the enclosing key explicitly declares a
    reference rather than a filesystem path. This prevents a checkpoint from
    becoming an object-construction or arbitrary-file transport.
    """

    def __init__(
        self,
        *,
        schema: str = RECOVERY_CHECKPOINT_SCHEMA,
        maximum_bytes: int = 8 * 1024 * 1024,
        maximum_depth: int = 64,
        maximum_items: int = 100_000,
        maximum_string_chars: int = 1_000_000,
    ) -> None:
        if maximum_bytes < 1024:
            raise ValueError("maximum checkpoint bytes must be at least 1024")
        if maximum_depth < 4:
            raise ValueError("maximum checkpoint depth must be at least 4")
        if maximum_items < 32:
            raise ValueError("maximum checkpoint item count must be at least 32")
        self.schema = schema
        self.maximum_bytes = maximum_bytes
        self.maximum_depth = maximum_depth
        self.maximum_items = maximum_items
        self.maximum_string_chars = maximum_string_chars

    def encode(self, checkpoint: RecoveryCheckpoint) -> bytes:
        if not isinstance(checkpoint, RecoveryCheckpoint):
            raise CheckpointCodecError("encode requires RecoveryCheckpoint")
        value = checkpoint.to_dict()
        normalized = self.normalize(value)
        if normalized.get("schema") != self.schema:
            raise CheckpointVersionError(f"unsupported checkpoint schema: {normalized.get('schema')!r}")
        payload = canonical_json(normalized).encode("utf-8")
        if len(payload) > self.maximum_bytes:
            raise CheckpointSizeError(
                f"checkpoint payload exceeds {self.maximum_bytes} bytes: {len(payload)}"
            )
        return payload

    def decode(self, payload: bytes | bytearray | memoryview | str) -> RecoveryCheckpoint:
        raw = self._as_bytes(payload)
        self._reject_pickle(raw)
        if len(raw) > self.maximum_bytes:
            raise CheckpointSizeError(
                f"checkpoint payload exceeds {self.maximum_bytes} bytes: {len(raw)}"
            )
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise CheckpointCorruptError("checkpoint is not valid UTF-8") from error
        try:
            value = json.loads(text)
        except json.JSONDecodeError as error:
            raise CheckpointCorruptError(
                f"checkpoint JSON is corrupt at line {error.lineno} column {error.colno}"
            ) from error
        if not isinstance(value, dict):
            raise CheckpointCorruptError("checkpoint root must be an object")
        normalized = self.normalize(value)
        if normalized.get("schema") != self.schema:
            raise CheckpointVersionError(f"unsupported checkpoint schema: {normalized.get('schema')!r}")
        try:
            return RecoveryCheckpoint.from_dict(normalized)
        except (KeyError, TypeError, ValueError) as error:
            raise CheckpointCorruptError(f"invalid checkpoint content: {error}") from error

    def normalize(self, value: Any) -> dict[str, Any]:
        counter = [0]
        normalized = self._normalize(value, path=("$",), depth=0, counter=counter)
        if not isinstance(normalized, dict):
            raise CheckpointCodecError("checkpoint root must be a mapping")
        return normalized

    def _normalize(
        self,
        value: Any,
        *,
        path: tuple[str, ...],
        depth: int,
        counter: list[int],
    ) -> Any:
        if depth > self.maximum_depth:
            raise CheckpointSizeError(f"checkpoint nesting exceeds {self.maximum_depth} at {self._path(path)}")
        counter[0] += 1
        if counter[0] > self.maximum_items:
            raise CheckpointSizeError(f"checkpoint item count exceeds {self.maximum_items}")
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise CheckpointCodecError(f"non-finite number at {self._path(path)}")
            return value
        if isinstance(value, str):
            if len(value) > self.maximum_string_chars:
                raise CheckpointSizeError(f"string exceeds limit at {self._path(path)}")
            self._reject_unsafe_string(value, path=path)
            return value
        if isinstance(value, (bytes, bytearray, memoryview)):
            raise CheckpointCodecError(f"binary values are not allowed at {self._path(path)}")
        if isinstance(value, PurePath):
            raise CheckpointPathError(f"path objects are not allowed at {self._path(path)}")
        if isinstance(value, Mapping):
            output: dict[str, Any] = {}
            for key in sorted(value, key=lambda item: str(item)):
                if not isinstance(key, str):
                    raise CheckpointCodecError(f"non-string key at {self._path(path)}")
                self._validate_key(key, path=path)
                output[key] = self._normalize(
                    value[key],
                    path=(*path, key),
                    depth=depth + 1,
                    counter=counter,
                )
            return output
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
            return [
                self._normalize(
                    item,
                    path=(*path, str(index)),
                    depth=depth + 1,
                    counter=counter,
                )
                for index, item in enumerate(value)
            ]
        raise CheckpointCodecError(
            f"unsupported value {type(value).__name__} at {self._path(path)}"
        )

    def _validate_key(self, key: str, *, path: tuple[str, ...]) -> None:
        if not key or not _SAFE_KEY.fullmatch(key):
            raise CheckpointCodecError(f"unsupported object key {key!r} at {self._path(path)}")
        lowered = key.casefold()
        if lowered in {
            "__class__", "__dict__", "__reduce__", "__reduce_ex__", "__getstate__",
            "__setstate__", "__module__", "$type", "$ref", "pickle", "marshal",
        }:
            raise CheckpointCodecError(f"unsafe object-construction key {key!r} at {self._path(path)}")

    def _reject_unsafe_string(self, value: str, *, path: tuple[str, ...]) -> None:
        if "\x00" in value:
            raise CheckpointCodecError(f"NUL byte is not allowed at {self._path(path)}")
        key = path[-1].casefold() if path else ""
        path_key = (
            key.endswith("_path")
            or key.endswith("_file")
            or key.endswith("_directory")
            or key in {"path", "file", "filename", "directory", "cwd", "root"}
        )
        if not path_key:
            return
        candidate = value.strip().replace("\\", "/")
        if not candidate:
            return
        if candidate.startswith(("/", "~/")) or _DRIVE_PREFIX.match(value):
            raise CheckpointPathError(f"absolute path is not allowed at {self._path(path)}")
        parts = [part for part in candidate.split("/") if part not in {"", "."}]
        if any(part == ".." for part in parts):
            raise CheckpointPathError(f"path traversal is not allowed at {self._path(path)}")
        if any(part.casefold() in {".git", ".ssh", ".aws", ".env"} for part in parts):
            raise CheckpointPathError(f"sensitive path segment is not allowed at {self._path(path)}")

    @staticmethod
    def _as_bytes(payload: bytes | bytearray | memoryview | str) -> bytes:
        if isinstance(payload, str):
            return payload.encode("utf-8")
        if isinstance(payload, bytes):
            return payload
        if isinstance(payload, (bytearray, memoryview)):
            return bytes(payload)
        raise CheckpointCodecError("checkpoint payload must be bytes or string")

    @staticmethod
    def _reject_pickle(payload: bytes) -> None:
        stripped = payload.lstrip()
        if any(stripped.startswith(prefix) for prefix in _PICKLE_PREFIXES):
            raise CheckpointCodecError("pickle checkpoint payloads are forbidden")
        lowered = stripped[:512].lower()
        if b"__reduce__" in lowered or b"copyreg" in lowered or b"pickle" in lowered:
            raise CheckpointCodecError("object-construction checkpoint payload is forbidden")

    @staticmethod
    def _path(parts: tuple[str, ...]) -> str:
        return ".".join(parts)


class AtomicCheckpointFile:
    """Optional JSON export/import with an explicit rooted path boundary.

    SQLite remains canonical. This class is used only for portable checkpoint
    artifacts and tests. It resolves the requested relative name under one
    fixed root, writes a sibling temporary file with exclusive creation, fsyncs
    it and atomically replaces the destination.
    """

    def __init__(self, root: str | Path, codec: SafeCheckpointCodec | None = None) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.codec = codec or SafeCheckpointCodec()

    def write(self, relative_name: str, checkpoint: RecoveryCheckpoint) -> Path:
        target = self.resolve(relative_name)
        payload = self.codec.encode(checkpoint)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.{checkpoint.checkpoint_id}.tmp")
        if temporary.exists():
            raise CheckpointPathError(f"checkpoint temporary path already exists: {temporary.name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with temporary.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            self._fsync_directory(target.parent)
        finally:
            if temporary.exists():
                temporary.unlink()
        return target

    def read(self, relative_name: str) -> RecoveryCheckpoint:
        target = self.resolve(relative_name)
        try:
            payload = target.read_bytes()
        except FileNotFoundError as error:
            raise CheckpointPathError(f"checkpoint export does not exist: {relative_name}") from error
        return self.codec.decode(payload)

    def resolve(self, relative_name: str) -> Path:
        name = str(relative_name or "").strip()
        if not name:
            raise CheckpointPathError("checkpoint relative name is required")
        candidate = Path(name)
        if candidate.is_absolute() or candidate.drive:
            raise CheckpointPathError("absolute checkpoint export paths are forbidden")
        if any(part in {"", ".", ".."} for part in candidate.parts):
            raise CheckpointPathError("checkpoint export path contains traversal or empty segment")
        if candidate.suffix.casefold() not in {".json", ".checkpoint"}:
            raise CheckpointPathError("checkpoint export suffix must be .json or .checkpoint")
        resolved = (self.root / candidate).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise CheckpointPathError("checkpoint export escapes configured root") from error
        return resolved

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = [
    "AtomicCheckpointFile",
    "CheckpointCodecError",
    "CheckpointCorruptError",
    "CheckpointPathError",
    "CheckpointSizeError",
    "CheckpointVersionError",
    "SafeCheckpointCodec",
]
