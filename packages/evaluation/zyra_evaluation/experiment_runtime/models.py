from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from typing import Any

from .canonical import canonicalize, digest, new_identity, utc_now


def _field_map(value: Any) -> dict[str, Any]:
    return {item.name: getattr(value, item.name) for item in fields(value)}


class ExperimentPhase(StrEnum):
    CREATED = "created"
    ADMITTED = "admitted"
    QUEUED = "queued"
    RUNNING = "running"
    AGGREGATING = "aggregating"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


TERMINAL_PHASES = {
    ExperimentPhase.SUCCEEDED,
    ExperimentPhase.FAILED,
    ExperimentPhase.CANCELLED,
    ExperimentPhase.ARCHIVED,
}


class VariantKind(StrEnum):
    BASELINE = "baseline"
    ABLATION = "ablation"


class BaselineKind(StrEnum):
    SINGLE_AGENT = "single_agent"
    STATIC_FULL_CONNECT_MULTI_AGENT = "static_full_connect_multi_agent"
    DYNAMIC_HETEROGENEOUS_SWARM = "dynamic_heterogeneous_swarm"


class AblationKind(StrEnum):
    NO_SCHEDULER = "no_scheduler"
    NO_MEMORY_COMPACT = "no_memory_compact"
    NO_RECOVERY = "no_recovery"
    NO_LOW_ENTROPY_COMMUNICATION = "no_low_entropy_communication"


class CellPhase(StrEnum):
    PLANNED = "planned"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class SampleStatus(StrEnum):
    OBSERVED = "observed"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"
    ANOMALOUS = "anomalous"


class EvidenceStatus(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    INVALID = "invalid"
    NON_CLAIM = "non_claim"
    DEFERRED = "deferred"


class ComparisonDirection(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"
    TARGET_IS_BETTER = "target_is_better"
    DESCRIPTIVE = "descriptive"


@dataclass(frozen=True, slots=True)
class CapabilityVector:
    scheduler: bool = True
    memory_compact: bool = True
    recovery: bool = True
    low_entropy_communication: bool = True
    dynamic_topology: bool = True
    heterogeneous_roles: bool = True
    worker_limit: int = 8
    communication_mode: str = "targeted"

    def to_dict(self) -> dict[str, Any]:
        return canonicalize(_field_map(self))

    def changed(self, other: "CapabilityVector") -> tuple[str, ...]:
        changed = [
            item.name
            for item in fields(self)
            if getattr(self, item.name) != getattr(other, item.name)
        ]
        # Communication mode is the concrete transport behavior of the
        # low-entropy capability, not an independent ablation dimension.
        if (
            "low_entropy_communication" in changed
            and "communication_mode" in changed
        ):
            changed.remove("communication_mode")
        return tuple(changed)


@dataclass(frozen=True, slots=True)
class VariantDefinition:
    variant_id: str
    kind: VariantKind
    title: str
    description: str
    capabilities: CapabilityVector
    comparison_anchor: str
    expected_disabled_capability: str = ""
    required: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def definition_digest(self) -> str:
        return digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.experiment-variant/v1",
            "variant_id": self.variant_id,
            "kind": self.kind.value,
            "title": self.title,
            "description": self.description,
            "capabilities": self.capabilities.to_dict(),
            "comparison_anchor": self.comparison_anchor,
            "expected_disabled_capability": self.expected_disabled_capability,
            "required": self.required,
            "metadata": canonicalize(self.metadata),
        }
        if include_digest:
            value["definition_digest"] = self.definition_digest
        return value


@dataclass(frozen=True, slots=True)
class BudgetEnvelope:
    maximum_effective_steps: int
    maximum_wall_time_ms: int
    maximum_token_units: int
    maximum_cost_microunits: int
    maximum_artifact_bytes: int
    maximum_fault_retries: int
    concurrency: int

    @property
    def budget_digest(self) -> str:
        return digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "maximum_effective_steps": self.maximum_effective_steps,
            "maximum_wall_time_ms": self.maximum_wall_time_ms,
            "maximum_token_units": self.maximum_token_units,
            "maximum_cost_microunits": self.maximum_cost_microunits,
            "maximum_artifact_bytes": self.maximum_artifact_bytes,
            "maximum_fault_retries": self.maximum_fault_retries,
            "concurrency": self.concurrency,
        }
        if include_digest:
            value["budget_digest"] = self.budget_digest
        return value


@dataclass(frozen=True, slots=True)
class HardwareEnvelope:
    profile_id: str
    os_family: str
    architecture: str
    cpu_class: str
    logical_cpu_count: int
    memory_limit_bytes: int
    edge_isolation_kind: str
    cloud_execution_allowed: bool
    accelerator: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def hardware_digest(self) -> str:
        return digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            **canonicalize(_field_map(self)),
            "metadata": canonicalize(self.metadata),
        }
        if include_digest:
            value["hardware_digest"] = self.hardware_digest
        return value


