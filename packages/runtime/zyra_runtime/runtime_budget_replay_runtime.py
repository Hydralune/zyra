from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .api_retry_playbook_runtime import ApiRetryPlaybookReport
from .compact_restore_policy_runtime import CompactRestorePolicyReport
from .compact_restore_runtime import CompactRestoreReport
from .runtime_budget_state import RuntimeBudgetEventKind, RuntimeBudgetMutation, RuntimeBudgetSnapshot


class RuntimeBudgetReplayStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    DISABLED = "disabled"


class RuntimeBudgetReplaySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class RuntimeBudgetReplaySurface(StrEnum):
    SNAPSHOT = "snapshot"
    MUTATION_SEQUENCE = "mutation_sequence"
    EVENT_FLOW = "event_flow"
    COMPACT_RESTORE = "compact_restore"
    API_RETRY = "api_retry"
    SOURCE_DECISION = "source_decision"


class RuntimeBudgetReplayNodeKind(StrEnum):
    CONTEXT_USAGE = "context_usage"
    TOOL_RESULT_USAGE = "tool_result_usage"
    MODEL_USAGE = "model_usage"
    RETRY_USAGE = "retry_usage"
    COMPACT_BOUNDARY = "compact_boundary"
    NEXT_TURN_RESTORE = "next_turn_restore"
    FINDING = "finding"
    UNKNOWN = "unknown"


class RuntimeBudgetReplayEdgeKind(StrEnum):
    MUTATION_ORDER = "mutation_order"
    CAUSAL_PARENT = "causal_parent"
    COMPACT_TO_RESTORE = "compact_to_restore"
    RETRY_TO_PLAYBOOK = "retry_to_playbook"
    EVENT_TO_MUTATION = "event_to_mutation"


class RuntimeBudgetReplayRuleKind(StrEnum):
    SNAPSHOT_PRESENT = "snapshot_present"
    MUTATION_IDS_UNIQUE = "mutation_ids_unique"
    INITIAL_CONTEXT_PRESENT = "initial_context_present"
    MODEL_USAGE_RECORDED = "model_usage_recorded"
    TOOL_CONTEXT_RECORDED = "tool_context_recorded"
    COMPACT_MUTATION_RECORDED = "compact_mutation_recorded"
    RESTORE_MUTATION_RECORDED = "restore_mutation_recorded"
    RETRY_BUDGET_MATCHES_PLAYBOOK = "retry_budget_matches_playbook"
    COMPACT_POLICY_MATCHES_BUDGET = "compact_policy_matches_budget"
    EVENT_FLOW_HAS_BUDGET_UPDATES = "event_flow_has_budget_updates"
    SOURCE_DECISIONS_CLEAN = "source_decisions_clean"


