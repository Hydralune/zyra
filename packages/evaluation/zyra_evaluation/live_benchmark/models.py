from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .canonical import canonicalize, digest


class CampaignPhase(StrEnum):
    CREATED = "created"
    PLANNED = "planned"
    RUNNING = "running"
    VERIFYING = "verifying"
    REPORTING = "reporting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    FROZEN = "frozen"


class CellPhase(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    RUNNING = "running"
    ADMITTED = "admitted"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DomainKind(StrEnum):
    SOFTWARE_DELIVERY = "software-delivery"
    CROSS_SOURCE_RESEARCH = "cross-source-research"


class VariantKind(StrEnum):
    BASELINE = "baseline"
    ABLATION = "ablation"


class EvidenceMode(StrEnum):
    LIVE = "live"
    PROTECTED_PRIOR = "protected-prior"
    REPLAY = "replay"
    FIXTURE = "fixture"
    SYNTHETIC = "synthetic"


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class MetricDirection(StrEnum):
    HIGHER_IS_BETTER = "higher-is-better"
    LOWER_IS_BETTER = "lower-is-better"
    TARGET_IS_BETTER = "target-is-better"
    INFORMATIONAL = "informational"


class SampleStatus(StrEnum):
    OBSERVED = "observed"
    MISSING = "missing"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class CapabilityVector:
    scheduler: bool
    memory_compact: bool
    recovery: bool
    low_entropy: bool
    dynamic_topology: bool
    heterogeneous_roles: bool
    worker_limit: int
    communication_mode: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheduler": self.scheduler,
            "memory_compact": self.memory_compact,
            "recovery": self.recovery,
            "low_entropy": self.low_entropy,
            "dynamic_topology": self.dynamic_topology,
            "heterogeneous_roles": self.heterogeneous_roles,
            "worker_limit": self.worker_limit,
            "communication_mode": self.communication_mode,
        }

    @property
    def capability_digest(self) -> str:
        return digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class Variant:
    variant_id: str
    kind: VariantKind
    title: str
    capabilities: CapabilityVector
    comparison_anchor: str
    expected_disabled_capability: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "kind": self.kind.value,
            "title": self.title,
            "capabilities": self.capabilities.to_dict(),
            "comparison_anchor": self.comparison_anchor,
            "expected_disabled_capability": self.expected_disabled_capability,
            "metadata": canonicalize(self.metadata),
        }

    @property
    def variant_digest(self) -> str:
        return digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class CampaignConditions:
    commit_sha: str
    sealed_policy_digest: str
    environment_digest: str
    hardware_digest: str
    deployment_digest: str
    provider_policy_digest: str
    verifier_digest: str
    failure_schedule_digest: str
    budget_digest: str
    source_evidence_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit_sha": self.commit_sha,
            "sealed_policy_digest": self.sealed_policy_digest,
            "environment_digest": self.environment_digest,
            "hardware_digest": self.hardware_digest,
            "deployment_digest": self.deployment_digest,
            "provider_policy_digest": self.provider_policy_digest,
            "verifier_digest": self.verifier_digest,
            "failure_schedule_digest": self.failure_schedule_digest,
            "budget_digest": self.budget_digest,
            "source_evidence_digest": self.source_evidence_digest,
        }

    @property
    def condition_digest(self) -> str:
        return digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class BenchmarkCell:
    cell_id: str
    campaign_id: str
    domain: DomainKind
    variant_id: str
    repetition: int
    seed: int
    input_revision: str
    task_family_digest: str
    condition_digest: str
    planned_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "campaign_id": self.campaign_id,
            "domain": self.domain.value,
            "variant_id": self.variant_id,
            "repetition": self.repetition,
            "seed": self.seed,
            "input_revision": self.input_revision,
            "task_family_digest": self.task_family_digest,
            "condition_digest": self.condition_digest,
            "planned_at": self.planned_at,
        }

    @property
    def cell_digest(self) -> str:
        return digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    metric_id: str
    title: str
    unit: str
    direction: MetricDirection
    required: bool
    minimum: float | None = None
    maximum: float | None = None
    target: float | None = None
    dimensions: tuple[str, ...] = ()
    requirement_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "title": self.title,
            "unit": self.unit,
            "direction": self.direction.value,
            "required": self.required,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "target": self.target,
            "dimensions": list(self.dimensions),
            "requirement_ids": list(self.requirement_ids),
        }


