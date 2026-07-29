from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class SourceExtractionRetirementCliTests(unittest.TestCase):
    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "scripts/zyra_source_extract.py", *arguments],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_status_reports_formal_runtime_and_no_writer(self) -> None:
        completed = self._run("status")
        payload = json.loads(completed.stdout)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "retired")
        self.assertFalse(payload["writer_available"])
        self.assertFalse(payload["external_source_workspace_required"])
        self.assertFalse(payload["fallback_available"])
        self.assertTrue(all(payload["checks"].values()))

    def test_historical_command_fails_closed_without_creating_roots(self) -> None:
        completed = self._run(
            "productize-claude-code",
            "--write-scaffold",
            "--write-crosswalk",
        )
        payload = json.loads(completed.stdout)

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(
            payload["error"],
            "legacy_source_extraction_command_retired",
        )
        self.assertFalse((ROOT / "vendor").exists())
        self.assertFalse((ROOT / "vendor-runtimes").exists())


if __name__ == "__main__":
    unittest.main()
