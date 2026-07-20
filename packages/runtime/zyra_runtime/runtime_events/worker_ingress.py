"""Live CodeWorker runtime-event ingress into the TypeScript canonical spine.

The query engine remains the owner of query/tool/compact state.  This adapter
only converts an already-emitted TypeScript runtime frame to the versioned
source-record contract and submits it immediately; it never parses stdout or
reconstructs a second session state machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import threading
from typing import Any, Mapping, MutableMapping, Sequence

from .integration import RuntimeEventSpineBridge
from .models import JsonValue, RuntimeEventContractError, coerce_json


SOURCE_SCHEMA = "zyra.runtime-source-record/v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Any) -> str:
    encoded = json.dumps(
        coerce_json(value),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _text(value: Any, fallback: str = "") -> str:
    return str(value).strip() if value is not None and str(value).strip() else fallback


def _integer(value: Any, fallback: int = 0) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []


@dataclass(frozen=True, slots=True)
class WorkerIngressIdentity:
    run_id: str
    task_id: str
    session_id: str
    worker_request_id: str
    node_id: str | None = None
    worker_id: str = "CodeWorkerRuntime"

    def to_jsonable(self, *, tool_call_id: str | None = None) -> dict[str, JsonValue]:
        value: dict[str, JsonValue] = {
            "runId": self.run_id,
            "taskId": self.task_id,
            "sessionId": self.session_id,
            "workerId": self.worker_id,
        }
        if self.node_id:
            value["nodeId"] = self.node_id
        if tool_call_id:
            value["toolCallId"] = tool_call_id
        return value


@dataclass(frozen=True, slots=True)
class WorkerIngressReceipt:
    source_id: str
    source_kind: str
    event_id: str | None
    event_type: str | None
    live_only: bool
    duplicate: bool
    projected: bool
    delivery_ids: tuple[str, ...]
    warnings: tuple[str, ...]

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "WorkerIngressReceipt":
        return cls(
            source_id=_text(value.get("sourceId")),
            source_kind=_text(value.get("sourceKind")),
            event_id=_text(value.get("eventId")) or None,
            event_type=_text(value.get("eventType")) or None,
            live_only=value.get("liveOnly") is True,
            duplicate=value.get("duplicate") is True,
            projected=value.get("projected") is True,
            delivery_ids=tuple(str(item) for item in _list(value.get("deliveryIds"))),
            warnings=tuple(str(item) for item in _list(value.get("warnings"))),
        )


@dataclass(frozen=True, slots=True)
class _Rule:
    kind: str
    domain: str = "codeworker"
    effective: bool = True
    live_only: bool = False
    requires_cause: bool = False


_DIRECT_RULES: Mapping[str, _Rule] = {
    "session_started": _Rule("query_admitted"),
    "context_restored": _Rule("compact_restore", domain="compact"),
    "turn_start": _Rule("turn_started"),
    "turn_started": _Rule("turn_started"),
    "turn_resumed": _Rule("turn_started"),
    "turn_end": _Rule("turn_completed", requires_cause=True),
    "turn_completed": _Rule("turn_completed", requires_cause=True),
    "stream_request_start": _Rule("text_started", effective=False),
    "message_delta": _Rule("text_delta", effective=False, live_only=True),
    "tool_call_started": _Rule("tool_called"),
    "tool_call_completed": _Rule("tool_succeeded", requires_cause=True),
    "context_compacted": _Rule("compact_completed", domain="compact", requires_cause=True),
    "compact_restore_report": _Rule("compact_restore", domain="compact"),
    "control_command": _Rule("control_completed", domain="control", requires_cause=True),
    "watchdog_signal": _Rule("recovery_requested", domain="recovery"),
    "error": _Rule("turn_failed", requires_cause=True),
}


class CodeWorkerRuntimeEventIngress:
    """Strict typed sink used at the real ``runtime.event`` frame boundary."""

    def __init__(
        self,
        bridge: RuntimeEventSpineBridge,
        identity: WorkerIngressIdentity,
        *,
        fail_closed: bool = True,
    ) -> None:
        self.bridge = bridge
        self.identity = identity
        self.fail_closed = fail_closed
        self._lock = threading.RLock()
        self._last_sequence = -1
        self._last_event_id: str | None = None
        self._turn_event_id: str | None = None
        self._stream_event_id: str | None = None
        self._tool_event_ids: MutableMapping[str, str] = {}
        self._compact_event_id: str | None = None
        self._control_event_ids: MutableMapping[str, str] = {}
        self._receipts: list[WorkerIngressReceipt] = []

    @property
    def receipts(self) -> tuple[WorkerIngressReceipt, ...]:
        with self._lock:
            return tuple(self._receipts)

    def admit_query(self, *, sequence: int = 0) -> WorkerIngressReceipt:
        payload = {
            "phase": "query_admitted",
            "query_id": self.identity.worker_request_id,
            "delivery": "immediate",
            "priority": 0,
            "context_digest": _digest(
                {
                    "session_id": self.identity.session_id,
                    "worker_request_id": self.identity.worker_request_id,
                }
            ),
        }
        receipt = self._emit(
            rule=_Rule("query_admitted"),
            phase="query_admitted",
            sequence=sequence,
            payload=payload,
            cause=None,
        )
        with self._lock:
            self._last_sequence = max(self._last_sequence, sequence)
        return receipt

    def emit_payload(
        self,
        payload: Mapping[str, Any],
        *,
        transport_sequence: int | None = None,
    ) -> tuple[WorkerIngressReceipt, ...]:
        data = dict(payload)
        phase = _text(data.get("phase"), "runtime_event")
        # A phase payload may contain a domain-local `sequence` that restarts
        # (tool progress, child session, compact reports).  The outer runtime
        # frame is the monotonic transport boundary and therefore wins when
        # the real CodeWorker host supplies it.
        sequence = (
            _integer(transport_sequence, self._last_sequence + 1)
            if transport_sequence is not None
            else _integer(data.get("sequence"), self._last_sequence + 1)
        )
        with self._lock:
            if sequence <= self._last_sequence:
                raise RuntimeEventContractError(
                    f"CodeWorker runtime event sequence is not monotonic: {sequence} <= {self._last_sequence}"
                )
            self._last_sequence = sequence
            if phase == "context_compacted":
                return self._emit_compact_pair(data, sequence)
            if phase == "control_command":
                return self._emit_control_pair(data, sequence)
            if phase in {"tool_failure_signal", "tool_result_budget_exceeded"}:
                return self._emit_recovery_signal(data, sequence)
            if phase == "tool_call_completed":
                return (self._emit_tool_terminal(data, sequence),)
            rule = _DIRECT_RULES.get(phase)
            if rule is None:
                # Non-state diagnostic frames stay out of canonical history.
                # They remain in the existing compatibility EventRecord trace,
                # so dropping them here cannot fabricate semantic progress.
                return ()
            cause = self._cause_for(rule, phase, data)
            receipt = self._emit(
                rule=rule,
                phase=phase,
                sequence=sequence,
                payload=self._payload_for(rule, phase, data),
                cause=cause,
            )
            self._remember(phase, data, receipt)
            return (receipt,)

    def emit_legacy_host_event(self, event: Any) -> WorkerIngressReceipt | None:
        """Commit a host-side permission/gateway EventRecord as it happens.

        The TypeScript source mapper remains preferred.  This path exists for
        physical host events that already have their own durable domain owner.
        """

        try:
            receipt = self.bridge.append_legacy_event(event)
        except Exception:
            if self.fail_closed:
                raise
            return None
        canonical = receipt.event.canonical or {}
        raw_provenance = canonical.get("provenance", {})
        provenance = raw_provenance if isinstance(raw_provenance, Mapping) else {}
        converted = WorkerIngressReceipt(
            source_id=str(provenance.get("sourceEventId") or receipt.event.event_id),
            source_kind=receipt.event.event_type,
            event_id=receipt.event.event_id,
            event_type=receipt.event.event_type,
            live_only=False,
            duplicate=receipt.duplicate,
            projected=receipt.projection_cursor > receipt.event.aggregate_sequence,
            # The compatibility AppendReceipt intentionally exposes only a
            # delivery count.  Canonical source-record receipts retain exact
            # delivery IDs; do not invent identifiers for legacy host facts.
            delivery_ids=(),
            warnings=(),
        )
        with self._lock:
            self._receipts.append(converted)
        return converted

    def _emit_compact_pair(
        self,
        payload: Mapping[str, Any],
        sequence: int,
    ) -> tuple[WorkerIngressReceipt, ...]:
        compact_id = _text(
            payload.get("compact_boundary_id"),
            f"compact:{self.identity.session_id}:{sequence}",
        )
        common = {
            "compact_id": compact_id,
            "reason": _text(payload.get("trigger"), "auto_threshold"),
            "before_tokens": _integer(payload.get("before_tokens")),
            "after_tokens": _integer(payload.get("after_tokens")),
            "restore_digest": _digest(payload),
            "content": coerce_json(payload),
        }
        started = self._emit(
            rule=_Rule("compact_started", domain="compact"),
            phase="compact_started",
            sequence=sequence * 10,
            payload=common,
            cause=None,
        )
        self._compact_event_id = started.event_id
        completed = self._emit(
            rule=_Rule("compact_completed", domain="compact", requires_cause=True),
            phase="compact_completed",
            sequence=sequence * 10 + 1,
            payload=common,
            cause=started.event_id,
        )
        return (started, completed)

    def _emit_control_pair(
        self,
        payload: Mapping[str, Any],
        sequence: int,
    ) -> tuple[WorkerIngressReceipt, ...]:
        control_id = _text(payload.get("command_id"), f"control:{sequence}")
        common = {
            "control_id": control_id,
            "control_kind": _text(payload.get("name"), _text(payload.get("command"), "runtime")),
            "decision": _text(payload.get("status"), "accepted"),
            "actor_id": "CodeWorkerRuntime",
            "reason_code": _text(payload.get("error")),
        }
        requested = self._emit(
            rule=_Rule("control_requested", domain="control"),
            phase="control_requested",
            sequence=sequence * 10,
            payload=common,
            cause=None,
        )
        self._control_event_ids[control_id] = requested.event_id or ""
        accepted_kind = "control_accepted" if common["decision"] != "rejected" else "control_rejected"
        accepted = self._emit(
            rule=_Rule(accepted_kind, domain="control", requires_cause=True),
            phase=accepted_kind,
            sequence=sequence * 10 + 1,
            payload=common,
            cause=requested.event_id,
        )
        if accepted_kind == "control_rejected":
            return (requested, accepted)
        completed = self._emit(
            rule=_Rule("control_completed", domain="control", requires_cause=True),
            phase="control_completed",
            sequence=sequence * 10 + 2,
            payload=common,
            cause=accepted.event_id,
        )
        return (requested, accepted, completed)

    def _emit_recovery_signal(
        self,
        payload: Mapping[str, Any],
        sequence: int,
    ) -> tuple[WorkerIngressReceipt, ...]:
        phase = _text(payload.get("phase"), "runtime_failure")
        recovery_payload = {
            "recovery_id": f"recovery:{self.identity.worker_request_id}:{sequence}",
            "fault_id": _text(payload.get("tool_call_id"), phase),
            "strategy": "replan" if phase == "tool_failure_signal" else "artifact_offload",
            "attempt": _integer(payload.get("attempt")),
            "status": "requested",
            "error_code": _text(payload.get("error"), phase),
        }
        return (
            self._emit(
                rule=_Rule("recovery_requested", domain="recovery"),
                phase=phase,
                sequence=sequence,
                payload=recovery_payload,
                cause=None,
            ),
        )

    def _emit_tool_terminal(
        self,
        payload: Mapping[str, Any],
        sequence: int,
    ) -> WorkerIngressReceipt:
        tool_result = _mapping(payload.get("tool_result"))
        tool_call_id = _text(
            tool_result.get("tool_call_id"),
            _text(payload.get("tool_call_id")),
        )
        cause = self._tool_event_ids.get(tool_call_id)
        if not cause:
            raise RuntimeEventContractError(
                f"tool result has no canonical tool-call cause: {tool_call_id or '<missing>'}"
            )
        ok = tool_result.get("ok") is True
        kind = "tool_succeeded" if ok else "tool_failed"
        normalized = {
            "tool_name": _text(payload.get("tool_name"), _text(tool_result.get("tool_name"), "unknown")),
            "tool_call_id": tool_call_id,
            "result_digest": _digest(tool_result),
            "error_code": "" if ok else _text(tool_result.get("error"), "tool_error"),
            "result": coerce_json(tool_result),
            "raw_result": coerce_json(tool_result.get("output")),
        }
        receipt = self._emit(
            rule=_Rule(kind, requires_cause=True),
            phase="tool_call_completed",
            sequence=sequence,
            payload=normalized,
            cause=cause,
        )
        self._tool_event_ids.pop(tool_call_id, None)
        return receipt

    def _cause_for(
        self,
        rule: _Rule,
        phase: str,
        payload: Mapping[str, Any],
    ) -> str | None:
        if not rule.requires_cause:
            return None
        if rule.kind in {"turn_completed", "turn_failed"}:
            return self._turn_event_id or self._last_event_id
        if rule.kind == "control_completed":
            control_id = _text(payload.get("command_id"))
            return self._control_event_ids.get(control_id) or self._last_event_id
        if rule.kind == "compact_completed":
            return self._compact_event_id or self._last_event_id
        return self._last_event_id

    def _payload_for(
        self,
        rule: _Rule,
        phase: str,
        payload: Mapping[str, Any],
    ) -> dict[str, JsonValue]:
        if rule.kind.startswith("turn_"):
            return {
                "status": "failed" if rule.kind == "turn_failed" else phase,
                "phase": phase,
                "attempt": _integer(payload.get("turn_index")),
                "turn_id": _text(payload.get("turn_id"), f"turn:{self.identity.session_id}:{_integer(payload.get('turn_index'))}"),
                "error_code": _text(payload.get("error")),
            }
        if rule.kind == "query_admitted":
            return {
                "query_id": self.identity.worker_request_id,
                "delivery": "immediate",
                "priority": 0,
                "context_digest": _digest(payload),
            }
        if rule.kind.startswith("text_"):
            content = payload.get("delta", payload.get("content", ""))
            return {
                "stream_id": _text(payload.get("stream_id"), self.identity.worker_request_id),
                "segment_index": _integer(payload.get("segment_index"), _integer(payload.get("sequence"))),
                "delta_bytes": len(str(content).encode("utf-8")),
                "content_digest": _digest(content),
                "content": coerce_json(content),
            }
        if rule.kind == "tool_called":
            tool_call_id = _text(payload.get("tool_call_id"))
            if not tool_call_id:
                raise RuntimeEventContractError("tool_call_started frame lacks tool_call_id")
            arguments = _mapping(payload.get("arguments"))
            return {
                "tool_name": _text(payload.get("tool_name"), "unknown"),
                "tool_call_id": tool_call_id,
                "input_digest": _digest(arguments),
                "arguments": coerce_json(arguments),
                "batch_index": _integer(payload.get("batch_index")),
                "started": True,
                "interruptible": payload.get("interruptible") is True,
            }
        if rule.kind == "compact_restore":
            return {
                "compact_id": _text(payload.get("compact_restore_contract_id"), f"restore:{self.identity.session_id}"),
                "checkpoint_id": _text(payload.get("checkpoint_id"), self.identity.session_id),
                "reason": "resume" if phase == "context_restored" else "post_compact_restore",
                "before_tokens": 0,
                "after_tokens": _integer(payload.get("restored_tokens")),
                "restore_digest": _digest(payload),
            }
        if rule.kind == "recovery_requested":
            return {
                "recovery_id": f"recovery:{self.identity.worker_request_id}:{_integer(payload.get('sequence'))}",
                "fault_id": _text(payload.get("fault_id"), phase),
                "strategy": _text(payload.get("strategy"), "replan"),
                "attempt": _integer(payload.get("attempt")),
                "status": "requested",
            }
        return {key: coerce_json(value) for key, value in payload.items()}

    def _emit(
        self,
        *,
        rule: _Rule,
        phase: str,
        sequence: int,
        payload: Mapping[str, Any],
        cause: str | None,
    ) -> WorkerIngressReceipt:
        if rule.requires_cause and not cause:
            raise RuntimeEventContractError(f"{rule.kind} requires a canonical cause")
        tool_call_id = _text(payload.get("tool_call_id")) or None
        source_id = self._source_id(rule, phase, sequence, payload)
        source: dict[str, Any] = {
            "schema": SOURCE_SCHEMA,
            "sourceId": source_id,
            "kind": rule.kind,
            "domain": rule.domain,
            "occurredAt": _text(payload.get("created_at"), _utc_now()),
            "identity": self.identity.to_jsonable(tool_call_id=tool_call_id),
            "owner": {
                "domain": rule.domain,
                "ownerId": self._owner_id(rule.domain),
                "transactionId": f"runtime-frame:{self.identity.worker_request_id}:{sequence}",
                "storeRef": f"runtime://codeworker/{self.identity.run_id}/{self.identity.task_id}",
                "committed": not rule.live_only,
                "committedAt": _utc_now(),
            },
            "causality": {
                "correlationId": self.identity.worker_request_id,
                "causationEventId": cause,
                "requestId": self.identity.worker_request_id,
                "producerSequence": max(0, sequence),
            },
            "delivery": {
                "mode": "live_only" if rule.live_only else "targeted",
                "dependencyRecipients": [],
                "topK": 4,
            },
            "subjectId": self.identity.worker_id,
            "summary": f"{phase} for {self.identity.task_id}"[:1024],
            "effective": rule.effective,
            "payload": {key: coerce_json(value) for key, value in payload.items()},
            "evidenceRefs": [],
            "artifactRefs": [],
            "metadata": {
                "runtime_protocol": "zyra.claude-runtime.v1",
                "worker_request_id": self.identity.worker_request_id,
                "frame_phase": phase,
                "raw_protocol_frame_forwarded": False,
            },
        }
        result = self.bridge.append_source_record(source)
        receipt = WorkerIngressReceipt.from_json(result)
        self._receipts.append(receipt)
        if receipt.event_id:
            self._last_event_id = receipt.event_id
        return receipt

    def _remember(
        self,
        phase: str,
        payload: Mapping[str, Any],
        receipt: WorkerIngressReceipt,
    ) -> None:
        if not receipt.event_id:
            return
        if phase in {"turn_start", "turn_started", "turn_resumed"}:
            self._turn_event_id = receipt.event_id
        elif phase == "stream_request_start":
            self._stream_event_id = receipt.event_id
        elif phase == "tool_call_started":
            tool_call_id = _text(payload.get("tool_call_id"))
            if tool_call_id:
                self._tool_event_ids[tool_call_id] = receipt.event_id

    def _source_id(
        self,
        rule: _Rule,
        phase: str,
        sequence: int,
        payload: Mapping[str, Any],
    ) -> str:
        fingerprint = _digest(
            {
                "request_id": self.identity.worker_request_id,
                "sequence": sequence,
                "phase": phase,
                "kind": rule.kind,
                "payload": payload,
            }
        )[7:23]
        return f"codeworker:{self.identity.worker_request_id}:{sequence}:{rule.kind}:{fingerprint}"

    @staticmethod
    def _owner_id(domain: str) -> str:
        return {
            "codeworker": "CodeWorkerRuntime",
            "permission": "ToolPermissionRuntime",
            "mcp": "McpRuntime",
            "skill": "SkillRuntime",
            "subagent": "SubagentRuntime",
            "compact": "CompactRuntime",
            "control": "RuntimeControlDispatcher",
            "recovery": "RecoveryRuntime",
        }.get(domain, "CodeWorkerRuntime")


__all__ = [
    "CodeWorkerRuntimeEventIngress",
    "SOURCE_SCHEMA",
    "WorkerIngressIdentity",
    "WorkerIngressReceipt",
]
