from __future__ import annotations

import time
import threading
from pathlib import Path
from typing import Any, Sequence

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
        continuation_request_ids: Sequence[str] = (),
    ) -> None:
        self.store = SQLiteStore(database_path)
        self.poll_seconds = max(0.05, min(2.0, float(poll_seconds)))
        self.maximum_wait_seconds = max(1.0, float(maximum_wait_seconds))
        self.continuation_request_ids = tuple(
            dict.fromkeys(
                str(request_id).strip()
                for request_id in continuation_request_ids
                if str(request_id).strip()
            )
        )
        self._claimed_continuation_requests: set[str] = set()
        self._continuation_lock = threading.Lock()

    def __call__(self, call: ToolCall) -> ToolResult:
        questions = normalize_user_input_questions(call.arguments.get("questions"))
        request_digest = canonical_user_input_digest(questions)
        request = self._claim_continuation_request(call, questions)
        rebound = request is not None
        if request is None:
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
        else:
            request_id = str(request["request_id"])

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
                "answers": self._answers_for_current_questions(request, questions),
            },
            metadata={
                "request_id": request_id,
                "request_revision": str(request["revision"]),
                "canonical_owner": "SQLiteStore.user_input_requests",
                "continuation_rebound": str(rebound).lower(),
            },
        )

    def _claim_continuation_request(
        self,
        call: ToolCall,
        questions: Sequence[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Bind one recovered provider call to its pre-restart user prompt.

        A deployment recovery owns a fresh permission session, so its provider
        may emit a new tool-call id while replaying the interrupted turn.  The
        adapter supplies only requests that were pending when that recovery
        began.  Matching the stable header and ordered option labels lets the
        recovered call wait on the original canonical request instead of
        showing the user the same question twice.
        """

        with self._continuation_lock:
            for request_id in self.continuation_request_ids:
                if request_id in self._claimed_continuation_requests:
                    continue
                request = self.store.user_input_request(call.task_id, request_id)
                if request is None or request.get("run_id") != call.run_id:
                    continue
                if request.get("status") not in {"pending", "answered"}:
                    continue
                if not self._questions_are_compatible(
                    request.get("questions") or (),
                    questions,
                ):
                    continue
                self._claimed_continuation_requests.add(request_id)
                return request
        return None

    @staticmethod
    def _questions_are_compatible(
        previous: Sequence[dict[str, Any]],
        current: Sequence[dict[str, Any]],
    ) -> bool:
        if len(previous) != len(current):
            return False
        for old, new in zip(previous, current, strict=True):
            if str(old.get("header") or "") != str(new.get("header") or ""):
                return False
            old_labels = tuple(
                str(option.get("label") or "")
                for option in old.get("options") or ()
                if isinstance(option, dict)
            )
            new_labels = tuple(
                str(option.get("label") or "")
                for option in new.get("options") or ()
                if isinstance(option, dict)
            )
            if not old_labels or old_labels != new_labels:
                return False
        return True

    @staticmethod
    def _answers_for_current_questions(
        request: dict[str, Any],
        current: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        previous = request.get("questions") or ()
        stored = request.get("answers") or {}
        if len(previous) != len(current):
            return dict(stored)
        remapped: dict[str, Any] = {}
        for old, new in zip(previous, current, strict=True):
            old_id = str(old.get("id") or "")
            new_id = str(new.get("id") or "")
            if not old_id or not new_id or old_id not in stored:
                return dict(stored)
            remapped[new_id] = stored[old_id]
        return remapped

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
