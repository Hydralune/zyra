from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from zyra_api.policy_api import (
    FilesystemPolicyMetricReportProvider,
    PolicyMetricApi,
)
from zyra_api.main import ZYRA_DYNAMIC_API_ROUTES
from zyra_evaluation.policy_benchmark import (
    CONTINUITY_RECEIPTS,
    PHYSICAL_DISPATCH_RECEIPTS,
    POLICY_DECISIONS,
    READINESS_REPORTS,
    SYMBOLIC_BUNDLES,
    TOPOLOGY_PROPOSALS,
    InMemoryCanonicalReceiptResolver,
    MetricStatus,
    Phase2MetricEngine,
    Phase2MetricError,
    RunMetricInput,
    canonical_digest,
)


def _run(
    receipts: dict[str, tuple[object, ...]],
    *,
    transitions: int = 1,
    disconnected: tuple[str, ...] = (),
) -> RunMetricInput:
    return RunMetricInput(
        run_id="run-anti-gaming",
        task_id="task-anti-gaming",
        scenario_id="scenario-anti-gaming",
        mechanism_profile="phase2_strongest_v1",
        receipt_resolver=InMemoryCanonicalReceiptResolver(
            receipts,
            disconnected=disconnected,
        ),
        task_succeeded=True,
        effective_transition_count=transitions,
    )


def _dispatch(location: str, attempt: str, *, simulated: bool) -> dict[str, object]:
    return {
        "schema_version": "zyra.physical-dispatch-receipt/v2",
        "contract_kind": "physical_dispatch_receipt",
        "contract_id": f"dispatch-{attempt}",
        "created_at": "2026-07-30T10:00:00Z",
        "source_event_id": f"event-{attempt}",
        "correlation_id": "run-anti-gaming",
        "causation_id": "task-anti-gaming",
        "mechanism_id": "ResourceScheduler",
        "mechanism_version": "physical-v1",
        "input_version": "owner-v1",
        "idempotency_key": f"dispatch:{attempt}",
        "configuration_digest": "a" * 64,
        "payload": {
            "placement_decision_id": f"placement-{attempt}",
            "alternatives": ["local", "edge", "cloud"],
            "input_signals": {"selected_location": location},
            "worker_manifest_ref": {"ref_id": "manifest"},
            "lease_id": f"lease-{attempt}",
            "physical_attempt_id": attempt,
            "physical_identity": {"location": location},
            "call_receipt": {"ref_id": f"call-{attempt}"},
            "artifact_ref": {"ref_id": f"artifact-{attempt}"},
            "verifier_ref": {"ref_id": f"verify-{attempt}"},
            "privacy_class": "internal",
            "allowed_placements": ["local", "edge", "cloud"],
            "permission_ref": "permission",
            "simulated": simulated,
            "semantic_only": False,
            "privacy_evidence": {"selected_placement": location},
            "provider_evidence": {},
        },
    }


def test_simulated_dispatch_is_excluded_from_real_edge_numerator() -> None:
    result = Phase2MetricEngine().evaluate_run(
        _run(
            {
                PHYSICAL_DISPATCH_RECEIPTS: (
                    _dispatch("local", "attempt-real", simulated=False),
                    _dispatch("edge", "attempt-simulated", simulated=True),
                )
            }
        )
    )
    local = result.metrics["dispatch.local_real_receipt_completeness"]
    edge = result.metrics["dispatch.edge_real_receipt_completeness"]
    assert local.value == 1.0
    assert local.numerator == 1
    assert edge.value is None
    assert edge.status is MetricStatus.DEGRADED
    assert edge.numerator == 0


def test_memory_presence_without_downstream_use_does_not_pass_continuity() -> None:
    receipt = {
        "schema_version": "zyra.memory-continuity-receipt/v1",
        "contract_kind": "memory_continuity_receipt",
        "idempotency_key": "continuity:unused",
        "payload": {
            "critical_fact_results": {
                "fact-a": {
                    "present_after": True,
                    "consumed": False,
                    "usage_event_refs": [],
                    "provenance_ref": "fact-a",
                }
            },
            "obligation_results": {
                "before_digest": "before",
                "after_digest": "after",
                "retained": True,
                "consumed_ids": [],
                "missing_consumption": ["obligation-a"],
            },
            "provenance_refs": [{"ref_id": "fact-a"}],
            "downstream_decision_ref": "",
            "continuity_result": "passed",
        },
    }
    result = Phase2MetricEngine().evaluate_run(
        _run({CONTINUITY_RECEIPTS: (receipt,)})
    )
    recall = result.metrics["continuity.critical_fact_recall"]
    first_decision = result.metrics["continuity.first_decision_correctness"]
    assert recall.value == 0.0
    assert "downstream_usage_required" in recall.reasons
    assert first_decision.value == 0.0


