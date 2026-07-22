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


OWNER_UNIT = "M1-S07C-01"
STAMP = "2026-07-22T12:00:00.000Z"
OMP_COMMIT = "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca"
DEFAULT_LEDGER = ROOT / "packages" / "integrations" / "zyra_integrations" / "data" / "internalization_ledger_seed.json"


def _omp_entry() -> InternalizationLedgerEntry:
    source_path = (
        "docs/non-compaction-retry-policy.md;"
        "packages/ai/src/auth-retry.ts;"
        "packages/coding-agent/src/session/session-manager.ts;"
        "packages/coding-agent/src/task/executor.ts;"
        "packages/coding-agent/src/task/worktree.ts"
    )
    targets = [
        "packages/runtime/claude-runtime/src/recovery/omp-recovery-runtime.ts",
        "packages/runtime/claude-runtime/src/recovery/continuity-runtime.ts",
        "packages/runtime/claude-runtime/src/watchdog/runtime.ts",
        "packages/scheduler/zyra_scheduler/recovery_runtime/signal_classifier.py",
    ]
    entry = InternalizationLedgerEntry.new(
        source_repo="oh-my-pi",
        source_path=source_path,
        capability_name="omp_retry_fallback_continuity_and_replay_fence_receipts",
        capability_summary=(
            "Cropped TypeScript retry/fallback classification, bounded backoff, append-only "
            "session/fork continuity, task terminal generations and worktree merge replay fences "
            "produce supplementary receipts consumed by the Python recovery policy owner."
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
    entry.downstream_units = ["M1-S07C-02", "M1-08", "M2-recovery-console"]
    entry.dependencies = ["M1-S02B-01", "M1-S02D-01", "M1-S03A-02", "M1-S07B-02"]
    entry.source_evidence = [SourceEvidence(
        source_repo="oh-my-pi",
        source_path=source_path,
        exists_in_workspace=True,
        source_kind="bounded_module_group",
        reason="Pinned OMP revision and selected recovery/session/task/worktree modules verified for M1-S07C-01.",
        symbols=["bounded retry", "provider fallback", "SessionManager resume/fork", "TaskTool terminal state", "worktree merge receipt"],
        tags=["supplementary_implementation", OWNER_UNIT],
    )]
    entry.main_path = MainPathBinding(
        surfaces=["claude_runtime_watchdog_recovery_receipt", "python_recovery_classifier", "task_recovery_api"],
        event_types=["tool_failure_signal", "recovery_planned", "topology_route"],
        api_routes=["POST /tasks/{task_id}/recovery/signals", "GET /tasks/{task_id}/recovery"],
        control_commands=[],
        artifact_kinds=["checkpoint_receipt", "execution_receipt"],
        worker_runtime="RuntimeWatchdogObserver -> OmpRecoveryReceiptRuntime -> RecoverySignalClassifier",
        ui_panels=[],
    )
    entry.line_count_policy = LineCountPolicy.COUNTS_AS_RUNTIME
    entry.license_notice = LicenseNotice(
        source_repo="oh-my-pi",
        status=NoticeStatus.RECORDED,
        license_hint="Cropped TypeScript mechanisms retained with provenance.",
        notice_path="packages/scheduler/THIRD_PARTY_NOTICES.md",
        notes="No runtime dependency on the parent OMP repository or its state stores.",
    )
    entry.runtime_entry = RuntimeEntry(
        module="@zyra/claude-runtime/recovery",
        function="OmpRecoveryReceiptRuntime",
        protocol="zyra.omp-recovery-receipt/v1",
        health_check="npm run test:recovery --workspace=@zyra/claude-runtime",
        config_refs=targets,
    )
    entry.test_entries = [
        TestEntry(
            path="packages/runtime/claude-runtime/test/recovery.test.ts",
            command="npm run test:recovery --workspace=@zyra/claude-runtime",
            kind="original_language_behavior",
            expected_signal="bounded retry, replay fences, append-only resume/fork, task terminal and worktree merge receipts mutate process-local state and fail closed",
            required=True,
        ),
        TestEntry(
            path="tests/integration/test_recovery_planner_routing_memory_api.py",
            command="python -m pytest -q tests/integration/test_recovery_planner_routing_memory_api.py",
            kind="integration_main_path",
            expected_signal="typed receipts enter the Python owner and concrete actions, events and routing memory change canonical execution",
            required=True,
        ),
    ]
    entry.replacement_plan = (
        f"source_role=supplementary_implementation; source_commit={OMP_COMMIT}; cropped same-language "
        "migration keeps receipt/backoff/replay mechanisms while excluding OMP session, task, provider and workspace stores."
    )
    entry.metadata = {
        "owner_unit": OWNER_UNIT,
        "source_role": "supplementary_implementation",
        "source_commit": OMP_COMMIT,
        "source_language": "typescript",
        "target_language": "typescript",
        "migration_mode": "cropped_same_language_recovery_receipt_migration",
        "canonical_policy_owner": "python.RecoveryDecisionRuntime",
        "canonical_checkpoint_owner": "python.RecoveryPlanStore",
        "omp_runtime_dependency": False,
        "root_source_runtime_dependency": False,
        "openclaw_forward_excluded": True,
    }
    entry.created_at = STAMP
    entry.updated_at = STAMP
    return entry


def expected_entries() -> list[InternalizationLedgerEntry]:
    # Conformance/reference decisions stay in the decision and evidence records.
    # The implementation ledger only materializes sources that carry production
    # migration, main-path and original-language custody obligations.
    entries = [_omp_entry()]
    for entry in entries:
        errors = entry.validate()
        if errors:
            raise ValueError(f"invalid recovery source ledger entry {entry.ledger_id}: {errors}")
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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    aligned, count = synchronize(arguments.ledger.resolve(), write=arguments.write or not arguments.check)
    print(f"recovery_planner_source_ledger_aligned={str(aligned).lower()}")
    print(f"recovery_planner_source_decision_count={count}")
    print(f"recovery_planner_source_owner_unit={OWNER_UNIT}")
    print(f"ledger_path={arguments.ledger.resolve()}")
    return 0 if aligned or not arguments.check else 1


if __name__ == "__main__":
    raise SystemExit(main())
