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
        self.assertGreater(report["line_counts"]["tracked_vendor"], 0)
        self.assertGreater(report["line_counts"]["tracked_non_vendor"], 0)
        self.assertFalse(report["fatal"])
        self.assertEqual(
            report["milestones"]["m2"]["assessment"],
            "accepted_as_heavy_integration_start_not_deep_internalization",
        )
        self.assertEqual(
            report["milestones"]["m3"]["assessment"],
            "accepted_as_symbolic_control_layer_with_internalization_debt",
        )
        debt_areas = {item["area"] for item in report["debt"]}
        self.assertIn("memory and compaction", debt_areas)
        self.assertIn("scheduler and fault recovery", debt_areas)


if __name__ == "__main__":
    unittest.main()
