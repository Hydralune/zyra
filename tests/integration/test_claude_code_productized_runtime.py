from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "workers",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations import (  # noqa: E402
    OverwritePolicy,
    SourceExtractor,
    assert_reference_crosswalk,
    build_claude_code_reference_crosswalk,
    claude_code_m1_02a_plan,
    reference_metadata_for_source,
)
from zyra_integrations.ledger_policy import REQUIRED_SOURCE_REPOS  # noqa: E402
from zyra_workers import CodeWorkerSidecarClient  # noqa: E402


class ClaudeCodeProductizedRuntimeTests(unittest.TestCase):
    def test_m1_02a_dry_run_extracts_productized_runtime_scope(self) -> None:
        plan = claude_code_m1_02a_plan(
            project_root=ROOT,
            source_workspace_root=ROOT.parent,
            dry_run=True,
            overwrite_policy=OverwritePolicy.IF_CHANGED,
        )

        report = SourceExtractor(plan).run()

        self.assertTrue(report.ok)
        self.assertGreaterEqual(report.effective_line_count, 18_000)
        self.assertGreater(report.upstream_type_stub_count, 0)
        self.assertGreater(report.upstream_type_stub_line_count, 0)
        self.assertGreaterEqual(len(report.ledger_upserts), 80)
        self.assertEqual(report.missing_count, 0)
        self.assertTrue(all(item.owner_unit == "M1-02A" for item in report.ledger_upserts))
        self.assertIn(
            "vendor-runtimes/claude-code-runtime/productized/claude-code-best/src/QueryEngine.ts",
            report.target_paths,
        )
        self.assertIn(
            "vendor-runtimes/claude-code-runtime/productized/claude-code-best/src/services/compact/compact.ts",
            report.target_paths,
        )

    def test_reference_only_crosswalk_links_auxiliary_docs_to_primary_sources(self) -> None:
        report = build_claude_code_reference_crosswalk(
            project_root=ROOT,
            source_workspace_root=ROOT.parent,
            target_mount="productized/claude-code-best",
        )

        assert_reference_crosswalk(report)
        summary = report.summary()
        self.assertTrue(report.ok)
        self.assertEqual(summary["primary_repo"], "claude-code-best")
        self.assertIn("claude-reviews-claude", summary["reference_only_repos"])
        self.assertIn("Dive-into-Claude-Code", summary["reference_only_repos"])
        self.assertNotIn("claude-reviews-claude", REQUIRED_SOURCE_REPOS)
        self.assertNotIn("Dive-into-Claude-Code", REQUIRED_SOURCE_REPOS)
        metadata = reference_metadata_for_source(report, "src/QueryEngine.ts")
        self.assertTrue(metadata["reference_only"])
        self.assertIn("query-engine-session-loop", metadata["crosswalk_ids"])

    @unittest.skipIf(shutil.which("node") is None, "node is required for code-worker sidecar")
    def test_sidecar_reads_productized_runtime_before_vendor_pool(self) -> None:
        client = CodeWorkerSidecarClient(ROOT)
        health = client.health()
        inventory = client.runtime_inventory()
        contract = client.query_contract()

        self.assertTrue(health["ok"])
        self.assertTrue(health["productizedRuntime"]["complete"])
        self.assertGreaterEqual(health["productizedRuntime"]["effectiveLineCount"], 18_000)
        self.assertTrue(inventory["productizedRuntime"]["moduleChecks"]["queryEngine"])
        self.assertTrue(inventory["productizedRuntime"]["moduleChecks"]["toolOrchestration"])
        self.assertTrue(inventory["productizedRuntime"]["referenceCrosswalk"]["ok"])
        self.assertIn("src/QueryEngine.ts", contract["sourceFiles"])
        self.assertTrue(contract["budgets"]["toolResultBudget"])
        self.assertTrue(contract["compactRuntime"]["autoCompact"])

    def test_productized_smoke_cli_reports_crosswalk_and_runtime_inventory(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/zyra_source_extract.py", "productized-smoke", "--json"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(completed.stdout)

        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["source_file_count"], 80)
        self.assertGreaterEqual(payload["effective_line_count"], 18_000)
        self.assertGreater(payload["upstream_type_stub_count"], 0)
        self.assertEqual(payload["crosswalk_summary"]["primary_repo"], "claude-code-best")
        self.assertEqual(payload["crosswalk_summary"]["missing_target_count"], 0)

    def test_runtime_readme_records_source_entries_and_auxiliary_usage(self) -> None:
        readme = ROOT / "vendor-runtimes" / "claude-code-runtime" / "README.md"
        text = readme.read_text(encoding="utf-8")

        self.assertIn("claude-code-best", text)
        self.assertIn("zyra-productized-smoke.mjs", text)
        self.assertIn("reference_crosswalk.json", text)
        self.assertIn("Auto-generated type stub", text)


if __name__ == "__main__":
    unittest.main()
