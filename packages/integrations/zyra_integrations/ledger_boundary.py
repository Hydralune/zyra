from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from .ledger_models import to_jsonable
from .ledger_policy import classify_path


class BoundarySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class BoundaryCode(StrEnum):
    CLEAN = "CLEAN"
    PARENT_SOURCE_REFERENCE = "PARENT_SOURCE_REFERENCE"
    ABSOLUTE_SOURCE_REFERENCE = "ABSOLUTE_SOURCE_REFERENCE"
    EDITABLE_OUTSIDE_PROJECT = "EDITABLE_OUTSIDE_PROJECT"
    PATH_ENV_OUTSIDE_PROJECT = "PATH_ENV_OUTSIDE_PROJECT"
    CACHE_ARTIFACT_PRESENT = "CACHE_ARTIFACT_PRESENT"
    GENERATED_ARTIFACT_PRESENT = "GENERATED_ARTIFACT_PRESENT"
    VENDOR_RUNTIME_OPAQUE = "VENDOR_RUNTIME_OPAQUE"
    VENDOR_RUNTIME_BOUNDARY_MISSING = "VENDOR_RUNTIME_BOUNDARY_MISSING"
    PYTHONPATH_INJECTION = "PYTHONPATH_INJECTION"
    SUBPROCESS_SOURCE_REPO_REFERENCE = "SUBPROCESS_SOURCE_REPO_REFERENCE"
    SOURCE_REPO_IMPORT_REFERENCE = "SOURCE_REPO_IMPORT_REFERENCE"
    UNKNOWN_RUNTIME_BOUNDARY = "UNKNOWN_RUNTIME_BOUNDARY"


SOURCE_REPOSITORIES = (
    "claude-code-best",
    "browser-use",
    "OpenHands",
    "openclaw",
    "agentscope",
    "agent-framework",
    "hermes-agent",
    "langgraph",
    "opencode",
)

SCANNED_SUFFIXES = {
    ".bat",
    ".cmd",
    ".css",
    ".html",
    ".js",
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
    ".json",
}

IGNORED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "tmp",
}

CACHE_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    "dist",
    "build",
    "coverage",
}

BOUNDARY_HINTS = (
    "runtime_entry",
    "health_check",
    "main_path",
    "api_routes",
    "worker_runtime",
    "event_types",
    "control_commands",
)

PARENT_RELATIVE_MARKER = ".." + "/"

VENDOR_LIKE_RUNTIME_ROOTS = (
    Path("packages/integrations/loopx_runtime"),
)

NON_RUNTIME_DATA_ROOTS = (
    Path("packages/integrations/zyra_integrations/data"),
)


@dataclass(slots=True)
class BoundaryFinding:
    code: BoundaryCode
    severity: BoundarySeverity
    message: str
    path: str = ""
    line: int = 0
    fragment: str = ""
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class SourceReference:
    repo: str
    pattern: str
    path: str
    line: int
    text: str
    reference_kind: str

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class CacheArtifact:
    path: str
    kind: str
    size_bytes: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class RuntimeBoundary:
    path: str
    boundary_kind: str
    source_repo: str = ""
    has_runtime_entry: bool = False
    has_health_check: bool = False
    has_main_path_binding: bool = False
    has_test_binding: bool = False
    evidence: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class BoundaryScanSummary:
    scanned_files: int
    scanned_lines: int
    source_reference_count: int
    cache_artifact_count: int
    runtime_boundary_count: int
    complete_runtime_boundaries: int
    warning_count: int
    error_count: int
    blocker_count: int

    @property
    def ok(self) -> bool:
        return self.error_count == 0 and self.blocker_count == 0

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["ok"] = self.ok
        return payload


@dataclass(slots=True)
class CleanBoundaryReport:
    project_root: str
    ok: bool
    findings: list[BoundaryFinding]
    source_references: list[SourceReference]
    cache_artifacts: list[CacheArtifact]
    runtime_boundaries: list[RuntimeBoundary]
    summary: BoundaryScanSummary
    scanned_roots: list[str] = field(default_factory=list)

    @property
    def blocker_count(self) -> int:
        return self.summary.blocker_count

    @property
    def error_count(self) -> int:
        return self.summary.error_count

    @property
    def warning_count(self) -> int:
        return self.summary.warning_count

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


