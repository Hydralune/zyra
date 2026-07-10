from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from .models import ToolIdentity

CANONICAL_ARGUMENTS_VERSION = "zyra-jcs-v1"


def canonicalize_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalized JSON value suitable for exact approval identity.

    Object keys and strings use NFC, keys are sorted, non-finite numbers and
    non-JSON values are rejected, and negative zero is normalized. Arrays keep
    their order because changing it can change tool semantics.
    """
    if not isinstance(arguments, Mapping):
        raise TypeError("tool arguments must be a mapping")
    value = _canonicalize(arguments, path="$")
    assert isinstance(value, dict)
    return value


def canonical_arguments_json(arguments: Mapping[str, Any]) -> str:
    value = canonicalize_arguments(arguments)
    return _encode(value)


def arguments_digest(arguments: Mapping[str, Any], *, algorithm: str = "sha256") -> str:
    try:
        digest = hashlib.new(algorithm)
    except ValueError as error:
        raise ValueError(f"unsupported digest algorithm: {algorithm}") from error
    digest.update(canonical_arguments_json(arguments).encode("utf-8"))
    return f"{algorithm}:{CANONICAL_ARGUMENTS_VERSION}:{digest.hexdigest()}"


def build_tool_identity(
    tool_name: str,
    *,
    namespace: str = "builtin",
    server_id: str = "",
    version: str = "",
    schema: Mapping[str, Any] | None = None,
    schema_digest: str = "",
) -> ToolIdentity:
    name = unicodedata.normalize("NFC", str(tool_name).strip())
    ns = unicodedata.normalize("NFC", str(namespace).strip() or "builtin")
    server = unicodedata.normalize("NFC", str(server_id).strip())
    if ns == "mcp" and not server:
        raise ValueError("MCP tool identity requires an exact server_id")
    if schema is not None and not schema_digest:
        schema_digest = arguments_digest(schema)
    return ToolIdentity(namespace=ns, name=name, server_id=server, version=str(version).strip(), schema_digest=schema_digest)


def build_request_fingerprint(
    tool_identity: ToolIdentity | Mapping[str, Any],
    argument_digest: str,
    *,
    session_id: str,
    tool_use_id: str,
    run_id: str = "",
    task_id: str = "",
) -> str:
    identity = tool_identity if isinstance(tool_identity, ToolIdentity) else ToolIdentity.from_dict(tool_identity)
    if not session_id or not tool_use_id or not argument_digest:
        raise ValueError("session_id, tool_use_id and argument_digest are required")
    payload = {
        "schema": "zyra.permission.request-fingerprint.v1",
        "session_id": session_id,
        "run_id": run_id,
        "task_id": task_id,
        "tool_use_id": tool_use_id,
        "tool_identity": identity.to_dict(),
        "arguments_digest": argument_digest,
    }
    encoded = _encode(_canonicalize(payload, path="$fingerprint"))
    return f"sha256:zyra-permission-request-v1:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _canonicalize(value: Any, *, path: str) -> Any:
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite number at {path}")
        return 0 if value == 0 else value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise TypeError(f"non-string object key at {path}: {raw_key!r}")
            key = unicodedata.normalize("NFC", raw_key)
            if key in output:
                raise ValueError(f"object keys collide after Unicode normalization at {path}: {key!r}")
            output[key] = _canonicalize(item, path=f"{path}.{key}")
        return {key: output[key] for key in sorted(output)}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return [_canonicalize(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    raise TypeError(f"non-JSON value at {path}: {type(value).__name__}")


def _encode(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = json.dumps(value, allow_nan=False, ensure_ascii=False)
        text = re.sub(r"e([+-]?)0+(\d+)$", r"e\1\2", text.lower())
        return text
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(f"{_encode(key)}:{_encode(value[key])}" for key in sorted(value)) + "}"
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")
