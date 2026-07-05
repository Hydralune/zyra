from __future__ import annotations

import sys
import tempfile
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
    LedgerAdvanceRequest,
    LedgerLifecycle,
    LedgerWorkflow,
    MainPathStatus,
    build_coverage_report,
    build_snapshot,
    build_unit_matrix,
    build_unit_readiness_report,
    diff_snapshots,
    load_seed_ledger,
)
from zyra_integrations.ledger_gate import build_completion_gate_report
from zyra_integrations.ledger_selectors import (
    LedgerSelector,
    build_selection_report,
    entries_missing_runtime,
    entries_missing_tests,
    entries_ready_for_connected_status,
    entries_with_data_only_targets,
)
from tests.unit.test_internalization_ledger_policy_linecount import sample_entry


class LedgerReportsWorkflowTests(unittest.TestCase):
    def test_coverage_report_tracks_source_repos_and_effective_targets(self) -> None:
        ledger = load_seed_ledger()

        report = build_coverage_report(ledger)

        self.assertGreaterEqual(report.total_entries, 800)
        self.assertIn("claude-code-best", report.by_source_repo)
        self.assertGreater(report.effective_target_records, 0)

    def test_unit_matrix_contains_dependency_order_and_missing_units(self) -> None:
        ledger = load_seed_ledger()

        matrix = build_unit_matrix(ledger)

        self.assertEqual(matrix.unit_count, 39)
        self.assertIn("M1-01A", matrix.dependency_order)
        self.assertIn("M3-03", matrix.dependency_order)
        self.assertGreaterEqual(matrix.uncovered_units, 0)

    def test_readiness_report_uses_line_count_gate(self) -> None:
        ledger = InternalizationLedger([sample_entry()])
        audit = InternalizationLedgerAuditor(ROOT, strict=False).audit(ledger)

        report = build_unit_readiness_report(
            ROOT,
            ledger,
            owner_unit="M1-01A",
            audit_report=audit,
            line_count_report=None,
        )

        self.assertEqual(report.total_entries, 1)
        self.assertEqual(report.ready_for_internalization, 1)
        self.assertFalse(report.line_count_ok)

    def test_workflow_rejects_invalid_advance_and_records_valid_mutation(self) -> None:
        entry = sample_entry()
        ledger = InternalizationLedger([entry])
        workflow = LedgerWorkflow(ROOT, ledger)

        rejected = workflow.advance(
            LedgerAdvanceRequest(
                ledger_id=entry.ledger_id,
                lifecycle=LedgerLifecycle.PRODUCTIZED,
                main_path_status=MainPathStatus.TESTED_MAIN_PATH,
                reason="missing productization evidence",
                write_event=False,
            )
        )
        accepted = workflow.advance(
            LedgerAdvanceRequest(
                ledger_id=entry.ledger_id,
                lifecycle=LedgerLifecycle.INTERNALIZED,
                main_path_status=MainPathStatus.API_CONNECTED,
                reason="adapter connected to API",
                effective_lines=100,
                write_event=False,
                force=True,
            )
        )

        self.assertFalse(rejected.ok)
        self.assertTrue(accepted.ok)
        self.assertIsNotNone(accepted.mutation)
        self.assertEqual(ledger.require(entry.ledger_id).lifecycle, LedgerLifecycle.INTERNALIZED)

    def test_snapshot_diff_detects_status_change(self) -> None:
        entry = sample_entry()
        ledger = InternalizationLedger([entry])
        before = build_snapshot(ROOT, ledger, label="before", owner_unit="M1-01A")
        entry.lifecycle = LedgerLifecycle.INTERNALIZED
        entry.main_path_status = MainPathStatus.API_CONNECTED
        ledger.upsert(entry)
        after = build_snapshot(ROOT, ledger, label="after", owner_unit="M1-01A")

        diff = diff_snapshots(before, after)

        self.assertEqual(diff.changed_count, 1)
        self.assertIn(entry.ledger_id, diff.changed_entries)
        self.assertIn("lifecycle", diff.changed_entries[entry.ledger_id])

    def test_selector_filters_runtime_tests_routes_and_target_verdicts(self) -> None:
        ledger = InternalizationLedger([sample_entry()])

        runtime = build_selection_report(ledger, LedgerSelector(runtime_module_contains="ledger_models"))
        api = build_selection_report(ledger, LedgerSelector(api_route_contains="/ledger"))
        tests = build_selection_report(ledger, LedgerSelector(test_kind="unit"))
        effective = build_selection_report(ledger, LedgerSelector(target_verdict="effective"))

        self.assertEqual(runtime.total_matches, 1)
        self.assertEqual(api.total_matches, 1)
        self.assertEqual(tests.total_matches, 1)
        self.assertEqual(effective.total_matches, 1)

    def test_selector_helpers_find_missing_and_ready_entries(self) -> None:
        ready = sample_entry()
        missing = sample_entry()
        missing.ledger_id = "missing-runtime"
        missing.runtime_entry.command = ""
        missing.runtime_entry.module = ""
        missing.runtime_entry.function = ""
        missing.test_entries = []
        data_only = sample_entry()
        data_only.ledger_id = "data-only"
        data_only.target_bindings[0].target_path = "packages/integrations/zyra_integrations/data/internalization_ledger_seed.json"
        entries = [ready, missing, data_only]

        self.assertEqual(len(entries_missing_runtime(entries)), 1)
        self.assertEqual(len(entries_missing_tests(entries)), 1)
        self.assertEqual(len(entries_with_data_only_targets(entries)), 1)
        self.assertEqual(len(entries_ready_for_connected_status(entries)), 2)

    def test_completion_gate_reports_line_count_shortfall(self) -> None:
        ledger = InternalizationLedger([sample_entry()])

        report = build_completion_gate_report(
            ROOT,
            ledger,
            owner_unit="M1-01A",
            base_commit="",
            minimum_effective_lines=10_000,
        )

        self.assertFalse(report.ok)
        self.assertGreaterEqual(report.error_count, 1)

    def test_snapshot_round_trip_file(self) -> None:
        ledger = InternalizationLedger([sample_entry()])
        snapshot = build_snapshot(ROOT, ledger, label="round-trip", owner_unit="M1-01A")

        with tempfile.TemporaryDirectory() as tmpdir:
            from zyra_integrations.ledger_snapshots import load_snapshot, save_snapshot

            path = save_snapshot(snapshot, Path(tmpdir) / "snapshot.json")
            loaded = load_snapshot(path)

        self.assertEqual(loaded.identity.snapshot_id, snapshot.identity.snapshot_id)
        self.assertEqual(loaded.summary["total_entries"], 1)


if __name__ == "__main__":
    unittest.main()
