from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import canonical_digest
from .metric_specs import (
    PHASE2_METRIC_SPECS,
    metric_spec_registry_payload,
    requirement_metric_map,
)
from .metrics import (
    MetricStatus,
    MetricValue,
    Phase2MetricEngine,
    RunMetricInput,
    RunMetricResult,
)


PHASE2_METRIC_REPORT_SCHEMA = "zyra.phase2-metric-report/v1"
PHASE2_METRIC_GROUP_SCHEMA = "zyra.phase2-metric-group/v1"
STRONGEST_PREFLIGHT_REPORT_SCHEMA = "zyra.strongest-preflight-report/v1"


@dataclass(frozen=True, slots=True)
class MetricGroupReport:
    dimension: str
    group_id: str
    run_count: int
    successful_run_count: int
    failed_run_count: int
    metrics: Mapping[str, MetricValue]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PHASE2_METRIC_GROUP_SCHEMA,
            "dimension": self.dimension,
            "group_id": self.group_id,
            "run_count": self.run_count,
            "successful_run_count": self.successful_run_count,
            "failed_run_count": self.failed_run_count,
            "metrics": {
                key: self.metrics[key].to_dict() for key in sorted(self.metrics)
            },
        }


@dataclass(frozen=True, slots=True)
class Phase2MetricReport:
    runs: tuple[RunMetricResult, ...]
    scenarios: tuple[MetricGroupReport, ...]
    mechanisms: tuple[MetricGroupReport, ...]
    aggregate: MetricGroupReport
    registry_digest: str
    requirement_metrics: Mapping[str, tuple[str, ...]]
    schema_version: str = PHASE2_METRIC_REPORT_SCHEMA

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        body = {
            "schema_version": self.schema_version,
            "registry_digest": self.registry_digest,
            "run_reports": [item.to_dict() for item in self.runs],
            "scenario_reports": [item.to_dict() for item in self.scenarios],
            "mechanism_reports": [item.to_dict() for item in self.mechanisms],
            "aggregate_report": self.aggregate.to_dict(),
            "requirement_metrics": {
                key: list(self.requirement_metrics[key])
                for key in sorted(self.requirement_metrics)
            },
            "anti_gaming": {
                "failed_runs_separate": True,
                "simulated_dispatch_excluded_from_real_numerator": True,
                "canonical_transition_count_is_evidence_volume_only": True,
                "receipt_resolver_disconnect_fails_closed": True,
                "idempotent_reimport_deduplicated": True,
            },
        }
        return {**body, "digest": self.digest} if include_digest else body


def _worst_status(values: Sequence[MetricValue]) -> MetricStatus:
    present = {item.status for item in values}
    for status in (
        MetricStatus.FAILED,
        MetricStatus.DEGRADED,
        MetricStatus.NOT_APPLICABLE,
        MetricStatus.OBSERVED,
    ):
        if status in present:
            return status
    return MetricStatus.DEGRADED


def _aggregate_metric(
    metric_id: str,
    values: Sequence[MetricValue],
) -> MetricValue:
    observed = [
        item for item in values if item.status is MetricStatus.OBSERVED
    ]
    numerator = sum(item.numerator for item in observed)
    denominator = sum(item.denominator for item in observed)
    if observed and denominator > 0:
        value = numerator / denominator
        status = MetricStatus.OBSERVED
    else:
        value = None
        status = _worst_status(values)
    return MetricValue(
        metric_id=metric_id,
        value=value,
        numerator=numerator,
        denominator=denominator,
        sample_count=sum(item.sample_count for item in values),
        failed_sample_count=sum(item.failed_sample_count for item in values),
        status=status,
        eligible_for_optimization=(
            bool(observed)
            and all(item.eligible_for_optimization for item in observed)
        ),
        reasons=tuple(
            sorted({reason for item in values for reason in item.reasons})
        ),
        source_refs=tuple(
            sorted({ref for item in values for ref in item.source_refs})
        ),
    )


def _group(
    dimension: str,
    group_id: str,
    runs: Sequence[RunMetricResult],
) -> MetricGroupReport:
    metrics = {
        metric_id: _aggregate_metric(
            metric_id,
            [run.metrics[metric_id] for run in runs],
        )
        for metric_id in sorted(PHASE2_METRIC_SPECS)
    }
    return MetricGroupReport(
        dimension=dimension,
        group_id=group_id,
        run_count=len(runs),
        successful_run_count=sum(item.task_succeeded for item in runs),
        failed_run_count=sum(not item.task_succeeded for item in runs),
        metrics=metrics,
    )


