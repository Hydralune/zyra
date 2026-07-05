from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from zyra_core import (
    AgentMessage,
    AgentRole,
    EventRecord,
    EventType,
    MessageIntent,
    PlanNode,
    PlanNodeStatus,
    TaskState,
    now_iso,
    to_jsonable,
)

GRAPH_VERSION = "m3-symbolic-v1"


@dataclass(frozen=True, slots=True)
class StageSpec:
    stage: str
    title: str
    description: str
    result_summary: str
    intent: MessageIntent = MessageIntent.PLAN
    completion_criteria: tuple[str, ...] = ()
    expected_output_schema: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class GraphExecutionContext:
    project_root: Path
    workspace_root: Path
    artifact_root: Path
    permission_store_path: Path | None = None

    @classmethod
    def from_paths(
        cls,
        *,
        project_root: str | Path,
        workspace_root: str | Path,
        artifact_root: str | Path,
        permission_store_path: str | Path | None = None,
    ) -> "GraphExecutionContext":
        return cls(
            project_root=Path(project_root).resolve(),
            workspace_root=Path(workspace_root).resolve(),
            artifact_root=Path(artifact_root).resolve(),
            permission_store_path=None if permission_store_path is None else Path(permission_store_path).resolve(),
        )


DEFAULT_STAGE_SPECS = [
    StageSpec(
        stage="plan",
        title="Plan",
        description="Decompose the user goal into an executable task graph.",
        result_summary="Generated the initial structured task graph.",
        completion_criteria=("Task graph has ordered stage nodes.", "Each stage is traceable by node_id."),
        expected_output_schema={"type": "object", "required": ["stage_order"]},
    ),
    StageSpec(
        stage="route",
        title="Route",
        description="Select the worker and control route for the executable node.",
        result_summary="Selected a topology route for the executable node.",
        intent=MessageIntent.DECISION,
        completion_criteria=("A topology_route event is emitted.", "A DecisionRecord is appended."),
        expected_output_schema={"type": "object", "required": ["selected_worker", "route_candidates"]},
    ),
    StageSpec(
        stage="execute",
        title="Execute",
        description="Run the current node through the selected worker runtime.",
        result_summary="Executed the selected worker runtime.",
        intent=MessageIntent.TOOL_CALL,
        completion_criteria=("Worker runtime returns trace events.", "Artifacts or tool results are preserved."),
        expected_output_schema={"type": "object", "required": ["worker_result"]},
    ),
    StageSpec(
        stage="verify",
        title="Verify",
        description="Check node outputs, event coverage, and checkpoint readiness.",
        result_summary="Verified constraints, event coverage, and checkpoint readiness.",
        intent=MessageIntent.CRITIQUE,
        completion_criteria=("ConstraintKeeper emits a constraint_check event.",),
        expected_output_schema={"type": "object", "required": ["constraint_results"]},
    ),
    StageSpec(
        stage="finalize",
        title="Finalize",
        description="Finalize the trace and mark the task ready for inspection.",
        result_summary="Finalized the M3 task trace.",
        intent=MessageIntent.STATUS,
        completion_criteria=("Task trace is ready for inspection.",),
        expected_output_schema={"type": "object", "required": ["status"]},
    ),
]


def ensure_default_graph(state: TaskState) -> list[EventRecord]:
    if state.metadata.get("graph_version") == GRAPH_VERSION:
        return []

    existing_stage_nodes = {
        str(node.metadata.get("stage")): node
        for node in state.plan_nodes.values()
        if node.metadata.get("stage")
    }
    previous_node_id = state.root_node_id
    stage_order: list[str] = []
    events: list[EventRecord] = []

    for spec in DEFAULT_STAGE_SPECS:
        existing = existing_stage_nodes.get(spec.stage)
        if existing is None:
            node = PlanNode(
                title=spec.title,
                description=spec.description,
                intent=spec.intent,
                parent_node_id=state.root_node_id,
                depends_on=[previous_node_id],
                expected_output_schema=dict(spec.expected_output_schema or {}),
                completion_criteria=list(spec.completion_criteria),
                metadata={"stage": spec.stage, "graph_version": GRAPH_VERSION},
            )
            state.plan_nodes[node.node_id] = node
            events.append(
                EventRecord(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    event_type=EventType.NODE_CREATED,
                    node_id=node.node_id,
                    payload={"node": to_jsonable(node)},
                )
            )
        else:
            node = existing
            node.intent = spec.intent
            node.expected_output_schema = dict(spec.expected_output_schema or {})
            node.completion_criteria = list(spec.completion_criteria)
            node.metadata["graph_version"] = GRAPH_VERSION
            node.updated_at = now_iso()
        stage_order.append(node.node_id)
        previous_node_id = node.node_id

    state.metadata["graph_version"] = GRAPH_VERSION
    state.metadata["stage_order"] = stage_order
    state.updated_at = now_iso()
    return events


