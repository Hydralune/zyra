from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from .canonical import (
    bounded_integer,
    bounded_text,
    digest,
    identity,
    invalid,
    new_identity,
    require_commit,
    require_digest,
    sequence,
    utc_now,
)
from .models import (
    BenchmarkCell,
    Campaign,
    CampaignConditions,
    CampaignPhase,
    CapabilityVector,
    DomainKind,
    Variant,
    VariantKind,
)


DYNAMIC_VARIANT = "dynamic-heterogeneous-swarm"
REQUIRED_VARIANT_IDS = (
    "single-agent",
    "static-full-connect-multi-agent",
    DYNAMIC_VARIANT,
    "no-scheduler",
    "no-memory-compact",
    "no-recovery",
    "no-low-entropy-communication",
)
REQUIRED_DOMAINS = (
    DomainKind.SOFTWARE_DELIVERY,
    DomainKind.CROSS_SOURCE_RESEARCH,
)


def default_variants() -> tuple[Variant, ...]:
    dynamic = CapabilityVector(
        scheduler=True,
        memory_compact=True,
        recovery=True,
        low_entropy=True,
        dynamic_topology=True,
        heterogeneous_roles=True,
        worker_limit=8,
        communication_mode="targeted",
    )
    return (
        Variant(
            variant_id="single-agent",
            kind=VariantKind.BASELINE,
            title="Single agent",
            capabilities=CapabilityVector(
                scheduler=False,
                memory_compact=True,
                recovery=True,
                low_entropy=True,
                dynamic_topology=False,
                heterogeneous_roles=False,
                worker_limit=1,
                communication_mode="self",
            ),
            comparison_anchor=DYNAMIC_VARIANT,
            metadata={"topology": "single-node"},
        ),
        Variant(
            variant_id="static-full-connect-multi-agent",
            kind=VariantKind.BASELINE,
            title="Static full-connect multi-agent",
            capabilities=CapabilityVector(
                scheduler=True,
                memory_compact=True,
                recovery=True,
                low_entropy=False,
                dynamic_topology=False,
                heterogeneous_roles=True,
                worker_limit=8,
                communication_mode="broadcast",
            ),
            comparison_anchor=DYNAMIC_VARIANT,
            metadata={"topology": "static-complete-graph"},
        ),
        Variant(
            variant_id=DYNAMIC_VARIANT,
            kind=VariantKind.BASELINE,
            title="Dynamic heterogeneous swarm",
            capabilities=dynamic,
            comparison_anchor=DYNAMIC_VARIANT,
            metadata={"topology": "runtime-evolvable-sparse", "production": True},
        ),
        Variant(
            variant_id="no-scheduler",
            kind=VariantKind.ABLATION,
            title="No scheduler",
            capabilities=replace(dynamic, scheduler=False),
            comparison_anchor=DYNAMIC_VARIANT,
            expected_disabled_capability="scheduler",
        ),
        Variant(
            variant_id="no-memory-compact",
            kind=VariantKind.ABLATION,
            title="No memory/compact",
            capabilities=replace(dynamic, memory_compact=False),
            comparison_anchor=DYNAMIC_VARIANT,
            expected_disabled_capability="memory_compact",
        ),
        Variant(
            variant_id="no-recovery",
            kind=VariantKind.ABLATION,
            title="No recovery",
            capabilities=replace(dynamic, recovery=False),
            comparison_anchor=DYNAMIC_VARIANT,
            expected_disabled_capability="recovery",
        ),
        Variant(
            variant_id="no-low-entropy-communication",
            kind=VariantKind.ABLATION,
            title="No low-entropy communication",
            capabilities=replace(
                dynamic,
                low_entropy=False,
                communication_mode="broadcast",
            ),
            comparison_anchor=DYNAMIC_VARIANT,
            expected_disabled_capability="low_entropy",
        ),
    )


