from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .ledger_models import InternalizationLedgerEntry, SourceEvidence, to_jsonable
from .ledger_policy import ALLOWED_SOURCE_REPOS, classify_path, normalize_repo_path
from .ledger_store import InternalizationLedger


FORBIDDEN_LITERAL_PATTERNS = [
    re.compile(r"\.\./(claude-code-best|browser-use|OpenHands|openclaw|agentscope|agent-framework|hermes-agent|langgraph|opencode)"),
    re.compile(r"\.\.\\(claude-code-best|browser-use|OpenHands|openclaw|agentscope|agent-framework|hermes-agent|langgraph|opencode)"),
    re.compile(r"G:\\agent-zoo\\(claude-code-best|browser-use|OpenHands|openclaw|agentscope|agent-framework|hermes-agent|langgraph|opencode)", re.IGNORECASE),
    re.compile(r"g:/agent-zoo/(claude-code-best|browser-use|OpenHands|openclaw|agentscope|agent-framework|hermes-agent|langgraph|opencode)", re.IGNORECASE),
]

FORBIDDEN_DYNAMIC_REPOS = {
    "claude-code-best",
    "browser-use",
    "OpenHands",
    "openclaw",
    "agentscope",
    "agent-framework",
    "hermes-agent",
    "langgraph",
    "opencode",
}

SCAN_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ps1",
    ".sh",
    ".cmd",
    ".bat",
}

IGNORED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "tmp",
}


@dataclass(slots=True)
class SourcePathVerification:
    source_repo: str
    source_path: str
    exists_in_workspace: bool
    absolute_path: str = ""
    evidence_count: int = 0
    reason: str = ""
    symbols: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class TargetPathVerification:
    target_path: str
    exists_in_project: bool
    classification: dict[str, Any]
    ledger_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class ForbiddenDependencyHit:
    path: str
    line: int
    column: int
    repo: str
    pattern: str
    kind: str
    excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class SourceScanReport:
    project_root: str
    source_root: str
    scanned_files: int
    source_verifications: list[SourcePathVerification] = field(default_factory=list)
    target_verifications: list[TargetPathVerification] = field(default_factory=list)
    forbidden_hits: list[ForbiddenDependencyHit] = field(default_factory=list)
    missing_source_count: int = 0
    missing_target_count: int = 0
    unverified_evidence_count: int = 0

    @property
    def ok(self) -> bool:
        return not self.forbidden_hits

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["ok"] = self.ok
        return payload


def verify_source_evidence(source_root: Path, entry: InternalizationLedgerEntry) -> list[SourcePathVerification]:
    evidence_items = entry.source_evidence or [
        SourceEvidence(
            source_repo=entry.source_repo,
            source_path=entry.source_path,
            exists_in_workspace=False,
            reason="implicit entry source path",
        )
    ]
    verifications: list[SourcePathVerification] = []
    for evidence in evidence_items:
        source_repo = evidence.source_repo or entry.source_repo
        source_path = evidence.source_path or entry.source_path
        absolute = source_root / source_repo / normalize_repo_path(source_path)
        exists = absolute.exists()
        verifications.append(
            SourcePathVerification(
                source_repo=source_repo,
                source_path=source_path,
                exists_in_workspace=exists,
                absolute_path=str(absolute),
                evidence_count=1,
                reason=evidence.reason,
                symbols=list(evidence.symbols),
                tags=list(evidence.tags),
            )
        )
    return verifications


def verify_target_paths(project_root: Path, ledger: InternalizationLedger) -> list[TargetPathVerification]:
    owners: dict[str, list[str]] = {}
    for entry in ledger.entries():
        for target in entry.target_paths:
            owners.setdefault(target, []).append(entry.ledger_id)
    verifications: list[TargetPathVerification] = []
    for target, ledger_ids in sorted(owners.items()):
        classification = classify_path(target)
        exists = classification.is_project_relative and (project_root / classification.normalized_path).exists()
        verifications.append(
            TargetPathVerification(
                target_path=target,
                exists_in_project=exists,
                classification=classification.to_dict(),
                ledger_ids=sorted(ledger_ids),
            )
        )
    return verifications


def scan_forbidden_dependencies(project_root: Path, *, include_tests: bool = False) -> list[ForbiddenDependencyHit]:
    hits: list[ForbiddenDependencyHit] = []
    for path in iter_scannable_files(project_root, include_tests=include_tests):
        text = _read_text(path)
        if not text:
            continue
        hits.extend(_scan_literal_patterns(project_root, path, text))
        if path.suffix == ".py":
            hits.extend(_scan_python_dynamic_parent_refs(project_root, path, text))
    return hits


