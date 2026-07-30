from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from .contracts import canonical_digest


PHASE2_METRIC_SPEC_SCHEMA = "zyra.phase2-metric-spec/v1"
PHASE2_METRIC_REGISTRY_SCHEMA = "zyra.phase2-metric-registry/v1"


class MetricDirection(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"
    TARGET_IS_BETTER = "target_is_better"
    INFORMATIONAL = "informational"


class EmptySampleSemantics(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    DEGRADED = "degraded"
    FAILED = "failed"


class FailedTaskSemantics(StrEnum):
    INCLUDE = "include"
    EXCLUDE_FROM_OPTIMIZATION = "exclude_from_optimization"
    COUNT_AS_FAILURE = "count_as_failure"


@dataclass(frozen=True, slots=True)
class MetricSpec:
    metric_id: str
    title: str
    category: str
    numerator: str
    denominator: str
    unit: str
    direction: MetricDirection
    aggregation_unit: str
    minimum_sample_size: int
    requirement_ids: tuple[str, ...]
    evidence_contracts: tuple[str, ...]
    empty_semantics: EmptySampleSemantics
    missing_semantics: EmptySampleSemantics
    failed_task_semantics: FailedTaskSemantics
    description: str
    schema_version: str = PHASE2_METRIC_SPEC_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "metric_id": self.metric_id,
            "title": self.title,
            "category": self.category,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "unit": self.unit,
            "direction": self.direction.value,
            "aggregation_unit": self.aggregation_unit,
            "minimum_sample_size": self.minimum_sample_size,
            "requirement_ids": list(self.requirement_ids),
            "evidence_contracts": list(self.evidence_contracts),
            "empty_semantics": self.empty_semantics.value,
            "missing_semantics": self.missing_semantics.value,
            "failed_task_semantics": self.failed_task_semantics.value,
            "description": self.description,
        }


def _spec(
    metric_id: str,
    title: str,
    category: str,
    numerator: str,
    denominator: str,
    unit: str,
    direction: MetricDirection,
    requirement_ids: tuple[str, ...],
    evidence_contracts: tuple[str, ...],
    *,
    aggregation_unit: str = "run",
    minimum_sample_size: int = 1,
    empty_semantics: EmptySampleSemantics = EmptySampleSemantics.DEGRADED,
    missing_semantics: EmptySampleSemantics = EmptySampleSemantics.FAILED,
    failed_task_semantics: FailedTaskSemantics = FailedTaskSemantics.INCLUDE,
    description: str = "",
) -> MetricSpec:
    return MetricSpec(
        metric_id=metric_id,
        title=title,
        category=category,
        numerator=numerator,
        denominator=denominator,
        unit=unit,
        direction=direction,
        aggregation_unit=aggregation_unit,
        minimum_sample_size=minimum_sample_size,
        requirement_ids=requirement_ids,
        evidence_contracts=evidence_contracts,
        empty_semantics=empty_semantics,
        missing_semantics=missing_semantics,
        failed_task_semantics=failed_task_semantics,
        description=description or title,
    )


_COMM = ("REQ-COMM-01", "SCORE-NOISE")
_TOPO = ("REQ-TOPO-01", "SCORE-ORG")
_TRACE = ("REQ-TRACE-01",)
_MEM = ("REQ-MEM-01", "SCORE-ALGO")
_EDGE = ("REQ-EDGE-01", "SCORE-COMPAT")
_ROBUST = ("REQ-FAULT-01", "SCORE-ROBUST")

_OBS = ("zyra.agentprune-outcome-observation/v1",)
_POLICY = (
    "zyra.topology-proposal-artifact/v1",
    "zyra.policy-decision-receipt/v1",
    "zyra.policy-outcome/v1",
)
_READINESS = ("zyra.mechanism-evidence-readiness-report/v1",)
_CONTINUITY = ("zyra.memory-continuity-receipt/v1",)
_SYMBOLIC = ("zyra.neuro-symbolic-evidence-bundle/v1",)
_EXIT = ("zyra.early-exit-decision-receipt/v1",)
_DEPTH = ("zyra.adaptive-depth-cost-receipt/v1",)
_DISPATCH = ("zyra.physical-dispatch-receipt/v2",)


_SPECS = (
    _spec("communication.normalized_entropy", "Normalized communication entropy", "communication", "entropy over delivered source-target pairs", "log2(maximum directed pairs for participating nodes)", "ratio", MetricDirection.LOWER_IS_BETTER, _COMM, _OBS, description="Graph-size-normalized communication entropy; a small graph is not automatically low entropy."),
    _spec("communication.sender_conditioned_recipient_entropy", "Sender-conditioned recipient entropy", "communication", "weighted recipient entropy conditioned on sender", "weighted log2(maximum recipients per sender)", "ratio", MetricDirection.LOWER_IS_BETTER, _COMM, _OBS),
    _spec("communication.spatial_delivery_count", "Spatial deliveries", "communication", "delivered spatial messages", "1", "messages", MetricDirection.INFORMATIONAL, _COMM, _OBS),
    _spec("communication.temporal_delivery_count", "Temporal deliveries", "communication", "delivered temporal messages", "1", "messages", MetricDirection.INFORMATIONAL, _COMM, _OBS),
    _spec("communication.delivered_bytes", "Delivered bytes", "communication", "actual bytes on delivered messages", "1", "bytes", MetricDirection.LOWER_IS_BETTER, _COMM, _OBS, failed_task_semantics=FailedTaskSemantics.EXCLUDE_FROM_OPTIMIZATION),
    _spec("communication.delivered_tokens", "Delivered tokens", "communication", "actual prompt plus completion tokens", "1", "tokens", MetricDirection.LOWER_IS_BETTER, _COMM, _OBS, failed_task_semantics=FailedTaskSemantics.EXCLUDE_FROM_OPTIMIZATION),
    _spec("communication.delivered_cost_usd", "Delivered cost", "communication", "actual communication cost", "1", "USD", MetricDirection.LOWER_IS_BETTER, _COMM, _OBS, failed_task_semantics=FailedTaskSemantics.EXCLUDE_FROM_OPTIMIZATION),
    _spec("communication.duplicate_semantic_payload_ratio", "Duplicate semantic payload ratio", "communication", "delivered messages whose digest repeats or carries a redundancy reference", "delivered messages", "ratio", MetricDirection.LOWER_IS_BETTER, _COMM, _OBS),
    _spec("communication.evidence_utilization_ratio", "Evidence utilization ratio", "communication", "unique evidence references consumed downstream", "unique evidence references carried", "ratio", MetricDirection.HIGHER_IS_BETTER, _COMM, _OBS),
    _spec("communication.useful_message_ratio", "Useful message ratio", "communication", "delivered messages with consumed evidence or verified artifact", "delivered messages", "ratio", MetricDirection.HIGHER_IS_BETTER, _COMM, _OBS),
    _spec("communication.cost_per_effective_transition", "Communication cost per effective transition", "communication", "actual communication cost", "effective canonical transitions", "USD/transition", MetricDirection.LOWER_IS_BETTER, _COMM + _TRACE, _OBS, failed_task_semantics=FailedTaskSemantics.EXCLUDE_FROM_OPTIMIZATION),
    _spec("topology.adaptation_latency_ms", "Topology adaptation latency", "topology", "sum of causal trigger-to-commit latency", "committed adaptations", "milliseconds", MetricDirection.LOWER_IS_BETTER, _TOPO + _ROBUST, _POLICY),
    _spec("topology.normalized_churn", "Normalized topology churn", "topology", "canonical node/edge/role/capability mutations", "participating graph entities times decision windows", "ratio", MetricDirection.LOWER_IS_BETTER, _TOPO, _POLICY),
    _spec("topology.oscillation_count", "Topology oscillation count", "topology", "unexplained inverse mutations to the same entity", "1", "mutations", MetricDirection.LOWER_IS_BETTER, _TOPO + _ROBUST, _POLICY),
    _spec("topology.proposal_accept_ratio", "Proposal accept ratio", "topology", "accepted decisions", "all proposal decisions", "ratio", MetricDirection.INFORMATIONAL, _TOPO, _POLICY),
    _spec("topology.proposal_reject_ratio", "Proposal reject ratio", "topology", "rejected decisions", "all proposal decisions", "ratio", MetricDirection.INFORMATIONAL, _TOPO, _POLICY),
    _spec("topology.proposal_project_ratio", "Proposal project ratio", "topology", "projected or rebased decisions", "all proposal decisions", "ratio", MetricDirection.INFORMATIONAL, _TOPO, _POLICY),
    _spec("topology.proposal_degraded_ratio", "Proposal degraded ratio", "topology", "conflict, diagnostic, or fallback decisions", "all proposal decisions", "ratio", MetricDirection.LOWER_IS_BETTER, _TOPO + _ROBUST, _POLICY),
    _spec("topology.mechanism_decision_overhead_ms", "Mechanism decision overhead", "topology", "sum of canonical mechanism decision latency", "measured decisions", "milliseconds", MetricDirection.LOWER_IS_BETTER, _TOPO, _POLICY),
    _spec("readiness.stage_score", "Readiness stage score", "readiness", "ordinal stage of the latest signed report", "activation_ready ordinal", "ratio", MetricDirection.HIGHER_IS_BETTER, _TRACE, _READINESS),
    _spec("readiness.status_score", "Readiness status score", "readiness", "ordinal status of the latest signed report", "deterministic_ready ordinal", "ratio", MetricDirection.HIGHER_IS_BETTER, _TRACE, _READINESS),
    _spec("readiness.required_input_coverage", "Required input coverage", "readiness", "covered required fields", "required fields", "ratio", MetricDirection.HIGHER_IS_BETTER, _TRACE, _READINESS),
    _spec("readiness.optional_input_coverage", "Optional input coverage", "readiness", "covered optional fields", "optional fields", "ratio", MetricDirection.HIGHER_IS_BETTER, _TRACE, _READINESS, empty_semantics=EmptySampleSemantics.NOT_APPLICABLE),
    _spec("readiness.freshness", "Input freshness", "readiness", "fresh required input observations", "required input observations", "ratio", MetricDirection.HIGHER_IS_BETTER, _TRACE, _READINESS),
    _spec("readiness.confidence", "Input confidence", "readiness", "confidence-weighted required input observations", "required input observations", "ratio", MetricDirection.HIGHER_IS_BETTER, _TRACE, _READINESS),
    _spec("readiness.missingness", "Input missingness", "readiness", "missing required input observations", "required input observations", "ratio", MetricDirection.LOWER_IS_BETTER, _TRACE, _READINESS),
    _spec("readiness.scenario_coverage", "Scenario coverage", "readiness", "covered required scenarios", "required scenarios", "ratio", MetricDirection.HIGHER_IS_BETTER, _TRACE, _READINESS),
    _spec("readiness.failure_path_coverage", "Failure-path coverage", "readiness", "covered required failure paths", "required failure paths", "ratio", MetricDirection.HIGHER_IS_BETTER, _ROBUST, _READINESS),
    _spec("readiness.causal_link_completeness", "Readiness causal-link completeness", "readiness", "present required causal links", "required causal links", "ratio", MetricDirection.HIGHER_IS_BETTER, _TRACE, _READINESS),
    _spec("readiness.deterministic_replay_match", "Deterministic replay match", "readiness", "matching deterministic replays", "evaluated replays", "ratio", MetricDirection.HIGHER_IS_BETTER, _TRACE, _READINESS),
    _spec("readiness.actual_default_mode_ratio", "Actual default mode ratio", "readiness", "mechanisms resolved to default mode", "mechanism mode resolutions", "ratio", MetricDirection.HIGHER_IS_BETTER, _TRACE, _READINESS),
    _spec("readiness.diagnostic_mode_ratio", "Diagnostic mode ratio", "readiness", "mechanisms resolved to diagnostic mode", "mechanism mode resolutions", "ratio", MetricDirection.LOWER_IS_BETTER, _TRACE, _READINESS),
    _spec("readiness.baseline_mode_ratio", "Baseline mode ratio", "readiness", "mechanisms resolved to baseline mode", "mechanism mode resolutions", "ratio", MetricDirection.LOWER_IS_BETTER, _TRACE, _READINESS),
    _spec("readiness.no_policy_audit_pass", "No-policy-update audit pass", "readiness", "reports with a passing prohibited-update audit", "readiness reports", "ratio", MetricDirection.HIGHER_IS_BETTER, _TRACE, _READINESS),
    _spec("continuity.critical_fact_recall", "Critical-fact recall", "continuity", "required critical facts recalled and used", "required critical facts", "ratio", MetricDirection.HIGHER_IS_BETTER, _MEM, _CONTINUITY),
    _spec("continuity.unresolved_obligation_retention", "Unresolved-obligation retention", "continuity", "unresolved obligations retained", "unresolved obligations before restore/handoff", "ratio", MetricDirection.HIGHER_IS_BETTER, _MEM, _CONTINUITY),
    _spec("continuity.provenance_coverage", "Provenance coverage", "continuity", "recalled facts with stable provenance", "recalled critical facts", "ratio", MetricDirection.HIGHER_IS_BETTER, _MEM + _TRACE, _CONTINUITY),
    _spec("continuity.stale_requirement_execution_count", "Stale requirement execution", "continuity", "stale-revision executions", "1", "executions", MetricDirection.LOWER_IS_BETTER, _MEM + _ROBUST, _CONTINUITY),
    _spec("continuity.duplicate_completed_work_count", "Duplicate completed work after handoff", "continuity", "duplicate completed obligations", "1", "obligations", MetricDirection.LOWER_IS_BETTER, _MEM + _ROBUST, _CONTINUITY),
    _spec("continuity.first_decision_correctness", "First-decision correctness after restore", "continuity", "correct downstream first decisions", "restore/handoff receipts", "ratio", MetricDirection.HIGHER_IS_BETTER, _MEM, _CONTINUITY),
    _spec("symbolic.adversarial_reject_ratio", "Adversarial reject ratio", "symbolic", "adversarial proposals rejected without commit", "adversarial proposals", "ratio", MetricDirection.INFORMATIONAL, _TOPO + _TRACE, _SYMBOLIC),
    _spec("symbolic.adversarial_project_ratio", "Adversarial project ratio", "symbolic", "adversarial proposals projected safely", "adversarial proposals", "ratio", MetricDirection.INFORMATIONAL, _TOPO + _TRACE, _SYMBOLIC),
    _spec("symbolic.adversarial_accept_ratio", "Adversarial accept ratio", "symbolic", "adversarial proposals accepted unchanged", "adversarial proposals", "ratio", MetricDirection.INFORMATIONAL, _TOPO + _TRACE, _SYMBOLIC),
    _spec("symbolic.unsafe_commit_count", "Unsafe commit count", "symbolic", "commits made after failed symbolic constraints", "1", "commits", MetricDirection.LOWER_IS_BETTER, _TOPO + _TRACE, _SYMBOLIC),
    _spec("symbolic.projector_bypass_reachable", "Projector bypass reachability", "symbolic", "production bundles missing projection/decision chain", "production bundles", "ratio", MetricDirection.LOWER_IS_BETTER, _TOPO + _TRACE, _SYMBOLIC),
    _spec("operator.early_exit_true_positive_ratio", "Early-exit true-positive ratio", "operator", "true exits", "all executed early exits with posterior result", "ratio", MetricDirection.HIGHER_IS_BETTER, _TRACE, _EXIT),
    _spec("operator.early_exit_false_positive_ratio", "Early-exit false-positive ratio", "operator", "false exits", "all executed early exits with posterior result", "ratio", MetricDirection.LOWER_IS_BETTER, _ROBUST, _EXIT),
    _spec("operator.executed_breadth", "Executed operator breadth", "operator", "executed operators", "executed depth", "operators/layer", MetricDirection.LOWER_IS_BETTER, _TRACE, _DEPTH),
    _spec("operator.executed_depth", "Executed operator depth", "operator", "executed layers", "adaptive-depth receipts", "layers", MetricDirection.LOWER_IS_BETTER, _TRACE, _DEPTH),
    _spec("dispatch.local_real_receipt_completeness", "Local real-receipt completeness", "dispatch", "complete real local dispatch receipts", "real local dispatch attempts", "ratio", MetricDirection.HIGHER_IS_BETTER, _EDGE, _DISPATCH),
    _spec("dispatch.edge_real_receipt_completeness", "Edge real-receipt completeness", "dispatch", "complete real edge dispatch receipts", "real edge dispatch attempts", "ratio", MetricDirection.HIGHER_IS_BETTER, _EDGE, _DISPATCH),
    _spec("dispatch.cloud_real_receipt_completeness", "Cloud real-receipt completeness", "dispatch", "complete real cloud dispatch receipts", "real cloud dispatch attempts", "ratio", MetricDirection.HIGHER_IS_BETTER, _EDGE, _DISPATCH),
    _spec("dispatch.physical_reroute_count", "Physical reroute count", "dispatch", "location changes across causally ordered attempts", "1", "reroutes", MetricDirection.INFORMATIONAL, _EDGE + _ROBUST, _DISPATCH),
    _spec("dispatch.privacy_placement_violation_count", "Privacy placement violations", "dispatch", "placements outside allowed locations or privacy policy", "1", "violations", MetricDirection.LOWER_IS_BETTER, _EDGE, _DISPATCH),
    _spec("dispatch.provider_receipt_coverage", "Provider receipt coverage", "dispatch", "cloud receipts with provider/model wire evidence", "real cloud receipts", "ratio", MetricDirection.HIGHER_IS_BETTER, _EDGE + _TRACE, _DISPATCH),
    _spec("dispatch.causal_chain_completeness", "Dispatch causal-chain completeness", "dispatch", "present decision, lease, attempt, call, artifact and verification links", "six required links per real receipt", "ratio", MetricDirection.HIGHER_IS_BETTER, _EDGE + _TRACE, _DISPATCH),
    _spec("evidence.canonical_transition_count", "Canonical evidence transitions", "evidence", "effective canonical transitions", "1", "transitions", MetricDirection.INFORMATIONAL, _TRACE, _POLICY, description="Evidence volume only; it is never interpreted as an independent sample count."),
)


PHASE2_METRIC_SPECS: Mapping[str, MetricSpec] = MappingProxyType(
    {item.metric_id: item for item in _SPECS}
)


FIRST_STAGE_METRIC_COMPATIBILITY: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "communication.normalized_entropy": ("communication_entropy", "communication.entropy"),
        "communication.useful_message_ratio": ("useful_communication_ratio", "communication.useful_ratio"),
        "communication.duplicate_semantic_payload_ratio": ("duplicate_fact_ratio", "communication.duplicate_ratio"),
        "communication.delivered_tokens": ("message_tokens", "communication.token_total"),
        "communication.cost_per_effective_transition": ("cost_per_transition",),
        "topology.normalized_churn": ("topology_churn", "topology.churn"),
        "topology.adaptation_latency_ms": ("adaptation_latency_ms",),
        "continuity.critical_fact_recall": ("memory_recall", "memory.critical_fact_recall"),
        "dispatch.causal_chain_completeness": ("dispatch_receipt_completeness",),
    }
)


def metric_spec_registry_payload() -> dict[str, Any]:
    specs = [PHASE2_METRIC_SPECS[key].to_dict() for key in sorted(PHASE2_METRIC_SPECS)]
    compatibility = {
        key: list(value)
        for key, value in sorted(FIRST_STAGE_METRIC_COMPATIBILITY.items())
    }
    body = {
        "schema_version": PHASE2_METRIC_REGISTRY_SCHEMA,
        "registry_id": "phase2_strongest_v1.metrics",
        "specs": specs,
        "first_stage_compatibility": compatibility,
        "history_rewrite": False,
    }
    return {**body, "digest": canonical_digest(body)}


def requirement_metric_map() -> dict[str, tuple[str, ...]]:
    result: dict[str, list[str]] = {}
    for metric_id, spec in PHASE2_METRIC_SPECS.items():
        for requirement_id in spec.requirement_ids:
            result.setdefault(requirement_id, []).append(metric_id)
    return {
        requirement_id: tuple(sorted(metric_ids))
        for requirement_id, metric_ids in sorted(result.items())
    }
