from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
INTEGRATIONS_ROOT = ROOT / "packages" / "integrations"
if str(INTEGRATIONS_ROOT) not in sys.path:
    sys.path.insert(0, str(INTEGRATIONS_ROOT))

from zyra_integrations.source_provenance import BundledSourceProvenance  # noqa: E402


SOURCE_REPOSITORIES = [
    "agent-framework",
    "agentscope",
    "browser-use",
    "claude-code-best",
    "claudecode-related",
    "hermes-agent",
    "langgraph",
    "long-horizon-systems",
    "oh-my-pi",
    "opencode",
    "openclaw",
    "OpenHands",
    "source-graphs",
]

SCANNED_SUFFIXES = {
    ".bat",
    ".cmd",
    ".js",
    ".json",
    ".jsx",
    ".mjs",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}

IGNORED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".tmp",
    "__pycache__",
    "dist",
    "docs",
    "node_modules",
    "provenance",
    "tmp",
}

IGNORED_FILENAMES = {
    "AGENTS.md",
    "README.md",
}

_TEXT_RUNTIME_CALL = re.compile(
    r"\b(?:Path|open|readFile|readFileSync|require|import|spawn|exec|chdir|cwd)\b",
    re.IGNORECASE,
)


def iter_scanned_files(root: Path = ROOT) -> list[Path]:
    root = root.resolve()
    files: list[Path] = []
    for directory, names, filenames in os.walk(root):
        names[:] = sorted(name for name in names if name not in IGNORED_DIRS)
        base = Path(directory)
        for name in sorted(filenames):
            path = base / name
            if name in IGNORED_FILENAMES or path.suffix not in SCANNED_SUFFIXES:
                continue
            files.append(path)
    return files


def forbidden_fragments() -> list[str]:
    fragments: list[str] = []
    for repository in SOURCE_REPOSITORIES:
        fragments.extend(
            [
                f"../{repository}",
                f"..\\{repository}",
                f"G:\\agent-zoo\\{repository}",
                f"g:\\agent-zoo\\{repository}",
                f"G:/agent-zoo/{repository}",
                f"g:/agent-zoo/{repository}",
            ]
        )
    return fragments


