from __future__ import annotations

import copy
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from zyra_core import ArtifactRef, EventRecord, EventType, now_iso, to_jsonable

from .event_port import BrowserActionEventPort
from .integration_models import (
    BrowserActionIntegrationError,
    BrowserActionPlan,
    DispatchBoundary,
    PlanAdmission,
    PlanExecutionResult,
    PlanPhase,
    StepOutcome,
    StepState,
)
from .models import ActionIdentity, ActionRequest, digest_value, stable_id
from .secret_policy import SecretRedactor


class BrowserActionEventWriterError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class BrowserActionContextReceipt:
    receipt_id: str
    request_id: str
    request_fingerprint: str
    browser_session_id: str
    worker_request_id: str
    action_id: str
    tool_call_id: str
    action: str
    step_index: int
    ok: bool
    status: str
    output: Mapping[str, Any]
    error_code: str = ""
    error_message: str = ""
    artifact_handoffs: tuple[Mapping[str, Any], ...] = ()
    partial: bool = False
    final: bool = True
    cancelled: bool = False
    side_effect_count: int = 0
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        required = (
            self.receipt_id,
            self.request_id,
            self.request_fingerprint,
            self.browser_session_id,
            self.worker_request_id,
            self.action_id,
            self.action,
        )
        if any(not str(value) for value in required) or self.step_index < 1:
            raise ValueError("browser action context receipt identity is incomplete")
        object.__setattr__(self, "output", copy.deepcopy(dict(self.output)))
        object.__setattr__(self, "artifact_handoffs", tuple(copy.deepcopy(dict(item)) for item in self.artifact_handoffs))
        if self.ok and self.error_code:
            raise ValueError("successful browser context receipt cannot contain an error code")

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "request_id": self.request_id,
            "request_fingerprint": self.request_fingerprint,
            "browser_session_id": self.browser_session_id,
            "worker_request_id": self.worker_request_id,
            "action_id": self.action_id,
            "tool_call_id": self.tool_call_id,
            "action": self.action,
            "step_index": self.step_index,
            "ok": self.ok,
            "status": self.status,
            "output": copy.deepcopy(dict(self.output)),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "artifact_handoffs": [copy.deepcopy(dict(item)) for item in self.artifact_handoffs],
            "partial": self.partial,
            "final": self.final,
            "cancelled": self.cancelled,
            "side_effect_count": self.side_effect_count,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class EventWriterSnapshot:
    plan_events: int
    tool_calls: int
    terminal_results: int
    partial_results: int
    permission_events: int
    action_events: int
    artifact_events: int
    failures: int
    event_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_id": "zyra-browser-action-event-writer",
            "owner_unit": "M1-S04C-02",
            "canonical_owner": "SQLiteStore/event log at API persistence boundary",
            "plan_events": self.plan_events,
            "tool_calls": self.tool_calls,
            "terminal_results": self.terminal_results,
            "partial_results": self.partial_results,
            "permission_events": self.permission_events,
            "action_events": self.action_events,
            "artifact_events": self.artifact_events,
            "failures": self.failures,
            "event_count": self.event_count,
        }


