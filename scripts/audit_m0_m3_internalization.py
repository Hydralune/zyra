from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TEXT_EXTENSIONS = {
    "",
    ".css",
    ".csv",
    ".example",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".py",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}


REQUIRED_PATHS: dict[str, tuple[str, ...]] = {
    "m0": (
        "pyproject.toml",
        "packages/core/zyra_core/models.py",
        "packages/core/zyra_core/event_log.py",
        "apps/api/zyra_api/main.py",
        "apps/web/index.html",
        "scripts/verify_m0.py",
    ),
    "m1": (
        "packages/orchestration/zyra_orchestration/task_graph.py",
        "packages/memory/zyra_memory/sqlite_store.py",
        "tests/integration/test_sqlite_store.py",
        "scripts/verify_m1.py",
    ),
    "m2": (
        "vendor/claude-code-best/src/QueryEngine.ts",
        "vendor/claude-code-best/src/query.ts",
        "vendor/browser-use/browser_use/agent",
        "vendor/browser-use/browser_use/browser",
        "apps/code-worker/src/main.mjs",
        "packages/integrations/zyra_integrations/vendor_manifest.py",
        "packages/runtime/zyra_runtime/executor.py",
        "packages/runtime/zyra_runtime/permissions.py",
        "packages/runtime/zyra_runtime/session.py",
        "packages/workers/zyra_workers/code_query_loop.py",
        "packages/workers/zyra_workers/code_worker_runtime.py",
        "packages/workers/zyra_workers/browser_worker.py",
        "packages/workers/zyra_workers/browser_use_runtime.py",
        "tests/integration/test_code_worker_sidecar.py",
        "tests/scenarios/test_m2_runtime_acceptance.py",
        "scripts/verify_m2.py",
    ),
    "m3": (
        "packages/symbolic/zyra_symbolic/constraints.py",
        "packages/symbolic/zyra_symbolic/control.py",
        "packages/symbolic/zyra_symbolic/topology.py",
        "packages/orchestration/zyra_orchestration/task_graph.py",
        "packages/evaluation/zyra_evaluation/trace.py",
        "tests/scenarios/test_m3_symbolic_collaboration.py",
        "scripts/verify_m3.py",
    ),
}


DEEP_INTERNALIZATION_DEBTS = (
    {
        "area": "claude-code-best QueryEngine",
        "current_state": "Vendored source and sidecar contract are present; Zyra runs a contract-backed Python loop.",
        "needed_later": "M4-M6 should migrate or encapsulate more of the real QueryEngine/session/permission/compact runtime.",
    },
    {
        "area": "permission, skills, subagent, MCP",
        "current_state": "Zyra has registries, permission policy, inventory, and skill invocation events.",
        "needed_later": "Move beyond inventory/metadata into executable SkillTool, AgentTool/subagent, MCP client, and permission handlers.",
    },
    {
        "area": "memory and compaction",
        "current_state": "M2 has query context budget artifacts and stateful context sessions.",
        "needed_later": "M4 must build MemoryFabric, retrieval, trajectory replay, compact restore, and skill memory as first-class modules.",
    },
    {
        "area": "scheduler and fault recovery",
        "current_state": "M3 routes workers and records failure-recovery decisions; scheduler package is not yet implemented.",
        "needed_later": "M5 must add WorkerManifest, ResourceScheduler, local/docker/cloud resources, watchdogs, and recovery policies.",
    },
    {
        "area": "control console",
        "current_state": "The current web app is a static connected console shell.",
        "needed_later": "M6 must internalize a full control console with graph, timeline, artifacts, diff/browser/terminal panes, and live control input.",
    },
)


@dataclass(frozen=True, slots=True)
class LineCounts:
    tracked_total: int
    tracked_vendor: int
    tracked_non_vendor: int
    tracked_non_vendor_source: int


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit M0-M3 against Zyra heavyweight internalization goals.")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable JSON report.")
    parser.add_argument("--fail-on-debt", action="store_true", help="Exit non-zero when known internalization debt remains.")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    report = build_report(root)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_markdown(report))

    if report["fatal"]:
        raise SystemExit(1)
    if args.fail_on_debt and report["status"] == "pass_with_debt":
        raise SystemExit(2)


