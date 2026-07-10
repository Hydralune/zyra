from __future__ import annotations

"""Canonical event projection for permission state transitions."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable


class PermissionRuntimeEventKind(StrEnum):
    EVALUATION_STARTED = "permission_evaluation_started"
    HOOKS_EVALUATED = "permission_hooks_evaluated"
    CLASSIFIER_EVALUATED = "permission_classifier_evaluated"
    DECISION_COMMITTED = "permission_decision"
    REQUEST_CREATED = "permission_request_created"
    REQUEST_DELIVERED = "permission_request_delivered"
    REQUEST_RESOLVED = "permission_request_resolved"
    REQUEST_EXPIRED = "permission_request_expired"
    REQUEST_CANCELLED = "permission_request_cancelled"
    REQUEST_ABORTED = "permission_request_aborted"
    EXECUTION_GRANT_ISSUED = "permission_execution_grant_issued"
    EXECUTION_GRANT_CONSUMED = "permission_execution_grant_consumed"
    EXECUTION_GRANT_REJECTED = "permission_execution_grant_rejected"
    RECOVERY_INPUT = "recovery_input"
    STATE_RESTORED = "permission_state_restored"
    MODE_TRANSITIONED = "permission_mode_transitioned"


@dataclass(frozen=True, slots=True)
class PermissionEventEnvelope:
    kind: PermissionRuntimeEventKind
    session_id: str
    worker_request_id: str
    tool_call_id: str = ""
    request_id: str = ""
    decision_id: str = ""
    arguments_digest: str = ""
    phase: str = ""
    cause_event_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.permission.event.v1",
            "kind": str(self.kind),
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "tool_call_id": self.tool_call_id,
            "request_id": self.request_id,
            "decision_id": self.decision_id,
            "arguments_digest": self.arguments_digest,
            "phase": self.phase or str(self.kind),
            "cause_event_id": self.cause_event_id,
            "payload": _safe_payload(self.payload),
            "created_at": self.created_at,
        }


class PermissionEventProjector:
    """Projects permission records to Zyra events with explicit causality."""

    def __init__(self, *, owner_unit: str = "M1-S03A-01", runtime_id: str = "zyra-tool-permission-runtime") -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id

    def event(
        self,
        envelope: PermissionEventEnvelope,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> EventRecord:
        payload = envelope.to_dict()
        payload.update({"owner_unit": self.owner_unit, "runtime_id": self.runtime_id})
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": envelope.session_id,
                    "worker_request_id": envelope.worker_request_id,
                    "phase": envelope.phase or str(envelope.kind),
                    "permission_runtime": payload,
                }
            },
        )

    def decision_events(
        self,
        decision: Any,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        recovery: Any | None = None,
        cause_event_id: str = "",
    ) -> list[EventRecord]:
        session_id = str(_get(decision, "session_id", ""))
        tool_call_id = str(_get(decision, "tool_call_id", _get(decision, "tool_use_id", "")))
        request_id = str(_get(decision, "request_id", ""))
        decision_id = str(_get(decision, "decision_id", new_id("permdecision")))
        digest = str(_get(decision, "arguments_digest", ""))
        effect = _enum_text(_get(decision, "effect", ""))
        decision_payload = _record_payload(decision)
        decision_metadata = _get(decision, "metadata", {})
        human_intervention_count = (
            int(decision_metadata.get("human_intervention_count") or 0)
            if isinstance(decision_metadata, Mapping)
            else 0
        )
        decision_payload.update(
            {
                "effect": effect,
                "side_effect_allowed": effect == "allow",
                "human_intervention_count": human_intervention_count,
            }
        )
        decision_event = self.event(
            PermissionEventEnvelope(
                kind=PermissionRuntimeEventKind.DECISION_COMMITTED,
                session_id=session_id,
                worker_request_id=worker_request_id,
                tool_call_id=tool_call_id,
                request_id=request_id,
                decision_id=decision_id,
                arguments_digest=digest,
                cause_event_id=cause_event_id,
                payload=decision_payload,
            ),
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
        )
        events = [decision_event]
        if recovery is not None and effect == "deny":
            recovery_payload = _record_payload(recovery)
            recovery_payload.update(
                {
                    "permission_decision_id": decision_id,
                    "permission_decision_event_id": decision_event.event_id,
                    "tool_call_id": tool_call_id,
                    "request_id": request_id,
                    "arguments_digest": digest,
                    "human_intervention_count": human_intervention_count,
                }
            )
            events.append(
                self.event(
                    PermissionEventEnvelope(
                        kind=PermissionRuntimeEventKind.RECOVERY_INPUT,
                        session_id=session_id,
                        worker_request_id=worker_request_id,
                        tool_call_id=tool_call_id,
                        request_id=request_id,
                        decision_id=decision_id,
                        arguments_digest=digest,
                        cause_event_id=decision_event.event_id,
                        payload=recovery_payload,
                    ),
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                )
            )
        return events

    def request_event(
        self,
        request: Any,
        *,
        kind: PermissionRuntimeEventKind,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        cause_event_id: str = "",
    ) -> EventRecord:
        return self.event(
            PermissionEventEnvelope(
                kind=kind,
                session_id=str(_get(request, "session_id", "")),
                worker_request_id=worker_request_id,
                tool_call_id=str(_get(request, "tool_call_id", _get(request, "tool_use_id", ""))),
                request_id=str(_get(request, "request_id", "")),
                arguments_digest=str(_get(request, "arguments_digest", "")),
                cause_event_id=cause_event_id,
                payload=_record_payload(request),
            ),
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
        )

    def grant_event(
        self,
        grant: Any,
        *,
        consumed: bool,
        accepted: bool,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        reason: str = "",
        cause_event_id: str = "",
    ) -> EventRecord:
        kind = (
            PermissionRuntimeEventKind.EXECUTION_GRANT_CONSUMED
            if consumed and accepted
            else PermissionRuntimeEventKind.EXECUTION_GRANT_REJECTED
            if consumed
            else PermissionRuntimeEventKind.EXECUTION_GRANT_ISSUED
        )
        payload = _record_payload(grant)
        # Tokens/signatures are intentionally never projected into events.
        payload.pop("token", None)
        payload.pop("signature", None)
        payload.update({"accepted": accepted, "reason": reason})
        return self.event(
            PermissionEventEnvelope(
                kind=kind,
                session_id=str(_get(grant, "session_id", "")),
                worker_request_id=worker_request_id,
                tool_call_id=str(_get(grant, "tool_call_id", "")),
                request_id=str(_get(grant, "request_id", "")),
                decision_id=str(_get(grant, "decision_id", "")),
                arguments_digest=str(_get(grant, "arguments_digest", "")),
                cause_event_id=cause_event_id,
                payload=payload,
            ),
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
        )

    def restore_event(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        run_id: str,
        task_id: str,
        node_id: str | None,
        restored_request_ids: Sequence[str],
        restored_rule_ids: Sequence[str],
        snapshot_digest: str,
    ) -> EventRecord:
        return self.event(
            PermissionEventEnvelope(
                kind=PermissionRuntimeEventKind.STATE_RESTORED,
                session_id=session_id,
                worker_request_id=worker_request_id,
                payload={
                    "restored_request_ids": list(restored_request_ids),
                    "restored_rule_ids": list(restored_rule_ids),
                    "snapshot_digest": snapshot_digest,
                },
            ),
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
        )


_SENSITIVE_FRAGMENTS = (
    "secret",
    "token",
    "password",
    "credential",
    "authorization",
    "cookie",
    "api_key",
    "private_key",
    "raw_arguments",
)


def _safe_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).lower()
        if any(fragment in normalized for fragment in _SENSITIVE_FRAGMENTS):
            output[str(key)] = "[REDACTED]"
        elif isinstance(item, Mapping):
            output[str(key)] = _safe_payload(item)
        elif isinstance(item, list | tuple):
            output[str(key)] = [
                _safe_payload(child) if isinstance(child, Mapping) else to_jsonable(child)
                for child in item
            ]
        else:
            output[str(key)] = to_jsonable(item)
    return output


def _record_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return _safe_payload(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        projected = to_dict()
        if isinstance(projected, Mapping):
            return _safe_payload(projected)
    return {"value": str(value)}


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value))


__all__ = [
    "PermissionEventEnvelope",
    "PermissionEventProjector",
    "PermissionRuntimeEventKind",
]
