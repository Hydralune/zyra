from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from tests.experiment_support import experiment_request


ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def experiment_api(tmp_path: Path) -> Iterator[str]:
    environment = {
        "ZYRA_SQLITE_PATH": str(tmp_path / "api.sqlite3"),
        "ZYRA_EVENT_LOG": str(tmp_path / "events.jsonl"),
        "ZYRA_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        "ZYRA_WORKER_POOL_STORE": str(tmp_path / "worker-pool.sqlite3"),
        "ZYRA_GRAPH_STATE_STORE": str(tmp_path / "graph.sqlite3"),
        "ZYRA_WORKSPACE_STATE_ROOT": str(tmp_path / "workspace-state"),
        "ZYRA_WORKSPACE_DATA_ROOT": str(tmp_path / "workspace-data"),
        "ZYRA_CONTROL_STATE": str(tmp_path / "control"),
        "ZYRA_SUBAGENT_STATE": str(tmp_path / "subagents"),
    }
    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    package_paths = [
        ROOT,
        ROOT / "apps" / "api",
        ROOT / "packages" / "core",
        ROOT / "packages" / "commands",
        ROOT / "packages" / "orchestration",
        ROOT / "packages" / "memory",
        ROOT / "packages" / "runtime",
        ROOT / "packages" / "integrations",
        ROOT / "packages" / "workers",
        ROOT / "packages" / "symbolic",
        ROOT / "packages" / "scheduler",
        ROOT / "packages" / "evaluation",
        ROOT / "packages" / "workspace",
        ROOT / "packages" / "code_index",
    ]
    for path in package_paths:
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from apps.api.zyra_api import main as api_main

    api_main = importlib.reload(api_main)
    api_main.reset_experiment_api(wait=True)
    api_main.reset_runtime_event_spine_bridge()
    server = ThreadingHTTPServer(("127.0.0.1", 0), api_main.ZyraRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=30)
        api_main.reset_experiment_api(wait=True)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def request(
    base: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    payload = (
        json.dumps(body, ensure_ascii=False).encode("utf-8")
        if body is not None
        else None
    )
    selected = urllib.request.Request(
        base + path,
        method=method,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Zyra-Api-Version": "1.0",
            "X-Zyra-Client": "experiment-integration-test",
            "X-Zyra-Client-Version": "0.1.0",
            "X-Request-Id": f"request_{uuid4().hex}",
            "Idempotency-Key": f"experiment-test-{uuid4().hex}",
        },
    )
    try:
        with urllib.request.urlopen(selected, timeout=240) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_experiment_api_backend_custody_report_navigation_and_bundle(
    tmp_path: Path,
) -> None:
    with experiment_api(tmp_path) as base:
        registry_status, registry = request(base, "GET", "/experiments/registry")
        assert registry_status == 200, registry
        assert len(registry["variants"]) == 7
        assert len(registry["metrics"]) == 32
        assert sum(item["score"] for item in registry["requirements"]) == 100
        assert registry["capabilities"]["browser_connection_required"] is False
        assert registry["capabilities"]["authenticated_provider_cli_allowed"] is False

        create_status, created = request(
            base,
            "POST",
            "/experiments/runs",
            experiment_request(title="HTTP main-path experiment"),
        )
        assert create_status == 201, created
        experiment_id = created["run"]["experiment_id"]
        assert created["run"]["phase"] == "admitted"
        assert created["run"]["progress"] == {
            "planned": 21,
            "succeeded": 0,
            "failed": 0,
            "terminal": 0,
        }

        start_status, started = request(
            base,
            "POST",
            f"/experiments/runs/{experiment_id}/start",
            {"wait": False},
        )
        assert start_status == 202, started
        assert started["backend_continues_after_disconnect"] is True

        deadline = time.monotonic() + 180
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            get_status, status = request(
                base,
                "GET",
                f"/experiments/runs/{experiment_id}",
            )
            assert get_status == 200, status
            if status["run"]["terminal"]:
                break
            time.sleep(0.25)
        assert status["run"]["phase"] == "succeeded", status["run"].get("failure")
        assert status["browser_connection_required"] is False
        assert status["sample_count"] == 672
        assert status["run"]["progress"]["succeeded"] == 21

        resources: dict[str, dict[str, Any]] = {}
        for resource in (
            "report",
            "samples?limit=1000",
            "bundle",
            "source",
            "requirements",
        ):
            resource_status, payload = request(
                base,
                "GET",
                f"/experiments/runs/{experiment_id}/{resource}",
            )
            assert resource_status == 200, payload
            resources[resource.split("?")[0]] = payload
        report = resources["report"]["report"]
        assert report["requirements"]["score_verified"] == 100
        assert report["statistics"]["raw_sample_count"] == 672
        assert report["reviewer_navigation"]["backend_log_required"] is False
        assert resources["samples"]["samples"]
        assert resources["bundle"]["bundle"]["verification"]["valid"] is True
        assert resources["source"]["source_admission_receipts"]
        assert (
            resources["requirements"]["requirements"]["score_verified"]
            == 100
        )

        verify_status, verified = request(
            base,
            "POST",
            f"/experiments/runs/{experiment_id}/verify",
            {},
        )
        assert verify_status == 200, verified
        assert verified["verification_receipt"]["valid"] is True
