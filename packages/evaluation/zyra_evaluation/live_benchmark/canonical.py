from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_METRIC = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_SECRET_KEYS = (
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
    "api_key",
    "api-key",
    "cookie",
    "private_key",
)


class BenchmarkValidationError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: str = "validation",
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = require_code(code)
        self.phase = require_code(phase)
        self.detail = redact(dict(detail or {}))
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "phase": self.phase,
            "detail": self.detail,
        }


def invalid(
    code: str,
    message: str,
    *,
    phase: str = "validation",
    detail: Mapping[str, Any] | None = None,
) -> BenchmarkValidationError:
    return BenchmarkValidationError(code, message, phase=phase, detail=detail)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any, label: str) -> datetime:
    text = bounded_text(value, label, maximum_bytes=128)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise invalid(
            "benchmark_timestamp_invalid",
            f"{label} is not an ISO-8601 timestamp.",
            detail={"label": label},
        ) from error
    if parsed.tzinfo is None:
        raise invalid(
            "benchmark_timestamp_timezone_missing",
            f"{label} must include a timezone.",
            detail={"label": label},
        )
    return parsed.astimezone(UTC)


def elapsed_ms(started_at: Any, completed_at: Any, label: str) -> int:
    started = parse_utc(started_at, f"{label} started_at")
    completed = parse_utc(completed_at, f"{label} completed_at")
    delta = int((completed - started).total_seconds() * 1000)
    if delta < 0:
        raise invalid(
            "benchmark_timestamp_order_invalid",
            f"{label} completed before it started.",
            detail={"started_at": started_at, "completed_at": completed_at},
        )
    return delta


def new_identity(prefix: str) -> str:
    selected = require_code(prefix)
    return f"{selected}_{secrets.token_hex(12)}"


def require_code(value: Any, label: str = "code") -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    if not text or len(text) > 128 or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", text):
        raise invalid(
            "benchmark_code_invalid",
            f"{label} is invalid.",
            detail={"label": label},
        )
    return text


