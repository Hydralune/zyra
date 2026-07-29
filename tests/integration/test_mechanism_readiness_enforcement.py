from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from zyra_evaluation.policy_benchmark import (
    MechanismExecutionMode,
    MechanismModeResolver,
    MechanismReadinessStatus,
    build_mechanism_readiness_report,
    canonical_digest,
)


ROOT = Path(__file__).resolve().parents[2]
IMPLEMENTATION_COMMIT = "f" * 40
GENERATED_AT = "2026-07-29T12:00:00Z"


@pytest.fixture(scope="module")
def formal_report() -> dict[str, object]:
    return build_mechanism_readiness_report(
        ROOT,
        implementation_commit=IMPLEMENTATION_COMMIT,
        generated_at=GENERATED_AT,
    )


def _write_report(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _redigest(report: dict[str, object]) -> dict[str, object]:
    report.pop("report_digest", None)
    report["report_digest"] = canonical_digest(report)
    return report


def test_formal_frozen_evidence_is_indexed_without_inflating_runs(
    formal_report: dict[str, object],
) -> None:
    index = formal_report["evidence_index"]
    assert isinstance(index, dict)
    assert index["classification"] == "read_only_evidence_index"
    assert index["volume"] == {
        "independent_source_run_count": 6,
        "derived_formal_cell_count": 42,
        "run_receipt_count": 42,
        "raw_event_count": 19191,
        "raw_sample_count": 1554,
        "training_sample_count": 0,
        "semantic_label": "evidence_volume_only",
    }
    assert len(index["source_runs"]) == 6
    assert all(item["read_only"] is True for item in index["source_runs"])
    assert all(item["read_only"] is True for item in index["references"])


def test_formal_report_excludes_future_features_and_fails_closed(
    formal_report: dict[str, object],
) -> None:
    assert formal_report["valid"] is True
    assert formal_report["activation_allowed"] is False
    assert formal_report["training_sample_count"] == 0
    assert formal_report["mechanism_statuses"] == {
        "arg_designer": "unavailable",
        "card": "unavailable",
        "agentprune": "unavailable",
        "maas": "unavailable",
    }
    mechanisms = formal_report["mechanisms"]
    assert isinstance(mechanisms, dict)
    assert all(
        value["deterministic_input_snapshot_replay"]["match"] is True
        for value in mechanisms.values()
    )
    assert all(
        value["causal_links"]["completeness_ratio"] == 0.0
        for value in mechanisms.values()
    )
    card_fields = {
        item["field_id"]: item for item in mechanisms["card"]["field_coverage"]
    }
    assert card_fields["latency"]["available_count"] == 0
    assert card_fields["latency"]["future_information_count"] == 12
    arg_fields = {
        item["field_id"]: item
        for item in mechanisms["arg_designer"]["field_coverage"]
    }
    assert arg_fields["task_phase"]["coverage_ratio"] == 1.0
    assert arg_fields["requirement_revision"]["coverage_ratio"] == 1.0
    assert arg_fields["immutable_graph_snapshot"]["future_information_count"] == 6


def test_identical_frozen_input_rebuilds_identical_snapshot_digests(
    formal_report: dict[str, object],
) -> None:
    repeated = build_mechanism_readiness_report(
        ROOT,
        implementation_commit=IMPLEMENTATION_COMMIT,
        generated_at=GENERATED_AT,
    )
    assert repeated["evidence_index"]["index_digest"] == formal_report[
        "evidence_index"
    ]["index_digest"]
    assert repeated["report_digest"] == formal_report["report_digest"]
    for mechanism_id in ("arg_designer", "card", "agentprune", "maas"):
        assert repeated["mechanisms"][mechanism_id][
            "deterministic_input_snapshot_replay"
        ] == formal_report["mechanisms"][mechanism_id][
            "deterministic_input_snapshot_replay"
        ]


def test_missing_or_corrupt_report_resolves_every_mechanism_to_baseline(
    tmp_path: Path,
) -> None:
    missing = MechanismModeResolver(
        ROOT,
        report_path=tmp_path / "missing.json",
    )
    for resolution in missing.resolve_all().values():
        assert resolution.status is MechanismReadinessStatus.UNAVAILABLE
        assert resolution.mode is MechanismExecutionMode.BASELINE
        assert resolution.canonical_mutation_allowed is False

    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_text("{", encoding="utf-8")
    corrupt = MechanismModeResolver(ROOT, report_path=corrupt_path)
    assert all(
        item.mode is MechanismExecutionMode.BASELINE
        for item in corrupt.resolve_all().values()
    )


def test_default_resolver_requires_registry_bound_formal_report() -> None:
    resolver = MechanismModeResolver(ROOT)
    resolutions = resolver.resolve_all()
    formal = json.loads(
        (
            ROOT
            / "docs"
            / "reviews"
            / "phase2"
            / "MechanismEvidenceReadinessReport.json"
        ).read_text(encoding="utf-8")
    )

    assert resolver.disconnect_reason == ""
    assert all(
        item.mode is MechanismExecutionMode.BASELINE
        for item in resolutions.values()
    )
    assert all(
        item.report_digest == formal["report_digest"]
        for item in resolutions.values()
    )


def test_report_digest_or_contract_mismatch_disconnects_resolver(
    tmp_path: Path,
    formal_report: dict[str, object],
) -> None:
    digest_mismatch = copy.deepcopy(formal_report)
    digest_mismatch["mechanisms"]["arg_designer"]["status"] = "evidence_only"
    digest_path = tmp_path / "digest-mismatch.json"
    _write_report(digest_path, digest_mismatch)
    resolver = MechanismModeResolver(ROOT, report_path=digest_path)
    assert all(
        item.mode is MechanismExecutionMode.BASELINE
        for item in resolver.resolve_all().values()
    )

    contract_mismatch = copy.deepcopy(formal_report)
    contract_mismatch["contract"]["sha256"] = "0" * 64
    contract_mismatch = _redigest(contract_mismatch)
    contract_path = tmp_path / "contract-mismatch.json"
    _write_report(contract_path, contract_mismatch)
    resolver = MechanismModeResolver(ROOT, report_path=contract_path)
    assert all(
        item.mode is MechanismExecutionMode.BASELINE
        for item in resolver.resolve_all().values()
    )


def test_per_mechanism_statuses_select_default_diagnostic_and_baseline(
    tmp_path: Path,
    formal_report: dict[str, object],
) -> None:
    selected = copy.deepcopy(formal_report)
    selected["readiness_stage"] = "activation_ready"
    selected["activation_allowed"] = True
    selected["mechanism_statuses"] = {
        "arg_designer": "deterministic_ready",
        "card": "evidence_only",
        "agentprune": "unavailable",
        "maas": "deterministic_ready",
    }
    for mechanism_id, status in selected["mechanism_statuses"].items():
        selected["mechanisms"][mechanism_id]["status"] = status
        selected["mechanisms"][mechanism_id][
            "readiness_stage"
        ] = "activation_ready"
    selected = _redigest(selected)
    report_path = tmp_path / "mixed-statuses.json"
    _write_report(report_path, selected)

    resolver = MechanismModeResolver(ROOT, report_path=report_path)
    resolutions = resolver.resolve_all()

    assert resolutions["arg_designer"].mode is MechanismExecutionMode.DEFAULT
    assert resolutions["arg_designer"].canonical_mutation_allowed is True
    assert resolutions["card"].mode is MechanismExecutionMode.DIAGNOSTIC
    assert resolutions["card"].canonical_mutation_allowed is False
    assert resolutions["agentprune"].mode is MechanismExecutionMode.BASELINE
    assert resolutions["maas"].mode is MechanismExecutionMode.DEFAULT
    unknown = resolver.resolve("unknown-mechanism")
    assert unknown.status is MechanismReadinessStatus.UNAVAILABLE
    assert unknown.mode is MechanismExecutionMode.BASELINE


def test_input_precheck_deterministic_ready_remains_validation_only(
    tmp_path: Path,
    formal_report: dict[str, object],
) -> None:
    selected = copy.deepcopy(formal_report)
    selected["mechanism_statuses"]["arg_designer"] = "deterministic_ready"
    selected["mechanisms"]["arg_designer"]["status"] = "deterministic_ready"
    selected = _redigest(selected)
    path = tmp_path / "input-precheck-ready.json"
    _write_report(path, selected)

    resolution = MechanismModeResolver(ROOT, report_path=path).resolve(
        "arg_designer"
    )
    assert resolution.status is MechanismReadinessStatus.DETERMINISTIC_READY
    assert resolution.mode is MechanismExecutionMode.VALIDATION
    assert resolution.canonical_mutation_allowed is False
