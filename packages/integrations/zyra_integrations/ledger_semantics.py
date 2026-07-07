from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from .ledger_boundary import build_clean_boundary_report
from .ledger_line_buckets import bucket_line_count_report
from .ledger_linecount import EffectiveLineCountReport
from .ledger_models import (
    InternalizationLedgerEntry,
    LedgerLifecycle,
    LicenseNotice,
    MainPathBinding,
    MainPathStatus,
    MigrationStrategy,
    NoticeStatus,
    RuntimeEntry,
    TargetBinding,
    TestEntry,
    to_jsonable,
)
from .ledger_persistence import validate_entry_for_persistence
from .ledger_reachability import build_reachability_report
from .ledger_store import InternalizationLedger


class SemanticProbeStatus(StrEnum):
    PASSING = "passing"
    FAILING = "failing"
    ERROR = "error"
    SKIPPED = "skipped"


class SemanticProbeKind(StrEnum):
    PERSISTENCE = "persistence"
    REACHABILITY = "reachability"
    LINE_BUCKET = "line_bucket"
    BOUNDARY = "boundary"
    ACCEPTANCE = "acceptance"


@dataclass(slots=True)
class SemanticProbeResult:
    name: str
    kind: SemanticProbeKind
    status: SemanticProbeStatus
    proves_effect: bool
    enabled_signal: str
    disabled_signal: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == SemanticProbeStatus.PASSING and self.proves_effect

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["ok"] = self.ok
        return payload


@dataclass(slots=True)
class SemanticProbeDefinition:
    name: str
    kind: SemanticProbeKind
    enabled_signal: str
    disabled_signal: str
    runner: Callable[[Path], SemanticProbeResult]
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": str(self.kind),
            "enabled_signal": self.enabled_signal,
            "disabled_signal": self.disabled_signal,
            "description": self.description,
        }


@dataclass(slots=True)
class SemanticEffectReport:
    ok: bool
    project_root: str
    total_probes: int
    passing_probes: int
    failing_probes: int
    error_probes: int
    skipped_probes: int
    results: list[SemanticProbeResult]
    definitions: list[dict[str, Any]]
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


def default_semantic_probes() -> list[SemanticProbeDefinition]:
    return [
        SemanticProbeDefinition(
            name="invalid_upsert_is_blocked",
            kind=SemanticProbeKind.PERSISTENCE,
            enabled_signal="validate_entry_for_persistence rejects schema/policy-invalid entry",
            disabled_signal="bad entry would be appended to ledger",
            runner=_probe_invalid_upsert,
            description="Proves persistence validator changes write behavior.",
        ),
        SemanticProbeDefinition(
            name="missing_route_breaks_reachability",
            kind=SemanticProbeKind.REACHABILITY,
            enabled_signal="connected entry with missing API route produces reachability error",
            disabled_signal="main_path_status string would pass without route resolution",
            runner=_probe_missing_route,
            description="Proves route discovery affects connected-entry audit behavior.",
        ),
        SemanticProbeDefinition(
            name="vendor_lines_do_not_satisfy_bucket_gate",
            kind=SemanticProbeKind.LINE_BUCKET,
            enabled_signal="vendor-like lines are separated from production/test/script lines",
            disabled_signal="vendor-runtimes physical lines would satisfy effective line count",
            runner=_probe_vendor_bucket,
            description="Proves bucket gate prevents source-pool line count inflation.",
        ),
        SemanticProbeDefinition(
            name="parent_source_dependency_blocks_boundary",
            kind=SemanticProbeKind.BOUNDARY,
            enabled_signal="subprocess reference to parent source repo creates boundary blocker",
            disabled_signal="clean boundary scan would ignore runtime source dependency",
            runner=_probe_parent_boundary,
            description="Proves boundary scan affects clean submission result.",
        ),
    ]