def build_conditions(value: Mapping[str, Any]) -> CampaignConditions:
    return CampaignConditions(
        commit_sha=require_commit(value.get("commit_sha")),
        sealed_policy_digest=require_digest(
            value.get("sealed_policy_digest"),
            "sealed policy digest",
        ),
        environment_digest=require_digest(
            value.get("environment_digest"),
            "environment digest",
        ),
        hardware_digest=require_digest(
            value.get("hardware_digest"),
            "hardware digest",
        ),
        deployment_digest=require_digest(
            value.get("deployment_digest"),
            "deployment digest",
        ),
        provider_policy_digest=require_digest(
            value.get("provider_policy_digest"),
            "provider policy digest",
        ),
        verifier_digest=require_digest(
            value.get("verifier_digest"),
            "verifier digest",
        ),
        failure_schedule_digest=require_digest(
            value.get("failure_schedule_digest"),
            "failure schedule digest",
        ),
        budget_digest=require_digest(
            value.get("budget_digest"),
            "budget digest",
        ),
        source_evidence_digest=require_digest(
            value.get("source_evidence_digest"),
            "source evidence digest",
        ),
    )


def validate_variants(variants: Iterable[Variant]) -> dict[str, Any]:
    selected = tuple(variants)
    identifiers = [item.variant_id for item in selected]
    duplicate_ids = sorted(
        item for item, count in Counter(identifiers).items() if count > 1
    )
    missing = sorted(set(REQUIRED_VARIANT_IDS) - set(identifiers))
    unexpected = sorted(set(identifiers) - set(REQUIRED_VARIANT_IDS))
    findings: list[dict[str, Any]] = []
    if duplicate_ids:
        findings.append({"code": "variant-duplicate", "variant_ids": duplicate_ids})
    if missing:
        findings.append({"code": "variant-missing", "variant_ids": missing})
    if unexpected:
        findings.append({"code": "variant-unexpected", "variant_ids": unexpected})
    by_id = {item.variant_id: item for item in selected}
    anchor = by_id.get(DYNAMIC_VARIANT)
    if anchor is None:
        findings.append({"code": "dynamic-anchor-missing"})
    else:
        for variant in selected:
            if variant.comparison_anchor != DYNAMIC_VARIANT:
                findings.append(
                    {
                        "code": "comparison-anchor-invalid",
                        "variant_id": variant.variant_id,
                        "observed": variant.comparison_anchor,
                    }
                )
            if variant.kind is not VariantKind.ABLATION:
                continue
            differences = capability_differences(
                anchor.capabilities,
                variant.capabilities,
            )
            expected = variant.expected_disabled_capability
            accepted = {expected}
            if expected == "low_entropy":
                accepted.add("communication_mode")
            if set(differences) != accepted:
                findings.append(
                    {
                        "code": "ablation-not-isolated",
                        "variant_id": variant.variant_id,
                        "expected": sorted(accepted),
                        "observed": list(differences),
                    }
                )
            changed_value = getattr(variant.capabilities, expected, None)
            if changed_value is not False:
                findings.append(
                    {
                        "code": "ablation-capability-not-disabled",
                        "variant_id": variant.variant_id,
                        "capability": expected,
                    }
                )
    if findings:
        raise invalid(
            "benchmark_variant_catalog_invalid",
            "Benchmark variant catalog is incomplete or unfair.",
            phase="plan",
            detail={"findings": findings},
        )
    receipt = {
        "schema": "zyra.live-benchmark-variant-verification/v1",
        "valid": True,
        "variant_ids": identifiers,
        "variant_digest": digest([item.to_dict() for item in selected]),
        "ablation_count": sum(
            item.kind is VariantKind.ABLATION for item in selected
        ),
        "verified_at": utc_now(),
    }
    receipt["receipt_digest"] = digest(receipt)
    return receipt


def capability_differences(
    expected: CapabilityVector,
    observed: CapabilityVector,
) -> tuple[str, ...]:
    left = expected.to_dict()
    right = observed.to_dict()
    return tuple(
        key
        for key in sorted(left)
        if left[key] != right[key]
    )


