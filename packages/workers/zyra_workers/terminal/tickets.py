from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .models import TerminalBinding, TerminalError


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decoded(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


@dataclass(frozen=True, slots=True)
class TerminalTicket:
    token: str
    nonce: str
    expires_at: float
    binding: TerminalBinding
    origin: str
    cursor: int
    protocol: str

    @property
    def expires_at_iso(self) -> str:
        return datetime.fromtimestamp(self.expires_at, UTC).isoformat()


class TerminalTicketAuthority:
    def __init__(
        self,
        secret: bytes,
        *,
        ttl_seconds: float = 20.0,
        maximum_outstanding: int = 4_096,
        clock: Any = time.time,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("terminal ticket secret must contain at least 32 bytes")
        if ttl_seconds < 1 or ttl_seconds > 300:
            raise ValueError("terminal ticket TTL must be in 1..300 seconds")
        self._secret = bytes(secret)
        self._ttl_seconds = ttl_seconds
        self._maximum_outstanding = maximum_outstanding
        self._clock = clock
        self._lock = threading.RLock()
        self._outstanding: dict[str, float] = {}
        self._consumed: dict[str, float] = {}

    @classmethod
    def random(
        cls,
        *,
        ttl_seconds: float = 20.0,
        maximum_outstanding: int = 4_096,
    ) -> TerminalTicketAuthority:
        return cls(
            secrets.token_bytes(32),
            ttl_seconds=ttl_seconds,
            maximum_outstanding=maximum_outstanding,
        )

    def issue(
        self,
        *,
        binding: TerminalBinding,
        origin: str,
        cursor: int,
        protocol: str = "zyra.terminal.v1",
    ) -> TerminalTicket:
        selected_origin = self._origin(origin)
        if cursor < 0:
            raise TerminalError(
                "terminal_ticket_cursor_invalid",
                "Terminal ticket cursor must be non-negative.",
                status=400,
            )
        if protocol != "zyra.terminal.v1":
            raise TerminalError(
                "terminal_ticket_protocol_unsupported",
                "Terminal ticket protocol is unsupported.",
                status=426,
            )
        now = float(self._clock())
        expires_at = now + self._ttl_seconds
        nonce = secrets.token_urlsafe(24)
        payload = {
            "v": 1,
            "nonce": nonce,
            "exp": expires_at,
            "iat": now,
            "origin": selected_origin,
            "cursor": cursor,
            "protocol": protocol,
            "binding": binding.to_json(),
        }
        material = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(self._secret, material, hashlib.sha256).digest()
        token = f"{_encoded(material)}.{_encoded(signature)}"
        with self._lock:
            self._prune(now)
            if len(self._outstanding) >= self._maximum_outstanding:
                oldest = min(self._outstanding, key=self._outstanding.__getitem__)
                self._outstanding.pop(oldest, None)
            self._outstanding[nonce] = expires_at
        return TerminalTicket(
            token=token,
            nonce=nonce,
            expires_at=expires_at,
            binding=binding,
            origin=selected_origin,
            cursor=cursor,
            protocol=protocol,
        )

    def consume(
        self,
        token: str,
        *,
        expected_origin: str,
        expected_task_id: str,
        expected_terminal_id: str,
        expected_protocol: str,
    ) -> TerminalTicket:
        if not token or len(token) > 8_192 or token.count(".") != 1:
            raise TerminalError(
                "terminal_ticket_invalid",
                "Terminal ticket is malformed.",
                status=403,
            )
        payload_part, signature_part = token.split(".", 1)
        try:
            material = _decoded(payload_part)
            signature = _decoded(signature_part)
        except (ValueError, base64.binascii.Error) as error:
            raise TerminalError(
                "terminal_ticket_invalid",
                "Terminal ticket encoding is invalid.",
                status=403,
            ) from error
        expected_signature = hmac.new(
            self._secret,
            material,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected_signature):
            raise TerminalError(
                "terminal_ticket_signature_invalid",
                "Terminal ticket signature is invalid.",
                status=403,
            )
        try:
            payload = json.loads(material)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TerminalError(
                "terminal_ticket_invalid",
                "Terminal ticket payload is invalid.",
                status=403,
            ) from error
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise TerminalError(
                "terminal_ticket_version_invalid",
                "Terminal ticket version is unsupported.",
                status=426,
            )
        nonce = str(payload.get("nonce") or "")
        expires_at = float(payload.get("exp") or 0)
        issued_at = float(payload.get("iat") or 0)
        now = float(self._clock())
        if expires_at <= now or issued_at > now + 5 or expires_at - issued_at > 300:
            raise TerminalError(
                "terminal_ticket_expired",
                "Terminal ticket expired.",
                status=410,
            )
        origin = self._origin(str(payload.get("origin") or ""))
        if not hmac.compare_digest(origin, self._origin(expected_origin)):
            raise TerminalError(
                "terminal_ticket_origin_mismatch",
                "Terminal ticket origin does not match the WebSocket origin.",
                status=403,
            )
        protocol = str(payload.get("protocol") or "")
        if (
            protocol != "zyra.terminal.v1"
            or protocol != expected_protocol
        ):
            raise TerminalError(
                "terminal_ticket_protocol_mismatch",
                "Terminal ticket protocol does not match the connection.",
                status=426,
            )
        binding = self._binding(payload.get("binding"))
        if (
            binding.task_id != expected_task_id
            or binding.terminal_id != expected_terminal_id
        ):
            raise TerminalError(
                "terminal_ticket_binding_mismatch",
                "Terminal ticket belongs to another task or terminal.",
                status=403,
            )
        cursor = int(payload.get("cursor") or 0)
        if cursor < 0:
            raise TerminalError(
                "terminal_ticket_cursor_invalid",
                "Terminal ticket cursor is invalid.",
                status=403,
            )
        with self._lock:
            self._prune(now)
            outstanding = self._outstanding.pop(nonce, None)
            if outstanding is None:
                code = (
                    "terminal_ticket_replayed"
                    if nonce in self._consumed
                    else "terminal_ticket_unknown"
                )
                raise TerminalError(
                    code,
                    "Terminal ticket was already consumed or is unknown.",
                    status=403,
                )
            if abs(outstanding - expires_at) > 0.001:
                raise TerminalError(
                    "terminal_ticket_state_mismatch",
                    "Terminal ticket authority state does not match its payload.",
                    status=403,
                )
            self._consumed[nonce] = expires_at
        return TerminalTicket(
            token=token,
            nonce=nonce,
            expires_at=expires_at,
            binding=binding,
            origin=origin,
            cursor=cursor,
            protocol=protocol,
        )

    def snapshot(self) -> dict[str, int | float]:
        now = float(self._clock())
        with self._lock:
            self._prune(now)
            return {
                "outstanding": len(self._outstanding),
                "consumed": len(self._consumed),
                "ttl_seconds": self._ttl_seconds,
                "maximum_outstanding": self._maximum_outstanding,
            }

    def _prune(self, now: float) -> None:
        self._outstanding = {
            nonce: expires
            for nonce, expires in self._outstanding.items()
            if expires > now
        }
        self._consumed = {
            nonce: expires
            for nonce, expires in self._consumed.items()
            if expires > now
        }

    @staticmethod
    def _origin(value: str) -> str:
        selected = value.strip()
        if len(selected) < 8 or len(selected) > 2_048:
            raise TerminalError(
                "terminal_origin_invalid",
                "Terminal origin is invalid.",
                status=403,
            )
        from urllib.parse import urlsplit

        parsed = urlsplit(selected)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise TerminalError(
                "terminal_origin_invalid",
                "Terminal origin must use HTTP or HTTPS.",
                status=403,
            )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise TerminalError(
                "terminal_origin_invalid",
                "Terminal origin contains unsupported components.",
                status=403,
            )
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _binding(value: Any) -> TerminalBinding:
        if not isinstance(value, dict):
            raise TerminalError(
                "terminal_ticket_binding_invalid",
                "Terminal ticket binding is invalid.",
                status=403,
            )
        try:
            return TerminalBinding(
                task_id=str(value["task_id"]),
                run_id=str(value["run_id"]),
                terminal_id=str(value["terminal_id"]),
                session_id=str(value["session_id"]),
                workspace_id=str(value["workspace_id"]),
                workspace_revision=int(value["workspace_revision"]),
                worker_id=str(value["worker_id"]),
                command_id=str(value["command_id"]),
                tool_call_id=str(value["tool_call_id"]),
                span_id=str(value["span_id"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise TerminalError(
                "terminal_ticket_binding_invalid",
                "Terminal ticket binding is incomplete.",
                status=403,
            ) from error
