from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import fail


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def canonical_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            normalize(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise fail(
            "canonical-json-invalid",
            "Value cannot be represented as canonical JSON.",
            phase="canonical",
            detail={"type": type(value).__name__},
        ) from error
    return encoded.encode("utf-8")


def canonical_text(value: Any, *, pretty: bool = False) -> str:
    if not pretty:
        return canonical_bytes(value).decode("utf-8")
    return json.dumps(
        normalize(value),
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def bytes_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_digest(path: str | Path) -> str:
    selected = Path(path)
    hasher = hashlib.sha256()
    try:
        with selected.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                hasher.update(chunk)
    except OSError as error:
        raise fail(
            "file-digest-failed",
            "Evidence member cannot be read.",
            phase="integrity",
            detail={"path": str(selected), "error": str(error)},
        ) from error
    return hasher.hexdigest()


def normalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): normalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [normalize(item) for item in value]
        return sorted(normalized, key=canonical_bytes)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, datetime):
        selected = value if value.tzinfo else value.replace(tzinfo=UTC)
        return selected.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise fail(
        "canonical-type-unsupported",
        "Value contains a type not allowed in freeze evidence.",
        phase="canonical",
        detail={"type": type(value).__name__},
    )


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise fail(
            "mapping-required",
            f"{label} must be a mapping.",
            phase="validation",
            detail={"label": label, "type": type(value).__name__},
        )
    return {str(key): item for key, item in value.items()}


def require_sequence(value: Any, label: str) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise fail(
            "sequence-required",
            f"{label} must be a sequence.",
            phase="validation",
            detail={"label": label, "type": type(value).__name__},
        )
    return list(value)


def require_text(
    value: Any,
    label: str,
    *,
    minimum: int = 1,
    maximum: int = 64 * 1024,
) -> str:
    if not isinstance(value, str):
        raise fail(
            "text-required",
            f"{label} must be text.",
            phase="validation",
            detail={"label": label},
        )
    selected = value.strip()
    size = len(selected.encode("utf-8"))
    if size < minimum or size > maximum:
        raise fail(
            "text-bounds-invalid",
            f"{label} has an invalid encoded size.",
            phase="validation",
            detail={"label": label, "bytes": size, "minimum": minimum, "maximum": maximum},
        )
    return selected


def require_identity(value: Any, label: str) -> str:
    selected = require_text(value, label, maximum=256)
    if not _IDENTITY.fullmatch(selected):
        raise fail(
            "identity-invalid",
            f"{label} is invalid.",
            phase="validation",
            detail={"label": label},
        )
    return selected


def require_digest(value: Any, label: str) -> str:
    selected = str(value or "").strip().lower()
    if not _DIGEST.fullmatch(selected):
        raise fail(
            "digest-invalid",
            f"{label} must be a lowercase SHA-256 digest.",
            phase="validation",
            detail={"label": label},
        )
    return selected


def require_commit(value: Any, label: str = "commit") -> str:
    selected = str(value or "").strip().lower()
    if not _COMMIT.fullmatch(selected):
        raise fail(
            "commit-invalid",
            f"{label} must be a full Git commit SHA.",
            phase="validation",
            detail={"label": label},
        )
    return selected


