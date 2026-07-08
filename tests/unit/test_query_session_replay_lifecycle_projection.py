from __future__ import annotations

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
from zyra_runtime import (  # noqa: E402
    CodeWorkerSessionFoundationRuntime,
    CodeWorkerSessionReplayRuntime,
    CodeWorkerSessionStore,
    ContextAssemblyRuntime,
    QueryInputProcessor,
    SessionAcceptanceRuntime,
    SessionApiProjectionBuilder,
    SessionFoundationAuditor,
    SessionLifecycleRuntime,
    SessionLineageRuntime,
    ToolExecutionContext,
    TurnLifecycleRuntime,
    WorkerRequest,
    build_productized_claude_runtime_contracts,
    foundation_audit_event,
)
from zyra_workers import CodeWorkerRuntime  # noqa: E402


class QuerySessionReplayLifecycleProjectionTests(unittest.TestCase):
    def test_replay_turn_acceptance_and_lifecycle_use_real_session_store_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("replay lifecycle")
            workspace = Path(tmpdir) / "workspace"
            artifacts = Path(tmpdir) / "artifacts"
            context = ToolExecutionContext.for_workspace(workspace, artifacts)
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "session_id": "codesession_replay_lifecycle",
                    "raw_input": "Prepare a replayable session.",
                    "query_turns": [[{"tool_name": "file_write", "arguments": {"path": "replay.txt", "content": "ok"}}]],
                },
            )
            input_report = QueryInputProcessor().process_worker_request(request)
            context_snapshot = ContextAssemblyRuntime().assemble(
                request=request,
                session_id="codesession_replay_lifecycle",
                input_records=input_report.records,
                tool_specs=context.registry.list(),
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifacts,
                permission_mode="workspace",
            )
            store = CodeWorkerSessionStore(artifacts / "session-store")
            foundation = CodeWorkerSessionFoundationRuntime(store=store)
            seed = foundation.build_seed(
                request=request,
                input_report=input_report,
                context_snapshot=context_snapshot,
                session_id="codesession_replay_lifecycle",
            )
            store.mark_query_engine_attached(
                session_id=seed.session_id,
                worker_request_id=request.request_id,
                run_id=request.run_id,
                task_id=request.task_id,
                query_session_id=seed.session_id,
                resume_token="resume-token",
            )
            replay_runtime = CodeWorkerSessionReplayRuntime(store)
            replay_plan = replay_runtime.build_plan(session_id=seed.session_id)
            replay_event = replay_runtime.event_for_plan(replay_plan, run_id=request.run_id, task_id=request.task_id)
            turn_runtime = TurnLifecycleRuntime()
            turn_projection = turn_runtime.project(
                session_id=seed.session_id,
                worker_request_id=request.request_id,
                input_records=input_report.records,
                query_turns=request.constraints["query_turns"],
                context_snapshot=context_snapshot,
                replay_plan=replay_plan,
            )
            turn_event = turn_runtime.event_for_projection(
                turn_projection,
                run_id=request.run_id,
                task_id=request.task_id,
                node_id=request.node_id,
            )
            seed_events = foundation.seed_events(seed)
            audit = SessionFoundationAuditor().audit_seed(
                seed,
                store=store,
                events=[*seed_events, replay_event, turn_event],
            )
            audit_event = foundation_audit_event(audit)
            acceptance = SessionAcceptanceRuntime().evaluate(
                seed=seed,
                foundation_audit=audit,
                turn_lifecycle=turn_projection,
                replay_plan=replay_plan,
                require_transcript=False,
            )
            acceptance_event = SessionAcceptanceRuntime().event_for_report(
                acceptance,
                run_id=request.run_id,
                task_id=request.task_id,
                node_id=request.node_id,
            )
            lifecycle = SessionLifecycleRuntime().build_report(
                [*seed_events, replay_event, turn_event, audit_event, acceptance_event],
                session_id=seed.session_id,
                worker_request_id=request.request_id,
                require_query_engine=False,
                require_transcript=False,
            )

            self.assertTrue(seed.ok)
            self.assertTrue(replay_plan.ok)
            self.assertGreaterEqual(len(replay_plan.replay_messages), 1)
            self.assertTrue(turn_projection.ok)
            self.assertTrue(audit.ok)
            self.assertTrue(acceptance.ok)
            self.assertTrue(lifecycle.ok)
            self.assertIn("session_replay_plan", lifecycle.phase_counts)
            self.assertIn("session_acceptance", lifecycle.phase_counts)

    def test_code_worker_emits_transcript_mapping_and_lifecycle_state_on_default_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("transcript mapping")
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
                    "tool_plan": [
                        {"tool_name": "file_write", "arguments": {"path": "mapping.txt", "content": "mapped"}},
                        {"tool_name": "file_read", "arguments": {"path": "mapping.txt"}},
                    ],
                },
            )

            run = runtime.run(request)

            self.assertTrue(run.worker_result.ok)
            self.assertEqual(run.worker_result.metadata["transcript_mapping_ok"], "true")
            self.assertEqual(run.worker_result.metadata["session_lifecycle_ok"], "true")
            phases = [
                event.payload["query_session"]["phase"]
                for event in run.event_records
                if isinstance(event.payload, dict) and "query_session" in event.payload
            ]
            self.assertLess(phases.index("session_completed"), phases.index("transcript_event_mapping"))
            self.assertLess(phases.index("transcript_event_mapping"), phases.index("session_acceptance"))
            self.assertLess(phases.index("session_acceptance"), phases.index("session_lifecycle_state"))

    def test_api_projection_builder_marks_live_foundation_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("api projection")
            workspace = Path(tmpdir) / "workspace"
            artifacts = Path(tmpdir) / "artifacts"
            context = ToolExecutionContext.for_workspace(workspace, artifacts)
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_name="CodeWorkerRuntime",
                constraints={
                    "session_id": "codesession_api_projection",
                    "raw_input": "Project session foundation",
                    "query_turns": [[{"tool_name": "trace", "arguments": {"limit": 1}}]],
                },
            )
            contracts = build_productized_claude_runtime_contracts(project_root=ROOT)
            input_report = QueryInputProcessor().process_worker_request(request)
            context_snapshot = ContextAssemblyRuntime().assemble(
                request=request,
                session_id="codesession_api_projection",
                input_records=input_report.records,
                tool_specs=context.registry.list(),
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=artifacts,
                runtime_contracts=contracts,
                permission_mode="workspace",
            )
            store = CodeWorkerSessionStore(artifacts / "session-store")
            foundation = CodeWorkerSessionFoundationRuntime(store=store)
            seed = foundation.build_seed(
                request=request,
                input_report=input_report,
                context_snapshot=context_snapshot,
                session_id="codesession_api_projection",
            )
            seed_events = foundation.seed_events(seed)
            turn_projection = TurnLifecycleRuntime().project(
                session_id=seed.session_id,
                worker_request_id=request.request_id,
                input_records=input_report.records,
                query_turns=request.constraints["query_turns"],
                context_snapshot=context_snapshot,
            )
            turn_event = TurnLifecycleRuntime().event_for_projection(
                turn_projection,
                run_id=request.run_id,
                task_id=request.task_id,
                node_id=request.node_id,
            )
            audit = SessionFoundationAuditor().audit_seed(seed, store=store, events=[*seed_events, turn_event])
            acceptance = SessionAcceptanceRuntime().evaluate(
                seed=seed,
                foundation_audit=audit,
                turn_lifecycle=turn_projection,
                require_transcript=False,
            )
            acceptance_event = SessionAcceptanceRuntime().event_for_report(
                acceptance,
                run_id=request.run_id,
                task_id=request.task_id,
                node_id=request.node_id,
            )
            lifecycle = SessionLifecycleRuntime().build_report(
                [*seed_events, turn_event, foundation_audit_event(audit), acceptance_event],
                session_id=seed.session_id,
                worker_request_id=request.request_id,
                require_query_engine=False,
                require_transcript=False,
            )
            lineage = SessionLineageRuntime().build_report(project_root=ROOT, contracts=contracts)

            projection = SessionApiProjectionBuilder().build(
                contracts=contracts,
                request=request,
                input_report=input_report,
                context_snapshot=context_snapshot,
                turn_lifecycle=turn_projection,
                acceptance_report=acceptance,
                lifecycle_report=lifecycle,
                lineage_report=lineage,
            )
            payload = projection.to_dict()

            self.assertTrue(projection.ok)
            self.assertEqual(payload["ownerUnit"], "M1-02B")
            self.assertEqual(payload["sessionFoundation"]["hasSessionLifecycleState"], True)
            self.assertEqual(payload["sessionLifecycle"]["ok"], True)
            self.assertEqual(payload["sessionLineage"]["ok"], True)
            self.assertEqual(payload["metadata"]["session_api_projection_ok"], "true")


if __name__ == "__main__":
    unittest.main()
