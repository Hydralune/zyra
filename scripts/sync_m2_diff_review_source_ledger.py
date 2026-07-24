from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
for package_path in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations import (  # noqa: E402
    InternalizationLedger,
    InternalizationLedgerEntry,
)
from zyra_integrations.ledger_models import to_jsonable  # noqa: E402


HELPER_PATH = ROOT / "scripts" / "sync_m2_artifact_viewer_source_ledger.py"
HELPER_SPEC = importlib.util.spec_from_file_location(
    "m2_diff_review_ledger_helpers",
    HELPER_PATH,
)
if HELPER_SPEC is None or HELPER_SPEC.loader is None:
    raise RuntimeError("M2 artifact ledger helper could not be loaded")
HELPER = importlib.util.module_from_spec(HELPER_SPEC)
HELPER_SPEC.loader.exec_module(HELPER)

DEFAULT_LEDGER = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
OWNER_UNIT = "M2-S03A-02"
SLICE_ID = OWNER_UNIT
IMPLEMENTATION_COMMIT = "ae2a37849cf1099c03e62e7228bfecb478c2cd8a"
BASELINE_COMMIT = "5dec6715db9f35913acd1b3a078ce9bcde943a8a"
STAMP = "2026-07-24T00:00:00.000Z"
WEB_TEST = "apps/web/test/diff-patch-review-integration.test.ts"
INTEGRATION_TEST = "tests/integration/test_diff_patch_review_integration.py"


DECISIONS: tuple[dict[str, Any], ...] = (
    {
        "source_repo": "opencode",
        "source_commit": "adf178a6b95c61506ddaadaf4dd062badb4a8fda",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "packages/app/src/pages/session/review-tab.tsx;"
            "packages/app/src/pages/session/v2/{review-panel-v2.tsx,"
            "review-panel-v2-state.ts,review-diff-kinds.ts};"
            "packages/app/src/utils/diffs.ts"
        ),
        "capability_name": "diff_review_runtime_and_stable_hunk_interaction",
        "capability_summary": (
            "Stable file focus, diff classification, hunk state, split/unified "
            "review interaction, filtering, navigation and refresh behavior."
        ),
        "targets": [
            "apps/web/src/features/diff-review/contracts.ts",
            "apps/web/src/features/diff-review/unified-diff.ts",
            "apps/web/src/features/diff-review/virtualization.ts",
            "apps/web/src/features/diff-review/search.ts",
            "apps/web/src/features/diff-review/review-model.ts",
            "apps/web/src/features/diff-review/runtime.ts",
        ],
        "source_role": "primary_implementation",
        "migration_mode": (
            "cropped_migration/same_language_component_integration"
        ),
        "migration_strategy": "direct_port",
        "rationale": (
            "OpenCode review mechanics were split into Zyra-owned parser, "
            "virtualization, review-model and orchestration modules. Its "
            "session store, server and patch owner were not copied."
        ),
    },
    {
        "source_repo": "OpenHands",
        "source_commit": "c105a82387898e744423c8831d412e26495b38a9",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "frontend/src/components/features/diff-viewer/"
            "{file-diff-viewer.tsx,editor-container.tsx};"
            "frontend/src/hooks/query/use-unified-git-diff.ts"
        ),
        "capability_name": "lazy_large_diff_view_and_fetch_lifecycle",
        "capability_summary": (
            "Lazy file loading, bounded request lifecycle, explicit "
            "loading/error/empty states and read-only diff presentation."
        ),
        "targets": [
            "apps/web/src/features/diff-review/budget.ts",
            "apps/web/src/features/diff-review/fetch-runtime.ts",
            (
                "apps/web/src/features/diff-review/view/"
                "diff-review-workbench.tsx"
            ),
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": (
            "cropped_migration/same_language_component_integration"
        ),
        "migration_strategy": "selective_port",
        "rationale": (
            "Only large diff loading and review-view lifecycle supplement "
            "the primary runtime. Monaco, Redux, sandbox and OpenHands server "
            "ownership remain excluded."
        ),
    },
    {
        "source_repo": "oh-my-pi",
        "source_commit": "c6b83c1d96d0e48d169a0519a6f2a72f2c3797ca",
        "source_language": "typescript",
        "target_language": "typescript",
        "source_path": (
            "packages/hashline/src/{mismatch.ts,diff-preview.ts,snapshots.ts,"
            "patcher.ts}"
        ),
        "capability_name": "hashline_preflight_three_way_and_receipt_binding",
        "capability_summary": (
            "Snapshot-bound edit anchors, stale diagnostics, bounded "
            "three-way conflict projection and exact transaction receipts."
        ),
        "targets": [
            "apps/web/src/features/diff-review/hashline.ts",
            "apps/web/src/features/diff-review/merge.ts",
            "apps/web/src/features/diff-review/transactions.ts",
        ],
        "source_role": "supplementary_implementation",
        "migration_mode": "bounded_same_language_semantic_integration",
        "migration_strategy": "selective_port",
        "rationale": (
            "OMP hashline receipt semantics were integrated as browser "
            "preflight and conflict projections. Its filesystem patcher does "
            "not own or execute Zyra workspace mutations."
        ),
    },
)