def build_clean_boundary_report(
    project_root: Path,
    *,
    include_tests: bool = True,
    include_cache: bool = True,
    scan_roots: Iterable[str] | None = None,
) -> CleanBoundaryReport:
    roots = list(scan_roots or ["apps", "packages", "scripts", "tests", "vendor-runtimes", "skills"])
    if not include_tests:
        roots = [root for root in roots if root != "tests"]
    files = list(iter_boundary_files(project_root, roots=roots))
    source_references: list[SourceReference] = []
    findings: list[BoundaryFinding] = []
    scanned_lines = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as error:
            findings.append(
                BoundaryFinding(
                    code=BoundaryCode.UNKNOWN_RUNTIME_BOUNDARY,
                    severity=BoundarySeverity.WARNING,
                    path=_relative(project_root, path),
                    message=f"Could not read boundary file: {error}",
                    remediation="Make the file readable or exclude it from the submitted source tree.",
                )
            )
            continue
        lines = text.splitlines()
        scanned_lines += len(lines)
        refs = scan_text_for_source_references(project_root, path, lines)
        source_references.extend(refs)
        findings.extend(_findings_for_source_references(refs))
        findings.extend(_findings_for_editable_install(project_root, path, lines))
        findings.extend(_findings_for_path_env(project_root, path, lines))
    cache_artifacts = list(scan_cache_artifacts(project_root)) if include_cache else []
    findings.extend(_findings_for_cache_artifacts(cache_artifacts))
    runtime_boundaries = discover_runtime_boundaries(
        project_root,
        roots=roots,
        files=files,
    )
    findings.extend(_findings_for_runtime_boundaries(runtime_boundaries))
    warning_count = sum(1 for finding in findings if finding.severity == BoundarySeverity.WARNING)
    error_count = sum(1 for finding in findings if finding.severity == BoundarySeverity.ERROR)
    blocker_count = sum(1 for finding in findings if finding.severity == BoundarySeverity.BLOCKER)
    summary = BoundaryScanSummary(
        scanned_files=len(files),
        scanned_lines=scanned_lines,
        source_reference_count=len(source_references),
        cache_artifact_count=len(cache_artifacts),
        runtime_boundary_count=len(runtime_boundaries),
        complete_runtime_boundaries=sum(1 for boundary in runtime_boundaries if boundary.complete),
        warning_count=warning_count,
        error_count=error_count,
        blocker_count=blocker_count,
    )
    return CleanBoundaryReport(
        project_root=str(project_root),
        ok=summary.ok,
        findings=findings,
        source_references=source_references,
        cache_artifacts=cache_artifacts,
        runtime_boundaries=runtime_boundaries,
        summary=summary,
        scanned_roots=roots,
    )


def iter_boundary_files(project_root: Path, *, roots: Iterable[str]) -> Iterable[Path]:
    for root in roots:
        root_path = project_root / root
        if not root_path.exists():
            continue
        if root_path.is_file():
            relative = root_path.relative_to(project_root)
            if (
                root_path.suffix in SCANNED_SUFFIXES
                and not _is_non_runtime_scan_path(relative)
            ):
                yield root_path
            continue
        for current_root, dirnames, filenames in os.walk(root_path):
            current = Path(current_root)
            try:
                relative_root = current.relative_to(project_root)
            except ValueError:
                dirnames[:] = []
                continue
            if (
                set(relative_root.parts) & IGNORED_DIRS
                or _is_non_runtime_scan_path(relative_root)
            ):
                dirnames[:] = []
                continue
            dirnames[:] = sorted(
                dirname
                for dirname in dirnames
                if dirname not in IGNORED_DIRS
                and not _is_non_runtime_scan_path(
                    relative_root / dirname
                )
            )
            for filename in sorted(filenames):
                path = current / filename
                if path.suffix in SCANNED_SUFFIXES:
                    yield path


def scan_text_for_source_references(project_root: Path, path: Path, lines: Iterable[str]) -> list[SourceReference]:
    references: list[SourceReference] = []
    for index, line in enumerate(lines, start=1):
        normalized = line.replace("\\", "/")
        for repo in SOURCE_REPOSITORIES:
            patterns = [
                f"../{repo}",
                f"..\\\\{repo}",
                f"G:/agent-zoo/{repo}",
                f"G:\\agent-zoo\\{repo}",
                f"g:/agent-zoo/{repo}",
                f"g:\\agent-zoo\\{repo}",
            ]
            for pattern in patterns:
                if pattern.replace("\\", "/").lower() in normalized.lower():
                    references.append(
                        SourceReference(
                            repo=repo,
                            pattern=pattern,
                            path=_relative(project_root, path),
                            line=index,
                            text=line.strip()[:300],
                            reference_kind=_reference_kind(line),
                        )
                    )
    return references


