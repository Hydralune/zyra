from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .models import (
    ArtifactLineage,
    HistoryKind,
    HistoryRecord,
    ObservationScope,
    ToolPair,
    TraceSpan,
    TraceSpanKind,
    TraceStatus,
    digest_value,
    utc_now,
)


class TracePairingError(RuntimeError):
    code = "browser_trace_pairing_error"


@dataclass(frozen=True, slots=True)
class TracePolicy:
    max_attribute_chars: int = 16_000
    max_spans_per_scope: int = 50_000
    require_tool_pairs: bool = True
    retain_inputs: bool = False
    retain_outputs: bool = False

    def __post_init__(self) -> None:
        if self.max_attribute_chars < 256:
            raise ValueError("trace attribute budget is too small")
        if self.max_spans_per_scope < 1:
            raise ValueError("trace span limit must be positive")


class BrowserTraceRuntime:
    """Zyra-owned trace normalization inspired by OMP event/tool pairing."""

    def __init__(
        self,
        *,
        policy: TracePolicy | None = None,
    ) -> None:
        self.policy = policy or TracePolicy()

    def spans_from_records(
        self,
        scope: ObservationScope,
        records: Sequence[HistoryRecord],
    ) -> tuple[TraceSpan, ...]:
        self._assert_scope(scope, records)
        spans: list[TraceSpan] = []
        pending: dict[str, HistoryRecord] = {}
        for record in records:
            if record.kind == HistoryKind.TOOL_CALL and record.tool_call_id:
                if record.tool_call_id in pending:
                    raise TracePairingError(
                        f"duplicate pending tool call {record.tool_call_id!r}"
                    )
                pending[record.tool_call_id] = record
                continue
            if record.kind == HistoryKind.TOOL_RESULT and record.tool_call_id:
                call = pending.pop(record.tool_call_id, None)
                if call is None:
                    raise TracePairingError(
                        f"tool result {record.tool_call_id!r} has no call record"
                    )
                spans.append(self._tool_span(scope, call, record))
                continue
            span = self._record_span(scope, record)
            if span is not None:
                spans.append(span)
            if len(spans) > self.policy.max_spans_per_scope:
                raise TracePairingError("trace span limit exceeded")
        if pending and self.policy.require_tool_pairs:
            raise TracePairingError(
                "unpaired browser tool calls: " + ", ".join(sorted(pending))
            )
        spans.extend(
            self._pending_span(scope, item)
            for item in pending.values()
        )
        return tuple(spans)

    def tool_pairs(
        self,
        scope: ObservationScope,
        records: Sequence[HistoryRecord],
    ) -> tuple[ToolPair, ...]:
        self._assert_scope(scope, records)
        calls: dict[str, HistoryRecord] = {}
        pairs: list[ToolPair] = []
        for record in records:
            if not record.tool_call_id:
                continue
            if record.kind == HistoryKind.TOOL_CALL:
                if record.tool_call_id in calls:
                    raise TracePairingError(
                        f"duplicate tool call id {record.tool_call_id!r}"
                    )
                calls[record.tool_call_id] = record
            elif record.kind == HistoryKind.TOOL_RESULT:
                call = calls.pop(record.tool_call_id, None)
                if call is None:
                    raise TracePairingError(
                        f"orphan tool result {record.tool_call_id!r}"
                    )
                pairs.append(
                    ToolPair(
                        tool_call_id=record.tool_call_id,
                        call_record_id=call.record_id,
                        result_record_id=record.record_id,
                        tool_name=str(
                            call.payload.get("tool_name")
                            or call.payload.get("action")
                            or "browser_action"
                        ),
                        ok=bool(record.payload.get("ok")),
                        started_at=call.created_at,
                        finished_at=record.created_at,
                        event_ids=tuple(
                            dict.fromkeys(
                                [
                                    *call.causal_event_ids,
                                    *record.causal_event_ids,
                                ]
                            )
                        ),
                        artifact_ids=tuple(
                            dict.fromkeys(
                                [
                                    *call.artifact_ids,
                                    *record.artifact_ids,
                                ]
                            )
                        ),
                    )
                )
        if calls and self.policy.require_tool_pairs:
            raise TracePairingError(
                "unpaired tool calls: " + ", ".join(sorted(calls))
            )
        return tuple(pairs)

    def span_for_artifact(
        self,
        lineage: ArtifactLineage,
        *,
        parent_span_id: str = "",
    ) -> TraceSpan:
        return TraceSpan(
            scope=lineage.scope,
            kind=TraceSpanKind.ARTIFACT,
            name=f"artifact:{lineage.role}",
            status=TraceStatus.OK,
            trace_id=lineage.scope.key,
            parent_span_id=parent_span_id,
            started_at=lineage.created_at,
            finished_at=lineage.created_at,
            duration_ms=0,
            output_digest=lineage.sha256,
            event_ids=lineage.source_event_ids,
            artifact_ids=(lineage.artifact_id,),
            attributes={
                "receipt_id": lineage.receipt_id,
                "role": str(lineage.role),
                "size_bytes": lineage.size_bytes,
                "media_type": lineage.media_type,
                "quarantined": lineage.quarantined,
            },
        )

    def public_projection(
        self,
        spans: Sequence[TraceSpan],
        *,
        limit: int = 200,
    ) -> dict[str, Any]:
        limited = tuple(spans)[-max(0, limit):]
        return {
            "schema": "zyra.browser-observability.trace-projection.v1",
            "span_count": len(spans),
            "returned_span_count": len(limited),
            "errors": sum(1 for item in spans if item.status == TraceStatus.ERROR),
            "pending": sum(1 for item in spans if item.status == TraceStatus.PENDING),
            "tool_spans": sum(
                1
                for item in spans
                if item.tool_call_id
                and item.kind in {
                    TraceSpanKind.TOOL_CALL,
                    TraceSpanKind.BROWSER_ACTION,
                }
            ),
            "spans": [item.to_dict() for item in limited],
        }

    def _tool_span(
        self,
        scope: ObservationScope,
        call: HistoryRecord,
        result: HistoryRecord,
    ) -> TraceSpan:
        ok = bool(result.payload.get("ok"))
        duration = self._duration_ms(call.created_at, result.created_at)
        input_value = call.payload.get("input", call.payload)
        output_value = result.payload.get("output", result.payload)
        attributes = {
            "call_record_id": call.record_id,
            "result_record_id": result.record_id,
            "tool_name": str(
                call.payload.get("tool_name")
                or call.payload.get("action")
                or "browser_action"
            ),
            "retryable": bool(result.payload.get("retryable")),
            "outcome_unknown": bool(result.payload.get("outcome_unknown")),
        }
        if self.policy.retain_inputs:
            attributes["input"] = self._bounded(input_value)
        if self.policy.retain_outputs:
            attributes["output"] = self._bounded(output_value)
        return TraceSpan(
            scope=scope,
            kind=TraceSpanKind.BROWSER_ACTION,
            name=str(attributes["tool_name"]),
            status=TraceStatus.OK if ok else TraceStatus.ERROR,
            trace_id=scope.key,
            started_at=call.created_at,
            finished_at=result.created_at,
            duration_ms=duration,
            tool_call_id=call.tool_call_id,
            input_digest=digest_value(input_value),
            output_digest=digest_value(output_value),
            error_code=str(result.payload.get("error_code") or ""),
            event_ids=tuple(
                dict.fromkeys(
                    [
                        *call.causal_event_ids,
                        *result.causal_event_ids,
                    ]
                )
            ),
            artifact_ids=tuple(
                dict.fromkeys(
                    [
                        *call.artifact_ids,
                        *result.artifact_ids,
                    ]
                )
            ),
            attributes=attributes,
        )

    def _record_span(
        self,
        scope: ObservationScope,
        record: HistoryRecord,
    ) -> TraceSpan | None:
        kinds = {
            HistoryKind.WATCHDOG_SIGNAL: TraceSpanKind.WATCHDOG,
            HistoryKind.ARTIFACT_PUBLISHED: TraceSpanKind.ARTIFACT,
            HistoryKind.STATE_CAPTURE: TraceSpanKind.BROWSER_ACTION,
            HistoryKind.JUDGE_ADVISORY: TraceSpanKind.MODEL_RESPONSE,
            HistoryKind.RECOVERY_INPUT: TraceSpanKind.WATCHDOG,
        }
        kind = kinds.get(record.kind)
        if kind is None:
            return None
        status = (
            TraceStatus.ERROR
            if bool(record.payload.get("error"))
            or str(record.payload.get("status") or "") in {"error", "unhealthy", "terminated"}
            else TraceStatus.OK
        )
        return TraceSpan(
            scope=scope,
            kind=kind,
            name=str(record.payload.get("name") or record.kind),
            status=status,
            trace_id=scope.key,
            started_at=record.created_at,
            finished_at=record.created_at,
            duration_ms=0,
            input_digest=digest_value(record.payload),
            output_digest=record.content_digest,
            event_ids=record.causal_event_ids,
            artifact_ids=record.artifact_ids,
            attributes={
                "record_id": record.record_id,
                "sequence": record.sequence,
                "branch_id": record.branch_id,
            },
        )

    def _pending_span(
        self,
        scope: ObservationScope,
        record: HistoryRecord,
    ) -> TraceSpan:
        return TraceSpan(
            scope=scope,
            kind=TraceSpanKind.TOOL_CALL,
            name=str(
                record.payload.get("tool_name")
                or record.payload.get("action")
                or "browser_action"
            ),
            status=TraceStatus.PENDING,
            trace_id=scope.key,
            started_at=record.created_at,
            tool_call_id=record.tool_call_id,
            input_digest=digest_value(record.payload),
            event_ids=record.causal_event_ids,
            artifact_ids=record.artifact_ids,
            attributes={
                "record_id": record.record_id,
                "sequence": record.sequence,
            },
        )

    def _bounded(
        self,
        value: Any,
    ) -> str:
        text = str(value)
        if len(text) <= self.policy.max_attribute_chars:
            return text
        return text[: self.policy.max_attribute_chars] + "...[truncated]"

    @staticmethod
    def _duration_ms(
        started_at: str,
        finished_at: str,
    ) -> int:
        try:
            start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            finish = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        except ValueError:
            return 0
        return max(0, int((finish - start).total_seconds() * 1000))

    @staticmethod
    def _assert_scope(
        scope: ObservationScope,
        records: Sequence[HistoryRecord],
    ) -> None:
        if any(item.scope != scope for item in records):
            raise TracePairingError("trace records cross observation scopes")
