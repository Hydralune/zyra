from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from collections.abc import Sequence
from typing import Any, Callable, Mapping
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
    workspace_runtime_resolver: (
        Callable[[TaskState, PlanNode, str], tuple[Path, Mapping[str, Any]]] | None
    ) = None
    topology_policy_trigger: (
        Callable[
            [TaskState, PlanNode | None, EventRecord | None],
            Mapping[str, Any],
        ]
        | None
    ) = None
    resource_scheduler: Any | None = None
    execution_placement_validator: (
        Callable[[TaskState, PlanNode | None], Mapping[str, Any]] | None
    ) = None
    physical_execution_runner: (
        Callable[[TaskState, PlanNode], tuple[Any, str]] | None
    ) = None
    execution_outcome_recorder: (
        Callable[[TaskState, PlanNode, Any], Mapping[str, Any]] | None
    ) = None
    # Restarts crashed deployment nodes and rebinds their worker manifests.
    # Recovery needs this before replanning: placement is bound to a process
    # identity, so a lost node must be replaced before a new lease can be
    # issued against a live one.
    physical_runtime_refresher: (
        Callable[[], Mapping[str, Any] | None] | None
    ) = None
    final_verifier: (
        Callable[[TaskState, Sequence[EventRecord]], Mapping[str, Any]] | None
    ) = None
    completion_gate: (
        Callable[
            [TaskState, Sequence[EventRecord]],
            Mapping[str, Any],
        ]
        | None
    ) = None

    @classmethod
    def from_paths(
        cls,
        *,
        project_root: str | Path,
        workspace_root: str | Path,
        artifact_root: str | Path,
        permission_store_path: str | Path | None = None,
        workspace_runtime_resolver: (
            Callable[[TaskState, PlanNode, str], tuple[Path, Mapping[str, Any]]] | None
        ) = None,
        topology_policy_trigger: (
            Callable[
                [TaskState, PlanNode | None, EventRecord | None],
                Mapping[str, Any],
            ]
            | None
        ) = None,
        resource_scheduler: Any | None = None,
        execution_placement_validator: (
            Callable[[TaskState, PlanNode | None], Mapping[str, Any]] | None
        ) = None,
        physical_execution_runner: (
            Callable[[TaskState, PlanNode], tuple[Any, str]] | None
        ) = None,
        execution_outcome_recorder: (
            Callable[[TaskState, PlanNode, Any], Mapping[str, Any]] | None
        ) = None,
        physical_runtime_refresher: (
            Callable[[], Mapping[str, Any] | None] | None
        ) = None,
        final_verifier: (
            Callable[[TaskState, Sequence[EventRecord]], Mapping[str, Any]] | None
        ) = None,
        completion_gate: (
            Callable[
                [TaskState, Sequence[EventRecord]],
                Mapping[str, Any],
            ]
            | None
        ) = None,
    ) -> "GraphExecutionContext":
        return cls(
            project_root=Path(project_root).resolve(),
            workspace_root=Path(workspace_root).resolve(),
            artifact_root=Path(artifact_root).resolve(),
            permission_store_path=None if permission_store_path is None else Path(permission_store_path).resolve(),
            workspace_runtime_resolver=workspace_runtime_resolver,
            topology_policy_trigger=topology_policy_trigger,
            resource_scheduler=resource_scheduler,
            execution_placement_validator=execution_placement_validator,
            physical_execution_runner=physical_execution_runner,
            physical_runtime_refresher=physical_runtime_refresher,
            execution_outcome_recorder=execution_outcome_recorder,
            final_verifier=final_verifier,
            completion_gate=completion_gate,
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


# Two replan passes per run admit the complete three-route production provider
# chain (initial GLM route, then DeepSeek, then Kimi).  A pass is gated either
# on reconciliation proving no side effect started or on the explicit durable
# workspace continuation contract.  A third identical failure is therefore a
# persistent fault for the recovery planner, not an unbounded replay.
_EXECUTION_RECOVERY_PASSES = 2


def _consume_execution_retry_request(state: TaskState) -> dict[str, Any] | None:
    """Take a pending replan request if the recovery budget still allows one."""

    request = state.metadata.pop("physical_execution_retry_requested", None)
    if not isinstance(request, Mapping):
        return None
    spent = int(state.metadata.get("physical_execution_recovery_passes") or 0)
    if spent >= _EXECUTION_RECOVERY_PASSES:
        return None
    state.metadata["physical_execution_recovery_passes"] = spent + 1
    return dict(request)


def _reset_stage_for_recovery(
    state: TaskState,
    request: Mapping[str, Any],
) -> list[EventRecord]:
    """Reopen the failed execute node and the route node that placed it.

    Placement is bound to a deployment process identity, so a lost node cannot
    be re-dispatched on the same lease.  Rerunning the route stage is what
    produces a lease bound to a live process.
    """

    node_id = str(request.get("node_id") or "")
    node = state.plan_nodes.get(node_id)
    if node is None:
        return []
    reopened: list[str] = []
    for candidate in state.plan_nodes.values():
        stage = str(candidate.metadata.get("stage") or "")
        if candidate.node_id == node_id or stage == "route":
            candidate.status = PlanNodeStatus.PENDING
            candidate.updated_at = now_iso()
            candidate.metadata.pop("result_summary", None)
            candidate.metadata.pop("worker_error", None)
            reopened.append(candidate.node_id)
    state.metadata.pop("operator_placement_binding", None)
    # Mark the pass that follows as a recovery trigger.  The topology composer
    # holds a minimum-dwell guard against churn, and the first window committed
    # seconds ago -- without this the recovery commit is rejected, the operator
    # candidate set is dropped, and placement is free to send a physical
    # operator to a worker that cannot host it.  Only the route stage reads
    # this, so it scopes to the one route this replan reopens.
    state.metadata["physical_execution_recovery_active"] = {
        "node_id": node_id,
        "recovery_pass": int(
            state.metadata.get("physical_execution_recovery_passes") or 0
        ),
        "requested_at": now_iso(),
    }
    state.status = PlanNodeStatus.RUNNING
    state.updated_at = now_iso()
    return [
        EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=node_id,
            event_type=EventType.RESOURCE_DECISION,
            payload={
                "schema": "zyra.execution-recovery-replan/v1",
                "summary": (
                    "Dispatch crossed a lost node after a durable workspace "
                    "checkpoint; continuing on fresh placement."
                    if request.get(
                        "checkpointed_side_effect_recovery_requested"
                    )
                    else
                    "Dispatch outcome reconciled as never started; replanning "
                    "the execute node onto fresh placement."
                ),
                "reopened_node_ids": reopened,
                "error_code": str(request.get("error_code") or ""),
                "error_message": str(request.get("error_message") or ""),
                "reconciliations": request.get("reconciliations") or [],
                "dispatch_error": request.get("dispatch_error") or {},
                "checkpointed_side_effect_recovery_requested": bool(
                    request.get(
                        "checkpointed_side_effect_recovery_requested"
                    )
                ),
                "runtime_refresh_error": str(
                    request.get("runtime_refresh_error") or ""
                ),
                "runtime_refresh_receipt": to_jsonable(
                    request.get("runtime_refresh_receipt") or {}
                ),
                "recovery_pass": int(
                    state.metadata.get("physical_execution_recovery_passes") or 0
                ),
            },
        )
    ]


