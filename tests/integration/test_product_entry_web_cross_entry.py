from __future__ import annotations

import importlib
import json
import os
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BUN = REPOSITORY_ROOT / "node_modules" / "bun" / "bin" / "bun.exe"
PROBE = (
    REPOSITORY_ROOT
    / "apps"
    / "web"
    / "test"
    / "cross-entry-continuity-probe.ts"
)


def test_cli_and_web_share_task_event_and_command_receipt(tmp_path: Path) -> None:
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
    }
    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
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
        completed = subprocess.run(
            [str(BUN), str(PROBE), base_url],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout.strip())
        assert result["task"]["taskId"].startswith("task_")
        assert result["task"]["runId"].startswith("run_")
        assert result["task"]["sessionId"].startswith("session_")
        assert result["task"]["listObserved"] is True
        assert result["command"]["webRequestId"] == result["command"]["cliRequestId"]
        assert result["command"]["sameReceipt"] is True
        assert result["command"]["effectEvents"] == 1
        # The control route preserves server-side idempotency but does not emit
        # the optional replay header.  Keep that absence explicit instead of
        # upgrading it to stronger evidence than the backend returned.
        assert result["command"]["replayHeaderPresent"] is False
        assert result["command"]["projectedLifecycle"] in {
            "queued",
            "running",
            "completed",
        }
        assert result["event"]["generation"] >= 1
        assert result["event"]["committedSequence"] >= 2
        assert result["event"]["eventCount"] >= 2
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
