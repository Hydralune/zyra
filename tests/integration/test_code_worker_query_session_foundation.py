from __future__ import annotations

import json
import sys
import tempfile
import unittest
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

from zyra_core import create_task_state  # noqa: E402
from zyra_runtime import WorkerRequest  # noqa: E402
from zyra_workers import CodeWorkerRuntime  # noqa: E402


class CodeWorkerQuerySessionFoundationTests(unittest.TestCase):
    def test_code_worker_default_path_emits_session_foundation_events_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("session foundation integration")
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
                constraints={
                    "raw_input": "Create and read a file through the session foundation.",
                    "query_turns": [
                        [
                            {
                                "tool_name": "file_write",
                                "arguments": {"path": "foundation/result.txt", "content": "ok"},
                            },
                            {"tool_name": "file_read", "arguments": {"path": "foundation/result.txt"}},
                        ]
                    ],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            phases = [
                event.payload["query_session"]["phase"]
                for event in run.event_records
                if isinstance(event.payload, dict) and "query_session" in event.payload
            ]
            self.assertIn("query_session_seed_created", phases)
            self.assertIn("query_input_processed", phases)
            self.assertIn("context_snapshot_ready", phases)
            self.assertIn("session_store_append", phases)
            self.assertIn("session_foundation_audit", phases)
            self.assertIn("turn_lifecycle_projection", phases)
            self.assertIn("query_session_seed_attached", phases)
            self.assertIn("context_snapshot_attached", phases)
            self.assertIn("transcript_event_mapping", phases)
            self.assertIn("session_acceptance", phases)
            self.assertIn("session_lifecycle_state", phases)
            metadata = run.worker_result.metadata
            self.assertEqual(metadata["code_worker_session_seed_ok"], "true")
            self.assertEqual(metadata["session_foundation_audit_ok"], "true")
            self.assertEqual(metadata["session_foundation_audit_blockers"], "0")
            self.assertEqual(metadata["turn_lifecycle_ok"], "true")
            self.assertEqual(metadata["transcript_mapping_ok"], "true")
            self.assertEqual(metadata["session_acceptance_ok"], "true")
            self.assertEqual(metadata["session_lifecycle_ok"], "true")
            self.assertEqual(metadata["query_input_text_count"], "1")
            self.assertEqual(metadata["query_input_structured_turn_count"], "1")
            self.assertEqual(metadata["context_assembly_ok"], "true")
            self.assertEqual(metadata["session_contract_has_input_processor"], "true")
            self.assertEqual(metadata["session_contract_has_context_assembly"], "true")
            self.assertEqual(metadata["session_contract_has_code_worker_session_store"], "true")
            store_path = Path(metadata["code_worker_session_store_path"])
            self.assertTrue(store_path.exists())
            records = [json.loads(line) for line in store_path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(record["record_type"] == "session_seed" for record in records))
            self.assertTrue(any(record["record_type"] == "input_accepted" for record in records))
            self.assertTrue(any(record["record_type"] == "context_snapshot" for record in records))
            self.assertEqual(records[0]["session_id"], metadata["query_session_id"])

    def test_code_worker_blocks_when_session_store_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("disabled session store")
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
                constraints={
                    "disable_code_worker_session_store": True,
                    "raw_input": "This should not reach QueryEngine.",
                    "query_turns": [[{"tool_name": "trace", "arguments": {"limit": 1}}]],
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "code_worker_session_foundation_failed")
            self.assertEqual(run.worker_result.metadata["code_worker_session_seed_ok"], "false")
            self.assertEqual(run.worker_result.metadata["code_worker_session_store_error"], "code_worker_session_store_disabled")
            phases = [
                event.payload["query_session"]["phase"]
                for event in run.event_records
                if isinstance(event.payload, dict) and "query_session" in event.payload
            ]
            self.assertIn("session_store_append", phases)
            self.assertNotIn("stream_request_start", phases)

    def test_code_worker_blocks_when_context_assembly_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("disabled context")
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
                constraints={
                    "disable_context_assembly": True,
                    "raw_input": "This should not reach QueryEngine.",
                    "query_turns": [[{"tool_name": "trace", "arguments": {"limit": 1}}]],
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "code_worker_session_foundation_failed")
            self.assertEqual(run.worker_result.metadata["context_assembly_ok"], "false")
            phases = [
                event.payload["query_session"]["phase"]
                for event in run.event_records
                if isinstance(event.payload, dict) and "query_session" in event.payload
            ]
            self.assertIn("context_snapshot_ready", phases)
            self.assertNotIn("stream_request_start", phases)

    def test_code_worker_blocks_when_input_processor_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("disabled input processor")
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
                constraints={
                    "disable_query_input_processor": True,
                    "raw_input": "This should not reach QueryEngine.",
                    "query_turns": [[{"tool_name": "trace", "arguments": {"limit": 1}}]],
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "code_worker_session_foundation_failed")
            self.assertEqual(run.worker_result.metadata["query_input_processor_ok"], "false")
            phases = [
                event.payload["query_session"]["phase"]
                for event in run.event_records
                if isinstance(event.payload, dict) and "query_session" in event.payload
            ]
            self.assertIn("query_input_processed", phases)
            self.assertNotIn("stream_request_start", phases)


if __name__ == "__main__":
    unittest.main()