def run_task_graph(
    state: TaskState,
    execution_context: GraphExecutionContext | None = None,
) -> list[EventRecord]:
    if state.status in {PlanNodeStatus.CANCELLED, PlanNodeStatus.COMPLETED}:
        return []

    events = ensure_default_graph(state)
    events.extend(_complete_root_if_needed(state))

    stage_results = {spec.stage: spec.result_summary for spec in DEFAULT_STAGE_SPECS}
    keeper, router = _symbolic_runtime()
    for node_id in list(state.metadata.get("stage_order", [])):
        node = state.plan_nodes.get(str(node_id))
        if node is None or node.status in {PlanNodeStatus.COMPLETED, PlanNodeStatus.SUPERSEDED}:
            continue
        if _has_superseded_dependency(state, node):
            node.status = PlanNodeStatus.SUPERSEDED
            node.updated_at = now_iso()
            node.metadata["superseded_by_dependency"] = True
            events.append(_node_event(state, node, "superseded", "Node superseded because a dependency was superseded."))
            continue
        if not _dependencies_completed(state, node):
            node.status = PlanNodeStatus.BLOCKED
            node.updated_at = now_iso()
            events.append(_node_event(state, node, "blocked", "Waiting for dependencies."))
            break

        stage = str(node.metadata.get("stage") or "")
        start_checks = keeper.check_task_state(state, node=node, transition="start")
        events.append(keeper.event_for_results(state, start_checks, node_id=node.node_id, stage=stage))
        if keeper.has_blocking_failure(start_checks):
            node.status = PlanNodeStatus.BLOCKED
            node.updated_at = now_iso()
            events.append(_node_event(state, node, "blocked", "ConstraintKeeper blocked node start."))
            break
        events.append(_structured_message_event(state, node, stage))
        if stage == "execute" and execution_context is not None:
            events.extend(_run_execute_node(state, node, execution_context))
        elif stage == "route":
            events.extend(_run_route_node(state, node, router, start_checks))
        elif stage == "verify":
            events.extend(_run_verify_node(state, node, keeper))
        else:
            events.extend(_run_node(state, node, stage_results.get(stage, "")))

    if _all_stage_nodes_completed(state):
        state.status = PlanNodeStatus.COMPLETED
        state.updated_at = now_iso()
        events.append(
            EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.SYSTEM_NOTICE,
                node_id=state.root_node_id,
                payload={
                    "status": str(state.status),
                    "summary": "Task graph completed.",
                    "stage_order": list(state.metadata.get("stage_order", [])),
                },
            )
        )

    return events


def cancel_task_graph(state: TaskState, reason: str = "") -> list[EventRecord]:
    if state.status == PlanNodeStatus.COMPLETED:
        return [
            EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.SYSTEM_NOTICE,
                node_id=state.root_node_id,
                payload={"status": str(state.status), "summary": "Completed task was not cancelled."},
            )
        ]

    events: list[EventRecord] = []
    state.status = PlanNodeStatus.CANCELLED
    state.updated_at = now_iso()
    for node in state.plan_nodes.values():
        if node.status not in {PlanNodeStatus.COMPLETED, PlanNodeStatus.CANCELLED}:
            node.status = PlanNodeStatus.CANCELLED
            node.updated_at = now_iso()
            events.append(_node_event(state, node, "cancelled", reason or "Task cancelled."))

    events.append(
        EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.SYSTEM_NOTICE,
            node_id=state.root_node_id,
            payload={"status": str(state.status), "summary": reason or "Task cancelled."},
        )
    )
    return events


def _complete_root_if_needed(state: TaskState) -> list[EventRecord]:
    root = state.plan_nodes[state.root_node_id]
    if root.status == PlanNodeStatus.COMPLETED:
        return []
    root.status = PlanNodeStatus.COMPLETED
    root.updated_at = now_iso()
    root.metadata["result_summary"] = "Accepted the initial user goal."
    return [_node_event(state, root, "completed", root.metadata["result_summary"])]


