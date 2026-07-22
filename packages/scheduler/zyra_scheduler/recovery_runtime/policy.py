from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from .contracts import (
    DecisionMode,
    RecoveryAction,
    RecoveryBudget,
    RecoveryCandidate,
    RecoveryContext,
    RecoveryDecision,
    RecoveryPlan,
    RecoveryPlanStatus,
    RecoverySignal,
    RecoverySignalKind,
    RouteLayer,
    RoutingMemoryRecord,
    stable_digest,
)
from .store import RecoveryPlanStore


class RecoveryPolicyError(RuntimeError):
    pass


class NoEligibleRecoveryAction(RecoveryPolicyError):
    def __init__(self, signal: RecoverySignal, candidates: Sequence[RecoveryCandidate]) -> None:
        self.signal = signal
        self.candidates = tuple(candidates)
        blockers = sorted({blocker for candidate in candidates for blocker in candidate.blockers})
        super().__init__(
            f"no eligible recovery action for {signal.kind.value}: "
            + ("; ".join(blockers) if blockers else "policy produced no eligible candidate")
        )


@dataclass(frozen=True, slots=True)
class RecoveryHistory:
    total_actions: int = 0
    action_counts: Mapping[RecoveryAction, int] = field(default_factory=dict)
    recent_failures: Mapping[RecoveryAction, int] = field(default_factory=dict)
    recent_successes: Mapping[RecoveryAction, int] = field(default_factory=dict)
    route_penalties: Mapping[str, float] = field(default_factory=dict)
    previous_actions: tuple[RecoveryAction, ...] = ()
    previous_plan_ids: tuple[str, ...] = ()

    def count(self, action: RecoveryAction) -> int:
        return int(self.action_counts.get(action, 0))

    def failures(self, action: RecoveryAction) -> int:
        return int(self.recent_failures.get(action, 0))

    def successes(self, action: RecoveryAction) -> int:
        return int(self.recent_successes.get(action, 0))


@dataclass(frozen=True, slots=True)
class CandidateTemplate:
    action: RecoveryAction
    base_score: float
    policy_rule: str
    reason: str
    route_layer: RouteLayer | None = None
    expected_effects: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    allow_after_partial_output: bool = False
    allow_after_side_effect: bool = False


