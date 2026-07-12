from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping, Protocol, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso
from zyra_runtime import LocalArtifactStore


@dataclass(frozen=True, slots=True)
class SideQuestionContextSnapshot:
    parent_session_id: str
    parent_session_revision: int
    context_epoch: int
    compact_boundary_id: str
    message_ids: tuple[str, ...]
    system_prompt_digest: str
    user_context_digest: str
    model: str
    thinking: str
    cache_prefix_digest: str
    snapshot_id: str = field(default_factory=lambda: new_id("sidectx"))
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_session_id": self.parent_session_id,
            "parent_session_revision": self.parent_session_revision,
            "context_epoch": self.context_epoch,
            "compact_boundary_id": self.compact_boundary_id,
            "message_ids": list(self.message_ids),
            "system_prompt_digest": self.system_prompt_digest,
            "user_context_digest": self.user_context_digest,
            "model": self.model,
            "thinking": self.thinking,
            "cache_prefix_digest": self.cache_prefix_digest,
            "snapshot_id": self.snapshot_id,
            "metadata": copy.deepcopy(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SideQuestionUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    wall_time_ms: int = 0
    model_calls: int = 1
    tool_calls: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "wall_time_ms": self.wall_time_ms,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
        }


@dataclass(frozen=True, slots=True)
class SideQuestionProviderResult:
    answer: str
    usage: SideQuestionUsage = field(default_factory=SideQuestionUsage)
    tool_requests: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


class SideQuestionProvider(Protocol):
    def ask(
        self,
        question: str,
        snapshot: SideQuestionContextSnapshot,
        *,
        tools: tuple[Any, ...],
        max_turns: int,
        cache_write: bool,
    ) -> SideQuestionProviderResult: ...


@dataclass(frozen=True, slots=True)
class SideQuestionResult:
    side_question_id: str
    run_id: str
    task_id: str
    question: str
    answer: str
    snapshot: SideQuestionContextSnapshot
    usage: SideQuestionUsage
    status: str
    tool_violation: bool
    cache_reuse: bool
    parent_messages_mutated: bool
    main_replan_triggered: bool
    transcript_ref: str
    event_ids: tuple[str, ...]
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "completed" and not self.tool_violation

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "side_question_id": self.side_question_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "question": self.question,
            "answer": self.answer,
            "snapshot": self.snapshot.to_dict(),
            "usage": self.usage.to_dict(),
            "status": self.status,
            "tool_violation": self.tool_violation,
            "cache_reuse": self.cache_reuse,
            "parent_messages_mutated": self.parent_messages_mutated,
            "main_replan_triggered": self.main_replan_triggered,
            "transcript_ref": self.transcript_ref,
            "event_ids": list(self.event_ids),
            "created_at": self.created_at,
            "metadata": copy.deepcopy(self.metadata),
        }


