from __future__ import annotations

import ast
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from .ledger_models import InternalizationLedgerEntry, TestEntry, to_jsonable
from .ledger_store import InternalizationLedger


class TestQualitySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class TestQualityCode(StrEnum):
    TEST_EXISTS = "TEST_EXISTS"
    TEST_MISSING = "TEST_MISSING"
    IMPORT_ONLY_TEST = "IMPORT_ONLY_TEST"
    MOCK_HEAVY_TEST = "MOCK_HEAVY_TEST"
    FIXTURE_ONLY_TEST = "FIXTURE_ONLY_TEST"
    NEGATIVE_PATH_MISSING = "NEGATIVE_PATH_MISSING"
    ASSERTION_MISSING = "ASSERTION_MISSING"
    API_BEHAVIOR_COVERED = "API_BEHAVIOR_COVERED"
    CLI_BEHAVIOR_COVERED = "CLI_BEHAVIOR_COVERED"
    EVENT_BEHAVIOR_COVERED = "EVENT_BEHAVIOR_COVERED"
    BOUNDARY_BEHAVIOR_COVERED = "BOUNDARY_BEHAVIOR_COVERED"
    LINE_BUCKET_BEHAVIOR_COVERED = "LINE_BUCKET_BEHAVIOR_COVERED"
    DISCONNECT_BEHAVIOR_COVERED = "DISCONNECT_BEHAVIOR_COVERED"


@dataclass(slots=True)
class TestFileSignals:
    path: str
    exists: bool
    test_functions: int = 0
    assertion_count: int = 0
    import_count: int = 0
    mock_markers: int = 0
    fixture_markers: int = 0
    subprocess_calls: int = 0
    api_calls: int = 0
    event_assertions: int = 0
    negative_assertions: int = 0
    boundary_assertions: int = 0
    line_bucket_assertions: int = 0
    disconnect_assertions: int = 0
    unittest_cases: int = 0
    raw_line_count: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def has_behavior_assertions(self) -> bool:
        return self.assertion_count > 0 and self.test_functions > 0

    @property
    def import_only(self) -> bool:
        return self.exists and self.test_functions == 0 and self.import_count > 0

    @property
    def mock_heavy(self) -> bool:
        return self.mock_markers > max(self.assertion_count, 1) * 2

    @property
    def fixture_only(self) -> bool:
        return self.fixture_markers > 0 and self.assertion_count == 0

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["has_behavior_assertions"] = self.has_behavior_assertions
        payload["import_only"] = self.import_only
        payload["mock_heavy"] = self.mock_heavy
        payload["fixture_only"] = self.fixture_only
        return payload


@dataclass(slots=True)
class TestEntryQuality:
    ledger_id: str
    source_repo: str
    owner_unit: str
    test_entry: dict[str, Any]
    file_signals: TestFileSignals
    quality_ok: bool
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class TestQualityFinding:
    code: TestQualityCode
    severity: TestQualitySeverity
    message: str
    path: str = ""
    ledger_id: str = ""
    owner_unit: str = ""
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class TestQualityReport:
    ok: bool
    project_root: str
    owner_unit: str
    checked_entries: int
    checked_test_entries: int
    existing_test_files: int
    missing_test_files: int
    behavior_test_files: int
    import_only_files: int
    mock_heavy_files: int
    fixture_only_files: int
    negative_path_files: int
    api_behavior_files: int
    cli_behavior_files: int
    event_behavior_files: int
    boundary_behavior_files: int
    line_bucket_behavior_files: int
    disconnect_behavior_files: int
    findings: list[TestQualityFinding] = field(default_factory=list)
    entries: list[TestEntryQuality] = field(default_factory=list)
    file_signals: list[TestFileSignals] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == TestQualitySeverity.BLOCKER)

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == TestQualitySeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == TestQualitySeverity.WARNING)

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["blocker_count"] = self.blocker_count
        payload["error_count"] = self.error_count
        payload["warning_count"] = self.warning_count
        return payload


