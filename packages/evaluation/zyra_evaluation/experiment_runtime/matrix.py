from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from .canonical import (
    bounded_integer,
    bounded_text,
    digest,
    identity,
    new_identity,
    require_digest,
    require_commit_digest,
    stable_unique,
    string_map,
    utc_now,
)
from .errors import invalid
from .models import (
    AblationKind,
    BaselineKind,
    BudgetEnvelope,
    CapabilityVector,
    ComparisonEnvelope,
    FailureScheduleEnvelope,
    HardwareEnvelope,
    MatrixCell,
    ProviderEnvelope,
    VariantDefinition,
    VariantKind,
    VerifierEnvelope,
)


REQUIRED_BASELINES = tuple(item.value for item in BaselineKind)
REQUIRED_ABLATIONS = tuple(item.value for item in AblationKind)
REQUIRED_VARIANTS = REQUIRED_BASELINES + REQUIRED_ABLATIONS


def default_variants() -> tuple[VariantDefinition, ...]:
    dynamic = CapabilityVector(
        scheduler=True,
        memory_compact=True,
        recovery=True,
        low_entropy_communication=True,
        dynamic_topology=True,
        heterogeneous_roles=True,
        worker_limit=8,
        communication_mode="targeted",
    )
    return (
        VariantDefinition(
            variant_id=BaselineKind.SINGLE_AGENT.value,
            kind=VariantKind.BASELINE,
            title="Single agent baseline",
            description=(
                "A single durable worker processes the same task, budget, verifier "
                "and failure schedule without inter-worker routing."
            ),
            capabilities=CapabilityVector(
                scheduler=False,
                memory_compact=True,
                recovery=True,
                low_entropy_communication=True,
                dynamic_topology=False,
                heterogeneous_roles=False,
                worker_limit=1,
                communication_mode="self",
            ),
            comparison_anchor=BaselineKind.DYNAMIC_HETEROGENEOUS_SWARM.value,
            metadata={
                "topology": "single_node",
                "formal_baseline": True,
                "one_capability_ablation": False,
            },
        ),
        VariantDefinition(
            variant_id=BaselineKind.STATIC_FULL_CONNECT_MULTI_AGENT.value,
            kind=VariantKind.BASELINE,
            title="Static full-connect multi-agent baseline",
            description=(
                "A fixed worker set broadcasts observations over a complete graph "
                "while preserving the same task and owner receipts."
            ),
            capabilities=CapabilityVector(
                scheduler=True,
                memory_compact=True,
                recovery=True,
                low_entropy_communication=False,
                dynamic_topology=False,
                heterogeneous_roles=True,
                worker_limit=8,
                communication_mode="broadcast",
            ),
            comparison_anchor=BaselineKind.DYNAMIC_HETEROGENEOUS_SWARM.value,
            metadata={
                "topology": "static_complete_graph",
                "formal_baseline": True,
                "one_capability_ablation": False,
            },
        ),
        VariantDefinition(
            variant_id=BaselineKind.DYNAMIC_HETEROGENEOUS_SWARM.value,
            kind=VariantKind.BASELINE,
            title="Dynamic heterogeneous swarm",
            description=(
                "The production algorithm dynamically selects heterogeneous roles, "
                "sparse routes, memory and deterministic recovery."
            ),
            capabilities=dynamic,
            comparison_anchor=BaselineKind.DYNAMIC_HETEROGENEOUS_SWARM.value,
            metadata={
                "topology": "runtime_evolvable_sparse",
                "formal_baseline": True,
                "production_candidate": True,
            },
        ),
        VariantDefinition(
            variant_id=AblationKind.NO_SCHEDULER.value,
            kind=VariantKind.ABLATION,
            title="Scheduler ablation",
            description=(
                "Disables resource-aware route selection while retaining every "
                "other dynamic-swarm capability."
            ),
            capabilities=CapabilityVector(
                scheduler=False,
                memory_compact=True,
                recovery=True,
                low_entropy_communication=True,
                dynamic_topology=True,
                heterogeneous_roles=True,
                worker_limit=8,
                communication_mode="targeted",
            ),
            comparison_anchor=BaselineKind.DYNAMIC_HETEROGENEOUS_SWARM.value,
            expected_disabled_capability="scheduler",
            metadata={"formal_ablation": True},
        ),
        VariantDefinition(
            variant_id=AblationKind.NO_MEMORY_COMPACT.value,
            kind=VariantKind.ABLATION,
            title="Memory and compact ablation",
            description=(
                "Disables memory retrieval, compaction and restore while retaining "
                "scheduler, recovery and targeted communication."
            ),
            capabilities=CapabilityVector(
                scheduler=True,
                memory_compact=False,
                recovery=True,
                low_entropy_communication=True,
                dynamic_topology=True,
                heterogeneous_roles=True,
                worker_limit=8,
                communication_mode="targeted",
            ),
            comparison_anchor=BaselineKind.DYNAMIC_HETEROGENEOUS_SWARM.value,
            expected_disabled_capability="memory_compact",
            metadata={"formal_ablation": True},
        ),
        VariantDefinition(
            variant_id=AblationKind.NO_RECOVERY.value,
            kind=VariantKind.ABLATION,
            title="Recovery ablation",
            description=(
                "Disables recovery planning and resume while preserving scheduler, "
                "memory and targeted communication."
            ),
            capabilities=CapabilityVector(
                scheduler=True,
                memory_compact=True,
                recovery=False,
                low_entropy_communication=True,
                dynamic_topology=True,
                heterogeneous_roles=True,
                worker_limit=8,
                communication_mode="targeted",
            ),
            comparison_anchor=BaselineKind.DYNAMIC_HETEROGENEOUS_SWARM.value,
            expected_disabled_capability="recovery",
            metadata={"formal_ablation": True},
        ),
        VariantDefinition(
            variant_id=AblationKind.NO_LOW_ENTROPY_COMMUNICATION.value,
            kind=VariantKind.ABLATION,
            title="Low-entropy communication ablation",
            description=(
                "Replaces targeted evidence routing with broadcast communication "
                "while preserving scheduler, memory and recovery."
            ),
            capabilities=CapabilityVector(
                scheduler=True,
                memory_compact=True,
                recovery=True,
                low_entropy_communication=False,
                dynamic_topology=True,
                heterogeneous_roles=True,
                worker_limit=8,
                communication_mode="broadcast",
            ),
            comparison_anchor=BaselineKind.DYNAMIC_HETEROGENEOUS_SWARM.value,
            expected_disabled_capability="low_entropy_communication",
            metadata={"formal_ablation": True},
        ),
    )


