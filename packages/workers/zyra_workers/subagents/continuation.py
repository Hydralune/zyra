from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from zyra_core import now_iso

from .context import SubagentContextFactory
from .digests import digest_object
from .errors import ContinuationRejected, SubagentDisabled
from .models import (
    AgentContextMode,
    StructuredSubagentMessage,
    SubagentTaskRecord,
    SubagentTaskStatus,
    TranscriptEntryKind,
)
from .task_store import SubagentTaskStore
from .transcript import SubagentTranscriptStore


@dataclass(frozen=True, slots=True)
class ContinuationRequest:
    task_id: str
    sender_task_id: str
    message: str
    intent: str = "continue"
    idempotency_key: str = ""
    maximum_chars: int = 8_000
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.message) > self.maximum_chars:
            raise ContinuationRejected(
                "continuation message exceeds the low-entropy budget",
                task_id=self.task_id,
                maximum_chars=self.maximum_chars,
                actual_chars=len(self.message),
            )
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", digest_object({
                "task_id": self.task_id,
                "sender_task_id": self.sender_task_id,
                "message": self.message,
                "intent": self.intent,
            }))


@dataclass(frozen=True, slots=True)
class ContinuationReceipt:
    task_id: str
    message_id: str
    action: str
    queued: bool
    resume_required: bool
    task_revision: int
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "message_id": self.message_id,
            "action": self.action,
            "queued": self.queued,
            "resume_required": self.resume_required,
            "task_revision": self.task_revision,
            "created_at": self.created_at,
        }


class SubagentContinuationRuntime:
    """Queue running continuations or request exact sidechain resume."""

    def __init__(
        self,
        *,
        task_store: SubagentTaskStore,
        transcript_store: SubagentTranscriptStore,
        context_factory: SubagentContextFactory,
        resume_callback: Callable[[SubagentTaskRecord, ContinuationRequest, tuple[dict[str, Any], ...]], Any] | None = None,
        disabled: bool = False,
    ) -> None:
        self.task_store = task_store
        self.transcript_store = transcript_store
        self.context_factory = context_factory
        self.resume_callback = resume_callback
        self.disabled = disabled
        self._seen: dict[str, ContinuationReceipt] = {}

    def send(self, request: ContinuationRequest) -> ContinuationReceipt:
        if self.disabled:
            raise SubagentDisabled("SubagentContinuationRuntime")
        existing = self._seen.get(request.idempotency_key)
        if existing:
            return existing
        record = self.task_store.get(request.task_id)
        structured = StructuredSubagentMessage(
            sender_task_id=request.sender_task_id,
            target_task_id=request.task_id,
            intent=request.intent,
            summary=request.message,
            metadata={
                **copy.deepcopy(request.metadata),
                "idempotency_key": request.idempotency_key,
                "raw_free_chat": False,
            },
        )
        if record.status in {
            SubagentTaskStatus.DISPATCHED,
            SubagentTaskStatus.RUNNING,
            SubagentTaskStatus.WAITING,
        }:
            updated = self.task_store.append_message(request.task_id, structured)
            self.transcript_store.append(
                request.task_id,
                TranscriptEntryKind.CONTINUATION,
                {"message": structured.to_dict(), "delivery": "pending_safe_boundary"},
            )
            receipt = ContinuationReceipt(
                task_id=request.task_id,
                message_id=structured.message_id,
                action="queued_running_task",
                queued=True,
                resume_required=False,
                task_revision=updated.revision,
            )
        elif record.status in {
            SubagentTaskStatus.COMPLETED,
            SubagentTaskStatus.FAILED,
            SubagentTaskStatus.KILLED,
        }:
            replay = self.transcript_store.sanitize_for_resume(request.task_id)
            if self.resume_callback is None:
                raise ContinuationRejected(
                    "terminal subagent requires a configured exact resume callback",
                    task_id=request.task_id,
                    status=record.status.value,
                )
            self.transcript_store.append(
                request.task_id,
                TranscriptEntryKind.CONTINUATION,
                {"message": structured.to_dict(), "delivery": "resume_sidechain"},
            )
            self.resume_callback(record, request, replay)
            updated = self.task_store.get(request.task_id)
            receipt = ContinuationReceipt(
                task_id=request.task_id,
                message_id=structured.message_id,
                action="resumed_terminal_task",
                queued=False,
                resume_required=True,
                task_revision=updated.revision,
            )
        else:
            raise ContinuationRejected(
                "subagent task cannot accept a continuation in its current state",
                task_id=request.task_id,
                status=record.status.value,
            )
        self._seen[request.idempotency_key] = receipt
        return receipt