def run_task_graph(
    state: TaskState,
    execution_context: GraphExecutionContext | None = None,
) -> list[EventRecord]:
    if state.status in {PlanNodeStatus.CANCELLED, PlanNodeStatus.COMPLETED}:
        return []

    events = ensure_default_graph(state)
    events.extend(_complete_root_if_needed(state))

    stage_results = {spec.stage: spec.result_summary for spec in DEFAULT_STAGE_SPECS}
    keeper, router = _symbolic_runtime(execution_context)
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
            handoff = _temporal_handoff_event(state, node, events)
            events.append(handoff)
            events.extend(
                _run_route_node(
                    state,
                    node,
                    router,
                    start_checks,
                    cause_event=handoff,
                )
            )
        elif stage == "verify":
            events.extend(_run_verify_node(state, node, keeper))
        else:
            events.extend(_run_node(state, node, stage_results.get(stage, "")))

    replan = _consume_execution_retry_request(state)
    if replan is not None:
        refresher = (
            execution_context.physical_runtime_refresher
            if execution_context is not None
            else None
        )
        refreshed_error = ""
        refresh_receipt: Mapping[str, Any] = {}
        if refresher is not None:
            try:
                refresh_receipt = refresher() or {}
            except Exception as error:  # noqa: BLE001 - replan must stay reportable.
                refreshed_error = f"{type(error).__name__}: {error}"
        events.extend(
            _reset_stage_for_recovery(
                state,
                {
                    **replan,
                    "runtime_refresh_error": refreshed_error,
                    "runtime_refresh_receipt": refresh_receipt,
                },
            )
        )
        try:
            events.extend(run_task_graph(state, execution_context))
        finally:
            # The marker only licenses the replan's own route stage.  Leaving it
            # in run metadata would make every later route claim the recovery
            # dwell exemption, including a resume of this task.
            state.metadata.pop("physical_execution_recovery_active", None)
        return events

    if _all_stage_nodes_completed(state):
        state.status = PlanNodeStatus.COMPLETED
        state.updated_at = now_iso()
        if execution_context is not None and execution_context.final_verifier is not None:
            verifier = dict(execution_context.final_verifier(state, tuple(events)))
            events.append(
                EventRecord(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    event_type=EventType.EVALUATION,
                    node_id=state.root_node_id,
                    payload=verifier,
                )
            )
        if execution_context is not None and execution_context.completion_gate is not None:
            try:
                gate = dict(
                    execution_context.completion_gate(state, tuple(events))
                )
            except Exception as error:  # noqa: BLE001 - post-side-effect uncertainty is terminal for this pass.
                state.status = PlanNodeStatus.BLOCKED
                state.updated_at = now_iso()
                events.append(
                    EventRecord(
                        run_id=state.run_id,
                        task_id=state.task_id,
                        event_type=EventType.SYSTEM_NOTICE,
                        node_id=state.root_node_id,
                        payload={
                            "schema": "zyra.production-completion-gate-error/v1",
                            "summary": (
                                "Physical execution completed, but canonical "
                                "completion evidence could not be closed."
                            ),
                            "error": type(error).__name__,
                            "message": str(error),
                            "automatic_execution_retry_allowed": False,
                            "recovery": "repair_evidence_then_revalidate",
                        },
                    )
                )
                return events
            decision = str(gate.get("decision") or "").casefold()
            remaining = int(gate.get("remaining_operator_count") or 0)
            hard_conditions_passed = gate.get("hard_conditions_passed") is True
            gate_allows_completion = hard_conditions_passed and (
                decision == "exit" or remaining == 0
            )
            events.append(
                EventRecord(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    event_type=EventType.EVALUATION,
                    node_id=state.root_node_id,
                    payload=gate,
                )
            )
            if decision == "continue" and remaining > 0:
                _prepare_adaptive_depth_continuation(state)
                events.append(
                    EventRecord(
                        run_id=state.run_id,
                        task_id=state.task_id,
                        event_type=EventType.SYSTEM_NOTICE,
                        node_id=state.root_node_id,
                        payload={
                            "schema": "zyra.production-adaptive-depth-continuation/v1",
                            "summary": "The next MaAS layer will execute through a new route and lease.",
                            "early_exit_decision_id": str(gate.get("decision_id") or ""),
                            "remaining_operator_count": remaining,
                            "executed_operator_refs": list(
                                gate.get("executed_operator_refs") or ()
                            ),
                        },
                    )
                )
                events.extend(run_task_graph(state, execution_context=execution_context))
                return events
            if not gate_allows_completion:
                state.status = PlanNodeStatus.BLOCKED
                state.updated_at = now_iso()
                events.append(
                    EventRecord(
                        run_id=state.run_id,
                        task_id=state.task_id,
                        event_type=EventType.SYSTEM_NOTICE,
                        node_id=state.root_node_id,
                        payload={
                            "schema": "zyra.production-completion-gate-blocked/v1",
                            "summary": (
                                "Task completion is blocked until the remaining "
                                "MaAS depth executes or early-exit hard conditions pass."
                            ),
                            "early_exit_decision_id": str(
                                gate.get("decision_id") or ""
                            ),
                            "remaining_operator_count": remaining,
                        },
                    )
                )
                return events
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


