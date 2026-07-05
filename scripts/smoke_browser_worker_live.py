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
    smoke_dir = paths.root / "worker-smoke"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    page = smoke_dir / "index.html"
    page.write_text(
        "<html><head><title>Zyra Worker Live Smoke</title></head>"
        "<body>"
        "<h1>Zyra browser worker live smoke</h1>"
        "<input id='q' name='q' aria-label='query'>"
        "<button id='run' onclick=\"const v=document.querySelector('#q').value;"
        "document.body.insertAdjacentHTML('beforeend','<p id=result>runtime fixture loaded '+v+'</p>')\">Run</button>"
        "<div style='height:1600px'>scroll target area</div>"
        "</body></html>",
        encoding="utf-8",
    )
    target = smoke_dir / "target.html"
    target.write_text(
        "<html><head><title>Zyra Worker Live Target</title></head><body>temporary target page</body></html>",
        encoding="utf-8",
    )

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietStaticHandler, directory=str(smoke_dir)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/{page.name}"
    target_url = f"http://127.0.0.1:{server.server_address[1]}/{target.name}"
    try:
        state = create_task_state("Smoke browser-use live worker backend.")
        run = BrowserWorkerRuntime(
            project_root=ROOT,
            workspace_root=paths.root / "worker-workspace",
            artifact_root=paths.root / "worker-artifacts",
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
                        {"action": "input_text", "arguments": {"index": 5, "text": "zyra live input"}},
                        {"action": "click_element", "arguments": {"index": 6}},
                        {
                            "action": "evaluate_js",
                            "arguments": {
                                "code": (
                                    "(function(){document.body.insertAdjacentHTML('beforeend',"
                                    "'<p id=\"eval\">zyra evaluated marker</p>');"
                                    "return document.querySelector('#eval').textContent;})()"
                                )
                            },
                        },
                        {"action": "take_screenshot", "arguments": {"full_page": False}},
                        {
                            "action": "save_as_pdf",
                            "arguments": {"paper_format": "Letter", "display_header_footer": False},
                        },
                        {"action": "wait", "arguments": {"seconds": 1}},
                        {"action": "scroll_page", "arguments": {"pages": 0.5}},
                        {"action": "scroll_to_text", "arguments": {"text": "scroll target area"}},
                        {"action": "send_keys", "arguments": {"keys": "Escape"}},
                        {"action": "search_page", "arguments": {"pattern": "zyra evaluated marker"}},
                        {"action": "open_url", "arguments": {"url": target_url}},
                        {"action": "go_back"},
                        {"action": "search_page", "arguments": {"pattern": "Zyra browser worker live smoke"}},
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
        matches = [
            event.payload.get("browser_result", {}).get("output", {}).get("match_count")
            for event in run.event_records
        ]
        raise SystemExit(0 if 1 in matches else 3)
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
        "match_count": output.get("match_count"),
        "screenshot_size_bytes": output.get("screenshot_size_bytes"),
        "pdf_size_bytes": output.get("pdf_size_bytes"),
        "browser_use_action_result": {
            "error": action_result.get("error"),
            "extracted_content": action_result.get("extracted_content"),
        },
        "url": output.get("url"),
        "selector_count": output.get("browser_use_selector_count"),
        "interactive_elements": output.get("browser_use_interactive_elements", [])[:5],
    }


if __name__ == "__main__":
    main()
