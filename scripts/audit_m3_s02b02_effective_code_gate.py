from __future__ import annotations

import argparse
import ast
import json
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import audit_m1_s07a02_effective_code_gate as common


SLICE_ID = "M3-S02B-02"
SLICE_BASELINE = "1774cd21fa430f0d626987a80dabc62d51ff9048"
SLICE_MINIMUM = 6_000
PRODUCTIZATION_ROOT = "packages/productization/zyra_productization/release/"
API_COMPOSITION = "apps/api/zyra_api/main.py"
DEPLOYMENT_ORCHESTRATOR = (
    "packages/orchestration/zyra_orchestration/deployment/orchestrator.py"
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
    if normalized in {API_COMPOSITION, DEPLOYMENT_ORCHESTRATOR}:
        return "zyra_owned_installed_release_lifecycle_integration"
    if not normalized.startswith(PRODUCTIZATION_ROOT):
        return "release_supporting_configuration_or_lock"
    if name in {"__init__.py", "errors.py", "models.py"}:
        return "schema_declaration_or_public_exports"
    if name in {"policy.py", "integrity.py"}:
        return "zyra_owned_release_boundary_and_integrity"
    if name == "inventory.py":
        return "zyra_owned_sbom_notice_config_and_runtime_inventory"
    if name == "transactions.py":
        return "zyra_owned_transaction_migration_rollback_uninstall"
    if name in {"bundle.py", "wheel.py"}:
        return "zyra_owned_reproducible_bundle_and_wheel"
    if name == "cleanroom.py":
        return "zyra_owned_cross_platform_clean_install_lifecycle"
    if name in {"ci.py", "runtime.py", "cli.py"}:
        return "zyra_owned_release_admission_and_control_surface"
    if name == "submission.py":
        return "zyra_owned_submission_and_evidence_binding"
    return "zyra_owned_release_productization"


def accounted(role: str) -> bool:
    return role.startswith("zyra_owned_")


def large_file_audit(
    target: str,
    report: Mapping[str, Any],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in report.get("files", ()):
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("source_role") or "")
        path = str(item.get("path") or "")
        if not accounted(role) or not path.endswith(".py"):
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
                    "The module owns one release boundary: integrity, "
                    "inventory, transaction, bundle, cleanroom or admission."
                ),
                "split_assessment": (
                    "State invariants and their fail-closed paths remain "
                    "co-located without creating a second product owner."
                ),
            }
        )
    return output


def audit(*, target: str) -> dict[str, Any]:
    resolved = common.git("rev-parse", f"{target}^{{commit}}").strip()
    original = common.source_role
    common.source_role = source_role
    try:
        report = common.audit_range(base=SLICE_BASELINE, target=resolved)
    finally:
        common.source_role = original
    roles: dict[str, int] = {}
    for item in report.get("files", ()):
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("source_role") or "")
        if accounted(role):
            roles[role] = roles.get(role, 0) + int(
                item.get("effective_production") or 0
            )
    effective = sum(roles.values())
    required = {
        "zyra_owned_release_boundary_and_integrity",
        "zyra_owned_sbom_notice_config_and_runtime_inventory",
        "zyra_owned_transaction_migration_rollback_uninstall",
        "zyra_owned_reproducible_bundle_and_wheel",
        "zyra_owned_cross_platform_clean_install_lifecycle",
        "zyra_owned_release_admission_and_control_surface",
        "zyra_owned_submission_and_evidence_binding",
        "zyra_owned_installed_release_lifecycle_integration",
    }
    blockers: list[str] = []
    if effective < SLICE_MINIMUM:
        blockers.append(
            f"slice effective production {effective} < {SLICE_MINIMUM}"
        )
    for role in sorted(required):
        if roles.get(role, 0) <= 0:
            blockers.append(f"required production role has zero code: {role}")
    large = large_file_audit(resolved, report)
    if not large:
        blockers.append("large-file cohesion audit is empty")
    return {
        "schema": "zyra.effective-code-language-gate-audit/v1",
        "slice_id": SLICE_ID,
        "parent_unit": "M3-02B",
        "audit_tool_is_nonproduction": True,
        "method": {
            "baseline": SLICE_BASELINE,
            "target": resolved,
            "raw": "target-side additions from git diff --unified=0",
            "python": (
                "AST/token exclusion of declarations, schema-only fields, "
                "comments, blank lines and non-behavioral mappings"
            ),
            "excluded": (
                "requirements lock, generated bundle/SBOM/report, tests, "
                "docs, CI YAML and audit/runner scripts"
            ),
            "tests_docs_scripts_count_toward_minimum": False,
        },
        "slice": report,
        "accounted_effective_production": effective,
        "source_role_totals": dict(sorted(roles.items())),
        "large_file_audit": large,
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
    parser.add_argument("--target", default="HEAD")
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--fail-on-gate", action="store_true")
    parser.add_argument("--output", default="")
    arguments = parser.parse_args(argv)
    result = audit(target=arguments.target)
    if arguments.summary_only:
        slice_report = result.get("slice")
        if isinstance(slice_report, dict):
            slice_report.pop("files", None)
        for item in result.get("large_file_audit", []):
            if isinstance(item, dict):
                item.pop("symbols", None)
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if arguments.output:
        output = Path(arguments.output).resolve(strict=False)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 1 if arguments.fail_on_gate and result["gate"]["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
