from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SubagentRuntimeError(RuntimeError):
    """Base error carrying a stable machine-readable code.

    The runtime never relies on exception text for recovery classification.
    Every boundary error has a code, a fail-closed flag, and a detached detail
    payload that can be persisted without serializing the exception object.
    """

    message: str
    code: str = "subagent_runtime_error"
    fail_closed: bool = True
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "fail_closed": self.fail_closed,
            "detail": dict(self.detail),
        }


class SubagentDisabled(SubagentRuntimeError):
    def __init__(self, component: str = "SubagentRuntime") -> None:
        super().__init__(
            f"{component} is disabled",
            code="subagent_component_disabled",
            detail={"component": component},
        )


class AgentDefinitionError(SubagentRuntimeError):
    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message, code="agent_definition_invalid", detail=detail)


class AgentDefinitionNotFound(SubagentRuntimeError):
    def __init__(self, agent_type: str) -> None:
        super().__init__(
            f"agent definition was not found: {agent_type}",
            code="agent_definition_not_found",
            detail={"agent_type": agent_type},
        )


class AgentDefinitionConflict(SubagentRuntimeError):
    def __init__(self, agent_type: str, source: str) -> None:
        super().__init__(
            f"agent definition conflict for {agent_type}",
            code="agent_definition_conflict",
            detail={"agent_type": agent_type, "source": source},
        )


class ToolScopeViolation(SubagentRuntimeError):
    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message, code="subagent_tool_scope_violation", detail=detail)


class PermissionExpansionDenied(SubagentRuntimeError):
    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message, code="subagent_permission_expansion_denied", detail=detail)


class SubagentBudgetExceeded(SubagentRuntimeError):
    def __init__(self, dimension: str, used: int | float, limit: int | float) -> None:
        super().__init__(
            f"subagent {dimension} budget exceeded ({used} > {limit})",
            code="subagent_budget_exceeded",
            detail={"dimension": dimension, "used": used, "limit": limit},
        )


class SubagentDepthExceeded(SubagentRuntimeError):
    def __init__(self, depth: int, maximum: int) -> None:
        super().__init__(
            f"subagent depth {depth} exceeds maximum {maximum}",
            code="subagent_depth_exceeded",
            detail={"depth": depth, "maximum": maximum},
        )


class SubagentCycleDetected(SubagentRuntimeError):
    def __init__(self, ancestry: tuple[str, ...], agent_type: str) -> None:
        super().__init__(
            f"subagent cycle detected for {agent_type}",
            code="subagent_cycle_detected",
            detail={"ancestry": list(ancestry), "agent_type": agent_type},
        )


class SubagentTaskNotFound(SubagentRuntimeError):
    def __init__(self, task_id: str) -> None:
        super().__init__(
            f"subagent task was not found: {task_id}",
            code="subagent_task_not_found",
            detail={"task_id": task_id},
        )


class SubagentTransitionRejected(SubagentRuntimeError):
    def __init__(self, task_id: str, current: str, requested: str) -> None:
        super().__init__(
            f"invalid subagent task transition {current} -> {requested}",
            code="subagent_transition_rejected",
            detail={"task_id": task_id, "current": current, "requested": requested},
        )


class SubagentRevisionConflict(SubagentRuntimeError):
    def __init__(self, task_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"subagent task revision conflict for {task_id}",
            code="subagent_revision_conflict",
            detail={"task_id": task_id, "expected": expected, "actual": actual},
        )


class SubagentDispatchRejected(SubagentRuntimeError):
    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message, code="subagent_dispatch_rejected", detail=detail)


class SubagentExecutionFailed(SubagentRuntimeError):
    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message, code="subagent_execution_failed", detail=detail)


class IsolationRequestRejected(SubagentRuntimeError):
    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message, code="subagent_isolation_rejected", detail=detail)


class IsolationCleanupFailed(SubagentRuntimeError):
    def __init__(self, isolation_id: str, message: str, **detail: Any) -> None:
        super().__init__(
            message,
            code="subagent_isolation_cleanup_failed",
            detail={"isolation_id": isolation_id, **detail},
        )


class TranscriptIntegrityError(SubagentRuntimeError):
    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message, code="subagent_transcript_integrity_error", detail=detail)


class ContinuationRejected(SubagentRuntimeError):
    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message, code="subagent_continuation_rejected", detail=detail)


class ParentCancelled(SubagentRuntimeError):
    def __init__(self, parent_task_id: str) -> None:
        super().__init__(
            "parent task was cancelled",
            code="subagent_parent_cancelled",
            detail={"parent_task_id": parent_task_id},
        )


class StructuredHandoffRequired(SubagentRuntimeError):
    def __init__(self, task_id: str) -> None:
        super().__init__(
            "subagent completion requires a structured handoff",
            code="subagent_structured_handoff_required",
            detail={"task_id": task_id},
        )