@dataclass(frozen=True, slots=True)
class ProviderEnvelope:
    policy_id: str
    policy_digest: str
    provider_catalog_digest: str
    allowed_provider_ids: tuple[str, ...]
    allowed_model_ids: tuple[str, ...]
    authenticated_provider_cli_allowed: bool
    external_model_request_allowed: bool
    credential_presence_digest: str
    prior_verified_receipt_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            **canonicalize(_field_map(self)),
            "allowed_provider_ids": list(self.allowed_provider_ids),
            "allowed_model_ids": list(self.allowed_model_ids),
            "prior_verified_receipt_ids": list(self.prior_verified_receipt_ids),
        }


@dataclass(frozen=True, slots=True)
class VerifierEnvelope:
    verifier_id: str
    version: str
    implementation_digest: str
    rules_digest: str
    required_checks: tuple[str, ...]
    fail_closed: bool = True

    @property
    def verifier_digest(self) -> str:
        return digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            **canonicalize(_field_map(self)),
            "required_checks": list(self.required_checks),
        }
        if include_digest:
            value["verifier_digest"] = self.verifier_digest
        return value


@dataclass(frozen=True, slots=True)
class FailureScheduleEnvelope:
    schedule_id: str
    schedule_digest: str
    fault_kinds: tuple[str, ...]
    requirement_change_ids: tuple[str, ...]
    injection_offsets: tuple[int, ...]
    deterministic: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            **canonicalize(_field_map(self)),
            "fault_kinds": list(self.fault_kinds),
            "requirement_change_ids": list(self.requirement_change_ids),
            "injection_offsets": list(self.injection_offsets),
        }


@dataclass(frozen=True, slots=True)
class ComparisonEnvelope:
    envelope_id: str
    scenario_id: str
    scenario_definition_digest: str
    task_input_digest: str
    task_input_bytes: int
    task_domain: str
    commit_sha: str
    environment_digest: str
    source_evidence_digest: str
    sealed_policy_digest: str
    budget: BudgetEnvelope
    hardware: HardwareEnvelope
    provider: ProviderEnvelope
    verifier: VerifierEnvelope
    failure_schedule: FailureScheduleEnvelope
    seed_plan: tuple[int, ...]
    created_at: str
    labels: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def envelope_digest(self) -> str:
        return digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.experiment-comparison-envelope/v1",
            "envelope_id": self.envelope_id,
            "scenario_id": self.scenario_id,
            "scenario_definition_digest": self.scenario_definition_digest,
            "task_input_digest": self.task_input_digest,
            "task_input_bytes": self.task_input_bytes,
            "task_domain": self.task_domain,
            "commit_sha": self.commit_sha,
            "environment_digest": self.environment_digest,
            "source_evidence_digest": self.source_evidence_digest,
            "sealed_policy_digest": self.sealed_policy_digest,
            "budget": self.budget.to_dict(),
            "hardware": self.hardware.to_dict(),
            "provider": self.provider.to_dict(),
            "verifier": self.verifier.to_dict(),
            "failure_schedule": self.failure_schedule.to_dict(),
            "seed_plan": list(self.seed_plan),
            "created_at": self.created_at,
            "labels": dict(self.labels),
            "metadata": canonicalize(self.metadata),
        }
        if include_digest:
            value["envelope_digest"] = self.envelope_digest
        return value


@dataclass(frozen=True, slots=True)
class MatrixCell:
    cell_id: str
    variant_id: str
    repetition: int
    seed: int
    envelope_digest: str
    phase: CellPhase
    revision: int
    created_at: str
    updated_at: str
    started_at: str = ""
    completed_at: str = ""
    scenario_run_id: str = ""
    owner_run_id: str = ""
    task_id: str = ""
    observation_digest: str = ""
    sample_ids: tuple[str, ...] = ()
    verification_receipt: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None

    @classmethod
    def plan(
        cls,
        *,
        variant_id: str,
        repetition: int,
        seed: int,
        envelope_digest: str,
    ) -> "MatrixCell":
        now = utc_now()
        return cls(
            cell_id=new_identity("cell"),
            variant_id=variant_id,
            repetition=repetition,
            seed=seed,
            envelope_digest=envelope_digest,
            phase=CellPhase.PLANNED,
            revision=0,
            created_at=now,
            updated_at=now,
        )

    @property
    def terminal(self) -> bool:
        return self.phase in {
            CellPhase.SUCCEEDED,
            CellPhase.FAILED,
            CellPhase.REJECTED,
            CellPhase.CANCELLED,
        }

    def evolve(self, **changes: Any) -> "MatrixCell":
        return replace(
            self,
            **changes,
            revision=self.revision + 1,
            updated_at=utc_now(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.experiment-matrix-cell/v1",
            **canonicalize(_field_map(self)),
            "phase": self.phase.value,
            "terminal": self.terminal,
            "sample_ids": list(self.sample_ids),
        }


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    metric: str
    unit: str
    direction: ComparisonDirection
    category: str
    required: bool
    minimum_samples: int
    target: float | None = None
    bounded_minimum: float | None = None
    bounded_maximum: float | None = None
    description: str = ""
    requirement_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            **canonicalize(_field_map(self)),
            "direction": self.direction.value,
            "requirement_ids": list(self.requirement_ids),
        }


