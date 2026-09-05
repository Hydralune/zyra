from __future__ import annotations

import base64
import hashlib
import io
import json
import urllib.request
import zipfile
from functools import lru_cache
from typing import Any, Mapping
from urllib.parse import urlparse


MAX_BYTES = 32 * 1024 * 1024


def encode_tensors(tensors: Mapping[str, Any]) -> str:
    import numpy as np

    if not tensors or sum(v.nbytes for v in tensors.values()) > MAX_BYTES:
        raise ValueError("tensor payload is empty or exceeds the byte budget")
    if any(v.dtype.kind not in "biuf" for v in tensors.values()):
        raise ValueError("only numeric and boolean tensors are supported")
    stream = io.BytesIO()
    np.savez(stream, **tensors)
    return base64.b64encode(stream.getvalue()).decode("ascii")


def decode_tensors(value: str) -> dict[str, Any]:
    import numpy as np

    if len(value) > MAX_BYTES * 2:
        raise ValueError("encoded tensor payload exceeds the byte budget")
    data = base64.b64decode(value, validate=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if sum(f.file_size for f in archive.infolist()) > MAX_BYTES:
            raise ValueError("expanded tensor archive exceeds the byte budget")
    with np.load(io.BytesIO(data), allow_pickle=False) as archive:
        result = {name: archive[name] for name in archive.files}
    if not result or any(v.dtype.kind not in "biuf" for v in result.values()):
        raise ValueError("tensor payload must contain numeric or boolean arrays")
    return result


def tensor_digest(tensors: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    for name, value in sorted(tensors.items()):
        header = json.dumps([name, str(value.dtype), list(value.shape)], separators=(",", ":"))
        digest.update(len(header.encode()).to_bytes(8, "big"))
        digest.update(header.encode())
        digest.update(value.tobytes(order="C"))
    return digest.hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        raise ValueError("inference endpoint redirects are forbidden")


@lru_cache(maxsize=1)
def _opener():
    # Reuse the TLS trust-store setup; rebuilding it for every sample is
    # particularly expensive on Windows. No cookies or shared credentials.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def request_json(url: str, token: str, payload: dict[str, Any] | None = None,
                 *, timeout: float = 15.0) -> dict[str, Any]:
    parsed = urlparse(url)
    if (parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.scheme not in {"http", "https"}):
        raise ValueError("invalid inference endpoint")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("non-loopback inference endpoints require HTTPS")
    if len(token) < 32:
        raise ValueError("inference authentication token must contain at least 32 characters")
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
    })
    with _opener().open(request, timeout=timeout) as response:
        data = response.read(MAX_BYTES * 2 + 1)
    if len(data) > MAX_BYTES * 2:
        raise ValueError("inference response exceeds the byte budget")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("inference response must be an object")
    return result
