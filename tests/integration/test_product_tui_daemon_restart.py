from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any
import urllib.request
from uuid import uuid4

import pytest


ROOT = Path(__file__).resolve().parents[2]
BUN = ROOT / "node_modules" / ".bin" / "bun.exe"
ANSI = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", re.DOTALL)


pytestmark = pytest.mark.skipif(os.name != "nt", reason="real daemon restart gate requires Windows ConPTY")


class _Capture:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = bytearray()

    def append(self, value: bytes) -> None:
        with self._lock:
            self._value.extend(value)
            if len(self._value) > 8 * 1024 * 1024:
                del self._value[: len(self._value) - 8 * 1024 * 1024]

    def bytes(self) -> bytes:
        with self._lock:
            return bytes(self._value)


def _environment(tmp_path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "ZYRA_PROJECT_ROOT": str(ROOT),
            "ZYRA_PYTHON": os.fspath(Path(sys.executable)),
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
            "ZYRA_DISABLE_LOCAL_PROVIDER_ENV_FILES": "1",
            "ZAI_API_KEY": "",
            "DEEPSEEK_API_KEY": "",
            "KIMI_API_KEY": "",
        }
    )
    return environment


def _origin() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as selected:
        selected.bind(("127.0.0.1", 0))
        port = selected.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def _run_cli(base_url: str, environment: dict[str, str], *arguments: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "node",
            str(ROOT / "apps" / "cli" / "dist" / "zyra.js"),
            *arguments,
            "--base-url",
            base_url,
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def _request(base_url: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        base_url + path,
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        method="GET" if payload is None else "POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Zyra-Api-Version": "1.0",
            "X-Zyra-Client": "product-tui-daemon-restart-gate",
            "X-Zyra-Client-Version": "0.1.0",
            "X-Request-Id": f"request_{uuid4().hex}",
            "Idempotency-Key": f"restart-gate:{uuid4().hex}",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _set_running(database: Path, task_id: str) -> None:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT checkpoint_json FROM checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        assert row is not None
        checkpoint = json.loads(str(row[0]))
        checkpoint["status"] = "running"
        checkpoint.setdefault("metadata", {})["daemon_restart_gate"] = {"epoch": 1}
        rendered = json.dumps(checkpoint, ensure_ascii=False, sort_keys=True)
        connection.execute(
            "UPDATE tasks SET status = ? WHERE task_id = ?",
            ("running", task_id),
        )
        connection.execute(
            "UPDATE checkpoints SET checkpoint_json = ? WHERE task_id = ?",
            (rendered, task_id),
        )


def _reader(process: object, capture: _Capture, completed: threading.Event) -> None:
    try:
        while True:
            value = process.read()  # type: ignore[attr-defined]
            if not value:
                return
            capture.append(value)
    finally:
        completed.set()


def _wait_for(capture: _Capture, marker: bytes, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while marker not in capture.bytes():
        if time.monotonic() >= deadline:
            visible = ANSI.sub(b"", capture.bytes()).decode("utf-8", "replace")[-4_000:]
            raise TimeoutError(f"marker {marker!r} was not observed:\n{visible}")
        time.sleep(0.02)


def _wait_for_after(capture: _Capture, marker: bytes, offset: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while marker not in capture.bytes()[offset:]:
        if time.monotonic() >= deadline:
            material = ANSI.sub(b"", capture.bytes()[offset:]).decode("utf-8", "replace")
            diagnostics = "\n".join(
                line
                for line in material.splitlines()
                if "连接恢复中" in line or "恢复探测" in line or "OBSERVER_" in line
            )
            raise TimeoutError(
                f"marker {marker!r} was not observed after offset {offset}:"
                f"\nDiagnostics:\n{diagnostics[-4_000:]}\nTail:\n{material[-4_000:]}"
            )
        time.sleep(0.02)


def test_product_tui_recovers_across_real_managed_daemon_restart(tmp_path: Path) -> None:
    from zyra_workers.terminal import PtySpawnOptions, spawn_pty

    assert BUN.is_file(), "bun install is required"
    assert (ROOT / "apps" / "cli" / "dist" / "zyra.js").is_file(), "run bun run build:cli first"
    base_url = _origin()
    environment = _environment(tmp_path)
    observer = None
    reader = None
    capture = _Capture()
    reader_completed = threading.Event()
    daemon_running = False
    try:
        started = _run_cli(
            base_url,
            environment,
            "scenario",
            "registry",
            "--startup-timeout=90s",
        )
        assert started.returncode == 0, started.stderr
        daemon_running = True
        first_health = _request(base_url, "/health")
        created = _request(
            base_url,
            "/tasks",
            {"goal": "Remain observable across a real daemon restart.", "auto_run": False},
        )
        task_id = created["task"]["task_id"]
        _set_running(Path(environment["ZYRA_SQLITE_PATH"]), task_id)
        assert _request(base_url, f"/tasks/{task_id}")["task"]["status"] == "running"

        command = subprocess.list2cmdline(
            [
                str(BUN),
                "scripts\\product-tui\\real_daemon_observer.ts",
                f"--base-url={base_url}",
                f"--task-id={task_id}",
            ]
        )
        observer = spawn_pty(
            PtySpawnOptions(
                command=command,
                cwd=ROOT,
                shell=os.environ.get("COMSPEC", "cmd.exe"),
                rows=32,
                cols=100,
                environment=environment,
            )
        )
        reader = threading.Thread(
            target=_reader,
            args=(observer, capture, reader_completed),
            name="zyra-real-daemon-restart-gate",
            daemon=True,
        )
        reader.start()
        _wait_for(capture, b">_ Zyra", 30)
        observer.write(b"/status\r")
        _wait_for(capture, b"connection connected", 10)
        time.sleep(1)

        stopped = _run_cli(
            base_url,
            environment,
            "daemon",
            "stop",
            "--force=true",
            "--startup-timeout=30s",
            timeout=60,
        )
        assert stopped.returncode == 0, stopped.stderr
        daemon_running = False
        _wait_for(capture, "正在重连".encode(), 30)
        reconnect_offset = len(capture.bytes())
        # Windows can keep the just-terminated listener unavailable for a
        # brief handoff window even after the owning PID has exited.
        time.sleep(1)

        restarted = _run_cli(
            base_url,
            environment,
            "scenario",
            "registry",
            "--startup-timeout=90s",
        )
        startup_log = Path(environment["ZYRA_CLI_STATE_DIR"]) / "daemon-startup.log"
        assert restarted.returncode == 0, (
            restarted.stderr
            + (startup_log.read_text(encoding="utf-8", errors="replace") if startup_log.is_file() else "")
        )
        daemon_running = True
        second_health = _request(base_url, "/health")
        assert second_health["process_id"] != first_health["process_id"]
        assert second_health["cli_daemon_generation"] != first_health["cli_daemon_generation"]

        status_deadline = time.monotonic() + 30
        while b"connection connected" not in capture.bytes()[reconnect_offset:] and time.monotonic() < status_deadline:
            observer.write(b"/status\r")
            time.sleep(0.25)
        _wait_for_after(capture, b"connection connected", reconnect_offset, 2)
        observer.write(b"/cancel daemon restart recovery gate\r")
        exit_code = observer.wait(timeout=30)
        reader_completed.wait(timeout=3)
        visible = ANSI.sub(b"", capture.bytes()).decode("utf-8", "replace")
        assert exit_code == 4, visible[-4_000:]
        assert "任务取消已提交" in visible
        assert "ZYRA_OBSERVER_ERROR" not in visible
        marker = visible.rsplit("ZYRA_OBSERVER_RESULT ", 1)[1].splitlines()[0]
        outcome = json.loads(marker)
        assert outcome["status"] == "cancelled"
        assert outcome["taskId"] == task_id
        assert outcome["result"]["revision"].startswith("2:")
        assert b"\x1b[?2004h" in capture.bytes()
        assert b"\x1b[?2004l" in capture.bytes()
    finally:
        if observer is not None:
            if observer.poll() is None:
                observer.terminate_tree(grace_seconds=0.5)
            observer.close()
        if reader is not None:
            reader.join(timeout=3)
        if daemon_running:
            _run_cli(
                base_url,
                environment,
                "daemon",
                "stop",
                "--force=true",
                "--startup-timeout=30s",
                timeout=60,
            )
