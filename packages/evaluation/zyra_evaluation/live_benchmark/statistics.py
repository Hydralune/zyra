from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Iterable, Sequence
from statistics import fmean
from typing import Any

from .canonical import digest, invalid, utc_now
from .metrics import MetricCatalog
from .models import (
    Distribution,
    MetricDirection,
    PairedComparison,
    RawSample,
    SampleStatus,
    Verdict,
)


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        raise invalid(
            "benchmark_statistics_empty",
            "Cannot compute a percentile for an empty series.",
            phase="statistics",
        )
    if fraction < 0 or fraction > 1:
        raise invalid(
            "benchmark_percentile_fraction_invalid",
            "Percentile fraction must be within [0, 1].",
            phase="statistics",
        )
    ordered = sorted(float(item) for item in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def standard_deviation(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = fmean(values)
    return math.sqrt(sum((item - mean) ** 2 for item in values) / (len(values) - 1))


def median_absolute_deviation(values: Sequence[float]) -> float:
    median = percentile(values, 0.5)
    return percentile([abs(item - median) for item in values], 0.5)


def interquartile_range(values: Sequence[float]) -> float:
    return percentile(values, 0.75) - percentile(values, 0.25)


def bootstrap_mean_interval(
    values: Sequence[float],
    *,
    confidence_level: float = 0.95,
    iterations: int = 2_000,
    seed: int = 0,
) -> tuple[float, float]:
    if not values:
        raise invalid(
            "benchmark_bootstrap_empty",
            "Cannot bootstrap an empty series.",
            phase="statistics",
        )
    if confidence_level <= 0 or confidence_level >= 1:
        raise invalid(
            "benchmark_confidence_level_invalid",
            "Confidence level must be between zero and one.",
            phase="statistics",
        )
    if iterations < 100 or iterations > 100_000:
        raise invalid(
            "benchmark_bootstrap_iterations_invalid",
            "Bootstrap iteration count is outside the allowed range.",
            phase="statistics",
        )
    if len(values) == 1:
        return float(values[0]), float(values[0])
    generator = random.Random(seed)
    count = len(values)
    estimates = [
        fmean(values[generator.randrange(count)] for _ in range(count))
        for _ in range(iterations)
    ]
    alpha = (1 - confidence_level) / 2
    return percentile(estimates, alpha), percentile(estimates, 1 - alpha)


class StatisticalEvaluator:
    def __init__(
        self,
        catalog: MetricCatalog | None = None,
        *,
        confidence_level: float = 0.95,
        bootstrap_iterations: int = 2_000,
    ) -> None:
        self.catalog = catalog or MetricCatalog()
        self.confidence_level = confidence_level
        self.bootstrap_iterations = bootstrap_iterations

    def distributions(
        self,
        samples: Iterable[RawSample],
    ) -> tuple[Distribution, ...]:
        selected = tuple(samples)
        grouped: dict[tuple[str, str, str], list[RawSample]] = defaultdict(list)
        for sample in selected:
            grouped[
                (sample.metric_id, sample.domain.value, sample.variant_id)
            ].append(sample)
        output: list[Distribution] = []
        for (metric_id, domain, variant_id), group in sorted(grouped.items()):
            definition = self.catalog.get(metric_id)
            observed = [
                float(item.value)
                for item in group
                if item.status is SampleStatus.OBSERVED and item.value is not None
            ]
            missing_count = len(group) - len(observed)
            if definition.required and missing_count:
                raise invalid(
                    "benchmark_distribution_required_samples_missing",
                    "Required metric distribution contains missing samples.",
                    phase="statistics",
                    detail={
                        "metric_id": metric_id,
                        "domain": domain,
                        "variant_id": variant_id,
                        "missing_count": missing_count,
                    },
                )
            if not observed:
                raise invalid(
                    "benchmark_distribution_empty",
                    "Metric distribution contains no observed values.",
                    phase="statistics",
                    detail={
                        "metric_id": metric_id,
                        "domain": domain,
                        "variant_id": variant_id,
                    },
                )
            units = {item.unit for item in group}
            if units != {definition.unit}:
                raise invalid(
                    "benchmark_distribution_unit_mismatch",
                    "Metric distribution mixes incompatible units.",
                    phase="statistics",
                    detail={"metric_id": metric_id, "units": sorted(units)},
                )
            low, high = bootstrap_mean_interval(
                observed,
                confidence_level=self.confidence_level,
                iterations=self.bootstrap_iterations,
                seed=seed_for(metric_id, domain, variant_id),
            )
            output.append(
                Distribution(
                    metric_id=metric_id,
                    domain=domain,
                    variant_id=variant_id,
                    unit=definition.unit,
                    count=len(observed),
                    missing_count=missing_count,
                    minimum=min(observed),
                    maximum=max(observed),
                    mean=fmean(observed),
                    standard_deviation=standard_deviation(observed),
                    p50=percentile(observed, 0.5),
                    p95=percentile(observed, 0.95),
                    median_absolute_deviation=median_absolute_deviation(observed),
                    interquartile_range=interquartile_range(observed),
                    confidence_low=low,
                    confidence_high=high,
                    confidence_level=self.confidence_level,
                )
            )
        return tuple(output)

    def comparisons(
        self,
        samples: Iterable[RawSample],
        *,
        anchor_variant: str = "dynamic-heterogeneous-swarm",
    ) -> tuple[PairedComparison, ...]:
        selected = tuple(samples)
        index: dict[tuple[str, str, str, int], RawSample] = {}
        variants: set[str] = set()
        metrics: set[str] = set()
        domains: set[str] = set()
        for sample in selected:
            key = (
                sample.metric_id,
                sample.domain.value,
                sample.variant_id,
                sample.repetition,
            )
            if key in index:
                raise invalid(
                    "benchmark_paired_sample_duplicate",
                    "Paired comparison contains duplicate samples.",
                    phase="statistics",
                    detail={"key": key},
                )
            index[key] = sample
            variants.add(sample.variant_id)
            metrics.add(sample.metric_id)
            domains.add(sample.domain.value)
        if anchor_variant not in variants:
            raise invalid(
                "benchmark_comparison_anchor_missing",
                "Dynamic comparison anchor has no samples.",
                phase="statistics",
            )
        output: list[PairedComparison] = []
        for metric_id in sorted(metrics):
            definition = self.catalog.get(metric_id)
            for domain in sorted(domains):
                repetitions = sorted(
                    {
                        sample.repetition
                        for sample in selected
                        if sample.metric_id == metric_id
                        and sample.domain.value == domain
                    }
                )
                for variant in sorted(variants - {anchor_variant}):
                    pairs: list[tuple[float, float]] = []
                    missing = 0
                    for repetition in repetitions:
                        anchor = index.get(
                            (metric_id, domain, anchor_variant, repetition)
                        )
                        candidate = index.get(
                            (metric_id, domain, variant, repetition)
                        )
                        if (
                            anchor is None
                            or candidate is None
                            or anchor.status is not SampleStatus.OBSERVED
                            or candidate.status is not SampleStatus.OBSERVED
                            or anchor.value is None
                            or candidate.value is None
                        ):
                            missing += 1
                            continue
                        if anchor.unit != candidate.unit:
                            raise invalid(
                                "benchmark_paired_unit_mismatch",
                                "Paired samples use incompatible units.",
                                phase="statistics",
                                detail={
                                    "metric_id": metric_id,
                                    "domain": domain,
                                    "variant": variant,
                                    "repetition": repetition,
                                },
                            )
                        pairs.append((float(anchor.value), float(candidate.value)))
                    if definition.required and missing:
                        raise invalid(
                            "benchmark_paired_samples_missing",
                            "Required paired comparison has missing repetitions.",
                            phase="statistics",
                            detail={
                                "metric_id": metric_id,
                                "domain": domain,
                                "variant": variant,
                                "missing": missing,
                            },
                        )
                    if not pairs:
                        raise invalid(
                            "benchmark_paired_comparison_empty",
                            "Paired comparison contains no observed pairs.",
                            phase="statistics",
                            detail={
                                "metric_id": metric_id,
                                "domain": domain,
                                "variant": variant,
                            },
                        )
                    deltas = [candidate - anchor for anchor, candidate in pairs]
                    low, high = bootstrap_mean_interval(
                        deltas,
                        confidence_level=self.confidence_level,
                        iterations=self.bootstrap_iterations,
                        seed=seed_for(metric_id, domain, variant, "paired"),
                    )
                    anchor_mean = fmean(anchor for anchor, _ in pairs)
                    delta_mean = fmean(deltas)
                    relative = None if anchor_mean == 0 else delta_mean / abs(anchor_mean)
                    direction, verdict = effect_verdict(
                        definition.direction,
                        delta_mean,
                        low,
                        high,
                        definition.target,
                        pairs,
                    )
                    output.append(
                        PairedComparison(
                            metric_id=metric_id,
                            domain=domain,
                            baseline_variant=anchor_variant,
                            candidate_variant=variant,
                            unit=definition.unit,
                            pair_count=len(pairs),
                            missing_pair_count=missing,
                            median_delta=percentile(deltas, 0.5),
                            p95_absolute_delta=percentile(
                                [abs(item) for item in deltas],
                                0.95,
                            ),
                            mean_delta=delta_mean,
                            confidence_low=low,
                            confidence_high=high,
                            relative_change=relative,
                            effect_direction=direction,
                            verdict=verdict,
                        )
                    )
        return tuple(output)

    def evaluate(self, samples: Iterable[RawSample]) -> dict[str, Any]:
        selected = tuple(samples)
        distributions = self.distributions(selected)
        comparisons = self.comparisons(selected)
        output = {
            "schema": "zyra.live-benchmark-statistical-evaluation/v1",
            "valid": True,
            "sample_count": len(selected),
            "distribution_count": len(distributions),
            "comparison_count": len(comparisons),
            "confidence_level": self.confidence_level,
            "bootstrap_iterations": self.bootstrap_iterations,
            "distributions": [item.to_dict() for item in distributions],
            "comparisons": [item.to_dict() for item in comparisons],
            "sample_digest": digest([item.to_dict() for item in selected]),
            "verified_at": utc_now(),
        }
        output["receipt_digest"] = digest(output)
        return output


def effect_verdict(
    direction: MetricDirection,
    mean_delta: float,
    confidence_low: float,
    confidence_high: float,
    target: float | None,
    pairs: Sequence[tuple[float, float]],
) -> tuple[str, Verdict]:
    if direction is MetricDirection.INFORMATIONAL:
        return "informational", Verdict.INCONCLUSIVE
    if direction is MetricDirection.HIGHER_IS_BETTER:
        if confidence_low > 0:
            return "candidate-better", Verdict.PASS
        if confidence_high < 0:
            return "candidate-worse", Verdict.FAIL
        return "uncertain", Verdict.INCONCLUSIVE
    if direction is MetricDirection.LOWER_IS_BETTER:
        if confidence_high < 0:
            return "candidate-better", Verdict.PASS
        if confidence_low > 0:
            return "candidate-worse", Verdict.FAIL
        return "uncertain", Verdict.INCONCLUSIVE
    if direction is MetricDirection.TARGET_IS_BETTER:
        if target is None:
            raise invalid(
                "benchmark_target_metric_target_missing",
                "Target-oriented metric has no target.",
                phase="statistics",
            )
        anchor_distance = fmean(abs(anchor - target) for anchor, _ in pairs)
        candidate_distance = fmean(abs(candidate - target) for _, candidate in pairs)
        if candidate_distance < anchor_distance:
            return "candidate-closer-to-target", Verdict.PASS
        if candidate_distance > anchor_distance:
            return "candidate-farther-from-target", Verdict.FAIL
        return "equivalent", Verdict.INCONCLUSIVE
    raise invalid(
        "benchmark_metric_direction_invalid",
        "Metric comparison direction is unsupported.",
        phase="statistics",
    )


def seed_for(*values: str) -> int:
    return int(digest(list(values))[:16], 16)
