from __future__ import annotations

"""Single deterministic permission evaluator.

This module is the authority boundary between a ``tool_use`` request and any
side effect.  Rules, tool risk, hooks, modes, and optional classifier proposals
are deliberately composed here so no adapter can become a second permission
system.  The evaluator returns evidence and a typed decision; it never calls a
tool and never treats model-authored arguments as approval.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Callable

from zyra_core import to_jsonable

from .classifier import (
    PermissionClassifierAdapter,
    PermissionClassifierAggregate,
    PermissionClassifierEffect,
    PermissionClassifierInput,
)
from .decision_log import PermissionDecisionEvidence, PermissionDecisionStage
from .hooks import (
    PermissionHookAdapter,
    PermissionHookAggregate,
    PermissionHookEffect,
    PermissionHookInput,
)
from .models import (
    PermissionDecisionRecord,
    PermissionEffect,
    PermissionEvaluationRequest,
    PermissionMode,
    PermissionRecoveryInput,
    PermissionRuleRecord,
    PermissionRuleSource,
    PermissionScope,
    PermissionScopeKind,
)
from .modes import DenialAction, DenialOutcome, ModeEvaluation, PermissionModeRuntime
from .risk import RiskBand, SafetyClass, ToolRiskPolicy, ToolSpecificPermissionCheck
from .rules import RuleEvaluation, RuleMatch, evaluate_rules


class PermissionEvaluationReason(StrEnum):
    RULE_DENY = "rule.deny"
    RULE_ASK = "rule.ask"
    RULE_ALLOW = "rule.allow"
    TOOL_HARD_DENY = "tool.hard_deny"
    TOOL_REVIEW = "tool.review"
    TOOL_LOW_RISK = "tool.low_risk"
    HOOK_ABORT = "hook.abort"
    HOOK_DENY = "hook.deny"
    HOOK_ASK = "hook.ask"
    MODE_ALLOW = "mode.allow"
    MODE_ASK = "mode.ask"
    MODE_DENY = "mode.deny"
    SEALED_DENY = "sealed.deny"
    CLASSIFIER_ADVISORY = "classifier.advisory"
    DENIAL_LIMIT_ABORT = "denial.limit_abort"


class PermissionEvaluatorDisabledError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PermissionBaseDecision:
    effect: PermissionEffect
    reason: str
    reason_code: str
    risk: RiskBand
    safety: SafetyClass
    bypass_immune: bool = False
    classifier_eligible: bool = False
    interactive_required: bool = False
    explicit_low_risk_allowlisted: bool = False
    safe_edit_ready: bool = False
    recovery_alternatives: tuple[str, ...] = ()
    winning_rule: RuleMatch | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def safety_critical(self) -> bool:
        return self.safety is SafetyClass.HARD_GUARD

    def to_mode_input(self) -> dict[str, Any]:
        return {
            "effect": str(self.effect),
            "reason": self.reason,
            "reason_code": self.reason_code,
            "risk": str(self.risk),
            "safety": str(self.safety),
            "bypass_immune": self.bypass_immune,
            "safety_critical": self.safety_critical,
            "classifier_eligible": self.classifier_eligible,
            "interactive_required": self.interactive_required,
            "explicit_low_risk_allowlisted": self.explicit_low_risk_allowlisted,
            "safe_edit_ready": self.safe_edit_ready,
            "recovery_alternatives": self.recovery_alternatives,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class PermissionEvaluationTrace:
    original_request: PermissionEvaluationRequest
    effective_request: PermissionEvaluationRequest
    effect: PermissionEffect
    reason: str
    reason_code: str
    scope: PermissionScope
    base_decision: PermissionBaseDecision
    risk_check: ToolSpecificPermissionCheck
    rule_evaluation: RuleEvaluation
    pre_tool_hook: PermissionHookAggregate
    permission_hook: PermissionHookAggregate | None
    mode_evaluation: ModeEvaluation
    classifier_evaluation: PermissionClassifierAggregate | None
    evidence: tuple[PermissionDecisionEvidence, ...]
    recovery_alternatives: tuple[str, ...]
    denial_outcome: DenialOutcome | None = None
    abort_loop: bool = False
    evaluated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def hook_changed_arguments(self) -> bool:
        return self.original_request.arguments_digest != self.effective_request.arguments_digest

    @property
    def winning_rule(self) -> RuleMatch | None:
        return self.rule_evaluation.winning_match

    @property
    def matched_rule_ids(self) -> tuple[str, ...]:
        return self.rule_evaluation.matched_rule_ids

    @property
    def matched_rule_sources(self) -> tuple[PermissionRuleSource, ...]:
        return tuple(item.rule.source for item in self.rule_evaluation.matches)

    def to_decision_record(
        self,
        *,
        request_id: str = "",
        rule_snapshot_id: str = "",
        mode_revision: int = 0,
        expiry_seconds: float | None = None,
    ) -> PermissionDecisionRecord:
        request = self.effective_request
        recovery_input = None
        if self.effect is PermissionEffect.DENY:
            recovery_input = PermissionRecoveryInput(
                decision_id="pending",
                session_id=request.session_id,
                task_id=request.task_id,
                run_id=request.run_id,
                tool_use_id=request.tool_use_id,
                reason_code=self.reason_code,
                retryable=bool(self.recovery_alternatives),
                alternatives=tuple(
                    {"kind": "permission_alternative", "instruction": item}
                    for item in self.recovery_alternatives
                ),
                constraints={
                    "arguments_digest": request.arguments_digest,
                    "tool_identity": request.tool_identity.to_dict(),
                    "scope": self.scope.to_dict(),
                },
                metadata={"owner_unit": "M1-03A", "human_intervention_count": 0},
            )
        expiry = None
        if expiry_seconds is not None:
            expiry = (datetime.now(timezone.utc) + timedelta(seconds=max(1.0, expiry_seconds))).isoformat()
        decision = PermissionDecisionRecord(
            effect=self.effect,
            mode=request.mode,
            request_fingerprint=request.request_fingerprint,
            arguments_digest=request.arguments_digest,
            tool_use_id=request.tool_use_id,
            tool_identity=request.tool_identity,
            session_id=request.session_id,
            task_id=request.task_id,
            run_id=request.run_id,
            worker_request_id=request.worker_request_id,
            reason_code=self.reason_code,
            reason=self.reason,
            scope=self.scope,
            matched_rule_ids=self.matched_rule_ids,
            matched_rule_sources=self.matched_rule_sources,
            request_id=request_id,
            rule_snapshot_id=rule_snapshot_id,
            mode_revision=mode_revision,
            hook_evidence=tuple(
                item.to_dict()
                for item in self.evidence
                if item.stage in {PermissionDecisionStage.PRE_TOOL_HOOK, PermissionDecisionStage.SAFETY}
            ),
            classifier_evidence=(
                self.classifier_evaluation.to_dict() if self.classifier_evaluation else {}
            ),
            recovery_input=recovery_input,
            expires_at=expiry,
            metadata={
                "owner_unit": "M1-03A",
                "risk": str(self.base_decision.risk),
                "safety": str(self.base_decision.safety),
                "hook_changed_arguments": self.hook_changed_arguments,
                "abort_loop": self.abort_loop,
                "human_intervention_count": 0,
                **dict(self.metadata),
            },
        )
        if recovery_input is not None:
            recovery_input = replace(recovery_input, decision_id=decision.decision_id)
            decision = replace(decision, recovery_input=recovery_input)
        return decision

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_request": self.original_request.to_dict(include_arguments=False),
            "effective_request": self.effective_request.to_dict(include_arguments=False),
            "effect": str(self.effect),
            "reason": self.reason,
            "reason_code": self.reason_code,
            "scope": self.scope.to_dict(),
            "base_decision": self.base_decision.to_mode_input(),
            "risk_check": self.risk_check.as_base_decision(),
            "rule_evaluation": self.rule_evaluation.to_dict(),
            "pre_tool_hook": self.pre_tool_hook.to_dict(),
            "permission_hook": self.permission_hook.to_dict() if self.permission_hook else None,
            "mode_evaluation": to_jsonable(self.mode_evaluation),
            "classifier_evaluation": self.classifier_evaluation.to_dict() if self.classifier_evaluation else None,
            "evidence": [item.to_dict() for item in self.evidence],
            "recovery_alternatives": list(self.recovery_alternatives),
            "denial_outcome": to_jsonable(self.denial_outcome) if self.denial_outcome else None,
            "abort_loop": self.abort_loop,
            "hook_changed_arguments": self.hook_changed_arguments,
            "evaluated_at": self.evaluated_at,
            "metadata": to_jsonable(dict(self.metadata)),
        }


class PermissionPolicyEvaluator:
    """Composes every permission input into one final deterministic result."""

    def __init__(
        self,
        *,
        mode_runtime: PermissionModeRuntime,
        risk_policy: ToolRiskPolicy | None = None,
        hook_adapter: PermissionHookAdapter | None = None,
        classifier_adapter: PermissionClassifierAdapter | None = None,
        disabled: bool = False,
    ) -> None:
        self.mode_runtime = mode_runtime
        self.risk_policy = risk_policy or ToolRiskPolicy()
        self.hook_adapter = hook_adapter or PermissionHookAdapter()
        self.classifier_adapter = classifier_adapter or PermissionClassifierAdapter()
        self.disabled = bool(disabled)

    def evaluate(
        self,
        request: PermissionEvaluationRequest,
        *,
        rules: Sequence[PermissionRuleRecord] = (),
        workspace_state: Mapping[str, Any] | None = None,
        workspace_state_resolver: Callable[[PermissionEvaluationRequest], Mapping[str, Any]] | None = None,
        messages: Sequence[Mapping[str, Any]] = (),
        queued_commands: Sequence[Mapping[str, Any] | str] = (),
        rule_snapshot_id: str = "",
    ) -> PermissionEvaluationTrace:
        if self.disabled:
            raise PermissionEvaluatorDisabledError("PermissionPolicyEvaluator is disabled")
        hook_input = self._hook_input(request)
        pre_hook = self.hook_adapter.run_pre_tool_use(hook_input)
        effective_request = self._request_after_hook(request, pre_hook)
        rule_evaluation = evaluate_rules(tuple(rules), effective_request)
        resolved_workspace_state = (
            dict(workspace_state_resolver(effective_request))
            if workspace_state_resolver is not None
            else dict(workspace_state or {})
        )
        merged_workspace_state = {
            "workspace_root": effective_request.workspace_root,
            **resolved_workspace_state,
        }
        risk = self.risk_policy.classify(
            {
                "tool_name": effective_request.tool_identity.name,
                "namespace": effective_request.tool_identity.namespace,
                "server_name": effective_request.tool_identity.server_id,
                "capabilities": tuple(effective_request.attributes.get("capabilities") or ()),
            },
            effective_request.arguments,
            merged_workspace_state,
        )
        evidence: list[PermissionDecisionEvidence] = []
        evidence.extend(self._hook_evidence(pre_hook, PermissionDecisionStage.PRE_TOOL_HOOK))
        evidence.extend(self._rule_evidence(rule_evaluation))
        evidence.append(self._risk_evidence(risk))

        base = self._compose_base(rule_evaluation, risk, pre_hook)
        permission_hook: PermissionHookAggregate | None = None
        if base.effect is PermissionEffect.ASK:
            permission_hook = self.hook_adapter.run_permission_request(
                self._hook_input(effective_request)
            )
            evidence.extend(self._hook_evidence(permission_hook, PermissionDecisionStage.SAFETY))
            base = self._tighten_with_hook(base, permission_hook)

        classifier_result: PermissionClassifierAggregate | None = None

        def classifier(context: Any) -> Mapping[str, Any]:
            nonlocal classifier_result
            classifier_input = PermissionClassifierInput.build(
                session_id=effective_request.session_id,
                run_id=effective_request.run_id,
                task_id=effective_request.task_id,
                worker_id=effective_request.worker_request_id,
                tool_call_id=effective_request.tool_use_id,
                tool_name=effective_request.tool_identity.name,
                server_name=effective_request.tool_identity.server_id,
                arguments=effective_request.arguments,
                messages=messages,
                queued_commands=queued_commands,
                policy_labels=effective_request.risk_tags,
                metadata={
                    "request_fingerprint": effective_request.request_fingerprint,
                    "deterministic_base_effect": str(base.effect),
                    "rule_snapshot_id": rule_snapshot_id,
                },
            )
            classifier_result = self.classifier_adapter.classify(classifier_input)
            proposed_effect = (
                "allow"
                if classifier_result.effect is PermissionClassifierEffect.ALLOW
                and classifier_result.can_auto_allow
                else "deny"
                if classifier_result.effect is PermissionClassifierEffect.DENY
                else "ask"
            )
            return {
                "effect": proposed_effect,
                "reason": classifier_result.reason,
                "advisory_only": True,
                "can_auto_allow": classifier_result.can_auto_allow,
            }

        classifier_callback = classifier if base.classifier_eligible else None
        mode_evaluation = self.mode_runtime.evaluate_mode(
            base.to_mode_input(),
            {
                **base.to_mode_input(),
                "headless": effective_request.headless,
                "interactive": effective_request.interactive,
                "requires_interaction": effective_request.requires_interaction,
            },
            classifier_callback,
        )
        if classifier_result is not None:
            evidence.append(
                PermissionDecisionEvidence(
                    stage=PermissionDecisionStage.CLASSIFIER,
                    effect=str(classifier_result.effect),
                    reason=classifier_result.reason,
                    source="permission_classifier_adapter",
                    advisory_only=True,
                    bypass_immune=False,
                    priority=0,
                    metadata={
                        "has_failure": classifier_result.has_failure,
                        "can_auto_allow": classifier_result.can_auto_allow,
                        "invocation_count": len(classifier_result.invocations),
                    },
                )
            )

        final_effect = PermissionEffect(str(mode_evaluation.effect))
        reason = mode_evaluation.reason
        reason_code = self._reason_code(final_effect, base, mode_evaluation)
        recovery = tuple(mode_evaluation.recovery_alternatives or base.recovery_alternatives)
        denial_outcome = None
        abort_loop = pre_hook.prevent_continuation or bool(
            permission_hook is not None and permission_hook.prevent_continuation
        )
        if final_effect is PermissionEffect.DENY:
            denial_outcome = self.mode_runtime.record_denial(headless=effective_request.headless)
            if denial_outcome.action is DenialAction.ABORT:
                abort_loop = True
                reason_code = PermissionEvaluationReason.DENIAL_LIMIT_ABORT
                reason = f"{reason}; denial limit requires loop abort"
        elif final_effect is PermissionEffect.ALLOW:
            self.mode_runtime.record_success()

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
            metadata={
                "owner_unit": "M1-03A",
                "exact_identity": True,
                "workspace_precondition": dict(resolved_workspace_state.get("workspace_precondition") or {}),
                "registered_tool_source": str(effective_request.metadata.get("registered_tool_source") or ""),
            },
        )
        evidence.append(
            PermissionDecisionEvidence(
                stage=PermissionDecisionStage.FINAL,
                effect=str(final_effect),
                reason=reason,
                source="permission_policy_evaluator",
                source_id=rule_snapshot_id,
                bypass_immune=base.bypass_immune,
                priority=1000,
                metadata={
                    "mode": str(self.mode_runtime.mode),
                    "base_effect": str(base.effect),
                    "reason_code": str(reason_code),
                    "abort_loop": abort_loop,
                },
            )
        )
        return PermissionEvaluationTrace(
            original_request=request,
            effective_request=effective_request,
            effect=final_effect,
            reason=reason,
            reason_code=str(reason_code),
            scope=scope,
            base_decision=base,
            risk_check=risk,
            rule_evaluation=rule_evaluation,
            pre_tool_hook=pre_hook,
            permission_hook=permission_hook,
            mode_evaluation=mode_evaluation,
            classifier_evaluation=classifier_result,
            evidence=tuple(evidence),
            recovery_alternatives=recovery,
            denial_outcome=denial_outcome,
            abort_loop=abort_loop,
            metadata={
                "rule_snapshot_id": rule_snapshot_id,
                "raw_approved_argument_ignored": effective_request.arguments.get("approved") is True,
                "human_intervention_count": 0,
            },
        )

    def _hook_input(self, request: PermissionEvaluationRequest) -> PermissionHookInput:
        return PermissionHookInput.build(
            session_id=request.session_id,
            run_id=request.run_id,
            task_id=request.task_id,
            worker_id=request.worker_request_id,
            tool_call_id=request.tool_use_id,
            tool_name=request.tool_identity.name,
            server_name=request.tool_identity.server_id,
            arguments=request.arguments,
            mode=str(request.mode),
            workspace_root=request.workspace_root,
            interactive=request.interactive,
            sealed=request.mode is PermissionMode.SEALED,
            metadata={
                "request_fingerprint": request.request_fingerprint,
                "node_id": request.node_id,
                "operation": request.operation,
                "tool_namespace": request.tool_identity.namespace,
                "tool_server_id": request.tool_identity.server_id,
                **dict(request.metadata),
            },
        )

    @staticmethod
    def _request_after_hook(
        request: PermissionEvaluationRequest,
        hook: PermissionHookAggregate,
    ) -> PermissionEvaluationRequest:
        if not hook.input_changed:
            return request
        # Re-canonicalization is mandatory.  Approval identity always binds the
        # post-hook arguments that the executor will actually receive.
        return replace(
            request,
            arguments=dict(hook.effective_input.arguments),
            arguments_digest="",
            request_fingerprint="",
            metadata={
                **request.metadata,
                "pre_hook_original_arguments_digest": request.arguments_digest,
                "pre_hook_effective_arguments_digest": hook.effective_input.arguments_digest,
            },
        )

    @staticmethod
    def _compose_base(
        rule_evaluation: RuleEvaluation,
        risk: ToolSpecificPermissionCheck,
        hook: PermissionHookAggregate,
    ) -> PermissionBaseDecision:
        matches = rule_evaluation.matches
        deny = next((item for item in matches if item.effect is PermissionEffect.DENY), None)
        ask = next((item for item in matches if item.effect is PermissionEffect.ASK), None)
        allow = next((item for item in matches if item.effect is PermissionEffect.ALLOW), None)
        if hook.effect is PermissionHookEffect.ABORT:
            return PermissionBaseDecision(
                PermissionEffect.DENY,
                hook.reason or "pre-tool hook aborted execution",
                PermissionEvaluationReason.HOOK_ABORT,
                RiskBand.CRITICAL,
                SafetyClass.HARD_GUARD,
                bypass_immune=True,
                recovery_alternatives=("remove the hook abort condition", "choose an allowed tool path"),
                metadata={"hook_prevent_continuation": True},
            )
        if deny is not None:
            return PermissionBaseDecision(
                PermissionEffect.DENY,
                deny.rule.reason or "explicit deny rule matched",
                PermissionEvaluationReason.RULE_DENY,
                RiskBand.CRITICAL,
                SafetyClass.HARD_GUARD,
                bypass_immune=True,
                recovery_alternatives=("use an operation outside the denied scope",),
                winning_rule=deny,
            )
        if risk.effect == "deny":
            return PermissionBaseDecision(
                PermissionEffect.DENY,
                risk.reason,
                risk.rule_code or PermissionEvaluationReason.TOOL_HARD_DENY,
                risk.risk,
                risk.safety,
                bypass_immune=True,
                classifier_eligible=False,
                interactive_required=risk.interactive_required,
                recovery_alternatives=risk.recovery_alternatives,
                metadata=risk.metadata,
            )
        if hook.effect is PermissionHookEffect.DENY:
            return PermissionBaseDecision(
                PermissionEffect.DENY,
                hook.reason or "pre-tool hook denied execution",
                PermissionEvaluationReason.HOOK_DENY,
                RiskBand.HIGH,
                risk.safety,
                bypass_immune=True,
                recovery_alternatives=risk.recovery_alternatives,
            )
        if ask is not None:
            return PermissionBaseDecision(
                PermissionEffect.ASK,
                ask.rule.reason or "explicit ask rule matched",
                PermissionEvaluationReason.RULE_ASK,
                max(risk.risk, RiskBand.MEDIUM, key=_risk_rank),
                risk.safety,
                bypass_immune=True,
                classifier_eligible=False,
                interactive_required=risk.interactive_required,
                recovery_alternatives=risk.recovery_alternatives,
                winning_rule=ask,
            )
        if hook.effect is PermissionHookEffect.ASK:
            return PermissionBaseDecision(
                PermissionEffect.ASK,
                hook.reason or "pre-tool hook requested approval",
                PermissionEvaluationReason.HOOK_ASK,
                risk.risk,
                risk.safety,
                bypass_immune=True,
                recovery_alternatives=risk.recovery_alternatives,
            )
        if risk.effect == "ask":
            # A deterministic one-use ACTION capability is itself the strong
            # approval demanded by shell/edit review.  It may satisfy an ASK,
            # but never an interactive-required or hard-deny check.  Broad or
            # reusable allows still cannot bypass safety friction.
            strong_exact_allow = bool(
                allow is not None
                and allow.rule.scope.kind is PermissionScopeKind.ACTION
                and bool(
                    allow.rule.scope.argument_digest
                    or allow.rule.scope.request_fingerprint
                )
                and allow.rule.max_uses == 1
            )
            if (
                allow is not None
                and not risk.interactive_required
                and (not risk.bypass_immune or strong_exact_allow)
            ):
                return PermissionBaseDecision(
                    PermissionEffect.ALLOW,
                    allow.rule.reason or "exact standing allow rule matched",
                    PermissionEvaluationReason.RULE_ALLOW,
                    risk.risk,
                    risk.safety,
                    recovery_alternatives=risk.recovery_alternatives,
                    winning_rule=allow,
                )
            return PermissionBaseDecision(
                PermissionEffect.ASK,
                risk.reason,
                risk.rule_code or PermissionEvaluationReason.TOOL_REVIEW,
                risk.risk,
                risk.safety,
                bypass_immune=risk.bypass_immune,
                classifier_eligible=risk.classifier_eligible,
                interactive_required=risk.interactive_required,
                recovery_alternatives=risk.recovery_alternatives,
                metadata=risk.metadata,
            )
        explicit_low_risk = risk.safety is SafetyClass.LOW_RISK_READ and risk.risk is RiskBand.LOW
        safe_edit = risk.safety is SafetyClass.FAST_EDIT and risk.effect == "allow"
        return PermissionBaseDecision(
            PermissionEffect.ALLOW,
            allow.rule.reason if allow is not None and allow.rule.reason else risk.reason,
            PermissionEvaluationReason.RULE_ALLOW if allow else PermissionEvaluationReason.TOOL_LOW_RISK,
            risk.risk,
            risk.safety,
            explicit_low_risk_allowlisted=explicit_low_risk,
            safe_edit_ready=safe_edit,
            recovery_alternatives=risk.recovery_alternatives,
            winning_rule=allow,
            metadata=risk.metadata,
        )

    @staticmethod
    def _tighten_with_hook(
        base: PermissionBaseDecision,
        hook: PermissionHookAggregate,
    ) -> PermissionBaseDecision:
        if hook.effect is PermissionHookEffect.ABORT:
            return replace(
                base,
                effect=PermissionEffect.DENY,
                reason=hook.reason or "permission hook aborted execution",
                reason_code=str(PermissionEvaluationReason.HOOK_ABORT),
                bypass_immune=True,
                classifier_eligible=False,
                metadata={**base.metadata, "hook_prevent_continuation": True},
            )
        if hook.effect is PermissionHookEffect.DENY:
            return replace(
                base,
                effect=PermissionEffect.DENY,
                reason=hook.reason or "permission hook denied execution",
                reason_code=str(PermissionEvaluationReason.HOOK_DENY),
                bypass_immune=True,
                classifier_eligible=False,
            )
        if hook.effect is PermissionHookEffect.ASK:
            return replace(
                base,
                effect=PermissionEffect.ASK,
                reason=hook.reason or base.reason,
                reason_code=str(PermissionEvaluationReason.HOOK_ASK),
                bypass_immune=True,
                classifier_eligible=False,
            )
        # Hook ALLOW is explanation only and cannot loosen the base result.
        return base

    @staticmethod
    def _reason_code(
        effect: PermissionEffect,
        base: PermissionBaseDecision,
        mode: ModeEvaluation,
    ) -> str:
        if effect is PermissionEffect.DENY and str(mode.mode) == "sealed":
            return str(PermissionEvaluationReason.SEALED_DENY)
        if effect is PermissionEffect.ALLOW and mode.classifier_consulted:
            return str(PermissionEvaluationReason.CLASSIFIER_ADVISORY)
        if effect is PermissionEffect.ALLOW:
            return str(PermissionEvaluationReason.MODE_ALLOW)
        if effect is PermissionEffect.ASK:
            return str(base.reason_code or PermissionEvaluationReason.MODE_ASK)
        return str(base.reason_code or PermissionEvaluationReason.MODE_DENY)

    @staticmethod
    def _rule_evidence(evaluation: RuleEvaluation) -> tuple[PermissionDecisionEvidence, ...]:
        values = []
        for match in evaluation.matches:
            stage = {
                PermissionEffect.DENY: PermissionDecisionStage.DENY_RULE,
                PermissionEffect.ASK: PermissionDecisionStage.ASK_RULE,
                PermissionEffect.ALLOW: PermissionDecisionStage.MODE,
            }[match.effect]
            values.append(
                PermissionDecisionEvidence(
                    stage=stage,
                    effect=str(match.effect),
                    reason=match.rule.reason or "permission rule matched",
                    source=str(match.rule.source),
                    source_id=match.rule.rule_id,
                    bypass_immune=match.effect in {PermissionEffect.DENY, PermissionEffect.ASK},
                    priority=match.rule.priority,
                    metadata={
                        "specificity": match.specificity,
                        "scope_kind": str(match.rule.scope.kind),
                        "matched_argument": bool(match.matched_argument),
                    },
                )
            )
        return tuple(values)

    @staticmethod
    def _risk_evidence(risk: ToolSpecificPermissionCheck) -> PermissionDecisionEvidence:
        stage = (
            PermissionDecisionStage.TOOL_DENY
            if risk.effect == "deny"
            else PermissionDecisionStage.INTERACTIVE
            if risk.interactive_required
            else PermissionDecisionStage.SAFETY
            if risk.bypass_immune
            else PermissionDecisionStage.TOOL_RISK
        )
        return PermissionDecisionEvidence(
            stage=stage,
            effect=risk.effect,
            reason=risk.reason,
            source="tool_risk_policy",
            source_id=risk.rule_code,
            bypass_immune=risk.bypass_immune,
            priority=800,
            metadata={
                "risk": str(risk.risk),
                "safety": str(risk.safety),
                "classifier_eligible": risk.classifier_eligible,
                "fast_edit_missing_prereqs": list(risk.fast_edit_missing_prereqs),
            },
        )

    @staticmethod
    def _hook_evidence(
        aggregate: PermissionHookAggregate,
        stage: PermissionDecisionStage,
    ) -> tuple[PermissionDecisionEvidence, ...]:
        return tuple(
            PermissionDecisionEvidence(
                stage=stage,
                effect=str(invocation.proposal.effect),
                reason=invocation.proposal.reason,
                source=invocation.source,
                source_id=invocation.hook_id,
                bypass_immune=invocation.proposal.effect
                in {PermissionHookEffect.ASK, PermissionHookEffect.DENY, PermissionHookEffect.ABORT},
                advisory_only=invocation.proposal.effect is PermissionHookEffect.ALLOW,
                matched=True,
                metadata={
                    "status": str(invocation.status),
                    "input_digest": invocation.input_digest,
                    "output_digest": invocation.output_digest,
                    "tightened": invocation.tightened,
                },
            )
            for invocation in aggregate.invocations
        )


def _risk_rank(value: RiskBand) -> int:
    return {
        RiskBand.LOW: 1,
        RiskBand.MEDIUM: 2,
        RiskBand.HIGH: 3,
        RiskBand.CRITICAL: 4,
    }[value]
