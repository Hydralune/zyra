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
    LedgerLifecycle,
    MainPathBinding,
    MainPathStatus,
    MigrationStrategy,
    TargetBinding,
    accounting_markdown,
    accounting_summary,
    assert_accounting_report,
    build_accounting_report,
    entry_debt_queue,
    source_account_rows,
    source_completion_profiles,
    target_conflict_rows,
    unit_completion_profiles,
    unit_account_rows,
)
from tests.unit.test_internalization_ledger_policy_linecount import sample_entry


class InternalizationLedgerAccountingTests(unittest.TestCase):
    def test_accounting_report_summarizes_source_unit_and_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write(root / "packages/integrations/zyra_integrations/ledger_models.py", "class Ledger: pass\n")
            ledger = InternalizationLedger([sample_entry()])

            report = build_accounting_report(root, ledger, owner_unit="M1-01A")
            summary = accounting_summary(report)

            self.assertTrue(report.ok)
            self.assertFalse(summary["has_blocking_findings"])
            self.assertEqual(summary["total_entries"], 1)
            self.assertEqual(summary["source_repo_count"], 1)
            self.assertEqual(summary["owner_unit_count"], 1)
            self.assertEqual(summary["target_path_count"], 1)
            self.assertEqual(summary["source_rows"][0]["source_repo"], "claude-code-best")
            self.assertEqual(summary["unit_rows"][0]["owner_unit"], "M1-01A")
            assert_accounting_report(report)

    def test_accounting_flags_materialized_entry_without_effective_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            entry = sample_entry()
            entry.target_bindings = [TargetBinding("packages/integrations/zyra_integrations/data/internalization_ledger_seed.json")]
            entry.lifecycle = LedgerLifecycle.INTERNALIZED
            ledger = InternalizationLedger([entry])

            report = build_accounting_report(root, ledger, owner_unit="M1-01A")
            codes = {finding.code for finding in report.findings}

            self.assertFalse(report.ok)
            self.assertIn("MATERIALIZED_TARGET_MISSING", codes)
            self.assertIn("MATERIALIZED_WITHOUT_EFFECTIVE_TARGET", codes)
            self.assertIn("TARGET_COUNTS_AS_DATA_ONLY", codes)
            with self.assertRaises(AssertionError):
                assert_accounting_report(report)

    def test_accounting_finds_target_ownership_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write(root / "packages/runtime/shared.py", "VALUE = 1\n")
            first = sample_entry()
            first.target_bindings = [TargetBinding("packages/runtime/shared.py")]
            second = sample_entry()
            second.ledger_id = "ile_browser_shared"
            second.source_repo = "browser-use"
            second.source_path = "browser_use/agent/service.py"
            second.capability_name = "browser service"
            second.owner_unit = "M1-04A"
            second.target_bindings = [TargetBinding("packages/runtime/shared.py")]
            ledger = InternalizationLedger([first, second])

            report = build_accounting_report(root, ledger)
            conflicts = target_conflict_rows(report)

            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0]["target_path"], "packages/runtime/shared.py")
            self.assertEqual(set(conflicts[0]["source_repos"]), {"browser-use", "claude-code-best"})
            self.assertTrue(any(finding.code == "TARGET_OWNERSHIP_CONFLICT" for finding in report.findings))

    def test_accounting_rows_are_stable_and_include_debt_ratios(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write(root / "packages/integrations/zyra_integrations/ledger_models.py", "class Ledger: pass\n")
            ledger = InternalizationLedger([sample_entry()])

            report = build_accounting_report(root, ledger)
            source_rows = source_account_rows(report)
            unit_rows = unit_account_rows(report)

            self.assertEqual(source_rows[0]["source_repo"], "claude-code-best")
            self.assertGreaterEqual(source_rows[0]["effective_target_ratio"], 0)
            self.assertEqual(unit_rows[0]["owner_unit"], "M1-01A")
            self.assertEqual(unit_rows[0]["minimum_effective_lines"], 10000)
            self.assertGreaterEqual(unit_rows[0]["wiring_ratio"], 0)

    def test_connected_status_without_main_path_is_accounted_as_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write(root / "packages/integrations/zyra_integrations/ledger_models.py", "class Ledger: pass\n")
            entry = sample_entry()
            entry.main_path_status = MainPathStatus.API_CONNECTED
            entry.main_path = MainPathBinding()
            ledger = InternalizationLedger([entry])

            report = build_accounting_report(root, ledger)
            codes = {finding.code for finding in report.findings}

            self.assertIn("CONNECTED_WITHOUT_MAIN_PATH", codes)
            self.assertIn("ENTRY_POLICY_ERRORS", codes)
            self.assertFalse(report.ok)

    def test_accounting_rejects_connected_vendor_runtime_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write(root / "packages/integrations/zyra_integrations/ledger_models.py", "class Ledger: pass\n")
            entry = sample_entry()
            entry.owner_unit = "M1-01B"
            entry.migration_strategy = MigrationStrategy.VENDORED_RUNTIME
            entry.main_path_status = MainPathStatus.WORKER_RUNTIME_CONNECTED
            entry.main_path = MainPathBinding(
                surfaces=["worker-runtime"],
                worker_runtime="zyra_runtime.query.QueryEngine",
            )
            ledger = InternalizationLedger([entry])

            report = build_accounting_report(root, ledger, owner_unit="M1-01B")
            codes = {finding.code for finding in report.findings}

            self.assertFalse(report.ok)
            self.assertIn("CONNECTED_SOURCE_POOL_NOT_DEEP_INTERNALIZED", codes)
            self.assertEqual(report.unit_accounts["M1-01B"].policy_error_entries, 1)
            self.assertEqual(report.source_accounts["claude-code-best"].policy_error_entries, 1)

    def test_completion_profiles_and_debt_queue_rank_missing_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            entry = sample_entry()
            entry.test_entries = []
            entry.main_path = MainPathBinding()
            ledger = InternalizationLedger([entry])

            report = build_accounting_report(root, ledger)
            source_profiles = source_completion_profiles(report)
            unit_profiles = unit_completion_profiles(report)
            debt = entry_debt_queue(report)

            self.assertEqual(source_profiles[0]["completion_band"], "empty")
            self.assertEqual(unit_profiles[0]["owner_unit"], "M1-01A")
            self.assertGreater(debt[0]["debt_score"], 0)
            self.assertIn("tests", debt[0]["missing_surfaces"])
            self.assertIn(debt[0]["recommended_next_action"], {"materialize recorded target inside zyra", "add test entry"})

    def test_accounting_markdown_contains_profiles_and_debt_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            entry = sample_entry()
            entry.test_entries = []
            ledger = InternalizationLedger([entry])

            report = build_accounting_report(root, ledger)
            markdown = accounting_markdown(report, limit=5)

            self.assertIn("# Internalization Ledger Accounting", markdown)
            self.assertIn("## Source Profiles", markdown)
            self.assertIn("## Unit Profiles", markdown)
            self.assertIn("## Debt Queue", markdown)
            self.assertIn("claude-code-best", markdown)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
