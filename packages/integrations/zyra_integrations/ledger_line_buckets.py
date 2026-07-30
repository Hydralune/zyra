from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from .ledger_linecount import EffectiveLineCountReport, NumstatFile, build_line_count_report
from .ledger_models import to_jsonable
from .ledger_policy import CountVerdict, classify_path


class LineBucket(StrEnum):
    PRODUCTION = "production"
    TEST = "test"
    SCRIPT = "script"
    GENERATED = "generated"
    DATA = "data"
    DOCS = "docs"
    VENDOR_LIKE = "vendor_like"
    SOURCE_POOL = "source_pool"
    ADAPTER_ONLY = "adapter_only"
    MOCK_FIXTURE = "mock_fixture"
    CACHE = "cache"
    REVIEW = "review"
    EXCLUDED = "excluded"


class BucketRisk(StrEnum):
    OK = "ok"
    REVIEW = "review"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


@dataclass(slots=True)
class BucketRule:
    bucket: LineBucket
    risk: BucketRisk
    reason: str
    patterns: list[str] = field(default_factory=list)
    suffixes: list[str] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)

    def matches(self, path: str) -> bool:
        normalized = path.replace("\\", "/").lower()
        suffix = Path(normalized).suffix
        root = normalized.split("/", 1)[0] if normalized else ""
        if self.roots and root in self.roots:
            return True
        if self.suffixes and suffix in self.suffixes:
            return True
        return any(pattern in normalized for pattern in self.patterns)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class BucketedFile:
    path: str
    added: int
    deleted: int
    effective_added: int
    excluded_added: int
    bucket: LineBucket
    risk: BucketRisk
    reason: str
    classification: dict[str, Any]
    flags: list[str] = field(default_factory=list)

    @property
    def counts_toward_minimum(self) -> bool:
        return self.bucket in {LineBucket.PRODUCTION, LineBucket.TEST, LineBucket.SCRIPT} and self.risk in {BucketRisk.OK, BucketRisk.REVIEW}

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["counts_toward_minimum"] = self.counts_toward_minimum
        return payload


@dataclass(slots=True)
class BucketTotals:
    bucket: LineBucket
    files: int = 0
    added: int = 0
    deleted: int = 0
    effective_added: int = 0
    excluded_added: int = 0
    risk: BucketRisk = BucketRisk.OK

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class BucketFinding:
    code: str
    risk: BucketRisk
    message: str
    path: str = ""
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class LineBucketReport:
    base: str
    head: str
    cached: bool
    minimum_effective_lines: int
    raw_added: int
    effective_added: int
    bucket_effective_added: int
    production_added: int
    test_added: int
    script_added: int
    excluded_added: int
    review_added: int
    adapter_only_added: int
    mock_fixture_added: int
    vendor_like_added: int
    data_added: int
    generated_added: int
    docs_added: int
    files: list[BucketedFile]
    totals: list[BucketTotals]
    findings: list[BucketFinding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.bucket_effective_added >= self.minimum_effective_lines
            and not any(finding.risk in {BucketRisk.ERROR, BucketRisk.BLOCKER} for finding in self.findings)
        )

    @property
    def shortfall(self) -> int:
        return max(0, self.minimum_effective_lines - self.bucket_effective_added)

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["ok"] = self.ok
        payload["shortfall"] = self.shortfall
        return payload


