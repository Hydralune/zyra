from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import invalid
from .models import (
    BudgetEnvelope,
    CapabilityVector,
    CellPhase,
    ComparisonDirection,
    ComparisonEnvelope,
    ConfidenceInterval,
    DistributionSummary,
    ExperimentPhase,
    ExperimentRun,
    FailureScheduleEnvelope,
    HardwareEnvelope,
    MatrixCell,
    MetricDefinition,
    ProviderEnvelope,
    RawMetricSample,
    SampleStatus,
    VariantComparison,
    VariantDefinition,
    VariantKind,
    VerifierEnvelope,
)


def mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise invalid(
            "experiment_persisted_shape_invalid",
            f"{label} must be an object.",
            phase="persistence",
        )
    return value


def comparison_envelope(value: Any) -> ComparisonEnvelope:
    selected = mapping(value, "comparison envelope")
    budget = mapping(selected.get("budget"), "budget")
    hardware = mapping(selected.get("hardware"), "hardware")
    provider = mapping(selected.get("provider"), "provider")
    verifier = mapping(selected.get("verifier"), "verifier")
    failure = mapping(selected.get("failure_schedule"), "failure schedule")
    return ComparisonEnvelope(
        envelope_id=str(selected["envelope_id"]),
        scenario_id=str(selected["scenario_id"]),
        scenario_definition_digest=str(selected["scenario_definition_digest"]),
        task_input_digest=str(selected["task_input_digest"]),
        task_input_bytes=int(selected["task_input_bytes"]),
        task_domain=str(selected["task_domain"]),
        commit_sha=str(selected["commit_sha"]),
        environment_digest=str(selected["environment_digest"]),
        source_evidence_digest=str(selected["source_evidence_digest"]),
        sealed_policy_digest=str(selected["sealed_policy_digest"]),
        budget=BudgetEnvelope(
            maximum_effective_steps=int(budget["maximum_effective_steps"]),
            maximum_wall_time_ms=int(budget["maximum_wall_time_ms"]),
            maximum_token_units=int(budget["maximum_token_units"]),
            maximum_cost_microunits=int(budget["maximum_cost_microunits"]),
            maximum_artifact_bytes=int(budget["maximum_artifact_bytes"]),
            maximum_fault_retries=int(budget["maximum_fault_retries"]),
            concurrency=int(budget["concurrency"]),
        ),
        hardware=HardwareEnvelope(
            profile_id=str(hardware["profile_id"]),
            os_family=str(hardware["os_family"]),
            architecture=str(hardware["architecture"]),
            cpu_class=str(hardware["cpu_class"]),
            logical_cpu_count=int(hardware["logical_cpu_count"]),
            memory_limit_bytes=int(hardware["memory_limit_bytes"]),
            edge_isolation_kind=str(hardware["edge_isolation_kind"]),
            cloud_execution_allowed=hardware["cloud_execution_allowed"] is True,
            accelerator=str(hardware.get("accelerator") or ""),
            metadata=dict(hardware.get("metadata") or {}),
        ),
        provider=ProviderEnvelope(
            policy_id=str(provider["policy_id"]),
            policy_digest=str(provider["policy_digest"]),
            provider_catalog_digest=str(provider["provider_catalog_digest"]),
            allowed_provider_ids=tuple(
                str(item) for item in provider.get("allowed_provider_ids") or ()
            ),
            allowed_model_ids=tuple(
                str(item) for item in provider.get("allowed_model_ids") or ()
            ),
            authenticated_provider_cli_allowed=(
                provider.get("authenticated_provider_cli_allowed") is True
            ),
            external_model_request_allowed=(
                provider.get("external_model_request_allowed") is True
            ),
            credential_presence_digest=str(provider["credential_presence_digest"]),
            prior_verified_receipt_ids=tuple(
                str(item)
                for item in provider.get("prior_verified_receipt_ids") or ()
            ),
            metadata=dict(provider.get("metadata") or {}),
        ),
        verifier=VerifierEnvelope(
            verifier_id=str(verifier["verifier_id"]),
            version=str(verifier["version"]),
            implementation_digest=str(verifier["implementation_digest"]),
            rules_digest=str(verifier["rules_digest"]),
            required_checks=tuple(
                str(item) for item in verifier.get("required_checks") or ()
            ),
            fail_closed=verifier.get("fail_closed") is True,
        ),
        failure_schedule=FailureScheduleEnvelope(
            schedule_id=str(failure["schedule_id"]),
            schedule_digest=str(failure["schedule_digest"]),
            fault_kinds=tuple(
                str(item) for item in failure.get("fault_kinds") or ()
            ),
            requirement_change_ids=tuple(
                str(item)
                for item in failure.get("requirement_change_ids") or ()
            ),
            injection_offsets=tuple(
                int(item) for item in failure.get("injection_offsets") or ()
            ),
            deterministic=failure.get("deterministic") is True,
            metadata=dict(failure.get("metadata") or {}),
        ),
        seed_plan=tuple(int(item) for item in selected.get("seed_plan") or ()),
        created_at=str(selected["created_at"]),
        labels={
            str(key): str(item)
            for key, item in dict(selected.get("labels") or {}).items()
        },
        metadata=dict(selected.get("metadata") or {}),
    )


