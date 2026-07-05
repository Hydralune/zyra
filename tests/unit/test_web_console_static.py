from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class WebConsoleStaticTests(unittest.TestCase):
    def test_web_console_contains_task_graph_runtime_surface(self) -> None:
        html = (ROOT / "apps" / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="graphSummary"', html)
        self.assertIn('id="graphMetrics"', html)
        self.assertIn('id="graphView"', html)
        self.assertIn('id="memorySummary"', html)
        self.assertIn('id="trajectorySummary"', html)
        self.assertIn('id="schedulerSummary"', html)
        self.assertIn('id="schedulerList"', html)
        self.assertIn('id="recoveryList"', html)
        self.assertIn("function renderGraph", html)
        self.assertIn("function orderGraphNodes", html)
        self.assertIn("function renderGraphNode", html)
        self.assertIn("function renderMemory", html)
        self.assertIn("function renderTrajectory", html)
        self.assertIn("function renderScheduler", html)
        self.assertIn("/memory/compact", html)
        self.assertIn("/trajectory", html)
        self.assertIn("/scheduler/manifests", html)
        self.assertIn("/recovery", html)
        self.assertIn("new URLSearchParams(window.location.search).get(\"api\")", html)


if __name__ == "__main__":
    unittest.main()
