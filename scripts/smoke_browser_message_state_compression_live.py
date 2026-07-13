from __future__ import annotations

"""Live Chrome evidence for M1-S04B-02 browser context integration.

The smoke deliberately uses a real productized Chrome/CDP session.  The page
contains a large DOM, open shadow root, same-origin frame, and a site-isolated
cross-origin frame.  Success requires the normal BrowserWorker path to produce
low-entropy disclosure, artifacts, a live selector resolution, OOPIF identity,
and the default-off three-lane compression ablation when explicitly enabled.
"""

import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
for package_path in sorted((ROOT / "packages").iterdir()):
    if package_path.is_dir() and str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


from zyra_core import create_task_state, to_jsonable  # noqa: E402
from zyra_runtime import WorkerRequest  # noqa: E402
from zyra_workers import (  # noqa: E402
    BrowserRuntimeConfig,
    BrowserRuntimeRegistry,
    BrowserSessionCommand,
    BrowserWorkerRuntime,
    find_browser_executable,
)


def _large_page(port: int) -> str:
    rows = "".join(
        f'<section class="record" data-index="{index}"><h2>Record {index}</h2>'
        f'<button id="action-{index}">Select record {index}</button>'
        f'<p>stable evidence value {index % 37}</p></section>'
        for index in range(1800)
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Zyra 04B live state</title></head>
<body>
  <h1>Browser message state compression live evidence</h1>
  <div id="shadow-host"></div>
  <iframe id="same-frame" src="/same.html"></iframe>
  <iframe id="cross-frame" src="http://127.0.0.1:{port}/cross.html"></iframe>
  <main>{rows}</main>
  <script>
    const host = document.querySelector('#shadow-host');
    const root = host.attachShadow({{mode: 'open'}});
    root.innerHTML = '<button id="shadow-action">Shadow action</button><span>shadow evidence</span>';
  </script>
</body></html>"""


class _LivePageHandler(BaseHTTPRequestHandler):
    server_version = "Zyra04B02Live/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract.
        port = int(self.server.server_address[1])
        if self.path in {"/", "/index.html"}:
            body = _large_page(port)
        elif self.path == "/same.html":
            body = (
                "<!doctype html><html><body><button id='same-action'>"
                "Same-origin frame action</button><p>same frame evidence</p></body></html>"
            )
        elif self.path == "/cross.html":
            body = (
                "<!doctype html><html><body><button id='cross-action'>"
                "Cross-origin OOPIF action</button><p>cross frame evidence</p></body></html>"
            )
        else:
            self.send_error(404)
            return
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature.
        return


def _context_payloads(run: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    capture: dict[str, Any] = {}
    disclosure: dict[str, Any] = {}
    for event in run.event_records:
        payload = event.payload if isinstance(event.payload, dict) else dict(event.payload)
        if "browser_dom_state" in payload:
            capture = payload
        if "browser_context_disclosure" in payload:
            disclosure = payload
    return capture, disclosure


def main() -> int:
    executable = find_browser_executable()
    if executable is None:
        print(json.dumps({
            "ok": False,
            "blocking": True,
            "error": "browser_executable_not_found",
        }, sort_keys=True))
        return 2

    page_server = ThreadingHTTPServer(("127.0.0.1", 0), _LivePageHandler)
    page_server.daemon_threads = True
    page_thread = threading.Thread(target=page_server.serve_forever, daemon=True)
    page_thread.start()
    page_url = f"http://localhost:{page_server.server_address[1]}/index.html"

    try:
        with tempfile.TemporaryDirectory(
            prefix="zyra-browser-message-state-live-",
            ignore_cleanup_errors=True,
        ) as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            registry = BrowserRuntimeRegistry()
            config = BrowserRuntimeConfig(
                root / "browser-state",
                root / "browser-runtime",
                root / "artifacts",
                request_timeout_seconds=45.0,
                connect_timeout_seconds=45.0,
            )
            runtime = registry.get_or_create(config)
            worker = BrowserWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=root / "artifacts",
                permission_state_path=root / "permission-state.json",
                browser_runtime_registry=registry,
            )
            state = create_task_state("Validate live browser message state compression integration.")
            canonical_session_id = f"task:{state.task_id}"
            setup_request_id = "04b-02-live-setup"
            prepared = runtime.ensure_started(BrowserSessionCommand(
                run_id=state.run_id,
                task_id=state.task_id,
                worker_request_id=setup_request_id,
                canonical_session_id=canonical_session_id,
                node_id=state.root_node_id,
                workspace_root=workspace,
                artifact_root=root / "artifacts",
                executable_path=Path(executable),
                headless=True,
                keep_alive=True,
                constraints={
                    "browser_transport": "websocket",
                    "browser_allow_unsafe_sandbox_bypass": True,
                    "browser_args": ["--site-per-process"],
                },
            ))
            setup_cdp = runtime.cdp_runtime(prepared.session.session_id)
            setup_targets = runtime.target_runtime(prepared.session.session_id)
            setup_target_id = setup_targets.active_target_id
            setup_cdp_session_id = setup_targets.cdp_session(setup_target_id).cdp_session_id
            setup_cdp.send("Page.enable", {}, cdp_session_id=setup_cdp_session_id)
            setup_cdp.send(
                "Page.navigate",
                {"url": page_url},
                cdp_session_id=setup_cdp_session_id,
            )
            deadline = time.monotonic() + 30.0
            loaded = False
            while time.monotonic() < deadline:
                ready = setup_cdp.send(
                    "Runtime.evaluate",
                    {
                        "expression": (
                            "document.readyState === 'complete' && "
                            "document.querySelectorAll('.record').length >= 1800 && "
                            "document.querySelector('#shadow-host').shadowRoot !== null"
                        ),
                        "returnByValue": True,
                    },
                    cdp_session_id=setup_cdp_session_id,
                )
                result = ready.get("result") if isinstance(ready, dict) else None
                if isinstance(result, dict) and result.get("value") is True:
                    loaded = True
                    break
                time.sleep(0.1)
            if not loaded:
                raise RuntimeError("live browser page did not reach the expected DOM state")
            # Allow Target.attachedToTarget events for the site-isolated iframe
            # to settle before BrowserWorker takes its authoritative capture.
            time.sleep(0.5)
            setup_targets.reconcile(recover_focus=False)
            for target in setup_targets.snapshot().targets:
                if target.target_type not in {"iframe", "frame"}:
                    continue
                try:
                    setup_targets.cdp_session(target.target_id)
                except Exception:
                    setup_cdp.send("Target.attachToTarget", {
                        "targetId": target.target_id,
                        "flatten": True,
                    })
                    setup_targets.wait_for_cdp_session(target.target_id, timeout=5.0)
            run = worker.run(WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="BrowserWorker",
                constraints={
                    "browser_transport": "websocket",
                    "browser_session_id": prepared.session.session_id,
                    "browser_endpoint_url": prepared.session.endpoint_url,
                    "canonical_session_id": canonical_session_id,
                    "keep_alive": True,
                    "permission_mode": "sealed",
                    "permission_headless": True,
                    "allowed_schemes": ["http"],
                    "allowed_domains": ["localhost", "127.0.0.1"],
                    "browser_live_selector_probe_limit": 16,
                    "browser_context_ablation": True,
                    "browser_context_budget": {
                        "max_context_tokens": 20000,
                        "max_context_bytes": 65536,
                        "max_inline_selectors": 96,
                        "raw_externalize_bytes": 8192,
                    },
                    "browser_plan": [
                        {"action": "list_targets", "arguments": {}},
                        {"action": "capture_trace", "arguments": {"marker": "04b-02-live"}},
                    ],
                },
            ))
            session_id = str(run.worker_result.metadata.get("browser_session_id") or "")
            capture_payload, disclosure_payload = _context_payloads(run)
            dom_state = capture_payload.get("browser_dom_state") or {}
            frames = list(dom_state.get("frames") or [])
            probe = disclosure_payload.get("browser_live_selector_probe") or {}
            low_entropy = disclosure_payload.get("browser_low_entropy_metrics") or {}
            ablation = disclosure_payload.get("browser_context_ablation") or {}
            lanes = list(ablation.get("lanes") or [])
            lane_names = {str(item.get("lane") or "") for item in lanes}
            artifacts = list(run.worker_result.artifacts)
            oopif_frames = [item for item in frames if item.get("oopif")]
            cross_origin_frames = [item for item in frames if item.get("cross_origin")]
            causal_event = any(
                isinstance(event.payload, dict)
                and event.payload.get("browser_context_disclosure", {}).get("disclosure_id")
                and event.payload.get("cause_event_id")
                for event in run.event_records
            )
            checks = {
                "worker_ok": bool(run.worker_result.ok),
                "session_bound": bool(session_id),
                "large_dom": int(dom_state.get("metrics", {}).get("dom_nodes") or 0) >= 1800,
                "same_origin_frame": len(frames) >= 2,
                "cross_origin_frame": bool(cross_origin_frames),
                "oopif_frame": bool(oopif_frames),
                "selector_probe_attempted": int(probe.get("attempted") or 0) > 0,
                "selector_probe_resolved": int(probe.get("resolved") or 0) > 0,
                "selector_probe_complete": (
                    int(probe.get("resolved") or 0) + int(probe.get("failed") or 0)
                    == int(probe.get("attempted") or 0)
                ),
                "low_entropy_reduced": (
                    int(low_entropy.get("full_state_bytes") or 0)
                    > int(low_entropy.get("disclosure_bytes") or 0)
                    > 0
                ),
                "artifact_externalized": len(artifacts) >= 3,
                "ablation_three_lanes": lane_names == {
                    "full_dom_state",
                    "structured_disclosure",
                    "bitmap_frame_experimental",
                },
                "ablation_structured_default": ablation.get("default_lane") == "structured_disclosure",
                "causal_event_linked": causal_event,
            }
            ok = all(checks.values())
            cleanup: dict[str, Any] = {"attempted": bool(session_id), "ok": False, "error": ""}
            if session_id:
                try:
                    stopped = worker.run(WorkerRequest(
                        run_id=state.run_id,
                        task_id=state.task_id,
                        node_id=state.root_node_id,
                        worker_name="BrowserWorker",
                        constraints={
                            "browser_lifecycle_command": "cancel",
                            "browser_session_id": session_id,
                            "canonical_session_id": canonical_session_id,
                            "reason": "04b_02_live_smoke_complete",
                        },
                    ))
                    cleanup["ok"] = bool(stopped.worker_result.ok)
                    cleanup["error"] = str(stopped.worker_result.error or "")
                except Exception as error:  # noqa: BLE001 - emit cleanup evidence.
                    cleanup["error"] = f"{type(error).__name__}: {error}"
            runtime.stop_all(force=True, reason="04b_02_live_smoke_finally")
            ok = bool(ok and cleanup["ok"])
            output = {
                "ok": ok,
                "blocking": not ok,
                "checks": checks,
                "executable": str(executable),
                "page_url": page_url,
                "session_id": session_id,
                "frame_count": len(frames),
                "oopif_frame_count": len(oopif_frames),
                "cross_origin_frame_count": len(cross_origin_frames),
                "probe": {
                    key: probe.get(key)
                    for key in (
                        "revision_id",
                        "attempted",
                        "resolved",
                        "failed",
                        "target_count",
                        "frame_count",
                        "shadow_count",
                        "executable_coverage",
                        "valid",
                    )
                },
                "low_entropy": low_entropy,
                "ablation": {
                    "report_id": ablation.get("report_id"),
                    "default_lane": ablation.get("default_lane"),
                    "experimental_lane_enabled": ablation.get("experimental_lane_enabled"),
                    "structured_wins_tokens": ablation.get("structured_wins_tokens"),
                    "lanes": [{
                        key: lane.get(key)
                        for key in (
                            "lane",
                            "status",
                            "bytes",
                            "tokens",
                            "compression_ratio",
                            "fact_fidelity",
                            "selector_fidelity",
                            "executable_selector_coverage",
                            "ocr_fidelity",
                            "coordinate_fidelity",
                            "task_success",
                            "authoritative",
                        )
                    } for lane in lanes],
                },
                "artifact_ids": [item.artifact_id for item in artifacts],
                "worker_error": run.worker_result.error,
                "worker_metadata": dict(run.worker_result.metadata),
                "event_count": len(run.event_records),
                "cleanup": cleanup,
            }
            print(json.dumps(to_jsonable(output), ensure_ascii=False, sort_keys=True))
            return 0 if ok else 2
    finally:
        page_server.shutdown()
        page_server.server_close()
        page_thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