def _prepare_adaptive_depth_continuation(state: TaskState) -> None:
    count = int(state.metadata.get("phase2_adaptive_depth_pass") or 0) + 1
    if count > 64:
        raise RuntimeError("adaptive depth exceeded the bounded production pass limit")
    state.metadata["phase2_adaptive_depth_pass"] = count
    state.status = PlanNodeStatus.PENDING
    state.updated_at = now_iso()
    for node in state.plan_nodes.values():
        stage = str(node.metadata.get("stage") or "")
        if stage not in {"route", "execute", "verify", "finalize"}:
            continue
        node.status = PlanNodeStatus.PENDING
        node.assigned_worker_id = None
        node.updated_at = state.updated_at


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


def _run_route_node(
    state: TaskState,
    node: PlanNode,
    router: Any,
    route_checks: list[Any],
    *,
    cause_event: EventRecord,
) -> list[EventRecord]:
    events: list[EventRecord] = []
    node.status = PlanNodeStatus.RUNNING
    node.updated_at = now_iso()
    events.append(_node_event(state, node, "running", "Topology routing started."))

    target = _first_stage_node(state, "execute") or node
    decision, route_event = router.route(
        state,
        node=target,
        route_type="worker_route",
        cause_event=cause_event,
    )
    decision.checks = [to_jsonable(result) for result in route_checks]
    route_event.payload["decision"] = to_jsonable(decision)
    events.append(route_event)
    resource_event = _resource_decision_event_from_route(route_event)
    if resource_event is not None:
        events.append(resource_event)

    topology_policy = route_event.payload.get("topology_policy")
    final_policy = topology_policy
    if (
        isinstance(topology_policy, Mapping)
        and topology_policy.get("reroute_required") is True
    ):
        reroute_cause = _policy_reroute_cause_event(state, node, events)
        events.append(reroute_cause)
        decision, route_event = router.route(
            state,
            node=target,
            route_type="worker_route",
            # The handoff is a new immutable observation window.  Its
            # causation points at the completed message window emitted by the
            # first invocation, so AgentPrune can only consume prior data.
            cause_event=reroute_cause,
        )
        decision.checks = [to_jsonable(result) for result in route_checks]
        route_event.payload["decision"] = to_jsonable(decision)
        events.append(route_event)
        resource_event = _resource_decision_event_from_route(route_event)
        if resource_event is not None:
            events.append(resource_event)
        final_policy = route_event.payload.get("topology_policy")
        if (
            isinstance(final_policy, Mapping)
            and final_policy.get("reroute_required") is True
        ):
            operator_selection = final_policy.get("operator_selection")
            operator_selection = (
                operator_selection
                if isinstance(operator_selection, Mapping)
                else {}
            )
            raise RuntimeError(
                "production topology reroute did not consume the prior actual communication window: "
                + json.dumps(
                    {
                        "degraded_reason": final_policy.get("degraded_reason"),
                        "operator_degraded_reason": operator_selection.get(
                            "degraded_reason"
                        ),
                        "topology_degraded_reason": (
                            final_policy.get("topology_result") or {}
                        ).get("degraded_reason"),
                        "reroute_reason": final_policy.get("reroute_reason"),
                        "candidate_count": len(
                            final_policy.get("communication_candidate_edges") or ()
                        ),
                        "outcome_count": final_policy.get(
                            "communication_outcome_count"
                        ),
                    },
                    sort_keys=True,
                )
            )

    formal_strongest_required = bool(
        state.metadata.get("formal_benchmark")
        or state.metadata.get("sealed_autonomous")
    )
    revalidated_nodes_value = state.metadata.get(
        "phase2_topology_revalidated_route_nodes"
    )
    revalidated_nodes = {
        str(item)
        for item in (
            revalidated_nodes_value
            if isinstance(revalidated_nodes_value, (list, tuple, set))
            else ()
        )
        if str(item)
    }
    revalidation_already_attempted = node.node_id in revalidated_nodes
    needs_bounded_revalidation = bool(
        formal_strongest_required
        and isinstance(final_policy, Mapping)
        and final_policy.get("used_baseline") is True
        and final_policy.get("committed") is not True
        and final_policy.get("communication_outcome_coverage_complete") is True
        and not revalidation_already_attempted
    )
    if needs_bounded_revalidation:
        revalidated_nodes.add(node.node_id)
        state.metadata["phase2_topology_revalidated_route_nodes"] = sorted(
            revalidated_nodes
        )
        state.metadata["phase2_topology_revalidation_count"] = int(
            state.metadata.get("phase2_topology_revalidation_count") or 0
        ) + 1
        revalidation_cause = _policy_reroute_cause_event(
            state,
            node,
            events,
            reason="revalidate_strongest_after_covered_baseline",
        )
        events.append(revalidation_cause)
        decision, route_event = router.route(
            state,
            node=target,
            route_type="worker_route",
            cause_event=revalidation_cause,
        )
        decision.checks = [to_jsonable(result) for result in route_checks]
        route_event.payload["decision"] = to_jsonable(decision)
        events.append(route_event)
        resource_event = _resource_decision_event_from_route(route_event)
        if resource_event is not None:
            events.append(resource_event)
        final_policy = route_event.payload.get("topology_policy")
    if formal_strongest_required and (
        not isinstance(final_policy, Mapping)
        or final_policy.get("used_baseline") is True
        or final_policy.get("committed") is not True
        or final_policy.get("reroute_required") is True
    ):
        policy_value = (
            final_policy if isinstance(final_policy, Mapping) else {}
        )
        operator_selection = policy_value.get("operator_selection")
        operator_selection = (
            operator_selection
            if isinstance(operator_selection, Mapping)
            else {}
        )
        raise RuntimeError(
            "formal strongest topology remained uncommitted after bounded revalidation: "
            + json.dumps(
                {
                    "degraded_reason": policy_value.get("degraded_reason"),
                    "operator_degraded_reason": operator_selection.get(
                        "degraded_reason"
                    ),
                    "execution_degraded_reason": (
                        policy_value.get("execution_receipt") or {}
                    ).get("degraded_reason"),
                    "topology_degraded_reason": (
                        policy_value.get("topology_result") or {}
                    ).get("degraded_reason"),
                    "candidate_count": len(
                        policy_value.get("communication_candidate_edges") or ()
                    ),
                    "outcome_count": policy_value.get(
                        "communication_outcome_count"
                    ),
                    "coverage_complete": policy_value.get(
                        "communication_outcome_coverage_complete"
                    ),
                    "reroute_reason": policy_value.get("reroute_reason"),
                    "revalidation_already_attempted": (
                        revalidation_already_attempted
                        or needs_bounded_revalidation
                    ),
                    "revalidation_count": int(
                        state.metadata.get("phase2_topology_revalidation_count")
                        or 0
                    ),
                },
                sort_keys=True,
            )
        )

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
    if execution_context.execution_placement_validator is not None:
        placement_gate = execution_context.execution_placement_validator(
            state,
            node,
        )
        events.append(
            EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.CONSTRAINT_CHECK,
                node_id=node.node_id,
                payload=dict(placement_gate),
            )
        )
    node.status = PlanNodeStatus.RUNNING
    node.updated_at = now_iso()
    events.append(_node_event(state, node, "running", "Worker runtime execution started."))

    continuation = _loopx_continuation(state)
    if continuation.get("enabled") and not continuation.get(
        "continuation_allowed"
    ):
        error = RuntimeError(
            "LoopX continuation is blocked by quota, sync, or validation state"
        )
        node.status = PlanNodeStatus.BLOCKED
        node.updated_at = now_iso()
        node.metadata["result_summary"] = (
            "LoopX continuation gate blocked physical worker dispatch."
        )
        node.metadata["loopx_continuation"] = continuation
        events.append(
            _node_event(
                state,
                node,
                "blocked",
                node.metadata["result_summary"],
            )
        )
        events.append(
            EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.SYSTEM_NOTICE,
                node_id=node.node_id,
                payload={
                    "schema": "zyra.loopx-continuation-blocked/v1",
                    "summary": node.metadata["result_summary"],
                    "loopx_continuation": continuation,
                    "physical_worker_dispatched": False,
                    "execution_budget_spent": False,
                    "recovery": "repair_sync_or_quota_then_replan",
                },
            )
        )
        events.extend(_plan_runtime_recovery(state, node, error=error))
        return events

    try:
        if execution_context.physical_execution_runner is not None:
            worker_run, worker_name = execution_context.physical_execution_runner(
                state,
                node,
            )
        else:
            worker_run, worker_name = _run_selected_worker(
                state,
                node,
                execution_context,
            )
    except Exception as error:  # noqa: BLE001 - worker failures must stay in the trace.
        # A dispatch reconciled as "never started", or one explicitly bound to
        # a durable continuation workspace, is not a task failure yet.  Record
        # the request so the stage pass can replan this node onto fresh
        # placement instead of ending the run.  This reads the dedicated
        # replan signal rather than the broader
        # ``automatic_execution_retry_allowed``, which several older failure
        # paths set to describe side-effect safety alone.
        retry_metadata = dict(getattr(error, "metadata", {}) or {})
        if retry_metadata.get("physical_execution_replan_requested") is True:
            state.metadata["physical_execution_retry_requested"] = {
                "node_id": node.node_id,
                "error_code": str(getattr(error, "code", "") or ""),
                "error_message": str(error)[:500],
                "reconciliations": to_jsonable(
                    retry_metadata.get("physical_dispatch_reconciliations") or []
                ),
                "dispatch_error": to_jsonable(
                    retry_metadata.get("physical_dispatch_error") or {}
                ),
                "checkpointed_side_effect_recovery_requested": bool(
                    retry_metadata.get(
                        "checkpointed_side_effect_recovery_requested"
                    )
                ),
            }
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
                    "error_code": str(getattr(error, "code", "") or ""),
                    "message": str(error),
                    "error_metadata": to_jsonable(
                        getattr(error, "metadata", {}) or {}
                    ),
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
    if execution_context.execution_outcome_recorder is not None:
        try:
            physical_receipt = dict(
                execution_context.execution_outcome_recorder(
                    state,
                    node,
                    worker_run,
                )
            )
        except Exception as error:  # noqa: BLE001 - do not replay an entered physical side effect.
            node.status = PlanNodeStatus.BLOCKED
            node.updated_at = now_iso()
            state.status = PlanNodeStatus.BLOCKED
            state.updated_at = node.updated_at
            events.append(
                EventRecord(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    event_type=EventType.SYSTEM_NOTICE,
                    node_id=node.node_id,
                    payload={
                        "schema": "zyra.production-physical-receipt-error/v1",
                        "summary": (
                            "Worker call returned, but the physical attempt "
                            "owner did not close a canonical receipt."
                        ),
                        "error": type(error).__name__,
                        "error_code": str(
                            getattr(error, "code", "") or ""
                        ),
                        "message": str(error),
                        "error_metadata": to_jsonable(
                            getattr(error, "metadata", {}) or {}
                        ),
                        "automatic_execution_retry_allowed": False,
                        "recovery": "reconcile_attempt_before_retry",
                    },
                )
            )
            events.extend(_plan_runtime_recovery(state, node, error=error))
            return events
        events.append(
            EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.RESOURCE_DECISION,
                node_id=node.node_id,
                payload={
                    "schema": "zyra.production-physical-execution-receipt/v1",
                    "physical_execution_receipt": physical_receipt,
                },
            )
        )
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
    worker_projection = state.metadata.get("worker_pool")
    recovery_worker_id = (
        str(worker_projection.get("worker_id") or "")
        if isinstance(worker_projection, Mapping)
        else ""
    )
    selected_manifest = _selected_manifest(
        {} if recovery_worker_id else resource_decision,
        recovery_worker_id or node.assigned_worker_id,
    )
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
        workspace_root, runtime_services = _resolve_worker_runtime(
            state,
            node,
            execution_context,
            "BrowserWorker",
        )
        constraints = _browser_constraints(state, hints, browser_url)
        request = WorkerRequest(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=node.node_id,
            worker_name="BrowserWorker",
            constraints=constraints,
            metadata=request_metadata,
        )
        runtime = BrowserWorkerRuntime(
            project_root=execution_context.project_root,
            workspace_root=workspace_root,
            artifact_root=execution_context.artifact_root,
            workspace_edit_port=runtime_services.get("workspace_edit_port"),
            workspace_gateway_required=bool(runtime_services.get("workspace_gateway_required", False)),
        )
        return _dispatch_selected_worker_callable(
            state,
            node,
            execution_context,
            request=request,
            runtime=runtime,
            runtime_worker="BrowserWorker",
            workspace_root=workspace_root,
            preferred_backend_id=str(
                request_metadata.get("backend_id")
                or request_metadata.get("worker_manifest_id")
                or ""
            ) or None,
            provider_route_id=str(request_metadata.get("provider_route_id") or "") or None,
        )

    workspace_root, runtime_services = _resolve_worker_runtime(
        state,
        node,
        execution_context,
        "CodeWorkerRuntime",
    )
    request = WorkerRequest(
        run_id=state.run_id,
        task_id=state.task_id,
        node_id=node.node_id,
        worker_name="CodeWorkerRuntime",
        constraints=_code_constraints_with_session(
            state,
            hints,
            execution_context,
        ),
        metadata=request_metadata,
    )
    runtime = CodeWorkerRuntime(
        project_root=execution_context.project_root,
        workspace_root=workspace_root,
        artifact_root=execution_context.artifact_root,
        permission_store=(
            JsonPermissionStore(execution_context.permission_store_path)
            if execution_context.permission_store_path is not None
            else None
        ),
        runtime_services=runtime_services,
    )
    return _dispatch_selected_worker_callable(
        state,
        node,
        execution_context,
        request=request,
        runtime=runtime,
        runtime_worker="CodeWorkerRuntime",
        workspace_root=workspace_root,
        preferred_backend_id=str(
            request_metadata.get("backend_id")
            or request_metadata.get("worker_manifest_id")
            or ""
        ) or None,
        provider_route_id=str(request_metadata.get("provider_route_id") or "") or None,
    )


