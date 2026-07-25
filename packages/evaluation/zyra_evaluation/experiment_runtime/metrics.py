from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .canonical import digest, new_identity, utc_now
from .errors import invalid
from .models import (
    ComparisonDirection,
    MetricDefinition,
    RawMetricSample,
    SampleStatus,
)
from .source import SourceArchive
from .workload import WorkloadObservation


Extractor = Callable[[WorkloadObservation, SourceArchive], float | None]


@dataclass(frozen=True, slots=True)
class MetricBinding:
    definition: MetricDefinition
    extractor: Extractor
    unavailable_reason: str = ""


def default_metric_bindings(
    *,
    minimum_samples: int = 3,
) -> tuple[MetricBinding, ...]:
    required = max(1, minimum_samples)
    return (
        _binding(
            "success_rate",
            "ratio",
            ComparisonDirection.HIGHER_IS_BETTER,
            "quality",
            required,
            lambda value, _: 1.0
            if (
                value.quality_score >= 0.5
                and (
                    not value.claims.get("variant_capability_changed")
                    or value.variant_id == "no_recovery"
                    or not value.unresolved_fault_ids
                )
            )
            else 0.0,
            minimum=0,
            maximum=1,
            requirements=("REQ-CLOSE-01", "SCORE-LOOP"),
            description="Deterministic verifier success under the frozen envelope.",
        ),
        _binding(
            "quality_score",
            "ratio",
            ComparisonDirection.HIGHER_IS_BETTER,
            "quality",
            required,
            lambda value, _: value.quality_score,
            minimum=0,
            maximum=1,
            requirements=("REQ-CLOSE-01", "SCORE-LOOP", "SCORE-VALUE"),
            description="Normalized delivery quality from coverage and owner constraints.",
        ),
        _binding(
            "artifact_drift_ratio",
            "ratio",
            ComparisonDirection.LOWER_IS_BETTER,
            "quality",
            required,
            lambda value, _: value.artifact_drift_ratio,
            minimum=0,
            maximum=1,
            requirements=("REQ-CLOSE-01", "REQ-TRACE-01", "SCORE-LOOP"),
            description="Observed divergence from the source live artifact set.",
        ),
        _binding(
            "effective_step_count",
            "count",
            ComparisonDirection.DESCRIPTIVE,
            "closed_loop",
            required,
            lambda value, _: float(len(value.processed_event_ids)),
            minimum=0,
            requirements=("REQ-CLOSE-01", "SCORE-LOOP"),
            description="Owner-bound semantic events processed by the controlled run.",
        ),
        _binding(
            "valid_transition_ratio",
            "ratio",
            ComparisonDirection.HIGHER_IS_BETTER,
            "closed_loop",
            required,
            lambda value, source: len(value.processed_event_ids)
            / max(1, len(source.events)),
            minimum=0,
            maximum=1,
            requirements=("REQ-CLOSE-01", "REQ-TRACE-01", "SCORE-LOOP"),
            description="Fraction of source live transitions retained and processed.",
        ),
        _binding(
            "memory_write_count",
            "count",
            ComparisonDirection.DESCRIPTIVE,
            "memory",
            required,
            lambda value, _: float(value.memory_writes),
            minimum=0,
            requirements=("REQ-MEM-01", "SCORE-ALGO"),
            description="Experiment-local memory mutations caused by semantic events.",
        ),
        _binding(
            "memory_hit_ratio",
            "ratio",
            ComparisonDirection.HIGHER_IS_BETTER,
            "memory",
            required,
            lambda value, _: (
                value.memory_hits / value.memory_writes
                if value.memory_writes
                else 0.0
            ),
            minimum=0,
            maximum=1,
            requirements=("REQ-MEM-01", "SCORE-ALGO"),
            description="Content-addressed memory hit ratio.",
        ),
        _binding(
            "compact_operation_count",
            "count",
            ComparisonDirection.DESCRIPTIVE,
            "memory",
            required,
            lambda value, _: float(value.compact_operations),
            minimum=0,
            requirements=("REQ-MEM-01", "SCORE-ALGO"),
            description="Semantic context-window compaction operations.",
        ),
        _binding(
            "compact_ratio",
            "ratio",
            ComparisonDirection.HIGHER_IS_BETTER,
            "memory",
            required,
            lambda value, _: value.compact_operations
            / max(1, value.memory_writes),
            minimum=0,
            maximum=1,
            requirements=("REQ-MEM-01", "SCORE-ALGO"),
            description="Compactions per memory mutation.",
        ),
        _binding(
            "restore_operation_count",
            "count",
            ComparisonDirection.DESCRIPTIVE,
            "memory",
            required,
            lambda value, _: float(value.restore_operations),
            minimum=0,
            requirements=("REQ-MEM-01", "REQ-FAULT-01", "SCORE-ROBUST"),
            description="Checkpoint or compact restore operations.",
        ),
        _binding(
            "restore_success_rate",
            "ratio",
            ComparisonDirection.HIGHER_IS_BETTER,
            "memory",
            required,
            lambda value, _: (
                sum(item.recovered for item in value.recoveries)
                / max(1, len(value.recoveries))
            ),
            minimum=0,
            maximum=1,
            requirements=("REQ-MEM-01", "REQ-FAULT-01", "SCORE-ROBUST"),
            description="Recovered fault/resume ratio.",
        ),
        _binding(
            "communication_entropy",
            "nat",
            ComparisonDirection.LOWER_IS_BETTER,
            "communication",
            required,
            lambda value, _: communication_entropy(value),
            minimum=0,
            requirements=("REQ-COMM-01", "SCORE-NOISE"),
            description="Shannon entropy of sender-recipient deliveries.",
        ),
        _binding(
            "useful_communication_ratio",
            "ratio",
            ComparisonDirection.HIGHER_IS_BETTER,
            "communication",
            required,
            lambda value, _: value.useful_messages / max(1, len(value.messages)),
            minimum=0,
            maximum=1,
            requirements=("REQ-COMM-01", "SCORE-NOISE"),
            description="Messages bound to useful semantic transitions.",
        ),
        _binding(
            "communication_delivery_count",
            "count",
            ComparisonDirection.LOWER_IS_BETTER,
            "communication",
            required,
            lambda value, _: float(
                sum(len(item.recipients) for item in value.messages)
            ),
            minimum=0,
            requirements=("REQ-COMM-01", "SCORE-NOISE", "SCORE-EFF"),
            description="Total inter-worker deliveries, including broadcasts.",
        ),
        _binding(
            "broadcast_delivery_count",
            "count",
            ComparisonDirection.LOWER_IS_BETTER,
            "communication",
            required,
            lambda value, _: float(value.broadcast_deliveries),
            minimum=0,
            requirements=("REQ-COMM-01", "SCORE-NOISE"),
            description="Deliveries caused by fan-out communication.",
        ),
        _binding(
            "topology_node_count",
            "count",
            ComparisonDirection.DESCRIPTIVE,
            "topology",
            required,
            lambda value, _: float(len(value.topology_nodes)),
            minimum=0,
            requirements=("REQ-TOPO-01", "SCORE-ORG", "SCORE-EFF"),
            description="Observed runtime topology node count.",
        ),
        _binding(
            "topology_edge_count",
            "count",
            ComparisonDirection.LOWER_IS_BETTER,
            "topology",
            required,
            lambda value, _: float(len(value.topology_edges)),
            minimum=0,
            requirements=("REQ-TOPO-01", "SCORE-ORG", "SCORE-EFF"),
            description="Observed runtime topology edge count.",
        ),
        _binding(
            "topology_sparsity",
            "ratio",
            ComparisonDirection.HIGHER_IS_BETTER,
            "topology",
            required,
            lambda value, _: topology_sparsity(value),
            minimum=0,
            maximum=1,
            requirements=("REQ-TOPO-01", "SCORE-ORG"),
            description="One minus observed edge density.",
        ),
        _binding(
            "topology_churn",
            "count",
            ComparisonDirection.DESCRIPTIVE,
            "topology",
            required,
            lambda value, _: float(value.topology_mutations),
            minimum=0,
            requirements=("REQ-TOPO-01", "SCORE-ORG"),
            description="Runtime node/edge mutation count.",
        ),
        _binding(
            "token_units",
            "token",
            ComparisonDirection.LOWER_IS_BETTER,
            "efficiency",
            required,
            lambda value, _: value.token_units,
            minimum=0,
            requirements=("SCORE-EFF",),
            description="Deterministic content and communication token units.",
        ),
        _binding(
            "wall_time_ms",
            "millisecond",
            ComparisonDirection.LOWER_IS_BETTER,
            "efficiency",
            required,
            lambda value, _: value.wall_time_ms,
            minimum=0,
            requirements=("SCORE-EFF",),
            description="Actual controlled execution CPU wall time.",
        ),
        _binding(
            "cost_microunits",
            "microunit",
            ComparisonDirection.LOWER_IS_BETTER,
            "efficiency",
            required,
            lambda value, _: value.cost_microunits,
            minimum=0,
            requirements=("SCORE-EFF",),
            description="Policy-weighted route and delivery cost.",
        ),
        _binding(
            "placement_policy_compliance",
            "ratio",
            ComparisonDirection.HIGHER_IS_BETTER,
            "placement",
            required,
            lambda value, _: sum(item.policy_compliant for item in value.routes)
            / max(1, len(value.routes)),
            minimum=0,
            maximum=1,
            requirements=("REQ-EDGE-01", "SCORE-COMPAT"),
            description="Routes admitted by the selected scheduling policy.",
        ),
        _binding(
            "privacy_policy_compliance",
            "ratio",
            ComparisonDirection.HIGHER_IS_BETTER,
            "placement",
            required,
            lambda value, _: sum(item.privacy_compliant for item in value.routes)
            / max(1, len(value.routes)),
            minimum=0,
            maximum=1,
            requirements=("REQ-EDGE-01", "SCORE-COMPAT"),
            description="Routes respecting evidence sensitivity constraints.",
        ),
        _binding(
            "privacy_violation_count",
            "count",
            ComparisonDirection.LOWER_IS_BETTER,
            "placement",
            required,
            lambda value, _: float(
                sum(not item.privacy_compliant for item in value.routes)
            ),
            minimum=0,
            requirements=("REQ-EDGE-01", "SCORE-COMPAT"),
            description="Rejected or non-compliant sensitive placements.",
        ),
        _binding(
            "route_worker_count",
            "count",
            ComparisonDirection.DESCRIPTIVE,
            "placement",
            required,
            lambda value, _: float(len(set(item.worker_id for item in value.routes))),
            minimum=0,
            requirements=("REQ-EDGE-01", "SCORE-ORG"),
            description="Distinct workers selected by the route algorithm.",
        ),
        _binding(
            "fault_recovery_rate",
            "ratio",
            ComparisonDirection.HIGHER_IS_BETTER,
            "recovery",
            required,
            lambda value, source: sum(item.recovered for item in value.recoveries)
            / max(1, len(source.fault_events)),
            minimum=0,
            maximum=1,
            requirements=("REQ-FAULT-01", "SCORE-LOOP", "SCORE-ROBUST"),
            description="Source live faults recovered in the controlled variant.",
        ),
        _binding(
            "recovery_mttr_units",
            "transition",
            ComparisonDirection.LOWER_IS_BETTER,
            "recovery",
            required,
            lambda value, _: recovery_mttr(value),
            minimum=0,
            requirements=("REQ-FAULT-01", "SCORE-ROBUST"),
            description="Mean event distance from fault to bound recovery.",
        ),
        _binding(
            "unresolved_fault_count",
            "count",
            ComparisonDirection.LOWER_IS_BETTER,
            "recovery",
            required,
            lambda value, _: float(len(value.unresolved_fault_ids)),
            minimum=0,
            requirements=("REQ-FAULT-01", "SCORE-LOOP", "SCORE-ROBUST"),
            description="Faults left unresolved after the controlled execution.",
        ),
        _binding(
            "provider_count",
            "count",
            ComparisonDirection.DESCRIPTIVE,
            "provider",
            required,
            lambda value, _: float(len(value.provider_ids)),
            minimum=0,
            requirements=("REQ-EDGE-01", "SCORE-COMPAT"),
            description="Providers observed in source owner receipts; no new call.",
        ),
        _binding(
            "model_count",
            "count",
            ComparisonDirection.DESCRIPTIVE,
            "provider",
            required,
            lambda value, _: float(len(value.model_ids)),
            minimum=0,
            requirements=("REQ-EDGE-01", "SCORE-COMPAT"),
            description="Models observed in source owner receipts; no new call.",
        ),
        _binding(
            "human_intervention_count",
            "count",
            ComparisonDirection.LOWER_IS_BETTER,
            "autonomy",
            required,
            lambda value, _: float(value.human_intervention_count),
            minimum=0,
            maximum=0,
            requirements=("REQ-CLOSE-01", "SCORE-LOOP"),
            description="Human intervention count under sealed policy.",
        ),
    )


