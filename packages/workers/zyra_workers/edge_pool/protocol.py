from __future__ import annotations

import hashlib
import hmac
import json
import socket
import struct
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, BinaryIO, Mapping
from uuid import uuid4


PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 4 * 1024 * 1024


class EdgeMessageKind(StrEnum):
    ATTEST = "attest"
    HEARTBEAT = "heartbeat"
    EXECUTE = "execute"
    CANCEL = "cancel"
    DRAIN = "drain"
    WAKE = "wake"
    STOP = "stop"
    RESPONSE = "response"
    ERROR = "error"


class EdgeProtocolError(RuntimeError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(value)).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EdgeFrame:
    kind: EdgeMessageKind
    worker_id: str
    payload: Mapping[str, Any]
    request_id: str = field(default_factory=lambda: f"edge_request_{uuid4().hex}")
    protocol_version: int = PROTOCOL_VERSION
    nonce: str = field(default_factory=lambda: uuid4().hex)
    signature: str = ""

    def unsigned(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "worker_id": self.worker_id,
            "payload": dict(self.payload),
            "request_id": self.request_id,
            "protocol_version": self.protocol_version,
            "nonce": self.nonce,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned(), "signature": self.signature}

    def sign(self, secret: bytes) -> "EdgeFrame":
        value = sign_mapping(self.unsigned(), secret)
        return EdgeFrame(
            kind=self.kind,
            worker_id=self.worker_id,
            payload=dict(self.payload),
            request_id=self.request_id,
            protocol_version=self.protocol_version,
            nonce=self.nonce,
            signature=value,
        )

    def verify(self, secret: bytes) -> None:
        expected = sign_mapping(self.unsigned(), secret)
        if not hmac.compare_digest(expected, self.signature):
            raise EdgeProtocolError("edge protocol frame signature mismatch")
        if self.protocol_version != PROTOCOL_VERSION:
            raise EdgeProtocolError("edge protocol version mismatch")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EdgeFrame":
        try:
            kind = EdgeMessageKind(str(value.get("kind") or ""))
        except ValueError as error:
            raise EdgeProtocolError("unknown edge protocol message kind") from error
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            raise EdgeProtocolError("edge protocol payload must be an object")
        return cls(
            kind=kind,
            worker_id=str(value.get("worker_id") or ""),
            payload=dict(payload),
            request_id=str(value.get("request_id") or ""),
            protocol_version=int(value.get("protocol_version") or 0),
            nonce=str(value.get("nonce") or ""),
            signature=str(value.get("signature") or ""),
        )


def sign_mapping(value: Mapping[str, Any], secret: bytes) -> str:
    return hmac.new(secret, canonical_json(dict(value)).encode("utf-8"), hashlib.sha256).hexdigest()


def write_frame(stream: BinaryIO, frame: EdgeFrame) -> None:
    content = canonical_json(frame.to_dict()).encode("utf-8")
    if len(content) > MAX_FRAME_BYTES:
        raise EdgeProtocolError("edge protocol frame exceeds size limit")
    stream.write(struct.pack("!I", len(content)))
    stream.write(content)
    stream.flush()


def read_frame(stream: BinaryIO) -> EdgeFrame:
    header = _read_exact(stream, 4)
    length = struct.unpack("!I", header)[0]
    if length <= 0 or length > MAX_FRAME_BYTES:
        raise EdgeProtocolError("edge protocol frame length is invalid")
    content = _read_exact(stream, length)
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EdgeProtocolError("edge protocol frame is not valid JSON") from error
    if not isinstance(value, Mapping):
        raise EdgeProtocolError("edge protocol frame root must be an object")
    return EdgeFrame.from_dict(value)


def request(
    *,
    host: str,
    port: int,
    frame: EdgeFrame,
    secret: bytes,
    timeout_seconds: float,
) -> EdgeFrame:
    signed = frame.sign(secret)
    with socket.create_connection((host, int(port)), timeout=max(0.1, timeout_seconds)) as connection:
        connection.settimeout(max(0.1, timeout_seconds))
        stream = connection.makefile("rwb", buffering=0)
        write_frame(stream, signed)
        response = read_frame(stream)
    response.verify(secret)
    if response.request_id != frame.request_id:
        raise EdgeProtocolError("edge response request id mismatch")
    if response.worker_id != frame.worker_id:
        raise EdgeProtocolError("edge response worker id mismatch")
    if response.kind is EdgeMessageKind.ERROR:
        raise EdgeProtocolError(str(response.payload.get("message") or "edge worker rejected request"))
    return response


def _read_exact(stream: BinaryIO, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EdgeProtocolError("edge protocol connection closed mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
