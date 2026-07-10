from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for package_path in (
    PROJECT_ROOT / "packages" / "core",
    PROJECT_ROOT / "packages" / "integrations",
    PROJECT_ROOT / "packages" / "runtime",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


from zyra_integrations import (  # noqa: E402
    InternalizationLedger,
    InternalizationLedgerEntry,
    LedgerLifecycle,
    LicenseNotice,
    LineCountPolicy,
    MainPathBinding,
    MainPathStatus,
    MigrationStrategy,
    NoticeStatus,
    RuntimeEntry,
    SourceEvidence,
    TargetBinding,
    TestEntry,
)
from zyra_integrations.ledger_models import to_jsonable  # noqa: E402
from zyra_runtime.permission.source_audit import (  # noqa: E402
    PERMISSION_SOURCE_DECISIONS,
    PermissionSourceDecision,
    SourceDisposition,
)


DEFAULT_LEDGER_PATH = (
    PROJECT_ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
OWNER_UNIT = "M1-03A"
SLICE_ID = "M1-S03A-01"
EVIDENCE_COMMIT = "46790d8"
STAMP = "2026-07-10T16:30:00.000Z"
DOWNSTREAM_UNITS = ["M1-03B", "M1-03C", "M1-03D", "M1-04C", "M1-07C", "M2-04A"]
PERMISSION_EVENTS = [
    "permission_evaluation_started",
    "permission_decision",
    "permission_request_created",
    "permission_request_resolved",
    "permission_execution_grant_issued",
    "permission_execution_grant_consumed",
    "recovery_input",
]


def target_path_for_symbol(symbol: str) -> str:
    module_name, separator, _ = symbol.rpartition(".")
    if not separator or not module_name.startswith("zyra_runtime"):
        raise ValueError(f"Unsupported permission target symbol: {symbol!r}")
    return f"packages/runtime/{module_name.replace('.', '/')}.py"


def target_paths_for_decision(decision: PermissionSourceDecision) -> list[str]:
    paths: list[str] = []
    for symbol in decision.target_symbols:
        path = target_path_for_symbol(symbol)
        if path not in paths:
            paths.append(path)
    if not paths:
        paths.append("packages/runtime/zyra_runtime/permission/risk.py")
    return paths


def strategy_for_decision(decision: PermissionSourceDecision) -> MigrationStrategy:
    if decision.disposition is SourceDisposition.ADAPTER:
        return MigrationStrategy.ADAPTER
    if decision.disposition is SourceDisposition.DEFERRED:
        return MigrationStrategy.CANDIDATE_REVIEW
    return MigrationStrategy.REIMPLEMENTED_PATTERN


def lifecycle_for_decision(decision: PermissionSourceDecision) -> LedgerLifecycle:
    if decision.disposition is SourceDisposition.DEFERRED:
        return LedgerLifecycle.DEFERRED
    return LedgerLifecycle.PRODUCTIZED


def status_for_decision(decision: PermissionSourceDecision) -> MainPathStatus:
    if decision.disposition is SourceDisposition.DEFERRED:
        return MainPathStatus.PLANNED
    return MainPathStatus.TESTED_MAIN_PATH


def test_entry_for_decision(decision: PermissionSourceDecision) -> TestEntry:
    path = decision.test_target or "tests/unit/test_permission_runtime_foundation.py"
    module = path.removesuffix(".py").replace("/", ".")
    kind = "integration" if "/integration/" in f"/{path}" else "unit"
    expected = (
        "real CodeWorker tool execution is guarded before side effects"
        if kind == "integration"
        else "permission source disposition and deterministic policy behavior are verified"
    )
    return TestEntry(
        path=path,
        command=f"python -m unittest {module}",
        kind=kind,
        expected_signal=expected,
        required=True,
    )


def replacement_plan_for_decision(decision: PermissionSourceDecision) -> str:
    if decision.disposition is SourceDisposition.DEFERRED:
        return (
            f"{decision.rationale} Current deterministic replacement: {decision.replacement}. "
            f"Deferred enrichment owner: {decision.next_owner}."
        ).strip()
    if decision.disposition in {SourceDisposition.CONTRACT_ONLY, SourceDisposition.REFERENCE_ONLY}:
        return (
            f"The source remains {decision.disposition.value}; the runtime behavior is replaced by "
            f"{decision.replacement or ', '.join(decision.target_symbols)} and is not a runtime dependency."
        )
    return (
        "The selected mechanism is internalized in the listed Zyra-owned modules and runs through "
        "CodeWorkerRuntime; the source repository is not required at runtime. Structured remote/API "
        "approval delivery and resume remain owned by M1-S03A-02."
    )


def build_entry(decision: PermissionSourceDecision) -> InternalizationLedgerEntry:
    targets = target_paths_for_decision(decision)
    primary, *supporting = targets
    target_role = "primary" if decision.authority.value == "primary" else "supporting"
    target_bindings = [TargetBinding(target_path=primary, role=target_role, required_for_main_path=True)]
    target_bindings.extend(
        TargetBinding(target_path=path, role="supporting", required_for_main_path=True)
        for path in supporting
    )
    target_bindings.append(
        TargetBinding(
            target_path="packages/runtime/zyra_runtime/permission/source_audit.py",
            role="source-audit",
            required_for_main_path=False,
        )
    )
    mechanisms = ", ".join(decision.mechanisms)
    capability_name = f"permission-foundation:{decision.source_path}"
    entry = InternalizationLedgerEntry(
        ledger_id=InternalizationLedgerEntry.new(
            source_repo=decision.repository,
            source_path=decision.source_path,
            capability_name=capability_name,
            capability_summary=mechanisms or "Permission runtime source decision.",
            target_paths=[primary],
        ).ledger_id,
        source_repo=decision.repository,
        source_path=decision.source_path,
        capability_name=capability_name,
        capability_summary=mechanisms or "Permission runtime source decision.",
        target_bindings=target_bindings,
        migration_strategy=strategy_for_decision(decision),
        main_path_status=status_for_decision(decision),
        lifecycle=lifecycle_for_decision(decision),
        runtime_entry=RuntimeEntry(
            module="zyra_runtime.permission.runtime",
            function="ToolPermissionRuntime",
            protocol="zyra-permission-runtime-v1",
            health_check=(
                "python -m unittest tests.unit.test_permission_runtime_foundation "
                "tests.integration.test_code_worker_permission_runtime_foundation"
            ),
            config_refs=["packages/runtime/zyra_runtime/permission/store.py"],
        ),
        test_entries=[test_entry_for_decision(decision)],
        main_path=MainPathBinding(
            surfaces=["permission_runtime", "tool_execution", "event_log", "code_worker"],
            event_types=list(PERMISSION_EVENTS),
            api_routes=[
                "POST /tasks/{task_id}/tools",
                "POST /tasks/{task_id}/workers/code",
            ],
            worker_runtime=(
                "CodeWorkerRuntime -> ZyraClaudeQueryEngine -> ToolExecutionRuntime -> ToolPermissionRuntime"
            ),
        ),
        line_count_policy=(
            LineCountPolicy.EXCLUDED_INVENTORY_ONLY
            if decision.disposition is SourceDisposition.DEFERRED
            else LineCountPolicy.COUNTS_WHEN_PRODUCTIZED
        ),
        license_notice=LicenseNotice(
            source_repo=decision.repository,
            status=NoticeStatus.RECORDED,
            license_hint="Source mechanism recorded for Zyra-owned permission runtime internalization.",
            notice_path="third_party/NOTICE.md",
            notes="No runtime dependency on the parent source repository.",
        ),
        owner_unit=OWNER_UNIT,
        milestone="M1",
        downstream_units=list(DOWNSTREAM_UNITS),
        dependencies=[],
        source_evidence=[
            SourceEvidence(
                source_repo=decision.repository,
                source_path=decision.source_path,
                exists_in_workspace=True,
                source_kind="file",
                reason="M1-S03A-01 permission source-to-target decision",
                symbols=[],
                tags=["permission", decision.authority.value, decision.disposition.value],
            )
        ],
        tags=[
            "m1-03a",
            "slice-03a-01",
            "permission-runtime",
            decision.authority.value,
            decision.disposition.value,
        ],
        blockers=[],
        risk_notes=list(decision.limitations),
        replacement_plan=replacement_plan_for_decision(decision),
        created_at=STAMP,
        updated_at=STAMP,
        metadata={
            "evidence_commit": EVIDENCE_COMMIT,
            "internalization_strategy": "zyra_owned_permission_state_machine",
            "mechanisms": list(decision.mechanisms),
            "next_owner": decision.next_owner or "",
            "source_authority": decision.authority.value,
            "source_disposition": decision.disposition.value,
            "source_graph_ref": "source-graphs/claude-code-best/batch-03-permission-runtime-hooks.md",
            "target_symbols": list(decision.target_symbols),
            "slice_id": SLICE_ID,
        },
    )
    errors = entry.validate()
    if errors:
        raise ValueError(f"Invalid permission ledger entry {entry.ledger_id}: {errors}")
    return entry


def expected_entries() -> list[InternalizationLedgerEntry]:
    entries = [build_entry(decision) for decision in PERMISSION_SOURCE_DECISIONS]
    keys = {(entry.source_repo, entry.source_path) for entry in entries}
    if len(keys) != len(entries):
        raise ValueError("Permission source decisions contain duplicate repository/path identities")
    return entries


def rewrite_payload(payload: dict[str, object]) -> dict[str, object]:
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("Ledger payload has no entries list")
    replacements = [entry.to_dict() for entry in expected_entries()]
    output: list[dict[str, object]] = []
    inserted = False
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ValueError("Ledger entry is not an object")
        if raw.get("owner_unit") == OWNER_UNIT:
            if not inserted:
                output.extend(replacements)
                inserted = True
            continue
        output.append(raw)
    if not inserted:
        output.extend(replacements)
    typed = [InternalizationLedgerEntry.from_dict(item) for item in output]
    result = dict(payload)
    result["entries"] = output
    result["summary"] = to_jsonable(InternalizationLedger(typed).summary())
    return result


def canonical_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def synchronize(path: Path, *, write: bool) -> tuple[bool, int]:
    original = json.loads(path.read_text(encoding="utf-8"))
    expected = rewrite_payload(original)
    before = canonical_json(original)
    after = canonical_json(expected)
    aligned = before == after
    if write and not aligned:
        path.write_text(after, encoding="utf-8", newline="\n")
        aligned = True
    return aligned, len(expected_entries())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize M1-S03A-01 source decisions into the bundled internalization ledger."
    )
    parser.add_argument("--ledger-path", type=Path, default=DEFAULT_LEDGER_PATH)
    parser.add_argument("--write", action="store_true", help="Write the deterministic ledger projection.")
    args = parser.parse_args(argv)
    aligned, count = synchronize(args.ledger_path.resolve(), write=args.write)
    print(f"permission_source_ledger_aligned={str(aligned).lower()}")
    print(f"permission_source_decision_count={count}")
    print(f"ledger_path={args.ledger_path.resolve()}")
    return 0 if aligned else 1


if __name__ == "__main__":
    raise SystemExit(main())