def _dispatch_selected_worker_callable(
    state: TaskState,
    node: PlanNode,
    execution_context: GraphExecutionContext,
    *,
    request: Any,
    runtime: Any,
    runtime_worker: str,
    workspace_root: Path,
    preferred_backend_id: str | None,
    provider_route_id: str | None,
):
    from zyra_scheduler import dispatch_worker_callable, event_record_from_backend
    from zyra_runtime.provider_control_plane import (
        ProviderRouteBindingRuntime,
        provider_database_path,
    )

    turn_id = str(
        request.metadata.get("turn_id")
        or state.metadata.get("provider_turn_id")
        or (
            f"{node.node_id}:worker-turn:"
            f"{int(state.metadata.get('phase2_adaptive_depth_pass') or 0)}"
        )
    )
    provider_session_id = str(
        request.metadata.get("provider_session_id")
        or state.metadata.get("provider_session_id")
        or f"provider:{state.run_id}:{state.task_id}"
    )
    provider_db = provider_database_path(execution_context.artifact_root)
    route_ref = ProviderRouteBindingRuntime(
        project_root=execution_context.project_root,
        database_path=provider_db,
        allow_explicit_sim_bootstrap=True,
    ).bind(
        run_id=state.run_id,
        task_id=state.task_id,
        node_id=node.node_id,
        session_id=provider_session_id,
        turn_id=turn_id,
        route_id=provider_route_id,
        purpose=str(request.metadata.get("provider_purpose") or "general"),
        preferred_provider_id=(
            str(request.metadata.get("preferred_provider_id") or "") or None
        ),
        preferred_model_id=(
            str(request.metadata.get("preferred_model_id") or "") or None
        ),
        require_tools=runtime_worker == "CodeWorkerRuntime",
        require_streaming=True,
        metadata={
            "runtimeWorker": runtime_worker,
            "m0ExecutionRef": f"worker_request:{request.request_id}",
        },
    )
    provider_route_id = route_ref.route_id
    request.constraints.update(route_ref.runtime_constraints(database_path=provider_db))
    request.metadata.update(
        {
            "provider_route_id": route_ref.route_id,
            "provider_route_checksum": route_ref.route_checksum,
            "provider_catalog_revision": str(route_ref.catalog_revision),
            "provider_credential_version": str(route_ref.credential_version),
            "provider_credential_fingerprint": route_ref.credential_fingerprint,
            "provider_transport_id": route_ref.transport_id,
            "provider_session_id": route_ref.session_id,
            "provider_turn_id": route_ref.turn_id,
            "provider_state_embedded": "false",
        }
    )
    node.metadata["provider_route_ref"] = route_ref.safe_dict()

    def execute(envelope: Any):
        request.metadata.update(
            {
                "backend_dispatch_envelope_id": str(envelope.envelope_id),
                "backend_lease_id": str(envelope.backend_lease_id),
                "backend_id": str(envelope.backend_id),
                "backend_kind": str(envelope.backend_kind),
                "backend_location": str(envelope.backend_location),
                "provider_route_id": str(envelope.provider_route_id or ""),
                "provider_route_checksum": envelope.provider_route_checksum,
                "provider_catalog_revision": str(envelope.provider_catalog_revision),
                "provider_credential_version": str(envelope.provider_credential_version),
                "provider_credential_fingerprint": envelope.provider_credential_fingerprint,
                "provider_transport_id": envelope.provider_transport_id,
                "m0_execution_ref": envelope.m0_execution_ref,
                "provider_state_embedded": "false",
            }
        )
        return runtime.run(request)

    competition_mode = str(
        state.metadata.get("competition_mode")
        or state.metadata.get("execution_mode")
        or ""
    ).strip().casefold()
    sealed_terminal_exclusion = bool(
        state.metadata.get("sealed")
        or state.metadata.get("formal_benchmark")
        or state.metadata.get("sealed_autonomous")
        or "sealed" in competition_mode
    )
    outcome = dispatch_worker_callable(
        run_id=state.run_id,
        task_id=state.task_id,
        node_id=node.node_id,
        runtime_worker=runtime_worker,
        preferred_backend_id=preferred_backend_id,
        workspace_root=workspace_root,
        artifact_root=execution_context.artifact_root,
        provider_route_id=provider_route_id,
        provider_route_checksum=route_ref.route_checksum,
        provider_catalog_revision=route_ref.catalog_revision,
        provider_credential_version=route_ref.credential_version,
        provider_credential_fingerprint=route_ref.credential_fingerprint,
        provider_transport_id=route_ref.transport_id,
        m0_execution_ref=f"worker_request:{request.request_id}",
        turn_id=turn_id,
        operation=execute,
        idempotency_key=f"worker-dispatch:{request.request_id}",
        exclude_terminal_backends=sealed_terminal_exclusion,
    )
    backend_events = [event_record_from_backend(event) for event in outcome.events]
    worker_run = replace(
        outcome.value,
        event_records=[*backend_events, *outcome.value.event_records],
    )
    node.metadata["backend_dispatch"] = {
        "state_owner": "python.BackendRegistryStore",
        "provider_state_owned": False,
        "final_lease": to_jsonable(outcome.final_lease),
        "final_envelope": to_jsonable(outcome.final_envelope),
        "attempts": [to_jsonable(item) for item in outcome.attempts],
        "backend_changed": outcome.backend_changed,
        "sealed_terminal_exclusion": sealed_terminal_exclusion,
        "dispatch_session": to_jsonable(outcome.session),
        "recovery_inputs": [to_jsonable(item) for item in outcome.recovery_inputs],
        "transport_responses": [
            to_jsonable(item) for item in outcome.transport_responses
        ],
        "provider_route_ref": route_ref.safe_dict(),
        "m0_execution_ref": f"worker_request:{request.request_id}",
    }
    return worker_run, runtime_worker


