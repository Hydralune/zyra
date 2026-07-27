from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import NodeAuthenticationError, NodeReplayRejected, redact
from .models import canonical_json, digest, now_iso


PROTOCOL_VERSION = "zyra.deployment-node/v1"
DEFAULT_CLOCK_SKEW_SECONDS = 30
DEFAULT_NONCE_TTL_SECONDS = 120
MAX_BODY_BYTES = 8 * 1024 * 1024


def derive_node_secret(
    supervisor_secret: bytes,
    *,
    node_id: str,
    generation_id: str,
) -> bytes:
    if len(supervisor_secret) < 32:
        raise ValueError("supervisor secret must contain at least 32 bytes")
    material = f"{PROTOCOL_VERSION}\0{node_id}\0{generation_id}".encode("utf-8")
    return hmac.new(supervisor_secret, material, hashlib.sha256).digest()


def request_signature(
    secret: bytes,
    *,
    method: str,
    path: str,
    timestamp: int,
    nonce: str,
    body: bytes,
) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    message = "\n".join(
        (
            PROTOCOL_VERSION,
            method.upper(),
            path,
            str(timestamp),
            nonce,
            body_hash,
        )
    ).encode("utf-8")
    return "sha256=" + hmac.new(secret, message, hashlib.sha256).hexdigest()


def response_signature(
    secret: bytes,
    *,
    request_nonce: str,
    status: int,
    body: bytes,
) -> str:
    message = "\n".join(
        (
            PROTOCOL_VERSION,
            request_nonce,
            str(status),
            hashlib.sha256(body).hexdigest(),
        )
    ).encode("utf-8")
    return "sha256=" + hmac.new(secret, message, hashlib.sha256).hexdigest()


def constant_time_valid(expected: str, actual: str) -> bool:
    if not expected or not actual:
        return False
    return hmac.compare_digest(expected.encode("ascii"), actual.encode("ascii"))


@dataclass(frozen=True, slots=True)
class SignedRequest:
    method: str
    path: str
    timestamp: int
    nonce: str
    body: bytes
    signature: str

    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Content-Length": str(len(self.body)),
            "X-Zyra-Node-Protocol": PROTOCOL_VERSION,
            "X-Zyra-Timestamp": str(self.timestamp),
            "X-Zyra-Nonce": self.nonce,
            "X-Zyra-Signature": self.signature,
        }


class RequestSigner:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("request signer secret must contain at least 32 bytes")
        self.secret = bytes(secret)

    def sign(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        timestamp: int | None = None,
        nonce: str | None = None,
    ) -> SignedRequest:
        body = canonical_json(dict(payload or {}))
        if len(body) > MAX_BODY_BYTES:
            raise ValueError("deployment node request exceeds maximum body size")
        observed_timestamp = int(time.time()) if timestamp is None else int(timestamp)
        observed_nonce = nonce or secrets.token_urlsafe(24)
        signature = request_signature(
            self.secret,
            method=method,
            path=path,
            timestamp=observed_timestamp,
            nonce=observed_nonce,
            body=body,
        )
        return SignedRequest(
            method=method.upper(),
            path=path,
            timestamp=observed_timestamp,
            nonce=observed_nonce,
            body=body,
            signature=signature,
        )

    def verify_response(
        self,
        request: SignedRequest,
        *,
        status: int,
        body: bytes,
        signature: str,
    ) -> None:
        expected = response_signature(
            self.secret,
            request_nonce=request.nonce,
            status=status,
            body=body,
        )
        if not constant_time_valid(expected, signature):
            raise NodeAuthenticationError(
                "node_response_signature_invalid",
                "node response signature did not match the authenticated request",
                operation=request.path,
            )