def build_semantic_effect_report(project_root: Path) -> SemanticEffectReport:
    definitions = default_semantic_probes()
    results: list[SemanticProbeResult] = []
    for definition in definitions:
        try:
            results.append(definition.runner(project_root))
        except Exception as error:
            results.append(
                SemanticProbeResult(
                    name=definition.name,
                    kind=definition.kind,
                    status=SemanticProbeStatus.ERROR,
                    proves_effect=False,
                    enabled_signal=definition.enabled_signal,
                    disabled_signal=definition.disabled_signal,
                    message="Probe raised an exception.",
                    error=str(error),
                )
            )
    passing = sum(1 for result in results if result.status == SemanticProbeStatus.PASSING)
    failing = sum(1 for result in results if result.status == SemanticProbeStatus.FAILING)
    errored = sum(1 for result in results if result.status == SemanticProbeStatus.ERROR)
    skipped = sum(1 for result in results if result.status == SemanticProbeStatus.SKIPPED)
    return SemanticEffectReport(
        ok=all(result.ok for result in results),
        project_root=str(project_root),
        total_probes=len(results),
        passing_probes=passing,
        failing_probes=failing,
        error_probes=errored,
        skipped_probes=skipped,
        results=results,
        definitions=[definition.to_dict() for definition in definitions],
        summary={
            "by_kind": _result_counts_by_kind(results),
            "failing": [result.name for result in results if not result.ok],
        },
    )


def semantic_payload(report: SemanticEffectReport) -> dict[str, Any]:
    payload = report.to_dict()
    payload["blocking_results"] = [
        result.to_dict()
        for result in report.results
        if not result.ok
    ]
    return payload


def assert_semantic_effects(report: SemanticEffectReport) -> None:
    if report.ok:
        return
    formatted = "\n".join(
        f"{result.status} {result.name}: {result.message} {result.error}"
        for result in report.results
        if not result.ok
    )
    raise AssertionError(f"Semantic effect probes failed:\n{formatted}")


def _probe_invalid_upsert(project_root: Path) -> SemanticProbeResult:
    ledger = InternalizationLedger([_valid_entry()])
    bad = InternalizationLedgerEntry(
        ledger_id="bad",
        source_repo="claude-code-best",
        source_path="src/QueryEngine.ts",
        capability_name="bad",
        capability_summary="missing target and tests",
        target_bindings=[],
        migration_strategy=MigrationStrategy.ADAPTER,
        main_path_status=MainPathStatus.API_CONNECTED,
        lifecycle=LedgerLifecycle.ACTIVE,
        runtime_entry=RuntimeEntry(module="zyra_integrations.ledger_models", function="InternalizationLedgerEntry"),
        license_notice=LicenseNotice(source_repo="claude-code-best", status=NoticeStatus.RECORDED),
    )
    validation = validate_entry_for_persistence(project_root, ledger, bad, strict=False)
    return SemanticProbeResult(
        name="invalid_upsert_is_blocked",
        kind=SemanticProbeKind.PERSISTENCE,
        status=SemanticProbeStatus.PASSING if not validation.ok and ledger.get("bad") is None else SemanticProbeStatus.FAILING,
        proves_effect=not validation.ok and ledger.get("bad") is None,
        enabled_signal="validation rejected bad entry",
        disabled_signal="ledger contains bad entry",
        message="Persistence validator rejects invalid entry before upsert.",
        evidence=validation.to_dict(),
    )


def _probe_missing_route(project_root: Path) -> SemanticProbeResult:
    entry = _valid_entry()
    entry.main_path_status = MainPathStatus.API_CONNECTED
    entry.main_path = MainPathBinding(api_routes=["GET /ledger/missing-semantic-probe-route"], event_types=["system_notice"])
    ledger = InternalizationLedger([entry])
    report = build_reachability_report(project_root, ledger, owner_unit="M1-01A", include_entries=True, strict_audit=False)
    missing_route = any("route" in finding.message.lower() for finding in report.findings)
    return SemanticProbeResult(
        name="missing_route_breaks_reachability",
        kind=SemanticProbeKind.REACHABILITY,
        status=SemanticProbeStatus.PASSING if missing_route and not report.ok else SemanticProbeStatus.FAILING,
        proves_effect=missing_route and not report.ok,
        enabled_signal="missing route finding exists",
        disabled_signal="report ok despite missing route",
        message="Reachability report rejects connected entry with missing API route.",
        evidence=report.to_dict(),
    )