def create_campaign(request: Mapping[str, Any]) -> Campaign:
    campaign_id = optional_campaign_id(request.get("campaign_id"))
    conditions = build_conditions(request)
    seeds = parse_seeds(request.get("seeds"))
    domains = parse_domains(request.get("domains"))
    variants = default_variants()
    validate_variants(variants)
    input_revisions = parse_input_revisions(
        request.get("input_revisions"),
        domains=domains,
        repetition_count=len(seeds),
    )
    task_families = parse_task_families(request.get("task_family_digests"), domains)
    created_at = utc_now()
    cells: list[BenchmarkCell] = []
    for domain in domains:
        for variant in variants:
            for repetition, seed in enumerate(seeds, start=1):
                cell_key = {
                    "campaign_id": campaign_id,
                    "domain": domain.value,
                    "variant_id": variant.variant_id,
                    "repetition": repetition,
                    "seed": seed,
                    "input_revision": input_revisions[domain][repetition - 1],
                    "condition_digest": conditions.condition_digest,
                }
                cells.append(
                    BenchmarkCell(
                        cell_id=f"cell-{digest(cell_key)[:24]}",
                        campaign_id=campaign_id,
                        domain=domain,
                        variant_id=variant.variant_id,
                        repetition=repetition,
                        seed=seed,
                        input_revision=input_revisions[domain][repetition - 1],
                        task_family_digest=task_families[domain],
                        condition_digest=conditions.condition_digest,
                        planned_at=created_at,
                    )
                )
    campaign = Campaign(
        campaign_id=campaign_id,
        schema_version="zyra.live-benchmark-campaign/v1",
        phase=CampaignPhase.PLANNED,
        conditions=conditions,
        domains=domains,
        variants=variants,
        seeds=seeds,
        cells=tuple(cells),
        created_at=created_at,
        updated_at=created_at,
        minimum_effective_steps=bounded_integer(
            request.get("minimum_effective_steps", 1),
            "minimum effective steps",
            minimum=1,
            maximum=10_000_000,
        ),
        required_long_run_steps=bounded_integer(
            request.get("required_long_run_steps", 2_000),
            "required long run steps",
            minimum=2_000,
            maximum=10_000_000,
        ),
        metadata=dict(request.get("metadata") or {}),
    )
    verify_campaign_plan(campaign)
    return campaign


def verify_campaign_plan(campaign: Campaign) -> dict[str, Any]:
    validate_variants(campaign.variants)
    findings: list[dict[str, Any]] = []
    if tuple(campaign.domains) != REQUIRED_DOMAINS:
        findings.append(
            {
                "code": "domain-plan-invalid",
                "expected": [item.value for item in REQUIRED_DOMAINS],
                "observed": [item.value for item in campaign.domains],
            }
        )
    if len(campaign.seeds) < 3:
        findings.append({"code": "repetition-count-insufficient"})
    expected = {
        (domain.value, variant.variant_id, repetition)
        for domain in campaign.domains
        for variant in campaign.variants
        for repetition in range(1, len(campaign.seeds) + 1)
    }
    actual = {
        (cell.domain.value, cell.variant_id, cell.repetition)
        for cell in campaign.cells
    }
    counts = Counter(
        (cell.domain.value, cell.variant_id, cell.repetition)
        for cell in campaign.cells
    )
    duplicates = sorted(
        f"{domain}:{variant}:{repetition}"
        for (domain, variant, repetition), count in counts.items()
        if count > 1
    )
    missing = sorted(
        f"{domain}:{variant}:{repetition}"
        for domain, variant, repetition in expected - actual
    )
    unexpected = sorted(
        f"{domain}:{variant}:{repetition}"
        for domain, variant, repetition in actual - expected
    )
    if duplicates:
        findings.append({"code": "cell-duplicate", "cells": duplicates})
    if missing:
        findings.append({"code": "cell-missing", "cells": missing})
    if unexpected:
        findings.append({"code": "cell-unexpected", "cells": unexpected})
    identifiers = [item.cell_id for item in campaign.cells]
    duplicate_identifiers = sorted(
        item for item, count in Counter(identifiers).items() if count > 1
    )
    if duplicate_identifiers:
        findings.append(
            {"code": "cell-identity-duplicate", "cell_ids": duplicate_identifiers}
        )
    input_findings = verify_paired_inputs(campaign)
    findings.extend(input_findings)
    for cell in campaign.cells:
        if cell.campaign_id != campaign.campaign_id:
            findings.append(
                {"code": "cell-campaign-mismatch", "cell_id": cell.cell_id}
            )
        if cell.condition_digest != campaign.conditions.condition_digest:
            findings.append(
                {"code": "cell-condition-mismatch", "cell_id": cell.cell_id}
            )
        expected_seed = campaign.seeds[cell.repetition - 1]
        if cell.seed != expected_seed:
            findings.append(
                {
                    "code": "cell-seed-mismatch",
                    "cell_id": cell.cell_id,
                    "expected": expected_seed,
                    "observed": cell.seed,
                }
            )
    if findings:
        raise invalid(
            "benchmark_campaign_plan_invalid",
            "Benchmark campaign plan is incomplete or inconsistent.",
            phase="plan",
            detail={"findings": findings},
        )
    receipt = {
        "schema": "zyra.live-benchmark-plan-verification/v1",
        "valid": True,
        "campaign_id": campaign.campaign_id,
        "campaign_digest": campaign.campaign_digest,
        "condition_digest": campaign.conditions.condition_digest,
        "domain_count": len(campaign.domains),
        "variant_count": len(campaign.variants),
        "repetition_count": len(campaign.seeds),
        "cell_count": len(campaign.cells),
        "cell_plan_digest": digest([cell.to_dict() for cell in campaign.cells]),
        "verified_at": utc_now(),
    }
    receipt["receipt_digest"] = digest(receipt)
    return receipt


