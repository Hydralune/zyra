from __future__ import annotations

import gc
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import create_task_state  # noqa: E402
from zyra_runtime import WorkerRequest  # noqa: E402
from zyra_workers import CodeWorkerRuntime  # noqa: E402


class CodeWorkerQuerySessionIntegrationTests(unittest.TestCase):
    def test_default_path_emits_query_entry_handoff_custody_and_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run = _run_worker(
                tmpdir,
                {
                    "raw_input": "Create and read through query entry.",
                    "query_turns": [
                        [
                            {"tool_name": "file_write", "arguments": {"path": "entry/result.txt", "content": "ok"}},
                            {"tool_name": "file_read", "arguments": {"path": "entry/result.txt"}},
                        ]
                    ],
                },
            )

            self.assertTrue(run.worker_result.ok)
            metadata = run.worker_result.metadata
            self.assertEqual(metadata["query_session_integration_ok"], "true")
            self.assertEqual(metadata["query_entry_ok"], "true")
            self.assertEqual(metadata["query_entry_handoff_ok"], "true")
            self.assertEqual(metadata["query_handoff_ok"], "true")
            self.assertEqual(metadata["query_custody_ok"], "true")
            self.assertEqual(metadata["query_custody_engine_attached"], "true")
            self.assertEqual(metadata["query_event_flow_ok"], "true")
            self.assertGreater(int(metadata["query_entry_message_count"]), 0)

            phases = _query_phases(run)
            self.assertIn("query_entry_packet_ready", phases)
            self.assertIn("query_started", phases)
            self.assertIn("query_downstream_handoff_ready", phases)
            self.assertIn("query_handoff_contract", phases)
            self.assertIn("query_resume_custody", phases)
            self.assertIn("query_event_flow_audit", phases)
            self.assertIn("stream_request_start", phases)
            self.assertLess(phases.index("query_started"), phases.index("stream_request_start"))

            store_records = _store_records(metadata["code_worker_session_store_path"])
            record_types = [record["record_type"] for record in store_records]
            self.assertIn("query_control_state", record_types)
            self.assertIn("query_entry_packet", record_types)
            self.assertIn("query_engine_attached", record_types)

    def test_cancel_blocks_query_engine_before_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run = _run_worker(
                tmpdir,
                {
                    "cancel_session": True,
                    "cancel_reason": "operator cancelled before model stream",
                    "raw_input": "This should not enter QueryEngine.",
                    "query_turns": [[{"tool_name": "file_write", "arguments": {"path": "blocked.txt", "content": "x"}}]],
                },
            )

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.metadata["query_session_integration_ok"], "false")
            self.assertEqual(run.worker_result.metadata["query_entry_block_reason"], "control_cancelled")
            self.assertEqual(run.worker_result.metadata["query_custody_engine_attached"], "false")
            phases = _query_phases(run)
            self.assertIn("query_cancelled", phases)
            self.assertIn("query_entry_packet_ready", phases)
            self.assertNotIn("stream_request_start", phases)

    def test_disabling_query_entry_packet_disconnects_worker_before_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run = _run_worker(
                tmpdir,
                {
                    "disable_query_entry_packet": True,
                    "raw_input": "This should stop at query entry packet.",
                    "query_turns": [[{"tool_name": "file_write", "arguments": {"path": "blocked.txt", "content": "x"}}]],
                },
            )

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.metadata["query_session_integration_ok"], "false")
            self.assertEqual(run.worker_result.metadata["query_entry_block_reason"], "disabled")
            phases = _query_phases(run)
            self.assertIn("query_entry_packet_ready", phases)
            self.assertNotIn("query_started", phases)
            self.assertNotIn("stream_request_start", phases)

    def test_interrupt_blocks_query_engine_before_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run = _run_worker(
                tmpdir,
                {
                    "interrupt_session": True,
                    "interrupt_reason": "operator interrupted before model stream",
                    "raw_input": "This should interrupt before QueryEngine.",
                    "query_turns": [[{"tool_name": "file_write", "arguments": {"path": "blocked.txt", "content": "x"}}]],
                },
            )

            self.assertFalse(run.worker_result.ok)
            metadata = run.worker_result.metadata
            self.assertEqual(metadata["query_entry_block_reason"], "control_interrupted")
            self.assertEqual(metadata["query_disconnect_terminal_control_observed"], "true")
            phases = _query_phases(run)
            self.assertIn("query_interrupted", phases)
            self.assertNotIn("stream_request_start", phases)

    def test_stale_context_fingerprint_blocks_stale_packet_before_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run = _run_worker(
                tmpdir,
                {
                    "expected_context_fingerprint": "definitely-stale-context",
                    "raw_input": "This should stale-block before QueryEngine.",
                    "query_turns": [[{"tool_name": "file_write", "arguments": {"path": "blocked.txt", "content": "x"}}]],
                },
            )

            self.assertFalse(run.worker_result.ok)
            metadata = run.worker_result.metadata
            self.assertEqual(metadata["query_entry_block_reason"], "stale_context")
            self.assertEqual(metadata["query_session_stale_context"], "true")
            phases = _query_phases(run)
            self.assertIn("query_stale_context_blocked", phases)
            self.assertNotIn("stream_request_start", phases)

    def test_checkpoint_request_writes_artifact_and_store_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            run = _run_worker(
                tmpdir,
                {
                    "checkpoint_session": True,
                    "raw_input": "Checkpoint before query.",
                    "query_turns": [[{"tool_name": "file_write", "arguments": {"path": "checkpoint.txt", "content": "ok"}}]],
                },
            )

            self.assertTrue(run.worker_result.ok)
            metadata = run.worker_result.metadata
            self.assertEqual(metadata["query_session_checkpoint_status"], "written")
            self.assertTrue(metadata["query_session_checkpoint_artifact_id"])
            self.assertEqual(metadata["query_custody_checkpoint_requested"], "true")
            store_records = _store_records(metadata["code_worker_session_store_path"])
            self.assertTrue(any(record["record_type"] == "query_checkpoint" for record in store_records))

    def test_resume_restores_parent_context_and_uses_replay_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("query session integration branch resume")
            first_runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
            )
            first = first_runtime.run(
                WorkerRequest(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    worker_name="CodeWorkerRuntime",
                    constraints={
                    "raw_input": "Create state for resume.",
                    "query_turns": [[{"tool_name": "file_write", "arguments": {"path": "resume.txt", "content": "ok"}}]],
                    },
                )
            )
            self.assertTrue(first.worker_result.ok)
            session_id = first.worker_result.metadata["code_worker_session_seed_session_id"]

            second_runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
            )
            second = second_runtime.run(
                WorkerRequest(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    worker_name="CodeWorkerRuntime",
                    constraints={
                        "session_id": "branch-resume-current-session",
                        "resume_session_id": session_id,
                        "raw_input": "Continue from previous session state.",
                        "query_turns": [[{"tool_name": "file_read", "arguments": {"path": "resume.txt"}}]],
                    },
                )
            )

            self.assertTrue(second.worker_result.ok)
            metadata = second.worker_result.metadata
            self.assertEqual(metadata["query_session_resume_requested"], "true")
            self.assertTrue(metadata["query_session_restored_parent_uuid"])
            self.assertTrue(metadata["query_session_restored_context_fingerprint"])
            self.assertEqual(metadata["query_custody_resume_requested"], "true")
            phases = _query_phases(second)
            self.assertIn("query_resume_restored", phases)
            self.assertIn("stream_request_start", phases)


