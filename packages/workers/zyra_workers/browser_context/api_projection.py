from __future__ import annotations

"""M2-facing read models for browser context, history, and causal state."""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zyra_core import EventRecord, EventType, now_iso, to_jsonable

from ..browser_state.contracts import digest_json, state_id
from .task_integration import (
    BROWSER_CONTEXT_CHECKPOINT_SCHEMA,
    BrowserContextDeliveryState,
    BrowserContextTaskCheckpoint,
)


class BrowserProjectionFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class BrowserProjectionFinding:
    code: str
    severity: BrowserProjectionFindingSeverity
    message: str
    blocks_projection: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "message": self.message,
            "blocks_projection": self.blocks_projection,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class BrowserContextQueueProjection:
    source_id: str
    disclosure_id: str
    state: BrowserContextDeliveryState
    capture_id: str
    selector_revision_id: str
    producer_worker_request_id: str
    consumer_worker_request_id: str
    claim_id: str
    artifact_ids: tuple[str, ...]
    action_receipt_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    bytes: int
    read_once: bool
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "disclosure_id": self.disclosure_id,
            "state": str(self.state),
            "capture_id": self.capture_id,
            "selector_revision_id": self.selector_revision_id,
            "producer_worker_request_id": self.producer_worker_request_id,
            "consumer_worker_request_id": self.consumer_worker_request_id,
            "claim_id": self.claim_id,
            "artifact_ids": list(self.artifact_ids),
            "action_receipt_ids": list(self.action_receipt_ids),
            "event_ids": list(self.event_ids),
            "bytes": self.bytes,
            "read_once": self.read_once,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class BrowserHistoryToolPairProjection:
    projection_id: str
    request_id: str
    receipt_id: str
    call_message_id: str
    result_message_id: str
    call_index: int
    result_index: int
    adjacent: bool
    result_ok: bool
    error_code: str
    artifact_ids: tuple[str, ...]
    selector_refs: tuple[str, ...]

    @property
    def atomic(self) -> bool:
        return bool(self.request_id and self.receipt_id and self.adjacent)

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "request_id": self.request_id,
            "receipt_id": self.receipt_id,
            "call_message_id": self.call_message_id,
            "result_message_id": self.result_message_id,
            "call_index": self.call_index,
            "result_index": self.result_index,
            "adjacent": self.adjacent,
            "atomic": self.atomic,
            "result_ok": self.result_ok,
            "error_code": self.error_code,
            "artifact_ids": list(self.artifact_ids),
            "selector_refs": list(self.selector_refs),
        }


@dataclass(frozen=True, slots=True)
class BrowserMemoryCandidateProjection:
    candidate_id: str
    kind: str
    summary: str
    confidence: float
    source_event_id: str
    source_action_receipt_id: str
    source_dom_capture_id: str
    artifact_ids: tuple[str, ...]
    retention_hint: str
    candidate_only: bool
    committed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "summary": self.summary,
            "confidence": self.confidence,
            "source_event_id": self.source_event_id,
            "source_action_receipt_id": self.source_action_receipt_id,
            "source_dom_capture_id": self.source_dom_capture_id,
            "artifact_ids": list(self.artifact_ids),
            "retention_hint": self.retention_hint,
            "candidate_only": self.candidate_only,
            "committed": self.committed,
        }


