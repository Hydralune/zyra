from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "orchestration",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
    ROOT / "packages" / "symbolic",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import (
    DecisionRecord,
    EventRecord,
    EventType,
    PlanNodeStatus,
    create_task_state,
)
from zyra_orchestration import GraphExecutionContext, ensure_default_graph, run_task_graph
from zyra_orchestration.task_graph import (
    _code_constraints,
    _commit_canonical_task_outcome,
    _consume_execution_retry_request,
    _plan_runtime_recovery,
    _run_route_node,
    _run_execute_node,
    _worker_request_metadata,
)


class _PolicySequenceRouter:
    def __init__(self, policies: list[dict[str, object]]) -> None:
        self.policies = policies
        self.calls = 0

    def route(self, state, *, node, route_type, cause_event):
        self.calls += 1
        policy = self.policies[min(self.calls - 1, len(self.policies) - 1)]
        decision = DecisionRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            decision_type=route_type,
            selected="CodeWorkerRuntime",
            summary="bounded revalidation test",
            rationale="formal strongest route",
        )
        event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.TOPOLOGY_ROUTE,
            node_id=node.node_id,
            payload={
                "topology_policy": policy,
                "cause_event_id": cause_event.event_id,
            },
        )
        return decision, event


def _formal_route_context():
    state = create_task_state("Exercise bounded formal topology recovery.")
    state.metadata["sealed_autonomous"] = True
    ensure_default_graph(state)
    route_node = next(
        node
        for node in state.plan_nodes.values()
        if node.metadata.get("stage") == "route"
    )
    cause = EventRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        event_type=EventType.RESOURCE_DECISION,
        node_id=route_node.node_id,
        payload={
            "schema": "zyra.phase2-temporal-handoff-receipt/v1",
            "acknowledged": True,
        },
    )
    return state, route_node, cause