class Phase2MetricReportBuilder:
    def __init__(self, engine: Phase2MetricEngine | None = None) -> None:
        self.engine = engine or Phase2MetricEngine()

    def build(
        self,
        values: Iterable[RunMetricInput],
    ) -> Phase2MetricReport:
        runs = tuple(
            sorted(
                (self.engine.evaluate_run(item) for item in values),
                key=lambda item: (item.scenario_id, item.run_id, item.task_id),
            )
        )
        if not runs:
            raise ValueError("a Phase 2 metric report requires at least one run")
        scenarios: dict[str, list[RunMetricResult]] = defaultdict(list)
        mechanisms: dict[str, list[RunMetricResult]] = defaultdict(list)
        for run in runs:
            scenarios[run.scenario_id].append(run)
            mechanisms[run.mechanism_profile].append(run)
        registry = metric_spec_registry_payload()
        return Phase2MetricReport(
            runs=runs,
            scenarios=tuple(
                _group("scenario", key, tuple(scenarios[key]))
                for key in sorted(scenarios)
            ),
            mechanisms=tuple(
                _group("mechanism", key, tuple(mechanisms[key]))
                for key in sorted(mechanisms)
            ),
            aggregate=_group("aggregate", "all", runs),
            registry_digest=str(registry["digest"]),
            requirement_metrics=requirement_metric_map(),
        )


@dataclass(frozen=True, slots=True)
class StrongestPreflightReport:
    preflight_id: str
    execution_mode: str
    status: str
    profile_family: str
    profile_version: str
    p2_eval_base_commit: str
    implementation_commit: str
    manifest_digest: str
    policy_registry_digest: str
    activation_gate_digest: str
    hard_gate_order: tuple[str, ...]
    hard_gates: Mapping[str, bool]
    metrics: Mapping[str, Any]
    raw_receipt_refs: tuple[str, ...]
    failed_receipt_refs: tuple[str, ...]
    outliers: tuple[Mapping[str, Any], ...]
    failure_retention: Mapping[str, Any]
    readiness_statuses: Mapping[str, str]
    resolver_before: str
    resolver_after: str
    replay_semantics: str
    training_sample_count: int = 0
    transition_count_semantics: str = "evidence_volume_only"
    schema: str = STRONGEST_PREFLIGHT_REPORT_SCHEMA

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": self.schema,
            "preflight_id": self.preflight_id,
            "execution_mode": self.execution_mode,
            "status": self.status,
            "profile_family": self.profile_family,
            "profile_version": self.profile_version,
            "p2_eval_base_commit": self.p2_eval_base_commit,
            "implementation_commit": self.implementation_commit,
            "manifest_digest": self.manifest_digest,
            "policy_registry_digest": self.policy_registry_digest,
            "activation_gate_digest": self.activation_gate_digest,
            "hard_gate_order": list(self.hard_gate_order),
            "hard_gates": {
                key: bool(self.hard_gates[key])
                for key in self.hard_gate_order
            },
            "metrics": dict(self.metrics),
            "raw_receipt_refs": list(self.raw_receipt_refs),
            "failed_receipt_refs": list(self.failed_receipt_refs),
            "outliers": [dict(item) for item in self.outliers],
            "failure_retention": dict(self.failure_retention),
            "readiness_statuses": dict(sorted(self.readiness_statuses.items())),
            "resolver_before": self.resolver_before,
            "resolver_after": self.resolver_after,
            "replay_semantics": self.replay_semantics,
            "training_sample_count": self.training_sample_count,
            "transition_count_semantics": self.transition_count_semantics,
            "anti_gaming": {
                "success_and_safety_precede_efficiency": True,
                "failed_and_degraded_receipts_retained": True,
                "replay_not_counted_as_live_improvement": True,
                "diagnostic_not_counted_as_execution": True,
                "transition_count_not_training_or_confidence": True,
                "combination_or_weight_search_performed": False,
            },
        }
        if include_digest:
            value["report_digest"] = self.digest
        return value


__all__ = [
    "MetricGroupReport",
    "PHASE2_METRIC_GROUP_SCHEMA",
    "PHASE2_METRIC_REPORT_SCHEMA",
    "STRONGEST_PREFLIGHT_REPORT_SCHEMA",
    "Phase2MetricReport",
    "Phase2MetricReportBuilder",
    "StrongestPreflightReport",
]