class RecoveryDecisionRuntime:
    """Deterministic recovery policy and the only applied-action selector."""

    POLICY_REVISION = 1

    def __init__(
        self,
        store: RecoveryPlanStore,
        *,
        advisory_provider: Callable[[RecoverySignal, RecoveryContext, Sequence[RecoveryCandidate]], Sequence[str]] | None = None,
    ) -> None:
        self.store = store
        self.advisory_provider = advisory_provider
        self._templates = self._build_templates()

    def plan(
        self,
        signal: RecoverySignal,
        context: RecoveryContext,
        *,
        idempotency_key: str = "",
    ) -> tuple[RecoveryPlan, bool]:
        self._validate_scope(signal, context)
        persisted_signal, _ = self.store.put_signal(signal)
        history = self.history(signal.refs.task_id)
        candidates = self.candidates(persisted_signal, context, history=history)
        eligible = [candidate for candidate in candidates if candidate.eligible]
        if not eligible:
            raise NoEligibleRecoveryAction(persisted_signal, candidates)
        selected = min(
            eligible,
            key=lambda candidate: (
                -candidate.score,
                self._action_precedence(candidate.action),
                candidate.delay_ms,
                candidate.policy_rule,
            ),
        )
        deterministic_key = stable_digest({
            "policy_revision": self.POLICY_REVISION,
            "signal": persisted_signal.to_dict(),
            "context": context.to_dict(),
            "history": self._history_dict(history),
            "candidates": [item.to_dict() for item in candidates],
        })
        advisory_notes: tuple[str, ...] = ()
        if self.advisory_provider is not None:
            notes = self.advisory_provider(persisted_signal, context, candidates)
            advisory_notes = tuple(str(item)[:1000] for item in notes if str(item).strip())[:16]
        decision = RecoveryDecision(
            selected=selected,
            candidates=tuple(candidates),
            policy_revision=self.POLICY_REVISION,
            deterministic_key=deterministic_key,
            rationale=self._rationale(persisted_signal, selected, history, context),
            advisory_notes=advisory_notes,
        )
        key = idempotency_key or f"recovery:{persisted_signal.fingerprint}:{deterministic_key[:24]}"
        sequence = self.action_sequence(persisted_signal, selected, context)
        plan = RecoveryPlan(
            signal=persisted_signal,
            decision=decision,
            status=RecoveryPlanStatus.PLANNED,
            idempotency_key=key,
            provenance={
                "policy_owner": "python.RecoveryDecisionRuntime",
                "policy_revision": self.POLICY_REVISION,
                "decision_mode": context.mode.value,
                "action_sequence": [action.value for action in sequence],
                "memory_evidence_count": len(context.memory_evidence),
                "llm_advisory_only": bool(advisory_notes),
                "llm_selected_action": False,
                "requirement_change_is_fault": False,
                "explicit_escalation": bool(context.metadata.get("explicit_escalation", False)),
                "escalation_ids": list(context.metadata.get("escalation_ids") or ()),
                "state_fusion_digest": str(context.metadata.get("state_fusion_digest") or ""),
                "observation_digest": str(context.metadata.get("observation_digest") or ""),
            },
        )
        return self.store.put_plan(plan)

    def candidates(
        self,
        signal: RecoverySignal,
        context: RecoveryContext,
        *,
        history: RecoveryHistory | None = None,
    ) -> tuple[RecoveryCandidate, ...]:
        self._validate_scope(signal, context)
        state = history or self.history(signal.refs.task_id)
        templates = list(self._templates.get(signal.kind, ()))
        templates.extend(self._contextual_templates(signal, context, templates))
        candidates = [self._evaluate(signal, context, state, template) for template in templates]
        candidates = self._deduplicate(candidates)
        if not candidates:
            candidates = [self._abort_candidate(signal, context, state, "policy.no_template")]
        if not any(candidate.eligible for candidate in candidates):
            abort = self._abort_candidate(signal, context, state, "policy.fail_closed")
            candidates = self._deduplicate([*candidates, abort])
        return tuple(sorted(
            candidates,
            key=lambda candidate: (
                not candidate.eligible,
                -candidate.score,
                self._action_precedence(candidate.action),
                candidate.policy_rule,
            ),
        ))

    def history(self, task_id: str) -> RecoveryHistory:
        plans = self.store.plans(task_id=task_id)
        action_counts: Counter[RecoveryAction] = Counter()
        failures: Counter[RecoveryAction] = Counter()
        successes: Counter[RecoveryAction] = Counter()
        previous_actions: list[RecoveryAction] = []
        previous_plan_ids: list[str] = []
        for plan in plans:
            action = plan.decision.selected.action
            action_counts[action] += len(self.store.action_receipts(plan_id=plan.plan_id))
            previous_actions.append(action)
            previous_plan_ids.append(plan.plan_id)
        for outcome in self.store.outcomes(task_id=task_id):
            if outcome.success:
                successes[outcome.action] += 1
            else:
                failures[outcome.action] += 1
        penalties: dict[str, float] = defaultdict(float)
        for record in self.store.feedback(task_id=task_id):
            for route_id in (record.worker_id, record.backend_id, record.provider_id, record.model_id):
                if route_id:
                    penalties[route_id] += record.score_delta
        return RecoveryHistory(
            total_actions=sum(action_counts.values()),
            action_counts=dict(action_counts),
            recent_failures=dict(failures),
            recent_successes=dict(successes),
            route_penalties=dict(penalties),
            previous_actions=tuple(previous_actions[-32:]),
            previous_plan_ids=tuple(previous_plan_ids[-32:]),
        )

    def action_sequence(
        self,
        signal: RecoverySignal,
        selected: RecoveryCandidate,
        context: RecoveryContext,
    ) -> tuple[RecoveryAction, ...]:
        if selected.action is RecoveryAction.COMPACT:
            return (RecoveryAction.COMPACT, RecoveryAction.RESUME_CHECKPOINT)
        if selected.action in {
            RecoveryAction.REROUTE,
            RecoveryAction.SWITCH_BACKEND,
            RecoveryAction.SWITCH_PROVIDER,
            RecoveryAction.DEGRADE_MODEL,
            RecoveryAction.REPLAN,
        } and self._checkpoint_available(context):
            return (selected.action, RecoveryAction.RESUME_CHECKPOINT)
        if selected.action is RecoveryAction.RETRY and (
            signal.partial_output or signal.observable_side_effect
        ):
            return (RecoveryAction.RESUME_CHECKPOINT,)
        return (selected.action,)

    def policy_contract(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-policy-contract/v1",
            "policy_revision": self.POLICY_REVISION,
            "owner": "python.RecoveryDecisionRuntime",
            "signal_actions": {
                kind.value: [template.action.value for template in templates]
                for kind, templates in sorted(self._templates.items(), key=lambda item: item[0].value)
            },
            "sealed_autonomous": {
                "ask_waits_for_human": False,
                "unknown_or_dangerous": "deny_and_replan",
                "llm_can_apply": False,
            },
            "route_owners": {
                RouteLayer.GRAPH.value: "07C graph route through GraphStateCustody",
                RouteLayer.WORKER.value: "07A WorkerPool",
                RouteLayer.BACKEND.value: "05D BackendRegistry",
                RouteLayer.WORKSPACE.value: "05A/05D workspace owner",
                RouteLayer.PROVIDER.value: "05D ProviderControlPlane",
                RouteLayer.MODEL.value: "05D ProviderControlPlane",
                RouteLayer.CREDENTIAL.value: "05D ProviderControlPlane",
                RouteLayer.TRANSPORT.value: "05D ProviderControlPlane",
            },
        }

    def _evaluate(
        self,
        signal: RecoverySignal,
        context: RecoveryContext,
        history: RecoveryHistory,
        template: CandidateTemplate,
    ) -> RecoveryCandidate:
        blockers: list[str] = []
        requirements = list(template.requirements)
        score = float(template.base_score)
        delay_ms = 0

        if template.action in context.forbidden_actions:
            blockers.append("action_forbidden_by_context")
        if history.total_actions >= context.budget.maximum_total_actions and template.action is not RecoveryAction.ABORT:
            blockers.append("total_recovery_action_budget_exhausted")
        count_limit = self._action_limit(template.action, context.budget)
        if count_limit is not None and history.count(template.action) >= count_limit:
            blockers.append(f"{template.action.value}_budget_exhausted")
        if not signal.recoverable and template.action not in {RecoveryAction.ABORT, RecoveryAction.REPLAN}:
            blockers.append("signal_marked_unrecoverable")
        if signal.terminal and template.action is RecoveryAction.RETRY:
            blockers.append("terminal_signal_cannot_retry")
        if signal.partial_output and not template.allow_after_partial_output:
            blockers.append("partial_output_requires_checkpoint_fence")
        if signal.observable_side_effect and not template.allow_after_side_effect:
            blockers.append("committed_side_effect_cannot_replay")
        if signal.kind is RecoverySignalKind.REQUIREMENT_CHANGED and template.action is not RecoveryAction.REPLAN:
            blockers.append("requirement_change_only_allows_graph_replan")
        if template.action is RecoveryAction.ASK_PERMISSION:
            if context.mode is DecisionMode.SEALED_AUTONOMOUS:
                blockers.append("sealed_autonomous_cannot_wait_for_permission")
            if not signal.refs.tool_call_id and not signal.refs.request_id:
                blockers.append("permission_request_identity_missing")
        if template.action is RecoveryAction.AUTHENTICATE_MCP and not signal.refs.mcp_server_id:
            blockers.append("mcp_server_identity_missing")
        if template.action is RecoveryAction.RESUME_CHECKPOINT and not self._checkpoint_available(context):
            blockers.append("committed_checkpoint_unavailable")
        if template.action is RecoveryAction.COMPACT and not signal.refs.session_id:
            blockers.append("session_identity_missing_for_compact")
        if template.action is RecoveryAction.REROUTE:
            if not (signal.refs.worker_id or signal.refs.worker_lease_id or signal.refs.subagent_id):
                blockers.append("worker_route_identity_missing")
            if not self._worker_route_available(context):
                blockers.append("worker_route_owner_unavailable")
        if template.action is RecoveryAction.SWITCH_BACKEND:
            if not (signal.refs.backend_id or signal.refs.backend_lease_id or signal.refs.worker_id):
                blockers.append("backend_route_identity_missing")
            if not self._backend_route_available(context):
                blockers.append("backend_route_owner_unavailable")
        if template.action in {RecoveryAction.SWITCH_PROVIDER, RecoveryAction.DEGRADE_MODEL}:
            if not (signal.refs.provider_id or signal.refs.provider_route_id or signal.refs.request_id):
                blockers.append("provider_route_identity_missing")
            if not self._provider_route_available(context):
                blockers.append("provider_route_owner_unavailable")
        if template.action is RecoveryAction.REPLAN and not (
            signal.refs.graph_id or signal.refs.node_id or context.refs.graph_id or context.refs.node_id
        ):
            blockers.append("graph_route_identity_missing")

        if template.action is RecoveryAction.RETRY:
            delay_ms = self._retry_delay(signal, context.budget, history.count(RecoveryAction.RETRY))
            if delay_ms > context.budget.maximum_delay_ms:
                blockers.append("retry_delay_exceeds_budget")
            score -= min(25.0, delay_ms / 10_000.0)
        score += self._history_adjustment(template.action, history)
        score += self._memory_adjustment(template.action, signal, context, history)
        score += self._state_adjustment(template.action, signal, context)
        if signal.suggested_actions and template.action in signal.suggested_actions:
            score += 4.0
        if template.action is RecoveryAction.ABORT and signal.recoverable:
            score -= 40.0
        if blockers:
            score -= 10_000.0 + 50.0 * len(blockers)

        metadata = {
            "history_count": history.count(template.action),
            "history_failures": history.failures(template.action),
            "history_successes": history.successes(template.action),
            "signal_retryable": signal.retryable,
            "signal_terminal": signal.terminal,
            "partial_output": signal.partial_output,
            "observable_side_effect": signal.observable_side_effect,
            "decision_mode": context.mode.value,
            "policy_owner": "python.RecoveryDecisionRuntime",
        }
        return RecoveryCandidate(
            action=template.action,
            score=round(score, 6),
            eligible=not blockers,
            policy_rule=template.policy_rule,
            reason=template.reason,
            route_layer=template.route_layer,
            delay_ms=delay_ms,
            requirements=tuple(requirements),
            blockers=tuple(blockers),
            expected_effects=template.expected_effects,
            metadata=metadata,
        )

    def _contextual_templates(
        self,
        signal: RecoverySignal,
        context: RecoveryContext,
        templates: Sequence[CandidateTemplate],
    ) -> tuple[CandidateTemplate, ...]:
        existing = {template.action for template in templates}
        additions: list[CandidateTemplate] = []
        if signal.retryable and RecoveryAction.RETRY not in existing:
            additions.append(self._template(
                RecoveryAction.RETRY, 48.0, "policy.retry.typed_hint",
                "typed source marked the failure retryable", requirements=("retry_budget",),
            ))
        if signal.partial_output or signal.observable_side_effect:
            if RecoveryAction.RESUME_CHECKPOINT not in existing:
                additions.append(self._template(
                    RecoveryAction.RESUME_CHECKPOINT, 94.0, "policy.resume.replay_fence",
                    "partial output or committed side effect requires exact resume",
                    expected=("bypass_completed_steps", "skip_processed_responses", "preserve_side_effect_fences"),
                    partial=True, side_effect=True,
                ))
        if context.mode is DecisionMode.SEALED_AUTONOMOUS and signal.kind in {
            RecoverySignalKind.PERMISSION_ASK,
            RecoverySignalKind.PERMISSION_PENDING,
            RecoverySignalKind.PERMISSION_DENIED,
        }:
            additions.append(self._template(
                RecoveryAction.REPLAN, 120.0, "policy.permission.sealed_replan",
                "sealed autonomous policy denies unknown/high-risk action and replans",
                layer=RouteLayer.GRAPH,
                expected=("no_human_wait", "graph_route_changed"),
            ))
        if signal.kind is RecoverySignalKind.UNKNOWN_FAILURE:
            additions.append(self._template(
                RecoveryAction.ABORT, 100.0, "policy.unknown.fail_closed",
                "unknown failures fail closed", expected=("unsafe_replay_prevented",),
                partial=True, side_effect=True,
            ))
        return tuple(additions)

    @classmethod
    def _build_templates(cls) -> dict[RecoverySignalKind, tuple[CandidateTemplate, ...]]:
        t = cls._template
        result: dict[RecoverySignalKind, tuple[CandidateTemplate, ...]] = {
            RecoverySignalKind.PERMISSION_ASK: (
                t(RecoveryAction.ASK_PERMISSION, 110, "policy.permission.ask", "interactive policy requires a real permission request", expected=("permission_request_created",)),
                t(RecoveryAction.REPLAN, 65, "policy.permission.replan", "replan around unavailable permission", layer=RouteLayer.GRAPH, expected=("graph_route_changed",)),
            ),
            RecoverySignalKind.PERMISSION_PENDING: (
                t(RecoveryAction.ASK_PERMISSION, 105, "policy.permission.pending", "resume the existing permission request", expected=("permission_request_linked",)),
                t(RecoveryAction.REPLAN, 60, "policy.permission.pending_replan", "avoid a blocked permission path", layer=RouteLayer.GRAPH, expected=("graph_route_changed",)),
            ),
            RecoverySignalKind.PERMISSION_DENIED: (
                t(RecoveryAction.REPLAN, 115, "policy.permission.denied", "permission denial is a graph constraint, not a retry", layer=RouteLayer.GRAPH, expected=("denied_tool_not_replayed", "graph_route_changed"), partial=True, side_effect=True),
                t(RecoveryAction.ABORT, 50, "policy.permission.abort", "abort when no safe graph alternative exists", expected=("denied_tool_not_replayed",), partial=True, side_effect=True),
            ),
            RecoverySignalKind.PROMPT_TOO_LONG: (
                t(RecoveryAction.COMPACT, 130, "policy.context.compact", "context overflow must compact before retry", expected=("context_compacted", "checkpoint_resumed"), partial=True, side_effect=True),
                t(RecoveryAction.RESUME_CHECKPOINT, 70, "policy.context.resume_existing", "resume an already compacted checkpoint", expected=("checkpoint_resumed",), partial=True, side_effect=True),
            ),
            RecoverySignalKind.COMPACT_NEEDED: (
                t(RecoveryAction.COMPACT, 125, "policy.context.compact_needed", "context pressure enters compact/restore", expected=("context_compacted", "checkpoint_resumed"), partial=True, side_effect=True),
                t(RecoveryAction.REPLAN, 40, "policy.context.replan", "reduce graph scope if compact is unavailable", layer=RouteLayer.GRAPH, expected=("graph_route_changed",), partial=True, side_effect=True),
            ),
            RecoverySignalKind.STREAM_STALL: (
                t(RecoveryAction.RETRY, 100, "policy.stream.retry", "retry a stalled stream before observable output", requirements=("retry_budget",)),
                t(RecoveryAction.DEGRADE_MODEL, 78, "policy.stream.degrade", "use a more available model after stream retry pressure", layer=RouteLayer.MODEL, expected=("provider_route_changed",)),
                t(RecoveryAction.SWITCH_PROVIDER, 72, "policy.stream.provider", "switch provider transport after repeated stalls", layer=RouteLayer.PROVIDER, expected=("provider_route_changed",)),
            ),
            RecoverySignalKind.STREAM_INTERRUPTED_AFTER_OUTPUT: (
                t(RecoveryAction.RESUME_CHECKPOINT, 135, "policy.stream.partial_resume", "partial output cannot be blindly retried", expected=("partial_output_tombstoned", "checkpoint_resumed", "side_effect_not_replayed"), partial=True, side_effect=True),
                t(RecoveryAction.REPLAN, 55, "policy.stream.partial_replan", "replan if exact resume is unavailable", layer=RouteLayer.GRAPH, expected=("side_effect_not_replayed",), partial=True, side_effect=True),
            ),
            RecoverySignalKind.API_RETRY_EXHAUSTED: (
                t(RecoveryAction.DEGRADE_MODEL, 110, "policy.api.degrade", "retry budget exhausted; request a model fallback", layer=RouteLayer.MODEL, expected=("provider_route_changed",)),
                t(RecoveryAction.SWITCH_PROVIDER, 102, "policy.api.switch_provider", "retry budget exhausted; request another provider", layer=RouteLayer.PROVIDER, expected=("provider_route_changed",)),
                t(RecoveryAction.REPLAN, 50, "policy.api.replan", "replan if provider alternatives are unavailable", layer=RouteLayer.GRAPH, expected=("graph_route_changed",)),
            ),
            RecoverySignalKind.RATE_LIMITED: (
                t(RecoveryAction.RETRY, 92, "policy.rate_limit.backoff", "honor retry-after within bounded retry policy", requirements=("retry_budget",)),
                t(RecoveryAction.SWITCH_PROVIDER, 88, "policy.rate_limit.provider", "request another provider or credential route", layer=RouteLayer.PROVIDER, expected=("provider_route_changed",)),
                t(RecoveryAction.DEGRADE_MODEL, 74, "policy.rate_limit.model", "select a lower-cost or less constrained model", layer=RouteLayer.MODEL, expected=("provider_route_changed",)),
            ),
            RecoverySignalKind.MCP_AUTH_REQUIRED: (
                t(RecoveryAction.AUTHENTICATE_MCP, 140, "policy.mcp.auth", "MCP auth is a control flow, not generic retry", expected=("mcp_auth_request_created",), partial=True, side_effect=True),
                t(RecoveryAction.REPLAN, 45, "policy.mcp.auth_replan", "replan without the MCP capability when auth cannot proceed", layer=RouteLayer.GRAPH, expected=("graph_route_changed",), partial=True, side_effect=True),
            ),
            RecoverySignalKind.MCP_DISCONNECTED: (
                t(RecoveryAction.RETRY, 88, "policy.mcp.reconnect", "retry a reconnectable MCP transport", requirements=("retry_budget",)),
                t(RecoveryAction.REPLAN, 62, "policy.mcp.replan", "replan without an unavailable MCP server", layer=RouteLayer.GRAPH, expected=("graph_route_changed",)),
            ),
            RecoverySignalKind.SUBAGENT_FAILED: (
                t(RecoveryAction.REROUTE, 125, "policy.subagent.reroute", "acquire a successor worker attempt", layer=RouteLayer.WORKER, expected=("worker_lease_changed", "checkpoint_resumed"), partial=True, side_effect=True),
                t(RecoveryAction.REPLAN, 92, "policy.subagent.replan", "change the graph when the role cannot be reassigned", layer=RouteLayer.GRAPH, expected=("graph_route_changed",), partial=True, side_effect=True),
            ),
            RecoverySignalKind.SUBAGENT_CANCELLED: (
                t(RecoveryAction.REPLAN, 100, "policy.subagent.cancelled", "cancelled work requires an explicit graph decision", layer=RouteLayer.GRAPH, expected=("graph_route_changed",), partial=True, side_effect=True),
                t(RecoveryAction.ABORT, 65, "policy.subagent.cancel_abort", "honor terminal cancellation", expected=("task_stopped",), partial=True, side_effect=True),
            ),
            RecoverySignalKind.WORKER_HEARTBEAT_STALE: (
                t(RecoveryAction.REROUTE, 118, "policy.worker.stale", "lease a healthy worker after heartbeat staleness", layer=RouteLayer.WORKER, expected=("worker_lease_changed", "checkpoint_resumed"), partial=True, side_effect=True),
                t(RecoveryAction.RETRY, 55, "policy.worker.stale_probe", "allow one bounded probe before reroute", requirements=("retry_budget",)),
            ),
            RecoverySignalKind.WORKER_LOST: (
                t(RecoveryAction.REROUTE, 132, "policy.worker.lost", "lost worker requires a new physical lease", layer=RouteLayer.WORKER, expected=("worker_lease_changed", "checkpoint_resumed"), partial=True, side_effect=True),
                t(RecoveryAction.REPLAN, 80, "policy.worker.lost_replan", "replan when no worker satisfies the role", layer=RouteLayer.GRAPH, expected=("graph_route_changed",), partial=True, side_effect=True),
            ),
            RecoverySignalKind.WORKER_LEASE_EXPIRED: (
                t(RecoveryAction.REROUTE, 135, "policy.worker.lease_expired", "expired lease must be fenced before successor allocation", layer=RouteLayer.WORKER, expected=("old_lease_fenced", "worker_lease_changed", "checkpoint_resumed"), partial=True, side_effect=True),
            ),
            RecoverySignalKind.BACKEND_UNAVAILABLE: (
                t(RecoveryAction.SWITCH_BACKEND, 138, "policy.backend.switch", "execution backend owner must issue a new lease", layer=RouteLayer.BACKEND, expected=("backend_lease_changed", "checkpoint_resumed"), partial=True, side_effect=True),
                t(RecoveryAction.REROUTE, 80, "policy.backend.worker_reroute", "worker reroute may select another backend-capable worker", layer=RouteLayer.WORKER, expected=("worker_lease_changed",), partial=True, side_effect=True),
            ),
            RecoverySignalKind.PROVIDER_UNAVAILABLE: (
                t(RecoveryAction.SWITCH_PROVIDER, 132, "policy.provider.switch", "provider owner must issue a different route", layer=RouteLayer.PROVIDER, expected=("provider_route_changed",), partial=True, side_effect=True),
                t(RecoveryAction.DEGRADE_MODEL, 98, "policy.provider.degrade", "degrade model on the current compatible provider", layer=RouteLayer.MODEL, expected=("provider_route_changed",), partial=True, side_effect=True),
            ),
            RecoverySignalKind.CREDENTIAL_EXHAUSTED: (
                t(RecoveryAction.SWITCH_PROVIDER, 130, "policy.credential.switch", "credential exhaustion requires a new provider route", layer=RouteLayer.CREDENTIAL, expected=("credential_route_changed",), partial=True, side_effect=True),
                t(RecoveryAction.REPLAN, 65, "policy.credential.replan", "replan if no credential route is allowed", layer=RouteLayer.GRAPH, expected=("graph_route_changed",), partial=True, side_effect=True),
            ),
            RecoverySignalKind.TOOL_ERROR: (
                t(RecoveryAction.RETRY, 84, "policy.tool.retry", "retry a typed retryable tool error", requirements=("retry_budget",)),
                t(RecoveryAction.REPLAN, 72, "policy.tool.replan", "select another tool or graph path", layer=RouteLayer.GRAPH, expected=("graph_route_changed",), partial=True, side_effect=True),
            ),
            RecoverySignalKind.TOOL_TIMEOUT: (
                t(RecoveryAction.RETRY, 90, "policy.tool.timeout_retry", "retry a fenced timed-out tool call", requirements=("retry_budget",)),
                t(RecoveryAction.REROUTE, 86, "policy.tool.timeout_reroute", "move the tool call to another worker", layer=RouteLayer.WORKER, expected=("worker_lease_changed",), partial=True, side_effect=True),
            ),
            RecoverySignalKind.WORKSPACE_CONFLICT: (
                t(RecoveryAction.REPLAN, 120, "policy.workspace.conflict", "workspace conflict requires merge-aware replan", layer=RouteLayer.WORKSPACE, expected=("workspace_conflict_preserved", "graph_route_changed"), partial=True, side_effect=True),
                t(RecoveryAction.REROUTE, 55, "policy.workspace.reroute", "use an isolated workspace if policy allows", layer=RouteLayer.WORKER, expected=("worker_lease_changed", "workspace_changed"), partial=True, side_effect=True),
            ),
            RecoverySignalKind.CHECKPOINT_DIVERGED: (
                t(RecoveryAction.REPLAN, 125, "policy.checkpoint.diverged", "checkpoint signature divergence forbids exact replay", layer=RouteLayer.GRAPH, expected=("divergent_checkpoint_not_loaded", "graph_route_changed"), partial=True, side_effect=True),
                t(RecoveryAction.ABORT, 95, "policy.checkpoint.abort", "fail closed if graph lineage cannot be reconciled", expected=("unsafe_resume_prevented",), partial=True, side_effect=True),
            ),
            RecoverySignalKind.REQUIREMENT_CHANGED: (
                t(RecoveryAction.REPLAN, 160, "policy.control.requirement_change", "requirement change is a graph control reason", layer=RouteLayer.GRAPH, expected=("graph_route_changed", "fault_counters_unchanged"), partial=True, side_effect=True),
            ),
            RecoverySignalKind.UNKNOWN_FAILURE: (
                t(RecoveryAction.REPLAN, 80, "policy.unknown.replan", "unknown failures require a conservative graph replan", layer=RouteLayer.GRAPH, expected=("unsafe_retry_prevented",), partial=True, side_effect=True),
                t(RecoveryAction.ABORT, 75, "policy.unknown.abort", "abort when no deterministic recovery is available", expected=("unsafe_retry_prevented",), partial=True, side_effect=True),
            ),
        }
        return result

    @staticmethod
    def _template(
        action: RecoveryAction,
        score: float,
        rule: str,
        reason: str,
        *,
        layer: RouteLayer | None = None,
        expected: Sequence[str] = (),
        requirements: Sequence[str] = (),
        partial: bool = False,
        side_effect: bool = False,
    ) -> CandidateTemplate:
        return CandidateTemplate(
            action=action,
            base_score=score,
            policy_rule=rule,
            reason=reason,
            route_layer=layer,
            expected_effects=tuple(expected),
            requirements=tuple(requirements),
            allow_after_partial_output=partial,
            allow_after_side_effect=side_effect,
        )

    @staticmethod
    def _deduplicate(candidates: Iterable[RecoveryCandidate]) -> list[RecoveryCandidate]:
        chosen: dict[RecoveryAction, RecoveryCandidate] = {}
        for candidate in candidates:
            current = chosen.get(candidate.action)
            if current is None or (
                candidate.eligible,
                candidate.score,
                -len(candidate.blockers),
                candidate.policy_rule,
            ) > (
                current.eligible,
                current.score,
                -len(current.blockers),
                current.policy_rule,
            ):
                chosen[candidate.action] = candidate
        return list(chosen.values())

    def _abort_candidate(
        self,
        signal: RecoverySignal,
        context: RecoveryContext,
        history: RecoveryHistory,
        rule: str,
    ) -> RecoveryCandidate:
        blocker = "action_forbidden_by_context" if RecoveryAction.ABORT in context.forbidden_actions else ""
        return RecoveryCandidate(
            action=RecoveryAction.ABORT,
            score=-10_000.0 if blocker else 1.0 + (20.0 if signal.terminal else 0.0),
            eligible=not blocker,
            policy_rule=rule,
            reason="fail closed after deterministic recovery candidates were exhausted",
            blockers=(blocker,) if blocker else (),
            expected_effects=("unsafe_replay_prevented", "terminal_failure_recorded"),
            metadata={"history_total_actions": history.total_actions},
        )

    @staticmethod
    def _retry_delay(signal: RecoverySignal, budget: RecoveryBudget, prior_retries: int) -> int:
        if signal.retry_after_ms:
            return signal.retry_after_ms
        exponent = min(20, max(0, prior_retries + signal.attempt_count))
        nominal = min(budget.maximum_delay_ms, budget.base_delay_ms * (2**exponent))
        deterministic_jitter = 0.75 + int(signal.fingerprint[:4], 16) / 0xFFFF * 0.25
        return int(nominal * deterministic_jitter)

    @staticmethod
    def _action_limit(action: RecoveryAction, budget: RecoveryBudget) -> int | None:
        return {
            RecoveryAction.RETRY: budget.maximum_retries,
            RecoveryAction.REROUTE: budget.maximum_reroutes,
            RecoveryAction.SWITCH_BACKEND: budget.maximum_backend_switches,
            RecoveryAction.SWITCH_PROVIDER: budget.maximum_provider_switches,
            RecoveryAction.DEGRADE_MODEL: budget.maximum_provider_switches,
            RecoveryAction.COMPACT: budget.maximum_compactions,
            RecoveryAction.RESUME_CHECKPOINT: budget.maximum_resumes,
        }.get(action)

    @staticmethod
    def _history_adjustment(action: RecoveryAction, history: RecoveryHistory) -> float:
        successes = history.successes(action)
        failures = history.failures(action)
        attempts = successes + failures
        if attempts == 0:
            return 0.0
        success_rate = successes / attempts
        confidence = 1.0 - math.exp(-attempts / 4.0)
        return round((success_rate - 0.5) * 40.0 * confidence - failures * 2.0, 6)

    @staticmethod
    def _memory_adjustment(
        action: RecoveryAction,
        signal: RecoverySignal,
        context: RecoveryContext,
        history: RecoveryHistory,
    ) -> float:
        adjustment = 0.0
        for evidence in context.memory_evidence:
            evidence_action = str(evidence.get("action") or "")
            evidence_kind = str(evidence.get("signal_kind") or "")
            if evidence_action and evidence_action != action.value:
                continue
            weight = float(evidence.get("weight") or 1.0)
            if evidence_kind and evidence_kind != signal.kind.value:
                weight *= 0.4
            success = bool(evidence.get("success", False))
            adjustment += (8.0 if success else -12.0) * max(0.0, min(4.0, weight))
        for route_id in (
            signal.refs.worker_id, signal.refs.backend_id, signal.refs.provider_id,
        ):
            if route_id:
                adjustment += history.route_penalties.get(route_id, 0.0)
        return max(-80.0, min(80.0, adjustment))

    @staticmethod
    def _state_adjustment(action: RecoveryAction, signal: RecoverySignal, context: RecoveryContext) -> float:
        score = 0.0
        if action is RecoveryAction.RETRY and signal.retryable:
            score += 12.0
        if action is RecoveryAction.RESUME_CHECKPOINT and context.checkpoint_state.get("exact_resume_ready"):
            score += 18.0
        if action is RecoveryAction.REROUTE and context.worker_state.get("healthy_alternative_count", 0):
            score += min(15.0, float(context.worker_state["healthy_alternative_count"]) * 4.0)
        if action is RecoveryAction.SWITCH_BACKEND and context.backend_state.get("eligible_alternative_count", 0):
            score += min(15.0, float(context.backend_state["eligible_alternative_count"]) * 4.0)
        if action in {RecoveryAction.SWITCH_PROVIDER, RecoveryAction.DEGRADE_MODEL} and context.provider_state.get("eligible_alternative_count", 0):
            score += min(15.0, float(context.provider_state["eligible_alternative_count"]) * 4.0)
        if action is RecoveryAction.ASK_PERMISSION and context.permission_state.get("pending_request_id"):
            score += 10.0
        if action is RecoveryAction.COMPACT and context.session_state.get("compact_available", True):
            score += 12.0
        return score

    @staticmethod
    def _checkpoint_available(context: RecoveryContext) -> bool:
        return bool(
            context.refs.checkpoint_id
            or context.checkpoint_state.get("checkpoint_id")
            or context.checkpoint_state.get("exact_resume_ready")
        )

    @staticmethod
    def _worker_route_available(context: RecoveryContext) -> bool:
        return bool(context.worker_state.get("owner_available", True))

    @staticmethod
    def _backend_route_available(context: RecoveryContext) -> bool:
        return bool(context.backend_state.get("owner_available", True))

    @staticmethod
    def _provider_route_available(context: RecoveryContext) -> bool:
        return bool(context.provider_state.get("owner_available", True))

    @staticmethod
    def _action_precedence(action: RecoveryAction) -> int:
        order = (
            RecoveryAction.ASK_PERMISSION,
            RecoveryAction.AUTHENTICATE_MCP,
            RecoveryAction.COMPACT,
            RecoveryAction.RESUME_CHECKPOINT,
            RecoveryAction.RETRY,
            RecoveryAction.REROUTE,
            RecoveryAction.SWITCH_BACKEND,
            RecoveryAction.SWITCH_PROVIDER,
            RecoveryAction.DEGRADE_MODEL,
            RecoveryAction.REPLAN,
            RecoveryAction.ABORT,
            RecoveryAction.NOOP,
        )
        return order.index(action)

    @staticmethod
    def _validate_scope(signal: RecoverySignal, context: RecoveryContext) -> None:
        if signal.refs.run_id != context.refs.run_id or signal.refs.task_id != context.refs.task_id:
            raise RecoveryPolicyError("recovery signal and context scope mismatch")
        for name in ("session_id", "node_id", "worker_id", "backend_id", "provider_id", "graph_id"):
            left = getattr(signal.refs, name)
            right = getattr(context.refs, name)
            if left and right and left != right:
                raise RecoveryPolicyError(f"recovery signal/context {name} mismatch")

    @staticmethod
    def _rationale(
        signal: RecoverySignal,
        selected: RecoveryCandidate,
        history: RecoveryHistory,
        context: RecoveryContext,
    ) -> str:
        return (
            f"Rule {selected.policy_rule} selected {selected.action.value} for "
            f"{signal.kind.value}; score={selected.score:.3f}, prior_action_count="
            f"{history.count(selected.action)}, mode={context.mode.value}."
        )

    @staticmethod
    def _history_dict(history: RecoveryHistory) -> dict[str, Any]:
        return {
            "total_actions": history.total_actions,
            "action_counts": {key.value: value for key, value in history.action_counts.items()},
            "recent_failures": {key.value: value for key, value in history.recent_failures.items()},
            "recent_successes": {key.value: value for key, value in history.recent_successes.items()},
            "route_penalties": dict(history.route_penalties),
            "previous_actions": [item.value for item in history.previous_actions],
            "previous_plan_ids": list(history.previous_plan_ids),
        }


__all__ = [
    "CandidateTemplate",
    "NoEligibleRecoveryAction",
    "RecoveryDecisionRuntime",
    "RecoveryHistory",
    "RecoveryPolicyError",
]
