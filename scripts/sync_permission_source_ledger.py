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
SLICE_ID = "M1-S03A-02"
EVIDENCE_COMMIT = "TO_BE_REPLACED_AFTER_EVIDENCE_COMMIT"
STAMP = "2026-07-10T23:30:00.000Z"
DOWNSTREAM_UNITS = ["M1-03B", "M1-03C", "M1-03D", "M1-04C", "M1-07C", "M2-04A"]
PERMISSION_EVENTS = [
    "permission_evaluation_started",
    "permission_decision",
    "permission_request_created",
    "permission_request_delivered",
    "permission_request_resolved",
    "permission_request_cancelled",
    "permission_request_aborted",
    "permission_request_expired",
    "permission_execution_grant_issued",
    "permission_execution_grant_consumed",
    "permission_mode_transitioned",
    "permission_state_restored",
    "recovery_input",
]
REFERENCE_TARGET_PATHS = {
    ("claude-code-best", "src/utils/permissions/bashClassifier.ts"): (
        "packages/runtime/zyra_runtime/permission/shell_analysis.py"
    ),
    ("opencode", "packages/opencode/src/permission/arity.ts"): (
        "packages/runtime/zyra_runtime/permission/shell_analysis.py"
    ),
    ("claude-code-best", "src/cli/handlers/autoMode.ts"): (
        "packages/runtime/zyra_runtime/permission/extensions.py"
    ),
    ("claude-code-best", "src/entrypoints/sdk/controlTypes.ts"): (
        "packages/runtime/zyra_runtime/permission/control_plane.py"
    ),
}
SUPPLEMENTAL_SOURCE_GRAPHS = {
    "agent-framework": "source-graphs/agent-framework/source-graph.md",
    "agentscope": "source-graphs/agentscope/source-graph.md",
    "hermes-agent": "source-graphs/hermes-agent/batch-02-tool-registry-approval-tool-search.md",
    "openclaw": "source-graphs/openclaw/source-graph.md",
    "opencode": "source-graphs/opencode/batch-02-tools-permission-mcp-skills-subagent.md",
}


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
        fallback = REFERENCE_TARGET_PATHS.get((decision.repository, decision.source_path))
        if fallback is None:
            raise ValueError(
                "Reference/contract permission decision needs an explicit replacement target: "
                f"{decision.repository}:{decision.source_path}"
            )
        paths.append(fallback)
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
    if decision.disposition in {SourceDisposition.CONTRACT_ONLY, SourceDisposition.REFERENCE_ONLY}:
        # These rows document a source decision and a Zyra replacement, but
        # deliberately claim no migrated runtime ownership.  Keeping them in
        # CANDIDATE/INVENTORIED avoids misclassifying a reference-only row as
        # materialized code or counting its replacement target twice.
        return LedgerLifecycle.CANDIDATE
    return LedgerLifecycle.PRODUCTIZED


def status_for_decision(decision: PermissionSourceDecision) -> MainPathStatus:
    if decision.disposition is SourceDisposition.DEFERRED:
        return MainPathStatus.PLANNED
    if decision.disposition in {SourceDisposition.CONTRACT_ONLY, SourceDisposition.REFERENCE_ONLY}:
        return MainPathStatus.INVENTORIED
    return MainPathStatus.TESTED_MAIN_PATH


def runtime_entry_for_decision(decision: PermissionSourceDecision) -> RuntimeEntry:
    symbol = (
        decision.runtime_entry
        if decision.claims_runtime_ownership and decision.runtime_entry
        else "zyra_runtime.permission.source_audit.source_decision"
    )
    module, separator, function = symbol.rpartition(".")
    if not separator:
        raise ValueError(f"Invalid permission runtime entry symbol: {symbol!r}")
    return RuntimeEntry(
        module=module,
        function=function,
        protocol=(
            "zyra-permission-runtime-v1"
            if decision.claims_runtime_ownership
            else "zyra-permission-source-decision-v1"
        ),
        health_check=(
            "python -m unittest tests.unit.test_permission_runtime_foundation "
            "tests.unit.test_permission_control_plane_integration "
            "tests.unit.test_permission_continuation "
            "tests.unit.test_permission_shell_extensions "
            "tests.integration.test_code_worker_permission_runtime_foundation "
            "tests.integration.test_code_worker_permission_continuation_integration "
            "tests.integration.test_browser_worker_permission_gate "
            "tests.integration.test_api_control_commands"
        ),
        config_refs=[
            "packages/runtime/zyra_runtime/permission/store.py",
            "packages/runtime/zyra_runtime/permission/control_plane.py",
            "packages/runtime/zyra_runtime/permission/extensions.py",
        ],
    )


