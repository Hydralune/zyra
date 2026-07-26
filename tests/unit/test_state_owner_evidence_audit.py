from __future__ import annotations

import copy
import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from zyra_evaluation.freeze_audit.catalog import CatalogLoader
from zyra_evaluation.freeze_audit.causality import CausalityAuditor
from zyra_evaluation.freeze_audit.lines import (
    PythonLineClassifier,
    classify_bucket,
)
from zyra_evaluation.freeze_audit.model import (
    AuditCatalog,
    EntrySurface,
    EventMutationSpec,
    LineBucket,
    RuleSwitches,
    SourceRef,
    StateRole,
)
from zyra_evaluation.freeze_audit.ownership import OwnershipAuditor
from zyra_evaluation.freeze_audit.policy import FreezeFindingPolicy
from zyra_evaluation.freeze_audit.python_graph import PythonGraphAnalyzer
from zyra_evaluation.freeze_audit.reachability import ReachabilityAuditor
from zyra_evaluation.freeze_audit.requirements import RequirementEvidenceAuditor
from zyra_evaluation.freeze_audit.script_graph import ScriptGraphAnalyzer
from zyra_evaluation.freeze_audit.source_bridge import SourceRiskBridge


ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = (
    ROOT
    / "packages"
    / "evaluation"
    / "zyra_evaluation"
    / "data"
    / "state_owner_evidence_catalog.json"
)
CATALOG_RELATIVE = (
    "packages/evaluation/zyra_evaluation/data/"
    "state_owner_evidence_catalog.json"
)
SOURCE_RECEIPT = (
    ROOT
    / "docs"
    / "reviews"
    / "evidence"
    / "M3-S01A-01"
    / "source-custody-receipt.json"
)
SOURCE_RECEIPT_REVISION = "a8273df7601b57cf3fecfb03936c815b9ca63f38"


@dataclass(frozen=True)
class AuditContext:
    catalog: AuditCatalog
    python: object
    script: object
    reachability: object
    ownership: object
    causality: object


@pytest.fixture(scope="module")
def context() -> AuditContext:
    loaded = CatalogLoader(ROOT).load(CATALOG_RELATIVE)
    assert loaded.catalog is not None
    catalog = loaded.catalog
    paths = {
        reference.path
        for owner in catalog.owners
        for reference in owner.all_refs()
    }
    paths.update(
        test_path
        for owner in catalog.owners
        for test_path in owner.tests
    )
    python = PythonGraphAnalyzer(ROOT).analyze(paths)
    script = ScriptGraphAnalyzer(ROOT).analyze(paths)
    reachability = ReachabilityAuditor(ROOT).audit(catalog, python, script)
    ownership = OwnershipAuditor(ROOT).audit(
        catalog,
        reachability.graph,
        python,
        script,
    )
    causality = CausalityAuditor(ROOT).audit(
        catalog,
        reachability.graph,
        python,
        script,
    )
    return AuditContext(
        catalog=catalog,
        python=python,
        script=script,
        reachability=reachability,
        ownership=ownership,
        causality=causality,
    )


def finding_codes(section: object) -> set[str]:
    return {item.code for item in section.findings}


def single_owner_catalog(
    catalog: AuditCatalog,
    domain: str,
    *,
    replacement: object | None = None,
) -> AuditCatalog:
    owner = catalog.owner_by_domain[domain]
    return replace(
        catalog,
        owners=(replacement or owner,),
        requirements=(),
        required_domains=(domain,),
        required_requirements=(),
    )


def test_authoritative_catalog_is_strict_and_complete(context: AuditContext) -> None:
    result = CatalogLoader(ROOT).load(CATALOG_RELATIVE)

    assert result.catalog == context.catalog
    assert not result.section.findings
    assert len(context.catalog.owners) == 11
    assert len(context.catalog.requirements) == 19
    assert set(context.catalog.required_domains) == set(
        context.catalog.owner_by_domain
    )
    assert set(context.catalog.required_requirements) == set(
        context.catalog.requirement_by_id
    )
    assert {
        entry.surface
        for owner in context.catalog.owners
        for entry in owner.entries
        if entry.default
    } == {
        EntrySurface.CLI,
        EntrySurface.API,
        EntrySurface.WEB,
        EntrySurface.WORKER,
    }


