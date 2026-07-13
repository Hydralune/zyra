from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package_path in sorted((ROOT / "packages").iterdir()):
    if package_path.is_dir() and str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import create_task_state, to_jsonable  # noqa: E402
from zyra_runtime import WorkerRequest  # noqa: E402
from zyra_workers import BrowserWorkerRuntime  # noqa: E402


class _CdpHandler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _write(self, value: object) -> None:
        data = json.dumps(value).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/json/version":
            self._write({
                "Browser": "ZyraProductizedSmoke/1",
                "Protocol-Version": "1.3",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9/devtools/browser/productized-smoke",
            })
        elif self.path == "/json/list":
            self._write([{"id": "smoke-page", "type": "page", "url": "about:blank", "title": "Smoke"}])
        elif self.path.startswith("/json/activate/"):
            self._write("Target activated")
        else:
            self.send_error(404)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="zyra-browser-productized-smoke-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CdpHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            state = create_task_state("Exercise the productized browser worker smoke.")
            worker = BrowserWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=root / "artifacts",
                permission_state_path=root / "permission-state.json",
            )
            run = worker.run(WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="BrowserWorker",
                constraints={
                    "browser_lifecycle_command": "start",
                    "browser_endpoint_url": endpoint,
                    "browser_transport": "memory",
                    "canonical_session_id": "productized-smoke",
                    "keep_alive": True,
                    "permission_mode": "sealed",
                },
            ))
            session_id = str(run.worker_result.metadata.get("browser_session_id") or "")
            stopped = worker.run(WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="BrowserWorker",
                constraints={
                    "browser_lifecycle_command": "cancel",
                    "browser_session_id": session_id,
                    "canonical_session_id": "productized-smoke",
                    "reason": "smoke_complete",
                },
            ))
            payload = {
                "ok": run.worker_result.ok and stopped.worker_result.ok,
                "run_ok": run.worker_result.ok,
                "run_summary": run.worker_result.summary,
                "run_error": run.worker_result.error,
                "run_metadata": dict(run.worker_result.metadata),
                "backend": run.worker_result.metadata.get("browser_backend"),
                "session_id": session_id,
                "session_revision": run.worker_result.metadata.get("browser_session_revision"),
                "capsule_fingerprint": run.worker_result.metadata.get("browser_resume_capsule_fingerprint"),
                "receipt_count": run.worker_result.metadata.get("browser_receipt_count"),
                "artifacts": [to_jsonable(item) for item in run.worker_result.artifacts],
                "event_count": len(run.event_records),
                "stop": to_jsonable(stopped.worker_result),
                "source_repository_dependency": False,
                "legacy_backend_counted": False,
            }
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0 if (
                payload["ok"]
                and session_id
                and payload["capsule_fingerprint"]
                and payload["event_count"]
            ) else 2
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    raise SystemExit(main())
