from __future__ import annotations

import base64
import hashlib
import json
import queue
import socket
import struct
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[2]
for package in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "workers",
):
    if str(package) not in sys.path:
        sys.path.insert(0, str(package))

from zyra_workers.terminal import (  # noqa: E402
    PtyProcess,
    PtySpawnOptions,
    TerminalBinding,
    TerminalCreateRequest,
    TerminalError,
    TerminalPermission,
    TerminalSessionRegistry,
    TerminalSpill,
    TerminalStateStore,
    TerminalTicketAuthority,
    TerminalWebSocketHandler,
)


ORIGIN = "https://console.example.test"
PROTOCOL = "zyra.terminal.v1"
SUBPROTOCOL = "zyra-terminal-v1"


class SocketFakePty(PtyProcess):
    def __init__(self, options: PtySpawnOptions) -> None:
        self.options = options
        self._pid = 51_001
        self._exit_code: int | None = None
        self._output: queue.Queue[bytes] = queue.Queue()
        self.writes: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self.terminated = 0
        self._output.put(b"ready from websocket\r\n")

    @property
    def pid(self) -> int:
        return self._pid

    def read(self, maximum: int = 65_536) -> bytes:
        del maximum
        return self._output.get(timeout=5)

    def write(self, data: bytes) -> int:
        material = bytes(data)
        self.writes.append(material)
        self._output.put(b"observed:" + material)
        return len(material)

    def resize(self, rows: int, cols: int) -> None:
        self.resizes.append((rows, cols))

    def poll(self) -> int | None:
        return self._exit_code

    def wait(self, timeout: float | None = None) -> int:
        deadline = time.monotonic() + (timeout if timeout is not None else 5)
        while self._exit_code is None:
            if time.monotonic() >= deadline:
                raise TimeoutError("fake terminal remains active")
            time.sleep(0.005)
        return self._exit_code

    def terminate_tree(self, grace_seconds: float = 1.0) -> None:
        del grace_seconds
        self.terminated += 1
        if self._exit_code is None:
            self._exit_code = 137
            self._output.put(b"")

    def close(self) -> None:
        if self._exit_code is None:
            self._exit_code = 0
            self._output.put(b"")