DEFAULT_BUCKET_RULES = [
    BucketRule(
        bucket=LineBucket.CACHE,
        risk=BucketRisk.BLOCKER,
        reason="cache/build output cannot count",
        patterns=["/__pycache__/", "/.pytest_cache/", "/.mypy_cache/", "/.ruff_cache/", "/node_modules/", "/dist/", "/build/"],
    ),
    BucketRule(
        bucket=LineBucket.VENDOR_LIKE,
        risk=BucketRisk.WARNING,
        reason="vendor-runtime lines count only for Zyra-owned adapter/bridge code",
        roots=["vendor-runtimes"],
        patterns=[
            "packages/integrations/loopx_runtime/",
            "vendor/",
            "third_party/",
        ],
    ),
    BucketRule(
        bucket=LineBucket.DATA,
        risk=BucketRisk.WARNING,
        reason="seed/inventory/source-map/data cannot count as implementation",
        patterns=["/data/", "seed", "inventory", "source_map", "source-map", "ledger_seed"],
        suffixes=[".json", ".jsonl", ".yaml", ".yml", ".csv", ".tsv", ".sqlite", ".sqlite3"],
    ),
    BucketRule(
        bucket=LineBucket.DOCS,
        risk=BucketRisk.WARNING,
        reason="documentation is excluded from effective code",
        roots=["docs"],
        suffixes=[".md", ".mdx", ".rst", ".txt", ".pdf"],
    ),
    BucketRule(
        bucket=LineBucket.MOCK_FIXTURE,
        risk=BucketRisk.WARNING,
        reason="mock/fixture-only code needs explicit review",
        patterns=["/fixtures/", "/fixture/", "/mocks/", "/mock_", "_mock", "golden", "snapshot"],
    ),
    BucketRule(
        bucket=LineBucket.SOURCE_POOL,
        risk=BucketRisk.BLOCKER,
        reason="source pools and raw upstream mirrors cannot count",
        patterns=["source_pool", "source-pool", "runtime-sources", "third_party/source", "upstream_mirror"],
    ),
    BucketRule(
        bucket=LineBucket.ADAPTER_ONLY,
        risk=BucketRisk.REVIEW,
        reason="thin adapters need main-path/event/test evidence",
        patterns=["adapter", "bridge", "gateway", "sidecar", "launcher"],
    ),
    BucketRule(
        bucket=LineBucket.TEST,
        risk=BucketRisk.REVIEW,
        reason="test lines count only with production behavior under test",
        roots=["tests"],
        patterns=["test_"],
    ),
    BucketRule(
        bucket=LineBucket.SCRIPT,
        risk=BucketRisk.REVIEW,
        reason="scripts count when they enforce or verify real behavior",
        roots=["scripts"],
    ),
    BucketRule(
        bucket=LineBucket.PRODUCTION,
        risk=BucketRisk.OK,
        reason="production source under apps/packages/skills",
        roots=["apps", "packages", "skills"],
    ),
]


def build_line_bucket_report(
    project_root: Path,
    *,
    base: str,
    head: str = "HEAD",
    cached: bool = False,
    minimum_effective_lines: int = 0,
    counted_paths: list[str] | None = None,
) -> LineBucketReport:
    line_count = build_line_count_report(
        project_root,
        base=base,
        head=head,
        cached=cached,
        minimum_effective_lines=minimum_effective_lines,
        counted_paths=counted_paths,
    )
    return bucket_line_count_report(project_root, line_count)


def bucket_line_count_report(project_root: Path, report: EffectiveLineCountReport) -> LineBucketReport:
    files = [bucket_numstat_file(project_root, item) for item in report.files]
    totals = bucket_totals(files)
    totals_by_bucket = {total.bucket: total for total in totals}
    findings = bucket_findings(files, report.minimum_effective_lines)
    bucket_effective_added = sum(file.added for file in files if file.counts_toward_minimum)
    return LineBucketReport(
        base=report.base,
        head=report.head,
        cached=report.cached,
        minimum_effective_lines=report.minimum_effective_lines,
        raw_added=report.raw_added,
        effective_added=report.effective_added,
        bucket_effective_added=bucket_effective_added,
        production_added=totals_by_bucket.get(LineBucket.PRODUCTION, BucketTotals(LineBucket.PRODUCTION)).added,
        test_added=totals_by_bucket.get(LineBucket.TEST, BucketTotals(LineBucket.TEST)).added,
        script_added=totals_by_bucket.get(LineBucket.SCRIPT, BucketTotals(LineBucket.SCRIPT)).added,
        excluded_added=report.excluded_added,
        review_added=report.review_added,
        adapter_only_added=totals_by_bucket.get(LineBucket.ADAPTER_ONLY, BucketTotals(LineBucket.ADAPTER_ONLY)).added,
        mock_fixture_added=totals_by_bucket.get(LineBucket.MOCK_FIXTURE, BucketTotals(LineBucket.MOCK_FIXTURE)).added,
        vendor_like_added=totals_by_bucket.get(LineBucket.VENDOR_LIKE, BucketTotals(LineBucket.VENDOR_LIKE)).added,
        data_added=totals_by_bucket.get(LineBucket.DATA, BucketTotals(LineBucket.DATA)).added,
        generated_added=totals_by_bucket.get(LineBucket.GENERATED, BucketTotals(LineBucket.GENERATED)).added,
        docs_added=totals_by_bucket.get(LineBucket.DOCS, BucketTotals(LineBucket.DOCS)).added,
        files=files,
        totals=totals,
        findings=findings,
    )


