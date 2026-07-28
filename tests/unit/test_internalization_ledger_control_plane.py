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
    InternalizationLedgerEntry,
    LedgerLifecycle,
    LicenseNotice,
    MainPathBinding,
    MainPathStatus,
    MigrationStrategy,
    NoticeStatus,
    RuntimeEntry,
    TargetBinding,
    TestEntry,
    build_acceptance_report,
    build_clean_boundary_report,
    build_cleanroom_report,
    build_evidence_graph_report,
    build_mutation_consistency_report,
    build_policy_matrix_report,
    build_reachability_report,
    build_schema_contract_report,
    build_semantic_effect_report,
    build_source_completion_profiles,
    build_state_custody_report,
    build_test_quality_report,
    build_unit_review_report,
    resolve_target_ownership_conflicts,
    validate_entry_for_persistence,
)
from zyra_integrations.ledger_acceptance import AcceptanceCode
from zyra_integrations.ledger_boundary import BoundaryCode
from zyra_integrations.ledger_line_buckets import LineBucket, bucket_line_count_report
from zyra_integrations.ledger_linecount import EffectiveLineCountReport, parse_numstat
from zyra_integrations.ledger_policy import CountVerdict, classify_path
from zyra_integrations.ledger_reachability import (
    discover_api_routes,
    discover_cli_commands,
    discover_event_producers,
)
from zyra_integrations.ledger_source_scan import iter_scannable_files
from tests.unit.test_internalization_ledger_policy_linecount import sample_entry