def _resolve_worker_runtime(
    state: TaskState,
    node: PlanNode,
    execution_context: GraphExecutionContext,
    worker_name: str,
) -> tuple[Path, dict[str, Any]]:
    resolver = execution_context.workspace_runtime_resolver
    if resolver is None:
        return execution_context.workspace_root, {}
    workspace_root, services = resolver(state, node, worker_name)
    return Path(workspace_root).resolve(), dict(services)


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
    if _benchmark_deadline_closeout_active(state):
        deadline = int(_runtime_hints(state).get("external_deadline_epoch_ms") or 0)
        node.metadata["recovery_plan"] = {
            "schema": "zyra.benchmark-deadline-terminal-recovery/v1",
            "action": "stop",
            "automatic_execution_retry_allowed": False,
            "external_deadline_epoch_ms": deadline,
        }
        return [
            EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.SYSTEM_NOTICE,
                node_id=node.node_id,
                payload={
                    "schema": "zyra.benchmark-deadline-terminal-recovery/v1",
                    "summary": (
                        "The benchmark closeout window is active; no recovery "
                        "dispatch will start beyond the authoritative budget."
                    ),
                    "automatic_execution_retry_allowed": False,
                    "external_deadline_epoch_ms": deadline,
                },
            )
        ]
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
    hints = _runtime_hints(state)
    worker_projection = (
        dict(state.metadata.get("worker_pool") or {})
        if isinstance(state.metadata.get("worker_pool"), Mapping)
        else {}
    )
    backend_projection = (
        dict(state.metadata.get("backend_route") or {})
        if isinstance(state.metadata.get("backend_route"), Mapping)
        else {}
    )
    provider_projection = (
        dict(state.metadata.get("provider_route") or {})
        if isinstance(state.metadata.get("provider_route"), Mapping)
        else {}
    )
    provider_route_id = str(
        provider_projection.get("route_id")
        or provider_projection.get("routeId")
        or hints.get("provider_route_id")
        or state.metadata.get("provider_route_id")
        or resource_decision.get("provider_route_id")
        or ""
    )
    worker_manifest_id = str(
        worker_projection.get("worker_id")
        or resource_decision.get("selected_manifest_id")
        or selected_manifest.get("worker_id")
        or ""
    )
    backend_id = str(
        backend_projection.get("backend_id")
        or backend_projection.get("backendId")
        or resource_decision.get("selected_backend_id")
        or ""
    )
    metadata = {
        "scheduler": "m5-resource-scheduler" if resource_decision else "",
        "resource_decision_id": str(resource_decision.get("decision_id") or ""),
        "worker_manifest_id": worker_manifest_id,
        "worker_lease_id": str(worker_projection.get("lease_id") or ""),
        "worker_attempt_id": str(worker_projection.get("attempt_id") or ""),
        "backend_id": backend_id,
        "backend_lease_id": str(
            backend_projection.get("lease_id")
            or backend_projection.get("backend_lease_id")
            or ""
        ),
        "backend": str(resource_decision.get("selected_backend") or selected_manifest.get("backend") or ""),
        "location": str(resource_decision.get("selected_location") or selected_manifest.get("location") or ""),
        "model_split": json.dumps(resource_decision.get("model_split") or {}, ensure_ascii=False, sort_keys=True),
        "provider_route_id": provider_route_id,
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
        provider_route_id=provider_route_id,
    )
    node.metadata["dispatch_envelope"] = to_jsonable(envelope)
    metadata.update(
        {
            "dispatch_envelope_id": envelope.envelope_id,
            "sandbox": envelope.sandbox,
            "gateway": envelope.gateway,
            "workspace_scope": str(envelope.metadata.get("workspace_scope") or ""),
            "provider_route_id": envelope.provider_route_id,
        }
    )
    return metadata


