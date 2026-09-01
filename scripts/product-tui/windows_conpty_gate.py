"""Real Windows ConPTY lifecycle and resize gate for the built Zyra product TUI."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from zyra_workers.terminal import PtySpawnOptions, spawn_pty


ROOT = Path(__file__).resolve().parents[2]
ANSI = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", re.DOTALL)
MAX_CAPTURE_BYTES = 8 * 1024 * 1024


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

    def contains(self, value: bytes) -> bool:
        return value in self.tail()


@dataclass(frozen=True, slots=True)
class CycleResult:
    cycle: int
    pid: int
    startup_ms: float
    exit_ms: float
    exit_code: int
    resize_count: int
    output_bytes: int
    bracketed_paste_enabled: bool
    bracketed_paste_disabled: bool
    alternate_screen_used: bool
    onboarding_completed: bool


def _reader(process: object, capture: RollingCapture, completed: threading.Event) -> None:
    try:
        while True:
            chunk = process.read()  # type: ignore[attr-defined]
            if not chunk:
                return
            capture.append(chunk)
    finally:
        completed.set()


def _wait_for(capture: RollingCapture, marker: bytes, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not capture.contains(marker):
        if time.monotonic() >= deadline:
            visible = ANSI.sub(b"", capture.tail()).decode("utf-8", "replace")[-2_000:]
            raise TimeoutError(f"TUI marker {marker!r} was not observed. Tail:\n{visible}")
        time.sleep(0.01)


def _type_command(process: object, value: str) -> None:
    for character in value:
        process.write(character.encode("utf-8"))  # type: ignore[attr-defined]
        time.sleep(0.02)
    time.sleep(0.15)
    process.write(b"\r")  # type: ignore[attr-defined]


def run_cycle(
    cycle: int,
    base_url: str,
    resize_count: int,
    timeout: float,
    onboarding: bool,
) -> CycleResult:
    command = subprocess.list2cmdline([
        "node",
        "apps\\cli\\dist\\zyra.js",
        "--base-url",
        base_url,
        "--startup-timeout",
        "10000ms",
    ])
    temporary_state = tempfile.TemporaryDirectory(
        prefix="zyra-conpty-onboarding-",
        dir=ROOT / ".tmp",
    )
    environment = dict(os.environ)
    environment["ZYRA_CLI_STATE_DIR"] = temporary_state.name
    environment["ZYRA_STATE_DIR"] = temporary_state.name
    if onboarding:
        environment.pop("ZYRA_SKIP_ONBOARDING", None)
    else:
        environment["ZYRA_SKIP_ONBOARDING"] = "1"
    process = spawn_pty(PtySpawnOptions(
        command=command,
        cwd=ROOT,
        shell=os.environ.get("COMSPEC", "cmd.exe"),
        rows=32,
        cols=100,
        environment=environment,
    ))
    capture = RollingCapture()
    completed = threading.Event()
    reader = threading.Thread(
        target=_reader,
        args=(process, capture, completed),
        name=f"zyra-product-tui-gate-{cycle}",
        daemon=True,
    )
    reader.start()
    started = time.monotonic()
    try:
        _wait_for(capture, b">_ Zyra", timeout)
        startup_ms = (time.monotonic() - started) * 1_000
        onboarding_completed = False
        if onboarding:
            _wait_for(capture, "首次使用设置".encode(), timeout)
            process.write(b"\r")
            _wait_for(capture, "首次使用设置完成".encode(), timeout)
            state_path = Path(temporary_state.name) / "product-onboarding.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if (
                state.get("schema") != "zyra.product-onboarding/v1"
                or state.get("choice") != "automatic"
                or str(ROOT).lower() in state_path.read_text(encoding="utf-8").lower()
            ):
                raise AssertionError("first-use onboarding state is invalid or leaks the workspace")
            onboarding_completed = True
        for index in range(resize_count):
            rows = 18 + (index % 43)
            cols = 60 + (index % 141)
            process.resize(rows, cols)
        exit_started = time.monotonic()
        _type_command(process, "/exit")
        exit_code = process.wait(timeout=timeout)
        exit_ms = (time.monotonic() - exit_started) * 1_000
        completed.wait(timeout=2)
        material = capture.tail()
        result = CycleResult(
            cycle=cycle,
            pid=process.pid,
            startup_ms=round(startup_ms, 3),
            exit_ms=round(exit_ms, 3),
            exit_code=exit_code,
            resize_count=resize_count,
            output_bytes=capture.total_bytes,
            bracketed_paste_enabled=b"\x1b[?2004h" in material,
            bracketed_paste_disabled=b"\x1b[?2004l" in material,
            alternate_screen_used=b"\x1b[?1049" in material,
            onboarding_completed=onboarding_completed,
        )
        if result.exit_code != 0:
            visible = ANSI.sub(b"", material).decode("utf-8", "replace")[-4_000:]
            raise AssertionError(
                f"product TUI exited with {result.exit_code}. Tail:\n{visible}"
            )
        if result.bracketed_paste_enabled or not result.bracketed_paste_disabled:
            enabled_offset = material.find(b"\x1b[?2004h")
            excerpt = material[max(0, enabled_offset - 80):enabled_offset + 100]
            raise AssertionError(
                "Windows product TUI must keep persistent bracketed-paste mode disabled; "
                f"enabled={result.bracketed_paste_enabled}, disabled={result.bracketed_paste_disabled}, "
                f"excerpt={excerpt!r}"
            )
        if result.alternate_screen_used:
            raise AssertionError("product TUI unexpectedly entered alternate screen")
        return result
    finally:
        if process.poll() is None:
            process.terminate_tree(grace_seconds=0.5)
        process.close()
        reader.join(timeout=2)
        temporary_state.cleanup()


def percentile(values: list[float], fraction: float) -> float:
    selected = sorted(values)
    index = max(0, min(len(selected) - 1, int((len(selected) - 1) * fraction + 0.999_999)))
    return selected[index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--resizes", type=int, default=1_000)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--maximum-startup-p95-ms", type=float, default=2_000.0)
    parser.add_argument("--maximum-exit-p95-ms", type=float, default=2_000.0)
    parser.add_argument("--onboarding", action="store_true")
    arguments = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("windows_conpty_gate.py requires Windows")
    if not 1 <= arguments.cycles <= 100:
        raise SystemExit("--cycles must be between 1 and 100")
    if not 0 <= arguments.resizes <= 10_000:
        raise SystemExit("--resizes must be between 0 and 10000")
    parsed_url = urlsplit(arguments.base_url)
    if (
        parsed_url.scheme != "http"
        or parsed_url.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed_url.username
        or parsed_url.password
        or parsed_url.path not in {"", "/"}
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise SystemExit("--base-url must be a credential-free loopback HTTP origin")
    if not (ROOT / "apps" / "cli" / "dist" / "zyra.js").is_file():
        raise SystemExit("built CLI is missing; run bun run build:cli first")

    results = [
        run_cycle(
            index + 1,
            arguments.base_url,
            arguments.resizes,
            arguments.timeout,
            arguments.onboarding,
        )
        for index in range(arguments.cycles)
    ]
    startup_p95 = round(percentile([item.startup_ms for item in results], 0.95), 3)
    exit_p95 = round(percentile([item.exit_ms for item in results], 0.95), 3)
    all_passed = (
        startup_p95 <= arguments.maximum_startup_p95_ms
        and exit_p95 <= arguments.maximum_exit_p95_ms
    )
    report = {
        "schema": "zyra.product-tui-conpty-gate/v1",
        "platform": os.name,
        "base_url": arguments.base_url,
        "cycles": [asdict(item) for item in results],
        "summary": {
            "cycle_count": len(results),
            "resize_count": sum(item.resize_count for item in results),
            "startup_p95_ms": startup_p95,
            "maximum_startup_p95_ms": arguments.maximum_startup_p95_ms,
            "exit_p95_ms": exit_p95,
            "maximum_exit_p95_ms": arguments.maximum_exit_p95_ms,
            "all_passed": all_passed,
        },
    }
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
