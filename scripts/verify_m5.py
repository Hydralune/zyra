from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "commands",
    ROOT / "packages" / "skills",
    ROOT / "packages" / "orchestration",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
    ROOT / "packages" / "symbolic",
    ROOT / "packages" / "scheduler",
    ROOT / "packages" / "memory",
    ROOT / "packages" / "evaluation",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import EventRecord, EventType, create_task_state
from zyra_memory import MemoryFabric, SQLiteStore
from zyra_orchestration import GraphExecutionContext, run_task_graph
from zyra_runtime import LocalArtifactStore
from zyra_scheduler import ResourceScheduler, WorkerPool, source_to_target_ledger
from zyra_symbolic import apply_failure_injection, apply_requirement_change


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        workspace = base / "workspace"
        workspace.mkdir()
        page = workspace / "m5.html"
        page.write_text("<html><body>M5 browser resource route.</body></html>", encoding="utf-8")
        store = SQLiteStore(base / "zyra.sqlite3")
        artifact_store = LocalArtifactStore(base / "artifacts")
        state = create_task_state(
            f"M5 verify: open {page.resolve().as_uri()}, preserve memory, handle requirement changes, and recover from failures."
        )
        events = run_task_graph(
            state,
            execution_context=GraphExecutionContext.from_paths(
                project_root=ROOT,
                workspace_root=workspace,
                artifact_root=base / "artifacts",
            ),
        )
        assert any(event.event_type == EventType.RESOURCE_DECISION for event in events), "resource decision missing"
        execute_node = next(node for node in state.plan_nodes.values() if node.metadata.get("stage") == "execute")
        assert execute_node.metadata.get("worker_manifest_id") == "edge-browser-worker", execute_node.metadata
        assert execute_node.metadata.get("dispatch_envelope", {}).get("backend") == "simulated_edge", execute_node.metadata

        change_event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.REQUIREMENT_CHANGE,
            node_id=state.root_node_id,
            payload={"raw": "During the same run, add stronger verifier evidence and preserve browser artifacts."},
        )
        events.append(change_event)
        events.extend(apply_requirement_change(state, change_event))

        failure_event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.FAILURE_INJECTED,
            node_id=state.root_node_id,
            payload={"raw": "BrowserWorker timeout and node failure during execute"},
        )
        events.append(failure_event)
        events.extend(apply_failure_injection(state, failure_event))

        store.append_events(events)
        store.save_checkpoint(state)
        fabric = MemoryFabric(store=store, artifact_store=artifact_store)
        snapshot = fabric.refresh_task_memory(state, store.task_events(state.task_id), persist=True)
        memory_decision = ResourceScheduler().decide(
            state,
            node=execute_node,
            events=store.task_events(state.task_id),
            memory_records=snapshot.records,
        )

        assert state.metadata.get("resource_decisions"), "state resource decision ledger missing"
        assert state.metadata.get("recovery_plans"), "recovery plan missing"
        assert any(event.event_type == EventType.RECOVERY_PLANNED for event in events), "recovery event missing"
        assert memory_decision.signals.memory_record_count >= len(snapshot.records)
        assert memory_decision.signals.failure_count >= 1
        assert memory_decision.signals.requirement_change_count >= 1
        assert len(WorkerPool().manifests()) >= 4
        assert len(source_to_target_ledger()) >= 6

        print(
            "M5 verification passed: "
            f"manifests={len(WorkerPool().manifests())}, "
            f"resource_decisions={len(state.metadata.get('resource_decisions', []))}, "
            f"recovery_plans={len(state.metadata.get('recovery_plans', []))}, "
            f"memory_records={len(snapshot.records)}, "
            f"latest_manifest={memory_decision.selected_manifest_id}"
        )


if __name__ == "__main__":
    main()