def test_all_declared_state_references_resolve_to_executable_source(
    context: AuditContext,
) -> None:
    metrics = context.ownership.section.metrics

    assert metrics["domain_count"] == 11
    assert metrics["reference_count"] >= 60
    assert metrics["invalid_reference_count"] == 0
    assert not {
        "state_owner_reference_missing",
        "state_owner_reference_nonexecutable",
        "state_owner_reference_nonproduction",
    } & finding_codes(context.ownership.section)


def test_inventory_preserves_real_reachability_and_causality_gaps(
    context: AuditContext,
) -> None:
    reachability_codes = finding_codes(context.reachability.section)
    causality_codes = finding_codes(context.causality.section)

    assert context.reachability.section.metrics["entry_count"] == 11
    assert 0 < context.reachability.section.metrics["reachable_entry_count"] < 11
    assert "canonical_owner_default_unreachable" in reachability_codes
    assert "default_trace_edge_unverified" in reachability_codes
    assert context.causality.section.metrics["declared_links"] == 11
    assert 0 < context.causality.section.metrics["valid_links"] < 11
    assert "causal_event_not_emitted" in causality_codes
    assert "event_mutation_path_unreachable" in causality_codes


def test_duplicate_owner_catalog_mutation_is_rejected(
    tmp_path: Path,
) -> None:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    duplicate = copy.deepcopy(payload["owners"][0])
    duplicate["description"] = (
        "A malicious duplicate attempts to install a second canonical owner."
    )
    payload["owners"].append(duplicate)
    mutated = tmp_path / "duplicate-owner.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")

    result = CatalogLoader(tmp_path).load(mutated.name)

    assert result.catalog is not None
    assert "catalog_duplicate_state_domain" in finding_codes(result.section)


def test_projection_promotion_catalog_mutation_is_rejected(
    tmp_path: Path,
) -> None:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    owner = payload["owners"][0]
    promoted = copy.deepcopy(owner["projections"][0])
    promoted["role"] = "owner"
    owner["owner"] = promoted
    mutated = tmp_path / "projection-owner.json"
    mutated.write_text(json.dumps(payload), encoding="utf-8")

    result = CatalogLoader(tmp_path).load(mutated.name)

    assert result.catalog is None
    assert "catalog_schema_invalid" in finding_codes(result.section)


def test_test_only_default_entry_mutation_is_blocked(
    context: AuditContext,
) -> None:
    contract = context.catalog.owner_by_domain["session_event_projection"]
    entry = contract.entries[0]
    test_reference = SourceRef(
        path="tests/unit/test_permission_continuation.py",
        symbol="PermissionContinuationTests",
        role=StateRole.WRITER,
    )
    mutant = replace(
        contract,
        entries=(
            replace(
                entry,
                reference=test_reference,
                command="python -m unittest tests.unit.test_permission_continuation",
                trace=(test_reference, contract.owner, contract.writers[0]),
            ),
        ),
    )
    catalog = single_owner_catalog(
        context.catalog,
        contract.domain,
        replacement=mutant,
    )

    result = ReachabilityAuditor(ROOT).audit(
        catalog,
        context.python,
        context.script,
    )

    assert "default_entry_nonproduction_only" in finding_codes(result.section)
    assert not result.observations[0].reachable


def test_fallback_takeover_mutation_is_blocked(
    context: AuditContext,
) -> None:
    contract = context.catalog.owner_by_domain["artifact"]
    fallback = replace(contract.projections[0], role=StateRole.FALLBACK)
    mutant = replace(contract, fallback_refs=(fallback,))
    catalog = single_owner_catalog(
        context.catalog,
        contract.domain,
        replacement=mutant,
    )

    result = ReachabilityAuditor(ROOT).audit(
        catalog,
        context.python,
        context.script,
    )

    assert "fallback_can_take_over_owner" in finding_codes(result.section)


def test_fake_causation_mutation_is_blocked(
    context: AuditContext,
) -> None:
    contract = context.catalog.owner_by_domain[
        "provider_credential_failover"
    ]
    event: EventMutationSpec = contract.events[0]
    mutant = replace(
        contract,
        events=(replace(event, event_name="heartbeat"),),
    )
    catalog = single_owner_catalog(
        context.catalog,
        contract.domain,
        replacement=mutant,
    )

    result = CausalityAuditor(ROOT).audit(
        catalog,
        context.reachability.graph,
        context.python,
        context.script,
    )

    codes = finding_codes(result.section)
    assert "fake_causation_non_effect_event" in codes
    assert "causal_event_not_emitted" in codes


