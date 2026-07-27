from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from .canonical import (
    digest,
    finite_number,
    identity,
    invalid,
    mapping,
    metric_name,
    new_identity,
    require_digest,
    sequence,
    utc_now,
)
from .models import (
    BenchmarkCell,
    DomainKind,
    MetricDefinition,
    MetricDirection,
    RawSample,
    SampleStatus,
)


Extractor = Callable[[Mapping[str, Any]], float | None]


class MetricCatalog:
    def __init__(self, definitions: Iterable[MetricDefinition] | None = None) -> None:
        selected = tuple(definitions or default_metric_definitions())
        if not selected:
            raise invalid(
                "benchmark_metric_catalog_empty",
                "Benchmark metric catalog cannot be empty.",
                phase="metrics",
            )
        self._definitions: dict[str, MetricDefinition] = {}
        for item in selected:
            metric_id = metric_name(item.metric_id)
            if metric_id in self._definitions:
                raise invalid(
                    "benchmark_metric_duplicate",
                    "Benchmark metric catalog contains a duplicate metric.",
                    phase="metrics",
                    detail={"metric_id": metric_id},
                )
            if item.minimum is not None and item.maximum is not None:
                if item.minimum > item.maximum:
                    raise invalid(
                        "benchmark_metric_bounds_invalid",
                        "Metric minimum exceeds its maximum.",
                        phase="metrics",
                        detail={"metric_id": metric_id},
                    )
            self._definitions[metric_id] = item

    def get(self, metric_id: str) -> MetricDefinition:
        selected = metric_name(metric_id)
        try:
            return self._definitions[selected]
        except KeyError as error:
            raise invalid(
                "benchmark_metric_not_found",
                "Metric does not exist in the benchmark catalog.",
                phase="metrics",
                detail={"metric_id": selected},
            ) from error

    def list(self) -> tuple[MetricDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))

    def required_ids(self) -> tuple[str, ...]:
        return tuple(item.metric_id for item in self.list() if item.required)


class MetricExtractor:
    def __init__(self, catalog: MetricCatalog | None = None) -> None:
        self.catalog = catalog or MetricCatalog()
        self._extractors = extractor_registry()
        missing = sorted(set(self.catalog.required_ids()) - set(self._extractors))
        if missing:
            raise invalid(
                "benchmark_metric_extractor_missing",
                "Required metrics do not have extraction behavior.",
                phase="metrics",
                detail={"metric_ids": missing},
            )

    def extract(
        self,
        *,
        campaign_id: str,
        cell: BenchmarkCell,
        run_id: str,
        receipt: Mapping[str, Any],
        evidence_digest: str,
    ) -> tuple[RawSample, ...]:
        selected_campaign_id = identity(campaign_id, "campaign id")
        selected_run_id = identity(run_id, "run id")
        selected_evidence = require_digest(evidence_digest, "evidence digest")
        samples: list[RawSample] = []
        for definition in self.catalog.list():
            extractor = self._extractors.get(definition.metric_id)
            if extractor is None:
                if definition.required:
                    raise invalid(
                        "benchmark_required_metric_unbound",
                        "Required metric has no extraction behavior.",
                        phase="metrics",
                        detail={"metric_id": definition.metric_id},
                    )
                samples.append(
                    self._sample(
                        definition=definition,
                        campaign_id=selected_campaign_id,
                        cell=cell,
                        run_id=selected_run_id,
                        value=None,
                        status=SampleStatus.MISSING,
                        evidence_digest=selected_evidence,
                        reason="optional metric extractor not configured",
                    )
                )
                continue
            try:
                value = extractor(receipt)
            except (KeyError, TypeError, ValueError, IndexError) as error:
                if definition.required:
                    raise invalid(
                        "benchmark_required_metric_extraction_failed",
                        "Required metric could not be extracted.",
                        phase="metrics",
                        detail={
                            "metric_id": definition.metric_id,
                            "error_type": type(error).__name__,
                        },
                    ) from error
                value = None
            if value is None:
                if definition.required:
                    raise invalid(
                        "benchmark_required_metric_missing",
                        "Required metric is missing from a formal run.",
                        phase="metrics",
                        detail={"metric_id": definition.metric_id},
                    )
                status = SampleStatus.MISSING
                reason = "metric not observed"
            else:
                value = finite_number(
                    value,
                    definition.metric_id,
                    minimum=definition.minimum,
                    maximum=definition.maximum,
                )
                status = SampleStatus.OBSERVED
                reason = ""
            samples.append(
                self._sample(
                    definition=definition,
                    campaign_id=selected_campaign_id,
                    cell=cell,
                    run_id=selected_run_id,
                    value=value,
                    status=status,
                    evidence_digest=selected_evidence,
                    reason=reason,
                )
            )
        verify_sample_block(samples, self.catalog, cell=cell)
        return tuple(samples)

    def _sample(
        self,
        *,
        definition: MetricDefinition,
        campaign_id: str,
        cell: BenchmarkCell,
        run_id: str,
        value: float | None,
        status: SampleStatus,
        evidence_digest: str,
        reason: str,
    ) -> RawSample:
        dimensions = {
            "domain": cell.domain.value,
            "variant": cell.variant_id,
            "repetition": str(cell.repetition),
            "input_revision": cell.input_revision,
        }
        return RawSample(
            sample_id=new_identity("sample"),
            campaign_id=campaign_id,
            cell_id=cell.cell_id,
            run_id=run_id,
            domain=cell.domain,
            variant_id=cell.variant_id,
            repetition=cell.repetition,
            seed=cell.seed,
            metric_id=definition.metric_id,
            value=value,
            unit=definition.unit,
            status=status,
            observed_at=utc_now(),
            evidence_digest=evidence_digest,
            dimensions=dimensions,
            reason=reason,
        )


