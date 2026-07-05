from __future__ import annotations

from zyra_core import ControlCommand, EventRecord, EventType, to_jsonable


EVENT_HINTS: dict[str, EventType] = {
    "control_command": EventType.CONTROL_COMMAND,
    "failure_injected": EventType.FAILURE_INJECTED,
    "requirement_change": EventType.REQUIREMENT_CHANGE,
    "evaluation": EventType.EVALUATION,
    "budget_updated": EventType.BUDGET_UPDATED,
}


def control_event_from_command(
    command: ControlCommand,
    node_id: str | None = None,
) -> EventRecord:
    hint = str(command.metadata.get("event_hint") or "control_command")
    event_type = EVENT_HINTS.get(hint, EventType.CONTROL_COMMAND)
    return EventRecord(
        run_id=command.run_id,
        task_id=command.task_id,
        event_type=event_type,
        node_id=node_id,
        payload={
            "command": to_jsonable(command),
            "raw": command.arguments.get("raw", ""),
            "source": command.metadata.get("source", ""),
        },
    )