def verify_paired_inputs(campaign: Campaign) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], list[BenchmarkCell]] = defaultdict(list)
    for cell in campaign.cells:
        grouped[(cell.domain.value, cell.repetition)].append(cell)
    findings: list[dict[str, Any]] = []
    expected_variants = {item.variant_id for item in campaign.variants}
    for (domain, repetition), cells in sorted(grouped.items()):
        revisions = {item.input_revision for item in cells}
        task_families = {item.task_family_digest for item in cells}
        conditions = {item.condition_digest for item in cells}
        variants = {item.variant_id for item in cells}
        if len(revisions) != 1:
            findings.append(
                {
                    "code": "paired-input-revision-mismatch",
                    "domain": domain,
                    "repetition": repetition,
                }
            )
        if len(task_families) != 1:
            findings.append(
                {
                    "code": "paired-task-family-mismatch",
                    "domain": domain,
                    "repetition": repetition,
                }
            )
        if len(conditions) != 1:
            findings.append(
                {
                    "code": "paired-condition-mismatch",
                    "domain": domain,
                    "repetition": repetition,
                }
            )
        if variants != expected_variants:
            findings.append(
                {
                    "code": "paired-variant-set-mismatch",
                    "domain": domain,
                    "repetition": repetition,
                    "missing": sorted(expected_variants - variants),
                    "unexpected": sorted(variants - expected_variants),
                }
            )
    return findings