def scan_cache_artifacts(project_root: Path) -> Iterable[CacheArtifact]:
    for path in project_root.rglob("*"):
        try:
            relative = path.relative_to(project_root)
        except ValueError:
            continue
        parts = set(relative.parts)
        cache_parts = parts & CACHE_DIRS
        if not cache_parts:
            continue
        if path.is_dir():
            yield CacheArtifact(
                path=relative.as_posix(),
                kind="directory",
                size_bytes=0,
                reason=f"cache/build directory {sorted(cache_parts)[0]} should not be used as completion evidence",
            )
        elif path.is_file():
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            yield CacheArtifact(
                path=relative.as_posix(),
                kind="file",
                size_bytes=size,
                reason=f"cache/build file under {sorted(cache_parts)[0]} should not be counted",
            )


def discover_runtime_boundaries(
    project_root: Path,
    *,
    roots: Iterable[str] | None = None,
    files: Iterable[Path] | None = None,
) -> list[RuntimeBoundary]:
    boundaries: list[RuntimeBoundary] = []
    selected_roots = list(roots or ["vendor-runtimes", "packages", "apps"])
    selected_files = (
        list(files)
        if files is not None
        else list(iter_boundary_files(project_root, roots=selected_roots))
    )
    for root in selected_roots:
        if root not in {"vendor-runtimes", "packages", "apps"}:
            continue
        root_path = project_root / root
        if not root_path.exists():
            continue
        if root == "vendor-runtimes":
            for child in sorted(root_path.iterdir()):
                if child.is_dir():
                    boundaries.append(_runtime_boundary_from_directory(project_root, child, "vendor_runtime"))
        else:
            for child in selected_files:
                try:
                    relative = child.relative_to(project_root)
                except ValueError:
                    continue
                if (
                    relative.parts
                    and relative.parts[0] == root
                    and child.suffix in {".py", ".ts", ".tsx"}
                ):
                    rel = relative.as_posix()
                    if any(
                        token in rel.lower()
                        for token in ["runtime", "sidecar", "adapter", "gateway"]
                    ):
                        boundaries.append(
                            _runtime_boundary_from_file(project_root, child)
                        )
    return _deduplicate_boundaries(boundaries)


def assert_clean_boundary(report: CleanBoundaryReport) -> None:
    if report.ok:
        return
    formatted = "\n".join(
        f"{finding.severity} {finding.code} {finding.path}:{finding.line} {finding.message}"
        for finding in report.findings
        if finding.severity in {BoundarySeverity.ERROR, BoundarySeverity.BLOCKER}
    )
    raise AssertionError(f"Zyra submission boundary failed:\n{formatted}")


def boundary_payload(report: CleanBoundaryReport) -> dict[str, Any]:
    payload = report.to_dict()
    payload["blocking_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.severity in {BoundarySeverity.ERROR, BoundarySeverity.BLOCKER}
    ]
    payload["warnings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.severity == BoundarySeverity.WARNING
    ]
    payload["source_reference_summary"] = summarize_source_references(report.source_references)
    payload["cache_summary"] = summarize_cache_artifacts(report.cache_artifacts)
    payload["runtime_boundary_summary"] = summarize_runtime_boundaries(report.runtime_boundaries)
    return payload


def summarize_source_references(references: Iterable[SourceReference]) -> dict[str, Any]:
    by_repo: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_path: dict[str, int] = {}
    for reference in references:
        by_repo[reference.repo] = by_repo.get(reference.repo, 0) + 1
        by_kind[reference.reference_kind] = by_kind.get(reference.reference_kind, 0) + 1
        by_path[reference.path] = by_path.get(reference.path, 0) + 1
    return {
        "total": sum(by_repo.values()),
        "by_repo": dict(sorted(by_repo.items())),
        "by_kind": dict(sorted(by_kind.items())),
        "by_path": dict(sorted(by_path.items())),
    }


