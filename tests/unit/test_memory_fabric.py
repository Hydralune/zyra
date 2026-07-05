from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "memory",
    ROOT / "packages" / "runtime",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import ArtifactKind, EventRecord, EventType, create_task_state, to_jsonable
from zyra_memory import CompactPolicy, MemoryFabric, MemoryLayer, SQLiteStore
from zyra_runtime import LocalArtifactStore


class MemoryFabricTests(unittest.TestCase):
    def test_refresh_compact_search_and_replay_cover_m4_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            store = SQLiteStore(base / "zyra.sqlite3")
            artifacts = LocalArtifactStore(base / "artifacts")
            state = create_task_state("Research dynamic heterogeneous agents and preserve verifier evidence.")
            evidence_artifact = artifacts.write_text(
                run_id=state.run_id,
                task_id=state.task_id,
                content="Semantic evidence about requirement changes, recovery, and trajectory replay.",
                title="research evidence",
                kind=ArtifactKind.MARKDOWN,
                extension=".md",
                producer_node_id=state.root_node_id,
            )
            state.artifacts.append(evidence_artifact)

            events = _m4_fixture_events(state)
            store.append_events(events)
            store.save_checkpoint(state)

            fabric = MemoryFabric(store=store, artifact_store=artifacts)
            snapshot = fabric.refresh_task_memory(state, store.task_events(state.task_id))
            counts = snapshot.layer_counts()

            self.assertGreaterEqual(counts[str(MemoryLayer.WORKING)], 1)
            self.assertGreaterEqual(counts[str(MemoryLayer.EPISODIC)], 5)
            self.assertGreaterEqual(counts[str(MemoryLayer.SEMANTIC)], 2)
            self.assertGreaterEqual(counts[str(MemoryLayer.SKILL)], 1)
            self.assertEqual(len(store.task_memory_records(state.task_id, MemoryLayer.WORKING)), 1)
            self.assertTrue(fabric.memory_view(state, store.task_events(state.task_id), query="verification")["data"]["search_results"])

            compact = fabric.compact_context(
                state,
                store.task_events(state.task_id),
                focus="verification evidence",
                policy=CompactPolicy(tail_groups=2, max_inline_chars=1000),
            )

            self.assertIn("event_task_created", compact.preserved_event_ids)
            self.assertIn("event_change", compact.preserved_event_ids)
            self.assertIn("event_failure", compact.preserved_event_ids)
            self.assertIn("event_tool_started", compact.preserved_event_ids)
            self.assertIn("event_tool_result", compact.preserved_event_ids)
            self.assertGreater(len(compact.summarized_event_ids), 0)
            self.assertGreaterEqual(len(compact.artifact_ids), 2)
            self.assertEqual(store.task_compactions(state.task_id)[0]["compact_id"], compact.compact_id)

            compact_preview = artifacts.read_preview(compact.artifacts[-1])["content"]
            self.assertIn("Zyra Context Compact", compact_preview)
            self.assertIn("Restore Contract", compact_preview)
            self.assertIn("verification evidence", compact_preview)
            self.assertIn("tool_call_id", compact_preview)

            frames = fabric.replay_trajectory(state, store.task_events(state.task_id))
            self.assertEqual(len(frames), len(events))
            self.assertTrue(any(frame.requirement_change for frame in frames))
            self.assertTrue(any(frame.failure_injection for frame in frames))
            self.assertTrue(any(frame.verification for frame in frames))
            self.assertTrue(any(frame.worker == "CodeWorkerRuntime" for frame in frames))


def _m4_fixture_events(state) -> list[EventRecord]:
    large_output = "verification trace " * 300
    return [
        EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_id="event_task_created",
            event_type=EventType.TASK_CREATED,
            node_id=state.root_node_id,
            payload={"task": to_jsonable(state)},
        ),
        EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_id="event_route",
            event_type=EventType.TOPOLOGY_ROUTE,
            node_id=state.root_node_id,
            payload={"decision": {"selected": "CodeWorkerRuntime", "summary": "Route to code worker."}},
        ),
        EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_id="event_constraint",
            event_type=EventType.CONSTRAINT_CHECK,
            node_id=state.root_node_id,
            payload={"summary": "Constraints passed for verifier evidence.", "results": [{"ok": True}]},
        ),
        EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_id="event_tool_started",
            event_type=EventType.AGENT_MESSAGE,
            node_id=state.root_node_id,
            payload={
                "query_session": {
                    "session_id": "codesession_test",
                    "phase": "tool_call_started",
                    "tool_call_id": "tool_verify_1",
                    "tool_name": "shell",
                }
            },
        ),
        EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_id="event_tool_result",
            event_type=EventType.AGENT_MESSAGE,
            node_id=state.root_node_id,
            payload={
                "tool_call": {"tool_call_id": "tool_verify_1", "tool_name": "shell"},
                "tool_result": {
                    "tool_call_id": "tool_verify_1",
                    "ok": True,
                    "summary": "Shell verification passed.",
                    "output": {"stdout": large_output},
                },
            },
        ),
        EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_id="event_tool_completed",
            event_type=EventType.AGENT_MESSAGE,
            node_id=state.root_node_id,
            payload={
                "query_session": {
                    "session_id": "codesession_test",
                    "phase": "tool_call_completed",
                    "tool_call_id": "tool_verify_1",
                    "tool_name": "shell",
                }
            },
        ),
        EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_id="event_skill",
            event_type=EventType.SKILL_INVOKED,
            node_id=state.root_node_id,
            payload={
                "skill_invocation": {
                    "skill_name": "trace-summary",
                    "purpose": "Summarize long trajectories.",
                    "source": "claude-code-best compact",
                    "preferred_runtime": "MemoryCurator",
                }
            },
        ),
        EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_id="event_change",
            event_type=EventType.REQUIREMENT_CHANGE,
            node_id=state.root_node_id,
            payload={"raw": "Add stronger verification evidence without restarting the run."},
        ),
        EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_id="event_failure",
            event_type=EventType.FAILURE_INJECTED,
            node_id=state.root_node_id,
            payload={"raw": "node=execute transient worker failure"},
        ),
        EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_id="event_eval",
            event_type=EventType.EVALUATION,
            node_id=state.root_node_id,
            payload={"summary": "Verifier confirmed replay and compact evidence."},
        ),
    ]


if __name__ == "__main__":
    unittest.main()