def build_test_quality_report(
    project_root: Path,
    ledger: InternalizationLedger,
    *,
    owner_unit: str = "",
    include_entries: bool = True,
) -> TestQualityReport:
    entries = ledger.by_owner_unit(owner_unit) if owner_unit else ledger.entries()
    signal_cache: dict[str, TestFileSignals] = {}
    entry_results: list[TestEntryQuality] = []
    findings: list[TestQualityFinding] = []
    for entry in entries:
        if not entry.test_entries:
            findings.append(
                TestQualityFinding(
                    code=TestQualityCode.TEST_MISSING,
                    severity=TestQualitySeverity.WARNING,
                    message="Ledger entry has no test entries.",
                    ledger_id=entry.ledger_id,
                    owner_unit=entry.owner_unit,
                    remediation="Add a behavior test entry before internalizing or productizing the record.",
                )
            )
            continue
        for test_entry in entry.test_entries:
            signals = signal_cache.setdefault(test_entry.path, analyze_test_file(project_root, test_entry.path))
            quality = quality_for_test_entry(entry, test_entry, signals)
            entry_results.append(quality)
            findings.extend(findings_for_test_entry(quality))
    file_signals = list(signal_cache.values())
    findings.extend(global_test_quality_findings(file_signals, owner_unit=owner_unit))
    ok = not any(finding.severity in {TestQualitySeverity.ERROR, TestQualitySeverity.BLOCKER} for finding in findings)
    existing = [signals for signals in file_signals if signals.exists]
    return TestQualityReport(
        ok=ok,
        project_root=str(project_root),
        owner_unit=owner_unit or "all",
        checked_entries=len(entries),
        checked_test_entries=sum(len(entry.test_entries) for entry in entries),
        existing_test_files=len(existing),
        missing_test_files=sum(1 for signals in file_signals if not signals.exists),
        behavior_test_files=sum(1 for signals in existing if signals.has_behavior_assertions),
        import_only_files=sum(1 for signals in existing if signals.import_only),
        mock_heavy_files=sum(1 for signals in existing if signals.mock_heavy),
        fixture_only_files=sum(1 for signals in existing if signals.fixture_only),
        negative_path_files=sum(1 for signals in existing if signals.negative_assertions),
        api_behavior_files=sum(1 for signals in existing if signals.api_calls),
        cli_behavior_files=sum(1 for signals in existing if signals.subprocess_calls),
        event_behavior_files=sum(1 for signals in existing if signals.event_assertions),
        boundary_behavior_files=sum(1 for signals in existing if signals.boundary_assertions),
        line_bucket_behavior_files=sum(1 for signals in existing if signals.line_bucket_assertions),
        disconnect_behavior_files=sum(1 for signals in existing if signals.disconnect_assertions),
        findings=findings,
        entries=entry_results if include_entries else [],
        file_signals=file_signals,
        summary={
            "by_owner_unit": _count_entry_quality(entry_results, "owner_unit"),
            "by_source_repo": _count_entry_quality(entry_results, "source_repo"),
            "finding_codes": _finding_code_counts(findings),
            "test_paths": sorted(signal_cache),
        },
    )


def analyze_test_file(project_root: Path, test_path: str) -> TestFileSignals:
    normalized = test_path.replace("\\", "/").strip()
    candidate = project_root / normalized
    if not candidate.exists() or not candidate.is_file():
        return TestFileSignals(path=normalized, exists=False)
    try:
        text = candidate.read_text(encoding="utf-8", errors="ignore")
    except OSError as error:
        return TestFileSignals(path=normalized, exists=True, errors=[str(error)])
    signals = TestFileSignals(path=normalized, exists=True, raw_line_count=len(text.splitlines()))
    signals.mock_markers = _count_tokens(text, ["mock", "patch(", "monkeypatch", "fake_", "dummy"])
    signals.fixture_markers = _count_tokens(text, ["fixture", "golden", "snapshot", "sample_", "tmpdir"])
    signals.api_calls = _count_tokens(text, ["urllib.request", "_get(", "_post(", "ThreadingHTTPServer", "/ledger", "/events"])
    signals.event_assertions = _count_tokens(text, ["integration_ledger_update", "integration_ledger_audit", "system_notice", "event"])
    signals.negative_assertions = _count_tokens(text, ["assertFalse", "assertRaises", "assertNotEqual", "BAD_REQUEST", "_post_error", "expected HTTP error"])
    signals.boundary_assertions = _count_tokens(text, ["boundary", "parent source", "PARENT_SOURCE", "SUBPROCESS_SOURCE"])
    signals.line_bucket_assertions = _count_tokens(text, ["bucket", "vendor_like", "LineBucket", "line_bucket"])
    signals.disconnect_assertions = _count_tokens(text, ["disconnect", "missing route", "not reachable", "definitely-missing"])
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        signals.errors.append(str(error))
        return signals
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            signals.import_count += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            signals.test_functions += 1
        elif isinstance(node, ast.ClassDef):
            if any(_base_name(base).endswith("TestCase") for base in node.bases):
                signals.unittest_cases += 1
        elif isinstance(node, ast.Assert):
            signals.assertion_count += 1
        elif isinstance(node, ast.Call):
            call_name = _call_name(node)
            if call_name.startswith("self.assert") or call_name == "assert":
                signals.assertion_count += 1
            if "subprocess" in call_name:
                signals.subprocess_calls += 1
    return signals