def require_integer(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise fail(
            "integer-invalid",
            f"{label} must be an integer.",
            phase="validation",
        )
    try:
        selected = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise fail(
            "integer-invalid",
            f"{label} must be an integer.",
            phase="validation",
        ) from error
    if selected < minimum or selected > maximum:
        raise fail(
            "integer-bounds-invalid",
            f"{label} is out of bounds.",
            phase="validation",
            detail={"label": label, "minimum": minimum, "maximum": maximum},
        )
    return selected


def require_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise fail(
            "number-invalid",
            f"{label} must be numeric.",
            phase="validation",
        )
    try:
        selected = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise fail(
            "number-invalid",
            f"{label} must be numeric.",
            phase="validation",
        ) from error
    if selected != selected or selected in (float("inf"), float("-inf")):
        raise fail(
            "number-finite-required",
            f"{label} must be finite.",
            phase="validation",
        )
    return selected


def safe_relative_path(value: Any, label: str = "path") -> str:
    selected = require_text(value, label, maximum=4096).replace("\\", "/")
    path = PurePosixPath(selected)
    if path.is_absolute() or not path.parts:
        raise fail(
            "relative-path-required",
            f"{label} must be relative.",
            phase="path",
            detail={"path": selected},
        )
    if any(part in ("", ".", "..") for part in path.parts):
        raise fail(
            "relative-path-escape",
            f"{label} contains an unsafe component.",
            phase="path",
            detail={"path": selected},
        )
    if ":" in path.parts[0]:
        raise fail(
            "relative-path-drive",
            f"{label} must not contain a drive.",
            phase="path",
            detail={"path": selected},
        )
    return path.as_posix()


def resolve_inside(root: str | Path, relative: Any, *, must_exist: bool = True) -> Path:
    base = Path(root).resolve(strict=True)
    path = base.joinpath(*PurePosixPath(safe_relative_path(relative)).parts)
    try:
        resolved = path.resolve(strict=must_exist)
    except OSError as error:
        raise fail(
            "evidence-path-unavailable",
            "Evidence reference cannot be resolved.",
            phase="path",
            detail={"root": str(base), "relative": str(relative)},
        ) from error
    try:
        resolved.relative_to(base)
    except ValueError as error:
        raise fail(
            "evidence-path-escape",
            "Evidence reference escapes its admitted root.",
            phase="path",
            detail={"root": str(base), "relative": str(relative)},
        ) from error
    return resolved


def load_json(path: str | Path, *, maximum_bytes: int = 256 * 1024 * 1024) -> dict[str, Any]:
    selected = Path(path)
    try:
        size = selected.stat().st_size
        if size > maximum_bytes:
            raise fail(
                "json-member-too-large",
                "JSON evidence member exceeds its admission limit.",
                phase="input",
                detail={"path": str(selected), "bytes": size, "maximum": maximum_bytes},
            )
        value = json.loads(selected.read_text(encoding="utf-8"))
    except UnicodeDecodeError as error:
        raise fail(
            "json-member-encoding-invalid",
            "JSON evidence member must be UTF-8.",
            phase="input",
            detail={"path": str(selected)},
        ) from error
    except json.JSONDecodeError as error:
        raise fail(
            "json-member-invalid",
            "JSON evidence member cannot be decoded.",
            phase="input",
            detail={"path": str(selected), "line": error.lineno, "column": error.colno},
        ) from error
    except OSError as error:
        raise fail(
            "json-member-unavailable",
            "JSON evidence member cannot be read.",
            phase="input",
            detail={"path": str(selected)},
        ) from error
    return require_mapping(value, str(selected))


def load_json_lines(
    path: str | Path,
    *,
    maximum_lines: int = 2_000_000,
    maximum_line_bytes: int = 8 * 1024 * 1024,
) -> Iterable[dict[str, Any]]:
    selected = Path(path)
    try:
        with selected.open("r", encoding="utf-8", newline="") as handle:
            for number, line in enumerate(handle, 1):
                if number > maximum_lines:
                    raise fail(
                        "jsonl-line-limit",
                        "JSONL member exceeds its line limit.",
                        phase="input",
                        detail={"path": str(selected), "maximum_lines": maximum_lines},
                    )
                if len(line.encode("utf-8")) > maximum_line_bytes:
                    raise fail(
                        "jsonl-record-too-large",
                        "JSONL record exceeds its size limit.",
                        phase="input",
                        detail={"path": str(selected), "line": number},
                    )
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    raise fail(
                        "jsonl-record-invalid",
                        "JSONL record cannot be decoded.",
                        phase="input",
                        detail={"path": str(selected), "line": number},
                    ) from error
                yield require_mapping(value, f"{selected}:{number}")
    except OSError as error:
        raise fail(
            "jsonl-member-unavailable",
            "JSONL evidence member cannot be read.",
            phase="input",
            detail={"path": str(selected)},
        ) from error


def verify_embedded_digest(
    value: Mapping[str, Any],
    field: str,
    *,
    required: bool = True,
) -> str:
    projection = dict(value)
    declared = projection.pop(field, None)
    if declared is None and not required:
        return digest(projection)
    expected = require_digest(declared, field)
    observed = digest(projection)
    if expected != observed:
        raise fail(
            "embedded-digest-mismatch",
            "Evidence object's embedded digest is invalid.",
            phase="integrity",
            detail={"field": field, "expected": expected, "observed": observed},
        )
    return observed


def atomic_write(path: str | Path, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = -1
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    except OSError as error:
        raise fail(
            "atomic-write-failed",
            "Freeze evidence output could not be committed atomically.",
            phase="output",
            detail={"path": str(target), "error": str(error)},
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def write_json(path: str | Path, value: Any) -> str:
    payload = canonical_text(value, pretty=True).encode("utf-8")
    atomic_write(path, payload)
    return bytes_digest(payload)


def stable_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result
