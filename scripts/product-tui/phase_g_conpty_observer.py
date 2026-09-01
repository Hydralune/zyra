"""Attach the built product TUI twice to a real canonical task and verify detach/resume."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlsplit
from urllib.parse import urlencode
from urllib.request import urlopen

from zyra_workers.terminal import PtySpawnOptions, spawn_pty


ROOT = Path(__file__).resolve().parents[2]
ANSI = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", re.DOTALL)
DEVELOPER_EVENT_FLOOD = re.compile(rb"\b\d{6}\s+(?:event|model|artifact|error)\s+runtime\.")
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
TERMINAL_STATUSES = frozenset({"completed", "failed", "blocked", "cancelled", "killed"})


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


@dataclass(frozen=True, slots=True)
class AttachResult:
    cycle: int
    pid: int
    startup_ms: float
    detach_ms: float
    exit_code: int
    resize_count: int
    output_bytes: int
    task_identity_visible: bool
    reported_status: str
    control_command: str | None
    control_receipt: str | None
    waited_for_terminal: bool
    canonical_status_at_detach: str
    developer_event_flood_visible: bool
    bracketed_paste_enabled: bool
    bracketed_paste_disabled: bool
    alternate_screen_used: bool


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _reader(process: object, capture: RollingCapture) -> None:
    while True:
        chunk = process.read()  # type: ignore[attr-defined]
        if not chunk:
            return
        capture.append(chunk)


def _visible(capture: RollingCapture) -> str:
    return ANSI.sub(b"", capture.tail()).decode("utf-8", "replace")


def _wait_for(capture: RollingCapture, marker: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while marker not in _visible(capture):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"missing TUI marker {marker!r}:\n{_visible(capture)[-2_000:]}")
        time.sleep(0.02)


def _wait_for_reported_status(capture: RollingCapture, task_id: str, timeout: float) -> str:
    pattern = re.compile(
        rf"task {re.escape(task_id)} · (pending|running|paused|completed|failed|blocked|cancelled|killed|interrupted)"
    )
    deadline = time.monotonic() + timeout
    while True:
        match = pattern.search(_visible(capture))
        if match:
            return match.group(1)
        if time.monotonic() >= deadline:
            raise TimeoutError(f"missing canonical status notice:\n{_visible(capture)[-2_000:]}")
        time.sleep(0.02)


def _request_status_until(
    process: object,
    capture: RollingCapture,
    task_id: str,
    expected_status: str,
    timeout: float,
) -> None:
    marker = f"task {task_id} · {expected_status}"
    deadline = time.monotonic() + timeout
    while marker not in _visible(capture):
        if process.poll() is not None:  # type: ignore[attr-defined]
            raise RuntimeError("product TUI exited before canonical status reconciliation")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"missing TUI marker {marker!r}:\n{_visible(capture)[-2_000:]}")
        _type_command(process, "/status")
        time.sleep(0.75)


def _wait_for_control_receipt(capture: RollingCapture, command_name: str, timeout: float) -> str:
    pattern = re.compile(
        rf"(queued|accepted|executing|applied|completed|failed|cancelled|rejected) "
        rf"{re.escape(command_name)} · request ([A-Za-z0-9_.:-]+)"
    )
    deadline = time.monotonic() + timeout
    while True:
        match = pattern.search(_visible(capture))
        if match:
            return match.group(0)
        if time.monotonic() >= deadline:
            raise TimeoutError(f"missing canonical control receipt:\n{_visible(capture)[-2_000:]}")
        time.sleep(0.02)


def _receipt_command_name(command: str) -> str:
    parts = command.split()
    trigger = parts[0]
    if trigger in {"/redirect", "/interrupt", "/review"}:
        return "/change"
    if trigger in {"/now", "/next", "/later"}:
        if len(parts) < 2 or not parts[1].startswith("/"):
            raise ValueError(f"{trigger} must wrap a slash command")
        return parts[1]
    if trigger in {
        "/status", "/pwd", "/doctor", "/model", "/mode", "/permissions",
        "/copy", "/export", "/raw", "/diff", "/plan", "/verification",
        "/verify", "/tools", "/artifact", "/ui", "/queue", "/cancel",
        "/continue", "/cancel-command", "/retry", "/approve", "/deny",
        "/exit", "/detach", "/help",
    }:
        raise ValueError(f"{trigger} does not produce the required control receipt in this observer")
    return trigger


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


def _wait_for_canonical_terminal(
    process: object,
    base_url: str,
    task_id: str,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        task = _canonical_task(base_url, task_id)
        if task.get("terminal") is True or task.get("status") in TERMINAL_STATUSES:
            return task
        exit_code = process.poll()  # type: ignore[attr-defined]
        if exit_code is not None:
            raise RuntimeError(f"product TUI exited with {exit_code} before canonical terminal state")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"canonical task {task_id} did not settle within {timeout:.3f}s")
        time.sleep(1.0)


def _ingress_statistics(base_url: str, task_id: str) -> dict[str, Any]:
    with urlopen(f"{base_url}/tasks/{task_id}/event-ingress/capabilities", timeout=10) as response:
        capabilities = json.load(response)
    generation = capabilities.get("generation")
    if not isinstance(generation, int) or generation < 1:
        raise AssertionError("event ingress capabilities omitted a valid generation")
    cursor: str | None = None
    frames = 0
    event_types: dict[str, int] = {}
    next_sequence = 0
    for _page in range(10_000):
        query: dict[str, object] = {"generation": generation, "limit": 1000}
        if cursor is not None:
            query["cursor"] = cursor
        with urlopen(
            f"{base_url}/tasks/{task_id}/event-ingress/snapshot?{urlencode(query)}",
            timeout=30,
        ) as response:
            payload = json.load(response)
        page_frames = payload.get("frames")
        if not isinstance(page_frames, list):
            raise AssertionError("event ingress snapshot omitted frames")
        frames += len(page_frames)
        for frame in page_frames:
            if isinstance(frame, dict) and isinstance(frame.get("eventType"), str):
                event_type = str(frame["eventType"])
                event_types[event_type] = event_types.get(event_type, 0) + 1
        next_sequence = int(payload.get("nextSequence") or next_sequence)
        cursor = payload.get("cursor") if isinstance(payload.get("cursor"), str) else cursor
        if payload.get("hasMore") is not True:
            if payload.get("complete") is not True:
                raise AssertionError("event ingress snapshot stopped before reaching a complete delta cursor")
            return {
                "generation": generation,
                "frame_count": frames,
                "next_sequence": next_sequence,
                "event_type_counts": dict(sorted(event_types.items())),
            }
    raise AssertionError("event ingress snapshot exceeded the bounded page budget")


def _attach_cycle(
    cycle: int,
    base_url: str,
    task_id: str,
    workspace: Path,
    resize_count: int,
    timeout: float,
    state_dir: Path,
    control_command: str | None,
    wait_terminal: bool,
    task_timeout: float,
) -> AttachResult:
    command = subprocess.list2cmdline([
        "node",
        str(ROOT / "apps" / "cli" / "dist" / "zyra.js"),
        "resume",
        task_id,
        "--base-url",
        base_url,
        "--startup-timeout",
        "300000ms",
        "--timeout",
        "0",
    ])
    environment = dict(os.environ)
    environment["ZYRA_SKIP_ONBOARDING"] = "1"
    environment["ZYRA_CLI_STATE_DIR"] = str(state_dir)
    environment["ZYRA_STATE_DIR"] = str(state_dir)
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
        _wait_for(capture, ">_ Zyra", timeout)
        _wait_for(capture, task_id, timeout)
        _wait_for(capture, "Tab 排队 · Esc 中断", timeout)
        startup_ms = (time.monotonic() - started) * 1_000
        control_receipt = None
        if control_command is not None:
            command_name = _receipt_command_name(control_command)
            _type_command(process, control_command)
            control_receipt = _wait_for_control_receipt(capture, command_name, timeout)
        _type_command(process, "/status")
        reported_status = _wait_for_reported_status(capture, task_id, timeout)
        for index in range(resize_count):
            process.resize(18 + (index % 43), 60 + (index % 141))
            if index % 25 == 0:
                time.sleep(0.005)
        canonical = _wait_for_canonical_terminal(process, base_url, task_id, task_timeout) \
            if wait_terminal else _canonical_task(base_url, task_id)
        canonical_status = str(canonical.get("status") or "unknown")
        if wait_terminal:
            _request_status_until(process, capture, task_id, canonical_status, timeout)
        detach_started = time.monotonic()
        _type_command(process, "/exit")
        exit_code = process.wait(timeout=timeout)
        detach_ms = (time.monotonic() - detach_started) * 1_000
        reader.join(timeout=3)
        material = capture.tail()
        visible = _visible(capture)
        result = AttachResult(
            cycle=cycle,
            pid=process.pid,
            startup_ms=round(startup_ms, 3),
            detach_ms=round(detach_ms, 3),
            exit_code=exit_code,
            resize_count=resize_count,
            output_bytes=capture.total_bytes,
            task_identity_visible=task_id in visible,
            reported_status=reported_status,
            control_command=control_command,
            control_receipt=control_receipt,
            waited_for_terminal=wait_terminal,
            canonical_status_at_detach=canonical_status,
            developer_event_flood_visible=DEVELOPER_EVENT_FLOOD.search(material) is not None,
            bracketed_paste_enabled=b"\x1b[?2004h" in material,
            bracketed_paste_disabled=b"\x1b[?2004l" in material,
            alternate_screen_used=b"\x1b[?1049" in material,
        )
        if (
            result.exit_code != 0
            or not result.task_identity_visible
            or result.reported_status not in {"pending", "running", "paused", "completed", "failed", "blocked", "cancelled", "killed", "interrupted"}
            or (result.waited_for_terminal and result.canonical_status_at_detach not in TERMINAL_STATUSES)
            or (result.control_command is not None and result.control_receipt is None)
            or result.developer_event_flood_visible
            or result.bracketed_paste_enabled
            or not result.bracketed_paste_disabled
            or result.alternate_screen_used
        ):
            raise AssertionError(f"product TUI attach gate failed: {asdict(result)}\n{visible[-2_000:]}")
        return result
    finally:
        if process.poll() is None:
            process.terminate_tree(grace_seconds=0.5)
        process.close()
        reader.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--resizes", type=int, default=250)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--task-timeout", type=float, default=7_200.0)
    parser.add_argument(
        "--wait-terminal-cycle",
        type=int,
        choices=(0, 1, 2),
        default=2,
        help="attach cycle that remains active until canonical settlement; 0 disables",
    )
    parser.add_argument(
        "--control-command",
        help="submit one receipt-producing slash mutation during the selected attach cycle",
    )
    parser.add_argument("--control-cycle", type=int, choices=(1, 2), default=1)
    arguments = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("phase_g_conpty_observer.py requires Windows")
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
    report = arguments.report.resolve(strict=False)
    if not 0 <= arguments.resizes <= 10_000:
        raise SystemExit("--resizes must be between 0 and 10000")
    if arguments.timeout <= 0 or arguments.task_timeout <= 0:
        raise SystemExit("--timeout and --task-timeout must be positive")
    if report.exists():
        raise SystemExit("--report must not overwrite an existing file")
    control_command = arguments.control_command.strip() if arguments.control_command else None
    if control_command is not None and (not control_command.startswith("/") or "\n" in control_command or "\r" in control_command):
        raise SystemExit("--control-command must be one single-line slash command")
    if control_command is not None:
        try:
            _receipt_command_name(control_command)
        except ValueError as error:
            raise SystemExit(str(error)) from error
    report.parent.mkdir(parents=True, exist_ok=True)
    before = _canonical_task(arguments.base_url, arguments.task_id)
    with tempfile.TemporaryDirectory(prefix="zyra-phase-g-state-", dir=ROOT / ".tmp") as state:
        cycles = [
            _attach_cycle(
                index,
                arguments.base_url,
                arguments.task_id,
                workspace,
                arguments.resizes,
                arguments.timeout,
                Path(state),
                control_command if index == arguments.control_cycle else None,
                arguments.wait_terminal_cycle == index,
                arguments.task_timeout,
            )
            for index in (1, 2)
        ]
    after = _canonical_task(arguments.base_url, arguments.task_id)
    if before.get("run_id") != after.get("run_id"):
        raise AssertionError("detach/resume changed the canonical run identity")
    payload = {
        "schema": "zyra.product-tui-phase-g-observer/v1",
        "recorded_at": _utc_now(),
        "source_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, text=True, capture_output=True
        ).stdout.strip(),
        "task_id": arguments.task_id,
        "run_id": before.get("run_id"),
        "canonical_before": {"status": before.get("status"), "terminal": before.get("terminal")},
        "canonical_after": {"status": after.get("status"), "terminal": after.get("terminal")},
        "control": {
            "cycle": arguments.control_cycle if control_command is not None else None,
            "command": control_command,
            "receipt_observed": any(item.control_receipt is not None for item in cycles),
        },
        "ingress": _ingress_statistics(arguments.base_url, arguments.task_id),
        "cycles": [asdict(item) for item in cycles],
        "all_passed": True,
    }
    with report.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
