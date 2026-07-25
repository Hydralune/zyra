from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
for package_path in (ROOT / "packages" / "core", ROOT / "packages" / "integrations"):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations import InternalizationLedger, InternalizationLedgerEntry
from zyra_integrations.ledger_models import to_jsonable

HELPER_PATH = ROOT / "scripts" / "sync_m2_mcp_skill_subagent_source_ledger.py"
HELPER_SPEC = importlib.util.spec_from_file_location(
    "m2_experiment_evidence_ledger_helpers",
    HELPER_PATH,
)
if HELPER_SPEC is None or HELPER_SPEC.loader is None:
    raise RuntimeError("M2 experiment evidence ledger helper could not be loaded")
HELPER = importlib.util.module_from_spec(HELPER_SPEC)
HELPER_SPEC.loader.exec_module(HELPER)

DEFAULT_LEDGER = (
    ROOT
    / "packages"
    / "integrations"
    / "zyra_integrations"
    / "data"
    / "internalization_ledger_seed.json"
)
OWNER_UNIT = "M2-S05-03"
BASELINE_COMMIT = "b3387c626fc587936c24777ce108eac6f962283e"
DECISION_COMMIT = "5d50f78"
STAMP = "2026-07-26T00:00:00.000Z"
TARGETS = [
    "packages/evaluation/zyra_evaluation/experiment_runtime/runtime.py",
    "packages/evaluation/zyra_evaluation/experiment_runtime/workload.py",
    "packages/evaluation/zyra_evaluation/experiment_runtime/statistics.py",
    "packages/evaluation/zyra_evaluation/experiment_runtime/bundle.py",
    "packages/evaluation/zyra_evaluation/experiment_runtime/portfolio.py",
    "packages/evaluation/zyra_evaluation/experiment_runtime/source_roles.py",
    "apps/api/zyra_api/experiment_api.py",
    "apps/web/src/features/experiments/runtime.ts",
    "apps/web/src/features/experiments/view/experiment-workbench.tsx",
]
TESTS = [
    "tests/scenarios/test_m2_s05_03_experiment_matrix.py",
    "tests/integration/test_experiment_evidence_api_main_path.py",
    "apps/web/test/experiment-evidence-workbench.test.ts",
]