def _run_node(state: TaskState, node: PlanNode, result_summary: str) -> list[EventRecord]:
    events: list[EventRecord] = []
    node.status = PlanNodeStatus.RUNNING
    node.updated_at = now_iso()
    events.append(_node_event(state, node, "running", "Node execution started."))

    node.status = PlanNodeStatus.COMPLETED
    node.updated_at = now_iso()
    node.metadata["result_summary"] = result_summary or "Node execution completed."
    events.append(_node_event(state, node, "completed", node.metadata["result_summary"]))
    return events


def _run_route_node(state: TaskState, node: PlanNode, router: Any, route_checks: list[Any]) -> list[EventRecord]:
    events: list[EventRecord] = []
    node.status = PlanNodeStatus.RUNNING
    node.updated_at = now_iso()
    events.append(_node_event(state, node, "running", "Topology routing started."))

    target = _first_stage_node(state, "execute") or node
    decision, route_event = router.route(state, node=target, route_type="worker_route")
    decision.checks = [to_jsonable(result) for result in route_checks]
    route_event.payload["decision"] = to_jsonable(decision)
    events.append(route_event)
    resource_event = _resource_decision_event_from_route(route_event)
    if resource_event is not None:
        events.append(resource_event)

    node.status = PlanNodeStatus.COMPLETED
    node.updated_at = now_iso()
    node.metadata["result_summary"] = "Selected a topology route for the executable node."
    node.metadata["selected_worker"] = target.assigned_worker_id
    events.append(_node_event(state, node, "completed", node.metadata["result_summary"]))
    return events


def _run_verify_node(state: TaskState, node: PlanNode, keeper: Any) -> list[EventRecord]:
    events: list[EventRecord] = []
    node.status = PlanNodeStatus.RUNNING
    node.updated_at = now_iso()
    events.append(_node_event(state, node, "running", "Constraint verification started."))

    results = keeper.check_task_state(state, node=node, transition="inspect")
    events.append(keeper.event_for_results(state, results, node_id=node.node_id, stage="verify"))
    if keeper.has_blocking_failure(results):
        node.status = PlanNodeStatus.BLOCKED
        node.updated_at = now_iso()
        node.metadata["result_summary"] = "Constraint verification found blocking issues."
        events.append(_node_event(state, node, "blocked", node.metadata["result_summary"]))
        return events

    node.status = PlanNodeStatus.COMPLETED
    node.updated_at = now_iso()
    node.metadata["result_summary"] = "Verified constraints, event coverage, and checkpoint readiness."
    events.append(_node_event(state, node, "completed", node.metadata["result_summary"]))
    return events


def _run_execute_node(
    state: TaskState,
    node: PlanNode,
    execution_context: GraphExecutionContext,
) -> list[EventRecord]:
    events: list[EventRecord] = []
    node.status = PlanNodeStatus.RUNNING
    node.updated_at = now_iso()
    events.append(_node_event(state, node, "running", "Worker runtime execution started."))

    try:
        worker_run, worker_name = _run_selected_worker(state, node, execution_context)
    except Exception as error:  # noqa: BLE001 - worker failures must stay in the trace.
        node.status = PlanNodeStatus.FAILED
        node.updated_at = now_iso()
        state.status = PlanNodeStatus.FAILED
        state.updated_at = node.updated_at
        node.metadata["result_summary"] = "Worker runtime failed before producing a result."
        node.metadata["worker_error"] = f"{type(error).__name__}: {error}"
        events.append(_node_event(state, node, "failed", node.metadata["result_summary"]))
        events.append(
            EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.SYSTEM_NOTICE,
                node_id=node.node_id,
                payload={
                    "summary": "Worker runtime raised an exception.",
                    "error": type(error).__name__,
                    "message": str(error),
                },
            )
        )
        events.extend(_plan_runtime_recovery(state, node, error=error))
        return events

    events.extend(worker_run.event_records)
    state.artifacts.extend(worker_run.worker_result.artifacts)
    state.budget.tool_calls += _count_runtime_events(worker_run.event_records)
    node.assigned_worker_id = worker_name
    node.status = PlanNodeStatus.COMPLETED if worker_run.worker_result.ok else PlanNodeStatus.FAILED
    node.updated_at = now_iso()
    if not worker_run.worker_result.ok:
        state.status = PlanNodeStatus.FAILED
        state.updated_at = node.updated_at
        events.extend(_plan_runtime_recovery(state, node, worker_result=worker_run.worker_result))
    node.metadata["result_summary"] = worker_run.worker_result.summary
    node.metadata["worker_result"] = to_jsonable(worker_run.worker_result)
    transition = "completed" if worker_run.worker_result.ok else "failed"
    events.append(_node_event(state, node, transition, worker_run.worker_result.summary))
    return events