def entry(decision: dict[str, Any]) -> dict[str, Any]:
    value = HELPER.PRIOR.entry(decision)
    source_role = str(decision["source_role"])
    value.update(
        {
            "owner_unit": OWNER_UNIT,
            "created_at": STAMP,
            "updated_at": STAMP,
            "downstream_units": ["M2-03B", "M2-04A", "M2-04B"],
            "replacement_plan": (
                "Replace behind DiffReviewWorkbenchRuntime and the typed "
                "diff-review protocol without moving workspace, permission "
                "or artifact custody."
            ),
            "risk_notes": [
                "No runtime path depends on a parent source repository.",
                (
                    "WorkspacePatchTransactionRuntime remains the only file "
                    "mutation, snapshot, verification and rollback owner."
                ),
                (
                    "PermissionCoordinator remains the only permission "
                    "decision and exact-permit owner."
                ),
                (
                    "Browser diff pages, selection, comments and preflight "
                    "are bounded projections, not canonical file truth."
                ),
            ],
        }
    )
    value["license_notice"] = {
        "source_repo": decision["source_repo"],
        "status": "recorded",
        "license_hint": "M2 diff/review source role recorded.",
        "notice_path": "third_party/NOTICE.md",
        "source_url": "",
        "notes": "No runtime dependency on a parent source repository.",
    }
    value["source_evidence"][0]["tags"] = ["m2-03a", source_role]
    value["main_path"] = {
        "surfaces": ["web_workbench", "task_detail", "task_api"],
        "event_types": [
            "diff_review",
            "diff_patch_transaction",
            "artifact.*",
            "workspace.*",
        ],
        "api_routes": [
            "/tasks/{task_id}/diff-reviews/{artifact_id}",
            (
                "/tasks/{task_id}/diff-reviews/{artifact_id}/"
                "files/{file_id}/hunks"
            ),
            "/tasks/{task_id}/diff-reviews/{artifact_id}/comments",
            "/tasks/{task_id}/diff-reviews/{artifact_id}/apply",
            (
                "/tasks/{task_id}/diff-reviews/transactions/"
                "{transaction_id}/rollback"
            ),
        ],
        "control_commands": ["permission exact permit claim"],
        "artifact_kinds": ["patch", "text", "verification", "workspace_snapshot"],
        "worker_runtime": (
            "TaskState artifact revision -> DiffReviewApiService -> typed "
            "TaskApi -> DiffReviewWorkbenchRuntime -> permission -> "
            "WorkspacePatchTransactionRuntime -> event/artifact refresh"
        ),
        "ui_panels": [
            "diff_file_list",
            "virtualized_diff",
            "review_comments",
            "patch_transaction_receipt",
        ],
    }
    value["runtime_entry"] = {
        "module": "apps.web.src.features.diff-review.runtime",
        "function": "DiffReviewWorkbenchRuntime/DiffReviewApiService",
        "protocol": "zyra.diff-review.v1",
        "health_check": (
            f"bun test ./{WEB_TEST}; "
            f"python -m pytest -q {INTEGRATION_TEST}"
        ),
        "command": "bun run build:web",
        "config_refs": list(decision["targets"]),
        "environment_refs": [
            "ZYRA_ARTIFACT_ROOT",
            "ZYRA_WORKSPACE_STATE_ROOT",
            "ZYRA_DIFF_REVIEW_DISABLED",
        ],
    }
    value["test_entries"] = [
        {
            "path": WEB_TEST,
            "command": f"bun test ./{WEB_TEST}",
            "kind": "unit",
            "expected_signal": (
                "strict diff/page/receipt contracts, 100k-line virtualization, "
                "paging, cancellation, search, stale preflight, three-way "
                "conflict and transaction identity"
            ),
            "required": True,
        },
        {
            "path": INTEGRATION_TEST,
            "command": f"python -m pytest -q {INTEGRATION_TEST}",
            "kind": "integration",
            "expected_signal": (
                "real workspace/artifact/HTTP paths prove add/delete/rename/"
                "binary/encoding, permission, sealed denial, stale, apply, "
                "rollback, rollback failure and owner disable"
            ),
            "required": True,
        },
    ]
    value["tags"] = [
        "m2-03a",
        SLICE_ID.lower(),
        "diff-review",
        "patch-transaction",
        source_role,
    ]
    value["metadata"].update(
        {
            "owner_unit": OWNER_UNIT,
            "slice_id": SLICE_ID,
            "source_role": source_role,
            "source_commit": decision["source_commit"],
            "source_language": decision["source_language"],
            "target_language": decision["target_language"],
            "migration_mode": decision["migration_mode"],
            "canonical_patch_owner": "python.WorkspacePatchTransactionRuntime",
            "canonical_workspace_owner": "python.WorkspaceManagerRuntime",
            "canonical_permission_owner": "typescript.PermissionCoordinator",
            "canonical_artifact_owner": "python.TaskState.artifacts",
            "transient_review_owner": "typescript.DiffReviewWorkbenchRuntime",
            "root_source_runtime_dependency": False,
            "implementation_commit": IMPLEMENTATION_COMMIT,
            "baseline_commit": BASELINE_COMMIT,
            "rationale": decision["rationale"],
        }
    )
    return value


