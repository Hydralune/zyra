"""OpenHands-derived event history filters over the canonical spine.

OpenHands contributes filtering, pagination and history-fold behavior.  It does
not own event persistence: every input is read from RuntimeEventSpineBridge and
the returned cursor remains a derived read-model cursor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from .integration import RuntimeEventSpineBridge
from .models import (
    JsonValue,
    RuntimeEventContractError,
    RuntimeEventEnvelope,
    RuntimeEventQuery,
    coerce_json,
)


class HistoryRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"
    WORKER = "worker"
    CONTROL = "control"
    UNKNOWN = "unknown"


class HistoryItemKind(str, Enum):
    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ARTIFACT = "artifact"
    CONTROL = "control"
    PERMISSION = "permission"
    WORKER = "worker"
    FAULT = "fault"
    RECOVERY = "recovery"
    CHECKPOINT = "checkpoint"
    STATE = "state"
    AUDIT = "audit"


@dataclass(frozen=True, slots=True)
class EventFilter:
    event_types: tuple[str, ...] = ()
    excluded_event_types: tuple[str, ...] = ()
    producers: tuple[str, ...] = ()
    subjects: tuple[str, ...] = ()
    intents: tuple[str, ...] = ()
    trust_levels: tuple[str, ...] = ()
    include_hidden: bool = False
    predicate: Callable[[RuntimeEventEnvelope], bool] | None = None

    def accepts(self, event: RuntimeEventEnvelope) -> bool:
        if self.event_types and not any(_matches_pattern(event.event_type, pattern) for pattern in self.event_types):
            return False
        if any(_matches_pattern(event.event_type, pattern) for pattern in self.excluded_event_types):
            return False
        if self.producers and event.producer not in self.producers:
            return False
        if self.subjects and event.subject not in self.subjects:
            return False
        if self.intents and event.intent not in self.intents:
            return False
        if self.trust_levels and event.trust not in self.trust_levels:
            return False
        hidden = event.payload.get("hidden") is True or event.payload.get("internalOnly") is True
        if hidden and not self.include_hidden:
            return False
        return self.predicate(event) if self.predicate is not None else True


@dataclass(frozen=True, slots=True)
class FoldCursor:
    global_sequence: int = 0
    event_count: int = 0
    high_watermark: int = 0
    exhausted: bool = False

    def advanced(
        self,
        *,
        sequence: int,
        accepted: int,
        high_watermark: int,
        exhausted: bool,
    ) -> "FoldCursor":
        if sequence < self.global_sequence:
            raise RuntimeEventContractError("fold cursor cannot move backward")
        return FoldCursor(
            global_sequence=sequence,
            event_count=self.event_count + accepted,
            high_watermark=max(self.high_watermark, high_watermark),
            exhausted=exhausted,
        )

    def to_jsonable(self) -> dict[str, JsonValue]:
        return {
            "globalSequence": self.global_sequence,
            "eventCount": self.event_count,
            "highWatermark": self.high_watermark,
            "exhausted": self.exhausted,
        }


@dataclass(frozen=True, slots=True)
class HistoryItem:
    item_id: str
    kind: HistoryItemKind
    role: HistoryRole
    event_type: str
    occurred_at: str
    sequence: int
    summary: str
    content: JsonValue
    correlation_id: str
    causation_id: str | None
    subject: str
    artifact_ids: tuple[str, ...] = ()
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, JsonValue]:
        return {
            "itemId": self.item_id,
            "kind": self.kind.value,
            "role": self.role.value,
            "eventType": self.event_type,
            "occurredAt": self.occurred_at,
            "sequence": self.sequence,
            "summary": self.summary,
            "content": self.content,
            "correlationId": self.correlation_id,
            "causationId": self.causation_id,
            "subject": self.subject,
            "artifactIds": list(self.artifact_ids),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolExchange:
    tool_call_id: str
    tool_name: str
    call_event_id: str
    result_event_id: str | None
    input_value: JsonValue
    output_value: JsonValue | None
    status: str
    started_sequence: int
    completed_sequence: int | None
    duration_ms: int | None
    artifact_ids: tuple[str, ...]

    def to_jsonable(self) -> dict[str, JsonValue]:
        return {
            "toolCallId": self.tool_call_id,
            "toolName": self.tool_name,
            "callEventId": self.call_event_id,
            "resultEventId": self.result_event_id,
            "input": self.input_value,
            "output": self.output_value,
            "status": self.status,
            "startedSequence": self.started_sequence,
            "completedSequence": self.completed_sequence,
            "durationMs": self.duration_ms,
            "artifactIds": list(self.artifact_ids),
        }


@dataclass(frozen=True, slots=True)
class ConversationHistory:
    session_id: str
    cursor: FoldCursor
    items: tuple[HistoryItem, ...]
    tool_exchanges: tuple[ToolExchange, ...]
    lifecycle: str
    last_error: Mapping[str, JsonValue] | None
    participant_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    event_type_counts: Mapping[str, int]
    causal_gaps: tuple[str, ...]
    open_tool_calls: tuple[str, ...]

    def to_jsonable(self) -> dict[str, JsonValue]:
        return {
            "sessionId": self.session_id,
            "cursor": self.cursor.to_jsonable(),
            "items": [item.to_jsonable() for item in self.items],
            "toolExchanges": [exchange.to_jsonable() for exchange in self.tool_exchanges],
            "lifecycle": self.lifecycle,
            "lastError": dict(self.last_error) if self.last_error else None,
            "participantIds": list(self.participant_ids),
            "artifactIds": list(self.artifact_ids),
            "eventTypeCounts": dict(self.event_type_counts),
            "causalGaps": list(self.causal_gaps),
            "openToolCalls": list(self.open_tool_calls),
        }


class EventHistoryFold:
    """Read and fold canonical events without taking write ownership."""

    def __init__(self, bridge: RuntimeEventSpineBridge, *, page_size: int = 250) -> None:
        if not 1 <= page_size <= 1000:
            raise RuntimeEventContractError("page_size must be between 1 and 1000")
        self.bridge = bridge
        self.page_size = page_size

    def iter_events(
        self,
        *,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        correlation_id: str | None = None,
        event_filter: EventFilter | None = None,
        after_sequence: int = 0,
        stop_sequence: int | None = None,
        max_events: int | None = None,
    ) -> Iterator[RuntimeEventEnvelope]:
        cursor = after_sequence
        yielded = 0
        effective_filter = event_filter or EventFilter()
        while True:
            remaining = self.page_size
            if max_events is not None:
                remaining = min(remaining, max_events - yielded)
                if remaining <= 0:
                    return
            page = self.bridge.query(
                RuntimeEventQuery(
                    after_sequence=cursor,
                    limit=remaining,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    correlation_id=correlation_id,
                )
            )
            if not page.events:
                return
            previous_cursor = cursor
            for event in page.events:
                cursor = max(cursor, event.global_sequence)
                if stop_sequence is not None and event.global_sequence > stop_sequence:
                    return
                if effective_filter.accepts(event):
                    yield event
                    yielded += 1
                    if max_events is not None and yielded >= max_events:
                        return
            if cursor <= previous_cursor:
                raise RuntimeEventContractError("runtime event pagination did not advance")
            if not page.has_more or cursor >= page.high_watermark:
                return

    def collect(
        self,
        *,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        correlation_id: str | None = None,
        event_filter: EventFilter | None = None,
        after_sequence: int = 0,
        max_events: int | None = None,
    ) -> tuple[RuntimeEventEnvelope, ...]:
        return tuple(
            self.iter_events(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                correlation_id=correlation_id,
                event_filter=event_filter,
                after_sequence=after_sequence,
                max_events=max_events,
            )
        )

    def fold_session(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        include_hidden: bool = False,
        max_events: int | None = None,
    ) -> ConversationHistory:
        events = self.collect(
            correlation_id=session_id,
            event_filter=EventFilter(include_hidden=include_hidden),
            after_sequence=after_sequence,
            max_events=max_events,
        )
        if not events:
            events = self.collect(
                aggregate_type="session",
                aggregate_id=session_id,
                event_filter=EventFilter(include_hidden=include_hidden),
                after_sequence=after_sequence,
                max_events=max_events,
            )
        return self.fold(session_id, events, base_cursor=after_sequence)

    def fold(
        self,
        session_id: str,
        events: Iterable[RuntimeEventEnvelope],
        *,
        base_cursor: int = 0,
    ) -> ConversationHistory:
        ordered = sorted(events, key=lambda event: event.global_sequence)
        known_ids: set[str] = set()
        causal_gaps: list[str] = []
        items: list[HistoryItem] = []
        tool_states: dict[str, dict[str, Any]] = {}
        participants: set[str] = set()
        artifacts: set[str] = set()
        type_counts: dict[str, int] = {}
        lifecycle = "unknown"
        last_error: Mapping[str, JsonValue] | None = None
        for event in ordered:
            if event.global_sequence <= base_cursor:
                continue
            known_ids.add(event.event_id)
            if event.causation_id and event.causation_id not in known_ids:
                causal_gaps.append(event.event_id)
            participants.add(event.subject)
            type_counts[event.event_type] = type_counts.get(event.event_type, 0) + 1
            for ref in event.artifact_refs:
                artifacts.add(ref.artifact_id)
            lifecycle = _advance_lifecycle(lifecycle, event)
            error = _extract_error(event)
            if error is not None:
                last_error = error
            item = _history_item(event)
            if item is not None:
                items.append(item)
            _fold_tool_state(tool_states, event)
        exchanges = tuple(
            _tool_exchange(tool_call_id, state)
            for tool_call_id, state in sorted(
                tool_states.items(),
                key=lambda item: int(item[1].get("startedSequence", 0)),
            )
        )
        high = max((event.global_sequence for event in ordered), default=base_cursor)
        open_calls = tuple(
            exchange.tool_call_id
            for exchange in exchanges
            if exchange.result_event_id is None
        )
        return ConversationHistory(
            session_id=session_id,
            cursor=FoldCursor(
                global_sequence=high,
                event_count=len(ordered),
                high_watermark=high,
                exhausted=True,
            ),
            items=tuple(items),
            tool_exchanges=exchanges,
            lifecycle=lifecycle,
            last_error=last_error,
            participant_ids=tuple(sorted(participants)),
            artifact_ids=tuple(sorted(artifacts)),
            event_type_counts=dict(sorted(type_counts.items())),
            causal_gaps=tuple(causal_gaps),
            open_tool_calls=open_calls,
        )

    def causal_chain(self, event_id: str, *, max_depth: int = 256) -> tuple[RuntimeEventEnvelope, ...]:
        if max_depth < 1:
            raise RuntimeEventContractError("max_depth must be positive")
        chain: list[RuntimeEventEnvelope] = []
        visited: set[str] = set()
        current_id: str | None = event_id
        while current_id and len(chain) < max_depth:
            if current_id in visited:
                raise RuntimeEventContractError(f"causation cycle detected at {current_id}")
            visited.add(current_id)
            event = self.bridge.get_event(current_id)
            if event is None:
                break
            chain.append(event)
            current_id = event.causation_id
        chain.reverse()
        return tuple(chain)

    def count_by_type(
        self,
        *,
        correlation_id: str | None = None,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
    ) -> Mapping[str, int]:
        counts: dict[str, int] = {}
        for event in self.iter_events(
            correlation_id=correlation_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
        ):
            counts[event.event_type] = counts.get(event.event_type, 0) + 1
        return dict(sorted(counts.items()))


def _matches_pattern(value: str, pattern: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return value == pattern


def _role_for_event(event: RuntimeEventEnvelope) -> HistoryRole:
    role = event.payload.get("role")
    if isinstance(role, str):
        try:
            return HistoryRole(role.lower())
        except ValueError:
            pass
    event_type = event.event_type
    if event_type.startswith("runtime.tool."):
        return HistoryRole.TOOL
    if event_type.startswith("runtime.control.") or event_type.startswith("runtime.permission."):
        return HistoryRole.CONTROL
    if event_type.startswith("runtime.worker.") or event_type.startswith("runtime.scheduler."):
        return HistoryRole.WORKER
    producer = event.producer.lower()
    if "user" in producer or event.subject.lower() == "user":
        return HistoryRole.USER
    if "system" in producer:
        return HistoryRole.SYSTEM
    if "assistant" in producer or "agent" in producer:
        return HistoryRole.ASSISTANT
    return HistoryRole.UNKNOWN


def _kind_for_event(event_type: str) -> HistoryItemKind:
    if event_type == "runtime.agent.message" or event_type.startswith("runtime.system.notice"):
        return HistoryItemKind.MESSAGE
    if event_type in {"runtime.tool.call.started", "runtime.tool.input.started", "runtime.tool.input.delta", "runtime.tool.input.ended"}:
        return HistoryItemKind.TOOL_CALL
    if event_type.startswith("runtime.tool.result"):
        return HistoryItemKind.TOOL_RESULT
    if event_type.startswith("runtime.artifact."):
        return HistoryItemKind.ARTIFACT
    if event_type.startswith("runtime.control."):
        return HistoryItemKind.CONTROL
    if event_type.startswith("runtime.permission."):
        return HistoryItemKind.PERMISSION
    if event_type.startswith("runtime.worker.") or event_type.startswith("runtime.scheduler."):
        return HistoryItemKind.WORKER
    if event_type.startswith("runtime.fault."):
        return HistoryItemKind.FAULT
    if event_type.startswith("runtime.recovery."):
        return HistoryItemKind.RECOVERY
    if event_type.startswith("runtime.checkpoint."):
        return HistoryItemKind.CHECKPOINT
    if event_type.startswith("runtime.audit."):
        return HistoryItemKind.AUDIT
    return HistoryItemKind.STATE


def _content_for_event(event: RuntimeEventEnvelope) -> JsonValue:
    for key in ("content", "message", "text", "output", "result", "input", "delta"):
        if key in event.payload:
            return coerce_json(event.payload[key])
    if event.artifact_refs:
        return {
            "summary": event.summary,
            "artifactRefs": [ref.to_jsonable() for ref in event.artifact_refs],
        }
    return dict(event.payload)


def _history_item(event: RuntimeEventEnvelope) -> HistoryItem | None:
    if event.event_type.endswith(".delta"):
        return None
    metadata: dict[str, JsonValue] = {
        "producer": event.producer,
        "intent": event.intent,
        "trust": event.trust,
        "aggregateType": event.aggregate_type,
        "aggregateId": event.aggregate_id,
        "aggregateSequence": event.aggregate_sequence,
    }
    return HistoryItem(
        item_id=event.event_id,
        kind=_kind_for_event(event.event_type),
        role=_role_for_event(event),
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        sequence=event.global_sequence,
        summary=event.summary,
        content=_content_for_event(event),
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        subject=event.subject,
        artifact_ids=tuple(ref.artifact_id for ref in event.artifact_refs),
        metadata=metadata,
    )


def _tool_call_id(event: RuntimeEventEnvelope) -> str | None:
    for key in ("toolCallId", "tool_call_id", "callId", "call_id"):
        value = event.payload.get(key)
        if isinstance(value, str) and value:
            return value
    if event.aggregate_type in {"tool_call", "tool"}:
        return event.aggregate_id
    return None


def _fold_tool_state(states: dict[str, dict[str, Any]], event: RuntimeEventEnvelope) -> None:
    if not event.event_type.startswith("runtime.tool."):
        return
    tool_call_id = _tool_call_id(event)
    if not tool_call_id:
        return
    state = states.setdefault(
        tool_call_id,
        {
            "toolName": "unknown",
            "callEventId": event.event_id,
            "input": None,
            "inputDeltas": [],
            "output": None,
            "status": "building",
            "startedSequence": event.global_sequence,
            "completedSequence": None,
            "durationMs": None,
            "artifactIds": set(),
        },
    )
    tool_name = event.payload.get("toolName", event.payload.get("tool_name"))
    if isinstance(tool_name, str) and tool_name:
        state["toolName"] = tool_name
    state["artifactIds"].update(ref.artifact_id for ref in event.artifact_refs)
    if event.event_type == "runtime.tool.input.started":
        state["status"] = "building"
        state["callEventId"] = event.event_id
    elif event.event_type == "runtime.tool.input.delta":
        delta = event.payload.get("delta")
        if delta is not None:
            state["inputDeltas"].append(coerce_json(delta))
    elif event.event_type in {"runtime.tool.input.ended", "runtime.tool.call.started"}:
        state["status"] = "running"
        if "input" in event.payload:
            state["input"] = coerce_json(event.payload["input"])
        elif state["inputDeltas"]:
            state["input"] = _join_deltas(state["inputDeltas"])
    elif event.event_type.startswith("runtime.tool.result"):
        state["resultEventId"] = event.event_id
        state["completedSequence"] = event.global_sequence
        state["output"] = _content_for_event(event)
        state["status"] = "failed" if event.event_type.endswith("failed") else "completed"
        duration = event.payload.get("durationMs", event.payload.get("duration_ms"))
        if isinstance(duration, int) and not isinstance(duration, bool):
            state["durationMs"] = duration


def _join_deltas(deltas: Sequence[JsonValue]) -> JsonValue:
    if all(isinstance(item, str) for item in deltas):
        return "".join(str(item) for item in deltas)
    return list(deltas)


def _tool_exchange(tool_call_id: str, state: Mapping[str, Any]) -> ToolExchange:
    return ToolExchange(
        tool_call_id=tool_call_id,
        tool_name=str(state.get("toolName", "unknown")),
        call_event_id=str(state.get("callEventId", "")),
        result_event_id=str(state["resultEventId"]) if state.get("resultEventId") else None,
        input_value=coerce_json(state.get("input")),
        output_value=coerce_json(state["output"]) if state.get("output") is not None else None,
        status=str(state.get("status", "unknown")),
        started_sequence=int(state.get("startedSequence", 0)),
        completed_sequence=int(state["completedSequence"]) if state.get("completedSequence") is not None else None,
        duration_ms=int(state["durationMs"]) if state.get("durationMs") is not None else None,
        artifact_ids=tuple(sorted(str(item) for item in state.get("artifactIds", set()))),
    )


def _advance_lifecycle(current: str, event: RuntimeEventEnvelope) -> str:
    transitions = {
        "runtime.session.created": "created",
        "runtime.session.started": "running",
        "runtime.session.completed": "completed",
        "runtime.session.failed": "failed",
        "runtime.session.cancelled": "cancelled",
        "runtime.recovery.started": "recovering",
        "runtime.recovery.completed": "running",
        "runtime.recovery.failed": "failed",
    }
    return transitions.get(event.event_type, current)


def _extract_error(event: RuntimeEventEnvelope) -> Mapping[str, JsonValue] | None:
    if not (event.event_type.endswith("failed") or event.event_type == "runtime.fault.detected"):
        return None
    raw = event.payload.get("error", event.payload.get("failure"))
    if isinstance(raw, Mapping):
        return {key: coerce_json(item) for key, item in raw.items()}
    return {
        "message": str(raw or event.summary),
        "eventId": event.event_id,
        "eventType": event.event_type,
    }

