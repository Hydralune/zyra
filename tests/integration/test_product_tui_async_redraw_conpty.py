from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
BUN = ROOT / "node_modules" / ".bin" / ("bun.exe" if os.name == "nt" else "bun")
FIXTURE = ROOT / "apps" / "cli" / "test" / "fixtures" / "product-async-redraw-process.ts"
CRASH_FIXTURE = ROOT / "apps" / "cli" / "test" / "fixtures" / "product-terminal-crash-process.ts"
ANSI = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", re.DOTALL)


class Capture:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = bytearray()

    def append(self, value: bytes) -> None:
        with self._lock:
            self._value.extend(value)

    def value(self) -> bytes:
        with self._lock:
            return bytes(self._value)


def _reader(process: object, capture: Capture) -> None:
    while True:
        chunk = process.read()  # type: ignore[attr-defined]
        if not chunk:
            return
        capture.append(chunk)


def _wait_for(capture: Capture, marker: bytes, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while marker not in capture.value():
        if time.monotonic() >= deadline:
            visible = ANSI.sub(b"", capture.value()).decode("utf-8", "replace")[-2_000:]
            raise TimeoutError(f"missing ConPTY marker {marker!r}:\n{visible}")
        time.sleep(0.01)


@pytest.mark.skipif(os.name != "nt", reason="Windows ConPTY evidence requires Windows")
def test_real_conpty_preserves_unicode_input_during_async_redraw_and_resize() -> None:
    from zyra_workers.terminal import PtySpawnOptions, spawn_pty

    command = subprocess.list2cmdline([str(BUN), str(FIXTURE)])
    process = spawn_pty(
        PtySpawnOptions(
            command=command,
            cwd=ROOT,
            shell=os.environ.get("COMSPEC", "cmd.exe"),
            rows=32,
            cols=100,
            environment=dict(os.environ),
        )
    )
    capture = Capture()
    reader = threading.Thread(target=_reader, args=(process, capture), daemon=True)
    reader.start()
    expected = "中文输入 é 👨‍👩‍👧‍👦 — ConPTY 异步重绘不丢字"
    try:
        _wait_for(capture, b"ZYRA_ASYNC_REDRAW_READY")
        for index, value in enumerate(expected.encode("utf-8")):
            process.write(bytes([value]))
            process.resize(18 + (index % 43), 60 + (index % 141))
            time.sleep(0.001)
        time.sleep(0.15)
        process.write(b"\r")
        _wait_for(capture, b"ZYRA_ASYNC_REDRAW_RESULT")
        exit_code = process.wait(timeout=30)
        reader.join(timeout=5)
        material = capture.value()
        visible = ANSI.sub(b"", material).decode("utf-8", "replace")
        match = re.search(r"ZYRA_ASYNC_REDRAW_RESULT (\{[^\r\n]+\})", visible)
        assert match is not None, visible[-2_000:]
        result = json.loads(match.group(1))

        assert exit_code == 0
        assert result["result"] == {"kind": "submit", "text": expected, "queue": False}
        assert result["eventUpdates"] >= 1_000
        assert result["diagnostics"]["requested"] > result["eventUpdates"]
        assert result["diagnostics"]["coalesced"] >= result["eventUpdates"] // 2
        assert result["diagnostics"]["writes"] < result["eventUpdates"] // 4
        assert "�" not in result["result"]["text"]
        assert b"\x1b[?2004h" not in material
        assert b"\x1b[?2004l" in material
        assert b"\x1b[?1049" not in material
    finally:
        if process.poll() is None:
            process.terminate_tree(grace_seconds=0.5)
        process.close()
        reader.join(timeout=2)


@pytest.mark.skipif(os.name != "nt", reason="Windows ConPTY evidence requires Windows")
def test_real_conpty_restores_terminal_state_after_uncaught_crash() -> None:
    from zyra_workers.terminal import PtySpawnOptions, spawn_pty

    command = subprocess.list2cmdline([str(BUN), str(CRASH_FIXTURE)])
    process = spawn_pty(
        PtySpawnOptions(
            command=command,
            cwd=ROOT,
            shell=os.environ.get("COMSPEC", "cmd.exe"),
            rows=24,
            cols=80,
            environment=dict(os.environ),
        )
    )
    capture = Capture()
    reader = threading.Thread(target=_reader, args=(process, capture), daemon=True)
    reader.start()
    try:
        _wait_for(capture, b"ZYRA_TERMINAL_CRASH_READY")
        exit_code = process.wait(timeout=30)
        reader.join(timeout=5)
        material = capture.value()
        visible = ANSI.sub(b"", material).decode("utf-8", "replace")

        assert exit_code != 0
        assert "controlled terminal crash" in visible
        ready = material.index(b"ZYRA_TERMINAL_CRASH_READY")
        assert b"\x1b[?2004h" not in material
        assert material.index(b"\x1b[?2004l") < ready
        assert material.index(b"\x1b[?2004l", ready) > ready
        assert material.index(b"\x1b[?25h", ready) > ready
        assert b"\x1b[?1049" not in material
    finally:
        if process.poll() is None:
            process.terminate_tree(grace_seconds=0.5)
        process.close()
        reader.join(timeout=2)


@pytest.mark.skipif(os.name != "nt", reason="Windows ConPTY evidence requires Windows")
def test_windows_conpty_100_force_kills_cannot_leave_persistent_terminal_modes() -> None:
    from zyra_workers.terminal import PtySpawnOptions, spawn_pty

    environment = dict(os.environ)
    environment["ZYRA_TEST_WAIT_FOR_FORCE_KILL"] = "1"
    command = subprocess.list2cmdline([str(BUN), str(CRASH_FIXTURE)])
    for _cycle in range(100):
        process = spawn_pty(
            PtySpawnOptions(
                command=command,
                cwd=ROOT,
                shell=os.environ.get("COMSPEC", "cmd.exe"),
                rows=24,
                cols=80,
                environment=environment,
            )
        )
        capture = Capture()
        reader = threading.Thread(target=_reader, args=(process, capture), daemon=True)
        reader.start()
        try:
            _wait_for(capture, b"ZYRA_TERMINAL_CRASH_READY")
            ready = capture.value().index(b"ZYRA_TERMINAL_CRASH_READY")
            process.terminate_tree(grace_seconds=0)
            process.wait(timeout=30)
            reader.join(timeout=5)
            material = capture.value()

            assert b"\x1b[?2004h" not in material
            assert material.index(b"\x1b[?2004l") < ready
            assert material.index(b"\x1b[?25h", ready) > ready
            assert b"\x1b[?1049" not in material
        finally:
            if process.poll() is None:
                process.terminate_tree(grace_seconds=0)
            process.close()
            reader.join(timeout=2)
