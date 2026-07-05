from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "commands",
    ROOT / "packages" / "orchestration",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "workers",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "symbolic",
    ROOT / "packages" / "evaluation",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_evaluation import run_m2_scenarios


class M2DemoScenarioTests(unittest.TestCase):
    def test_m2_demo_scenario_runner_writes_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            report = run_m2_scenarios(ROOT, Path(tmpdir) / "reports")

            self.assertEqual(report["scenario_count"], 3)
            expected_passed = 3 if shutil.which("node") is not None else 2
            self.assertEqual(report["passed"], expected_passed)
            names = {scenario["name"] for scenario in report["scenarios"]}
            self.assertEqual(names, {"software_engineering", "browser_research", "dynamic_control"})
            dynamic = next(scenario for scenario in report["scenarios"] if scenario["name"] == "dynamic_control")
            self.assertEqual(dynamic["evaluation"]["metrics"]["requirement_change_count"], 1)
            self.assertEqual(dynamic["evaluation"]["metrics"]["failure_injection_count"], 1)
            self.assertEqual(dynamic["evaluation"]["metrics"]["skill_invocation_count"], 1)
            self.assertGreaterEqual(dynamic["evaluation"]["metrics"]["constraint_check_count"], 2)
            self.assertGreaterEqual(dynamic["evaluation"]["metrics"]["topology_route_count"], 2)
            self.assertGreaterEqual(dynamic["evaluation"]["metrics"]["replanned_node_count"], 1)
            self.assertTrue((Path(tmpdir) / "reports" / "m2_scenarios_report.json").exists())
            self.assertTrue((Path(tmpdir) / "reports" / "m2_scenarios_report.md").exists())


if __name__ == "__main__":
    unittest.main()