@dataclass(frozen=True, slots=True)
class RuntimeBudgetReplayFinding:
    code: str
    severity: RuntimeBudgetReplaySeverity
    surface: RuntimeBudgetReplaySurface
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == RuntimeBudgetReplaySeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "blocking": self.blocking,
            "evidence": to_jsonable(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class RuntimeBudgetReplayNode:
    node_id: str
    kind: RuntimeBudgetReplayNodeKind
    sequence: int
    mutation_id: str
    mutation_kind: str
    scope: str
    reason: str
    used_delta: int = 0
    input_tokens_delta: int = 0
    output_tokens_delta: int = 0
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": str(self.kind),
            "sequence": self.sequence,
            "mutation_id": self.mutation_id,
            "mutation_kind": self.mutation_kind,
            "scope": self.scope,
            "reason": self.reason,
            "used_delta": self.used_delta,
            "input_tokens_delta": self.input_tokens_delta,
            "output_tokens_delta": self.output_tokens_delta,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RuntimeBudgetReplayEdge:
    edge_id: str
    kind: RuntimeBudgetReplayEdgeKind
    source_node_id: str
    target_node_id: str
    ok: bool = True
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "kind": str(self.kind),
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "ok": self.ok,
            "evidence": to_jsonable(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class RuntimeBudgetReplayRule:
    rule_id: str
    kind: RuntimeBudgetReplayRuleKind
    required: bool
    satisfied: bool
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "kind": str(self.kind),
            "required": self.required,
            "satisfied": self.satisfied,
            "message": self.message,
            "evidence": to_jsonable(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class RuntimeBudgetReplayReport:
    report_id: str
    session_id: str
    worker_request_id: str
    status: RuntimeBudgetReplayStatus
    nodes: tuple[RuntimeBudgetReplayNode, ...]
    edges: tuple[RuntimeBudgetReplayEdge, ...]
    rules: tuple[RuntimeBudgetReplayRule, ...]
    findings: tuple[RuntimeBudgetReplayFinding, ...]
    event_phase_counts: dict[str, int]
    snapshot_status: str
    mutation_count: int
    retry_mutation_count: int
    compact_mutation_count: int
    restore_mutation_count: int
    source_decisions: tuple[dict[str, str], ...] = ()
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return self.status != RuntimeBudgetReplayStatus.BLOCKED and not any(finding.blocking for finding in self.findings)

    @property
    def blocking_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def satisfied_rule_count(self) -> int:
        return sum(1 for rule in self.rules if rule.satisfied)

    @property
    def required_rule_count(self) -> int:
        return sum(1 for rule in self.rules if rule.required)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.runtime_budget_replay.v1",
            "report_id": self.report_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "status": str(self.status),
            "ok": self.ok,
            "snapshot_status": self.snapshot_status,
            "mutation_count": self.mutation_count,
            "retry_mutation_count": self.retry_mutation_count,
            "compact_mutation_count": self.compact_mutation_count,
            "restore_mutation_count": self.restore_mutation_count,
            "event_phase_counts": dict(self.event_phase_counts),
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "rules": [rule.to_dict() for rule in self.rules],
            "findings": [finding.to_dict() for finding in self.findings],
            "source_decisions": [dict(item) for item in self.source_decisions],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "runtime_budget_replay_ok": str(self.ok).lower(),
            "runtime_budget_replay_status": str(self.status),
            "runtime_budget_replay_report_id": self.report_id,
            "runtime_budget_replay_snapshot_status": self.snapshot_status,
            "runtime_budget_replay_mutations": str(self.mutation_count),
            "runtime_budget_replay_nodes": str(len(self.nodes)),
            "runtime_budget_replay_edges": str(len(self.edges)),
            "runtime_budget_replay_rules": str(len(self.rules)),
            "runtime_budget_replay_required_rules": str(self.required_rule_count),
            "runtime_budget_replay_satisfied_rules": str(self.satisfied_rule_count),
            "runtime_budget_replay_blocking_count": str(self.blocking_count),
            "runtime_budget_replay_retry_mutations": str(self.retry_mutation_count),
            "runtime_budget_replay_compact_mutations": str(self.compact_mutation_count),
            "runtime_budget_replay_restore_mutations": str(self.restore_mutation_count),
        }


class RuntimeBudgetReplayRuntime:
    """Replay and audit RuntimeBudgetState as the causal budget ledger.

    The runtime is intentionally downstream of compact restore and API retry.
    It treats RuntimeBudgetState mutations as the state custody source and then
    cross-checks the semantic reports that claim compact, restore and retry
    happened. This catches fake integrations where events exist but the durable
    budget ledger was not mutated.
    """

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        budget_snapshot: RuntimeBudgetSnapshot | None,
        compact_restore: CompactRestoreReport | None,
        compact_policy: CompactRestorePolicyReport | None,
        api_retry_playbook: ApiRetryPlaybookReport | None,
        event_records: Sequence[EventRecord],
        source_decisions: Sequence[Mapping[str, str]] = (),
    ) -> RuntimeBudgetReplayReport:
        findings: list[RuntimeBudgetReplayFinding] = []
        rules: list[RuntimeBudgetReplayRule] = []
        nodes: list[RuntimeBudgetReplayNode] = []
        edges: list[RuntimeBudgetReplayEdge] = []
        phases = _phase_counts(event_records)
        if budget_snapshot is None:
            findings.append(
                RuntimeBudgetReplayFinding(
                    code="RUNTIME_BUDGET_SNAPSHOT_MISSING",
                    severity=RuntimeBudgetReplaySeverity.BLOCKER,
                    surface=RuntimeBudgetReplaySurface.SNAPSHOT,
                    message="RuntimeBudgetReplayRuntime could not replay state because no RuntimeBudgetSnapshot was supplied.",
                )
            )
            rules.append(
                RuntimeBudgetReplayRule(
                    rule_id="budget-replay-snapshot-present",
                    kind=RuntimeBudgetReplayRuleKind.SNAPSHOT_PRESENT,
                    required=True,
                    satisfied=False,
                    message="RuntimeBudgetSnapshot must be present.",
                )
            )
            return RuntimeBudgetReplayReport(
                report_id=new_id("budget_replay"),
                session_id=session_id,
                worker_request_id=worker_request_id,
                status=RuntimeBudgetReplayStatus.BLOCKED,
                nodes=(),
                edges=(),
                rules=tuple(rules),
                findings=tuple(findings),
                event_phase_counts=phases,
                snapshot_status="missing",
                mutation_count=0,
                retry_mutation_count=0,
                compact_mutation_count=0,
                restore_mutation_count=0,
                source_decisions=tuple(dict(item) for item in source_decisions),
            )
        mutations = tuple(budget_snapshot.mutations)
        nodes = _nodes_from_mutations(mutations)
        edges.extend(_ordered_edges(nodes))
        edges.extend(_compact_restore_edges(nodes, compact_restore))
        edges.extend(_retry_edges(nodes, api_retry_playbook))
        mutation_ids = [mutation.mutation_id for mutation in mutations]
        duplicate_ids = sorted({mutation_id for mutation_id in mutation_ids if mutation_ids.count(mutation_id) > 1})
        kind_counts = _kind_counts(mutations)
        context_count = kind_counts.get(str(RuntimeBudgetEventKind.CONTEXT_USAGE), 0)
        model_count = kind_counts.get(str(RuntimeBudgetEventKind.MODEL_USAGE), 0)
        tool_count = kind_counts.get(str(RuntimeBudgetEventKind.TOOL_RESULT_USAGE), 0)
        retry_count = kind_counts.get(str(RuntimeBudgetEventKind.RETRY_USAGE), 0)
        compact_count = kind_counts.get(str(RuntimeBudgetEventKind.COMPACT_BOUNDARY), 0)
        restore_count = kind_counts.get(str(RuntimeBudgetEventKind.NEXT_TURN_RESTORE), 0)
        rules.extend(
            [
                RuntimeBudgetReplayRule(
                    rule_id="budget-replay-snapshot-present",
                    kind=RuntimeBudgetReplayRuleKind.SNAPSHOT_PRESENT,
                    required=True,
                    satisfied=True,
                    message="RuntimeBudgetSnapshot is present.",
                    evidence={"snapshot_id": budget_snapshot.snapshot_id, "status": str(budget_snapshot.status)},
                ),
                RuntimeBudgetReplayRule(
                    rule_id="budget-replay-mutation-ids-unique",
                    kind=RuntimeBudgetReplayRuleKind.MUTATION_IDS_UNIQUE,
                    required=True,
                    satisfied=not duplicate_ids,
                    message="Budget mutation ids must be unique.",
                    evidence={"duplicate_ids": duplicate_ids, "mutation_count": len(mutations)},
                ),
                RuntimeBudgetReplayRule(
                    rule_id="budget-replay-initial-context-present",
                    kind=RuntimeBudgetReplayRuleKind.INITIAL_CONTEXT_PRESENT,
                    required=True,
                    satisfied=context_count > 0,
                    message="Context usage must be recorded before compact or restore decisions.",
                    evidence={"context_mutations": context_count},
                ),
                RuntimeBudgetReplayRule(
                    rule_id="budget-replay-model-usage-recorded",
                    kind=RuntimeBudgetReplayRuleKind.MODEL_USAGE_RECORDED,
                    required=True,
                    satisfied=model_count > 0,
                    message="Model stream usage must mutate RuntimeBudgetState.",
                    evidence={"model_mutations": model_count, "model_stream_phases": phases.get("model_stream_report", 0)},
                ),
                RuntimeBudgetReplayRule(
                    rule_id="budget-replay-tool-context-recorded",
                    kind=RuntimeBudgetReplayRuleKind.TOOL_CONTEXT_RECORDED,
                    required=True,
                    satisfied=tool_count > 0,
                    message="Tool result context projection must mutate RuntimeBudgetState.",
                    evidence={"tool_result_mutations": tool_count},
                ),
                RuntimeBudgetReplayRule(
                    rule_id="budget-replay-compact-mutation-recorded",
                    kind=RuntimeBudgetReplayRuleKind.COMPACT_MUTATION_RECORDED,
                    required=bool(compact_restore and compact_restore.compact_needed),
                    satisfied=(compact_count > 0) if bool(compact_restore and compact_restore.compact_needed) else True,
                    message="Compact boundary decisions must be mirrored into RuntimeBudgetState.",
                    evidence={
                        "compact_needed": bool(compact_restore and compact_restore.compact_needed),
                        "compact_mutations": compact_count,
                        "compact_phase_count": phases.get("compact_boundary_created", 0) + phases.get("compact_needed", 0),
                    },
                ),
                RuntimeBudgetReplayRule(
                    rule_id="budget-replay-restore-mutation-recorded",
                    kind=RuntimeBudgetReplayRuleKind.RESTORE_MUTATION_RECORDED,
                    required=bool(compact_restore and compact_restore.restore_contract is not None),
                    satisfied=(restore_count > 0) if bool(compact_restore and compact_restore.restore_contract is not None) else True,
                    message="Next-turn restore contracts must be mirrored into RuntimeBudgetState.",
                    evidence={
                        "restore_contract_present": bool(compact_restore and compact_restore.restore_contract is not None),
                        "restore_mutations": restore_count,
                        "restore_phase_count": phases.get("next_turn_restore_contract", 0),
                    },
                ),
                RuntimeBudgetReplayRule(
                    rule_id="budget-replay-event-flow-has-budget-updates",
                    kind=RuntimeBudgetReplayRuleKind.EVENT_FLOW_HAS_BUDGET_UPDATES,
                    required=True,
                    satisfied=phases.get("runtime_budget_updated", 0) > 0,
                    message="The query event stream must expose runtime_budget_updated phases.",
                    evidence={"runtime_budget_updated": phases.get("runtime_budget_updated", 0)},
                ),
            ]
        )
        retry_decisions_requiring_budget = (
            api_retry_playbook.retry_budget_required_count if api_retry_playbook is not None else 0
        )
        rules.append(
            RuntimeBudgetReplayRule(
                rule_id="budget-replay-retry-budget-matches-playbook",
                kind=RuntimeBudgetReplayRuleKind.RETRY_BUDGET_MATCHES_PLAYBOOK,
                required=retry_decisions_requiring_budget > 0,
                satisfied=retry_count >= retry_decisions_requiring_budget,
                message="Retry playbook decisions that require budget state must have matching retry mutations.",
                evidence={
                    "retry_mutations": retry_count,
                    "retry_budget_required": retry_decisions_requiring_budget,
                    "api_retry_playbook_status": str(api_retry_playbook.status) if api_retry_playbook is not None else "missing",
                },
            )
        )
        policy_required_segments = compact_policy.required_segment_count if compact_policy is not None else 0
        rules.append(
            RuntimeBudgetReplayRule(
                rule_id="budget-replay-compact-policy-matches-budget",
                kind=RuntimeBudgetReplayRuleKind.COMPACT_POLICY_MATCHES_BUDGET,
                required=bool(compact_policy is not None and compact_policy.required_segment_count > 0),
                satisfied=_compact_policy_budget_match(compact_policy, compact_count=compact_count, restore_count=restore_count),
                message="Compact restore policy must have a matching compact and restore budget mutation.",
                evidence={
                    "required_segments": policy_required_segments,
                    "compact_mutations": compact_count,
                    "restore_mutations": restore_count,
                    "compact_policy_status": str(compact_policy.status) if compact_policy is not None else "missing",
                },
            )
        )
        clean_source_decisions = all(_source_decision_ok(item) for item in source_decisions)
        rules.append(
            RuntimeBudgetReplayRule(
                rule_id="budget-replay-source-decisions-clean",
                kind=RuntimeBudgetReplayRuleKind.SOURCE_DECISIONS_CLEAN,
                required=True,
                satisfied=clean_source_decisions,
                message="Replay runtime source decisions must land in Zyra-owned modules.",
                evidence={"source_decision_count": len(source_decisions)},
            )
        )
        findings.extend(_findings_for_rules(rules))
        findings.extend(_sequence_findings(nodes))
        findings.extend(_edge_findings(edges))
        if str(budget_snapshot.status) in {"blocked", "disabled"}:
            findings.append(
                RuntimeBudgetReplayFinding(
                    code="RUNTIME_BUDGET_STATE_NOT_READY",
                    severity=RuntimeBudgetReplaySeverity.BLOCKER,
                    surface=RuntimeBudgetReplaySurface.SNAPSHOT,
                    message="RuntimeBudgetState snapshot status is not ready for replay.",
                    evidence={"status": str(budget_snapshot.status), "snapshot_id": budget_snapshot.snapshot_id},
                )
            )
        status = RuntimeBudgetReplayStatus.READY
        if any(finding.blocking for finding in findings):
            status = RuntimeBudgetReplayStatus.BLOCKED
        elif findings:
            status = RuntimeBudgetReplayStatus.DEGRADED
        return RuntimeBudgetReplayReport(
            report_id=new_id("budget_replay"),
            session_id=session_id,
            worker_request_id=worker_request_id,
            status=status,
            nodes=tuple(nodes),
            edges=tuple(edges),
            rules=tuple(rules),
            findings=tuple(findings),
            event_phase_counts=phases,
            snapshot_status=str(budget_snapshot.status),
            mutation_count=len(mutations),
            retry_mutation_count=retry_count,
            compact_mutation_count=compact_count,
            restore_mutation_count=restore_count,
            source_decisions=tuple(dict(item) for item in source_decisions),
        )

    def event_for_report(
        self,
        report: RuntimeBudgetReplayReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": report.session_id,
                    "worker_request_id": report.worker_request_id,
                    "phase": "runtime_budget_replay",
                    "runtime_budget_replay": report.to_dict(),
                }
            },
        )