def default_metric_definitions() -> tuple[MetricDefinition, ...]:
    return (
        metric("task.success", "ratio", MetricDirection.HIGHER_IS_BETTER, 0, 1),
        metric("quality.constraint_satisfaction", "ratio", MetricDirection.HIGHER_IS_BETTER, 0, 1),
        metric("quality.deterministic_score", "ratio", MetricDirection.HIGHER_IS_BETTER, 0, 1),
        metric("quality.artifact_drift", "ratio", MetricDirection.LOWER_IS_BETTER, 0, 1),
        metric("quality.requirement_drift", "ratio", MetricDirection.LOWER_IS_BETTER, 0, 1),
        metric("quality.coherence", "ratio", MetricDirection.HIGHER_IS_BETTER, 0, 1),
        metric("quality.verifier_disagreement", "ratio", MetricDirection.LOWER_IS_BETTER, 0, 1),
        metric("memory.hit_rate", "ratio", MetricDirection.HIGHER_IS_BETTER, 0, 1),
        metric("memory.restore_success", "ratio", MetricDirection.HIGHER_IS_BETTER, 0, 1),
        metric("memory.compact_ratio", "ratio", MetricDirection.INFORMATIONAL, 0, 1),
        metric("memory.context_loss", "ratio", MetricDirection.LOWER_IS_BETTER, 0, 1),
        metric("communication.entropy", "bits", MetricDirection.LOWER_IS_BETTER, 0, None),
        metric("communication.useful_ratio", "ratio", MetricDirection.HIGHER_IS_BETTER, 0, 1),
        metric("communication.duplicate_ratio", "ratio", MetricDirection.LOWER_IS_BETTER, 0, 1),
        metric("topology.sparsity", "ratio", MetricDirection.HIGHER_IS_BETTER, 0, 1),
        metric("topology.churn", "mutations", MetricDirection.INFORMATIONAL, 0, None),
        metric("topology.active_role_diversity", "roles", MetricDirection.HIGHER_IS_BETTER, 1, None),
        metric("efficiency.token_units", "tokens", MetricDirection.LOWER_IS_BETTER, 0, None),
        metric("efficiency.wall_time_ms", "milliseconds", MetricDirection.LOWER_IS_BETTER, 1, None),
        metric("efficiency.throughput", "steps_per_second", MetricDirection.HIGHER_IS_BETTER, 0, None),
        metric("efficiency.cost_usd", "usd", MetricDirection.LOWER_IS_BETTER, 0, None),
        metric("resource.cpu_utilization", "ratio", MetricDirection.INFORMATIONAL, 0, 1),
        metric("resource.memory_utilization", "ratio", MetricDirection.INFORMATIONAL, 0, 1),
        metric("resource.sla_compliance", "ratio", MetricDirection.HIGHER_IS_BETTER, 0, 1),
        metric("resource.privacy_compliance", "ratio", MetricDirection.HIGHER_IS_BETTER, 0, 1),
        metric("resource.tier_diversity", "tiers", MetricDirection.HIGHER_IS_BETTER, 1, 3),
        metric("provider.model_diversity", "models", MetricDirection.HIGHER_IS_BETTER, 1, None),
        metric("provider.mix_entropy", "bits", MetricDirection.INFORMATIONAL, 0, None),
        metric("recovery.fault_success_rate", "ratio", MetricDirection.HIGHER_IS_BETTER, 0, 1),
        metric("recovery.mttr_ms", "milliseconds", MetricDirection.LOWER_IS_BETTER, 0, None),
        metric("recovery.detection_ms", "milliseconds", MetricDirection.LOWER_IS_BETTER, 0, None),
        metric("recovery.delivery_after_fault", "ratio", MetricDirection.HIGHER_IS_BETTER, 0, 1),
        metric("autonomy.human_interventions", "count", MetricDirection.LOWER_IS_BETTER, 0, 0),
        metric("autonomy.operator_interventions", "count", MetricDirection.LOWER_IS_BETTER, 0, 0),
        metric("steps.effective", "transitions", MetricDirection.HIGHER_IS_BETTER, 1, None),
        metric("steps.raw", "events", MetricDirection.INFORMATIONAL, 1, None),
        metric("steps.excluded_ratio", "ratio", MetricDirection.INFORMATIONAL, 0, 1),
    )


