from __future__ import annotations

import copy
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from zyra_core import ArtifactKind, ArtifactRef, EventRecord, EventType, now_iso

from .models import (
    ActionExecutionResult,
    ActionFailureKind,
    ActionIdentity,
    ActionPhase,
    ActionPreflightReceipt,
    ActionRequest,
    ActionTransition,
    digest_value,
    stable_id,
)
from .secret_policy import SecretRedactor


class BrowserActionEventError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


class ArtifactPort(Protocol):
    def write(
        self,
        *,
        kind: ArtifactKind,
        title: str,
        content: bytes,
        metadata: Mapping[str, Any],
    ) -> ArtifactRef: ...


@dataclass(slots=True)
class MemoryArtifactPort:
    artifacts: dict[str, tuple[ArtifactRef, bytes]] = field(default_factory=dict)

    def write(
        self,
        *,
        kind: ArtifactKind,
        title: str,
        content: bytes,
        metadata: Mapping[str, Any],
    ) -> ArtifactRef:
        artifact_id = stable_id("brartifact", kind, title, digest_value(content.hex()), len(self.artifacts))
        reference = ArtifactRef(
            artifact_id=artifact_id,
            kind=kind,
            uri=f"memory://browser-action/{artifact_id}",
            title=title,
            metadata={**dict(metadata), "size": len(content), "sha256": digest_value(content.hex()).removeprefix("sha256:")},
        )
        self.artifacts[artifact_id] = (reference, bytes(content))
        return reference

    def read(self, artifact_id: str) -> bytes:
        return self.artifacts[artifact_id][1]


@dataclass(frozen=True, slots=True)
class ResultProjection:
    result: ActionExecutionResult
    public_output: Mapping[str, Any]
    artifacts: tuple[ArtifactRef, ...]
    inline_chars: int
    externalized_chars: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "public_output", dict(self.public_output))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))


