from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


REQUIRED_FILES = (
    "apps/code-worker/src/main.ts",
    "packages/runtime/claude-runtime/src/protocol.ts",
    "packages/runtime/claude-runtime/src/query-engine.ts",
    "packages/runtime/claude-runtime/src/session.ts",
    "packages/runtime/claude-runtime/src/tools.ts",
    "packages/runtime/claude-runtime/src/budget.ts",
    "packages/workers/zyra_workers/typescript_claude_runtime.py",
)


def audit(project_root: Path) -> dict[str, Any]:
    missing = [path for path in REQUIRED_FILES if not (project_root / path).is_file()]
    worker_text = (
        project_root
        / "packages"
        / "workers"
        / "zyra_workers"
        / "code_worker_runtime.py"
    ).read_text(encoding="utf-8")
    typescript_text = "\n".join(
        (project_root / path).read_text(encoding="utf-8")
        for path in REQUIRED_FILES
        if path.endswith(".ts") and (project_root / path).is_file()
    )
    findings: list[dict[str, str]] = []
    if "query_engine_factory: Any | None = TypeScriptClaudeQueryEngine" not in worker_text:
        findings.append(
            {
                "code": "default_owner_not_typescript",
                "message": "CodeWorkerRuntime default factory is not TypeScriptClaudeQueryEngine.",
            }
        )
    if "query_engine_factory: Any | None = ZyraClaudeQueryEngine" in worker_text:
        findings.append(
            {
                "code": "python_default_owner_present",
                "message": "The old Python QueryEngine remains a default owner.",
            }
        )
    if (project_root / "apps" / "code-worker" / "src" / "main.mjs").exists():
        findings.append(
            {
                "code": "legacy_inspection_sidecar_present",
                "message": "The legacy main.mjs inspection sidecar still exists.",
            }
        )
    for marker in (
        "../claude-code-best",
        "..\\claude-code-best",
        "vendor/claude-code-best",
        "vendor-runtimes/claude-code-runtime",
    ):
        if marker in typescript_text:
            findings.append(
                {
                    "code": "forbidden_source_dependency",
                    "message": f"TypeScript runtime contains forbidden source path: {marker}",
                }
            )
    return {
        "ok": not missing and not findings,
        "runtime_id": "zyra-typescript-claude-runtime",
        "canonical_owner": "typescript",
        "required_files": list(REQUIRED_FILES),
        "missing_files": missing,
        "findings": findings,
        "boundaries": {
            "query_loop": "typescript",
            "session_lifecycle": "typescript",
            "tool_registry_scheduler_budget": "typescript",
            "context_compact_restore": "typescript",
            "permission_and_tool_side_effects": "python",
            "event_artifact_checkpoint_projection": "python",
            "python_query_engine_fallback": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = audit(args.project_root.resolve())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("typescript runtime custody:", "PASS" if report["ok"] else "FAIL")
        for finding in report["findings"]:
            print(f"- {finding['code']}: {finding['message']}")
        for path in report["missing_files"]:
            print(f"- missing: {path}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
