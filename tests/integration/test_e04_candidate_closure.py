from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = REPO_ROOT / "packages" / "runtime"
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from zyra_runtime.runtime_events.integration import LegacyEventNormalizer  # noqa: E402


def test_e04_root_dependency() -> None:
    inspected = [
        REPO_ROOT / "package.json",
        REPO_ROOT / "bun.lock",
        REPO_ROOT / "apps" / "code-worker" / "src" / "main.ts",
        REPO_ROOT
        / "packages"
        / "workers"
        / "zyra_workers"
        / "typescript_claude_runtime.py",
    ]
    forbidden = (
        "../claude-code-best",
        "../opencode",
        "../OpenClaw",
        "../openclaw",
        "file:../",
        "npm link",
    )
    hits = [
        f"{path.relative_to(REPO_ROOT)}:{pattern}"
        for path in inspected
        for pattern in forbidden
        if pattern in path.read_text(encoding="utf-8")
    ]
    assert hits == []


def test_slotted_legacy_event_is_normalized() -> None:
    @dataclass(frozen=True, slots=True)
    class SlottedEvent:
        event_id: str
        event_type: str
        run_id: str
        task_id: str
        payload: dict[str, object]

    normalized = LegacyEventNormalizer().normalize(
        SlottedEvent(
            event_id="e04-slotted-event",
            event_type="task_created",
            run_id="e04-run",
            task_id="e04-task",
            payload={"state": "created"},
        )
    )
    assert normalized.legacy_event_id == "e04-slotted-event"
    assert normalized.draft["payload"]["taskId"] == "e04-task"
