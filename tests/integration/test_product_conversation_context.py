from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.api.zyra_api.session_api import session_conversation_context, session_projection
from zyra_core import PlanNodeStatus, create_task_state
from zyra_memory import SQLiteStore
from zyra_orchestration.deployment.code_worker_adapter import _conversation_prompt


def test_conversation_uses_only_earlier_terminal_turns_in_the_same_session(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "conversation.sqlite3")

    def task(goal: str, second: int, session: str = "session_a", status=PlanNodeStatus.COMPLETED):
        state = create_task_state(goal)
        state.created_at = state.updated_at = f"2026-09-05T00:00:{second:02d}.000Z"
        state.status = status
        state.metadata.update(query_session_id=session, final_answer=f"answer:{goal}", permission_token="never-forward")
        store.save_checkpoint(state)
        return state

    first = task("青松七号", 1)
    task("foreign-private", 2, session="session_b")
    task("still-running", 3, status=PlanNodeStatus.RUNNING)
    current = task("repeat the marker", 4, status=PlanNodeStatus.RUNNING)
    task("future-turn", 5)
    context = session_conversation_context(store, current)
    assert context["turns"] == [{
        "task_id": first.task_id, "status": "completed", "user": "青松七号", "assistant": "answer:青松七号",
    }]
    assert context["truncated"] is False
    prompt = _conversation_prompt({"conversation": context}, task_id=current.task_id)
    assert "青松七号" in prompt
    assert "never-forward" not in prompt
    with pytest.raises(ValueError, match="binding"):
        _conversation_prompt({"conversation": context}, task_id="task_foreign")


def test_conversation_marks_omitted_history_and_bounds_model_context(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "conversation.sqlite3")
    prior = create_task_state("x" * 80_000)
    prior.status = PlanNodeStatus.COMPLETED
    prior.created_at = prior.updated_at = "2026-09-05T00:00:00.000Z"
    prior.metadata.update(query_session_id="session_long", final_answer="y" * 80_000)
    store.save_checkpoint(prior)
    current = create_task_state("continue")
    current.metadata["query_session_id"] = "session_long"
    current.created_at = "2026-09-05T00:00:01.000Z"
    context = session_conversation_context(store, current)
    assert context["truncated"] is True
    assert len(json.dumps(context)) < 65_000
    assert context["turns"][0]["user"].startswith("x")
    assert context["turns"][0]["assistant"].startswith("y")


def test_completed_multi_turn_session_resumes_latest_and_has_a_readable_title() -> None:
    tasks = [{
        "task_id": f"task_{index}", "session_id": "session_a", "status": "completed",
        "user_goal": "original question" if index == 1 else "follow-up",
        "created_at": f"2026-09-05T00:00:0{index}.000Z",
        "updated_at": f"2026-09-05T00:00:0{index}.000Z",
    } for index in [1, 2]]
    session = session_projection(tasks, "session_a")
    assert session["resolution"] == "resolved"
    assert session["resume_task_id"] == "task_2"
    assert session["title"] == "original question"
    for task in tasks:
        task["status"] = "running"
    assert session_projection(tasks, "session_a")["resolution"] == "ambiguous"