def _probe_vendor_bucket(project_root: Path) -> SemanticProbeResult:
    from .ledger_linecount import EffectiveLineCountReport, parse_numstat

    files = parse_numstat(
        "12000\t0\tvendor-runtimes/claude-code-runtime/productized/src/QueryEngine.ts\n",
        project_root=project_root,
    )
    report = EffectiveLineCountReport(
        base="base",
        head="HEAD",
        cached=False,
        counted_paths=["vendor-runtimes"],
        raw_added=12_000,
        raw_deleted=0,
        effective_added=sum(file.effective_added for file in files),
        effective_deleted=0,
        excluded_added=sum(file.excluded_added for file in files),
        review_added=sum(file.added for file in files if str(file.effective_verdict) == "review"),
        files=files,
        excluded_files=[file for file in files if file.excluded_added],
        review_files=[file for file in files if str(file.effective_verdict) == "review"],
        effective_files=[file for file in files if file.effective_added],
        minimum_effective_lines=10_000,
    )
    bucketed = bucket_line_count_report(project_root, report)
    proves = not bucketed.ok and bucketed.vendor_like_added == 12_000 and bucketed.bucket_effective_added == 0
    return SemanticProbeResult(
        name="vendor_lines_do_not_satisfy_bucket_gate",
        kind=SemanticProbeKind.LINE_BUCKET,
        status=SemanticProbeStatus.PASSING if proves else SemanticProbeStatus.FAILING,
        proves_effect=proves,
        enabled_signal="vendor_like lines do not count",
        disabled_signal="vendor_like lines satisfy minimum",
        message="Bucket gate excludes vendor-like runtime source pool from strict effective lines.",
        evidence=bucketed.to_dict(),
    )


def _probe_parent_boundary(project_root: Path) -> SemanticProbeResult:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        target = root / "packages/runtime/bad_runtime.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        bad_ref = "../" + "claude-code-best" + "/bin/run"
        target.write_text(f"import subprocess\nsubprocess.run('{bad_ref}')\n", encoding="utf-8")
        report = build_clean_boundary_report(root, include_tests=False, include_cache=False, scan_roots=["packages"])
    proves = not report.ok and report.blocker_count > 0
    return SemanticProbeResult(
        name="parent_source_dependency_blocks_boundary",
        kind=SemanticProbeKind.BOUNDARY,
        status=SemanticProbeStatus.PASSING if proves else SemanticProbeStatus.FAILING,
        proves_effect=proves,
        enabled_signal="boundary blocker exists",
        disabled_signal="report ok with parent source subprocess",
        message="Boundary report blocks runtime subprocess dependency on parent source repository.",
        evidence=report.to_dict(),
    )


def _valid_entry() -> InternalizationLedgerEntry:
    return InternalizationLedgerEntry.new(
        source_repo="claude-code-best",
        source_path="src/QueryEngine.ts",
        capability_name="semantic valid entry",
        capability_summary="valid entry for semantic probes",
        target_paths=["packages/integrations/zyra_integrations/ledger_models.py"],
        migration_strategy=MigrationStrategy.ADAPTER,
        main_path_status=MainPathStatus.API_CONNECTED,
        lifecycle=LedgerLifecycle.ACTIVE,
        owner_unit="M1-01A",
        milestone="M1",
        runtime_entry=RuntimeEntry(module="zyra_integrations.ledger_models", function="InternalizationLedgerEntry"),
        test_entries=[TestEntry(path="tests/unit/test_internalization_ledger.py", command="python -m unittest")],
        main_path=MainPathBinding(api_routes=["GET /ledger"], event_types=["system_notice"]),
        license_notice=LicenseNotice(source_repo="claude-code-best", status=NoticeStatus.RECORDED),
    )


def _result_counts_by_kind(results: list[SemanticProbeResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        key = str(result.kind)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))