def capability_vector(value: Any) -> CapabilityVector:
    selected = mapping(value, "capability vector")
    return CapabilityVector(
        scheduler=selected.get("scheduler") is True,
        memory_compact=selected.get("memory_compact") is True,
        recovery=selected.get("recovery") is True,
        low_entropy_communication=(
            selected.get("low_entropy_communication") is True
        ),
        dynamic_topology=selected.get("dynamic_topology") is True,
        heterogeneous_roles=selected.get("heterogeneous_roles") is True,
        worker_limit=int(selected.get("worker_limit") or 1),
        communication_mode=str(selected.get("communication_mode") or ""),
    )


def variant_definition(value: Any) -> VariantDefinition:
    selected = mapping(value, "variant definition")
    return VariantDefinition(
        variant_id=str(selected["variant_id"]),
        kind=VariantKind(str(selected["kind"])),
        title=str(selected["title"]),
        description=str(selected["description"]),
        capabilities=capability_vector(selected.get("capabilities")),
        comparison_anchor=str(selected["comparison_anchor"]),
        expected_disabled_capability=str(
            selected.get("expected_disabled_capability") or ""
        ),
        required=selected.get("required") is not False,
        metadata=dict(selected.get("metadata") or {}),
    )


def matrix_cell(value: Any) -> MatrixCell:
    selected = mapping(value, "matrix cell")
    return MatrixCell(
        cell_id=str(selected["cell_id"]),
        variant_id=str(selected["variant_id"]),
        repetition=int(selected["repetition"]),
        seed=int(selected["seed"]),
        envelope_digest=str(selected["envelope_digest"]),
        phase=CellPhase(str(selected["phase"])),
        revision=int(selected["revision"]),
        created_at=str(selected["created_at"]),
        updated_at=str(selected["updated_at"]),
        started_at=str(selected.get("started_at") or ""),
        completed_at=str(selected.get("completed_at") or ""),
        scenario_run_id=str(selected.get("scenario_run_id") or ""),
        owner_run_id=str(selected.get("owner_run_id") or ""),
        task_id=str(selected.get("task_id") or ""),
        observation_digest=str(selected.get("observation_digest") or ""),
        sample_ids=tuple(
            str(item) for item in selected.get("sample_ids") or ()
        ),
        verification_receipt=(
            dict(selected["verification_receipt"])
            if isinstance(selected.get("verification_receipt"), Mapping)
            else None
        ),
        failure=(
            dict(selected["failure"])
            if isinstance(selected.get("failure"), Mapping)
            else None
        ),
    )


def experiment_run(value: Any) -> ExperimentRun:
    selected = mapping(value, "experiment run")
    return ExperimentRun(
        experiment_id=str(selected["experiment_id"]),
        title=str(selected["title"]),
        phase=ExperimentPhase(str(selected["phase"])),
        revision=int(selected["revision"]),
        repetitions=int(selected["repetitions"]),
        envelope=comparison_envelope(selected.get("envelope")),
        variants=tuple(
            variant_definition(item)
            for item in selected.get("variants") or ()
        ),
        cells=tuple(
            matrix_cell(item) for item in selected.get("cells") or ()
        ),
        created_at=str(selected["created_at"]),
        updated_at=str(selected["updated_at"]),
        started_at=str(selected.get("started_at") or ""),
        completed_at=str(selected.get("completed_at") or ""),
        requested_by=str(selected.get("requested_by") or ""),
        report=(
            dict(selected["report"])
            if isinstance(selected.get("report"), Mapping)
            else None
        ),
        bundle_manifest=(
            dict(selected["bundle_manifest"])
            if isinstance(selected.get("bundle_manifest"), Mapping)
            else None
        ),
        verification_receipt=(
            dict(selected["verification_receipt"])
            if isinstance(selected.get("verification_receipt"), Mapping)
            else None
        ),
        failure=(
            dict(selected["failure"])
            if isinstance(selected.get("failure"), Mapping)
            else None
        ),
        cancel_requested=selected.get("cancel_requested") is True,
        archive_reason=str(selected.get("archive_reason") or ""),
        metadata=dict(selected.get("metadata") or {}),
    )


