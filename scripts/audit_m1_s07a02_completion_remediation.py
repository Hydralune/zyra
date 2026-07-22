from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import audit_m1_s07a02_effective_code_gate as frozen


DECISION_COMMIT = "b4bd15832fc3729e451e438bae23fbbfaa063f06"
IMPLEMENTATION_COMMIT = "2cf2bfa718d1c22d06cf4ef48119d21408dd3a0c"
CURRENT_PARENT_IMPLEMENTATION_COMMITS = {
    *frozen.PARENT_IMPLEMENTATION_COMMITS,
    IMPLEMENTATION_COMMIT,
}


def _production_by_role(report: Mapping[str, object]) -> dict[tuple[str, str], int]:
    rows = report.get("by_source_role_and_language")
    if not isinstance(rows, list):
        return {}
    return {
        (str(item["source_role"]), str(item["language"])): int(item["production"])
        for item in rows
        if isinstance(item, Mapping)
    }


def audit() -> dict[str, object]:
    frozen_slice = frozen.audit_range(
        base=frozen.SLICE_BASELINE,
        target=frozen.IMPLEMENTATION,
    )
    remediation = frozen.audit_range(
        base=DECISION_COMMIT,
        target=IMPLEMENTATION_COMMIT,
    )
    current_parent = frozen.audit_range(
        base=frozen.PARENT_BASELINE,
        target=IMPLEMENTATION_COMMIT,
        allowed_commits=CURRENT_PARENT_IMPLEMENTATION_COMMITS,
    )
    blockers: list[str] = []
    frozen_effective = int(frozen_slice["totals"]["effective_production"])
    remediation_effective = int(remediation["totals"]["effective_production"])
    parent_effective = int(current_parent["totals"]["effective_production"])
    if frozen_effective < 6000:
        blockers.append(f"frozen slice effective production {frozen_effective} < 6000")
    if remediation_effective <= 0:
        blockers.append("remediation has no effective production")
    if parent_effective < 15000:
        blockers.append(f"current parent effective production {parent_effective} < 15000")
    roles = _production_by_role(current_parent)
    if roles.get(("agentscope_primary_same_language", "python"), 0) <= 0:
        blockers.append("AgentScope primary Python effective production is zero")
    if roles.get(("oh-my-pi_supplementary_same_language", "typescript"), 0) <= 0:
        blockers.append("OMP supplementary TypeScript effective production is zero")
    return {
        "schema": "zyra.m1-s07a-02-completion-remediation-audit/v1",
        "commit_boundary": {
            "parent_baseline": frozen.PARENT_BASELINE,
            "slice_baseline": frozen.SLICE_BASELINE,
            "frozen_original_implementation": frozen.IMPLEMENTATION,
            "frozen_original_evidence": "66b3c42da106a799b65eea3e17a7efcdff0b5f2b",
            "remediation_audit_baseline": "2bdaa4acd860e62eda705f2cb248cc7483a8a2aa",
            "prospective_decision": DECISION_COMMIT,
            "remediation_implementation": IMPLEMENTATION_COMMIT,
            "current_parent_implementation_commits": sorted(
                CURRENT_PARENT_IMPLEMENTATION_COMMITS
            ),
        },
        "method": {
            "raw": "target-side added lines from git diff --unified=0",
            "ownership": "git blame at each frozen target; current parent accepts only enumerated 07A implementation commits",
            "effective": "production_runtime plus UI_behavior only",
            "excluded": [
                "type/interface/import-only declarations",
                "schema/DTO/data/ledger",
                "generated",
                "tests/mock/fixture",
                "adapter-only",
                "nonproduction tooling",
                "docs/comments/blank",
                "unrelated or evidence commits",
            ],
            "frozen_history_policy": "the original implementation/evidence report is unchanged; remediation is a prospective range",
        },
        "language_contract": {
            "agentscope_primary": {
                "source_language": "python",
                "target_language": "python",
                "migration_mode": "cropped_migration/same_language_module_integration",
            },
            "oh_my_pi_supplementary": {
                "source_language": "typescript",
                "target_language": "typescript",
                "migration_mode": "native_extract/cropped_migration",
            },
            "protocol_adapter": {
                "path": "packages/workers/zyra_workers/edge_pool/integration.py",
                "target_language": "python",
                "effective_production": 0,
                "excluded_as": "adapter_only",
            },
            "cross_language_exception": None,
        },
        "frozen_slice": frozen_slice,
        "remediation": remediation,
        "current_parent": current_parent,
        "gate": {
            "ok": not blockers,
            "blockers": blockers,
            "slice_minimum": 6000,
            "parent_minimum": 15000,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--fail-on-gate", action="store_true")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    result = audit()
    if arguments.summary_only:
        for scope in ("frozen_slice", "remediation", "current_parent"):
            report = result.get(scope)
            if isinstance(report, dict):
                report.pop("files", None)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 1 if arguments.fail_on_gate and not result["gate"]["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
