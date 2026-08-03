from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class ClaudeProductizationFoundationCliTests(unittest.TestCase):
    def test_foundation_verify_script_runs_worker_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/verify_claude_productization_foundation.py",
                    "--json",
                    "--artifact-root",
                    tmp,
                ],
                cwd=ROOT,
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

        payload = json.loads(completed.stdout)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["probe"]["owner_unit"], "M1-02A")
        self.assertEqual(payload["probe"]["coverage"]["boundary_count"], 8)
        self.assertGreaterEqual(payload["probe"]["coverage"]["zyra_target_count"], 20)
        self.assertEqual(payload["probe"]["coverage"]["source_pool_target_count"], 0)
        self.assertEqual(payload["probe"]["blocking_count"], 0)
        self.assertGreaterEqual(payload["eventCount"], 2)
        self.assertEqual(payload["artifactCount"], 1)

    def test_m1_02a_policy_matrix_no_longer_accepts_source_pool_as_connected_runtime(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "scripts/zyra_integration_ledger.py",
                "policy-matrix",
                "--owner-unit",
                "M1-02A",
                "--json",
                "--fail-on-error",
            ],
            cwd=ROOT,
            check=True,
            text=True,
            capture_output=True,
        )

        payload = json.loads(completed.stdout)

        self.assertTrue(payload["ok"])
        finding_codes = {finding["code"] for finding in payload.get("findings", [])}
        self.assertNotIn("CONNECTED_SOURCE_POOL_NOT_DEEP_INTERNALIZED", finding_codes)

    def test_clean_project_copy_runs_without_source_pool_or_parent_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clean_root = Path(tmp) / "zyra-clean"
            clean_root.mkdir()
            for name in ("apps", "packages", "scripts"):
                shutil.copytree(
                    ROOT / name,
                    clean_root / name,
                    ignore=shutil.ignore_patterns(
                        "__pycache__",
                        ".pytest_cache",
                        ".mypy_cache",
                        ".ruff_cache",
                        "tmp",
                        "vendor",
                        "vendor-runtimes",
                    ),
                )
            workspace_packages = {
                "claude-mcp": "packages/integrations/claude-mcp",
                "provider-control-plane": "packages/runtime/provider-control-plane",
                "skill-memory-runtime": "packages/memory/skill-memory-runtime",
            }
            for package_name, relative_path in workspace_packages.items():
                shutil.copytree(
                    clean_root / relative_path,
                    clean_root / "node_modules" / "@zyra" / package_name,
                )
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            local_bun = ROOT / "node_modules" / "bun" / "bin" / (
                "bun.exe" if os.name == "nt" else "bun"
            )
            bun_executable = str(local_bun) if local_bun.is_file() else shutil.which("bun")
            self.assertTrue(bun_executable, "Bun 1.2.15 is required for the TypeScript runtime")
            env["ZYRA_BUN_EXECUTABLE"] = str(bun_executable)
            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/verify_claude_productization_foundation.py",
                    "--clean-source",
                    "--json",
                    "--artifact-root",
                    str(clean_root / "tmp" / "artifacts"),
                ],
                cwd=clean_root,
                env=env,
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

        payload = json.loads(completed.stdout)
        audit = payload["cleanRuntimeAudit"]

        self.assertTrue(payload["ok"])
        self.assertFalse((clean_root / "vendor").exists())
        self.assertFalse((clean_root / "vendor-runtimes").exists())
        self.assertEqual(payload["probe"]["coverage"]["source_pool_only_primary_sources"], 0)
        self.assertEqual(payload["probe"]["coverage"]["source_pool_target_count"], 0)
        self.assertEqual(audit["blocking_count"], 0)
        self.assertTrue(audit["default_path"]["default_path_exercised"])
        self.assertFalse(audit["default_path"]["sidecar_used"])
        self.assertFalse(audit["default_path"]["source_repo_required"])
        self.assertEqual(audit["default_path"]["contract_source"], "zyra-claude-productized")
        self.assertTrue(audit["disconnect_evidence"][0]["failed_as_expected"])
        self.assertNotIn(str(ROOT.parent / "claude-code-best"), completed.stdout)


if __name__ == "__main__":
    unittest.main()
