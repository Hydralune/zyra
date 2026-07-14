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

from zyra_integrations.ledger_migrations import normalize_ledger_for_current_policy  # noqa: E402
from zyra_integrations.ledger_models import (  # noqa: E402
    InternalizationLedgerEntry,
    LedgerLifecycle,
    LineCountPolicy,
    MainPathStatus,
    MigrationStrategy,
)
from zyra_runtime import (  # noqa: E402
    ClaudeProductizationFoundation,
    FoundationExecutionRequest,
    TargetSurfaceSpec,
    SurfaceKind,
)
from zyra_workers import ClaudeProductizationFoundationWorker  # noqa: E402


class ClaudeProductizationFoundationTests(unittest.TestCase):
    def test_foundation_probe_uses_zyra_owned_targets_and_emits_event(self) -> None:
        foundation = ClaudeProductizationFoundation(ROOT, source_workspace_root=ROOT.parent)

        result = foundation.inspect(FoundationExecutionRequest(run_id="run-foundation-test"))

        self.assertTrue(result.ok, [finding.to_dict() for finding in result.findings if finding.blocking])
        self.assertEqual(result.coverage["boundary_count"], 8)
        self.assertGreaterEqual(result.coverage["primary_source_count"], 8)
        self.assertGreaterEqual(result.coverage["zyra_target_count"], 20)
        self.assertEqual(result.coverage["source_pool_target_count"], 0)
        self.assertEqual(result.blocking_count, 0)
        self.assertIn("active", "".join(result.decision_counts.keys()))
        self.assertTrue(result.event_records)
        self.assertEqual(result.event_records[0].run_id, "run-foundation-test")

    def test_source_pool_surfaces_are_rejected_when_marked_main_path(self) -> None:
        surface = TargetSurfaceSpec(
            "vendor-runtimes/claude-code-runtime/productized/claude-code-best/src/QueryEngine.ts",
            SurfaceKind.RUNTIME_PACKAGE,
            "source-pool",
            required_for_main_path=True,
        )

        self.assertTrue(surface.is_source_pool_like)

    def test_ledger_migration_downgrades_legacy_productized_rows_and_adds_foundation_rows(self) -> None:
        legacy = InternalizationLedgerEntry.new(
            source_repo="claude-code-best",
            source_path="src/QueryEngine.ts",
            capability_name="claude_code_runtime_productized_src_QueryEngine_ts",
            capability_summary="legacy productized row",
            target_paths=[
                "vendor-runtimes/claude-code-runtime/productized/claude-code-best/src/QueryEngine.ts"
            ],
            migration_strategy=MigrationStrategy.VENDORED_RUNTIME,
            main_path_status=MainPathStatus.WORKER_RUNTIME_CONNECTED,
            lifecycle=LedgerLifecycle.PRODUCTIZED,
            owner_unit="M1-02A",
            milestone="M1",
            line_count_policy=LineCountPolicy.COUNTS_AS_RUNTIME,
            effective_line_count=1200,
            source_pool_target="vendor-runtimes/claude-code-runtime/productized/claude-code-best/src/QueryEngine.ts",
            effective_target_count=1,
        )

        entries = normalize_ledger_for_current_policy([legacy])
        migrated = next(entry for entry in entries if entry.ledger_id == legacy.ledger_id)
        foundation_rows = [
            entry
            for entry in entries
            if entry.owner_unit == "M1-02A" and "claude-productization-foundation" in entry.tags
        ]

        self.assertEqual(migrated.main_path_status, MainPathStatus.INVENTORIED)
        self.assertEqual(migrated.lifecycle, LedgerLifecycle.CANDIDATE)
        self.assertEqual(migrated.migration_strategy, MigrationStrategy.CANDIDATE_REVIEW)
        self.assertEqual(migrated.line_count_policy, LineCountPolicy.EXCLUDED_INVENTORY_ONLY)
        self.assertEqual(migrated.metadata["effective_line_count"], 0)
        self.assertTrue(all(binding.role == "source_pool" for binding in migrated.target_bindings))
        self.assertTrue(all(not binding.required_for_main_path for binding in migrated.target_bindings))
        self.assertEqual(len(foundation_rows), 8)
        self.assertTrue(all(entry.main_path_status == MainPathStatus.WORKER_RUNTIME_CONNECTED for entry in foundation_rows))
        self.assertTrue(all(entry.line_count_policy == LineCountPolicy.COUNTS_AS_RUNTIME for entry in foundation_rows))

    def test_ledger_migration_downgrades_legacy_vendored_connected_seed_row(self) -> None:
        legacy = InternalizationLedgerEntry.new(
            source_repo="claude-code-best",
            source_path="src/QueryEngine.ts",
            capability_name="Legacy M0 CodeWorker sidecar inventory",
            capability_summary="legacy vendored connected seed row",
            target_paths=[
                "apps/code-worker/src/main.ts",
                "packages/workers/zyra_workers/code_worker_bridge.py",
            ],
            migration_strategy=MigrationStrategy.VENDORED_RUNTIME,
            main_path_status=MainPathStatus.WORKER_RUNTIME_CONNECTED,
            lifecycle=LedgerLifecycle.ACTIVE,
            owner_unit="M1-02A",
            milestone="M1",
            line_count_policy=LineCountPolicy.COUNTS_AS_RUNTIME,
            legacy=True,
        )
        legacy.tags = ["legacy-m0", "main-path", "seeded"]

        entries = normalize_ledger_for_current_policy([legacy])
        migrated = next(entry for entry in entries if entry.ledger_id == legacy.ledger_id)

        self.assertEqual(migrated.main_path_status, MainPathStatus.INVENTORIED)
        self.assertEqual(migrated.lifecycle, LedgerLifecycle.CANDIDATE)
        self.assertEqual(migrated.migration_strategy, MigrationStrategy.CANDIDATE_REVIEW)
        self.assertEqual(migrated.line_count_policy, LineCountPolicy.EXCLUDED_INVENTORY_ONLY)
        self.assertTrue(all(binding.role == "legacy_zyra_surface" for binding in migrated.target_bindings))
        self.assertTrue(all(not binding.required_for_main_path for binding in migrated.target_bindings))

    def test_worker_run_writes_probe_artifact_and_preserves_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = ClaudeProductizationFoundationWorker(
                project_root=ROOT,
                artifact_root=Path(tmp),
                source_workspace_root=ROOT.parent,
            )

            run = worker.run()

            self.assertTrue(run.worker_result.ok)
            self.assertEqual(run.worker_result.metadata["claude_foundation_owner_unit"], "M1-02A")
            self.assertEqual(run.worker_result.metadata["claude_foundation_blocking_count"], "0")
            self.assertEqual(len(run.worker_result.artifacts), 1)
            self.assertTrue(Path(run.worker_result.artifacts[0].uri).exists())
            self.assertGreaterEqual(len(run.event_records), 2)


if __name__ == "__main__":
    unittest.main()