class NonceWindow:
    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_NONCE_TTL_SECONDS,
        maximum_entries: int = 4096,
        clock: Any = time.time,
    ) -> None:
        if ttl_seconds < 10 or ttl_seconds > 3600:
            raise ValueError("nonce TTL must be between 10 and 3600 seconds")
        if maximum_entries < 64 or maximum_entries > 1_000_000:
            raise ValueError("nonce maximum entries is outside the supported range")
        self.ttl_seconds = ttl_seconds
        self.maximum_entries = maximum_entries
        self.clock = clock
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, float] = OrderedDict()

    def admit(self, nonce: str, *, now: float | None = None) -> None:
        observed = float(self.clock() if now is None else now)
        normalized = nonce.strip()
        if len(normalized) < 16 or len(normalized) > 256:
            raise NodeReplayRejected(
                "node_nonce_invalid",
                "node request nonce has an invalid length",
            )
        with self._lock:
            self._prune(observed)
            if normalized in self._entries:
                raise NodeReplayRejected(
                    "node_nonce_replayed",
                    "node request nonce was already admitted",
                    details={"nonce_digest": digest(normalized)},
                )
            self._entries[normalized] = observed
            while len(self._entries) > self.maximum_entries:
                self._entries.popitem(last=False)

    def _prune(self, now: float) -> None:
        threshold = now - self.ttl_seconds
        while self._entries:
            _nonce, observed = next(iter(self._entries.items()))
            if observed >= threshold:
                break
            self._entries.popitem(last=False)

    @property
    def size(self) -> int:
        with self._lock:
            self._prune(float(self.clock()))
            return len(self._entries)


class RequestAuthenticator:
    def __init__(
        self,
        secret: bytes,
        *,
        nonce_window: NonceWindow | None = None,
        clock_skew_seconds: int = DEFAULT_CLOCK_SKEW_SECONDS,
        clock: Any = time.time,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("request authenticator secret must contain at least 32 bytes")
        if clock_skew_seconds < 1 or clock_skew_seconds > 300:
            raise ValueError("clock skew must be between 1 and 300 seconds")
        self.secret = bytes(secret)
        self.nonces = nonce_window or NonceWindow(clock=clock)
        self.clock_skew_seconds = clock_skew_seconds
        self.clock = clock

    def authenticate(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
    ) -> str:
        protocol = str(headers.get("X-Zyra-Node-Protocol") or "")
        if protocol != PROTOCOL_VERSION:
            raise NodeAuthenticationError(
                "node_protocol_version_invalid",
                "deployment node protocol version is missing or unsupported",
                operation=path,
                details={"protocol": protocol},
            )
        if len(body) > MAX_BODY_BYTES:
            raise NodeAuthenticationError(
                "node_request_too_large",
                "deployment node request exceeds the maximum body size",
                operation=path,
            )
        raw_timestamp = str(headers.get("X-Zyra-Timestamp") or "")
        try:
            timestamp = int(raw_timestamp)
        except ValueError as error:
            raise NodeAuthenticationError(
                "node_timestamp_invalid",
                "deployment node request timestamp is invalid",
                operation=path,
            ) from error
        now = int(self.clock())
        if abs(now - timestamp) > self.clock_skew_seconds:
            raise NodeAuthenticationError(
                "node_timestamp_stale",
                "deployment node request timestamp is outside the accepted window",
                operation=path,
                details={"observed_at": now_iso(), "skew_seconds": abs(now - timestamp)},
            )
        nonce = str(headers.get("X-Zyra-Nonce") or "")
        signature = str(headers.get("X-Zyra-Signature") or "")
        expected = request_signature(
            self.secret,
            method=method,
            path=path,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        )
        if not constant_time_valid(expected, signature):
            raise NodeAuthenticationError(
                "node_request_signature_invalid",
                "deployment node request signature is invalid",
                operation=path,
            )
        self.nonces.admit(nonce, now=float(now))
        return nonce

    def sign_response(
        self,
        *,
        request_nonce: str,
        status: int,
        body: bytes,
    ) -> str:
        return response_signature(
            self.secret,
            request_nonce=request_nonce,
            status=status,
            body=body,
        )


def safe_json_loads(body: bytes) -> dict[str, Any]:
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("JSON body exceeds maximum size")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("JSON body is invalid") from error
    if not isinstance(value, dict):
        raise ValueError("JSON request body must be an object")
    return value


def public_error(error: BaseException) -> dict[str, Any]:
    if hasattr(error, "to_dict") and callable(error.to_dict):
        return redact(error.to_dict())
    return {
        "schema": "zyra.deployment-error/v1",
        "error": "node_internal_error",
        "message": f"{type(error).__name__}: {error}",
        "fallback": False,
    }


__all__ = [
    "DEFAULT_CLOCK_SKEW_SECONDS",
    "DEFAULT_NONCE_TTL_SECONDS",
    "MAX_BODY_BYTES",
    "NonceWindow",
    "PROTOCOL_VERSION",
    "RequestAuthenticator",
    "RequestSigner",
    "SignedRequest",
    "constant_time_valid",
    "derive_node_secret",
    "public_error",
    "request_signature",
    "response_signature",
    "safe_json_loads",
]
