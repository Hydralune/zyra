from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
    LedgerQuery,
    LicenseNotice,
    LineCountPolicy,
    MainPathBinding,
    MainPathStatus,
    MigrationStrategy,
    NoticeStatus,
    RuntimeEntry,
    TargetBinding,
    TestEntry,
    load_seed_ledger,
)
from zyra_integrations.ledger_audit import (
    REQUIRED_SOURCE_REPOS,
    AuditFindingCode,
    _python_runtime_dependency_fragments,
)
from zyra_integrations.ledger_migrations import normalize_ledger_for_current_policy


class InternalizationLedgerTests(unittest.TestCase):
    def test_forward_excluded_openclaw_is_historical_not_required(self) -> None:
        self.assertNotIn("openclaw", REQUIRED_SOURCE_REPOS)
        self.assertIn("openclaw", load_seed_ledger().summary().by_source_repo)

    def test_dependency_literal_scan_ignores_denylist_but_catches_runtime_path(self) -> None:
        forbidden = ["../claude-code-best"]

        self.assertEqual(
            _python_runtime_dependency_fragments(
                "FORBIDDEN_PATH_MARKERS = ('../claude-code-best',)\n",
                forbidden,
            ),
            [],
        )
        self.assertEqual(
            _python_runtime_dependency_fragments(
                "runtime_root = '../claude-code-best'\n",
                forbidden,
            ),
            forbidden,
        )

    def test_current_policy_remaps_retired_connected_targets(self) -> None:
        legacy = InternalizationLedgerEntry.new(
            source_repo="claude-code-best",
            source_path="src/QueryEngine.ts",
            capability_name="retired connected paths",
            capability_summary="historical row that still names temporary owners",
            target_paths=[
                "apps/code-worker/src/main.mjs",
                "packages/workers/zyra_workers/code_query_loop.py",
            ],
            migration_strategy=MigrationStrategy.DIRECT_PORT,
            main_path_status=MainPathStatus.WORKER_RUNTIME_CONNECTED,
            lifecycle=LedgerLifecycle.INTERNALIZED,
            owner_unit="M1-01B",
            milestone="M1",
            line_count_policy=LineCountPolicy.COUNTS_AS_RUNTIME,
        )
        legacy.runtime_entry.command = "node apps/code-worker/src/main.mjs"
        legacy.runtime_entry.config_refs = ["packages/workers/zyra_workers/code_query_loop.py"]
        legacy.main_path.surfaces = ["apps/code-worker/src/main.mjs"]

        entries = normalize_ledger_for_current_policy([legacy])
        migrated = next(entry for entry in entries if entry.ledger_id == legacy.ledger_id)

        self.assertEqual(
            migrated.target_paths,
            [
                "apps/code-worker/src/main.ts",
                "packages/runtime/claude-runtime/src/query-engine.ts",
            ],
        )
        self.assertEqual(migrated.runtime_entry.command, "node apps/code-worker/src/main.ts")
        self.assertEqual(
            migrated.runtime_entry.config_refs,
            ["packages/runtime/claude-runtime/src/query-engine.ts"],
        )
        self.assertEqual(migrated.main_path.surfaces, ["apps/code-worker/src/main.ts"])

    def test_seed_ledger_covers_required_repositories_and_units(self) -> None:
        ledger = load_seed_ledger()
        summary = ledger.summary()

        self.assertGreaterEqual(len(ledger), 800)
        for repo in [
            "claude-code-best",
            "browser-use",
            "OpenHands",
            "openclaw",
            "agentscope",
            "agent-framework",
            "hermes-agent",
            "langgraph",
            "opencode",
        ]:
            self.assertIn(repo, summary.by_source_repo)
        self.assertGreater(summary.by_owner_unit["M1-02B"], 0)
        self.assertGreater(summary.by_owner_unit["M1-04A"], 0)
        self.assertGreater(summary.active_or_internalized, 0)

    def test_entry_validation_requires_target_test_and_main_path_status(self) -> None:
        entry = InternalizationLedgerEntry(
            ledger_id="bad",
            source_repo="claude-code-best",
            source_path="src/QueryEngine.ts",
            capability_name="bad entry",
            capability_summary="missing target and tests",
            target_bindings=[],
            migration_strategy=MigrationStrategy.VENDORED_RUNTIME,
            main_path_status=MainPathStatus.PLANNED,
        )

        errors = entry.validate()

        self.assertTrue(any("target_path" in error for error in errors))
        self.assertTrue(any("test_entry" in error for error in errors))

    def test_json_and_yaml_extension_round_trip_use_dependency_free_payload(self) -> None:
        ledger = InternalizationLedger([sample_entry()])

        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = Path(tmpdir) / "ledger.json"
            yaml_path = Path(tmpdir) / "ledger.yaml"
            ledger.save(json_path)
            ledger.save(yaml_path)

            loaded_json = InternalizationLedger.load(json_path)
            loaded_yaml = InternalizationLedger.load(yaml_path)

        self.assertEqual(loaded_json.summary().total_entries, 1)
        self.assertEqual(loaded_yaml.summary().total_entries, 1)
        self.assertEqual(loaded_yaml.entries()[0].source_repo, "claude-code-best")

    def test_query_filters_by_repo_unit_status_target_and_capability(self) -> None:
        ledger = load_seed_ledger()

        query = LedgerQuery(
            source_repo="claude-code-best",
            owner_unit="M1-02B",
            main_path_status=MainPathStatus.VENDORED,
            target_contains="packages/workers",
            capability_contains="query",
            limit=5,
        )
        entries = ledger.query(query)

        self.assertTrue(entries)
        self.assertLessEqual(len(entries), 5)
        self.assertTrue(all(entry.source_repo == "claude-code-best" for entry in entries))
        self.assertTrue(all(entry.owner_unit == "M1-02B" for entry in entries))

    def test_audit_reports_missing_target_test_and_false_connected_status(self) -> None:
        entry = InternalizationLedgerEntry(
            ledger_id="ile_bad",
            source_repo="claude-code-best",
            source_path="src/QueryEngine.ts",
            capability_name="bad connected",
            capability_summary="claims connected without evidence",
            target_bindings=[],
            migration_strategy=MigrationStrategy.VENDORED_RUNTIME,
            main_path_status=MainPathStatus.WORKER_RUNTIME_CONNECTED,
            lifecycle=LedgerLifecycle.ACTIVE,
            license_notice=LicenseNotice(source_repo="claude-code-best", status=NoticeStatus.RECORDED),
            line_count_policy=LineCountPolicy.COUNTS_AS_RUNTIME,
        )
        ledger = InternalizationLedger([entry])

        report = InternalizationLedgerAuditor(ROOT, strict=False).audit(ledger)
        codes = {finding.code for finding in report.findings}

        self.assertFalse(report.ok)
        self.assertIn(AuditFindingCode.MISSING_TARGET_PATH, codes)
        self.assertIn(AuditFindingCode.MISSING_TEST_ENTRY, codes)
        self.assertIn(AuditFindingCode.FALSE_CONNECTED_STATUS, codes)

    def test_audit_treats_planned_unmaterialized_targets_as_warnings_not_failure(self) -> None:
        ledger = load_seed_ledger()
        entry = sample_entry()
        entry.ledger_id = "ile_test_planned_future_target"
        entry.target_bindings = [TargetBinding("packages/integrations/zyra_integrations/future_adapter.py")]
        ledger.upsert(entry)

        report = InternalizationLedgerAuditor(ROOT, strict=False).audit(ledger)
        codes = {finding.code for finding in report.findings}

        self.assertTrue(report.ok)
        self.assertIn(AuditFindingCode.PLANNED_TARGET_NOT_MATERIALIZED, codes)

    def test_upsert_and_remove_record_mutations(self) -> None:
        ledger = InternalizationLedger()
        entry = sample_entry()

        add = ledger.upsert(entry)
        entry.capability_summary = "updated"
        update = ledger.upsert(entry)
        remove = ledger.remove(entry.ledger_id)

        self.assertEqual(add.action, "add")
        self.assertEqual(update.action, "update")
        self.assertEqual(remove.action, "remove")
        self.assertEqual(len(ledger), 0)

    def test_entry_serialization_is_stable_json(self) -> None:
        entry = sample_entry()
        payload = entry.to_dict()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        decoded = InternalizationLedgerEntry.from_dict(json.loads(encoded))

        self.assertEqual(decoded.ledger_id, entry.ledger_id)
        self.assertEqual(decoded.runtime_entry.module, entry.runtime_entry.module)
        self.assertEqual(decoded.test_entries[0].path, entry.test_entries[0].path)

    def test_strict_audit_scanner_uses_git_file_list_and_filters_ignored_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "packages" / "runtime" / "live.py"
            ignored = root / ".git" / "config"
            source.parent.mkdir(parents=True)
            ignored.parent.mkdir(parents=True)
            source.write_text("RUNTIME_SOURCE = '../claude-code-best'\n", encoding="utf-8")
            ignored.write_text("ignored ../claude-code-best\n", encoding="utf-8")

            with patch(
                "zyra_integrations.ledger_audit.subprocess.run",
                return_value=SimpleNamespace(stdout="packages/runtime/live.py\n.git/config\n"),
            ):
                scanned = InternalizationLedgerAuditor(root, strict=True)._iter_scanned_project_files()

        self.assertEqual([path.as_posix() for path in scanned], [source.as_posix()])


def sample_entry() -> InternalizationLedgerEntry:
    return InternalizationLedgerEntry.new(
        source_repo="claude-code-best",
        source_path="src/QueryEngine.ts",
        capability_name="sample query engine",
        capability_summary="sample source-to-target entry",
        target_paths=["packages/integrations/zyra_integrations/ledger_models.py"],
        migration_strategy=MigrationStrategy.PLANNED_ADAPTER,
        main_path_status=MainPathStatus.PLANNED,
        lifecycle=LedgerLifecycle.PLANNED,
        owner_unit="M1-01A",
        milestone="M1",
        runtime_entry=RuntimeEntry(module="zyra_integrations.ledger_models", function="InternalizationLedgerEntry"),
        test_entries=[TestEntry(path="tests/unit/test_internalization_ledger.py", command="python -m unittest tests.unit.test_internalization_ledger")],
        main_path=MainPathBinding(surfaces=["ledger"], event_types=["system_notice"], api_routes=["GET /ledger"]),
        license_notice=LicenseNotice(source_repo="claude-code-best", status=NoticeStatus.PENDING),
    )


if __name__ == "__main__":
    unittest.main()