class CodeWorkerQuerySessionIntegrationApiTests(unittest.TestCase):
    def test_session_integration_endpoint_returns_typescript_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(Path(tmpdir) / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                payload = _get(base_url, "/workers/code/session-integration?q=Inspect%20query%20entry")

                self.assertTrue(payload["ok"])
                self.assertEqual(payload["canonicalOwner"], "typescript")
                self.assertEqual(payload["source"], "zyra-typescript-runtime")
                self.assertTrue(payload["budgets"]["toolResultBudget"])
                self.assertTrue(payload["compactRuntime"]["postCompactRestore"])
                self.assertEqual(
                    payload["sessionContract"]["snapshotVersion"],
                    "zyra.typescript-query-session.v1",
                )
                self.assertFalse(payload["sessionContract"]["pythonProjectionIsCanonical"])
                self.assertTrue(payload["defaultRoute"])
                self.assertFalse(payload["fallbackUsed"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                from apps.api.zyra_api.main import reset_api_product_bootstrap
                from apps.api.zyra_api.experiment_api import reset_experiment_api

                reset_experiment_api(wait=True)
                reset_api_product_bootstrap()
                gc.collect()


def _run_worker(tmpdir: str, constraints: dict[str, Any]) -> Any:
    state = create_task_state("query session integration")
    runtime = CodeWorkerRuntime(
        project_root=ROOT,
        workspace_root=Path(tmpdir) / "workspace",
        artifact_root=Path(tmpdir) / "artifacts",
    )
    request = WorkerRequest(
        run_id=state.run_id,
        task_id=state.task_id,
        node_id=state.root_node_id,
        worker_name="CodeWorkerRuntime",
        constraints=constraints,
    )
    return runtime.run(request)


def _query_phases(run: Any) -> list[str]:
    return [
        event.payload["query_session"]["phase"]
        for event in run.event_records
        if isinstance(event.payload, dict) and "query_session" in event.payload
    ]


def _store_records(path: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _get(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