def _runtime_hints(state: TaskState) -> dict[str, Any]:
    hints = state.metadata.get("runtime_hints")
    return dict(hints) if isinstance(hints, dict) else {}


def _benchmark_deadline_closeout_active(state: TaskState) -> bool:
    hints = _runtime_hints(state)
    try:
        deadline = int(hints.get("external_deadline_epoch_ms") or 0)
        reserve_seconds = float(
            hints.get("benchmark_closeout_reserve_seconds") or 0.0
        )
    except (TypeError, ValueError):
        return False
    if deadline <= 0 or reserve_seconds <= 0:
        return False
    return int(time.time() * 1000) >= deadline - int(reserve_seconds * 1000)


def _loopx_continuation(state: TaskState) -> dict[str, Any]:
    value = state.metadata.get("loopx_continuation")
    return dict(value) if isinstance(value, Mapping) else {}


def _code_constraints(state: TaskState, hints: dict[str, Any]) -> dict[str, Any]:
    continuation = _loopx_continuation(state)
    if isinstance(hints.get("tool_plan"), list):
        constraints: dict[str, Any] = {
            "tool_plan": hints["tool_plan"],
            "permission_mode": "acceptEdits",
        }
        if str(hints.get("session_id") or ""):
            constraints["session_id"] = str(hints["session_id"])
        if continuation.get("enabled"):
            constraints["loopx_continuation"] = continuation
        return constraints
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
            "## LoopX Continuation",
            "",
            (
                f"- goal_id: `{continuation.get('goal_id')}`\n"
                f"- todo_id: `{continuation.get('todo_id')}`\n"
                f"- obligation: {continuation.get('obligation')}"
                if continuation.get("enabled")
                else "- disabled"
            ),
            "",
            "## Runtime",
            "",
            "Executed through CodeWorkerRuntime and Zyra ToolExecutor.",
            "",
        ]
    )
    constraints = {
        "permission_mode": "acceptEdits",
        "tool_plan": [
            {"tool_name": "file_write", "arguments": {"path": relative_path, "content": content}},
            {"tool_name": "file_read", "arguments": {"path": relative_path}},
        ]
    }
    if str(hints.get("session_id") or ""):
        constraints["session_id"] = str(hints["session_id"])
    if continuation.get("enabled"):
        constraints["loopx_continuation"] = continuation
    return constraints


