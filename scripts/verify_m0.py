from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE_PATH = ROOT / "packages" / "core"
if str(CORE_PATH) not in sys.path:
    sys.path.insert(0, str(CORE_PATH))

from zyra_core import (
    AgentMessage,
    AgentRole,
    ControlCommand,
    EventRecord,
    EventType,
    MessageIntent,
    create_task_state,
    to_jsonable,
)
from zyra_core.event_log import append_event, read_events


REQUIRED_PATHS = [
    "pyproject.toml",
    ".env.example",
    "apps/api/zyra_api/main.py",
    "apps/web/index.html",
    "packages/core/zyra_core/models.py",
    "packages/core/zyra_core/event_log.py",
    "packages/orchestration",
    "packages/runtime",
    "packages/commands",
    "packages/skills",
    "packages/workers",
    "packages/memory",
    "packages/scheduler",
    "packages/symbolic",
    "packages/evaluation",
    "packages/integrations",
    "docs/architecture",
    "docs/plans/M0.md",
    "docs/scenarios",
]


def verify_required_paths() -> None:
    missing = [path for path in REQUIRED_PATHS if not (ROOT / path).exists()]
    if missing:
        raise AssertionError(f"Missing required paths: {missing}")


def verify_core_schema_and_event_log() -> None:
    state = create_task_state("Verify the M0 baseline.")
    message = AgentMessage(
        run_id=state.run_id,
        task_id=state.task_id,
        node_id=state.root_node_id,
        sender_role=AgentRole.USER,
        receiver_role=AgentRole.SUPERVISOR,
        intent=MessageIntent.REQUEST,
        content="Start the run.",
    )
    command = ControlCommand(
        run_id=state.run_id,
        task_id=state.task_id,
        name="/status",
        arguments={"format": "json"},
    )
    event = EventRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        event_type=EventType.AGENT_MESSAGE,
        node_id=state.root_node_id,
        payload={"message": to_jsonable(message), "command": to_jsonable(command)},
    )

    encoded = json.dumps(to_jsonable(event), ensure_ascii=False)
    decoded = json.loads(encoded)
    assert decoded["run_id"].startswith("run_")
    assert decoded["task_id"].startswith("task_")
    assert decoded["node_id"].startswith("node_")
    assert decoded["event_type"] == str(EventType.AGENT_MESSAGE)
    assert decoded["payload"]["message"]["intent"] == str(MessageIntent.REQUEST)
    assert decoded["payload"]["command"]["name"] == "/status"

    log_path = ROOT / "tmp" / "verify" / "events.jsonl"
    append_event(event, log_path)
    events = read_events(log_path)
    assert events[-1]["event_id"] == event.event_id


def main() -> None:
    verify_required_paths()
    verify_core_schema_and_event_log()
    print("M0 verification passed")


if __name__ == "__main__":
    main()
