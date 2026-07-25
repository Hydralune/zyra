from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from zyra_evaluation.experiment_runtime import (
    EvidenceArchiveLoader,
    ExperimentMatrixRuntime,
    ExperimentStore,
    M2ExitPortfolioBuilder,
    SourceRoleExitAuditor,
)
from zyra_evaluation.experiment_runtime.canonical import digest, pretty_json, utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "docs" / "reviews" / "evidence" / "M2-S05-03"
DEFAULT_WORK = PROJECT_ROOT / ".tmp" / "m2-s05-03-formal"
M1_EVIDENCE = (
    PROJECT_ROOT
    / "docs"
    / "reviews"
    / "evidence"
    / "M1-08-and-M1-exit-review-2026-07-23.json"
)
M2_LIVE_EVIDENCE = (
    PROJECT_ROOT
    / "docs"
    / "reviews"
    / "evidence"
    / "M2-S05-02"
    / "verification-summary.json"
)
SCENARIOS = (
    (
        "software-delivery",
        PROJECT_ROOT
        / "docs"
        / "reviews"
        / "evidence"
        / "M2-S05-02"
        / "software-causal-archive.zip",
    ),
    (
        "cross-source-research",
        PROJECT_ROOT
        / "docs"
        / "reviews"
        / "evidence"
        / "M2-S05-02"
        / "research-causal-archive.zip",
    ),
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the formal M2-S05-03 two-domain seven-variant experiment "
            "matrix without provider/model CLI or external model requests."
        )
    )
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK)
    parser.add_argument(
        "--seeds",
        default="117,311,911",
        help="Comma-separated unique non-negative integer seeds (minimum three).",
    )
    parser.add_argument(
        "--scenario",
        choices=("all", "software-delivery", "cross-source-research"),
        default="all",
        help="Run both formal domains by default; one-domain mode is diagnostic.",
    )
    return parser.parse_args()


def _seeds(value: str) -> list[int]:
    values = [int(item.strip()) for item in value.split(",") if item.strip()]
    if len(values) < 3 or len(values) != len(set(values)) or min(values) < 0:
        raise ValueError("At least three unique non-negative seeds are required.")
    return values


def _envelope(
    *,
    source: Any,
    commit: str,
    seeds: list[int],
    scenario_name: str,
) -> dict[str, Any]:
    return {
        "scenario_id": f"m2-exit.{scenario_name}",
        "scenario_definition_digest": digest(
            {
                "scenario": scenario_name,
                "source_archive_digest": source.archive_digest,
                "matrix": "M2-S05-03/v1",
            }
        ),
        "task_input_digest": source.input_digest,
        "task_input_bytes": max(1, len(source.input_digest)),
        "task_domain": source.domain,
        "commit_sha": commit,
        "environment_digest": digest(
            {
                "profile": "m2-s05-03-controlled-windows-x64",
                "runtime": "python+typescript-workbench",
                "provider_calls": False,
                "source_archive": source.archive_digest,
            }
        ),
        "source_evidence_digest": source.archive_digest,
        "sealed_policy_digest": digest(
            {
                "mode": "sealed_autonomous",
                "low_risk": "deterministic_allowlist",
                "high_or_unknown": "deny_recover_replan",
                "human_intervention_count": 0,
            }
        ),
        "budget": {
            "maximum_effective_steps": 100_000,
            "maximum_wall_time_ms": 30 * 60 * 1000,
            "maximum_token_units": 10_000_000_000,
            "maximum_cost_microunits": 100_000_000_000,
            "maximum_artifact_bytes": 2 * 1024 * 1024 * 1024,
            "maximum_fault_retries": 100,
            "concurrency": 2,
        },
        "hardware": {
            "profile_id": "m2-s05-03-windows-x64",
            "os_family": "windows",
            "architecture": "x86_64",
            "cpu_class": "controlled-local-evaluation",
            "logical_cpu_count": 2,
            "memory_limit_bytes": 8 * 1024 * 1024 * 1024,
            "edge_isolation_kind": "frozen-m1-external-evidence",
            "cloud_execution_allowed": False,
            "metadata": {
                "new_cloud_dispatch": False,
                "same_hardware_envelope_for_all_cells": True,
            },
        },
        "provider": {
            "policy_id": "m2-exit-no-new-provider-call",
            "policy_digest": digest(
                {
                    "authenticated_provider_cli_allowed": False,
                    "external_model_request_allowed": False,
                    "m1_receipts": "frozen",
                }
            ),
            "provider_catalog_digest": digest(
                {
                    "catalog": "frozen-m1-verified",
                    "providers": ["openai", "anthropic"],
                }
            ),
            "allowed_provider_ids": ["openai", "anthropic"],
            "allowed_model_ids": ["gpt-5", "claude-sonnet"],
            "authenticated_provider_cli_allowed": False,
            "external_model_request_allowed": False,
            "credential_presence_digest": digest(
                {"credentials_read": False, "credentials_used": False}
            ),
            "prior_verified_receipt_ids": [
                "M1-08-real-local-edge-cloud",
                "M1-08-multi-provider-model",
            ],
            "metadata": {
                "new_provider_dispatch_claimed": False,
                "prior_evidence_path": str(
                    M1_EVIDENCE.relative_to(PROJECT_ROOT)
                ).replace("\\", "/"),
            },
        },
        "verifier": {
            "verifier_id": "zyra-m2-s05-03-fail-closed",
            "version": "1",
            "implementation_digest": digest(
                {
                    "module": "zyra_evaluation.experiment_runtime.verification",
                    "version": 1,
                }
            ),
            "rules_digest": digest(
                {
                    "source_live": True,
                    "same_conditions": True,
                    "isolated_ablation": True,
                    "raw_metrics": True,
                    "tamper_evident_bundle": True,
                }
            ),
            "required_checks": [
                "source_archive",
                "matrix_plan",
                "same_conditions",
                "workload_observation",
                "isolated_ablation_effect",
                "metric_aggregation",
                "requirement_evidence",
                "report",
                "bundle",
            ],
            "fail_closed": True,
        },
        "failure_schedule": {
            "schedule_id": "m2-exit-representative-faults-v1",
            "schedule_digest": digest(
                {
                    "fault_kinds": [
                        "timeout",
                        "node_loss",
                        "requirement_change",
                        "route_failure",
                        "artifact_conflict",
                    ],
                    "injection_offsets": [64, 257, 1024],
                    "deterministic": True,
                }
            ),
            "fault_kinds": [
                "timeout",
                "node_loss",
                "requirement_change",
                "route_failure",
                "artifact_conflict",
            ],
            "requirement_change_ids": [
                "scope-change-1",
                "acceptance-change-1",
            ],
            "injection_offsets": [64, 257, 1024],
            "deterministic": True,
            "metadata": {
                "source_faults_remain_canonical": True,
                "same_schedule_for_all_cells": True,
            },
        },
        "seeds": seeds,
        "labels": {
            "slice": "M2-S05-03",
            "scenario": scenario_name,
            "formal": "true",
        },
        "metadata": {
            "execution_kind": "controlled_live_evidence_workload",
            "fixture": False,
            "replay": False,
            "browser_connection_required": False,
        },
    }