def _code_constraints_with_session(
    state: TaskState,
    hints: dict[str, Any],
    execution_context: GraphExecutionContext,
) -> dict[str, Any]:
    constraints = _code_constraints(state, hints)
    adaptive_pass = int(
        state.metadata.get("phase2_adaptive_depth_pass") or 0
    )
    if adaptive_pass > 0:
        # Distinct MaAS operators own distinct query sessions even when they
        # share one canonical task and execute node.  Memory/graph continuity
        # is carried by their owners, not by replaying another operator's
        # private permission-session token.
        constraints["session_id"] = (
            f"task:{state.task_id}:maas-layer:{adaptive_pass + 1}"
        )
        constraints.pop("session_custody_token", None)
        constraints.pop("permission_session_custody_token", None)
        return constraints
    provider = getattr(
        execution_context.topology_policy_trigger,
        "worker_session_envelope",
        None,
    )
    if callable(provider):
        envelope = provider(state)
        if isinstance(envelope, Mapping):
            session_id = str(envelope.get("session_id") or "")
            custody_token = str(envelope.get("session_custody_token") or "")
            if session_id and custody_token:
                constraints["session_id"] = session_id
                constraints["session_custody_token"] = custody_token
    return constraints


def _browser_constraints(
    state: TaskState,
    hints: dict[str, Any],
    browser_url: str,
) -> dict[str, Any]:
    constraints: dict[str, Any] = {}
    continuation = _loopx_continuation(state)
    if continuation.get("enabled"):
        constraints["loopx_continuation"] = continuation
    browser_backend = hints.get("browser_backend")
    if isinstance(browser_backend, str) and browser_backend.strip():
        constraints["browser_backend"] = browser_backend.strip()
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


