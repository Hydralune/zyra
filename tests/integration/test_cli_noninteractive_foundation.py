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
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any, Iterator
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUN = ROOT / "node_modules" / ".bin" / (
    "bun.exe" if os.name == "nt" else "bun"
)
CLI = ROOT / "apps" / "cli" / "src" / "index.ts"
LIVE_PROVIDER_REQUIRED = pytest.mark.skipif(
    os.environ.get("ZYRA_RUN_LIVE_PROVIDER_TESTS") != "1",
    reason=(
        "set ZYRA_RUN_LIVE_PROVIDER_TESTS=1 only after authorizing the bounded "
        "external provider request"
    ),
)
INTERACTIVE_PROVIDER_REQUIRED = pytest.mark.skipif(
    os.environ.get("ZYRA_RUN_LIVE_INTERACTIVE_PROVIDER_TESTS") != "1",
    reason=(
        "set ZYRA_RUN_LIVE_INTERACTIVE_PROVIDER_TESTS=1 only after authorizing "
        "the separate interactive external-provider scenario"
    ),
)


def _records(completed: subprocess.CompletedProcess[str]) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in completed.stdout.splitlines() if line]
    assert records
    assert records[-1]["type"] == "result"
    assert "\x1b" not in completed.stdout
    return records


def _task_failure_diagnostic(
    base_url: str,
    records: list[dict[str, Any]],
) -> str:
    """Return bounded structural failure evidence without provider content."""

    task_id = str(records[-1].get("task_id") or "")
    if not task_id:
        return json.dumps({"task_id": "", "result": records[-1]}, sort_keys=True)
    task = _get(base_url, f"/tasks/{task_id}")["task"]
    events = _get(base_url, f"/tasks/{task_id}/events")["events"]
    node_failures = [
        {
            "node_id": node.get("node_id"),
            "stage": node.get("metadata", {}).get("stage"),
            "status": node.get("status"),
            "result_summary": node.get("metadata", {}).get("result_summary"),
            "worker_error": node.get("metadata", {}).get("worker_error"),
        }
        for node in task.get("plan_nodes", {}).values()
        if node.get("status") in {"failed", "blocked"}
        or node.get("metadata", {}).get("worker_error")
    ]
    notices = []

    def bounded_scalar(value: object, limit: int) -> str:
        if not isinstance(value, (str, int, float, bool)):
            return ""
        return str(value)[:limit]

    for event in events:
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if not any(
            key in payload
            for key in ("error", "error_code", "message", "failed_conditions")
        ) and payload.get("status") not in {"failed", "blocked"}:
            continue
        error_metadata = (
            payload.get("error_metadata")
            if isinstance(payload.get("error_metadata"), dict)
            else {}
        )
        physical_failure = (
            error_metadata.get("physical_execution_failure_receipt")
            if isinstance(
                error_metadata.get("physical_execution_failure_receipt"),
                dict,
            )
            else {}
        )
        dispatch_error = (
            error_metadata.get("physical_dispatch_error")
            if isinstance(error_metadata.get("physical_dispatch_error"), dict)
            else {}
        )
        notices.append(
            {
                "event_type": event.get("event_type"),
                "schema": payload.get("schema"),
                "status": payload.get("status"),
                "summary": bounded_scalar(payload.get("summary"), 500),
                "error": bounded_scalar(payload.get("error"), 500),
                "error_code": bounded_scalar(payload.get("error_code"), 200),
                "message": bounded_scalar(payload.get("message"), 500),
                "failed_conditions": payload.get("failed_conditions"),
                "physical_failure_code": physical_failure.get("failure_code"),
                "physical_failure_side_effect_started": physical_failure.get(
                    "side_effect_started"
                ),
                "dispatch_error_code": dispatch_error.get("code"),
                "dispatch_node_error": (
                    {
                        key: dispatch_error.get("node_error", {}).get(key)
                        for key in ("error", "message", "status", "code")
                        if key in dispatch_error.get("node_error", {})
                    }
                    | {
                        "details": {
                            "worker_error": str(
                                dispatch_error.get("node_error", {})
                                .get("details", {})
                                .get("worker_error")
                                or ""
                            )[:200],
                            "provider_called": (
                                dispatch_error.get("node_error", {})
                                .get("details", {})
                                .get("provider_called")
                            ),
                            "tool_call_count": (
                                dispatch_error.get("node_error", {})
                                .get("details", {})
                                .get("tool_call_count")
                            ),
                            "provider_failure": (
                                dispatch_error.get("node_error", {})
                                .get("details", {})
                                .get("provider_failure")
                            ),
                        }
                    }
                    if isinstance(dispatch_error.get("node_error"), dict)
                    else {}
                ),
            }
        )
    return json.dumps(
        {
            "task_id": task_id,
            "task_status": task.get("status"),
            "node_failures": node_failures,
            "notices": notices[-12:],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _run_cli(
    base_url: str,
    *arguments: str,
    timeout: int = 180,
    environment: dict[str, str] | None = None,
    auto_start: bool = False,
    stdin_text: str | None = None,
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
        input=stdin_text,
    )


def _isolated_daemon_environment(tmp_path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "ZYRA_PROJECT_ROOT": str(ROOT),
            "ZYRA_PYTHON": sys.executable,
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
def _real_api(
    tmp_path: Path,
    *,
    live_provider: bool = False,
) -> Iterator[str]:
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
    if not live_provider:
        environment.update(
            {
                "ZYRA_DISABLE_LOCAL_PROVIDER_ENV_FILES": "1",
                "ZAI_API_KEY": "",
                "DEEPSEEK_API_KEY": "",
                "KIMI_API_KEY": "",
            }
        )
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


def _post(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        base_url + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Zyra-Api-Version": "1.0",
            "X-Zyra-Client": "fe-s03-integration",
            "X-Zyra-Client-Version": "0.1.0",
            "X-Request-Id": f"request_{uuid4().hex}",
            "Idempotency-Key": f"fe-s03:{uuid4().hex}",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


@LIVE_PROVIDER_REQUIRED
def test_cli_run_uses_real_task_event_and_verifier_owners(tmp_path: Path) -> None:
    with _real_api(tmp_path, live_provider=True) as base_url:
        completed = _run_cli(
            base_url,
            "run",
            "--timeout=3m",
            "测试，收到请回复 ok",
        )
        records = _records(completed)
        assert completed.returncode == 0, _task_failure_diagnostic(base_url, records)
        result = records[-1]
        assert result["exit_code"] == 0
        assert result["task_id"].startswith("task_")
        assert result["run_id"].startswith("run_")
        assert result["verifier"]["present"] is True
        assert result["verifier"]["passed"] is True
        assert result["verifier"]["completion_gate_passed"] is True
        assert result["result"]["final_answer"] == "ok"
        assert any(
            record.get("payload", {}).get("schema") == "zyra.cli-task-event.v1"
            for record in records[:-1]
        )
        assert _get(base_url, "/health")["service"] == "zyra-api"


@LIVE_PROVIDER_REQUIRED
def test_cli_run_delivers_requested_file_through_product_entry(
    tmp_path: Path,
) -> None:
    with _real_api(tmp_path, live_provider=True) as base_url:
        completed = _run_cli(
            base_url,
            "run",
            "--timeout=5m",
            "建一个 smoke.txt 文件，内容是一行 ZYRA_SMOKE_OK",
            timeout=360,
        )
        records = _records(completed)
        assert completed.returncode == 0, _task_failure_diagnostic(base_url, records)
        final = records[-1]
        assert final["exit_code"] == 0
        delivery = final["result"]["workspace_delivery"]
        assert "smoke.txt" in delivery["changed_paths"]
        assert delivery["file_api_resource"] == (
            f"workspaces/{delivery['workspace_id']}/files"
        )
        query = urllib.parse.urlencode(
            {"path": "smoke.txt", "read": "true", "encoding": "utf-8"}
        )
        delivered = _get(
            base_url,
            f"/workspaces/{delivery['workspace_id']}/files?{query}",
        )
        assert delivered["content"] in {
            "ZYRA_SMOKE_OK",
            "ZYRA_SMOKE_OK\n",
            "ZYRA_SMOKE_OK\r\n",
        }
        task = _get(base_url, f"/tasks/{final['task_id']}")["task"]
        assert task["status"] == "completed"
        assert task["metadata"]["delivery"]["changed_paths"] == ["smoke.txt"]


@LIVE_PROVIDER_REQUIRED
def test_cli_run_reads_piped_stdin_without_polluting_jsonl(tmp_path: Path) -> None:
    with _real_api(tmp_path, live_provider=True) as base_url:
        completed = _run_cli(
            base_url,
            "run",
            "--timeout=3m",
            stdin_text=(
                "Return exactly FE-S06-STDIN and produce independently "
                "verifiable evidence.\n"
            ),
        )
        records = _records(completed)
        assert completed.returncode == 0, completed.stderr
        assert records[-1]["exit_code"] == 0
        task = _get(base_url, f"/tasks/{records[-1]['task_id']}")["task"]
        assert task["user_goal"].startswith("Return exactly FE-S06-STDIN")
        assert all(line.startswith("{") for line in completed.stdout.splitlines())


@LIVE_PROVIDER_REQUIRED
def test_cli_signal_policy_submits_real_cancel_receipt(tmp_path: Path) -> None:
    with _real_api(tmp_path, live_provider=True) as base_url:
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

        scenario = _get(base_url, f"/scenarios/runs/{run_id}")["run"]
        task_id = scenario["task_id"]
        task = _get(base_url, f"/tasks/{task_id}")["task"]
        assert task["metadata"]["worker_pool"]["worker_id"].startswith(
            "foundation-scenario-worker"
        )
        task_events = _get(base_url, f"/tasks/{task_id}/events")["events"]
        execution_receipt = next(
            item["payload"]
            for item in task_events
            if item.get("payload", {}).get("schema")
            == "zyra.foundation-owner-execution-receipt/v1"
        )
        assert execution_receipt["runtime_worker"] == "ScenarioOwnerChainRuntime"
        assert execution_receipt["provider_called"] is False
        assert execution_receipt["provider_reasoning_required"] is False
        assert all(execution_receipt["checks"].values())
        assert not (
            tmp_path
            / "artifacts"
            / ".provider-control-plane"
            / "provider.sqlite3"
        ).exists()

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
        health = _get(base_url, "/health")
        assert projection["pid"] == health["process_id"]
        assert projection["generation"] == health["cli_daemon_generation"]
        assert not list(Path(environment["ZYRA_CLI_STATE_DIR"]).glob("daemon-runtime-*.json"))
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


def test_cli_daemon_stop_protects_active_tasks_and_force_is_audited(
    tmp_path: Path,
) -> None:
    base_url = _unused_loopback_origin()
    environment = _isolated_daemon_environment(tmp_path)
    managed_started = False
    force_stopped = False
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
        managed_started = True

        created = _post(
            base_url,
            "/tasks",
            {
                "goal": "Remain pending while FE-S03 verifies daemon stop protection.",
                "auto_run": False,
            },
        )
        task_id = created["task"]["task_id"]

        rejected = _run_cli(
            base_url,
            "daemon",
            "stop",
            "--startup-timeout=30s",
            timeout=60,
            environment=environment,
        )
        rejected_records = _records(rejected)
        assert rejected.returncode == 1, rejected.stderr
        error = rejected_records[-1]["error"]
        assert error["code"] == "daemon_active_tasks"
        assert task_id in error["details"]["active_task_ids"]
        assert error["details"]["audit_persisted"] is True

        forced = _run_cli(
            base_url,
            "daemon",
            "stop",
            "--force=true",
            "--startup-timeout=30s",
            timeout=60,
            environment=environment,
        )
        forced_records = _records(forced)
        assert forced.returncode == 0, forced.stderr
        audit = forced_records[-1]["result"]["audit"]
        assert audit["phase"] == "committed"
        assert audit["forced"] is True
        assert task_id in audit["active_task_ids"]
        force_stopped = True

        audit_path = Path(environment["ZYRA_CLI_STATE_DIR"]) / "daemon-stop-audit.jsonl"
        audit_records = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        assert [item["phase"] for item in audit_records] == ["rejected", "committed"]
        assert audit_records[-1]["audit_id"] == audit["audit_id"]
    finally:
        if managed_started and not force_stopped:
            _run_cli(
                base_url,
                "daemon",
                "stop",
                "--force=true",
                "--startup-timeout=30s",
                timeout=60,
                environment=environment,
            )


@INTERACTIVE_PROVIDER_REQUIRED
def test_cli_interactive_stream_resume_list_and_web_share_server_identity(
    tmp_path: Path,
) -> None:
    with _real_api(tmp_path, live_provider=True) as base_url:
        completed = _run_cli(
            base_url,
            "--timeout=3m",
            "Return exactly FE-S02-INTEGRATION and produce independently verifiable evidence.",
        )
        assert completed.returncode == 0, completed.stderr
        assert "\x1b[?1049" not in completed.stdout
        assert "session task_" in completed.stdout
        assert "revision 1:" in completed.stdout
        assert "model" in completed.stdout
        assert "artifact" in completed.stdout
        assert "[complete]" in completed.stdout
        task_id = completed.stdout.split("session ", 1)[1].split(" ", 1)[0]

        listed = _run_cli(base_url, "ls", "--limit=100")
        list_records = _records(listed)
        projection = list_records[-1]["result"]
        assert projection["state_owner"] == "task_store_projection"
        assert any(item["task_id"] == task_id for item in projection["tasks"])
        session = next(
            item for item in projection["sessions"]
            if item["resume_task_id"] == task_id
        )

        web_probe = subprocess.run(
            [
                str(BUN),
                "-e",
                (
                    "import {ZyraApiClient} from './apps/web/src/api/client.ts';"
                    "import {TaskApi} from './apps/web/src/api/task-api.ts';"
                    f"const client=new ZyraApiClient({{baseUrl:{json.dumps(base_url)}}});"
                    "const api=new TaskApi(client);"
                    f"const task=await api.get({json.dumps(task_id)});"
                    "console.log(JSON.stringify({taskId:task.taskId,runId:task.runId,"
                    "updatedAt:task.updatedAt,sessionId:task.sessionId}));client.close();"
                ),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert web_probe.returncode == 0, web_probe.stderr
        observed = json.loads(web_probe.stdout)
        assert observed["taskId"] == task_id
        task_projection = next(
            item for item in projection["tasks"] if item["task_id"] == task_id
        )
        assert observed["runId"] == task_projection["run_id"]
        assert observed["updatedAt"] == task_projection["updated_at"]

        before = _get(base_url, "/tasks?limit=100")["total"]
        from apps.api.zyra_api import main as api_main

        rpc_id = f"rpc-{uuid4().hex}"
        tool_call_id = f"toolcall-{uuid4().hex}"
        request_id = f"request-{uuid4().hex}"
        context = {
            "identity": {
                "runId": observed["runId"],
                "sessionId": session["session_id"],
                "taskId": task_id,
                "workerId": "fe-s02-real-tool-worker",
            },
            "requestId": request_id,
            "producerSequence": 1,
            "receivedAt": "2026-08-04T00:00:01.000Z",
        }
        called = api_main.get_runtime_event_spine_bridge().append_omp_rpc_frame(
            {
                "type": "host_tool_call",
                "payload": {
                    "id": rpc_id,
                    "tool_call_id": tool_call_id,
                    "tool_name": "mcp_list_resources",
                    "arguments": {},
                },
            },
            context=context,
        )
        context["producerSequence"] = 2
        context["receivedAt"] = "2026-08-04T00:00:02.000Z"
        succeeded = api_main.get_runtime_event_spine_bridge().append_omp_rpc_frame(
            {
                "type": "host_tool_result",
                "payload": {"id": rpc_id, "result": {"resources": []}},
            },
            context=context,
        )
        assert called["results"][0]["eventType"] == "runtime.tool.called"
        assert succeeded["results"][0]["eventType"] == "runtime.tool.succeeded"
        resumed = _run_cli(base_url, "resume", session["session_id"])
        after = _get(base_url, "/tasks?limit=100")["total"]
        assert resumed.returncode == 0, resumed.stderr
        assert f"session {task_id}" in resumed.stdout
        assert "revision 1:" in resumed.stdout
        assert "tool" in resumed.stdout
        assert before == after
