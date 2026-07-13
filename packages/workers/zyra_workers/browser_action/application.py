from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from zyra_core import EventRecord, EventType, to_jsonable
from zyra_runtime import LocalArtifactStore, WorkerRequest, WorkerResult

from zyra_workers.browser_session.application import BrowserSessionApplicationResult
from zyra_workers.browser_session.integration_models import BrowserApplicationResult

from .continuation_runtime import (
    BrowserActionContinuationRuntime,
    BrowserContinuationClaim,
    BrowserContinuationPayloadStore,
)
from .control_runtime import BrowserActionControlCommand, BrowserActionControlResult, BrowserActionControlRuntime
from .deadline_runtime import BrowserActionDeadlineRuntime
from .download_guard import BrowserDownloadGuard
from .download_runtime import BrowserDownloadLedger, BrowserNativeDownloadRuntime
from .event_writer import BrowserActionEventWriter
from .factory import (
    BrowserActionFoundation,
    BrowserActionFoundationFactory,
    BrowserActionFoundationOptions,
)
from .gateway import AuthorizedBrowserAction, BrowserActionGatewayError, PreparedBrowserAction
from .integration_models import (
    BrowserActionIntegrationError,
    BrowserActionPlan,
    DispatchBoundary,
    PlanAdmission,
    PlanExecutionResult,
    PlanPhase,
    StepOutcome,
    StepState,
    create_execution_id,
)
from .models import ActionExecutionResult, ActionFailureKind, ActionPhase, stable_id
from .network_policy import HostResolver
from .plan_adapter import BrowserActionPlanAdapter, PlanAdapterConfig
from .semantic_probe import CdpElementSemanticProbe
from .sensitive_policy import ExecutionMode
from .session_adapter import (
    BrowserActionArtifactPort,
    BrowserNetworkInterception,
    CdpClipboardPort,
    CdpDownloadControlPort,
    SessionBoundCdpTransport,
    SessionTransportConfig,
)


