from __future__ import annotations

from typing import Any, Mapping

from zyra_core import EventRecord, EventType

from .models import (
    IsolationCleanupReceipt,
    StructuredHandoff,
    SubagentDispatchReceipt,
    SubagentProgress,
    SubagentTaskRecord,
)


def subagent_event(
    record: SubagentTaskRecord,
    phase: str,
    *,
    root_task_id: str | None = None,
    node_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
    causation_id: str = "",
    correlation_id: str = "",
) -> EventRecord:
    event_type = {
        "task_created": EventType.SUBAGENT_TASK_CREATED,
        "validated": EventType.SUBAGENT_TASK_UPDATED,
        "dispatched": EventType.SUBAGENT_DISPATCHED,
        "running": EventType.SUBAGENT_TASK_UPDATED,
        "progress": EventType.SUBAGENT_PROGRESS,
        "completed": EventType.SUBAGENT_COMPLETED,
        "failed": EventType.SUBAGENT_FAILED,
        "cancelled": EventType.SUBAGENT_CANCELLED,
        "resumed": EventType.SUBAGENT_RESUMED,
        "message_queued": EventType.SUBAGENT_MESSAGE,
        "cleanup": EventType.SUBAGENT_ISOLATION,
    }.get(phase, EventType.SUBAGENT_TASK_UPDATED)
    return EventRecord(
        run_id=record.run_id,
        task_id=root_task_id or record.parent_task_id,
        node_id=node_id,
        event_type=event_type,
        payload={
            "schema": "zyra.subagent-event/v1",
            "phase": phase,
            "subagent_task_id": record.task_id,
            "parent_task_id": record.parent_task_id,
            "parent_session_id": record.parent_session_id,
            "agent_type": record.agent_type,
            "status": record.status.value,
            "revision": record.revision,
            "attempt": record.attempt,
            "execution_ref": record.execution_ref,
            "correlation_id": correlation_id or record.task_id,
            "causation_id": causation_id,
            "payload": dict(payload or {}),
        },
    )

def dispatch_event(
    record: SubagentTaskRecord,
    receipt: SubagentDispatchReceipt,
    *,
    root_task_id: str | None = None,
) -> EventRecord:
    return subagent_event(
        record,
        "dispatched",
        root_task_id=root_task_id,
        payload={"dispatch": receipt.to_dict()},
        causation_id=receipt.dispatch_id,
    )


def progress_event(record: SubagentTaskRecord, progress: SubagentProgress) -> EventRecord:
    return subagent_event(
        record,
        "progress",
        payload={"progress": progress.to_dict()},
        causation_id=progress.progress_id,
    )


def handoff_event(record: SubagentTaskRecord, handoff: StructuredHandoff) -> EventRecord:
    phase = "completed" if handoff.recovery_signal is None else "failed"
    return subagent_event(
        record,
        phase,
        payload={"handoff": handoff.safe_dict()},
        causation_id=handoff.handoff_id,
    )


def cleanup_event(record: SubagentTaskRecord, receipt: IsolationCleanupReceipt) -> EventRecord:
    return subagent_event(
        record,
        "cleanup",
        payload={"cleanup": receipt.to_dict()},
        causation_id=receipt.cleanup_id,
    )