def runtime_budget_replay_metadata(report: RuntimeBudgetReplayReport | None) -> dict[str, str]:
    if report is None:
        return {
            "runtime_budget_replay_ok": "false",
            "runtime_budget_replay_status": "missing",
            "runtime_budget_replay_report_id": "",
        }
    return report.metadata()


def render_runtime_budget_replay_markdown(report: RuntimeBudgetReplayReport) -> str:
    lines = [
        "# Runtime Budget Replay",
        "",
        f"- report_id: {report.report_id}",
        f"- status: {report.status}",
        f"- ok: {str(report.ok).lower()}",
        f"- mutations: {report.mutation_count}",
        f"- retry_mutations: {report.retry_mutation_count}",
        f"- compact_mutations: {report.compact_mutation_count}",
        f"- restore_mutations: {report.restore_mutation_count}",
        "",
        "## Rules",
    ]
    for rule in report.rules:
        lines.append(f"- {rule.rule_id}: satisfied={str(rule.satisfied).lower()} required={str(rule.required).lower()}")
    lines.extend(["", "## Findings"])
    if report.findings:
        for finding in report.findings:
            lines.append(f"- {finding.severity} {finding.code}: {finding.message}")
    else:
        lines.append("- none")
    return "\n".join(lines)


def default_runtime_budget_replay_source_decisions() -> tuple[dict[str, str], ...]:
    return (
        {
            "source_repo": "opencode",
            "source_path": "packages/opencode/src/session",
            "target_path": "packages/runtime/zyra_runtime/runtime_budget_replay_runtime.py",
            "decision": "zyra_module_migrated",
            "capability": "durable budget mutation replay and state custody validation",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/QueryEngine.ts",
            "target_path": "packages/runtime/zyra_runtime/runtime_budget_replay_runtime.py",
            "decision": "zyra_module_migrated",
            "capability": "context compact and model retry event causality validation",
        },
    )


