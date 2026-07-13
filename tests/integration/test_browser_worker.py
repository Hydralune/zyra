from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import create_task_state
from zyra_runtime import WorkerRequest
from zyra_runtime.permission.custody import (
    PermissionSessionCustodyBinding,
    PermissionSessionCustodyStore,
)
from zyra_runtime.permission.models import (
    PermissionEffect,
    PermissionRuleRecord,
    PermissionRuleSource,
    PermissionScope,
    PermissionScopeKind,
)
from zyra_runtime.permission.store import PermissionStateStore
from zyra_workers import (
    BrowserWorkerRuntime,
    configure_browser_use_environment,
    default_browser_action_registry,
    find_browser_executable,
    inspect_browser_use_runtime,
)


class QuietStaticHandler(SimpleHTTPRequestHandler):
    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError):
            return

    def log_message(self, format: str, *args: object) -> None:
        return


def _browser_events(run: object) -> list[object]:
    return [
        event
        for event in run.event_records
        if isinstance(event.payload.get("browser_result"), dict)
    ]


def _preauthorize_browser_session(
    *,
    runtime: BrowserWorkerRuntime,
    run_id: str,
    task_id: str,
    session_id: str,
) -> dict[str, str]:
    state_store = PermissionStateStore(runtime.permission_state_path)
    custody = PermissionSessionCustodyStore(state_store).claim(
        PermissionSessionCustodyBinding(
            session_id=session_id,
            run_id=run_id,
            task_id=task_id,
            workspace_root=str(runtime.workspace_root),
        )
    )
    action_names = {
        str(item["action"])
        for item in runtime.action_registry.describe()["actions"]
    }
    action_names.add("browser_agent_task")
    for action_name in sorted(action_names):
        state_store.add_session_rule(
            session_id,
            PermissionRuleRecord(
                effect=PermissionEffect.ALLOW,
                source=PermissionRuleSource.SESSION,
                scope=PermissionScope(PermissionScopeKind.SESSION, session_id=session_id),
                namespace_pattern="browser",
                tool_pattern=action_name,
                reason="server-owned live-browser smoke authorization",
            ),
        )
    return {
        "permission_session_id": session_id,
        "permission_session_custody_token": custody.token,
    }


