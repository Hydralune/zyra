from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping, Sequence

from .attachments import SkillAttachmentRuntime, attachment_delta_digest, estimate_tokens
from .body_loader import SkillBodyResourceLoader
from .budget_runtime import SkillBudgetStage, SkillContextBudgetRuntime
from .digests import arguments_digest, digest_object
from .errors import (
    SkillForkUnavailable,
    SkillInvocationConflict,
    SkillRuntimeDisabled,
    SkillRuntimeError,
)
from .events import SkillEventProjector, SkillRuntimeEvent, event_records
from .hooks import SkillHookRuntime
from .invocation_permission import (
    DenySkillInvocationPermission,
    SkillInvocationPermissionPort,
    SkillInvocationPermissionResult,
)
from .models import (
    InvokedSkillState,
    SkillForkRequest,
    SkillHookEvent,
    SkillInvocationMode,
    SkillInvocationPlan,
    SkillInvocationRequest,
    SkillInvocationStatus,
    SkillMessageDelta,
    SkillOutcomeProjection,
    SkillRevision,
    new_id,
)
from .policy import SkillAllowedToolsPolicy
from .registry import SkillRegistry
from .resource_loader import SkillResourceLoader
from .state import SkillInvocationStateStore
from .subagent_contract import SkillForkPort, UnavailableSkillForkPort