def metric(
    metric_id: str,
    unit: str,
    direction: MetricDirection,
    minimum: float | None,
    maximum: float | None,
) -> MetricDefinition:
    return MetricDefinition(
        metric_id=metric_id,
        title=metric_id.replace(".", " ").replace("_", " ").title(),
        unit=unit,
        direction=direction,
        required=True,
        minimum=minimum,
        maximum=maximum,
        dimensions=("domain", "variant", "repetition"),
    )


def extractor_registry() -> dict[str, Extractor]:
    return {
        "task.success": lambda r: boolean_ratio(path(r, "task", "succeeded")),
        "quality.constraint_satisfaction": lambda r: number(r, "quality", "constraint_satisfaction"),
        "quality.deterministic_score": lambda r: number(r, "quality", "deterministic_score"),
        "quality.artifact_drift": lambda r: number(r, "quality", "artifact_drift"),
        "quality.requirement_drift": lambda r: number(r, "quality", "requirement_drift"),
        "quality.coherence": lambda r: number(r, "quality", "coherence"),
        "quality.verifier_disagreement": lambda r: boolean_ratio(path(r, "quality", "verifier_disagreement")),
        "memory.hit_rate": lambda r: number(r, "memory", "hit_rate"),
        "memory.restore_success": lambda r: number(r, "memory", "restore_success"),
        "memory.compact_ratio": lambda r: number(r, "memory", "compact_ratio"),
        "memory.context_loss": lambda r: number(r, "memory", "context_loss"),
        "communication.entropy": lambda r: number(r, "communication", "entropy"),
        "communication.useful_ratio": lambda r: number(r, "communication", "useful_ratio"),
        "communication.duplicate_ratio": lambda r: number(r, "communication", "duplicate_ratio"),
        "topology.sparsity": lambda r: number(r, "topology", "sparsity"),
        "topology.churn": lambda r: number(r, "topology", "churn"),
        "topology.active_role_diversity": lambda r: number(r, "topology", "active_role_diversity"),
        "efficiency.token_units": lambda r: number(r, "efficiency", "token_units"),
        "efficiency.wall_time_ms": lambda r: number(r, "efficiency", "wall_time_ms"),
        "efficiency.throughput": lambda r: number(r, "efficiency", "throughput"),
        "efficiency.cost_usd": lambda r: number(r, "efficiency", "cost_usd"),
        "resource.cpu_utilization": lambda r: number(r, "resource", "cpu_utilization"),
        "resource.memory_utilization": lambda r: number(r, "resource", "memory_utilization"),
        "resource.sla_compliance": lambda r: number(r, "resource", "sla_compliance"),
        "resource.privacy_compliance": lambda r: number(r, "resource", "privacy_compliance"),
        "resource.tier_diversity": lambda r: number(r, "resource", "tier_diversity"),
        "provider.model_diversity": lambda r: number(r, "provider", "model_diversity"),
        "provider.mix_entropy": lambda r: number(r, "provider", "mix_entropy"),
        "recovery.fault_success_rate": lambda r: number(r, "recovery", "fault_success_rate"),
        "recovery.mttr_ms": lambda r: number(r, "recovery", "mttr_ms"),
        "recovery.detection_ms": lambda r: number(r, "recovery", "detection_ms"),
        "recovery.delivery_after_fault": lambda r: boolean_ratio(path(r, "recovery", "delivery_after_fault")),
        "autonomy.human_interventions": lambda r: number(r, "autonomy", "human_interventions"),
        "autonomy.operator_interventions": lambda r: number(r, "autonomy", "operator_interventions"),
        "steps.effective": lambda r: number(r, "steps", "effective"),
        "steps.raw": lambda r: number(r, "steps", "raw"),
        "steps.excluded_ratio": lambda r: number(r, "steps", "excluded_ratio"),
    }


