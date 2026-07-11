from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


def normalize_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(normalize_text(value).encode("utf-8"))


def canonical_json(value: object) -> str:
    return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_object(value: object) -> str:
    return sha256_text(canonical_json(value))


def digest_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def digest_resource_manifest(resources: Iterable[Mapping[str, Any] | object]) -> str:
    items: list[dict[str, object]] = []
    for resource in resources:
        value = _canonical(resource)
        if not isinstance(value, dict):
            raise TypeError("resource descriptor must serialize to a mapping")
        items.append(
            {
                "relative_path": str(value.get("relative_path") or "").replace("\\", "/"),
                "digest": str(value.get("digest") or ""),
                "size_bytes": int(value.get("size_bytes") or 0),
                "media_type": str(value.get("media_type") or ""),
            }
        )
    return digest_object(sorted(items, key=lambda item: str(item["relative_path"])))


def digest_skill_package(
    *,
    metadata: object,
    body_digest: str,
    resource_manifest_digest: str,
    schema: str,
) -> str:
    return digest_object(
        {
            "schema": schema,
            "metadata": metadata,
            "body_digest": body_digest,
            "resource_manifest_digest": resource_manifest_digest,
        }
    )


def estimate_tokens(value: str | bytes) -> int:
    if isinstance(value, bytes):
        size = len(value)
    else:
        size = len(value.encode("utf-8"))
    return max(1, (size + 3) // 4)


def arguments_digest(arguments: Mapping[str, Any]) -> str:
    return digest_object(dict(arguments))


def _canonical(value: object) -> object:
    if is_dataclass(value):
        return _canonical(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_canonical(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if hasattr(value, "value") and value.__class__.__module__ == "enum":
        return str(getattr(value, "value"))
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "to_dict"):
        return _canonical(value.to_dict())
    return str(value)
