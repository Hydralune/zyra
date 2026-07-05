from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "orchestration",
    ROOT / "packages" / "memory",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "symbolic",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import EventRecord, EventType, PlanNodeStatus, create_task_state
from zyra_memory import SQLiteStore
from zyra_orchestration import ensure_default_graph, run_task_graph


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = SQLiteStore(Path(tmpdir) / "m1.sqlite3")

        state = create_task_state("Verify the M1 task graph and checkpoint store.")
        created = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.TASK_CREATED,
            node_id=state.root_node_id,
            payload={"user_goal": state.user_goal},
        )
        events = [created, *ensure_default_graph(state), *run_task_graph(state)]
        store.append_events(events)
        store.save_checkpoint(state)

        loaded = store.load_task(state.task_id)
        assert loaded is not None
        assert loaded.status == PlanNodeStatus.COMPLETED
        assert loaded.metadata["graph_version"] == "m3-symbolic-v1"
        assert len(loaded.metadata["stage_order"]) == 5
        assert len(store.task_events(state.task_id)) >= 1 + 5 + 1
        assert store.list_tasks()[0]["task_id"] == state.task_id

    print("M1 verification passed")


if __name__ == "__main__":
    main()
