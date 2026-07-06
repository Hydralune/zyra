from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_runtime import (  # noqa: E402
    QueryMessageRole,
    QuerySession,
    QueryStreamEventType,
    StopReason,
    TurnLifecyclePhase,
    replay_from_snapshot,
    snapshot_checkpoint_metadata,
)


class QuerySessionLifecycleTests(unittest.TestCase):
    def test_normal_turn_builds_parent_uuid_chain_snapshot_and_replay(self) -> None:
        session = QuerySession(
            run_id="run-test",
            task_id="task-test",
            node_id="node-code",
            worker_request_id="workerreq-test",
            source_contract={"session_contract": {"source": "claude-code-best"}},
        )

        turn = session.start_turn(1, user_content="Read and summarize the file.")
        session.start_stream_request(turn_id=turn.turn_id, metadata={"source_path": "src/query.ts"})
        session.start_assistant_message(turn_id=turn.turn_id)
        session.append_assistant_delta("I will read the target file.")
        session.record_tool_call(tool_call_id="tool-call-1", tool_name="file_read", turn_id=turn.turn_id)
        session.record_tool_result(
            tool_call_id="tool-call-1",
            tool_name="file_read",
            summary="Read target.txt",
            ok=True,
            artifacts=["artifact-file"],
            turn_id=turn.turn_id,
        )
        session.end_turn(ok=True, stop_reason=StopReason.END_TURN)
        session.complete_session(ok=True, stop_reason=StopReason.SESSION_COMPLETED)

        snapshot = session.snapshot_payload()

        self.assertEqual(snapshot["session_id"], session.session_id)
        self.assertTrue(snapshot["resume_token"].startswith(session.session_id))
        self.assertTrue(snapshot["consistency"]["ok"])
        self.assertEqual(snapshot["stats"]["turn_count"], 1)
        self.assertEqual(snapshot["stats"]["tool_message_count"], 1)
        self.assertGreaterEqual(snapshot["stats"]["transcript_entry_count"], 8)
        self.assertGreaterEqual(len(session.chain_from_leaf()), 3)
        self.assertEqual(snapshot["turns"][0]["phase"], str(TurnLifecyclePhase.COMPLETED))

        restored = QuerySession.restore(snapshot)
        self.assertEqual(restored.session_id, session.session_id)
        self.assertEqual(restored.resume_token, session.resume_token)
        self.assertTrue(restored.consistency_report()["ok"])
        replay = restored.replay(after_sequence=2)
        self.assertTrue(replay)
        self.assertTrue(all(item["sequence"] > 2 for item in replay))

        metadata = snapshot_checkpoint_metadata(snapshot)
        self.assertEqual(metadata["query_session_id"], session.session_id)
        self.assertEqual(metadata["query_session_consistent"], "true")
        self.assertEqual(metadata["query_session_turns"], "1")

    def test_error_and_continue_are_recorded_without_breaking_snapshot(self) -> None:
        session = QuerySession(
            run_id="run-test",
            task_id="task-test",
            node_id="node-code",
            worker_request_id="workerreq-test",
        )
        turn = session.start_turn(1, user_content="Run shell and continue on failure.")
        session.start_stream_request(turn_id=turn.turn_id)
        session.start_assistant_message(turn_id=turn.turn_id)
        session.append_assistant_delta("Running the shell command.")
        session.record_tool_call(tool_call_id="tool-call-1", tool_name="shell", turn_id=turn.turn_id)
        session.record_tool_result(
            tool_call_id="tool-call-1",
            tool_name="shell",
            summary="Shell exited with code 1",
            ok=False,
            error="non_zero_exit",
            turn_id=turn.turn_id,
        )
        session.record_continue(reason=StopReason.CONTINUE_REQUESTED, error="non_zero_exit")
        session.append_assistant_delta("Continuing with a fallback read-only step.")
        session.end_turn(ok=True, stop_reason=StopReason.END_TURN)
        session.complete_session(ok=True)

        snapshot = session.snapshot_payload()
        event_types = [
            item["event_type"]
            for item in snapshot["transcript"]
            if item["type"] == "stream_event" and item.get("event_type")
        ]

        self.assertIn(str(QueryStreamEventType.CONTINUE), event_types)
        self.assertIn(str(QueryStreamEventType.MESSAGE_DELTA), event_types)
        self.assertEqual(snapshot["stats"]["continue_count"], 1)
        self.assertTrue(snapshot["consistency"]["ok"])

    def test_snapshot_replay_helper_is_stable_for_checkpoint_consumers(self) -> None:
        session = QuerySession(
            run_id="run-test",
            task_id="task-test",
            node_id=None,
            worker_request_id="workerreq-test",
        )
        turn = session.start_turn(1, user_content="One turn.")
        session.start_stream_request(turn_id=turn.turn_id)
        session.end_turn(ok=True)
        session.complete_session(ok=True)
        snapshot = session.snapshot_payload()

        encoded = json.dumps(snapshot, ensure_ascii=False)
        decoded = json.loads(encoded)
        replay = replay_from_snapshot(decoded, after_sequence=0, limit=3)

        self.assertEqual(len(replay), 3)
        self.assertEqual(replay[0]["type"], "session_metadata")
        self.assertEqual(snapshot_checkpoint_metadata(decoded)["query_session_status"], "completed")

    def test_message_lifecycle_tracks_roles_and_tool_ids(self) -> None:
        session = QuerySession(
            run_id="run-test",
            task_id="task-test",
            node_id="node-code",
            worker_request_id="workerreq-test",
        )
        turn = session.start_turn(1, user_content="Call a tool.")
        session.start_assistant_message(turn_id=turn.turn_id)
        session.record_tool_result(
            tool_call_id="tool-call-42",
            tool_name="trace",
            summary="Trace read",
            ok=True,
            turn_id=turn.turn_id,
        )

        tool_messages = [message for message in session.messages if message.role == QueryMessageRole.TOOL]

        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0].tool_call_id, "tool-call-42")
        self.assertEqual(tool_messages[0].tool_name, "trace")
        self.assertEqual(tool_messages[0].content, "Trace read")


if __name__ == "__main__":
    unittest.main()
