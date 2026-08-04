from __future__ import annotations

from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any, Iterator
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
BUN = ROOT / "node_modules" / ".bin" / (
    "bun.exe" if os.name == "nt" else "bun"
)
CLI = ROOT / "apps" / "cli" / "src" / "index.ts"


def _records(completed: subprocess.CompletedProcess[str]) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in completed.stdout.splitlines() if line]
    assert records
    assert records[-1]["type"] == "result"
    assert "\x1b" not in completed.stdout
    return records


def _run_cli(
    base_url: str,
    *arguments: str,
    timeout: int = 180,
    environment: dict[str, str] | None = None,
    auto_start: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [
        str(BUN),
        str(CLI),
        *arguments,
        f"--base-url={base_url}",
    ]
    if not auto_start:
        command.append("--autostart=false")
    return subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        env=environment,
    )


def _isolated_daemon_environment(tmp_path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "ZYRA_PROJECT_ROOT": str(ROOT),
            "ZYRA_CLI_STATE_DIR": str(tmp_path / "cli-state"),
            "ZYRA_SQLITE_PATH": str(tmp_path / "api.sqlite3"),
            "ZYRA_EVENT_LOG": str(tmp_path / "events.jsonl"),
            "ZYRA_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
            "ZYRA_TOOL_WORKSPACE": str(tmp_path / "tool-workspace"),
            "ZYRA_PERMISSION_STATE": str(tmp_path / "permissions.json"),
            "ZYRA_WORKER_POOL_STORE": str(tmp_path / "worker-pool.sqlite3"),
            "ZYRA_GRAPH_STATE_STORE": str(tmp_path / "graph.sqlite3"),
            "ZYRA_WORKSPACE_STATE_ROOT": str(tmp_path / "workspace-state"),
            "ZYRA_WORKSPACE_DATA_ROOT": str(tmp_path / "workspace-data"),
            "ZYRA_CONTROL_STATE": str(tmp_path / "control"),
            "ZYRA_SUBAGENT_STATE": str(tmp_path / "subagents"),
        }
    )
    return environment


def _unused_loopback_origin() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as selected:
        selected.bind(("127.0.0.1", 0))
        port = selected.getsockname()[1]
    return f"http://127.0.0.1:{port}"