def rewrite(document: Any) -> Any:
    if isinstance(document, list):
        entries = document
    elif isinstance(document, dict) and isinstance(document.get("entries"), list):
        entries = document["entries"]
    else:
        raise ValueError("ledger seed must be a list or contain entries")
    replacements = [entry(decision) for decision in DECISIONS]
    output: list[dict[str, Any]] = []
    inserted = False
    for item in entries:
        owned = (
            str(item.get("owner_unit") or "") == OWNER_UNIT
            or str((item.get("metadata") or {}).get("slice_id") or "")
            == SLICE_ID
        )
        if owned:
            if not inserted:
                output.extend(replacements)
                inserted = True
            continue
        output.append(item)
    if not inserted:
        output.extend(replacements)
    typed = [InternalizationLedgerEntry.from_dict(item) for item in output]
    if isinstance(document, list):
        return output
    return {
        **document,
        "entries": output,
        "summary": to_jsonable(InternalizationLedger(typed).summary()),
    }


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def git_file_exists(commit: str, path: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{path}"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def audit_targets(document: Any) -> tuple[int, list[str]]:
    entries = document if isinstance(document, list) else document["entries"]
    selected = [
        item
        for item in entries
        if str(item.get("owner_unit") or "") == OWNER_UNIT
    ]
    errors: list[str] = []
    for item in selected:
        identity = str(item.get("ledger_id") or "")
        for binding in item.get("target_bindings") or []:
            path = str(binding.get("target_path") or "")
            if path and not git_file_exists(IMPLEMENTATION_COMMIT, path):
                errors.append(f"{identity}:missing:{path}")
    return len(selected), errors


def synchronize(path: Path, *, write: bool) -> tuple[bool, int, list[str]]:
    current = json.loads(path.read_text(encoding="utf-8"))
    expected = rewrite(current)
    aligned = canonical(current) == canonical(expected)
    if write and not aligned:
        path.write_text(canonical(expected), encoding="utf-8", newline="\n")
        current = expected
        aligned = True
    checked = current if aligned else expected
    count, errors = audit_targets(checked)
    return aligned, count, errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    aligned, count, errors = synchronize(
        args.ledger.resolve(),
        write=args.write or not args.check,
    )
    print(f"m2_diff_review_ledger_aligned={str(aligned).lower()}")
    print(f"m2_diff_review_source_decision_count={len(DECISIONS)}")
    print(f"m2_diff_review_ledger_entry_count={count}")
    print(f"m2_diff_review_missing_target_count={len(errors)}")
    for error in errors:
        print(f"target_error={error}")
    print(f"ledger_path={args.ledger.resolve()}")
    return 0 if aligned and not errors and count == len(DECISIONS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
