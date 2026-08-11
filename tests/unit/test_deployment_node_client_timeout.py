from __future__ import annotations

import socket
from typing import Any

import pytest

from zyra_orchestration.deployment import node_client
from zyra_orchestration.deployment.errors import ProcessUnavailable


class _TimeoutConnection:
    observed_timeouts: list[float | None] = []

    def __init__(self, host: str, port: int, *, timeout: float | None) -> None:
        del host, port
        self.observed_timeouts.append(timeout)

    def request(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise socket.timeout("controlled timeout")

    def close(self) -> None:
        return None


def test_execution_none_is_open_while_health_keeps_short_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _TimeoutConnection.observed_timeouts.clear()
    monkeypatch.setattr(node_client, "HTTPConnection", _TimeoutConnection)
    client = node_client.DeploymentNodeClient(
        "http://127.0.0.1:8312",
        b"x" * 32,
        timeout_seconds=10.0,
    )

    with pytest.raises(ProcessUnavailable):
        client.execute({}, timeout_seconds=None)
    with pytest.raises(ProcessUnavailable):
        client.execute({}, timeout_seconds=75.0)
    with pytest.raises(ProcessUnavailable):
        client.health()

    assert _TimeoutConnection.observed_timeouts == [None, 75.0, 10.0]
