from __future__ import annotations

import math
import random
from collections import Counter
from collections.abc import Iterable, Sequence
from statistics import fmean
from typing import Any

from .canonical import digest, finite_number, stable_unique, utc_now
from .errors import invalid
from .models import (
    ComparisonDirection,
    ConfidenceInterval,
    DistributionSummary,
    MetricDefinition,
    RawMetricSample,
    SampleStatus,
    VariantComparison,
)


def quantile(
    values: Sequence[float],
    probability: float,
    *,
    method: str = "linear",
) -> float:
    if not values:
        raise invalid(
            "experiment_quantile_empty",
            "Quantile requires at least one value.",
        )
    p = finite_number(probability, "quantile probability", minimum=0, maximum=1)
    ordered = sorted(finite_number(item, "quantile value") for item in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * p
    lower = math.floor(position)
    upper = math.ceil(position)
    if method == "lower":
        return ordered[lower]
    if method == "higher":
        return ordered[upper]
    if method == "nearest":
        return ordered[int(round(position))]
    if method == "midpoint":
        return (ordered[lower] + ordered[upper]) / 2
    if method != "linear":
        raise invalid(
            "experiment_quantile_method_invalid",
            "Unsupported quantile interpolation method.",
            detail={"method": method},
        )
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


class OnlineMoments:
    def __init__(self) -> None:
        self.count = 0
        self.mean = 0.0
        self.m2 = 0.0
        self.minimum: float | None = None
        self.maximum: float | None = None

    def update(self, value: float) -> None:
        selected = finite_number(value, "moment value")
        self.count += 1
        delta = selected - self.mean
        self.mean += delta / self.count
        delta2 = selected - self.mean
        self.m2 += delta * delta2
        self.minimum = (
            selected if self.minimum is None else min(self.minimum, selected)
        )
        self.maximum = (
            selected if self.maximum is None else max(self.maximum, selected)
        )

    def extend(self, values: Iterable[float]) -> None:
        for value in values:
            self.update(value)

    @property
    def population_variance(self) -> float | None:
        return self.m2 / self.count if self.count else None

    @property
    def sample_variance(self) -> float | None:
        return self.m2 / (self.count - 1) if self.count > 1 else None

    @property
    def standard_deviation(self) -> float | None:
        variance = self.sample_variance
        return math.sqrt(variance) if variance is not None else None

    def merge(self, other: "OnlineMoments") -> "OnlineMoments":
        if other.count == 0:
            return self
        if self.count == 0:
            self.count = other.count
            self.mean = other.mean
            self.m2 = other.m2
            self.minimum = other.minimum
            self.maximum = other.maximum
            return self
        combined = self.count + other.count
        delta = other.mean - self.mean
        self.m2 = (
            self.m2
            + other.m2
            + delta * delta * self.count * other.count / combined
        )
        self.mean = (
            self.mean * self.count + other.mean * other.count
        ) / combined
        self.count = combined
        self.minimum = min(
            value
            for value in (self.minimum, other.minimum)
            if value is not None
        )
        self.maximum = max(
            value
            for value in (self.maximum, other.maximum)
            if value is not None
        )
        return self


def median_absolute_deviation(values: Sequence[float]) -> float:
    if not values:
        raise invalid(
            "experiment_mad_empty",
            "Median absolute deviation requires values.",
        )
    median = quantile(values, 0.5)
    return quantile([abs(item - median) for item in values], 0.5)


def interquartile_range(values: Sequence[float]) -> float:
    if not values:
        raise invalid(
            "experiment_iqr_empty",
            "Interquartile range requires values.",
        )
    return quantile(values, 0.75) - quantile(values, 0.25)


def bootstrap_mean_interval(
    values: Sequence[float],
    *,
    confidence_level: float = 0.95,
    resamples: int = 2000,
    seed: int = 0,
) -> ConfidenceInterval:
    selected = tuple(finite_number(item, "bootstrap value") for item in values)
    level = finite_number(
        confidence_level,
        "confidence level",
        minimum=0.5,
        maximum=0.999,
    )
    if not selected:
        return ConfidenceInterval(
            method="bootstrap_percentile_mean",
            level=level,
            lower=None,
            upper=None,
            resamples=0,
            seed=seed,
            reason="no observed samples",
        )
    if len(selected) == 1:
        return ConfidenceInterval(
            method="degenerate_single_sample",
            level=level,
            lower=selected[0],
            upper=selected[0],
            resamples=0,
            seed=seed,
            reason="one observed sample",
        )
    count = max(100, min(100_000, int(resamples)))
    rng = random.Random(seed)
    size = len(selected)
    means: list[float] = []
    for _ in range(count):
        means.append(fmean(selected[rng.randrange(size)] for _ in range(size)))
    alpha = (1 - level) / 2
    return ConfidenceInterval(
        method="bootstrap_percentile_mean",
        level=level,
        lower=quantile(means, alpha),
        upper=quantile(means, 1 - alpha),
        resamples=count,
        seed=seed,
    )


def tukey_anomaly_bounds(
    values: Sequence[float],
    *,
    multiplier: float = 1.5,
) -> tuple[float, float]:
    if not values:
        raise invalid(
            "experiment_anomaly_values_empty",
            "Anomaly bounds require values.",
        )
    factor = finite_number(multiplier, "Tukey multiplier", minimum=0)
    q1 = quantile(values, 0.25)
    q3 = quantile(values, 0.75)
    width = q3 - q1
    return q1 - factor * width, q3 + factor * width


def classify_anomalies(
    samples: Iterable[RawMetricSample],
    *,
    multiplier: float = 1.5,
) -> tuple[RawMetricSample, ...]:
    from dataclasses import replace

    selected = tuple(samples)
    values = tuple(
        item.value
        for item in selected
        if item.status is SampleStatus.OBSERVED and item.value is not None
    )
    if len(values) < 4:
        return selected
    lower, upper = tukey_anomaly_bounds(values, multiplier=multiplier)
    output: list[RawMetricSample] = []
    for item in selected:
        if (
            item.status is SampleStatus.OBSERVED
            and item.value is not None
            and (item.value < lower or item.value > upper)
        ):
            output.append(
                replace(
                    item,
                    status=SampleStatus.ANOMALOUS,
                    anomaly_reason=(
                        f"value {item.value:g} is outside Tukey bounds "
                        f"[{lower:g}, {upper:g}]"
                    ),
                )
            )
        else:
            output.append(item)
    return tuple(output)


class DistributionAggregator:
    def __init__(
        self,
        *,
        confidence_level: float = 0.95,
        bootstrap_resamples: int = 2000,
        include_anomalies: bool = True,
    ) -> None:
        self.confidence_level = confidence_level
        self.bootstrap_resamples = bootstrap_resamples
        self.include_anomalies = include_anomalies

    def summarize(
        self,
        definition: MetricDefinition,
        samples: Iterable[RawMetricSample],
        *,
        variant_id: str,
        seed: int,
    ) -> DistributionSummary:
        selected = tuple(
            item
            for item in samples
            if item.metric == definition.metric and item.variant_id == variant_id
        )
        wrong_unit = sorted(
            item.sample_id for item in selected if item.unit != definition.unit
        )
        if wrong_unit:
            raise invalid(
                "experiment_metric_unit_mismatch",
                "Metric samples use inconsistent units.",
                detail={
                    "metric": definition.metric,
                    "expected_unit": definition.unit,
                    "sample_ids": wrong_unit,
                },
            )
        observed_statuses = {SampleStatus.OBSERVED}
        if self.include_anomalies:
            observed_statuses.add(SampleStatus.ANOMALOUS)
        values = tuple(
            item.value
            for item in selected
            if item.status in observed_statuses and item.value is not None
        )
        moments = OnlineMoments()
        moments.extend(values)
        statuses = Counter(item.status for item in selected)
        anomaly_reasons = stable_unique(
            item.anomaly_reason
            for item in selected
            if item.status is SampleStatus.ANOMALOUS and item.anomaly_reason
        )
        confidence = bootstrap_mean_interval(
            values,
            confidence_level=self.confidence_level,
            resamples=self.bootstrap_resamples,
            seed=seed,
        )
        summary = DistributionSummary(
            metric=definition.metric,
            unit=definition.unit,
            variant_id=variant_id,
            total_sample_count=len(selected),
            observed_sample_count=sum(
                1 for item in selected if item.status in observed_statuses
            ),
            unavailable_sample_count=statuses[SampleStatus.UNAVAILABLE],
            anomalous_sample_count=statuses[SampleStatus.ANOMALOUS],
            rejected_sample_count=statuses[SampleStatus.REJECTED],
            minimum=moments.minimum,
            maximum=moments.maximum,
            mean=moments.mean if moments.count else None,
            p50=quantile(values, 0.5) if values else None,
            p95=quantile(values, 0.95) if values else None,
            population_variance=moments.population_variance,
            sample_variance=moments.sample_variance,
            standard_deviation=moments.standard_deviation,
            median_absolute_deviation=(
                median_absolute_deviation(values) if values else None
            ),
            interquartile_range=interquartile_range(values) if values else None,
            coefficient_of_variation=(
                None
                if not moments.count or moments.mean == 0
                else (moments.standard_deviation or 0.0) / abs(moments.mean)
            ),
            confidence=confidence,
            anomaly_reasons=anomaly_reasons,
            source_sample_ids=tuple(item.sample_id for item in selected),
            computed_at=utc_now(),
        )
        if (
            definition.required
            and summary.observed_sample_count < definition.minimum_samples
        ):
            raise invalid(
                "experiment_metric_sample_count_insufficient",
                "Required metric does not have enough observed samples.",
                phase="aggregation",
                detail={
                    "metric": definition.metric,
                    "variant_id": variant_id,
                    "observed": summary.observed_sample_count,
                    "minimum": definition.minimum_samples,
                },
            )
        return summary


def compare_summaries(
    definition: MetricDefinition,
    baseline: DistributionSummary,
    compared: DistributionSummary,
) -> VariantComparison:
    if baseline.metric != definition.metric or compared.metric != definition.metric:
        raise invalid(
            "experiment_comparison_metric_mismatch",
            "Summary metric does not match comparison definition.",
        )
    if baseline.unit != definition.unit or compared.unit != definition.unit:
        raise invalid(
            "experiment_comparison_unit_mismatch",
            "Summary unit does not match comparison definition.",
        )
    left = baseline.p50
    right = compared.p50
    if left is None or right is None:
        return VariantComparison(
            metric=definition.metric,
            unit=definition.unit,
            baseline_variant_id=baseline.variant_id,
            compared_variant_id=compared.variant_id,
            baseline_p50=left,
            compared_p50=right,
            absolute_delta=None,
            relative_delta=None,
            effect_direction="unavailable",
            better=None,
            comparable=False,
            reason="one or both variants have no observed P50",
            baseline_summary_digest=baseline.summary_digest,
            compared_summary_digest=compared.summary_digest,
        )
    delta = right - left
    relative = None if left == 0 else delta / abs(left)
    direction = definition.direction
    if direction is ComparisonDirection.HIGHER_IS_BETTER:
        better: bool | None = right > left
        effect = "improved" if delta > 0 else "degraded" if delta < 0 else "equal"
    elif direction is ComparisonDirection.LOWER_IS_BETTER:
        better = right < left
        effect = "improved" if delta < 0 else "degraded" if delta > 0 else "equal"
    elif direction is ComparisonDirection.TARGET_IS_BETTER:
        if definition.target is None:
            raise invalid(
                "experiment_metric_target_missing",
                "Target-oriented metric has no target.",
                detail={"metric": definition.metric},
            )
        left_distance = abs(left - definition.target)
        right_distance = abs(right - definition.target)
        better = right_distance < left_distance
        effect = (
            "improved"
            if right_distance < left_distance
            else "degraded"
            if right_distance > left_distance
            else "equal"
        )
    else:
        better = None
        effect = "descriptive"
    return VariantComparison(
        metric=definition.metric,
        unit=definition.unit,
        baseline_variant_id=baseline.variant_id,
        compared_variant_id=compared.variant_id,
        baseline_p50=left,
        compared_p50=right,
        absolute_delta=delta,
        relative_delta=relative,
        effect_direction=effect,
        better=better,
        comparable=True,
        reason="P50 comparison under the same immutable envelope",
        baseline_summary_digest=baseline.summary_digest,
        compared_summary_digest=compared.summary_digest,
    )


def verify_summary(
    summary: DistributionSummary,
    definition: MetricDefinition,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    if summary.total_sample_count != len(summary.source_sample_ids):
        findings.append({"code": "source_sample_count_mismatch"})
    if summary.observed_sample_count >= definition.minimum_samples:
        for name in (
            "minimum",
            "maximum",
            "mean",
            "p50",
            "p95",
            "population_variance",
            "median_absolute_deviation",
            "interquartile_range",
        ):
            if getattr(summary, name) is None:
                findings.append({"code": "statistic_missing", "field": name})
    if summary.minimum is not None and summary.maximum is not None:
        if summary.minimum > summary.maximum:
            findings.append({"code": "minimum_above_maximum"})
        if summary.p50 is not None and not (
            summary.minimum <= summary.p50 <= summary.maximum
        ):
            findings.append({"code": "p50_out_of_bounds"})
        if summary.p95 is not None and not (
            summary.minimum <= summary.p95 <= summary.maximum
        ):
            findings.append({"code": "p95_out_of_bounds"})
        if (
            summary.p50 is not None
            and summary.p95 is not None
            and summary.p50 > summary.p95
        ):
            findings.append({"code": "p50_above_p95"})
    if definition.bounded_minimum is not None and summary.minimum is not None:
        if summary.minimum < definition.bounded_minimum:
            findings.append(
                {
                    "code": "value_below_metric_bound",
                    "minimum": summary.minimum,
                    "bound": definition.bounded_minimum,
                }
            )
    if definition.bounded_maximum is not None and summary.maximum is not None:
        if summary.maximum > definition.bounded_maximum:
            findings.append(
                {
                    "code": "value_above_metric_bound",
                    "maximum": summary.maximum,
                    "bound": definition.bounded_maximum,
                }
            )
    receipt = {
        "schema": "zyra.experiment-summary-verification/v1",
        "valid": not findings,
        "metric": summary.metric,
        "variant_id": summary.variant_id,
        "summary_digest": summary.summary_digest,
        "findings": findings,
        "verified_at": utc_now(),
    }
    receipt["receipt_digest"] = digest(receipt)
    if findings:
        raise invalid(
            "experiment_summary_invalid",
            "Metric distribution summary failed verification.",
            phase="aggregation",
            detail=receipt,
        )
    return receipt


def aggregation_digest(
    summaries: Iterable[DistributionSummary],
    comparisons: Iterable[VariantComparison],
) -> str:
    return digest(
        {
            "summaries": [
                item.to_dict()
                for item in sorted(
                    summaries,
                    key=lambda value: (value.metric, value.variant_id),
                )
            ],
            "comparisons": [
                item.to_dict()
                for item in sorted(
                    comparisons,
                    key=lambda value: (
                        value.metric,
                        value.baseline_variant_id,
                        value.compared_variant_id,
                    ),
                )
            ],
        }
    )