class BrowserActionEventWriter:
    """Causal action/tool event projection consumed by the API event log."""

    def __init__(self, *, disabled: bool = False) -> None:
        self.disabled = disabled
        self._events: list[EventRecord] = []
        self._event_ids: set[str] = set()
        self._tool_calls: dict[str, EventRecord] = {}
        self._terminal_results: dict[str, EventRecord] = {}
        self._action_identity: dict[str, ActionIdentity] = {}
        self._plan_event_id = ""
        self._plan_id = ""
        self._lock = threading.RLock()
        self._plan_events = 0
        self._partial_results = 0
        self._permission_events = 0
        self._action_events = 0
        self._artifact_events = 0
        self._failures = 0

    def sink(self, event: EventRecord) -> None:
        self._ensure_available()
        self._validate_event(event)
        with self._lock:
            if event.event_id in self._event_ids:
                return
            self._events.append(event)
            self._event_ids.add(event.event_id)
            if event.event_type == EventType.ARTIFACT_WRITTEN:
                self._artifact_events += 1
            elif "browser_action" in event.payload:
                self._action_events += 1
            if str(event.event_type).startswith("permission") or "permission" in repr(event.payload).casefold():
                self._permission_events += 1

    def plan_admitted(self, admission: PlanAdmission, *, node_id: str = "") -> EventRecord:
        self._ensure_available()
        plan = admission.require()
        with self._lock:
            if self._plan_event_id:
                raise BrowserActionEventWriterError("plan_event_replayed", "browser action plan admission event already exists")
        event = EventRecord(
            run_id=plan.run_id,
            task_id=plan.task_id,
            node_id=node_id or None,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "browser_action_plan": {
                    "schema": "zyra.browser-action.plan-admitted.v1",
                    "plan_id": plan.plan_id,
                    "plan_digest": plan.digest,
                    "worker_request_id": plan.worker_request_id,
                    "browser_session_id": plan.browser_session_id,
                    "permission_session_id": plan.permission_session_id,
                    "admission_id": admission.admission_id,
                    "registry_digest": admission.registry_digest,
                    "schema_projection_digest": admission.schema_projection_digest,
                    "bypass_scan_digest": admission.bypass_scan_digest,
                    "action_ids": list(plan.action_ids),
                    "action_count": len(plan.steps),
                }
            },
        )
        self.sink(event)
        with self._lock:
            self._plan_event_id = event.event_id
            self._plan_id = plan.plan_id
            self._plan_events += 1
        return event

    def plan_rejected(
        self,
        admission: PlanAdmission,
        *,
        run_id: str,
        task_id: str,
        worker_request_id: str,
        node_id: str = "",
    ) -> EventRecord:
        self._ensure_available()
        event = EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id or None,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "browser_action_plan": {
                    "schema": "zyra.browser-action.plan-rejected.v1",
                    "admission_id": admission.admission_id,
                    "worker_request_id": worker_request_id,
                    "registry_digest": admission.registry_digest,
                    "schema_projection_digest": admission.schema_projection_digest,
                    "issues": [issue.public_dict() for issue in admission.issues],
                    "side_effect_count": 0,
                }
            },
        )
        self.sink(event)
        self._failures += 1
        return event

    def tool_call(self, request: ActionRequest, *, event_port: BrowserActionEventPort) -> EventRecord:
        self._ensure_available()
        identity = request.identity
        with self._lock:
            if identity.action_id in self._tool_calls:
                raise BrowserActionEventWriterError("tool_call_replayed", "browser action tool call already exists")
            if self._plan_id and request.identity.worker_request_id == "":
                raise BrowserActionEventWriterError("tool_call_identity_missing", "browser action tool call has no worker identity")
        safe_arguments = SecretRedactor().redact(request.arguments)
        event = EventRecord(
            run_id=identity.run_id,
            task_id=identity.task_id,
            node_id=identity.node_id or None,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "tool_call": {
                    "schema": "zyra.browser-action.tool-call.v1",
                    "tool_call_id": identity.action_id,
                    "action_id": identity.action_id,
                    "tool_name": request.action,
                    "namespace": "browser",
                    "run_id": identity.run_id,
                    "task_id": identity.task_id,
                    "worker_request_id": identity.worker_request_id,
                    "browser_session_id": identity.browser_session_id,
                    "permission_session_id": identity.session_id,
                    "step_index": identity.step_index,
                    "arguments": safe_arguments,
                    "arguments_digest": digest_value(request.arguments),
                    "request_digest": request.request_digest,
                    "deadline_at": request.deadline_at,
                    "cause_event_id": self._plan_event_id,
                }
            },
        )
        self.sink(event)
        event_port.seed_cause(identity, event.event_id)
        with self._lock:
            self._tool_calls[identity.action_id] = event
            self._action_identity[identity.action_id] = identity
        return event

    def partial(
        self,
        request: ActionRequest,
        *,
        phase: PlanPhase,
        details: Mapping[str, Any],
        cause_event_id: str,
    ) -> EventRecord:
        self._ensure_tool_call(request.identity.action_id)
        event = EventRecord(
            run_id=request.identity.run_id,
            task_id=request.identity.task_id,
            node_id=request.identity.node_id or None,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "tool_result": {
                    "schema": "zyra.browser-action.tool-result.v1",
                    "tool_call_id": request.identity.action_id,
                    "action_id": request.identity.action_id,
                    "tool_name": request.action,
                    "worker_request_id": request.identity.worker_request_id,
                    "browser_session_id": request.identity.browser_session_id,
                    "step_index": request.identity.step_index,
                    "status": "partial",
                    "partial": True,
                    "final": False,
                    "phase": str(phase),
                    "details": SecretRedactor().redact(dict(details)),
                    "cause_event_id": cause_event_id,
                }
            },
        )
        self.sink(event)
        self._partial_results += 1
        return event

    def terminal(self, outcome: StepOutcome, *, cause_event_id: str = "") -> EventRecord:
        self._ensure_available()
        self._ensure_tool_call(outcome.action_id)
        with self._lock:
            if outcome.action_id in self._terminal_results:
                raise BrowserActionEventWriterError("terminal_result_replayed", "browser action already has a terminal result")
            identity = self._action_identity[outcome.action_id]
            tool_call = self._tool_calls[outcome.action_id]
        selected_cause = cause_event_id or (
            outcome.events[-1].event_id if outcome.events else tool_call.event_id
        )
        output = outcome.result.output if outcome.result else {}
        event = EventRecord(
            run_id=identity.run_id,
            task_id=identity.task_id,
            node_id=identity.node_id or None,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "tool_result": {
                    "schema": "zyra.browser-action.tool-result.v1",
                    "tool_call_id": outcome.action_id,
                    "action_id": outcome.action_id,
                    "tool_name": outcome.action,
                    "run_id": identity.run_id,
                    "task_id": identity.task_id,
                    "worker_request_id": identity.worker_request_id,
                    "browser_session_id": identity.browser_session_id,
                    "permission_session_id": identity.session_id,
                    "step_index": outcome.step_index,
                    "status": str(outcome.state),
                    "ok": outcome.ok,
                    "pending": outcome.pending,
                    "partial": False,
                    "final": True,
                    "output": SecretRedactor().redact(output),
                    "output_digest": digest_value(output),
                    "artifact_ids": [artifact.artifact_id for artifact in outcome.artifacts],
                    "error_code": outcome.error_code,
                    "error_message": SecretRedactor().redact(outcome.error_message),
                    "failure_phase": outcome.failure_phase,
                    "dispatch_boundary": str(outcome.dispatch_boundary),
                    "side_effect_count": outcome.side_effect_count,
                    "cdp_effect_count": outcome.cdp_effect_count,
                    "network_effect_count": outcome.network_effect_count,
                    "file_effect_count": outcome.file_effect_count,
                    "automatic_replay_allowed": (
                        outcome.side_effect_count == 0
                        and outcome.dispatch_boundary != DispatchBoundary.AFTER_EFFECT
                        and not outcome.ok
                    ),
                    "cause_event_id": selected_cause,
                }
            },
        )
        self.sink(event)
        with self._lock:
            self._terminal_results[outcome.action_id] = event
            if not outcome.ok:
                self._failures += 1
        return event

    def has_terminal(self, action_id: str) -> bool:
        """Return whether the action already has its exactly-one final result."""
        with self._lock:
            return str(action_id) in self._terminal_results

    def artifact_event(
        self,
        identity: ActionIdentity,
        artifact: ArtifactRef,
        *,
        receipt_id: str,
        cause_event_id: str,
    ) -> EventRecord:
        self._ensure_tool_call(identity.action_id)
        if str(artifact.metadata.get("action_id") or "") not in {"", identity.action_id}:
            raise BrowserActionEventWriterError(
                "artifact_action_mismatch",
                "browser artifact metadata belongs to another action",
            )
        event = EventRecord(
            run_id=identity.run_id,
            task_id=identity.task_id,
            node_id=identity.node_id or None,
            event_type=EventType.ARTIFACT_WRITTEN,
            payload={
                "browser_action": {
                    "schema": "zyra.browser-action.artifact.v1",
                    "identity": identity.to_dict(),
                    "action_id": identity.action_id,
                    "worker_request_id": identity.worker_request_id,
                    "browser_session_id": identity.browser_session_id,
                    "receipt_id": receipt_id,
                    "cause_event_id": cause_event_id,
                    "artifact": to_jsonable(artifact),
                }
            },
        )
        self.sink(event)
        return event

    def finish_plan(self, execution: PlanExecutionResult, *, node_id: str = "") -> EventRecord:
        self._ensure_available()
        missing = [
            outcome.action_id
            for outcome in execution.outcomes
            if outcome.action_id not in self._terminal_results
        ]
        if missing:
            raise BrowserActionEventWriterError(
                "terminal_result_missing",
                "browser plan cannot finish without one terminal result per attempted action",
                details={"missing": missing},
            )
        cause = self._events[-1].event_id if self._events else self._plan_event_id
        event = EventRecord(
            run_id=execution.plan.run_id,
            task_id=execution.plan.task_id,
            node_id=node_id or None,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "browser_action_plan": {
                    "schema": "zyra.browser-action.plan-result.v1",
                    "execution": execution.public_dict(),
                    "worker_request_id": execution.plan.worker_request_id,
                    "browser_session_id": execution.plan.browser_session_id,
                    "permission_session_id": execution.plan.permission_session_id,
                    "cause_event_id": cause,
                }
            },
        )
        self.sink(event)
        return event

    def context_receipts(self, outcomes: Sequence[StepOutcome]) -> tuple[BrowserActionContextReceipt, ...]:
        receipts: list[BrowserActionContextReceipt] = []
        for outcome in outcomes:
            identity = self._action_identity.get(outcome.action_id)
            if identity is None:
                raise BrowserActionEventWriterError("tool_call_missing", "cannot project context receipt without tool call")
            handoffs = tuple(
                {
                    "artifact_id": artifact.artifact_id,
                    "kind": str(artifact.kind),
                    "uri": artifact.uri,
                    "title": artifact.title,
                    "metadata": copy.deepcopy(artifact.metadata),
                }
                for artifact in outcome.artifacts
            )
            receipts.append(
                BrowserActionContextReceipt(
                    receipt_id=outcome.preflight_receipt_id or stable_id("brcontextreceipt", outcome.action_id),
                    request_id=outcome.action_id,
                    request_fingerprint=outcome.request_digest,
                    browser_session_id=identity.browser_session_id,
                    worker_request_id=identity.worker_request_id,
                    action_id=outcome.action_id,
                    tool_call_id=outcome.permission_tool_use_id or outcome.action_id,
                    action=outcome.action,
                    step_index=outcome.step_index,
                    ok=outcome.ok,
                    status=str(outcome.state),
                    output=outcome.result.output if outcome.result else {},
                    error_code=outcome.error_code,
                    error_message=outcome.error_message,
                    artifact_handoffs=handoffs,
                    partial=False,
                    final=True,
                    cancelled=outcome.state == StepState.CANCELLED,
                    side_effect_count=outcome.side_effect_count,
                )
            )
        return tuple(receipts)

    def events(self) -> tuple[EventRecord, ...]:
        with self._lock:
            return tuple(self._events)

    def snapshot(self) -> EventWriterSnapshot:
        with self._lock:
            return EventWriterSnapshot(
                plan_events=self._plan_events,
                tool_calls=len(self._tool_calls),
                terminal_results=len(self._terminal_results),
                partial_results=self._partial_results,
                permission_events=self._permission_events,
                action_events=self._action_events,
                artifact_events=self._artifact_events,
                failures=self._failures,
                event_count=len(self._events),
            )

    def assert_complete(self, plan: BrowserActionPlan, outcomes: Sequence[StepOutcome]) -> None:
        attempted = {outcome.action_id for outcome in outcomes}
        with self._lock:
            calls = set(self._tool_calls)
            terminals = set(self._terminal_results)
        if attempted != terminals or not terminals.issubset(calls):
            raise BrowserActionEventWriterError(
                "tool_result_pairing_failed",
                "browser action tool-call/result pairing is incomplete",
                details={
                    "attempted": sorted(attempted),
                    "calls": sorted(calls),
                    "terminals": sorted(terminals),
                    "plan_action_ids": list(plan.action_ids),
                },
            )

    def _ensure_tool_call(self, action_id: str) -> None:
        with self._lock:
            if action_id not in self._tool_calls:
                raise BrowserActionEventWriterError("tool_call_missing", "browser action has no causal tool call")

    @staticmethod
    def _validate_event(event: EventRecord) -> None:
        if not isinstance(event, EventRecord):
            raise BrowserActionEventWriterError("event_not_record", "browser action event writer accepts EventRecord only")
        if not event.run_id or not event.task_id or not event.event_id:
            raise BrowserActionEventWriterError("event_identity_missing", "browser action event identity is incomplete")
        SecretRedactor().assert_clean(event.payload)

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserActionEventWriterError("event_writer_disabled", "browser action event writer is disabled")
