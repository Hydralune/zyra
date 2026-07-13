from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from zyra_core import EventRecord, EventType, now_iso
from zyra_runtime import WorkerRequest

from .deadline_runtime import ActionDeadline, BrowserActionDeadlineRuntime, CancellationRecord
from .integration_models import BrowserActionIntegrationError, PlanPhase
from .models import digest_value, stable_id
from .secret_policy import SecretRedactor


class BrowserActionControlKind(StrEnum):
    CANCEL = "cancel"
    INSPECT = "inspect"


class BrowserActionControlStatus(StrEnum):
    APPLIED = "applied"
    OBSERVED = "observed"
    REPLAYED = "replayed"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class BrowserActionControlCommand:
    command_id: str
    kind: BrowserActionControlKind
    run_id: str
    task_id: str
    worker_request_id: str
    browser_session_id: str = ""
    action_ids: tuple[str, ...] = ()
    target_worker_request_id: str = ""
    reason: str = ""
    actor_id: str = "control-plane"
    expected_revisions: Mapping[str, int] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        required = (self.command_id, self.run_id, self.task_id, self.worker_request_id)
        if any(not str(value).strip() for value in required):
            raise ValueError("browser action control identity is incomplete")
        action_ids = tuple(dict.fromkeys(str(item).strip() for item in self.action_ids if str(item).strip()))
        revisions = {str(key): int(value) for key, value in dict(self.expected_revisions).items()}
        if any(value < 0 for value in revisions.values()):
            raise ValueError("browser action control revisions cannot be negative")
        if self.kind is BrowserActionControlKind.CANCEL and not action_ids and not self.target_worker_request_id:
            raise ValueError("browser action cancel requires action_ids or target_worker_request_id")
        object.__setattr__(self, "action_ids", action_ids)
        object.__setattr__(self, "expected_revisions", revisions)
        object.__setattr__(self, "reason", str(self.reason or "cancelled by browser action control")[:1000])
        object.__setattr__(self, "actor_id", str(self.actor_id or "control-plane")[:256])

    @property
    def fingerprint(self) -> str:
        return digest_value(
            {
                "kind": str(self.kind),
                "run_id": self.run_id,
                "task_id": self.task_id,
                "browser_session_id": self.browser_session_id,
                "action_ids": list(self.action_ids),
                "target_worker_request_id": self.target_worker_request_id,
                "reason": self.reason,
                "actor_id": self.actor_id,
                "expected_revisions": dict(sorted(self.expected_revisions.items())),
            }
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "kind": str(self.kind),
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_request_id": self.worker_request_id,
            "browser_session_id": self.browser_session_id,
            "action_ids": list(self.action_ids),
            "target_worker_request_id": self.target_worker_request_id,
            "reason": self.reason,
            "actor_id": self.actor_id,
            "expected_revisions": dict(self.expected_revisions),
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
        }

    @classmethod
    def from_worker_request(cls, request: WorkerRequest) -> BrowserActionControlCommand | None:
        constraints = request.constraints if isinstance(request.constraints, Mapping) else {}
        raw = constraints.get("browser_action_control")
        payload = dict(raw) if isinstance(raw, Mapping) else {}
        kind_value = payload.get("command") or payload.get("kind") or constraints.get("browser_action_control_command")
        if kind_value in (None, ""):
            return None
        normalized = str(kind_value).strip().casefold().replace("_", "-")
        aliases = {
            "cancel": BrowserActionControlKind.CANCEL,
            "cancel-action": BrowserActionControlKind.CANCEL,
            "cancel-actions": BrowserActionControlKind.CANCEL,
            "inspect": BrowserActionControlKind.INSPECT,
            "status": BrowserActionControlKind.INSPECT,
        }
        kind = aliases.get(normalized)
        if kind is None:
            raise BrowserActionIntegrationError(
                "browser_action_control_unknown",
                f"unknown browser action control command {kind_value!r}",
                phase=PlanPhase.CANCELLED,
            )
        action_ids = _strings(payload.get("action_ids") or constraints.get("browser_action_ids"))
        scalar_action = payload.get("action_id") or constraints.get("browser_action_id")
        if scalar_action:
            action_ids = tuple(dict.fromkeys((*action_ids, str(scalar_action).strip())))
        target_request = str(
            payload.get("target_worker_request_id")
            or constraints.get("browser_action_target_worker_request_id")
            or ""
        ).strip()
        session_id = str(
            payload.get("browser_session_id")
            or constraints.get("browser_session_id")
            or ""
        ).strip()
        reason = str(payload.get("reason") or constraints.get("reason") or "")
        actor_id = str(payload.get("actor_id") or request.node_id or "control-plane")
        revisions = payload.get("expected_revisions")
        revisions = dict(revisions) if isinstance(revisions, Mapping) else {}
        supplied_id = str(payload.get("command_id") or constraints.get("browser_action_control_id") or "").strip()
        command_id = supplied_id or stable_id(
            "brcontrol",
            request.run_id,
            request.task_id,
            request.request_id,
            kind,
            action_ids,
            target_request,
        )
        return cls(
            command_id=command_id,
            kind=kind,
            run_id=request.run_id,
            task_id=request.task_id,
            worker_request_id=request.request_id,
            browser_session_id=session_id,
            action_ids=action_ids,
            target_worker_request_id=target_request,
            reason=reason,
            actor_id=actor_id,
            expected_revisions=revisions,
        )


