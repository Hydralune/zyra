from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ledger_policy import CountVerdict, PathClassification, classify_path
from .ledger_models import to_jsonable


COUNTED_PATHS = ["apps", "packages", "tests", "scripts", "vendor-runtimes", "skills"]
UPSTREAM_TYPE_STUB_MARKER = "Auto-generated type stub"


@dataclass(slots=True)
class NumstatFile:
    path: str
    added: int
    deleted: int
    classification: PathClassification
    content_flags: list[str] = field(default_factory=list)

    @property
    def is_content_excluded(self) -> bool:
        return "upstream-type-stub" in self.content_flags

    @property
    def effective_verdict(self) -> CountVerdict:
        if self.is_content_excluded:
            return CountVerdict.EXCLUDED
        return self.classification.verdict

    @property
    def effective_reason(self) -> str:
        if self.is_content_excluded:
            return "upstream auto-generated type stubs are excluded from effective code"
        return self.classification.reason

    @property
    def effective_added(self) -> int:
        return self.added if self.effective_verdict == CountVerdict.EFFECTIVE else 0

    @property
    def excluded_added(self) -> int:
        return 0 if self.effective_verdict == CountVerdict.EFFECTIVE else self.added

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "added": self.added,
            "deleted": self.deleted,
            "effective_added": self.effective_added,
            "excluded_added": self.excluded_added,
            "classification": self.classification.to_dict(),
            "content_flags": list(self.content_flags),
            "effective_verdict": str(self.effective_verdict),
            "effective_reason": self.effective_reason,
        }


@dataclass(slots=True)
class EffectiveLineCountReport:
    base: str
    head: str
    cached: bool
    counted_paths: list[str]
    raw_added: int
    raw_deleted: int
    effective_added: int
    effective_deleted: int
    excluded_added: int
    review_added: int
    files: list[NumstatFile] = field(default_factory=list)
    excluded_files: list[NumstatFile] = field(default_factory=list)
    review_files: list[NumstatFile] = field(default_factory=list)
    effective_files: list[NumstatFile] = field(default_factory=list)
    minimum_effective_lines: int = 0

    @property
    def ok(self) -> bool:
        return self.minimum_effective_lines <= 0 or self.effective_added >= self.minimum_effective_lines

    @property
    def shortfall(self) -> int:
        return max(0, self.minimum_effective_lines - self.effective_added)

    def to_dict(self) -> dict[str, Any]:
        return {
            "base": self.base,
            "head": self.head,
            "cached": self.cached,
            "counted_paths": list(self.counted_paths),
            "raw_added": self.raw_added,
            "raw_deleted": self.raw_deleted,
            "effective_added": self.effective_added,
            "effective_deleted": self.effective_deleted,
            "excluded_added": self.excluded_added,
            "review_added": self.review_added,
            "minimum_effective_lines": self.minimum_effective_lines,
            "ok": self.ok,
            "shortfall": self.shortfall,
            "files": [item.to_dict() for item in self.files],
            "effective_files": [item.to_dict() for item in self.effective_files],
            "excluded_files": [item.to_dict() for item in self.excluded_files],
            "review_files": [item.to_dict() for item in self.review_files],
        }


def build_line_count_report(
    project_root: Path,
    *,
    base: str,
    head: str = "HEAD",
    cached: bool = False,
    minimum_effective_lines: int = 0,
    counted_paths: list[str] | None = None,
) -> EffectiveLineCountReport:
    paths = counted_paths or COUNTED_PATHS
    files = diff_numstat(project_root, base=base, head=head, cached=cached, counted_paths=paths)
    raw_added = sum(item.added for item in files)
    raw_deleted = sum(item.deleted for item in files)
    effective_files = [item for item in files if item.effective_added]
    excluded_files = [item for item in files if item.excluded_added]
    review_files = [item for item in files if item.effective_verdict == CountVerdict.REVIEW]
    return EffectiveLineCountReport(
        base=base,
        head=head,
        cached=cached,
        counted_paths=list(paths),
        raw_added=raw_added,
        raw_deleted=raw_deleted,
        effective_added=sum(item.added for item in effective_files),
        effective_deleted=sum(item.deleted for item in effective_files),
        excluded_added=sum(item.added for item in excluded_files),
        review_added=sum(item.added for item in review_files),
        files=files,
        excluded_files=excluded_files,
        review_files=review_files,
        effective_files=effective_files,
        minimum_effective_lines=minimum_effective_lines,
    )


def diff_numstat(
    project_root: Path,
    *,
    base: str,
    head: str = "HEAD",
    cached: bool = False,
    counted_paths: list[str] | None = None,
) -> list[NumstatFile]:
    command = ["git", "diff", "--numstat"]
    if cached:
        command.append("--cached")
    command.append(base)
    if not cached:
        command.append(head)
    command.extend(["--", *(counted_paths or COUNTED_PATHS)])
    completed = subprocess.run(command, cwd=project_root, check=True, text=True, capture_output=True)
    return parse_numstat(completed.stdout, project_root=project_root)


def parse_numstat(text: str, *, project_root: Path | None = None) -> list[NumstatFile]:
    files: list[NumstatFile] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added = _parse_numstat_int(parts[0])
        deleted = _parse_numstat_int(parts[1])
        path = parts[2]
        files.append(
            NumstatFile(
                path=path,
                added=added,
                deleted=deleted,
                classification=classify_path(path),
                content_flags=_content_flags(project_root, path),
            )
        )
    return files


def summarize_line_count_by_reason(report: EffectiveLineCountReport) -> dict[str, int]:
    totals: dict[str, int] = {}
    for item in report.files:
        key = f"{item.effective_verdict}:{item.effective_reason}"
        totals[key] = totals.get(key, 0) + item.added
    return dict(sorted(totals.items()))


def assert_effective_line_count(report: EffectiveLineCountReport) -> None:
    if report.ok:
        return
    excluded = "\n".join(
        f"- {item.path}: +{item.added} excluded because {item.classification.reason}"
        for item in report.excluded_files[:20]
    )
    raise AssertionError(
        f"Effective line-count gate failed: required={report.minimum_effective_lines}, "
        f"effective={report.effective_added}, raw={report.raw_added}, shortfall={report.shortfall}\n{excluded}"
    )


def _parse_numstat_int(value: str) -> int:
    return int(value) if value.isdigit() else 0


def line_count_payload(report: EffectiveLineCountReport) -> dict[str, Any]:
    payload = report.to_dict()
    payload["by_reason"] = summarize_line_count_by_reason(report)
    payload["seed_or_inventory_excluded"] = [
        item.to_dict()
        for item in report.excluded_files
        if item.classification.is_generated_data
    ]
    payload["upstream_type_stub_excluded"] = [
        item.to_dict()
        for item in report.excluded_files
        if item.is_content_excluded
    ]
    payload["review_required"] = [item.to_dict() for item in report.review_files]
    return to_jsonable(payload)


def _content_flags(project_root: Path | None, path: str) -> list[str]:
    if project_root is None:
        return []
    classification = classify_path(path)
    if not classification.is_project_relative:
        return []
    candidate = project_root / classification.normalized_path
    if not candidate.exists() or not candidate.is_file():
        return []
    if classification.suffix.lower() not in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
        return []
    try:
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if UPSTREAM_TYPE_STUB_MARKER in text:
        return ["upstream-type-stub"]
    return []
