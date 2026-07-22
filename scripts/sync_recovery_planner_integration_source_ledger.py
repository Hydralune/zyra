from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for package_path in (ROOT / "packages" / "core", ROOT / "packages" / "integrations"):
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


OWNER_UNIT = "M1-S07C-02"
STAMP = "2026-07-22T14:30:00.000Z"
OMP_COMMIT = "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca"
DEFAULT_LEDGER = ROOT / "packages" / "integrations" / "zyra_integrations" / "data" / "internalization_ledger_seed.json"


def _omp_integration_entry() -> InternalizationLedgerEntry:
    source_path = (
        "packages/ai/src/auth-retry.ts;"
        "packages/coding-agent/src/session/streaming.ts;"
        "packages/coding-agent/src/mcp/client.ts;"
        "packages/coding-agent/src/task/executor.ts;"
        "packages/coding-agent/src/task/worktree.ts"
    )
    targets = [
        "packages/runtime/claude-runtime/src/recovery/omp-integration-runtime.ts",
        "packages/runtime/claude-runtime/src/recovery/omp-recovery-runtime.ts",
        "packages/scheduler/zyra_scheduler/recovery_runtime/ingress_runtime.py",
        "packages/scheduler/zyra_scheduler/recovery_runtime/exact_recovery_runtime.py",
        "packages/scheduler/zyra_scheduler/recovery_runtime/integration_runtime.py",
    ]
    entry = InternalizationLedgerEntry.new(
        source_repo="oh-my-pi",
        source_path=source_path,
        capability_name="omp_provider_stream_mcp_worktree_background_recovery_receipts",
        capability_summary=(
            "Cropped TypeScript mechanisms normalize provider credential rotation, partial-stream replay fences, "
            "MCP breaker state, dirty/conflicted worktree preservation and durable background-task restart evidence. "
            "The Python recovery ingress consumes those supplementary receipts while retaining policy and state custody."
        ),
        target_paths=targets,
    )
    entry.target_bindings = [
        TargetBinding(target_path=path, role="primary" if index == 0 else "supporting", required_for_main_path=True)
        for index, path in enumerate(targets)
    ]
    entry.migration_strategy = MigrationStrategy.DIRECT_PORT
    entry.main_path_status = MainPathStatus.TESTED_MAIN_PATH
    entry.lifecycle = LedgerLifecycle.PRODUCTIZED
    entry.owner_unit = OWNER_UNIT
    entry.milestone = "M1"
    entry.downstream_units = ["M1-08", "M2-recovery-console", "M3-recovery-hardening"]
    entry.dependencies = ["M1-S07C-01", "M1-S05D-02", "M1-S07A-02", "M1-S07B-02"]
    entry.source_evidence = [SourceEvidence(
        source_repo="oh-my-pi",
        source_path=source_path,
        exists_in_workspace=True,
        source_kind="bounded_module_group",
        reason="Pinned OMP revision and selected provider/stream/MCP/task/worktree mechanisms verified before M1-S07C-02 production changes.",
        symbols=[
            "provider auth retry and credential rotation",
            "partial stream replay fence",
            "MCP reconnect breaker",
            "dirty worktree preservation",
            "background task generation restart",
        ],
        tags=["supplementary_implementation", OWNER_UNIT],
    )]
    entry.main_path = MainPathBinding(
        surfaces=[
            "claude_runtime_watchdog_recovery_receipt",
            "python_typed_recovery_ingress",
            "task_recovery_observation_api",
        ],
        event_types=[
            "recovery_observation_admitted",
            "recovery_integration_applied",
            "recovery_feedback_influence_proven",
        ],
        api_routes=[
            "POST /tasks/{task_id}/recovery/observations",
            "POST /recovery/plans/{plan_id}/restart",
            "GET /tasks/{task_id}/recovery",
        ],
        control_commands=[],
        artifact_kinds=["checkpoint_receipt", "side_effect_fence", "execution_receipt"],
        worker_runtime=(
            "RuntimeWatchdogObserver -> OmpRecoveryReceiptRuntime/OmpRecoveryIntegrationRuntime -> "
            "RecoveryIngressRuntime -> RecoveryDecisionRuntime"
        ),
        ui_panels=[],
    )
    entry.line_count_policy = LineCountPolicy.COUNTS_AS_RUNTIME
    entry.license_notice = LicenseNotice(
        source_repo="oh-my-pi",
        status=NoticeStatus.RECORDED,
        license_hint="Cropped TypeScript recovery mechanisms retained with provenance.",
        notice_path="packages/scheduler/THIRD_PARTY_NOTICES.md",
        notes="No runtime dependency on the parent OMP repository, CLI, RPC process, or mutable stores.",
    )
    entry.runtime_entry = RuntimeEntry(
        module="@zyra/claude-runtime/recovery",
        function="OmpRecoveryIntegrationRuntime",
        protocol="zyra.omp-recovery-integration-evidence/v1",
        health_check=(
            "node --experimental-strip-types --test "
            "packages/runtime/claude-runtime/test/recovery-integration.test.ts"
        ),
        config_refs=targets,
    )
    entry.test_entries = [
        TestEntry(
            path="packages/runtime/claude-runtime/test/recovery-integration.test.ts",
            command=(
                "node --experimental-strip-types --test "
                "packages/runtime/claude-runtime/test/recovery-integration.test.ts"
            ),
            kind="original_language_behavior",
            expected_signal=(
                "credential rotation, partial-stream de-duplication, MCP breaker, dirty WIP preservation and "
                "background-task restart produce deterministic supplementary receipts"
            ),
            required=True,
        ),
        TestEntry(
            path="tests/integration/test_recovery_planner_routing_memory_api.py",
            command="python -m pytest -q tests/integration/test_recovery_planner_routing_memory_api.py",
            kind="integration_main_path",
            expected_signal=(
                "typed owner observation reaches canonical policy/action owners, changes continuation, writes memory "
                "only after proof and survives API runtime restart"
            ),
            required=True,
        ),
    ]
    entry.replacement_plan = (
        f"source_role=supplementary_implementation; source_commit={OMP_COMMIT}; cropped same-language migration "
        "keeps bounded receipt and replay mechanisms while excluding OMP policy, provider, MCP, task and worktree stores."
    )
    entry.metadata = {
        "owner_unit": OWNER_UNIT,
        "source_role": "supplementary_implementation",
        "source_commit": OMP_COMMIT,
        "source_language": "typescript",
        "target_language": "typescript",
        "migration_mode": "cropped_same_language_recovery_integration_receipts",
        "canonical_policy_owner": "python.RecoveryDecisionRuntime",
        "canonical_checkpoint_owner": "python.RecoveryPlanStore",
        "canonical_route_owners": "GraphStateCustody/WorkerPool/BackendRegistry/ProviderControlPlane",
        "route_mutation_applied_by_typescript": False,
        "omp_runtime_dependency": False,
        "root_source_runtime_dependency": False,
        "openclaw_forward_excluded": True,
    }
    entry.created_at = STAMP
    entry.updated_at = STAMP
    return entry