class PythonRuntimePathVisitor(ast.NodeVisitor):
    _INSPECTION_CALLS = {
        "count",
        "endswith",
        "exists",
        "find",
        "fullmatch",
        "is_dir",
        "is_file",
        "is_relative_to",
        "match",
        "replace",
        "search",
        "startswith",
    }
    _RUNTIME_CALLS = {
        "Path",
        "call",
        "check_call",
        "check_output",
        "chdir",
        "exec",
        "execute",
        "open",
        "Popen",
        "read_bytes",
        "read_text",
        "run",
        "spawn",
        "write_bytes",
        "write_text",
    }
    _TEST_DATA_CALLS = {
        "assertEqual",
        "assertIn",
        "assertNotIn",
        "parse",
        "write_bytes",
        "write_text",
    }

    def __init__(
        self,
        *,
        fragments: list[str],
        root: Path = ROOT,
        source_path: Path | None = None,
    ) -> None:
        self.fragments = fragments
        self.root = root.resolve()
        self.source_path = source_path.resolve() if source_path is not None else None
        self.assignments: dict[str, Any] = {}
        if self.source_path is not None:
            self.assignments["__file__"] = str(self.source_path)
        self.violations: list[tuple[int, str]] = []

    @property
    def _is_test_source(self) -> bool:
        return self.source_path is not None and "tests" in self.source_path.parts

    def visit_Assign(self, node: ast.Assign) -> None:
        value = self._literal(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name) and value is not None:
                self.assignments[target.id] = value
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            value = self._literal(node.value)
            if value is not None:
                self.assignments[node.target.id] = value
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        call_name = self._qualified(node.func).rsplit(".", 1)[-1]
        if call_name in self._INSPECTION_CALLS:
            return
        if self._is_test_source and call_name in self._TEST_DATA_CALLS:
            return
        if call_name in self._RUNTIME_CALLS:
            receiver = (
                (node.func.value,)
                if isinstance(node.func, ast.Attribute)
                else ()
            )
            for argument in (
                *receiver,
                *node.args,
                *(item.value for item in node.keywords),
            ):
                value = self._literal(argument)
                for candidate in self._strings(value):
                    fragment = self._forbidden_fragment(candidate)
                    if fragment is not None:
                        self.violations.append(
                            (int(getattr(argument, "lineno", 0)), fragment)
                        )
        self.generic_visit(node)

    def _forbidden_fragment(self, candidate: str) -> str | None:
        normalized = candidate.replace("\\", "/")
        lowered = normalized.lower()
        for fragment in self.fragments:
            if fragment.replace("\\", "/").lower() in lowered:
                return fragment
        return None

    def _literal(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Name):
            return self.assignments.get(node.id)
        if isinstance(node, ast.BinOp):
            left = self._literal(node.left)
            right = self._literal(node.right)
            if isinstance(left, str) and isinstance(right, str):
                if isinstance(node.op, ast.Div):
                    return str(Path(left) / right)
                if isinstance(node.op, ast.Add):
                    return left + right
        if isinstance(node, ast.Attribute):
            base = self._literal(node.value)
            if isinstance(base, str) and node.attr == "parent":
                return str(Path(base).parent)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
            base = self._literal(node.value.value)
            index = self._literal(node.slice)
            if (
                isinstance(base, str)
                and node.value.attr == "parents"
                and isinstance(index, int)
                and index >= 0
            ):
                return str(Path(base).parents[index])
        if isinstance(node, ast.Call):
            name = self._qualified(node.func).rsplit(".", 1)[-1]
            if name == "Path" and node.args:
                return self._literal(node.args[0])
            if name in {"absolute", "resolve"} and isinstance(node.func, ast.Attribute):
                return self._literal(node.func.value)
            if name == "str" and node.args:
                value = self._literal(node.args[0])
                return str(value) if value is not None else None
        try:
            return ast.literal_eval(node)
        except (SyntaxError, TypeError, ValueError):
            return None

    def _strings(self, value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            return (value,)
        if isinstance(value, (list, tuple, set, frozenset)):
            return tuple(
                nested
                for item in value
                for nested in self._strings(item)
            )
        if isinstance(value, dict):
            return tuple(
                nested
                for item in (*value.keys(), *value.values())
                for nested in self._strings(item)
            )
        return ()

    @staticmethod
    def _qualified(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = PythonRuntimePathVisitor._qualified(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        return ""


def verify_submission_boundary(
    root: Path = ROOT,
    *,
    verify_provenance: bool = True,
) -> None:
    root = root.resolve()
    violations: list[str] = []
    if verify_provenance:
        BundledSourceProvenance(root).verify()
    fragments = forbidden_fragments()
    for path in iter_scanned_files(root):
        text = path.read_text(encoding="utf-8", errors="ignore")
        relative = path.relative_to(root)
        if path.suffix == ".py":
            try:
                tree = ast.parse(text, filename=str(relative))
            except SyntaxError as error:
                violations.append(
                    f"{relative}: Python boundary parse failed: {error}"
                )
                continue
            visitor = PythonRuntimePathVisitor(
                fragments=fragments,
                root=root,
                source_path=path,
            )
            visitor.visit(tree)
            for line, fragment in visitor.violations:
                violations.append(
                    f"{relative}:{line}: forbidden runtime dependency "
                    f"{fragment!r}"
                )
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if _TEXT_RUNTIME_CALL.search(line) is None:
                continue
            normalized = line.replace("\\", "/").lower()
            for fragment in fragments:
                if fragment.replace("\\", "/").lower() in normalized:
                    violations.append(
                        f"{relative}:{line_number}: forbidden runtime dependency {fragment!r}"
                    )
                    break

    if violations:
        message = "\n".join(violations)
        raise AssertionError(f"Submission boundary violations found:\n{message}")


def main() -> None:
    verify_submission_boundary()
    print("Submission boundary verification passed")


if __name__ == "__main__":
    main()