class TaskGraphTests(unittest.TestCase):
    def test_incomplete_physical_delivery_requests_bounded_replan(self) -> None:
        from zyra_runtime import WorkerResult

        state = create_task_state("Continue an incomplete physical delivery.")
        ensure_default_graph(state)
        execute_node = next(
            node
            for node in state.plan_nodes.values()
            if node.metadata.get("stage") == "execute"
        )
        partial = WorkerResult(
            request_id="partial-call",
            ok=False,
            summary="partial delivery preserved",
            error="code_worker_delivery_incomplete",
            metadata={
                "execution_outcome": "needs_verification",
                "physical_execution_replan_requested": "true",
                "checkpointed_side_effect_recovery_requested": "true",
            },
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = GraphExecutionContext(
                project_root=root,
                workspace_root=root / "workspace",
                artifact_root=root / "artifacts",
                physical_execution_runner=lambda *_args: (
                    SimpleNamespace(worker_result=partial, event_records=[]),
                    "physical-worker",
                ),
            )

            _run_execute_node(state, execute_node, context)

        request = state.metadata["physical_execution_retry_requested"]
        self.assertEqual(request["node_id"], execute_node.node_id)
        self.assertEqual(
            request["error_code"], "code_worker_delivery_incomplete"
        )
        self.assertTrue(request["checkpointed_side_effect_recovery_requested"])

    def test_failed_stage_start_constraint_blocks_canonical_task(self) -> None:
        state = create_task_state("Keep task status aligned with a blocked graph.")
        ensure_default_graph(state)
        state.status = PlanNodeStatus.RUNNING
        for node in state.plan_nodes.values():
            stage = str(node.metadata.get("stage") or "")
            if stage in {"plan", "route"} or node.node_id == state.root_node_id:
                node.status = PlanNodeStatus.COMPLETED
            elif stage == "execute":
                node.status = PlanNodeStatus.FAILED

        events = run_task_graph(state)

        execute = next(
            node
            for node in state.plan_nodes.values()
            if node.metadata.get("stage") == "execute"
        )
        self.assertEqual(execute.status, PlanNodeStatus.BLOCKED)
        self.assertEqual(state.status, PlanNodeStatus.BLOCKED)
        self.assertTrue(
            any(
                event.node_id == execute.node_id
                and event.payload.get("transition") == "blocked"
                for event in events
            )
        )

    def test_canonical_task_outcome_is_immutable_and_diagnostics_are_supplemental(
        self,
    ) -> None:
        state = create_task_state("Persist exactly one canonical outcome.")
        state.status = PlanNodeStatus.COMPLETED
        committed = _commit_canonical_task_outcome(
            state,
            verifier={
                "schema": "verifier/v1",
                "passed": True,
                "decision_id": "ok",
                "checks": {"artifact_integrity": True, "required_tests": False},
                "delivery_verification": {
                    "schema": "delivery/v1",
                    "passed": True,
                    "checks": {"required_paths_present": True},
                },
            },
            gate={
                "schema": "gate/v1",
                "hard_conditions_passed": True,
                "decision": "exit",
                "failed_conditions": [],
            },
        )
        state.status = PlanNodeStatus.BLOCKED
        observed = _commit_canonical_task_outcome(
            state,
            verifier={"schema": "verifier/v1", "passed": False},
            gate={},
            diagnostics=({
                "schema": "zyra.task-outcome-diagnostic/v1",
                "stage": "event_sync",
                "error_type": "OSError",
                "message": "late event read failed",
                "recoverable": True,
            },),
        )

        self.assertEqual(committed, observed)
        self.assertEqual(committed["task_status"], "completed")
        self.assertEqual(
            state.metadata["canonical_task_outcome"]["verification"][
                "final_verifier"
            ]["passed"],
            True,
        )
        self.assertEqual(
            state.metadata["task_outcome_diagnostics"][0]["stage"],
            "event_sync",
        )
        verification = committed["verification"]
        self.assertEqual(
            verification["final_verifier"]["checks"],
            [
                {"name": "artifact_integrity", "status": "passed", "passed": True},
                {"name": "required_tests", "status": "failed", "passed": False},
            ],
        )
        self.assertEqual(
            verification["delivery_verifier"]["checks"],
            [{"name": "required_paths_present", "status": "passed", "passed": True}],
        )
        self.assertEqual(
            verification["command_evidence"],
            {"status": "not_recorded", "receipts": []},
        )

    def test_benchmark_closeout_window_stops_runtime_recovery_dispatch(self) -> None:
        state = create_task_state("Do not exceed the external benchmark deadline.")
        ensure_default_graph(state)
        state.metadata["runtime_hints"] = {
            "external_deadline_epoch_ms": int(time.time() * 1000) + 60_000,
            "benchmark_closeout_reserve_seconds": 120,
        }
        execute_node = next(
            node
            for node in state.plan_nodes.values()
            if node.metadata.get("stage") == "execute"
        )

        events = _plan_runtime_recovery(
            state,
            execute_node,
            error=TimeoutError("runtime deadline reached"),
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].payload["schema"],
            "zyra.benchmark-deadline-terminal-recovery/v1",
        )
        self.assertFalse(events[0].payload["automatic_execution_retry_allowed"])
        self.assertEqual(execute_node.metadata["recovery_plan"]["action"], "stop")

    def test_physical_recovery_budget_admits_three_provider_routes(self) -> None:
        state = create_task_state("Exercise the bounded provider fallback chain.")

        for expected_pass in (1, 2):
            state.metadata["physical_execution_retry_requested"] = {
                "node_id": f"execute-{expected_pass}",
                "error_code": "node_provider_failure",
            }
            request = _consume_execution_retry_request(state)
            self.assertIsNotNone(request)
            self.assertEqual(
                state.metadata["physical_execution_recovery_passes"],
                expected_pass,
            )

        state.metadata["physical_execution_retry_requested"] = {
            "node_id": "execute-3",
            "error_code": "node_provider_failure",
        }
        self.assertIsNone(_consume_execution_retry_request(state))
        self.assertEqual(state.metadata["physical_execution_recovery_passes"], 2)

    def test_physical_retry_exhaustion_projects_one_terminal_task_outcome(self) -> None:
        from zyra_runtime import WorkerResult

        state = create_task_state("Terminalize a generic exhausted physical task.")
        ensure_default_graph(state)
        state.status = PlanNodeStatus.RUNNING
        state.metadata["physical_execution_recovery_passes"] = 2
        for node in state.plan_nodes.values():
            stage = str(node.metadata.get("stage") or "")
            if node.node_id == state.root_node_id or stage in {"plan", "route"}:
                node.status = PlanNodeStatus.COMPLETED
            else:
                node.status = PlanNodeStatus.PENDING
        failure = WorkerResult(
            request_id="terminal-worker-call",
            ok=False,
            summary="terminal physical worker failure",
            error="node_code_worker_failed",
            metadata={"physical_execution_replan_requested": "true"},
        )
        projections: list[tuple[str, PlanNodeStatus]] = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = GraphExecutionContext(
                project_root=root,
                workspace_root=root / "workspace",
                artifact_root=root / "artifacts",
                physical_execution_runner=lambda *_args: (
                    SimpleNamespace(worker_result=failure, event_records=[]),
                    "physical-worker",
                ),
                execution_state_projector=lambda projected, _events, phase: (
                    projections.append((phase, projected.status))
                ),
            )
            events = run_task_graph(state, execution_context=context)

        self.assertEqual(state.status, PlanNodeStatus.FAILED)
        self.assertEqual(projections, [("physical_retry_exhausted", PlanNodeStatus.FAILED)])
        self.assertEqual(
            state.metadata["canonical_task_outcome"]["task_status"],
            "failed",
        )
        self.assertTrue(state.metadata["canonical_task_outcome"]["terminal"])
        self.assertEqual(
            sum(
                event.payload.get("schema")
                == "zyra.physical-retry-exhausted-task-projection/v1"
                for event in events
            ),
            1,
        )

    def test_live_physical_retry_checkpoints_running_without_terminal_projection(self) -> None:
        from zyra_runtime import WorkerResult

        state = create_task_state("Keep a generic admitted physical retry live.")
        ensure_default_graph(state)
        state.status = PlanNodeStatus.RUNNING
        for node in state.plan_nodes.values():
            stage = str(node.metadata.get("stage") or "")
            if node.node_id == state.root_node_id or stage in {"plan", "route"}:
                node.status = PlanNodeStatus.COMPLETED
            else:
                node.status = PlanNodeStatus.PENDING
        failure = WorkerResult(
            request_id="retryable-worker-call",
            ok=False,
            summary="checkpointed partial delivery",
            error="code_worker_delivery_incomplete",
            metadata={
                "physical_execution_replan_requested": "true",
                "checkpointed_side_effect_recovery_requested": "true",
            },
        )
        success = WorkerResult(
            request_id="recovered-worker-call",
            ok=True,
            summary="recovered physical execution",
        )
        calls = 0
        projections: list[tuple[str, PlanNodeStatus]] = []

        def runner(*_args):
            nonlocal calls
            calls += 1
            if calls == 1:
                return SimpleNamespace(worker_result=failure, event_records=[]), "physical-worker"
            return SimpleNamespace(worker_result=success, event_records=[]), "physical-worker"

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = GraphExecutionContext(
                project_root=root,
                workspace_root=root / "workspace",
                artifact_root=root / "artifacts",
                physical_execution_runner=runner,
                execution_state_projector=lambda projected, _events, phase: (
                    projections.append((phase, projected.status))
                ),
            )
            run_task_graph(state, execution_context=context)

        self.assertGreaterEqual(calls, 2)
        self.assertEqual(projections[0], ("physical_retry_admitted", PlanNodeStatus.RUNNING))
        self.assertNotIn(
            "physical_execution_terminal_projection",
            state.metadata,
        )

    def test_formal_route_revalidates_one_covered_baseline_before_placement(self) -> None:
        state = create_task_state("Revalidate one transient formal baseline.")
        state.metadata["sealed_autonomous"] = True
        ensure_default_graph(state)
        route_node = next(
            node
            for node in state.plan_nodes.values()
            if node.metadata.get("stage") == "route"
        )

        class Router:
            def __init__(self) -> None:
                self.calls = 0

            def route(self, selected_state, *, node, route_type, cause_event):
                del node, route_type
                self.calls += 1
                if self.calls == 1:
                    policy = {
                        "used_baseline": True,
                        "committed": False,
                        "reroute_required": True,
                    }
                elif self.calls == 2:
                    policy = {
                        "used_baseline": True,
                        "committed": False,
                        "reroute_required": False,
                        "communication_outcome_coverage_complete": True,
                        "communication_outcome_count": 7,
                        "execution_receipt": {
                            "degraded_reason": "transient_snapshot"
                        },
                    }
                else:
                    policy = {
                        "used_baseline": False,
                        "committed": True,
                        "reroute_required": False,
                    }
                decision = DecisionRecord(
                    run_id=selected_state.run_id,
                    task_id=selected_state.task_id,
                    decision_type="worker_route",
                    selected="CodeWorkerRuntime",
                    summary="bounded revalidation",
                    rationale="formal strongest route",
                )
                event = EventRecord(
                    run_id=selected_state.run_id,
                    task_id=selected_state.task_id,
                    event_type=EventType.TOPOLOGY_ROUTE,
                    node_id=route_node.node_id,
                    payload={
                        "topology_policy": policy,
                        "cause_event_id": cause_event.event_id,
                    },
                )
                return decision, event

        router = Router()
        cause = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.RESOURCE_DECISION,
            node_id=route_node.node_id,
            payload={
                "schema": "zyra.phase2-temporal-handoff-receipt/v1",
                "acknowledged": True,
            },
        )

        events = _run_route_node(
            state,
            route_node,
            router,
            [],
            cause_event=cause,
        )

        self.assertEqual(router.calls, 3)
        self.assertEqual(state.metadata["phase2_topology_revalidation_count"], 1)
        self.assertEqual(route_node.status, PlanNodeStatus.COMPLETED)
        self.assertEqual(
            sum(item.event_type is EventType.TOPOLOGY_ROUTE for item in events),
            3,
        )

    def test_formal_route_revalidation_guard_survives_failed_reentry(self) -> None:
        baseline = {
            "used_baseline": True,
            "committed": False,
            "reroute_required": False,
            "communication_outcome_coverage_complete": True,
            "operator_selection": {
                "degraded_reason": "topology_strongest_not_committed"
            },
        }
        state, route_node, cause = _formal_route_context()
        router = _PolicySequenceRouter([baseline, baseline])

        with self.assertRaisesRegex(
            RuntimeError,
            "topology_strongest_not_committed",
        ):
            _run_route_node(
                state,
                route_node,
                router,
                [],
                cause_event=cause,
            )
        self.assertEqual(router.calls, 2)
        self.assertEqual(state.metadata["phase2_topology_revalidation_count"], 1)
        self.assertEqual(
            state.metadata["phase2_topology_revalidated_route_nodes"],
            [route_node.node_id],
        )

        with self.assertRaisesRegex(RuntimeError, '"revalidation_count": 1'):
            _run_route_node(
                state,
                route_node,
                router,
                [],
                cause_event=cause,
            )
        self.assertEqual(router.calls, 3)
        self.assertEqual(state.metadata["phase2_topology_revalidation_count"], 1)

    def test_formal_route_incomplete_coverage_does_not_revalidate(self) -> None:
        state, route_node, cause = _formal_route_context()
        router = _PolicySequenceRouter(
            [
                {
                    "used_baseline": True,
                    "committed": False,
                    "reroute_required": False,
                    "communication_outcome_coverage_complete": False,
                }
            ]
        )

        with self.assertRaisesRegex(RuntimeError, '"coverage_complete": false'):
            _run_route_node(
                state,
                route_node,
                router,
                [],
                cause_event=cause,
            )

        self.assertEqual(router.calls, 1)
        self.assertNotIn("phase2_topology_revalidation_count", state.metadata)

    def test_formal_route_revalidation_rejects_uncommitted_nonbaseline(self) -> None:
        state, route_node, cause = _formal_route_context()
        router = _PolicySequenceRouter(
            [
                {
                    "used_baseline": True,
                    "committed": False,
                    "reroute_required": False,
                    "communication_outcome_coverage_complete": True,
                },
                {
                    "used_baseline": False,
                    "committed": False,
                    "reroute_required": False,
                    "degraded_reason": "canonical_commit_missing",
                },
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "canonical_commit_missing"):
            _run_route_node(
                state,
                route_node,
                router,
                [],
                cause_event=cause,
            )

        self.assertEqual(router.calls, 2)
        self.assertEqual(state.metadata["phase2_topology_revalidation_count"], 1)

    def test_formal_route_second_reroute_fails_with_actual_reason(self) -> None:
        state, route_node, cause = _formal_route_context()
        router = _PolicySequenceRouter(
            [
                {
                    "used_baseline": True,
                    "committed": False,
                    "reroute_required": True,
                },
                {
                    "used_baseline": True,
                    "committed": False,
                    "reroute_required": True,
                    "reroute_reason": "window_still_incomplete",
                },
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "window_still_incomplete"):
            _run_route_node(
                state,
                route_node,
                router,
                [],
                cause_event=cause,
            )

        self.assertEqual(router.calls, 2)
        self.assertNotIn("phase2_topology_revalidation_count", state.metadata)

    def test_recovery_route_projection_overrides_stale_dispatch_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Consume the canonical recovery route.")
            state.metadata.update(
                {
                    "worker_pool": {
                        "worker_id": "recovered-worker",
                        "lease_id": "worker-lease-recovery",
                        "attempt_id": "worker-attempt-recovery",
                    },
                    "backend_route": {
                        "backend_id": "recovered-backend",
                        "lease_id": "backend-lease-recovery",
                    },
                    "provider_route": {"route_id": "provider-route-recovery"},
                    "runtime_hints": {"session_id": "query:run:task:recovery:plan"},
                }
            )
            ensure_default_graph(state)
            execute = next(
                node
                for node in state.plan_nodes.values()
                if node.metadata.get("stage") == "execute"
            )
            context = GraphExecutionContext.from_paths(
                project_root=ROOT,
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
            )

            metadata = _worker_request_metadata(
                state,
                execute,
                context,
                resource_decision={
                    "decision_id": "stale-decision",
                    "selected_manifest_id": "stale-worker",
                    "selected_backend_id": "stale-backend",
                    "provider_route_id": "stale-provider-route",
                },
                selected_manifest={"worker_id": "stale-worker"},
            )

            self.assertEqual(metadata["worker_manifest_id"], "recovered-worker")
            self.assertEqual(metadata["worker_lease_id"], "worker-lease-recovery")
            self.assertEqual(metadata["worker_attempt_id"], "worker-attempt-recovery")
            self.assertEqual(metadata["backend_id"], "recovered-backend")
            self.assertEqual(metadata["backend_lease_id"], "backend-lease-recovery")
            self.assertEqual(metadata["provider_route_id"], "provider-route-recovery")
            self.assertEqual(
                _code_constraints(state, state.metadata["runtime_hints"])["session_id"],
                "query:run:task:recovery:plan",
            )

    def test_default_graph_runs_to_completion(self) -> None:
        state = create_task_state("Run a graph.")
        created_events = ensure_default_graph(state)
        run_events = run_task_graph(state)

        self.assertEqual(len(created_events), 5)
        self.assertEqual(state.status, PlanNodeStatus.COMPLETED)
        self.assertEqual(len(state.metadata["stage_order"]), 5)
        self.assertTrue(
            any(event.event_type == EventType.NODE_UPDATED for event in run_events)
        )
        self.assertTrue(any(event.event_type == EventType.CONSTRAINT_CHECK for event in run_events))
        self.assertTrue(any(event.event_type == EventType.TOPOLOGY_ROUTE for event in run_events))
        self.assertTrue(any(event.event_type == EventType.AGENT_MESSAGE for event in run_events))
        self.assertTrue(state.decisions)
        self.assertTrue(state.decisions[0].checks)
        self.assertEqual(state.metadata["graph_version"], "m3-symbolic-v1")

    def test_graph_execute_stage_can_call_code_worker_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Run a graph through a worker runtime.")
            context = GraphExecutionContext.from_paths(
                project_root=ROOT,
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
            )

            events = run_task_graph(state, execution_context=context)

            self.assertEqual(state.status, PlanNodeStatus.COMPLETED)
            self.assertTrue(any("tool_result" in event.payload for event in events))
            self.assertGreaterEqual(state.budget.tool_calls, 2)
            execute_nodes = [
                node
                for node in state.plan_nodes.values()
                if node.metadata.get("stage") == "execute"
            ]
            self.assertEqual(execute_nodes[0].assigned_worker_id, "CodeWorkerRuntime")
            self.assertTrue(any(event.event_type == EventType.TOPOLOGY_ROUTE for event in events))
            self.assertTrue((Path(tmpdir) / "workspace" / "runs" / state.task_id / "execution-summary.md").exists())

    def test_graph_marks_task_failed_when_worker_runtime_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            outside = Path(tmpdir) / "outside.html"
            outside.write_text("<html><body>outside</body></html>", encoding="utf-8")
            state = create_task_state("Run a graph through a failing browser worker.")
            state.metadata["runtime_hints"] = {
                "preferred_worker": "BrowserWorker",
                "browser_backend": "static",
                "browser_plan": [{"action": "open_url", "arguments": {"url": outside.resolve().as_uri()}}],
                "allowed_schemes": ["file"],
            }
            context = GraphExecutionContext.from_paths(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )

            events = run_task_graph(state, execution_context=context)

            self.assertEqual(state.status, PlanNodeStatus.FAILED)
            self.assertTrue(any("browser_result" in event.payload for event in events))
            execute_nodes = [
                node
                for node in state.plan_nodes.values()
                if node.metadata.get("stage") == "execute"
            ]
            self.assertEqual(execute_nodes[0].status, PlanNodeStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
