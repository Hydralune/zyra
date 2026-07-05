from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from zyra_core import EventRecord, EventType, PlanNode, PlanNodeStatus, TaskState, new_id, to_jsonable


@dataclass(frozen=True, slots=True)
class ConstraintCheckResult:
    check_id: str = field(default_factory=lambda: new_id("check"))
    check_type: str = ""
    ok: bool = True
    severity: str = "info"
    reason: str = ""
    node_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return not self.ok and self.severity == "error"


class ConstraintKeeper:
    """Symbolic guard for task graph state transitions and sparse communication."""

    terminal_statuses = {
        PlanNodeStatus.COMPLETED,
        PlanNodeStatus.CANCELLED,
        PlanNodeStatus.SUPERSEDED,
    }

    runnable_statuses = {
        PlanNodeStatus.PENDING,
        PlanNodeStatus.BLOCKED,
        PlanNodeStatus.REPLANNED,
        PlanNodeStatus.NEEDS_REVISION,
    }

    def check_task_state(
        self,
        state: TaskState,
        *,
        node: PlanNode | None = None,
        transition: str = "inspect",
    ) -> list[ConstraintCheckResult]:
        nodes = [node] if node is not None else list(state.plan_nodes.values())
        results: list[ConstraintCheckResult] = [
            self._check_root_exists(state),
            self._check_budget(state),
        ]
        for item in nodes:
            results.extend(self.check_node(state, item, transition=transition))
        return results

    def check_node(
        self,
        state: TaskState,
        node: PlanNode,
        *,
        transition: str = "inspect",
    ) -> list[ConstraintCheckResult]:
        return [
            self._check_node_schema(node),
            self._check_dependencies(state, node),
            self._check_permission_surface(state, node),
            self._check_message_budget(state, node),
            self._check_forbidden_terms(state, node),
            self._check_transition(node, transition),
            self._check_termination(node),
        ]

    def event_for_results(
        self,
        state: TaskState,
        results: list[ConstraintCheckResult],
        *,
        node_id: str | None = None,
        stage: str = "",
    ) -> EventRecord:
        blocking = [result for result in results if result.blocking]
        warnings = [result for result in results if not result.ok and result.severity == "warning"]
        return EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.CONSTRAINT_CHECK,
            node_id=node_id,
            payload={
                "stage": stage,
                "ok": not blocking,
                "blocking_count": len(blocking),
                "warning_count": len(warnings),
                "results": [to_jsonable(result) for result in results],
            },
        )

    def has_blocking_failure(self, results: list[ConstraintCheckResult]) -> bool:
        return any(result.blocking for result in results)

    def _check_root_exists(self, state: TaskState) -> ConstraintCheckResult:
        ok = state.root_node_id in state.plan_nodes
        return ConstraintCheckResult(
            check_type="schema.root_node",
            ok=ok,
            severity="error" if not ok else "info",
            reason="Root node is present." if ok else "TaskState root_node_id does not reference a known PlanNode.",
            node_id=state.root_node_id,
        )

    def _check_budget(self, state: TaskState) -> ConstraintCheckResult:
        limit = state.constraints.max_tool_calls
        if limit is None:
            return ConstraintCheckResult(
                check_type="budget.tool_calls",
                ok=True,
                reason="No max_tool_calls limit is active.",
                metadata={"tool_calls": state.budget.tool_calls},
            )
        ok = state.budget.tool_calls <= limit
        return ConstraintCheckResult(
            check_type="budget.tool_calls",
            ok=ok,
            severity="error" if not ok else "info",
            reason="Tool-call budget is within limit." if ok else "Tool-call budget exceeded.",
            metadata={"tool_calls": state.budget.tool_calls, "max_tool_calls": limit},
        )

    def _check_node_schema(self, node: PlanNode) -> ConstraintCheckResult:
        missing = []
        if not node.title.strip():
            missing.append("title")
        if not node.description.strip():
            missing.append("description")
        if not node.completion_criteria and node.metadata.get("stage") not in {"route"}:
            missing.append("completion_criteria")
        ok = not missing
        return ConstraintCheckResult(
            check_type="schema.plan_node",
            ok=ok,
            severity="warning" if missing == ["completion_criteria"] else ("error" if not ok else "info"),
            reason="PlanNode schema is sufficiently structured." if ok else "PlanNode is missing structured fields.",
            node_id=node.node_id,
            metadata={"missing": missing},
        )

    def _check_dependencies(self, state: TaskState, node: PlanNode) -> ConstraintCheckResult:
        missing = [dependency_id for dependency_id in node.depends_on if dependency_id not in state.plan_nodes]
        incomplete = [
            dependency_id
            for dependency_id in node.depends_on
            if dependency_id in state.plan_nodes
            and state.plan_nodes[dependency_id].status != PlanNodeStatus.COMPLETED
        ]
        ok = not missing and not incomplete
        return ConstraintCheckResult(
            check_type="graph.dependencies",
            ok=ok,
            severity="error" if missing else ("warning" if incomplete else "info"),
            reason=(
                "Dependencies are complete."
                if ok
                else "PlanNode has missing or incomplete dependencies."
            ),
            node_id=node.node_id,
            metadata={"missing": missing, "incomplete": incomplete},
        )

    def _check_permission_surface(self, state: TaskState, node: PlanNode) -> ConstraintCheckResult:
        allowed = node.constraints.allowed_workers or state.constraints.allowed_workers
        if not allowed or not node.assigned_worker_id:
            return ConstraintCheckResult(
                check_type="permission.worker",
                ok=True,
                reason="No worker allow-list violation is present.",
                node_id=node.node_id,
                metadata={"allowed_workers": allowed, "assigned_worker_id": node.assigned_worker_id},
            )
        ok = node.assigned_worker_id in allowed
        return ConstraintCheckResult(
            check_type="permission.worker",
            ok=ok,
            severity="error" if not ok else "info",
            reason="Assigned worker is allowed." if ok else "Assigned worker violates allowed_workers.",
            node_id=node.node_id,
            metadata={"allowed_workers": allowed, "assigned_worker_id": node.assigned_worker_id},
        )

    def _check_message_budget(self, state: TaskState, node: PlanNode) -> ConstraintCheckResult:
        budget = node.constraints.message_budget_chars or state.constraints.message_budget_chars
        text_size = len(node.description) + len(node.summary)
        ok = text_size <= budget
        return ConstraintCheckResult(
            check_type="communication.low_entropy_budget",
            ok=ok,
            severity="warning" if not ok else "info",
            reason="Node summary fits the low-entropy message budget." if ok else "Node carries too much free text.",
            node_id=node.node_id,
            metadata={"chars": text_size, "budget": budget},
        )

    def _check_forbidden_terms(self, state: TaskState, node: PlanNode) -> ConstraintCheckResult:
        forbidden = [item.lower() for item in [*state.constraints.forbidden, *node.constraints.forbidden]]
        haystack = f"{state.user_goal}\n{node.title}\n{node.description}".lower()
        matches = [item for item in forbidden if item and item in haystack]
        ok = not matches
        return ConstraintCheckResult(
            check_type="policy.forbidden_terms",
            ok=ok,
            severity="error" if not ok else "info",
            reason="No forbidden term was found." if ok else "Forbidden constraint term appears in task content.",
            node_id=node.node_id,
            metadata={"matches": matches},
        )

    def _check_transition(self, node: PlanNode, transition: str) -> ConstraintCheckResult:
        if transition == "start" and node.status not in self.runnable_statuses:
            return ConstraintCheckResult(
                check_type="state_transition.start",
                ok=False,
                severity="error",
                reason="Only pending, blocked, replanned, or needs_revision nodes can start.",
                node_id=node.node_id,
                metadata={"status": str(node.status)},
            )
        if transition == "complete" and node.status != PlanNodeStatus.RUNNING:
            return ConstraintCheckResult(
                check_type="state_transition.complete",
                ok=False,
                severity="warning",
                reason="Completion should normally follow a running transition.",
                node_id=node.node_id,
                metadata={"status": str(node.status)},
            )
        return ConstraintCheckResult(
            check_type=f"state_transition.{transition}",
            ok=True,
            reason="State transition is acceptable for M3 control.",
            node_id=node.node_id,
            metadata={"status": str(node.status)},
        )

    def _check_termination(self, node: PlanNode) -> ConstraintCheckResult:
        if node.status not in self.terminal_statuses:
            return ConstraintCheckResult(
                check_type="termination.criteria",
                ok=True,
                reason="Node is not terminal yet.",
                node_id=node.node_id,
            )
        if node.completion_criteria:
            return ConstraintCheckResult(
                check_type="termination.criteria",
                ok=True,
                reason="Terminal node has explicit completion criteria.",
                node_id=node.node_id,
            )
        return ConstraintCheckResult(
            check_type="termination.criteria",
            ok=False,
            severity="warning",
            reason="Terminal node has no explicit completion criteria.",
            node_id=node.node_id,
        )
