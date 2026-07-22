from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

import audit_m1_s07a02_effective_code_gate as common


SLICE_BASELINE = "805d31985df5f549745ac2fd2b21d4bfc6236dd6"
PARENT_BASELINE = "6900e96dcb6dd73c22b803787afc30125fbe947c"
PREVIOUS_IMPLEMENTATION_COMMITS = {
    "db27e57892cbb62eec4e783f73394c0a6a5d54df",
    "242038c842a02b3311e2452dad3cb86a2fa864e6",
}
SLICE_MINIMUM = 6500
PARENT_MINIMUM = 15000


def source_role(path: str) -> str:
    normalized = path.casefold()
    name = PurePosixPath(path).name.casefold()
    if normalized.startswith("docs/") or "/data/" in normalized:
        return "evidence_or_data"
    if normalized.startswith("scripts/"):
        return "nonproduction_audit_tooling"
    if normalized.startswith("tests/") or "/test/" in normalized:
        return "behavior_verification"
    if normalized == (
        "packages/scheduler/zyra_scheduler/fault_runtime/browser_integration.py"
    ):
        return "browser-use_primary_same_language"
    if normalized.startswith("packages/runtime/claude-runtime/src/watchdog/"):
        if name == "index.ts":
            return "typescript_export"
        return "oh-my-pi_supplementary_same_language"
    if normalized == "packages/runtime/claude-runtime/src/stdio.ts":
        return "oh-my-pi_supplementary_same_language"
    if normalized.startswith("packages/scheduler/zyra_scheduler/fault_runtime/"):
        return "zyra_owned_fault_integration"
    if normalized.startswith("packages/commands/"):
        return "zyra_owned_control_main_path"
    if normalized.startswith("apps/api/"):
        return "zyra_owned_api_main_path"
    return "zyra_supporting"


def resolve_commit(revision: str) -> str:
    return common.git("rev-parse", f"{revision}^{{commit}}").strip()


def audit(*, target: str) -> dict[str, Any]:
    resolved_target = resolve_commit(target)
    original_role = common.source_role
    common.source_role = source_role
    try:
        slice_report = common.audit_range(
            base=SLICE_BASELINE,
            target=resolved_target,
        )
        parent_report = common.audit_range(
            base=PARENT_BASELINE,
            target=resolved_target,
            allowed_commits={*PREVIOUS_IMPLEMENTATION_COMMITS, resolved_target},
        )
    finally:
        common.source_role = original_role

    blockers: list[str] = []
    slice_effective = int(slice_report["totals"]["effective_production"])
    parent_effective = int(parent_report["totals"]["effective_production"])
    if slice_effective < SLICE_MINIMUM:
        blockers.append(
            f"slice effective production {slice_effective} < {SLICE_MINIMUM}"
        )
    if parent_effective < PARENT_MINIMUM:
        blockers.append(
            f"parent effective production {parent_effective} < {PARENT_MINIMUM}"
        )

    role_totals = {
        (str(item["source_role"]), str(item["language"])): int(item["production"])
        for item in slice_report["by_source_role_and_language"]
    }
    if role_totals.get(("browser-use_primary_same_language", "python"), 0) <= 0:
        blockers.append("Browser Use primary Python effective production is zero")
    if role_totals.get(("oh-my-pi_supplementary_same_language", "typescript"), 0) <= 0:
        blockers.append("OMP supplementary TypeScript effective production is zero")

    return {
        "schema": "zyra.effective-code-language-gate-audit/v1",
        "slice_id": "M1-S07B-02",
        "audit_tool_is_nonproduction": True,
        "method": {
            "raw": "target-side added lines from git diff --unified=0",
            "ownership": "git blame at the frozen implementation target for parent scope",
            "python": "AST/token exclusion of types, schema/DTO declarations, comments and blank lines",
            "typescript": "type/interface/import-type/declaration and comment range exclusion",
            "effective": "production_runtime plus UI_behavior only",
            "parent": "recomputed from the frozen parent baseline; no arithmetic carry-forward",
        },
        "slice": slice_report,
        "parent": parent_report,
        "gate": {
            "ok": not blockers,
            "blockers": blockers,
            "slice_minimum": SLICE_MINIMUM,
            "parent_minimum": PARENT_MINIMUM,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="HEAD")
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--fail-on-gate", action="store_true")
    arguments = parser.parse_args(argv)
    result = audit(target=arguments.target)
    if arguments.summary_only:
        for scope in ("slice", "parent"):
            report = result.get(scope)
            if isinstance(report, Mapping):
                report.pop("files", None)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if arguments.fail_on_gate and result["gate"]["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
