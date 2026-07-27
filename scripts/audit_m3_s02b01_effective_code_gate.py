from __future__ import annotations

import argparse
import ast
import json
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import audit_m1_s07a02_effective_code_gate as common


SLICE_ID = "M3-S02B-01"
SLICE_BASELINE = "e1d828964f9b909d34c053c90780a9817f184997"
SLICE_MINIMUM = 6_000
DEPLOYMENT_ROOT = "packages/orchestration/zyra_orchestration/deployment/"
DEPLOYMENT_API = "apps/api/zyra_api/deployment_api.py"
API_COMPOSITION = "apps/api/zyra_api/main.py"
SANDBOX_FIX = (
    "packages/runtime/zyra_runtime/sandbox_gateway/integration_tools.py"
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
    if normalized in {DEPLOYMENT_API, API_COMPOSITION}:
        return "zyra_owned_deployment_control_surface"
    if normalized == SANDBOX_FIX:
        return "zyra_owned_sandbox_session_custody_fix"
    if not normalized.startswith(DEPLOYMENT_ROOT):
        return "slice_supporting_configuration"
    if name in {"__init__.py", "models.py", "errors.py"}:
        return "schema_declaration_or_public_exports"
    if name in {"profiles.py", "placement.py"}:
        return "zyra_owned_profile_policy_and_placement"
    if name in {
        "security.py",
        "ports.py",
        "resource_control.py",
        "node_runtime.py",
        "node_server.py",
        "node_client.py",
        "process_manager.py",
    }:
        return "zyra_owned_supervisor_node_and_security_runtime"
    if name in {
        "state_store.py",
        "dispatch.py",
        "handoff.py",
        "recovery.py",
    }:
        return "zyra_owned_dispatch_state_handoff_and_recovery"
    if name in {
        "clean_state.py",
        "doctor.py",
        "short_task.py",
        "semantic_health.py",
    }:
        return "zyra_owned_semantic_health_and_diagnostics"
    if name in {"orchestrator.py", "cli.py", "http_client.py", "evidence_gate.py"}:
        return "zyra_owned_deployment_product_orchestration"
    return "zyra_owned_deployment_runtime"


def is_accounted_role(role: str) -> bool:
    return role.startswith("zyra_owned_")


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
                    "The module owns one deployment lifecycle, state, "
                    "semantic-health, or fail-closed security responsibility."
                ),
                "split_assessment": (
                    "The boundary keeps its state invariants and failure "
                    "paths together without introducing another canonical owner."
                ),
            }
        )
    return output


def audit(*, target: str) -> dict[str, Any]:
    resolved_target = common.git(
        "rev-parse",
        f"{target}^{{commit}}",
    ).strip()
    original_role = common.source_role
    common.source_role = source_role
    try:
        report = common.audit_range(
            base=SLICE_BASELINE,
            target=resolved_target,
        )
    finally:
        common.source_role = original_role
    effective = sum(
        int(item.get("effective_production") or 0)
        for item in report.get("files", ())
        if isinstance(item, Mapping)
        and is_accounted_role(str(item.get("source_role") or ""))
    )
    roles: dict[str, int] = {}
    for item in report.get("files", ()):
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("source_role") or "")
        if is_accounted_role(role):
            roles[role] = roles.get(role, 0) + int(
                item.get("effective_production") or 0
            )
    required_roles = {
        "zyra_owned_deployment_control_surface",
        "zyra_owned_profile_policy_and_placement",
        "zyra_owned_supervisor_node_and_security_runtime",
        "zyra_owned_dispatch_state_handoff_and_recovery",
        "zyra_owned_semantic_health_and_diagnostics",
        "zyra_owned_deployment_product_orchestration",
        "zyra_owned_sandbox_session_custody_fix",
    }
    blockers: list[str] = []
    if effective < SLICE_MINIMUM:
        blockers.append(
            f"slice effective production {effective} < {SLICE_MINIMUM}"
        )
    for role in sorted(required_roles):
        if roles.get(role, 0) <= 0:
            blockers.append(f"required production role has zero code: {role}")
    large_files = _large_file_audit(resolved_target, report)
    if not large_files:
        blockers.append("large-file cohesion audit is empty")
    return {
        "schema": "zyra.effective-code-language-gate-audit/v1",
        "slice_id": SLICE_ID,
        "parent_unit": "M3-02B",
        "audit_tool_is_nonproduction": True,
        "method": {
            "raw": "target-side added lines from git diff --unified=0",
            "python": (
                "AST/token exclusion of Protocol, dataclass/enum fields, DTO "
                "mapping, schema constants, comments and blank lines"
            ),
            "effective": (
                "behavior-bearing deployment profiles, supervisor, node "
                "protocol, state, dispatch, recovery, semantic diagnostics, "
                "control surface and product lifecycle only"
            ),
            "baseline": SLICE_BASELINE,
            "target": resolved_target,
            "tests_docs_scripts_count_toward_minimum": False,
        },
        "slice": report,
        "accounted_effective_production": effective,
        "source_role_totals": dict(sorted(roles.items())),
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
    return 1 if arguments.fail_on_gate and result["gate"]["blockers"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
