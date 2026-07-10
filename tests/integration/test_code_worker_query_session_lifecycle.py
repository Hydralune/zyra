from __future__ import annotations

import json
import shutil
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
from zyra_workers import CodeWorkerRuntime, CodeWorkerSidecarClient  # noqa: E402


@unittest.skipIf(shutil.which("node") is None, "node is required for code-worker sidecar")
class CodeWorkerQuerySessionLifecycleTests(unittest.TestCase):
    def test_sidecar_session_contract_maps_claude_code_sources_to_zyra_runtime(self) -> None:
        contract = CodeWorkerSidecarClient(ROOT).session_contract()

        self.assertEqual(contract["source"], "claude-code-best")
        self.assertEqual(contract["ownerUnit"], "M1-02B")
        self.assertTrue(contract["inventoryExists"])
        self.assertIn("src/utils/sessionStorage.ts", contract["sourceFiles"])
        self.assertIn("src/utils/sessionRestore.ts", contract["sourceFiles"])
        self.assertIn("src/services/api/claude.ts", contract["sourceFiles"])
        self.assertIn("src/services/api/client.ts", contract["sourceFiles"])
        self.assertIn("src/services/api/filesApi.ts", contract["sourceFiles"])
        self.assertIn("src/services/api/withRetry.ts", contract["sourceFiles"])
        self.assertIn("src/bridge/inboundMessages.ts", contract["sourceFiles"])
        self.assertIn("src/commands/clear/conversation.ts", contract["sourceFiles"])
        self.assertTrue(contract["transcriptPersistence"]["appendOnlyJsonl"])
        self.assertTrue(contract["transcriptPersistence"]["parentUuidChain"])
        self.assertEqual(contract["transcriptPersistence"]["liteReadWindowBytes"], 65536)
        self.assertTrue(contract["resumeRecovery"]["hasChainTraversal"])
        self.assertTrue(contract["resumeRecovery"]["hasInterruptionDetection"])
        self.assertTrue(contract["resumeRecovery"]["hasOrphanedToolResultRecovery"])
        self.assertTrue(contract["streamRuntime"]["rawSseStateMachine"])
        self.assertTrue(contract["streamRuntime"]["hasApiClient"])
        self.assertTrue(contract["streamRuntime"]["hasFilesApi"])
        self.assertTrue(contract["streamRuntime"]["hasQueryProfiler"])
        self.assertTrue(contract["streamRuntime"]["retryMatrix"]["unattendedRetry"])
        self.assertTrue(contract["bridgeSessionRuntime"]["hasInboundMessages"])
        self.assertTrue(contract["sessionCommands"]["hasClearConversation"])
        self.assertEqual(
            contract["zyraRuntimeMapping"]["querySession"],
            "packages/runtime/zyra_runtime/query_session.py",
        )

    def test_runtime_emits_turn_message_snapshot_and_transcript_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Exercise CodeWorker query session lifecycle.")
            workspace = Path(tmpdir) / "workspace"
            artifacts = Path(tmpdir) / "artifacts"
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifacts,
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "query_turns": [
                        [
                            {
                                "tool_name": "file_write",
                                "prompt": "Create a file that proves session lifecycle wiring.",
                                "arguments": {"path": "session/result.txt", "content": "session ok"},
                            },
                            {"tool_name": "file_read", "arguments": {"path": "session/result.txt"}},
                        ],
                        [
                            {
                                "tool_name": "artifact_write",
                                "prompt": "Record a short session report.",
                                "arguments": {
                                    "title": "session report",
                                    "kind": "trace",
                                    "extension": ".md",
                                    "content": "# Session report\n\nok",
                                },
                            }
                        ],
                    ],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            phases = [
                event.payload["query_session"]["phase"]
                for event in run.event_records
                if "query_session" in event.payload
            ]
            self.assertIn("stream_request_start", phases)
            self.assertIn("turn_start", phases)
            self.assertIn("message_delta", phases)
            self.assertIn("turn_end", phases)
            self.assertIn("query_session_snapshot", phases)
            self.assertIn("session_completed", phases)
            self.assertIn("transcript_event_mapping", phases)
            self.assertIn("session_acceptance", phases)
            self.assertIn("session_lifecycle_state", phases)
            self.assertLess(phases.index("session_completed"), phases.index("transcript_event_mapping"))
            self.assertLess(phases.index("transcript_event_mapping"), phases.index("session_acceptance"))
            self.assertLess(phases.index("session_acceptance"), phases.index("session_lifecycle_state"))
            self.assertEqual(run.worker_result.metadata["session_contract_source"], "zyra-claude-productized")
            self.assertEqual(run.worker_result.metadata["session_contract_parent_uuid_chain"], "true")
            self.assertEqual(run.worker_result.metadata["sidecar_contracts_used"], "false")
            self.assertEqual(run.worker_result.metadata["query_session_checkpoint_ready"], "true")
            self.assertEqual(run.worker_result.metadata["query_session_turns"], "2")
            self.assertEqual(run.worker_result.metadata["query_session_consistent"], "true")
            self.assertTrue(run.worker_result.metadata["query_session_resume_token"].startswith("codesession_"))

            snapshot_artifact_id = run.worker_result.metadata["query_session_snapshot_artifact_id"]
            transcript_artifact_id = run.worker_result.metadata["query_session_transcript_artifact_id"]
            snapshot_artifacts = [artifact for artifact in run.worker_result.artifacts if artifact.artifact_id == snapshot_artifact_id]
            transcript_artifacts = [artifact for artifact in run.worker_result.artifacts if artifact.artifact_id == transcript_artifact_id]
            self.assertEqual(len(snapshot_artifacts), 1)
            self.assertEqual(len(transcript_artifacts), 1)
            snapshot = json.loads(Path(snapshot_artifacts[0].uri).read_text(encoding="utf-8"))
            transcript = Path(transcript_artifacts[0].uri).read_text(encoding="utf-8").splitlines()

            self.assertEqual(snapshot["session_id"], run.worker_result.metadata["query_session_id"])
            self.assertTrue(snapshot["consistency"]["ok"])
            self.assertEqual(snapshot["stats"]["turn_count"], 2)
            self.assertGreaterEqual(snapshot["stats"]["message_count"], 6)
            self.assertGreater(len(transcript), snapshot["stats"]["message_count"])
            self.assertTrue(any(json.loads(line)["type"] == "session_metadata" for line in transcript))
            self.assertTrue(any(json.loads(line).get("event_type") == "message_delta" for line in transcript))

    def test_runtime_records_error_and_continue_events_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Continue after an expected missing file.")
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
                    "continue_on_error": True,
                    "query_turns": [
                        [
                            {"tool_name": "file_read", "arguments": {"path": "missing.txt"}},
                            {"tool_name": "artifact_write", "arguments": {"title": "fallback", "content": "continued"}},
                        ]
                    ],
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            phases = [
                event.payload["query_session"]["phase"]
                for event in run.event_records
                if "query_session" in event.payload
            ]
            self.assertIn("continue", phases)
            self.assertIn("message_delta", phases)
            self.assertEqual(run.worker_result.metadata["query_session_consistent"], "true")
            snapshot_path = next(
                artifact.uri
                for artifact in run.worker_result.artifacts
                if artifact.artifact_id == run.worker_result.metadata["query_session_snapshot_artifact_id"]
            )
            snapshot = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))

            self.assertEqual(snapshot["stats"]["continue_count"], 1)
            self.assertEqual(snapshot["status"], "failed")


if __name__ == "__main__":
    unittest.main()
