from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import ArtifactKind, ArtifactRef, TaskState, to_jsonable

from .models import CompactPolicy, CompactResult, MemoryLayer, MemoryRecord, MemorySnapshot, TrajectoryFrame

SOURCE_MODULES = {
    "claude-code-best": [
        "src/services/compact/compact.ts",
        "src/services/compact/sessionMemoryCompact.ts",
    ],
    "hermes-agent": ["trajectory_compressor.py", "agent/memory_manager.py", "agent/trajectory.py"],
    "agent-framework": ["docs/decisions/0019-python-context-compaction-strategy.md"],
    "agentscope": ["src/agentscope/rag/_chunker/_approx_token_chunker.py"],
    "openclaw": [
        "docs/concepts/compaction.md",
        "docs/concepts/active-memory.md",
        "docs/tools/trajectory.md",
    ],
    "langgraph": ["libs/checkpoint", "libs/checkpoint-sqlite", "libs/checkpoint/langgraph/store"],
}


class MemoryFabric:
    """Task memory, compaction, and trajectory replay for long-running Zyra tasks.

    The implementation internalizes five mature patterns into Zyra's event,
    checkpoint, artifact, and command boundaries:

    - Claude Code / Agent Framework: group-aware compaction and tool pair preservation.
    - Hermes: protect head/tail and compress the middle trajectory.
    - AgentScope: dependency-light approximate token accounting and chunking.
    - OpenClaw: memory flush before compaction and compact artifacts that do not erase the full transcript.
    - LangGraph: checkpoint/store style separation between durable state and replayable event history.
    """

    def __init__(
        self,
        store: Any | None = None,
        artifact_store: Any | None = None,
        index_runtime: Any | None = None,
    ) -> None:
        self.store = store
        self.artifact_store = artifact_store
        self.index_runtime = index_runtime
        if self.index_runtime is None and store is not None and getattr(store, "path", None) is not None:
            from .memory_index import MemoryIndexRuntime

            canonical_path = store.path
            index_path = canonical_path.with_name(f"{canonical_path.stem}.memory-index.sqlite3")
            self.index_runtime = MemoryIndexRuntime(
                canonical_store=store,
                index_path=index_path,
                artifact_store=artifact_store,
            )

    def refresh_task_memory(
        self,
        state: TaskState,
        events: Sequence[Mapping[str, Any]],
        *,
        persist: bool = True,
    ) -> MemorySnapshot:
        normalized_events = [_event_dict(event) for event in events]
        records: list[MemoryRecord] = []
        records.extend(self._working_records(state, normalized_events))
        records.extend(self._episodic_records(state, normalized_events))
        records.extend(self._semantic_records(state))
        records.extend(self._skill_records(state, normalized_events))
        snapshot = MemorySnapshot(
            run_id=state.run_id,
            task_id=state.task_id,
            records=records,
            metadata={
                "source_modules": SOURCE_MODULES,
                "ingestion": ["event_log", "checkpoint", "artifact_refs", "worker_trace", "skill_invocations"],
            },
        )
        if persist and self.store is not None:
            self.store.save_memory_records(records)
        return snapshot

    def memory_view(
        self,
        state: TaskState,
        events: Sequence[Mapping[str, Any]],
        *,
        query: str = "",
        limit: int = 12,
    ) -> dict[str, Any]:
        snapshot = self.refresh_task_memory(state, events, persist=True)
        stored = self.store.task_memory_records(state.task_id) if self.store is not None else snapshot.records
        search_results: list[MemoryRecord] = []
        retrieval: dict[str, Any] | None = None
        if query.strip() and self.index_runtime is not None:
            self.index_runtime.synchronize_task(state.task_id, records=stored, process=True)
            hydrated = self.index_runtime.retrieve(state.task_id, query)
            search_results = list(hydrated.records[:limit])
            retrieval = hydrated.to_dict()
        return {
            "summary": "MemoryFabric task memory view.",
            "data": {
                "task_id": state.task_id,
                "run_id": state.run_id,
                "layer_counts": snapshot.layer_counts(),
                "record_count": len(stored),
                "source_modules": SOURCE_MODULES,
                "working": [to_jsonable(record) for record in _records_for_layer(stored, MemoryLayer.WORKING)[:limit]],
                "episodic": [to_jsonable(record) for record in _records_for_layer(stored, MemoryLayer.EPISODIC)[:limit]],
                "semantic": [to_jsonable(record) for record in _records_for_layer(stored, MemoryLayer.SEMANTIC)[:limit]],
                "skill": [to_jsonable(record) for record in _records_for_layer(stored, MemoryLayer.SKILL)[:limit]],
                "search_results": [to_jsonable(record) for record in search_results],
                "retrieval": retrieval,
                "retrieval_runtime": "MemoryIndexRuntime" if self.index_runtime is not None else "disabled",
                "legacy_substring_fallback": False,
                "compactions": (
                    [to_jsonable(item) for item in self.store.task_compactions(state.task_id)]
                    if self.store is not None
                    else []
                ),
            },
        }

    def canonical_task_records(self, task_id: str) -> tuple[MemoryRecord, ...]:
        """Return MemoryFabric-owned facts through a read-only continuity port.

        Continuity verification must inspect the canonical records instead of
        copying the memory index or accepting caller-supplied fact bodies.
        """

        if self.store is None or not hasattr(self.store, "task_memory_records"):
            raise RuntimeError("MemoryFabric canonical store is unavailable")
        return tuple(self.store.task_memory_records(str(task_id)))

    def compact_context(
        self,
        state: TaskState,
        events: Sequence[Mapping[str, Any]],
        *,
        focus: str = "",
        policy: CompactPolicy | None = None,
        source_event_id: str = "",
        persist: bool = True,
    ) -> CompactResult:
        compact_policy = policy or CompactPolicy()
        snapshot = self.refresh_task_memory(state, events, persist=persist)
        normalized_events = [_event_dict(event) for event in events]
        groups = _event_groups(normalized_events)
        tail_start = max(len(groups) - compact_policy.tail_groups, 0)
        preserved_indices = self._preserved_group_indices(groups, tail_start, compact_policy)
        preserved_groups = [group for index, group in enumerate(groups) if index in preserved_indices]
        summarized_groups = [group for index, group in enumerate(groups) if index not in preserved_indices]

        raw_artifacts: list[ArtifactRef] = []
        for group in groups:
            should_externalize = (
                len(group["raw_text"]) > compact_policy.max_inline_chars
                and self.artifact_store is not None
                and (group.get("tool_group_key") or "agent_message" in set(group["event_types"]))
            )
            if should_externalize:
                artifact = self.artifact_store.write_text(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    content=group["raw_text"],
                    title=f"compact-source:{group['label']}",
                    kind=ArtifactKind.STRUCTURED_DATA,
                    extension=".json",
                    producer_node_id=group.get("node_id"),
                )
                group["artifact_ids"].append(artifact.artifact_id)
                raw_artifacts.append(artifact)

        memory_ids = [record.memory_id for record in snapshot.records]
        summary = _compact_summary(state, focus, preserved_groups, summarized_groups, snapshot)
        markdown = _compact_markdown(
            state,
            focus,
            compact_policy,
            preserved_groups,
            summarized_groups,
            summary,
            snapshot,
        )
        compact_artifacts = list(raw_artifacts)
        if self.artifact_store is not None:
            artifact = self.artifact_store.write_text(
                run_id=state.run_id,
                task_id=state.task_id,
                content=markdown,
                title="Context compact summary",
                kind=ArtifactKind.TRACE,
                extension=".md",
                producer_node_id=state.root_node_id,
            )
            compact_artifacts.append(artifact)

        preserved_event_ids = _event_ids_for_groups(preserved_groups)
        summarized_event_ids = _event_ids_for_groups(summarized_groups)
        original_tokens = sum(int(group["approx_tokens"]) for group in groups)
        compacted_tokens = approx_tokens(markdown)
        result = CompactResult(
            run_id=state.run_id,
            task_id=state.task_id,
            focus=focus,
            summary=summary,
            preserved_event_ids=preserved_event_ids,
            summarized_event_ids=summarized_event_ids,
            artifact_ids=[artifact.artifact_id for artifact in compact_artifacts],
            artifacts=compact_artifacts,
            memory_ids=memory_ids,
            original_approx_tokens=original_tokens,
            compacted_approx_tokens=compacted_tokens,
            compression_ratio=round(compacted_tokens / max(original_tokens, 1), 4),
            preserved_group_count=len(preserved_groups),
            summarized_group_count=len(summarized_groups),
            policy=to_jsonable(compact_policy),
            metadata={
                "source_modules": SOURCE_MODULES,
                "source_event_id": source_event_id,
                "tool_group_preservation": "grouped_by_tool_call_id",
                "full_event_log_retained": True,
                "memory_flush_before_compact": True,
            },
        )
        if persist and self.store is not None:
            self.store.save_compact_result(result)
        return result

    def replay_trajectory(
        self,
        state: TaskState,
        events: Sequence[Mapping[str, Any]],
    ) -> list[TrajectoryFrame]:
        normalized_events = [_event_dict(event) for event in events]
        frames: list[TrajectoryFrame] = []
        for index, event in enumerate(normalized_events):
            payload = _payload(event)
            summary = _event_summary(event)
            route = _route_from_payload(payload)
            worker = _worker_from_payload(payload)
            status = _status_from_payload(payload)
            frame = TrajectoryFrame(
                run_id=state.run_id,
                task_id=state.task_id,
                event_id=_event_id(event),
                event_type=_event_type(event),
                node_id=_node_id(event),
                index=index,
                created_at=str(event.get("created_at") or ""),
                title=_frame_title(event),
                summary=summary,
                worker=worker,
                route=route,
                status=status,
                requirement_change=_event_type(event) == "requirement_change",
                failure_injection=_event_type(event) in {"failure_injected", "node_failed"},
                verification=_event_type(event) in {"constraint_check", "evaluation"} or "verification" in summary.lower(),
                artifact_ids=_artifact_ids_from_event(event),
                state_delta=_state_delta_from_payload(payload),
                metadata={
                    "source": "event_log",
                    "payload_keys": sorted(str(key) for key in payload.keys()),
                    "replay_schema": "zyra-m4-trajectory-v1",
                },
            )
            frames.append(frame)
        return frames

    def _working_records(self, state: TaskState, events: Sequence[Mapping[str, Any]]) -> list[MemoryRecord]:
        node_statuses: dict[str, int] = {}
        for node in state.plan_nodes.values():
            key = str(node.status)
            node_statuses[key] = node_statuses.get(key, 0) + 1
        recent_decisions = [decision.summary for decision in state.decisions[-6:]]
        recent_events = [_event_summary(event) for event in list(events)[-8:]]
        summary = f"Task {state.task_id} is {state.status}; {len(state.plan_nodes)} plan node(s), {len(state.artifacts)} artifact(s)."
        return [
            MemoryRecord(
                memory_id=_stable_memory_id(state.run_id, state.task_id, "working", "checkpoint", state.updated_at),
                run_id=state.run_id,
                task_id=state.task_id,
                layer=MemoryLayer.WORKING,
                source_type="checkpoint",
                source_id=state.task_id,
                summary=summary,
                content={
                    "user_goal": state.user_goal,
                    "status": str(state.status),
                    "objectives": list(state.constraints.objectives),
                    "requirements": list(state.constraints.requirements),
                    "success_criteria": list(state.constraints.success_criteria),
                    "node_statuses": node_statuses,
                    "recent_decisions": recent_decisions,
                    "recent_events": recent_events,
                    "budget": to_jsonable(state.budget),
                },
                keywords=_keywords([state.user_goal, *recent_decisions, *recent_events]),
                artifact_ids=[artifact.artifact_id for artifact in state.artifacts[-12:]],
                score=1.0,
                metadata={"checkpoint_updated_at": state.updated_at},
            )
        ]

    def _episodic_records(self, state: TaskState, events: Sequence[Mapping[str, Any]]) -> list[MemoryRecord]:
        records: list[MemoryRecord] = []
        for event in events:
            event_type = _event_type(event)
            if not _is_episodic_event(event):
                continue
            source_type = "worker_trace" if _is_worker_trace(event) else "event_log"
            summary = _event_summary(event)
            records.append(
                MemoryRecord(
                    memory_id=_stable_memory_id(state.run_id, state.task_id, "episodic", _event_id(event), event_type),
                    run_id=state.run_id,
                    task_id=state.task_id,
                    layer=MemoryLayer.EPISODIC,
                    source_type=source_type,
                    source_id=_event_id(event),
                    node_id=_node_id(event),
                    summary=summary,
                    content={
                        "event_type": event_type,
                        "created_at": event.get("created_at"),
                        "payload_preview": _payload_preview(_payload(event)),
                    },
                    keywords=_keywords([event_type, summary, json.dumps(_payload(event), ensure_ascii=False)[:1200]]),
                    artifact_ids=_artifact_ids_from_event(event),
                    score=_event_score(event),
                    metadata={"payload_keys": sorted(str(key) for key in _payload(event).keys())},
                )
            )
        return records

    def _semantic_records(self, state: TaskState) -> list[MemoryRecord]:
        records = [
            MemoryRecord(
                memory_id=_stable_memory_id(state.run_id, state.task_id, "semantic", "goal"),
                run_id=state.run_id,
                task_id=state.task_id,
                layer=MemoryLayer.SEMANTIC,
                source_type="checkpoint",
                source_id="goal_constraints",
                summary="User goal, requirements, assumptions, and success criteria.",
                content={
                    "user_goal": state.user_goal,
                    "objectives": list(state.constraints.objectives),
                    "requirements": list(state.constraints.requirements),
                    "assumptions": list(state.constraints.assumptions),
                    "success_criteria": list(state.constraints.success_criteria),
                    "forbidden": list(state.constraints.forbidden),
                },
                keywords=_keywords(
                    [
                        state.user_goal,
                        *state.constraints.objectives,
                        *state.constraints.requirements,
                        *state.constraints.success_criteria,
                    ]
                ),
                score=1.0,
                metadata={"source": "checkpoint_constraints"},
            )
        ]
        for artifact in state.artifacts:
            preview = self._artifact_preview(artifact)
            title = artifact.title or artifact.artifact_id
            records.append(
                MemoryRecord(
                    memory_id=_stable_memory_id(state.run_id, state.task_id, "semantic", artifact.artifact_id),
                    run_id=state.run_id,
                    task_id=state.task_id,
                    layer=MemoryLayer.SEMANTIC,
                    source_type="artifact",
                    source_id=artifact.artifact_id,
                    node_id=artifact.producer_node_id,
                    summary=f"Artifact evidence: {title}",
                    content={
                        "artifact": to_jsonable(artifact),
                        "preview": preview,
                    },
                    keywords=_keywords([title, preview]),
                    artifact_ids=[artifact.artifact_id],
                    score=0.85,
                    metadata={"artifact_kind": str(artifact.kind), "storage": artifact.metadata.get("storage", "")},
                )
            )
        return records

    def _skill_records(self, state: TaskState, events: Sequence[Mapping[str, Any]]) -> list[MemoryRecord]:
        records: list[MemoryRecord] = []
        seen: set[str] = set()
        for event in events:
            payload = _payload(event)
            invocation = payload.get("skill_invocation")
            if not isinstance(invocation, Mapping):
                continue
            skill_name = str(invocation.get("skill_name") or "unknown-skill")
            seen.add(skill_name)
            records.append(
                MemoryRecord(
                    memory_id=_stable_memory_id(state.run_id, state.task_id, "skill", _event_id(event), skill_name),
                    run_id=state.run_id,
                    task_id=state.task_id,
                    layer=MemoryLayer.SKILL,
                    source_type="skill_invocation",
                    source_id=_event_id(event),
                    node_id=_node_id(event),
                    summary=f"Skill invoked: {skill_name} via {invocation.get('preferred_runtime', '')}.",
                    content=dict(invocation),
                    keywords=_keywords([skill_name, str(invocation.get("purpose") or ""), str(invocation.get("source") or "")]),
                    score=0.9,
                    metadata={
                        "preferred_runtime": str(invocation.get("preferred_runtime") or ""),
                        "source": str(invocation.get("source") or ""),
                    },
                )
            )
        for item in state.metadata.get("skill_invocations", []):
            if not isinstance(item, Mapping):
                continue
            skill_name = str(item.get("skill_name") or "")
            if not skill_name or skill_name in seen:
                continue
            records.append(
                MemoryRecord(
                    memory_id=_stable_memory_id(state.run_id, state.task_id, "skill", skill_name),
                    run_id=state.run_id,
                    task_id=state.task_id,
                    layer=MemoryLayer.SKILL,
                    source_type="checkpoint_metadata",
                    source_id=skill_name,
                    node_id=str(item.get("node_id") or "") or None,
                    summary=f"Skill invocation metadata: {skill_name}.",
                    content=dict(item),
                    keywords=_keywords([skill_name, str(item.get("preferred_runtime") or "")]),
                    score=0.7,
                    metadata={"source": "checkpoint_metadata"},
                )
            )
        return records

    def _artifact_preview(self, artifact: ArtifactRef) -> str:
        if self.artifact_store is None:
            return ""
        try:
            preview = self.artifact_store.read_preview(artifact, max_chars=3000)
        except Exception:  # noqa: BLE001 - broken artifacts should not break memory ingestion.
            return ""
        content = preview.get("content") if isinstance(preview, Mapping) else ""
        return str(content or "")

    def _preserved_group_indices(
        self,
        groups: Sequence[dict[str, Any]],
        tail_start: int,
        policy: CompactPolicy,
    ) -> set[int]:
        preserved: set[int] = set()
        for index, group in enumerate(groups):
            event_types = set(group["event_types"])
            if index >= tail_start:
                preserved.add(index)
            if index == 0 and policy.preserve_initial_goal:
                preserved.add(index)
            if policy.preserve_requirement_changes and "requirement_change" in event_types:
                preserved.add(index)
            if policy.preserve_failures and event_types.intersection({"failure_injected", "node_failed"}):
                preserved.add(index)
            if policy.preserve_decisions and event_types.intersection({"topology_route", "resource_decision", "recovery_planned", "constraint_check", "evaluation"}):
                preserved.add(index)
            if policy.preserve_tool_groups and group.get("tool_group_key"):
                preserved.add(index)
        return preserved