def _external_evidence(commit: str) -> dict[str, Any]:
    return {
        "REQ-EDGE-01": {
            "path": str(M1_EVIDENCE.relative_to(PROJECT_ROOT)).replace("\\", "/"),
            "claim": "Frozen real local, isolated edge and cloud dispatch evidence.",
            "commit": commit,
        },
        "SCORE-COMPAT": {
            "path": str(M1_EVIDENCE.relative_to(PROJECT_ROOT)).replace("\\", "/"),
            "claim": "Frozen provider and model compatibility evidence.",
            "commit": commit,
        },
        "SCORE-TASKS": {
            "path": str(M2_LIVE_EVIDENCE.relative_to(PROJECT_ROOT)).replace("\\", "/"),
            "claim": "Frozen software and research live scenario evidence.",
            "commit": commit,
        },
    }


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pretty_json(value) + "\n", encoding="utf-8")


def main() -> int:
    args = _parse_args()
    seeds = _seeds(args.seeds)
    output = args.output.resolve(strict=False)
    work = args.work.resolve(strict=False)
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(
            f"Output must be absent or empty for a clean formal run: {output}"
        )
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    loader = EvidenceArchiveLoader(
        allowed_roots=(PROJECT_ROOT, output, work),
    )
    store = ExperimentStore(work / "experiments.sqlite3")
    runtime = ExperimentMatrixRuntime(
        project_root=PROJECT_ROOT,
        store=store,
        artifact_root=work / "artifacts",
        allowed_source_roots=(PROJECT_ROOT, output, work),
        maximum_workers=2,
        enable_ablation_verifier=True,
        enable_metric_aggregator=True,
        enable_evidence_verifier=True,
        auto_reconcile=True,
    )
    run_records: dict[str, Any] = {}
    bundle_paths: list[Path] = []
    try:
        selected_scenarios = tuple(
            item
            for item in SCENARIOS
            if args.scenario == "all" or item[0] == args.scenario
        )
        for scenario_name, archive_path in selected_scenarios:
            source = loader.load(archive_path)
            print(
                json.dumps(
                    {
                        "phase": "domain_start",
                        "scenario": scenario_name,
                        "canonical_event_count": len(source.events),
                        "matrix_cell_count": 7 * len(seeds),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            admitted = runtime.create(
                {
                    "title": f"M2 exit {scenario_name} controlled matrix",
                    "source_archive_path": str(archive_path),
                    "envelope": _envelope(
                        source=source,
                        commit=args.commit,
                        seeds=seeds,
                        scenario_name=scenario_name,
                    ),
                    "external_evidence": _external_evidence(args.commit),
                    "screenshot_index": {
                        "screenshots": [],
                        "note": (
                            "Behavior and reviewer navigation are verified by "
                            "the M2 workbench build/tests; screenshots are not "
                            "runtime correctness inputs."
                        ),
                    },
                    "requested_by": "M2-S05-03-formal-runner",
                }
            )
            completed = runtime.start(
                admitted.experiment_id,
                wait=True,
                timeout=30 * 60,
            )
            if completed.phase.value != "succeeded":
                raise RuntimeError(
                    f"{scenario_name} experiment ended in {completed.phase.value}: "
                    f"{completed.failure}"
                )
            verification = runtime.verify(completed.experiment_id)
            bundle = runtime.bundle(completed.experiment_id)
            report = runtime.report(completed.experiment_id)
            status = runtime.status(completed.experiment_id)
            samples = runtime.raw_samples(
                completed.experiment_id,
                limit=100_000,
            )
            target_bundle = output / f"{scenario_name}-experiment-evidence.zip"
            shutil.copyfile(bundle["bundle_path"], target_bundle)
            bundle_paths.append(target_bundle)
            _write(output / f"{scenario_name}-report.json", report)
            _write(output / f"{scenario_name}-status.json", status)
            _write(output / f"{scenario_name}-raw-samples.json", samples)
            _write(
                output / f"{scenario_name}-verification.json",
                verification,
            )
            run_records[scenario_name] = {
                "experiment_id": completed.experiment_id,
                "scenario_run_id": source.scenario_run_id,
                "owner_run_id": source.owner_run_id,
                "task_id": source.task_id,
                "domain": source.domain,
                "source_archive_digest": source.archive_digest,
                "canonical_event_count": len(source.events),
                "fault_count": len(source.fault_events),
                "repetition_count": completed.repetitions,
                "variant_count": len(completed.variants),
                "cell_count": len(completed.cells),
                "raw_sample_count": status["sample_count"],
                "report_digest": report["report_digest"],
                "bundle_sha256": bundle["bundle_sha256"],
                "bundle_manifest_digest": bundle["manifest_digest"],
                "verification_receipt_digest": verification["receipt_digest"],
                "human_intervention_count": 0,
            }
            print(
                json.dumps(
                    {
                        "phase": "domain_succeeded",
                        "scenario": scenario_name,
                        "experiment_id": completed.experiment_id,
                        "raw_sample_count": status["sample_count"],
                        "bundle_sha256": bundle["bundle_sha256"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        if len(run_records) < 2:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "diagnostic_only": True,
                        "output": str(output),
                        "experiments": len(run_records),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        source_audit = SourceRoleExitAuditor(
            project_root=PROJECT_ROOT
        ).require_valid()
        portfolio = M2ExitPortfolioBuilder(
            artifact_root=work / "artifacts",
        ).build(
            experiment_bundles=bundle_paths,
            m1_exit_evidence=M1_EVIDENCE,
            m2_live_evidence=M2_LIVE_EVIDENCE,
            implementation_commit=args.commit,
            source_role_disposition=source_audit,
            schedule={
                "official_submission_deadline": "2026-09-15",
                "m2_exit_date": "2026-07-26",
                "m3_focus": [
                    "reverification",
                    "packaging",
                    "deployment rehearsal",
                    "submission buffer",
                ],
                "buffer_preserved": True,
            },
        )
        portfolio_target = output / "M2-exit-competition-evidence-bundle.zip"
        shutil.copyfile(portfolio["path"], portfolio_target)
        _write(output / "source-role-disposition.json", source_audit)
        _write(output / "M2-exit-portfolio-result.json", portfolio)
        summary = {
            "schema": "zyra.m2-s05-03-formal-experiment-summary/v1",
            "slice": "M2-S05-03",
            "implementation_commit": args.commit,
            "generated_at": utc_now(),
            "runs": run_records,
            "totals": {
                "domain_count": len(run_records),
                "canonical_event_count": sum(
                    item["canonical_event_count"]
                    for item in run_records.values()
                ),
                "matrix_cell_count": sum(
                    item["cell_count"] for item in run_records.values()
                ),
                "raw_sample_count": sum(
                    item["raw_sample_count"] for item in run_records.values()
                ),
                "human_intervention_count": 0,
            },
            "portfolio": {
                "path": portfolio_target.name,
                "sha256": portfolio["sha256"],
                "manifest_digest": portfolio["manifest"]["manifest_digest"],
                "root_digest": portfolio["manifest"]["root_digest"],
                "score_total": portfolio["manifest"]["score_total"],
                "score_verified": portfolio["manifest"]["score_verified"],
                "verification_receipt_digest": portfolio["verification"][
                    "receipt_digest"
                ],
            },
            "user_boundary": {
                "authenticated_provider_cli_allowed": False,
                "authenticated_provider_cli_invoked": False,
                "external_model_request_allowed": False,
                "external_model_request_made": False,
                "network_listener_started": False,
                "background_service_started": False,
            },
            "summary_digest": "",
        }
        summary["summary_digest"] = digest(
            {key: value for key, value in summary.items() if key != "summary_digest"}
        )
        _write(output / "verification-summary.json", summary)
        print(
            json.dumps(
                {
                    "ok": True,
                    "output": str(output),
                    "experiments": len(run_records),
                    "portfolio_sha256": portfolio["sha256"],
                    "summary_digest": summary["summary_digest"],
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        runtime.close(wait=True)


if __name__ == "__main__":
    raise SystemExit(main())