def _binding(
    metric: str,
    unit: str,
    direction: ComparisonDirection,
    category: str,
    minimum_samples: int,
    extractor: Extractor,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    target: float | None = None,
    requirements: tuple[str, ...] = (),
    description: str,
) -> MetricBinding:
    return MetricBinding(
        definition=MetricDefinition(
            metric=metric,
            unit=unit,
            direction=direction,
            category=category,
            required=True,
            minimum_samples=minimum_samples,
            target=target,
            bounded_minimum=minimum,
            bounded_maximum=maximum,
            description=description,
            requirement_ids=requirements,
        ),
        extractor=extractor,
    )


def topology_sparsity(value: WorkloadObservation) -> float:
    nodes = len(value.topology_nodes)
    if nodes <= 1:
        return 1.0
    possible = nodes * (nodes - 1)
    density = len(value.topology_edges) / possible
    return max(0.0, min(1.0, 1.0 - density))


def communication_entropy(value: WorkloadObservation) -> float:
    counts: Counter[tuple[str, str]] = Counter()
    for message in value.messages:
        for recipient in message.recipients:
            counts[(message.sender, recipient)] += 1
    total = sum(counts.values())
    if not total:
        return 0.0
    result = 0.0
    for count in counts.values():
        probability = count / total
        result -= probability * math.log(probability)
    return result


