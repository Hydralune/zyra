from __future__ import annotations

import re
from typing import Any

from zyra_core import (
    ConstraintSet,
    EventRecord,
    EventType,
    MessageIntent,
    PlanNode,
    PlanNodeStatus,
    TaskState,
    now_iso,
    to_jsonable,
)

from .constraints import ConstraintKeeper
from .topology import TopologyRouter


def apply_requirement_change(
    state: TaskState,
    event: EventRecord,
    *,
    router: TopologyRouter | None = None,
    keeper: ConstraintKeeper | None = None,
) -> list[EventRecord]:
    raw = _event_text(event)
    affected_nodes = _select_affected_nodes(state, raw)
    route = router or TopologyRouter()
    guard = keeper or ConstraintKeeper()
    events: list[EventRecord] = []

    affected_node_ids: list[str] = []
    for node in affected_nodes:
        previous_status = node.status
        node.status = PlanNodeStatus.SUPERSEDED
        node.updated_at = now_iso()
        node.metadata["superseded_by_requirement_change"] = event.event_id
        node.metadata["previous_status"] = str(previous_status)
        node.state_delta["status"] = str(node.status)
        affected_node_ids.append(node.node_id)
        events.append(_node_update_event(state, node, "superseded", "Node superseded by requirement change."))

    replan_node = PlanNode(
        title="Requirement Change Replan",
        description=raw or "Requirement change requested during task execution.",
        intent=MessageIntent.REQUIREMENT_CHANGE,
        parent_node_id=state.root_node_id,
        depends_on=[state.root_node_id],
        constraints=ConstraintSet(
            requirements=[raw] if raw else [],
            success_criteria=[
                "Associate the change with affected PlanNodes.",
                "Produce a local graph adjustment instead of clearing task state.",
            ],
            output_schema={
                "type": "object",
                "required": ["affected_node_ids", "revision_plan", "worker_route"],
            },
        ),
        expected_output_schema={
            "type": "object",
            "required": ["affected_node_ids", "revision_plan", "worker_route"],
        },
        completion_criteria=[
            "Requirement change has affected_node_ids.",
            "A topology route decision exists for the revision node.",
        ],
        metadata={
            "stage": "replan",
            "source": "requirement_change",
            "source_event_id": event.event_id,
            "affected_node_ids": list(affected_node_ids),
            "graph_version": "m3-symbolic-v1",
        },
    )
    state.plan_nodes[replan_node.node_id] = replan_node
    state.metadata.setdefault("stage_order", []).append(replan_node.node_id)
    state.status = PlanNodeStatus.NEEDS_REVISION
    state.updated_at = event.created_at
    events.append(
        EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.NODE_CREATED,
            node_id=replan_node.node_id,
            payload={"node": to_jsonable(replan_node), "source_event_id": event.event_id},
        )
    )

    decision, route_event = route.route(
        state,
        node=replan_node,
        cause_event=event,
        route_type="requirement_change_replan",
    )
    decision.affected_node_ids = [*affected_node_ids, replan_node.node_id]
    decision.state_delta["affected_node_ids"] = list(affected_node_ids)
    route_event.payload["decision"] = to_jsonable(decision)
    events.append(route_event)
    resource_event = _resource_decision_event_from_route(route_event)
    if resource_event is not None:
        events.append(resource_event)

    check_results = guard.check_task_state(state, node=replan_node, transition="start")
    decision.checks = [to_jsonable(result) for result in check_results]
    route_event.payload["decision"] = to_jsonable(decision)
    events.append(
        guard.event_for_results(
            state,
            check_results,
            node_id=replan_node.node_id,
            stage="requirement_change_replan",
        )
    )

    changes = state.metadata.setdefault("requirement_changes", [])
    changes.append(
        {
            "event_id": event.event_id,
            "node_id": event.node_id,
            "text": raw,
            "affected_node_ids": affected_node_ids,
            "replan_node_id": replan_node.node_id,
            "decision_id": decision.decision_id,
            "resource_decision_id": str(route_event.payload.get("resource_decision", {}).get("decision_id") if isinstance(route_event.payload.get("resource_decision"), dict) else ""),
            "created_at": event.created_at,
        }
    )
    return events