def _nodes_from_mutations(mutations: Sequence[RuntimeBudgetMutation]) -> list[RuntimeBudgetReplayNode]:
    nodes: list[RuntimeBudgetReplayNode] = []
    for index, mutation in enumerate(mutations, start=1):
        nodes.append(
            RuntimeBudgetReplayNode(
                node_id=new_id("budget_node"),
                kind=_node_kind_for_mutation(mutation),
                sequence=index,
                mutation_id=mutation.mutation_id,
                mutation_kind=str(mutation.kind),
                scope=str(mutation.scope),
                reason=mutation.reason,
                used_delta=mutation.used_delta,
                input_tokens_delta=mutation.input_tokens_delta,
                output_tokens_delta=mutation.output_tokens_delta,
                metadata=dict(mutation.metadata),
            )
        )
    return nodes


def _node_kind_for_mutation(mutation: RuntimeBudgetMutation) -> RuntimeBudgetReplayNodeKind:
    kind = str(mutation.kind)
    if kind == str(RuntimeBudgetEventKind.CONTEXT_USAGE):
        return RuntimeBudgetReplayNodeKind.CONTEXT_USAGE
    if kind == str(RuntimeBudgetEventKind.TOOL_RESULT_USAGE):
        return RuntimeBudgetReplayNodeKind.TOOL_RESULT_USAGE
    if kind == str(RuntimeBudgetEventKind.MODEL_USAGE):
        return RuntimeBudgetReplayNodeKind.MODEL_USAGE
    if kind == str(RuntimeBudgetEventKind.RETRY_USAGE):
        return RuntimeBudgetReplayNodeKind.RETRY_USAGE
    if kind == str(RuntimeBudgetEventKind.COMPACT_BOUNDARY):
        return RuntimeBudgetReplayNodeKind.COMPACT_BOUNDARY
    if kind == str(RuntimeBudgetEventKind.NEXT_TURN_RESTORE):
        return RuntimeBudgetReplayNodeKind.NEXT_TURN_RESTORE
    return RuntimeBudgetReplayNodeKind.UNKNOWN