def bucket_numstat_file(project_root: Path, item: NumstatFile) -> BucketedFile:
    classification = item.classification
    bucket, risk, reason = classify_line_bucket(item.path)
    flags = list(classification.flags)
    if item.is_content_excluded:
        bucket = LineBucket.GENERATED
        risk = BucketRisk.WARNING
        reason = item.effective_reason
        flags.append("content-excluded")
    if classification.verdict == CountVerdict.EXCLUDED and bucket not in {LineBucket.DATA, LineBucket.DOCS, LineBucket.CACHE, LineBucket.SOURCE_POOL}:
        bucket = LineBucket.EXCLUDED
        risk = BucketRisk.WARNING
        reason = classification.reason
    if classification.verdict == CountVerdict.REVIEW and risk == BucketRisk.OK:
        bucket = LineBucket.REVIEW
        risk = BucketRisk.REVIEW
        reason = classification.reason
    content_flags = inspect_file_content(project_root, item.path)
    flags.extend(content_flags)
    if "mock-fixture-content" in content_flags and bucket in {LineBucket.PRODUCTION, LineBucket.SCRIPT, LineBucket.TEST}:
        bucket = LineBucket.MOCK_FIXTURE
        risk = BucketRisk.WARNING
        reason = "file content indicates mock/fixture-only implementation"
    if "adapter-only-content" in content_flags and bucket == LineBucket.PRODUCTION:
        bucket = LineBucket.ADAPTER_ONLY
        risk = BucketRisk.REVIEW
        reason = "file content appears adapter-heavy and needs main-path evidence"
    return BucketedFile(
        path=item.path,
        added=item.added,
        deleted=item.deleted,
        effective_added=item.effective_added,
        excluded_added=item.excluded_added,
        bucket=bucket,
        risk=risk,
        reason=reason,
        classification=classification.to_dict(),
        flags=sorted(set(flags)),
    )


def classify_line_bucket(path: str) -> tuple[LineBucket, BucketRisk, str]:
    normalized = path.replace("\\", "/").lower()
    for rule in DEFAULT_BUCKET_RULES:
        if rule.matches(normalized):
            return rule.bucket, rule.risk, rule.reason
    classification = classify_path(path)
    if classification.is_cache:
        return LineBucket.CACHE, BucketRisk.BLOCKER, classification.reason
    if classification.is_generated_data:
        return LineBucket.DATA, BucketRisk.WARNING, classification.reason
    if classification.is_documentation:
        return LineBucket.DOCS, BucketRisk.WARNING, classification.reason
    if classification.verdict == CountVerdict.EXCLUDED:
        return LineBucket.EXCLUDED, BucketRisk.WARNING, classification.reason
    if classification.verdict == CountVerdict.REVIEW:
        return LineBucket.REVIEW, BucketRisk.REVIEW, classification.reason
    return LineBucket.PRODUCTION, BucketRisk.OK, classification.reason