@dataclass(frozen=True, slots=True)
class BrowserCausalTimelineEntry:
    event_id: str
    event_type: str
    phase: str
    cause_event_ids: tuple[str, ...]
    capture_id: str = ""
    disclosure_id: str = ""
    artifact_id: str = ""
    claim_id: str = ""
    source_ids: tuple[str, ...] = ()
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "phase": self.phase,
            "cause_event_ids": list(self.cause_event_ids),
            "capture_id": self.capture_id,
            "disclosure_id": self.disclosure_id,
            "artifact_id": self.artifact_id,
            "claim_id": self.claim_id,
            "source_ids": list(self.source_ids),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class BrowserContextApiProjection:
    projection_id: str
    task_id: str
    run_id: str
    session_id: str
    checkpoint_revision: int
    checkpoint_digest: str
    queue: tuple[BrowserContextQueueProjection, ...]
    tool_pairs: tuple[BrowserHistoryToolPairProjection, ...]
    memory_candidates: tuple[BrowserMemoryCandidateProjection, ...]
    timeline: tuple[BrowserCausalTimelineEntry, ...]
    state_counts: Mapping[str, int]
    artifact_ids: tuple[str, ...]
    findings: tuple[BrowserProjectionFinding, ...]
    last_turn_id: str
    last_capture_id: str
    last_selector_revision_id: str
    created_at: str = field(default_factory=now_iso)

    @property
    def blocking_findings(self) -> tuple[BrowserProjectionFinding, ...]:
        return tuple(item for item in self.findings if item.blocks_projection)

    @property
    def ok(self) -> bool:
        return not self.blocking_findings

    @property
    def pending_count(self) -> int:
        return self.state_counts.get(str(BrowserContextDeliveryState.PENDING), 0) + self.state_counts.get(
            str(BrowserContextDeliveryState.RELEASED), 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-context-api-projection.v1",
            "projection_id": self.projection_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "checkpoint_revision": self.checkpoint_revision,
            "checkpoint_digest": self.checkpoint_digest,
            "queue": [item.to_dict() for item in self.queue],
            "tool_pairs": [item.to_dict() for item in self.tool_pairs],
            "memory_candidates": [item.to_dict() for item in self.memory_candidates],
            "timeline": [item.to_dict() for item in self.timeline],
            "state_counts": dict(self.state_counts),
            "pending_count": self.pending_count,
            "artifact_ids": list(self.artifact_ids),
            "findings": [item.to_dict() for item in self.findings],
            "blocking_findings": len(self.blocking_findings),
            "ok": self.ok,
            "last_turn_id": self.last_turn_id,
            "last_capture_id": self.last_capture_id,
            "last_selector_revision_id": self.last_selector_revision_id,
            "owners": {
                "checkpoint": "TaskState.metadata/SQLiteStore",
                "context": "ClaudeContextWindowManager/M1-02D",
                "history": "event log + M1-02D context",
                "selectors": "BrowserSelectorMapStore/M1-04B",
                "memory_commit": "MemoryFabric/M1-06B-M1-06C",
                "browser_session": "BrowserRuntime/M1-04A",
            },
            "created_at": self.created_at,
        }


