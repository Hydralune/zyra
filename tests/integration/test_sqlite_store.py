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
from zyra_memory import SQLiteStore
from zyra_orchestration import ensure_default_graph, run_task_graph


class SQLiteStoreTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
