from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE_PATH = ROOT / "packages" / "core"
if str(CORE_PATH) not in sys.path:
    sys.path.insert(0, str(CORE_PATH))

from zyra_core import (
    AgentMessage,
    AgentRole,
    ConstraintSet,
    DecisionRecord,
    EventRecord,
    EventType,
    MessageIntent,
    create_task_state,
    task_state_from_json,
    to_jsonable,
)


class CoreModelTests(unittest.TestCase):
    def test_task_state_serializes_with_runtime_ids(self) -> None:
        state = create_task_state("Run a complex task.")
        encoded = json.dumps(to_jsonable(state))
        decoded = json.loads(encoded)

        self.assertTrue(decoded["run_id"].startswith("run_"))
        self.assertTrue(decoded["task_id"].startswith("task_"))
        self.assertTrue(decoded["root_node_id"].startswith("node_"))
        self.assertIn(decoded["root_node_id"], decoded["plan_nodes"])

    def test_event_record_serializes_enum_values(self) -> None:
        state = create_task_state("Track an event.")
        event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.TASK_CREATED,
            node_id=state.root_node_id,
            payload={"task": to_jsonable(state)},
        )
        decoded = json.loads(json.dumps(to_jsonable(event)))

        self.assertEqual(decoded["event_type"], "task_created")
        self.assertEqual(decoded["payload"]["task"]["task_id"], state.task_id)

    def test_m3_structured_fields_round_trip(self) -> None:
        state = create_task_state("Route a structured M3 task.")
        root = state.plan_nodes[state.root_node_id]
        root.constraints = ConstraintSet(
            allowed_workers=["CodeWorkerRuntime"],
            output_schema={"type": "object"},
            max_tool_calls=8,
            message_budget_chars=512,
        )
        root.expected_output_schema = {"type": "object", "required": ["result"]}
        root.completion_criteria = ["Produce a structured result."]
        state.decisions.append(
            DecisionRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=root.node_id,
                decision_type="topology_route",
                selected="CodeWorkerRuntime",
                summary="Route to code worker.",
                route_candidates=[{"worker_name": "CodeWorkerRuntime", "score": 9}],
                affected_node_ids=[root.node_id],
                state_delta={"assigned_worker_id": "CodeWorkerRuntime"},
            )
        )

        decoded = json.loads(json.dumps(to_jsonable(state)))
        loaded = task_state_from_json(decoded)
        loaded_root = loaded.plan_nodes[loaded.root_node_id]

        self.assertEqual(loaded_root.constraints.allowed_workers, ["CodeWorkerRuntime"])
        self.assertEqual(loaded_root.constraints.max_tool_calls, 8)
        self.assertEqual(loaded_root.expected_output_schema["required"], ["result"])
        self.assertEqual(loaded.decisions[0].decision_type, "topology_route")
        self.assertEqual(loaded.decisions[0].selected, "CodeWorkerRuntime")

    def test_agent_message_serializes_low_entropy_payload(self) -> None:
        state = create_task_state("Send structured message.")
        message = AgentMessage(
            run_id=state.run_id,
            task_id=state.task_id,
            sender_role=AgentRole.SUPERVISOR,
            receiver_role=AgentRole.ROUTER,
            intent=MessageIntent.DECISION,
            content="Route execute node.",
            summary="Route",
            expected_output_schema={"type": "object"},
            state_delta={"node_id": state.root_node_id},
            message_budget_chars=256,
        )
        decoded = json.loads(json.dumps(to_jsonable(message)))

        self.assertEqual(decoded["summary"], "Route")
        self.assertEqual(decoded["receiver_role"], "router")
        self.assertEqual(decoded["message_budget_chars"], 256)


if __name__ == "__main__":
    unittest.main()