def quality_for_test_entry(
    entry: InternalizationLedgerEntry,
    test_entry: TestEntry,
    signals: TestFileSignals,
) -> TestEntryQuality:
    warnings: list[str] = []
    blockers: list[str] = []
    if not signals.exists:
        blockers.append("test file does not exist")
    if signals.import_only:
        warnings.append("test file appears import-only")
    if signals.mock_heavy:
        warnings.append("test file is mock-heavy")
    if signals.fixture_only:
        warnings.append("test file appears fixture-only")
    if signals.exists and not signals.has_behavior_assertions:
        warnings.append("test file has no behavior assertions")
    if "api_connected" in str(entry.main_path_status) and not signals.api_calls:
        warnings.append("API-connected entry test does not exercise API calls")
    if "event_log_connected" in str(entry.main_path_status) and not signals.event_assertions:
        warnings.append("event-connected entry test does not assert event payloads")
    quality_ok = not blockers and not any("import-only" in warning or "fixture-only" in warning for warning in warnings)
    return TestEntryQuality(
        ledger_id=entry.ledger_id,
        source_repo=entry.source_repo,
        owner_unit=entry.owner_unit,
        test_entry=to_jsonable(test_entry),
        file_signals=signals,
        quality_ok=quality_ok,
        warnings=sorted(set(warnings)),
        blockers=sorted(set(blockers)),
    )


def findings_for_test_entry(quality: TestEntryQuality) -> list[TestQualityFinding]:
    findings: list[TestQualityFinding] = []
    if quality.blockers:
        for blocker in quality.blockers:
            findings.append(
                TestQualityFinding(
                    code=TestQualityCode.TEST_MISSING,
                    severity=TestQualitySeverity.ERROR,
                    message=blocker,
                    path=quality.file_signals.path,
                    ledger_id=quality.ledger_id,
                    owner_unit=quality.owner_unit,
                    remediation="Point test_entry.path at a checked-in behavior test.",
                )
            )
    for warning in quality.warnings:
        code = TestQualityCode.ASSERTION_MISSING
        if "import-only" in warning:
            code = TestQualityCode.IMPORT_ONLY_TEST
        elif "mock-heavy" in warning:
            code = TestQualityCode.MOCK_HEAVY_TEST
        elif "fixture-only" in warning:
            code = TestQualityCode.FIXTURE_ONLY_TEST
        findings.append(
            TestQualityFinding(
                code=code,
                severity=TestQualitySeverity.WARNING,
                message=warning,
                path=quality.file_signals.path,
                ledger_id=quality.ledger_id,
                owner_unit=quality.owner_unit,
                remediation="Add non-fixture behavior assertions covering API/CLI/runtime/event failure paths.",
            )
        )
    return findings


