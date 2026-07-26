from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_PATH = ROOT / "scripts"
if str(SCRIPTS_PATH) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_PATH))

from verify_submission_boundary import (
    PythonRuntimePathVisitor,
    forbidden_fragments,
    verify_submission_boundary,
)


class SubmissionBoundaryTests(unittest.TestCase):
    def test_forbidden_fragments_cover_workspace_source_repositories(self) -> None:
        fragments = forbidden_fragments()

        self.assertIn("../claude-code-best", fragments)
        self.assertIn("..\\browser-use", fragments)
        self.assertIn("G:\\agent-zoo\\OpenHands", fragments)

    def test_current_runtime_sources_do_not_depend_on_workspace_repos(self) -> None:
        verify_submission_boundary()

    def test_python_deny_list_literal_is_not_a_runtime_dependency(self) -> None:
        tree = ast.parse(
            'FORBIDDEN = ("../claude-code-best",)\n'
            'def inspect(line: str) -> bool:\n'
            '    return line.count("../claude-code-best") > 0\n'
        )
        visitor = PythonRuntimePathVisitor(fragments=forbidden_fragments())
        visitor.visit(tree)

        self.assertEqual(visitor.violations, [])

    def test_python_path_call_with_parent_source_is_rejected(self) -> None:
        tree = ast.parse(
            'SOURCE = "../claude-code-best/runtime"\n'
            "runtime_root = Path(SOURCE)\n"
        )
        visitor = PythonRuntimePathVisitor(fragments=forbidden_fragments())
        visitor.visit(tree)

        self.assertEqual(
            visitor.violations,
            [(2, "../claude-code-best")],
        )


if __name__ == "__main__":
    unittest.main()
