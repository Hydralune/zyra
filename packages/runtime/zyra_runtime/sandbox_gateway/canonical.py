from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from .constants import (
    COMMAND_DIGEST_PREFIX,
    CONTENT_DIGEST_PREFIX,
    DIGEST_PREFIX,
    ENVIRONMENT_DIGEST_PREFIX,
    PROVENANCE_DIGEST_PREFIX,
    TOKEN_DIGEST_PREFIX,
)
from .errors import GatewayErrorCode, SandboxGatewayError

_DRIVE = re.compile(r"^[a-zA-Z]:")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def normalize_text(value: Any, *, field: str = "value", allow_empty: bool = True) -> str:
    text = unicodedata.normalize("NFC", str(value))
    if _CONTROL.search(text):
        raise SandboxGatewayError(
            GatewayErrorCode.INVALID_REQUEST,
            f"{field} contains control characters",
            operation="canonicalize",
        )
    text = text.strip()
    if not allow_empty and not text:
        raise SandboxGatewayError(
            GatewayErrorCode.INVALID_REQUEST,
            f"{field} is required",
            operation="canonicalize",
        )
    return text


def canonical_logical_path(value: str, *, allow_root: bool = False) -> str:
    raw = unicodedata.normalize("NFC", str(value)).replace("\\", "/")
    if _CONTROL.search(raw):
        raise SandboxGatewayError(
            GatewayErrorCode.PATH_INVALID,
            "logical path contains control characters",
            operation="canonical_path",
        )
    if raw.startswith("//") or raw.startswith("/") or _DRIVE.match(raw):
        raise SandboxGatewayError(
            GatewayErrorCode.PATH_ESCAPE,
            "absolute, drive-qualified, and UNC paths are forbidden",
            operation="canonical_path",
        )
    parts: list[str] = []
    for part in raw.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise SandboxGatewayError(
                GatewayErrorCode.PATH_ESCAPE,
                "parent traversal is forbidden",
                operation="canonical_path",
            )
        normalized = unicodedata.normalize("NFC", part)
        if normalized.endswith((" ", ".")):
            raise SandboxGatewayError(
                GatewayErrorCode.PATH_INVALID,
                "trailing space or dot is not portable",
                operation="canonical_path",
            )
        if normalized.casefold() in {"con", "prn", "aux", "nul"}:
            raise SandboxGatewayError(
                GatewayErrorCode.PATH_INVALID,
                "reserved device path is forbidden",
                operation="canonical_path",
            )
        parts.append(normalized)
    canonical = PurePosixPath(*parts).as_posix() if parts else "."
    if canonical == "." and not allow_root:
        raise SandboxGatewayError(
            GatewayErrorCode.PATH_INVALID,
            "root path is not valid for this operation",
            operation="canonical_path",
        )
    return canonical