@dataclass(frozen=True, slots=True)
class RawMetricSample:
    sample_id: str
    experiment_id: str
    cell_id: str
    variant_id: str
    repetition: int
    metric: str
    value: float | None
    unit: str
    status: SampleStatus
    observed_at: str
    source_kind: str
    source_ids: tuple[str, ...]
    source_digest: str
    dimensions: dict[str, str]
    anomaly_reason: str = ""
    unavailable_reason: str = ""
    sequence: int = 0

    @property
    def sample_digest(self) -> str:
        return digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.experiment-raw-metric-sample/v1",
            "sample_id": self.sample_id,
            "experiment_id": self.experiment_id,
            "cell_id": self.cell_id,
            "variant_id": self.variant_id,
            "repetition": self.repetition,
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "status": self.status.value,
            "observed_at": self.observed_at,
            "source_kind": self.source_kind,
            "source_ids": list(self.source_ids),
            "source_digest": self.source_digest,
            "dimensions": dict(self.dimensions),
            "anomaly_reason": self.anomaly_reason,
            "unavailable_reason": self.unavailable_reason,
            "sequence": self.sequence,
        }
        if include_digest:
            value["sample_digest"] = self.sample_digest
        return value


@dataclass(frozen=True, slots=True)
class ConfidenceInterval:
    method: str
    level: float
    lower: float | None
    upper: float | None
    resamples: int
    seed: int
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return canonicalize(_field_map(self))


@dataclass(frozen=True, slots=True)
class DistributionSummary:
    metric: str
    unit: str
    variant_id: str
    total_sample_count: int
    observed_sample_count: int
    unavailable_sample_count: int
    anomalous_sample_count: int
    rejected_sample_count: int
    minimum: float | None
    maximum: float | None
    mean: float | None
    p50: float | None
    p95: float | None
    population_variance: float | None
    sample_variance: float | None
    standard_deviation: float | None
    median_absolute_deviation: float | None
    interquartile_range: float | None
    coefficient_of_variation: float | None
    confidence: ConfidenceInterval
    anomaly_reasons: tuple[str, ...]
    source_sample_ids: tuple[str, ...]
    computed_at: str

    @property
    def summary_digest(self) -> str:
        return digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.experiment-distribution-summary/v1",
            **canonicalize(_field_map(self)),
            "confidence": self.confidence.to_dict(),
            "anomaly_reasons": list(self.anomaly_reasons),
            "source_sample_ids": list(self.source_sample_ids),
        }
        if include_digest:
            value["summary_digest"] = self.summary_digest
        return value


@dataclass(frozen=True, slots=True)
class VariantComparison:
    metric: str
    unit: str
    baseline_variant_id: str
    compared_variant_id: str
    baseline_p50: float | None
    compared_p50: float | None
    absolute_delta: float | None
    relative_delta: float | None
    effect_direction: str
    better: bool | None
    comparable: bool
    reason: str
    baseline_summary_digest: str
    compared_summary_digest: str

    @property
    def comparison_digest(self) -> str:
        return digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.experiment-variant-comparison/v1",
            **canonicalize(_field_map(self)),
        }
        if include_digest:
            value["comparison_digest"] = self.comparison_digest
        return value


@dataclass(frozen=True, slots=True)
class RequirementEvidence:
    requirement_id: str
    score: int
    status: EvidenceStatus
    owner: str
    evidence_paths: tuple[str, ...]
    metric_names: tuple[str, ...]
    run_ids: tuple[str, ...]
    claim: str
    reason: str
    verified: bool
    source_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **canonicalize(_field_map(self)),
            "status": self.status.value,
            "evidence_paths": list(self.evidence_paths),
            "metric_names": list(self.metric_names),
            "run_ids": list(self.run_ids),
        }