def apply_failure_injection(
    state: TaskState,
    event: EventRecord,
    *,
    router: TopologyRouter | None = None,
    keeper: ConstraintKeeper | None = None,
) -> list[EventRecord]:
    raw = _event_text(event)
    target = _select_failure_target(state, raw)
    route = router or TopologyRouter()
    guard = keeper or ConstraintKeeper()
    events: list[EventRecord] = []
    target_node_id = None

    if target is not None:
        target_node_id = target.node_id
        previous_status = target.status
        target.status = PlanNodeStatus.SUPERSEDED
        target.updated_at = now_iso()
        target.metadata["failure_injection_event_id"] = event.event_id
        target.metadata["previous_status"] = str(previous_status)
        target.metadata["failure_status"] = "failed"
        target.state_delta["status"] = str(target.status)
        events.append(
            EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.NODE_FAILED,
                node_id=target.node_id,
                payload={
                    "summary": "Node failure injected and superseded for recovery routing.",
                    "raw": raw,
                    "node": to_jsonable(target),
                },
            )
        )

    recovery_node = PlanNode(
        title="Failure Recovery",
        description=raw or "Recover from an injected anomaly or node failure.",
        intent=MessageIntent.NODE_FAILURE,
        parent_node_id=state.root_node_id,
        depends_on=[state.root_node_id],
        constraints=ConstraintSet(
            requirements=["Recover from injected failure without clearing durable task state."],
            success_criteria=[
                "Identify the failed or perturbed node.",
                "Select a recovery worker route with failure history considered.",
            ],
            output_schema={
                "type": "object",
                "required": ["target_node_id", "recovery_action", "worker_route"],
            },
        ),
        expected_output_schema={
            "type": "object",
            "required": ["target_node_id", "recovery_action", "worker_route"],
        },
        completion_criteria=[
            "A recovery route decision exists.",
            "Failure event is preserved in the trace.",
        ],
        metadata={
            "stage": "recover",
            "source": "failure_injection",
            "source_event_id": event.event_id,
            "target_node_id": target_node_id,
            "graph_version": "m3-symbolic-v1",
        },
    )
    state.plan_nodes[recovery_node.node_id] = recovery_node
    state.metadata.setdefault("stage_order", []).append(recovery_node.node_id)
    state.status = PlanNodeStatus.NEEDS_REVISION
    state.updated_at = event.created_at
    events.append(
        EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.NODE_CREATED,
            node_id=recovery_node.node_id,
            payload={"node": to_jsonable(recovery_node), "source_event_id": event.event_id},
        )
    )

    decision, route_event = route.route(
        state,
        node=recovery_node,
        cause_event=event,
        route_type="failure_recovery_route",
    )
    decision.affected_node_ids = [item for item in [target_node_id, recovery_node.node_id] if item]
    decision.state_delta["target_node_id"] = target_node_id
    route_event.payload["decision"] = to_jsonable(decision)
    events.append(route_event)
    resource_event = _resource_decision_event_from_route(route_event)
    if resource_event is not None:
        events.append(resource_event)

    check_results = guard.check_task_state(state, node=recovery_node, transition="start")
    decision.checks = [to_jsonable(result) for result in check_results]
    route_event.payload["decision"] = to_jsonable(decision)
    events.append(
        guard.event_for_results(
            state,
            check_results,
            node_id=recovery_node.node_id,
            stage="failure_recovery_route",
        )
    )

    recovery_plan_id = ""
    try:
        from zyra_scheduler import RecoveryPlanner, RuntimeWatchdog
    except Exception:  # noqa: BLE001 - symbolic control remains usable before scheduler path is configured.
        pass
    else:
        signal = RuntimeWatchdog().classify(state, node=recovery_node, event=event, decision=None)
        planner = RecoveryPlanner()
        plan = planner.plan(state, signal, node=recovery_node, cause_event=event)
        recovery_plan_id = plan.plan_id
        recovery_node.metadata["recovery_plan"] = to_jsonable(plan)
        events.append(planner.event_for_plan(plan, signal))

    failures = state.metadata.setdefault("failure_injections", [])
    failures.append(
        {
            "event_id": event.event_id,
            "node_id": event.node_id,
            "text": raw,
            "target_node_id": target_node_id,
            "recovery_node_id": recovery_node.node_id,
            "decision_id": decision.decision_id,
            "resource_decision_id": str(route_event.payload.get("resource_decision", {}).get("decision_id") if isinstance(route_event.payload.get("resource_decision"), dict) else ""),
            "recovery_plan_id": recovery_plan_id,
            "created_at": event.created_at,
        }
    )
    return events


def _event_text(event: EventRecord) -> str:
    return str(event.payload.get("raw") or "").strip()


def _select_affected_nodes(state: TaskState, text: str) -> list[PlanNode]:
    tokens = _tokens(text)
    scored: list[tuple[int, PlanNode]] = []
    for node in state.plan_nodes.values():
        if node.node_id == state.root_node_id or node.status in {PlanNodeStatus.CANCELLED, PlanNodeStatus.SUPERSEDED}:
            continue
        haystack = " ".join([node.title, node.description, str(node.metadata)]).lower()
        score = sum(1 for token in tokens if token in haystack)
        stage = str(node.metadata.get("stage") or "").lower()
        if stage and stage in text.lower():
            score += 3
        if score:
            scored.append((score, node))
    if scored:
        scored.sort(key=lambda item: item[0], reverse=True)
        return [node for _, node in scored[:4]]
    preferred_stages = {"execute", "verify", "finalize", "route"}
    fallback = [
        node
        for node in state.plan_nodes.values()
        if node.node_id != state.root_node_id
        and node.status not in {PlanNodeStatus.CANCELLED, PlanNodeStatus.SUPERSEDED}
        and str(node.metadata.get("stage") or "") in preferred_stages
    ]
    if fallback:
        return fallback[:3]
    root = state.plan_nodes.get(state.root_node_id)
    return [] if root is None else [root]


def _select_failure_target(state: TaskState, text: str) -> PlanNode | None:
    lowered = text.lower()
    for node in state.plan_nodes.values():
        if node.node_id in text:
            return node
    for node in state.plan_nodes.values():
        worker = str(node.assigned_worker_id or "").lower()
        stage = str(node.metadata.get("stage") or "").lower()
        if worker and worker in lowered:
            return node
        if stage and stage in lowered:
            return node
    for status in [PlanNodeStatus.RUNNING, PlanNodeStatus.PENDING, PlanNodeStatus.BLOCKED, PlanNodeStatus.COMPLETED]:
        for node in state.plan_nodes.values():
            if node.node_id != state.root_node_id and node.status == status:
                return node
    return state.plan_nodes.get(state.root_node_id)


def _node_update_event(state: TaskState, node: PlanNode, transition: str, summary: str) -> EventRecord:
    return EventRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        event_type=EventType.NODE_UPDATED,
        node_id=node.node_id,
        payload={"transition": transition, "summary": summary, "node": to_jsonable(node)},
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


def _tokens(text: str) -> list[str]:
    return [token for token in re.findall(r"[A-Za-z0-9_./:-]+", text.lower()) if len(token) > 2]
