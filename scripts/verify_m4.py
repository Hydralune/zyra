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
    ROOT / "packages" / "memory",
    ROOT / "packages" / "evaluation",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import ArtifactKind, EventRecord, EventType, create_task_state, to_jsonable
from zyra_memory import CompactPolicy, MemoryFabric, MemoryLayer, SQLiteStore
from zyra_orchestration import GraphExecutionContext, run_task_graph
from zyra_runtime import LocalArtifactStore
from zyra_symbolic import apply_failure_injection, apply_requirement_change


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        store = SQLiteStore(base / "zyra.sqlite3")
        artifacts = LocalArtifactStore(base / "artifacts")
        state = create_task_state(
            "M4 verify: preserve long-horizon goals, route decisions, requirement changes, "
            "failure recovery, worker traces, and verifier evidence."
        )
        state.constraints.requirements.append("Do not restart the run when requirements change.")
        evidence = artifacts.write_text(
            run_id=state.run_id,
            task_id=state.task_id,
            content="M4 semantic evidence: memory fabric, compact restore, trajectory replay.",
            title="M4 semantic evidence",
            kind=ArtifactKind.MARKDOWN,
            extension=".md",
            producer_node_id=state.root_node_id,
        )
        state.artifacts.append(evidence)

        events: list[EventRecord] = [
            EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.TASK_CREATED,
                node_id=state.root_node_id,
                payload={"task": to_jsonable(state)},
            )
        ]
        events.extend(
            run_task_graph(
                state,
                execution_context=GraphExecutionContext.from_paths(
                    project_root=ROOT,
                    workspace_root=base / "workspace",
                    artifact_root=base / "artifacts",
                ),
            )
        )
        events.append(
            EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.SKILL_INVOKED,
                node_id=state.root_node_id,
                payload={
                    "skill_invocation": {
                        "skill_name": "trace-summary",
                        "purpose": "Summarize long trajectories and compact context.",
                        "source": "claude-code-best compact",
                        "preferred_runtime": "MemoryCurator",
                        "allowed_tools": ["trace", "artifact_write"],
                    }
                },
            )
        )
        large_tool_event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_id="event_m4_large_tool_result",
            event_type=EventType.AGENT_MESSAGE,
            node_id=state.root_node_id,
            payload={
                "query_session": {
                    "session_id": "codesession_m4_verify",
                    "phase": "tool_call_completed",
                    "tool_call_id": "tool_m4_verify",
                    "tool_name": "shell",
                },
                "tool_call": {"tool_call_id": "tool_m4_verify", "tool_name": "shell"},
                "tool_result": {
                    "tool_call_id": "tool_m4_verify",
                    "ok": True,
                    "summary": "Large verifier output captured.",
                    "output": {"stdout": "verifier output " * 800},
                },
            },
        )
        events.append(large_tool_event)
        change_event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.REQUIREMENT_CHANGE,
            node_id=state.root_node_id,
            payload={"raw": "During the same run, add stronger replay evidence."},
        )
        events.append(change_event)
        events.extend(apply_requirement_change(state, change_event))
        failure_event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.FAILURE_INJECTED,
            node_id=state.root_node_id,
            payload={"raw": "node=execute transient worker failure"},
        )
        events.append(failure_event)
        events.extend(apply_failure_injection(state, failure_event))

        store.append_events(events)
        store.save_checkpoint(state)

        fabric = MemoryFabric(store=store, artifact_store=artifacts)
        snapshot = fabric.refresh_task_memory(state, store.task_events(state.task_id))
        counts = snapshot.layer_counts()
        assert counts[str(MemoryLayer.WORKING)] >= 1, counts
        assert counts[str(MemoryLayer.EPISODIC)] >= 8, counts
        assert counts[str(MemoryLayer.SEMANTIC)] >= 2, counts
        assert counts[str(MemoryLayer.SKILL)] >= 1, counts

        search = fabric.memory_view(state, store.task_events(state.task_id), query="replay")["data"]["search_results"]
        assert search, "Expected replay search to return memory records."

        compact = fabric.compact_context(
            state,
            store.task_events(state.task_id),
            focus="replay evidence and verifier output",
            policy=CompactPolicy(tail_groups=6, max_inline_chars=1200),
        )
        assert large_tool_event.event_id in compact.preserved_event_ids, compact
        assert change_event.event_id in compact.preserved_event_ids, compact
        assert failure_event.event_id in compact.preserved_event_ids, compact
        assert compact.summarized_event_ids, compact
        assert len(compact.artifact_ids) >= 2, compact
        assert compact.memory_ids, compact
        assert store.task_compactions(state.task_id), "Compact result was not persisted."
        compact_preview = artifacts.read_preview(compact.artifacts[-1])["content"] or ""
        assert "Zyra Context Compact" in compact_preview
        assert "Restore Contract" in compact_preview
        assert "tool_call_id" in compact_preview

        frames = fabric.replay_trajectory(state, store.task_events(state.task_id))
        assert len(frames) == len(events)
        assert any(frame.requirement_change for frame in frames)
        assert any(frame.failure_injection for frame in frames)
        assert any(frame.verification for frame in frames)
        assert any(frame.artifact_ids for frame in frames) or state.artifacts

        print(
            "M4 verification passed: "
            f"memory_layers={counts}, compact_artifacts={len(compact.artifact_ids)}, "
            f"trajectory_frames={len(frames)}"
        )


if __name__ == "__main__":
    main()