def recovery_mttr(value: WorkloadObservation) -> float:
    observed = tuple(
        item.elapsed_units for item in value.recoveries if item.recovered
    )
    if not observed:
        return 0.0
    return sum(observed) / len(observed)


class MetricCatalog:
    def __init__(
        self,
        bindings: Iterable[MetricBinding] | None = None,
        *,
        minimum_samples: int = 3,
    ) -> None:
        selected = tuple(
            bindings or default_metric_bindings(minimum_samples=minimum_samples)
        )
        self._bindings: dict[str, MetricBinding] = {}
        for binding in selected:
            name = binding.definition.metric
            if name in self._bindings:
                raise invalid(
                    "experiment_metric_duplicate",
                    "Metric catalog contains a duplicate metric.",
                    detail={"metric": name},
                )
            self._bindings[name] = binding
        self.require_complete()

    def list(self) -> tuple[MetricDefinition, ...]:
        return tuple(item.definition for item in self._bindings.values())

    def binding(self, metric: str) -> MetricBinding:
        try:
            return self._bindings[metric]
        except KeyError as error:
            raise invalid(
                "experiment_metric_unknown",
                "Metric is not registered.",
                detail={"metric": metric},
            ) from error

    def require_complete(self) -> dict[str, Any]:
        categories = {
            "quality",
            "closed_loop",
            "memory",
            "communication",
            "topology",
            "efficiency",
            "placement",
            "recovery",
            "provider",
            "autonomy",
        }
        actual = {item.definition.category for item in self._bindings.values()}
        missing = sorted(categories - actual)
        required_names = {
            "success_rate",
            "quality_score",
            "artifact_drift_ratio",
            "memory_hit_ratio",
            "compact_ratio",
            "communication_entropy",
            "useful_communication_ratio",
            "topology_sparsity",
            "topology_churn",
            "token_units",
            "wall_time_ms",
            "cost_microunits",
            "placement_policy_compliance",
            "privacy_violation_count",
            "recovery_mttr_units",
            "fault_recovery_rate",
            "provider_count",
            "model_count",
            "human_intervention_count",
        }
        missing_names = sorted(required_names - set(self._bindings))
        if missing or missing_names:
            raise invalid(
                "experiment_metric_catalog_incomplete",
                "Metric catalog does not cover the formal experiment.",
                detail={
                    "missing_categories": missing,
                    "missing_metrics": missing_names,
                },
            )
        receipt = {
            "schema": "zyra.experiment-metric-catalog-verification/v1",
            "valid": True,
            "metric_count": len(self._bindings),
            "categories": sorted(actual),
            "catalog_digest": digest(
                [item.to_dict() for item in self.list()]
            ),
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt


class MetricExtractor:
    def __init__(self, catalog: MetricCatalog) -> None:
        self.catalog = catalog

    def extract(
        self,
        observation: WorkloadObservation,
        source: SourceArchive,
        *,
        experiment_id: str,
        cell_id: str,
    ) -> tuple[RawMetricSample, ...]:
        samples: list[RawMetricSample] = []
        for sequence, binding in enumerate(self.catalog._bindings.values(), start=1):
            definition = binding.definition
            try:
                value = binding.extractor(observation, source)
                status = (
                    SampleStatus.OBSERVED
                    if value is not None
                    else SampleStatus.UNAVAILABLE
                )
                unavailable_reason = (
                    ""
                    if value is not None
                    else binding.unavailable_reason
                    or "metric is not present in the verified source observation"
                )
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
                value = None
                status = SampleStatus.UNAVAILABLE
                unavailable_reason = f"extractor rejected source: {error}"
            sample = RawMetricSample(
                sample_id=new_identity("sample"),
                experiment_id=experiment_id,
                cell_id=cell_id,
                variant_id=observation.variant_id,
                repetition=observation.repetition,
                metric=definition.metric,
                value=None if value is None else float(value),
                unit=definition.unit,
                status=status,
                observed_at=observation.completed_at,
                source_kind="controlled_live_evidence_workload",
                source_ids=(
                    observation.observation_id,
                    source.archive_id,
                    source.scenario_run_id,
                    source.owner_run_id,
                ),
                source_digest=digest(
                    {
                        "observation": observation.observation_digest,
                        "source_archive": source.archive_digest,
                        "metric_definition": definition.to_dict(),
                    }
                ),
                dimensions={
                    "experiment_id": experiment_id,
                    "cell_id": cell_id,
                    "variant_id": observation.variant_id,
                    "repetition": str(observation.repetition),
                    "seed": str(observation.seed),
                    "scenario_run_id": source.scenario_run_id,
                    "owner_run_id": source.owner_run_id,
                    "task_id": source.task_id,
                    "domain": source.domain,
                    "category": definition.category,
                },
                unavailable_reason=unavailable_reason,
                sequence=sequence,
            )
            samples.append(sample)
        self.verify(samples, observation=observation)
        return tuple(samples)

    def verify(
        self,
        samples: Iterable[RawMetricSample],
        *,
        observation: WorkloadObservation,
    ) -> dict[str, Any]:
        selected = tuple(samples)
        findings: list[dict[str, Any]] = []
        names = [item.metric for item in selected]
        duplicate = sorted(
            name for name, count in Counter(names).items() if count > 1
        )
        if duplicate:
            findings.append({"code": "metric_duplicate", "metrics": duplicate})
        missing = sorted(set(self.catalog._bindings) - set(names))
        if missing:
            findings.append({"code": "metric_missing", "metrics": missing})
        for item in selected:
            definition = self.catalog.binding(item.metric).definition
            if item.unit != definition.unit:
                findings.append(
                    {
                        "code": "metric_unit_mismatch",
                        "sample_id": item.sample_id,
                    }
                )
            if item.variant_id != observation.variant_id:
                findings.append(
                    {
                        "code": "variant_binding_mismatch",
                        "sample_id": item.sample_id,
                    }
                )
            if item.repetition != observation.repetition:
                findings.append(
                    {
                        "code": "repetition_binding_mismatch",
                        "sample_id": item.sample_id,
                    }
                )
            if item.status is SampleStatus.OBSERVED and item.value is None:
                findings.append(
                    {
                        "code": "observed_value_missing",
                        "sample_id": item.sample_id,
                    }
                )
            if item.status is SampleStatus.UNAVAILABLE and not item.unavailable_reason:
                findings.append(
                    {
                        "code": "unavailable_reason_missing",
                        "sample_id": item.sample_id,
                    }
                )
            if item.value is not None:
                if not math.isfinite(item.value):
                    findings.append(
                        {
                            "code": "non_finite_value",
                            "sample_id": item.sample_id,
                        }
                    )
                if (
                    definition.bounded_minimum is not None
                    and item.value < definition.bounded_minimum
                ):
                    findings.append(
                        {
                            "code": "below_bound",
                            "sample_id": item.sample_id,
                            "value": item.value,
                        }
                    )
                if (
                    definition.bounded_maximum is not None
                    and item.value > definition.bounded_maximum
                ):
                    findings.append(
                        {
                            "code": "above_bound",
                            "sample_id": item.sample_id,
                            "value": item.value,
                        }
                    )
        receipt = {
            "schema": "zyra.experiment-metric-extraction-verification/v1",
            "valid": not findings,
            "observation_id": observation.observation_id,
            "observation_digest": observation.observation_digest,
            "sample_count": len(selected),
            "sample_digest": digest([item.to_dict() for item in selected]),
            "findings": findings,
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        if findings:
            raise invalid(
                "experiment_metric_extraction_invalid",
                "Extracted raw metrics failed verification.",
                phase="metrics",
                detail=receipt,
            )
        return receipt


def group_samples(
    samples: Iterable[RawMetricSample],
) -> dict[tuple[str, str], tuple[RawMetricSample, ...]]:
    grouped: dict[tuple[str, str], list[RawMetricSample]] = defaultdict(list)
    for item in samples:
        grouped[(item.metric, item.variant_id)].append(item)
    return {
        key: tuple(
            sorted(
                values,
                key=lambda item: (
                    item.repetition,
                    item.sequence,
                    item.sample_id,
                ),
            )
        )
        for key, values in grouped.items()
    }
