"""Launch a real product-TUI task through ConPTY, detach, and record canonical identity."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlsplit
from urllib.request import urlopen

from zyra_workers.terminal import PtySpawnOptions, spawn_pty


ROOT = Path(__file__).resolve().parents[2]
ANSI = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", re.DOTALL)
DEVELOPER_EVENT_FLOOD = re.compile(rb"\b\d{6}\s+(?:event|model|artifact|error)\s+runtime\.")
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_GOAL_BYTES = 256 * 1024


class RollingCapture:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tail = bytearray()
        self.total_bytes = 0

    def append(self, value: bytes) -> None:
        with self._lock:
            self.total_bytes += len(value)
            self._tail.extend(value)
            if len(self._tail) > MAX_CAPTURE_BYTES:
                del self._tail[: len(self._tail) - MAX_CAPTURE_BYTES]

    def tail(self) -> bytes:
        with self._lock:
            return bytes(self._tail)


def _reader(process: object, capture: RollingCapture) -> None:
    while True:
        chunk = process.read()  # type: ignore[attr-defined]
        if not chunk:
            return
        capture.append(chunk)


def _visible(capture: RollingCapture) -> str:
    return ANSI.sub(b"", capture.tail()).decode("utf-8", "replace")


def _canonical_tasks(base_url: str) -> dict[str, dict[str, Any]]:
    with urlopen(f"{base_url}/tasks?limit=1000", timeout=10) as response:
        payload = json.load(response)
    tasks = payload.get("tasks", payload)
    if not isinstance(tasks, list):
        raise AssertionError("canonical task list response did not contain a task list")
    result: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if not isinstance(task, dict):
            raise AssertionError("canonical task list contained a non-object task")
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise AssertionError("canonical task list contained a task without an identity")
        result[task_id] = task
    return result


def _wait_for_new_task(base_url: str, previous: set[str], timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        tasks = _canonical_tasks(base_url)
        created = set(tasks).difference(previous)
        if len(created) == 1:
            return tasks[created.pop()]
        if len(created) > 1:
            raise AssertionError(f"expected exactly one new canonical task, received {sorted(created)!r}")
        if time.monotonic() >= deadline:
            raise TimeoutError("product TUI did not create a new canonical task")
        time.sleep(0.1)


def _wait_for(capture: RollingCapture, marker: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while marker not in _visible(capture):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"product TUI did not expose marker {marker!r}")
        time.sleep(0.02)


def _type_command(process: object, command: str) -> None:
    for character in command:
        process.write(character.encode("utf-8"))  # type: ignore[attr-defined]
        time.sleep(0.02)
    time.sleep(0.15)
    process.write(b"\r")  # type: ignore[attr-defined]


def _canonical_task(base_url: str, task_id: str) -> dict[str, Any]:
    with urlopen(f"{base_url}/tasks/{task_id}", timeout=10) as response:
        payload = json.load(response)
    task = payload.get("task", payload)
    if not isinstance(task, dict) or task.get("task_id") != task_id:
        raise AssertionError("canonical task response did not preserve task identity")
    return task


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--goal-file", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    arguments = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("phase_g_task_launcher.py requires Windows")
    parsed = urlsplit(arguments.base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit("--base-url must be a credential-free loopback origin")
    workspace = arguments.workspace.resolve(strict=True)
    goal_path = arguments.goal_file.resolve(strict=True)
    report = arguments.report.resolve(strict=False)
    goal_bytes = goal_path.read_bytes()
    if not goal_bytes or len(goal_bytes) > MAX_GOAL_BYTES:
        raise SystemExit("--goal-file must contain between 1 and 262144 UTF-8 bytes")
    goal = goal_bytes.decode("utf-8", "strict").strip()
    if not goal:
        raise SystemExit("--goal-file must not be blank")
    if report.exists():
        raise SystemExit("--report must not overwrite an existing file")
    report.parent.mkdir(parents=True, exist_ok=True)

    previous_tasks = set(_canonical_tasks(arguments.base_url))
    command = subprocess.list2cmdline([
        "node",
        str(ROOT / "apps" / "cli" / "dist" / "zyra.js"),
        "--base-url",
        arguments.base_url,
        "--autostart=false",
        "--startup-timeout",
        "300000ms",
        "--timeout",
        "0",
    ])
    with tempfile.TemporaryDirectory(prefix="zyra-phase-g-launch-", dir=ROOT / ".tmp") as state:
        environment = dict(os.environ)
        environment["ZYRA_SKIP_ONBOARDING"] = "1"
        environment["ZYRA_CLI_STATE_DIR"] = state
        process = spawn_pty(PtySpawnOptions(
            command=command,
            cwd=workspace,
            shell=os.environ.get("COMSPEC", "cmd.exe"),
            rows=32,
            cols=100,
            environment=environment,
        ))
        capture = RollingCapture()
        reader = threading.Thread(target=_reader, args=(process, capture), daemon=True)
        reader.start()
        started = time.monotonic()
        try:
            _wait_for(capture, ">_ Zyra", arguments.timeout)
            _wait_for(capture, "/help 查看命令", arguments.timeout)
            composer_ready_ms = round((time.monotonic() - started) * 1_000, 3)
            _type_command(process, "/model")
            # The command-completion row also contains this title in its
            # description.  Wait for the picker's bordered title so Enter is
            # never sent to the composer overlay by mistake.
            _wait_for(capture, "╭─ 选择后续任务模型", arguments.timeout)
            process.write(b"\r")  # type: ignore[attr-defined]
            try:
                _wait_for(capture, "选择推理强度", 2.0)
            except TimeoutError:
                pass
            else:
                process.write(b"\r")  # type: ignore[attr-defined]
            _wait_for(capture, "作用域 · 后续新 task", arguments.timeout)
            submitted = time.monotonic()
            _type_command(process, goal)
            created = _wait_for_new_task(arguments.base_url, previous_tasks, arguments.timeout)
            task_id = str(created["task_id"])
            submit_to_canonical_ms = round((time.monotonic() - submitted) * 1_000, 3)
            before = _canonical_task(arguments.base_url, task_id)
            if before.get("user_goal") != goal:
                raise AssertionError("canonical task goal did not exactly match the composer submission")
            _type_command(process, "/status")
            _wait_for(capture, f"task {task_id} ·", arguments.timeout)
            _type_command(process, "/exit")
            exit_code = process.wait(timeout=arguments.timeout)
            reader.join(timeout=3)
            after = _canonical_task(arguments.base_url, task_id)
            material = capture.tail()
            payload = {
                "schema": "zyra.product-tui-phase-g-launch/v1",
                "recorded_at": datetime.now(UTC).isoformat(),
                "source_commit": subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, text=True, capture_output=True
                ).stdout.strip(),
                "task_id": task_id,
                "run_id": before.get("run_id"),
                "goal_sha256": hashlib.sha256(goal_bytes).hexdigest(),
                "canonical_before_detach": {"status": before.get("status"), "terminal": before.get("terminal")},
                "canonical_after_detach": {"status": after.get("status"), "terminal": after.get("terminal")},
                "explicit_model_selection": True,
                "tui": {
                    "composer_ready_ms": composer_ready_ms,
                    "submit_to_canonical_ms": submit_to_canonical_ms,
                    "exit_code": exit_code,
                    "output_bytes": capture.total_bytes,
                    "developer_event_flood_visible": DEVELOPER_EVENT_FLOOD.search(material) is not None,
                    "alternate_screen_used": b"\x1b[?1049" in material,
                    "bracketed_paste_disabled": b"\x1b[?2004l" in material,
                    "cursor_restored": b"\x1b[?25h" in material,
                },
            }
            payload["all_passed"] = bool(
                exit_code == 0
                and before.get("run_id") == after.get("run_id")
                and not payload["tui"]["developer_event_flood_visible"]
                and not payload["tui"]["alternate_screen_used"]
                and payload["tui"]["bracketed_paste_disabled"]
                and payload["tui"]["cursor_restored"]
            )
            with report.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            return 0 if payload["all_passed"] else 1
        except Exception:
            print("--- product TUI visible tail ---", file=sys.stderr)
            print(_visible(capture)[-4_000:], file=sys.stderr)
            raise
        finally:
            if process.poll() is None:
                process.terminate_tree(grace_seconds=0.5)
            process.close()
            reader.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
