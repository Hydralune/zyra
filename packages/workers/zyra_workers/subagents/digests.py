from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping


def canonical_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_object(value: Any, *, prefix: str = "sha256") -> str:
    payload = canonical_json(value).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(payload).hexdigest()}"


def digest_text(value: str, *, prefix: str = "sha256") -> str:
    return f"{prefix}:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def digest_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def stable_id(namespace: str, *parts: Any, length: int = 24) -> str:
    digest = digest_object({"namespace": namespace, "parts": parts}).split(":", 1)[1]
    return f"{namespace}_{digest[:length]}"


def verify_digest(value: Any, expected: str) -> bool:
    if not expected:
        return False
    return digest_object(value) == expected


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    return value

