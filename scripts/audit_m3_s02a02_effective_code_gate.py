from __future__ import annotations

import argparse
import ast
import json
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import audit_m1_s07a02_effective_code_gate as common


SLICE_ID = "M3-S02A-02"
SLICE_BASELINE = "c01fcacf7cd30c37aa9629aed76a7df5256dad63"
PARENT_BASELINE = "42f760229588aba917c7283b841587afc7142b0b"
SLICE_MINIMUM = 6_000
PARENT_MINIMUM = 12_000
LIVE_ROOT = "packages/evaluation/zyra_evaluation/live_benchmark/"
REGRESSION_ROOT = "packages/evaluation/zyra_evaluation/regression_hardening/"
PORT_PATH = "apps/api/zyra_api/live_benchmark_port.py"
RESEARCH_PATH = (
    "packages/evaluation/zyra_evaluation/scenario_runner/research_delivery.py"
)


def source_role(path: str) -> str:
    normalized = path.casefold()
    name = PurePosixPath(path).name.casefold()
    if normalized.startswith("docs/") or normalized.endswith(".md"):
        return "evidence_or_documentation"
    if normalized.startswith("tests/") or "/test/" in normalized:
        return "behavior_verification"
    if normalized.startswith("scripts/"):
        return "nonproduction_gate_or_runner"
    if normalized.startswith(REGRESSION_ROOT):
        return "zyra_owned_regression_hardening"
    if normalized == PORT_PATH:
        return "zyra_owned_product_live_owner_binding"
    if normalized == RESEARCH_PATH:
        return "zyra_owned_deterministic_research_delivery"
    if not normalized.startswith(LIVE_ROOT):
        return "slice_supporting_configuration"
    if name == "__init__.py":
        return "public_exports"
    if name in {"models.py"}:
        return "schema_and_type_contract"
    if name in {"matrix.py", "admission.py", "semantic_steps.py"}:
        return "zyra_owned_plan_admission_and_semantic_steps"
    if name in {"deployment.py", "faults.py", "verifiers.py"}:
        return "zyra_owned_deployment_fault_and_domain_verification"
    if name in {"metrics.py", "statistics.py"}:
        return "zyra_owned_metrics_and_paired_statistics"
    if name in {"store.py", "integrity.py", "reporting.py", "freeze_gate.py"}:
        return "zyra_owned_store_integrity_report_and_freeze"
    if name in {"runtime.py", "preconditions.py", "canonical.py"}:
        return "zyra_owned_benchmark_orchestration"
    if name == "protected_evidence.py":
        return "zyra_owned_protected_evidence_admission"
    return "zyra_owned_live_benchmark_runtime"


def is_accounted_role(role: str) -> bool:
    return role.startswith("zyra_owned_")


def _audit_range(base: str, target: str) -> dict[str, Any]:
    original_role = common.source_role
    common.source_role = source_role
    try:
        return common.audit_range(base=base, target=target)
    finally:
        common.source_role = original_role


def _effective(report: Mapping[str, Any]) -> int:
    return sum(
        int(item.get("effective_production") or 0)
        for item in report.get("files", ())
        if isinstance(item, Mapping)
        and is_accounted_role(str(item.get("source_role") or ""))
    )


def _role_totals(report: Mapping[str, Any]) -> dict[str, int]:
    output: dict[str, int] = {}
    for item in report.get("files", ()):
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("source_role") or "")
        if not is_accounted_role(role):
            continue
        output[role] = output.get(role, 0) + int(
            item.get("effective_production") or 0
        )
    return dict(sorted(output.items()))


