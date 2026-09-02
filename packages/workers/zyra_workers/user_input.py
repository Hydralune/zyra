from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from zyra_core import now_iso
from zyra_memory import (
    SQLiteStore,
    canonical_user_input_digest,
    normalize_user_input_questions,
)
from zyra_runtime.tools import ToolCall, ToolResult, ToolSpec


def user_input_tool_spec() -> ToolSpec:
    return ToolSpec(
        "request_user_input",
        (
            "Ask the user one to three concise questions when their decision is "
            "required to continue. Offer two or three mutually exclusive options "
            "for each question; the client also permits a free-form answer."
        ),
        "zyra canonical user-input continuation",
        input_schema={
            "type": "object",
            "required": ["questions"],
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "required": ["header", "id", "question", "options"],
                        "properties": {
                            "header": {"type": "string", "maxLength": 12},
                            "id": {
                                "type": "string",
                                "pattern": "^[a-z][a-z0-9_]{0,63}$",
                            },
                            "question": {"type": "string", "maxLength": 4096},
                            "options": {
                                "type": "array",
                                "minItems": 2,
                                "maxItems": 3,
                                "items": {
                                    "type": "object",
                                    "required": ["label", "description"],
                                    "properties": {
                                        "label": {"type": "string", "maxLength": 80},
                                        "description": {
                                            "type": "string",
                                            "maxLength": 1024,
                                        },
                                    },
                                },
                            },
                        },
                    },
                }
            },
        },
        output_schema={
            "type": "object",
            "required": ["answers"],
            "properties": {"answers": {"type": "object"}},
        },
        metadata={
            "access_mode": "read_only",
            "read_only": "true",
            "concurrency_safe": "false",
            "mutates_workspace": "false",
            "blocks_for_user_input": "true",
        },
    )


class CanonicalUserInputBridge:
    """Durably suspend a provider tool call until the exact request is answered."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        poll_seconds: float = 0.2,
        maximum_wait_seconds: float = 86_400.0,
    ) -> None:
        self.store = SQLiteStore(database_path)
        self.poll_seconds = max(0.05, min(2.0, float(poll_seconds)))
        self.maximum_wait_seconds = max(1.0, float(maximum_wait_seconds))

    def __call__(self, call: ToolCall) -> ToolResult:
        questions = normalize_user_input_questions(call.arguments.get("questions"))
        request_digest = canonical_user_input_digest(questions)
        request_id = "request_" + canonical_user_input_digest(
            {
                "task_id": call.task_id,
                "tool_call_id": call.tool_call_id,
                "request_digest": request_digest,
            }
        )[:28]
        created_at = now_iso()
        request = self.store.create_user_input_request(
            request_id=request_id,
            run_id=call.run_id,
            task_id=call.task_id,
            node_id=call.node_id,
            tool_call_id=call.tool_call_id,
            request_digest=request_digest,
            questions=questions,
            created_at=created_at,
        )

        deadline = time.monotonic() + self.maximum_wait_seconds
        while request["status"] == "pending":
            task = self.store.load_task(call.task_id)
            if task is None or str(task.status) in {
                "cancelled",
                "completed",
                "failed",
                "blocked",
                "killed",
            }:
                request = self._close_request(
                    call.task_id,
                    request,
                    status="cancelled",
                )
                if request["status"] == "answered":
                    break
                return ToolResult(
                    tool_call_id=call.tool_call_id,
                    ok=False,
                    summary="User input was cancelled because the task is no longer active.",
                    error="user_input_task_inactive",
                    metadata={"request_id": request_id},
                )
            if time.monotonic() >= deadline:
                request = self._close_request(
                    call.task_id,
                    request,
                    status="expired",
                )
                if request["status"] == "answered":
                    break
                return ToolResult(
                    tool_call_id=call.tool_call_id,
                    ok=False,
                    summary="Timed out while waiting for user input.",
                    error="user_input_timeout",
                    metadata={"request_id": request_id},
                )
            time.sleep(self.poll_seconds)
            request = self.store.user_input_request(call.task_id, request_id) or request

        if request["status"] != "answered":
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary="User input request was closed without an answer.",
                error="user_input_unavailable",
                metadata={"request_id": request_id},
            )
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=True,
            summary="User answered the requested questions.",
            output={
                "request_id": request_id,
                "answers": dict(request["answers"]),
            },
            metadata={
                "request_id": request_id,
                "request_revision": str(request["revision"]),
                "canonical_owner": "SQLiteStore.user_input_requests",
            },
        )

    def _close_request(
        self,
        task_id: str,
        request: dict[str, Any],
        *,
        status: str,
    ) -> dict[str, Any]:
        try:
            return self.store.close_user_input_request(
                task_id=task_id,
                request_id=str(request["request_id"]),
                expected_revision=int(request["revision"]),
                status=status,
                closed_at=now_iso(),
            )
        except (KeyError, ValueError):
            # The user may have answered between the last poll and close.
            return self.store.user_input_request(
                task_id,
                str(request["request_id"]),
            ) or request


__all__ = ["CanonicalUserInputBridge", "user_input_tool_spec"]
