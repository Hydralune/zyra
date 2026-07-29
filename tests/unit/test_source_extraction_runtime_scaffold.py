from __future__ import annotations

import sys
import tempfile
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

from zyra_integrations.source_extraction import (  # noqa: E402
    LegacySourcePoolRetiredError,
    claude_code_m1_01b_plan,
    claude_code_m1_02a_plan,
    claude_code_m1_02b_plan,
    claude_code_m1_02c_plan,
    write_productized_runtime_files,
    write_runtime_scaffold_files,
)
from zyra_runtime import default_m1_01b_runtime_scaffold  # noqa: E402
from zyra_workers.runtime_scaffold import (  # noqa: E402
    build_m1_01b_worker_scaffolds,
    worker_scaffold_health_payload,
)


class SourceExtractionRetirementTests(unittest.TestCase):
    def test_all_historical_plans_fail_closed(self) -> None:
        for builder in (
            claude_code_m1_01b_plan,
            claude_code_m1_02a_plan,
            claude_code_m1_02b_plan,
            claude_code_m1_02c_plan,
        ):
            with self.subTest(builder=builder.__name__):
                with self.assertRaises(LegacySourcePoolRetiredError):
                    builder(
                        project_root=ROOT,
                        source_workspace_root=ROOT.parent,
                        dry_run=True,
                    )

    def test_historical_writers_create_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for writer in (
                write_runtime_scaffold_files,
                write_productized_runtime_files,
            ):
                with self.subTest(writer=writer.__name__):
                    with self.assertRaises(LegacySourcePoolRetiredError):
                        writer(root)
            self.assertEqual(list(root.iterdir()), [])

    def test_code_worker_scaffold_uses_formal_runtime_owner(self) -> None:
        scaffold = default_m1_01b_runtime_scaffold(ROOT)
        code = next(
            item
            for item in build_m1_01b_worker_scaffolds(
                scaffold,
                project_root=ROOT,
            )
            if item.contract.worker_kind == "code"
        )

        health = code.health()

        self.assertTrue(health.ok, health.to_dict())
        self.assertTrue(health.checks["has_formal_runtime_root"])
        self.assertTrue(health.checks["has_formal_runtime_package"])
        self.assertTrue(health.checks["has_formal_source_identity"])
        self.assertTrue(health.checks["has_canonical_typescript_entrypoint"])
        self.assertNotIn("has_reference_crosswalk", health.checks)

    def test_all_worker_scaffolds_are_healthy_without_source_pool(self) -> None:
        payload = worker_scaffold_health_payload(project_root=ROOT)

        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["worker_count"], 5)
        self.assertFalse((ROOT / "vendor").exists())
        self.assertFalse((ROOT / "vendor-runtimes").exists())


if __name__ == "__main__":
    unittest.main()
