from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

SOURCE_REPOSITORIES = [
    "agent-framework",
    "agentscope",
    "browser-use",
    "claude-code-best",
    "hermes-agent",
    "langgraph",
    "opencode",
    "openclaw",
    "OpenHands",
]

SCANNED_SUFFIXES = {
    ".bat",
    ".cmd",
    ".css",
    ".html",
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
    "tests",
    "tmp",
}

AUDIT_ONLY_FILENAMES = {
    "claude_source_graph_crosswalk.py",
    "integration_audit.py",
    "integration_custody.py",
    "source_audit.py",
    "source_custody.py",
    "vendor_manifest.py",
    "verify_typescript_runtime_custody.py",
}

AUDIT_ONLY_DIRECTORIES = {
    ("scripts", "remediation"),
}

IGNORED_FILENAMES = {
    "AGENTS.md",
    "README.md",
}


def iter_scanned_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative_parts = set(path.relative_to(ROOT).parts)
        if relative_parts & IGNORED_DIRS:
            continue
        if path.name in IGNORED_FILENAMES:
            continue
        relative = path.relative_to(ROOT)
        if path.name in AUDIT_ONLY_FILENAMES:
            continue
        if any(
            relative.parts[: len(prefix)] == prefix
            for prefix in AUDIT_ONLY_DIRECTORIES
        ):
            continue
        if path.suffix not in SCANNED_SUFFIXES:
            continue
        files.append(path)
    return files


def forbidden_fragments() -> list[str]:
    fragments: list[str] = []
    for repo in SOURCE_REPOSITORIES:
        fragments.extend(
            [
                f"../{repo}",
                f"..\\{repo}",
                f"G:\\agent-zoo\\{repo}",
                f"g:\\agent-zoo\\{repo}",
            ]
        )
    return fragments


class PythonRuntimePathVisitor(ast.NodeVisitor):
    _INSPECTION_CALLS = {
        "count",
        "endswith",
        "find",
        "fullmatch",
        "match",
        "replace",
        "search",
        "startswith",
    }

    def __init__(self, *, fragments: list[str]) -> None:
        self.fragments = fragments
        self.assignments: dict[str, Any] = {}
        self.violations: list[tuple[int, str]] = []

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
        if call_name not in self._INSPECTION_CALLS:
            for argument in (*node.args, *(item.value for item in node.keywords)):
                value = self._literal(argument)
                for text in self._strings(value):
                    for fragment in self.fragments:
                        if fragment in text:
                            self.violations.append(
                                (int(getattr(argument, "lineno", 0)), fragment)
                            )
        self.generic_visit(node)

    def _literal(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Name):
            return self.assignments.get(node.id)
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


def verify_submission_boundary() -> None:
    violations: list[str] = []
    fragments = forbidden_fragments()
    for path in iter_scanned_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        relative = path.relative_to(ROOT)
        if path.suffix == ".py":
            try:
                tree = ast.parse(text, filename=str(relative))
            except SyntaxError as error:
                violations.append(
                    f"{relative}: Python boundary parse failed: {error}"
                )
                continue
            visitor = PythonRuntimePathVisitor(fragments=fragments)
            visitor.visit(tree)
            for line, fragment in visitor.violations:
                violations.append(
                    f"{relative}:{line}: forbidden runtime dependency "
                    f"{fragment!r}"
                )
            continue
        for fragment in fragments:
            if fragment in text:
                violations.append(f"{relative}: forbidden runtime dependency {fragment!r}")

    if violations:
        message = "\n".join(violations)
        raise AssertionError(f"Submission boundary violations found:\n{message}")


def main() -> None:
    verify_submission_boundary()
    print("Submission boundary verification passed")


if __name__ == "__main__":
    main()
