from __future__ import annotations

import threading
from collections.abc import Mapping

from .websocket import (
    WebSocketClient,
    WebSocketClosed,
    WebSocketConfig,
    WebSocketState,
    WebSocketTimeout,
)


class WebSocketCdpTransport:
    """Synchronous CDP transport backed by Zyra's owned RFC6455 client.

    ``CdpRequestRuntime`` owns request correlation and its reader thread.  This
    class deliberately owns only the wire lifecycle and translates websocket
    read timeouts into the port's built-in ``TimeoutError`` contract.
    """

    def __init__(
        self,
        websocket_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        proxy_url: str = "",
        verify_tls: bool = True,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        max_message_bytes: int = 200 * 1024 * 1024,
    ) -> None:
        self.config = WebSocketConfig(
            url=websocket_url,
            headers=dict(headers or {}),
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            max_message_bytes=max_message_bytes,
            verify_tls=verify_tls,
            proxy_url=proxy_url,
        )
        self._client = WebSocketClient(self.config)
        self._lock = threading.RLock()

    def open(self) -> None:
        with self._lock:
            if self._client.state == WebSocketState.OPEN:
                return
            self._client.connect()

    def close(self) -> None:
        with self._lock:
            state = self._client.state
            if state in {WebSocketState.NEW, WebSocketState.CLOSED}:
                return
            try:
                self._client.close(reason="CDP transport closed")
            except WebSocketClosed:
                return

    def send(self, payload: str) -> None:
        if not self.alive():
            raise ConnectionError("CDP websocket transport is closed")
        self._client.send_text(payload)
        return None

    def receive(self, timeout: float | None = None) -> str:
        if not self.alive():
            raise ConnectionError("CDP websocket transport is closed")
        try:
            return self._client.receive_text(timeout=timeout)
        except WebSocketTimeout as error:
            raise TimeoutError("CDP websocket receive timed out") from error

    def alive(self) -> bool:
        return self._client.state == WebSocketState.OPEN

    def snapshot(self):
        return self._client.snapshot()
