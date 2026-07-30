from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class M0M3InternalizationAuditTests(unittest.TestCase):
    def test_audit_script_reports_pass_with_debt(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/audit_m0_m3_internalization.py", "--json"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        report = json.loads(completed.stdout)

        self.assertEqual(report["status"], "pass_with_debt")
        self.assertGreater(report["line_counts"]["tracked_total"], 0)
        self.assertEqual(report["line_counts"]["tracked_vendor"], 0)
        self.assertGreater(report["line_counts"]["tracked_non_vendor"], 0)
        self.assertFalse(report["fatal"])
        retirement = report["legacy_source_retirement"]
        self.assertTrue(retirement["valid"], retirement["findings"])
        self.assertEqual(retirement["retired_file_count"], 3684)
        self.assertEqual(retirement["current_legacy_target_count"], 0)
        self.assertEqual(
            set(report["required_path_checks"]["m2"]["retired"]),
            {
                "vendor/claude-code-best/src/QueryEngine.ts",
                "vendor/claude-code-best/src/query.ts",
                "vendor/browser-use/browser_use/agent",
                "vendor/browser-use/browser_use/browser",
                "packages/integrations/zyra_integrations/vendor_manifest.py",
            },
        )
        self.assertEqual(
            report["scope"],
            "historical_pre_reset_m0_0_to_m0_3",
        )
        self.assertEqual(
            report["current_authority"],
            "docs/milestones/execution-state.yaml",
        )
        self.assertEqual(
            report["milestones"]["m2"]["assessment"],
            "accepted_as_heavy_integration_start_not_deep_internalization",
        )
        self.assertEqual(
            report["milestones"]["m3"]["assessment"],
            "accepted_as_symbolic_control_layer_with_internalization_debt",
        )
        debt_areas = {item["area"] for item in report["debt"]}
        self.assertEqual(debt_areas, {"historical audit scope"})

    def test_m3_verifier_uses_current_typescript_skill_and_scheduler_paths(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/verify_m3.py"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn("M3 verification passed", completed.stdout)

    def test_m3_01b_verifiers_bootstrap_repository_packages(self) -> None:
        for script in (
            "scripts/verify_m3_01_source_custody_closure.py",
            "scripts/verify_m3_s01b01_runtime_absorption.py",
            "scripts/verify_m3_s01b02_config_migration.py",
        ):
            with self.subTest(script=script):
                completed = subprocess.run(
                    [sys.executable, script, "--help"],
                    cwd=ROOT,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertIn("usage:", completed.stdout)

    def test_m3_01_slice_language_custody_is_machine_verifiable(self) -> None:
        cases = (
            (
                "M3-S01A-01-language-custody.json",
                "1d19ea39a8313091dcfbc00f78c79fdfdeab7cf4",
                "a8273df7601b57cf3fecfb03936c815b9ca63f38",
            ),
            (
                "M3-S01A-02-language-custody.json",
                "ce799c7fe02d1f5fc1832bbbff1e76b5a73e8caa",
                "2de115f565667eda0ab9664f98af3181f45460e9",
            ),
            (
                "M3-S01B-01-language-custody.json",
                "4b6d0d1d81332886385adcd32ff6205f6403d0f0",
                "d65856eebafd2a046953886b4605ec4ed2c06642",
            ),
            (
                "M3-S01B-02-language-custody.json",
                "9f2bfe85bc8324e2531e0ba636cc4410bda9334e",
                "7ff45a7c52539c920539d398a9469c3ef7c7aeb8",
            ),
        )
        evidence_root = Path(
            "docs/reviews/evidence/M3-01-independent-review"
        )
        for filename, base, target in cases:
            with self.subTest(filename=filename):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "scripts/verify_source_language_custody.py",
                        "--evidence",
                        str(evidence_root / filename),
                        "--base",
                        base,
                        "--target",
                        target,
                    ],
                    cwd=ROOT,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                report = json.loads(completed.stdout)
                self.assertTrue(report["ok"], report["violations"])


if __name__ == "__main__":
    unittest.main()
