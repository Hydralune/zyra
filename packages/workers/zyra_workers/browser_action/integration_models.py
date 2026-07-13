from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from zyra_core import ArtifactRef, EventRecord, now_iso, to_jsonable

from .models import ActionExecutionResult, ActionIdentity, ActionRequest, digest_value, stable_id
from .selector_guard import SelectorExpectation


class BrowserActionIntegrationError(RuntimeError):
    """Typed failure returned by the 04C productized application boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: str = "",
        step_index: int = 0,
        action_id: str = "",
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
        pending: bool = False,
        side_effect_count: int = 0,
        events: Sequence[EventRecord] = (),
    ) -> None:
        self.code = str(code or "browser_action_integration_failed")
        self.phase = str(phase)
        self.step_index = int(step_index)
        self.action_id = str(action_id)
        self.details = dict(details or {})
        self.retryable = bool(retryable)
        self.pending = bool(pending)
        self.side_effect_count = max(0, int(side_effect_count))
        self.events = tuple(events)
        super().__init__(message)

    def public_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "phase": self.phase,
            "step_index": self.step_index,
            "action_id": self.action_id,
            "details": to_jsonable(self.details),
            "retryable": self.retryable,
            "pending": self.pending,
            "side_effect_count": self.side_effect_count,
            "event_ids": [event.event_id for event in self.events],
        }


class PlanPhase(StrEnum):
    RECEIVED = "received"
    STATIC_ADMISSION = "static_admission"
    SECURITY_PREFLIGHT = "security_preflight"
    PERMISSION = "permission"
    PERMISSION_PENDING = "permission_pending"
    RESUME_VALIDATION = "resume_validation"
    DISPATCH = "dispatch"
    RESULT_PROJECTION = "result_projection"
    CONTEXT_CAPTURE = "context_capture"
    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class StepState(StrEnum):
    ACCEPTED = "accepted"
    PREFLIGHTED = "preflighted"
    PENDING = "pending"
    DENIED = "denied"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class DispatchBoundary(StrEnum):
    BEFORE_GRANT = "before_grant"
    AFTER_GRANT_BEFORE_EFFECT = "after_grant_before_effect"
    AFTER_EFFECT = "after_effect"
    RESULT_ONLY = "result_only"


class PlanAdmissionIssueKind(StrEnum):
    SHAPE = "shape"
    UNKNOWN_ACTION = "unknown_action"
    SCHEMA = "schema"
    SELECTOR = "selector"
    BYPASS = "bypass"
    IDENTITY = "identity"
    DEADLINE = "deadline"
    OWNER = "owner"


@dataclass(frozen=True, slots=True)
class PlanAdmissionIssue:
    step_index: int
    action: str
    code: str
    message: str
    kind: PlanAdmissionIssueKind
    path: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.step_index < 0:
            raise ValueError("browser plan issue step index cannot be negative")
        if not self.code or not self.message:
            raise ValueError("browser plan issue requires code and message")
        object.__setattr__(self, "details", dict(self.details))

    def public_dict(self) -> dict[str, Any]:
        return {
            "step_index": self.step_index,
            "action": self.action,
            "code": self.code,
            "message": self.message,
            "kind": str(self.kind),
            "path": self.path,
            "details": to_jsonable(self.details),
        }


@dataclass(frozen=True, slots=True)
class PlanStep:
    index: int
    request: ActionRequest
    selector_expectation: SelectorExpectation | None
    original_action: str
    original_arguments_digest: str
    source: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.index < 1:
            raise ValueError("browser plan step index must be positive")
        if self.request.identity.step_index != self.index:
            raise ValueError("browser plan step and action identity indexes differ")
        if not self.original_action:
            raise ValueError("browser plan step requires original action")
        object.__setattr__(self, "source", dict(self.source))

    @property
    def action_id(self) -> str:
        return self.request.identity.action_id

    @property
    def canonical_action(self) -> str:
        return self.request.action

    @property
    def digest(self) -> str:
        return digest_value(
            {
                "index": self.index,
                "request_digest": self.request.request_digest,
                "selector_expectation": (
                    self.selector_expectation.to_dict() if self.selector_expectation else None
                ),
                "original_action": self.original_action,
                "original_arguments_digest": self.original_arguments_digest,
            }
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action_id": self.action_id,
            "canonical_action": self.canonical_action,
            "original_action": self.original_action,
            "request_digest": self.request.request_digest,
            "step_digest": self.digest,
            "selector_expectation": (
                self.selector_expectation.to_dict() if self.selector_expectation else None
            ),
            "deadline_at": self.request.deadline_at,
        }


@dataclass(frozen=True, slots=True)
class BrowserActionPlan:
    plan_id: str
    run_id: str
    task_id: str
    worker_request_id: str
    browser_session_id: str
    permission_session_id: str
    canonical_session_id: str
    steps: tuple[PlanStep, ...]
    backend: str = "zyra-browser-productized"
    continue_on_error: bool = False
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(self.steps))
        required = (
            self.plan_id,
            self.run_id,
            self.task_id,
            self.worker_request_id,
            self.browser_session_id,
            self.permission_session_id,
            self.canonical_session_id,
            self.backend,
        )
        if any(not str(value).strip() for value in required):
            raise ValueError("browser action plan identity is incomplete")
        if not self.steps:
            raise ValueError("browser action plan requires at least one step")
        if tuple(step.index for step in self.steps) != tuple(range(1, len(self.steps) + 1)):
            raise ValueError("browser action plan step indexes must be contiguous")
        action_ids = tuple(step.action_id for step in self.steps)
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("browser action plan action ids must be unique")
        for step in self.steps:
            identity = step.request.identity
            if identity.run_id != self.run_id or identity.task_id != self.task_id:
                raise ValueError("browser action plan contains another run/task")
            if identity.worker_request_id != self.worker_request_id:
                raise ValueError("browser action plan contains another worker request")
            if identity.browser_session_id != self.browser_session_id:
                raise ValueError("browser action plan contains another browser session")
            if identity.session_id != self.permission_session_id:
                raise ValueError("browser action plan contains another permission session")

    @property
    def digest(self) -> str:
        return digest_value(
            {
                "plan_id": self.plan_id,
                "run_id": self.run_id,
                "task_id": self.task_id,
                "worker_request_id": self.worker_request_id,
                "browser_session_id": self.browser_session_id,
                "permission_session_id": self.permission_session_id,
                "canonical_session_id": self.canonical_session_id,
                "backend": self.backend,
                "continue_on_error": self.continue_on_error,
                "steps": [step.digest for step in self.steps],
            }
        )

    @property
    def action_ids(self) -> tuple[str, ...]:
        return tuple(step.action_id for step in self.steps)

    def step(self, index: int) -> PlanStep:
        if index < 1 or index > len(self.steps):
            raise KeyError(f"browser action step {index} is outside the plan")
        return self.steps[index - 1]

    def public_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "plan_digest": self.digest,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_request_id": self.worker_request_id,
            "browser_session_id": self.browser_session_id,
            "permission_session_id": self.permission_session_id,
            "canonical_session_id": self.canonical_session_id,
            "backend": self.backend,
            "continue_on_error": self.continue_on_error,
            "steps": [step.public_dict() for step in self.steps],
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class PlanAdmission:
    admission_id: str
    plan: BrowserActionPlan | None
    issues: tuple[PlanAdmissionIssue, ...]
    registry_digest: str
    schema_projection_digest: str
    bypass_scan_digest: str
    admitted_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", tuple(self.issues))
        if not self.admission_id or not self.registry_digest or not self.schema_projection_digest:
            raise ValueError("browser plan admission identity is incomplete")

    @property
    def ok(self) -> bool:
        return self.plan is not None and not self.issues

    def require(self) -> BrowserActionPlan:
        if not self.ok or self.plan is None:
            raise BrowserActionIntegrationError(
                "browser_plan_admission_failed",
                "browser action plan failed all-actions-first static admission",
                phase=PlanPhase.STATIC_ADMISSION,
                details={"issues": [issue.public_dict() for issue in self.issues]},
            )
        return self.plan

    def public_dict(self) -> dict[str, Any]:
        return {
            "admission_id": self.admission_id,
            "ok": self.ok,
            "plan": self.plan.public_dict() if self.plan else None,
            "issues": [issue.public_dict() for issue in self.issues],
            "registry_digest": self.registry_digest,
            "schema_projection_digest": self.schema_projection_digest,
            "bypass_scan_digest": self.bypass_scan_digest,
            "admitted_at": self.admitted_at,
        }


@dataclass(frozen=True, slots=True)
class PendingActionCheckpoint:
    checkpoint_id: str
    plan_id: str
    plan_digest: str
    action_id: str
    step_index: int
    permission_request_id: str
    permission_tool_use_id: str
    permission_session_id: str
    browser_session_id: str
    canonical_session_id: str
    request_digest: str
    preflight_receipt_id: str
    selector_expectation_digest: str = ""
    target_id: str = ""
    target_generation: int = 0
    cdp_session_id: str = ""
    cdp_generation: int = 0
    selector_revision_id: str = ""
    state_revision: int = 1
    expires_at: str = ""
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        required = (
            self.checkpoint_id,
            self.plan_id,
            self.plan_digest,
            self.action_id,
            self.permission_request_id,
            self.permission_tool_use_id,
            self.permission_session_id,
            self.browser_session_id,
            self.canonical_session_id,
            self.request_digest,
            self.preflight_receipt_id,
        )
        if any(not str(value).strip() for value in required):
            raise ValueError("pending browser action checkpoint identity is incomplete")
        if self.step_index < 1 or self.state_revision < 1:
            raise ValueError("pending browser action checkpoint revisions must be positive")
        if min(self.target_generation, self.cdp_generation) < 0:
            raise ValueError("pending browser action generations cannot be negative")

    @property
    def expired(self) -> bool:
        if not self.expires_at:
            return False
        return parse_timestamp(self.expires_at) <= dt.datetime.now(dt.UTC)

    @property
    def binding_digest(self) -> str:
        return digest_value(
            {
                "browser_session_id": self.browser_session_id,
                "target_id": self.target_id,
                "target_generation": self.target_generation,
                "cdp_session_id": self.cdp_session_id,
                "cdp_generation": self.cdp_generation,
                "selector_revision_id": self.selector_revision_id,
                "selector_expectation_digest": self.selector_expectation_digest,
            }
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "expired": self.expired,
            "binding_digest": self.binding_digest,
        }


@dataclass(frozen=True, slots=True)
class StepOutcome:
    action_id: str
    step_index: int
    action: str
    state: StepState
    request_digest: str
    preflight_receipt_id: str = ""
    permission_decision_id: str = ""
    permission_request_id: str = ""
    permission_tool_use_id: str = ""
    result: ActionExecutionResult | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    events: tuple[EventRecord, ...] = ()
    error_code: str = ""
    error_message: str = ""
    failure_phase: str = ""
    dispatch_boundary: DispatchBoundary = DispatchBoundary.BEFORE_GRANT
    retryable: bool = False
    started_at: str = field(default_factory=now_iso)
    completed_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "events", tuple(self.events))
        if not self.action_id or self.step_index < 1 or not self.action or not self.request_digest:
            raise ValueError("browser step outcome identity is incomplete")
        if self.state == StepState.SUCCEEDED and (self.result is None or not self.result.ok):
            raise ValueError("successful browser step requires a successful action result")
        if self.state in {StepState.FAILED, StepState.DENIED, StepState.CANCELLED} and not self.error_code:
            raise ValueError("failed browser step requires an error code")

    @property
    def ok(self) -> bool:
        return self.state == StepState.SUCCEEDED and bool(self.result and self.result.ok)

    @property
    def pending(self) -> bool:
        return self.state == StepState.PENDING

    @property
    def side_effect_count(self) -> int:
        return int(self.result.side_effect_count if self.result else 0)

    @property
    def cdp_effect_count(self) -> int:
        return int(self.result.cdp_effect_count if self.result else 0)

    @property
    def file_effect_count(self) -> int:
        return int(self.result.file_effect_count if self.result else 0)

    @property
    def network_effect_count(self) -> int:
        return int(self.result.network_effect_count if self.result else 0)

    @property
    def receipt_id(self) -> str:
        return self.preflight_receipt_id

    def public_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "step_index": self.step_index,
            "action": self.action,
            "state": str(self.state),
            "ok": self.ok,
            "pending": self.pending,
            "request_digest": self.request_digest,
            "preflight_receipt_id": self.preflight_receipt_id,
            "permission_decision_id": self.permission_decision_id,
            "permission_request_id": self.permission_request_id,
            "permission_tool_use_id": self.permission_tool_use_id,
            "result": self.result.public_dict() if self.result else None,
            "artifact_ids": [artifact.artifact_id for artifact in self.artifacts],
            "event_ids": [event.event_id for event in self.events],
            "error_code": self.error_code,
            "error_message": self.error_message,
            "failure_phase": self.failure_phase,
            "dispatch_boundary": str(self.dispatch_boundary),
            "retryable": self.retryable,
            "side_effect_count": self.side_effect_count,
            "cdp_effect_count": self.cdp_effect_count,
            "network_effect_count": self.network_effect_count,
            "file_effect_count": self.file_effect_count,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class PlanExecutionResult:
    execution_id: str
    plan: BrowserActionPlan
    phase: PlanPhase
    outcomes: tuple[StepOutcome, ...]
    events: tuple[EventRecord, ...]
    artifacts: tuple[ArtifactRef, ...]
    pending_checkpoint: PendingActionCheckpoint | None = None
    error_code: str = ""
    error_message: str = ""
    started_at: str = field(default_factory=now_iso)
    completed_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "outcomes", tuple(self.outcomes))
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        if not self.execution_id:
            raise ValueError("browser plan execution requires an id")
        if len({event.event_id for event in self.events}) != len(self.events):
            raise ValueError("browser plan execution contains duplicate event ids")
        if len({artifact.artifact_id for artifact in self.artifacts}) != len(self.artifacts):
            raise ValueError("browser plan execution contains duplicate artifact ids")
        if self.pending_checkpoint and self.phase != PlanPhase.PERMISSION_PENDING:
            raise ValueError("pending checkpoint requires permission_pending plan phase")
        if self.phase == PlanPhase.COMPLETED and not self.ok:
            raise ValueError("completed browser plan contains non-success outcome")

    @property
    def ok(self) -> bool:
        return (
            self.phase == PlanPhase.COMPLETED
            and len(self.outcomes) == len(self.plan.steps)
            and all(outcome.ok for outcome in self.outcomes)
        )

    @property
    def pending(self) -> bool:
        return self.phase == PlanPhase.PERMISSION_PENDING and self.pending_checkpoint is not None

    @property
    def partial(self) -> bool:
        return self.phase == PlanPhase.PARTIAL or (
            bool(self.outcomes) and any(outcome.ok for outcome in self.outcomes) and not self.ok
        )

    @property
    def action_execution_count(self) -> int:
        return sum(outcome.ok for outcome in self.outcomes)

    @property
    def side_effect_count(self) -> int:
        return sum(outcome.side_effect_count for outcome in self.outcomes)

    def public_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "plan_id": self.plan.plan_id,
            "plan_digest": self.plan.digest,
            "phase": str(self.phase),
            "ok": self.ok,
            "pending": self.pending,
            "partial": self.partial,
            "outcomes": [outcome.public_dict() for outcome in self.outcomes],
            "event_ids": [event.event_id for event in self.events],
            "artifact_ids": [artifact.artifact_id for artifact in self.artifacts],
            "pending_checkpoint": self.pending_checkpoint.public_dict() if self.pending_checkpoint else None,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "action_execution_count": self.action_execution_count,
            "side_effect_count": self.side_effect_count,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


def create_plan_id(
    *,
    run_id: str,
    task_id: str,
    worker_request_id: str,
    browser_session_id: str,
    step_digests: Sequence[str],
) -> str:
    return stable_id(
        "bractionplan",
        run_id,
        task_id,
        worker_request_id,
        browser_session_id,
        tuple(step_digests),
    )


def create_execution_id(plan: BrowserActionPlan, attempt: int = 1) -> str:
    if attempt < 1:
        raise ValueError("browser action execution attempt must be positive")
    return stable_id("bractionexec", plan.plan_id, plan.digest, attempt)


def create_checkpoint_id(
    plan: BrowserActionPlan,
    *,
    action_id: str,
    permission_request_id: str,
    permission_tool_use_id: str,
) -> str:
    return stable_id(
        "bractionpending",
        plan.plan_id,
        plan.digest,
        action_id,
        permission_request_id,
        permission_tool_use_id,
    )


def parse_timestamp(value: str) -> dt.datetime:
    normalized = str(value).strip().replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def selector_expectation_from_dict(raw: Mapping[str, Any]) -> SelectorExpectation:
    from zyra_workers.browser_state.contracts import SelectorMapIdentity

    identity_raw = raw.get("identity")
    if not isinstance(identity_raw, Mapping):
        raise ValueError("selector expectation identity is required")
    return SelectorExpectation(
        selector_ref=str(raw.get("selector_ref") or ""),
        identity=SelectorMapIdentity(**dict(identity_raw)),
        revision_id=str(raw.get("revision_id") or ""),
        selector_index=_optional_int(raw.get("selector_index")),
        backend_node_id=_optional_int(raw.get("backend_node_id")),
        frame_id=str(raw.get("frame_id") or ""),
        stable_hash=str(raw.get("stable_hash") or ""),
        attributes_digest=str(raw.get("attributes_digest") or ""),
        require_visible=bool(raw.get("require_visible", True)),
        require_interactive=bool(raw.get("require_interactive", True)),
        allow_disabled=bool(raw.get("allow_disabled", False)),
        expected_tag=str(raw.get("expected_tag") or ""),
        expected_role=str(raw.get("expected_role") or ""),
    )


def action_identity_from_dict(raw: Mapping[str, Any]) -> ActionIdentity:
    return ActionIdentity(
        run_id=str(raw.get("run_id") or ""),
        task_id=str(raw.get("task_id") or ""),
        worker_request_id=str(raw.get("worker_request_id") or ""),
        session_id=str(raw.get("session_id") or ""),
        browser_session_id=str(raw.get("browser_session_id") or ""),
        step_index=int(raw.get("step_index") or 0),
        node_id=str(raw.get("node_id") or ""),
        turn_id=str(raw.get("turn_id") or ""),
        action_id=str(raw.get("action_id") or ""),
    )


def action_request_from_dict(raw: Mapping[str, Any]) -> ActionRequest:
    identity_raw = raw.get("identity")
    if not isinstance(identity_raw, Mapping):
        raise ValueError("serialized browser action request has no identity")
    selector_raw = raw.get("selector")
    selector = None
    if isinstance(selector_raw, Mapping):
        from .models import SelectorBinding

        selector = SelectorBinding(**dict(selector_raw))
    return ActionRequest(
        identity=action_identity_from_dict(identity_raw),
        action=str(raw.get("action") or ""),
        arguments=dict(raw.get("arguments") or {}),
        backend=str(raw.get("backend") or "zyra-browser-productized"),
        current_url=str(raw.get("current_url") or ""),
        target_url=str(raw.get("target_url") or ""),
        selector=selector,
        deadline_at=str(raw.get("deadline_at") or ""),
        metadata=dict(raw.get("metadata") or {}),
        received_at=str(raw.get("received_at") or now_iso()),
    )


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)