class BrowserWorkerTests(unittest.TestCase):
    def test_browser_use_runtime_health_uses_project_local_directories(self) -> None:
        health = inspect_browser_use_runtime(ROOT)

        self.assertTrue(health.environment_configured)
        self.assertEqual(health.paths.config_dir.relative_to(ROOT).parts[0], "tmp")
        self.assertEqual(health.paths.cache_dir.relative_to(ROOT).parts[0], "tmp")
        self.assertEqual(health.paths.temp_dir.relative_to(ROOT).parts[0], "tmp")
        if not health.importable:
            self.assertIn(health.error_type, {"ModuleNotFoundError", "ImportError"})
            self.assertTrue(health.error)
            return
        self.assertEqual(health.classes["BrowserSession"], "BrowserSession")
        self.assertEqual(health.classes["BrowserProfile"], "BrowserProfile")
        self.assertEqual(health.classes["Tools"], "Tools")
        self.assertEqual(health.classes["Agent"], "Agent")
        self.assertEqual(health.classes["AgentHistoryList"], "AgentHistoryList")
        self.assertEqual(health.classes["get_llm_by_name"], "get_llm_by_name")
        self.assertEqual(health.classes["UploadFileAction"], "UploadFileAction")
        self.assertEqual(health.classes["ScreenshotAction"], "ScreenshotAction")
        self.assertEqual(health.classes["SaveAsPdfAction"], "SaveAsPdfAction")

    def test_browser_action_registry_is_zyra_owned_and_source_checkout_independent(self) -> None:
        registry = default_browser_action_registry(ROOT)
        navigate = registry.get("navigate")
        description = registry.describe()
        actions = {action["name"] for action in description["source_registered_actions"]}

        self.assertIsNotNone(navigate)
        self.assertEqual(description["source"], "zyra-browser-action-foundation")
        self.assertFalse(description["filesystem_source_scan"])
        self.assertFalse(description["external_runtime_required"])
        self.assertEqual(description["source_models_path"], "")
        self.assertEqual(description["source_service_path"], "")
        self.assertEqual(navigate.action, "open_url")
        self.assertEqual(registry.get("click").action, "click_element")
        self.assertEqual(registry.get("input").action, "input_text")
        self.assertEqual(registry.get("search_page").action, "search_page")
        self.assertEqual(registry.get("scroll").action, "scroll_page")
        self.assertEqual(registry.get("evaluate").action, "evaluate_js")
        self.assertEqual(registry.get("javascript").action, "evaluate_js")
        self.assertEqual(registry.get("screenshot").action, "take_screenshot")
        self.assertEqual(registry.get("pdf").action, "save_as_pdf")
        self.assertEqual(registry.get("upload").action, "upload_file")
        self.assertEqual(registry.get("downloads").action, "collect_downloads")
        self.assertIn("navigate", actions)
        self.assertIn("extract", actions)
        self.assertIn("click", actions)
        self.assertIn("input", actions)
        self.assertIn("upload_file", actions)
        self.assertIn("search_page", actions)
        self.assertIn("go_back", actions)
        self.assertIn("scroll", actions)
        self.assertIn("send_keys", actions)
        self.assertIn("screenshot", actions)
        self.assertIn("save_as_pdf", actions)
        self.assertIn("evaluate", actions)
        self.assertEqual(
            registry.validate_plan([{"action": "navigate", "arguments": {}}])[0].reason,
            "missing_required_argument:url",
        )
        self.assertEqual(
            registry.validate_plan([{"action": "evaluate_js", "arguments": {}}])[0].reason,
            "missing_required_argument:code",
        )
        self.assertEqual(
            registry.validate_plan([{"action": "upload_file", "arguments": {"index": 5}}])[0].reason,
            "missing_required_argument:path",
        )

    def test_browser_worker_extracts_local_html_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Extract a browser page.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            page = workspace / "index.html"
            page.write_text(
                """
                <html>
                  <head><title>Zyra Browser Fixture</title></head>
                  <body>
                    <h1>Browser Worker</h1>
                    <p>Extract this visible content.</p>
                    <a href="https://example.test/docs">Docs</a>
                  </body>
                </html>
                """,
                encoding="utf-8",
            )
            runtime = BrowserWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="BrowserWorker",
                constraints={
                    "browser_backend": "static",
                    "browser_plan": [
                        {"action": "open_url", "arguments": {"url": page.resolve().as_uri()}},
                        {"action": "extract_text"},
                        {"action": "snapshot_state"},
                    ],
                    "allowed_schemes": ["file"],
                },
            )

            run = runtime.run(request)
            browser_events = _browser_events(run)

            self.assertTrue(run.worker_result.ok)
            self.assertEqual(len(browser_events), 3)
            self.assertEqual(run.worker_result.metadata["vendor"], "browser-use")
            self.assertEqual(run.worker_result.metadata["vendor_complete"], "true")
            self.assertEqual(run.worker_result.metadata["action_registry_source"], "zyra-browser-action-foundation")
            self.assertEqual(
                run.worker_result.metadata["browser_use_python_importable"],
                str(runtime.browser_use_health.importable).lower(),
            )
            self.assertEqual(run.worker_result.metadata["browser_use_environment_configured"], "true")
            self.assertEqual(browser_events[0].payload["browser_action"]["source_action"], "navigate")
            self.assertTrue(any(artifact.kind == "markdown" for artifact in run.worker_result.artifacts))
            self.assertTrue(any(artifact.kind == "structured_data" for artifact in run.worker_result.artifacts))
            extracted = browser_events[1].payload["browser_result"]["output"]["text_preview"]
            self.assertIn("Extract this visible content.", extracted)

    def test_browser_worker_clicks_link_inputs_text_and_searches_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Operate on a browser page.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            index = workspace / "index.html"
            target = workspace / "target.html"
            index.write_text(
                """
                <html>
                  <head><title>Index</title></head>
                  <body>
                    <a href="target.html">Open target</a>
                    <input name="query" />
                  </body>
                </html>
                """,
                encoding="utf-8",
            )
            target.write_text(
                "<html><head><title>Target</title></head><body><p>Needle evidence appears here.</p></body></html>",
                encoding="utf-8",
            )
            runtime = BrowserWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="BrowserWorker",
                constraints={
                    "browser_backend": "static",
                    "browser_plan": [
                        {"action": "open_url", "arguments": {"url": index.resolve().as_uri()}},
                        {"action": "input_text", "arguments": {"index": 0, "text": "needle"}},
                        {"action": "click_element", "arguments": {"index": 0}},
                        {"action": "search_page", "arguments": {"pattern": "Needle", "max_results": 5}},
                    ],
                    "allowed_schemes": ["file"],
                },
            )

            run = runtime.run(request)
            browser_events = _browser_events(run)

            self.assertTrue(run.worker_result.ok)
            self.assertEqual(len(browser_events), 4)
            self.assertEqual(browser_events[1].payload["browser_result"]["output"]["virtual_inputs"]["0"], "needle")
            self.assertEqual(browser_events[2].payload["browser_result"]["output"]["title"], "Target")
            self.assertEqual(browser_events[3].payload["browser_result"]["output"]["match_count"], 1)
            self.assertEqual(browser_events[3].payload["browser_action"]["source_action"], "search_page")

    def test_browser_worker_static_backend_reports_live_only_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Reject live-only browser action on static backend.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            runtime = BrowserWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="BrowserWorker",
                constraints={
                    "browser_backend": "static",
                    "browser_plan": [
                        {"action": "evaluate_js", "arguments": {"code": "1 + 1"}},
                    ],
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.event_records[0].payload["browser_result"]["error"], "browser_action_requires_live_backend")

    def test_browser_worker_agent_backend_fails_closed_before_model_or_browser_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Run browser-use Agent backend.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            runtime = BrowserWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="BrowserWorker",
                constraints={
                    "browser_backend": "browser-use-agent",
                    "agent_task": "Open https://example.com and report the page title.",
                    "browser_use_agent_provider": "openai",
                    "browser_use_agent_model": "gpt-4.1-mini",
                    "browser_use_agent_api_key_env": "ZYRA_BROWSER_USE_AGENT_MISSING_KEY",
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "browser_agent_per_action_gate_unavailable")
            self.assertEqual(run.worker_result.metadata["browser_backend"], "browser-use-agent")
            self.assertEqual(run.worker_result.metadata["browser_permission_action_execution_count"], "0")
            self.assertEqual(run.worker_result.metadata["browser_agent_per_action_gate_integrated"], "false")
            permission_kinds = [
                event.payload.get("query_session", {}).get("permission_runtime", {}).get("kind")
                for event in run.event_records
            ]
            self.assertIn("browser_action_permission_unavailable", permission_kinds)
            self.assertIn("recovery_input", permission_kinds)
            agent_events = [
                event for event in run.event_records if "browser_agent_result" in event.payload
            ]
            self.assertEqual(
                agent_events[0].payload["browser_agent_result"]["error"],
                "browser_agent_per_action_gate_unavailable",
            )
            self.assertTrue(any(artifact.kind == "trace" for artifact in run.worker_result.artifacts))

    def test_browser_worker_live_backend_operates_input_click_and_search(self) -> None:
        if find_browser_executable() is None:
            self.skipTest("Chrome or Edge executable is not available for browser-use live smoke.")
        health = inspect_browser_use_runtime(ROOT)
        if not health.importable:
            self.skipTest(f"browser_use Python runtime is not importable: {health.error}")

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = configure_browser_use_environment(ROOT)
            state = create_task_state("Operate on a live browser page.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            page = workspace / "index.html"
            page.write_text(
                "<html><head><title>Live Browser Fixture</title></head>"
                "<body>"
                "<h1>Live Browser Fixture</h1>"
                "<input id='q' name='q' aria-label='query'>"
                "<button id='run' onclick=\"const v=document.querySelector('#q').value;"
                "document.body.insertAdjacentHTML('beforeend','<p id=result>live result '+v+'</p>')\">Run</button>"
                "<div style='height:1600px'>scroll target area</div>"
                "</body></html>",
                encoding="utf-8",
            )
            target = workspace / "target.html"
            target.write_text(
                "<html><head><title>Live Browser Target</title></head><body>temporary target page</body></html>",
                encoding="utf-8",
            )
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                partial(QuietStaticHandler, directory=str(workspace)),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_address[1]}/{page.name}"
            target_url = f"http://127.0.0.1:{server.server_address[1]}/{target.name}"
            try:
                runtime = BrowserWorkerRuntime(
                    project_root=ROOT,
                    workspace_root=paths.root / "test-live-workspace",
                    artifact_root=Path(tmpdir) / "artifacts",
                )
                permission_constraints = _preauthorize_browser_session(
                    runtime=runtime,
                    run_id=state.run_id,
                    task_id=state.task_id,
                    session_id="browser-live-operations",
                )
                request = WorkerRequest(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    worker_name="BrowserWorker",
                    constraints={
                        "browser_backend": "browser-use-live",
                        "browser_plan": [
                            {"action": "open_url", "arguments": {"url": url}},
                            {"action": "input_text", "arguments": {"id": "q", "text": "zyra live test"}},
                            {"action": "snapshot_state"},
                            {"action": "click_element", "arguments": {"id": "run"}},
                            {
                                "action": "evaluate_js",
                                "arguments": {
                                    "code": (
                                        "(function(){const clicked=document.querySelector('#result')?.textContent;"
                                        "if(clicked!=='live result zyra live test'){throw new Error('click contract failed: '+clicked);};"
                                        "document.body.insertAdjacentHTML('beforeend',"
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
                            {"action": "search_page", "arguments": {"pattern": "Live Browser Fixture"}},
                        ],
                        "live_timeout_seconds": 90,
                        "live_navigation_timeout_seconds": 30,
                        "live_action_timeout_seconds": 30,
                        **permission_constraints,
                    },
                )

                run = runtime.run(request)
                browser_events = _browser_events(run)

                self.assertTrue(
                    run.worker_result.ok,
                    json.dumps(
                        {
                            "error": run.worker_result.error,
                            "metadata": run.worker_result.metadata,
                        },
                        sort_keys=True,
                    ),
                )
                self.assertEqual(run.worker_result.metadata["browser_backend"], "browser-use-live")
                self.assertEqual(run.worker_result.metadata["live_navigation_timeout_seconds"], "30")
                self.assertEqual(run.worker_result.metadata["live_action_timeout_seconds"], "30")
                self.assertEqual(len(browser_events), 15)
                self.assertEqual(
                    browser_events[1].payload["browser_result"]["output"]["browser_use_action_result"][
                        "extracted_content"
                    ],
                    "Typed 'zyra live test'",
                )
                self.assertEqual(
                    browser_events[4].payload["browser_result"]["output"]["browser_use_action_result"][
                        "extracted_content"
                    ],
                    "zyra evaluated marker",
                )
                selector_contract = {
                    item["attributes"].get("id"): item
                    for item in browser_events[2].payload["browser_result"]["output"][
                        "browser_use_interactive_elements"
                    ]
                }
                self.assertIn("q", selector_contract)
                self.assertIn("run", selector_contract)
                self.assertNotEqual(selector_contract["q"]["index"], selector_contract["run"]["index"])
                self.assertGreater(browser_events[5].payload["browser_result"]["output"]["screenshot_size_bytes"], 0)
                self.assertTrue(
                    any(artifact.kind == "screenshot" for artifact in run.worker_result.artifacts),
                    "expected screenshot artifact",
                )
                self.assertGreater(browser_events[6].payload["browser_result"]["output"]["pdf_size_bytes"], 0)
                self.assertTrue(
                    any(str(artifact.uri).lower().endswith(".pdf") for artifact in run.worker_result.artifacts),
                    "expected PDF artifact",
                )
                self.assertEqual(browser_events[11].payload["browser_result"]["output"]["match_count"], 1)
                self.assertEqual(browser_events[9].payload["browser_action"]["source_action"], "find_text")
                self.assertEqual(
                    browser_events[13].payload["browser_result"]["output"]["browser_use_action_result"][
                        "extracted_content"
                    ],
                    "Navigated back",
                )
                self.assertGreaterEqual(browser_events[14].payload["browser_result"]["output"]["match_count"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_browser_worker_live_backend_uploads_and_collects_downloads(self) -> None:
        if find_browser_executable() is None:
            self.skipTest("Chrome or Edge executable is not available for browser-use live smoke.")
        health = inspect_browser_use_runtime(ROOT)
        if not health.importable:
            self.skipTest(f"browser_use Python runtime is not importable: {health.error}")

        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Transfer files through a live browser page.")
            served_workspace = Path(tmpdir) / "served"
            served_workspace.mkdir()
            runtime_workspace = Path(tmpdir) / "runtime-workspace"
            runtime_workspace.mkdir()
            upload_file = runtime_workspace / "upload.txt"
            upload_file.write_text("zyra upload payload", encoding="utf-8")
            download_file = served_workspace / "download.txt"
            download_file.write_text("zyra download payload", encoding="utf-8")
            page = served_workspace / "files.html"
            page.write_text(
                "<html><head><title>Live Browser File Fixture</title></head>"
                "<body>"
                "<h1>Live Browser File Fixture</h1>"
                "<input id='upload' type='file' onchange=\"document.body.insertAdjacentHTML('beforeend',"
                "'<p id=uploaded>'+this.files[0].name+'</p>')\">"
                "<a id='download' href='download.txt' download='download.txt'>Download evidence</a>"
                "</body></html>",
                encoding="utf-8",
            )
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                partial(QuietStaticHandler, directory=str(served_workspace)),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_address[1]}/{page.name}"
            try:
                runtime = BrowserWorkerRuntime(
                    project_root=ROOT,
                    workspace_root=runtime_workspace,
                    artifact_root=Path(tmpdir) / "artifacts",
                )
                permission_constraints = _preauthorize_browser_session(
                    runtime=runtime,
                    run_id=state.run_id,
                    task_id=state.task_id,
                    session_id="browser-live-file-transfer",
                )
                request = WorkerRequest(
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
                        **permission_constraints,
                    },
                )

                run = runtime.run(request)
                browser_events = _browser_events(run)

                self.assertTrue(run.worker_result.ok, run.worker_result.error)
                self.assertEqual(len(browser_events), 5)
                self.assertTrue(
                    browser_events[1].payload["browser_result"]["output"]["browser_use_action_result"][
                        "extracted_content"
                    ].startswith("Successfully uploaded file to index"),
                )
                self.assertEqual(browser_events[2].payload["browser_result"]["output"]["match_count"], 1)
                interactive_ids = {
                    item["attributes"].get("id")
                    for item in browser_events[0].payload["browser_result"]["output"][
                        "browser_use_interactive_elements"
                    ]
                }
                self.assertIn("upload", interactive_ids)
                self.assertIn("download", interactive_ids)
                self.assertEqual(
                    browser_events[3].payload["browser_result"]["output"]["browser_use_action_result"][
                        "extracted_content"
                    ],
                    "download clicked",
                )
                download_output = browser_events[4].payload["browser_result"]["output"]
                self.assertEqual(download_output["download_count"], 1)
                self.assertEqual(download_output["downloaded_files"][0]["name"], "download.txt")
                self.assertTrue(
                    any(
                        artifact.metadata.get("downloaded_file_name") == "download.txt"
                        for artifact in run.worker_result.artifacts
                    ),
                    "expected downloaded file artifact",
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_browser_worker_rejects_invalid_plan_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Reject invalid browser plan.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            runtime = BrowserWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="BrowserWorker",
                constraints={"browser_backend": "static", "browser_plan": [{"action": "navigate", "arguments": {}}]},
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "invalid_browser_plan")
            self.assertEqual(run.worker_result.events[0]["reason"], "missing_required_argument:url")

    def test_browser_worker_rejects_file_url_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Reject outside browser file.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            outside = Path(tmpdir) / "outside.html"
            outside.write_text("<html><body>outside</body></html>", encoding="utf-8")
            runtime = BrowserWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="BrowserWorker",
                constraints={
                    "browser_backend": "static",
                    "browser_plan": [{"action": "open_url", "arguments": {"url": outside.resolve().as_uri()}}],
                    "allowed_schemes": ["file"],
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "browser_action_failed")


if __name__ == "__main__":
    unittest.main()
