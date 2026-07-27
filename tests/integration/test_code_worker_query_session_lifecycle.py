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
    ROOT / "packages" / "workers",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import create_task_state
from zyra_runtime import WorkerRequest
from zyra_workers import CodeWorkerRuntime, CodeWorkerSidecarClient


@unittest.skipIf(
    shutil.which("node") is None and shutil.which("bun") is None,
    "TypeScript runtime is required",
)
class CodeWorkerQuerySessionLifecycleTests(unittest.TestCase):
    def test_session_contract_names_typescript_as_canonical_owner(self) -> None:
        contract = CodeWorkerSidecarClient(ROOT).session_contract()

        self.assertEqual(contract["source"], "zyra-typescript-runtime")
        self.assertEqual(contract["snapshotVersion"], "zyra.typescript-query-session.v1")
        self.assertTrue(contract["checksum"])
        self.assertTrue(contract["exactResume"])
        self.assertFalse(contract["pythonProjectionIsCanonical"])

    def test_runtime_emits_typescript_lifecycle_and_persists_projection_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Exercise the TypeScript query-session lifecycle.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "alpha.txt").write_text("alpha", encoding="utf-8")
            (workspace / "beta.txt").write_text("beta", encoding="utf-8")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            run = runtime.run(
                WorkerRequest(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    worker_name="CodeWorkerRuntime",
                    constraints={
                        "query_turns": [
                            [{"tool_name": "file_read", "arguments": {"path": "alpha.txt"}}],
                            [{"tool_name": "file_read", "arguments": {"path": "beta.txt"}}],
                        ]
                    },
                )
            )

            self.assertTrue(run.worker_result.ok, run.worker_result.error)
            phases = _query_phases(run.event_records)
            for phase in (
                "session_started",
                "turn_start",
                "message_delta",
                "turn_end",
                "session_completed",
                "query_session_snapshot",
            ):
                self.assertIn(phase, phases)
            self.assertLess(
                phases.index("session_completed"),
                phases.index("query_session_snapshot"),
            )
            metadata = run.worker_result.metadata
            self.assertEqual(metadata["canonical_runtime_owner"], "typescript")
            self.assertEqual(metadata["python_query_engine_fallback"], "false")
            self.assertEqual(metadata["query_session_checkpoint_ready"], "true")
            self.assertEqual(metadata["query_session_turns"], "2")
            self.assertEqual(metadata["query_session_consistent"], "true")

            snapshot = _artifact_json(
                run.worker_result.artifacts,
                metadata["query_session_snapshot_artifact_id"],
            )
            transcript = _artifact_lines(
                run.worker_result.artifacts,
                metadata["query_session_transcript_artifact_id"],
            )
            self.assertTrue(snapshot["consistency"]["ok"])
            self.assertEqual(snapshot["stats"]["turn_count"], 2)
            self.assertTrue(any(item.get("type") == "session_metadata" for item in transcript))
            self.assertTrue(any(item.get("event_type") == "message_delta" for item in transcript))

    def test_continue_on_error_executes_later_tool_but_preserves_failed_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Continue after an expected missing file.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            run = runtime.run(
                WorkerRequest(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    worker_name="CodeWorkerRuntime",
                    constraints={
                        "continue_on_error": True,
                        "e02PermissionPolicy": {"default_effect": "allow"},
                        "query_turns": [[
                            {"tool_name": "file_read", "arguments": {"path": "missing.txt"}},
                            {
                                "tool_name": "artifact_write",
                                "arguments": {"title": "fallback", "content": "continued"},
                            },
                        ]],
                    },
                )
            )

            self.assertFalse(run.worker_result.ok)
            phases = _query_phases(run.event_records)
            self.assertIn("continue", phases)
            completed = _query_events(run.event_records, "tool_call_completed")
            self.assertEqual(len(completed), 2)
            self.assertTrue(any(item["tool_name"] == "artifact_write" for item in completed))
            self.assertEqual(run.worker_result.metadata["query_session_consistent"], "true")

    def test_context_security_disconnect_fails_closed_before_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Disconnect the TypeScript context-security owner.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
            )
            run = runtime.run(
                WorkerRequest(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    worker_name="CodeWorkerRuntime",
                    constraints={
                        "disable_context_security_runtime": True,
                        "query_turns": [[
                            {"tool_name": "trace", "arguments": {"limit": 1}},
                        ]],
                    },
                )
            )

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "codeworker_api_foundation_disabled")
            self.assertEqual(
                run.worker_result.metadata["tool_runtime_gate_failures"],
                "disable_context_security_runtime",
            )
            phases = _query_phases(run.event_records)
            self.assertIn("codeworker_api_foundation", phases)
            self.assertNotIn("stream_request_start", phases)

    def test_restore_integration_disconnect_fails_closed_before_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Disconnect the TypeScript restore-integration owner.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
            )
            run = runtime.run(
                WorkerRequest(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    worker_name="CodeWorkerRuntime",
                    constraints={
                        "disable_restore_integration_runtime": True,
                        "query_turns": [[
                            {"tool_name": "trace", "arguments": {"limit": 1}},
                        ]],
                    },
                )
            )

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "codeworker_api_foundation_disabled")
            self.assertEqual(
                run.worker_result.metadata["tool_runtime_gate_failures"],
                "disable_restore_integration_runtime",
            )
            phases = _query_phases(run.event_records)
            self.assertIn("codeworker_api_foundation", phases)
            self.assertNotIn("stream_request_start", phases)


def _query_events(events: list[object], phase: str) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    for event in events:
        payload = getattr(event, "payload", {})
        query = payload.get("query_session") if isinstance(payload, dict) else None
        if isinstance(query, dict) and query.get("phase") == phase:
            selected.append(query)
    return selected


def _query_phases(events: list[object]) -> list[str]:
    return [
        str(item.get("phase"))
        for event in events
        if isinstance(getattr(event, "payload", {}), dict)
        for item in [getattr(event, "payload", {}).get("query_session")]
        if isinstance(item, dict)
    ]


def _artifact_path(artifacts: list[object], artifact_id: str) -> Path:
    return Path(next(item.uri for item in artifacts if item.artifact_id == artifact_id))


def _artifact_json(artifacts: list[object], artifact_id: str) -> dict[str, object]:
    return json.loads(_artifact_path(artifacts, artifact_id).read_text(encoding="utf-8"))


def _artifact_lines(artifacts: list[object], artifact_id: str) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in _artifact_path(artifacts, artifact_id).read_text(encoding="utf-8").splitlines()
    ]


if __name__ == "__main__":
    unittest.main()
