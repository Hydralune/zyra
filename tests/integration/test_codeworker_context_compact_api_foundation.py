from __future__ import annotations

import json
import importlib
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT,
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "workers",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import create_task_state
from zyra_runtime import WorkerRequest
from zyra_workers import CodeWorkerRuntime


@unittest.skipIf(
    shutil.which("node") is None and shutil.which("bun") is None,
    "TypeScript runtime is required",
)
class CodeWorkerContextCompactApiFoundationTests(unittest.TestCase):
    def test_default_path_emits_compact_restore_and_budget_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Compact and restore TypeScript runtime context.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "large.txt").write_text("compact evidence " * 300, encoding="utf-8")
            runtime = _runtime(tmpdir, workspace)
            run = runtime.run(
                _request(
                    state,
                    query_context_budget_chars=900,
                    force_compact_restore=True,
                    query_turns=[
                        [{"tool_name": "file_read", "arguments": {"path": "large.txt"}}],
                        [{"tool_name": "file_read", "arguments": {"path": "large.txt"}}],
                    ],
                )
            )

            self.assertTrue(run.worker_result.ok, run.worker_result.error)
            metadata = run.worker_result.metadata
            self.assertEqual(metadata["canonical_runtime_owner"], "typescript")
            self.assertEqual(metadata["model_stream_ok"], "true")
            self.assertEqual(metadata["api_retry_status"], "not_needed")
            self.assertEqual(metadata["compact_restore_ok"], "true")
            self.assertEqual(metadata["runtime_budget_state_ok"], "true")
            self.assertEqual(metadata["codeworker_api_foundation_ok"], "true")
            self.assertGreaterEqual(int(metadata["context_compactions"]), 1)
            for phase in (
                "model_stream_report",
                "api_retry_report",
                "compact_restore_report",
                "codeworker_restore_integration",
                "compact_state_projection",
                "runtime_budget_replay",
            ):
                self.assertTrue(_query_events(run.event_records, phase), phase)

    def test_http_sse_retry_uses_fallback_model_plan_not_request_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, _LocalSseProvider(failures=3) as provider:
            state = create_task_state("Exercise real model retry and fallback.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            runtime = _runtime(tmpdir, workspace)
            run = runtime.run(
                _request(
                    state,
                    raw_input="Use the provider tool call.",
                    model_transport="http_sse",
                    model_api_base_url=provider.base_url,
                    model_api_timeout_seconds=5,
                    api_retry_max_attempts=4,
                    api_retry_fallback_models="zyra-fallback-a",
                    tool_plan=[{
                        "tool_name": "file_write",
                        "arguments": {"path": "request-plan.txt", "content": "must not run"},
                    }],
                )
            )

            self.assertTrue(run.worker_result.ok, run.worker_result.error)
            self.assertEqual(len(provider.requests), 4)
            self.assertEqual(
                [item["model"] for item in provider.requests[:3]],
                ["zyra-local-code-model"] * 3,
            )
            self.assertNotEqual(provider.requests[2]["model"], provider.requests[3]["model"])
            self.assertEqual(run.worker_result.metadata["api_retry_status"], "fallback_selected")
            self.assertEqual(run.worker_result.metadata["api_retry_fallback_used"], "true")
            self.assertEqual(run.worker_result.metadata["api_retry_final_model"], "zyra-fallback-a")
            reports = _query_events(run.event_records, "model_stream_report")
            self.assertFalse(reports[0]["model_stream"]["ok"])
            self.assertTrue(reports[-1]["model_stream"]["ok"])
            retry = _query_events(run.event_records, "api_retry_report")[-1]["api_retry"]
            self.assertEqual(
                [item["decision"] for item in retry["attempts"][:3]],
                ["retry", "retry", "fallback"],
            )
            self.assertEqual(run.worker_result.metadata["tool_runtime_completed"], "0")
            self.assertFalse((workspace / "request-plan.txt").exists())

    def test_exhausted_model_attempts_fail_before_mutating_tool_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, _LocalSseProvider(failures=20) as provider:
            state = create_task_state("Fail closed when the model transport is exhausted.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            runtime = _runtime(tmpdir, workspace)
            run = runtime.run(
                _request(
                    state,
                    model_transport="http_sse",
                    model_api_base_url=provider.base_url,
                    model_api_timeout_seconds=5,
                    api_retry_max_attempts=2,
                    api_retry_fallback_models="zyra-fallback-a",
                    tool_plan=[{
                        "tool_name": "file_write",
                        "arguments": {"path": "must-not-exist.txt", "content": "forbidden"},
                    }],
                )
            )

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(len(provider.requests), 2)
            self.assertEqual(run.worker_result.metadata["api_retry_recovered"], "false")
            self.assertFalse((workspace / "must-not-exist.txt").exists())

    def test_required_api_component_disconnects_fail_before_tools(self) -> None:
        for constraint, metadata_key in (
            ("disable_compact_restore_runtime", "compact_restore_ok"),
            ("disable_model_stream_runtime", "model_stream_ok"),
            ("disable_runtime_budget_state", "runtime_budget_state_ok"),
        ):
            with self.subTest(constraint=constraint), tempfile.TemporaryDirectory() as tmpdir:
                state = create_task_state("Disconnect " + constraint)
                workspace = Path(tmpdir) / "workspace"
                workspace.mkdir()
                runtime = _runtime(tmpdir, workspace)
                run = runtime.run(
                    _request(
                        state,
                        **{
                            constraint: True,
                            "force_compact_restore": True,
                            "query_turns": [[{
                                "tool_name": "file_write",
                                "arguments": {"path": "must-not-exist.txt", "content": "forbidden"},
                            }]],
                        },
                    )
                )

                self.assertFalse(run.worker_result.ok)
                self.assertEqual(run.worker_result.metadata[metadata_key], "false")
                self.assertEqual(run.worker_result.metadata["codeworker_api_foundation_ok"], "false")
                self.assertFalse((workspace / "must-not-exist.txt").exists())

    def test_compact_state_api_returns_live_typescript_projection_without_get_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "large.txt").write_text("api compact state " * 220, encoding="utf-8")
            environment = {
                "ZYRA_SQLITE_PATH": str(Path(tmpdir) / "api.sqlite3"),
                "ZYRA_EVENT_LOG": str(Path(tmpdir) / "events.jsonl"),
                "ZYRA_TOOL_WORKSPACE": str(workspace),
                "ZYRA_ARTIFACT_ROOT": str(Path(tmpdir) / "artifacts"),
            }
            previous = {key: os.environ.get(key) for key in environment}
            os.environ.update(environment)
            module_name = "apps.api.zyra_api.main"
            module = importlib.import_module(module_name)
            ZyraRequestHandler = importlib.reload(module).ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Project compact state.", "auto_run": False})
                task_id = created["task"]["task_id"]
                executed = _post(
                    base_url,
                    f"/tasks/{task_id}/workers/code",
                    {
                        "raw_input": "Read and compact the file.",
                        "query_context_budget_chars": 900,
                        "force_compact_restore": True,
                        "query_turns": [
                            [{"tool_name": "file_read", "arguments": {"path": "large.txt"}}],
                            [{"tool_name": "file_read", "arguments": {"path": "large.txt"}}],
                        ],
                    },
                )
                self.assertTrue(executed["worker_result"]["ok"])
                before = _workspace_snapshot(workspace)
                payload = _get(base_url, f"/workers/code/compact-state?task_id={task_id}")
                after = _workspace_snapshot(workspace)
                self.assertTrue(payload["route_contract"]["ok"])
                self.assertTrue(payload["compact_state"]["restore_contract_id"])
                self.assertEqual(before, after)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


def _runtime(tmpdir: str, workspace: Path) -> CodeWorkerRuntime:
    return CodeWorkerRuntime(
        project_root=ROOT,
        workspace_root=workspace,
        artifact_root=Path(tmpdir) / "artifacts",
    )


def _request(state: object, **constraints: object) -> WorkerRequest:
    return WorkerRequest(
        run_id=state.run_id,
        task_id=state.task_id,
        node_id=state.root_node_id,
        worker_name="CodeWorkerRuntime",
        constraints=constraints,
    )


def _query_events(events: list[object], phase: str) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for event in events:
        payload = getattr(event, "payload", {})
        query = payload.get("query_session") if isinstance(payload, dict) else None
        if isinstance(query, dict) and query.get("phase") == phase:
            selected.append(query)
    return selected


def _get(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(base_url + path, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        base_url + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _workspace_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class _LocalSseProvider:
    def __init__(self, *, failures: int) -> None:
        self.failures_remaining = failures
        self.successful_responses = 0
        self.requests: list[dict[str, Any]] = []
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.base_url = ""

    def __enter__(self) -> "_LocalSseProvider":
        scenario = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                scenario.requests.append(json.loads(raw.decode("utf-8")) if raw else {})
                if scenario.failures_remaining > 0:
                    scenario.failures_remaining -= 1
                    body = json.dumps({"error": {"message": "provider overloaded"}}).encode("utf-8")
                    self.send_response(529)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                scenario.successful_responses += 1
                chunks = [
                    {
                        "id": "chatcmpl-provider",
                        "choices": [{
                            "index": 0,
                            "delta": {"role": "assistant", "content": "done"},
                            "finish_reason": None,
                        }],
                    },
                    {
                        "id": "chatcmpl-provider",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    },
                ]
                body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
                body += "data: [DONE]\n\n"
                encoded = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
