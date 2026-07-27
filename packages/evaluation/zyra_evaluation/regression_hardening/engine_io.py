from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contracts import ContractError, stable_digest


def atomic_json_write(path: str | Path, payload: Any) -> Path:
    destination = Path(path).resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def load_json_object(
    path: str | Path,
    *,
    maximum_bytes: int = 128 * 1024 * 1024,
) -> Mapping[str, Any]:
    source = Path(path).resolve(strict=False)
    if not source.is_file():
        raise ContractError(f"JSON source does not exist: {source}")
    if source.stat().st_size > maximum_bytes:
        raise ContractError(
            f"JSON source exceeds {maximum_bytes} bytes: {source}"
        )
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot load JSON object {source}: {error}") from error
    if not isinstance(value, Mapping):
        raise ContractError(f"JSON source is not an object: {source}")
    return value


def json_file_digest(path: str | Path) -> str:
    value = load_json_object(path)
    return stable_digest(value)


__all__ = [
    "atomic_json_write",
    "json_file_digest",
    "load_json_object",
]