class WebSocketHarness:
    def __init__(self, tmp_path: Path) -> None:
        self.processes: list[SocketFakePty] = []
        self.events: list[dict[str, Any]] = []

        def workspace(
            request: TerminalCreateRequest,
        ) -> tuple[str, int, Path]:
            assert request.task_id == "task-websocket"
            return "workspace-websocket", 3, tmp_path

        def permission(action: str, request: object) -> TerminalPermission:
            del request
            return TerminalPermission(
                effect="allow",
                decision_id=f"decision-{action}",
                reason_code=f"terminal.{action}.allow",
                reason=f"{action} allowed by integration harness",
            )

        def event(payload: Mapping[str, Any]) -> str:
            self.events.append(dict(payload))
            return f"event-{len(self.events)}"

        def spill_factory(binding: TerminalBinding):
            def spill(
                data: bytes,
                first_cursor: int,
                next_cursor: int,
                binary: bool,
                redacted: bool,
            ) -> TerminalSpill:
                digest = hashlib.sha256(data).hexdigest()
                return TerminalSpill(
                    artifact_id=f"artifact-{digest[:16]}",
                    revision=f"sha256:{digest}",
                    media_type=(
                        "application/octet-stream"
                        if binary
                        else "text/plain"
                    ),
                    sha256=digest,
                    byte_length=next_cursor - first_cursor,
                    first_cursor=first_cursor,
                    next_cursor=next_cursor,
                    binary=binary,
                    redacted=redacted,
                )

            assert binding.task_id == "task-websocket"
            return spill

        def spawn(options: PtySpawnOptions) -> PtyProcess:
            process = SocketFakePty(options)
            self.processes.append(process)
            return process

        self.registry = TerminalSessionRegistry(
            state_store=TerminalStateStore(tmp_path / "terminal-state.json"),
            workspace_resolver=workspace,
            permission_port=permission,
            event_sink=event,
            spill_sink_factory=spill_factory,
            ticket_authority=TerminalTicketAuthority(
                b"websocket-integration-secret-key",
                ttl_seconds=20,
            ),
            pty_spawner=spawn,
            maximum_sessions=4,
        )
        projection = self.registry.create(
            TerminalCreateRequest(
                task_id="task-websocket",
                run_id="run-websocket",
                session_id="session-websocket",
                worker_id="worker-websocket",
                command_id="command-websocket",
                tool_call_id="tool-websocket",
                span_id="span-websocket",
                actor_id="operator-websocket",
                command="python -i",
                title="WebSocket terminal",
                cwd=".",
                shell="",
                rows=24,
                cols=80,
                sealed=False,
                competition_mode="interactive",
                environment={},
                correlation_id="correlation-websocket",
                causation_id="cause-websocket",
            )
        )
        self.binding = projection.binding

    @property
    def process(self) -> SocketFakePty:
        return self.processes[0]

    def issue(self, *, origin: str = ORIGIN, cursor: int = 0) -> str:
        projection = self.registry.ticket(
            task_id=self.binding.task_id,
            terminal_id=self.binding.terminal_id,
            run_id=self.binding.run_id,
            session_id=self.binding.session_id,
            origin=origin,
            cursor=cursor,
            protocol=PROTOCOL,
        )
        assert projection.ticket
        return projection.ticket

    def wait_until(self, predicate, *, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError("condition did not become true")
            time.sleep(0.01)


class TerminalHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def handler_type(registry: TerminalSessionRegistry):
    websocket = TerminalWebSocketHandler(
        registry,
        heartbeat_seconds=0.2,
        maximum_unacked_bytes=64 * 1_024,
    )

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            parts = [part for part in parsed.path.split("/") if part]
            query = parse_qs(parsed.query)
            if (
                len(parts) != 5
                or parts[0] != "tasks"
                or parts[2] != "terminals"
                or parts[4] != "connect"
            ):
                self.send_error(404)
                return
            try:
                websocket.upgrade(
                    self,
                    task_id=parts[1],
                    terminal_id=parts[3],
                    ticket=str(query.get("ticket", [""])[0]),
                    cursor=int(query.get("cursor", ["0"])[0]),
                    protocol=str(query.get("protocol", [""])[0]),
                )
            except (TypeError, ValueError):
                self.send_error(400)
            except TerminalError as error:
                self.send_error(error.status, error.code)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    return Handler


@pytest.fixture
def websocket_server(
    tmp_path: Path,
) -> Iterator[tuple[WebSocketHarness, tuple[str, int]]]:
    harness = WebSocketHarness(tmp_path)
    server = TerminalHttpServer(
        ("127.0.0.1", 0),
        handler_type(harness.registry),
    )
    thread = threading.Thread(
        target=server.serve_forever,
        name="terminal-websocket-integration",
        daemon=True,
    )
    thread.start()
    try:
        yield harness, server.server_address
    finally:
        server.shutdown()
        server.server_close()
        harness.registry.shutdown()
        thread.join(timeout=2)


class WebSocketClient:
    def __init__(
        self,
        address: tuple[str, int],
        *,
        path: str,
        origin: str,
    ) -> None:
        self.socket = socket.create_connection(address, timeout=2)
        self.socket.settimeout(2)
        self.buffer = bytearray()
        key = base64.b64encode(b"terminal-testkey").decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {address[0]}:{address[1]}\r\n"
            f"Origin: {origin}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Protocol: {SUBPROTOCOL}\r\n"
            "\r\n"
        )
        self.socket.sendall(request.encode("ascii"))
        header = self._read_header()
        first_line = header.split(b"\r\n", 1)[0].decode("ascii")
        self.status = int(first_line.split(" ", 2)[1])
        self.header = header.decode("iso-8859-1")

    def _read_header(self) -> bytes:
        marker = b"\r\n\r\n"
        while marker not in self.buffer:
            chunk = self.socket.recv(65_536)
            if not chunk:
                raise AssertionError("HTTP response ended before headers")
            self.buffer.extend(chunk)
        index = self.buffer.index(marker) + len(marker)
        header = bytes(self.buffer[:index])
        del self.buffer[:index]
        return header

    def _read_exact(self, length: int) -> bytes:
        while len(self.buffer) < length:
            chunk = self.socket.recv(max(1, length - len(self.buffer)))
            if not chunk:
                raise EOFError("WebSocket connection closed")
            self.buffer.extend(chunk)
        value = bytes(self.buffer[:length])
        del self.buffer[:length]
        return value

    def frame(self) -> tuple[int, bytes]:
        first = self._read_exact(2)
        opcode = first[0] & 0x0F
        length = first[1] & 0x7F
        assert not first[1] & 0x80
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        return opcode, self._read_exact(length)

    def json(self) -> dict[str, Any]:
        while True:
            opcode, data = self.frame()
            if opcode == 0x1:
                value = json.loads(data.decode("utf-8"))
                assert isinstance(value, dict)
                return value
            if opcode == 0x9:
                self.send_frame(0xA, data)
                continue
            if opcode == 0x8:
                raise EOFError("server closed before expected JSON frame")

    def collect(self, kinds: set[str], *, maximum: int = 20) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for _ in range(maximum):
            value = self.json()
            kind = str(value.get("kind") or "")
            found[kind] = value
            if kinds.issubset(found):
                return found
        raise AssertionError(f"missing WebSocket frame kinds: {kinds - found.keys()}")

    def send_json(self, value: Mapping[str, Any]) -> None:
        self.send_frame(
            0x1,
            json.dumps(value, separators=(",", ":")).encode("utf-8"),
        )

    def send_frame(self, opcode: int, data: bytes = b"") -> None:
        mask = b"\x13\x37\x42\x99"
        length = len(data)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked = bytes(
            value ^ mask[index % len(mask)]
            for index, value in enumerate(data)
        )
        self.socket.sendall(bytes(header) + masked)

    def close(self) -> None:
        try:
            self.send_frame(0x8, struct.pack("!H", 1000))
            self.socket.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        finally:
            self.socket.close()