class SkillInvocationRuntime:
    """Atomic SkillRegistry -> loader -> policy -> invocation state chain."""

    def __init__(
        self,
        *,
        registry: SkillRegistry,
        body_loader: SkillBodyResourceLoader,
        resource_loader: SkillResourceLoader,
        allowed_tools_policy: SkillAllowedToolsPolicy,
        state_store: SkillInvocationStateStore,
        hook_runtime: SkillHookRuntime,
        attachment_runtime: SkillAttachmentRuntime,
        permission_port: SkillInvocationPermissionPort | None = None,
        fork_port: SkillForkPort | None = None,
        event_projector: SkillEventProjector | None = None,
        budget_runtime: SkillContextBudgetRuntime | None = None,
        disabled: bool = False,
    ) -> None:
        self.registry = registry
        self.body_loader = body_loader
        self.resource_loader = resource_loader
        self.allowed_tools_policy = allowed_tools_policy
        self.state_store = state_store
        self.hook_runtime = hook_runtime
        self.attachment_runtime = attachment_runtime
        self.permission_port = permission_port or DenySkillInvocationPermission()
        self.fork_port = fork_port or UnavailableSkillForkPort()
        self.event_projector = event_projector or SkillEventProjector()
        self.budget_runtime = budget_runtime or SkillContextBudgetRuntime()
        self.disabled = disabled
        self._lock = RLock()
        self._events: dict[str, tuple[SkillRuntimeEvent, ...]] = {}

    def invoke(
        self,
        request: SkillInvocationRequest,
        *,
        workspace_paths: Sequence[str] = (),
        load_resources: Sequence[str] = (),
        require_model_invocable: bool = False,
        require_user_invocable: bool = True,
    ) -> SkillInvocationPlan:
        self._ensure_enabled()
        if request.idempotency_key:
            existing = self.state_store.get_by_idempotency(request.idempotency_key)
            if existing is not None:
                raise SkillInvocationConflict(
                    "skill invocation idempotency key was already consumed",
                    detail={
                        "idempotency_key": request.idempotency_key,
                        "invocation_id": existing.invocation_id,
                        "status": str(existing.status),
                    },
                )
        revision = self.registry.resolve(
            request.skill_name,
            requested_ref=request.requested_version_ref,
            workspace_paths=workspace_paths,
            require_model_invocable=require_model_invocable,
            require_user_invocable=require_user_invocable,
        )
        self._validate_depth(request, revision)
        permission = self.permission_port.guard(request, revision)
        permission.require_allow()
        initial = InvokedSkillState(
            invocation_id=request.invocation_id,
            run_id=request.run_id,
            task_id=request.task_id,
            session_id=request.session_id,
            agent_id=request.agent_id,
            version_ref=revision.version_ref,
            status=SkillInvocationStatus.REQUESTED,
        )
        state = self.state_store.create(initial, idempotency_key=request.idempotency_key)
        self.budget_runtime.open(
            invocation_id=request.invocation_id,
            session_id=request.session_id,
            budget=revision.metadata.context_budget,
        )
        leases = ()
        policy_bound = False
        try:
            argument_tokens = estimate_tokens(str(request.arguments))
            self.budget_runtime.reserve(
                invocation_id=request.invocation_id,
                stage=SkillBudgetStage.ATTACHMENT,
                tokens=argument_tokens,
                source_ref=f"skill-arguments://{request.invocation_id}",
                reason="skill arguments rendered into the inline context delta",
            )
            state = self.state_store.transition(
                state.invocation_id,
                SkillInvocationStatus.REF_RESOLVED,
                expected_revision=state.revision,
            )
            body = self.body_loader.load_body(revision)
            self.budget_runtime.reserve(
                invocation_id=state.invocation_id,
                stage=SkillBudgetStage.BODY,
                tokens=body.token_estimate,
                source_ref=body.version_ref.immutable_ref,
                reason="lazy skill body loaded into invocation context",
            )
            state = self.state_store.transition(
                state.invocation_id,
                SkillInvocationStatus.BODY_LOADED,
                expected_revision=state.revision,
            )
            resources = self._load_resources(
                revision,
                requested=load_resources,
                remaining_tokens=max(
                    0,
                    revision.metadata.context_budget.invocation_total_tokens
                    - self.budget_runtime.state(state.invocation_id).consumed_tokens,
                ),
            )
            for resource in resources:
                self.budget_runtime.reserve(
                    invocation_id=state.invocation_id,
                    stage=SkillBudgetStage.RESOURCE,
                    tokens=resource.token_estimate,
                    source_ref=resource.descriptor.immutable_ref,
                    reason="declared skill resource loaded into invocation context",
                )
            policy = self.allowed_tools_policy.bind(
                invocation_id=state.invocation_id,
                session_id=state.session_id,
                revision=revision,
                parent_snapshot_ids=request.parent_policy_snapshot_ids,
            )
            policy_bound = True
            state = self.state_store.transition(
                state.invocation_id,
                SkillInvocationStatus.POLICY_BOUND,
                expected_revision=state.revision,
                policy_snapshot=policy,
            )
            leases = self.hook_runtime.register(
                invocation_id=state.invocation_id,
                session_id=state.session_id,
                version_ref=state.version_ref,
                specs=revision.metadata.hooks,
            )
            if leases:
                state = self.state_store.transition(
                    state.invocation_id,
                    SkillInvocationStatus.HOOKS_REGISTERED,
                    expected_revision=state.revision,
                    hook_lease_ids=tuple(lease.lease_id for lease in leases),
                )
            pre_results = self.hook_runtime.fire(
                SkillHookEvent.PRE_INVOKE,
                invocation_id=state.invocation_id,
                context={
                    "run_id": state.run_id,
                    "task_id": state.task_id,
                    "session_id": state.session_id,
                    "agent_id": state.agent_id,
                    "skill_ref": state.version_ref.immutable_ref,
                },
            )
            failed_pre_hooks = [result for result in pre_results if not result.ok]
            if failed_pre_hooks:
                raise SkillRuntimeError(
                    "skill pre-invocation hook failed",
                    detail={"hooks": [result.to_dict() for result in failed_pre_hooks]},
                )
            attachments = self.attachment_runtime.invocation_attachments(
                state=state,
                body=body,
                policy_snapshot=policy,
                resource_refs=(item.descriptor.immutable_ref for item in resources),
            )
            message = self.attachment_runtime.inline_message(
                body=body,
                attachments=attachments,
                parent_tool_use_id=request.parent_tool_use_id,
                base_directory=revision.skill_root,
                arguments=request.arguments,
            )
            if revision.metadata.invocation.mode is SkillInvocationMode.FORK:
                plan = self._fork_plan(
                    request=request,
                    revision=revision,
                    state=state,
                    body=body,
                    resources=resources,
                    policy=policy,
                    attachments=attachments,
                    message=message,
                    permission=permission,
                )
            else:
                active_status = SkillInvocationStatus.INLINE_ACTIVE
                state = self.state_store.transition(
                    state.invocation_id,
                    active_status,
                    expected_revision=state.revision,
                    attachment_refs=tuple(item.immutable_ref for item in attachments),
                    message_delta_refs=(f"skill-message://{state.invocation_id}/0",),
                )
                plan = SkillInvocationPlan(
                    request=request,
                    revision=revision,
                    body=body,
                    resources=resources,
                    policy_snapshot=policy,
                    messages=(message,),
                    attachments=attachments,
                    state=state,
                    permission_events=permission.events,
                )
            events = self.event_projector.invocation_events(
                request,
                revision=revision,
                plan=plan,
            )
            with self._lock:
                self._events[request.invocation_id] = events
            return plan
        except Exception as error:
            self._rollback_failed_invocation(
                request=request,
                policy_bound=policy_bound,
                error=error,
            )
            raise

    def complete(
        self,
        invocation_id: str,
        *,
        outcome_refs: Iterable[str] = (),
        evidence_refs: Iterable[str] = (),
        artifact_refs: Iterable[str] = (),
    ) -> InvokedSkillState:
        current = self.state_store.get(invocation_id)
        post_results = self.hook_runtime.fire(
            SkillHookEvent.POST_INVOKE,
            invocation_id=invocation_id,
            context={
                "task_id": current.task_id,
                "outcome_refs": list(outcome_refs),
                "evidence_refs": list(evidence_refs),
                "artifact_refs": list(artifact_refs),
            },
        )
        failures = [result for result in post_results if not result.ok]
        status = SkillInvocationStatus.FAILED if failures else SkillInvocationStatus.COMPLETED
        state = self.state_store.transition(
            invocation_id,
            status,
            expected_revision=current.revision,
            outcome_refs=tuple(outcome_refs),
            evidence_refs=tuple(evidence_refs),
            artifact_refs=tuple(artifact_refs),
            error_code="skill_post_hook_failed" if failures else "",
            error_message="one or more post-invocation hooks failed" if failures else "",
        )
        self._cleanup_terminal(state, reason=str(status))
        return state

    def fail(self, invocation_id: str, error: Exception) -> InvokedSkillState:
        current = self.state_store.get(invocation_id)
        self.hook_runtime.fire(
            SkillHookEvent.ON_ERROR,
            invocation_id=invocation_id,
            context={"error_type": type(error).__name__, "error_code": getattr(error, "code", "")},
        )
        state = self.state_store.transition(
            invocation_id,
            SkillInvocationStatus.FAILED,
            expected_revision=current.revision,
            error_code=str(getattr(error, "code", "skill_invocation_failed")),
            error_message=str(error),
        )
        self._cleanup_terminal(state, reason="failed")
        return state

    def cancel(self, invocation_id: str, *, reason: str = "cancelled") -> InvokedSkillState:
        current = self.state_store.get(invocation_id)
        self.hook_runtime.fire(
            SkillHookEvent.ON_CANCEL,
            invocation_id=invocation_id,
            context={"reason": reason},
        )
        state = self.state_store.transition(
            invocation_id,
            SkillInvocationStatus.CANCELLED,
            expected_revision=current.revision,
            error_code="skill_invocation_cancelled",
            error_message=reason,
        )
        self._cleanup_terminal(state, reason=reason)
        return state

    def session_end(self, session_id: str) -> tuple[InvokedSkillState, ...]:
        states = self.state_store.terminate_session(session_id)
        for state in states:
            self._cleanup_terminal(state, reason="session_end")
        self.hook_runtime.cleanup_session(session_id)
        self.allowed_tools_policy.clear_session(session_id)
        self.attachment_runtime.clear_session(session_id)
        return states

    def revoke_active_version(self, revision: SkillRevision, *, reason: str) -> tuple[InvokedSkillState, ...]:
        states = self.state_store.terminate_version(revision.version_ref, reason=reason)
        self.hook_runtime.cleanup_version(revision.version_ref, reason=reason)
        self.allowed_tools_policy.clear_version(revision.version_ref)
        return states

    def events(self, invocation_id: str) -> tuple[SkillRuntimeEvent, ...]:
        with self._lock:
            return self._events.get(invocation_id, ())

    def event_records(self, invocation_id: str) -> tuple[Any, ...]:
        return event_records(self.events(invocation_id))

    def _fork_plan(
        self,
        *,
        request: SkillInvocationRequest,
        revision: SkillRevision,
        state: InvokedSkillState,
        body: Any,
        resources: tuple[Any, ...],
        policy: Any,
        attachments: tuple[Any, ...],
        message: SkillMessageDelta,
        permission: SkillInvocationPermissionResult,
    ) -> SkillInvocationPlan:
        fork_request = SkillForkRequest(
            invocation_id=request.invocation_id,
            run_id=request.run_id,
            task_id=request.task_id,
            parent_session_id=request.session_id,
            parent_agent_id=request.agent_id,
            agent_type=revision.metadata.invocation.agent,
            version_ref=revision.version_ref,
            arguments_digest=arguments_digest(request.arguments),
            policy_snapshot=policy,
            context_refs=request.context_refs,
            attachment_refs=tuple(
                dict.fromkeys((*request.attachment_refs, *(item.immutable_ref for item in attachments)))
            ),
            body_ref=body.version_ref.immutable_ref,
        )
        receipt = self.fork_port.dispatch(fork_request)
        state = self.state_store.transition(
            state.invocation_id,
            SkillInvocationStatus.FORK_PENDING,
            expected_revision=state.revision,
            fork_request_id=receipt.fork_request_id,
            attachment_refs=tuple(item.immutable_ref for item in attachments),
            message_delta_refs=(f"skill-message://{state.invocation_id}/0",),
        )
        return SkillInvocationPlan(
            request=request,
            revision=revision,
            body=body,
            resources=resources,
            policy_snapshot=policy,
            messages=(message,),
            attachments=attachments,
            state=state,
            permission_events=permission.events,
            fork_request=fork_request,
        )

    def _load_resources(
        self,
        revision: SkillRevision,
        *,
        requested: Sequence[str],
        remaining_tokens: int,
    ) -> tuple[Any, ...]:
        if not requested:
            return ()
        unknown = set(requested) - {item.relative_path for item in revision.resources}
        if unknown:
            from .errors import SkillResourceNotFound

            raise SkillResourceNotFound(
                "invocation requested an undeclared skill resource",
                detail={"paths": sorted(unknown)},
            )
        return self.resource_loader.load_many(
            revision,
            requested,
            total_token_budget=remaining_tokens,
        )

    def _validate_depth(self, request: SkillInvocationRequest, revision: SkillRevision) -> None:
        maximum = revision.metadata.invocation.max_skill_depth
        if request.skill_depth > maximum:
            raise SkillInvocationConflict(
                "nested skill invocation exceeds max-skill-depth",
                detail={"depth": request.skill_depth, "maximum": maximum},
            )

    def _rollback_failed_invocation(
        self,
        *,
        request: SkillInvocationRequest,
        policy_bound: bool,
        error: Exception,
    ) -> None:
        try:
            state = self.state_store.get(request.invocation_id)
        except Exception:
            return
        if not state.status.terminal:
            try:
                self.state_store.transition(
                    state.invocation_id,
                    SkillInvocationStatus.BLOCKED
                    if isinstance(error, SkillForkUnavailable)
                    else SkillInvocationStatus.FAILED,
                    expected_revision=state.revision,
                    error_code=str(getattr(error, "code", "skill_invocation_failed")),
                    error_message=str(error),
                )
            except Exception:
                pass
        self.hook_runtime.cleanup_invocation(request.invocation_id, reason="invocation rollback")
        if policy_bound:
            self.allowed_tools_policy.unbind(request.invocation_id)
        try:
            self.budget_runtime.close(request.invocation_id, reason="invocation rollback")
        except Exception:
            pass

    def _cleanup_terminal(self, state: InvokedSkillState, *, reason: str) -> None:
        self.hook_runtime.cleanup_invocation(state.invocation_id, reason=reason)
        self.allowed_tools_policy.unbind(state.invocation_id)
        self.budget_runtime.close(state.invocation_id, reason=reason)

    def _ensure_enabled(self) -> None:
        if self.disabled:
            raise SkillRuntimeDisabled("SkillInvocationRuntime is disabled")