def _run_selected_worker(
    state: TaskState,
    node: PlanNode,
    execution_context: GraphExecutionContext,
):
    from zyra_runtime import JsonPermissionStore, WorkerRequest
    from zyra_workers import BrowserWorkerRuntime, CodeWorkerRuntime

    hints = _runtime_hints(state)
    resource_decision = _node_resource_decision(node)
    selected_manifest = _selected_manifest(resource_decision, node.assigned_worker_id)
    preferred_worker = str(
        (selected_manifest or {}).get("runtime_worker")
        or resource_decision.get("selected_worker")
        or node.assigned_worker_id
        or hints.get("preferred_worker")
        or hints.get("worker")
        or ""
    )
    request_metadata = _worker_request_metadata(
        state,
        node,
        execution_context,
        resource_decision=resource_decision,
        selected_manifest=selected_manifest,
    )
    browser_url = str(hints.get("browser_url") or _extract_first_url(state.user_goal) or "")
    if preferred_worker == "BrowserWorker" or browser_url:
        constraints = _browser_constraints(state, hints, browser_url)
        request = WorkerRequest(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=node.node_id,
            worker_name="BrowserWorker",
            constraints=constraints,
            metadata=request_metadata,
        )
        return (
            BrowserWorkerRuntime(
                project_root=execution_context.project_root,
                workspace_root=execution_context.workspace_root,
                artifact_root=execution_context.artifact_root,
            ).run(request),
            "BrowserWorker",
        )

    request = WorkerRequest(
        run_id=state.run_id,
        task_id=state.task_id,
        node_id=node.node_id,
        worker_name="CodeWorkerRuntime",
        constraints=_code_constraints(state, hints),
        metadata=request_metadata,
    )
    return (
        CodeWorkerRuntime(
            project_root=execution_context.project_root,
            workspace_root=execution_context.workspace_root,
            artifact_root=execution_context.artifact_root,
            permission_store=(
                JsonPermissionStore(execution_context.permission_store_path)
                if execution_context.permission_store_path is not None
                else None
            ),
        ).run(request),
        "CodeWorkerRuntime",
    )


def _resource_decision_event_from_route(route_event: EventRecord) -> EventRecord | None:
    resource_decision = route_event.payload.get("resource_decision")
    if not isinstance(resource_decision, dict):
        return None
    return EventRecord(
        run_id=route_event.run_id,
        task_id=route_event.task_id,
        event_type=EventType.RESOURCE_DECISION,
        node_id=route_event.node_id,
        payload={
            "resource_decision": resource_decision,
            "topology_event_id": route_event.event_id,
            "selected_worker": resource_decision.get("selected_worker"),
            "selected_manifest_id": resource_decision.get("selected_manifest_id"),
            "selected_backend": resource_decision.get("selected_backend"),
            "selected_location": resource_decision.get("selected_location"),
            "model_split": resource_decision.get("model_split") or {},
        },
    )


def _plan_runtime_recovery(
    state: TaskState,
    node: PlanNode,
    *,
    worker_result: Any | None = None,
    error: BaseException | None = None,
) -> list[EventRecord]:
    try:
        from zyra_scheduler import RecoveryPlanner, RuntimeWatchdog
    except Exception:  # noqa: BLE001 - recovery planner should not hide the original worker failure.
        return []
    decision_payload = _node_resource_decision(node)
    signal = RuntimeWatchdog().classify(
        state,
        node=node,
        worker_result=worker_result,
        error=error,
        decision=_resource_decision_from_payload(decision_payload),
    )
    planner = RecoveryPlanner()
    plan = planner.plan(
        state,
        signal,
        node=node,
    )
    node.metadata["recovery_plan"] = to_jsonable(plan)
    return [planner.event_for_plan(plan, signal)]