def socket_path(harness: WebSocketHarness, ticket: str, cursor: int) -> str:
    return (
        f"/tasks/{harness.binding.task_id}/terminals/"
        f"{harness.binding.terminal_id}/connect"
        f"?ticket={ticket}&cursor={cursor}&protocol={PROTOCOL}"
    )


def command(
    harness: WebSocketHarness,
    connection_id: str,
    kind: str,
    **values: Any,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "protocol": PROTOCOL,
        "terminal_id": harness.binding.terminal_id,
        "connection_id": connection_id,
        **values,
    }


def test_websocket_upgrade_replay_input_resize_ping_and_detach(
    websocket_server: tuple[WebSocketHarness, tuple[str, int]],
) -> None:
    harness, address = websocket_server
    ticket = harness.issue()
    client = WebSocketClient(
        address,
        path=socket_path(harness, ticket, 0),
        origin=ORIGIN,
    )
    assert client.status == 101
    assert f"Sec-WebSocket-Protocol: {SUBPROTOCOL}" in client.header

    frames = client.collect({"hello", "output", "status"})
    hello = frames["hello"]
    output = frames["output"]
    connection_id = str(hello["connection_id"])
    assert hello["binding"]["terminal_id"] == harness.binding.terminal_id
    assert str(output["text"]).startswith("ready")

    client.send_json(command(
        harness,
        connection_id,
        "ack",
        cursor=output["next_cursor"],
    ))
    client.send_json(command(
        harness,
        connection_id,
        "input",
        sequence=1,
        data="echo websocket\n",
        actor_id="operator-websocket",
    ))
    harness.wait_until(lambda: harness.process.writes == [b"echo websocket\n"])
    client.send_json(command(
        harness,
        connection_id,
        "resize",
        sequence=1,
        rows=33,
        cols=101,
    ))
    harness.wait_until(lambda: harness.process.resizes == [(33, 101)])
    client.send_json(command(
        harness,
        connection_id,
        "ping",
        nonce="roundtrip-one",
    ))
    pong = client.collect({"pong"})["pong"]
    assert pong["nonce"] == "roundtrip-one"

    client.close()
    session = harness.registry.get(
        harness.binding.task_id,
        harness.binding.terminal_id,
    )
    harness.wait_until(lambda: session.status.viewers == 0)
    assert session.status.phase.value == "running"
    assert harness.process.terminated == 0

    replay = WebSocketClient(
        address,
        path=socket_path(harness, ticket, 0),
        origin=ORIGIN,
    )
    assert replay.status == 403
    replay.socket.close()


def test_origin_rejection_does_not_burn_ticket_and_future_cursor_resyncs(
    websocket_server: tuple[WebSocketHarness, tuple[str, int]],
) -> None:
    harness, address = websocket_server
    ticket = harness.issue()
    rejected = WebSocketClient(
        address,
        path=socket_path(harness, ticket, 0),
        origin="https://evil.example.test",
    )
    assert rejected.status == 403
    rejected.socket.close()

    accepted = WebSocketClient(
        address,
        path=socket_path(harness, ticket, 0),
        origin=ORIGIN,
    )
    assert accepted.status == 101
    assert accepted.collect({"hello"})["hello"]["accepted_cursor"] == 0
    accepted.close()

    future_ticket = harness.issue(cursor=999)
    future = WebSocketClient(
        address,
        path=socket_path(harness, future_ticket, 999),
        origin=ORIGIN,
    )
    assert future.status == 101
    frames = future.collect({"hello", "resync"})
    assert frames["resync"]["requested_cursor"] == 999
    assert frames["resync"]["reason"] == "future_cursor"
    assert frames["resync"]["accepted_cursor"] == frames["hello"]["accepted_cursor"]
    future.close()