@dataclass(frozen=True, slots=True)
class BrowserActionControlResult:
    command: BrowserActionControlCommand
    status: BrowserActionControlStatus
    records: tuple[CancellationRecord, ...]
    active_action_ids: tuple[str, ...]
    events: tuple[EventRecord, ...]
    error_code: str = ""
    error_message: str = ""
    replayed: bool = False
    completed_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "active_action_ids", tuple(self.active_action_ids))
        object.__setattr__(self, "events", tuple(self.events))
        if self.ok and self.error_code:
            raise ValueError("successful browser action control result cannot contain error_code")

    @property
    def ok(self) -> bool:
        return self.status is not BrowserActionControlStatus.REJECTED

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-action.control-result.v1",
            "command": self.command.public_dict(),
            "status": str(self.status),
            "ok": self.ok,
            "records": [record.public_dict() for record in self.records],
            "active_action_ids": list(self.active_action_ids),
            "event_ids": [event.event_id for event in self.events],
            "error_code": self.error_code,
            "error_message": self.error_message,
            "replayed": self.replayed,
            "completed_at": self.completed_at,
        }


class BrowserActionControlRuntime:
    """Cancellation-only worker control over active 04C deadlines.

    The durable API event is a control-plane fact. The in-process cancellation
    signal remains intentionally subordinate to the canonical task/session
    control owners and cannot create, authorize, resume, or replay an action.
    """

    def __init__(
        self,
        deadlines: BrowserActionDeadlineRuntime,
        *,
        maximum_idempotency_records: int = 4096,
        disabled: bool = False,
    ) -> None:
        if deadlines is None or maximum_idempotency_records < 1:
            raise ValueError("browser action control requires deadline owner and positive retention")
        self.deadlines = deadlines
        self.maximum_idempotency_records = maximum_idempotency_records
        self.disabled = disabled
        self._results: dict[str, BrowserActionControlResult] = {}
        self._fingerprints: dict[str, str] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._applied = 0
        self._inspected = 0
        self._rejected = 0
        self._replayed = 0

    def apply(self, command: BrowserActionControlCommand) -> BrowserActionControlResult:
        self._ensure_available()
        if not isinstance(command, BrowserActionControlCommand):
            raise TypeError("browser action control accepts BrowserActionControlCommand only")
        with self._lock:
            existing = self._results.get(command.command_id)
            fingerprint = self._fingerprints.get(command.command_id)
            if existing is not None:
                if fingerprint != command.fingerprint:
                    self._rejected += 1
                    error = BrowserActionIntegrationError(
                        "browser_action_control_idempotency_conflict",
                        "browser action control id was reused with another body",
                        phase=PlanPhase.CANCELLED,
                        details={"command_id": command.command_id},
                    )
                    return self._rejection(command, error)
                self._replayed += 1
                return replace(existing, status=BrowserActionControlStatus.REPLAYED, replayed=True)
        try:
            result = self._apply_once(command)
        except Exception as exc:
            self._rejected += 1
            result = self._rejection(command, exc)
        self._remember(result)
        return result

    def _apply_once(self, command: BrowserActionControlCommand) -> BrowserActionControlResult:
        active = self.deadlines.active()
        owned = tuple(deadline for deadline in active if self._owned(command, deadline))
        selected = self._select(command, owned)
        if command.kind is BrowserActionControlKind.INSPECT:
            self._inspected += 1
            event = self._event(command, status=BrowserActionControlStatus.OBSERVED, deadlines=selected, records=())
            return BrowserActionControlResult(
                command,
                BrowserActionControlStatus.OBSERVED,
                (),
                tuple(item.action_id for item in selected),
                (event,),
            )
        if not selected:
            raise BrowserActionIntegrationError(
                "browser_action_control_target_not_active",
                "browser action control matched no active action owned by this run/task/session",
                phase=PlanPhase.CANCELLED,
                details={
                    "requested_action_ids": list(command.action_ids),
                    "target_worker_request_id": command.target_worker_request_id,
                },
            )
        records: list[CancellationRecord] = []
        for deadline in selected:
            expected = command.expected_revisions.get(deadline.action_id)
            records.append(
                self.deadlines.cancellation.request_cancel(
                    deadline.action_id,
                    reason=command.reason,
                    actor_id=command.actor_id,
                    expected_revision=expected,
                )
            )
        self._applied += 1
        event = self._event(
            command,
            status=BrowserActionControlStatus.APPLIED,
            deadlines=selected,
            records=records,
        )
        return BrowserActionControlResult(
            command,
            BrowserActionControlStatus.APPLIED,
            tuple(records),
            tuple(item.action_id for item in active),
            (event,),
        )

    @staticmethod
    def _owned(command: BrowserActionControlCommand, deadline: ActionDeadline) -> bool:
        identity = deadline.request.identity
        return (
            identity.run_id == command.run_id
            and identity.task_id == command.task_id
            and (not command.browser_session_id or identity.browser_session_id == command.browser_session_id)
        )

    @staticmethod
    def _select(
        command: BrowserActionControlCommand,
        owned: Sequence[ActionDeadline],
    ) -> tuple[ActionDeadline, ...]:
        requested = set(command.action_ids)
        selected = tuple(
            deadline
            for deadline in owned
            if (not requested or deadline.action_id in requested)
            and (
                not command.target_worker_request_id
                or deadline.request.identity.worker_request_id == command.target_worker_request_id
            )
        )
        if requested:
            missing = requested.difference(item.action_id for item in selected)
            if missing:
                raise BrowserActionIntegrationError(
                    "browser_action_control_identity_mismatch",
                    "browser action control contains an unknown or foreign action id",
                    phase=PlanPhase.CANCELLED,
                    details={"missing_action_ids": sorted(missing)},
                )
        return selected

    def _rejection(
        self,
        command: BrowserActionControlCommand,
        error: Exception,
    ) -> BrowserActionControlResult:
        code = str(getattr(error, "code", "browser_action_control_rejected"))
        message = SecretRedactor().redact(str(error))
        event = self._event(
            command,
            status=BrowserActionControlStatus.REJECTED,
            deadlines=(),
            records=(),
            error_code=code,
            error_message=message,
        )
        return BrowserActionControlResult(
            command,
            BrowserActionControlStatus.REJECTED,
            (),
            tuple(item.action_id for item in self.deadlines.active()),
            (event,),
            error_code=code,
            error_message=message,
        )

    @staticmethod
    def _event(
        command: BrowserActionControlCommand,
        *,
        status: BrowserActionControlStatus,
        deadlines: Sequence[ActionDeadline],
        records: Sequence[CancellationRecord],
        error_code: str = "",
        error_message: str = "",
    ) -> EventRecord:
        payload = {
            "browser_action_control": {
                "schema": "zyra.browser-action.control.v1",
                "command_id": command.command_id,
                "command_fingerprint": command.fingerprint,
                "kind": str(command.kind),
                "status": str(status),
                "worker_request_id": command.worker_request_id,
                "browser_session_id": command.browser_session_id,
                "target_worker_request_id": command.target_worker_request_id,
                "action_ids": [item.action_id for item in deadlines],
                "cancellations": [record.public_dict() for record in records],
                "reason": command.reason,
                "actor_id": command.actor_id,
                "error_code": error_code,
                "error_message": error_message,
                "authorizes_execution": False,
                "fallback_allowed": False,
            }
        }
        SecretRedactor().assert_clean(payload)
        return EventRecord(
            run_id=command.run_id,
            task_id=command.task_id,
            event_type=EventType.CONTROL_COMMAND,
            payload=payload,
        )

    def _remember(self, result: BrowserActionControlResult) -> None:
        command_id = result.command.command_id
        with self._lock:
            self._results[command_id] = result
            self._fingerprints[command_id] = result.command.fingerprint
            self._order.append(command_id)
            while len(self._order) > self.maximum_idempotency_records:
                removed = self._order.pop(0)
                self._results.pop(removed, None)
                self._fingerprints.pop(removed, None)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            retained = len(self._results)
        return {
            "runtime_id": "zyra-browser-action-control-runtime",
            "owner_unit": "M1-S04C-02",
            "canonical_task_control_owner": "M1-03D/M1-04A",
            "disabled": self.disabled,
            "retained_idempotency_records": retained,
            "applied": self._applied,
            "inspected": self._inspected,
            "rejected": self._rejected,
            "replayed": self._replayed,
            "deadline_runtime": self.deadlines.snapshot(),
        }

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserActionIntegrationError(
                "browser_action_control_disabled",
                "browser action control runtime is disabled",
                phase=PlanPhase.CANCELLED,
            )


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()
