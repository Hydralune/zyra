from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "symbolic",
    ROOT / "packages" / "orchestration",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import ConstraintSet, EventRecord, EventType, PlanNodeStatus, create_task_state
from zyra_orchestration import ensure_default_graph
from zyra_symbolic import ConstraintKeeper, TopologyRouter, apply_failure_injection, apply_requirement_change


class SymbolicControlTests(unittest.TestCase):
    def test_constraint_keeper_checks_schema_budget_and_permissions(self) -> None:
        state = create_task_state("Verify symbolic constraints.")
        node = state.plan_nodes[state.root_node_id]
        node.assigned_worker_id = "BrowserWorker"
        node.constraints = ConstraintSet(
            allowed_workers=["CodeWorkerRuntime"],
            forbidden=["blocked-term"],
            message_budget_chars=4,
        )
        node.description = "blocked-term and too long"

        results = ConstraintKeeper().check_task_state(state, node=node, transition="start")

        self.assertTrue(any(result.check_type == "permission.worker" and result.blocking for result in results))
        self.assertTrue(any(result.check_type == "policy.forbidden_terms" and result.blocking for result in results))
        self.assertTrue(any(result.check_type == "communication.low_entropy_budget" and not result.ok for result in results))

    def test_topology_router_selects_code_or_browser_from_task_profile(self) -> None:
        code_state = create_task_state("Patch code and run tests.")
        ensure_default_graph(code_state)
        execute_node = next(node for node in code_state.plan_nodes.values() if node.metadata.get("stage") == "execute")
        code_decision, code_event = TopologyRouter().route(code_state, node=execute_node)

        browser_state = create_task_state("Open https://example.com and extract browser evidence.")
        ensure_default_graph(browser_state)
        browser_execute = next(node for node in browser_state.plan_nodes.values() if node.metadata.get("stage") == "execute")
        browser_decision, _ = TopologyRouter().route(browser_state, node=browser_execute)

        self.assertEqual(code_decision.selected, "CodeWorkerRuntime")
        self.assertEqual(code_event.event_type, EventType.TOPOLOGY_ROUTE)
        self.assertEqual(browser_decision.selected, "BrowserWorker")
        self.assertEqual(browser_execute.assigned_worker_id, "BrowserWorker")

    def test_topology_router_uses_runtime_browser_plan_hints(self) -> None:
        state = create_task_state("Operate on a prepared browser page.")
        state.metadata["runtime_hints"] = {
            "browser_plan": [
                {"action": "open_url", "arguments": {"url": "file:///tmp/page.html"}},
                {"action": "extract_text"},
            ],
            "allowed_schemes": ["file"],
        }
        ensure_default_graph(state)
        execute_node = next(node for node in state.plan_nodes.values() if node.metadata.get("stage") == "execute")

        decision, _ = TopologyRouter().route(state, node=execute_node)

        self.assertEqual(decision.selected, "BrowserWorker")
        browser_candidate = next(candidate for candidate in decision.route_candidates if candidate["worker_name"] == "BrowserWorker")
        self.assertTrue(any("browser" in reason for reason in browser_candidate["reasons"]))

    def test_formal_baseline_never_reaches_scheduler_or_placement_binder(self) -> None:
        class Scheduler:
            def __init__(self) -> None:
                self.calls = 0

            def decide(self, *args, **kwargs):
                del args, kwargs
                self.calls += 1
                raise AssertionError("formal baseline reached ResourceScheduler")

        class Trigger:
            fail_closed = True

            def __init__(self) -> None:
                self.bind_calls = 0

            def __call__(self, state, node, cause_event):
                del state, node, cause_event
                return {
                    "used_baseline": True,
                    "committed": False,
                    "reroute_required": False,
                    "communication_outcome_coverage_complete": True,
                }

            def bind_resource_decision(self, *args, **kwargs):
                del args, kwargs
                self.bind_calls += 1
                raise AssertionError("formal baseline reached placement binder")

        for formal_flag in ("sealed_autonomous", "formal_benchmark"):
            with self.subTest(formal_flag=formal_flag):
                state = create_task_state("Keep formal baseline fail-closed.")
                state.metadata[formal_flag] = True
                ensure_default_graph(state)
                execute_node = next(
                    node
                    for node in state.plan_nodes.values()
                    if node.metadata.get("stage") == "execute"
                )
                scheduler = Scheduler()
                trigger = Trigger()

                decision, event = TopologyRouter(
                    resource_scheduler=scheduler,
                    topology_policy_trigger=trigger,
                ).route(state, node=execute_node)

                self.assertEqual(scheduler.calls, 0)
                self.assertEqual(trigger.bind_calls, 0)
                self.assertEqual(decision.metadata["physical_lease_ref"], "")
                self.assertNotIn(
                    "physical_placement",
                    event.payload["topology_policy"],
                )

    def test_nonformal_baseline_keeps_scheduler_compatibility(self) -> None:
        class Trigger:
            fail_closed = True

            def __init__(self) -> None:
                self.bind_calls = 0

            def __call__(self, state, node, cause_event):
                del state, node, cause_event
                return {
                    "used_baseline": True,
                    "committed": False,
                    "reroute_required": False,
                }

            def bind_resource_decision(self, *args, **kwargs):
                del args, kwargs
                self.bind_calls += 1
                return {"compatibility": "phase1_deterministic_baseline"}

        state = create_task_state("Keep non-formal baseline compatible.")
        ensure_default_graph(state)
        execute_node = next(
            node
            for node in state.plan_nodes.values()
            if node.metadata.get("stage") == "execute"
        )
        trigger = Trigger()

        decision, event = TopologyRouter(
            topology_policy_trigger=trigger,
        ).route(state, node=execute_node)

        self.assertEqual(trigger.bind_calls, 1)
        self.assertEqual(
            decision.metadata["router"],
            "m5-resource-aware-topology-router",
        )
        self.assertEqual(
            event.payload["topology_policy"]["physical_placement"]["compatibility"],
            "phase1_deterministic_baseline",
        )

    def test_requirement_change_supersedes_affected_nodes_and_creates_replan_decision(self) -> None:
        state = create_task_state("Build, execute, and verify a long task.")
        ensure_default_graph(state)
        event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.REQUIREMENT_CHANGE,
            node_id=state.root_node_id,
            payload={"raw": "tighten verify criteria without clearing state"},
        )

        events = apply_requirement_change(state, event)

        change = state.metadata["requirement_changes"][0]
        self.assertTrue(change["affected_node_ids"])
        self.assertIn(change["replan_node_id"], state.plan_nodes)
        self.assertEqual(state.plan_nodes[change["replan_node_id"]].metadata["source"], "requirement_change")
        self.assertEqual(state.status, PlanNodeStatus.NEEDS_REVISION)
        self.assertTrue(any(item.event_type == EventType.TOPOLOGY_ROUTE for item in events))
        self.assertTrue(any(item.event_type == EventType.CONSTRAINT_CHECK for item in events))
        self.assertEqual(state.decisions[-1].decision_type, "requirement_change_replan")
        self.assertIn(change["replan_node_id"], state.decisions[-1].affected_node_ids)
        self.assertTrue(state.decisions[-1].checks)

    def test_requirement_change_without_stage_graph_still_links_a_node(self) -> None:
        state = create_task_state("Initial goal can change before graph materializes.")
        event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.REQUIREMENT_CHANGE,
            node_id=state.root_node_id,
            payload={"raw": "change output format before execution"},
        )

        apply_requirement_change(state, event)

        change = state.metadata["requirement_changes"][0]
        self.assertEqual(change["affected_node_ids"], [state.root_node_id])
        self.assertIn(state.root_node_id, state.decisions[-1].affected_node_ids)

    def test_failure_injection_creates_recovery_route_with_target(self) -> None:
        state = create_task_state("Execute and recover if a node fails.")
        ensure_default_graph(state)
        execute_node = next(node for node in state.plan_nodes.values() if node.metadata.get("stage") == "execute")
        event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.FAILURE_INJECTED,
            node_id=state.root_node_id,
            payload={"raw": f"node={execute_node.node_id} timeout"},
        )

        events = apply_failure_injection(state, event)

        failure = state.metadata["failure_injections"][0]
        self.assertEqual(failure["target_node_id"], execute_node.node_id)
        self.assertIn(failure["recovery_node_id"], state.plan_nodes)
        self.assertEqual(execute_node.status, PlanNodeStatus.SUPERSEDED)
        self.assertTrue(any(item.event_type == EventType.NODE_FAILED for item in events))
        self.assertTrue(any(item.event_type == EventType.TOPOLOGY_ROUTE for item in events))
        self.assertEqual(state.decisions[-1].decision_type, "failure_recovery_route")
        self.assertTrue(state.decisions[-1].checks)


if __name__ == "__main__":
    unittest.main()
