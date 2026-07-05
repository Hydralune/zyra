from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_PATH = ROOT / "scripts"
if str(SCRIPTS_PATH) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_PATH))

from verify_submission_boundary import forbidden_fragments, verify_submission_boundary


class SubmissionBoundaryTests(unittest.TestCase):
    def test_forbidden_fragments_cover_workspace_source_repositories(self) -> None:
        fragments = forbidden_fragments()

        self.assertIn("../claude-code-best", fragments)
        self.assertIn("..\\browser-use", fragments)
        self.assertIn("G:\\agent-zoo\\OpenHands", fragments)

    def test_current_runtime_sources_do_not_depend_on_workspace_repos(self) -> None:
        verify_submission_boundary()


if __name__ == "__main__":
    unittest.main()