def identity(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not _IDENTITY.fullmatch(text):
        raise invalid(
            "benchmark_identity_invalid",
            f"{label} is invalid.",
            detail={"label": label},
        )
    return text


def optional_identity(value: Any, label: str) -> str:
    if value is None or str(value).strip() == "":
        return ""
    return identity(value, label)


def require_digest(value: Any, label: str) -> str:
    text = str(value or "").strip().lower()
    if not _DIGEST.fullmatch(text):
        raise invalid(
            "benchmark_digest_invalid",
            f"{label} must be a lowercase SHA-256 digest.",
            detail={"label": label},
        )
    return text


def require_commit(value: Any, label: str = "commit") -> str:
    text = str(value or "").strip().lower()
    if not _COMMIT.fullmatch(text):
        raise invalid(
            "benchmark_commit_invalid",
            f"{label} must be a full Git commit SHA.",
            detail={"label": label},
        )
    return text


def metric_name(value: Any, label: str = "metric") -> str:
    text = str(value or "").strip().lower()
    if not _METRIC.fullmatch(text):
        raise invalid(
            "benchmark_metric_name_invalid",
            f"{label} is invalid.",
            detail={"label": label},
        )
    return text


def bounded_text(
    value: Any,
    label: str,
    *,
    minimum_bytes: int = 1,
    maximum_bytes: int = 64 * 1024,
) -> str:
    if not isinstance(value, str):
        raise invalid(
            "benchmark_text_type_invalid",
            f"{label} must be text.",
            detail={"label": label},
        )
    text = value.strip()
    size = len(text.encode("utf-8"))
    if size < minimum_bytes or size > maximum_bytes:
        raise invalid(
            "benchmark_text_bounds_invalid",
            f"{label} has an invalid encoded length.",
            detail={
                "label": label,
                "minimum_bytes": minimum_bytes,
                "maximum_bytes": maximum_bytes,
                "observed_bytes": size,
            },
        )
    return text


def bounded_integer(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise invalid(
            "benchmark_integer_type_invalid",
            f"{label} must be an integer.",
        )
    try:
        selected = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise invalid(
            "benchmark_integer_type_invalid",
            f"{label} must be an integer.",
        ) from error
    if selected != value and not (
        isinstance(value, str) and str(selected) == value.strip()
    ):
        raise invalid(
            "benchmark_integer_lossy",
            f"{label} cannot be converted losslessly to an integer.",
        )
    if selected < minimum or selected > maximum:
        raise invalid(
            "benchmark_integer_bounds_invalid",
            f"{label} is outside the allowed range.",
            detail={"minimum": minimum, "maximum": maximum, "observed": selected},
        )
    return selected


def finite_number(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool):
        raise invalid(
            "benchmark_number_type_invalid",
            f"{label} must be numeric.",
        )
    try:
        selected = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise invalid(
            "benchmark_number_type_invalid",
            f"{label} must be numeric.",
        ) from error
    if not math.isfinite(selected):
        raise invalid(
            "benchmark_number_non_finite",
            f"{label} must be finite.",
        )
    if minimum is not None and selected < minimum:
        raise invalid(
            "benchmark_number_below_minimum",
            f"{label} is below the allowed minimum.",
            detail={"minimum": minimum, "observed": selected},
        )
    if maximum is not None and selected > maximum:
        raise invalid(
            "benchmark_number_above_maximum",
            f"{label} is above the allowed maximum.",
            detail={"maximum": maximum, "observed": selected},
        )
    return selected


def mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise invalid(
            "benchmark_mapping_invalid",
            f"{label} must be an object.",
            detail={"label": label},
        )
    return value


def sequence(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, (list, tuple)):
        raise invalid(
            "benchmark_sequence_invalid",
            f"{label} must be an array.",
            detail={"label": label},
        )
    return value


def string_sequence(
    value: Any,
    label: str,
    *,
    minimum: int = 1,
    maximum: int = 10_000,
) -> tuple[str, ...]:
    items = sequence(value, label)
    if len(items) < minimum or len(items) > maximum:
        raise invalid(
            "benchmark_sequence_bounds_invalid",
            f"{label} has an invalid item count.",
            detail={"minimum": minimum, "maximum": maximum, "observed": len(items)},
        )
    output: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        selected = bounded_text(
            item,
            f"{label}[{index}]",
            maximum_bytes=4096,
        )
        if selected in seen:
            raise invalid(
                "benchmark_sequence_duplicate",
                f"{label} contains a duplicate item.",
                detail={"item": selected},
            )
        seen.add(selected)
        output.append(selected)
    return tuple(output)


def require_keys(
    value: Mapping[str, Any],
    keys: Iterable[str],
    label: str,
) -> None:
    missing = sorted(key for key in keys if key not in value)
    if missing:
        raise invalid(
            "benchmark_keys_missing",
            f"{label} is missing required fields.",
            detail={"missing": missing},
        )


def canonicalize(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise invalid(
                "benchmark_canonical_non_finite",
                "Canonical JSON cannot contain non-finite numbers.",
            )
        if value == 0:
            return 0
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise invalid(
                "benchmark_canonical_datetime_naive",
                "Canonical datetime must contain a timezone.",
            )
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if key in output:
                raise invalid(
                    "benchmark_canonical_key_collision",
                    "Canonical mapping contains colliding string keys.",
                    detail={"key": key},
                )
            output[key] = canonicalize(raw_value)
        return {key: output[key] for key in sorted(output)}
    if isinstance(value, (list, tuple)):
        return [canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        selected = [canonicalize(item) for item in value]
        return sorted(
            selected,
            key=lambda item: json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
        )
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return canonicalize(to_dict())
    raise invalid(
        "benchmark_canonical_type_unsupported",
        "Value cannot be represented as canonical JSON.",
        detail={"type": type(value).__name__},
    )


def canonical_json(value: Any) -> str:
    return json.dumps(
        canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def pretty_json(value: Any) -> str:
    return (
        json.dumps(
            canonicalize(value),
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def content_digest(value: bytes | str) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def file_digest(
    path: str | Path,
    *,
    maximum_bytes: int = 2 * 1024 * 1024 * 1024,
) -> tuple[str, int]:
    selected = Path(path)
    try:
        size = selected.stat().st_size
    except OSError as error:
        raise invalid(
            "benchmark_file_unavailable",
            "Benchmark file cannot be read.",
            detail={"path": str(selected)},
        ) from error
    if size < 0 or size > maximum_bytes:
        raise invalid(
            "benchmark_file_size_invalid",
            "Benchmark file exceeds the allowed size.",
            detail={"path": str(selected), "size": size, "maximum": maximum_bytes},
        )
    hasher = hashlib.sha256()
    observed = 0
    try:
        with selected.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                observed += len(block)
                if observed > maximum_bytes:
                    raise invalid(
                        "benchmark_file_size_invalid",
                        "Benchmark file grew beyond the allowed size.",
                        detail={"path": str(selected), "maximum": maximum_bytes},
                    )
                hasher.update(block)
    except OSError as error:
        raise invalid(
            "benchmark_file_unavailable",
            "Benchmark file cannot be read.",
            detail={"path": str(selected)},
        ) from error
    if observed != size:
        raise invalid(
            "benchmark_file_changed_during_digest",
            "Benchmark file changed while it was being hashed.",
            detail={"path": str(selected), "before": size, "observed": observed},
        )
    return hasher.hexdigest(), observed


def resolve_within(
    path: str | Path,
    root: str | Path,
    label: str,
    *,
    must_exist: bool = False,
) -> Path:
    base = Path(root).resolve(strict=True)
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as error:
        raise invalid(
            "benchmark_path_unavailable",
            f"{label} cannot be resolved.",
            detail={"path": str(candidate)},
        ) from error
    if resolved != base and base not in resolved.parents:
        raise invalid(
            "benchmark_path_escape",
            f"{label} escapes its declared root.",
            phase="integrity",
            detail={"path": str(resolved), "root": str(base)},
        )
    return resolved


def atomic_write(path: str | Path, payload: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_json(path: str | Path, value: Any) -> tuple[str, int]:
    payload = pretty_json(value).encode("utf-8")
    atomic_write(path, payload)
    return content_digest(payload), len(payload)


def redact(value: Any, *, maximum_depth: int = 16, _depth: int = 0) -> Any:
    if _depth > maximum_depth:
        return "[depth-redacted]"
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if any(marker in lowered for marker in _SECRET_KEYS):
                output[key] = "[redacted]"
            else:
                output[key] = redact(
                    raw_value,
                    maximum_depth=maximum_depth,
                    _depth=_depth + 1,
                )
        return output
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            redact(item, maximum_depth=maximum_depth, _depth=_depth + 1)
            for item in value
        ]
    if isinstance(value, bytes):
        return f"[bytes:{len(value)}:{content_digest(value)}]"
    if isinstance(value, Path):
        return str(value)
    return value


def scan_secret_canaries(payload: bytes | str, canaries: Iterable[str]) -> None:
    selected = payload.encode("utf-8") if isinstance(payload, str) else payload
    findings: list[str] = []
    for index, value in enumerate(canaries):
        canary = str(value).encode("utf-8")
        if canary and canary in selected:
            findings.append(f"canary-{index + 1}")
    if findings:
        raise invalid(
            "benchmark_secret_canary_leaked",
            "Benchmark output contains protected canary material.",
            phase="security",
            detail={"findings": findings},
        )


def stable_unique(values: Iterable[str]) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            output.append(value)
            seen.add(value)
    return tuple(output)


def chunks(values: Sequence[Any], size: int) -> tuple[tuple[Any, ...], ...]:
    selected = bounded_integer(size, "chunk size", minimum=1, maximum=1_000_000)
    return tuple(
        tuple(values[index : index + selected])
        for index in range(0, len(values), selected)
    )