def search_memory_records(records: Sequence[MemoryRecord], query: str, *, limit: int = 10) -> list[MemoryRecord]:
    terms = _keywords([query])
    if not terms:
        return []
    scored: list[tuple[float, MemoryRecord]] = []
    for record in records:
        haystack = " ".join([record.summary, *record.keywords, json.dumps(record.content, ensure_ascii=False)[:4000]]).lower()
        score = sum(1 for term in terms if term in haystack) + record.score
        if score > record.score:
            scored.append((score, record))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [record for _, record in scored[:limit]]


def approx_tokens(text: str) -> int:
    return max(1, len(text.encode("utf-8")) // 4) if text else 0


def _records_for_layer(records: Sequence[MemoryRecord], layer: MemoryLayer) -> list[MemoryRecord]:
    return [record for record in records if record.layer == layer]


def _event_dict(event: Mapping[str, Any]) -> dict[str, Any]:
    return dict(event)


def _payload(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    return dict(payload) if isinstance(payload, Mapping) else {}


def _event_id(event: Mapping[str, Any]) -> str:
    return str(event.get("event_id") or "")


def _event_type(event: Mapping[str, Any]) -> str:
    return str(event.get("event_type") or "unknown")


def _node_id(event: Mapping[str, Any]) -> str | None:
    value = event.get("node_id")
    return None if value is None else str(value)


def _event_summary(event: Mapping[str, Any]) -> str:
    payload = _payload(event)
    event_type = _event_type(event)
    if event_type == "requirement_change":
        return f"Requirement changed: {payload.get('raw') or payload.get('text') or payload.get('summary') or ''}".strip()
    if event_type in {"failure_injected", "node_failed"}:
        return f"Failure path: {payload.get('raw') or payload.get('summary') or payload.get('reason') or ''}".strip()
    if event_type == "topology_route":
        decision = payload.get("decision") if isinstance(payload.get("decision"), Mapping) else {}
        return f"Topology route selected {decision.get('selected') or payload.get('selected_worker') or ''}: {decision.get('summary') or decision.get('rationale') or ''}".strip()
    if event_type == "resource_decision":
        decision = payload.get("resource_decision") if isinstance(payload.get("resource_decision"), Mapping) else {}
        return (
            f"Resource decision selected {decision.get('selected_worker') or payload.get('selected_worker') or ''} "
            f"via {decision.get('selected_manifest_id') or payload.get('selected_manifest_id') or ''}."
        ).strip()
    if event_type == "recovery_planned":
        plan = payload.get("recovery_plan") if isinstance(payload.get("recovery_plan"), Mapping) else {}
        return f"Recovery planned: {plan.get('summary') or payload.get('selected_worker') or ''}".strip()
    if event_type == "constraint_check":
        return f"Constraint check: {_first_text(payload, ['summary', 'status']) or len(payload.get('results', []))}."
    if event_type == "skill_invoked":
        invocation = payload.get("skill_invocation") if isinstance(payload.get("skill_invocation"), Mapping) else {}
        return f"Skill invoked: {invocation.get('skill_name') or ''}."
    if "tool_call" in payload or "tool_result" in payload:
        call = payload.get("tool_call") if isinstance(payload.get("tool_call"), Mapping) else {}
        result = payload.get("tool_result") if isinstance(payload.get("tool_result"), Mapping) else {}
        return f"Tool {call.get('tool_name') or result.get('metadata', {}).get('tool_name') or ''}: {result.get('summary') or result.get('error') or ''}".strip()
    if "browser_action" in payload or "browser_result" in payload:
        action = payload.get("browser_action") if isinstance(payload.get("browser_action"), Mapping) else {}
        result = payload.get("browser_result") if isinstance(payload.get("browser_result"), Mapping) else {}
        return f"Browser {action.get('action') or ''}: {result.get('summary') or result.get('error') or ''}".strip()
    if "worker_result" in payload:
        result = payload.get("worker_result") if isinstance(payload.get("worker_result"), Mapping) else {}
        return f"Worker result: {result.get('summary') or result.get('error') or ''}".strip()
    if "query_session" in payload:
        query = payload.get("query_session") if isinstance(payload.get("query_session"), Mapping) else {}
        return f"Query session {query.get('session_id') or ''} {query.get('phase') or ''}".strip()
    if event_type == "node_updated":
        return str(payload.get("summary") or payload.get("transition") or "Plan node updated.")
    if event_type == "evaluation":
        return str(payload.get("summary") or "Trace evaluation.")
    return str(_first_text(payload, ["summary", "raw", "status"]) or event_type)


def _first_text(payload: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _is_episodic_event(event: Mapping[str, Any]) -> bool:
    event_type = _event_type(event)
    if event_type in {
        "task_created",
        "node_updated",
        "agent_message",
        "skill_invoked",
        "control_command",
        "requirement_change",
        "failure_injected",
        "node_failed",
        "constraint_check",
        "topology_route",
        "resource_decision",
        "recovery_planned",
        "worker_health",
        "evaluation",
        "system_notice",
    }:
        return True
    return bool({"tool_result", "browser_result", "worker_result", "query_session"}.intersection(_payload(event).keys()))


def _is_worker_trace(event: Mapping[str, Any]) -> bool:
    return bool({"tool_result", "browser_result", "worker_result", "query_session"}.intersection(_payload(event).keys()))


def _event_score(event: Mapping[str, Any]) -> float:
    event_type = _event_type(event)
    if event_type in {"requirement_change", "failure_injected", "node_failed"}:
        return 1.0
    if event_type in {"topology_route", "resource_decision", "recovery_planned", "constraint_check", "evaluation", "skill_invoked"}:
        return 0.85
    if _is_worker_trace(event):
        return 0.75
    return 0.55


def _payload_preview(payload: Mapping[str, Any], max_chars: int = 1800) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(text) <= max_chars:
        return dict(payload)
    return {
        "truncated": True,
        "preview": text[:max_chars],
        "chars": len(text),
    }


def _artifact_ids_from_event(event: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    _collect_artifact_ids(_payload(event), ids)
    return sorted(set(ids))


def _collect_artifact_ids(value: Any, ids: list[str]) -> None:
    if isinstance(value, Mapping):
        artifact_id = value.get("artifact_id")
        if isinstance(artifact_id, str) and artifact_id:
            ids.append(artifact_id)
        for item in value.values():
            _collect_artifact_ids(item, ids)
    elif isinstance(value, list):
        for item in value:
            _collect_artifact_ids(item, ids)


def _event_groups(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    key_to_events: dict[str, list[Mapping[str, Any]]] = {}
    for event in events:
        key = _tool_group_key(event)
        if key:
            key_to_events.setdefault(key, []).append(event)

    consumed: set[str] = set()
    groups: list[dict[str, Any]] = []
    for event in events:
        event_id = _event_id(event)
        if event_id in consumed:
            continue
        key = _tool_group_key(event)
        group_events = key_to_events.get(key, [event]) if key else [event]
        for group_event in group_events:
            consumed.add(_event_id(group_event))
        raw_text = json.dumps(group_events, ensure_ascii=False, indent=2, sort_keys=True)
        event_types = [_event_type(item) for item in group_events]
        summaries = [_event_summary(item) for item in group_events]
        groups.append(
            {
                "event_ids": [_event_id(item) for item in group_events],
                "event_types": event_types,
                "node_id": _node_id(group_events[0]),
                "created_at": str(group_events[0].get("created_at") or ""),
                "summary": "; ".join(summary for summary in summaries if summary)[:1200],
                "label": key or _event_id(event) or f"group_{len(groups)}",
                "raw_text": raw_text,
                "approx_tokens": approx_tokens(raw_text),
                "artifact_ids": sorted({artifact_id for item in group_events for artifact_id in _artifact_ids_from_event(item)}),
                "tool_group_key": key,
            }
        )
    return groups


def _tool_group_key(event: Mapping[str, Any]) -> str:
    payload = _payload(event)
    for key in ("tool_call", "tool_result"):
        item = payload.get(key)
        if isinstance(item, Mapping) and item.get("tool_call_id"):
            return f"tool:{item['tool_call_id']}"
    query = payload.get("query_session")
    if isinstance(query, Mapping) and query.get("tool_call_id"):
        return f"tool:{query['tool_call_id']}"
    return ""


def _event_ids_for_groups(groups: Sequence[Mapping[str, Any]]) -> list[str]:
    ids: list[str] = []
    for group in groups:
        ids.extend(str(item) for item in group.get("event_ids", []))
    return ids


def _compact_summary(
    state: TaskState,
    focus: str,
    preserved_groups: Sequence[Mapping[str, Any]],
    summarized_groups: Sequence[Mapping[str, Any]],
    snapshot: MemorySnapshot,
) -> str:
    focus_text = f" Focus: {focus}." if focus else ""
    return (
        f"Compacted task {state.task_id} while preserving the initial goal, constraints, "
        f"{len(preserved_groups)} protected event group(s), and {snapshot.layer_counts()} memory records."
        f" Summarized {len(summarized_groups)} middle group(s).{focus_text}"
    )


def _compact_markdown(
    state: TaskState,
    focus: str,
    policy: CompactPolicy,
    preserved_groups: Sequence[Mapping[str, Any]],
    summarized_groups: Sequence[Mapping[str, Any]],
    summary: str,
    snapshot: MemorySnapshot,
) -> str:
    lines = [
        "# Zyra Context Compact",
        "",
        summary,
        "",
        "## Stable Task Context",
        "",
        f"- task_id: `{state.task_id}`",
        f"- run_id: `{state.run_id}`",
        f"- status: `{state.status}`",
        f"- focus: `{focus or 'general'}`",
        f"- user_goal: {state.user_goal}",
        f"- objectives: {json.dumps(state.constraints.objectives, ensure_ascii=False)}",
        f"- requirements: {json.dumps(state.constraints.requirements, ensure_ascii=False)}",
        f"- success_criteria: {json.dumps(state.constraints.success_criteria, ensure_ascii=False)}",
        "",
        "## Memory Flush",
        "",
        f"- records: `{len(snapshot.records)}`",
        f"- layer_counts: `{json.dumps(snapshot.layer_counts(), ensure_ascii=False, sort_keys=True)}`",
        f"- source_modules: `{', '.join(SOURCE_MODULES.keys())}`",
        "",
        "## Preserved Event Groups",
        "",
    ]
    if not preserved_groups:
        lines.append("- none")
    for group in preserved_groups:
        lines.append(_format_group_line(group))
    lines.extend(["", "## Summarized Middle Groups", ""])
    if not summarized_groups:
        lines.append("- none")
    for group in summarized_groups:
        lines.append(_format_group_line(group))
    lines.extend(
        [
            "",
            "## Restore Contract",
            "",
            "- The durable event log remains authoritative.",
            "- Tool-related events are grouped by tool_call_id so split points do not orphan tool results.",
            "- Large group payloads are represented by artifact refs instead of inline context.",
            "- Future context rebuild should start from stable task context, then inject MemoryFabric retrieval hits, preserved groups, and the final tail.",
            "",
            "## Policy",
            "",
            "```json",
            json.dumps(to_jsonable(policy), ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _format_group_line(group: Mapping[str, Any]) -> str:
    refs = group.get("artifact_ids") or []
    ref_text = f" artifact_refs={json.dumps(refs, ensure_ascii=False)}" if refs else ""
    return (
        f"- `{group.get('created_at')}` events={json.dumps(group.get('event_ids', []), ensure_ascii=False)} "
        f"types={json.dumps(group.get('event_types', []), ensure_ascii=False)} "
        f"tokens~{group.get('approx_tokens')}: {group.get('summary')}{ref_text}"
    )


def _frame_title(event: Mapping[str, Any]) -> str:
    payload = _payload(event)
    if "query_session" in payload and isinstance(payload["query_session"], Mapping):
        return f"query:{payload['query_session'].get('phase', '')}"
    if "tool_call" in payload and isinstance(payload["tool_call"], Mapping):
        return f"tool:{payload['tool_call'].get('tool_name', '')}"
    if "browser_action" in payload and isinstance(payload["browser_action"], Mapping):
        return f"browser:{payload['browser_action'].get('action', '')}"
    return _event_type(event)


def _route_from_payload(payload: Mapping[str, Any]) -> str | None:
    decision = payload.get("decision") if isinstance(payload.get("decision"), Mapping) else {}
    resource = payload.get("resource_decision") if isinstance(payload.get("resource_decision"), Mapping) else {}
    plan = payload.get("recovery_plan") if isinstance(payload.get("recovery_plan"), Mapping) else {}
    selected = decision.get("selected") or resource.get("selected_worker") or plan.get("selected_worker") or payload.get("selected_worker")
    return None if selected is None else str(selected)


def _worker_from_payload(payload: Mapping[str, Any]) -> str | None:
    request = payload.get("worker_request") if isinstance(payload.get("worker_request"), Mapping) else {}
    result = payload.get("worker_result") if isinstance(payload.get("worker_result"), Mapping) else {}
    metadata = result.get("metadata") if isinstance(result.get("metadata"), Mapping) else {}
    worker = request.get("worker_name") or metadata.get("worker")
    if not worker and "tool_call" in payload:
        worker = "CodeWorkerRuntime"
    if not worker and "browser_result" in payload:
        worker = "BrowserWorker"
    return None if worker is None else str(worker)


def _status_from_payload(payload: Mapping[str, Any]) -> str | None:
    if "transition" in payload:
        return str(payload["transition"])
    for key in ("tool_result", "browser_result", "worker_result"):
        item = payload.get(key)
        if isinstance(item, Mapping) and "ok" in item:
            return "ok" if item.get("ok") is True else "failed"
    return None


def _state_delta_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("state_delta", "delta"):
        item = payload.get(key)
        if isinstance(item, Mapping):
            return dict(item)
    node = payload.get("node")
    if isinstance(node, Mapping):
        return {
            "node_id": node.get("node_id"),
            "status": node.get("status"),
            "assigned_worker_id": node.get("assigned_worker_id"),
        }
    return {}


def _keywords(texts: Iterable[str], limit: int = 32) -> list[str]:
    seen: list[str] = []
    for text in texts:
        for word in re.findall(r"[\w\u4e00-\u9fff]{2,}", str(text).lower()):
            if word not in seen:
                seen.append(word)
            if len(seen) >= limit:
                return seen
    return seen


def _stable_memory_id(*parts: str) -> str:
    digest = hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"memory_{digest}"