class BrowserActionResultProjector:
    """Apply per-action result budgets before event and trajectory emission."""

    def __init__(self, artifact_port: ArtifactPort | None = None, *, disabled: bool = False) -> None:
        self.artifact_port = artifact_port
        self.disabled = disabled

    def project(
        self,
        *,
        request: ActionRequest,
        receipt: ActionPreflightReceipt,
        result: ActionExecutionResult,
        redactor: SecretRedactor,
    ) -> ResultProjection:
        if self.disabled:
            raise BrowserActionEventError("result_projector_disabled", "browser action result projector is disabled")
        safe_output = redactor.redact(result.output)
        redactor.assert_clean(safe_output)
        encoded = json.dumps(safe_output, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        budget = receipt.definition.result_budget_chars
        artifacts: list[ArtifactRef] = []
        public_output: dict[str, Any]
        externalized = 0
        if len(encoded) <= budget:
            public_output = copy.deepcopy(dict(safe_output))
        else:
            if self.artifact_port is None:
                public_output = {
                    "externalization_required": True,
                    "content_digest": digest_value(safe_output),
                    "original_chars": len(encoded),
                    "budget_chars": budget,
                }
            else:
                metadata = causal_metadata(request.identity, receipt)
                reference = self.artifact_port.write(
                    kind=ArtifactKind.STRUCTURED_DATA,
                    title=f"Browser action {receipt.definition.name} result",
                    content=encoded.encode("utf-8"),
                    metadata=metadata,
                )
                artifacts.append(reference)
                public_output = {
                    "externalized": True,
                    "artifact_id": reference.artifact_id,
                    "content_digest": digest_value(safe_output),
                    "original_chars": len(encoded),
                    "budget_chars": budget,
                }
            externalized = len(encoded)
        projected = ActionExecutionResult(
            action_id=result.action_id,
            ok=result.ok,
            summary=redactor.redact(result.summary),
            output=public_output,
            artifact_ids=tuple(dict.fromkeys((*result.artifact_ids, *(item.artifact_id for item in artifacts)))),
            error_code=result.error_code,
            error_message=redactor.redact(result.error_message),
            failure_kind=result.failure_kind,
            side_effect_count=result.side_effect_count,
            network_effect_count=result.network_effect_count,
            file_effect_count=result.file_effect_count,
            cdp_effect_count=result.cdp_effect_count,
            completed_at=result.completed_at,
        )
        return ResultProjection(projected, public_output, tuple(artifacts), len(json.dumps(public_output)), externalized)


class BrowserActionEventPort:
    """Causal event adapter; the canonical event log remains the state owner."""

    def __init__(
        self,
        sink: Callable[[EventRecord], None] | None = None,
        *,
        disabled: bool = False,
    ) -> None:
        self.sink = sink
        self.disabled = disabled
        self._transitions: dict[str, list[ActionTransition]] = {}
        self._events: dict[str, list[EventRecord]] = {}
        self._lock = threading.RLock()

    def ensure_available(self) -> None:
        if self.disabled:
            raise BrowserActionEventError("event_port_disabled", "browser action event port is disabled")

    def start(self, request: ActionRequest, *, public_arguments: Mapping[str, Any]) -> EventRecord:
        self.ensure_available()
        if request.identity.action_id in self._transitions:
            raise BrowserActionEventError("action_already_started", "browser action already has an active event chain")
        return self.transition(
            request.identity,
            ActionPhase.RECEIVED,
            details={
                "action": request.action,
                "backend": request.backend,
                "arguments": dict(public_arguments),
                "arguments_digest": digest_value(public_arguments),
                "request_digest": request.request_digest,
            },
        )

    def transition(
        self,
        identity: ActionIdentity,
        phase: ActionPhase,
        *,
        details: Mapping[str, Any] | None = None,
        cause_event_id: str = "",
    ) -> EventRecord:
        self.ensure_available()
        with self._lock:
            transitions = self._transitions.setdefault(identity.action_id, [])
            events = self._events.setdefault(identity.action_id, [])
            expected_cause = events[-1].event_id if events else ""
            if cause_event_id and expected_cause and cause_event_id != expected_cause:
                raise BrowserActionEventError(
                    "invalid_causal_edge",
                    "browser action transition cause is not the previous event",
                    details={"expected": expected_cause, "actual": cause_event_id},
                )
            selected_cause = cause_event_id or expected_cause
            transition = ActionTransition(
                action_id=identity.action_id,
                sequence=len(transitions) + 1,
                phase=phase,
                cause_id=selected_cause,
                details=dict(details or {}),
            )
            self._validate_phase(transitions, phase)
            event = EventRecord(
                run_id=identity.run_id,
                task_id=identity.task_id,
                node_id=identity.node_id or None,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "browser_action": {
                        "schema": "zyra.browser-action.transition.v1",
                        "transition": transition.to_dict(),
                        "identity": identity.to_dict(),
                        "worker_request_id": identity.worker_request_id,
                        "browser_session_id": identity.browser_session_id,
                        "cause_event_id": selected_cause,
                    }
                },
            )
            transitions.append(transition)
            events.append(event)
        if self.sink is not None:
            self.sink(event)
        return event

    def permission_events(self, identity: ActionIdentity, events: Sequence[EventRecord]) -> tuple[EventRecord, ...]:
        self.ensure_available()
        output: list[EventRecord] = []
        for event in events:
            safe = EventRecord(
                run_id=event.run_id,
                task_id=event.task_id,
                node_id=event.node_id,
                event_type=event.event_type,
                event_id=event.event_id,
                created_at=event.created_at,
                payload=SecretRedactor().redact(event.payload),
            )
            output.append(safe)
            if self.sink is not None:
                self.sink(safe)
        return tuple(output)

    def success(
        self,
        identity: ActionIdentity,
        receipt: ActionPreflightReceipt,
        projection: ResultProjection,
    ) -> tuple[EventRecord, ...]:
        result_event = self.transition(
            identity,
            ActionPhase.SUCCEEDED,
            details={
                "preflight_receipt_id": receipt.receipt_id,
                "permission_decision_id": receipt.permission_decision_id,
                "permission_request_id": receipt.permission_request_id,
                "permission_tool_use_id": receipt.permission_tool_use_id,
                "result": projection.result.public_dict(),
                "inline_chars": projection.inline_chars,
                "externalized_chars": projection.externalized_chars,
            },
        )
        artifact_events: list[EventRecord] = []
        for artifact in projection.artifacts:
            event = EventRecord(
                run_id=identity.run_id,
                task_id=identity.task_id,
                node_id=identity.node_id or None,
                event_type=EventType.ARTIFACT_WRITTEN,
                payload={
                    "browser_action": {
                        "schema": "zyra.browser-action.artifact.v1",
                        "action_id": identity.action_id,
                        "cause_event_id": result_event.event_id,
                        "receipt_id": receipt.receipt_id,
                        "artifact": {
                            "artifact_id": artifact.artifact_id,
                            "kind": str(artifact.kind),
                            "uri": artifact.uri,
                            "title": artifact.title,
                            "metadata": copy.deepcopy(artifact.metadata),
                        },
                    }
                },
            )
            artifact_events.append(event)
            if self.sink is not None:
                self.sink(event)
        return (result_event, *artifact_events)

    def failure(
        self,
        identity: ActionIdentity,
        *,
        failure_kind: ActionFailureKind,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
        blocked: bool = True,
    ) -> tuple[EventRecord, EventRecord]:
        redactor = SecretRedactor()
        failure = self.transition(
            identity,
            ActionPhase.BLOCKED if blocked else ActionPhase.FAILED,
            details={
                "failure_kind": str(failure_kind),
                "code": code,
                "message": redactor.redact(message),
                "details": redactor.redact(dict(details or {})),
                "side_effect_count": 0 if blocked else None,
            },
        )
        recovery = EventRecord(
            run_id=identity.run_id,
            task_id=identity.task_id,
            node_id=identity.node_id or None,
            event_type=EventType.RECOVERY_PLANNED,
            payload={
                "browser_action": {
                    "schema": "zyra.browser-action.recovery.v1",
                    "action_id": identity.action_id,
                    "cause_event_id": failure.event_id,
                    "failure_kind": str(failure_kind),
                    "code": code,
                    "retry_requires_new_preflight": True,
                    "reuse_permission_grant": False,
                }
            },
        )
        if self.sink is not None:
            self.sink(recovery)
        return failure, recovery

    def events(self, action_id: str) -> tuple[EventRecord, ...]:
        with self._lock:
            return tuple(self._events.get(action_id, ()))

    @staticmethod
    def _validate_phase(existing: Sequence[ActionTransition], phase: ActionPhase) -> None:
        if not existing and phase != ActionPhase.RECEIVED:
            raise BrowserActionEventError("action_not_started", "browser action event chain must start at received")
        if existing and existing[-1].phase in {ActionPhase.SUCCEEDED, ActionPhase.FAILED, ActionPhase.BLOCKED}:
            raise BrowserActionEventError("action_already_terminal", "browser action event chain is already terminal")


def causal_metadata(identity: ActionIdentity, receipt: ActionPreflightReceipt) -> dict[str, Any]:
    return {
        "action_id": identity.action_id,
        "run_id": identity.run_id,
        "task_id": identity.task_id,
        "worker_request_id": identity.worker_request_id,
        "browser_session_id": identity.browser_session_id,
        "receipt_id": receipt.receipt_id,
        "permission_decision_id": receipt.permission_decision_id,
        "permission_request_id": receipt.permission_request_id,
        "permission_tool_use_id": receipt.permission_tool_use_id,
        "created_at": now_iso(),
    }