@dataclass(frozen=True, slots=True)
class BundleMember:
    path: str
    media_type: str
    size: int
    sha256: str
    category: str
    requirement_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    executable: bool = False
    redacted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            **canonicalize(_field_map(self)),
            "requirement_ids": list(self.requirement_ids),
            "source_ids": list(self.source_ids),
        }


@dataclass(frozen=True, slots=True)
class EvidenceBundleManifest:
    bundle_id: str
    experiment_id: str
    schema_version: str
    commit_sha: str
    envelope_digest: str
    report_digest: str
    members: tuple[BundleMember, ...]
    root_digest: str
    created_at: str
    source_run_ids: tuple[str, ...]
    requirement_ids: tuple[str, ...]
    source_role_audit_digest: str
    non_claims: tuple[str, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def manifest_digest(self) -> str:
        return digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.experiment-evidence-bundle-manifest/v1",
            "bundle_id": self.bundle_id,
            "experiment_id": self.experiment_id,
            "schema_version": self.schema_version,
            "commit_sha": self.commit_sha,
            "envelope_digest": self.envelope_digest,
            "report_digest": self.report_digest,
            "members": [item.to_dict() for item in self.members],
            "root_digest": self.root_digest,
            "created_at": self.created_at,
            "source_run_ids": list(self.source_run_ids),
            "requirement_ids": list(self.requirement_ids),
            "source_role_audit_digest": self.source_role_audit_digest,
            "non_claims": list(self.non_claims),
            "metadata": canonicalize(self.metadata),
        }
        if include_digest:
            value["manifest_digest"] = self.manifest_digest
        return value


@dataclass(frozen=True, slots=True)
class ExperimentRun:
    experiment_id: str
    title: str
    phase: ExperimentPhase
    revision: int
    repetitions: int
    envelope: ComparisonEnvelope
    variants: tuple[VariantDefinition, ...]
    cells: tuple[MatrixCell, ...]
    created_at: str
    updated_at: str
    started_at: str = ""
    completed_at: str = ""
    requested_by: str = ""
    report: dict[str, Any] | None = None
    bundle_manifest: dict[str, Any] | None = None
    verification_receipt: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    cancel_requested: bool = False
    archive_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        title: str,
        repetitions: int,
        envelope: ComparisonEnvelope,
        variants: tuple[VariantDefinition, ...],
        cells: tuple[MatrixCell, ...],
        requested_by: str,
        metadata: dict[str, Any] | None = None,
    ) -> "ExperimentRun":
        now = utc_now()
        return cls(
            experiment_id=new_identity("experiment"),
            title=title,
            phase=ExperimentPhase.CREATED,
            revision=0,
            repetitions=repetitions,
            envelope=envelope,
            variants=variants,
            cells=cells,
            created_at=now,
            updated_at=now,
            requested_by=requested_by,
            metadata=dict(metadata or {}),
        )

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    @property
    def succeeded_cells(self) -> tuple[MatrixCell, ...]:
        return tuple(item for item in self.cells if item.phase is CellPhase.SUCCEEDED)

    @property
    def failed_cells(self) -> tuple[MatrixCell, ...]:
        return tuple(
            item
            for item in self.cells
            if item.phase in {CellPhase.FAILED, CellPhase.REJECTED}
        )

    def cell(self, cell_id: str) -> MatrixCell:
        for item in self.cells:
            if item.cell_id == cell_id:
                return item
        raise KeyError(cell_id)

    def replace_cell(self, cell: MatrixCell) -> "ExperimentRun":
        cells = tuple(
            cell if item.cell_id == cell.cell_id else item for item in self.cells
        )
        return self.evolve(cells=cells)

    def evolve(self, **changes: Any) -> "ExperimentRun":
        return replace(
            self,
            **changes,
            revision=self.revision + 1,
            updated_at=utc_now(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.experiment-run/v1",
            "experiment_id": self.experiment_id,
            "title": self.title,
            "phase": self.phase.value,
            "terminal": self.terminal,
            "revision": self.revision,
            "repetitions": self.repetitions,
            "envelope": self.envelope.to_dict(),
            "variants": [item.to_dict() for item in self.variants],
            "cells": [item.to_dict() for item in self.cells],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "requested_by": self.requested_by,
            "report": canonicalize(self.report),
            "bundle_manifest": canonicalize(self.bundle_manifest),
            "verification_receipt": canonicalize(self.verification_receipt),
            "failure": canonicalize(self.failure),
            "cancel_requested": self.cancel_requested,
            "archive_reason": self.archive_reason,
            "metadata": canonicalize(self.metadata),
            "progress": {
                "planned": len(self.cells),
                "succeeded": len(self.succeeded_cells),
                "failed": len(self.failed_cells),
                "terminal": sum(1 for item in self.cells if item.terminal),
            },
        }