class LocalContextSideQuestionProvider:
    """Hermetic provider used when no model transport is configured.

    It answers from the immutable snapshot metadata and never pretends to be a
    model. Production deployments can inject the same model stream used by the
    parent session through the typed provider port.
    """

    def ask(self, question, snapshot, *, tools, max_turns, cache_write):
        context_summary = str(snapshot.metadata.get("context_summary") or "The current session context is available by reference.")
        answer = f"Side question: {question.strip()}\n\nContext note: {context_summary}"
        return SideQuestionProviderResult(
            answer=answer,
            usage=SideQuestionUsage(
                input_tokens=max(1, (len(question) + len(context_summary)) // 4),
                output_tokens=max(1, len(answer) // 4),
                model_calls=0,
                tool_calls=0,
            ),
            metadata={"provider": "local_context_projection", "model_inference": False},
        )


class SideQuestionRuntime:
    """One-turn, tool-free, independently persisted `/btw` runtime."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        provider: SideQuestionProvider | None = None,
        artifact_store: LocalArtifactStore | None = None,
        event_sink: Callable[[EventRecord], None] | None = None,
        disabled: bool = False,
    ) -> None:
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.provider = provider or LocalContextSideQuestionProvider()
        self.artifact_store = artifact_store
        self.event_sink = event_sink
        self.disabled = disabled
        self._lock = RLock()

    def ask(
        self,
        *,
        run_id: str,
        task_id: str,
        question: str,
        snapshot: SideQuestionContextSnapshot,
        parent_messages_before: Sequence[Mapping[str, Any]],
        parent_messages_after: Callable[[], Sequence[Mapping[str, Any]]],
    ) -> SideQuestionResult:
        if self.disabled:
            raise RuntimeError("SideQuestionRuntime is disabled")
        if not question.strip():
            raise ValueError("side question cannot be empty")
        side_question_id = new_id("sideq")
        before_digest = _digest_messages(parent_messages_before)
        start_event = EventRecord(
            run_id=run_id,
            task_id=task_id,
            event_type=EventType.SIDE_QUESTION,
            payload={
                "schema": "zyra.side-question/v1",
                "phase": "started",
                "side_question_id": side_question_id,
                "snapshot_id": snapshot.snapshot_id,
                "tools": [],
                "max_turns": 1,
                "cache_write": False,
            },
        )
        self._emit(start_event)
        provider_result = self.provider.ask(
            question,
            snapshot,
            tools=(),
            max_turns=1,
            cache_write=False,
        )
        tool_violation = bool(provider_result.tool_requests) or provider_result.usage.tool_calls > 0
        after_digest = _digest_messages(parent_messages_after())
        parent_mutated = before_digest != after_digest
        status = "failed" if tool_violation or parent_mutated else "completed"
        violation_event = None
        if tool_violation:
            violation_event = EventRecord(
                run_id=run_id,
                task_id=task_id,
                event_type=EventType.SIDE_QUESTION,
                payload={
                    "schema": "zyra.side-question/v1",
                    "phase": "tool_denied",
                    "side_question_id": side_question_id,
                    "requested_tools": [copy.deepcopy(item) for item in provider_result.tool_requests],
                    "error_code": "side_question_tool_violation",
                },
            )
            self._emit(violation_event)
        if parent_mutated:
            raise RuntimeError("side question provider mutated the parent message sequence")
        transcript_path = self.state_root / f"{side_question_id}.jsonl"
        entries = [
            {"type": "metadata", "side_question_id": side_question_id, "snapshot": snapshot.to_dict()},
            {"type": "user", "content": question},
            {"type": "assistant", "content": provider_result.answer, "tool_violation": tool_violation},
            {"type": "usage", "usage": provider_result.usage.to_dict()},
        ]
        with self._lock, transcript_path.open("x", encoding="utf-8", newline="\n") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True))
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        finish_event = EventRecord(
            run_id=run_id,
            task_id=task_id,
            event_type=EventType.SIDE_QUESTION,
            payload={
                "schema": "zyra.side-question/v1",
                "phase": "completed" if status == "completed" else "failed",
                "side_question_id": side_question_id,
                "snapshot_id": snapshot.snapshot_id,
                "usage": provider_result.usage.to_dict(),
                "tool_violation": tool_violation,
                "parent_messages_mutated": False,
                "main_replan_triggered": False,
                "transcript_ref": str(transcript_path),
            },
        )
        self._emit(finish_event)
        event_ids = tuple(item.event_id for item in (start_event, violation_event, finish_event) if item is not None)
        return SideQuestionResult(
            side_question_id=side_question_id,
            run_id=run_id,
            task_id=task_id,
            question=question,
            answer=provider_result.answer if not tool_violation else "",
            snapshot=snapshot,
            usage=provider_result.usage,
            status=status,
            tool_violation=tool_violation,
            cache_reuse=bool(snapshot.cache_prefix_digest),
            parent_messages_mutated=False,
            main_replan_triggered=False,
            transcript_ref=str(transcript_path),
            event_ids=event_ids,
            metadata={
                **copy.deepcopy(provider_result.metadata),
                "cache_reuse_reason": "exact_parent_prefix" if snapshot.cache_prefix_digest else "snapshot_rebuilt",
                "tools_disabled": True,
                "max_turns": 1,
                "cache_write": False,
            },
        )

    def read(self, side_question_id: str) -> tuple[dict[str, Any], ...]:
        path = self.state_root / f"{side_question_id}.jsonl"
        if not path.exists():
            raise KeyError(side_question_id)
        return tuple(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())

    def _emit(self, event: EventRecord) -> None:
        if self.event_sink:
            self.event_sink(event)


def _digest_messages(messages: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps([copy.deepcopy(dict(item)) for item in messages], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    import hashlib

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
