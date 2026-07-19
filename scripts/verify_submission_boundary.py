from __future__ import annotations

from pathlib import Path

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


def verify_submission_boundary() -> None:
    violations: list[str] = []
    fragments = forbidden_fragments()
    for path in iter_scanned_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for fragment in fragments:
            if fragment in text:
                relative = path.relative_to(ROOT)
                violations.append(f"{relative}: forbidden runtime dependency {fragment!r}")

    if violations:
        message = "\n".join(violations)
        raise AssertionError(f"Submission boundary violations found:\n{message}")


def main() -> None:
    verify_submission_boundary()
    print("Submission boundary verification passed")


if __name__ == "__main__":
    main()
