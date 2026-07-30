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
    InternalizationLedgerEntry,
    LedgerLifecycle,
    LicenseNotice,
    LineCountPolicy,
    MainPathBinding,
    MainPathStatus,
    MigrationStrategy,
    NoticeStatus,
    RuntimeEntry,
    TargetBinding,
    TestEntry,
    classify_path,
    evaluate_transition,
    minimum_effective_lines_for_unit,
    validate_entry_policy,
)
from zyra_integrations.ledger_linecount import assert_effective_line_count, parse_numstat
from zyra_integrations.ledger_policy import (
    CountVerdict,
    LedgerPolicyCode,
    LedgerSurface,
    UNIT_BUDGETS,
)


class LedgerPolicyLineCountTests(unittest.TestCase):
    def test_seed_json_is_excluded_but_seed_builder_script_counts(self) -> None:
        seed = classify_path("packages/integrations/zyra_integrations/data/internalization_ledger_seed.json")
        builder = classify_path("scripts/build_integration_ledger_seed.py")
        policy = classify_path("packages/integrations/zyra_integrations/ledger_policy.py")

        self.assertEqual(seed.verdict, CountVerdict.EXCLUDED)
        self.assertTrue(seed.is_generated_data)
        self.assertEqual(builder.verdict, CountVerdict.EFFECTIVE)
        self.assertFalse(builder.is_generated_data)
        self.assertEqual(policy.verdict, CountVerdict.EFFECTIVE)

    def test_inventory_yaml_is_excluded_even_under_packages(self) -> None:
        classification = classify_path("packages/integrations/zyra_integrations/data/source_inventory.yaml")

        self.assertEqual(classification.verdict, CountVerdict.EXCLUDED)
        self.assertEqual(classification.surface, "data")
        self.assertIn("seed/inventory/data", classification.reason)

    def test_reference_crosswalk_json_is_vendor_like_under_vendor_runtime(
        self,
    ) -> None:
        classification = classify_path("vendor-runtimes/claude-code-runtime/metadata/reference_crosswalk.json")

        self.assertEqual(classification.verdict, CountVerdict.REVIEW)
        self.assertEqual(classification.surface, LedgerSurface.VENDOR_RUNTIME)
        self.assertTrue(classification.is_generated_data)

    def test_embedded_loopx_is_vendor_like_but_zyra_bridge_is_production(
        self,
    ) -> None:
        upstream = classify_path(
            "packages/integrations/loopx_runtime/loopx/runtime.py"
        )
        upstream_data = classify_path(
            "packages/integrations/loopx_runtime/runtime.json"
        )
        bridge = classify_path(
            "packages/integrations/zyra_integrations/loopx/runtime.py"
        )

        for classification in (upstream, upstream_data):
            self.assertEqual(classification.verdict, CountVerdict.REVIEW)
            self.assertEqual(
                classification.surface,
                LedgerSurface.VENDOR_RUNTIME,
            )
            self.assertTrue(classification.is_vendor_runtime)
        self.assertEqual(bridge.verdict, CountVerdict.EFFECTIVE)
        self.assertEqual(bridge.surface, LedgerSurface.PACKAGE)
        self.assertFalse(bridge.is_vendor_runtime)

    def test_parent_source_reference_is_excluded_and_flagged(self) -> None:
        classification = classify_path("../claude-code-best/src/QueryEngine.ts")

        self.assertEqual(classification.verdict, CountVerdict.EXCLUDED)
        self.assertIn("parent-reference", classification.flags)

    def test_parse_numstat_splits_effective_and_excluded_lines(self) -> None:
        files = parse_numstat(
            "108161\t0\tpackages/integrations/zyra_integrations/data/internalization_ledger_seed.json\n"
            "817\t0\tpackages/integrations/zyra_integrations/ledger_policy.py\n"
            "553\t0\tscripts/build_integration_ledger_seed.py\n"
        )

        effective = sum(item.effective_added for item in files)
        excluded = sum(item.excluded_added for item in files)

        self.assertEqual(effective, 1370)
        self.assertEqual(excluded, 108161)

    def test_parse_numstat_excludes_upstream_type_stub_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "vendor-runtimes/claude-code-runtime/productized/claude-code-best/src/tools/BashTool/src/Tool.ts"
            target.parent.mkdir(parents=True)
            target.write_text("// Auto-generated type stub - replace with real implementation\nexport type Tool = any;\n", encoding="utf-8")
            files = parse_numstat(f"2\t0\t{target.relative_to(root).as_posix()}\n", project_root=root)

        self.assertEqual(files[0].effective_added, 0)
        self.assertEqual(files[0].excluded_added, 2)
        self.assertIn("upstream-type-stub", files[0].content_flags)

    def test_effective_line_count_gate_fails_shortfall(self) -> None:
        from zyra_integrations.ledger_linecount import EffectiveLineCountReport

        files = parse_numstat(
            "108161\t0\tpackages/integrations/zyra_integrations/data/internalization_ledger_seed.json\n"
            "2999\t0\tpackages/integrations/zyra_integrations/ledger_models.py\n"
        )
        effective_files = [item for item in files if item.effective_added]
        excluded_files = [item for item in files if item.excluded_added]
        report = EffectiveLineCountReport(
            base="base",
            head="HEAD",
            cached=False,
            counted_paths=["apps", "packages", "tests", "scripts"],
            raw_added=sum(item.added for item in files),
            raw_deleted=0,
            effective_added=sum(item.effective_added for item in files),
            effective_deleted=0,
            excluded_added=sum(item.excluded_added for item in files),
            review_added=0,
            files=files,
            excluded_files=excluded_files,
            review_files=[],
            effective_files=effective_files,
            minimum_effective_lines=10_000,
        )

        self.assertFalse(report.ok)
        self.assertEqual(report.shortfall, 7001)
        with self.assertRaises(AssertionError):
            assert_effective_line_count(report)

    def test_unit_budget_contains_all_execution_units(self) -> None:
        self.assertEqual(len(UNIT_BUDGETS), 39)
        self.assertEqual(minimum_effective_lines_for_unit("M1-01A"), 10_000)
        self.assertEqual(minimum_effective_lines_for_unit("M2-05"), 20_000)
        self.assertEqual(minimum_effective_lines_for_unit("M3-03"), 9_000)

    def test_policy_warns_data_only_target_and_blocks_materialized_inventory(self) -> None:
        entry = sample_entry()
        entry.target_bindings = [TargetBinding("packages/integrations/zyra_integrations/data/source_inventory.json")]
        entry.lifecycle = LedgerLifecycle.INTERNALIZED
        entry.line_count_policy = LineCountPolicy.COUNTS_AS_RUNTIME

        findings = validate_entry_policy(entry)
        codes = {finding.code for finding in findings}

        self.assertIn(LedgerPolicyCode.TARGET_IS_DATA_ONLY, codes)

    def test_transition_to_productized_requires_runtime_test_main_path_notice_and_code(self) -> None:
        entry = sample_entry()
        entry.runtime_entry = RuntimeEntry()
        entry.test_entries = []
        entry.main_path = MainPathBinding()
        entry.license_notice = LicenseNotice(source_repo="claude-code-best", status=NoticeStatus.PENDING)

        result = evaluate_transition(
            entry,
            to_lifecycle=LedgerLifecycle.PRODUCTIZED,
            to_status=MainPathStatus.TESTED_MAIN_PATH,
            reason="trying to productize too early",
            effective_lines=0,
        )

        self.assertFalse(result.allowed)
        codes = {finding.code for finding in result.findings}
        self.assertIn(LedgerPolicyCode.MATERIALIZATION_REQUIRES_RUNTIME, codes)
        self.assertIn(LedgerPolicyCode.MATERIALIZATION_REQUIRES_TEST, codes)
        self.assertIn(LedgerPolicyCode.PRODUCTIZATION_REQUIRES_MAIN_PATH, codes)


def sample_entry() -> InternalizationLedgerEntry:
    return InternalizationLedgerEntry.new(
        source_repo="claude-code-best",
        source_path="src/QueryEngine.ts",
        capability_name="query engine",
        capability_summary="sample runtime",
        target_paths=["packages/integrations/zyra_integrations/ledger_models.py"],
        migration_strategy=MigrationStrategy.ADAPTER,
        main_path_status=MainPathStatus.ADAPTER_READY,
        lifecycle=LedgerLifecycle.ACTIVE,
        owner_unit="M1-01A",
        milestone="M1",
        runtime_entry=RuntimeEntry(module="zyra_integrations.ledger_models", function="InternalizationLedgerEntry"),
        test_entries=[TestEntry(path="tests/unit/test_internalization_ledger.py", command="python -m unittest")],
        main_path=MainPathBinding(surfaces=["ledger"], api_routes=["GET /ledger"], event_types=["system_notice"]),
        license_notice=LicenseNotice(source_repo="claude-code-best", status=NoticeStatus.RECORDED),
        line_count_policy=LineCountPolicy.COUNTS_AS_RUNTIME,
    )


if __name__ == "__main__":
    unittest.main()
