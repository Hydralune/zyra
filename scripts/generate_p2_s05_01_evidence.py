from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for package_path in (
    PROJECT_ROOT,
    PROJECT_ROOT / "packages" / "core",
    PROJECT_ROOT / "packages" / "orchestration",
    PROJECT_ROOT / "packages" / "scheduler",
    PROJECT_ROOT / "packages" / "evaluation",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from tests.integration.test_policy_metrics_from_receipts import (
    _communication_receipts,
    _continuity_receipt,
    _dispatch,
    _header,
    _policy_receipts,
    _symbolic_bundle,
)
from zyra_evaluation.policy_benchmark import (
    ADAPTIVE_DEPTH_RECEIPTS,
    COMMUNICATION_RECEIPTS,
    CONTINUITY_RECEIPTS,
    EARLY_EXIT_RECEIPTS,
    PHYSICAL_DISPATCH_RECEIPTS,
    POLICY_DECISIONS,
    POLICY_OUTCOMES,
    READINESS_REPORTS,
    SYMBOLIC_BUNDLES,
    TOPOLOGY_PROPOSALS,
    InMemoryCanonicalReceiptResolver,
    Phase2MetricReportBuilder,
    RunMetricInput,
    canonical_digest,
    metric_spec_registry_payload,
)
from zyra_scheduler.operator_policy.adaptive_depth import (
    AdaptiveDepthCostReceipt,
)
from zyra_scheduler.operator_policy.early_exit import (
    ExitConditionResult,
    ExitDecision,
    ExitDecisionReceipt,
    ExitPosteriorResult,
)


SLICE_ID = "P2-S05-01"


def _readiness_report() -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema": "zyra.mechanism-evidence-readiness-report/v1",
        "mechanisms": {
            "arg": {
                "mechanism_id": "arg",
                "readiness_stage": "implementation_validated",
                "status": "deterministic_ready",
                "actual_mode": "default",
                "field_coverage": [
                    {
                        "field_id": "graph",
                        "required": True,
                        "sample_count": 1,
                        "missing_count": 0,
                        "coverage_ratio": 1.0,
                        "freshness_ratio": 1.0,
                        "confidence_ratio": 1.0,
                    },
                    {
                        "field_id": "optional_signal",
                        "required": False,
                        "sample_count": 1,
                        "missing_count": 0,
                        "coverage_ratio": 1.0,
                        "freshness_ratio": 1.0,
                        "confidence_ratio": 1.0,
                    },
                ],
                "scenario_coverage": {
                    "required": ["normal"],
                    "observed": ["normal"],
                },
                "failure_path_coverage": {
                    "required": ["projector_reject"],
                    "observed": ["projector_reject"],
                },
                "causal_links": {
                    "required": ["proposal", "decision", "commit"],
                    "completeness_ratio": 1.0,
                },
                "deterministic_input_snapshot_replay": {"passed": True},
            }
        },
        "no_policy_training_audit": {
            "passed": True,
            "training_sample_count": 0,
            "datasets": [],
            "checkpoints": [],
        },
    }
    return {**body, "report_digest": canonical_digest(body)}


def _metric_input() -> RunMetricInput:
    proposal, decision, outcome = _policy_receipts()
    exit_receipt = ExitDecisionReceipt(
        header=_header("exit-real", mechanism="maas_early_exit"),
        decision_id="exit-real",
        snapshot_id="snapshot-real",
        snapshot_digest="2" * 64,
        decision=ExitDecision.EXIT,
        conditions=(
            ExitConditionResult(
                condition_id="final_verifier",
                passed=True,
                reason="final verifier passed",
                evidence_refs=("verification-real",),
            ),
        ),
        confidence=1.0,
        avoided_operator_count=2,
        avoided_tokens=200,
        avoided_cost_usd=0.02,
        verifier_refs=("verification-real",),
        artifact_refs=("artifact-real",),
        posterior_result=ExitPosteriorResult.TRUE_EXIT,
        posterior_outcome_ref="outcome-real",
    )
    depth_receipt = AdaptiveDepthCostReceipt(
        proposal_id="operator-proposal-real",
        proposal_digest="3" * 64,
        decision_ref="exit-real",
        proposed_depth=3,
        executed_depth=2,
        proposed_operator_count=5,
        executed_operator_count=3,
        avoided_operator_refs=("operator-4", "operator-5"),
        actual_tokens=300,
        estimated_avoided_tokens=200,
        actual_cost_usd=0.03,
        estimated_avoided_cost_usd=0.02,
        actual_latency_ms=600,
        task_completed=True,
        verifier_passed=True,
        artifact_complete=True,
    )
    resolver = InMemoryCanonicalReceiptResolver(
        {
            COMMUNICATION_RECEIPTS: _communication_receipts(),
            TOPOLOGY_PROPOSALS: (proposal,),
            POLICY_DECISIONS: (decision,),
            POLICY_OUTCOMES: (outcome,),
            READINESS_REPORTS: (_readiness_report(),),
            CONTINUITY_RECEIPTS: (_continuity_receipt(),),
            SYMBOLIC_BUNDLES: (_symbolic_bundle(),),
            EARLY_EXIT_RECEIPTS: (exit_receipt,),
            ADAPTIVE_DEPTH_RECEIPTS: (depth_receipt,),
            PHYSICAL_DISPATCH_RECEIPTS: (
                _dispatch("local", 1),
                _dispatch("edge", 2),
                _dispatch("cloud", 3),
            ),
        }
    )
    return RunMetricInput(
        run_id="run-real-receipts",
        task_id="task-real-receipts",
        scenario_id="cross-domain-long-run",
        mechanism_profile="phase2_strongest_v1",
        receipt_resolver=resolver,
        task_succeeded=True,
        effective_transition_count=3,
    )


def _write(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--implementation-commit", required=True)
    arguments = parser.parse_args()
    output_dir = arguments.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    registry = metric_spec_registry_payload()
    report = Phase2MetricReportBuilder().build((_metric_input(),))
    report_payload = report.to_dict()
    run = report.runs[0]
    lineage_body = {
        "schema_version": "zyra.phase2-metric-lineage/v1",
        "slice_id": SLICE_ID,
        "implementation_commit": arguments.implementation_commit,
        "run_id": run.run_id,
        "task_id": run.task_id,
        "receipt_kinds": {
            kind: {
                "receipt_count": len(refs),
                "receipt_digests": list(refs),
            }
            for kind, refs in sorted(run.lineage.items())
        },
        "metric_sources": {
            metric_id: list(value.source_refs)
            for metric_id, value in sorted(run.metrics.items())
        },
        "canonical_only": True,
        "ui_projection_used_as_input": False,
    }
    lineage = {**lineage_body, "digest": canonical_digest(lineage_body)}
    manifest_body = {
        "schema_version": "zyra.p2-s05-01-evidence-manifest/v1",
        "slice_id": SLICE_ID,
        "implementation_commit": arguments.implementation_commit,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "metric_spec_count": len(registry["specs"]),
        "registry_digest": registry["digest"],
        "report_digest": report_payload["digest"],
        "lineage_digest": lineage["digest"],
        "run_count": len(report.runs),
        "scenario_count": len(report.scenarios),
        "mechanism_count": len(report.mechanisms),
        "failed_run_count": report.aggregate.failed_run_count,
    }
    manifest = {**manifest_body, "digest": canonical_digest(manifest_body)}
    _write(output_dir / "metric-spec-registry.json", registry)
    _write(output_dir / "metric-report.json", report_payload)
    _write(output_dir / "receipt-metric-lineage.json", lineage)
    _write(output_dir / "evidence-manifest.json", manifest)
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
