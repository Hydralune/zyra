from __future__ import annotations

import json
import shutil
import subprocess
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

from zyra_runtime import build_productized_claude_runtime_contracts
from zyra_workers import CodeWorkerSidecarClient, code_worker_entrypoint


@unittest.skipIf(shutil.which("node") is None and shutil.which("bun") is None, "TypeScript runtime is required")
class ClaudeCodeProductizedRuntimeTests(unittest.TestCase):
    def test_source_to_target_contract_points_to_internalized_typescript_modules(self) -> None:
        contracts = build_productized_claude_runtime_contracts(project_root=ROOT)
        mappings = [
            item
            for item in contracts.source_to_target
            if item.source_path in {"src/QueryEngine.ts", "src/query.ts"}
        ]
        targets = {path for item in mappings for path in item.target_paths}

        self.assertIn("packages/runtime/claude-runtime/src/query-engine.ts", targets)
        self.assertIn("packages/runtime/claude-runtime/src/session.ts", targets)
        self.assertIn("packages/runtime/claude-runtime/src/protocol.ts", targets)
        self.assertNotIn("vendor-runtimes/claude-code-runtime/productized/claude-code-best/src/QueryEngine.ts", targets)
        self.assertEqual(contracts.query_contract["canonicalRuntime"]["ownerLanguage"], "typescript")
        self.assertFalse(contracts.query_contract["canonicalRuntime"]["pythonFallback"])

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

    def test_runtime_custody_audit_rejects_python_default_and_source_dependencies(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/verify_typescript_runtime_custody.py", "--json"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(completed.stdout)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["canonical_owner"], "typescript")
        self.assertFalse(payload["boundaries"]["python_query_engine_fallback"])
        self.assertEqual(payload["boundaries"]["permission_and_tool_side_effects"], "python")

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
