from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations.extraction_acceptance import build_m1_01b_acceptance_report  # noqa: E402
from zyra_integrations.extraction_lineage import build_m1_01b_lineage_report  # noqa: E402
from zyra_integrations.extraction_rules import (  # noqa: E402
    RuleDecision,
    RuleReason,
    audit_extraction_plan,
    build_rule_coverage_report,
    claude_code_runtime_profile,
)
from zyra_integrations.source_extraction import ExtractionPlan, claude_code_m1_01b_plan  # noqa: E402
from zyra_runtime import RuntimeSurface, build_default_lifecycle, run_scaffold_fault_matrix, scaffold_lifecycle_payload  # noqa: E402
from zyra_runtime.extraction_runtime import run_extraction_runtime_controller  # noqa: E402
from zyra_workers.scaffold_bridge_runtime import run_worker_bridge_probes  # noqa: E402
from zyra_workers.scaffold_supervisor import build_worker_contract_matrix, run_supervised_scaffold_workers  # noqa: E402


class M101BRuntimeScaffoldAcceptanceTests(unittest.TestCase):
    def test_claude_code_rule_profile_blocks_escape_and_excludes_generated_stub(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_root = root / "claude-code-best"
            project_root = root / "zyra"
            source_root.mkdir()
            project_root.mkdir()
            (source_root / "src" / "query").mkdir(parents=True)
            (source_root / "src" / "query" / "config.ts").write_text("export const config = true;\n", encoding="utf-8")
            (source_root / "src" / "query" / "stub.ts").write_text("Auto-generated type stub\n", encoding="utf-8")
            plan = ExtractionPlan(
                source_repo="claude-code-best",
                source_root=source_root,
                project_root=project_root,
                target_root=project_root / "vendor-runtimes" / "claude-code-runtime",
                include_paths=["src/query", "../escape.ts"],
                owner_unit="M1-01B",
            )

            audit = audit_extraction_plan(plan)
            by_path = {item.repo_path: item for item in audit.evaluations}

            self.assertFalse(audit.ok)
            self.assertEqual(by_path["src/query/config.ts"].decision, RuleDecision.INCLUDE)
            self.assertEqual(by_path["src/query/stub.ts"].reason, RuleReason.EXCLUDED_BY_CONTENT)
            self.assertEqual(by_path["../escape.ts"].decision, RuleDecision.BLOCK)
            self.assertTrue(any(finding.code == "BLOCKED_EXTRACTION_PATH" for finding in audit.findings))

    def test_real_m1_01b_rule_audit_keeps_pilot_narrow(self) -> None:
        plan = claude_code_m1_01b_plan(project_root=ROOT, source_workspace_root=ROOT.parent, dry_run=True)
        audit = audit_extraction_plan(plan)
        summary = audit.summary()

        self.assertTrue(audit.ok, [finding.to_dict() for finding in audit.findings])
        self.assertGreaterEqual(summary["included_files"], 20)
        self.assertLessEqual(summary["included_files"], claude_code_runtime_profile().max_pilot_files)
        self.assertGreater(summary["source_like_lines"], 1000)
        self.assertTrue(all("../" not in path for path in audit.include_paths))

    def test_rule_coverage_matrix_maps_each_include_to_capability(self) -> None:
        plan = claude_code_m1_01b_plan(project_root=ROOT, source_workspace_root=ROOT.parent, dry_run=True)
        audit = audit_extraction_plan(plan)
        coverage = build_rule_coverage_report(audit)
        by_capability = coverage.by_capability()

        self.assertTrue(coverage.ok, [finding.to_dict() for finding in coverage.findings])
        self.assertEqual(coverage.summary["covered_include_rule_count"], coverage.summary["include_rule_count"])
        self.assertIn("tool contract", by_capability)
        self.assertIn("subagent contract", by_capability)
        self.assertIn("skill contract", by_capability)
        self.assertGreater(by_capability["shell permission"].included_files, 1)

    def test_runtime_lifecycle_disconnect_probe_fails_each_surface(self) -> None:
        lifecycle = build_default_lifecycle(ROOT)
        baseline = lifecycle.snapshot()

        self.assertTrue(baseline.ok, [item.to_dict() for item in baseline.invariants])
        for surface in RuntimeSurface:
            probe = lifecycle.disconnect_probe(surface)
            self.assertTrue(probe.ok, probe.to_dict())
            self.assertFalse(probe.disconnected_ok)
            self.assertTrue(probe.failing_invariants)

    def test_scaffold_lifecycle_payload_contains_event_and_probe_evidence(self) -> None:
        payload = scaffold_lifecycle_payload(ROOT)

        self.assertTrue(payload["ok"], payload)
        self.assertGreaterEqual(payload["event_count"], 8)
        self.assertEqual(len(payload["disconnect_probes"]), len(RuntimeSurface))
        self.assertTrue(all(item["ok"] for item in payload["disconnect_probes"]))
        self.assertTrue(payload["fault_matrix"]["ok"], payload["fault_matrix"])
        self.assertEqual(payload["fault_matrix"]["summary"]["failed_result_count"], 0)

    def test_scaffold_fault_matrix_catches_multiple_surface_failures(self) -> None:
        report = run_scaffold_fault_matrix(ROOT)
        by_kind = report.summary["fault_kinds"]

        self.assertTrue(report.ok, report.to_dict())
        self.assertEqual(report.summary["failed_result_count"], 0)
        self.assertIn("disconnect", by_kind)
        self.assertIn("not_ready", by_kind)
        self.assertIn("clear_state_ref", by_kind)
        self.assertIn("drop_event_ref", by_kind)
        self.assertEqual(report.summary["result_count"], len(RuntimeSurface) * 4)


    def test_worker_bridge_probes_execute_real_behavior(self) -> None:
        payload = run_worker_bridge_probes(ROOT)
        results = {item["worker_kind"]: item for item in payload["results"]}

        self.assertTrue(payload["ok"], json.dumps(payload, ensure_ascii=False, indent=2))
        self.assertEqual(payload["worker_count"], 5)
        self.assertIn("browser", results)
        browser_steps = {step["name"]: step for step in results["browser"]["steps"]}
        sandbox_steps = {step["name"]: step for step in results["sandbox"]["steps"]}
        memory_steps = {step["name"]: step for step in results["memory"]["steps"]}
        scheduler_steps = {step["name"]: step for step in results["scheduler"]["steps"]}
        self.assertTrue(browser_steps["browser_tool_executes"]["ok"])
        self.assertTrue(browser_steps["browser_extracts_text"]["ok"])
        self.assertTrue(sandbox_steps["sandbox_blocks_parent_read"]["ok"])
        self.assertTrue(memory_steps["memory_projection"]["ok"])
        self.assertTrue(scheduler_steps["selects_worker"]["ok"])

    def test_lineage_report_connects_sources_to_effective_targets(self) -> None:
        report = build_m1_01b_lineage_report(ROOT, source_workspace_root=ROOT.parent)

        self.assertTrue(report.ok, [finding.to_dict() for finding in report.findings])
        self.assertGreaterEqual(report.summary["effective_target_count"], 10)
        self.assertGreaterEqual(report.summary["source_pool_target_count"], 30)
        self.assertEqual(report.summary["missing_required_targets"], 0)
        self.assertGreater(report.summary["edge_kinds"]["derived_from"], 0)
        self.assertGreater(report.summary["edge_kinds"]["extracts_to"], 0)

    def test_runtime_controller_composes_acceptance_checks(self) -> None:
        report = run_extraction_runtime_controller(ROOT, source_workspace_root=ROOT.parent, include_worker_probes=False)
        checks = {str(check.kind): check for check in report.checks}

        self.assertTrue(report.ok, report.to_dict())
        self.assertIn("rule_audit", checks)
        self.assertIn("source_lineage", checks)
        self.assertIn("clean_boundary", checks)
        self.assertTrue(all(check.ok for check in report.checks))

    def test_worker_supervisor_runs_all_probe_contracts(self) -> None:
        report = run_supervised_scaffold_workers(ROOT, max_attempts=1)

        self.assertTrue(report.ok, report.to_dict())
        self.assertEqual(report.summary["worker_count"], 5)
        self.assertEqual(report.summary["failed_worker_count"], 0)
        self.assertGreaterEqual(len(report.events), 5)
        self.assertIsNotNone(report.contract_matrix)
        self.assertTrue(report.contract_matrix.ok, report.contract_matrix.to_dict())

    def test_worker_contract_matrix_requires_events_artifacts_and_scope(self) -> None:
        report = run_supervised_scaffold_workers(ROOT, max_attempts=1)
        matrix = build_worker_contract_matrix(
            ROOT,
            contracts=report.contract_matrix.rows and build_default_lifecycle(ROOT, complete=False).workers,
            attempts=report.attempts,
            events=report.events,
        )
        rows = {row.worker_kind: row for row in matrix.rows}

        self.assertTrue(matrix.ok, matrix.to_dict())
        self.assertEqual(matrix.summary["worker_count"], 5)
        self.assertEqual(matrix.summary["blocking_findings"], 0)
        self.assertIn("code", rows)
        self.assertIn("sandbox", rows)
        self.assertTrue(all(row.ok for row in rows.values()))
        self.assertGreater(matrix.summary["criteria_counts"]["artifact_output"], 0)

    def test_acceptance_report_composes_rules_ledger_runtime_and_worker_checks(self) -> None:
        report = build_m1_01b_acceptance_report(ROOT)
        summary = report.summary()
        checks = {check.name: check for check in report.checks}

        self.assertTrue(report.ok, [check.to_dict() for check in report.checks if not check.ok])
        self.assertTrue(summary["rule_audit_ok"])
        self.assertTrue(summary["rule_coverage_ok"])
        self.assertTrue(summary["runtime_ok"])
        self.assertTrue(summary["worker_ok"])
        self.assertTrue(summary["lineage_ok"])
        self.assertTrue(summary["controller_ok"])
        self.assertGreater(summary["accounting_entries"], 0)
        self.assertIn("rule-audit", checks)
        self.assertIn("rule-coverage", checks)
        self.assertIn("runtime-lifecycle", checks)
        self.assertIn("worker-bridge", checks)
        self.assertIn("source-lineage", checks)
        self.assertIn("runtime-controller", checks)

    def test_source_extract_cli_exposes_rule_audit_and_acceptance(self) -> None:
        rules = _run_json(["scripts/zyra_source_extract.py", "audit-rules", "--json"])
        acceptance = _run_json(["scripts/zyra_source_extract.py", "m1-01b-acceptance", "--json"])

        self.assertTrue(rules["summary"]["ok"], rules["findings"])
        self.assertTrue(acceptance["summary"]["ok"], acceptance["checks"])


def _run_json(args: list[str]) -> dict:
    completed = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()
