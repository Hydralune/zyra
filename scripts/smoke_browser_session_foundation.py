from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package_path in sorted((ROOT / "packages").iterdir()):
    if not package_path.is_dir():
        continue
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_workers.browser_session import BrowserRuntime, BrowserRuntimeConfig, BrowserSessionCommand


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/json/version":
            value: object = {"Browser": "ZyraSmoke/1", "Protocol-Version": "1.3", "webSocketDebuggerUrl": "ws://127.0.0.1:9/devtools/browser/smoke"}
        elif self.path == "/json/list":
            value = [{"id": "smoke-page", "type": "page", "url": "about:blank", "title": "Smoke"}]
        elif self.path.startswith("/json/activate/"):
            value = "Target activated"
        else:
            self.send_error(404)
            return
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Zyra-owned browser session foundation smoke.")
    parser.add_argument("--root", type=Path, help="Persistent smoke root; default is temporary.")
    args = parser.parse_args()
    temporary = tempfile.TemporaryDirectory(prefix="zyra-browser-session-smoke-") if args.root is None else None
    root = (Path(temporary.name) if temporary else args.root).resolve()  # type: ignore[union-attr]
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        workspace = root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        runtime = BrowserRuntime(BrowserRuntimeConfig(root / "state", root / "runtime", root / "artifacts", request_timeout_seconds=0.2))
        command = BrowserSessionCommand(
            run_id="run-smoke", task_id="task-smoke", worker_request_id="request-smoke",
            canonical_session_id="canonical-smoke", node_id="browser-smoke",
            workspace_root=workspace, artifact_root=root / "artifacts", endpoint_url=endpoint,
        )
        started = runtime.start(command)
        replay = runtime.ensure_started(command)
        diagnostic = runtime.diagnose(started.session.session_id)
        stopped = runtime.stop(started.session.session_id, reason="smoke_complete")
        payload = {
            "ok": started.ok and replay.reused and diagnostic.cdp_connected and stopped.ok,
            "runtime": runtime.metadata(),
            "started": started.to_dict(),
            "replay_reused": replay.reused,
            "diagnostic": diagnostic.to_dict(),
            "stopped": stopped.to_dict(),
            "source_repository_dependency": False,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if payload["ok"] else 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