def _entry(implementation_commit: str) -> dict[str, Any]:
    decision = {
        "source_repo": "zyra",
        "source_commit": BASELINE_COMMIT,
        "source_language": "python/typescript/tsx",
        "target_language": "python/typescript/tsx",
        "source_path": (
            "packages/evaluation;packages/runtime;packages/memory;"
            "packages/scheduler;packages/orchestration;apps/web/src"
        ),
        "capability_name": "experiment_matrix_metrics_bundle_and_m2_exit_owner",
        "capability_summary": (
            "Durable seven-variant experiment orchestration, checksum-bound "
            "raw samples, P50/P95/dispersion/confidence aggregation, isolated "
            "ablation verification, requirement evidence mapping, reviewer "
            "navigation, tamper-evident experiment bundles and M2 exit portfolio."
        ),
        "targets": TARGETS,
        "source_role": "primary_implementation",
        "migration_mode": "scenario_and_evidence_integration_only",
        "migration_strategy": "direct_port",
        "rationale": (
            "M2-S05-03 assigns a new Zyra-owned evaluation/evidence state domain. "
            "It composes existing Python and TypeScript owners without selecting "
            "or migrating an upstream experiment runtime."
        ),
    }
    value = HELPER.entry(decision)
    value.update(
        {
            "owner_unit": OWNER_UNIT,
            "created_at": STAMP,
            "updated_at": STAMP,
            "downstream_units": ["M2-05", "M2", "M3"],
            "replacement_plan": (
                "Replace behind ExperimentMatrixRuntime, ExperimentApi and "
                "ExperimentWorkbenchRuntime while retaining durable envelope, "
                "sample, report and bundle contracts."
            ),
            "risk_notes": [
                "ExperimentMatrixRuntime is the only experiment/sample state owner.",
                "Task, scheduler, memory, recovery and artifact custody remains in M1 owners.",
                "All seven variants share one immutable envelope and source archive.",
                "No authenticated provider/model CLI or new external model request is allowed.",
                "Browser detach never cancels backend-owned matrix work.",
                "OpenClaw remains excluded_forward_only with no new ledger entry.",
                "No runtime path depends on a parent source repository.",
            ],
            "main_path": {
                "surfaces": [
                    "experiment_api",
                    "formal_experiment_runner",
                    "experiment_workbench",
                    "competition_bundle_export",
                ],
                "event_types": [
                    "experiment lifecycle",
                    "cell transition",
                    "raw metric sample",
                    "verification receipt",
                    "bundle commit",
                ],
                "api_routes": [
                    "/experiments/registry",
                    "/experiments/runs",
                    "/experiments/runs/{experiment_id}/report",
                    "/experiments/runs/{experiment_id}/samples",
                    "/experiments/runs/{experiment_id}/bundle",
                ],
                "control_commands": [
                    "create",
                    "start",
                    "verify",
                    "archive",
                ],
                "artifact_kinds": [
                    "experiment_evidence_bundle",
                    "m2_exit_evidence_portfolio",
                ],
                "worker_runtime": (
                    "ExperimentMatrixRuntime -> EvidenceBackedWorkloadRuntime "
                    "-> MetricExtractor -> DistributionAggregator -> "
                    "EvidenceBundleBuilder"
                ),
                "ui_panels": ["experiments", "metrics", "requirements", "evidence"],
            },
            "runtime_entry": {
                "module": "zyra_evaluation.experiment_runtime.runtime",
                "function": "ExperimentMatrixRuntime",
                "protocol": "zyra.experiment.api/v1",
                "health_check": (
                    "python -m pytest "
                    "tests/scenarios/test_m2_s05_03_experiment_matrix.py -q"
                ),
                "command": (
                    "python scripts/run_m2_s05_03_experiments.py "
                    "--commit <implementation-commit>"
                ),
                "config_refs": TARGETS,
                "environment_refs": [],
            },
            "test_entries": [
                {
                    "path": path,
                    "command": (
                        f"python -m pytest {path} -q"
                        if path.endswith(".py")
                        else f"bun test ./{path}"
                    ),
                    "kind": "integration",
                    "expected_signal": (
                        "same-condition matrix, isolated ablation effects, "
                        "raw distribution metrics, durable backend custody, "
                        "tamper rejection and no-log reviewer navigation"
                    ),
                    "required": True,
                }
                for path in TESTS
            ],
            "tags": [
                "m2-05",
                "m2-s05-03",
                "experiment",
                "ablation",
                "metrics",
                "evidence-bundle",
                "primary_implementation",
            ],
            "metadata": {
                "owner_unit": OWNER_UNIT,
                "slice_id": OWNER_UNIT,
                "source_role": "primary_implementation",
                "source_commit": BASELINE_COMMIT,
                "source_language": "python/typescript/tsx",
                "target_language": "python/typescript/tsx",
                "migration_mode": "scenario_and_evidence_integration_only",
                "canonical_experiment_owner": "python.ExperimentMatrixRuntime",
                "canonical_metric_owner": "python.DistributionAggregator",
                "canonical_bundle_owner": "python.EvidenceBundleBuilder",
                "canonical_frontend_projection": (
                    "typescript.ExperimentProjectionStore"
                ),
                "root_source_runtime_dependency": False,
                "decision_commit": DECISION_COMMIT,
                "implementation_commit": implementation_commit,
                "baseline_commit": BASELINE_COMMIT,
                "rationale": decision["rationale"],
            },
        }
    )
    value["source_evidence"][0]["reason"] = (
        f"{OWNER_UNIT} preimplementation source-role decision"
    )
    value["source_evidence"][0]["tags"] = [
        "m2-05",
        "primary_implementation",
    ]
    value["license_notice"] = {
        "source_repo": "zyra",
        "status": "recorded",
        "license_hint": "Zyra-owned experiment/evidence implementation.",
        "notice_path": "third_party/NOTICE.md",
        "source_url": "",
        "notes": "No new upstream runtime or parent-repository dependency.",
    }
    return value


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _rewrite(document: Any, implementation_commit: str) -> Any:
    entries = document if isinstance(document, list) else document.get("entries")
    if not isinstance(entries, list):
        raise ValueError("ledger seed must be a list or contain entries")
    output = [
        item
        for item in entries
        if str(item.get("owner_unit") or "") != OWNER_UNIT
        and str((item.get("metadata") or {}).get("slice_id") or "") != OWNER_UNIT
    ]
    output.append(_entry(implementation_commit))
    if isinstance(document, list):
        return output
    typed = [InternalizationLedgerEntry.from_dict(item) for item in output]
    return {
        **document,
        "entries": output,
        "summary": to_jsonable(InternalizationLedger(typed).summary()),
    }


def _target_exists(commit: str, path: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}:{path}"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--implementation-commit", required=True)
    parser.add_argument("--write", action="store_true")
    arguments = parser.parse_args(argv)
    ledger = arguments.ledger.resolve()
    current = json.loads(ledger.read_text(encoding="utf-8"))
    expected = _rewrite(current, arguments.implementation_commit)
    aligned = _canonical(current) == _canonical(expected)
    if arguments.write and not aligned:
        ledger.write_text(
            _canonical(expected),
            encoding="utf-8",
            newline="\n",
        )
        aligned = True
    missing = [
        path
        for path in TARGETS
        if not _target_exists(arguments.implementation_commit, path)
    ]
    print(f"m2_experiment_evidence_ledger_aligned={str(aligned).lower()}")
    print("m2_experiment_evidence_ledger_entry_count=1")
    print(f"m2_experiment_evidence_missing_target_count={len(missing)}")
    for path in missing:
        print(f"target_error=missing:{path}")
    print(f"ledger_path={ledger}")
    return 0 if aligned and not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
