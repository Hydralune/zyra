from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import invalid


IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+~-]{0,254}$")
HEX_256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
SECRET_KEY_PATTERN = re.compile(
    r"(?:authorization|credential|password|secret|token|api[_-]?key|cookie)",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_identity(prefix: str) -> str:
    normalized = identity(prefix, "identity prefix")
    return f"{normalized}_{uuid4().hex}"


def identity(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    if not IDENTITY_PATTERN.fullmatch(normalized):
        raise invalid(
            "scenario_identity_invalid",
            f"{label} is not a valid Zyra identity.",
            detail={"label": label, "value": normalized[:256]},
        )
    return normalized


def optional_identity(value: Any, label: str) -> str:
    normalized = str(value or "").strip()
    return identity(normalized, label) if normalized else ""


def bounded_text(
    value: Any,
    label: str,
    *,
    minimum: int = 1,
    maximum_bytes: int = 256 * 1024,
) -> str:
    normalized = str(value or "").strip()
    size = len(normalized.encode("utf-8"))
    if size < minimum or size > maximum_bytes:
        raise invalid(
            "scenario_text_invalid",
            f"{label} must contain {minimum} through {maximum_bytes} UTF-8 bytes.",
            detail={"label": label, "size": size},
        )
    return normalized


def bounded_integer(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int,
    fallback: int | None = None,
) -> int:
    if value is None and fallback is not None:
        return fallback
    if isinstance(value, bool):
        raise invalid("scenario_integer_invalid", f"{label} must be an integer.")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as error:
        raise invalid("scenario_integer_invalid", f"{label} must be an integer.") from error
    if normalized < minimum or normalized > maximum:
        raise invalid(
            "scenario_integer_out_of_range",
            f"{label} must be between {minimum} and {maximum}.",
            detail={"label": label, "value": normalized},
        )
    return normalized


def canonicalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [canonicalize(item) for item in value]
        return sorted(normalized, key=canonical_json)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return canonicalize(value.value)
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return canonicalize(value.to_dict())
    if hasattr(value, "__dict__"):
        return canonicalize(vars(value))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def canonical_json(value: Any) -> str:
    return json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def pretty_json(value: Any) -> str:
    return json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def content_digest(value: str | bytes) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return sha256_bytes(payload)


def require_digest(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not HEX_256_PATTERN.fullmatch(normalized):
        raise invalid(
            "scenario_digest_invalid",
            f"{label} must be a lowercase SHA-256 digest.",
            detail={"label": label},
        )
    return normalized


def resolve_path(
    value: Any,
    label: str,
    *,
    base: str | Path | None = None,
    must_be_absolute: bool = False,
) -> Path:
    raw = bounded_text(value, label, maximum_bytes=32 * 1024)
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        if must_be_absolute and base is None:
            raise invalid(
                "scenario_path_not_absolute",
                f"{label} must be absolute.",
                detail={"path": raw},
            )
        candidate = Path(base or Path.cwd()) / candidate
    try:
        return candidate.resolve(strict=False)
    except OSError as error:
        raise invalid(
            "scenario_path_invalid",
            f"{label} could not be resolved.",
            detail={"path": raw, "reason": str(error)},
        ) from error


def path_within(candidate: str | Path, root: str | Path) -> bool:
    try:
        Path(candidate).resolve(strict=False).relative_to(Path(root).resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def require_path_within(candidate: str | Path, root: str | Path, label: str) -> Path:
    selected = Path(candidate).resolve(strict=False)
    boundary = Path(root).resolve(strict=False)
    if not path_within(selected, boundary):
        raise invalid(
            "scenario_path_outside_boundary",
            f"{label} is outside its declared owner boundary.",
            detail={"path": str(selected), "boundary": str(boundary)},
        )
    return selected


def file_digest(path: str | Path, *, maximum_bytes: int = 512 * 1024 * 1024) -> tuple[str, int]:
    selected = Path(path)
    size = selected.stat().st_size
    if size > maximum_bytes:
        raise invalid(
            "scenario_artifact_too_large",
            "Artifact exceeds the evidence checksum limit.",
            detail={"path": str(selected), "size": size, "maximum": maximum_bytes},
        )
    hasher = hashlib.sha256()
    with selected.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest(), size


def redact(value: Any, *, maximum_depth: int = 12, _depth: int = 0) -> Any:
    if _depth > maximum_depth:
        return "[depth-limit]"
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            output[name] = (
                "[redacted]"
                if SECRET_KEY_PATTERN.search(name)
                else redact(item, maximum_depth=maximum_depth, _depth=_depth + 1)
            )
        return output
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            redact(item, maximum_depth=maximum_depth, _depth=_depth + 1)
            for item in value
        ]
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": sha256_bytes(value)}
    return value


def stable_unique(values: Iterable[str]) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(normalized)
    return tuple(output)


def normalized_string_map(value: Any, label: str, *, maximum: int = 256) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise invalid("scenario_mapping_invalid", f"{label} must be an object.")
    if len(value) > maximum:
        raise invalid(
            "scenario_mapping_too_large",
            f"{label} contains too many keys.",
            detail={"count": len(value), "maximum": maximum},
        )
    output: dict[str, str] = {}
    for key, item in value.items():
        name = identity(key, f"{label} key")
        output[name] = bounded_text(item, f"{label}.{name}", maximum_bytes=64 * 1024)
    return output


def bool_env(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on", "enabled"}


def partition(values: Sequence[Any], predicate: Any) -> tuple[list[Any], list[Any]]:
    accepted: list[Any] = []
    rejected: list[Any] = []
    for value in values:
        (accepted if predicate(value) else rejected).append(value)
    return accepted, rejected