@dataclass(frozen=True, slots=True)
class RawSample:
    sample_id: str
    campaign_id: str
    cell_id: str
    run_id: str
    domain: DomainKind
    variant_id: str
    repetition: int
    seed: int
    metric_id: str
    value: float | None
    unit: str
    status: SampleStatus
    observed_at: str
    evidence_digest: str
    dimensions: dict[str, str] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "campaign_id": self.campaign_id,
            "cell_id": self.cell_id,
            "run_id": self.run_id,
            "domain": self.domain.value,
            "variant_id": self.variant_id,
            "repetition": self.repetition,
            "seed": self.seed,
            "metric_id": self.metric_id,
            "value": self.value,
            "unit": self.unit,
            "status": self.status.value,
            "observed_at": self.observed_at,
            "evidence_digest": self.evidence_digest,
            "dimensions": dict(sorted(self.dimensions.items())),
            "reason": self.reason,
        }

    @property
    def sample_digest(self) -> str:
        return digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class Distribution:
    metric_id: str
    domain: str
    variant_id: str
    unit: str
    count: int
    missing_count: int
    minimum: float
    maximum: float
    mean: float
    standard_deviation: float
    p50: float
    p95: float
    median_absolute_deviation: float
    interquartile_range: float
    confidence_low: float
    confidence_high: float
    confidence_level: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "domain": self.domain,
            "variant_id": self.variant_id,
            "unit": self.unit,
            "count": self.count,
            "missing_count": self.missing_count,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "mean": self.mean,
            "standard_deviation": self.standard_deviation,
            "p50": self.p50,
            "p95": self.p95,
            "median_absolute_deviation": self.median_absolute_deviation,
            "interquartile_range": self.interquartile_range,
            "confidence_low": self.confidence_low,
            "confidence_high": self.confidence_high,
            "confidence_level": self.confidence_level,
        }


@dataclass(frozen=True, slots=True)
class PairedComparison:
    metric_id: str
    domain: str
    baseline_variant: str
    candidate_variant: str
    unit: str
    pair_count: int
    missing_pair_count: int
    median_delta: float
    p95_absolute_delta: float
    mean_delta: float
    confidence_low: float
    confidence_high: float
    relative_change: float | None
    effect_direction: str
    verdict: Verdict

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "domain": self.domain,
            "baseline_variant": self.baseline_variant,
            "candidate_variant": self.candidate_variant,
            "unit": self.unit,
            "pair_count": self.pair_count,
            "missing_pair_count": self.missing_pair_count,
            "median_delta": self.median_delta,
            "p95_absolute_delta": self.p95_absolute_delta,
            "mean_delta": self.mean_delta,
            "confidence_low": self.confidence_low,
            "confidence_high": self.confidence_high,
            "relative_change": self.relative_change,
            "effect_direction": self.effect_direction,
            "verdict": self.verdict.value,
        }


@dataclass(frozen=True, slots=True)
class CellResult:
    cell: BenchmarkCell
    run_id: str
    phase: CellPhase
    live_receipt: dict[str, Any]
    admission_receipt: dict[str, Any]
    verifier_receipt: dict[str, Any]
    deployment_receipt: dict[str, Any]
    fault_receipt: dict[str, Any]
    samples: tuple[RawSample, ...]
    started_at: str
    completed_at: str
    failure: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell": self.cell.to_dict(),
            "run_id": self.run_id,
            "phase": self.phase.value,
            "live_receipt": canonicalize(self.live_receipt),
            "admission_receipt": canonicalize(self.admission_receipt),
            "verifier_receipt": canonicalize(self.verifier_receipt),
            "deployment_receipt": canonicalize(self.deployment_receipt),
            "fault_receipt": canonicalize(self.fault_receipt),
            "samples": [item.to_dict() for item in self.samples],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "failure": canonicalize(self.failure),
        }

    @property
    def result_digest(self) -> str:
        return digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class Campaign:
    campaign_id: str
    schema_version: str
    phase: CampaignPhase
    conditions: CampaignConditions
    domains: tuple[DomainKind, ...]
    variants: tuple[Variant, ...]
    seeds: tuple[int, ...]
    cells: tuple[BenchmarkCell, ...]
    created_at: str
    updated_at: str
    minimum_effective_steps: int
    required_long_run_steps: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "schema_version": self.schema_version,
            "phase": self.phase.value,
            "conditions": self.conditions.to_dict(),
            "domains": [item.value for item in self.domains],
            "variants": [item.to_dict() for item in self.variants],
            "seeds": list(self.seeds),
            "cells": [item.to_dict() for item in self.cells],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "minimum_effective_steps": self.minimum_effective_steps,
            "required_long_run_steps": self.required_long_run_steps,
            "metadata": canonicalize(self.metadata),
        }

    @property
    def campaign_digest(self) -> str:
        return digest(self.to_dict())