def build_source_scan_report(
    project_root: Path,
    source_root: Path,
    ledger: InternalizationLedger,
    *,
    include_tests: bool = False,
) -> SourceScanReport:
    source_verifications: list[SourcePathVerification] = []
    for entry in ledger.entries():
        source_verifications.extend(verify_source_evidence(source_root, entry))
    target_verifications = verify_target_paths(project_root, ledger)
    forbidden_hits = scan_forbidden_dependencies(project_root, include_tests=include_tests)
    missing_source_count = sum(1 for item in source_verifications if not item.exists_in_workspace)
    missing_target_count = sum(1 for item in target_verifications if not item.exists_in_project)
    unverified_evidence_count = sum(1 for item in source_verifications if not item.reason and not item.symbols and not item.tags)
    return SourceScanReport(
        project_root=str(project_root),
        source_root=str(source_root),
        scanned_files=sum(1 for _ in iter_scannable_files(project_root, include_tests=include_tests)),
        source_verifications=source_verifications,
        target_verifications=target_verifications,
        forbidden_hits=forbidden_hits,
        missing_source_count=missing_source_count,
        missing_target_count=missing_target_count,
        unverified_evidence_count=unverified_evidence_count,
    )


def iter_scannable_files(project_root: Path, *, include_tests: bool = False) -> Iterable[Path]:
    for path in project_root.rglob("*"):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        try:
            parts = set(path.relative_to(project_root).parts)
        except ValueError:
            continue
        if parts & IGNORED_PARTS:
            continue
        if not include_tests and "tests" in parts:
            continue
        yield path


def repo_from_source_path(path: str) -> str:
    normalized = normalize_repo_path(path)
    for repo in ALLOWED_SOURCE_REPOS:
        if normalized.startswith(repo + "/") or normalized == repo:
            return repo
    return ""


def normalize_source_evidence(source_root: Path, entry: InternalizationLedgerEntry) -> list[SourceEvidence]:
    normalized: list[SourceEvidence] = []
    for verification in verify_source_evidence(source_root, entry):
        normalized.append(
            SourceEvidence(
                source_repo=verification.source_repo,
                source_path=verification.source_path,
                exists_in_workspace=verification.exists_in_workspace,
                reason=verification.reason or "verified by ledger source scanner",
                symbols=verification.symbols,
                tags=verification.tags,
            )
        )
    return normalized


def _scan_literal_patterns(project_root: Path, path: Path, text: str) -> list[ForbiddenDependencyHit]:
    hits: list[ForbiddenDependencyHit] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for pattern in FORBIDDEN_LITERAL_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            repo = match.group(1)
            hits.append(
                ForbiddenDependencyHit(
                    path=path.relative_to(project_root).as_posix(),
                    line=line_number,
                    column=match.start() + 1,
                    repo=repo,
                    pattern=pattern.pattern,
                    kind="literal",
                    excerpt=line.strip()[:240],
                )
            )
    return hits


def _scan_python_dynamic_parent_refs(project_root: Path, path: Path, text: str) -> list[ForbiddenDependencyHit]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    hits: list[ForbiddenDependencyHit] = []
    for node in ast.walk(tree):
        if _is_parent_repo_path_expr(node):
            repo = _repo_from_ast_expr(node)
            hits.append(
                ForbiddenDependencyHit(
                    path=path.relative_to(project_root).as_posix(),
                    line=getattr(node, "lineno", 0),
                    column=getattr(node, "col_offset", 0) + 1,
                    repo=repo,
                    pattern="dynamic parent source repo path",
                    kind="python_ast",
                    excerpt=ast.unparse(node) if hasattr(ast, "unparse") else "",
                )
            )
    return hits


def _is_parent_repo_path_expr(node: ast.AST) -> bool:
    text = ast.unparse(node) if hasattr(ast, "unparse") else ""
    if not text:
        return False
    lowered = text.lower().replace('"', "'")
    for repo in FORBIDDEN_DYNAMIC_REPOS:
        repo_lower = repo.lower()
        if repo_lower not in lowered:
            continue
        patterns = [
            f"parent / '{repo_lower}'",
            f"path('..') / '{repo_lower}'",
            f"join('..', '{repo_lower}'",
            f"join(\"..\", \"{repo_lower}\"",
            f"os.environ.get('zyra_source_root'",
        ]
        if any(pattern in lowered for pattern in patterns):
            return True
    return False


def _repo_from_ast_expr(node: ast.AST) -> str:
    text = ast.unparse(node) if hasattr(ast, "unparse") else ""
    lowered = text.lower()
    for repo in FORBIDDEN_DYNAMIC_REPOS:
        if repo.lower() in lowered:
            return repo
    return ""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