def _resource_decision_from_payload(payload: dict[str, Any]) -> Any | None:
    if not payload:
        return None
    try:
        from zyra_scheduler import ResourceDecision, SchedulerSignals, WorkerBackendKind, ResourceLocation
    except Exception:  # noqa: BLE001
        return None
    try:
        signals_payload = payload.get("signals") if isinstance(payload.get("signals"), dict) else {}
        return ResourceDecision(
            run_id=str(payload.get("run_id") or ""),
            task_id=str(payload.get("task_id") or ""),
            node_id=str(payload.get("node_id")) if payload.get("node_id") is not None else None,
            selected_manifest_id=str(payload.get("selected_manifest_id") or ""),
            selected_worker=str(payload.get("selected_worker") or ""),
            selected_backend=WorkerBackendKind(str(payload.get("selected_backend") or WorkerBackendKind.LOCAL_PROCESS)),
            selected_location=ResourceLocation(str(payload.get("selected_location") or ResourceLocation.LOCAL)),
            score=float(payload.get("score") or 0),
            reasons=[str(item) for item in payload.get("reasons", [])],
            alternatives=[dict(item) for item in payload.get("alternatives", []) if isinstance(item, dict)],
            model_split=dict(payload.get("model_split") or {}),
            signals=SchedulerSignals(**signals_payload),
            decision_id=str(payload.get("decision_id") or ""),
            source_modules=dict(payload.get("source_modules") or {}),
            metadata=dict(payload.get("metadata") or {}),
        )
    except Exception:  # noqa: BLE001 - malformed metadata should not break recovery planning.
        return None


def _node_resource_decision(node: PlanNode) -> dict[str, Any]:
    decision = node.metadata.get("resource_decision")
    return dict(decision) if isinstance(decision, dict) else {}


def _selected_manifest(resource_decision: dict[str, Any], fallback_worker: str | None) -> dict[str, str]:
    manifest_id = str(resource_decision.get("selected_manifest_id") or fallback_worker or "")
    try:
        from zyra_scheduler import WorkerPool
    except Exception:  # noqa: BLE001 - early package imports can run without scheduler path.
        return {}
    manifest = WorkerPool().by_id(manifest_id)
    if manifest is None:
        return {}
    return {
        "worker_id": manifest.worker_id,
        "runtime_worker": manifest.runtime_worker,
        "backend": str(manifest.backend),
        "location": str(manifest.location),
        "sandbox": manifest.sandbox,
        "gateway": manifest.gateway,
        "workspace_scope": manifest.workspace_scope,
        "privacy_level": manifest.privacy_level,
    }


def _worker_request_metadata(
    state: TaskState,
    node: PlanNode,
    execution_context: GraphExecutionContext,
    *,
    resource_decision: dict[str, Any],
    selected_manifest: dict[str, str],
) -> dict[str, str]:
    metadata = {
        "scheduler": "m5-resource-scheduler" if resource_decision else "",
        "resource_decision_id": str(resource_decision.get("decision_id") or ""),
        "worker_manifest_id": str(resource_decision.get("selected_manifest_id") or selected_manifest.get("worker_id") or ""),
        "backend": str(resource_decision.get("selected_backend") or selected_manifest.get("backend") or ""),
        "location": str(resource_decision.get("selected_location") or selected_manifest.get("location") or ""),
        "model_split": json.dumps(resource_decision.get("model_split") or {}, ensure_ascii=False, sort_keys=True),
    }
    try:
        from zyra_scheduler import WorkerPool, build_dispatch_envelope
    except Exception:  # noqa: BLE001 - dispatch envelope is an M5 enhancement, not an import-time requirement.
        return metadata
    manifest = WorkerPool().by_id(metadata["worker_manifest_id"] or node.assigned_worker_id or "")
    if manifest is None:
        return metadata
    envelope = build_dispatch_envelope(
        state,
        manifest,
        workspace_root=execution_context.workspace_root,
        artifact_root=execution_context.artifact_root,
        node_id=node.node_id,
        decision_id=metadata["resource_decision_id"],
    )
    node.metadata["dispatch_envelope"] = to_jsonable(envelope)
    metadata.update(
        {
            "dispatch_envelope_id": envelope.envelope_id,
            "sandbox": envelope.sandbox,
            "gateway": envelope.gateway,
            "workspace_scope": str(envelope.metadata.get("workspace_scope") or ""),
        }
    )
    return metadata


def _runtime_hints(state: TaskState) -> dict[str, Any]:
    hints = state.metadata.get("runtime_hints")
    return dict(hints) if isinstance(hints, dict) else {}


def _code_constraints(state: TaskState, hints: dict[str, Any]) -> dict[str, Any]:
    if isinstance(hints.get("tool_plan"), list):
        return {"tool_plan": hints["tool_plan"]}
    relative_path = f"runs/{state.task_id}/execution-summary.md"
    content = "\n".join(
        [
            "# Zyra Worker Execution Summary",
            "",
            f"- run_id: `{state.run_id}`",
            f"- task_id: `{state.task_id}`",
            "",
            "## User Goal",
            "",
            state.user_goal,
            "",
            "## Runtime",
            "",
            "Executed through CodeWorkerRuntime and Zyra ToolExecutor.",
            "",
        ]
    )
    return {
        "tool_plan": [
            {"tool_name": "file_write", "arguments": {"path": relative_path, "content": content}},
            {"tool_name": "file_read", "arguments": {"path": relative_path}},
        ]
    }