def global_test_quality_findings(signals: Iterable[TestFileSignals], *, owner_unit: str) -> list[TestQualityFinding]:
    files = list(signals)
    existing = [item for item in files if item.exists]
    findings: list[TestQualityFinding] = []
    if owner_unit and existing and not any(item.negative_assertions for item in existing):
        findings.append(
            TestQualityFinding(
                code=TestQualityCode.NEGATIVE_PATH_MISSING,
                severity=TestQualitySeverity.WARNING,
                message=f"{owner_unit} tests have no obvious negative-path assertions.",
                owner_unit=owner_unit,
                remediation="Add at least one failing API/CLI/runtime/boundary case for the unit.",
            )
        )
    if owner_unit == "M1-01A":
        coverage_expectations = [
            (TestQualityCode.API_BEHAVIOR_COVERED, "API behavior", any(item.api_calls for item in existing)),
            (TestQualityCode.CLI_BEHAVIOR_COVERED, "CLI behavior", any(item.subprocess_calls for item in existing)),
            (TestQualityCode.EVENT_BEHAVIOR_COVERED, "event payload behavior", any(item.event_assertions for item in existing)),
            (TestQualityCode.BOUNDARY_BEHAVIOR_COVERED, "boundary behavior", any(item.boundary_assertions for item in existing)),
            (TestQualityCode.LINE_BUCKET_BEHAVIOR_COVERED, "line bucket behavior", any(item.line_bucket_assertions for item in existing)),
            (TestQualityCode.DISCONNECT_BEHAVIOR_COVERED, "disconnect behavior", any(item.disconnect_assertions for item in existing)),
        ]
        for code, label, present in coverage_expectations:
            findings.append(
                TestQualityFinding(
                    code=code,
                    severity=TestQualitySeverity.INFO if present else TestQualitySeverity.WARNING,
                    message=f"{label} {'is covered' if present else 'is not clearly covered'} for {owner_unit}.",
                    owner_unit=owner_unit,
                    remediation="Add explicit behavior tests if this warning remains.",
                )
            )
    return findings


def test_quality_payload(report: TestQualityReport) -> dict[str, Any]:
    payload = report.to_dict()
    payload["blocking_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.severity in {TestQualitySeverity.ERROR, TestQualitySeverity.BLOCKER}
    ]
    payload["warning_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.severity == TestQualitySeverity.WARNING
    ]
    return payload


def assert_test_quality(report: TestQualityReport) -> None:
    if report.ok:
        return
    formatted = "\n".join(
        f"{finding.severity} {finding.code} {finding.path}: {finding.message}"
        for finding in report.findings
        if finding.severity in {TestQualitySeverity.ERROR, TestQualitySeverity.BLOCKER}
    )
    raise AssertionError(f"Ledger test quality failed:\n{formatted}")


def _count_tokens(text: str, tokens: Iterable[str]) -> int:
    lowered = text.lower()
    return sum(lowered.count(token.lower()) for token in tokens)


def _base_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_base_name(node.value)}.{node.attr}"
    return ""


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return f"{_base_name(node.func.value)}.{node.func.attr}"
    return ""


def _count_entry_quality(entries: Iterable[TestEntryQuality], field_name: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        key = str(getattr(entry, field_name) or "unassigned")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _finding_code_counts(findings: Iterable[TestQualityFinding]) -> dict[str, int]:
    counts = Counter(str(finding.code) for finding in findings)
    return dict(sorted(counts.items()))


def indexed_test_signals(project_root: Path, roots: Iterable[str] = ("tests",)) -> dict[str, TestFileSignals]:
    index: dict[str, TestFileSignals] = {}
    for root in roots:
        root_path = project_root / root
        if not root_path.exists():
            continue
        for path in root_path.rglob("test_*.py"):
            rel = path.relative_to(project_root).as_posix()
            index[rel] = analyze_test_file(project_root, rel)
    return dict(sorted(index.items()))


def unit_test_quality_matrix(project_root: Path, ledger: InternalizationLedger) -> dict[str, Any]:
    by_unit: dict[str, list[InternalizationLedgerEntry]] = defaultdict(list)
    for entry in ledger.entries():
        by_unit[entry.owner_unit or "unassigned"].append(entry)
    rows: dict[str, Any] = {}
    for owner_unit, entries in sorted(by_unit.items()):
        scoped = InternalizationLedger(entries)
        report = build_test_quality_report(project_root, scoped, owner_unit=owner_unit, include_entries=False)
        rows[owner_unit] = {
            "owner_unit": owner_unit,
            "checked_entries": report.checked_entries,
            "checked_test_entries": report.checked_test_entries,
            "behavior_test_files": report.behavior_test_files,
            "missing_test_files": report.missing_test_files,
            "warning_count": report.warning_count,
            "error_count": report.error_count,
            "ok": report.ok,
        }
    return rows