class InternalizationLedgerControlPlaneTests(unittest.TestCase):
    def test_source_scan_fallback_prunes_generated_and_cache_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write(root / "packages/runtime.py", "value = 1\n")
            _write(root / "tests/test_runtime.py", "def test_runtime(): pass\n")
            for generated in (
                ".tmp/generated.py",
                "tmp/generated.py",
                "dist/generated.py",
                "build/generated.py",
                "node_modules/generated.py",
                "docs/reviews/evidence.json",
            ):
                _write(root / generated, "value = 'generated'\n")

            production = {
                path.relative_to(root).as_posix()
                for path in iter_scannable_files(root)
            }
            with_tests = {
                path.relative_to(root).as_posix()
                for path in iter_scannable_files(root, include_tests=True)
            }

        self.assertEqual(production, {"packages/runtime.py"})
        self.assertEqual(
            with_tests,
            {"packages/runtime.py", "tests/test_runtime.py"},
        )

    def test_vendor_runtime_path_requires_review_not_effective_by_default(self) -> None:
        classification = classify_path("vendor-runtimes/claude-code-runtime/productized/src/QueryEngine.ts")

        self.assertEqual(classification.verdict, CountVerdict.REVIEW)
        self.assertIn("vendor-runtime", classification.reason)

    def test_line_bucket_report_separates_data_tests_vendor_and_production(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write(root / "packages/integrations/zyra_integrations/ledger_boundary.py", "def f():\n    return 'runtime event state'\n")
            _write(root / "tests/unit/test_boundary.py", "def test_f():\n    assert True\n")
            _write(root / "packages/integrations/zyra_integrations/data/internalization_ledger_seed.json", "{}\n")
            _write(root / "vendor-runtimes/claude-code-runtime/productized/src/QueryEngine.ts", "export const x = 1;\n")
            files = parse_numstat(
                "2\t0\tpackages/integrations/zyra_integrations/ledger_boundary.py\n"
                "2\t0\ttests/unit/test_boundary.py\n"
                "1\t0\tpackages/integrations/zyra_integrations/data/internalization_ledger_seed.json\n"
                "1\t0\tvendor-runtimes/claude-code-runtime/productized/src/QueryEngine.ts\n",
                project_root=root,
            )
            effective = [item for item in files if item.effective_added]
            excluded = [item for item in files if item.excluded_added]
            report = EffectiveLineCountReport(
                base="base",
                head="HEAD",
                cached=False,
                counted_paths=["apps", "packages", "tests", "scripts", "vendor-runtimes", "skills"],
                raw_added=sum(item.added for item in files),
                raw_deleted=0,
                effective_added=sum(item.effective_added for item in files),
                effective_deleted=0,
                excluded_added=sum(item.excluded_added for item in files),
                review_added=sum(item.added for item in files if item.effective_verdict == CountVerdict.REVIEW),
                files=files,
                excluded_files=excluded,
                review_files=[item for item in files if item.effective_verdict == CountVerdict.REVIEW],
                effective_files=effective,
                minimum_effective_lines=4,
            )

            bucketed = bucket_line_count_report(root, report)
            buckets = {file.path: file.bucket for file in bucketed.files}

        self.assertEqual(buckets["packages/integrations/zyra_integrations/ledger_boundary.py"], LineBucket.PRODUCTION)
        self.assertEqual(buckets["tests/unit/test_boundary.py"], LineBucket.TEST)
        self.assertEqual(buckets["packages/integrations/zyra_integrations/data/internalization_ledger_seed.json"], LineBucket.DATA)
        self.assertEqual(buckets["vendor-runtimes/claude-code-runtime/productized/src/QueryEngine.ts"], LineBucket.VENDOR_LIKE)
        self.assertEqual(bucketed.bucket_effective_added, 4)
        self.assertEqual(bucketed.vendor_like_added, 1)
        self.assertEqual(bucketed.data_added, 1)

    def test_boundary_scan_flags_parent_source_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bad_ref = "../" + "claude-code-best" + "/bin/run"
            _write(root / "packages/runtime/bad_runtime.py", f"import subprocess\nsubprocess.run('{bad_ref}')\n")

            report = build_clean_boundary_report(root, include_cache=False)

        codes = {finding.code for finding in report.findings}
        self.assertFalse(report.ok)
        self.assertIn(BoundaryCode.SUBPROCESS_SOURCE_REPO_REFERENCE, codes)

    def test_reachability_flags_missing_connected_route(self) -> None:
        entry = sample_entry()
        entry.main_path_status = MainPathStatus.API_CONNECTED
        entry.main_path = MainPathBinding(api_routes=["GET /ledger/definitely-missing-route"], event_types=["system_notice"])
        ledger = InternalizationLedger([entry])

        report = build_reachability_report(ROOT, ledger, owner_unit="M1-01A", strict_audit=False)

        self.assertFalse(report.ok)
        self.assertTrue(any("route" in finding.message.lower() for finding in report.findings))

    def test_reachability_discovers_delegated_mcp_surfaces(self) -> None:
        routes = {(probe.method, probe.route) for probe in discover_api_routes(ROOT)}
        commands = {probe.command for probe in discover_cli_commands(ROOT)}
        events = {probe.event_type for probe in discover_event_producers(ROOT)}

        self.assertIn(("GET", "/mcp"), routes)
        self.assertIn(("POST", "/mcp/reload"), routes)
        self.assertIn(("POST", "/mcp/resources/read"), routes)
        self.assertIn(("POST", "/mcp/prompts/get"), routes)
        self.assertIn("/mcp", commands)
        self.assertIn("mcp_connection_changed", events)
        self.assertIn("mcp_capabilities_changed", events)
        self.assertIn("mcp_auth_changed", events)
        self.assertIn("mcp_elicitation", events)
        self.assertIn("mcp_task_updated", events)
        self.assertIn("mcp_instructions_changed", events)
        self.assertIn("mcp_tool_result", events)

    def test_persistence_validation_rejects_bad_entry_before_upsert(self) -> None:
        ledger = InternalizationLedger([sample_entry()])
        bad = InternalizationLedgerEntry(
            ledger_id="bad",
            source_repo="claude-code-best",
            source_path="src/QueryEngine.ts",
            capability_name="bad",
            capability_summary="no target or tests",
            target_bindings=[],
            migration_strategy=MigrationStrategy.ADAPTER,
            main_path_status=MainPathStatus.API_CONNECTED,
            lifecycle=LedgerLifecycle.ACTIVE,
            runtime_entry=RuntimeEntry(module="zyra_integrations.ledger_models", function="InternalizationLedgerEntry"),
            license_notice=LicenseNotice(source_repo="claude-code-best", status=NoticeStatus.RECORDED),
        )

        validation = validate_entry_for_persistence(ROOT, ledger, bad, strict=True)

        self.assertFalse(validation.ok)
        self.assertGreater(validation.error_count + validation.blocker_count, 0)
        self.assertIsNone(ledger.get("bad"))

    def test_acceptance_report_distinguishes_overstatement_from_completion(self) -> None:
        entries = []
        for index in range(5):
            planned = sample_entry()
            planned.ledger_id = f"planned-{index}"
            planned.lifecycle = LedgerLifecycle.PLANNED
            planned.main_path_status = MainPathStatus.VENDORED
            planned.migration_strategy = MigrationStrategy.VENDORED_RUNTIME
            entries.append(planned)
        active = sample_entry()
        active.ledger_id = "active"
        active.lifecycle = LedgerLifecycle.ACTIVE
        active.main_path_status = MainPathStatus.API_CONNECTED
        entries.append(active)
        ledger = InternalizationLedger(entries)

        profiles = build_source_completion_profiles(ledger)
        audit = InternalizationLedgerAuditor(ROOT, strict=False).audit(ledger)
        boundary = build_clean_boundary_report(ROOT, include_tests=False, include_cache=False, scan_roots=["packages"])
        reachability = build_reachability_report(ROOT, ledger, owner_unit="M1-01A", include_entries=False, strict_audit=False)
        report = build_acceptance_report(
            ROOT,
            ledger,
            owner_unit="M1-01A",
            include_entries=False,
            audit_report=audit,
            boundary_report=boundary,
            reachability_report=reachability,
        )

        self.assertEqual(len(profiles), 1)
        self.assertTrue(profiles[0].overstatement_risk)
        self.assertTrue(any(criterion.code == AcceptanceCode.SOURCE_COMPLETION_OVERSTATED for criterion in report.criteria))

    def test_target_ownership_conflict_requires_explicit_shared_boundary(self) -> None:
        first = sample_entry()
        first.ledger_id = "first"
        first.source_repo = "claude-code-best"
        first.owner_unit = "M1-02B"
        first.target_bindings = [TargetBinding("packages/runtime/zyra_runtime/shared_runtime.py")]
        first.lifecycle = LedgerLifecycle.ACTIVE
        second = sample_entry()
        second.ledger_id = "second"
        second.source_repo = "browser-use"
        second.owner_unit = "M1-04A"
        second.target_bindings = [TargetBinding("packages/runtime/zyra_runtime/shared_runtime.py")]
        second.lifecycle = LedgerLifecycle.ACTIVE
        ledger = InternalizationLedger([first, second])

        conflicts = resolve_target_ownership_conflicts(ledger)

        self.assertEqual(len(conflicts), 1)
        self.assertFalse(conflicts[0].accepted_shared_target)
        self.assertIn("claude-code-best", conflicts[0].source_repos)
        self.assertIn("browser-use", conflicts[0].source_repos)

    def test_semantic_effect_report_proves_controls_change_behavior(self) -> None:
        report = build_semantic_effect_report(ROOT)

        self.assertTrue(report.ok)
        self.assertEqual(report.total_probes, 4)
        self.assertEqual(report.passing_probes, 4)
        self.assertTrue(all(result.proves_effect for result in report.results))

    def test_cleanroom_report_has_self_contained_commands_without_source_scan(self) -> None:
        ledger = InternalizationLedger([sample_entry()])

        report = build_cleanroom_report(
            ROOT,
            ledger,
            include_source_scan=False,
            scan_roots=["packages"],
        )

        self.assertTrue(report.commands)
        self.assertTrue(all(command.cleanroom_safe for command in report.commands))
        self.assertEqual(report.summary["source_scan_forbidden_hits"], 0)
        self.assertIn("packages", report.copy_plan.copy_roots)

    def test_test_quality_report_detects_behavior_test_signals(self) -> None:
        ledger = InternalizationLedger([sample_entry()])

        report = build_test_quality_report(ROOT, ledger, owner_unit="M1-01A", include_entries=True)

        self.assertTrue(report.ok)
        self.assertGreaterEqual(report.checked_test_entries, 1)
        self.assertGreaterEqual(report.behavior_test_files, 1)
        self.assertGreaterEqual(report.negative_path_files, 1)

    def test_schema_graph_custody_mutation_and_policy_reports_are_executable(self) -> None:
        ledger = InternalizationLedger([sample_entry()])

        schema = build_schema_contract_report(ledger, owner_unit="M1-01A", include_entries=True)
        graph = build_evidence_graph_report(ROOT, ledger, owner_unit="M1-01A", include_nodes=True)
        custody = build_state_custody_report(ROOT)
        mutation = build_mutation_consistency_report(ROOT)
        policy = build_policy_matrix_report(ledger, owner_unit="M1-01A", include_decisions=True)

        self.assertTrue(schema.ok)
        self.assertGreaterEqual(schema.checked_entries, 1)
        self.assertTrue(graph.nodes)
        self.assertTrue(graph.edges)
        self.assertTrue(custody.claims)
        self.assertTrue(mutation.ok)
        self.assertEqual(mutation.summary["passing_probes"], mutation.summary["probe_count"])
        self.assertTrue(policy.decisions)

    def test_policy_matrix_accepts_worker_connected_owned_runtime_evidence(self) -> None:
        entry = sample_entry()
        entry.main_path_status = MainPathStatus.WORKER_RUNTIME_CONNECTED
        entry.migration_strategy = MigrationStrategy.ADAPTER
        entry.main_path = MainPathBinding(
            surfaces=["worker-runtime"],
            event_types=["runtime.worker.started"],
            worker_runtime="zyra_runtime.query.QueryEngine",
        )
        ledger = InternalizationLedger([entry])

        report = build_policy_matrix_report(ledger, owner_unit="M1-01A", include_decisions=True)

        self.assertTrue(report.ok)
        self.assertEqual(report.error_count, 0)
        self.assertEqual(report.decisions[0].missing_required_fields, [])

    def test_unit_review_composes_control_plane_evidence(self) -> None:
        ledger = InternalizationLedger([sample_entry()])

        report = build_unit_review_report(
            ROOT,
            ledger,
            owner_unit="M1-01A",
            minimum_effective_lines=0,
            include_reports=False,
        )

        self.assertTrue(report.evidence)
        self.assertTrue(any(item.report_name == "schema_contract" for item in report.evidence))
        self.assertTrue(any(item.report_name == "evidence_graph" for item in report.evidence))
        self.assertIn("objective_count", report.summary)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
