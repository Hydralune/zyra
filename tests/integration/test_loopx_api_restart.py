from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError

from tests.integration.test_loopx_control_commands import (
    ROOT,
    _configure,
    _request,
)


def _serve():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    environment = dict(os.environ)
    environment["ZYRA_API_HOST"] = "127.0.0.1"
    environment["ZYRA_API_PORT"] = str(port)
    server_code = (
        "from http.server import ThreadingHTTPServer;"
        "from apps.api.zyra_api.main import ZyraRequestHandler;"
        f"server=ThreadingHTTPServer(('127.0.0.1',{port}),ZyraRequestHandler);"
        "server.serve_forever();"
        "server.server_close()"
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", server_code],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=5)
            raise RuntimeError(
                f"API process exited {process.returncode}: {stdout}\n{stderr}"
            )
        try:
            status, _ = _request(base, "/health")
            if status == 200:
                return process, base
        except (HTTPError, URLError, TimeoutError):
            pass
        time.sleep(0.05)
    process.terminate()
    stdout, stderr = process.communicate(timeout=10)
    raise RuntimeError(f"API startup timed out: {stdout}\n{stderr}")


def _stop(process: subprocess.Popen[str], base: str) -> None:
    if process.poll() is not None:
        return
    try:
        _request(base, "/deployment/shutdown", method="POST", payload={})
        process.wait(timeout=20)
    except (HTTPError, URLError, TimeoutError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def test_clean_process_restart_restores_loopx_control_and_outbox_cursor(
    tmp_path: Path,
) -> None:
    _configure(tmp_path)
    process, base = _serve()
    try:
        _, created = _request(
            base,
            "/tasks",
            method="POST",
            payload={
                "goal": "Resume a LoopX-controlled task after restart.",
                "auto_run": False,
            },
        )
        task_id = created["task"]["task_id"]
        _, connected = _request(
            base,
            f"/tasks/{task_id}/loopx/commands",
            method="POST",
            payload={
                "action": "connect",
                "todo_id": "todo_restart",
                "todo_title": "Resume after a clean process restart",
                "limit_slots": 4,
            },
        )
        _, claimed = _request(
            base,
            f"/tasks/{task_id}/loopx/commands",
            method="POST",
            payload={
                "action": "claim",
                "todo_id": "todo_restart",
                "claimant": "restart-controller",
            },
        )
        before = claimed["state"]
        assert before["sync"]["cursor"] == 2
        assert before["private_state"]["claims"] == [
            {
                "todo_id": "todo_restart",
                "claimant": "restart-controller",
            }
        ]
        assert connected["state"]["last_validated_receipt"][
            "canonical_commit_id"
        ]
    finally:
        _stop(process, base)

    restarted, restarted_base = _serve()
    try:
        status, after = _request(
            restarted_base,
            f"/tasks/{task_id}/loopx",
        )
        assert status == 200, json.dumps(after)
        assert after["schema"] == "zyra.loopx-control-state/v1"
        assert after["lifecycle"] == "enabled"
        assert after["sync"]["cursor"] == before["sync"]["cursor"]
        assert after["sync"]["acked"] == before["sync"]["acked"]
        assert after["private_state"]["claims"] == before["private_state"][
            "claims"
        ]
        assert after["private_state"]["quota"] == before["private_state"][
            "quota"
        ]
        assert after["private_state"]["history"]
        assert after["continuation"]["allowed"] is True

        state_path = (
            tmp_path
            / "workspace"
            / ".zyra"
            / "loopx"
            / "state"
            / "private"
            / "goals"
            / after["goal_id"]
            / "bridge-state.json"
        )
        corrupt = json.loads(state_path.read_text(encoding="utf-8"))
        corrupt["mapping_version"] = "zyra.loopx-state-mapping/v999"
        state_path.write_text(
            json.dumps(corrupt),
            encoding="utf-8",
        )
        degraded_status, degraded = _request(
            restarted_base,
            f"/tasks/{task_id}/loopx",
        )
        assert degraded_status == 200
        assert degraded["lifecycle"] == "degraded"
        assert degraded["error"]["code"] == "loopx_state_version_mismatch"
        assert degraded["continuation"]["allowed"] is False
        assert degraded["error"]["recovery"] == (
            "retry_sync_or_disconnect_and_reconnect"
        )
        repaired_status, repaired = _request(
            restarted_base,
            f"/tasks/{task_id}/loopx/commands",
            method="POST",
            payload={"action": "sync_retry"},
        )
        assert repaired_status == 201
        assert repaired["state"]["lifecycle"] == "enabled"
        assert repaired["state"]["continuation"]["allowed"] is True
        assert repaired["command_result"]["result"]["data"][
            "rebuilt_private_projection"
        ]["status"] == "idempotent_replay"
    finally:
        _stop(restarted, restarted_base)
