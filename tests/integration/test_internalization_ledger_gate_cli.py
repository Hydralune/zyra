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

            boundary = _run(["boundary", "--roots", "packages", "--json"], env)
            self.assertIn("summary", boundary)
            self.assertIn("runtime_boundary_summary", boundary)

            reachability = _run(["reachability", "--owner-unit", "M1-01A", "--no-entries", "--json"], env)
            self.assertEqual(reachability["total_entries"], 1)
            self.assertIn("route_count", reachability)

            acceptance = _run(["acceptance", "--owner-unit", "M1-01A", "--boundary-roots", "packages", "--json"], env)
            self.assertEqual(acceptance["owner_unit"], "M1-01A")
            self.assertIn("criteria", acceptance)

            persistence = _run(["persistence", "--json"], env)
            self.assertIn("revision", persistence)
            self.assertEqual(Path(persistence["ledger_path"]), ledger_path)

            cleanroom = _run(["cleanroom", "--no-source-scan", "--roots", "packages", "--json"], env)
            self.assertIn("commands_to_run", cleanroom)
            self.assertIn("copy_plan", cleanroom)

            semantic = _run(["semantic-effects", "--json"], env)
            self.assertTrue(semantic["ok"])
            self.assertEqual(semantic["total_probes"], 4)

            test_quality = _run(["test-quality", "--owner-unit", "M1-01A", "--json"], env)
            self.assertIn("checked_test_entries", test_quality)
            self.assertIn("warning_findings", test_quality)

            schema = _run(["schema-contract", "--owner-unit", "M1-01A", "--json"], env)
            self.assertIn("contract_version", schema)
            self.assertIn("field_contracts", schema)

            graph = _run(["evidence-graph", "--owner-unit", "M1-01A", "--json"], env)
            self.assertIn("target_impacts", graph)
            self.assertIn("summary", graph)

            custody = _run(["state-custody", "--json"], env)
            self.assertIn("claims", custody)
            self.assertIn("module_signals", custody)

            mutation = _run(["mutation-consistency", "--json"], env)
            self.assertTrue(mutation["ok"])
            self.assertIn("probes", mutation)

            policy = _run(["policy-matrix", "--owner-unit", "M1-01A", "--json"], env)
            self.assertIn("coverage", policy)
            self.assertIn("summary", policy)

            review = _run(["unit-review", "--owner-unit", "M1-01A", "--minimum-effective-lines", "0", "--json"], env)
            self.assertIn("objectives", review)
            self.assertIn("evidence", review)

            snapshot = _run(["snapshot", "--label", "cli-test", "--owner-unit", "M1-01A", "--json"], env)
            self.assertTrue(Path(snapshot["path"]).exists())

            snapshots = _run(["snapshots", "--json"], env)
            self.assertTrue(any(item["snapshot_id"] == snapshot["snapshot"]["identity"]["snapshot_id"] for item in snapshots["snapshots"]))

    def test_cli_gate_text_output_handles_gate_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = os.environ.copy()
            ledger_path = Path(tmpdir) / "ledger.json"
            env["ZYRA_INTEGRATION_LEDGER"] = str(ledger_path)
            InternalizationLedger([sample_entry()]).save(ledger_path)

            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/zyra_integration_ledger.py",
                    "gate",
                    "--owner-unit",
                    "M1-01A",
                    "--minimum-effective-lines",
                    "10000",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertIn("unit=M1-01A", completed.stdout)
        self.assertIn("findings=", completed.stdout)
        self.assertIn("LINE_COUNT_BASE_MISSING", completed.stdout)

    def test_cli_linecount_reports_seed_exclusion_and_shortfall(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
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

    def test_cli_buckets_reports_vendor_like_separately(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "scripts/zyra_integration_ledger.py",
                "buckets",
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

        self.assertIn("by_bucket", payload)
        self.assertIn("vendor_like", payload["by_bucket"])

    def test_cli_advance_rejects_unknown_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env = os.environ.copy()
            ledger_path = Path(tmpdir) / "ledger.json"
            env["ZYRA_INTEGRATION_LEDGER"] = str(ledger_path)
            InternalizationLedger([sample_entry()]).save(ledger_path)

            completed = subprocess.run(
                [
                    sys.executable,
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
        [sys.executable, "scripts/zyra_integration_ledger.py", *args],
        cwd=ROOT,
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()
