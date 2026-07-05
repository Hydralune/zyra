from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations import (
    InternalizationLedger,
    InternalizationLedgerAuditor,
    LedgerLifecycle,
    LineCountPolicy,
    MainPathBinding,
    MainPathStatus,
    NoticeStatus,
    TargetBinding,
    build_coverage_report,
    build_debt_report,
    build_full_ledger_report,
    build_remediation_plan,
    build_unit_handoff_package,
    build_unit_matrix,
    build_unit_readiness_report,
    compute_health_score,
    handoff_markdown,
    health_payload,
    remediation_markdown,
)
from zyra_integrations.ledger_linecount import EffectiveLineCountReport, parse_numstat
from tests.unit.test_internalization_ledger_policy_linecount import sample_entry


class LedgerHandoffHealthTests(unittest.TestCase):
    def test_remediation_plan_groups_audit_findings_into_actions(self) -> None:
        bad = sample_entry()
        bad.ledger_id = "bad"
        bad.target_bindings = []
        bad.test_entries = []
        bad.runtime_entry.command = ""
        bad.runtime_entry.module = ""
        bad.runtime_entry.function = ""
        bad.main_path = MainPathBinding()
        bad.main_path_status = MainPathStatus.WORKER_RUNTIME_CONNECTED
        bad.lifecycle = LedgerLifecycle.ACTIVE
        ledger = InternalizationLedger([bad])

        audit = InternalizationLedgerAuditor(ROOT, strict=False).audit(ledger)
        plan = build_remediation_plan(audit, owner_unit="M1-01A")
        markdown = remediation_markdown(plan)

        self.assertFalse(plan.ok_to_continue)
        self.assertGreaterEqual(plan.required_actions, 1)
        self.assertIn("Internalization Ledger Remediation Plan", markdown)

    def test_handoff_package_contains_upstream_downstream_commands_and_entries(self) -> None:
        ledger = InternalizationLedger([sample_entry()])
        handoff = build_unit_handoff_package(ROOT, ledger, owner_unit="M1-01A")
        markdown = handoff_markdown(handoff)

        self.assertEqual(handoff.owner_unit, "M1-01A")
        self.assertEqual(handoff.entry_count, 1)
        self.assertTrue(handoff.recommended_commands)
        self.assertTrue(handoff.next_agent_instructions)
        self.assertIn("M1-01A Handoff Package", markdown)

    def test_health_score_combines_audit_coverage_debt_readiness_matrix_and_linecount(self) -> None:
        ledger = InternalizationLedger([sample_entry()])
        audit = InternalizationLedgerAuditor(ROOT, strict=False).audit(ledger)
        coverage = build_coverage_report(ledger)
        debt = build_debt_report(audit)
        readiness = build_unit_readiness_report(ROOT, ledger, owner_unit="M1-01A", audit_report=audit)
        matrix = build_unit_matrix(ledger)
        line_count = line_count_report(effective=12_000, minimum=10_000)

        score = compute_health_score(
            audit=audit,
            coverage=coverage,
            debt=debt,
            readiness=readiness,
            matrix=matrix,
            line_count=line_count,
        )
        payload = health_payload(
            audit=audit,
            coverage=coverage,
            debt=debt,
            readiness=readiness,
            matrix=matrix,
            line_count=line_count,
        )

        self.assertGreaterEqual(score.score, 0)
        self.assertLessEqual(score.score, 100)
        self.assertIn("score", payload)
        self.assertEqual(payload["line_count"]["effective_added"], 12_000)

    def test_full_report_contains_health_and_unit_matrix(self) -> None:
        ledger = InternalizationLedger([sample_entry()])

        report = build_full_ledger_report(
            ROOT,
            ledger,
            owner_unit="M1-01A",
            line_count_report=line_count_report(effective=12_000, minimum=10_000),
        )

        self.assertIn("health", report)
        self.assertIn("unit_matrix", report)
        self.assertIn("contracts", report)
        self.assertIn("readiness", report)

    def test_materialized_data_only_entry_generates_remediation(self) -> None:
        entry = sample_entry()
        entry.ledger_id = "data-only"
        entry.target_bindings = [TargetBinding("packages/integrations/zyra_integrations/data/source_inventory.json")]
        entry.lifecycle = LedgerLifecycle.INTERNALIZED
        entry.main_path_status = MainPathStatus.API_CONNECTED
        entry.line_count_policy = LineCountPolicy.COUNTS_AS_RUNTIME
        entry.license_notice.status = NoticeStatus.RECORDED
        ledger = InternalizationLedger([entry])

        audit = InternalizationLedgerAuditor(ROOT, strict=False).audit(ledger)
        plan = build_remediation_plan(audit, owner_unit="M1-01A")
        action_types = {str(action.action_type) for action in plan.actions}

        self.assertIn("create_target", action_types)


def line_count_report(*, effective: int, minimum: int) -> EffectiveLineCountReport:
    files = parse_numstat(f"{effective}\t0\tpackages/integrations/zyra_integrations/ledger_policy.py\n")
    return EffectiveLineCountReport(
        base="base",
        head="HEAD",
        cached=False,
        counted_paths=["packages"],
        raw_added=effective,
        raw_deleted=0,
        effective_added=effective,
        effective_deleted=0,
        excluded_added=0,
        review_added=0,
        files=files,
        excluded_files=[],
        review_files=[],
        effective_files=files,
        minimum_effective_lines=minimum,
    )


if __name__ == "__main__":
    unittest.main()