def test_projector_reject_without_commit_records_zero_unsafe_commit() -> None:
    bundle = {
        "schema_version": "zyra.neuro-symbolic-evidence-bundle/v1",
        "contract_kind": "neuro_symbolic_evidence_bundle",
        "idempotency_key": "symbolic:reject",
        "payload": {
            "commit_or_no_commit": {
                "result": "rejected",
                "decision_disposition": "reject",
                "canonical_commit_present": False,
                "unsafe_commit": False,
                "projector_bypass_production_reachable": False,
            }
        },
    }
    result = Phase2MetricEngine().evaluate_run(
        _run({SYMBOLIC_BUNDLES: (bundle,)})
    )
    assert result.metrics["symbolic.adversarial_reject_ratio"].value == 1.0
    assert result.metrics["symbolic.unsafe_commit_count"].value == 0.0
    assert result.metrics["symbolic.projector_bypass_reachable"].value == 0.0


def test_transition_volume_cannot_raise_readiness_status() -> None:
    readiness = {
        "report_digest": "readiness-evidence-only",
        "mechanism_id": "arg",
        "readiness_stage": "input_precheck",
        "status": "evidence_only",
        "actual_mode": "diagnostic",
    }
    engine = Phase2MetricEngine()
    short = engine.evaluate_run(
        _run({READINESS_REPORTS: (readiness,)}, transitions=1)
    )
    long = engine.evaluate_run(
        _run({READINESS_REPORTS: (readiness,)}, transitions=10_000)
    )
    assert short.metrics["readiness.status_score"].value == 0.5
    assert long.metrics["readiness.status_score"].value == 0.5
    assert (
        short.metrics["evidence.canonical_transition_count"].value
        != long.metrics["evidence.canonical_transition_count"].value
    )


def test_stale_proposal_and_missing_outcome_fail_closed() -> None:
    proposal = {
        "schema_version": "zyra.topology-proposal-artifact/v1",
        "contract_kind": "topology_proposal_artifact",
        "contract_id": "proposal-stale",
        "created_at": "2026-07-30T10:00:00Z",
        "idempotency_key": "proposal:stale",
        "payload": {
            "proposal_id": "proposal-stale",
            "expires_at": "2026-07-30T10:00:01Z",
            "operations": [],
        },
    }
    decision = {
        "schema_version": "zyra.policy-decision-receipt/v1",
        "contract_kind": "policy_decision_receipt",
        "contract_id": "decision-stale",
        "created_at": "2026-07-30T10:00:02Z",
        "idempotency_key": "decision:stale",
        "payload": {
            "decision_id": "decision-stale",
            "proposal_id": "proposal-stale",
            "proposal_digest": canonical_digest(proposal),
            "disposition": "accept",
            "projected_operations": [],
            "graph_commit": {"commit_id": "commit-stale"},
        },
    }
    with pytest.raises(Phase2MetricError) as stale:
        Phase2MetricEngine().evaluate_run(
            _run(
                {
                    TOPOLOGY_PROPOSALS: (proposal,),
                    POLICY_DECISIONS: (decision,),
                }
            )
        )
    assert stale.value.code == "metric_receipt_stale"

    decision["created_at"] = "2026-07-30T10:00:00.500Z"
    with pytest.raises(Phase2MetricError) as missing:
        Phase2MetricEngine().evaluate_run(
            _run(
                {
                    TOPOLOGY_PROPOSALS: (proposal,),
                    POLICY_DECISIONS: (decision,),
                }
            )
        )
    assert missing.value.code == "metric_policy_outcome_reference_missing"


def test_zero_denominator_and_disconnected_resolver_are_non_success() -> None:
    empty = Phase2MetricEngine().evaluate_run(_run({}))
    assert empty.metrics["communication.cost_per_effective_transition"].status is not MetricStatus.OBSERVED
    disconnected = Phase2MetricEngine().evaluate_run(
        _run(
            {},
            disconnected=(PHYSICAL_DISPATCH_RECEIPTS,),
        )
    )
    assert disconnected.metrics["dispatch.causal_chain_completeness"].status is MetricStatus.FAILED


def test_policy_api_reads_digest_verified_report_without_owning_state() -> None:
    body = {
        "schema_version": "zyra.phase2-metric-report/v1",
        "registry_digest": "a" * 64,
        "run_reports": [],
        "scenario_reports": [],
        "mechanism_reports": [],
        "aggregate_report": {},
        "requirement_metrics": {},
        "anti_gaming": {},
    }
    payload = {**body, "digest": canonical_digest(body)}
    with tempfile.TemporaryDirectory(
        prefix=".p2-s05-01-api-",
        dir=Path.cwd(),
    ) as directory:
        report_root = Path(directory)
        (report_root / "report-real.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        api = PolicyMetricApi(
            FilesystemPolicyMetricReportProvider(report_root)
        )
        response = api.route_get(
            ("policy", "metrics", "reports", "report-real"),
            {},
        )
    assert response is not None
    assert response.status == 200
    assert response.body["digest"] == payload["digest"]
    assert response.headers["Cache-Control"] == "no-store, max-age=0"
    assert ("GET", "/policy/metrics/specs") in ZYRA_DYNAMIC_API_ROUTES
    assert (
        "GET",
        "/policy/metrics/reports/{report_id}",
    ) in ZYRA_DYNAMIC_API_ROUTES