def raw_metric_sample(value: Any) -> RawMetricSample:
    selected = mapping(value, "raw metric sample")
    return RawMetricSample(
        sample_id=str(selected["sample_id"]),
        experiment_id=str(selected["experiment_id"]),
        cell_id=str(selected["cell_id"]),
        variant_id=str(selected["variant_id"]),
        repetition=int(selected["repetition"]),
        metric=str(selected["metric"]),
        value=(
            None if selected.get("value") is None else float(selected["value"])
        ),
        unit=str(selected["unit"]),
        status=SampleStatus(str(selected["status"])),
        observed_at=str(selected["observed_at"]),
        source_kind=str(selected["source_kind"]),
        source_ids=tuple(
            str(item) for item in selected.get("source_ids") or ()
        ),
        source_digest=str(selected["source_digest"]),
        dimensions={
            str(key): str(item)
            for key, item in dict(selected.get("dimensions") or {}).items()
        },
        anomaly_reason=str(selected.get("anomaly_reason") or ""),
        unavailable_reason=str(selected.get("unavailable_reason") or ""),
        sequence=int(selected.get("sequence") or 0),
    )


def metric_definition(value: Any) -> MetricDefinition:
    selected = mapping(value, "metric definition")
    return MetricDefinition(
        metric=str(selected["metric"]),
        unit=str(selected["unit"]),
        direction=ComparisonDirection(str(selected["direction"])),
        category=str(selected["category"]),
        required=selected.get("required") is True,
        minimum_samples=int(selected["minimum_samples"]),
        target=(
            None if selected.get("target") is None else float(selected["target"])
        ),
        bounded_minimum=(
            None
            if selected.get("bounded_minimum") is None
            else float(selected["bounded_minimum"])
        ),
        bounded_maximum=(
            None
            if selected.get("bounded_maximum") is None
            else float(selected["bounded_maximum"])
        ),
        description=str(selected.get("description") or ""),
        requirement_ids=tuple(
            str(item) for item in selected.get("requirement_ids") or ()
        ),
    )


def confidence_interval(value: Any) -> ConfidenceInterval:
    selected = mapping(value, "confidence interval")
    return ConfidenceInterval(
        method=str(selected["method"]),
        level=float(selected["level"]),
        lower=(
            None if selected.get("lower") is None else float(selected["lower"])
        ),
        upper=(
            None if selected.get("upper") is None else float(selected["upper"])
        ),
        resamples=int(selected["resamples"]),
        seed=int(selected["seed"]),
        reason=str(selected.get("reason") or ""),
    )


def distribution_summary(value: Any) -> DistributionSummary:
    selected = mapping(value, "distribution summary")
    number_fields = {
        name: (
            None if selected.get(name) is None else float(selected[name])
        )
        for name in (
            "minimum",
            "maximum",
            "mean",
            "p50",
            "p95",
            "population_variance",
            "sample_variance",
            "standard_deviation",
            "median_absolute_deviation",
            "interquartile_range",
            "coefficient_of_variation",
        )
    }
    return DistributionSummary(
        metric=str(selected["metric"]),
        unit=str(selected["unit"]),
        variant_id=str(selected["variant_id"]),
        total_sample_count=int(selected["total_sample_count"]),
        observed_sample_count=int(selected["observed_sample_count"]),
        unavailable_sample_count=int(selected["unavailable_sample_count"]),
        anomalous_sample_count=int(selected["anomalous_sample_count"]),
        rejected_sample_count=int(selected["rejected_sample_count"]),
        confidence=confidence_interval(selected.get("confidence")),
        anomaly_reasons=tuple(
            str(item) for item in selected.get("anomaly_reasons") or ()
        ),
        source_sample_ids=tuple(
            str(item) for item in selected.get("source_sample_ids") or ()
        ),
        computed_at=str(selected["computed_at"]),
        **number_fields,
    )


def variant_comparison(value: Any) -> VariantComparison:
    selected = mapping(value, "variant comparison")
    optional_numbers = {
        name: (
            None if selected.get(name) is None else float(selected[name])
        )
        for name in (
            "baseline_p50",
            "compared_p50",
            "absolute_delta",
            "relative_delta",
        )
    }
    return VariantComparison(
        metric=str(selected["metric"]),
        unit=str(selected["unit"]),
        baseline_variant_id=str(selected["baseline_variant_id"]),
        compared_variant_id=str(selected["compared_variant_id"]),
        effect_direction=str(selected["effect_direction"]),
        better=(
            selected.get("better")
            if isinstance(selected.get("better"), bool)
            else None
        ),
        comparable=selected.get("comparable") is True,
        reason=str(selected.get("reason") or ""),
        baseline_summary_digest=str(selected["baseline_summary_digest"]),
        compared_summary_digest=str(selected["compared_summary_digest"]),
        **optional_numbers,
    )
