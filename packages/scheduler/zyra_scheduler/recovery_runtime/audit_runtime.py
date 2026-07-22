from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .contracts import (
    CheckpointPhase,
    PendingWriteState,
    RecoveryAction,
    RecoveryAttemptStatus,
    RecoveryOutcomeKind,
    RecoveryPlanStatus,
    RouteLayer,
    SideEffectState,
    stable_digest,
    utc_now,
)
from .store import RecoveryPlanStore


class AuditSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class RecoveryAuditFinding:
    code: str
    severity: AuditSeverity
    message: str
    entity_kind: str
    entity_id: str
    evidence_refs: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def blocks_execution(self) -> bool:
        return self.severity in {AuditSeverity.ERROR, AuditSeverity.BLOCKER}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "entity_kind": self.entity_kind,
            "entity_id": self.entity_id,
            "evidence_refs": list(self.evidence_refs),
            "blocks_execution": self.blocks_execution,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class RecoveryAuditReport:
    task_id: str
    run_ids: tuple[str, ...]
    findings: tuple[RecoveryAuditFinding, ...]
    counts: Mapping[str, int]
    custody: Mapping[str, Any]
    report_id: str
    created_at: str = field(default_factory=utc_now)

    @property
    def ok(self) -> bool:
        return not any(item.blocks_execution for item in self.findings)

    @property
    def blocker_count(self) -> int:
        return sum(item.severity is AuditSeverity.BLOCKER for item in self.findings)

    @property
    def error_count(self) -> int:
        return sum(item.severity is AuditSeverity.ERROR for item in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-runtime-audit/v1",
            "report_id": self.report_id,
            "task_id": self.task_id,
            "run_ids": list(self.run_ids),
            "ok": self.ok,
            "blocker_count": self.blocker_count,
            "error_count": self.error_count,
            "findings": [item.to_dict() for item in self.findings],
            "counts": dict(self.counts),
            "custody": copy.deepcopy(dict(self.custody)),
            "created_at": self.created_at,
        }


