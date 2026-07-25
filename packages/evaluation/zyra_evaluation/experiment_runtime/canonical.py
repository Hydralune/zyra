from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import invalid


IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+~-]{0,254}$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
COMMIT_DIGEST = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
METRIC = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
UNIT = re.compile(r"^[A-Za-z][A-Za-z0-9_./%*-]{0,63}$")
SECRET = re.compile(
    r"(authorization|credential|password|secret|token|api[_-]?key|cookie)",
    re.IGNORECASE,
)


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def new_identity(prefix: str) -> str:
    return f"{identity(prefix, 'identity prefix')}_{uuid4().hex}"


def identity(value: Any, label: str) -> str:
    selected = str(value or "").strip()
    if not IDENTITY.fullmatch(selected):
        raise invalid(
            "experiment_identity_invalid",
            f"{label} is not a valid Zyra identity.",
            detail={"label": label, "value": selected[:256]},
        )
    return selected


def optional_identity(value: Any, label: str) -> str:
    selected = str(value or "").strip()
    return identity(selected, label) if selected else ""


def require_digest(value: Any, label: str) -> str:
    selected = str(value or "").strip().lower()
    if not DIGEST.fullmatch(selected):
        raise invalid(
            "experiment_digest_invalid",
            f"{label} must be a lowercase SHA-256 digest.",
            detail={"label": label},
        )
    return selected


def require_commit_digest(value: Any, label: str = "commit SHA") -> str:
    selected = str(value or "").strip().lower()
    if not COMMIT_DIGEST.fullmatch(selected):
        raise invalid(
            "experiment_commit_digest_invalid",
            f"{label} must be a 40- or 64-character hexadecimal commit digest.",
            detail={"label": label},
        )
    return selected


def metric_name(value: Any, label: str = "metric") -> str:
    selected = str(value or "").strip()
    if not METRIC.fullmatch(selected):
        raise invalid(
            "experiment_metric_invalid",
            f"{label} is not a valid metric name.",
            detail={"label": label, "value": selected[:128]},
        )
    return selected


def unit_name(value: Any, label: str = "unit") -> str:
    selected = str(value or "").strip()
    if not UNIT.fullmatch(selected):
        raise invalid(
            "experiment_unit_invalid",
            f"{label} is not a valid metric unit.",
            detail={"label": label, "value": selected[:64]},
        )
    return selected


def bounded_text(
    value: Any,
    label: str,
    *,
    minimum: int = 1,
    maximum_bytes: int = 256 * 1024,
) -> str:
    selected = str(value or "").strip()
    size = len(selected.encode("utf-8"))
    if size < minimum or size > maximum_bytes:
        raise invalid(
            "experiment_text_invalid",
            f"{label} must contain {minimum} through {maximum_bytes} UTF-8 bytes.",
            detail={"label": label, "size": size},
        )
    return selected


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
        raise invalid(
            "experiment_integer_invalid",
            f"{label} must be an integer.",
        )
    try:
        selected = int(value)
    except (TypeError, ValueError) as error:
        raise invalid(
            "experiment_integer_invalid",
            f"{label} must be an integer.",
        ) from error
    if selected < minimum or selected > maximum:
        raise invalid(
            "experiment_integer_out_of_range",
            f"{label} must be between {minimum} and {maximum}.",
            detail={"label": label, "value": selected},
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
            "experiment_number_invalid",
            f"{label} must be a finite number.",
        )
    try:
        selected = float(value)
    except (TypeError, ValueError) as error:
        raise invalid(
            "experiment_number_invalid",
            f"{label} must be a finite number.",
        ) from error
    if not math.isfinite(selected):
        raise invalid(
            "experiment_number_invalid",
            f"{label} must be a finite number.",
        )
    if minimum is not None and selected < minimum:
        raise invalid(
            "experiment_number_out_of_range",
            f"{label} is below its minimum.",
            detail={"label": label, "value": selected, "minimum": minimum},
        )
    if maximum is not None and selected > maximum:
        raise invalid(
            "experiment_number_out_of_range",
            f"{label} exceeds its maximum.",
            detail={"label": label, "value": selected, "maximum": maximum},
        )
    return selected


def canonicalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((canonicalize(item) for item in value), key=canonical_json)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        selected = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return selected.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return canonicalize(value.value)
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return canonicalize(value.to_dict())
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise invalid(
                "experiment_non_finite_value",
                "Canonical evidence cannot contain NaN or infinity.",
            )
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


def content_digest(value: bytes | str) -> str:
    selected = value.encode("utf-8") if isinstance(value, str) else value
    return sha256_bytes(selected)


def stable_unique(values: Iterable[str]) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        selected = str(value or "").strip()
        if not selected or selected in seen:
            continue
        seen.add(selected)
        output.append(selected)
    return tuple(output)


def string_map(
    value: Any,
    label: str,
    *,
    maximum: int = 256,
    value_bytes: int = 64 * 1024,
) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise invalid(
            "experiment_mapping_invalid",
            f"{label} must be an object.",
        )
    if len(value) > maximum:
        raise invalid(
            "experiment_mapping_too_large",
            f"{label} has too many keys.",
            detail={"label": label, "count": len(value), "maximum": maximum},
        )
    output: dict[str, str] = {}
    for key, item in value.items():
        name = identity(key, f"{label} key")
        output[name] = bounded_text(
            item,
            f"{label}.{name}",
            maximum_bytes=value_bytes,
        )
    return output


def redact(value: Any, *, maximum_depth: int = 16, _depth: int = 0) -> Any:
    if _depth > maximum_depth:
        return "[depth-limit]"
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            output[name] = (
                "[redacted]"
                if SECRET.search(name)
                else redact(
                    item,
                    maximum_depth=maximum_depth,
                    _depth=_depth + 1,
                )
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


def require_keys(
    value: Mapping[str, Any],
    keys: Iterable[str],
    label: str,
) -> None:
    missing = sorted(key for key in keys if key not in value)
    if missing:
        raise invalid(
            "experiment_fields_missing",
            f"{label} is missing required fields.",
            detail={"label": label, "missing": missing},
        )


def parse_utc(value: Any, label: str) -> datetime:
    selected = bounded_text(value, label, maximum_bytes=128)
    try:
        moment = datetime.fromisoformat(selected.replace("Z", "+00:00"))
    except ValueError as error:
        raise invalid(
            "experiment_timestamp_invalid",
            f"{label} is not an ISO-8601 timestamp.",
            detail={"label": label, "value": selected},
        ) from error
    if moment.tzinfo is None:
        raise invalid(
            "experiment_timestamp_timezone_missing",
            f"{label} must include a timezone.",
        )
    return moment.astimezone(timezone.utc)