def _ordered_edges(nodes: Sequence[RuntimeBudgetReplayNode]) -> list[RuntimeBudgetReplayEdge]:
    edges: list[RuntimeBudgetReplayEdge] = []
    previous: RuntimeBudgetReplayNode | None = None
    for node in nodes:
        if previous is not None:
            edges.append(
                RuntimeBudgetReplayEdge(
                    edge_id=new_id("budget_edge"),
                    kind=RuntimeBudgetReplayEdgeKind.MUTATION_ORDER,
                    source_node_id=previous.node_id,
                    target_node_id=node.node_id,
                    ok=previous.sequence < node.sequence,
                    evidence={"source_sequence": previous.sequence, "target_sequence": node.sequence},
                )
            )
        previous = node
    return edges


def _compact_restore_edges(
    nodes: Sequence[RuntimeBudgetReplayNode],
    compact_restore: CompactRestoreReport | None,
) -> list[RuntimeBudgetReplayEdge]:
    edges: list[RuntimeBudgetReplayEdge] = []
    if compact_restore is None:
        return edges
    compact_nodes = [node for node in nodes if node.kind == RuntimeBudgetReplayNodeKind.COMPACT_BOUNDARY]
    restore_nodes = [node for node in nodes if node.kind == RuntimeBudgetReplayNodeKind.NEXT_TURN_RESTORE]
    for compact_node in compact_nodes:
        for restore_node in restore_nodes:
            if restore_node.sequence <= compact_node.sequence:
                continue
            edges.append(
                RuntimeBudgetReplayEdge(
                    edge_id=new_id("budget_edge"),
                    kind=RuntimeBudgetReplayEdgeKind.COMPACT_TO_RESTORE,
                    source_node_id=compact_node.node_id,
                    target_node_id=restore_node.node_id,
                    ok=True,
                    evidence={
                        "boundary_id": compact_restore.boundary.boundary_id if compact_restore.boundary is not None else "",
                        "restore_contract_id": compact_restore.restore_contract.contract_id
                        if compact_restore.restore_contract is not None
                        else "",
                    },
                )
            )
            break
    return edges