class BrowserActionApplication:
    """Default 04A→04B→04C→03A browser action application.

    This is the only production facade allowed to turn a productized
    ``browser_plan`` into browser side effects.  It statically admits every
    step, security-preflights every step, obtains every permission decision,
    and only then consumes a grant inside the session-bound CDP fence.
    """

    def __new__(cls, browser_runtime: Any, *args: Any, **kwargs: Any):
        if browser_runtime is None or bool(kwargs.get("disabled", False)):
            return super().__new__(cls)
        existing = getattr(browser_runtime, "_browser_action_application", None)
        if isinstance(existing, cls):
            return existing
        instance = super().__new__(cls)
        setattr(browser_runtime, "_browser_action_application", instance)
        return instance

    def __init__(
        self,
        browser_runtime: Any,
        *,
        artifact_store: LocalArtifactStore,
        message_state_application: Any,
        resolver: HostResolver | None = None,
        deadline_runtime: BrowserActionDeadlineRuntime | None = None,
        disabled: bool = False,
    ) -> None:
        if getattr(self, "_initialized", False):
            if resolver is not None:
                self.resolver = resolver
            return
        if browser_runtime is None or artifact_store is None or message_state_application is None:
            raise ValueError("browser action application requires 04A, 04B and artifact owners")
        self.browser_runtime = browser_runtime
        self.artifact_store = artifact_store
        self.message_state_application = message_state_application
        self.resolver = resolver
        self.deadlines = deadline_runtime or BrowserActionDeadlineRuntime()
        self.controls = BrowserActionControlRuntime(self.deadlines, disabled=disabled)
        self.disabled = disabled
        self._lock = threading.RLock()
        self._executions = 0
        self._failures = 0
        self._pending = 0
        self._initialized = True

    def apply_control(self, command: BrowserActionControlCommand) -> BrowserActionControlResult:
        """Apply cancellation/inspection without waiting on the execution lock."""
        return self.controls.apply(command)

    def execute_plan(
        self,
        request: WorkerRequest,
        session_start: Any,
        raw_plan: Sequence[Mapping[str, Any]],
        permission_gate: Any,
    ) -> BrowserSessionApplicationResult:
        validation = self._validate(request, session_start, raw_plan, permission_gate)
        if validation:
            return self._boundary_failure(request, session_start, validation)
        with self._lock:
            try:
                return self._execute(request, session_start, raw_plan, permission_gate)
            except Exception as exc:  # noqa: BLE001 - public application must fail closed.
                self._failures += 1
                return self._boundary_failure(
                    request,
                    session_start,
                    getattr(exc, "code", "browser_action_application_failed"),
                    details=f"{type(exc).__name__}: {exc}",
                    events=tuple(getattr(exc, "events", ())),
                )

    def _execute(
        self,
        request: WorkerRequest,
        session_start: Any,
        raw_plan: Sequence[Mapping[str, Any]],
        permission_gate: Any,
    ) -> BrowserSessionApplicationResult:
        session = session_start.session
        target_runtime = self.browser_runtime.target_runtime(session.session_id)
        cdp_runtime = self.browser_runtime.cdp_runtime(session.session_id)
        writer = BrowserActionEventWriter()
        transport = SessionBoundCdpTransport(
            cdp_runtime,
            target_runtime,
            browser_session_id=session.session_id,
            config=SessionTransportConfig(
                request_timeout_seconds=float(self.browser_runtime.config.request_timeout_seconds),
            ),
        )
        artifact_port = BrowserActionArtifactPort(
            self.artifact_store,
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id or "",
            browser_session_id=session.session_id,
        )
        clipboard_port = CdpClipboardPort(transport)
        semantic_probe = CdpElementSemanticProbe(transport)
        constraints = dict(request.constraints or {})
        execution_mode = (
            ExecutionMode.SEALED_AUTONOMOUS
            if str(getattr(permission_gate, "effective_mode", "")).casefold() == "sealed"
            else ExecutionMode.INTERACTIVE
        )
        options = BrowserActionFoundationOptions(
            workspace_root=self._workspace_root(constraints),
            artifact_root=self.artifact_store.root,
            downloads_root=self.artifact_store.root / ".browser-downloads" / session.session_id,
            allowed_domains=_strings(constraints.get("browser_allowed_domains")),
            denied_domains=_strings(constraints.get("browser_denied_domains")),
            require_domain_allowlist=bool(constraints.get("browser_require_domain_allowlist", False)),
            allow_loopback=bool(constraints.get("browser_allow_loopback", False)),
            allow_private=bool(constraints.get("browser_allow_private_network", False)),
            allow_literal_ip=bool(constraints.get("browser_allow_literal_ip", False)),
            execution_mode=execution_mode,
            receipt_ttl_seconds=float(constraints.get("browser_preflight_ttl_seconds") or 30.0),
            browser_context_id=session.session_id,
        )

        def network_scope(context: Any) -> BrowserNetworkInterception:
            receipt = context.bindings.network_receipt
            if receipt is None:
                raise BrowserActionIntegrationError(
                    "browser_network_receipt_missing",
                    "network dispatch has no exact network receipt",
                    phase=PlanPhase.DISPATCH,
                    action_id=context.request.identity.action_id,
                )
            deadline = transport._deadline
            timeout = float(self.browser_runtime.config.request_timeout_seconds)
            if deadline is not None and deadline.remaining_seconds is not None:
                timeout = max(0.01, min(timeout, deadline.remaining_seconds))
            return BrowserNetworkInterception(
                action_id=context.request.identity.action_id,
                network_policy=foundation.network_policy,
                initial_receipt=receipt,
                cdp_runtime=cdp_runtime,
                target_runtime=target_runtime,
                timeout_seconds=timeout,
            )

        foundation = BrowserActionFoundationFactory.create(
            options=options,
            permission_gate=permission_gate,
            selector_store=self.message_state_application.selector_store,
            cdp_transport=transport,
            artifact_port=artifact_port,
            resolver=self.resolver,
            clipboard_port=clipboard_port,
            semantic_probe=semantic_probe,
            network_scope_factory=network_scope,
            require_network_scope=True,
            event_sink=writer.sink,
        )
        download_ledger = self._download_ledger(session.session_id)
        download_guard = BrowserDownloadGuard(
            file_policy=foundation.file_policy,
            control_port=CdpDownloadControlPort(transport),
            quarantine_parent=Path(options.downloads_root) / ".quarantine",
        )
        foundation.gateway.executor.download_runtime = BrowserNativeDownloadRuntime(
            guard=download_guard,
            cdp_runtime=cdp_runtime,
            transport=transport,
            artifact_port=artifact_port,
            ledger=download_ledger,
            browser_context_id=session.session_id,
            default_timeout_seconds=float(self.browser_runtime.config.request_timeout_seconds),
        )
        continuation = BrowserActionContinuationRuntime(
            permission_gate,
            BrowserContinuationPayloadStore(
                Path(self.browser_runtime.config.state_root) / "action-continuations" / session.session_id
            ),
        )
        effective_plan = self._resumable_raw_plan(
            continuation,
            request=request,
            browser_session_id=session.session_id,
        ) or tuple(raw_plan)
        adapter = BrowserActionPlanAdapter(
            foundation.gateway.registry,
            self.message_state_application.selector_store,
            config=PlanAdapterConfig(
                maximum_steps=int(constraints.get("browser_maximum_plan_steps") or 256),
                maximum_plan_bytes=int(constraints.get("browser_maximum_plan_bytes") or 2_000_000),
            ),
        )
        admission = adapter.admit(
            request,
            session_start,
            effective_plan,
            permission_session_id=permission_gate.session_id,
            target_runtime=target_runtime,
        )
        if not admission.ok:
            writer.plan_rejected(
                admission,
                run_id=request.run_id,
                task_id=request.task_id,
                worker_request_id=request.request_id,
                node_id=request.node_id or "",
            )
            return self._admission_failure(request, session_start, admission, writer)
        plan = admission.require()
        writer.plan_admitted(admission, node_id=request.node_id or "")
        for step in plan.steps:
            writer.tool_call(step.request, event_port=foundation.event_port)

        prepared, preflight_failure = self._preflight_all(
            plan,
            foundation,
            transport,
        )
        if preflight_failure is not None:
            outcomes = self._abort_outcomes(
                plan,
                failing_step=preflight_failure[0],
                error=preflight_failure[1],
                state=StepState.FAILED,
                phase=PlanPhase.SECURITY_PREFLIGHT,
                transport=transport,
            )
            return self._finish(
                request,
                session_start,
                plan,
                outcomes,
                writer,
                artifact_port,
                phase=PlanPhase.BLOCKED,
                error_code=outcomes[preflight_failure[0].index - 1].error_code,
            )

        authorized, authorization_failures = self._authorize_all(plan, foundation, prepared)
        if authorization_failures:
            step, error = authorization_failures[0]
            outcomes = self._abort_outcomes(
                plan,
                failing_step=step,
                error=error,
                state=StepState.DENIED,
                phase=PlanPhase.PERMISSION,
                transport=transport,
                prepared=prepared,
                authorized=authorized,
            )
            return self._finish(
                request,
                session_start,
                plan,
                outcomes,
                writer,
                artifact_port,
                phase=PlanPhase.BLOCKED,
                error_code=outcomes[step.index - 1].error_code,
            )

        pending = [item for item in authorized if item.pending]
        if pending:
            checkpoints = [
                continuation.park(plan, item.prepared, item)
                for item in pending
            ]
            outcomes = tuple(
                self._pending_outcome(step, prepared[step.index - 1], authorized[step.index - 1])
                if authorized[step.index - 1].pending
                else self._skipped_outcome(
                    step,
                    prepared=prepared[step.index - 1],
                    authorized=authorized[step.index - 1],
                    code="browser_plan_waiting_for_permission",
                    message="all actions wait until every permission decision is terminal",
                )
                for step in plan.steps
            )
            for outcome in outcomes:
                writer.partial(
                    plan.step(outcome.step_index).request,
                    phase=PlanPhase.PERMISSION_PENDING,
                    details=outcome.public_dict(),
                    cause_event_id=(outcome.events[-1].event_id if outcome.events else ""),
                )
            execution = PlanExecutionResult(
                execution_id=create_execution_id(plan),
                plan=plan,
                phase=PlanPhase.PERMISSION_PENDING,
                outcomes=outcomes,
                events=writer.events(),
                artifacts=(),
                pending_checkpoint=checkpoints[0],
                error_code="browser_action_permission_pending",
                error_message="browser action plan is parked pending 03A approval",
            )
            self._pending += 1
            return self._project_result(
                request,
                session_start,
                execution,
                writer,
                receipts=(),
                metadata={
                    "browser_permission_pending": "true",
                    "browser_pending_checkpoint_id": checkpoints[0].checkpoint_id,
                    "browser_pending_permission_request_id": checkpoints[0].permission_request_id,
                    "browser_pending_permission_tool_use_id": checkpoints[0].permission_tool_use_id,
                    "browser_pending_count": str(len(checkpoints)),
                    "browser_session_must_remain_live": "true",
                },
            )

        outcomes = self._dispatch_all(
            plan,
            prepared,
            authorized,
            continuation,
            foundation,
            transport,
            artifact_port,
            writer,
            claimant=f"browser-worker:{request.request_id}",
        )
        succeeded = all(outcome.ok for outcome in outcomes)
        phase = PlanPhase.COMPLETED if succeeded else (
            PlanPhase.PARTIAL if any(outcome.ok for outcome in outcomes) else PlanPhase.BLOCKED
        )
        first_error = next((outcome.error_code for outcome in outcomes if outcome.error_code), "")
        return self._finish(
            request,
            session_start,
            plan,
            outcomes,
            writer,
            artifact_port,
            phase=phase,
            error_code=first_error,
        )

    def _preflight_all(
        self,
        plan: BrowserActionPlan,
        foundation: BrowserActionFoundation,
        transport: SessionBoundCdpTransport,
    ) -> tuple[tuple[PreparedBrowserAction, ...], tuple[Any, Exception] | None]:
        prepared: list[PreparedBrowserAction] = []
        for step in plan.steps:
            deadline = self.deadlines.get(step.action_id)
            if deadline is None:
                deadline = self.deadlines.begin(step.request)
            elif deadline.request.request_digest != step.request.request_digest:
                raise BrowserActionIntegrationError(
                    "browser_action_resume_request_changed",
                    "active browser action deadline belongs to another request body",
                    phase=PlanPhase.RESUME_VALIDATION,
                    action_id=step.action_id,
                    step_index=step.index,
                )
            transport.bind_action(step.action_id, deadline)
            try:
                deadline.checkpoint(PlanPhase.SECURITY_PREFLIGHT)
                prepared.append(
                    foundation.gateway.prepare(
                        step.request,
                        selector_expectation=step.selector_expectation,
                    )
                )
            except Exception as exc:
                self.deadlines.finish(step.action_id, failed=True)
                return tuple(prepared), (step, exc)
            finally:
                transport.release_action(step.action_id)
        return tuple(prepared), None

    @staticmethod
    def _authorize_all(
        plan: BrowserActionPlan,
        foundation: BrowserActionFoundation,
        prepared: Sequence[PreparedBrowserAction],
    ) -> tuple[tuple[AuthorizedBrowserAction, ...], tuple[tuple[Any, Exception], ...]]:
        authorized: list[AuthorizedBrowserAction] = []
        failures: list[tuple[Any, Exception]] = []
        for step, item in zip(plan.steps, prepared, strict=True):
            try:
                decision = foundation.gateway.authorize(item)
                authorized.append(decision)
                if not decision.allowed and not decision.pending:
                    failures.append(
                        (
                            step,
                            BrowserActionIntegrationError(
                                "browser_action_permission_denied",
                                "03A permission owner denied the browser action",
                                phase=PlanPhase.PERMISSION,
                                action_id=step.action_id,
                                step_index=step.index,
                                details=decision.permission.public_dict(),
                            ),
                        )
                    )
            except Exception as exc:
                failures.append((step, exc))
                break
        return tuple(authorized), tuple(failures)

    def _dispatch_all(
        self,
        plan: BrowserActionPlan,
        prepared: Sequence[PreparedBrowserAction],
        authorized: Sequence[AuthorizedBrowserAction],
        continuation: BrowserActionContinuationRuntime,
        foundation: BrowserActionFoundation,
        transport: SessionBoundCdpTransport,
        artifact_port: BrowserActionArtifactPort,
        writer: BrowserActionEventWriter,
        *,
        claimant: str,
    ) -> tuple[StepOutcome, ...]:
        outcomes: list[StepOutcome] = []
        stop = False
        for step, preflight, decision in zip(plan.steps, prepared, authorized, strict=True):
            if stop:
                outcome = self._skipped_outcome(
                    step,
                    prepared=preflight,
                    authorized=decision,
                    code="browser_plan_stopped_after_failure",
                    message="a prior action failed and continue_on_error is false",
                )
                outcomes.append(outcome)
                writer.terminal(outcome)
                self._finish_deadline(step.action_id, failed=True)
                continue
            deadline = self.deadlines.require(step.action_id)
            transport.bind_action(step.action_id, deadline)
            command_start = len(transport.commands)
            artifact_start = len(artifact_port.artifacts)
            claim: BrowserContinuationClaim | None = None
            try:
                deadline.checkpoint(PlanPhase.RESUME_VALIDATION)
                claim = continuation.claim_for_authorized(
                    plan,
                    preflight,
                    decision,
                    claimant=claimant,
                )
                deadline.checkpoint(PlanPhase.DISPATCH)
                completed = foundation.gateway.execute(decision)
                artifacts = _dedupe_artifacts(
                    (*completed.projection.artifacts, *artifact_port.artifacts[artifact_start:])
                )
                outcome = StepOutcome(
                    action_id=step.action_id,
                    step_index=step.index,
                    action=step.canonical_action,
                    state=StepState.SUCCEEDED,
                    request_digest=step.request.request_digest,
                    preflight_receipt_id=completed.permission.preflight.receipt_id,
                    permission_decision_id=decision.permission.decision.decision_id,
                    permission_request_id=decision.permission.decision.request_id,
                    permission_tool_use_id=decision.permission.decision.tool_use_id,
                    result=completed.result,
                    artifacts=artifacts,
                    events=completed.events,
                    dispatch_boundary=DispatchBoundary.RESULT_ONLY,
                )
                for artifact in artifacts:
                    writer.artifact_event(
                        step.request.identity,
                        artifact,
                        receipt_id=completed.permission.preflight.receipt_id,
                        cause_event_id=completed.events[-1].event_id,
                    )
                writer.terminal(outcome, cause_event_id=completed.events[-1].event_id)
                if claim is not None:
                    continuation.complete(claim)
                outcomes.append(outcome)
                self.deadlines.finish(step.action_id)
            except Exception as exc:
                commands = transport.command_slice(command_start)
                cdp_effects = sum(command.mutating for command in commands)
                details = dict(getattr(exc, "details", {}) or {})
                side_effects = max(cdp_effects, int(details.get("side_effect_count") or 0))
                outcome_unknown = bool(details.get("outcome_unknown", False)) or side_effects > 0
                state = (
                    StepState.CANCELLED
                    if any(token in str(getattr(exc, "code", type(exc).__name__)).casefold() for token in ("cancel", "deadline", "timeout"))
                    else StepState.DENIED
                    if "permission" in str(getattr(exc, "code", "")).casefold() and side_effects == 0
                    else StepState.FAILED
                )
                result = ActionExecutionResult(
                    action_id=step.action_id,
                    ok=False,
                    summary="Browser action failed at the guarded dispatch boundary.",
                    output={"outcome_unknown": outcome_unknown},
                    error_code=str(getattr(exc, "code", "browser_action_dispatch_failed")),
                    error_message=str(exc),
                    failure_kind=getattr(exc, "failure_kind", ActionFailureKind.EXECUTION),
                    side_effect_count=side_effects,
                    cdp_effect_count=cdp_effects,
                )
                outcome = StepOutcome(
                    action_id=step.action_id,
                    step_index=step.index,
                    action=step.canonical_action,
                    state=state,
                    request_digest=step.request.request_digest,
                    preflight_receipt_id=preflight.receipt.receipt_id,
                    permission_decision_id=decision.permission.decision.decision_id,
                    permission_request_id=decision.permission.decision.request_id,
                    permission_tool_use_id=decision.permission.decision.tool_use_id,
                    result=result,
                    artifacts=tuple(artifact_port.artifacts[artifact_start:]),
                    events=tuple(getattr(exc, "events", ())),
                    error_code=result.error_code,
                    error_message=result.error_message,
                    failure_phase=str(details.get("latest_phase") or PlanPhase.DISPATCH),
                    dispatch_boundary=(
                        DispatchBoundary.AFTER_EFFECT
                        if side_effects
                        else DispatchBoundary.AFTER_GRANT_BEFORE_EFFECT
                    ),
                    retryable=side_effects == 0,
                )
                writer.terminal(outcome)
                if claim is not None:
                    continuation.fail(claim, code=result.error_code, outcome_unknown=outcome_unknown)
                outcomes.append(outcome)
                self.deadlines.finish(step.action_id, failed=True, cancelled=state == StepState.CANCELLED)
                stop = not plan.continue_on_error
            finally:
                transport.release_action(step.action_id)
        return tuple(outcomes)

    def _finish(
        self,
        request: WorkerRequest,
        session_start: Any,
        plan: BrowserActionPlan,
        outcomes: Sequence[StepOutcome],
        writer: BrowserActionEventWriter,
        artifact_port: BrowserActionArtifactPort,
        *,
        phase: PlanPhase,
        error_code: str,
    ) -> BrowserSessionApplicationResult:
        for outcome in outcomes:
            # Dispatch writes its own causally linked terminal. Atomic abort
            # paths arrive here without one and are completed exactly once.
            if not writer.has_terminal(outcome.action_id):
                writer.terminal(outcome)
        execution = PlanExecutionResult(
            execution_id=create_execution_id(plan),
            plan=plan,
            phase=phase,
            outcomes=tuple(outcomes),
            events=writer.events(),
            artifacts=_dedupe_artifacts(artifact_port.artifacts),
            error_code=error_code,
            error_message=next((outcome.error_message for outcome in outcomes if outcome.error_message), ""),
        )
        writer.finish_plan(execution, node_id=request.node_id or "")
        writer.assert_complete(plan, outcomes)
        receipts = writer.context_receipts(outcomes)
        execution = PlanExecutionResult(
            execution_id=execution.execution_id,
            plan=plan,
            phase=phase,
            outcomes=tuple(outcomes),
            events=writer.events(),
            artifacts=_dedupe_artifacts(artifact_port.artifacts),
            error_code=error_code,
            error_message=execution.error_message,
        )
        if execution.ok:
            self._executions += 1
        else:
            self._failures += 1
        return self._project_result(request, session_start, execution, writer, receipts=receipts)

    def _project_result(
        self,
        request: WorkerRequest,
        session_start: Any,
        execution: PlanExecutionResult,
        writer: BrowserActionEventWriter,
        *,
        receipts: Sequence[Any],
        metadata: Mapping[str, Any] | None = None,
    ) -> BrowserSessionApplicationResult:
        values = {
            "browser_backend": "zyra-browser-productized",
            "browser_application_runtime": "zyra-browser-action-application",
            "browser_action_owner_unit": "M1-S04C-02",
            "browser_action_plan_id": execution.plan.plan_id,
            "browser_action_plan_phase": str(execution.phase),
            "browser_action_execution_id": execution.execution_id,
            "browser_action_execution_count": str(execution.action_execution_count),
            "browser_action_side_effect_count": str(execution.side_effect_count),
            "browser_action_pending": str(execution.pending).lower(),
            "browser_action_partial": str(execution.partial).lower(),
            "browser_action_fallback_allowed": "false",
            "browser_action_default_gateway": "true",
            **dict(metadata or {}),
        }
        error = execution.error_code or (None if execution.ok else "browser_action_plan_failed")
        domain = BrowserApplicationResult(
            ok=execution.ok,
            events=writer.events(),
            artifacts=execution.artifacts,
            action_receipts=tuple(receipts),
            error=str(error or ""),
            metadata=values,
        )
        worker = WorkerResult(
            request_id=request.request_id,
            ok=execution.ok,
            summary=(
                f"Browser action plan executed {execution.action_execution_count} action(s)."
                if execution.ok
                else "Browser action plan stopped at a guarded boundary."
            ),
            artifacts=list(execution.artifacts),
            events=[to_jsonable(event) for event in writer.events()],
            error=error,
            metadata={str(key): str(value) for key, value in values.items()},
        )
        return BrowserSessionApplicationResult(
            worker_result=worker,
            event_records=writer.events(),
            receipts=tuple(receipts),
            session_start=session_start,
            domain_result=domain,
        )

    def _admission_failure(
        self,
        request: WorkerRequest,
        session_start: Any,
        admission: PlanAdmission,
        writer: BrowserActionEventWriter,
    ) -> BrowserSessionApplicationResult:
        details = {"issues": [issue.public_dict() for issue in admission.issues]}
        return self._boundary_failure(
            request,
            session_start,
            "browser_plan_admission_failed",
            details=str(details),
            events=writer.events(),
        )

    @staticmethod
    def _pending_outcome(
        step: Any,
        prepared: PreparedBrowserAction,
        authorized: AuthorizedBrowserAction,
    ) -> StepOutcome:
        return StepOutcome(
            action_id=step.action_id,
            step_index=step.index,
            action=step.canonical_action,
            state=StepState.PENDING,
            request_digest=step.request.request_digest,
            preflight_receipt_id=prepared.receipt.receipt_id,
            permission_decision_id=authorized.permission.decision.decision_id,
            permission_request_id=authorized.permission.decision.request_id,
            permission_tool_use_id=authorized.permission.decision.tool_use_id,
            events=authorized.events,
            dispatch_boundary=DispatchBoundary.BEFORE_GRANT,
        )

    @staticmethod
    def _skipped_outcome(
        step: Any,
        *,
        prepared: PreparedBrowserAction | None = None,
        authorized: AuthorizedBrowserAction | None = None,
        code: str,
        message: str,
    ) -> StepOutcome:
        return StepOutcome(
            action_id=step.action_id,
            step_index=step.index,
            action=step.canonical_action,
            state=StepState.SKIPPED,
            request_digest=step.request.request_digest,
            preflight_receipt_id=prepared.receipt.receipt_id if prepared else "",
            permission_decision_id=authorized.permission.decision.decision_id if authorized else "",
            permission_request_id=authorized.permission.decision.request_id if authorized else "",
            permission_tool_use_id=authorized.permission.decision.tool_use_id if authorized else "",
            events=authorized.events if authorized else (),
            error_code=code,
            error_message=message,
            failure_phase=str(PlanPhase.BLOCKED),
            dispatch_boundary=DispatchBoundary.BEFORE_GRANT,
        )

    def _abort_outcomes(
        self,
        plan: BrowserActionPlan,
        *,
        failing_step: Any,
        error: Exception,
        state: StepState,
        phase: PlanPhase,
        transport: SessionBoundCdpTransport,
        prepared: Sequence[PreparedBrowserAction] = (),
        authorized: Sequence[AuthorizedBrowserAction] = (),
    ) -> tuple[StepOutcome, ...]:
        outcomes: list[StepOutcome] = []
        code = str(getattr(error, "code", "browser_action_preflight_failed"))
        for step in plan.steps:
            preflight = prepared[step.index - 1] if step.index <= len(prepared) else None
            decision = authorized[step.index - 1] if step.index <= len(authorized) else None
            if step.index == failing_step.index:
                outcome = StepOutcome(
                    action_id=step.action_id,
                    step_index=step.index,
                    action=step.canonical_action,
                    state=state,
                    request_digest=step.request.request_digest,
                    preflight_receipt_id=preflight.receipt.receipt_id if preflight else "",
                    permission_decision_id=decision.permission.decision.decision_id if decision else "",
                    permission_request_id=decision.permission.decision.request_id if decision else "",
                    permission_tool_use_id=decision.permission.decision.tool_use_id if decision else "",
                    events=tuple(getattr(error, "events", ())),
                    error_code=code,
                    error_message=str(error),
                    failure_phase=str(phase),
                    dispatch_boundary=DispatchBoundary.BEFORE_GRANT,
                    retryable=False,
                )
            else:
                outcome = self._skipped_outcome(
                    step,
                    prepared=preflight,
                    authorized=decision,
                    code="browser_plan_atomic_barrier_failed",
                    message="another action failed before the all-actions-first barrier",
                )
            outcomes.append(outcome)
            self._finish_deadline(step.action_id, failed=True)
        if transport.mutating_commands:
            raise BrowserActionIntegrationError(
                "browser_atomic_barrier_side_effect_detected",
                "browser plan mutated CDP before the all-actions-first barrier",
                phase=phase,
                side_effect_count=len(transport.mutating_commands),
            )
        return tuple(outcomes)

    def _finish_deadline(self, action_id: str, *, failed: bool) -> None:
        if self.deadlines.get(action_id) is not None:
            self.deadlines.finish(action_id, failed=failed)

    def _resumable_raw_plan(
        self,
        continuation: BrowserActionContinuationRuntime,
        *,
        request: WorkerRequest,
        browser_session_id: str,
    ) -> tuple[Mapping[str, Any], ...]:
        matches: list[tuple[Mapping[str, Any], ...]] = []
        for record in continuation.runtime.records():
            if record.terminal or record.run_id != request.run_id or record.task_id != request.task_id:
                continue
            try:
                application = continuation.payload_store.application_payload(record)
            except Exception:
                continue
            plan = application.get("plan")
            raw = application.get("raw_plan")
            if not isinstance(plan, Mapping) or not isinstance(raw, Sequence):
                continue
            if str(plan.get("worker_request_id") or "") != request.request_id:
                continue
            if str(plan.get("browser_session_id") or "") != browser_session_id:
                continue
            normalized: list[Mapping[str, Any]] = []
            plan_steps = plan.get("steps") if isinstance(plan.get("steps"), Sequence) else ()
            for index, item in enumerate(raw):
                if not isinstance(item, Mapping):
                    break
                step = dict(item)
                if index < len(plan_steps) and isinstance(plan_steps[index], Mapping):
                    deadline_at = str(plan_steps[index].get("deadline_at") or "")
                    if deadline_at:
                        step["deadline_at"] = deadline_at
                normalized.append(step)
            else:
                matches.append(tuple(normalized))
        if len(matches) > 1:
            raise BrowserActionIntegrationError(
                "browser_continuation_ambiguous",
                "multiple pending browser plans match the worker request",
                phase=PlanPhase.RESUME_VALIDATION,
            )
        return matches[0] if matches else ()

    def _download_ledger(self, browser_session_id: str) -> BrowserDownloadLedger:
        ledgers = getattr(self.browser_runtime, "_browser_download_ledgers", None)
        if not isinstance(ledgers, dict):
            ledgers = {}
            setattr(self.browser_runtime, "_browser_download_ledgers", ledgers)
        ledger = ledgers.get(browser_session_id)
        if not isinstance(ledger, BrowserDownloadLedger):
            ledger = BrowserDownloadLedger(browser_session_id)
            ledgers[browser_session_id] = ledger
        return ledger

    def _workspace_root(self, constraints: Mapping[str, Any]) -> Path:
        configured = constraints.get("workspace_root") or constraints.get("browser_workspace_root")
        if configured:
            candidate = Path(str(configured)).resolve(strict=False)
            # Workspace is caller-selected only when it stays below the
            # process-owned runtime root; file policy performs the exact check.
            return candidate
        return Path(self.browser_runtime.config.runtime_root) / "workspace"

    def _validate(self, request: Any, session_start: Any, raw_plan: Any, permission_gate: Any) -> str:
        if self.disabled:
            return "browser_action_application_disabled"
        if not isinstance(request, WorkerRequest):
            return "invalid_browser_worker_request"
        session = getattr(session_start, "session", None)
        if not bool(getattr(session_start, "ok", False)) or session is None:
            return "invalid_browser_session_start"
        if str(getattr(session, "status", "")) != "running":
            return "browser_session_not_running"
        if str(getattr(session, "run_id", "")) != request.run_id or str(getattr(session, "task_id", "")) != request.task_id:
            return "browser_session_identity_mismatch"
        if permission_gate is None:
            return "browser_permission_gate_unavailable"
        if isinstance(raw_plan, (str, bytes, bytearray)) or not isinstance(raw_plan, Sequence) or not raw_plan:
            return "invalid_browser_plan"
        return ""

    def _boundary_failure(
        self,
        request: Any,
        session_start: Any,
        code: str,
        *,
        details: str = "",
        events: Sequence[EventRecord] = (),
    ) -> BrowserSessionApplicationResult:
        run_id = str(getattr(request, "run_id", ""))
        task_id = str(getattr(request, "task_id", ""))
        node_id = getattr(request, "node_id", None)
        session = getattr(session_start, "session", None)
        values = list(events)
        if not values:
            values.append(
                EventRecord(
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    event_type=EventType.AGENT_MESSAGE,
                    payload={
                        "browser_action_plan": {
                            "schema": "zyra.browser-action.application-failure.v1",
                            "phase": str(PlanPhase.BLOCKED),
                            "error_code": code,
                            "error_message": details,
                            "browser_session_id": str(getattr(session, "session_id", "") or ""),
                            "side_effect_count": 0,
                            "fallback_allowed": False,
                        }
                    },
                )
            )
        metadata = {
            "browser_backend": "zyra-browser-productized",
            "browser_application_runtime": "zyra-browser-action-application",
            "browser_action_owner_unit": "M1-S04C-02",
            "browser_action_fallback_allowed": "false",
            "browser_action_default_gateway": "true",
            "failure_details": details,
        }
        domain = BrowserApplicationResult(
            ok=False,
            events=tuple(values),
            artifacts=(),
            action_receipts=(),
            error=code,
            metadata=metadata,
        )
        worker = WorkerResult(
            request_id=str(getattr(request, "request_id", "")),
            ok=False,
            summary="Browser action application failed closed.",
            artifacts=[],
            events=[to_jsonable(event) for event in values],
            error=code,
            metadata={str(key): str(value) for key, value in metadata.items()},
        )
        return BrowserSessionApplicationResult(
            worker_result=worker,
            event_records=tuple(values),
            receipts=(),
            session_start=session_start,
            domain_result=domain,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "runtime_id": "zyra-browser-action-application",
            "owner_unit": "M1-S04C-02",
            "disabled": self.disabled,
            "executions": self._executions,
            "failures": self._failures,
            "pending": self._pending,
            "default_route": True,
            "fallback_allowed": False,
            "deadline_runtime": self.deadlines.snapshot(),
            "control_runtime": self.controls.snapshot(),
        }


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _dedupe_artifacts(values: Sequence[Any]) -> tuple[Any, ...]:
    output: list[Any] = []
    seen: set[str] = set()
    for value in values:
        artifact_id = str(getattr(value, "artifact_id", "") or "")
        if artifact_id and artifact_id not in seen:
            output.append(value)
            seen.add(artifact_id)
    return tuple(output)
