"""Record a secret-free real ConPTY baseline for the local Codex reference binary."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Callable

from zyra_workers.terminal import PtySpawnOptions, spawn_pty


ROOT = Path(__file__).resolve().parents[2]
ANSI = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", re.DOTALL)
MAX_CAPTURE_BYTES = 4 * 1024 * 1024


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


def _wait_until(predicate: Callable[[], bool], timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError(message)
        time.sleep(0.02)


def _type_command(process: object, command: str) -> None:
    for character in command:
        process.write(character.encode("utf-8"))  # type: ignore[attr-defined]
        time.sleep(0.02)
    time.sleep(0.15)
    process.write(b"\r")  # type: ignore[attr-defined]


def _diagnostic_markers(capture: RollingCapture) -> dict[str, bool]:
    visible = _visible(capture)
    lowered = visible.lower()
    return {
        "codex_brand": "codex" in lowered,
        "composer_prompt": "ask codex" in lowered or "›" in visible,
        "trust_prompt": "do you trust" in lowered or "trust this" in lowered,
        "login_prompt": "sign in" in lowered or "log in" in lowered,
        "update_prompt": "update available" in lowered,
        "migration_prompt": "migration" in lowered or "migrate" in lowered,
        "status_command_echoed": "/status" in visible,
        "model_field": "Model" in visible,
        "directory_field": "Directory" in visible,
        "permissions_field": "Permissions" in visible,
        "replacement_character": "�" in visible,
    }


def _reference_commit(workspace: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _write_report(report: Path, payload: dict[str, object]) -> None:
    with report.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-exe", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    arguments = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("codex_reference_conpty_probe.py requires Windows")
    executable = arguments.codex_exe.resolve(strict=True)
    workspace = arguments.workspace.resolve(strict=True)
    report = arguments.report.resolve(strict=False)
    if executable.suffix.lower() != ".exe":
        raise SystemExit("--codex-exe must identify a Windows executable")
    if report.exists():
        raise SystemExit("--report must not overwrite an existing file")
    report.parent.mkdir(parents=True, exist_ok=True)

    version = subprocess.run(
        [str(executable), "--version"],
        cwd=workspace,
        check=True,
        text=True,
        capture_output=True,
        timeout=arguments.timeout,
    ).stdout.strip()
    command = subprocess.list2cmdline([
        str(executable),
        "--no-alt-screen",
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
        "--cd",
        str(workspace),
    ])
    process = spawn_pty(PtySpawnOptions(
        command=command,
        cwd=workspace,
        shell=os.environ.get("COMSPEC", "cmd.exe"),
        rows=32,
        cols=100,
        environment=dict(os.environ),
    ))
    capture = RollingCapture()
    reader = threading.Thread(target=_reader, args=(process, capture), daemon=True)
    reader.start()
    started = time.monotonic()
    try:
        _wait_until(
            lambda: capture.total_bytes >= 100,
            arguments.timeout,
            "Codex TUI did not produce startup output",
        )
        time.sleep(1.0)
        startup_ms = round((time.monotonic() - started) * 1_000, 3)
        startup_markers = _diagnostic_markers(capture)
        if startup_markers["login_prompt"]:
            process.write(b"\x03")
            try:
                exit_code = process.wait(timeout=10)
            except TimeoutError:
                process.write(b"\x03")
                exit_code = process.wait(timeout=10)
            reader.join(timeout=3)
            material = capture.tail()
            payload: dict[str, object] = {
                "schema": "zyra.codex-reference-conpty-baseline/v1",
                "recorded_at": datetime.now(UTC).isoformat(),
                "outcome": "authentication_required",
                "codex_version": version,
                "codex_executable_name": executable.name,
                "reference_repo_commit": _reference_commit(workspace),
                "terminal": {"rows": 32, "columns": 100, "inline_requested": True},
                "interaction": {
                    "startup_ms": startup_ms,
                    "login_surface_visible": True,
                    "session_commands_exercised": [],
                    "output_bytes": capture.total_bytes,
                    "exit_code": exit_code,
                },
                "terminal_modes": {
                    "alternate_screen_used": b"\x1b[?1049" in material,
                    "bracketed_paste_enabled": b"\x1b[?2004h" in material,
                    "bracketed_paste_disabled": b"\x1b[?2004l" in material,
                    "cursor_restored": b"\x1b[?25h" in material,
                },
                "raw_transcript_persisted": False,
                "all_passed": False,
            }
            _write_report(report, payload)
            return 2
        status_offset = capture.total_bytes
        try:
            _type_command(process, "/status")
        except Exception as error:
            raise RuntimeError(
                f"Codex exited before /status; secret-free markers="
                f"{json.dumps(_diagnostic_markers(capture), sort_keys=True)}; "
                f"output_bytes={capture.total_bytes}; exit_code={process.poll()}"
            ) from error
        try:
            _wait_until(
                lambda: capture.total_bytes > status_offset
                and all(label in _visible(capture) for label in ("Model", "Directory", "Permissions")),
                arguments.timeout,
                "Codex /status did not expose the expected reference fields",
            )
        except TimeoutError as error:
            raise TimeoutError(
                f"{error}; secret-free markers={json.dumps(_diagnostic_markers(capture), sort_keys=True)}; "
                f"output_bytes={capture.total_bytes}"
            ) from error
        _type_command(process, "/exit")
        exit_code = process.wait(timeout=arguments.timeout)
        reader.join(timeout=3)
        material = capture.tail()
        payload = {
            "schema": "zyra.codex-reference-conpty-baseline/v1",
            "recorded_at": datetime.now(UTC).isoformat(),
            "codex_version": version,
            "codex_executable_name": executable.name,
            "reference_repo_commit": _reference_commit(workspace),
            "terminal": {"rows": 32, "columns": 100, "inline_requested": True},
            "interaction": {
                "startup_ms": startup_ms,
                "commands": ["/status", "/exit"],
                "status_fields_visible": ["Model", "Directory", "Permissions"],
                "output_bytes": capture.total_bytes,
                "exit_code": exit_code,
            },
            "terminal_modes": {
                "alternate_screen_used": b"\x1b[?1049" in material,
                "bracketed_paste_enabled": b"\x1b[?2004h" in material,
                "bracketed_paste_disabled": b"\x1b[?2004l" in material,
                "cursor_restored": b"\x1b[?25h" in material,
            },
            "raw_transcript_persisted": False,
        }
        payload["all_passed"] = bool(
            exit_code == 0
            and not payload["terminal_modes"]["alternate_screen_used"]
            and payload["terminal_modes"]["cursor_restored"]
        )
        _write_report(report, payload)
        return 0 if payload["all_passed"] else 1
    finally:
        if process.poll() is None:
            process.terminate_tree(grace_seconds=0.5)
        process.close()
        reader.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
