from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "workers",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_workers import CodeWorkerSidecarClient, code_worker_entrypoint


@unittest.skipIf(shutil.which("node") is None and shutil.which("bun") is None, "TypeScript runtime is required")
class ClaudeCodeProductizedRuntimeTests(unittest.TestCase):
    def test_runtime_inventory_reads_zyra_typescript_modules_not_source_pool(self) -> None:
        client = CodeWorkerSidecarClient(ROOT)
        health = client.health()
        inventory = client.runtime_inventory()
        contract = client.query_contract()

        self.assertTrue(health["ok"])
        self.assertEqual(health["canonicalOwner"], "typescript")
        self.assertTrue(inventory["productizedRuntime"]["moduleChecks"]["queryEngine"])
        self.assertTrue(inventory["productizedRuntime"]["moduleChecks"]["toolOrchestration"])
        self.assertIn("packages/runtime/claude-runtime/src/query-engine.ts", contract["targetFiles"])
        self.assertFalse(contract["requiresRootSourceRepo"])
        self.assertFalse(contract["requiresVendorRuntime"])

    def test_application_entrypoint_is_not_the_legacy_inspection_sidecar(self) -> None:
        entrypoint = code_worker_entrypoint(ROOT)

        self.assertEqual(entrypoint, ROOT / "apps" / "code-worker" / "src" / "main.ts")
        self.assertTrue(entrypoint.exists())
        self.assertFalse((entrypoint.parent / "main.mjs").exists())
        text = entrypoint.read_text(encoding="utf-8")
        self.assertIn("runStdioRuntime", text)
        self.assertNotIn("vendor/claude-code-best", text)
        self.assertNotIn("vendor-runtimes", text)


if __name__ == "__main__":
    unittest.main()
