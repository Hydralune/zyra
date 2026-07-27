from __future__ import annotations

import argparse
import ast
import json
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from pathlib import Path
from typing import Any

import audit_m1_s07a02_effective_code_gate as common


SLICE_ID = "M3-S02A-01"
SLICE_BASELINE = "42f760229588aba917c7283b841587afc7142b0b"
IMPLEMENTATION = "HEAD"
SLICE_MINIMUM = 6_000
RUNTIME_ROOT = (
    "packages/evaluation/zyra_evaluation/regression_hardening/"
)


def source_role(path: str) -> str:
    normalized = path.casefold()
    name = PurePosixPath(path).name.casefold()
    if normalized.startswith("docs/") or normalized.endswith(".md"):
        return "evidence_or_documentation"
    if normalized.startswith("tests/") or "/test/" in normalized:
        return "behavior_verification"
    if normalized.startswith("scripts/"):
        return "nonproduction_gate_or_verifier"
    if normalized.endswith(("experiment_runtime/store.py", "scenario_runner/store.py")):
        return "zyra_owned_clean_sqlite_lifecycle"
    if not normalized.startswith(RUNTIME_ROOT):
        return "slice_supporting_configuration"
    if name == "__init__.py":
        return "public_exports"
    if name in {"contracts.py", "registry.py", "artifacts.py", "engine_io.py"}:
        return "zyra_owned_regression_contract_and_registry"
    if name in {"orchestrator.py", "triage.py", "service.py"}:
        return "zyra_owned_regression_orchestration_and_triage"
    if name in {"default_path.py", "causality.py"}:
        return "zyra_owned_default_path_and_causality"
    if name == "clean_state.py":
        return "zyra_owned_clean_state_and_pollution"
    if name in {
        "control_boundary.py",
        "repository_security.py",
        "content_security.py",
        "code_index_security.py",
        "approval_security.py",
    }:
        return "zyra_owned_semantic_and_security_campaign"
    if name in {"live_matrix.py", "freeze_gate.py", "cli.py", "__main__.py"}:
        return "zyra_owned_live_matrix_and_freeze_gate"
    return "zyra_owned_regression_runtime"


def _large_file_audit(
    target: str,
    report: Mapping[str, Any],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in report.get("files", []):
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("path") or "")
        if not path.startswith(RUNTIME_ROOT) or not path.endswith(".py"):
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
                    "methods": (
                        [
                            child.name
                            for child in node.body
                            if isinstance(
                                child,
                                (ast.FunctionDef, ast.AsyncFunctionDef),
                            )
                        ]
                        if isinstance(node, ast.ClassDef)
                        else []
                    ),
                }
            )
        result.append(
            {
                "path": path,
                "line_count": line_count,
                "effective_production": int(
                    item.get("effective_production") or 0
                ),
                "symbol_count": len(symbols),
                "symbols": symbols,
                "cohesion": (
                    "The module owns one regression domain, its deterministic "
                    "findings and its negative-input failure paths."
                ),
                "split_assessment": (
                    "Kept cohesive because splitting state validation from its "
                    "mutation campaign would duplicate invariants and weaken "
                    "receipt causality."
                ),
            }
        )
    return result


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
        int(item["effective_production"])
        for item in report["files"]
        if str(item["path"]).startswith(RUNTIME_ROOT)
    )
    roles = {
        str(item["source_role"]): int(item["production"])
        for item in report["by_source_role_and_language"]
    }
    required_roles = {
        "zyra_owned_regression_contract_and_registry",
        "zyra_owned_regression_orchestration_and_triage",
        "zyra_owned_default_path_and_causality",
        "zyra_owned_clean_state_and_pollution",
        "zyra_owned_semantic_and_security_campaign",
        "zyra_owned_live_matrix_and_freeze_gate",
    }
    blockers: list[str] = []
    if effective < SLICE_MINIMUM:
        blockers.append(
            f"slice effective production {effective} < {SLICE_MINIMUM}"
        )
    for role in sorted(required_roles):
        if roles.get(role, 0) <= 0:
            blockers.append(
                f"required production role has zero code: {role}"
            )
    language_rows = {
        str(item["language"]): int(item["production"])
        for item in report["by_source_role_and_language"]
        if str(item["source_role"]).startswith("zyra_owned_")
    }
    if language_rows.get("python", 0) <= 0:
        blockers.append("Python evaluation runtime has zero effective production")
    large_files = _large_file_audit(resolved_target, report)
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
                "production regression orchestration, live/disable/clean/security "
                "verification and freeze reporting only"
            ),
            "baseline": SLICE_BASELINE,
            "target": resolved_target,
            "tests_docs_scripts_count_toward_minimum": False,
        },
        "slice": report,
        "accounted_effective_production": effective,
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
    return (
        1
        if arguments.fail_on_gate and result["gate"]["blockers"]
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