def test_data_as_code_inflation_is_removed_from_effective_lines() -> None:
    rows = ",\n".join(f"    {index}: 'value-{index}'" for index in range(40))
    source = (
        "CATALOG = {\n"
        f"{rows}\n"
        "}\n\n"
        "def select(index: int) -> str:\n"
        "    return CATALOG[index]\n"
    )
    added = frozenset(range(1, len(source.splitlines()) + 1))

    result = PythonLineClassifier().classify(source, added)

    assert len(result.data_as_code_lines) >= 32
    assert result.data_as_code_lines.isdisjoint(result.effective_lines)
    assert result.exclusions["data_as_code"] >= 32
    assert classify_bucket("packages/evaluation/x.py") is LineBucket.PRODUCTION
    assert (
        classify_bucket("packages/evaluation/data/catalog.json")
        is LineBucket.DATA
    )


def test_protected_source_custody_receipt_is_bridged_read_only() -> None:
    result = SourceRiskBridge(ROOT).audit(
        receipt_path=SOURCE_RECEIPT,
        expected_revision=SOURCE_RECEIPT_REVISION,
    )

    assert result.source_receipt_digest.startswith("sha256:")
    assert result.section.metrics["input_valid"] is True
    assert result.section.metrics["risk_records"] == len(result.records)
    assert len(result.records) > 100
    assert {
        category
        for record in result.records
        for category in record.categories
    } & {
        "semantic_port",
        "opaque_bundle",
        "dynamic_download",
        "upstream_boundary",
    }


def test_requirement_rows_bind_runtime_and_preserve_m3_work(
    context: AuditContext,
) -> None:
    result = RequirementEvidenceAuditor(ROOT).audit(
        context.catalog,
        context.ownership,
        context.reachability,
        context.causality,
    )

    assert len(result.observations) == 19
    assert result.section.metrics["requirement_count"] == 19
    assert result.section.metrics["status_counts"]["partial"] >= 3
    assert "requirement_evidence_not_verified" in finding_codes(result.section)
    assert set(result.section.metrics["m3_owner_counts"]) <= {
        "M3-01B",
        "M3-02A",
        "M3-02B",
        "M3-03",
    }


@pytest.mark.parametrize(
    ("rule_name", "section_name"),
    (
        ("catalog", "state_catalog"),
        ("ownership", "state_ownership"),
        ("reachability", "default_reachability"),
        ("causality", "event_mutation_causality"),
        ("source_risks", "source_risk_bridge"),
        ("effective_lines", "effective_lines_slice"),
        ("requirements", "requirement_runtime_evidence"),
    ),
)
def test_rule_disable_mutants_are_not_release_evidence(
    context: AuditContext,
    rule_name: str,
    section_name: str,
) -> None:
    switches = RuleSwitches().disabled(rule_name)
    if rule_name == "catalog":
        audit_section = CatalogLoader(ROOT, switches=switches).load(
            CATALOG_RELATIVE
        ).section
    elif rule_name == "ownership":
        audit_section = OwnershipAuditor(ROOT, switches=switches).audit(
            context.catalog,
            context.reachability.graph,
            context.python,
            context.script,
        ).section
    elif rule_name == "reachability":
        audit_section = ReachabilityAuditor(ROOT, switches=switches).audit(
            context.catalog,
            context.python,
            context.script,
        ).section
    elif rule_name == "causality":
        audit_section = CausalityAuditor(ROOT, switches=switches).audit(
            context.catalog,
            context.reachability.graph,
            context.python,
            context.script,
        ).section
    elif rule_name == "source_risks":
        audit_section = SourceRiskBridge(ROOT, switches=switches).audit(
            receipt_path=SOURCE_RECEIPT,
            expected_revision=SOURCE_RECEIPT_REVISION,
        ).section
    elif rule_name == "requirements":
        audit_section = RequirementEvidenceAuditor(
            ROOT,
            switches=switches,
        ).audit(
            context.catalog,
            context.ownership,
            context.reachability,
            context.causality,
        ).section
    else:
        from zyra_evaluation.freeze_audit.lines import EffectiveLineAuditor

        audit_section = EffectiveLineAuditor(
            ROOT,
            switches=switches,
        ).audit(
            "not-a-revision",
            head="also-not-a-revision",
            minimum=10_000,
            audit_id="slice",
        ).section

    assert audit_section.name == section_name
    assert audit_section.metrics["rule_enabled"] is False
    policy = FreezeFindingPolicy().apply(
        (audit_section,),
        mode="freeze",
    )
    assert policy.release_ready
    assert rule_name not in switches.enabled()