def inspect_file_content(project_root: Path, path: str) -> list[str]:
    classification = classify_path(path)
    if not classification.is_project_relative:
        return []
    candidate = project_root / classification.normalized_path
    if not candidate.exists() or not candidate.is_file():
        return []
    if candidate.suffix.lower() not in {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
        return []
    try:
        text = candidate.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    lowered = text.lower()
    flags: list[str] = []
    mock_tokens = ["mock", "fixture", "golden", "snapshot", "fake_", "dummy"]
    adapter_tokens = ["adapter", "bridge", "gateway", "sidecar", "subprocess", "proxy"]
    runtime_tokens = ["event", "audit", "state", "persist", "transition", "policy", "route"]
    if sum(1 for token in mock_tokens if token in lowered) >= 3 and not any(token in lowered for token in runtime_tokens):
        flags.append("mock-fixture-content")
    if sum(1 for token in adapter_tokens if token in lowered) >= 3 and "event" not in lowered and "state" not in lowered:
        flags.append("adapter-only-content")
    if "auto-generated" in lowered or "generated by" in lowered:
        flags.append("generated-content")
    return flags


def bucket_totals(files: Iterable[BucketedFile]) -> list[BucketTotals]:
    totals: dict[LineBucket, BucketTotals] = {}
    risk_rank = {
        BucketRisk.OK: 0,
        BucketRisk.REVIEW: 1,
        BucketRisk.WARNING: 2,
        BucketRisk.ERROR: 3,
        BucketRisk.BLOCKER: 4,
    }
    for file in files:
        total = totals.setdefault(file.bucket, BucketTotals(bucket=file.bucket))
        total.files += 1
        total.added += file.added
        total.deleted += file.deleted
        total.effective_added += file.effective_added
        total.excluded_added += file.excluded_added
        if risk_rank[file.risk] > risk_rank[total.risk]:
            total.risk = file.risk
    return [totals[key] for key in sorted(totals, key=lambda bucket: str(bucket))]


def bucket_findings(files: list[BucketedFile], minimum_effective_lines: int) -> list[BucketFinding]:
    findings: list[BucketFinding] = []
    raw_added = sum(file.added for file in files)
    bucket_effective = sum(file.added for file in files if file.counts_toward_minimum)
    if minimum_effective_lines and bucket_effective < minimum_effective_lines:
        findings.append(
            BucketFinding(
                code="LINE_BUCKET_SHORTFALL",
                risk=BucketRisk.BLOCKER,
                message=f"Bucketed effective lines {bucket_effective} are below minimum {minimum_effective_lines}.",
                remediation="Add production/script/test code that drives real behavior, or document a strong reason.",
                metadata={"minimum_effective_lines": minimum_effective_lines, "bucket_effective_added": bucket_effective},
            )
        )
    counts = Counter(file.bucket for file in files)
    added_by_bucket: dict[LineBucket, int] = defaultdict(int)
    for file in files:
        added_by_bucket[file.bucket] += file.added
        if file.risk == BucketRisk.BLOCKER:
            findings.append(
                BucketFinding(
                    code="BLOCKED_LINE_BUCKET",
                    risk=BucketRisk.BLOCKER,
                    message=f"{file.path} is in blocked line bucket {file.bucket}: {file.reason}",
                    path=file.path,
                    remediation="Remove or reclassify the file before using line counts as evidence.",
                )
            )
    if raw_added:
        non_counting = sum(
            added
            for bucket, added in added_by_bucket.items()
            if bucket
            in {
                LineBucket.DATA,
                LineBucket.DOCS,
                LineBucket.GENERATED,
                LineBucket.VENDOR_LIKE,
                LineBucket.SOURCE_POOL,
                LineBucket.MOCK_FIXTURE,
                LineBucket.CACHE,
                LineBucket.EXCLUDED,
            }
        )
        if non_counting / max(raw_added, 1) > 0.4:
            findings.append(
                BucketFinding(
                    code="NON_COUNTING_BUCKETS_DOMINATE",
                    risk=BucketRisk.WARNING,
                    message="Non-counting buckets dominate the raw diff.",
                    remediation="Report data/docs/vendor/mock/generated separately from effective code.",
                    metadata={"raw_added": raw_added, "non_counting_added": non_counting},
                )
            )
    test_added = added_by_bucket[LineBucket.TEST]
    production_added = added_by_bucket[LineBucket.PRODUCTION] + added_by_bucket[LineBucket.SCRIPT]
    if test_added > production_added * 2 and test_added > 1000:
        findings.append(
            BucketFinding(
                code="TEST_LINES_DOMINATE",
                risk=BucketRisk.WARNING,
                message="Test lines dominate production/script implementation lines.",
                remediation="Ensure production behavior is substantial and not replaced by test volume.",
                metadata={"test_added": test_added, "production_or_script_added": production_added},
            )
        )
    if counts[LineBucket.ADAPTER_ONLY] and added_by_bucket[LineBucket.ADAPTER_ONLY] > production_added:
        findings.append(
            BucketFinding(
                code="ADAPTER_ONLY_DOMINATES",
                risk=BucketRisk.WARNING,
                message="Adapter-only lines exceed production implementation lines.",
                remediation="Show main-path state/event/audit behavior behind the adapter before counting it.",
                metadata={"adapter_only_added": added_by_bucket[LineBucket.ADAPTER_ONLY], "production_or_script_added": production_added},
            )
        )
    return findings


def line_bucket_payload(report: LineBucketReport) -> dict[str, Any]:
    payload = report.to_dict()
    payload["by_bucket"] = {str(total.bucket): total.to_dict() for total in report.totals}
    payload["blocking_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.risk in {BucketRisk.ERROR, BucketRisk.BLOCKER}
    ]
    payload["warning_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.risk == BucketRisk.WARNING
    ]
    return payload


def assert_line_buckets(report: LineBucketReport) -> None:
    if report.ok:
        return
    formatted = "\n".join(
        f"{finding.risk} {finding.code} {finding.path}: {finding.message}"
        for finding in report.findings
        if finding.risk in {BucketRisk.ERROR, BucketRisk.BLOCKER}
    )
    raise AssertionError(f"Line bucket gate failed:\n{formatted}")
