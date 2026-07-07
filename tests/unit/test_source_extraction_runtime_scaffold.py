from __future__ import annotations

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

from zyra_integrations import (  # noqa: E402
    ExtractionPlan,
    MainPathStatus,
    MigrationStrategy,
    OverwritePolicy,
    SourceExtractionError,
    SourceExtractor,
    claude_code_m1_01b_plan,
    write_runtime_scaffold_files,
)
from zyra_runtime.scaffold import (  # noqa: E402
    RuntimeOperation,
    RuntimeSurface,
    default_m1_01b_runtime_scaffold,
    runtime_scaffold_event_payload,
)
from zyra_workers.runtime_scaffold import (  # noqa: E402
    WorkerScaffoldKind,
    build_m1_01b_worker_scaffolds,
    worker_scaffold_health_payload,
)


class SourceExtractionRuntimeScaffoldTests(unittest.TestCase):
    def test_claude_pilot_dry_run_reports_vendor_runtime_source_scale_without_effective_credit(self) -> None:
        plan = claude_code_m1_01b_plan(
            project_root=ROOT,
            source_workspace_root=ROOT.parent,
            dry_run=True,
            overwrite_policy=OverwritePolicy.IF_CHANGED,
        )

        report = SourceExtractor(plan).run()

        self.assertTrue(report.ok)
        self.assertGreaterEqual(report.raw_line_count, 10_000)
        self.assertEqual(report.effective_line_count, 0)
        self.assertEqual(report.missing_count, 0)
        self.assertEqual(report.excluded_count, 0)
        self.assertGreaterEqual(len(report.ledger_upserts), 30)
        self.assertIn("vendor-runtimes/claude-code-runtime/pilot/claude-code-best/src/Tool.ts", report.target_paths)
        self.assertTrue(all(item.owner_unit == "M1-01B" for item in report.ledger_upserts))

    def test_extractor_enforces_source_and_target_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_root = root / "source"
            project_root = root / "project"
            source_root.mkdir()
            project_root.mkdir()
            (source_root / "ok.ts").write_text("export const ok = true;\n", encoding="utf-8")
            parent_plan = ExtractionPlan(
                source_repo="test-repo",
                source_root=source_root,
                project_root=project_root,
                target_root=project_root / "vendor-runtimes" / "test",
                include_paths=["../outside.ts"],
            )
            escaping_target_plan = ExtractionPlan(
                source_repo="test-repo",
                source_root=source_root,
                project_root=project_root,
                target_root=root / "outside-runtime",
                include_paths=["ok.ts"],
            )

            with self.assertRaises(SourceExtractionError):
                SourceExtractor(parent_plan).run()
            with self.assertRaises(SourceExtractionError):
                SourceExtractor(escaping_target_plan)

    def test_extractor_writes_manifest_report_and_ledger_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_root = root / "source"
            project_root = root / "project"
            source_root.mkdir()
            project_root.mkdir()
            (source_root / "runtime.ts").write_text("export function runtime() { return 'ok'; }\n", encoding="utf-8")
            (source_root / "README.md").write_text("not runtime\n", encoding="utf-8")
            plan = ExtractionPlan(
                source_repo="test-repo",
                source_root=source_root,
                project_root=project_root,
                target_root=project_root / "vendor-runtimes" / "test-runtime",
                include_paths=["runtime.ts", "README.md"],
                owner_unit="M1-01B",
            )

            report = SourceExtractor(plan).run()
            entries = SourceExtractor(plan).build_ledger_entries(report)

            self.assertTrue(report.ok)
            self.assertEqual(report.copied_count, 1)
            self.assertEqual(report.excluded_count, 1)
            self.assertEqual(len(report.ledger_upserts), 1)
            self.assertTrue((project_root / "vendor-runtimes" / "test-runtime" / "src" / "zyra-pilot-manifest.mjs").exists())
            inventory = project_root / "vendor-runtimes" / "test-runtime" / "metadata" / "source_inventory.json"
            self.assertIn('"ledger_upsert_count": 1', inventory.read_text(encoding="utf-8"))
            self.assertEqual(entries[0].migration_strategy, MigrationStrategy.ADAPTER)
            self.assertEqual(entries[0].main_path_status, MainPathStatus.WORKER_RUNTIME_CONNECTED)
            self.assertEqual(entries[0].source_evidence[0].source_path, "runtime.ts")
            self.assertNotIn("vendor-runtimes", entries[0].main_path.surfaces)
            self.assertTrue(all("vendor-runtimes" not in ref for ref in entries[0].runtime_entry.config_refs))
            self.assertNotIn("source_evidence", entries[0].metadata)

            always_plan = ExtractionPlan(
                source_repo="test-repo",
                source_root=source_root,
                project_root=project_root,
                target_root=project_root / "vendor-runtimes" / "test-runtime",
                include_paths=["runtime.ts"],
                owner_unit="M1-01B",
                overwrite_policy=OverwritePolicy.ALWAYS,
            )
            forced = SourceExtractor(always_plan).run()
            self.assertEqual(forced.copied_count, 1)
            self.assertEqual(forced.copied[0].reason, "overwrite policy is always")

    def test_runtime_scaffold_defines_required_surfaces_and_event_payload(self) -> None:
        scaffold = default_m1_01b_runtime_scaffold(ROOT)
        surfaces = {component.surface for component in scaffold.components}
        payload = runtime_scaffold_event_payload(scaffold)

        self.assertTrue(scaffold.health()["ok"])
        self.assertEqual(scaffold.health()["component_count"], 7)
        for surface in RuntimeSurface:
            self.assertIn(surface, surfaces)
        self.assertEqual(scaffold.permission_contract.operation, RuntimeOperation.SHELL)
        self.assertIn("runtime_scaffold_health", payload)
        self.assertEqual(payload["runtime_scaffold_health"]["worker_bridge_count"], 5)

    def test_worker_scaffolds_health_and_smoke_boundaries(self) -> None:
        write_runtime_scaffold_files(ROOT)
        scaffold = default_m1_01b_runtime_scaffold(ROOT)
        workers = build_m1_01b_worker_scaffolds(scaffold, project_root=ROOT)
        health = worker_scaffold_health_payload(scaffold, project_root=ROOT)

        self.assertEqual({worker.worker_kind for worker in workers}, set(WorkerScaffoldKind))
        self.assertTrue(health["ok"])
        self.assertEqual(health["worker_count"], 5)
        for worker in workers:
            smoke = worker.smoke()
            self.assertTrue(smoke.ok, smoke.messages)
            self.assertIn("worker_scaffold_smoke", smoke.event_payload)


if __name__ == "__main__":
    unittest.main()
