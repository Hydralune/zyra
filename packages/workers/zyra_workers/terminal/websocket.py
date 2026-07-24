from __future__ import annotations

import base64
import hashlib
import json
import select
import socket
import struct
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any

from .models import TerminalControlRequest, TerminalError, TerminalPhase
from .runtime import TerminalSession, TerminalSessionRegistry


WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TERMINAL_SUBPROTOCOL = "zyra-terminal-v1"


@dataclass(frozen=True, slots=True)
class WebSocketMessage:
    opcode: int
    data: bytes


class WebSocketPeer:
    def __init__(
        self,
        connection: socket.socket,
        *,
        maximum_message_bytes: int = 512 * 1_024,
    ) -> None:
        self.connection = connection
        self.maximum_message_bytes = maximum_message_bytes
        self.closed = False
        self._fragment_opcode = 0
        self._fragments = bytearray()

    def send_json(self, value: Any) -> None:
        material = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        self.send(0x1, material)

    def send(self, opcode: int, data: bytes = b"") -> None:
        if self.closed:
            return
        if len(data) > self.maximum_message_bytes:
            raise TerminalError(
                "terminal_websocket_message_oversized",
                "Terminal WebSocket server message exceeds its budget.",
                status=500,
            )
        header = bytearray([0x80 | (opcode & 0x0F)])
        if len(data) < 126:
            header.append(len(data))
        elif len(data) <= 0xFFFF:
            header.append(126)
            header.extend(struct.pack("!H", len(data)))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", len(data)))
        self.connection.sendall(bytes(header) + data)

    def receive(self) -> WebSocketMessage | None:
        while not self.closed:
            first = self._read_exact(2)
            if first is None:
                return None
            final = bool(first[0] & 0x80)
            reserved = first[0] & 0x70
            opcode = first[0] & 0x0F
            masked = bool(first[1] & 0x80)
            length = first[1] & 0x7F
            if reserved:
                self.close(1002, "Reserved WebSocket bits are unsupported.")
                return None
            if not masked:
                self.close(1002, "Client WebSocket frames must be masked.")
                return None
            if length == 126:
                raw = self._read_exact(2)
                if raw is None:
                    return None
                length = struct.unpack("!H", raw)[0]
            elif length == 127:
                raw = self._read_exact(8)
                if raw is None:
                    return None
                length = struct.unpack("!Q", raw)[0]
                if length & (1 << 63):
                    self.close(1002, "Invalid WebSocket frame length.")
                    return None
            control = opcode >= 0x8
            if control and (not final or length > 125):
                self.close(1002, "Invalid fragmented WebSocket control frame.")
                return None
            if length > self.maximum_message_bytes:
                self.close(1009, "Terminal WebSocket message is too large.")
                return None
            mask = self._read_exact(4)
            payload = self._read_exact(length)
            if mask is None or payload is None:
                return None
            data = bytes(
                value ^ mask[index % 4]
                for index, value in enumerate(payload)
            )
            if opcode == 0x8:
                code = 1000
                reason = ""
                if len(data) == 1:
                    self.close(1002, "Invalid WebSocket close payload.")
                    return None
                if len(data) >= 2:
                    code = struct.unpack("!H", data[:2])[0]
                    reason = data[2:].decode("utf-8", errors="replace")
                self.close(code, reason, reply=True)
                return None
            if opcode == 0x9:
                self.send(0xA, data)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x0:
                if not self._fragment_opcode:
                    self.close(1002, "Unexpected WebSocket continuation frame.")
                    return None
                self._fragments.extend(data)
                if len(self._fragments) > self.maximum_message_bytes:
                    self.close(1009, "Fragmented terminal message is too large.")
                    return None
                if not final:
                    continue
                message = WebSocketMessage(
                    self._fragment_opcode,
                    bytes(self._fragments),
                )
                self._fragment_opcode = 0
                self._fragments.clear()
                return message
            if opcode not in {0x1, 0x2}:
                self.close(1002, "Unsupported WebSocket opcode.")
                return None
            if final:
                return WebSocketMessage(opcode, data)
            self._fragment_opcode = opcode
            self._fragments = bytearray(data)
        return None

    def close(
        self,
        code: int = 1000,
        reason: str = "",
        *,
        reply: bool = False,
    ) -> None:
        if self.closed:
            return
        selected_reason = reason.encode("utf-8")[:123]
        if not reply:
            try:
                self.send(0x8, struct.pack("!H", code) + selected_reason)
            except OSError:
                pass
        else:
            try:
                self.send(0x8, struct.pack("!H", code) + selected_reason)
            except OSError:
                pass
        self.closed = True

    def _read_exact(self, length: int) -> bytes | None:
        material = bytearray()
        while len(material) < length:
            try:
                chunk = self.connection.recv(length - len(material))
            except socket.timeout:
                raise
            except (ConnectionError, OSError):
                self.closed = True
                return None
            if not chunk:
                self.closed = True
                return None
            material.extend(chunk)
        return bytes(material)