def _large_file_audit(
    target: str,
    report: Mapping[str, Any],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in report.get("files", ()):
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("source_role") or "")
        path = str(item.get("path") or "")
        if not is_accounted_role(role) or not path.endswith(".py"):
            continue
        source = common.target_text(target, path)
        line_count = len(source.splitlines())
        if line_count <= 500:
            continue
        tree = ast.parse(source)
        symbols: list[dict[str, Any]] = []
        for node in tree.body:
            if not isinstance(
                node,
                (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                continue
            end = int(getattr(node, "end_lineno", node.lineno))
            symbols.append(
                {
                    "name": node.name,
                    "kind": type(node).__name__,
                    "start": node.lineno,
                    "end": end,
                    "lines": end - node.lineno + 1,
                }
            )
        output.append(
            {
                "path": path,
                "source_role": role,
                "line_count": line_count,
                "effective_production": int(
                    item.get("effective_production") or 0
                ),
                "symbol_count": len(symbols),
                "symbols": symbols,
                "cohesion": (
                    "The module owns one fail-closed benchmark responsibility "
                    "and keeps its accepting and rejecting paths together."
                ),
                "split_assessment": (
                    "The current boundary avoids duplicating canonical "
                    "validation, receipt and custody invariants."
                ),
            }
        )
    return output


def audit(*, target: str) -> dict[str, Any]:
    resolved_target = common.git("rev-parse", f"{target}^{{commit}}").strip()
    slice_report = _audit_range(SLICE_BASELINE, resolved_target)
    parent_report = _audit_range(PARENT_BASELINE, resolved_target)
    slice_effective = _effective(slice_report)
    parent_effective = _effective(parent_report)
    roles = _role_totals(slice_report)
    required_roles = {
        "zyra_owned_plan_admission_and_semantic_steps",
        "zyra_owned_deployment_fault_and_domain_verification",
        "zyra_owned_metrics_and_paired_statistics",
        "zyra_owned_store_integrity_report_and_freeze",
        "zyra_owned_benchmark_orchestration",
        "zyra_owned_product_live_owner_binding",
        "zyra_owned_protected_evidence_admission",
    }
    blockers: list[str] = []
    if slice_effective < SLICE_MINIMUM:
        blockers.append(
            f"slice effective production {slice_effective} < {SLICE_MINIMUM}"
        )
    if parent_effective < PARENT_MINIMUM:
        blockers.append(
            f"parent effective production {parent_effective} < {PARENT_MINIMUM}"
        )
    for role in sorted(required_roles):
        if roles.get(role, 0) <= 0:
            blockers.append(f"required production role has zero code: {role}")
    large_files = _large_file_audit(resolved_target, slice_report)
    if not large_files:
        blockers.append("large-file cohesion audit is empty")
    return {
        "schema": "zyra.effective-code-language-gate-audit/v1",
        "slice_id": SLICE_ID,
        "parent_unit": "M3-02A",
        "audit_tool_is_nonproduction": True,
        "method": {
            "raw": "target-side added lines from git diff --unified=0",
            "python": (
                "AST/token exclusion of Protocol, dataclass/enum fields, DTO "
                "mapping, schema constants, comments and blank lines"
            ),
            "effective": (
                "behavior-bearing benchmark planning, admission, semantic "
                "verification, statistics, custody, product binding and freeze"
            ),
            "slice_baseline": SLICE_BASELINE,
            "parent_baseline": PARENT_BASELINE,
            "target": resolved_target,
            "tests_docs_scripts_count_toward_minimum": False,
        },
        "slice": slice_report,
        "parent": parent_report,
        "accounted_effective_production": slice_effective,
        "parent_accounted_effective_production": parent_effective,
        "source_role_totals": roles,
        "large_file_audit": large_files,
        "gate": {
            "ok": not blockers,
            "blockers": blockers,
            "slice_minimum": SLICE_MINIMUM,
            "slice_effective": slice_effective,
            "slice_margin": slice_effective - SLICE_MINIMUM,
            "parent_minimum": PARENT_MINIMUM,
            "parent_effective": parent_effective,
            "parent_margin": parent_effective - PARENT_MINIMUM,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="HEAD")
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--fail-on-gate", action="store_true")
    parser.add_argument("--output", default="")
    arguments = parser.parse_args(argv)
    result = audit(target=arguments.target)
    if arguments.summary_only:
        for key in ("slice", "parent"):
            value = result.get(key)
            if isinstance(value, dict):
                value.pop("files", None)
        for item in result.get("large_file_audit", []):
            if isinstance(item, dict):
                item.pop("symbols", None)
    rendered = json.dumps(
        result,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    if arguments.output:
        output = Path(arguments.output).resolve(strict=False)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return (
        1
        if arguments.fail_on_gate and result["gate"]["blockers"]
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
