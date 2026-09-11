"""One-click local launch for the Zyra API + Web workbench.

Starts the Python control plane (``scripts/dev_api.py``) on
``http://127.0.0.1:8000``, waits until it reports healthy, then starts the Web
workbench (``scripts/dev_web.py``) on ``http://127.0.0.1:5173`` proxying
``/api/*`` to the API.  Ctrl+C stops both processes.

Environment overrides (same as the individual launchers):
    ZYRA_API_HOST / ZYRA_API_PORT   API bind address (default 127.0.0.1:8000)
    ZYRA_WEB_HOST / ZYRA_WEB_PORT   Web bind address (default 127.0.0.1:5173)

Run from the repository root:
    python scripts/dev_up.py
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

API_HOST = os.environ.get("ZYRA_API_HOST", "127.0.0.1")
API_PORT = int(os.environ.get("ZYRA_API_PORT", "8000"))
WEB_HOST = os.environ.get("ZYRA_WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("ZYRA_WEB_PORT", "5173"))
API_ORIGIN = f"http://{API_HOST}:{API_PORT}"
WEB_URL = f"http://{WEB_HOST}:{WEB_PORT}"

if os.name == "nt":
    _NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
else:
    _NEW_GROUP = 0

_processes: list[subprocess.Popen[str]] = []


def _start(name: str, script: str, env: dict[str, str]) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / script)],
        cwd=str(ROOT),
        env=env,
        creationflags=_NEW_GROUP,
    )
    _processes.append(process)
    print(f"[dev_up] started {name} (pid {process.pid})")
    return process


def _get(host: str, port: int, path: str, timeout: float = 2.0) -> tuple[int, bytes]:
    """Fetch a loopback URL without honoring any system proxy.

    ``urllib.request`` reads the Windows registry proxy (e.g. a local
    127.0.0.1:7897 tunnel) and, absent a ``no_proxy`` entry, would route even a
    loopback health check through it and report the API as down.  Use a raw
    ``http.client`` connection instead, which never consults proxy settings.
    """

    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read(256 * 1024)
    finally:
        connection.close()


def _wait_http(host: str, port: int, path: str, timeout: float, *, predicate) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(process.poll() is not None for process in _processes):
            print("[dev_up] a child process exited early")
            return False
        try:
            status, body = _get(host, port, path)
            if predicate(status, body):
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def _api_healthy(status: int, body: bytes) -> bool:
    if status != 200:
        return False
    try:
        return json.loads(body).get("ok") is True
    except (ValueError, AttributeError):
        return False


def _stop(*_args) -> None:
    print("\n[dev_up] stopping children ...")
    for process in _processes:
        if process.poll() is None:
            try:
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.terminate()
            except OSError:
                pass
    for process in _processes:
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
    _processes.clear()


def main() -> int:
    web_index = ROOT / "apps" / "web" / "dist" / "index.html"
    if not web_index.is_file():
        print(
            "[dev_up] Web bundle missing: apps/web/dist/index.html. "
            "Run `bun run build:web` first.",
            file=sys.stderr,
        )
        return 2

    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    api_env = dict(os.environ)
    web_env = dict(os.environ)
    web_env["ZYRA_WEB_API_ORIGIN"] = API_ORIGIN

    _start("zyra-api", "dev_api.py", api_env)
    print(f"[dev_up] waiting for API at {API_ORIGIN} ...")
    if not _wait_http(API_HOST, API_PORT, "/health", timeout=90, predicate=_api_healthy):
        print("[dev_up] API did not become healthy in time.", file=sys.stderr)
        _stop()
        return 1

    _start("zyra-web", "dev_web.py", web_env)
    print(f"[dev_up] waiting for Web at {WEB_URL} ...")
    if not _wait_http(WEB_HOST, WEB_PORT, "/", timeout=30, predicate=lambda status, _body: status == 200):
        print("[dev_up] Web server did not become reachable in time.", file=sys.stderr)
        _stop()
        return 1

    print()
    print("Zyra is running:")
    print(f"  API   -> {API_ORIGIN}")
    print(f"  WebUI -> {WEB_URL}")
    print("Press Ctrl+C to stop both services.")
    try:
        while any(process.poll() is None for process in _processes):
            time.sleep(1)
    except KeyboardInterrupt:
        _stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
