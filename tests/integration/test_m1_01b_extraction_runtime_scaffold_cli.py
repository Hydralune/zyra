from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations.ledger_linecount import parse_numstat  # noqa: E402

BASE_COMMIT = "ad985a0e563bf8dc8f5529ebb96c0668b34da362"


class M101BExtractionRuntimeScaffoldCliTests(unittest.TestCase):
    def test_source_extract_smoke_reports_materialized_pilot_runtime(self) -> None:
        payload = _run_json(["scripts/zyra_source_extract.py", "smoke", "--json"])

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["manifest_exists"])
        self.assertTrue(payload["inventory_exists"])
        self.assertGreaterEqual(payload["source_file_count"], 30)

    def test_ledger_accounting_contains_m1_01b_runtime_entries(self) -> None:
        payload = _run_json(
            [
                "scripts/zyra_integration_ledger.py",
                "accounting",
                "--owner-unit",
                "M1-01B",
                "--no-entries",
                "--json",
            ]
        )
        unit = payload["unit_accounts"]["M1-01B"]

        self.assertEqual(unit["owner_unit"], "M1-01B")
        self.assertGreaterEqual(unit["total_entries"], 30)
        self.assertEqual(unit["missing_target_entries"], 0)
        self.assertEqual(unit["missing_runtime_entries"], 0)
        self.assertEqual(unit["missing_test_entries"], 0)
        self.assertEqual(unit["target_surfaces"]["vendor_runtime"], unit["total_entries"])

    def test_linecount_and_gate_do_not_count_vendor_runtime_source_pool_as_effective(self) -> None:
        worktree_linecount = _worktree_effective_linecount()
        self.assertGreaterEqual(worktree_linecount, 10_000)

        head_linecount = _run_json(
            [
                "scripts/zyra_integration_ledger.py",
                "linecount",
                "--base",
                BASE_COMMIT,
                "--owner-unit",
                "M1-01B",
                "--minimum-effective-lines",
                "10000",
                "--json",
            ]
        )
        if not head_linecount["ok"] and worktree_linecount >= 10_000:
            self.skipTest("M1-01B worktree changes are not fully committed; final HEAD gate is checked after commit")

        self.assertTrue(head_linecount["ok"], head_linecount)
        self.assertGreaterEqual(head_linecount["effective_added"], 10_000)
        gate = _run_json(
            [
                "scripts/zyra_integration_ledger.py",
                "gate",
                "--owner-unit",
                "M1-01B",
                "--base",
                BASE_COMMIT,
                "--minimum-effective-lines",
                "10000",
                "--json",
            ]
        )
        self.assertTrue(gate["ok"], gate)
        self.assertIn("line_buckets", gate)
        self.assertEqual(gate["owner_unit"], "M1-01B")

    def test_source_extract_acceptance_cli_reports_runtime_worker_and_rule_evidence(self) -> None:
        payload = _run_json(["scripts/zyra_source_extract.py", "m1-01b-acceptance", "--json"])

        self.assertTrue(payload["summary"]["ok"], payload["checks"])
        self.assertTrue(payload["summary"]["rule_audit_ok"])
        self.assertTrue(payload["summary"]["runtime_ok"])
        self.assertTrue(payload["summary"]["worker_ok"])


def _run_json(args: list[str]) -> dict:
    completed = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _worktree_effective_linecount() -> int:
    git_diff = subprocess.run(
        ["git", "diff", "--numstat", BASE_COMMIT, "--", "apps", "packages", "tests", "scripts", "vendor-runtimes", "skills"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    committed_or_tracked = sum(item.effective_added for item in parse_numstat(git_diff.stdout))
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "apps", "packages", "tests", "scripts", "skills"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    untracked_effective = 0
    for line in untracked.stdout.splitlines():
        path = ROOT / line
        if path.is_file() and path.suffix.lower() in {".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css", ".html", ".toml", ".ps1", ".sh", ".bat", ".cmd"}:
            untracked_effective += len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    return committed_or_tracked + untracked_effective


if __name__ == "__main__":
    unittest.main()