def canonicalize(value: Any, *, path: str = "$") -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str):
            return unicodedata.normalize("NFC", value)
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite float at {path}")
        return value
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    if isinstance(value, Enum):
        return canonicalize(value.value, path=path)
    if is_dataclass(value):
        return canonicalize(asdict(value), path=path)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return canonicalize(value.to_dict(), path=path)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key in sorted(value, key=lambda item: unicodedata.normalize("NFC", str(item))):
            normalized = unicodedata.normalize("NFC", str(key))
            if normalized in result:
                raise ValueError(f"canonical key collision at {path}.{normalized}")
            result[normalized] = canonicalize(value[key], path=f"{path}.{normalized}")
        return result
    if isinstance(value, (list, tuple)):
        return [canonicalize(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, (set, frozenset)):
        items = [canonicalize(item, path=f"{path}[]") for item in value]
        return sorted(items, key=lambda item: canonical_json(item))
    raise TypeError(f"unsupported canonical value at {path}: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value: Any, *, prefix: str = DIGEST_PREFIX) -> str:
    encoded = canonical_json(value).encode("utf-8")
    return f"{prefix}{hashlib.sha256(encoded).hexdigest()}"


def content_digest(content: bytes | str) -> str:
    encoded = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    return f"{CONTENT_DIGEST_PREFIX}{hashlib.sha256(encoded).hexdigest()}"


def token_digest(token: str) -> str:
    return f"{TOKEN_DIGEST_PREFIX}{hashlib.sha256(token.encode('utf-8')).hexdigest()}"


def command_digest(executable: str, argv: Sequence[str], cwd: str, env_digest: str) -> str:
    return digest(
        {
            "executable": normalize_text(executable, field="executable", allow_empty=False),
            "argv": [unicodedata.normalize("NFC", str(item)) for item in argv],
            "cwd": canonical_logical_path(cwd, allow_root=True),
            "environment_digest": env_digest,
        },
        prefix=COMMAND_DIGEST_PREFIX,
    )


def environment_digest(environment: Mapping[str, str]) -> str:
    projected = {
        unicodedata.normalize("NFC", str(key)): unicodedata.normalize("NFC", str(value))
        for key, value in environment.items()
    }
    return digest(projected, prefix=ENVIRONMENT_DIGEST_PREFIX)


def provenance_digest(value: Mapping[str, Any]) -> str:
    return digest(value, prefix=PROVENANCE_DIGEST_PREFIX)


def stable_id(namespace: str, *parts: Any, length: int = 32) -> str:
    name = normalize_text(namespace, field="namespace", allow_empty=False)
    value = digest({"namespace": name, "parts": list(parts)}).split(":")[-1]
    return f"{name}-{value[:length]}"


def bounded_utf8(value: str, *, maximum: int, field: str) -> str:
    normalized = unicodedata.normalize("NFC", str(value))
    if len(normalized.encode("utf-8")) > maximum:
        raise SandboxGatewayError(
            GatewayErrorCode.INVALID_REQUEST,
            f"{field} exceeds {maximum} bytes",
            operation="canonicalize",
        )
    return normalized


def normalize_environment(
    environment: Mapping[str, Any],
    *,
    allowed_keys: Iterable[str] | None = None,
    maximum_entries: int = 128,
    maximum_value_bytes: int = 16 * 1024,
) -> dict[str, str]:
    if len(environment) > maximum_entries:
        raise SandboxGatewayError(
            GatewayErrorCode.INVALID_REQUEST,
            "environment has too many entries",
            operation="canonicalize_environment",
        )
    allowed = {item.casefold() for item in allowed_keys} if allowed_keys is not None else None
    result: dict[str, str] = {}
    for raw_key, raw_value in sorted(environment.items(), key=lambda item: str(item[0]).casefold()):
        key = normalize_text(raw_key, field="environment key", allow_empty=False)
        if "=" in key or "\x00" in key:
            raise SandboxGatewayError(
                GatewayErrorCode.INVALID_REQUEST,
                "environment key is invalid",
                operation="canonicalize_environment",
            )
        if allowed is not None and key.casefold() not in allowed:
            raise SandboxGatewayError(
                GatewayErrorCode.POLICY_DENIED,
                f"environment key is not allowlisted: {key}",
                operation="canonicalize_environment",
            )
        value = bounded_utf8(str(raw_value), maximum=maximum_value_bytes, field=f"environment.{key}")
        result[key] = value
    return result


def executable_name(value: str) -> str:
    normalized = normalize_text(value, field="executable", allow_empty=False).replace("\\", "/")
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[-1]
    return normalized.casefold()


def is_path_like_argument(value: str) -> bool:
    return (
        value.startswith(("./", "../", ".\\", "..\\", "/", "\\\\"))
        or bool(_DRIVE.match(value))
        or "/" in value
        or "\\" in value
    )


def merge_digest(*values: str, namespace: str = "merge") -> str:
    return digest({"namespace": namespace, "values": list(values)})


def random_nonce(byte_count: int = 32) -> str:
    if byte_count < 16:
        raise ValueError("nonce must contain at least 128 bits")
    return os.urandom(byte_count).hex()