@contextmanager
def _real_api(tmp_path: Path) -> Iterator[str]:
    environment = {
        "ZYRA_SQLITE_PATH": str(tmp_path / "api.sqlite3"),
        "ZYRA_EVENT_LOG": str(tmp_path / "events.jsonl"),
        "ZYRA_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        "ZYRA_TOOL_WORKSPACE": str(tmp_path / "tool-workspace"),
        "ZYRA_PERMISSION_STATE": str(tmp_path / "permissions.json"),
        "ZYRA_WORKER_POOL_STORE": str(tmp_path / "worker-pool.sqlite3"),
        "ZYRA_GRAPH_STATE_STORE": str(tmp_path / "graph.sqlite3"),
        "ZYRA_WORKSPACE_STATE_ROOT": str(tmp_path / "workspace-state"),
        "ZYRA_WORKSPACE_DATA_ROOT": str(tmp_path / "workspace-data"),
        "ZYRA_CONTROL_STATE": str(tmp_path / "control"),
        "ZYRA_SUBAGENT_STATE": str(tmp_path / "subagents"),
        "ZYRA_CLI_STATE_DIR": str(tmp_path / "cli-state"),
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
    api_main.reset_scenario_runner_api(wait=True)
    api_main.reset_runtime_event_spine_bridge()
    api_main.reset_worker_pool_api()
    server = ThreadingHTTPServer(("127.0.0.1", 0), api_main.ZyraRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        assert _get(base_url, "/health")["service"] == "zyra-api"
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=30)
        api_main.reset_scenario_runner_api(wait=True)
        api_main.reset_runtime_event_spine_bridge()
        api_main.reset_worker_pool_api()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _get(base_url: str, path: str) -> dict[str, Any]:
    request = urllib.request.Request(
        base_url + path,
        headers={
            "Accept": "application/json",
            "X-Zyra-Api-Version": "1.0",
            "X-Zyra-Client": "fe-s01-integration",
            "X-Zyra-Client-Version": "0.1.0",
            "X-Request-Id": f"request_{uuid4().hex}",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def test_cli_run_uses_real_task_event_and_verifier_owners(tmp_path: Path) -> None:
    with _real_api(tmp_path) as base_url:
        completed = _run_cli(
            base_url,
            "run",
            "--timeout=3m",
            "Return exactly FE-S01-INTEGRATION and produce independently verifiable evidence.",
        )
        records = _records(completed)
        assert completed.returncode == 0, completed.stderr
        result = records[-1]
        assert result["exit_code"] == 0
        assert result["task_id"].startswith("task_")
        assert result["run_id"].startswith("run_")
        assert result["verifier"]["present"] is True
        assert result["verifier"]["passed"] is True
        assert result["verifier"]["completion_gate_passed"] is True
        assert any(
            record.get("payload", {}).get("schema") == "zyra.cli-task-event.v1"
            for record in records[:-1]
        )
        assert _get(base_url, "/health")["service"] == "zyra-api"


def test_cli_signal_policy_submits_real_cancel_receipt(tmp_path: Path) -> None:
    with _real_api(tmp_path) as base_url:
        completed = _run_cli(
            base_url,
            "run",
            "--timeout=3m",
            "--sealed=true",
            "--cancel-after=1s",
            "Perform a bounded task whose explicit CLI cancellation is durably receipted.",
        )
        records = _records(completed)
        assert completed.returncode == 4, completed.stderr
        assert records[-1]["exit_code"] == 4
        cancel = next(
            record
            for record in records
            if record.get("payload", {}).get("schema")
            == "zyra.cli-cancel-receipt.v1"
        )
        receipt = cancel["payload"]["receipt"]
        assert receipt["operation"] == "task.cancel"
        assert receipt["status"] == "committed"
        task = _get(base_url, f"/tasks/{records[-1]['task_id']}")["task"]
        assert task["status"] == "cancelled"


def test_cli_scenario_calls_existing_http_lifecycle_directly(tmp_path: Path) -> None:
    with _real_api(tmp_path) as base_url:
        registry = _run_cli(base_url, "scenario", "registry")
        registry_records = _records(registry)
        assert registry.returncode == 0, registry.stderr
        response = registry_records[-1]["result"]["response"]
        policy = response["policies"][0]

        rejected = _run_cli(
            base_url,
            "scenario",
            "create",
            "--scenario-id=foundation.short-owner-chain",
            f"--policy-digest={policy['policy_digest']}",
            "--seed=25",
            "Exercise FE-S01 through the real scenario owner chain.",
        )
        rejected_records = _records(rejected)
        assert rejected.returncode == 1
        assert rejected_records[-1]["error"]["code"] == "scenario_preflight_dirty"
        assert rejected_records[-1]["error"]["details"]["dirty_kinds"] == ["artifact"]

        clean_root = tmp_path / "scenario-preflight"
        preflight = [
            {
                "kind": "database",
                "path": str(clean_root / "state.sqlite3"),
                "policy": "sqlite_no_user_rows",
            },
            *[
                {
                    "kind": kind,
                    "path": str(clean_root / kind),
                    "policy": "absent_or_empty",
                }
                for kind in ("cache", "index", "artifact", "build")
            ],
        ]
        created = _run_cli(
            base_url,
            "scenario",
            "create",
            "--scenario-id=foundation.short-owner-chain",
            f"--policy-digest={policy['policy_digest']}",
            f"--preflight={json.dumps(preflight, separators=(',', ':'))}",
            "--seed=26",
            "Exercise FE-S01 sealed admission through the real scenario owner chain.",
        )
        created_records = _records(created)
        assert created.returncode == 0, created.stderr
        run_id = created_records[-1]["run_id"]

        started = _run_cli(
            base_url,
            "scenario",
            "start",
            "--timeout=2m",
            run_id,
            "--wait",
        )
        started_records = _records(started)
        assert started.returncode == 0, started.stderr
        assert started_records[-1]["status"] == "succeeded", started.stdout

        verified = _run_cli(base_url, "scenario", "verify", run_id)
        verified_records = _records(verified)
        assert verified.returncode == 0, verified.stderr
        assert (
            verified_records[-1]["result"]["response"]
            ["verification_receipt"]["valid"]
            is True
        )

        evidence = _run_cli(base_url, "scenario", "evidence", run_id)
        evidence_records = _records(evidence)
        assert evidence.returncode == 0, evidence.stderr
        assert evidence_records[-1]["result"]["response"]["evidence_manifest"]


def test_cli_autostarts_managed_daemon_and_leaves_it_alive(tmp_path: Path) -> None:
    base_url = _unused_loopback_origin()
    environment = _isolated_daemon_environment(tmp_path)
    managed_started = False
    try:
        started = _run_cli(
            base_url,
            "scenario",
            "registry",
            "--startup-timeout=90s",
            timeout=120,
            environment=environment,
            auto_start=True,
        )
        assert started.returncode == 0, started.stderr
        _records(started)

        status = _run_cli(
            base_url,
            "daemon",
            "status",
            environment=environment,
        )
        status_records = _records(status)
        assert status.returncode == 0, status.stderr
        projection = status_records[-1]["result"]
        assert projection["reachable"] is True
        assert projection["managed"] is True
        assert isinstance(projection["pid"], int) and projection["pid"] > 0
        managed_started = True
    finally:
        stopped = _run_cli(
            base_url,
            "daemon",
            "stop",
            "--force=true",
            "--startup-timeout=30s",
            timeout=60,
            environment=environment,
        )
        if managed_started:
            assert stopped.returncode == 0, stopped.stderr