def main_path_for_decision(decision: PermissionSourceDecision) -> MainPathBinding:
    if not decision.claims_runtime_ownership:
        return MainPathBinding(
            surfaces=["permission_source_decision"],
            worker_runtime=(
                "Inventory-only source decision; runtime behavior is owned by the explicit "
                f"replacement {decision.replacement or ', '.join(decision.target_symbols)}."
            ),
        )
    symbols = " ".join((*decision.target_symbols, decision.runtime_entry)).casefold()
    surfaces = ["permission_runtime"]
    routes: list[str] = []
    if any(marker in symbols for marker in ("control_plane", "permission.api", "transport")):
        surfaces.append("permission_control")
        routes.extend(
            [
                "GET /permissions/requests",
                "POST /permissions/requests/{request_id}/resolve",
            ]
        )
    if any(marker in symbols for marker in ("tool_execution", "query_engine", "permission.runtime")):
        surfaces.extend(["tool_execution", "code_worker"])
        routes.extend(
            [
                "POST /tasks/{task_id}/tools",
                "POST /tasks/{task_id}/workers/code",
            ]
        )
    if decision.test_target == "tests/integration/test_browser_worker_permission_gate.py":
        surfaces.extend(["browser_action", "browser_worker"])
        routes.append("POST /tasks/{task_id}/workers/browser")
    if any(marker in symbols for marker in ("event", "control_plane", "permission.runtime")):
        surfaces.append("event_log")
    return MainPathBinding(
        surfaces=list(dict.fromkeys(surfaces)),
        event_types=list(PERMISSION_EVENTS),
        api_routes=list(dict.fromkeys(routes)),
        worker_runtime=(
            f"{decision.runtime_entry} -> PermissionStateStore -> exact permission/continuation boundary"
        ),
    )


def source_graph_ref_for_decision(decision: PermissionSourceDecision) -> str:
    if decision.repository != "claude-code-best":
        return SUPPLEMENTAL_SOURCE_GRAPHS[decision.repository]
    integration_prefixes = (
        "src/cli/",
        "src/commands",
        "src/entrypoints/",
        "src/main",
        "src/types/command",
        "src/utils/QueryGuard",
        "src/hooks/useCommandQueue",
        "src/utils/messageQueue",
        "src/utils/queueProcessor",
    )
    if decision.source_path.startswith(integration_prefixes):
        return "source-graphs/claude-code-best/batch-09-tui-cli-commands-control.md"
    return "source-graphs/claude-code-best/batch-03-permission-runtime-hooks.md"


def test_entry_for_decision(decision: PermissionSourceDecision) -> TestEntry:
    path = decision.test_target or "tests/unit/test_permission_runtime_foundation.py"
    module = path.removesuffix(".py").replace("/", ".")
    kind = "integration" if "/integration/" in f"/{path}" else "unit"
    expected = (
        "real API/CodeWorker/BrowserWorker execution is guarded before side effects"
        if kind == "integration"
        else "permission control, continuation, transport, shell and deterministic policy behavior are verified"
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
        "CodeWorkerRuntime or BrowserWorkerRuntime; the source repository is not required at runtime. "
        "Structured API/CLI/bridge delivery, exact resolve and continuation resume are Zyra-owned."
    )


def build_entry(decision: PermissionSourceDecision) -> InternalizationLedgerEntry:
    targets = target_paths_for_decision(decision)
    primary, *supporting = targets
    target_role = "primary" if decision.authority.value == "primary" else "supporting"
    runtime_owner = decision.claims_runtime_ownership
    target_bindings = [
        TargetBinding(
            target_path=primary,
            role=target_role,
            required_for_main_path=runtime_owner,
        )
    ]
    target_bindings.extend(
        TargetBinding(
            target_path=path,
            role="supporting",
            required_for_main_path=runtime_owner,
        )
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
    capability_name = f"permission-runtime:{decision.source_path}"
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
        runtime_entry=runtime_entry_for_decision(decision),
        test_entries=[test_entry_for_decision(decision)],
        main_path=main_path_for_decision(decision),
        line_count_policy=(
            LineCountPolicy.EXCLUDED_INVENTORY_ONLY
            if decision.disposition
            in {
                SourceDisposition.DEFERRED,
                SourceDisposition.CONTRACT_ONLY,
                SourceDisposition.REFERENCE_ONLY,
            }
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
                reason="M1-S03A-02 permission integration source-to-target decision",
                symbols=[],
                tags=["permission", decision.authority.value, decision.disposition.value],
            )
        ],
        tags=[
            "m1-03a",
            "slice-03a-02",
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
            "internalization_strategy": "zyra_owned_permission_control_and_execution_state_machine",
            "mechanisms": list(decision.mechanisms),
            "next_owner": decision.next_owner or "",
            "source_authority": decision.authority.value,
            "source_disposition": decision.disposition.value,
            "source_graph_ref": source_graph_ref_for_decision(decision),
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
        description="Synchronize M1-S03A-02 source decisions into the bundled internalization ledger."
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
