from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "orchestration",
    ROOT / "packages" / "memory",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "symbolic",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import EventRecord, EventType, PlanNodeStatus, create_task_state, now_iso
from zyra_memory import SQLiteStore, canonical_user_input_digest
from zyra_orchestration import ensure_default_graph, run_task_graph


class SQLiteStoreTests(unittest.TestCase):
    def test_running_checkpoint_preserves_concurrent_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStore(Path(tmpdir) / "zyra.sqlite3")
            executor = create_task_state("Long-running task")
            store.save_checkpoint(executor)
            control = store.load_task(executor.task_id)
            control.metadata.update(session_title="Renamed from CLI", session_title_revision=2)
            store.save_checkpoint(control)
            executor.status = PlanNodeStatus.COMPLETED
            store.save_checkpoint(executor)
            final = store.load_task(executor.task_id)
            self.assertEqual(final.metadata["session_title"], "Renamed from CLI")
            self.assertEqual(final.status, PlanNodeStatus.COMPLETED)

    def test_checkpoint_can_be_reloaded_from_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStore(Path(tmpdir) / "zyra.sqlite3")
            state = create_task_state("Persist a checkpoint.")
            event = EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.TASK_CREATED,
                node_id=state.root_node_id,
                payload={"user_goal": state.user_goal},
            )
            events = [event, *ensure_default_graph(state), *run_task_graph(state)]
            store.append_events(events)
            store.save_checkpoint(state)

            reloaded_store = SQLiteStore(Path(tmpdir) / "zyra.sqlite3")
            loaded = reloaded_store.load_task(state.task_id)

            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.status, PlanNodeStatus.COMPLETED)
            self.assertGreaterEqual(len(reloaded_store.task_events(state.task_id)), 7)

    def test_events_keep_insert_order_for_identical_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStore(Path(tmpdir) / "zyra.sqlite3")
            state = create_task_state("Persist ordered events.")
            timestamp = now_iso()
            first = EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.SYSTEM_NOTICE,
                created_at=timestamp,
                payload={"order": 1},
            )
            second = EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.SYSTEM_NOTICE,
                created_at=timestamp,
                payload={"order": 2},
            )

            store.append_events([first, second])
            events = store.task_events(state.task_id)

            self.assertEqual([event["payload"]["order"] for event in events], [1, 2])

    def test_task_list_projects_durable_session_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStore(Path(tmpdir) / "zyra.sqlite3")
            state = create_task_state("Continue one conversation.")
            state.metadata["query_session_id"] = "session:conversation-001"
            store.save_checkpoint(state)

            tasks = store.list_tasks()

            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["task_id"], state.task_id)
            self.assertEqual(tasks[0]["session_id"], "session:conversation-001")
            self.assertNotIn("checkpoint_json", tasks[0])

    def test_user_input_request_survives_reopen_and_accepts_one_exact_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "zyra.sqlite3"
            store = SQLiteStore(path)
            state = create_task_state("Ask the user before continuing.")
            store.save_checkpoint(state)
            questions = [
                {
                    "header": "范围",
                    "id": "scope",
                    "question": "这次修改采用哪个范围？",
                    "options": [
                        {"label": "最小修改", "description": "只完成当前请求。"},
                        {"label": "完整重构", "description": "同步整理相关模块。"},
                    ],
                }
            ]
            request = store.create_user_input_request(
                request_id="request_exact",
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                tool_call_id="toolcall_exact",
                request_digest=canonical_user_input_digest(questions),
                questions=questions,
                created_at=now_iso(),
            )

            reopened = SQLiteStore(path)
            pending = reopened.user_input_requests(
                state.task_id,
                include_terminal=False,
            )
            self.assertEqual([item["request_id"] for item in pending], ["request_exact"])
            answers = {"scope": {"answers": ["完整重构"]}}
            answered = reopened.answer_user_input_request(
                task_id=state.task_id,
                request_id=request["request_id"],
                expected_revision=request["revision"],
                answer_id="answer_exact",
                answer_digest=canonical_user_input_digest(answers),
                answers=answers,
                responder="test",
                answered_at=now_iso(),
            )

            self.assertEqual(answered["status"], "answered")
            self.assertEqual(answered["revision"], 1)
            self.assertEqual(answered["answers"], answers)
            self.assertEqual(
                reopened.answer_user_input_request(
                    task_id=state.task_id,
                    request_id=request["request_id"],
                    expected_revision=request["revision"],
                    answer_id="answer_exact",
                    answer_digest=canonical_user_input_digest(answers),
                    answers=answers,
                    responder="test",
                    answered_at=now_iso(),
                ),
                answered,
            )
            self.assertEqual(
                [event["payload"]["phase"] for event in reopened.task_events(state.task_id)],
                ["requested", "answered"],
            )


if __name__ == "__main__":
    unittest.main()
