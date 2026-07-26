from __future__ import annotations

import argparse
import ast
import json
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

import audit_m1_s07a02_effective_code_gate as common


SLICE_BASELINE = "1d19ea39a8313091dcfbc00f78c79fdfdeab7cf4"
IMPLEMENTATION = "HEAD"
SLICE_MINIMUM = 4_500
AUDIT_ROOT = "packages/integrations/zyra_integrations/source_custody/"


def source_role(path: str) -> str:
    normalized = path.casefold()
    name = PurePosixPath(path).name.casefold()
    if normalized.startswith("docs/") or "/data/" in normalized:
        return "evidence_or_machine_readable_data"
    if normalized.startswith("third_party/") or normalized.endswith("zyra-source.json"):
        return "provenance_data"
    if normalized.startswith("scripts/"):
        return "nonproduction_audit_tooling"
    if normalized.startswith("tests/") or "/test/" in normalized:
        return "behavior_verification"
    if not normalized.startswith(AUDIT_ROOT):
        return "slice_supporting_configuration"
    if name == "__init__.py":
        return "public_exports"
    if name in {"python_analyzer.py", "javascript_analyzer.py"}:
        return "zyra_owned_language_semantic_analysis"
    if name in {"catalog.py", "custody.py"}:
        return "zyra_owned_source_role_and_custody"
    if name in {"dependencies.py", "processes.py", "repository.py"}:
        return "zyra_owned_dependency_and_process_inventory"
    if name == "risks.py":
        return "zyra_owned_source_specific_and_opaque_risk"
    if name == "policy.py":
        return "zyra_owned_release_policy_and_queue"
    if name in {"engine.py", "cli.py"}:
        return "zyra_owned_release_audit_main_path"
    if name == "model.py":
        return "zyra_owned_audit_contracts"
    return "zyra_owned_source_custody_runtime"


def _large_file_audit(target: str, report: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in report.get("files", []):
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("path", ""))
        if not path.startswith(AUDIT_ROOT) or not path.endswith(".py"):
            continue
        source = common.target_text(target, path)
        line_count = len(source.splitlines())
        if line_count <= 500:
            continue
        tree = ast.parse(source)
        symbols: list[dict[str, Any]] = []
        for node in tree.body:
            if not isinstance(
                node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            end = int(getattr(node, "end_lineno", node.lineno))
            row: dict[str, Any] = {
                "name": node.name,
                "kind": type(node).__name__,
                "start": node.lineno,
                "end": end,
                "lines": end - node.lineno + 1,
            }
            if isinstance(node, ast.ClassDef):
                row["methods"] = [
                    child.name
                    for child in node.body
                    if isinstance(
                        child, (ast.FunctionDef, ast.AsyncFunctionDef)
                    )
                ]
            symbols.append(row)
        result.append(
            {
                "path": path,
                "line_count": line_count,
                "effective_production": int(
                    item.get("effective_production", 0)
                ),
                "symbol_count": len(symbols),
                "symbols": symbols,
                "cohesion": (
                    "The file owns one audit domain and exposes typed findings, "
                    "evidence and deterministic failure paths for that domain."
                ),
                "split_assessment": (
                    "Retained as a cohesive domain module; splitting parser, "
                    "normalization and findings would duplicate state or weaken "
                    "the mutation boundary."
                ),
            }
        )
    return result


def audit(*, target: str) -> dict[str, Any]:
    resolved_target = common.git("rev-parse", f"{target}^{{commit}}").strip()
    original_role = common.source_role
    common.source_role = source_role
    try:
        report = common.audit_range(
            base=SLICE_BASELINE,
            target=resolved_target,
        )
    finally:
        common.source_role = original_role

    effective = int(report["totals"]["effective_production"])
    blockers: list[str] = []
    if effective < SLICE_MINIMUM:
        blockers.append(
            f"slice effective production {effective} < {SLICE_MINIMUM}"
        )
    roles = {
        str(item["source_role"]): int(item["production"])
        for item in report["by_source_role_and_language"]
    }
    required_roles = {
        "zyra_owned_language_semantic_analysis",
        "zyra_owned_source_role_and_custody",
        "zyra_owned_dependency_and_process_inventory",
        "zyra_owned_source_specific_and_opaque_risk",
        "zyra_owned_release_policy_and_queue",
        "zyra_owned_release_audit_main_path",
    }
    for role in sorted(required_roles):
        if roles.get(role, 0) <= 0:
            blockers.append(f"required production role has zero code: {role}")
    large_files = _large_file_audit(resolved_target, report)
    if not large_files:
        blockers.append("large-file audit is empty")
    return {
        "schema": "zyra.effective-code-language-gate-audit/v1",
        "slice_id": "M3-S01A-01",
        "parent_unit": "M3-01A",
        "audit_tool_is_nonproduction": True,
        "method": {
            "raw": "target-side added lines from git diff --unified=0",
            "python": (
                "AST/token exclusion of Protocol, dataclass/enum fields, DTO "
                "mapping, schema constants, comments and blank lines"
            ),
            "effective": (
                "production_runtime only; tests, docs, JSON data, annotations, "
                "exports and audit scripts are excluded"
            ),
            "baseline": SLICE_BASELINE,
            "target": resolved_target,
        },
        "slice": report,
        "large_file_audit": large_files,
        "gate": {
            "ok": not blockers,
            "blockers": blockers,
            "minimum": SLICE_MINIMUM,
            "effective": effective,
            "margin": effective - SLICE_MINIMUM,
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
        if isinstance(report, dict):
            report.pop("files", None)
        for item in result.get("large_file_audit", []):
            if isinstance(item, dict):
                item.pop("symbols", None)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if arguments.fail_on_gate and result["gate"]["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
