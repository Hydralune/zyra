from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from statistics import fmean
from typing import Any

from .canonical import digest, new_identity, utc_now
from .effective_steps import StepBatch
from .models import MetricSample, StepDisposition


class ScenarioMetricCollector:
    def __init__(self, *, maximum_raw_samples: int = 1_000_000) -> None:
        self._maximum_raw_samples = maximum_raw_samples

    def collect(
        self,
        batch: StepBatch,
        *,
        scenario_run_id: str,
        owner_run_id: str,
        task_id: str,
        started_at: str,
        completed_at: str,
    ) -> tuple[MetricSample, ...]:
        samples: list[MetricSample] = []
        for step in batch.steps:
            if len(samples) >= self._maximum_raw_samples:
                break
            dimensions = {
                "scenario_run_id": scenario_run_id,
                "owner_run_id": owner_run_id,
                "task_id": task_id,
                "stage": step.stage or "unknown",
                "worker": step.worker_id or "unassigned",
                "profile": step.profile_id,
                "provider": step.provider_id,
                "event_type": step.event_type,
                "effect": step.effect.value,
                "disposition": step.disposition.value,
            }
            samples.append(
                MetricSample(
                    sample_id=new_identity("sample"),
                    metric="canonical_event_candidate",
                    value=1.0,
                    unit="event",
                    dimensions=dimensions,
                    source_step_ids=(step.step_id,),
                    observed_at=step.created_at or utc_now(),
                )
            )
            if step.disposition is StepDisposition.ADMITTED:
                samples.append(
                    MetricSample(
                        sample_id=new_identity("sample"),
                        metric="effective_transition",
                        value=1.0,
                        unit="transition",
                        dimensions=dimensions,
                        source_step_ids=(step.step_id,),
                        observed_at=step.created_at or utc_now(),
                    )
                )
        duration = _duration_ms(started_at, completed_at)
        samples.append(
            MetricSample(
                sample_id=new_identity("sample"),
                metric="scenario_wall_time",
                value=float(duration),
                unit="millisecond",
                dimensions={
                    "scenario_run_id": scenario_run_id,
                    "owner_run_id": owner_run_id,
                    "task_id": task_id,
                    "stage": "scenario",
                    "worker": "all",
                    "profile": batch.admitted[0].profile_id if batch.admitted else "unknown",
                    "provider": batch.admitted[0].provider_id if batch.admitted else "unknown",
                    "event_type": "scenario_lifecycle",
                    "effect": "lifecycle",
                    "disposition": "observed",
                },
                source_step_ids=tuple(item.step_id for item in batch.admitted),
                observed_at=completed_at,
            )
        )
        return tuple(samples[: self._maximum_raw_samples])

    def summarize(self, samples: Iterable[MetricSample]) -> dict[str, Any]:
        values = tuple(samples)
        by_metric: dict[str, list[MetricSample]] = defaultdict(list)
        by_stage: Counter[str] = Counter()
        by_worker: Counter[str] = Counter()
        by_profile: Counter[str] = Counter()
        by_provider: Counter[str] = Counter()
        by_effect: Counter[str] = Counter()
        by_disposition: Counter[str] = Counter()
        for sample in values:
            by_metric[sample.metric].append(sample)
            if sample.metric != "effective_transition":
                continue
            by_stage[sample.dimensions["stage"]] += int(sample.value)
            by_worker[sample.dimensions["worker"]] += int(sample.value)
            by_profile[sample.dimensions["profile"]] += int(sample.value)
            by_provider[sample.dimensions["provider"]] += int(sample.value)
            by_effect[sample.dimensions["effect"]] += int(sample.value)
            by_disposition[sample.dimensions["disposition"]] += int(sample.value)
        metrics = {
            name: {
                "count": len(items),
                "sum": sum(item.value for item in items),
                "minimum": min((item.value for item in items), default=0),
                "maximum": max((item.value for item in items), default=0),
                "mean": fmean(item.value for item in items) if items else 0,
                "unit": items[0].unit if items else "",
            }
            for name, items in sorted(by_metric.items())
        }
        result = {
            "schema": "zyra.scenario-metric-summary/v1",
            "raw_sample_count": len(values),
            "metrics": metrics,
            "effective_by_stage": dict(sorted(by_stage.items())),
            "effective_by_worker": dict(sorted(by_worker.items())),
            "effective_by_profile": dict(sorted(by_profile.items())),
            "effective_by_provider": dict(sorted(by_provider.items())),
            "effective_by_effect": dict(sorted(by_effect.items())),
            "effective_by_disposition": dict(sorted(by_disposition.items())),
        }
        result["summary_digest"] = digest(result)
        return result

    def raw_sample_page(
        self,
        samples: Iterable[MetricSample],
        *,
        cursor: int = 0,
        limit: int = 100,
        filters: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        selected = tuple(samples)
        filters = {
            str(key): str(value)
            for key, value in (filters or {}).items()
            if str(value)
        }
        if filters:
            selected = tuple(
                item
                for item in selected
                if all(
                    item.dimensions.get(key) == value
                    for key, value in filters.items()
                )
            )
        start = max(0, int(cursor))
        size = max(1, min(10_000, int(limit)))
        page = selected[start : start + size]
        next_cursor = start + len(page)
        return {
            "schema": "zyra.scenario-metric-page/v1",
            "cursor": start,
            "next_cursor": next_cursor if next_cursor < len(selected) else None,
            "total": len(selected),
            "filters": filters,
            "samples": [item.to_dict() for item in page],
        }


def verify_metric_dimensions(samples: Iterable[MetricSample]) -> dict[str, Any]:
    required = {
        "scenario_run_id",
        "owner_run_id",
        "task_id",
        "stage",
        "worker",
        "profile",
        "provider",
        "event_type",
        "effect",
        "disposition",
    }
    failures: list[dict[str, Any]] = []
    count = 0
    for sample in samples:
        count += 1
        missing = sorted(required - set(sample.dimensions))
        blank = sorted(
            key for key in required if not str(sample.dimensions.get(key) or "").strip()
        )
        if missing or blank:
            failures.append(
                {
                    "sample_id": sample.sample_id,
                    "missing": missing,
                    "blank": blank,
                }
            )
    receipt = {
        "schema": "zyra.scenario-metric-dimension-verification/v1",
        "valid": not failures,
        "sample_count": count,
        "failures": failures,
        "verified_at": utc_now(),
    }
    receipt["receipt_digest"] = digest(receipt)
    return receipt


def _duration_ms(start: str, end: str) -> int:
    from datetime import datetime

    try:
        left = datetime.fromisoformat(start.replace("Z", "+00:00"))
        right = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return max(0, int((right - left).total_seconds() * 1000))
    except (ValueError, TypeError):
        return 0