def _retry_edges(
    nodes: Sequence[RuntimeBudgetReplayNode],
    api_retry_playbook: ApiRetryPlaybookReport | None,
) -> list[RuntimeBudgetReplayEdge]:
    if api_retry_playbook is None:
        return []
    retry_nodes = [node for node in nodes if node.kind == RuntimeBudgetReplayNodeKind.RETRY_USAGE]
    decision_count = api_retry_playbook.retry_budget_required_count
    edges: list[RuntimeBudgetReplayEdge] = []
    for index, node in enumerate(retry_nodes[:decision_count], start=1):
        edges.append(
            RuntimeBudgetReplayEdge(
                edge_id=new_id("budget_edge"),
                kind=RuntimeBudgetReplayEdgeKind.RETRY_TO_PLAYBOOK,
                source_node_id=node.node_id,
                target_node_id=api_retry_playbook.report_id,
                ok=True,
                evidence={"playbook_decision_index": index, "required_retry_budget": decision_count},
            )
        )
    return edges


def _findings_for_rules(rules: Iterable[RuntimeBudgetReplayRule]) -> list[RuntimeBudgetReplayFinding]:
    findings: list[RuntimeBudgetReplayFinding] = []
    for rule in rules:
        if rule.satisfied:
            continue
        findings.append(
            RuntimeBudgetReplayFinding(
                code=f"RULE_FAILED_{str(rule.kind).upper()}",
                severity=RuntimeBudgetReplaySeverity.BLOCKER if rule.required else RuntimeBudgetReplaySeverity.WARNING,
                surface=_surface_for_rule(rule.kind),
                message=rule.message,
                evidence={"rule_id": rule.rule_id, **rule.evidence},
            )
        )
    return findings