class TerminalWebSocketHandler:
    def __init__(
        self,
        registry: TerminalSessionRegistry,
        *,
        heartbeat_seconds: float = 5.0,
        maximum_unacked_bytes: int = 4 * 1_024 * 1_024,
    ) -> None:
        self.registry = registry
        self.heartbeat_seconds = heartbeat_seconds
        self.maximum_unacked_bytes = maximum_unacked_bytes

    def upgrade(
        self,
        handler: BaseHTTPRequestHandler,
        *,
        task_id: str,
        terminal_id: str,
        ticket: str,
        cursor: int,
        protocol: str,
    ) -> None:
        origin = str(handler.headers.get("Origin") or "").strip()
        self._validate_upgrade(handler)
        session, consumed, connection_id = self.registry.consume_ticket(
            ticket,
            origin=origin,
            task_id=task_id,
            terminal_id=terminal_id,
            protocol=protocol,
        )
        if cursor != consumed.cursor:
            session.detach(connection_id)
            raise TerminalError(
                "terminal_ticket_cursor_mismatch",
                "WebSocket cursor does not match the issued terminal ticket.",
                status=403,
            )
        key = str(handler.headers.get("Sec-WebSocket-Key") or "")
        accept = base64.b64encode(
            hashlib.sha1(
                f"{key}{WEBSOCKET_GUID}".encode("ascii"),
                usedforsecurity=False,
            ).digest()
        ).decode("ascii")
        handler.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        handler.send_header("Upgrade", "websocket")
        handler.send_header("Connection", "Upgrade")
        handler.send_header("Sec-WebSocket-Accept", accept)
        handler.send_header("Sec-WebSocket-Protocol", TERMINAL_SUBPROTOCOL)
        handler.end_headers()
        handler.wfile.flush()
        handler.close_connection = True
        peer = WebSocketPeer(handler.connection)
        previous_timeout = handler.connection.gettimeout()
        handler.connection.settimeout(2.0)
        try:
            try:
                self._serve(
                    peer,
                    session=session,
                    connection_id=connection_id,
                    cursor=cursor,
                )
            except TerminalError as error:
                try:
                    peer.send_json(
                        {
                            "kind": "error",
                            "protocol": "zyra.terminal.v1",
                            "binding": session.binding.to_json(),
                            "code": error.code,
                            "message": str(error),
                            "retryable": error.retryable,
                            "close_code": 1011,
                            "details": dict(error.details),
                        }
                    )
                except OSError:
                    pass
                peer.close(1011, error.code)
            except (ConnectionError, OSError):
                peer.close(1011, "terminal_websocket_transport_failed")
        finally:
            peer.close()
            session.detach(connection_id)
            try:
                handler.connection.settimeout(previous_timeout)
            except OSError:
                pass

    def _serve(
        self,
        peer: WebSocketPeer,
        *,
        session: TerminalSession,
        connection_id: str,
        cursor: int,
    ) -> None:
        replay = session.journal.replay(cursor)
        peer.send_json(
            {
                "kind": "hello",
                "protocol": "zyra.terminal.v1",
                "binding": session.binding.to_json(),
                "status": session.status.to_json(),
                "accepted_cursor": replay.accepted_cursor,
                "earliest_cursor": replay.earliest_cursor,
                "connection_id": connection_id,
                "heartbeat_ms": int(self.heartbeat_seconds * 1_000),
                "maximum_unacked_bytes": self.maximum_unacked_bytes,
            }
        )
        if replay.resync:
            peer.send_json(
                {
                    "kind": "resync",
                    "protocol": "zyra.terminal.v1",
                    "binding": session.binding.to_json(),
                    "requested_cursor": replay.requested_cursor,
                    "accepted_cursor": replay.accepted_cursor,
                    "earliest_cursor": replay.earliest_cursor,
                    "reason": replay.reason,
                    "spills": [spill.to_json() for spill in replay.spills],
                }
            )
        cursor = replay.accepted_cursor
        acknowledged = cursor
        output_sequence = 0
        status_mutation = ""
        last_heartbeat = time.monotonic()
        while not peer.closed:
            replay = session.journal.replay(cursor)
            for chunk in replay.chunks:
                if chunk.first_cursor < cursor:
                    continue
                if chunk.next_cursor - acknowledged > self.maximum_unacked_bytes:
                    peer.send_json(
                        {
                            "kind": "backpressure",
                            "protocol": "zyra.terminal.v1",
                            "binding": session.binding.to_json(),
                            "unacked_bytes": chunk.next_cursor - acknowledged,
                            "maximum_unacked_bytes": self.maximum_unacked_bytes,
                            "paused": True,
                            "required_cursor": acknowledged,
                        }
                    )
                    break
                output_sequence += 1
                frame = chunk.to_frame(session.binding.to_json())
                frame["sequence"] = output_sequence
                peer.send_json(frame)
                cursor = chunk.next_cursor
            status = session.status
            if status.state_mutation_id != status_mutation:
                status_mutation = status.state_mutation_id
                peer.send_json(
                    {
                        "kind": "status",
                        "protocol": "zyra.terminal.v1",
                        "binding": session.binding.to_json(),
                        "status": status.to_json(),
                    }
                )
            if (
                status.phase != TerminalPhase.RUNNING
                and cursor >= session.journal.cursor
            ):
                peer.close(1000, f"terminal {status.phase}")
                return
            readable, _, _ = select.select([peer.connection], [], [], 0.1)
            if readable:
                try:
                    message = peer.receive()
                except socket.timeout:
                    peer.close(1002, "Terminal WebSocket frame timed out.")
                    return
            else:
                message = None
            if message is not None:
                if message.opcode != 0x1:
                    peer.close(1003, "Terminal accepts JSON text frames only.")
                    return
                try:
                    payload = json.loads(message.data.decode("utf-8"))
                    acknowledged = self._command(
                        peer,
                        session=session,
                        connection_id=connection_id,
                        payload=payload,
                        acknowledged=acknowledged,
                        cursor=cursor,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError):
                    peer.close(1007, "Terminal command is not valid UTF-8 JSON.")
                    return
                except TerminalError as error:
                    peer.send_json(
                        {
                            "kind": "error",
                            "protocol": "zyra.terminal.v1",
                            "binding": session.binding.to_json(),
                            "code": error.code,
                            "message": str(error),
                            "retryable": error.retryable,
                            "close_code": 1008 if error.status in {400, 403} else 1011,
                            "details": dict(error.details),
                        }
                    )
                    if not error.retryable:
                        peer.close(1008, error.code)
                        return
            now = time.monotonic()
            if now - last_heartbeat >= self.heartbeat_seconds:
                peer.send_json(
                    {
                        "kind": "cursor",
                        "protocol": "zyra.terminal.v1",
                        "binding": session.binding.to_json(),
                        "cursor": cursor,
                        "sequence": max(1, output_sequence),
                    }
                )
                last_heartbeat = now

    def _command(
        self,
        peer: WebSocketPeer,
        *,
        session: TerminalSession,
        connection_id: str,
        payload: Any,
        acknowledged: int,
        cursor: int,
    ) -> int:
        if not isinstance(payload, dict):
            raise TerminalError(
                "terminal_command_invalid",
                "Terminal WebSocket command must be an object.",
                status=400,
            )
        if payload.get("protocol") != "zyra.terminal.v1":
            raise TerminalError(
                "terminal_protocol_mismatch",
                "Terminal command protocol is unsupported.",
                status=426,
            )
        if (
            str(payload.get("terminal_id") or "") != session.binding.terminal_id
            or str(payload.get("connection_id") or "") != connection_id
        ):
            raise TerminalError(
                "terminal_connection_binding_mismatch",
                "Terminal command belongs to another connection.",
                status=403,
            )
        kind = str(payload.get("kind") or "")
        if kind == "ack":
            selected = int(payload.get("cursor") or 0)
            if selected < acknowledged or selected > cursor:
                raise TerminalError(
                    "terminal_ack_cursor_invalid",
                    "Terminal acknowledgement cursor is invalid.",
                    status=409,
                )
            return selected
        if kind == "ping":
            nonce = str(payload.get("nonce") or "")
            if not nonce or len(nonce) > 255:
                raise TerminalError(
                    "terminal_ping_nonce_invalid",
                    "Terminal ping nonce is invalid.",
                    status=400,
                )
            peer.send_json(
                {
                    "kind": "pong",
                    "protocol": "zyra.terminal.v1",
                    "binding": session.binding.to_json(),
                    "nonce": nonce,
                    "server_time": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ",
                        time.gmtime(),
                    ),
                }
            )
            return acknowledged
        sequence = int(payload.get("sequence") or 0)
        request = TerminalControlRequest(
            task_id=session.binding.task_id,
            run_id=session.binding.run_id,
            terminal_id=session.binding.terminal_id,
            session_id=session.binding.session_id,
            worker_id=session.binding.worker_id,
            tool_call_id=session.binding.tool_call_id,
            span_id=session.binding.span_id,
            actor_id=str(payload.get("actor_id") or "zyra-web-terminal"),
            action=kind,
            sequence=sequence,
            sealed=False,
            competition_mode="interactive",
            data=str(payload.get("data") or ""),
            rows=int(payload.get("rows") or 0),
            cols=int(payload.get("cols") or 0),
            reason=str(payload.get("reason") or ""),
            permission_permit_id=str(
                payload.get("permission_permit_id") or ""
            ),
        )
        self.registry.control(request)
        return acknowledged

    @staticmethod
    def _validate_upgrade(handler: BaseHTTPRequestHandler) -> None:
        if str(handler.headers.get("Upgrade") or "").casefold() != "websocket":
            raise TerminalError(
                "terminal_websocket_upgrade_required",
                "Terminal connection requires a WebSocket upgrade.",
                status=426,
            )
        connection = {
            item.strip().casefold()
            for item in str(handler.headers.get("Connection") or "").split(",")
        }
        if "upgrade" not in connection:
            raise TerminalError(
                "terminal_websocket_connection_invalid",
                "WebSocket Connection header is invalid.",
                status=400,
            )
        if str(handler.headers.get("Sec-WebSocket-Version") or "") != "13":
            raise TerminalError(
                "terminal_websocket_version_invalid",
                "WebSocket version 13 is required.",
                status=426,
            )
        requested_protocols = {
            item.strip()
            for item in str(
                handler.headers.get("Sec-WebSocket-Protocol") or ""
            ).split(",")
            if item.strip()
        }
        if TERMINAL_SUBPROTOCOL not in requested_protocols:
            raise TerminalError(
                "terminal_websocket_subprotocol_required",
                "Terminal WebSocket requires the zyra-terminal-v1 subprotocol.",
                status=426,
            )
        key = str(handler.headers.get("Sec-WebSocket-Key") or "")
        try:
            decoded = base64.b64decode(key, validate=True)
        except (ValueError, base64.binascii.Error) as error:
            raise TerminalError(
                "terminal_websocket_key_invalid",
                "WebSocket key is invalid.",
                status=400,
            ) from error
        if len(decoded) != 16:
            raise TerminalError(
                "terminal_websocket_key_invalid",
                "WebSocket key must decode to 16 bytes.",
                status=400,
            )
        protocols = {
            item.strip()
            for item in str(
                handler.headers.get("Sec-WebSocket-Protocol") or ""
            ).split(",")
        }
        if TERMINAL_SUBPROTOCOL not in protocols:
            raise TerminalError(
                "terminal_websocket_subprotocol_invalid",
                "Terminal WebSocket subprotocol is required.",
                status=426,
            )