class VariantCatalog:
    def __init__(self, variants: Iterable[VariantDefinition] | None = None) -> None:
        selected = tuple(variants or default_variants())
        if not selected:
            raise invalid(
                "experiment_variants_empty",
                "Experiment matrix must contain variants.",
            )
        self._variants: dict[str, VariantDefinition] = {}
        for variant in selected:
            if variant.variant_id in self._variants:
                raise invalid(
                    "experiment_variant_duplicate",
                    "Experiment matrix contains a duplicate variant.",
                    detail={"variant_id": variant.variant_id},
                )
            identity(variant.variant_id, "variant id")
            self._variants[variant.variant_id] = variant
        self.require_formal_matrix()

    def list(self) -> tuple[VariantDefinition, ...]:
        return tuple(self._variants.values())

    def require(self, variant_id: str) -> VariantDefinition:
        selected = identity(variant_id, "variant id")
        try:
            return self._variants[selected]
        except KeyError as error:
            raise invalid(
                "experiment_variant_unknown",
                "Experiment variant is not registered.",
                detail={"variant_id": selected},
            ) from error

    def require_formal_matrix(self) -> dict[str, Any]:
        missing = sorted(set(REQUIRED_VARIANTS) - set(self._variants))
        extra_required = sorted(
            variant.variant_id
            for variant in self._variants.values()
            if variant.required and variant.variant_id not in REQUIRED_VARIANTS
        )
        wrong_kind: list[str] = []
        for baseline in REQUIRED_BASELINES:
            if (
                baseline in self._variants
                and self._variants[baseline].kind is not VariantKind.BASELINE
            ):
                wrong_kind.append(baseline)
        for ablation in REQUIRED_ABLATIONS:
            if (
                ablation in self._variants
                and self._variants[ablation].kind is not VariantKind.ABLATION
            ):
                wrong_kind.append(ablation)
        if missing or wrong_kind:
            raise invalid(
                "experiment_formal_matrix_invalid",
                "Formal baseline and ablation matrix is incomplete.",
                detail={
                    "missing": missing,
                    "wrong_kind": sorted(wrong_kind),
                    "extra_required": extra_required,
                },
            )
        dynamic = self._variants[BaselineKind.DYNAMIC_HETEROGENEOUS_SWARM.value]
        ablation_findings: list[dict[str, Any]] = []
        for name in REQUIRED_ABLATIONS:
            variant = self._variants[name]
            changed = variant.capabilities.changed(dynamic.capabilities)
            expected = variant.expected_disabled_capability
            if changed != (expected,):
                ablation_findings.append(
                    {
                        "variant_id": name,
                        "expected": expected,
                        "changed": list(changed),
                    }
                )
                continue
            if getattr(dynamic.capabilities, expected) is not True:
                ablation_findings.append(
                    {
                        "variant_id": name,
                        "expected": expected,
                        "reason": "anchor capability is not enabled",
                    }
                )
            if getattr(variant.capabilities, expected) is not False:
                ablation_findings.append(
                    {
                        "variant_id": name,
                        "expected": expected,
                        "reason": "ablation capability is not disabled",
                    }
                )
        if ablation_findings:
            raise invalid(
                "experiment_ablation_not_isolated",
                "Each formal ablation must change exactly one capability.",
                detail={"findings": ablation_findings},
            )
        receipt = {
            "schema": "zyra.experiment-variant-catalog-verification/v1",
            "valid": True,
            "baseline_count": len(REQUIRED_BASELINES),
            "ablation_count": len(REQUIRED_ABLATIONS),
            "required_variant_ids": list(REQUIRED_VARIANTS),
            "catalog_digest": digest(
                [item.to_dict() for item in self._variants.values()]
            ),
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt


def build_envelope(request: Mapping[str, Any]) -> ComparisonEnvelope:
    budget_value = _mapping(request.get("budget"), "budget")
    hardware_value = _mapping(request.get("hardware"), "hardware")
    provider_value = _mapping(request.get("provider"), "provider")
    verifier_value = _mapping(request.get("verifier"), "verifier")
    failure_value = _mapping(request.get("failure_schedule"), "failure schedule")
    seeds_value = request.get("seeds")
    if not isinstance(seeds_value, (list, tuple)):
        raise invalid(
            "experiment_seeds_invalid",
            "Experiment seeds must be an array.",
        )
    seeds = tuple(
        bounded_integer(
            item,
            f"seed {index}",
            minimum=0,
            maximum=2**63 - 1,
        )
        for index, item in enumerate(seeds_value)
    )
    if len(seeds) < 3 or len(seeds) > 100:
        raise invalid(
            "experiment_seed_count_invalid",
            "Formal experiments require between 3 and 100 repetitions.",
            detail={"count": len(seeds)},
        )
    if len(set(seeds)) != len(seeds):
        raise invalid(
            "experiment_seed_duplicate",
            "Experiment seeds must be unique.",
        )
    budget = BudgetEnvelope(
        maximum_effective_steps=bounded_integer(
            budget_value.get("maximum_effective_steps"),
            "maximum effective steps",
            minimum=1,
            maximum=10_000_000,
        ),
        maximum_wall_time_ms=bounded_integer(
            budget_value.get("maximum_wall_time_ms"),
            "maximum wall time",
            minimum=1,
            maximum=7 * 24 * 60 * 60 * 1000,
        ),
        maximum_token_units=bounded_integer(
            budget_value.get("maximum_token_units"),
            "maximum token units",
            minimum=0,
            maximum=10**12,
        ),
        maximum_cost_microunits=bounded_integer(
            budget_value.get("maximum_cost_microunits"),
            "maximum cost microunits",
            minimum=0,
            maximum=10**15,
        ),
        maximum_artifact_bytes=bounded_integer(
            budget_value.get("maximum_artifact_bytes"),
            "maximum artifact bytes",
            minimum=1,
            maximum=2**50,
        ),
        maximum_fault_retries=bounded_integer(
            budget_value.get("maximum_fault_retries"),
            "maximum fault retries",
            minimum=0,
            maximum=1_000_000,
        ),
        concurrency=bounded_integer(
            budget_value.get("concurrency"),
            "experiment concurrency",
            minimum=1,
            maximum=128,
        ),
    )
    hardware = HardwareEnvelope(
        profile_id=identity(hardware_value.get("profile_id"), "hardware profile id"),
        os_family=bounded_text(
            hardware_value.get("os_family"),
            "hardware OS family",
            maximum_bytes=128,
        ),
        architecture=bounded_text(
            hardware_value.get("architecture"),
            "hardware architecture",
            maximum_bytes=128,
        ),
        cpu_class=bounded_text(
            hardware_value.get("cpu_class"),
            "CPU class",
            maximum_bytes=256,
        ),
        logical_cpu_count=bounded_integer(
            hardware_value.get("logical_cpu_count"),
            "logical CPU count",
            minimum=1,
            maximum=65_536,
        ),
        memory_limit_bytes=bounded_integer(
            hardware_value.get("memory_limit_bytes"),
            "memory limit",
            minimum=1,
            maximum=2**60,
        ),
        edge_isolation_kind=bounded_text(
            hardware_value.get("edge_isolation_kind"),
            "edge isolation kind",
            maximum_bytes=256,
        ),
        cloud_execution_allowed=hardware_value.get("cloud_execution_allowed") is True,
        accelerator=str(hardware_value.get("accelerator") or "").strip(),
        metadata=dict(hardware_value.get("metadata") or {}),
    )
    providers = _string_sequence(
        provider_value.get("allowed_provider_ids"),
        "allowed provider ids",
    )
    models = _string_sequence(
        provider_value.get("allowed_model_ids"),
        "allowed model ids",
    )
    prior_receipts = _string_sequence(
        provider_value.get("prior_verified_receipt_ids") or (),
        "prior verified receipt ids",
    )
    provider = ProviderEnvelope(
        policy_id=identity(provider_value.get("policy_id"), "provider policy id"),
        policy_digest=require_digest(
            provider_value.get("policy_digest"),
            "provider policy digest",
        ),
        provider_catalog_digest=require_digest(
            provider_value.get("provider_catalog_digest"),
            "provider catalog digest",
        ),
        allowed_provider_ids=providers,
        allowed_model_ids=models,
        authenticated_provider_cli_allowed=(
            provider_value.get("authenticated_provider_cli_allowed") is True
        ),
        external_model_request_allowed=(
            provider_value.get("external_model_request_allowed") is True
        ),
        credential_presence_digest=require_digest(
            provider_value.get("credential_presence_digest"),
            "credential presence digest",
        ),
        prior_verified_receipt_ids=prior_receipts,
        metadata=dict(provider_value.get("metadata") or {}),
    )
    if provider.authenticated_provider_cli_allowed:
        raise invalid(
            "experiment_authenticated_cli_forbidden",
            "M2-S05-03 forbids authenticated provider/model CLI execution.",
            phase="policy",
        )
    if provider.external_model_request_allowed:
        raise invalid(
            "experiment_external_model_request_forbidden",
            "M2-S05-03 forbids new external model requests.",
            phase="policy",
        )
    verifier = VerifierEnvelope(
        verifier_id=identity(verifier_value.get("verifier_id"), "verifier id"),
        version=bounded_text(
            verifier_value.get("version"),
            "verifier version",
            maximum_bytes=128,
        ),
        implementation_digest=require_digest(
            verifier_value.get("implementation_digest"),
            "verifier implementation digest",
        ),
        rules_digest=require_digest(
            verifier_value.get("rules_digest"),
            "verifier rules digest",
        ),
        required_checks=_string_sequence(
            verifier_value.get("required_checks"),
            "required verifier checks",
        ),
        fail_closed=verifier_value.get("fail_closed") is True,
    )
    if not verifier.fail_closed:
        raise invalid(
            "experiment_verifier_not_fail_closed",
            "Formal experiment verifier must fail closed.",
        )
    failure = FailureScheduleEnvelope(
        schedule_id=identity(
            failure_value.get("schedule_id"),
            "failure schedule id",
        ),
        schedule_digest=require_digest(
            failure_value.get("schedule_digest"),
            "failure schedule digest",
        ),
        fault_kinds=_string_sequence(
            failure_value.get("fault_kinds"),
            "fault kinds",
        ),
        requirement_change_ids=_string_sequence(
            failure_value.get("requirement_change_ids"),
            "requirement change ids",
        ),
        injection_offsets=tuple(
            bounded_integer(
                item,
                f"failure injection offset {index}",
                minimum=0,
                maximum=budget.maximum_effective_steps,
            )
            for index, item in enumerate(
                _sequence(
                    failure_value.get("injection_offsets"),
                    "failure injection offsets",
                )
            )
        ),
        deterministic=failure_value.get("deterministic") is True,
        metadata=dict(failure_value.get("metadata") or {}),
    )
    if not failure.deterministic:
        raise invalid(
            "experiment_failure_schedule_nondeterministic",
            "Formal experiment failure schedule must be deterministic.",
        )
    envelope = ComparisonEnvelope(
        envelope_id=new_identity("envelope"),
        scenario_id=identity(request.get("scenario_id"), "scenario id"),
        scenario_definition_digest=require_digest(
            request.get("scenario_definition_digest"),
            "scenario definition digest",
        ),
        task_input_digest=require_digest(
            request.get("task_input_digest"),
            "task input digest",
        ),
        task_input_bytes=bounded_integer(
            request.get("task_input_bytes"),
            "task input bytes",
            minimum=1,
            maximum=1024 * 1024 * 1024,
        ),
        task_domain=bounded_text(
            request.get("task_domain"),
            "task domain",
            maximum_bytes=256,
        ),
        commit_sha=require_commit_digest(request.get("commit_sha"), "commit SHA"),
        environment_digest=require_digest(
            request.get("environment_digest"),
            "environment digest",
        ),
        source_evidence_digest=require_digest(
            request.get("source_evidence_digest"),
            "source evidence digest",
        ),
        sealed_policy_digest=require_digest(
            request.get("sealed_policy_digest"),
            "sealed policy digest",
        ),
        budget=budget,
        hardware=hardware,
        provider=provider,
        verifier=verifier,
        failure_schedule=failure,
        seed_plan=seeds,
        created_at=utc_now(),
        labels=string_map(request.get("labels"), "experiment labels"),
        metadata=dict(request.get("metadata") or {}),
    )
    verify_envelope(envelope)
    return envelope


def verify_envelope(envelope: ComparisonEnvelope) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    if len(envelope.seed_plan) < 3:
        findings.append(
            {
                "code": "seed_count_insufficient",
                "count": len(envelope.seed_plan),
            }
        )
    if not envelope.failure_schedule.fault_kinds:
        findings.append({"code": "failure_schedule_empty"})
    if not envelope.failure_schedule.requirement_change_ids:
        findings.append({"code": "requirement_change_schedule_empty"})
    if not envelope.verifier.required_checks:
        findings.append({"code": "verifier_checks_empty"})
    if envelope.provider.authenticated_provider_cli_allowed:
        findings.append({"code": "authenticated_provider_cli_allowed"})
    if envelope.provider.external_model_request_allowed:
        findings.append({"code": "external_model_request_allowed"})
    if envelope.budget.concurrency > envelope.hardware.logical_cpu_count * 8:
        findings.append(
            {
                "code": "concurrency_hardware_mismatch",
                "concurrency": envelope.budget.concurrency,
                "logical_cpu_count": envelope.hardware.logical_cpu_count,
            }
        )
    if findings:
        raise invalid(
            "experiment_comparison_envelope_invalid",
            "Experiment comparison envelope failed formal verification.",
            detail={"findings": findings},
        )
    receipt = {
        "schema": "zyra.experiment-envelope-verification/v1",
        "valid": True,
        "envelope_id": envelope.envelope_id,
        "envelope_digest": envelope.envelope_digest,
        "seed_count": len(envelope.seed_plan),
        "condition_digests": {
            "budget": envelope.budget.budget_digest,
            "hardware": envelope.hardware.hardware_digest,
            "provider": digest(envelope.provider.to_dict()),
            "verifier": envelope.verifier.verifier_digest,
            "failure_schedule": envelope.failure_schedule.schedule_digest,
            "task_input": envelope.task_input_digest,
            "environment": envelope.environment_digest,
            "source_evidence": envelope.source_evidence_digest,
        },
        "verified_at": utc_now(),
    }
    receipt["receipt_digest"] = digest(receipt)
    return receipt


def plan_cells(
    envelope: ComparisonEnvelope,
    catalog: VariantCatalog,
) -> tuple[MatrixCell, ...]:
    cells: list[MatrixCell] = []
    for variant in catalog.list():
        if not variant.required:
            continue
        for repetition, seed in enumerate(envelope.seed_plan, start=1):
            cells.append(
                MatrixCell.plan(
                    variant_id=variant.variant_id,
                    repetition=repetition,
                    seed=seed,
                    envelope_digest=envelope.envelope_digest,
                )
            )
    return tuple(cells)


def verify_cell_plan(
    cells: Iterable[MatrixCell],
    *,
    envelope: ComparisonEnvelope,
    catalog: VariantCatalog,
) -> dict[str, Any]:
    selected = tuple(cells)
    expected_pairs = {
        (variant.variant_id, repetition)
        for variant in catalog.list()
        if variant.required
        for repetition in range(1, len(envelope.seed_plan) + 1)
    }
    actual_pairs = {(item.variant_id, item.repetition) for item in selected}
    counts = Counter((item.variant_id, item.repetition) for item in selected)
    duplicates = sorted(
        f"{variant}:{repetition}"
        for (variant, repetition), count in counts.items()
        if count > 1
    )
    missing = sorted(f"{variant}:{repetition}" for variant, repetition in expected_pairs - actual_pairs)
    unexpected = sorted(f"{variant}:{repetition}" for variant, repetition in actual_pairs - expected_pairs)
    envelope_mismatch = sorted(
        item.cell_id
        for item in selected
        if item.envelope_digest != envelope.envelope_digest
    )
    seed_mismatch = sorted(
        item.cell_id
        for item in selected
        if (
            item.repetition < 1
            or item.repetition > len(envelope.seed_plan)
            or item.seed != envelope.seed_plan[item.repetition - 1]
        )
    )
    if missing or unexpected or duplicates or envelope_mismatch or seed_mismatch:
        raise invalid(
            "experiment_cell_plan_invalid",
            "Experiment cell plan is not a complete Cartesian matrix.",
            detail={
                "missing": missing,
                "unexpected": unexpected,
                "duplicates": duplicates,
                "envelope_mismatch": envelope_mismatch,
                "seed_mismatch": seed_mismatch,
            },
        )
    receipt = {
        "schema": "zyra.experiment-cell-plan-verification/v1",
        "valid": True,
        "cell_count": len(selected),
        "variant_count": len(catalog.list()),
        "repetition_count": len(envelope.seed_plan),
        "cell_plan_digest": digest([item.to_dict() for item in selected]),
        "verified_at": utc_now(),
    }
    receipt["receipt_digest"] = digest(receipt)
    return receipt


def comparison_condition_digest(
    envelope: ComparisonEnvelope,
    *,
    exclude: Iterable[str] = (),
) -> str:
    removed = set(exclude)
    value = envelope.to_dict(include_digest=False)
    for key in removed:
        value.pop(key, None)
    return digest(value)


def assert_same_conditions(
    expected: ComparisonEnvelope,
    observed: Mapping[str, Any],
    *,
    allowed_variant_fields: Iterable[str] = ("capabilities", "variant_id"),
) -> dict[str, Any]:
    allowed = set(allowed_variant_fields)
    expected_value = expected.to_dict()
    findings: list[dict[str, Any]] = []
    observed_digest = str(observed.get("envelope_digest") or "")
    if observed_digest != expected.envelope_digest:
        findings.append(
            {
                "field": "envelope_digest",
                "expected": expected.envelope_digest,
                "observed": observed_digest,
            }
        )
    condition_keys = (
        "scenario_id",
        "scenario_definition_digest",
        "task_input_digest",
        "commit_sha",
        "environment_digest",
        "source_evidence_digest",
        "sealed_policy_digest",
    )
    for key in condition_keys:
        if key in allowed:
            continue
        if key in observed and observed[key] != expected_value[key]:
            findings.append(
                {
                    "field": key,
                    "expected": expected_value[key],
                    "observed": observed[key],
                }
            )
    nested_digests = {
        "budget_digest": expected.budget.budget_digest,
        "hardware_digest": expected.hardware.hardware_digest,
        "provider_digest": digest(expected.provider.to_dict()),
        "verifier_digest": expected.verifier.verifier_digest,
        "failure_schedule_digest": expected.failure_schedule.schedule_digest,
    }
    for key, value in nested_digests.items():
        if key in observed and str(observed[key]) != value:
            findings.append(
                {"field": key, "expected": value, "observed": observed[key]}
            )
    if findings:
        raise invalid(
            "experiment_conditions_inconsistent",
            "Experiment cell changed a protected comparison condition.",
            phase="execution",
            detail={"findings": findings},
        )
    receipt = {
        "schema": "zyra.experiment-condition-verification/v1",
        "valid": True,
        "envelope_digest": expected.envelope_digest,
        "condition_digests": nested_digests,
        "allowed_variant_fields": sorted(allowed),
        "verified_at": utc_now(),
    }
    receipt["receipt_digest"] = digest(receipt)
    return receipt


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise invalid(
            "experiment_mapping_invalid",
            f"{label} must be an object.",
        )
    return value


def _sequence(value: Any, label: str) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise invalid(
            "experiment_sequence_invalid",
            f"{label} must be an array.",
        )
    return tuple(value)


def _string_sequence(value: Any, label: str) -> tuple[str, ...]:
    selected = _sequence(value, label)
    output = stable_unique(
        bounded_text(item, f"{label} item", maximum_bytes=1024)
        for item in selected
    )
    if not output:
        raise invalid(
            "experiment_sequence_empty",
            f"{label} must not be empty.",
        )
    return output
