from __future__ import annotations

import ast
import sys
import tempfile
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
        self.assertIn("../long-horizon-systems", fragments)
        self.assertIn("../oh-my-pi", fragments)
        self.assertIn("../source-graphs", fragments)

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

    def test_dynamically_composed_long_horizon_parent_path_is_rejected(self) -> None:
        source_path = ROOT / "scripts" / "fixture.py"
        tree = ast.parse(
            "from pathlib import Path\n"
            "PROJECT_ROOT = Path(__file__).resolve().parents[1]\n"
            'source = PROJECT_ROOT.parent / "long-horizon-systems" / "loopx"\n'
            "payload = source.read_bytes()\n"
        )
        visitor = PythonRuntimePathVisitor(
            fragments=forbidden_fragments(),
            root=ROOT,
            source_path=source_path,
        )
        visitor.visit(tree)

        self.assertTrue(visitor.violations)
        self.assertIn("long-horizon-systems", visitor.violations[0][1])

    def test_path_existence_probe_does_not_hide_nested_parent_dependency(self) -> None:
        tree = ast.parse(
            "from pathlib import Path\n"
            'available = Path("../long-horizon-systems/loopx").exists()\n'
        )
        visitor = PythonRuntimePathVisitor(fragments=forbidden_fragments())

        visitor.visit(tree)

        self.assertEqual(visitor.violations, [(2, "../long-horizon-systems")])

    def test_custom_runtime_loader_is_scanned_without_a_call_allowlist(self) -> None:
        tree = ast.parse(
            'source = "../long-horizon-systems/loopx"\n'
            "runtime = load_runtime(source)\n"
        )
        visitor = PythonRuntimePathVisitor(fragments=forbidden_fragments())

        visitor.visit(tree)

        self.assertEqual(visitor.violations, [(2, "../long-horizon-systems")])

    def test_declarative_json_and_html_runtime_paths_are_rejected(self) -> None:
        fixtures = {
            "config.json": '{"sourceRoot": "../long-horizon-systems/loopx"}\n',
            "index.html": '<script src="../long-horizon-systems/loopx/app.js"></script>\n',
            "style.css": '@import "../long-horizon-systems/loopx/theme.css";\n',
            "runtime.ts": 'loadRuntime("../long-horizon-systems/loopx")\n',
        }
        for filename, content in fixtures.items():
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / filename).write_text(content, encoding="utf-8")

                with self.assertRaisesRegex(AssertionError, "long-horizon-systems"):
                    verify_submission_boundary(root, verify_provenance=False)

    def test_tests_and_remediation_directories_are_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "scripts" / "remediation" / "bad.py"
            target.parent.mkdir(parents=True)
            target.write_text(
                'from pathlib import Path\nsource = Path("../long-horizon-systems/loopx")\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(AssertionError, "long-horizon-systems"):
                verify_submission_boundary(root, verify_provenance=False)


if __name__ == "__main__":
    unittest.main()