def path(value: Mapping[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        current = mapping(current, ".".join(keys))
        current = current[key]
    return current


def number(value: Mapping[str, Any], *keys: str) -> float:
    return float(path(value, *keys))


def boolean_ratio(value: Any) -> float:
    if value is True:
        return 1.0
    if value is False:
        return 0.0
    raise ValueError("expected boolean")


def verify_sample_block(
    samples: Sequence[RawSample],
    catalog: MetricCatalog,
    *,
    cell: BenchmarkCell,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    by_metric: dict[str, list[RawSample]] = defaultdict(list)
    identifiers: set[str] = set()
    for sample in samples:
        by_metric[sample.metric_id].append(sample)
        if sample.sample_id in identifiers:
            findings.append(
                {"code": "sample-id-duplicate", "sample_id": sample.sample_id}
            )
        identifiers.add(sample.sample_id)
        if sample.cell_id != cell.cell_id:
            findings.append(
                {"code": "sample-cell-mismatch", "sample_id": sample.sample_id}
            )
        if sample.domain is not cell.domain:
            findings.append(
                {"code": "sample-domain-mismatch", "sample_id": sample.sample_id}
            )
        if sample.variant_id != cell.variant_id:
            findings.append(
                {"code": "sample-variant-mismatch", "sample_id": sample.sample_id}
            )
        if sample.repetition != cell.repetition or sample.seed != cell.seed:
            findings.append(
                {"code": "sample-repetition-mismatch", "sample_id": sample.sample_id}
            )
        definition = catalog.get(sample.metric_id)
        if sample.unit != definition.unit:
            findings.append(
                {
                    "code": "sample-unit-mismatch",
                    "sample_id": sample.sample_id,
                    "expected": definition.unit,
                    "observed": sample.unit,
                }
            )
        if definition.required and sample.status is not SampleStatus.OBSERVED:
            findings.append(
                {
                    "code": "required-sample-not-observed",
                    "sample_id": sample.sample_id,
                }
            )
    required = set(catalog.required_ids())
    missing = sorted(required - set(by_metric))
    if missing:
        findings.append({"code": "required-metric-block-missing", "metrics": missing})
    duplicated = sorted(key for key, values in by_metric.items() if len(values) > 1)
    if duplicated:
        findings.append({"code": "metric-sampled-twice", "metrics": duplicated})
    if findings:
        raise invalid(
            "benchmark_metric_sample_block_invalid",
            "Metric sample block is incomplete or inconsistent.",
            phase="metrics",
            detail={"findings": findings},
        )
    output = {
        "schema": "zyra.live-benchmark-sample-block-verification/v1",
        "valid": True,
        "cell_id": cell.cell_id,
        "sample_count": len(samples),
        "observed_count": sum(
            item.status is SampleStatus.OBSERVED for item in samples
        ),
        "metric_ids": sorted(by_metric),
        "sample_digest": __import__("hashlib").sha256(
            "|".join(item.sample_digest for item in samples).encode("utf-8")
        ).hexdigest(),
        "verified_at": utc_now(),
    }
    return output


def verify_campaign_metric_completeness(
    samples: Iterable[RawSample],
    *,
    expected_cell_ids: Sequence[str],
    catalog: MetricCatalog,
) -> dict[str, Any]:
    selected = tuple(samples)
    expected_cells = set(expected_cell_ids)
    expected_metrics = set(catalog.required_ids())
    observed: dict[str, set[str]] = defaultdict(set)
    counts: Counter[tuple[str, str]] = Counter()
    findings: list[dict[str, Any]] = []
    for sample in selected:
        observed[sample.cell_id].add(sample.metric_id)
        counts[(sample.cell_id, sample.metric_id)] += 1
    for cell_id in sorted(expected_cells):
        missing = sorted(expected_metrics - observed.get(cell_id, set()))
        if missing:
            findings.append(
                {
                    "code": "cell-metric-block-missing",
                    "cell_id": cell_id,
                    "metric_ids": missing,
                }
            )
    unexpected_cells = sorted(set(observed) - expected_cells)
    if unexpected_cells:
        findings.append(
            {"code": "unexpected-metric-cells", "cell_ids": unexpected_cells}
        )
    duplicates = sorted(
        f"{cell_id}:{metric_id}"
        for (cell_id, metric_id), count in counts.items()
        if count > 1
    )
    if duplicates:
        findings.append({"code": "metric-sample-duplicate", "pairs": duplicates})
    if findings:
        raise invalid(
            "benchmark_campaign_metrics_incomplete",
            "Campaign raw metric matrix is incomplete.",
            phase="metrics",
            detail={"findings": findings},
        )
    output = {
        "schema": "zyra.live-benchmark-metric-completeness/v1",
        "valid": True,
        "cell_count": len(expected_cells),
        "metric_count": len(expected_metrics),
        "sample_count": len(selected),
        "matrix_width": len(expected_metrics),
        "verified_at": utc_now(),
    }
    output["receipt_digest"] = digest(output)
    return output