def summarize_cache_artifacts(artifacts: Iterable[CacheArtifact]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    total_bytes = 0
    for artifact in artifacts:
        by_kind[artifact.kind] = by_kind.get(artifact.kind, 0) + 1
        total_bytes += artifact.size_bytes
    return {
        "total": sum(by_kind.values()),
        "by_kind": dict(sorted(by_kind.items())),
        "total_bytes": total_bytes,
    }


def summarize_runtime_boundaries(boundaries: Iterable[RuntimeBoundary]) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    incomplete: list[str] = []
    complete = 0
    for boundary in boundaries:
        by_kind[boundary.boundary_kind] = by_kind.get(boundary.boundary_kind, 0) + 1
        if boundary.complete:
            complete += 1
        else:
            incomplete.append(boundary.path)
    return {
        "total": sum(by_kind.values()),
        "complete": complete,
        "incomplete": sorted(incomplete),
        "by_kind": dict(sorted(by_kind.items())),
    }


def _findings_for_source_references(references: Iterable[SourceReference]) -> list[BoundaryFinding]:
    findings: list[BoundaryFinding] = []
    for reference in references:
        severity = BoundarySeverity.BLOCKER if reference.reference_kind in {"runtime", "subprocess", "import"} else BoundarySeverity.WARNING
        code = BoundaryCode.PARENT_SOURCE_REFERENCE
        if reference.pattern.lower().startswith("g:"):
            code = BoundaryCode.ABSOLUTE_SOURCE_REFERENCE
        if reference.reference_kind == "subprocess":
            code = BoundaryCode.SUBPROCESS_SOURCE_REPO_REFERENCE
        elif reference.reference_kind == "import":
            code = BoundaryCode.SOURCE_REPO_IMPORT_REFERENCE
        findings.append(
            BoundaryFinding(
                code=code,
                severity=severity,
                message=f"{reference.path} references source repository {reference.repo} through {reference.pattern!r}",
                path=reference.path,
                line=reference.line,
                fragment=reference.text,
                remediation="Move required code into zyra or use a productized runtime under vendor-runtimes with ledger boundary evidence.",
                metadata={"repo": reference.repo, "reference_kind": reference.reference_kind},
            )
        )
    return findings


def _findings_for_editable_install(project_root: Path, path: Path, lines: Iterable[str]) -> list[BoundaryFinding]:
    findings: list[BoundaryFinding] = []
    if path.name not in {"pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.cfg"}:
        return findings
    for index, line in enumerate(lines, start=1):
        normalized = line.replace("\\", "/").strip()
        if "-e ../" in normalized or "editable = true" in normalized and "../" in normalized:
            findings.append(
                BoundaryFinding(
                    code=BoundaryCode.EDITABLE_OUTSIDE_PROJECT,
                    severity=BoundarySeverity.BLOCKER,
                    message="Editable dependency points outside the zyra project.",
                    path=_relative(project_root, path),
                    line=index,
                    fragment=line.strip(),
                    remediation="Vendor or productize the dependency inside zyra before relying on it.",
                )
            )
    return findings


def _findings_for_path_env(project_root: Path, path: Path, lines: Iterable[str]) -> list[BoundaryFinding]:
    findings: list[BoundaryFinding] = []
    for index, line in enumerate(lines, start=1):
        normalized = line.replace("\\", "/")
        lower = normalized.lower()
        if "pythonpath" in lower and PARENT_RELATIVE_MARKER in normalized:
            findings.append(
                BoundaryFinding(
                    code=BoundaryCode.PYTHONPATH_INJECTION,
                    severity=BoundarySeverity.BLOCKER,
                    message="PYTHONPATH injects a parent source repository or parent workspace.",
                    path=_relative(project_root, path),
                    line=index,
                    fragment=line.strip()[:300],
                    remediation="Use package paths inside zyra rather than parent workspace injection.",
                )
            )
        if re.search(r"\bPATH\b|\bNODE_PATH\b", line) and PARENT_RELATIVE_MARKER in normalized:
            findings.append(
                BoundaryFinding(
                    code=BoundaryCode.PATH_ENV_OUTSIDE_PROJECT,
                    severity=BoundarySeverity.ERROR,
                    message="Environment path points outside zyra.",
                    path=_relative(project_root, path),
                    line=index,
                    fragment=line.strip()[:300],
                    remediation="Resolve the path to a checked-in zyra directory.",
                )
            )
    return findings


def _findings_for_cache_artifacts(artifacts: Iterable[CacheArtifact]) -> list[BoundaryFinding]:
    findings: list[BoundaryFinding] = []
    for artifact in artifacts:
        severity = BoundarySeverity.INFO
        code = BoundaryCode.CACHE_ARTIFACT_PRESENT
        if artifact.path.startswith(("packages/", "apps/", "scripts/", "tests/")):
            severity = BoundarySeverity.WARNING
        findings.append(
            BoundaryFinding(
                code=code,
                severity=severity,
                message=artifact.reason,
                path=artifact.path,
                remediation="Ignore cache artifacts in line-count and completion evidence; remove before release packaging.",
                metadata={"kind": artifact.kind, "size_bytes": artifact.size_bytes},
            )
        )
    return findings


def _findings_for_runtime_boundaries(boundaries: Iterable[RuntimeBoundary]) -> list[BoundaryFinding]:
    findings: list[BoundaryFinding] = []
    for boundary in boundaries:
        if boundary.complete:
            continue
        severity = BoundarySeverity.WARNING
        if boundary.boundary_kind == "vendor_runtime":
            severity = BoundarySeverity.ERROR
        findings.append(
            BoundaryFinding(
                code=BoundaryCode.VENDOR_RUNTIME_BOUNDARY_MISSING
                if boundary.boundary_kind == "vendor_runtime"
                else BoundaryCode.UNKNOWN_RUNTIME_BOUNDARY,
                severity=severity,
                message=f"Runtime boundary {boundary.path} is missing {', '.join(boundary.missing)}.",
                path=boundary.path,
                remediation="Record runtime_entry, health_check, main_path and test bindings before treating the runtime as productized.",
                metadata={"boundary": boundary.to_dict()},
            )
        )
    return findings


def _runtime_boundary_from_directory(project_root: Path, path: Path, boundary_kind: str) -> RuntimeBoundary:
    rel = _relative(project_root, path)
    evidence: list[str] = []
    for marker in ["README.md", "pyproject.toml", "package.json", "runtime.json", "health.py", "health.ts"]:
        if (path / marker).exists():
            evidence.append(f"marker:{marker}")
    text = _read_small_tree_text(path, limit_files=40)
    has_runtime_entry = any(token in text for token in ["runtime_entry", "RuntimeEntry", "protocol", "launcher"])
    has_health_check = any(token in text for token in ["health_check", "health", "smoke"])
    has_main_path_binding = any(token in text for token in ["main_path", "api_routes", "worker_runtime", "event_types"])
    has_test_binding = any(token in text for token in ["tests/", "unittest", "pytest", "smoke"])
    missing = _missing_boundary_fields(has_runtime_entry, has_health_check, has_main_path_binding, has_test_binding)
    return RuntimeBoundary(
        path=rel,
        boundary_kind=boundary_kind,
        source_repo=_source_repo_from_path(rel),
        has_runtime_entry=has_runtime_entry,
        has_health_check=has_health_check,
        has_main_path_binding=has_main_path_binding,
        has_test_binding=has_test_binding,
        evidence=evidence,
        missing=missing,
    )


def _runtime_boundary_from_file(project_root: Path, path: Path) -> RuntimeBoundary:
    rel = _relative(project_root, path)
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        text = ""
    has_runtime_entry = "RuntimeEntry" in text or "runtime_entry" in text or "command" in text
    has_health_check = "health" in text or "smoke" in text
    has_main_path_binding = any(token in text for token in BOUNDARY_HINTS)
    has_test_binding = "test_" in text or "tests/" in text
    missing = _missing_boundary_fields(has_runtime_entry, has_health_check, has_main_path_binding, has_test_binding)
    return RuntimeBoundary(
        path=rel,
        boundary_kind="source_runtime",
        source_repo=_source_repo_from_path(rel),
        has_runtime_entry=has_runtime_entry,
        has_health_check=has_health_check,
        has_main_path_binding=has_main_path_binding,
        has_test_binding=has_test_binding,
        evidence=_boundary_evidence_from_text(text),
        missing=missing,
    )


def _missing_boundary_fields(
    has_runtime_entry: bool,
    has_health_check: bool,
    has_main_path_binding: bool,
    has_test_binding: bool,
) -> list[str]:
    missing: list[str] = []
    if not has_runtime_entry:
        missing.append("runtime_entry")
    if not has_health_check:
        missing.append("health_check")
    if not has_main_path_binding:
        missing.append("main_path_binding")
    if not has_test_binding:
        missing.append("test_binding")
    return missing


def _boundary_evidence_from_text(text: str) -> list[str]:
    evidence: list[str] = []
    for hint in BOUNDARY_HINTS:
        if hint in text:
            evidence.append(f"hint:{hint}")
    if "unittest" in text or "pytest" in text:
        evidence.append("hint:test_framework")
    if "health" in text:
        evidence.append("hint:health")
    return sorted(set(evidence))


def _read_small_tree_text(path: Path, *, limit_files: int) -> str:
    chunks: list[str] = []
    count = 0
    for child in path.rglob("*"):
        if count >= limit_files:
            break
        if not child.is_file() or child.suffix not in SCANNED_SUFFIXES:
            continue
        try:
            chunks.append(child.read_text(encoding="utf-8", errors="ignore")[:20_000])
            count += 1
        except OSError:
            continue
    return "\n".join(chunks)


def _deduplicate_boundaries(boundaries: Iterable[RuntimeBoundary]) -> list[RuntimeBoundary]:
    result: dict[str, RuntimeBoundary] = {}
    for boundary in boundaries:
        existing = result.get(boundary.path)
        if existing is None:
            result[boundary.path] = boundary
            continue
        merged = RuntimeBoundary(
            path=boundary.path,
            boundary_kind=existing.boundary_kind,
            source_repo=existing.source_repo or boundary.source_repo,
            has_runtime_entry=existing.has_runtime_entry or boundary.has_runtime_entry,
            has_health_check=existing.has_health_check or boundary.has_health_check,
            has_main_path_binding=existing.has_main_path_binding or boundary.has_main_path_binding,
            has_test_binding=existing.has_test_binding or boundary.has_test_binding,
            evidence=sorted(set(existing.evidence + boundary.evidence)),
            missing=[],
        )
        merged.missing = _missing_boundary_fields(
            merged.has_runtime_entry,
            merged.has_health_check,
            merged.has_main_path_binding,
            merged.has_test_binding,
        )
        result[boundary.path] = merged
    return [result[key] for key in sorted(result)]


def _reference_kind(line: str) -> str:
    lower = line.lower()
    if "subprocess" in lower or "start-process" in lower or "popen" in lower:
        return "subprocess"
    if "import" in lower or "sys.path" in lower:
        return "import"
    if "runtime" in lower or "sidecar" in lower or "launcher" in lower:
        return "runtime"
    if "test" in lower or "fixture" in lower:
        return "test"
    return "metadata"


def _source_repo_from_path(path: str) -> str:
    lowered = path.lower()
    for repo in SOURCE_REPOSITORIES:
        if repo.lower().replace("-", "_") in lowered or repo.lower() in lowered:
            return repo
    return ""


def _is_non_runtime_scan_path(path: Path) -> bool:
    return any(
        path == root or root in path.parents
        for root in VENDOR_LIKE_RUNTIME_ROOTS + NON_RUNTIME_DATA_ROOTS
    )


def _relative(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def write_boundary_report(path: Path, report: CleanBoundaryReport) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(boundary_payload(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def environment_boundary_notes(project_root: Path) -> list[BoundaryFinding]:
    findings: list[BoundaryFinding] = []
    for name in ["PYTHONPATH", "NODE_PATH", "PATH"]:
        value = os.environ.get(name, "")
        normalized = value.replace("\\", "/")
        if PARENT_RELATIVE_MARKER not in normalized and "G:/agent-zoo" not in normalized and "g:/agent-zoo" not in normalized:
            continue
        for repo in SOURCE_REPOSITORIES:
            if repo.lower() not in normalized.lower():
                continue
            findings.append(
                BoundaryFinding(
                    code=BoundaryCode.PATH_ENV_OUTSIDE_PROJECT,
                    severity=BoundarySeverity.WARNING,
                    message=f"Environment variable {name} references source repository {repo}.",
                    path=f"env:{name}",
                    fragment=value[:300],
                    remediation="Do not require this environment value for default zyra execution.",
                    metadata={"project_root": str(project_root), "repo": repo},
                )
            )
    return findings


def clean_copy_instructions(report: CleanBoundaryReport) -> dict[str, Any]:
    return {
        "copy_roots": ["apps", "packages", "scripts", "tests", "vendor-runtimes", "skills", "pyproject.toml", "README.md"],
        "exclude_roots": [".git", ".venv", "tmp", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"],
        "must_not_copy_source_repos": list(SOURCE_REPOSITORIES),
        "boundary_ok": report.ok,
        "blocking_findings": [
            finding.to_dict()
            for finding in report.findings
            if finding.severity in {BoundarySeverity.ERROR, BoundarySeverity.BLOCKER}
        ],
    }


def path_is_countable_source(path: str) -> bool:
    classification = classify_path(path)
    return str(classification.verdict) == "effective" and classification.is_source_like