def assert_result_conditions(
    campaign: Campaign,
    cell: BenchmarkCell,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    protected = {
        "commit_sha": campaign.conditions.commit_sha,
        "sealed_policy_digest": campaign.conditions.sealed_policy_digest,
        "environment_digest": campaign.conditions.environment_digest,
        "hardware_digest": campaign.conditions.hardware_digest,
        "deployment_digest": campaign.conditions.deployment_digest,
        "provider_policy_digest": campaign.conditions.provider_policy_digest,
        "verifier_digest": campaign.conditions.verifier_digest,
        "failure_schedule_digest": campaign.conditions.failure_schedule_digest,
        "budget_digest": campaign.conditions.budget_digest,
        "source_evidence_digest": campaign.conditions.source_evidence_digest,
        "condition_digest": campaign.conditions.condition_digest,
        "input_revision": cell.input_revision,
        "task_family_digest": cell.task_family_digest,
        "seed": cell.seed,
        "repetition": cell.repetition,
        "variant_id": cell.variant_id,
        "domain": cell.domain.value,
    }
    findings: list[dict[str, Any]] = []
    for key, expected in protected.items():
        observed = receipt.get(key)
        if observed != expected:
            findings.append(
                {
                    "field": key,
                    "expected": expected,
                    "observed": observed,
                }
            )
    if findings:
        raise invalid(
            "benchmark_result_conditions_mismatch",
            "Live result changed a protected comparison condition.",
            phase="admission",
            detail={"cell_id": cell.cell_id, "findings": findings},
        )
    output = {
        "schema": "zyra.live-benchmark-condition-admission/v1",
        "valid": True,
        "cell_id": cell.cell_id,
        "condition_digest": campaign.conditions.condition_digest,
        "protected_field_count": len(protected),
        "verified_at": utc_now(),
    }
    output["receipt_digest"] = digest(output)
    return output


def variant_for(campaign: Campaign, variant_id: str) -> Variant:
    selected = identity(variant_id, "variant id")
    for item in campaign.variants:
        if item.variant_id == selected:
            return item
    raise invalid(
        "benchmark_variant_not_found",
        "Benchmark variant does not exist in the campaign.",
        phase="lookup",
        detail={"variant_id": selected},
    )


def cell_for(campaign: Campaign, cell_id: str) -> BenchmarkCell:
    selected = identity(cell_id, "cell id")
    for item in campaign.cells:
        if item.cell_id == selected:
            return item
    raise invalid(
        "benchmark_cell_not_found",
        "Benchmark cell does not exist in the campaign.",
        phase="lookup",
        detail={"cell_id": selected},
    )


def parse_seeds(value: Any) -> tuple[int, ...]:
    values = sequence(value, "seeds")
    if len(values) < 3 or len(values) > 100:
        raise invalid(
            "benchmark_seed_count_invalid",
            "Formal benchmark requires between 3 and 100 repetitions.",
            phase="plan",
            detail={"observed": len(values)},
        )
    selected = tuple(
        bounded_integer(
            item,
            f"seed[{index}]",
            minimum=0,
            maximum=2**63 - 1,
        )
        for index, item in enumerate(values)
    )
    if len(set(selected)) != len(selected):
        raise invalid(
            "benchmark_seed_duplicate",
            "Benchmark repetition seeds must be distinct.",
            phase="plan",
        )
    return selected


def parse_domains(value: Any) -> tuple[DomainKind, ...]:
    values = sequence(value, "domains")
    selected: list[DomainKind] = []
    for item in values:
        try:
            selected.append(DomainKind(str(item)))
        except ValueError as error:
            raise invalid(
                "benchmark_domain_invalid",
                "Benchmark domain is not supported.",
                phase="plan",
                detail={"domain": str(item)},
            ) from error
    if tuple(selected) != REQUIRED_DOMAINS:
        raise invalid(
            "benchmark_domains_incomplete",
            "Formal benchmark requires both domains in canonical order.",
            phase="plan",
            detail={
                "expected": [item.value for item in REQUIRED_DOMAINS],
                "observed": [item.value for item in selected],
            },
        )
    return tuple(selected)


def parse_input_revisions(
    value: Any,
    *,
    domains: Sequence[DomainKind],
    repetition_count: int,
) -> dict[DomainKind, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        raise invalid(
            "benchmark_input_revisions_invalid",
            "Input revisions must be a domain-keyed object.",
            phase="plan",
        )
    output: dict[DomainKind, tuple[str, ...]] = {}
    all_revisions: set[str] = set()
    for domain in domains:
        raw = sequence(value.get(domain.value), f"{domain.value} input revisions")
        if len(raw) != repetition_count:
            raise invalid(
                "benchmark_input_revision_count_invalid",
                "Every domain requires one fresh input revision per repetition.",
                phase="plan",
                detail={
                    "domain": domain.value,
                    "expected": repetition_count,
                    "observed": len(raw),
                },
            )
        revisions = tuple(
            bounded_text(
                item,
                f"{domain.value} input revision[{index}]",
                maximum_bytes=256,
            )
            for index, item in enumerate(raw)
        )
        if len(set(revisions)) != len(revisions):
            raise invalid(
                "benchmark_input_revision_reused",
                "Input revisions must be distinct within a domain.",
                phase="plan",
                detail={"domain": domain.value},
            )
        collision = all_revisions.intersection(revisions)
        if collision:
            raise invalid(
                "benchmark_input_revision_cross_domain_collision",
                "Input revision identity cannot be reused across domains.",
                phase="plan",
                detail={"revisions": sorted(collision)},
            )
        all_revisions.update(revisions)
        output[domain] = revisions
    return output


def parse_task_families(
    value: Any,
    domains: Sequence[DomainKind],
) -> dict[DomainKind, str]:
    if not isinstance(value, Mapping):
        raise invalid(
            "benchmark_task_family_invalid",
            "Task family digests must be a domain-keyed object.",
            phase="plan",
        )
    output = {
        domain: require_digest(
            value.get(domain.value),
            f"{domain.value} task family digest",
        )
        for domain in domains
    }
    if len(set(output.values())) != len(output):
        raise invalid(
            "benchmark_task_family_not_cross_domain",
            "Formal domains must use distinct task families.",
            phase="plan",
        )
    return output


def optional_campaign_id(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return new_identity("campaign")
    return identity(value, "campaign id")
