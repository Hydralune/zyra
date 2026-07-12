from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any


class WebSocketError(RuntimeError):
    pass


class WebSocketHandshakeError(WebSocketError):
    pass


class WebSocketProtocolError(WebSocketError):
    pass


class WebSocketClosed(WebSocketError):
    pass


class WebSocketTimeout(WebSocketError):
    pass


class WebSocketState(StrEnum):
    NEW = "new"
    CONNECTING = "connecting"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


class Opcode(IntEnum):
    CONTINUATION = 0x0
    TEXT = 0x1
    BINARY = 0x2
    CLOSE = 0x8
    PING = 0x9
    PONG = 0xA


@dataclass(frozen=True, slots=True)
class WebSocketConfig:
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    max_message_bytes: int = 32 * 1024 * 1024
    verify_tls: bool = True
    proxy_url: str = ""
    origin: str = ""
    subprotocols: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlparse(self.url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError("websocket URL must be absolute ws(s)")
        if self.connect_timeout <= 0 or self.read_timeout <= 0:
            raise ValueError("websocket timeouts must be positive")
        if self.max_message_bytes < 1024:
            raise ValueError("websocket max message bytes is too small")
        object.__setattr__(self, "headers", {str(k): str(v) for k, v in self.headers.items()})
        object.__setattr__(self, "subprotocols", tuple(str(item) for item in self.subprotocols if str(item)))


@dataclass(frozen=True, slots=True)
class WebSocketSnapshot:
    state: WebSocketState
    connected_at: float | None
    closed_at: float | None
    messages_sent: int
    messages_received: int
    bytes_sent: int
    bytes_received: int
    pings_received: int
    pongs_received: int
    close_code: int | None
    close_reason: str
    error: str


@dataclass(frozen=True, slots=True)
class _Frame:
    fin: bool
    opcode: Opcode
    payload: bytes


class WebSocketClient:
    """Small RFC6455 client suitable for Chrome DevTools Protocol.

    It intentionally implements only client functionality, but validates
    server masking, control frame bounds, fragmentation, UTF-8 and close
    semantics.  No third-party websocket runtime is required.
    """

    def __init__(self, config: WebSocketConfig) -> None:
        self.config = config
        self._socket: socket.socket | ssl.SSLSocket | None = None
        self._state = WebSocketState.NEW
        self._connected_at: float | None = None
        self._closed_at: float | None = None
        self._messages_sent = 0
        self._messages_received = 0
        self._bytes_sent = 0
        self._bytes_received = 0
        self._pings_received = 0
        self._pongs_received = 0
        self._close_code: int | None = None
        self._close_reason = ""
        self._error = ""
        self._send_lock = threading.Lock()
        self._receive_lock = threading.Lock()
        self._state_lock = threading.RLock()

    @property
    def state(self) -> WebSocketState:
        with self._state_lock:
            return self._state

    def connect(self) -> None:
        with self._state_lock:
            if self._state == WebSocketState.OPEN:
                return
            if self._state in {WebSocketState.CONNECTING, WebSocketState.CLOSING}:
                raise WebSocketError(f"websocket is {self._state}")
            self._state = WebSocketState.CONNECTING
            self._error = ""
        try:
            raw_socket, request_target = self._open_transport()
            self._perform_handshake(raw_socket, request_target)
            raw_socket.settimeout(self.config.read_timeout)
        except Exception as error:
            with self._state_lock:
                self._state = WebSocketState.FAILED
                self._error = type(error).__name__
                self._closed_at = time.monotonic()
            try:
                raw_socket.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            raise
        with self._state_lock:
            self._socket = raw_socket
            self._state = WebSocketState.OPEN
            self._connected_at = time.monotonic()
            self._closed_at = None

    def send_text(self, text: str) -> None:
        encoded = text.encode("utf-8")
        self._send_data(Opcode.TEXT, encoded)

    def send_binary(self, payload: bytes | bytearray | memoryview) -> None:
        self._send_data(Opcode.BINARY, bytes(payload))

    def send_json(self, payload: Mapping[str, Any]) -> None:
        self.send_text(json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")))

    def receive_text(self, *, timeout: float | None = None) -> str:
        opcode, payload = self.receive_message(timeout=timeout)
        if opcode != Opcode.TEXT:
            raise WebSocketProtocolError("expected text websocket message")
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as error:
            self._fail_protocol(1007, "invalid UTF-8")
            raise WebSocketProtocolError("websocket text message is not UTF-8") from error

    def receive_json(self, *, timeout: float | None = None) -> Any:
        text = self.receive_text(timeout=timeout)
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise WebSocketProtocolError("websocket message is not JSON") from error

    def receive_message(self, *, timeout: float | None = None) -> tuple[Opcode, bytes]:
        with self._receive_lock:
            sock = self._require_open_socket()
            previous_timeout = sock.gettimeout()
            if timeout is not None:
                if timeout <= 0:
                    raise ValueError("receive timeout must be positive")
                sock.settimeout(timeout)
            fragments = bytearray()
            message_opcode: Opcode | None = None
            try:
                while True:
                    frame = self._read_frame(sock)
                    if frame.opcode == Opcode.PING:
                        self._pings_received += 1
                        self._send_frame(Opcode.PONG, frame.payload)
                        continue
                    if frame.opcode == Opcode.PONG:
                        self._pongs_received += 1
                        continue
                    if frame.opcode == Opcode.CLOSE:
                        self._accept_close(frame.payload)
                        raise WebSocketClosed(self._close_reason or "remote websocket closed")
                    if frame.opcode in {Opcode.TEXT, Opcode.BINARY}:
                        if message_opcode is not None:
                            self._fail_protocol(1002, "unexpected data frame during fragmented message")
                            raise WebSocketProtocolError("unexpected data frame")
                        message_opcode = frame.opcode
                    elif frame.opcode == Opcode.CONTINUATION:
                        if message_opcode is None:
                            self._fail_protocol(1002, "unexpected continuation frame")
                            raise WebSocketProtocolError("unexpected continuation frame")
                    else:
                        self._fail_protocol(1002, "unsupported opcode")
                        raise WebSocketProtocolError("unsupported websocket opcode")
                    fragments.extend(frame.payload)
                    if len(fragments) > self.config.max_message_bytes:
                        self._fail_protocol(1009, "message too large")
                        raise WebSocketProtocolError("websocket message exceeds configured limit")
                    if frame.fin:
                        assert message_opcode is not None
                        self._messages_received += 1
                        return message_opcode, bytes(fragments)
            except socket.timeout as error:
                raise WebSocketTimeout("websocket receive timed out") from error
            except OSError as error:
                self._mark_failed(error)
                raise WebSocketClosed("websocket transport dropped") from error
            finally:
                if timeout is not None:
                    try:
                        sock.settimeout(previous_timeout)
                    except OSError:
                        pass

    def ping(self, payload: bytes = b"") -> None:
        if len(payload) > 125:
            raise ValueError("ping payload exceeds control frame limit")
        self._send_frame(Opcode.PING, payload)

    def close(self, *, code: int = 1000, reason: str = "", timeout: float = 2.0) -> None:
        encoded_reason = reason.encode("utf-8")
        if len(encoded_reason) > 123:
            encoded_reason = encoded_reason[:123]
            while True:
                try:
                    encoded_reason.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    encoded_reason = encoded_reason[:-1]
        with self._state_lock:
            if self._state in {WebSocketState.NEW, WebSocketState.CLOSED}:
                self._state = WebSocketState.CLOSED
                self._closed_at = time.monotonic()
                return
            if self._state == WebSocketState.OPEN:
                self._state = WebSocketState.CLOSING
        try:
            if self._socket is not None:
                self._send_frame(Opcode.CLOSE, struct.pack("!H", code) + encoded_reason, allow_closing=True)
                self._socket.settimeout(max(timeout, 0.01))
                try:
                    frame = self._read_frame(self._socket)
                    if frame.opcode == Opcode.CLOSE:
                        self._parse_close(frame.payload)
                except (WebSocketError, OSError, socket.timeout):
                    pass
        finally:
            self._close_transport(code=code, reason=reason)

    def snapshot(self) -> WebSocketSnapshot:
        with self._state_lock:
            return WebSocketSnapshot(
                state=self._state,
                connected_at=self._connected_at,
                closed_at=self._closed_at,
                messages_sent=self._messages_sent,
                messages_received=self._messages_received,
                bytes_sent=self._bytes_sent,
                bytes_received=self._bytes_received,
                pings_received=self._pings_received,
                pongs_received=self._pongs_received,
                close_code=self._close_code,
                close_reason=self._close_reason,
                error=self._error,
            )

    def _open_transport(self) -> tuple[socket.socket | ssl.SSLSocket, str]:
        parsed = urllib.parse.urlparse(self.config.url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        request_target = urllib.parse.urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
        connect_host, connect_port = host, port
        proxy = urllib.parse.urlparse(self.config.proxy_url) if self.config.proxy_url else None
        if proxy:
            if proxy.scheme not in {"http", "https"} or not proxy.hostname:
                raise WebSocketHandshakeError("unsupported websocket proxy URL")
            connect_host = proxy.hostname
            connect_port = proxy.port or (443 if proxy.scheme == "https" else 80)
        raw = socket.create_connection((connect_host, connect_port), timeout=self.config.connect_timeout)
        raw.settimeout(self.config.connect_timeout)
        if proxy and proxy.scheme == "https":
            raw = self._wrap_tls(raw, proxy.hostname or "")
        if proxy:
            self._proxy_connect(raw, host, port, proxy)
        if parsed.scheme == "wss":
            raw = self._wrap_tls(raw, host)
        return raw, request_target

    def _wrap_tls(self, sock: socket.socket, server_hostname: str) -> ssl.SSLSocket:
        context = ssl.create_default_context()
        if not self.config.verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context.wrap_socket(sock, server_hostname=server_hostname)

    def _proxy_connect(self, sock: socket.socket, host: str, port: int, proxy: urllib.parse.ParseResult) -> None:
        headers = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
        if proxy.username is not None:
            credentials = f"{urllib.parse.unquote(proxy.username)}:{urllib.parse.unquote(proxy.password or '')}"
            headers.append(f"Proxy-Authorization: Basic {base64.b64encode(credentials.encode()).decode()}")
        sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
        response = self._read_http_headers(sock)
        status = response.split("\r\n", 1)[0]
        if " 200 " not in f" {status} ":
            raise WebSocketHandshakeError(f"websocket proxy CONNECT failed: {status}")

    def _perform_handshake(self, sock: socket.socket, request_target: str) -> None:
        parsed = urllib.parse.urlparse(self.config.url)
        host = parsed.hostname or ""
        port = parsed.port
        default_port = 443 if parsed.scheme == "wss" else 80
        host_header = host if port in {None, default_port} else f"{host}:{port}"
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = [
            f"GET {request_target} HTTP/1.1",
            f"Host: {host_header}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if self.config.origin:
            lines.append(f"Origin: {self.config.origin}")
        if self.config.subprotocols:
            lines.append(f"Sec-WebSocket-Protocol: {', '.join(self.config.subprotocols)}")
        reserved = {"host", "upgrade", "connection", "sec-websocket-key", "sec-websocket-version"}
        for name, value in self.config.headers.items():
            if name.casefold() not in reserved:
                lines.append(f"{name}: {value}")
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
        response = self._read_http_headers(sock)
        rows = response.split("\r\n")
        if not rows or " 101 " not in f" {rows[0]} ":
            raise WebSocketHandshakeError(f"websocket upgrade failed: {rows[0] if rows else 'empty response'}")
        headers: dict[str, str] = {}
        for row in rows[1:]:
            if ":" in row:
                name, value = row.split(":", 1)
                headers[name.strip().casefold()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise WebSocketHandshakeError("websocket accept digest mismatch")
        if "websocket" not in headers.get("upgrade", "").casefold():
            raise WebSocketHandshakeError("websocket upgrade header missing")

    def _read_http_headers(self, sock: socket.socket) -> str:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            if len(data) > 64 * 1024:
                raise WebSocketHandshakeError("HTTP handshake headers exceed limit")
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketHandshakeError("connection closed during HTTP handshake")
            data.extend(chunk)
        header, _separator, remainder = bytes(data).partition(b"\r\n\r\n")
        if remainder:
            raise WebSocketHandshakeError("unexpected bytes after HTTP handshake")
        return header.decode("iso-8859-1")

    def _send_data(self, opcode: Opcode, payload: bytes) -> None:
        if len(payload) > self.config.max_message_bytes:
            raise ValueError("websocket message exceeds configured limit")
        self._send_frame(opcode, payload)
        self._messages_sent += 1

    def _send_frame(self, opcode: Opcode, payload: bytes, *, allow_closing: bool = False) -> None:
        if opcode.value >= 0x8 and len(payload) > 125:
            raise ValueError("control frame payload exceeds 125 bytes")
        with self._send_lock:
            sock = self._socket
            state = self.state
            if sock is None or (state != WebSocketState.OPEN and not (allow_closing and state == WebSocketState.CLOSING)):
                raise WebSocketClosed("websocket is not open")
            mask = os.urandom(4)
            length = len(payload)
            header = bytearray([0x80 | int(opcode)])
            if length < 126:
                header.append(0x80 | length)
            elif length <= 0xFFFF:
                header.append(0x80 | 126)
                header.extend(struct.pack("!H", length))
            else:
                header.append(0x80 | 127)
                header.extend(struct.pack("!Q", length))
            masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            packet = bytes(header) + mask + masked
            try:
                sock.sendall(packet)
            except socket.timeout as error:
                raise WebSocketTimeout("websocket send timed out") from error
            except OSError as error:
                self._mark_failed(error)
                raise WebSocketClosed("websocket transport dropped during send") from error
            self._bytes_sent += len(payload)

    def _read_frame(self, sock: socket.socket | ssl.SSLSocket) -> _Frame:
        first = self._read_exact(sock, 2)
        byte1, byte2 = first
        fin = bool(byte1 & 0x80)
        rsv = byte1 & 0x70
        if rsv:
            raise WebSocketProtocolError("websocket extension bits are unsupported")
        try:
            opcode = Opcode(byte1 & 0x0F)
        except ValueError as error:
            raise WebSocketProtocolError("unknown websocket opcode") from error
        masked = bool(byte2 & 0x80)
        if masked:
            raise WebSocketProtocolError("server websocket frames must not be masked")
        length = byte2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(sock, 2))[0]
        elif length == 127:
            encoded = self._read_exact(sock, 8)
            if encoded[0] & 0x80:
                raise WebSocketProtocolError("invalid 64-bit websocket length")
            length = struct.unpack("!Q", encoded)[0]
        if opcode.value >= 0x8 and (not fin or length > 125):
            raise WebSocketProtocolError("invalid websocket control frame")
        if length > self.config.max_message_bytes:
            raise WebSocketProtocolError("websocket frame exceeds configured limit")
        payload = self._read_exact(sock, length)
        self._bytes_received += len(payload)
        return _Frame(fin=fin, opcode=opcode, payload=payload)

    @staticmethod
    def _read_exact(sock: socket.socket | ssl.SSLSocket, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise WebSocketClosed("websocket transport reached EOF")
            data.extend(chunk)
        return bytes(data)

    def _accept_close(self, payload: bytes) -> None:
        self._parse_close(payload)
        try:
            self._send_frame(Opcode.CLOSE, payload, allow_closing=True)
        except WebSocketError:
            pass
        self._close_transport(code=self._close_code or 1000, reason=self._close_reason)

    def _parse_close(self, payload: bytes) -> None:
        if len(payload) == 1:
            raise WebSocketProtocolError("invalid websocket close payload")
        if len(payload) >= 2:
            self._close_code = struct.unpack("!H", payload[:2])[0]
            try:
                self._close_reason = payload[2:].decode("utf-8")
            except UnicodeDecodeError as error:
                raise WebSocketProtocolError("invalid close reason UTF-8") from error

    def _fail_protocol(self, code: int, reason: str) -> None:
        try:
            self.close(code=code, reason=reason, timeout=0.1)
        except WebSocketError:
            self._close_transport(code=code, reason=reason)

    def _mark_failed(self, error: BaseException) -> None:
        with self._state_lock:
            self._state = WebSocketState.FAILED
            self._error = type(error).__name__
            self._closed_at = time.monotonic()
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass

    def _close_transport(self, *, code: int, reason: str) -> None:
        sock = self._socket
        self._socket = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        with self._state_lock:
            self._state = WebSocketState.CLOSED
            self._close_code = self._close_code or code
            self._close_reason = self._close_reason or reason
            self._closed_at = time.monotonic()

    def _require_open_socket(self) -> socket.socket | ssl.SSLSocket:
        with self._state_lock:
            if self._state != WebSocketState.OPEN or self._socket is None:
                raise WebSocketClosed("websocket is not open")
            return self._socket
