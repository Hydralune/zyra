from __future__ import annotations

import json
from pathlib import Path

from zyra_evaluation.freeze_audit.cli import build_parser, switches_from_args
from zyra_evaluation.freeze_audit.engine import (
    FreezeAuditEngine,
    adapt_source_section,
)
from zyra_evaluation.freeze_audit.model import RuleSwitches
from zyra_evaluation.freeze_audit.policy import queue_summary
from zyra_integrations.source_custody.repository import RepositoryScanner


ROOT = Path(__file__).resolve().parents[2]
CATALOG = (
    "packages/evaluation/zyra_evaluation/data/"
    "state_owner_evidence_catalog.json"
)
SOURCE_RECEIPT = (
    "docs/reviews/evidence/M3-S01A-01/source-custody-receipt.json"
)


def test_repository_inventory_section_crosses_model_boundary_explicitly() -> None:
    _, source_section = RepositoryScanner(
        ROOT,
        include_vendor=False,
    ).scan()

    adapted = adapt_source_section(source_section)

    assert adapted.name == source_section.name
    assert adapted.metrics == source_section.metrics
    assert all(hasattr(item, "domain") for item in adapted.findings)
    assert all(hasattr(item, "requirement_id") for item in adapted.findings)


def test_real_repository_audit_writes_stable_downstream_inputs(
    tmp_path: Path,
) -> None:
    switches = RuleSwitches(
        source_risks=True,
        effective_lines=False,
    )
    result = FreezeAuditEngine(
        ROOT,
        mode="inventory",
        catalog_path=CATALOG,
        source_receipt_path=SOURCE_RECEIPT,
        run_source_custody=False,
        baseline_revision="",
        parent_baseline_revision="",
        switches=switches,
    ).run()

    receipt_path = result.write_receipt(tmp_path / "receipt.json")
    summary_path = result.write_summary(tmp_path / "summary.json")
    queue_path = result.write_work_queue(tmp_path / "queue.json")
    downstream = result.write_downstream_directory(tmp_path / "downstream")

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    assert receipt["schema"] == (
        "zyra.state-owner-reachability-evidence-audit/v1"
    )
    assert receipt["state_owner_summary"]["owner_count"] == 11
    assert receipt["state_owner_summary"]["requirement_count"] == 19
    assert receipt["config"]["switches"]["effective_lines"] is False
    assert summary["mode"] == "inventory"
    assert summary["valid"] is True
    assert summary["release_ready"] is False
    assert queue["summary"] == queue_summary(result.policy.work_queue)
    assert {item.name for item in downstream} == {
        "m3_01b.json",
        "m3_02a.json",
        "m3_02b.json",
        "m3_03.json",
    }
    assert all(
        json.loads(item.read_text(encoding="utf-8"))["receipt_digest"]
        == result.receipt_digest
        for item in downstream
    )


def test_cli_exposes_all_freeze_inputs_and_mutation_switches() -> None:
    parser = build_parser()
    arguments = parser.parse_args(
        [
            "--project-root",
            str(ROOT),
            "--mode",
            "inventory",
            "--catalog",
            CATALOG,
            "--source-receipt",
            SOURCE_RECEIPT,
            "--no-source-scan",
            "--disable-rule",
            "causality",
            "--disable-rule",
            "requirements",
        ]
    )

    switches = switches_from_args(arguments)

    assert arguments.mode == "inventory"
    assert arguments.no_source_scan is True
    assert switches.causality is False
    assert switches.requirements is False
    assert switches.ownership is True
