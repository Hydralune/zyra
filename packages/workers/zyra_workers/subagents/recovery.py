from __future__ import annotations

from typing import Any

from .errors import SubagentRuntimeError
from .models import (
    RecoveryDisposition,
    RecoverySignal,
    SubagentFailureKind,
    SubagentTaskRecord,
)


class SubagentRecoverySignalRuntime:
    """Classify failures into a versioned recovery input.

    Recovery planning remains owned by M1-07C. This runtime only guarantees
    deterministic classification and records enough context for that planner.
    """

    RETRYABLE_CODES = frozenset(
        {
            "tool_timeout",
            "model_rate_limited",
            "stream_stalled",
            "transient_transport_error",
            "subagent_execution_failed",
        }
    )

    def signal(
        self,
        record: SubagentTaskRecord,
        error: BaseException | None,
        *,
        error_code: str = "",
        reason: str = "",
    ) -> RecoverySignal:
        code = error_code or getattr(error, "code", "") or type(error).__name__ if error else error_code
        kind = self._kind(code, error)
        retryable = code in self.RETRYABLE_CODES or kind in {
            SubagentFailureKind.TIMEOUT,
            SubagentFailureKind.DISPATCH,
        }
        disposition = RecoveryDisposition.RETRY if retryable else RecoveryDisposition.REPLAN
        if kind in {SubagentFailureKind.PERMISSION, SubagentFailureKind.ISOLATION, SubagentFailureKind.CLEANUP}:
            disposition = RecoveryDisposition.REROUTE
        if kind == SubagentFailureKind.CANCELLED:
            disposition = RecoveryDisposition.TERMINATE
            retryable = False
        message = reason or (str(error) if error else code or "subagent failure")
        detail = getattr(error, "detail", {}) if isinstance(error, SubagentRuntimeError) else {}
        return RecoverySignal(
            failure_kind=kind,
            disposition=disposition,
            reason=message,
            retryable=retryable,
            task_id=record.task_id,
            attempt=record.attempt,
            error_code=str(code),
            metadata={
                "agent_type": record.agent_type,
                "definition_id": record.definition_id,
                "execution_ref": record.execution_ref,
                "tool_scope_digest": record.tool_scope.digest,
                "permission_digest": record.permission.digest,
                "detail": dict(detail),
                "owner": "M1-03D recovery signal; planner owner M1-07C",
            },
        )

    def _kind(self, code: str, error: BaseException | None) -> SubagentFailureKind:
        normalized = str(code).lower()
        if "permission" in normalized or "grant" in normalized:
            return SubagentFailureKind.PERMISSION
        if "budget" in normalized or "turn" in normalized or "depth" in normalized or "cycle" in normalized:
            return SubagentFailureKind.BUDGET
        if "dispatch" in normalized or "transport" in normalized:
            return SubagentFailureKind.DISPATCH
        if "timeout" in normalized or "stalled" in normalized:
            return SubagentFailureKind.TIMEOUT
        if "cancel" in normalized or "killed" in normalized:
            return SubagentFailureKind.CANCELLED
        if "cleanup" in normalized:
            return SubagentFailureKind.CLEANUP
        if "isolation" in normalized or "workspace" in normalized:
            return SubagentFailureKind.ISOLATION
        if "transcript" in normalized or "sidechain" in normalized:
            return SubagentFailureKind.TRANSCRIPT
        if "definition" in normalized or "validation" in normalized or "scope" in normalized:
            return SubagentFailureKind.VALIDATION
        if error is not None:
            return SubagentFailureKind.EXECUTION
        return SubagentFailureKind.UNKNOWN