def _sequence_findings(nodes: Sequence[RuntimeBudgetReplayNode]) -> list[RuntimeBudgetReplayFinding]:
    findings: list[RuntimeBudgetReplayFinding] = []
    first_context = _first_sequence(nodes, RuntimeBudgetReplayNodeKind.CONTEXT_USAGE)
    first_model = _first_sequence(nodes, RuntimeBudgetReplayNodeKind.MODEL_USAGE)
    first_compact = _first_sequence(nodes, RuntimeBudgetReplayNodeKind.COMPACT_BOUNDARY)
    first_restore = _first_sequence(nodes, RuntimeBudgetReplayNodeKind.NEXT_TURN_RESTORE)
    if first_context is not None and first_model is not None and first_model < first_context:
        findings.append(
            RuntimeBudgetReplayFinding(
                code="MODEL_BEFORE_CONTEXT",
                severity=RuntimeBudgetReplaySeverity.ERROR,
                surface=RuntimeBudgetReplaySurface.MUTATION_SEQUENCE,
                message="Model usage was recorded before initial context usage.",
                evidence={"first_context": first_context, "first_model": first_model},
            )
        )
    if first_restore is not None and first_compact is not None and first_restore < first_compact:
        findings.append(
            RuntimeBudgetReplayFinding(
                code="RESTORE_BEFORE_COMPACT",
                severity=RuntimeBudgetReplaySeverity.WARNING,
                surface=RuntimeBudgetReplaySurface.COMPACT_RESTORE,
                message="Next-turn restore mutation was recorded before compact boundary mutation in the same compact report.",
                evidence={"first_restore": first_restore, "first_compact": first_compact},
            )
        )
    if first_restore is not None and first_context is not None and first_restore < first_context:
        findings.append(
            RuntimeBudgetReplayFinding(
                code="RESTORE_BEFORE_CONTEXT",
                severity=RuntimeBudgetReplaySeverity.BLOCKER,
                surface=RuntimeBudgetReplaySurface.MUTATION_SEQUENCE,
                message="Restore mutation appeared before context state existed.",
                evidence={"first_restore": first_restore, "first_context": first_context},
            )
        )
    return findings