class BrowserContextApiProjectionRuntime:
    """Fold checkpoint and event log into one redacted API/UI read model."""

    def __init__(self, *, disabled: bool = False) -> None:
        self.disabled = disabled
        self._projections = 0
        self._failures = 0

    def build(
        self,
        checkpoint: BrowserContextTaskCheckpoint,
        *,
        events: Sequence[EventRecord | Mapping[str, Any]] = (),
        task_artifact_ids: Sequence[str] = (),
    ) -> BrowserContextApiProjection:
        if self.disabled:
            raise RuntimeError("browser context API projection runtime is disabled")
        findings: list[BrowserProjectionFinding] = []
        queue = tuple(self._queue_item(item) for item in checkpoint.queue)
        state_counts = Counter(str(item.state) for item in checkpoint.queue)
        tool_pairs, pair_findings = self._tool_pairs(checkpoint.history_messages)
        findings.extend(pair_findings)
        memory = tuple(self._memory_candidate(item) for item in checkpoint.memory_candidates)
        timeline = self._timeline(events)
        # The public timeline intentionally shows only causal browser phases,
        # while a queue item also records context-selection, memory-candidate,
        # and fidelity events.  Validate reachability against the complete event
        # input rather than incorrectly treating the redacted view as the log.
        known_event_ids = {
            str(raw.get("event_id") or "")
            for event in events
            for raw in (to_jsonable(event),)
            if isinstance(raw, Mapping) and str(raw.get("event_id") or "")
        }
        known_artifacts = set(task_artifact_ids)
        queue_artifacts = {artifact for item in queue for artifact in item.artifact_ids}
        memory_artifacts = {artifact for item in memory for artifact in item.artifact_ids}
        if known_artifacts:
            missing = sorted((queue_artifacts | memory_artifacts) - known_artifacts)
            if missing:
                findings.append(BrowserProjectionFinding(
                    "checkpoint_artifact_missing_from_task",
                    BrowserProjectionFindingSeverity.ERROR,
                    "browser checkpoint references artifacts outside the task checkpoint",
                    True,
                    {"artifact_ids": missing},
                ))
        for item in queue:
            missing_events = tuple(event_id for event_id in item.event_ids if known_event_ids and event_id not in known_event_ids)
            if missing_events:
                findings.append(BrowserProjectionFinding(
                    "queue_event_cause_missing",
                    BrowserProjectionFindingSeverity.ERROR,
                    "browser disclosure queue references missing causal events",
                    True,
                    {"source_id": item.source_id, "event_ids": list(missing_events)},
                ))
            if item.state == BrowserContextDeliveryState.CONSUMED and not item.consumer_worker_request_id:
                findings.append(BrowserProjectionFinding(
                    "consumed_without_consumer",
                    BrowserProjectionFindingSeverity.ERROR,
                    "consumed browser disclosure has no consumer worker request",
                    True,
                    {"source_id": item.source_id},
                ))
            if item.state == BrowserContextDeliveryState.CLAIMED and not item.claim_id:
                findings.append(BrowserProjectionFinding(
                    "claimed_without_claim_id",
                    BrowserProjectionFindingSeverity.ERROR,
                    "claimed browser disclosure has no claim id",
                    True,
                    {"source_id": item.source_id},
                ))
        duplicate_disclosures = [
            disclosure_id for disclosure_id, count in Counter(item.disclosure_id for item in queue).items() if count > 1
        ]
        if duplicate_disclosures:
            findings.append(BrowserProjectionFinding(
                "duplicate_disclosure_identity",
                BrowserProjectionFindingSeverity.ERROR,
                "checkpoint contains duplicate browser disclosure identities",
                True,
                {"disclosure_ids": duplicate_disclosures},
            ))
        projection = BrowserContextApiProjection(
            projection_id=state_id("brctxprojection"),
            task_id=checkpoint.scope.task_id,
            run_id=checkpoint.scope.run_id,
            session_id=checkpoint.scope.session_id,
            checkpoint_revision=checkpoint.revision,
            checkpoint_digest=checkpoint.digest,
            queue=queue,
            tool_pairs=tool_pairs,
            memory_candidates=memory,
            timeline=timeline,
            state_counts=dict(state_counts),
            artifact_ids=tuple(sorted(queue_artifacts | memory_artifacts)),
            findings=tuple(findings),
            last_turn_id=checkpoint.last_turn_id,
            last_capture_id=checkpoint.last_capture_id,
            last_selector_revision_id=checkpoint.last_selector_revision_id,
        )
        self._projections += 1
        if not projection.ok:
            self._failures += 1
        return projection

    def event_for_projection(
        self,
        projection: BrowserContextApiProjection,
        *,
        node_id: str,
    ) -> EventRecord:
        return EventRecord(
            run_id=projection.run_id,
            task_id=projection.task_id,
            node_id=node_id or None,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "browser_context_api_projection": {
                    "projection_id": projection.projection_id,
                    "checkpoint_revision": projection.checkpoint_revision,
                    "checkpoint_digest": projection.checkpoint_digest,
                    "pending_count": projection.pending_count,
                    "queue_count": len(projection.queue),
                    "tool_pair_count": len(projection.tool_pairs),
                    "memory_candidate_count": len(projection.memory_candidates),
                    "timeline_count": len(projection.timeline),
                    "ok": projection.ok,
                },
                "cause_event_ids": [item.event_id for item in projection.timeline[-32:]],
            },
        )

    def _queue_item(self, item: Any) -> BrowserContextQueueProjection:
        block = item.context_block
        text = str(block.get("text") or "") if isinstance(block, Mapping) else ""
        consumer = item.consumed_by_worker_request_id or item.claimed_by_worker_request_id
        return BrowserContextQueueProjection(
            source_id=item.source_id,
            disclosure_id=item.disclosure_id,
            state=item.state,
            capture_id=item.capture_id,
            selector_revision_id=item.selector_revision_id,
            producer_worker_request_id=item.producer_worker_request_id,
            consumer_worker_request_id=consumer,
            claim_id=item.claim_id,
            artifact_ids=item.artifact_ids,
            action_receipt_ids=item.action_receipt_ids,
            event_ids=tuple((*item.event_ids, *item.consumed_event_ids)),
            bytes=len(text.encode("utf-8")),
            read_once=item.read_once,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    def _tool_pairs(
        self,
        messages: Sequence[Mapping[str, Any]],
    ) -> tuple[tuple[BrowserHistoryToolPairProjection, ...], tuple[BrowserProjectionFinding, ...]]:
        calls: dict[str, tuple[int, Mapping[str, Any]]] = {}
        results: dict[str, tuple[int, Mapping[str, Any]]] = {}
        findings: list[BrowserProjectionFinding] = []
        for index, message in enumerate(messages):
            kind = str(message.get("kind") or "")
            source_id = str(message.get("source_id") or "")
            causation_id = str(message.get("causation_id") or "")
            if kind.endswith("action_call") and source_id:
                calls[source_id] = (index, message)
            elif kind.endswith("action_result") and causation_id:
                results[causation_id] = (index, message)
        pairs: list[BrowserHistoryToolPairProjection] = []
        for request_id, (call_index, call) in calls.items():
            result_value = results.get(request_id)
            if result_value is None:
                findings.append(BrowserProjectionFinding(
                    "orphan_browser_action_call",
                    BrowserProjectionFindingSeverity.ERROR,
                    "browser history contains an action call without its result",
                    True,
                    {"request_id": request_id},
                ))
                continue
            result_index, result = result_value
            metadata = _mapping(result.get("metadata"))
            projection_id = str(metadata.get("projection_id") or _mapping(call.get("metadata")).get("projection_id") or "")
            pair = BrowserHistoryToolPairProjection(
                projection_id=projection_id,
                request_id=request_id,
                receipt_id=str(result.get("source_id") or ""),
                call_message_id=str(call.get("message_id") or ""),
                result_message_id=str(result.get("message_id") or ""),
                call_index=call_index,
                result_index=result_index,
                adjacent=result_index == call_index + 1,
                result_ok=bool(metadata.get("ok")),
                error_code=str(metadata.get("error") or ""),
                artifact_ids=_strings(result.get("artifact_ids")),
                selector_refs=_strings(result.get("selector_refs")),
            )
            if not pair.atomic:
                findings.append(BrowserProjectionFinding(
                    "browser_tool_pair_not_atomic",
                    BrowserProjectionFindingSeverity.ERROR,
                    "browser action call and result are not adjacent and atomic",
                    True,
                    {"request_id": request_id, "call_index": call_index, "result_index": result_index},
                ))
            pairs.append(pair)
        orphan_results = set(results) - set(calls)
        for request_id in sorted(orphan_results):
            findings.append(BrowserProjectionFinding(
                "orphan_browser_action_result",
                BrowserProjectionFindingSeverity.ERROR,
                "browser history contains a result without its action call",
                True,
                {"request_id": request_id},
            ))
        return tuple(pairs), tuple(findings)

    def _memory_candidate(self, value: Mapping[str, Any]) -> BrowserMemoryCandidateProjection:
        return BrowserMemoryCandidateProjection(
            candidate_id=str(value.get("candidate_id") or ""),
            kind=str(value.get("kind") or ""),
            summary=str(value.get("summary") or "")[:1000],
            confidence=_float(value.get("confidence")),
            source_event_id=str(value.get("source_event_id") or ""),
            source_action_receipt_id=str(value.get("source_action_receipt_id") or ""),
            source_dom_capture_id=str(value.get("source_dom_capture_id") or ""),
            artifact_ids=_strings(value.get("artifact_ids")),
            retention_hint=str(value.get("retention_hint") or ""),
            candidate_only=bool(value.get("candidate_only")),
            committed=False,
        )

    def _timeline(
        self,
        events: Sequence[EventRecord | Mapping[str, Any]],
    ) -> tuple[BrowserCausalTimelineEntry, ...]:
        result: list[BrowserCausalTimelineEntry] = []
        for event in events:
            raw_value = to_jsonable(event)
            raw = dict(raw_value) if isinstance(raw_value, Mapping) else {}
            payload = _mapping(raw.get("payload"))
            capture = _mapping(payload.get("browser_dom_state"))
            disclosure = _mapping(payload.get("browser_context_disclosure"))
            artifact = _mapping(payload.get("browser_artifact"))
            external = _mapping(payload.get("browser_external_context"))
            if not any((capture, disclosure, artifact, external)):
                continue
            phase = (
                "capture" if capture else
                "artifact" if artifact else
                str(external.get("operation") or "external_context") if external else
                "disclosure"
            )
            causes = list(_strings(payload.get("cause_event_ids")))
            single_cause = str(payload.get("cause_event_id") or "")
            if single_cause and single_cause not in causes:
                causes.insert(0, single_cause)
            result.append(BrowserCausalTimelineEntry(
                event_id=str(raw.get("event_id") or ""),
                event_type=str(raw.get("event_type") or ""),
                phase=phase,
                cause_event_ids=tuple(causes),
                capture_id=str(capture.get("capture_id") or disclosure.get("capture_id") or artifact.get("capture_id") or external.get("capture_id") or ""),
                disclosure_id=str(disclosure.get("disclosure_id") or external.get("disclosure_id") or ""),
                artifact_id=str(artifact.get("artifact_id") or ""),
                claim_id=str(external.get("claim_id") or ""),
                source_ids=_strings(external.get("source_ids")) or _strings((external.get("source_id"),)),
                created_at=str(raw.get("created_at") or ""),
            ))
        return tuple(result[-512:])

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserContextApiProjectionRuntime",
            "owner_unit": "M1-S04B-02",
            "checkpoint_schema": BROWSER_CONTEXT_CHECKPOINT_SCHEMA,
            "disabled": self.disabled,
            "projections": self._projections,
            "failures": self._failures,
        }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(dict.fromkeys(str(item) for item in value if str(item) and str(item) != "None"))
    return ()


def _float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, result))
