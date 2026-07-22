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
from zyra_runtime import QuerySession, WorkerRequest  # noqa: E402
from zyra_workers import CodeWorkerRuntime  # noqa: E402


class ExplodingSidecarClient:
    def health(self):
        raise AssertionError("sidecar must not be called by default runtime path")

    def runtime_inventory(self):
        raise AssertionError("sidecar must not be called by default runtime path")

    def query_contract(self):
        raise AssertionError("sidecar must not be called by default runtime path")

    def session_contract(self):
        raise AssertionError("sidecar must not be called by default runtime path")

    def tool_loop_contract(self):
        raise AssertionError("sidecar must not be called by default runtime path")


class CodeWorkerCleanProductizedRuntimeTests(unittest.TestCase):
    def test_default_runtime_runs_without_source_workspace_or_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Clean productized CodeWorker path.")
            workspace = Path(tmpdir) / "workspace"
            (workspace / "clean").mkdir(parents=True)
            (workspace / "clean" / "result.txt").write_text("clean ok", encoding="utf-8")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
                sidecar_client=ExplodingSidecarClient(),
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "tool_plan": [
                        {"tool_name": "file_read", "arguments": {"path": "clean/result.txt"}},
                        {"tool_name": "file_read", "arguments": {"path": "clean/result.txt"}},
                    ],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            self.assertEqual(run.worker_result.metadata["sidecar_contracts_used"], "false")
            self.assertEqual(run.worker_result.metadata["query_contract_source"], "zyra-claude-productized")
            self.assertEqual(run.worker_result.metadata["loop"], "zyra_typescript_query_engine_runtime")
            self.assertEqual(run.worker_result.metadata["query_plan_ok"], "true")
            self.assertEqual(run.worker_result.metadata["query_plan_tool_steps"], "2")
            self.assertEqual(run.worker_result.metadata["runtime_state_ok"], "true")
            self.assertGreaterEqual(int(run.worker_result.metadata["runtime_state_mutations"]), 5)
            self.assertTrue((workspace / "clean" / "result.txt").exists())
            phases = [
                event.payload["query_session"]["phase"]
                for event in run.event_records
                if "query_session" in event.payload
            ]
            self.assertIn("tool_loop_plan", phases)
            self.assertIn("query_session_snapshot", phases)

    def test_disconnecting_productized_query_engine_fails_default_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Disconnected QueryEngine must fail.")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
                sidecar_client=ExplodingSidecarClient(),
                query_engine_factory=None,
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={"tool_plan": [{"tool_name": "file_write", "arguments": {"path": "x.txt", "content": "x"}}]},
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "productized_query_engine_runtime_disabled")
            self.assertFalse((Path(tmpdir) / "workspace" / "x.txt").exists())

    def test_permission_semantics_fail_without_workspace_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Permission denial must alter execution.")
            outside = Path(tmpdir) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
                sidecar_client=ExplodingSidecarClient(),
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "tool_plan": [{"tool_name": "file_read", "arguments": {"path": str(outside)}}],
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "permission_denied")
            failures = [
                event.payload["query_session"]
                for event in run.event_records
                if event.payload.get("query_session", {}).get("phase") == "tool_failure_signal"
                and "signal" in event.payload.get("query_session", {})
            ]
            watchdogs = [
                event.payload["query_session"]
                for event in run.event_records
                if event.payload.get("query_session", {}).get("phase") == "watchdog_signal"
            ]
            self.assertEqual(failures[0]["signal"]["kind"], "permission_denied")
            self.assertEqual(watchdogs[0]["watchdog_signal"]["route"], "permission_runtime")

    def test_default_codeworker_frame_forwards_structured_watchdog_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("A denied tool must reach the canonical fault sink.")
            outside = Path(tmpdir) / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            captured: list[dict[str, object]] = []
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
                sidecar_client=ExplodingSidecarClient(),
                runtime_services={
                    "fault_observation_sink": lambda payload: captured.append(dict(payload)),
                    "fault_observation_sink_required": True,
                },
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "tool_plan": [
                        {
                            "tool_name": "file_read",
                            "arguments": {"path": str(outside)},
                        }
                    ],
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertTrue(captured)
            observation = captured[0]["observation"]
            self.assertIsInstance(observation, dict)
            self.assertEqual(observation["category"], "permission")
            self.assertEqual(observation["code"], "permission_denied")
            self.assertEqual(observation["refs"]["task_id"], state.task_id)

    def test_context_budget_changes_runtime_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Context budget must trigger compact artifact.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "large.txt").write_text("context-" * 100, encoding="utf-8")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
                sidecar_client=ExplodingSidecarClient(),
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "query_context_budget_chars": 160,
                    "tool_plan": [
                        {"tool_name": "file_read", "arguments": {"path": "large.txt"}},
                        {"tool_name": "file_read", "arguments": {"path": "large.txt"}},
                    ],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            self.assertGreaterEqual(int(run.worker_result.metadata["context_compactions"]), 1)
            compact_events = [
                event.payload["query_session"]
                for event in run.event_records
                if event.payload.get("query_session", {}).get("phase") == "context_compacted"
            ]
            self.assertTrue(compact_events)
            compact_artifact_ids = {event["artifact_id"] for event in compact_events}
            self.assertTrue(any(artifact.artifact_id in compact_artifact_ids for artifact in run.worker_result.artifacts))

    def test_session_snapshot_restores_without_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Session snapshot restore.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "restore.txt").write_text("restore ok", encoding="utf-8")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
                sidecar_client=ExplodingSidecarClient(),
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "query_turns": [
                        [{"tool_name": "file_read", "arguments": {"path": "restore.txt"}}],
                        [{"tool_name": "file_read", "arguments": {"path": "restore.txt"}}],
                    ],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            snapshot_artifact = next(
                artifact
                for artifact in run.worker_result.artifacts
                if artifact.artifact_id == run.worker_result.metadata["query_session_snapshot_artifact_id"]
            )
            snapshot = json.loads(Path(snapshot_artifact.uri).read_text(encoding="utf-8"))
            restored = QuerySession.restore(snapshot)

            self.assertEqual(restored.session_id, snapshot["session_id"])
            self.assertEqual(restored.resume_token, snapshot["resume_token"])
            self.assertEqual(restored.stats()["turn_count"], 2)
            self.assertEqual(restored.consistency_report()["ok"], True)

    def test_control_commands_and_semantic_runtime_enter_default_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Control commands on productized runtime.")
            workspace = Path(tmpdir) / "workspace"
            (workspace / "control").mkdir(parents=True)
            (workspace / "control" / "result.txt").write_text("control ok", encoding="utf-8")
            runtime = CodeWorkerRuntime(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
                sidecar_client=ExplodingSidecarClient(),
            )
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "tool_plan": [
                        {"tool_name": "file_read", "arguments": {"path": "control/result.txt"}},
                        {"tool_name": "file_read", "arguments": {"path": "control/result.txt"}},
                    ],
                    "control_commands": [
                        {"name": "context"},
                        {"name": "tools"},
                        {"name": "resume"},
                        {"name": "doctor", "artifact_policy": "artifact"},
                    ],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            self.assertEqual(run.worker_result.metadata["control_command_count"], "4")
            self.assertEqual(run.worker_result.metadata["control_command_failed"], "0")
            self.assertEqual(run.worker_result.metadata["tool_runtime_planned"], "2")
            self.assertEqual(run.worker_result.metadata["tool_runtime_mutating"], "0")
            self.assertEqual(run.worker_result.metadata["runtime_state_control_mutations"], "4")
            self.assertEqual(run.worker_result.metadata["session_lifecycle_resume_plans"], "1")
            self.assertEqual(run.worker_result.metadata["session_lifecycle_latest_resume_status"], "ready")
            phases = [
                event.payload["query_session"]["phase"]
                for event in run.event_records
                if "query_session" in event.payload
            ]
            self.assertIn("control_command", phases)
            control_artifacts = [
                artifact
                for artifact in run.worker_result.artifacts
                if "Claude control command" in artifact.title
            ]
            self.assertEqual(len(control_artifacts), 1)
            payload = json.loads(Path(control_artifacts[0].uri).read_text(encoding="utf-8"))
            self.assertEqual(payload["name"], "doctor")
            self.assertEqual(payload["status"], "ok")


if __name__ == "__main__":
    unittest.main()