def expected_entries() -> list[InternalizationLedgerEntry]:
    entries = [_omp_integration_entry()]
    for entry in entries:
        errors = entry.validate()
        if errors:
            raise ValueError(f"invalid recovery integration ledger entry {entry.ledger_id}: {errors}")
    return entries


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def synchronize(path: Path, *, write: bool) -> tuple[bool, int]:
    document = json.loads(path.read_text(encoding="utf-8"))
    entries = document if isinstance(document, list) else document.get("entries")
    if not isinstance(entries, list):
        raise ValueError("internalization ledger seed must be a list or contain entries")
    replacements = {item.ledger_id: item.to_dict() for item in expected_entries()}
    output: list[dict[str, object]] = []
    for raw in entries:
        if not isinstance(raw, dict):
            raise ValueError("ledger entry is not an object")
        ledger_id = str(raw.get("ledger_id") or "")
        if str(raw.get("owner_unit") or "") == OWNER_UNIT and ledger_id not in replacements:
            continue
        output.append(replacements.pop(ledger_id, raw))
    output.extend(replacements.values())
    typed = [InternalizationLedgerEntry.from_dict(item) for item in output]
    expected = output if isinstance(document, list) else {
        **document,
        "entries": output,
        "summary": to_jsonable(InternalizationLedger(typed).summary()),
    }
    aligned = _canonical(document) == _canonical(expected)
    if write and not aligned:
        path.write_text(_canonical(expected), encoding="utf-8", newline="\n")
        aligned = True
    return aligned, len(expected_entries())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--print-entry", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    if arguments.print_entry:
        print(_canonical(expected_entries()[0].to_dict()), end="")
        return 0
    aligned, count = synchronize(arguments.ledger.resolve(), write=arguments.write or not arguments.check)
    print(f"recovery_planner_integration_source_ledger_aligned={str(aligned).lower()}")
    print(f"recovery_planner_integration_source_decision_count={count}")
    print(f"recovery_planner_integration_source_owner_unit={OWNER_UNIT}")
    print(f"ledger_path={arguments.ledger.resolve()}")
    return 0 if aligned or not arguments.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
