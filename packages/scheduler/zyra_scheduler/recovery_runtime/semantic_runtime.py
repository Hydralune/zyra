from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .action_runtime import ExecutionResult
from .contracts import (
    RecoveryAction,
    RecoveryActionReceipt,
    RecoveryContext,
    RecoveryPlan,
    RecoverySignalKind,
    RouteLayer,
    stable_digest,
    utc_now,
)


class RecoverySemanticError(RuntimeError):
    pass


class RecoverySemanticRejected(RecoverySemanticError):
    pass


class SemanticRequirement(StrEnum):
    TOOL_BLOCKED = "tool_blocked"
    PERMISSION_PENDING = "permission_pending"
    CONTEXT_COMPACTED = "context_compacted"
    CHECKPOINT_RESUMED = "checkpoint_resumed"
    MODEL_CHANGED = "model_changed"
    PROVIDER_CHANGED = "provider_changed"
    CREDENTIAL_CHANGED = "credential_changed"
    TRANSPORT_CHANGED = "transport_changed"
    WORKSPACE_CHANGED = "workspace_changed"
    BACKEND_CHANGED = "backend_changed"
    WORKER_LEASE_CHANGED = "worker_lease_changed"
    GRAPH_CHANGED = "graph_changed"
    MEMORY_WRITTEN = "memory_written"
    MCP_CONTROL_CHANGED = "mcp_control_changed"
    SIDE_EFFECT_NOT_REPLAYED = "side_effect_not_replayed"
    RESPONSE_NOT_REPLAYED = "response_not_replayed"
    FAILURE_COUNTER_STABLE = "failure_counter_stable"
    WORKSPACE_CONFLICT_PRESERVED = "workspace_conflict_preserved"
    TASK_TERMINATED = "task_terminated"
    CONTINUATION_CHANGED = "continuation_changed"


@dataclass(frozen=True, slots=True)
class SemanticCheck:
    requirement: SemanticRequirement
    passed: bool
    message: str
    evidence_refs: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement": self.requirement.value,
            "passed": self.passed,
            "message": self.message,
            "evidence_refs": list(self.evidence_refs),
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class SemanticExpectation:
    signal_kind: RecoverySignalKind
    allowed_primary_actions: tuple[RecoveryAction, ...]
    forbidden_actions: tuple[RecoveryAction, ...]
    required_sequence: tuple[RecoveryAction, ...]
    required_effects: tuple[SemanticRequirement, ...]
    expected_route_layer: RouteLayer | None
    allow_waiting: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_kind": self.signal_kind.value,
            "allowed_primary_actions": [item.value for item in self.allowed_primary_actions],
            "forbidden_actions": [item.value for item in self.forbidden_actions],
            "required_sequence": [item.value for item in self.required_sequence],
            "required_effects": [item.value for item in self.required_effects],
            "expected_route_layer": self.expected_route_layer.value if self.expected_route_layer else "",
            "allow_waiting": self.allow_waiting,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SemanticPreflight:
    plan_id: str
    expectation: SemanticExpectation
    selected_action: RecoveryAction
    action_sequence: tuple[RecoveryAction, ...]
    checks: tuple[SemanticCheck, ...]
    created_at: str = field(default_factory=utc_now)

    @property
    def accepted(self) -> bool:
        return all(item.passed for item in self.checks)

    def require_accepted(self) -> "SemanticPreflight":
        if not self.accepted:
            raise RecoverySemanticRejected("; ".join(item.message for item in self.checks if not item.passed))
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-semantic-preflight/v1",
            "plan_id": self.plan_id,
            "expectation": self.expectation.to_dict(),
            "selected_action": self.selected_action.value,
            "action_sequence": [item.value for item in self.action_sequence],
            "checks": [item.to_dict() for item in self.checks],
            "accepted": self.accepted,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class SemanticOutcomeReport:
    report_id: str
    plan_id: str
    signal_kind: RecoverySignalKind
    action: RecoveryAction
    checks: tuple[SemanticCheck, ...]
    receipt_ids: tuple[str, ...]
    routing_memory_id: str
    continuation_receipt_id: str
    created_at: str = field(default_factory=utc_now)

    @property
    def accepted(self) -> bool:
        return all(item.passed for item in self.checks)

    def require_accepted(self) -> "SemanticOutcomeReport":
        if not self.accepted:
            raise RecoverySemanticRejected("; ".join(item.message for item in self.checks if not item.passed))
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-semantic-outcome-report/v1",
            "report_id": self.report_id,
            "plan_id": self.plan_id,
            "signal_kind": self.signal_kind.value,
            "action": self.action.value,
            "checks": [item.to_dict() for item in self.checks],
            "receipt_ids": list(self.receipt_ids),
            "routing_memory_id": self.routing_memory_id,
            "continuation_receipt_id": self.continuation_receipt_id,
            "accepted": self.accepted,
            "created_at": self.created_at,
        }


