from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
for package_path in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


from zyra_integrations import (  # noqa: E402
    InternalizationLedger,
    InternalizationLedgerEntry,
    RuntimeEntry,
    build_reachability_report,
)
from zyra_integrations.ledger_reachability import (  # noqa: E402
    build_runtime_probe,
    discover_api_routes,
    discover_event_producers,
)


class SkillMemoryReachabilityContractTests(unittest.TestCase):
    def test_dynamic_routes_and_typescript_events_are_statically_auditable(self) -> None:
        routes = {(probe.method, probe.route) for probe in discover_api_routes(ROOT)}
        events = {probe.event_type for probe in discover_event_producers(ROOT)}

        self.assertIn(("GET", "/tasks/{task_id}/memory/procedures"), routes)
        self.assertIn(("POST", "/tasks/{task_id}/memory/procedures/mine"), routes)
        self.assertIn(("POST", "/tasks/{task_id}/memory/procedures/routing"), routes)
        self.assertIn(("POST", "/tasks/{task_id}/memory/procedures/recovery"), routes)
        self.assertIn(("POST", "/tasks/{task_id}/memory/procedures/context"), routes)
        self.assertIn("skill_memory_updated", events)
        self.assertIn("skill_memory_compact_trigger", events)
        self.assertIn("skill_memory_compact_restored", events)

    def test_workspace_runtime_probe_resolves_exported_typescript_owner(self) -> None:
        entry = InternalizationLedgerEntry.new(
            source_repo="claude-code-best",
            source_path="src/tools/SkillTool/SkillTool.ts",
            capability_name="skill-memory-probe",
            capability_summary="probe the Zyra TypeScript workspace export",
            target_paths=["packages/memory/skill-memory-runtime/src/runtime.ts"],
            runtime_entry=RuntimeEntry(
                module="@zyra/skill-memory-runtime",
                function="SkillMemoryApplication",
                protocol="zyra.skill-memory/v1",
            ),
        )

        probe = build_runtime_probe(entry, project_root=ROOT)

        self.assertTrue(probe.importable)
        self.assertTrue(probe.callable)
        self.assertIn("workspace:packages/memory/skill-memory-runtime", probe.evidence)

    def test_current_slice_entries_have_no_unreachable_runtime_surface(self) -> None:
        ledger = InternalizationLedger.load(
            ROOT
            / "packages"
            / "integrations"
            / "zyra_integrations"
            / "data"
            / "internalization_ledger_seed.json",
            normalize_current_policy=True,
        )

        report = build_reachability_report(
            ROOT,
            ledger,
            owner_unit="M1-S06C-01",
            strict_audit=False,
        )

        self.assertEqual(report.total_entries, 3)
        self.assertEqual(report.unreachable_entries, 0)
        self.assertTrue(all(entry.reachable for entry in report.entries))


if __name__ == "__main__":
    unittest.main()
