from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from apps.api.zyra_api import main as api


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    body = None
    headers: dict[str, str] = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["content-type"] = "application/json"
    request = Request(
        base_url + path,
        data=body,
        method=method,
        headers=headers,
    )
    try:
        with urlopen(request, timeout=15) as response:  # noqa: S310 - local test server.
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_task_owner_exception_returns_structured_503_and_server_stays_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_composition() -> object:
        raise RuntimeError("stale physical worker registration has live leases")

    monkeypatch.setattr(api, "graph_execution_context", fail_composition)
    server = ThreadingHTTPServer(("127.0.0.1", 0), api.ZyraRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, failure = _request(
            base_url,
            "/tasks",
            method="POST",
            payload={"goal": "Exercise structured failure handling.", "auto_run": True},
        )
        assert status == 503
        assert failure["error"] == "task_execution_failed"
        assert failure["retryable"] is False
        assert failure["fallback"] is False
        assert failure["task"]["status"] == "blocked"
        assert failure["events"][-1]["payload"]["schema"] == (
            "zyra.task-execution-error/v1"
        )

        readiness_status, readiness = _request(base_url, "/runtime/readiness")
        assert readiness_status == 200
        assert readiness["ready"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