class RecoverySemanticRuntime:
    def __init__(self) -> None:
        self._expectations = self._build_expectations()

    def preflight(self, plan: RecoveryPlan, context: RecoveryContext) -> SemanticPreflight:
        expectation = self._resolved_expectation(plan)
        sequence = self._sequence(plan)
        checks: list[SemanticCheck] = []
        checks.append(self._check(
            SemanticRequirement.CONTINUATION_CHANGED,
            plan.decision.selected.action in expectation.allowed_primary_actions,
            f"selected action {plan.decision.selected.action.value} is allowed for {plan.signal.kind.value}",
            failure=f"selected action {plan.decision.selected.action.value} violates semantic policy for {plan.signal.kind.value}",
        ))
        forbidden_selected = [item for item in sequence if item in expectation.forbidden_actions]
        checks.append(self._check(
            SemanticRequirement.CONTINUATION_CHANGED,
            not forbidden_selected,
            "action sequence contains no signal-specific forbidden action",
            failure="forbidden recovery actions: " + ", ".join(item.value for item in forbidden_selected),
        ))
        if expectation.required_sequence:
            checks.append(self._check(
                SemanticRequirement.CONTINUATION_CHANGED,
                self._contains_ordered(sequence, expectation.required_sequence),
                "required recovery sequence is preserved",
                failure="required ordered sequence is missing: " + ", ".join(item.value for item in expectation.required_sequence),
            ))
        if plan.signal.kind in {RecoverySignalKind.PERMISSION_DENIED, RecoverySignalKind.PERMISSION_PENDING, RecoverySignalKind.PERMISSION_ASK}:
            pending = bool(
                context.permission_state.get("pending_request_id")
                or context.permission_state.get("pending_count")
                or plan.signal.refs.permission_request_id
                or plan.signal.refs.request_id
            )
            checks.append(self._check(
                SemanticRequirement.PERMISSION_PENDING,
                pending,
                "permission request identity is available",
                failure="permission recovery lacks a pending/request identity",
            ))
            checks.append(self._check(
                SemanticRequirement.TOOL_BLOCKED,
                not bool(context.permission_state.get("tool_dispatch_allowed", False)),
                "tool dispatch remains blocked",
                failure="permission recovery context permits tool dispatch",
            ))
        if plan.signal.partial_output or plan.signal.observable_side_effect:
            checkpoint = bool(context.checkpoint_state.get("checkpoint_id") or plan.signal.refs.checkpoint_id)
            checks.append(self._check(
                SemanticRequirement.CHECKPOINT_RESUMED,
                checkpoint,
                "checkpoint identity is present for replay-sensitive recovery",
                failure="partial output or side effect lacks an exact-resume checkpoint",
            ))
            checks.append(self._check(
                SemanticRequirement.SIDE_EFFECT_NOT_REPLAYED,
                RecoveryAction.RETRY not in sequence,
                "blind retry is absent after partial output or side effect",
                failure="replay-sensitive recovery contains blind retry",
            ))
        if expectation.expected_route_layer is not None:
            selected_layer = plan.decision.selected.route_layer
            checks.append(self._check(
                SemanticRequirement.CONTINUATION_CHANGED,
                selected_layer is expectation.expected_route_layer,
                f"selected route layer is {expectation.expected_route_layer.value}",
                failure=f"selected route layer {selected_layer} differs from {expectation.expected_route_layer.value}",
            ))
        state_fusion = context.metadata.get("state_fusion")
        family_count = int(state_fusion.get("family_count") or 0) if isinstance(state_fusion, Mapping) else 0
        checks.append(self._check(
            SemanticRequirement.CONTINUATION_CHANGED,
            family_count >= 3,
            f"recovery decision consumes {family_count} real state families",
            failure="recovery decision consumes fewer than three real state families",
        ))
        return SemanticPreflight(
            plan_id=plan.plan_id,
            expectation=expectation,
            selected_action=plan.decision.selected.action,
            action_sequence=sequence,
            checks=tuple(checks),
        )

    def verify(
        self,
        plan: RecoveryPlan,
        context: RecoveryContext,
        execution: ExecutionResult,
        *,
        require_feedback: bool = True,
    ) -> SemanticOutcomeReport:
        if execution.plan.plan_id != plan.plan_id:
            raise RecoverySemanticRejected("execution belongs to another recovery plan")
        expectation = self._resolved_expectation(plan)
        receipts = execution.receipts
        proof = dict(execution.applied_proof)
        continuation = dict(proof.get("continuation") or {})
        continuation_id = str(continuation.get("receipt_id") or "")
        checks: list[SemanticCheck] = []
        checks.append(self._check(
            SemanticRequirement.CONTINUATION_CHANGED,
            bool(proof.get("applied")) and bool(continuation_id),
            "applied proof contains a changed continuation receipt",
            failure="recovery outcome lacks an applied continuation proof",
            refs=(str(proof.get("proof_id") or ""), continuation_id),
        ))
        checks.extend(self._effect_checks(plan, context, execution, expectation, require_feedback=require_feedback))
        required = set(expectation.required_effects)
        if not require_feedback:
            required.discard(SemanticRequirement.MEMORY_WRITTEN)
        observed = {item.requirement for item in checks if item.passed}
        for missing in sorted(required - observed, key=lambda item: item.value):
            checks.append(self._check(
                missing,
                False,
                "",
                failure=f"required semantic effect was not proven: {missing.value}",
            ))
        report_id = "recoverysemantic:" + stable_digest({
            "plan_id": plan.plan_id,
            "outcome_id": execution.outcome.outcome_id,
            "checks": [item.to_dict() for item in checks],
        })[:40]
        return SemanticOutcomeReport(
            report_id=report_id,
            plan_id=plan.plan_id,
            signal_kind=plan.signal.kind,
            action=execution.outcome.action,
            checks=tuple(checks),
            receipt_ids=tuple(item.receipt_id for item in receipts),
            routing_memory_id=execution.routing_memory_id,
            continuation_receipt_id=continuation_id,
        )

    def expectation(self, signal_kind: RecoverySignalKind) -> SemanticExpectation:
        return self._expectations[signal_kind]

    def contract(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-semantic-runtime-contract/v1",
            "expectations": {kind.value: value.to_dict() for kind, value in self._expectations.items()},
            "action_selector": False,
            "policy_owner": "RecoveryDecisionRuntime",
            "postcondition_owner": "RecoverySemanticRuntime",
        }

    def _effect_checks(
        self,
        plan: RecoveryPlan,
        context: RecoveryContext,
        execution: ExecutionResult,
        expectation: SemanticExpectation,
        *,
        require_feedback: bool,
    ) -> list[SemanticCheck]:
        receipts = execution.receipts
        checks: list[SemanticCheck] = []
        changed = [item for item in receipts if item.changed_execution]
        route_receipts = [item for item in receipts if item.route_decision is not None]
        checkpoint_receipts = [item for item in receipts if item.checkpoint_receipt is not None]
        if SemanticRequirement.TOOL_BLOCKED in expectation.required_effects:
            tool_allowed = bool((execution.applied_proof.get("continuation") or {}).get("metadata", {}).get("tool_dispatch_allowed", False))
            checks.append(self._check(
                SemanticRequirement.TOOL_BLOCKED,
                not tool_allowed,
                "continuation keeps tool dispatch blocked",
                failure="permission continuation dispatched a tool",
                refs=tuple(item.receipt_id for item in receipts),
            ))
        if SemanticRequirement.PERMISSION_PENDING in expectation.required_effects:
            permission = [item for item in receipts if item.action is RecoveryAction.ASK_PERMISSION]
            checks.append(self._check(
                SemanticRequirement.PERMISSION_PENDING,
                any(item.after.get("pending") or item.after.get("request_id") for item in permission),
                "PermissionControlPlane created or preserved a pending request",
                failure="permission action lacks a pending request receipt",
                refs=tuple(item.external_receipt_ref for item in permission),
            ))
        if SemanticRequirement.CONTEXT_COMPACTED in expectation.required_effects:
            compact = [item for item in receipts if item.action is RecoveryAction.COMPACT]
            checks.append(self._check(
                SemanticRequirement.CONTEXT_COMPACTED,
                any(item.changed_execution and item.after != item.before for item in compact),
                "compact owner changed canonical context",
                failure="compact action did not change canonical context",
                refs=tuple(item.external_receipt_ref for item in compact),
            ))
        if SemanticRequirement.CHECKPOINT_RESUMED in expectation.required_effects:
            checks.append(self._check(
                SemanticRequirement.CHECKPOINT_RESUMED,
                any(item.checkpoint_receipt and item.checkpoint_receipt.resume_token for item in checkpoint_receipts)
                or bool(execution.outcome.checkpoint_id),
                "checkpoint resume receipt is linked",
                failure="recovery lacks a checkpoint resume receipt",
                refs=tuple(item.checkpoint_receipt.receipt_id for item in checkpoint_receipts if item.checkpoint_receipt),
            ))
        layer_requirements = {
            SemanticRequirement.GRAPH_CHANGED: RouteLayer.GRAPH,
            SemanticRequirement.WORKER_LEASE_CHANGED: RouteLayer.WORKER,
            SemanticRequirement.BACKEND_CHANGED: RouteLayer.BACKEND,
            SemanticRequirement.PROVIDER_CHANGED: RouteLayer.PROVIDER,
            SemanticRequirement.CREDENTIAL_CHANGED: RouteLayer.CREDENTIAL,
            SemanticRequirement.TRANSPORT_CHANGED: RouteLayer.TRANSPORT,
            SemanticRequirement.WORKSPACE_CHANGED: RouteLayer.WORKSPACE,
            SemanticRequirement.MODEL_CHANGED: RouteLayer.MODEL,
        }
        for requirement, layer in layer_requirements.items():
            if requirement not in expectation.required_effects:
                continue
            changes = [
                change
                for receipt in route_receipts
                for change in receipt.route_decision.changes
                if change.layer is layer
            ]
            checks.append(self._check(
                requirement,
                any(change.applied and change.before_ref != change.after_ref and change.receipt_id for change in changes),
                f"canonical {layer.value} owner applied a different route",
                failure=f"{layer.value} route did not change through its owner",
                refs=tuple(change.receipt_id for change in changes),
            ))
        if SemanticRequirement.MCP_CONTROL_CHANGED in expectation.required_effects:
            mcp = [item for item in receipts if item.action is RecoveryAction.AUTHENTICATE_MCP]
            checks.append(self._check(
                SemanticRequirement.MCP_CONTROL_CHANGED,
                any(item.changed_execution and item.external_receipt_ref for item in mcp),
                "MCP control owner changed auth/reconnect state",
                failure="MCP recovery lacks a changed control receipt",
                refs=tuple(item.external_receipt_ref for item in mcp),
            ))
        if require_feedback and SemanticRequirement.MEMORY_WRITTEN in expectation.required_effects:
            checks.append(self._check(
                SemanticRequirement.MEMORY_WRITTEN,
                bool(execution.routing_memory_id),
                "applied outcome produced routing memory",
                failure="applied subagent/route recovery did not write routing memory",
                refs=(execution.routing_memory_id,),
            ))
        if SemanticRequirement.SIDE_EFFECT_NOT_REPLAYED in expectation.required_effects:
            replayed = any(
                item.checkpoint_receipt
                and bool(item.checkpoint_receipt.metadata.get("committed_side_effect_replay", False))
                for item in checkpoint_receipts
            )
            checks.append(self._check(
                SemanticRequirement.SIDE_EFFECT_NOT_REPLAYED,
                not replayed,
                "committed side effects were not replayed",
                failure="checkpoint resume permits committed side-effect replay",
            ))
        if SemanticRequirement.RESPONSE_NOT_REPLAYED in expectation.required_effects:
            replayed = any(
                item.checkpoint_receipt
                and bool(item.checkpoint_receipt.metadata.get("processed_response_replay", False))
                for item in checkpoint_receipts
            )
            checks.append(self._check(
                SemanticRequirement.RESPONSE_NOT_REPLAYED,
                not replayed,
                "processed responses were not replayed",
                failure="checkpoint resume permits processed response replay",
            ))
        if SemanticRequirement.FAILURE_COUNTER_STABLE in expectation.required_effects:
            before = int(context.metadata.get("failure_count_before") or 0)
            after = int(context.metadata.get("failure_count_after") or before)
            checks.append(self._check(
                SemanticRequirement.FAILURE_COUNTER_STABLE,
                before == after,
                "requirement control replan did not increment failure history",
                failure="requirement change polluted failure counters",
            ))
        if SemanticRequirement.WORKSPACE_CONFLICT_PRESERVED in expectation.required_effects:
            preserved = any(
                item.action is RecoveryAction.REPLAN
                and bool(plan.signal.details.get("conflict_paths") or plan.signal.details.get("dirty_paths"))
                for item in receipts
            )
            checks.append(self._check(
                SemanticRequirement.WORKSPACE_CONFLICT_PRESERVED,
                preserved,
                "workspace conflict evidence is preserved for replan",
                failure="workspace recovery discarded dirty/conflict evidence",
            ))
        if SemanticRequirement.TASK_TERMINATED in expectation.required_effects:
            aborts = [item for item in receipts if item.action is RecoveryAction.ABORT]
            checks.append(self._check(
                SemanticRequirement.TASK_TERMINATED,
                any(item.changed_execution for item in aborts),
                "TaskState entered a terminal state",
                failure="abort did not terminate TaskState",
                refs=tuple(item.external_receipt_ref for item in aborts),
            ))
        checks.append(self._check(
            SemanticRequirement.CONTINUATION_CHANGED,
            bool(changed),
            "at least one action receipt changed execution",
            failure="recovery produced no changed action receipt",
            refs=tuple(item.receipt_id for item in changed),
        ))
        return checks

    @staticmethod
    def _sequence(plan: RecoveryPlan) -> tuple[RecoveryAction, ...]:
        return tuple(
            RecoveryAction(str(item))
            for item in plan.provenance.get("action_sequence") or (plan.decision.selected.action.value,)
        )

    @staticmethod
    def _contains_ordered(actual: Sequence[RecoveryAction], required: Sequence[RecoveryAction]) -> bool:
        cursor = 0
        for item in actual:
            if cursor < len(required) and item is required[cursor]:
                cursor += 1
        return cursor == len(required)

    @staticmethod
    def _check(
        requirement: SemanticRequirement,
        passed: bool,
        success: str,
        *,
        failure: str,
        refs: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> SemanticCheck:
        return SemanticCheck(
            requirement=requirement,
            passed=bool(passed),
            message=success if passed else failure,
            evidence_refs=tuple(str(item) for item in refs if str(item)),
            metadata=copy.deepcopy(dict(metadata or {})),
        )

    @staticmethod
    def _build_expectations() -> dict[RecoverySignalKind, SemanticExpectation]:
        E = SemanticExpectation
        A = RecoveryAction
        R = SemanticRequirement
        L = RouteLayer
        values: dict[RecoverySignalKind, SemanticExpectation] = {}

        def add(
            kind: RecoverySignalKind,
            allowed: Sequence[RecoveryAction],
            effects: Sequence[SemanticRequirement],
            reason: str,
            *,
            forbidden: Sequence[RecoveryAction] = (),
            sequence: Sequence[RecoveryAction] = (),
            layer: RouteLayer | None = None,
            waiting: bool = False,
        ) -> None:
            values[kind] = E(kind, tuple(allowed), tuple(forbidden), tuple(sequence), tuple(effects), layer, waiting, reason)

        for kind in (RecoverySignalKind.PERMISSION_DENIED, RecoverySignalKind.PERMISSION_PENDING, RecoverySignalKind.PERMISSION_ASK):
            add(kind, (A.ASK_PERMISSION, A.REPLAN, A.ABORT), (R.TOOL_BLOCKED, R.PERMISSION_PENDING), "permission control must block the tool", forbidden=(A.RETRY,), waiting=True)
        for kind in (RecoverySignalKind.PROMPT_TOO_LONG, RecoverySignalKind.COMPACT_NEEDED):
            add(kind, (A.COMPACT,), (R.CONTEXT_COMPACTED, R.CHECKPOINT_RESUMED, R.CONTINUATION_CHANGED), "context overflow compacts and resumes", sequence=(A.COMPACT, A.RESUME_CHECKPOINT))
        add(RecoverySignalKind.API_RETRY_EXHAUSTED, (A.DEGRADE_MODEL, A.SWITCH_PROVIDER, A.REPLAN), (R.MODEL_CHANGED, R.CONTINUATION_CHANGED), "retry exhaustion changes the provider/model path", forbidden=(A.RETRY,), layer=L.MODEL)
        add(RecoverySignalKind.RATE_LIMITED, (A.RETRY, A.SWITCH_PROVIDER, A.DEGRADE_MODEL), (R.CONTINUATION_CHANGED,), "rate limit uses bounded retry or route change")
        add(RecoverySignalKind.PROVIDER_UNAVAILABLE, (A.SWITCH_PROVIDER, A.DEGRADE_MODEL), (R.PROVIDER_CHANGED, R.CONTINUATION_CHANGED), "provider failure changes provider route", layer=L.PROVIDER)
        add(RecoverySignalKind.CREDENTIAL_EXHAUSTED, (A.SWITCH_PROVIDER, A.REPLAN), (R.PROVIDER_CHANGED, R.CONTINUATION_CHANGED), "credential exhaustion changes provider route", forbidden=(A.RETRY,), layer=L.PROVIDER)
        for kind in (RecoverySignalKind.WORKER_LOST, RecoverySignalKind.WORKER_HEARTBEAT_STALE, RecoverySignalKind.WORKER_LEASE_EXPIRED):
            add(kind, (A.REROUTE,), (R.WORKER_LEASE_CHANGED, R.CONTINUATION_CHANGED), "worker failure acquires a successor lease", layer=L.WORKER)
        add(RecoverySignalKind.BACKEND_UNAVAILABLE, (A.SWITCH_BACKEND,), (R.BACKEND_CHANGED, R.CONTINUATION_CHANGED), "backend failure acquires a successor backend lease", layer=L.BACKEND)
        add(RecoverySignalKind.SUBAGENT_FAILED, (A.REROUTE, A.REPLAN), (R.MEMORY_WRITTEN, R.CONTINUATION_CHANGED), "subagent failure changes route or graph and writes memory")
        add(RecoverySignalKind.SUBAGENT_CANCELLED, (A.REPLAN, A.ABORT), (R.CONTINUATION_CHANGED,), "subagent cancellation is an explicit graph/task transition")
        add(RecoverySignalKind.MCP_AUTH_REQUIRED, (A.AUTHENTICATE_MCP,), (R.MCP_CONTROL_CHANGED,), "MCP auth is a control flow", forbidden=(A.RETRY,), waiting=True)
        add(RecoverySignalKind.MCP_DISCONNECTED, (A.RETRY, A.REPLAN), (R.CONTINUATION_CHANGED,), "MCP disconnect reconnects within breaker budget")
        add(RecoverySignalKind.STREAM_STALL, (A.RETRY, A.DEGRADE_MODEL, A.SWITCH_PROVIDER), (R.CONTINUATION_CHANGED,), "pre-output stream stall uses bounded retry/fallback")
        add(RecoverySignalKind.STREAM_INTERRUPTED_AFTER_OUTPUT, (A.RESUME_CHECKPOINT, A.REPLAN), (R.CHECKPOINT_RESUMED, R.SIDE_EFFECT_NOT_REPLAYED, R.RESPONSE_NOT_REPLAYED), "partial stream resumes exactly", forbidden=(A.RETRY,))
        add(RecoverySignalKind.REQUIREMENT_CHANGED, (A.REPLAN,), (R.GRAPH_CHANGED, R.FAILURE_COUNTER_STABLE), "requirement change mutates graph without failure accounting", layer=L.GRAPH)
        add(RecoverySignalKind.WORKSPACE_CONFLICT, (A.REPLAN, A.REROUTE), (R.WORKSPACE_CONFLICT_PRESERVED, R.CONTINUATION_CHANGED), "workspace dirty/conflict evidence is preserved")
        add(RecoverySignalKind.CHECKPOINT_DIVERGED, (A.REPLAN, A.ABORT), (R.SIDE_EFFECT_NOT_REPLAYED,), "divergent checkpoint fails closed", forbidden=(A.RESUME_CHECKPOINT, A.RETRY))
        add(RecoverySignalKind.TOOL_ERROR, (A.RETRY, A.REPLAN), (R.CONTINUATION_CHANGED,), "tool error retries only when typed and fenced")
        add(RecoverySignalKind.TOOL_TIMEOUT, (A.RETRY, A.REROUTE), (R.CONTINUATION_CHANGED,), "tool timeout retries or reroutes within budget")
        add(RecoverySignalKind.UNKNOWN_FAILURE, (A.REPLAN, A.ABORT), (R.CONTINUATION_CHANGED,), "unknown failure replans or aborts", forbidden=(A.RETRY,))
        return values

    def _resolved_expectation(self, plan: RecoveryPlan) -> SemanticExpectation:
        """Resolve action-specific effects without taking policy ownership.

        A signal can legitimately produce different policy-owned actions.  The
        semantic layer validates the action that RecoveryDecisionRuntime chose;
        it never substitutes another action or invents a second route owner.
        """
        base = self.expectation(plan.signal.kind)
        action = plan.decision.selected.action
        layer = plan.decision.selected.route_layer
        route_requirements = {
            SemanticRequirement.GRAPH_CHANGED,
            SemanticRequirement.WORKER_LEASE_CHANGED,
            SemanticRequirement.BACKEND_CHANGED,
            SemanticRequirement.PROVIDER_CHANGED,
            SemanticRequirement.MODEL_CHANGED,
            SemanticRequirement.CREDENTIAL_CHANGED,
            SemanticRequirement.TRANSPORT_CHANGED,
            SemanticRequirement.WORKSPACE_CHANGED,
        }
        effects = [item for item in base.required_effects if item not in route_requirements]
        requirement_by_layer = {
            RouteLayer.GRAPH: SemanticRequirement.GRAPH_CHANGED,
            RouteLayer.WORKER: SemanticRequirement.WORKER_LEASE_CHANGED,
            RouteLayer.BACKEND: SemanticRequirement.BACKEND_CHANGED,
            RouteLayer.WORKSPACE: SemanticRequirement.WORKSPACE_CHANGED,
            RouteLayer.PROVIDER: SemanticRequirement.PROVIDER_CHANGED,
            RouteLayer.MODEL: SemanticRequirement.MODEL_CHANGED,
            RouteLayer.CREDENTIAL: SemanticRequirement.CREDENTIAL_CHANGED,
            RouteLayer.TRANSPORT: SemanticRequirement.TRANSPORT_CHANGED,
        }
        if action in {
            RecoveryAction.REPLAN,
            RecoveryAction.REROUTE,
            RecoveryAction.SWITCH_BACKEND,
            RecoveryAction.SWITCH_PROVIDER,
            RecoveryAction.DEGRADE_MODEL,
        } and layer is not None:
            effects.append(requirement_by_layer[layer])
        if plan.signal.kind is RecoverySignalKind.PERMISSION_DENIED:
            effects = [item for item in effects if item is not SemanticRequirement.PERMISSION_PENDING]
        if action is RecoveryAction.RETRY:
            effects = [item for item in effects if item not in route_requirements]
            layer = None
        return SemanticExpectation(
            signal_kind=base.signal_kind,
            allowed_primary_actions=base.allowed_primary_actions,
            forbidden_actions=base.forbidden_actions,
            required_sequence=base.required_sequence,
            required_effects=tuple(dict.fromkeys(effects)),
            expected_route_layer=layer if action in {
                RecoveryAction.REPLAN,
                RecoveryAction.REROUTE,
                RecoveryAction.SWITCH_BACKEND,
                RecoveryAction.SWITCH_PROVIDER,
                RecoveryAction.DEGRADE_MODEL,
            } else None,
            allow_waiting=base.allow_waiting,
            reason=base.reason,
        )


def semantic_runtime_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.recovery-semantic-runtime-surface/v1",
        "policy_owner": "RecoveryDecisionRuntime",
        "semantic_guard_selects_action": False,
        "preflight": "selected policy action and state prerequisites",
        "postflight": "owner receipt, continuation, route, checkpoint and memory effects",
        "required_main_paths": [
            "permission blocked",
            "compact restore continue",
            "API retry exhaustion route change",
            "worker/backend successor lease",
            "subagent memory and reroute/replan",
        ],
    }


__all__ = [
    "RecoverySemanticError",
    "RecoverySemanticRejected",
    "RecoverySemanticRuntime",
    "SemanticCheck",
    "SemanticExpectation",
    "SemanticOutcomeReport",
    "SemanticPreflight",
    "SemanticRequirement",
    "semantic_runtime_contract",
]
