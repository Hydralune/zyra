from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class WebConsoleStaticTests(unittest.TestCase):
    def test_web_console_mounts_production_workbench_surface(self) -> None:
        html = (ROOT / "apps" / "web" / "index.html").read_text(encoding="utf-8")
        main = (ROOT / "apps" / "web" / "src" / "main.tsx").read_text(encoding="utf-8")
        runtime = (ROOT / "apps" / "web" / "src" / "app" / "runtime.ts").read_text(
            encoding="utf-8"
        )
        workbench = (
            ROOT / "apps" / "web" / "src" / "app" / "workbench-app.tsx"
        ).read_text(encoding="utf-8")
        command = (
            ROOT / "apps" / "web" / "src" / "command" / "coordinator.ts"
        ).read_text(encoding="utf-8")

        self.assertIn('id="root"', html)
        self.assertIn("<title>Zyra Workbench</title>", html)
        self.assertIn("./src/main.tsx", html)
        self.assertNotIn("temporary M2-S01A-01", html)
        self.assertIn("createWorkbenchRuntime", main)
        self.assertIn("<WorkbenchApp", main)
        self.assertIn("createZyraApi", runtime)
        self.assertIn("<TaskList", workbench)
        self.assertIn("<TaskDetail", workbench)
        self.assertIn("<CommandInput", workbench)
        self.assertIn("TaskLifecycleCoordinator", command)
        self.assertNotIn("fetch(", workbench)
        self.assertNotIn("fetch(", command)


if __name__ == "__main__":
    unittest.main()
