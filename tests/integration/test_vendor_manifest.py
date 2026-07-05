from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INTEGRATIONS_PATH = ROOT / "packages" / "integrations"
if str(INTEGRATIONS_PATH) not in sys.path:
    sys.path.insert(0, str(INTEGRATIONS_PATH))

from zyra_integrations import (
    browser_use_snapshot,
    claude_code_best_snapshot,
    validate_vendor_snapshot,
)


class VendorManifestTests(unittest.TestCase):
    def test_claude_code_best_vendor_snapshot_is_inside_zyra(self) -> None:
        snapshot = claude_code_best_snapshot(ROOT)

        self.assertTrue(snapshot.root.exists())
        self.assertTrue(snapshot.root.is_relative_to(ROOT))
        self.assertFalse((snapshot.root / ".git").exists())

    def test_claude_code_best_priority_modules_exist(self) -> None:
        snapshot = claude_code_best_snapshot(ROOT)

        validate_vendor_snapshot(snapshot)
        module_names = {module.name for module in snapshot.modules}
        self.assertIn("query-engine", module_names)
        self.assertIn("permission-runtime", module_names)
        self.assertIn("skill-runtime", module_names)
        self.assertIn("subagent-runtime", module_names)

    def test_browser_use_vendor_snapshot_is_inside_zyra(self) -> None:
        snapshot = browser_use_snapshot(ROOT)

        self.assertTrue(snapshot.root.exists())
        self.assertTrue(snapshot.root.is_relative_to(ROOT))
        self.assertFalse((snapshot.root / ".git").exists())

    def test_browser_use_priority_modules_exist(self) -> None:
        snapshot = browser_use_snapshot(ROOT)

        validate_vendor_snapshot(snapshot)
        module_names = {module.name for module in snapshot.modules}
        self.assertIn("browser-agent", module_names)
        self.assertIn("browser-control", module_names)
        self.assertIn("browser-tools-skills", module_names)


if __name__ == "__main__":
    unittest.main()
