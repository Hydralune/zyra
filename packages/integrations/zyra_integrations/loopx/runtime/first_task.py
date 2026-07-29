from __future__ import annotations

import argparse
import importlib
import json
import os
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError

from .resolver import LoopXRuntimeResolver


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=(
            json.dumps(payload).encode("utf-8")
            if payload is not None
            else None
        ),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _configure(root: Path) -> tuple[Path, Path]:
    tool_workspace = root / "workspace"
    user_home = root / "user-home"
    user_home.mkdir(parents=True, exist_ok=True)
    values = {
        "HOME": user_home,
        "USERPROFILE": user_home,
        "ZYRA_SQLITE_PATH": root / "api.sqlite3",
        "ZYRA_EVENT_LOG": root / "events.jsonl",
        "ZYRA_TOOL_WORKSPACE": tool_workspace,
        "ZYRA_ARTIFACT_ROOT": root / "artifacts",
        "ZYRA_CONTROL_STATE": root / "control",
        "ZYRA_GRAPH_STATE_STORE": root / "graph.sqlite3",
        "ZYRA_WORKER_POOL_STORE": root / "worker-pool.sqlite3",
        "ZYRA_PERMISSION_STATE": root / "permission.json",
        "ZYRA_NETWORK_MODE": "offline",
        "BUN_INSTALL_CACHE_DIR": root / "cache" / "bun",
        "XDG_CACHE_HOME": root / "cache" / "xdg",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name, value in values.items():
        os.environ[name] = str(value)
    return tool_workspace, user_home


def run_first_task_probe(root: Path) -> dict[str, Any]:
    probe_root = root.resolve()
    probe_root.mkdir(parents=True, exist_ok=True)
    tool_workspace, user_home = _configure(probe_root)
    retired_install = tool_workspace / ".zyra" / "loopx" / "install"
    if retired_install.exists():
        raise RuntimeError("retired LoopX install path exists before first task")

    module = importlib.import_module("zyra_api.main")
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.ZyraRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        create_status, created = _request(
            base_url,
            "/tasks",
            method="POST",
            payload={
                "goal": "Verify the first embedded LoopX release task.",
                "auto_run": False,
            },
        )
        if create_status != 201:
            raise RuntimeError(f"first task creation failed: {created}")
        task_id = str(created["task"]["task_id"])
        command_status, command = _request(
            base_url,
            f"/tasks/{task_id}/commands",
            method="POST",
            payload={
                "text": "/loopx-connect",
                "request_id": "detached_first_task_request",
                "idempotency_key": "detached_first_task_connect",
                "arguments": {
                    "todo_id": "todo_detached_first_task",
                    "todo_title": "Verify embedded runtime delivery",
                    "limit_slots": 2,
                },
            },
        )
        if command_status != 201:
            raise RuntimeError(f"first LoopX command failed: {command}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    data = command["command_result"]["data"]
    state = data["state"]
    receipt = data["receipt"]
    runtime = LoopXRuntimeResolver(Path.cwd()).receipt(tool_workspace)
    home_entries = sorted(path.name for path in user_home.iterdir())
    ready = all(
        (
            receipt["status"] == "applied",
            state["lifecycle"] == "enabled",
            state["canonical_state"]["loopx_claim_is_worker_lease"] is False,
            state["canonical_state"]["loopx_quota_is_execution_budget"] is False,
            runtime["version"] == "0.2.13",
            runtime["source_kind"] == "embedded_source",
            runtime["archive_extraction"] is False,
            runtime["archive_fallback"] is False,
            retired_install.exists() is False,
            not home_entries,
        )
    )
    result = {
        "schema": "zyra.loopx-detached-first-task/v1",
        "ready": ready,
        "task_id": task_id,
        "receipt_status": receipt["status"],
        "event_id": receipt["event_id"],
        "artifact_id": receipt["artifact_id"],
        "lifecycle": state["lifecycle"],
        "sync_cursor": state["sync"]["cursor"],
        "claim_is_worker_lease": state["canonical_state"][
            "loopx_claim_is_worker_lease"
        ],
        "quota_is_execution_budget": state["canonical_state"][
            "loopx_quota_is_execution_budget"
        ],
        "runtime": runtime,
        "retired_install_created": retired_install.exists(),
        "user_home_entries": home_entries,
        "network_mode": os.environ["ZYRA_NETWORK_MODE"],
    }
    if not ready:
        raise RuntimeError(f"detached first task probe failed: {result}")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    arguments = parser.parse_args(argv)
    print(
        json.dumps(
            run_first_task_probe(Path(arguments.workspace)),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
