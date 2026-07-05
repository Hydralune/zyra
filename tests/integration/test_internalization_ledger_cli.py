from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class InternalizationLedgerCliTests(unittest.TestCase):
    def test_cli_seed_list_audit_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "ledger.json"
            event_log = Path(tmpdir) / "events.jsonl"
            export_path = Path(tmpdir) / "export.yaml"
            env = os.environ.copy()
            env["ZYRA_INTEGRATION_LEDGER"] = str(ledger_path)

            seed = _run(["seed", "--json"], env)
            self.assertEqual(Path(seed["ledger_path"]), ledger_path)
            self.assertGreaterEqual(seed["summary"]["total_entries"], 800)

            listed = _run(["list", "--source-repo", "browser-use", "--limit", "3", "--json"], env)
            self.assertEqual(len(listed["entries"]), 3)
            self.assertTrue(all(entry["source_repo"] == "browser-use" for entry in listed["entries"]))

            audit = _run(["audit", "--strict", "--write-event", "--event-log", str(event_log), "--json"], env)
            self.assertTrue(audit["ok"])
            self.assertTrue(audit["event_written"])
            self.assertTrue(event_log.exists())
            self.assertIn("integration_ledger_audit", event_log.read_text(encoding="utf-8"))

            export_completed = subprocess.run(
                [
                    str(ROOT / ".venv" / "Scripts" / "python.exe"),
                    "scripts/zyra_integration_ledger.py",
                    "export",
                    "--source-repo",
                    "claude-code-best",
                    "--limit",
                    "1",
                    "--format",
                    "yaml",
                    "--output",
                    str(export_path),
                ],
                cwd=ROOT,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertIn(str(export_path), export_completed.stdout)
            exported = json.loads(export_path.read_text(encoding="utf-8"))
            self.assertEqual(len(exported["entries"]), 1)


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
