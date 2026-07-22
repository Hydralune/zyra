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
                if not linked_memory:
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
