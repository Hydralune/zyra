from __future__ import annotations

"""Session-owned permission guard used by the CodeWorker tool loop."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
import hashlib
import hmac
import time
from typing import Any

from zyra_core import EventRecord, new_id, now_iso, to_jsonable

from .classifier import PermissionClassifierAdapter
from .canonical import (
    arguments_digest,
    build_request_fingerprint,
    build_tool_identity,
    canonical_arguments_json,
)
from .decision_log import (
    PermissionDecisionEvidence,
    PermissionDecisionLog,
    PermissionDecisionStage,
)
from .evaluator import PermissionEvaluationTrace, PermissionPolicyEvaluator
from .events import PermissionEventProjector, PermissionRuntimeEventKind
from .grants import (
    ExecutionGrant,
    ExecutionGrantBinding,
    ExecutionGrantStore,
    GrantValidation,
)
from .hooks import PermissionHookAdapter
from .models import (
    PermissionDecisionRecord,
    PermissionEffect,
    PermissionEvaluationRequest,
    PermissionMode,
    PermissionRequestRecord,
    PermissionRequestStatus,
)
from .modes import ModeName, PermissionModeRuntime
from .request_queue import PermissionRequestQueue
from .risk import ToolRiskPolicy
from .store import (
    PermissionIdentityMismatch,
    PermissionRuleStore,
    PermissionStateCorrupt,
    PermissionStateDisabled,
    PermissionStateStore,
)


PERMISSION_RUNTIME_ID = "zyra-tool-permission-runtime"
PERMISSION_RUNTIME_OWNER_UNIT = "M1-S03A-01"
PERMISSION_RUNTIME_SNAPSHOT_VERSION = 1


class ToolPermissionRuntimeDisabledError(RuntimeError):
    def __init__(self, component: str) -> None:
        super().__init__(f"permission foundation component is disabled: {component}")
        self.component = component


class PermissionRuntimeIdentityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PermissionRuntimeConfig:
    mode: str = "default"
    approval_ttl_seconds: float = 300.0
    execution_grant_ttl_seconds: float = 30.0
    bypass_available: bool = False
    auto_available: bool = True
    use_auto_in_plan: bool = False
    interactive: bool = True
    headless: bool = False
    disabled: bool = False
    disable_rule_store: bool = False
    disable_request_queue: bool = False
    disable_decision_log: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.approval_ttl_seconds <= 0:
            raise ValueError("approval_ttl_seconds must be positive")
        if self.execution_grant_ttl_seconds <= 0:
            raise ValueError("execution_grant_ttl_seconds must be positive")


@dataclass(frozen=True, slots=True)
class PermissionGuardResult:
    request: PermissionEvaluationRequest
    trace: PermissionEvaluationTrace
    decision: PermissionDecisionRecord
    execution_grant: ExecutionGrant | None = field(default=None, repr=False)
    pending_request: PermissionRequestRecord | None = None
    events: tuple[EventRecord, ...] = ()
    restored_approval: bool = False

    @property
    def effect(self) -> PermissionEffect:
        return self.decision.effect

    @property
    def allowed(self) -> bool:
        return self.effect is PermissionEffect.ALLOW and self.execution_grant is not None

    @property
    def blocked(self) -> bool:
        return not self.allowed

    @property
    def ask_pending(self) -> bool:
        return self.effect is PermissionEffect.ASK and self.pending_request is not None

    @property
    def abort_loop(self) -> bool:
        return self.trace.abort_loop

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(include_arguments=False),
            "trace": self.trace.to_dict(),
            "decision": self.decision.to_dict(),
            "execution_grant": self.execution_grant.to_dict() if self.execution_grant else None,
            "pending_request": self.pending_request.to_dict() if self.pending_request else None,
            "event_ids": [event.event_id for event in self.events],
            "restored_approval": self.restored_approval,
            "allowed": self.allowed,
            "blocked": self.blocked,
            "ask_pending": self.ask_pending,
            "abort_loop": self.abort_loop,
        }


@dataclass(frozen=True, slots=True)
class TypeScriptPermissionCommitResult:
    """Durable receipt for a policy decision made by the TypeScript runtime."""

    request: PermissionEvaluationRequest
    decision: PermissionDecisionRecord
    execution_grant: ExecutionGrant | None = field(default=None, repr=False)
    pending_request: PermissionRequestRecord | None = None
    events: tuple[EventRecord, ...] = ()
    restored_approval: bool = False
    abort_loop: bool = False

    @property
    def effect(self) -> PermissionEffect:
        return self.decision.effect

    @property
    def allowed(self) -> bool:
        return self.effect is PermissionEffect.ALLOW and self.execution_grant is not None

    @property
    def blocked(self) -> bool:
        return not self.allowed

    @property
    def ask_pending(self) -> bool:
        return self.effect is PermissionEffect.ASK and self.pending_request is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(include_arguments=False),
            "decision": self.decision.to_dict(),
            "execution_grant": self.execution_grant.to_dict()
            if self.execution_grant
            else None,
            "pending_request": self.pending_request.to_dict()
            if self.pending_request
            else None,
            "event_ids": [event.event_id for event in self.events],
            "restored_approval": self.restored_approval,
            "abort_loop": self.abort_loop,
            "allowed": self.allowed,
            "blocked": self.blocked,
            "ask_pending": self.ask_pending,
            "canonical_policy_owner": "typescript",
        }


@dataclass(frozen=True, slots=True)
class PermissionGrantConsumption:
    accepted: bool
    validation: GrantValidation
    event: EventRecord | None
    tool_call_id: str
    decision_id: str
    request_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "validation": self.validation.to_dict(),
            "event_id": self.event.event_id if self.event else "",
            "tool_call_id": self.tool_call_id,
            "decision_id": self.decision_id,
            "request_id": self.request_id,
        }


class ToolPermissionRuntime:
    """The only production authority that may issue a tool execution grant."""

    def __init__(
        self,
        *,
        session_id: str,
        state_store: PermissionStateStore,
        rule_store: PermissionRuleStore,
        request_queue: PermissionRequestQueue,
        mode_runtime: PermissionModeRuntime,
        evaluator: PermissionPolicyEvaluator,
        decision_log: PermissionDecisionLog,
        grant_store: ExecutionGrantStore,
        event_projector: PermissionEventProjector | None = None,
        config: PermissionRuntimeConfig | None = None,
        workspace_root: str | Path | None = None,
        custody_fingerprint: str = "",
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not session_id:
            raise ValueError("permission runtime requires session_id")
        self.session_id = session_id
        self.state_store = state_store
        self.rule_store = rule_store
        self.request_queue = request_queue
        self.mode_runtime = mode_runtime
        self.evaluator = evaluator
        self.decision_log = decision_log
        self.grant_store = grant_store
        self.event_projector = event_projector or PermissionEventProjector()
        self.config = config or PermissionRuntimeConfig(mode=str(mode_runtime.mode))
        self.clock = clock
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None
        self.custody_fingerprint = str(custody_fingerprint or "")
        self._lock = RLock()
        self._grant_context: dict[str, dict[str, Any]] = {}
        self._grant_scope_context: dict[str, dict[str, Any]] = {}
        self._grant_identity_context: dict[str, dict[str, Any]] = {}
        self._consumption_events: dict[str, list[EventRecord]] = {}
        self._decision_count = 0
        self._allow_count = 0
        self._ask_count = 0
        self._deny_count = 0

    @classmethod
    def for_session(
        cls,
        *,
        session_id: str,
        state_path: str | Path,
        config: PermissionRuntimeConfig | None = None,
        restored_snapshot: Mapping[str, Any] | None = None,
        hook_adapter: PermissionHookAdapter | None = None,
        classifier_adapter: PermissionClassifierAdapter | None = None,
        workspace_root: str | Path | None = None,
        custody_fingerprint: str = "",
        clock: Callable[[], float] = time.time,
    ) -> "ToolPermissionRuntime":
        selected = config or PermissionRuntimeConfig()
        if selected.headless and not selected.interactive and selected.mode not in {
            str(ModeName.SEALED),
            str(ModeName.DONT_ASK),
        }:
            # A headless process cannot leave an ASK suspended indefinitely.
            # Sealed policy deterministically converts ASK to deny/recovery.
            selected = replace(selected, mode=str(ModeName.SEALED))
        if selected.disabled:
            raise ToolPermissionRuntimeDisabledError("ToolPermissionRuntime")
        state_store = PermissionStateStore(
            state_path,
            disabled=False,
        )
        _validate_session_custody_state(
            state_store,
            session_id=session_id,
            custody_fingerprint=custody_fingerprint,
        )
        if selected.disable_rule_store:
            raise ToolPermissionRuntimeDisabledError("PermissionRuleStore")
        if selected.disable_request_queue:
            raise ToolPermissionRuntimeDisabledError("PermissionRequestQueue")
        if selected.disable_decision_log:
            raise ToolPermissionRuntimeDisabledError("PermissionDecisionLog")

        state_snapshot = None
        snapshot_session_matches = True
        if isinstance(restored_snapshot, Mapping):
            if int(restored_snapshot.get("version") or 0) != PERMISSION_RUNTIME_SNAPSHOT_VERSION:
                raise PermissionStateCorrupt("unsupported permission runtime snapshot version")
            if str(restored_snapshot.get("runtime_id") or "") != PERMISSION_RUNTIME_ID:
                raise PermissionStateCorrupt("permission runtime snapshot identity is invalid")
            restored_session_id = str(restored_snapshot.get("session_id") or "")
            if not restored_session_id or restored_session_id != session_id:
                raise PermissionIdentityMismatch("permission runtime snapshot session does not match restore target")
            checksum = str(restored_snapshot.get("checksum") or "")
            if not checksum or not hmac.compare_digest(checksum, _runtime_snapshot_checksum(restored_snapshot)):
                raise PermissionStateCorrupt("permission runtime snapshot checksum mismatch")
            restored_custody = str(restored_snapshot.get("custody_fingerprint") or "")
            if restored_custody and (
                not custody_fingerprint
                or not hmac.compare_digest(restored_custody, custody_fingerprint)
            ):
                raise PermissionIdentityMismatch("permission runtime snapshot custody proof is missing or mismatched")
        if restored_snapshot:
            candidate = restored_snapshot.get("state_store")
            if isinstance(candidate, Mapping) and snapshot_session_matches:
                state_snapshot = candidate
        if state_snapshot is not None:
            state_store.restore_snapshot(state_snapshot, session_id=session_id)
        overlay = state_store.freeze_session_rules(session_id)
        rule_store = PermissionRuleStore(state_store, session_id=session_id)
        request_queue = PermissionRequestQueue(state_store, session_id=session_id, disabled=False)

        mode_snapshot = (
            restored_snapshot.get("mode")
            if isinstance(restored_snapshot, Mapping) and snapshot_session_matches
            else None
        )
        if isinstance(mode_snapshot, Mapping):
            clamped_mode_snapshot = dict(mode_snapshot)
            clamped_mode_snapshot["bypass_available"] = bool(
                selected.bypass_available and mode_snapshot.get("bypass_available", False)
            )
            clamped_mode_snapshot["auto_available"] = bool(
                selected.auto_available and mode_snapshot.get("auto_available", True)
            )
            clamped_mode_snapshot["use_auto_in_plan"] = bool(
                selected.use_auto_in_plan and mode_snapshot.get("use_auto_in_plan", False)
            )
            if (
                str(clamped_mode_snapshot.get("mode") or "") == str(ModeName.BYPASS)
                and not clamped_mode_snapshot["bypass_available"]
            ):
                clamped_mode_snapshot["mode"] = selected.mode
                clamped_mode_snapshot["auto_active"] = selected.mode == str(ModeName.AUTO)
            if (
                str(clamped_mode_snapshot.get("mode") or "") == str(ModeName.AUTO)
                and not clamped_mode_snapshot["auto_available"]
            ):
                clamped_mode_snapshot["mode"] = selected.mode if selected.mode != str(ModeName.AUTO) else "default"
                clamped_mode_snapshot["auto_active"] = False
            restored_mode = str(clamped_mode_snapshot.get("mode") or selected.mode)
            clamped_mode_snapshot["auto_active"] = bool(
                restored_mode == str(ModeName.AUTO)
                and clamped_mode_snapshot["auto_available"]
            ) or bool(
                restored_mode == str(ModeName.PLAN)
                and clamped_mode_snapshot["use_auto_in_plan"]
                and clamped_mode_snapshot["auto_available"]
            )
            pre_plan_mode = str(clamped_mode_snapshot.get("pre_plan_mode") or "")
            if pre_plan_mode == str(ModeName.BYPASS) and not clamped_mode_snapshot["bypass_available"]:
                clamped_mode_snapshot["pre_plan_mode"] = str(ModeName.DEFAULT)
            elif pre_plan_mode == str(ModeName.AUTO) and not clamped_mode_snapshot["auto_available"]:
                clamped_mode_snapshot["pre_plan_mode"] = str(ModeName.DEFAULT)
            mode_runtime = PermissionModeRuntime.from_snapshot(clamped_mode_snapshot, clock=clock)
        else:
            mode_runtime = PermissionModeRuntime(
                selected.mode,
                clock=clock,
                auto_available=selected.auto_available,
                bypass_available=selected.bypass_available,
                use_auto_in_plan=selected.use_auto_in_plan,
            )
        evaluator = PermissionPolicyEvaluator(
            mode_runtime=mode_runtime,
            risk_policy=ToolRiskPolicy(),
            hook_adapter=hook_adapter,
            classifier_adapter=classifier_adapter,
        )
        decision_log = PermissionDecisionLog()
        if isinstance(restored_snapshot, Mapping) and snapshot_session_matches:
            log_snapshot = restored_snapshot.get("decision_log")
            if isinstance(log_snapshot, Mapping):
                decision_log.restore(log_snapshot, session_id=session_id)
        grant_store = ExecutionGrantStore(clock=clock, owner_id=f"permission-session:{session_id}")
        if isinstance(restored_snapshot, Mapping) and snapshot_session_matches:
            grant_snapshot = restored_snapshot.get("grant_store")
            if isinstance(grant_snapshot, Mapping):
                grant_store.restore(grant_snapshot)
        runtime = cls(
            session_id=session_id,
            state_store=state_store,
            rule_store=rule_store,
            request_queue=request_queue,
            mode_runtime=mode_runtime,
            evaluator=evaluator,
            decision_log=decision_log,
            grant_store=grant_store,
            config=selected,
            workspace_root=workspace_root,
            custody_fingerprint=custody_fingerprint,
            clock=clock,
        )
        runtime._rule_snapshot_id = str(overlay.get("snapshot_id") or overlay.get("created_at") or "")
        return runtime

    def guard(
        self,
        request: PermissionEvaluationRequest,
        *,
        workspace_state: Mapping[str, Any] | None = None,
        workspace_state_resolver: Callable[[PermissionEvaluationRequest], Mapping[str, Any]] | None = None,
        messages: Sequence[Mapping[str, Any]] = (),
        queued_commands: Sequence[Mapping[str, Any] | str] = (),
    ) -> PermissionGuardResult:
        self._ensure_available()
        if request.session_id != self.session_id:
            raise PermissionRuntimeIdentityError("permission request session does not match runtime owner")
        if not request.worker_request_id:
            raise PermissionRuntimeIdentityError("permission request worker identity is required")
        request_workspace = Path(request.workspace_root).resolve() if request.workspace_root else None
        if self.workspace_root is None and request_workspace is not None:
            self.workspace_root = request_workspace
        elif (
            self.workspace_root is not None
            and request_workspace is not None
            and request_workspace != self.workspace_root
        ):
            raise PermissionRuntimeIdentityError("permission request workspace does not match runtime owner")
        effective_request = replace(
            request,
            mode=_model_mode(self.mode_runtime.mode),
            interactive=self.config.interactive,
            headless=self.config.headless,
        )
        with self._lock:
            self.request_queue.expire_due()
            rules = self.rule_store.list(effective=True, include_inactive=False)
            trace = self.evaluator.evaluate(
                effective_request,
                rules=tuple(rules),
                workspace_state=workspace_state,
                workspace_state_resolver=workspace_state_resolver,
                messages=messages,
                queued_commands=queued_commands,
                rule_snapshot_id=getattr(self, "_rule_snapshot_id", ""),
            )
            restored_approval = False
            approval = None
            if trace.effect is PermissionEffect.ASK:
                approval = self._approved_request_for(trace.effective_request)
                if approval is not None:
                    restored_approval = True
                    trace = self._apply_resolved_approval(trace, approval)

            pending = None
            if trace.effect is PermissionEffect.ASK:
                pending = self._create_pending_request(trace)
                decision = trace.to_decision_record(
                    request_id=pending.request_id,
                    rule_snapshot_id=getattr(self, "_rule_snapshot_id", ""),
                    mode_revision=self._mode_revision(),
                    expiry_seconds=self.config.approval_ttl_seconds,
                )
            else:
                decision = trace.to_decision_record(
                    request_id=approval.request_id if approval is not None else "",
                    rule_snapshot_id=getattr(self, "_rule_snapshot_id", ""),
                    mode_revision=self._mode_revision(),
                    expiry_seconds=self.config.execution_grant_ttl_seconds
                    if trace.effect is PermissionEffect.ALLOW
                    else None,
                )

            grant = None
            if decision.effect is PermissionEffect.ALLOW:
                # Grant construction is side-effect free outside the private
                # capability store.  Prepare it before burning a finite rule
                # or claiming a resolved approval; invalidate it if the single
                # durable permission transaction cannot commit.
                grant = self._issue_grant(decision, trace.effective_request)
            try:
                decision = self._commit_decision(
                    decision,
                    trace,
                    approval=approval,
                )
            except Exception:
                if grant is not None:
                    self.grant_store.invalidate(
                        grant,
                        reason="durable permission decision/claim commit failed",
                    )
                    self._discard_grant_context(grant.grant_id)
                raise
            external_cause = self._latest_permission_event_id(decision.request_id)
            evaluation_event = self.event_projector.request_event(
                trace.effective_request,
                kind=PermissionRuntimeEventKind.EVALUATION_STARTED,
                run_id=decision.run_id,
                task_id=decision.task_id,
                node_id=trace.effective_request.node_id,
                worker_request_id=decision.worker_request_id,
                cause_event_id=external_cause,
            )
            decision_events = self.event_projector.decision_events(
                decision,
                run_id=decision.run_id,
                task_id=decision.task_id,
                node_id=trace.effective_request.node_id,
                worker_request_id=decision.worker_request_id,
                recovery=decision.recovery_input,
                cause_event_id=evaluation_event.event_id,
            )
            events = [evaluation_event, *decision_events]
            decision_event = decision_events[0]
            if pending is not None:
                events.append(
                    self.event_projector.request_event(
                        pending,
                        kind=PermissionRuntimeEventKind.REQUEST_CREATED,
                        run_id=decision.run_id,
                        task_id=decision.task_id,
                        node_id=trace.effective_request.node_id,
                        worker_request_id=decision.worker_request_id,
                        cause_event_id=decision_event.event_id,
                    )
                )

            if decision.effect is PermissionEffect.ALLOW:
                if grant is None:
                    raise PermissionStateCorrupt("allow decision committed without an execution grant")
                issued_event = self.event_projector.grant_event(
                        _grant_event_mapping(grant),
                        consumed=False,
                        accepted=True,
                        run_id=decision.run_id,
                        task_id=decision.task_id,
                        node_id=trace.effective_request.node_id,
                        worker_request_id=decision.worker_request_id,
                        reason="exact one-use grant issued after permission decision",
                        cause_event_id=decision_event.event_id,
                    )
                events.append(issued_event)
                self._grant_context.setdefault(grant.grant_id, {})["issued_event_id"] = issued_event.event_id
            linked_request_id = pending.request_id if pending is not None else decision.request_id
            if linked_request_id:
                try:
                    self._persist_permission_event_links(linked_request_id, events)
                except Exception:
                    if grant is not None:
                        self.grant_store.invalidate(
                            grant,
                            reason="permission event causality persistence failed",
                        )
                        self._discard_grant_context(grant.grant_id)
                    raise
            self._decision_count += 1
            if decision.effect is PermissionEffect.ALLOW:
                self._allow_count += 1
            elif decision.effect is PermissionEffect.ASK:
                self._ask_count += 1
            else:
                self._deny_count += 1
            return PermissionGuardResult(
                request=trace.effective_request,
                trace=trace,
                decision=decision,
                execution_grant=grant,
                pending_request=pending,
                events=tuple(events),
                restored_approval=restored_approval,
            )

    def validate_and_consume(
        self,
        call: Any,
        grant: Any,
        execution_context: Any | None = None,
    ) -> bool:
        """ToolExecutor callback: exact validation and atomic consumption."""

        self._ensure_available()
        if not isinstance(grant, ExecutionGrant):
            return False
        metadata = getattr(call, "metadata", {}) or {}
        arguments = getattr(call, "arguments", None)
        if not isinstance(arguments, Mapping):
            return False
        execution_workspace = None
        if execution_context is not None:
            raw_workspace = getattr(execution_context, "workspace_root", None)
            if raw_workspace is not None:
                execution_workspace = Path(raw_workspace).resolve()
        if self.workspace_root is None or execution_workspace is None:
            return False
        if execution_workspace != self.workspace_root:
            return False
        scope_context = self._grant_scope_context.get(grant.grant_id)
        identity_context = self._grant_identity_context.get(grant.grant_id)
        if not isinstance(scope_context, Mapping) or not isinstance(identity_context, Mapping):
            return False
        call_run_id = str(getattr(call, "run_id", ""))
        call_task_id = str(getattr(call, "task_id", ""))
        call_tool_id = str(getattr(call, "tool_call_id", ""))
        call_tool_name = str(getattr(call, "tool_name", ""))
        if not all((call_run_id, call_task_id, call_tool_id, call_tool_name)):
            return False
        provided_namespace = str(metadata.get("tool_namespace") or metadata.get("namespace") or "")
        provided_server = str(metadata.get("server_id") or metadata.get("server_name") or "")
        if provided_namespace and provided_namespace != str(identity_context.get("namespace") or "builtin"):
            return False
        if provided_server and provided_server != str(identity_context.get("server_id") or ""):
            return False
        registry = getattr(execution_context, "registry", None)
        spec = registry.get(call_tool_name) if registry is not None else None
        expected_source = str(dict(scope_context.get("metadata") or {}).get("registered_tool_source") or "")
        actual_source = str(getattr(spec, "source", "") or "")
        schema = getattr(spec, "input_schema", None) if expected_source else None
        presented_identity = build_tool_identity(
            call_tool_name,
            namespace=str(identity_context.get("namespace") or "builtin"),
            server_id=str(identity_context.get("server_id") or ""),
            version=str(identity_context.get("version") or ""),
            schema=schema,
        )
        digest = arguments_digest(arguments)
        fingerprint = build_request_fingerprint(
            presented_identity,
            digest,
            session_id=self.session_id,
            tool_use_id=call_tool_id,
            run_id=call_run_id,
            task_id=call_task_id,
        )
        scope_metadata = dict(scope_context.get("metadata") or {})
        if expected_source:
            scope_metadata["registered_tool_source"] = actual_source
        expected_precondition = scope_metadata.get("workspace_precondition")
        if isinstance(expected_precondition, Mapping) and expected_precondition:
            scope_metadata["workspace_precondition"] = _execution_workspace_precondition(
                execution_workspace,
                arguments,
            )
        presented_scope = {
            **dict(scope_context),
            "session_id": self.session_id,
            "task_id": call_task_id,
            "run_id": call_run_id,
            "workspace_root": str(execution_workspace),
            "tool_namespace": presented_identity.namespace,
            "tool_name": presented_identity.name,
            "server_id": presented_identity.server_id,
            "argument_digest": digest,
            "request_fingerprint": fingerprint,
            "metadata": scope_metadata,
        }

        binding = ExecutionGrantBinding(
            request_id=grant.binding.request_id,
            decision_id=grant.binding.decision_id,
            session_id=self.session_id,
            tool_call_id=call_tool_id,
            tool_name=call_tool_name,
            tool_namespace=presented_identity.namespace,
            server_name=presented_identity.server_id,
            arguments_digest=digest,
            scope=presented_scope,
            expiry=grant.binding.expiry,
        )
        validation = self.grant_store.validate_and_consume(grant.authorization_token, binding)
        context = self._grant_context.get(grant.grant_id, {})
        event = self.event_projector.grant_event(
            {
                **_grant_event_mapping(grant),
                "validation": validation.to_dict(),
            },
            consumed=True,
            accepted=validation.accepted,
            run_id=str(context.get("run_id") or getattr(call, "run_id", "")),
            task_id=str(context.get("task_id") or getattr(call, "task_id", "")),
            node_id=context.get("node_id") or getattr(call, "node_id", None),
            worker_request_id=str(context.get("worker_request_id") or ""),
            reason=validation.reason,
            cause_event_id=str(context.get("issued_event_id") or ""),
        )
        self._persist_permission_event_links(grant.binding.request_id, (event,))
        with self._lock:
            self._consumption_events.setdefault(binding.tool_call_id, []).append(event)
            if validation.accepted:
                self._discard_grant_context(grant.grant_id)
        return validation.accepted

    def typescript_policy_snapshot(self) -> dict[str, Any]:
        """Return durable rules and mode facts without assigning policy ownership to Python."""

        self._ensure_available()
        with self._lock:
            rules = list(self.state_store.effective_rules(self.session_id))
            mode = _model_mode(self.mode_runtime.mode)
            snapshot = {
                "version": "zyra.typescript-permission-policy-input.v1",
                "canonical_owner": "typescript",
                "durable_store_owner": "python",
                "session_id": self.session_id,
                "workspace_root": str(self.workspace_root or ""),
                "mode": str(mode),
                # Denial counters are execution state, not policy revision. A
                # same-run batch may commit several decisions against one
                # TypeScript policy snapshot; mode/rule changes still alter the
                # digest through their semantic fields.
                "mode_revision": 0,
                "rule_snapshot_id": "",
                "interactive": bool(self.config.interactive),
                "headless": bool(self.config.headless),
                "rules": [rule.to_dict() for rule in rules],
                "python_policy_fallback": False,
            }
            from .canonical import arguments_digest

            snapshot["policy_digest"] = arguments_digest(snapshot)
            return snapshot

    def commit_typescript_decision(
        self,
        request: PermissionEvaluationRequest,
        decision_payload: Mapping[str, Any],
    ) -> TypeScriptPermissionCommitResult:
        """Persist a bound TypeScript decision and mint at most one exact grant.

        This method deliberately does not call ``PermissionPolicyEvaluator``. Python owns
        durable state, approval receipts, CAS, and grant signing; TypeScript owns the
        in-run allow/deny/ask decision.
        """

        from datetime import datetime, timedelta, timezone

        from .models import (
            PermissionRecoveryInput,
            PermissionScope,
            PermissionScopeKind,
        )
        from .modes import DenialAction

        self._ensure_available()
        if request.session_id != self.session_id:
            raise PermissionRuntimeIdentityError(
                "TypeScript permission decision session does not match runtime session"
            )
        request_workspace = str(Path(request.workspace_root).resolve()) if request.workspace_root else ""
        if request_workspace and self.workspace_root and request_workspace != str(self.workspace_root):
            raise PermissionRuntimeIdentityError(
                "TypeScript permission decision workspace does not match runtime workspace"
            )
        effective_request = replace(
            request,
            workspace_root=str(self.workspace_root or request_workspace),
            mode=_model_mode(self.mode_runtime.mode),
            interactive=bool(self.config.interactive),
            headless=bool(self.config.headless),
        )
        payload = dict(decision_payload)
        if str(payload.get("canonical_owner") or "") != "typescript":
            raise PermissionRuntimeIdentityError("permission decision owner must be TypeScript")
        binding = dict(payload.get("request_binding") or {})
        expected_binding = {
            "session_id": effective_request.session_id,
            "run_id": effective_request.run_id,
            "task_id": effective_request.task_id,
            "tool_use_id": effective_request.tool_use_id,
            "namespace": effective_request.tool_identity.namespace,
            "tool_name": effective_request.tool_identity.name,
            "server_id": effective_request.tool_identity.server_id,
            "version": effective_request.tool_identity.version,
            "schema_digest": effective_request.tool_identity.schema_digest,
            "arguments_digest": effective_request.arguments_digest,
            "request_fingerprint": effective_request.request_fingerprint,
        }
        for key, expected in expected_binding.items():
            if str(binding.get(key) or "") != str(expected or ""):
                raise PermissionRuntimeIdentityError(
                    f"TypeScript permission binding mismatch for {key}"
                )
        if str(payload.get("arguments_digest") or "") != effective_request.arguments_digest:
            raise PermissionRuntimeIdentityError("TypeScript arguments digest mismatch")
        if str(payload.get("request_fingerprint") or "") != effective_request.request_fingerprint:
            raise PermissionRuntimeIdentityError("TypeScript request fingerprint mismatch")
        try:
            effect = PermissionEffect(str(payload.get("effect") or ""))
        except ValueError as exc:
            raise PermissionRuntimeIdentityError("invalid TypeScript permission effect") from exc
        if str(payload.get("mode") or "") != str(effective_request.mode):
            raise PermissionRuntimeIdentityError("TypeScript permission mode snapshot is stale")
        current_policy_digest = str(
            self.typescript_policy_snapshot().get("policy_digest") or ""
        )
        if str(payload.get("policy_digest") or "") != current_policy_digest:
            raise PermissionRuntimeIdentityError(
                "TypeScript permission policy snapshot no longer matches durable state"
            )
        matched_rule_ids = tuple(
            str(value) for value in payload.get("matched_rule_ids", ()) if str(value)
        )
        durable_rules = {
            rule.rule_id: rule for rule in self.state_store.effective_rules(self.session_id)
        }
        for rule_id in matched_rule_ids:
            if rule_id not in durable_rules:
                raise PermissionRuntimeIdentityError(
                    f"TypeScript decision references unavailable durable rule {rule_id}"
                )
        restored_approval = False
        approval = self._approved_request_for(effective_request) if effect == PermissionEffect.ASK else None
        if approval is not None:
            effect = PermissionEffect.ALLOW
            restored_approval = True
        reason_code = str(payload.get("reason_code") or "typescript_permission_decision")
        reason = str(payload.get("reason") or "TypeScript runtime permission decision")
        if restored_approval:
            reason_code = "typescript_exact_approval_consumed"
            reason = "Exact durable user approval consumed by the TypeScript permission runtime"
        recovery_alternatives = tuple(
            str(value)
            for value in payload.get("recovery_alternatives", ())
            if str(value)
        )
        scope = PermissionScope(
            kind=PermissionScopeKind.ACTION,
            session_id=effective_request.session_id,
            task_id=effective_request.task_id,
            run_id=effective_request.run_id,
            workspace_root=effective_request.workspace_root,
            tool_namespace=effective_request.tool_identity.namespace,
            tool_name=effective_request.tool_identity.name,
            server_id=effective_request.tool_identity.server_id,
            argument_digest=effective_request.arguments_digest,
            request_fingerprint=effective_request.request_fingerprint,
            metadata={"canonical_policy_owner": "typescript"},
        )
        with self._lock:
            self.request_queue.expire_due()
            abort_loop = False
            if effect == PermissionEffect.DENY:
                denial_outcome = self.mode_runtime.record_denial(
                    headless=effective_request.headless
                )
                if denial_outcome.action is DenialAction.ABORT:
                    abort_loop = True
                    reason_code = "denial.limit_abort"
                    reason = f"{reason}; denial limit requires loop abort"
            elif effect == PermissionEffect.ALLOW:
                self.mode_runtime.record_success()
            pending: PermissionRequestRecord | None = None
            if effect == PermissionEffect.ASK:
                expiry = datetime.now(timezone.utc) + timedelta(
                    seconds=self.config.approval_ttl_seconds
                )
                pending = self.request_queue.create(
                    PermissionRequestRecord(
                        session_id=effective_request.session_id,
                        task_id=effective_request.task_id,
                        run_id=effective_request.run_id,
                        worker_request_id=effective_request.worker_request_id,
                        tool_use_id=effective_request.tool_use_id,
                        tool_identity=effective_request.tool_identity,
                        arguments_digest=effective_request.arguments_digest,
                        request_fingerprint=effective_request.request_fingerprint,
                        scope=scope,
                        expires_at=expiry.isoformat(),
                        reason_code=reason_code,
                        reason=reason,
                        rule_snapshot_id=str(payload.get("rule_snapshot_id") or ""),
                        mode=effective_request.mode,
                        metadata={
                            "canonical_policy_owner": "typescript",
                            "policy_digest": str(payload.get("policy_digest") or ""),
                            "no_python_policy_fallback": True,
                        },
                    )
                )
            decision = PermissionDecisionRecord(
                effect=effect,
                mode=effective_request.mode,
                request_fingerprint=effective_request.request_fingerprint,
                arguments_digest=effective_request.arguments_digest,
                tool_use_id=effective_request.tool_use_id,
                tool_identity=effective_request.tool_identity,
                session_id=effective_request.session_id,
                task_id=effective_request.task_id,
                run_id=effective_request.run_id,
                worker_request_id=effective_request.worker_request_id,
                request_id=(approval.request_id if approval is not None else pending.request_id if pending else ""),
                reason_code=reason_code,
                reason=reason,
                scope=scope,
                matched_rule_ids=matched_rule_ids,
                rule_snapshot_id=str(payload.get("rule_snapshot_id") or ""),
                mode_revision=int(payload.get("mode_revision") or 0),
                metadata={
                    "canonical_policy_owner": "typescript",
                    "policy_digest": str(payload.get("policy_digest") or ""),
                    "evaluated_at": str(payload.get("evaluated_at") or ""),
                    "no_python_policy_fallback": True,
                    "approval_restored": restored_approval,
                    "abort_loop": abort_loop,
                    "human_intervention_count": 0,
                    "human_intervention_delta": 0,
                },
            )
            consume_rule_id = matched_rule_ids[0] if matched_rule_ids and effect == PermissionEffect.ALLOW else ""
            committed = self.state_store.commit_decision(
                decision,
                consume_rule_id=consume_rule_id,
                consume_approval_request_id=(approval.request_id if approval is not None else ""),
            )
            winning_rule = durable_rules.get(consume_rule_id)
            self.decision_log.append(
                session_id=committed.session_id,
                run_id=committed.run_id,
                task_id=committed.task_id,
                node_id=effective_request.node_id,
                worker_request_id=committed.worker_request_id,
                turn_id=effective_request.turn_id,
                tool_call_id=committed.tool_use_id,
                tool_name=committed.tool_identity.name,
                namespace=committed.tool_identity.namespace,
                server_name=committed.tool_identity.server_id,
                arguments_digest=committed.arguments_digest,
                scope_digest=committed.request_fingerprint,
                effect=committed.effect,
                reason=committed.reason,
                mode=str(committed.mode),
                risk="typescript_policy",
                request_id=committed.request_id,
                rule_id=consume_rule_id,
                rule_source=(str(winning_rule.source) if winning_rule is not None else "typescript"),
                recovery_alternatives=recovery_alternatives,
                evidence=(),
                metadata=committed.metadata,
                decision_id=committed.decision_id,
            )
            grant = (
                self._issue_grant(committed, effective_request)
                if committed.effect == PermissionEffect.ALLOW
                else None
            )
            events: list[EventRecord] = []
            if pending is not None:
                events.append(
                    self.event_projector.request_event(
                        pending,
                        kind=PermissionRuntimeEventKind.REQUEST_CREATED,
                        run_id=effective_request.run_id,
                        task_id=effective_request.task_id,
                        node_id=effective_request.node_id,
                        worker_request_id=effective_request.worker_request_id,
                    )
                )
            recovery = (
                PermissionRecoveryInput(
                    decision_id=committed.decision_id,
                    session_id=effective_request.session_id,
                    task_id=effective_request.task_id,
                    run_id=effective_request.run_id,
                    tool_use_id=effective_request.tool_use_id,
                    reason_code=committed.reason_code,
                    retryable=bool(recovery_alternatives),
                    alternatives=tuple(
                        {
                            "kind": "permission_alternative",
                            "instruction": alternative,
                        }
                        for alternative in recovery_alternatives
                    ),
                    constraints={
                        "arguments_digest": effective_request.arguments_digest,
                        "tool_identity": effective_request.tool_identity.to_dict(),
                        "scope": scope.to_dict(),
                    },
                    metadata={"canonical_policy_owner": "typescript"},
                )
                if committed.effect == PermissionEffect.DENY
                else None
            )
            decision_events = self.event_projector.decision_events(
                committed,
                run_id=effective_request.run_id,
                task_id=effective_request.task_id,
                node_id=effective_request.node_id,
                worker_request_id=effective_request.worker_request_id,
                recovery=recovery,
            )
            events.extend(decision_events)
            decision_event = decision_events[0]
            if grant is not None:
                issued_event = self.event_projector.grant_event(
                    grant,
                    consumed=False,
                    accepted=True,
                    run_id=effective_request.run_id,
                    task_id=effective_request.task_id,
                    node_id=effective_request.node_id,
                    worker_request_id=effective_request.worker_request_id,
                    reason="typescript_permission_decision",
                    cause_event_id=decision_event.event_id,
                )
                events.append(issued_event)
                self._grant_context.setdefault(grant.grant_id, {})[
                    "issued_event_id"
                ] = issued_event.event_id
            self._decision_count += 1
            if committed.effect == PermissionEffect.ALLOW:
                self._allow_count += 1
            elif committed.effect == PermissionEffect.ASK:
                self._ask_count += 1
            else:
                self._deny_count += 1
            return TypeScriptPermissionCommitResult(
                request=effective_request,
                decision=committed,
                execution_grant=grant,
                pending_request=pending,
                events=tuple(events),
                restored_approval=restored_approval,
                abort_loop=abort_loop,
            )

    def drain_execution_events(self, tool_call_id: str) -> tuple[EventRecord, ...]:
        with self._lock:
            return tuple(self._consumption_events.pop(tool_call_id, ()))

    def resolve(self, response: Any) -> PermissionRequestRecord:
        self._ensure_available()
        return self.request_queue.resolve(response)

    def mark_delivered(
        self,
        request_id: str,
        *,
        expected_revision: int,
        channel: str,
    ) -> PermissionRequestRecord:
        self._ensure_available()
        return self.request_queue.mark_delivered(
            request_id,
            expected_revision=expected_revision,
            channel=channel,
        )

    def snapshot(self) -> dict[str, Any]:
        self._ensure_available()
        with self._lock:
            snapshot = {
                "version": PERMISSION_RUNTIME_SNAPSHOT_VERSION,
                "runtime_id": PERMISSION_RUNTIME_ID,
                "owner_unit": PERMISSION_RUNTIME_OWNER_UNIT,
                "session_id": self.session_id,
                "custody_fingerprint": self.custody_fingerprint,
                "state_store": self.state_store.snapshot(self.session_id),
                "mode": self.mode_runtime.snapshot(),
                "decision_log": self.decision_log.snapshot(session_id=self.session_id),
                "grant_store": self.grant_store.snapshot(),
                "metrics": self.metadata(),
                "restore_contract": {
                    "pending_requests_preserved": True,
                    "standing_rule_overlay_preserved": True,
                    "unconsumed_execution_grants_invalidated": True,
                    "signing_key_persisted": False,
                },
            }
            snapshot["checksum"] = _runtime_snapshot_checksum(snapshot)
            return snapshot

    def metadata(self) -> dict[str, str]:
        queue_metrics = self.request_queue.metrics()
        state = self.state_store.read_state()
        session_metrics = (
            state.get("metadata", {}).get("session_metrics", {}).get(self.session_id, {})
        )
        if not isinstance(session_metrics, Mapping):
            session_metrics = {}
        return {
            "permission_runtime_id": PERMISSION_RUNTIME_ID,
            "permission_runtime_owner_unit": PERMISSION_RUNTIME_OWNER_UNIT,
            "permission_runtime_session_id": self.session_id,
            "permission_runtime_mode": str(self.mode_runtime.mode),
            "permission_runtime_decisions": str(self._decision_count),
            "permission_runtime_allows": str(self._allow_count),
            "permission_runtime_asks": str(self._ask_count),
            "permission_runtime_denies": str(self._deny_count),
            "permission_runtime_denials": str(self._deny_count),
            "permission_runtime_pending": str(queue_metrics.get("pending", 0)),
            "permission_runtime_consumed_approvals": str(
                int(session_metrics.get("approval_execution_claim_count") or 0)
            ),
            "permission_runtime_human_intervention_count": str(
                int(session_metrics.get("human_intervention_count") or 0)
            ),
            "permission_runtime_state_owner": "PermissionStateStore",
            "permission_runtime_grant_owner": self.grant_store.owner_id,
            "permission_runtime_custody_bound": str(bool(self.custody_fingerprint)).lower(),
        }

    def _ensure_available(self) -> None:
        if self.config.disabled:
            raise ToolPermissionRuntimeDisabledError("ToolPermissionRuntime")
        if self.config.disable_rule_store:
            raise ToolPermissionRuntimeDisabledError("PermissionRuleStore")
        if self.config.disable_request_queue:
            raise ToolPermissionRuntimeDisabledError("PermissionRequestQueue")
        if self.config.disable_decision_log:
            raise ToolPermissionRuntimeDisabledError("PermissionDecisionLog")
        if getattr(self.state_store, "disabled", False):
            raise ToolPermissionRuntimeDisabledError("PermissionStateStore")

    def _create_pending_request(self, trace: PermissionEvaluationTrace) -> PermissionRequestRecord:
        request = trace.effective_request
        expiry = (
            datetime.now(timezone.utc) + timedelta(seconds=self.config.approval_ttl_seconds)
        ).isoformat()
        record = PermissionRequestRecord(
            session_id=request.session_id,
            task_id=request.task_id,
            run_id=request.run_id,
            worker_request_id=request.worker_request_id,
            tool_use_id=request.tool_use_id,
            tool_identity=request.tool_identity,
            arguments_digest=request.arguments_digest,
            request_fingerprint=request.request_fingerprint,
            scope=trace.scope,
            expires_at=expiry,
            reason_code=trace.reason_code,
            reason=trace.reason,
            rule_snapshot_id=getattr(self, "_rule_snapshot_id", ""),
            mode=request.mode,
            metadata={
                "owner_unit": PERMISSION_RUNTIME_OWNER_UNIT,
                "risk": str(trace.base_decision.risk),
                "safety": str(trace.base_decision.safety),
                "recovery_alternatives": list(trace.recovery_alternatives),
                "human_intervention_count": 0,
            },
        )
        return self.request_queue.create(record)

    def _approved_request_for(
        self,
        request: PermissionEvaluationRequest,
    ) -> PermissionRequestRecord | None:
        records = self.request_queue.list(status=PermissionRequestStatus.APPROVED)
        for record in reversed(records):
            if record.metadata.get("execution_claim_decision_id"):
                continue
            try:
                expiry = datetime.fromisoformat(record.expires_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= datetime.now(timezone.utc):
                continue
            if (
                record.session_id == request.session_id
                and record.task_id == request.task_id
                and record.run_id == request.run_id
                and record.tool_use_id == request.tool_use_id
                and record.tool_identity == request.tool_identity
                and record.arguments_digest == request.arguments_digest
                and record.request_fingerprint == request.request_fingerprint
                and record.scope.contains(request)
            ):
                return record
        return None

    @staticmethod
    def _apply_resolved_approval(
        trace: PermissionEvaluationTrace,
        approval: PermissionRequestRecord,
    ) -> PermissionEvaluationTrace:
        evidence = (
            *trace.evidence,
            PermissionDecisionEvidence(
                stage=PermissionDecisionStage.USER_RESOLUTION,
                effect="allow",
                reason="exact pending permission request was resolved allow",
                source=approval.resolution_channel or "user",
                source_id=approval.request_id,
                bypass_immune=False,
                priority=950,
                metadata={
                    "request_revision": approval.revision,
                    "resolved_by": approval.resolved_by,
                    "exact_identity": True,
                    "resolve_once": True,
                },
            ),
        )
        return replace(
            trace,
            effect=PermissionEffect.ALLOW,
            reason="exact resolved permission request authorized one execution",
            reason_code="user_resolution.allow_exact",
            evidence=evidence,
            metadata={**trace.metadata, "resolved_approval_request_id": approval.request_id},
        )

    def _commit_decision(
        self,
        decision: PermissionDecisionRecord,
        trace: PermissionEvaluationTrace,
        *,
        approval: PermissionRequestRecord | None = None,
    ) -> PermissionDecisionRecord:
        winning = trace.winning_rule
        committed = self.state_store.commit_decision(
            decision,
            consume_rule_id=(
                winning.rule.rule_id
                if (
                    winning is not None
                    and winning.rule.effect is PermissionEffect.ALLOW
                    and decision.effect is PermissionEffect.ALLOW
                )
                else ""
            ),
            consume_approval_request_id=approval.request_id if approval is not None else "",
        )
        # PermissionStateStore is the durable authority.  The structured
        # decision log is an in-memory query index rebuilt from checkpoints;
        # it is appended only after the security transaction commits.
        self.decision_log.append(
            decision_id=committed.decision_id,
            session_id=committed.session_id,
            run_id=committed.run_id,
            task_id=committed.task_id,
            node_id=trace.effective_request.node_id,
            worker_request_id=committed.worker_request_id,
            turn_id=trace.effective_request.turn_id,
            tool_call_id=committed.tool_use_id,
            tool_name=committed.tool_identity.name,
            namespace=committed.tool_identity.namespace,
            server_name=committed.tool_identity.server_id,
            arguments_digest=committed.arguments_digest,
            scope_digest=committed.scope.request_fingerprint or committed.scope.argument_digest,
            effect=committed.effect,
            reason=committed.reason,
            mode=str(committed.mode),
            risk=str(trace.base_decision.risk),
            request_id=committed.request_id,
            rule_id=winning.rule.rule_id if winning else "",
            rule_source=str(winning.rule.source) if winning else "",
            recovery_alternatives=trace.recovery_alternatives,
            evidence=trace.evidence,
            metadata={
                "reason_code": committed.reason_code,
                "rule_snapshot_id": committed.rule_snapshot_id,
                "abort_loop": trace.abort_loop,
                "human_intervention_count": committed.metadata.get(
                    "human_intervention_count",
                    0,
                ),
                "human_intervention_delta": committed.metadata.get(
                    "human_intervention_delta",
                    0,
                ),
            },
        )
        return committed

    def _issue_grant(
        self,
        decision: PermissionDecisionRecord,
        request: PermissionEvaluationRequest,
    ) -> ExecutionGrant:
        request_id = decision.request_id or f"direct-{decision.decision_id}"
        expiry = self.clock() + self.config.execution_grant_ttl_seconds
        binding = ExecutionGrantBinding(
            request_id=request_id,
            decision_id=decision.decision_id,
            session_id=decision.session_id,
            tool_call_id=decision.tool_use_id,
            tool_name=decision.tool_identity.name,
            tool_namespace=decision.tool_identity.namespace,
            server_name=decision.tool_identity.server_id,
            arguments_digest=decision.arguments_digest,
            scope=decision.scope.to_dict(),
            expiry=expiry,
        )
        grant = self.grant_store.issue(binding)
        self._grant_context[grant.grant_id] = {
            "run_id": decision.run_id,
            "task_id": decision.task_id,
            "node_id": request.node_id,
            "worker_request_id": decision.worker_request_id,
        }
        self._grant_scope_context[grant.grant_id] = decision.scope.to_dict()
        self._grant_identity_context[grant.grant_id] = request.tool_identity.to_dict()
        return grant

    def _discard_grant_context(self, grant_id: str) -> None:
        self._grant_context.pop(grant_id, None)
        self._grant_scope_context.pop(grant_id, None)
        self._grant_identity_context.pop(grant_id, None)

    def _latest_permission_event_id(self, request_id: str) -> str:
        if not request_id:
            return ""
        state = self.state_store.read_state()
        integration = state.get("metadata", {}).get("permission_integration", {})
        links = integration.get("event_links", {}) if isinstance(integration, Mapping) else {}
        history = links.get(request_id) if isinstance(links, Mapping) else None
        if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
            return ""
        for item in reversed(history):
            if isinstance(item, Mapping) and str(item.get("event_id") or ""):
                return str(item["event_id"])
        return ""

    def _persist_permission_event_links(
        self,
        request_id: str,
        events: Sequence[EventRecord],
    ) -> None:
        if not request_id or not events:
            return
        projected: list[dict[str, str]] = []
        for event in events:
            query_session = event.payload.get("query_session") if isinstance(event.payload, Mapping) else None
            envelope = (
                query_session.get("permission_runtime")
                if isinstance(query_session, Mapping)
                else None
            )
            if not isinstance(envelope, Mapping):
                continue
            projected.append(
                {
                    "phase": str(envelope.get("phase") or envelope.get("kind") or "permission_event"),
                    "event_id": event.event_id,
                    "cause_event_id": str(envelope.get("cause_event_id") or ""),
                    "linked_at": now_iso(),
                }
            )
        if not projected:
            return

        def mutate(state: dict[str, Any]) -> None:
            metadata = state.setdefault("metadata", {})
            integration = metadata.setdefault(
                "permission_integration",
                {
                    "schema": "zyra.permission-integration-state.v1",
                    "owner_unit": "M1-S03A-02",
                    "retry_descriptors": {},
                    "session_modes": {},
                    "event_links": {},
                    "metadata": {"legacy_store_is_authority": False},
                },
            )
            links = integration.setdefault("event_links", {})
            history = links.setdefault(request_id, [])
            if not isinstance(history, list):
                raise PermissionStateCorrupt("permission event link history is corrupt")
            seen = {
                str(item.get("event_id") or "")
                for item in history
                if isinstance(item, Mapping)
            }
            history.extend(item for item in projected if item["event_id"] not in seen)
            del history[:-64]

        self.state_store.mutate(mutate)

    def _mode_revision(self) -> int:
        snapshot = self.mode_runtime.snapshot()
        return int(snapshot["denials"]["total"])


def _validate_session_custody_state(
    state_store: PermissionStateStore,
    *,
    session_id: str,
    custody_fingerprint: str,
) -> None:
    state = state_store.read_state()
    custody = state.get("metadata", {}).get("session_custody", {})
    records = custody.get("records", {}) if isinstance(custody, Mapping) else {}
    record = records.get(session_id) if isinstance(records, Mapping) else None
    if not isinstance(record, Mapping):
        if custody_fingerprint:
            raise PermissionIdentityMismatch("permission session custody record is missing")
        return
    expected = str(record.get("custody_fingerprint") or "")
    if not custody_fingerprint:
        raise PermissionIdentityMismatch("permission session custody proof is required")
    if not expected or not hmac.compare_digest(expected, custody_fingerprint):
        raise PermissionIdentityMismatch("permission session custody proof does not match state owner")


def _runtime_snapshot_checksum(snapshot: Mapping[str, Any]) -> str:
    payload = {key: to_jsonable(value) for key, value in snapshot.items() if key != "checksum"}
    encoded = canonical_arguments_json(payload)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _execution_workspace_precondition(
    workspace_root: Path,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    raw_path = arguments.get("path") or arguments.get("file_path")
    if not raw_path:
        return {}
    try:
        candidate = Path(str(raw_path))
        target = (workspace_root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        target.relative_to(workspace_root)
    except (OSError, ValueError):
        return {"outside_workspace": True}
    try:
        stat = target.stat()
    except FileNotFoundError:
        return {"exists": False}
    except OSError as error:
        return {"exists": None, "error": type(error).__name__}
    return {
        "exists": True,
        "device": int(getattr(stat, "st_dev", 0)),
        "inode": int(getattr(stat, "st_ino", 0)),
        "mode": int(stat.st_mode),
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }


def _model_mode(value: ModeName | str) -> PermissionMode:
    text = str(value)
    aliases = {
        "acceptEdits": PermissionMode.ACCEPT_EDITS,
        "dontAsk": PermissionMode.DONT_ASK,
        "bypassPermissions": PermissionMode.BYPASS,
    }
    if text in aliases:
        return aliases[text]
    return PermissionMode(text)


def _grant_event_mapping(grant: ExecutionGrant) -> dict[str, Any]:
    return {
        **grant.event_payload(),
        "tool_name": grant.binding.tool_name,
        "namespace": grant.binding.tool_namespace,
        "server_name": grant.binding.server_name,
        "arguments_digest": grant.binding.arguments_digest,
        "scope_digest": grant.binding.scope,
        "expiry": grant.binding.expiry,
    }