class RecoveryInvariantAuditor:
    ROUTE_OWNERS = {
        RouteLayer.GRAPH: "GraphStateCustody",
        RouteLayer.WORKER: "WorkerPoolFoundationRuntime",
        RouteLayer.BACKEND: "BackendRegistry",
        RouteLayer.WORKSPACE: "WorkspaceManager",
        RouteLayer.PROVIDER: "ProviderControlPlane",
        RouteLayer.MODEL: "ProviderControlPlane",
        RouteLayer.CREDENTIAL: "CredentialStore",
        RouteLayer.TRANSPORT: "ProviderStreamRuntime",
    }

    def __init__(self, store: RecoveryPlanStore) -> None:
        self.store = store

    def audit_task(self, task_id: str) -> RecoveryAuditReport:
        plans = self.store.plans(task_id=task_id, limit=5000)
        checkpoints = self.store.checkpoints(task_id=task_id, limit=5000)
        routes = self.store.route_decisions(task_id=task_id, limit=5000)
        feedback = self.store.feedback(task_id=task_id, limit=5000)
        outcomes = self.store.outcomes(task_id=task_id, limit=5000)
        findings: list[RecoveryAuditFinding] = []
        findings.extend(self._plan_findings(plans, outcomes, routes, feedback))
        findings.extend(self._checkpoint_findings(task_id, checkpoints))
        findings.extend(self._route_findings(routes))
        findings.extend(self._feedback_findings(plans, outcomes, routes, feedback))
        findings.extend(self._integration_findings(plans, outcomes, routes, feedback, checkpoints))
        findings.extend(self._journal_findings(task_id))
        findings = self._dedupe(findings)
        counts = {
            "plans": len(plans),
            "signals": len(self.store.signals(task_id=task_id, limit=5000)),
            "outcomes": len(outcomes),
            "route_decisions": len(routes),
            "routing_feedback": len(feedback),
            "checkpoints": len(checkpoints),
            "action_receipts": sum(len(self.store.action_receipts(plan_id=item.plan_id)) for item in plans),
            "checkpoint_receipts": sum(len(self.store.checkpoint_receipts(item.checkpoint_id)) for item in checkpoints),
            "journal_entries": len(self.store.journal(task_id=task_id, limit=5000)),
        }
        run_ids = tuple(sorted({plan.signal.refs.run_id for plan in plans} | {item.run_id for item in checkpoints}))
        report_id = "recoveryaudit_" + stable_digest({
            "task_id": task_id,
            "counts": counts,
            "findings": [item.to_dict() for item in findings],
        })[:40]
        return RecoveryAuditReport(
            task_id=task_id,
            run_ids=run_ids,
            findings=tuple(findings),
            counts=counts,
            custody={
                "recovery_plan": "RecoveryPlanStore",
                "checkpoint": "RecoveryPlanStore atomic checkpoint tables",
                "graph_route": "GraphStateCustody",
                "worker_route": "WorkerPoolFoundationRuntime",
                "backend_route": "BackendRegistry",
                "provider_route": "ProviderControlPlane",
                "task_projection": "SQLiteStore.TaskState",
                "routing_memory": "RecoveryPlanStore + MemoryFabric event projection",
                "llm_is_owner": False,
            },
            report_id=report_id,
        )

    def require_valid(self, task_id: str) -> RecoveryAuditReport:
        report = self.audit_task(task_id)
        if not report.ok:
            codes = ", ".join(item.code for item in report.findings if item.blocks_execution)
            raise RuntimeError(f"recovery runtime audit failed: {codes}")
        return report

    def _plan_findings(
        self,
        plans: Sequence[Any],
        outcomes: Sequence[Any],
        routes: Sequence[Any],
        feedback: Sequence[Any],
    ) -> list[RecoveryAuditFinding]:
        findings: list[RecoveryAuditFinding] = []
        outcome_by_plan: dict[str, list[Any]] = defaultdict(list)
        route_by_plan: dict[str, list[Any]] = defaultdict(list)
        feedback_by_plan: dict[str, list[Any]] = defaultdict(list)
        for outcome in outcomes:
            outcome_by_plan[outcome.plan_id].append(outcome)
        for route in routes:
            route_by_plan[route.plan_id].append(route)
        for record in feedback:
            for evidence in record.evidence_refs:
                feedback_by_plan[evidence].append(record)
        for plan in plans:
            receipts = self.store.action_receipts(plan_id=plan.plan_id)
            plan_outcomes = outcome_by_plan.get(plan.plan_id, [])
            if plan.status.terminal and not plan_outcomes:
                findings.append(self._finding(
                    "terminal_plan_without_outcome",
                    AuditSeverity.BLOCKER,
                    "terminal recovery plan has no durable outcome",
                    "plan",
                    plan.plan_id,
                ))
            if plan.status is RecoveryPlanStatus.SUCCEEDED and not receipts:
                findings.append(self._finding(
                    "successful_plan_without_receipt",
                    AuditSeverity.BLOCKER,
                    "successful recovery plan has no concrete owner receipt",
                    "plan",
                    plan.plan_id,
                ))
            sequence = tuple(str(item) for item in plan.provenance.get("action_sequence") or ())
            if not sequence:
                findings.append(self._finding(
                    "plan_action_sequence_missing",
                    AuditSeverity.ERROR,
                    "recovery plan does not persist its deterministic action sequence",
                    "plan",
                    plan.plan_id,
                ))
            elif plan.action_cursor > len(sequence):
                findings.append(self._finding(
                    "plan_cursor_out_of_range",
                    AuditSeverity.BLOCKER,
                    "recovery plan cursor exceeds its action sequence",
                    "plan",
                    plan.plan_id,
                    metadata={"cursor": plan.action_cursor, "sequence_length": len(sequence)},
                ))
            if plan.decision.selected.action.value not in sequence:
                findings.append(self._finding(
                    "selected_action_not_in_sequence",
                    AuditSeverity.ERROR,
                    "selected recovery action is absent from the applied sequence",
                    "plan",
                    plan.plan_id,
                ))
            if plan.provenance.get("llm_selected_action") is not False:
                findings.append(self._finding(
                    "llm_action_authority_not_denied",
                    AuditSeverity.BLOCKER,
                    "recovery provenance does not explicitly deny model action authority",
                    "plan",
                    plan.plan_id,
                ))
            receipt_digests: set[str] = set()
            for receipt in receipts:
                if receipt.request_digest in receipt_digests:
                    findings.append(self._finding(
                        "duplicate_action_request_digest",
                        AuditSeverity.BLOCKER,
                        "action request digest was executed more than once",
                        "receipt",
                        receipt.receipt_id,
                    ))
                receipt_digests.add(receipt.request_digest)
                if receipt.plan_id != plan.plan_id:
                    findings.append(self._finding(
                        "receipt_plan_identity_mismatch",
                        AuditSeverity.BLOCKER,
                        "action receipt points at another plan",
                        "receipt",
                        receipt.receipt_id,
                    ))
                if receipt.changed_execution and not receipt.after:
                    findings.append(self._finding(
                        "changed_receipt_without_after_state",
                        AuditSeverity.ERROR,
                        "receipt claims execution changed without an after projection",
                        "receipt",
                        receipt.receipt_id,
                    ))
            for outcome in plan_outcomes:
                missing = set(outcome.receipt_ids) - {item.receipt_id for item in receipts}
                if missing:
                    findings.append(self._finding(
                        "outcome_receipt_missing",
                        AuditSeverity.BLOCKER,
                        "recovery outcome references missing action receipts",
                        "outcome",
                        outcome.outcome_id,
                        evidence_refs=tuple(sorted(missing)),
                    ))
                if outcome.success and not any(item.changed_execution for item in receipts):
                    findings.append(self._finding(
                        "success_without_semantic_effect",
                        AuditSeverity.WARNING,
                        "successful recovery outcome has no receipt that changed execution",
                        "outcome",
                        outcome.outcome_id,
                    ))
                linked_memory = [item for item in feedback if outcome.outcome_id in item.evidence_refs]
                integrated = bool(plan.provenance.get("state_fusion_digest"))
                requires_memory = not integrated or outcome.success
                if requires_memory and not linked_memory:
                    findings.append(self._finding(
                        "outcome_without_routing_memory",
                        AuditSeverity.ERROR,
                        "recovery outcome was not fed into routing memory",
                        "outcome",
                        outcome.outcome_id,
                    ))
            route_action = plan.decision.selected.action in {
                RecoveryAction.REROUTE,
                RecoveryAction.SWITCH_BACKEND,
                RecoveryAction.SWITCH_PROVIDER,
                RecoveryAction.DEGRADE_MODEL,
                RecoveryAction.REPLAN,
            }
            if plan.status is RecoveryPlanStatus.SUCCEEDED and route_action and not route_by_plan.get(plan.plan_id):
                findings.append(self._finding(
                    "route_action_without_route_decision",
                    AuditSeverity.BLOCKER,
                    "route recovery succeeded without a layered route decision",
                    "plan",
                    plan.plan_id,
                ))
        return findings

    def _checkpoint_findings(self, task_id: str, checkpoints: Sequence[Any]) -> list[RecoveryAuditFinding]:
        findings: list[RecoveryAuditFinding] = []
        by_id = {item.checkpoint_id: item for item in checkpoints}
        child_revisions: dict[str, list[int]] = defaultdict(list)
        for checkpoint in checkpoints:
            if checkpoint.parent_checkpoint_id:
                parent = by_id.get(checkpoint.parent_checkpoint_id)
                if parent is None:
                    findings.append(self._finding(
                        "checkpoint_parent_missing",
                        AuditSeverity.BLOCKER,
                        "checkpoint ancestry points outside the task lineage",
                        "checkpoint",
                        checkpoint.checkpoint_id,
                        evidence_refs=(checkpoint.parent_checkpoint_id,),
                    ))
                elif parent.commit_revision >= checkpoint.commit_revision:
                    findings.append(self._finding(
                        "checkpoint_revision_not_monotonic",
                        AuditSeverity.BLOCKER,
                        "checkpoint child revision does not advance its parent",
                        "checkpoint",
                        checkpoint.checkpoint_id,
                    ))
                child_revisions[checkpoint.parent_checkpoint_id].append(checkpoint.commit_revision)
            if checkpoint.task_id != task_id:
                findings.append(self._finding(
                    "checkpoint_task_identity_mismatch",
                    AuditSeverity.BLOCKER,
                    "checkpoint belongs to another task",
                    "checkpoint",
                    checkpoint.checkpoint_id,
                ))
            committed_ids = {item.write_id for item in checkpoint.committed_writes}
            pending_ids = {item.write_id for item in checkpoint.pending_writes}
            overlap = committed_ids & pending_ids
            if overlap:
                findings.append(self._finding(
                    "pending_committed_write_overlap",
                    AuditSeverity.BLOCKER,
                    "checkpoint contains the same write as pending and committed",
                    "checkpoint",
                    checkpoint.checkpoint_id,
                    evidence_refs=tuple(sorted(overlap)),
                ))
            invalid_committed = [item.write_id for item in checkpoint.committed_writes if item.state is not PendingWriteState.COMMITTED]
            invalid_pending = [item.write_id for item in checkpoint.pending_writes if item.state is not PendingWriteState.PENDING]
            if invalid_committed or invalid_pending:
                findings.append(self._finding(
                    "checkpoint_write_phase_mismatch",
                    AuditSeverity.BLOCKER,
                    "checkpoint write is stored in a list that contradicts its phase",
                    "checkpoint",
                    checkpoint.checkpoint_id,
                    evidence_refs=tuple(invalid_committed + invalid_pending),
                ))
            if checkpoint.content_digest != stable_digest(checkpoint.to_dict(include_digest=False)):
                findings.append(self._finding(
                    "checkpoint_content_digest_mismatch",
                    AuditSeverity.BLOCKER,
                    "checkpoint content digest does not authenticate the decoded payload",
                    "checkpoint",
                    checkpoint.checkpoint_id,
                ))
            processed = set(checkpoint.processed_response_ids)
            replayable = [
                item.message_id for item in checkpoint.in_flight_messages
                if item.processed and item.correlation_id not in processed
            ]
            if replayable:
                findings.append(self._finding(
                    "processed_message_without_response_fence",
                    AuditSeverity.ERROR,
                    "processed in-flight message lacks a processed-response fence",
                    "checkpoint",
                    checkpoint.checkpoint_id,
                    evidence_refs=tuple(replayable),
                ))
            for fence_key in checkpoint.side_effect_fence_keys:
                fence = self.store.side_effect_fence(fence_key)
                if fence is None:
                    findings.append(self._finding(
                        "checkpoint_side_effect_fence_missing",
                        AuditSeverity.BLOCKER,
                        "checkpoint references a missing side-effect fence",
                        "checkpoint",
                        checkpoint.checkpoint_id,
                        evidence_refs=(fence_key,),
                    ))
                elif fence.task_id != task_id:
                    findings.append(self._finding(
                        "checkpoint_side_effect_fence_cross_task",
                        AuditSeverity.BLOCKER,
                        "checkpoint side-effect fence belongs to another task",
                        "checkpoint",
                        checkpoint.checkpoint_id,
                        evidence_refs=(fence_key,),
                    ))
            if checkpoint.phase is CheckpointPhase.RESUMED:
                receipts = self.store.checkpoint_receipts(checkpoint.checkpoint_id)
                if not any(item.phase is CheckpointPhase.RESUMED for item in receipts):
                    findings.append(self._finding(
                        "resumed_checkpoint_without_receipt",
                        AuditSeverity.ERROR,
                        "checkpoint phase is resumed without a durable resume receipt",
                        "checkpoint",
                        checkpoint.checkpoint_id,
                    ))
        for parent_id, revisions in child_revisions.items():
            if len(revisions) != len(set(revisions)):
                findings.append(self._finding(
                    "checkpoint_sibling_revision_collision",
                    AuditSeverity.ERROR,
                    "checkpoint siblings reuse a commit revision",
                    "checkpoint",
                    parent_id,
                ))
        findings.extend(self._lineage_cycles(checkpoints))
        return findings

    def _route_findings(self, routes: Sequence[Any]) -> list[RecoveryAuditFinding]:
        findings: list[RecoveryAuditFinding] = []
        for decision in routes:
            if not decision.changes:
                findings.append(self._finding(
                    "empty_route_decision",
                    AuditSeverity.ERROR,
                    "layered route decision contains no owner request",
                    "route_decision",
                    decision.route_decision_id,
                ))
            for change in decision.changes:
                expected_owner = self.ROUTE_OWNERS.get(change.layer)
                if expected_owner and change.owner != expected_owner:
                    findings.append(self._finding(
                        "route_owner_mismatch",
                        AuditSeverity.BLOCKER,
                        "route layer was mutated by a non-canonical owner",
                        "route_decision",
                        decision.route_decision_id,
                        metadata={
                            "layer": change.layer.value,
                            "expected_owner": expected_owner,
                            "actual_owner": change.owner,
                        },
                    ))
                if change.applied and not change.receipt_id:
                    findings.append(self._finding(
                        "applied_route_without_owner_receipt",
                        AuditSeverity.BLOCKER,
                        "applied route layer has no canonical owner receipt",
                        "route_decision",
                        decision.route_decision_id,
                    ))
                if change.applied and dict(change.before_ref) == dict(change.after_ref):
                    findings.append(self._finding(
                        "route_claimed_change_without_delta",
                        AuditSeverity.ERROR,
                        "route layer claims mutation but before and after refs match",
                        "route_decision",
                        decision.route_decision_id,
                    ))
        return findings

    def _feedback_findings(
        self,
        plans: Sequence[Any],
        outcomes: Sequence[Any],
        routes: Sequence[Any],
        feedback: Sequence[Any],
    ) -> list[RecoveryAuditFinding]:
        findings: list[RecoveryAuditFinding] = []
        plan_ids = {item.plan_id for item in plans}
        outcome_ids = {item.outcome_id for item in outcomes}
        route_ids = {item.route_decision_id for item in routes}
        for record in feedback:
            evidence = set(record.evidence_refs)
            if not (evidence & outcome_ids):
                findings.append(self._finding(
                    "routing_memory_without_outcome",
                    AuditSeverity.ERROR,
                    "routing memory is not grounded in a recovery outcome",
                    "routing_memory",
                    record.record_id,
                ))
            claimed_route = str(record.metadata.get("route_decision_id") or "")
            if claimed_route and claimed_route not in route_ids:
                findings.append(self._finding(
                    "routing_memory_route_missing",
                    AuditSeverity.ERROR,
                    "routing memory references a missing layered route decision",
                    "routing_memory",
                    record.record_id,
                    evidence_refs=(claimed_route,),
                ))
            if not record.success and record.score_delta > 0:
                findings.append(self._finding(
                    "failed_route_positive_feedback",
                    AuditSeverity.BLOCKER,
                    "failed recovery increased route preference",
                    "routing_memory",
                    record.record_id,
                ))
            if record.success and record.score_delta < 0:
                findings.append(self._finding(
                    "successful_route_negative_feedback",
                    AuditSeverity.WARNING,
                    "successful recovery decreased route preference",
                    "routing_memory",
                    record.record_id,
                ))
            if not record.evidence_refs:
                findings.append(self._finding(
                    "routing_memory_without_evidence",
                    AuditSeverity.BLOCKER,
                    "routing memory has no causal evidence references",
                    "routing_memory",
                    record.record_id,
                ))
        return findings

    def _integration_findings(
        self,
        plans: Sequence[Any],
        outcomes: Sequence[Any],
        routes: Sequence[Any],
        feedback: Sequence[Any],
        checkpoints: Sequence[Any],
    ) -> list[RecoveryAuditFinding]:
        """Audit the 07C-02 applied-proof, isolation, and exact-resume contract.

        Foundation plans remain valid without an integration observation.  The
        stronger rules are activated only by the non-empty state-fusion digest
        written by the integrated ingress path.
        """
        findings: list[RecoveryAuditFinding] = []
        plan_by_id = {item.plan_id: item for item in plans}
        outcomes_by_plan: dict[str, list[Any]] = defaultdict(list)
        routes_by_plan: dict[str, list[Any]] = defaultdict(list)
        feedback_by_outcome: dict[str, list[Any]] = defaultdict(list)
        feedback_by_plan: dict[str, list[Any]] = defaultdict(list)
        for outcome in outcomes:
            outcomes_by_plan[outcome.plan_id].append(outcome)
        for decision in routes:
            routes_by_plan[decision.plan_id].append(decision)
        outcome_ids = {item.outcome_id for item in outcomes}
        for record in feedback:
            for evidence_ref in record.evidence_refs:
                if evidence_ref in outcome_ids:
                    feedback_by_outcome[evidence_ref].append(record)
            for plan in plans:
                if plan.signal.signal_id in record.evidence_refs:
                    feedback_by_plan[plan.plan_id].append(record)

        for plan in plans:
            fusion_digest = str(plan.provenance.get("state_fusion_digest") or "")
            if not fusion_digest:
                continue
            plan_outcomes = outcomes_by_plan.get(plan.plan_id, [])
            receipts = self.store.action_receipts(plan_id=plan.plan_id)
            if not str(plan.provenance.get("observation_digest") or ""):
                findings.append(self._finding(
                    "integrated_plan_observation_missing",
                    AuditSeverity.ERROR,
                    "integrated recovery plan is not linked to its typed owner observation",
                    "plan",
                    plan.plan_id,
                ))
            if plan.provenance.get("policy_owner") != "python.RecoveryDecisionRuntime":
                findings.append(self._finding(
                    "integrated_plan_policy_owner_mismatch",
                    AuditSeverity.BLOCKER,
                    "integrated recovery plan does not preserve the canonical Python policy owner",
                    "plan",
                    plan.plan_id,
                ))
            if plan.provenance.get("llm_selected_action") is not False:
                findings.append(self._finding(
                    "integrated_plan_model_authority",
                    AuditSeverity.BLOCKER,
                    "integrated recovery plan permits model-owned action selection",
                    "plan",
                    plan.plan_id,
                ))
            if not plan_outcomes and plan.status is not RecoveryPlanStatus.PLANNED:
                findings.append(self._finding(
                    "integrated_plan_outcome_missing",
                    AuditSeverity.BLOCKER,
                    "started integrated recovery plan has no durable outcome",
                    "plan",
                    plan.plan_id,
                ))
            findings.extend(self._integrated_receipt_findings(plan, receipts))
            for outcome in plan_outcomes:
                linked_feedback = feedback_by_outcome.get(outcome.outcome_id, [])
                findings.extend(self._integrated_outcome_findings(
                    plan,
                    outcome,
                    receipts,
                    linked_feedback,
                ))
            findings.extend(self._integrated_route_findings(
                plan,
                routes_by_plan.get(plan.plan_id, []),
                feedback_by_plan.get(plan.plan_id, []),
            ))

        findings.extend(self._integrated_checkpoint_findings(checkpoints))
        for decision in routes:
            if decision.plan_id not in plan_by_id:
                findings.append(self._finding(
                    "orphan_integrated_route_decision",
                    AuditSeverity.BLOCKER,
                    "route decision cannot be traced to a durable recovery plan",
                    "route_decision",
                    decision.route_decision_id,
                ))
        return findings

    def _integrated_receipt_findings(
        self,
        plan: Any,
        receipts: Sequence[Any],
    ) -> list[RecoveryAuditFinding]:
        findings: list[RecoveryAuditFinding] = []
        receipt_ids: set[str] = set()
        external_refs: set[tuple[str, str]] = set()
        for receipt in receipts:
            if receipt.receipt_id in receipt_ids:
                findings.append(self._finding(
                    "integrated_receipt_identity_reused",
                    AuditSeverity.BLOCKER,
                    "integrated recovery reused an action receipt identity",
                    "receipt",
                    receipt.receipt_id,
                ))
            receipt_ids.add(receipt.receipt_id)
            if receipt.changed_execution and not receipt.external_receipt_ref and not (
                receipt.route_decision or receipt.checkpoint_receipt
            ):
                findings.append(self._finding(
                    "integrated_changed_receipt_unfenced",
                    AuditSeverity.BLOCKER,
                    "changed integrated action lacks a canonical external, route, or checkpoint receipt",
                    "receipt",
                    receipt.receipt_id,
                ))
            if receipt.external_receipt_ref:
                owner_ref = (receipt.owner, receipt.external_receipt_ref)
                if owner_ref in external_refs and receipt.status is not RecoveryAttemptStatus.DEFERRED:
                    findings.append(self._finding(
                        "integrated_external_receipt_replayed",
                        AuditSeverity.BLOCKER,
                        "canonical owner receipt was consumed by multiple applied actions",
                        "receipt",
                        receipt.receipt_id,
                        evidence_refs=(receipt.external_receipt_ref,),
                    ))
                external_refs.add(owner_ref)
            if receipt.plan_id != plan.plan_id:
                findings.append(self._finding(
                    "integrated_receipt_cross_plan",
                    AuditSeverity.BLOCKER,
                    "integrated action receipt belongs to another recovery plan",
                    "receipt",
                    receipt.receipt_id,
                ))
        return findings

    def _integrated_outcome_findings(
        self,
        plan: Any,
        outcome: Any,
        receipts: Sequence[Any],
        linked_feedback: Sequence[Any],
    ) -> list[RecoveryAuditFinding]:
        findings: list[RecoveryAuditFinding] = []
        if outcome.kind is RecoveryOutcomeKind.WAITING:
            if linked_feedback:
                findings.append(self._finding(
                    "waiting_outcome_wrote_routing_memory",
                    AuditSeverity.BLOCKER,
                    "waiting permission/auth/backoff outcome wrote routing memory before an applied proof",
                    "outcome",
                    outcome.outcome_id,
                    evidence_refs=tuple(item.record_id for item in linked_feedback),
                ))
            if not any(item.status is RecoveryAttemptStatus.DEFERRED for item in receipts):
                findings.append(self._finding(
                    "waiting_outcome_without_deferred_receipt",
                    AuditSeverity.ERROR,
                    "waiting integrated outcome has no deferred canonical owner receipt",
                    "outcome",
                    outcome.outcome_id,
                ))
            if plan.status not in {
                RecoveryPlanStatus.WAITING_PERMISSION,
                RecoveryPlanStatus.WAITING_AUTH,
                RecoveryPlanStatus.WAITING_BACKOFF,
            }:
                findings.append(self._finding(
                    "waiting_outcome_plan_status_mismatch",
                    AuditSeverity.BLOCKER,
                    "waiting integrated outcome does not leave its plan in a waiting state",
                    "outcome",
                    outcome.outcome_id,
                ))
            return findings

        proof_id = str(outcome.metadata.get("applied_proof_id") or "")
        continuation_id = str(outcome.metadata.get("continuation_receipt_id") or "")
        proof_before_feedback = outcome.metadata.get("feedback_after_applied_proof") is True
        if outcome.success:
            if not proof_id:
                findings.append(self._finding(
                    "integrated_success_without_applied_proof",
                    AuditSeverity.BLOCKER,
                    "successful integrated recovery lacks an applied-outcome proof",
                    "outcome",
                    outcome.outcome_id,
                ))
            if not continuation_id:
                findings.append(self._finding(
                    "integrated_success_without_continuation",
                    AuditSeverity.BLOCKER,
                    "successful integrated recovery lacks a changed downstream continuation receipt",
                    "outcome",
                    outcome.outcome_id,
                ))
            if not proof_before_feedback:
                findings.append(self._finding(
                    "integrated_feedback_order_unproven",
                    AuditSeverity.BLOCKER,
                    "integrated recovery does not prove routing memory was written after applied verification",
                    "outcome",
                    outcome.outcome_id,
                ))
            if not linked_feedback:
                findings.append(self._finding(
                    "integrated_applied_outcome_without_memory",
                    AuditSeverity.BLOCKER,
                    "applied integrated outcome was not fed to routing memory",
                    "outcome",
                    outcome.outcome_id,
                ))
            for record in linked_feedback:
                if proof_id and proof_id not in record.evidence_refs:
                    findings.append(self._finding(
                        "integrated_memory_missing_proof_ref",
                        AuditSeverity.BLOCKER,
                        "routing memory does not cite the applied-outcome proof",
                        "routing_memory",
                        record.record_id,
                        evidence_refs=(proof_id,),
                    ))
                if record.created_at < outcome.created_at:
                    findings.append(self._finding(
                        "integrated_memory_precedes_outcome",
                        AuditSeverity.BLOCKER,
                        "routing memory timestamp precedes its durable outcome",
                        "routing_memory",
                        record.record_id,
                        evidence_refs=(outcome.outcome_id,),
                    ))
        elif linked_feedback:
            findings.append(self._finding(
                "unverified_failure_wrote_routing_memory",
                AuditSeverity.BLOCKER,
                "failed integrated attempt wrote routing memory without an applied proof",
                "outcome",
                outcome.outcome_id,
                evidence_refs=tuple(item.record_id for item in linked_feedback),
            ))
        return findings

    def _integrated_route_findings(
        self,
        plan: Any,
        decisions: Sequence[Any],
        feedback: Sequence[Any],
    ) -> list[RecoveryAuditFinding]:
        findings: list[RecoveryAuditFinding] = []
        for decision in decisions:
            applied = [item for item in decision.changes if item.applied]
            requested = [item for item in decision.changes if item.requested]
            if any(item not in requested for item in applied):
                findings.append(self._finding(
                    "integrated_route_applied_without_request",
                    AuditSeverity.BLOCKER,
                    "route owner applied a layer that was not requested",
                    "route_decision",
                    decision.route_decision_id,
                ))
            explicit = bool(plan.provenance.get("explicit_escalation")) or decision.escalation not in {"", "none"}
            if len(applied) > 1 and not explicit:
                findings.append(self._finding(
                    "integrated_cross_layer_route_without_escalation",
                    AuditSeverity.BLOCKER,
                    "multiple route layers changed without an explicit escalation receipt",
                    "route_decision",
                    decision.route_decision_id,
                    metadata={"layers": [item.layer.value for item in applied]},
                ))
            selected_layer = plan.decision.selected.route_layer
            if selected_layer is not None and applied and selected_layer not in {item.layer for item in applied}:
                findings.append(self._finding(
                    "integrated_selected_route_layer_not_applied",
                    AuditSeverity.BLOCKER,
                    "applied route differs from the layer selected by RecoveryDecisionRuntime",
                    "route_decision",
                    decision.route_decision_id,
                    metadata={
                        "selected": selected_layer.value,
                        "applied": [item.layer.value for item in applied],
                    },
                ))
            matching_feedback = [
                item for item in feedback
                if decision.route_decision_id in item.evidence_refs
                or item.metadata.get("route_decision_id") == decision.route_decision_id
            ]
            applied_layers = tuple(item.layer for item in applied)
            for record in matching_feedback:
                if tuple(record.route_layers) != tuple(item.layer for item in decision.changes):
                    findings.append(self._finding(
                        "integrated_memory_route_layer_mismatch",
                        AuditSeverity.ERROR,
                        "routing memory layer projection differs from its canonical route decision",
                        "routing_memory",
                        record.record_id,
                        metadata={
                            "applied_layers": [item.value for item in applied_layers],
                            "memory_layers": [item.value for item in record.route_layers],
                        },
                    ))
        return findings

    def _integrated_checkpoint_findings(
        self,
        checkpoints: Sequence[Any],
    ) -> list[RecoveryAuditFinding]:
        findings: list[RecoveryAuditFinding] = []
        seen_resume_tokens: dict[str, str] = {}
        for checkpoint in checkpoints:
            receipts = self.store.checkpoint_receipts(checkpoint.checkpoint_id)
            for receipt in receipts:
                if receipt.phase is not CheckpointPhase.RESUMED:
                    continue
                if not receipt.resume_token:
                    findings.append(self._finding(
                        "integrated_resume_token_missing",
                        AuditSeverity.BLOCKER,
                        "resumed checkpoint lacks a stable exact-resume token",
                        "checkpoint_receipt",
                        receipt.receipt_id,
                    ))
                previous = seen_resume_tokens.get(receipt.resume_token)
                if previous and previous != receipt.checkpoint_id:
                    findings.append(self._finding(
                        "integrated_resume_token_cross_checkpoint",
                        AuditSeverity.BLOCKER,
                        "exact-resume token was reused by another checkpoint",
                        "checkpoint_receipt",
                        receipt.receipt_id,
                        evidence_refs=(previous, receipt.checkpoint_id),
                    ))
                seen_resume_tokens[receipt.resume_token] = receipt.checkpoint_id
                completed = set(checkpoint.completed_step_ids)
                if not set(receipt.bypassed_step_ids).issubset(completed):
                    findings.append(self._finding(
                        "integrated_resume_bypass_not_completed",
                        AuditSeverity.BLOCKER,
                        "exact resume bypasses a step that is not committed in the checkpoint",
                        "checkpoint_receipt",
                        receipt.receipt_id,
                        evidence_refs=tuple(sorted(set(receipt.bypassed_step_ids) - completed)),
                    ))
                processed = set(checkpoint.processed_response_ids)
                if not set(receipt.skipped_response_ids).issubset(processed):
                    findings.append(self._finding(
                        "integrated_resume_response_fence_mismatch",
                        AuditSeverity.BLOCKER,
                        "exact resume skips a response absent from the processed-response fence",
                        "checkpoint_receipt",
                        receipt.receipt_id,
                    ))
                if len(receipt.fenced_effect_keys) != len(set(receipt.fenced_effect_keys)):
                    findings.append(self._finding(
                        "integrated_resume_duplicate_effect_fence",
                        AuditSeverity.ERROR,
                        "exact-resume receipt repeats a side-effect fence",
                        "checkpoint_receipt",
                        receipt.receipt_id,
                    ))
                for fence_key in receipt.fenced_effect_keys:
                    fence = self.store.side_effect_fence(fence_key)
                    if fence is None or fence.state is not SideEffectState.COMMITTED:
                        findings.append(self._finding(
                            "integrated_resume_effect_fence_not_committed",
                            AuditSeverity.BLOCKER,
                            "exact resume preserves a missing or uncommitted side-effect fence",
                            "checkpoint_receipt",
                            receipt.receipt_id,
                            evidence_refs=(fence_key,),
                        ))
        return findings

    def _journal_findings(self, task_id: str) -> list[RecoveryAuditFinding]:
        findings: list[RecoveryAuditFinding] = []
        journal = self.store.journal(task_id=task_id, limit=5000)
        sequences = [int(item["sequence"]) for item in journal]
        if sequences != sorted(sequences):
            findings.append(self._finding(
                "journal_sequence_not_monotonic",
                AuditSeverity.BLOCKER,
                "recovery journal order is not monotonic",
                "journal",
                task_id,
            ))
        seen: set[int] = set()
        for item in journal:
            sequence = int(item["sequence"])
            if sequence in seen:
                findings.append(self._finding(
                    "journal_sequence_duplicate",
                    AuditSeverity.BLOCKER,
                    "recovery journal sequence was reused",
                    "journal",
                    str(sequence),
                ))
            seen.add(sequence)
            if str(item.get("task_id") or "") != task_id:
                findings.append(self._finding(
                    "journal_cross_task_entry",
                    AuditSeverity.BLOCKER,
                    "task journal contains an entry from another task",
                    "journal",
                    str(sequence),
                ))
        return findings

    def _lineage_cycles(self, checkpoints: Sequence[Any]) -> list[RecoveryAuditFinding]:
        parent = {item.checkpoint_id: item.parent_checkpoint_id for item in checkpoints if item.parent_checkpoint_id}
        findings: list[RecoveryAuditFinding] = []
        for checkpoint_id in parent:
            path: list[str] = []
            current = checkpoint_id
            while current:
                if current in path:
                    cycle = path[path.index(current):] + [current]
                    findings.append(self._finding(
                        "checkpoint_lineage_cycle",
                        AuditSeverity.BLOCKER,
                        "checkpoint ancestry contains a cycle",
                        "checkpoint",
                        checkpoint_id,
                        evidence_refs=tuple(cycle),
                    ))
                    break
                path.append(current)
                current = parent.get(current, "")
        return findings

    @staticmethod
    def _finding(
        code: str,
        severity: AuditSeverity,
        message: str,
        entity_kind: str,
        entity_id: str,
        *,
        evidence_refs: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> RecoveryAuditFinding:
        return RecoveryAuditFinding(
            code=code,
            severity=severity,
            message=message,
            entity_kind=entity_kind,
            entity_id=entity_id,
            evidence_refs=tuple(str(item) for item in evidence_refs if str(item)),
            metadata=dict(metadata or {}),
        )

    @staticmethod
    def _dedupe(findings: Sequence[RecoveryAuditFinding]) -> list[RecoveryAuditFinding]:
        result: list[RecoveryAuditFinding] = []
        keys: set[str] = set()
        for finding in findings:
            key = stable_digest(finding.to_dict())
            if key not in keys:
                keys.add(key)
                result.append(finding)
        return sorted(
            result,
            key=lambda item: (
                -[AuditSeverity.INFO, AuditSeverity.WARNING, AuditSeverity.ERROR, AuditSeverity.BLOCKER].index(item.severity),
                item.code,
                item.entity_kind,
                item.entity_id,
            ),
        )


def recovery_audit_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.recovery-runtime-audit-contract/v1",
        "checks": [
            "terminal plan has outcome",
            "successful plan has concrete receipt",
            "action cursor remains within deterministic sequence",
            "LLM does not own applied action",
            "pending and committed writes do not overlap",
            "checkpoint digest and ancestry are valid",
            "processed responses and side effects remain fenced",
            "canonical route owner receipt exists",
            "outcome feeds subsequent routing memory",
            "journal causality remains task-local and monotonic",
        ],
    }