def _edge_findings(edges: Sequence[RuntimeBudgetReplayEdge]) -> list[RuntimeBudgetReplayFinding]:
    findings: list[RuntimeBudgetReplayFinding] = []
    for edge in edges:
        if edge.ok:
            continue
        findings.append(
            RuntimeBudgetReplayFinding(
                code="BUDGET_REPLAY_EDGE_FAILED",
                severity=RuntimeBudgetReplaySeverity.BLOCKER,
                surface=RuntimeBudgetReplaySurface.MUTATION_SEQUENCE,
                message=f"Runtime budget replay edge {edge.edge_id} failed.",
                evidence=edge.to_dict(),
            )
        )
    return findings


def _first_sequence(nodes: Sequence[RuntimeBudgetReplayNode], kind: RuntimeBudgetReplayNodeKind) -> int | None:
    for node in nodes:
        if node.kind == kind:
            return node.sequence
    return None


def _compact_policy_budget_match(
    compact_policy: CompactRestorePolicyReport | None,
    *,
    compact_count: int,
    restore_count: int,
) -> bool:
    if compact_policy is None:
        return False
    if not compact_policy.ok:
        return False
    if compact_policy.compact_needed and compact_count <= 0:
        return False
    if compact_policy.restore_contract_present and restore_count <= 0:
        return False
    return True


def _phase_counts(event_records: Sequence[EventRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in event_records:
        payload = record.payload if isinstance(record.payload, Mapping) else {}
        query_session = payload.get("query_session")
        if not isinstance(query_session, Mapping):
            continue
        phase = str(query_session.get("phase") or "")
        if not phase:
            continue
        counts[phase] = counts.get(phase, 0) + 1
    return counts


def _kind_counts(mutations: Sequence[RuntimeBudgetMutation]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for mutation in mutations:
        kind = str(mutation.kind)
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _surface_for_rule(kind: RuntimeBudgetReplayRuleKind) -> RuntimeBudgetReplaySurface:
    if kind in {
        RuntimeBudgetReplayRuleKind.SNAPSHOT_PRESENT,
        RuntimeBudgetReplayRuleKind.MUTATION_IDS_UNIQUE,
        RuntimeBudgetReplayRuleKind.INITIAL_CONTEXT_PRESENT,
        RuntimeBudgetReplayRuleKind.MODEL_USAGE_RECORDED,
        RuntimeBudgetReplayRuleKind.TOOL_CONTEXT_RECORDED,
    }:
        return RuntimeBudgetReplaySurface.MUTATION_SEQUENCE
    if kind in {
        RuntimeBudgetReplayRuleKind.COMPACT_MUTATION_RECORDED,
        RuntimeBudgetReplayRuleKind.RESTORE_MUTATION_RECORDED,
        RuntimeBudgetReplayRuleKind.COMPACT_POLICY_MATCHES_BUDGET,
    }:
        return RuntimeBudgetReplaySurface.COMPACT_RESTORE
    if kind == RuntimeBudgetReplayRuleKind.RETRY_BUDGET_MATCHES_PLAYBOOK:
        return RuntimeBudgetReplaySurface.API_RETRY
    if kind == RuntimeBudgetReplayRuleKind.EVENT_FLOW_HAS_BUDGET_UPDATES:
        return RuntimeBudgetReplaySurface.EVENT_FLOW
    if kind == RuntimeBudgetReplayRuleKind.SOURCE_DECISIONS_CLEAN:
        return RuntimeBudgetReplaySurface.SOURCE_DECISION
    return RuntimeBudgetReplaySurface.SNAPSHOT


def _source_decision_ok(item: Mapping[str, str]) -> bool:
    target = str(item.get("target_path") or "").replace("\\", "/").lower()
    decision = str(item.get("decision") or "")
    if not target.startswith("packages/") and not target.startswith("apps/"):
        return False
    if any(part in target for part in ("vendor", "vendor-runtimes", "source-pool", "runtime-sources")):
        return False
    return decision in {"zyra_module_migrated", "adapter_encapsulated"}