def _temporal_handoff_event(
    state: TaskState,
    route_node: PlanNode,
    events: list[EventRecord],
) -> EventRecord:
    source = _first_stage_node(state, "plan")
    source_refs = tuple(
        item.event_id
        for item in events
        if item.node_id == (source.node_id if source is not None else None)
        and item.event_type in {EventType.AGENT_MESSAGE, EventType.NODE_UPDATED}
    )
    return EventRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        event_type=EventType.RESOURCE_DECISION,
        node_id=route_node.node_id,
        payload={
            "schema": "zyra.phase2-temporal-handoff-receipt/v1",
            "checkpoint_ref": (
                source_refs[-1]
                if source_refs
                else f"task-state:{state.task_id}:plan"
            ),
            "source_stage": "plan",
            "target_stage": "route",
            "source_event_refs": list(source_refs),
            "acknowledged": bool(
                source is not None
                and source.status is PlanNodeStatus.COMPLETED
                and route_node.status is PlanNodeStatus.PENDING
            ),
            "canonical_owner": "TaskGraphRuntime/EventRecord",
        },
    )


def _policy_reroute_cause_event(
    state: TaskState,
    route_node: PlanNode,
    events: list[EventRecord],
    *,
    reason: str = "consume_completed_communication_window",
) -> EventRecord:
    prior_route_refs = tuple(
        item.event_id
        for item in events
        if item.event_type is EventType.TOPOLOGY_ROUTE and item.event_id
    )
    return EventRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        event_type=EventType.SYSTEM_NOTICE,
        node_id=route_node.node_id,
        payload={
            "schema": "zyra.phase2-temporal-handoff-receipt/v1",
            "checkpoint_ref": (
                prior_route_refs[-1]
                if prior_route_refs
                else f"task-state:{state.task_id}:route-observation"
            ),
            "source_stage": "route_observation",
            "target_stage": "route",
            "source_event_refs": list(prior_route_refs),
            "acknowledged": True,
            "reason": reason,
            "canonical_owner": "TaskGraphRuntime/EventRecord",
        },
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


def _symbolic_runtime(
    execution_context: GraphExecutionContext | None = None,
) -> tuple[Any, Any]:
    from zyra_symbolic import ConstraintKeeper, TopologyRouter

    return ConstraintKeeper(), TopologyRouter(
        resource_scheduler=(
            None
            if execution_context is None
            else execution_context.resource_scheduler
        ),
        topology_policy_trigger=(
            None
            if execution_context is None
            else execution_context.topology_policy_trigger
        )
    )
