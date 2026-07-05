from __future__ import annotations

import json
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import create_task_state, to_jsonable  # noqa: E402
from zyra_runtime import WorkerRequest  # noqa: E402
from zyra_workers import BrowserWorkerRuntime, configure_browser_use_environment  # noqa: E402


def main() -> None:
    paths = configure_browser_use_environment(ROOT)
    smoke_dir = paths.root / "worker-file-transfer-smoke"
    runtime_workspace = paths.root / "worker-file-transfer-workspace"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    runtime_workspace.mkdir(parents=True, exist_ok=True)

    (runtime_workspace / "upload.txt").write_text("zyra upload payload", encoding="utf-8")
    (smoke_dir / "download.txt").write_text("zyra download payload", encoding="utf-8")
    page = smoke_dir / "files.html"
    page.write_text(
        "<html><head><title>Zyra Worker File Transfer Smoke</title></head>"
        "<body>"
        "<h1>Zyra worker file transfer smoke</h1>"
        "<input id='upload' type='file' onchange=\"document.body.insertAdjacentHTML('beforeend',"
        "'<p id=uploaded>'+this.files[0].name+'</p>')\">"
        "<a id='download' href='download.txt' download='download.txt'>Download evidence</a>"
        "</body></html>",
        encoding="utf-8",
    )

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietStaticHandler, directory=str(smoke_dir)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/{page.name}"
    try:
        state = create_task_state("Smoke browser-use live file transfer.")
        run = BrowserWorkerRuntime(
            project_root=ROOT,
            workspace_root=runtime_workspace,
            artifact_root=paths.root / "worker-file-transfer-artifacts",
        ).run(
            WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="BrowserWorker",
                constraints={
                    "browser_backend": "browser-use-live",
                    "browser_plan": [
                        {"action": "open_url", "arguments": {"url": url}},
                        {"action": "upload_file", "arguments": {"id": "upload", "path": "upload.txt"}},
                        {"action": "search_page", "arguments": {"pattern": "upload.txt"}},
                        {
                            "action": "evaluate_js",
                            "arguments": {"code": "document.querySelector('#download').click(); 'download clicked'"},
                        },
                        {"action": "collect_downloads", "arguments": {"settle_seconds": 1}},
                    ],
                    "live_timeout_seconds": 90,
                },
            )
        )
        payload = {
            "ok": run.worker_result.ok,
            "summary": run.worker_result.summary,
            "error": run.worker_result.error,
            "metadata": run.worker_result.metadata,
            "artifact_count": len(run.worker_result.artifacts),
            "event_summaries": [_event_summary(event) for event in run.event_records if "browser_result" in event.payload],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if not run.worker_result.ok:
            raise SystemExit(2)
        download_counts = [
            event.payload.get("browser_result", {}).get("output", {}).get("download_count")
            for event in run.event_records
        ]
        raise SystemExit(0 if 1 in download_counts else 3)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError):
            return

    def log_message(self, format: str, *args: Any) -> None:
        return


def _event_summary(event: Any) -> dict[str, Any]:
    payload = to_jsonable(event.payload)
    action = payload.get("browser_action", {})
    result = payload.get("browser_result", {})
    output = result.get("output", {}) if isinstance(result, dict) else {}
    action_result = output.get("browser_use_action_result", {}) if isinstance(output, dict) else {}
    return {
        "step_index": action.get("step_index"),
        "action": action.get("action"),
        "source_action": action.get("source_action"),
        "ok": result.get("ok"),
        "summary": result.get("summary"),
        "download_count": output.get("download_count"),
        "downloaded_files": output.get("downloaded_files"),
        "browser_use_action_result": {
            "error": action_result.get("error"),
            "extracted_content": action_result.get("extracted_content"),
        },
        "selector_count": output.get("browser_use_selector_count"),
        "interactive_elements": output.get("browser_use_interactive_elements", [])[:5],
    }


if __name__ == "__main__":
    main()