def build_report(root: Path) -> dict[str, Any]:
    files = tracked_files(root)
    counts = count_lines(root, files)
    milestone_checks = {milestone: check_paths(root, paths) for milestone, paths in REQUIRED_PATHS.items()}
    fatal = {
        milestone: check["missing"]
        for milestone, check in milestone_checks.items()
        if check["missing"]
    }
    debt = list(DEEP_INTERNALIZATION_DEBTS)
    if fatal:
        status = "fail"
    elif debt:
        status = "pass_with_debt"
    else:
        status = "pass"

    return {
        "status": status,
        "root": str(root),
        "line_counts": {
            "tracked_total": counts.tracked_total,
            "tracked_vendor": counts.tracked_vendor,
            "tracked_non_vendor": counts.tracked_non_vendor,
            "tracked_non_vendor_source": counts.tracked_non_vendor_source,
            "vendor_ratio": round(counts.tracked_vendor / counts.tracked_total, 4)
            if counts.tracked_total
            else 0.0,
        },
        "milestones": {
            "m0": {
                "assessment": "accepted_intentionally_light",
                "reason": "M0 was allowed to establish schema, event log, API, and web shell without heavy reuse.",
            },
            "m1": {
                "assessment": "accepted_intentionally_light",
                "reason": "M1 was allowed to establish task graph and checkpoint control plane before runtime integration.",
            },
            "m2": {
                "assessment": "accepted_as_heavy_integration_start_not_deep_internalization",
                "reason": (
                    "M2 landed vendored claude-code-best/browser-use code, CodeWorker sidecar, "
                    "contract-backed query loop, BrowserWorker live/agent boundaries, tools, permissions, "
                    "commands, skills, artifacts, and runtime tests. It did not complete deep internalization."
                ),
            },
            "m3": {
                "assessment": "accepted_as_symbolic_control_layer_with_internalization_debt",
                "reason": (
                    "M3 connected structured messages, ConstraintKeeper, TopologyRouter, requirement changes, "
                    "failure injection, and evaluation metrics to the main graph. It was not a large code-ingestion milestone."
                ),
            },
        },
        "required_path_checks": milestone_checks,
        "debt": debt,
        "fatal": fatal,
    }


def tracked_files(root: Path) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "ls-files"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        return [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.CalledProcessError):
        files: list[str] = []
        for path in root.rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                files.append(path.relative_to(root).as_posix())
        return sorted(files)


def count_lines(root: Path, files: list[str]) -> LineCounts:
    tracked_total = 0
    tracked_vendor = 0
    tracked_non_vendor = 0
    tracked_non_vendor_source = 0
    for relative in files:
        if should_skip(relative):
            continue
        path = root / relative
        if not path.is_file() or not looks_textual(path):
            continue
        line_count = count_file_lines(path)
        tracked_total += line_count
        if relative.startswith("vendor/"):
            tracked_vendor += line_count
        else:
            tracked_non_vendor += line_count
            if path.suffix.lower() in {".py", ".mjs", ".js", ".ts", ".tsx", ".html", ".css"}:
                tracked_non_vendor_source += line_count
    return LineCounts(
        tracked_total=tracked_total,
        tracked_vendor=tracked_vendor,
        tracked_non_vendor=tracked_non_vendor,
        tracked_non_vendor_source=tracked_non_vendor_source,
    )


def check_paths(root: Path, paths: tuple[str, ...]) -> dict[str, Any]:
    present: list[str] = []
    missing: list[str] = []
    for relative in paths:
        target = root / relative
        if target.exists():
            present.append(relative)
        else:
            missing.append(relative)
    return {"present": present, "missing": missing}


def should_skip(relative: str) -> bool:
    parts = set(relative.replace("\\", "/").split("/"))
    return "__pycache__" in parts or relative.endswith(".pyc") or relative.startswith("tmp/")


def looks_textual(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTENSIONS


def count_file_lines(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            return sum(1 for _line in handle)
    except OSError:
        return 0


def format_markdown(report: dict[str, Any]) -> str:
    counts = report["line_counts"]
    lines = [
        "# M0-M3 Heavyweight Internalization Audit",
        "",
        f"- status: `{report['status']}`",
        f"- tracked_total_lines: `{counts['tracked_total']}`",
        f"- tracked_vendor_lines: `{counts['tracked_vendor']}`",
        f"- tracked_non_vendor_lines: `{counts['tracked_non_vendor']}`",
        f"- tracked_non_vendor_source_lines: `{counts['tracked_non_vendor_source']}`",
        f"- vendor_ratio: `{counts['vendor_ratio']}`",
        "",
        "## Milestone Assessments",
        "",
    ]
    for milestone, data in report["milestones"].items():
        lines.extend(
            [
                f"### {milestone.upper()}",
                "",
                f"- assessment: `{data['assessment']}`",
                f"- reason: {data['reason']}",
                "",
            ]
        )
    lines.extend(["## Known Internalization Debt", ""])
    for item in report["debt"]:
        lines.extend(
            [
                f"### {item['area']}",
                "",
                f"- current_state: {item['current_state']}",
                f"- needed_later: {item['needed_later']}",
                "",
            ]
        )
    if report["fatal"]:
        lines.extend(["## Fatal Missing Paths", ""])
        for milestone, missing in report["fatal"].items():
            lines.append(f"- {milestone}: {', '.join(missing)}")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
