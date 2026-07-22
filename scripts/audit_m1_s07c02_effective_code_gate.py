from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

import audit_m1_s07a02_effective_code_gate as common


SLICE_BASELINE = "fabe347207bc3d2d144ad68c029cb288f09e73a8"
PARENT_BASELINE = "aa494526ed7f32b177f9a10fa3a93d8a684e0264"
IMPLEMENTATION = "8de780f"
SLICE_MINIMUM = 7_000
PARENT_FOUNDATION_EFFECTIVE = 8_429
PARENT_MINIMUM = 15_000


def source_role(path: str) -> str:
    normalized = path.casefold()
    name = PurePosixPath(path).name.casefold()
    if normalized.startswith("docs/") or "/data/" in normalized:
        return "evidence_or_data"
    if normalized.startswith("scripts/"):
        return "nonproduction_audit_tooling"
    if normalized.startswith("tests/") or "/test/" in normalized:
        return "behavior_verification"
    if normalized.startswith("packages/runtime/claude-runtime/src/recovery/"):
        return "typescript_export" if name == "index.ts" else "oh-my-pi_supplementary_same_language"
    if normalized.startswith("packages/scheduler/zyra_scheduler/recovery_runtime/"):
        return "python_export" if name == "__init__.py" else "zyra_owned_recovery_primary"
    if normalized.startswith("apps/api/zyra_api/"):
        return "zyra_owned_recovery_main_path"
    return "zyra_supporting"


def audit(*, target: str) -> dict[str, Any]:
    resolved_target = common.git("rev-parse", f"{target}^{{commit}}").strip()
    original_role = common.source_role
    common.source_role = source_role
    try:
        report = common.audit_range(base=SLICE_BASELINE, target=resolved_target)
        parent_report = common.audit_range(base=PARENT_BASELINE, target=resolved_target)
    finally:
        common.source_role = original_role

    blockers: list[str] = []
    effective = int(report["totals"]["effective_production"])
    cumulative_sum = PARENT_FOUNDATION_EFFECTIVE + effective
    cumulative = int(parent_report["totals"]["effective_production"])
    if effective < SLICE_MINIMUM:
        blockers.append(f"slice effective production {effective} < {SLICE_MINIMUM}")
    if cumulative < PARENT_MINIMUM:
        blockers.append(f"parent cumulative effective production {cumulative} < {PARENT_MINIMUM}")
    role_totals = {
        (str(item["source_role"]), str(item["language"])): int(item["production"])
        for item in report["by_source_role_and_language"]
    }
    if role_totals.get(("zyra_owned_recovery_primary", "python"), 0) <= 0:
        blockers.append("Zyra-owned Python recovery production is zero")
    if role_totals.get(("oh-my-pi_supplementary_same_language", "typescript"), 0) <= 0:
        blockers.append("OMP supplementary TypeScript production is zero")
    return {
        "schema": "zyra.effective-code-language-gate-audit/v1",
        "slice_id": "M1-S07C-02",
        "parent_unit": "M1-07C",
        "audit_tool_is_nonproduction": True,
        "method": {
            "raw": "target-side added lines from git diff --unified=0",
            "python": "AST/token exclusion of Protocol, dataclass/enum fields, DTO mapping, SQL schema, comments and blank lines",
            "typescript": "interface/type/import-type/declaration and comment range exclusion",
            "effective": "production_runtime only; tests, docs, data, exports and audit tooling excluded",
            "target": resolved_target,
        },
        "slice": report,
        "gate": {
            "ok": not blockers,
            "blockers": blockers,
            "minimum": SLICE_MINIMUM,
            "effective": effective,
            "margin": effective - SLICE_MINIMUM,
        },
        "parent_gate": {
            "ok": cumulative >= PARENT_MINIMUM,
            "minimum": PARENT_MINIMUM,
            "foundation_effective": PARENT_FOUNDATION_EFFECTIVE,
            "integration_effective": effective,
            "slice_sum_effective": cumulative_sum,
            "cumulative_effective": cumulative,
            "recomputed_from_parent_baseline": PARENT_BASELINE,
            "slice_sum_delta": cumulative - cumulative_sum,
            "margin": cumulative - PARENT_MINIMUM,
        },
        "parent_recomputed": {
            key: value for key, value in parent_report.items() if key != "files"
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default=IMPLEMENTATION)
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--fail-on-gate", action="store_true")
    arguments = parser.parse_args(argv)
    result = audit(target=arguments.target)
    if arguments.summary_only:
        report = result.get("slice")
        if isinstance(report, Mapping):
            report.pop("files", None)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if arguments.fail_on_gate and result["gate"]["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
