from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations import InternalizationLedger
from tests.unit.test_internalization_ledger_policy_linecount import sample_entry


class InternalizationLedgerGateCliTests(unittest.TestCase):
    def test_cli_matrix_gate_snapshot_and_source_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = os.environ.copy()
            ledger_path = Path(tmpdir) / "ledger.json"
            env["ZYRA_INTEGRATION_LEDGER"] = str(ledger_path)
            InternalizationLedger([sample_entry()]).save(ledger_path)

            matrix = _run(["matrix", "--json"], env)
            self.assertEqual(matrix["unit_count"], 39)
            self.assertIn("M1-01A", matrix["dependency_order"])

            gate = _run(["gate", "--owner-unit", "M1-01A", "--json"], env)
            self.assertEqual(gate["owner_unit"], "M1-01A")
            self.assertIn("readiness", gate)

            accounting = _run(["accounting", "--owner-unit", "M1-01A", "--no-entries", "--json"], env)
            self.assertEqual(accounting["summary"]["owner_unit"], "M1-01A")
            self.assertIn("source_accounts", accounting)
            self.assertIn("unit_accounts", accounting)

            scan = _run(["source-scan", "--json"], env)
            self.assertIn("forbidden_hits", scan)
            self.assertIn("target_verifications", scan)

            snapshot = _run(["snapshot", "--label", "cli-test", "--owner-unit", "M1-01A", "--json"], env)
            self.assertTrue(Path(snapshot["path"]).exists())

            snapshots = _run(["snapshots", "--json"], env)
            self.assertTrue(any(item["snapshot_id"] == snapshot["snapshot"]["identity"]["snapshot_id"] for item in snapshots["snapshots"]))

    def test_cli_linecount_reports_seed_exclusion_and_shortfall(self) -> None:
        completed = subprocess.run(
            [
                str(ROOT / ".venv" / "Scripts" / "python.exe"),
                "scripts/zyra_integration_ledger.py",
                "linecount",
                "--base",
                "68587549447cacfdbf7992387823b5af6f7f9cf3",
                "--owner-unit",
                "M1-01A",
                "--minimum-effective-lines",
                "10000",
                "--json",
            ],
            cwd=ROOT,
            check=True,
            text=True,
            capture_output=True,
        )
        payload = json.loads(completed.stdout)

        self.assertGreater(payload["raw_added"], payload["effective_added"])
        self.assertTrue(payload["seed_or_inventory_excluded"])

    def test_cli_advance_rejects_unknown_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = os.environ.copy()
            ledger_path = Path(tmpdir) / "ledger.json"
            env["ZYRA_INTEGRATION_LEDGER"] = str(ledger_path)
            InternalizationLedger([sample_entry()]).save(ledger_path)

            completed = subprocess.run(
                [
                    str(ROOT / ".venv" / "Scripts" / "python.exe"),
                    "scripts/zyra_integration_ledger.py",
                    "advance",
                    "missing",
                    "--lifecycle",
                    "internalized",
                    "--reason",
                    "test unknown id",
                    "--json",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertNotEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("Unknown ledger entry", payload["message"])


def _run(args: list[str], env: dict[str, str]) -> dict:
    completed = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), "scripts/zyra_integration_ledger.py", *args],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()
