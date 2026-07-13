from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from zyra_core import EventRecord, EventType, to_jsonable

from .history_store import BrowserHistoryStore, HistoryAppendReceipt
from .models import (
    ArtifactLineage,
    HistoryKind,
    HistoryRecord,
    ObservationScope,
    RecoveryInput,
    TraceSpan,
    WatchdogSignal,
)

if False:  # pragma: no cover - type-checking import without a runtime cycle.
    from .integration.contracts import RuntimeEvidenceEnvelope


@dataclass(frozen=True, slots=True)
class HistoryWriteResult:
    records: tuple[HistoryRecord, ...]
    receipts: tuple[HistoryAppendReceipt, ...]

    @property
    def head_digest(self) -> str:
        return self.receipts[-1].content_digest if self.receipts else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_count": len(self.records),
            "records": [item.to_dict() for item in self.records],
            "receipts": [item.to_dict() for item in self.receipts],
            "head_digest": self.head_digest,
        }


class BrowserHistoryRuntime:
    """Normalizes action/session facts into the 04D durable history owner."""

    def __init__(
        self,
        store: BrowserHistoryStore,
    ) -> None:
        self.store = store

    def session_started(
        self,
        scope: ObservationScope,
        session_start: Any,
        *,
        source_event: EventRecord,
    ) -> HistoryWriteResult:
        session = getattr(session_start, "session", None)
        payload = {
            "name": "browser_session_started",
            "browser_session_id": scope.browser_session_id,
            "canonical_session_id": scope.canonical_session_id,
            "status": str(getattr(session, "status", "running")),
            "revision": int(getattr(session, "revision", 0) or 0),
            "runtime_id": str(getattr(session, "runtime_id", "") or ""),
            "resource_id": str(getattr(session, "resource_id", "") or ""),
            "created": bool(getattr(session_start, "created", False)),
            "reused": bool(getattr(session_start, "reused", False)),
            "canonical_state_owner": "SQLite/TaskState",
            "browser_resource_owner": "M1-04A",
        }
        record = self.store.next_record(
            scope,
            HistoryKind.SESSION_STARTED,
            payload,
            causal_event_ids=(source_event.event_id,),
        )
        transaction = self.store.append_transaction(
            scope,
            (record,),
            transaction_id=(
                f"session-start-{scope.worker_request_id}-{source_event.event_id}"
            ),
        )
        return HistoryWriteResult(transaction.records, transaction.receipts)

    def action_receipts(
        self,
        scope: ObservationScope,
        receipts: Sequence[Any],
        *,
        events: Sequence[EventRecord],
    ) -> HistoryWriteResult:
        records: list[HistoryRecord] = []
        append_receipts: list[HistoryAppendReceipt] = []
        event_ids = tuple(item.event_id for item in events)
        events_by_tool = self._events_by_tool(events)
        for index, receipt in enumerate(receipts, start=1):
            value = self._mapping(receipt)
            tool_call_id = str(
                value.get("tool_call_id")
                or value.get("action_id")
                or value.get("receipt_id")
                or f"{scope.worker_request_id}:{index}"
            )
            receipt_id = str(value.get("receipt_id") or "")
            action_name = str(
                value.get("action")
                or value.get("tool_name")
                or value.get("name")
                or "browser_action"
            )
            source_ids = events_by_tool.get(tool_call_id, event_ids)
            artifact_ids = self._artifact_ids(value)
            call = self.store.next_record(
                scope,
                HistoryKind.TOOL_CALL,
                {
                    "name": action_name,
                    "tool_name": action_name,
                    "input": self._safe_input(value),
                    "request_digest": str(value.get("request_digest") or ""),
                    "permission_decision_id": str(
                        value.get("permission_decision_id")
                        or value.get("decision_id")
                        or ""
                    ),
                    "permission_request_id": str(
                        value.get("permission_request_id")
                        or ""
                    ),
                    "preflight_receipt_id": str(
                        value.get("preflight_receipt_id")
                        or ""
                    ),
                    "receipt_id": receipt_id,
                    "owner": "M1-04C",
                },
                causal_event_ids=source_ids,
                artifact_ids=artifact_ids,
                tool_call_id=tool_call_id,
            )
            ok = bool(value.get("ok"))
            result = self.store.next_record(
                scope,
                HistoryKind.TOOL_RESULT,
                {
                    "name": action_name,
                    "tool_name": action_name,
                    "ok": ok,
                    "output": self._safe_output(value),
                    "error_code": str(value.get("error_code") or ""),
                    "error_message": str(
                        value.get("error_message")
                        or value.get("error")
                        or ""
                    ),
                    "retryable": bool(value.get("retryable")),
                    "outcome_unknown": bool(value.get("outcome_unknown")),
                    "side_effect_count": int(
                        value.get("side_effect_count")
                        or value.get("effect_count")
                        or 0
                    ),
                    "receipt_id": receipt_id,
                    "call_record_id": call.record_id,
                    "owner": "M1-04C",
                },
                causal_event_ids=source_ids,
                artifact_ids=artifact_ids,
                tool_call_id=tool_call_id,
                parent_record_id=call.record_id,
            )
            transaction = self.store.append_transaction(
                scope,
                (call, result),
                transaction_id=(
                    f"action-pair-{receipt_id}"
                    if receipt_id
                    else f"action-pair-{tool_call_id}-{index}"
                ),
            )
            records.extend(transaction.records)
            append_receipts.extend(transaction.receipts)
        return HistoryWriteResult(
            records=tuple(records),
            receipts=tuple(append_receipts),
        )

    def state_capture(
        self,
        scope: ObservationScope,
        message_turn: Any,
        *,
        events: Sequence[EventRecord],
    ) -> HistoryWriteResult:
        if message_turn is None:
            return HistoryWriteResult((), ())
        artifacts = tuple(
            getattr(item, "artifact_id", "")
            for item in getattr(message_turn, "artifacts", ())
            if getattr(item, "artifact_id", "")
        )
        next_context = getattr(message_turn, "next_context", None)
        disclosure = getattr(message_turn, "disclosure", None)
        payload = {
            "name": "browser_state_capture",
            "capture_id": str(getattr(message_turn, "capture_id", "") or ""),
            "selector_revision_id": str(
                getattr(message_turn, "selector_revision_id", "")
                or ""
            ),
            "context_receipt_id": str(
                getattr(next_context, "receipt_id", "")
                or ""
            ),
            "context_tokens": int(getattr(disclosure, "tokens", 0) or 0),
            "context_bytes": int(getattr(disclosure, "bytes", 0) or 0),
            "status": str(getattr(message_turn, "status", "") or ""),
            "ok": bool(getattr(message_turn, "ok", False)),
            "dom_owner": "M1-04B",
            "context_owner": "M1-02D",
        }
        record = self.store.next_record(
            scope,
            HistoryKind.STATE_CAPTURE,
            payload,
            causal_event_ids=tuple(item.event_id for item in events),
            artifact_ids=artifacts,
        )
        return self._write(scope, (record,))

    def artifact(
        self,
        lineage: ArtifactLineage,
    ) -> HistoryWriteResult:
        scope = lineage.scope
        record = self.store.next_record(
            scope,
            HistoryKind.ARTIFACT_PUBLISHED,
            {
                "name": f"artifact:{lineage.role}",
                "lineage": lineage.to_dict(),
            },
            causal_event_ids=lineage.source_event_ids,
            artifact_ids=(lineage.artifact_id,),
        )
        return self._write(scope, (record,))

    def signals(
        self,
        scope: ObservationScope,
        signals: Sequence[WatchdogSignal],
    ) -> HistoryWriteResult:
        records: list[HistoryRecord] = []
        receipts: list[HistoryAppendReceipt] = []
        for signal in signals:
            if signal.scope != scope:
                raise ValueError("watchdog signal crosses history scope")
            record = self.store.next_record(
                scope,
                HistoryKind.WATCHDOG_SIGNAL,
                {
                    "name": f"watchdog:{signal.watchdog}",
                    "signal": signal.to_dict(),
                    "status": str(signal.status),
                    "error": signal.terminal,
                },
                causal_event_ids=signal.evidence_event_ids,
                artifact_ids=signal.artifact_ids,
            )
            receipt = self.store.append(record)
            records.append(record)
            receipts.append(receipt)
        return HistoryWriteResult(tuple(records), tuple(receipts))

    def recovery_inputs(
        self,
        scope: ObservationScope,
        values: Sequence[RecoveryInput],
    ) -> HistoryWriteResult:
        records: list[HistoryRecord] = []
        receipts: list[HistoryAppendReceipt] = []
        for item in values:
            if item.scope != scope:
                raise ValueError("recovery input crosses history scope")
            record = self.store.next_record(
                scope,
                HistoryKind.RECOVERY_INPUT,
                {
                    "name": f"recovery_input:{item.reason}",
                    "recovery_input": item.to_dict(),
                    "status": "handoff",
                    "planner_owner": "M1-07C",
                },
                causal_event_ids=item.evidence_event_ids,
                artifact_ids=item.artifact_ids,
            )
            receipt = self.store.append(record)
            records.append(record)
            receipts.append(receipt)
        return HistoryWriteResult(tuple(records), tuple(receipts))

    def trace_spans(
        self,
        scope: ObservationScope,
        spans: Sequence[TraceSpan],
    ) -> HistoryWriteResult:
        records: list[HistoryRecord] = []
        receipts: list[HistoryAppendReceipt] = []
        for span in spans:
            if span.scope != scope:
                raise ValueError("trace span crosses history scope")
            record = self.store.next_record(
                scope,
                HistoryKind.TRACE_SPAN,
                {
                    "name": span.name,
                    "span": span.to_dict(),
                    "status": str(span.status),
                },
                causal_event_ids=span.event_ids,
                artifact_ids=span.artifact_ids,
                tool_call_id=span.tool_call_id,
            )
            receipt = self.store.append(record)
            records.append(record)
            receipts.append(receipt)
        return HistoryWriteResult(tuple(records), tuple(receipts))

    def runtime_evidence(
        self,
        scope: ObservationScope,
        values: Sequence["RuntimeEvidenceEnvelope"],
    ) -> HistoryWriteResult:
        records: list[HistoryRecord] = []
        receipts: list[HistoryAppendReceipt] = []
        for item in values:
            if item.scope != scope:
                raise ValueError("runtime evidence crosses history scope")
            record = self.store.next_record(
                scope,
                HistoryKind.OBSERVATION,
                {
                    "name": f"runtime_evidence:{item.source}",
                    "runtime_evidence": item.to_dict(),
                    "status": str(item.terminal_state),
                    "supplementary": item.source
                    in {
                        "provider_stream",
                        "mcp",
                        "subagent",
                        "background_task",
                        "hashline",
                        "worktree",
                    },
                },
                causal_event_ids=tuple(
                    dict.fromkeys(
                        [
                            item.source_event_id,
                            item.causation_event_id,
                            *item.correlation_event_ids,
                        ]
                    )
                ),
                artifact_ids=item.artifact_ids,
                tool_call_id=item.tool_call_id or item.parent_tool_call_id,
            )
            receipt = self.store.append(record)
            records.append(record)
            receipts.append(receipt)
        return HistoryWriteResult(tuple(records), tuple(receipts))

    def session_stopped(
        self,
        scope: ObservationScope,
        *,
        stop_event: EventRecord | None,
        stop_error: str,
        intentional: bool,
    ) -> HistoryWriteResult:
        record = self.store.next_record(
            scope,
            HistoryKind.SESSION_STOPPED,
            {
                "name": "browser_session_stopped",
                "intentional": intentional,
                "ok": not bool(stop_error),
                "error": stop_error,
                "resource_owner": "M1-04A",
            },
            causal_event_ids=(
                (stop_event.event_id,)
                if stop_event is not None
                else ()
            ),
        )
        return self._write(scope, (record,))

    def projection(
        self,
        scope: ObservationScope,
        *,
        limit: int = 100,
    ) -> dict[str, Any]:
        head = self.store.head(scope)
        records = self.store.tail(scope, limit=limit)
        return {
            "schema": "zyra.browser-observability.history-projection.v1",
            "scope": scope.to_dict(),
            "head": head.to_dict() if head else None,
            "record_count": head.sequence if head else 0,
            "returned_count": len(records),
            "records": [item.to_dict() for item in records],
            "owner": "M1-S04D-01",
            "canonical_task_owner": "SQLite/TaskState",
        }

    def _write(
        self,
        scope: ObservationScope,
        records: Sequence[HistoryRecord],
    ) -> HistoryWriteResult:
        receipts = self.store.append_many(scope, records)
        return HistoryWriteResult(tuple(records), receipts)

    def _receipt_for(
        self,
        scope: ObservationScope,
        record: HistoryRecord,
    ) -> HistoryAppendReceipt:
        location = self.store._location_for_record(scope, record.record_id)
        return HistoryAppendReceipt(
            scope_key=scope.key,
            record_id=record.record_id,
            sequence=record.sequence,
            content_digest=record.content_digest,
            segment=int(location.get("segment") or 0),
            offset=int(location.get("offset") or 0),
            length=int(location.get("length") or 0),
            idempotent=True,
        )

    @staticmethod
    def _mapping(
        value: Any,
    ) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        projected = to_jsonable(value)
        return dict(projected) if isinstance(projected, Mapping) else {}

    @staticmethod
    def _safe_input(
        value: Mapping[str, Any],
    ) -> Any:
        for key in ("input", "arguments", "parameters", "request"):
            if key in value:
                return value[key]
        return {
            key: item
            for key, item in value.items()
            if key
            in {
                "action",
                "tool_name",
                "request_digest",
                "target_id",
                "selector_revision_id",
            }
        }

    @staticmethod
    def _safe_output(
        value: Mapping[str, Any],
    ) -> Any:
        for key in ("output", "result", "observation"):
            if key in value:
                return value[key]
        return {
            key: item
            for key, item in value.items()
            if key
            in {
                "ok",
                "error_code",
                "error_message",
                "side_effect_count",
                "retryable",
                "outcome_unknown",
            }
        }

    @staticmethod
    def _artifact_ids(
        value: Mapping[str, Any],
    ) -> tuple[str, ...]:
        raw = value.get("artifact_ids") or value.get("artifacts") or ()
        if isinstance(raw, str):
            return (raw,) if raw else ()
        if isinstance(raw, Sequence):
            output: list[str] = []
            for item in raw:
                if isinstance(item, Mapping):
                    artifact_id = str(item.get("artifact_id") or "")
                else:
                    artifact_id = str(getattr(item, "artifact_id", item) or "")
                if artifact_id:
                    output.append(artifact_id)
            return tuple(dict.fromkeys(output))
        return ()

    @staticmethod
    def _events_by_tool(
        events: Sequence[EventRecord],
    ) -> dict[str, tuple[str, ...]]:
        output: dict[str, list[str]] = {}
        for event in events:
            values = [event.payload]
            while values:
                value = values.pop()
                if not isinstance(value, Mapping):
                    continue
                tool_call_id = str(
                    value.get("tool_call_id")
                    or value.get("action_id")
                    or ""
                )
                if tool_call_id:
                    output.setdefault(tool_call_id, []).append(event.event_id)
                values.extend(
                    item
                    for item in value.values()
                    if isinstance(item, Mapping)
                )
        return {
            key: tuple(dict.fromkeys(value))
            for key, value in output.items()
        }