def _browser_constraints(
    state: TaskState,
    hints: dict[str, Any],
    browser_url: str,
) -> dict[str, Any]:
    constraints: dict[str, Any] = {}
    if isinstance(hints.get("browser_plan"), list):
        constraints["browser_plan"] = hints["browser_plan"]
    else:
        constraints["browser_plan"] = [
            {"action": "open_url", "arguments": {"url": browser_url}},
            {"action": "extract_text"},
        ]
    allowed_schemes = hints.get("allowed_schemes")
    if isinstance(allowed_schemes, list):
        constraints["allowed_schemes"] = allowed_schemes
    else:
        constraints["allowed_schemes"] = ["file", "http", "https"]
    allowed_domains = hints.get("allowed_domains")
    if isinstance(allowed_domains, list):
        constraints["allowed_domains"] = allowed_domains
    return constraints


def _extract_first_url(text: str) -> str | None:
    for token in text.split():
        candidate = token.strip(".,;()[]{}<>\"'")
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https", "file"}:
            return candidate
    return None


def _count_runtime_events(events: list[EventRecord]) -> int:
    return sum(1 for event in events if "tool_result" in event.payload or "browser_result" in event.payload)


def _node_event(
    state: TaskState,
    node: PlanNode,
    transition: str,
    summary: str,
) -> EventRecord:
    return EventRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        event_type=EventType.NODE_UPDATED,
        node_id=node.node_id,
        payload={"transition": transition, "summary": summary, "node": to_jsonable(node)},
    )


def _structured_message_event(state: TaskState, node: PlanNode, stage: str) -> EventRecord:
    receiver = {
        "route": AgentRole.ROUTER,
        "verify": AgentRole.SYMBOLIC,
    }.get(stage, AgentRole.WORKER)
    budget = node.constraints.message_budget_chars or state.constraints.message_budget_chars
    message = AgentMessage(
        run_id=state.run_id,
        task_id=state.task_id,
        sender_role=AgentRole.SUPERVISOR,
        receiver_role=receiver,
        intent=node.intent,
        node_id=node.node_id,
        content=_compact_text(node.description, budget),
        summary=node.summary or node.title,
        constraints=node.constraints,
        evidence_refs=list(node.evidence_refs),
        artifact_refs=list(node.artifact_refs),
        expected_output_schema=dict(node.expected_output_schema),
        state_delta={"node_id": node.node_id, "stage": stage, "status": str(node.status)},
        message_budget_chars=budget,
        metadata={"communication": "m3-low-entropy-structured", "stage": stage},
    )
    return EventRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        event_type=EventType.AGENT_MESSAGE,
        node_id=node.node_id,
        payload={"message": to_jsonable(message), "low_entropy": True},
    )


def _dependencies_completed(state: TaskState, node: PlanNode) -> bool:
    for dependency_id in node.depends_on:
        dependency = state.plan_nodes.get(dependency_id)
        if dependency is None or dependency.status != PlanNodeStatus.COMPLETED:
            return False
    return True


def _has_superseded_dependency(state: TaskState, node: PlanNode) -> bool:
    return any(
        (dependency := state.plan_nodes.get(dependency_id)) is not None
        and dependency.status == PlanNodeStatus.SUPERSEDED
        for dependency_id in node.depends_on
    )


def _all_stage_nodes_completed(state: TaskState) -> bool:
    for node_id in state.metadata.get("stage_order", []):
        node = state.plan_nodes.get(str(node_id))
        if node is None or node.status not in {PlanNodeStatus.COMPLETED, PlanNodeStatus.SUPERSEDED}:
            return False
    return True


def _first_stage_node(state: TaskState, stage: str) -> PlanNode | None:
    for node in state.plan_nodes.values():
        if node.metadata.get("stage") == stage:
            return node
    return None


def _compact_text(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    return f"{text[: max(0, budget - 32)]}\n[truncated by M3 message budget]"


def _symbolic_runtime() -> tuple[Any, Any]:
    from zyra_symbolic import ConstraintKeeper, TopologyRouter

    return ConstraintKeeper(), TopologyRouter()
