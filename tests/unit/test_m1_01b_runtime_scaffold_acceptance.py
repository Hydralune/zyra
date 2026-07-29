from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_root in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "workers",
):
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))

from zyra_runtime.extraction_runtime import (  # noqa: E402
    ExtractionRuntimeController,
    RuntimeCheckKind,
)
from zyra_workers.scaffold_bridge_runtime import (  # noqa: E402
    build_bridge_runtimes,
)


class RuntimeScaffoldRetirementAcceptanceTests(unittest.TestCase):
    def test_controller_marks_rule_and_lineage_inputs_as_retired(self) -> None:
        controller = ExtractionRuntimeController(ROOT)

        rule = controller._check_rule_audit()
        lineage = controller._check_lineage()

        self.assertEqual(rule.kind, RuntimeCheckKind.RULE_AUDIT)
        self.assertTrue(rule.ok)
        self.assertEqual(rule.evidence["status"], "retired")
        self.assertFalse(rule.evidence["source_workspace_required"])
        self.assertFalse(rule.evidence["writer_available"])
        self.assertFalse(rule.evidence["fallback_available"])
        self.assertEqual(lineage.kind, RuntimeCheckKind.SOURCE_LINEAGE)
        self.assertTrue(lineage.ok)
        self.assertEqual(lineage.evidence["status"], "historical_frozen")
        self.assertEqual(lineage.evidence["current_legacy_target_count"], 0)

    def test_controller_plan_has_no_external_source_workspace(self) -> None:
        plan = ExtractionRuntimeController(
            ROOT,
            source_workspace_root=ROOT.parent / "must-not-be-used",
        ).build_plan()

        self.assertEqual(
            plan.source_workspace_root,
            "not_applicable_legacy_source_pool_retired",
        )

    def test_code_bridge_probes_formal_runtime_files(self) -> None:
        code_runtime = next(
            runtime
            for runtime in build_bridge_runtimes(project_root=ROOT)
            if runtime.contract.worker_kind == "code"
        )

        steps = {step.name: step for step in code_runtime._probe_code()}

        self.assertTrue(steps["formal_runtime_root"].ok)
        self.assertTrue(steps["formal_runtime_package"].ok)
        self.assertTrue(steps["formal_source_identity"].ok)
        self.assertTrue(steps["canonical_typescript_entrypoint"].ok)
        self.assertNotIn("pilot_manifest", steps)
        self.assertNotIn("productized_manifest", steps)


if __name__ == "__main__":
    unittest.main()
