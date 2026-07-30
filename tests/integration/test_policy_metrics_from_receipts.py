from __future__ import annotations

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
    MetricStatus,
    Phase2MetricReportBuilder,
    RunMetricInput,
)
from zyra_orchestration.topology_policy import (
    ConstraintResult,
    ContractHeader,
    FrozenDict,
    GraphSnapshotRef,
    MemoryContinuityReceipt,
    NeuroSymbolicEvidenceBundle,
    PhysicalDispatchReceipt,
    PolicyDecisionDisposition,
    PolicyDecisionReceipt,
    PolicyOutcome,
    StableArtifactRef,
    TopologyOperation,
    TopologyOperationKind,
    TopologyProposalArtifact,
)
from zyra_orchestration.topology_policy.pruning import (
    CommunicationEdgeType,
    CommunicationOutcomeObservation,
)
from zyra_scheduler.operator_policy.adaptive_depth import AdaptiveDepthCostReceipt
from zyra_scheduler.operator_policy.early_exit import (
    ExitConditionResult,
    ExitDecision,
    ExitDecisionReceipt,
    ExitPosteriorResult,
)


DIGEST_A = "a" * 64


def _header(
    contract_id: str,
    *,
    created_at: str = "2026-07-30T09:00:00Z",
    mechanism: str = "phase2_test",
) -> ContractHeader:
    return ContractHeader(
        contract_id=contract_id,
        created_at=created_at,
        source_event_id=f"event:{contract_id}",
        correlation_id="run-real-receipts",
        causation_id="task-real-receipts",
        mechanism_id=mechanism,
        mechanism_version="deterministic-v1",
        input_version="canonical-owner-v1",
        idempotency_key=f"idempotency:{contract_id}",
        configuration_digest="b" * 64,
    )


def _artifact(ref_id: str) -> StableArtifactRef:
    return StableArtifactRef(
        ref_id=ref_id,
        uri=f"artifact://{ref_id}",
        digest=DIGEST_A,
    )


def _policy_receipts() -> tuple[
    TopologyProposalArtifact,
    PolicyDecisionReceipt,
    PolicyOutcome,
]:
    operation = TopologyOperation(
        kind=TopologyOperationKind.ADD_NODE,
        entity_id="node-new",
        value=FrozenDict({"role": "worker", "capabilities": ["execute"]}),
        required_permissions=("graph.write",),
        reason="requirement change needs an execution worker",
    )
    proposal = TopologyProposalArtifact(
        header=_header("proposal-real"),
        proposal_id="proposal-real",
        input_snapshot_digest="c" * 64,
        base_graph=GraphSnapshotRef(
            graph_id="graph-real",
            run_id="run-real-receipts",
            revision=2,
            signature="d" * 64,
            commit_id="commit-before",
        ),
        operations=(operation,),
        expected_outcome=FrozenDict({"confidence": 1.0}),
        alternatives=(),
        reasons=("ARG deterministic proposal",),
        constraint_assumptions=("worker registry is current",),
        expires_at="2026-07-30T09:05:00Z",
        fallback_profile="phase1_deterministic_baseline",
    )
    decision = PolicyDecisionReceipt(
        header=_header(
            "decision-real",
            created_at="2026-07-30T09:00:00.250Z",
            mechanism="symbolic_projector",
        ),
        decision_id="decision-real",
        proposal_id=proposal.proposal_id,
        proposal_digest=proposal.digest,
        disposition=PolicyDecisionDisposition.ACCEPT,
        constraint_results=(
            ConstraintResult(
                constraint_id="permission",
                passed=True,
                reason_code="allowed",
                message="graph mutation allowed",
                evidence_refs=("permission-real",),
            ),
        ),
        projected_operations=(operation,),
        delta_id="delta-real",
        delta_digest="e" * 64,
        graph_commit=FrozenDict(
            {"commit_id": "commit-real", "revision": 3}
        ),
    )
    outcome = PolicyOutcome(
        header=_header(
            "outcome-real",
            created_at="2026-07-30T09:00:00.500Z",
        ),
        proposal_ref=proposal.proposal_id,
        decision_ref=decision.decision_id,
        commit_ref="commit-real",
        verifier_result="passed",
        artifact_refs=(_artifact("topology-verifier"),),
        metrics=FrozenDict({"mechanism_decision_overhead_ms": 7.5}),
        permission_result="allowed",
        recovery_result="not_required",
        causal_refs=("delta-real", "commit-real", "topology-verifier"),
    )
    return proposal, decision, outcome


def _communication_receipts() -> tuple[CommunicationOutcomeObservation, ...]:
    return tuple(
        CommunicationOutcomeObservation(
            observation_id=f"observation-{index}",
            run_id="run-real-receipts",
            task_id="task-real-receipts",
            window_id="window-real",
            completed_at="2026-07-30T09:01:00Z",
            edge_id=f"edge-{source}-{target}",
            source_node_id=source,
            target_node_id=target,
            edge_type=edge_type,
            round_index=1,
            message_id=f"message-{index}",
            payload_digest=f"{index:064x}",
            delivered=True,
            delivery_receipt_ref=f"delivery-{index}",
            usage_receipt_ref=f"usage-{index}",
            message_bytes=128,
            prompt_tokens=12,
            completion_tokens=4,
            cost_usd=0.002,
            evidence_refs=(f"evidence-{index}",),
            utilized_evidence_refs=(f"evidence-{index}",),
            artifact_refs=(f"artifact-{index}",),
            verifier_result="passed",
            permission_result="allowed",
            causal_refs=(f"delivery-{index}", f"usage-{index}"),
        )
        for index, (source, target, edge_type) in enumerate(
            (
                ("node-a", "node-b", CommunicationEdgeType.SPATIAL),
                ("node-b", "node-a", CommunicationEdgeType.TEMPORAL),
            ),
            start=1,
        )
    )


def _continuity_receipt() -> MemoryContinuityReceipt:
    return MemoryContinuityReceipt(
        header=_header("continuity-real", mechanism="MemoryContinuityVerifier"),
        before_digest="f" * 64,
        after_digest="1" * 64,
        requirement_revision="requirement-r2",
        critical_fact_results=FrozenDict(
            {
                "fact-real": {
                    "present_after": True,
                    "consumed": True,
                    "usage_event_refs": ["event:first-decision"],
                    "provenance_ref": "fact-real",
                }
            }
        ),
        obligation_results=FrozenDict(
            {
                "before_digest": "before",
                "after_digest": "after",
                "retained": True,
                "consumed_ids": ["obligation-real"],
                "missing_consumption": [],
                "stale_requirement_execution": False,
                "duplicate_work_artifact_ids": [],
            }
        ),
        provenance_refs=(_artifact("fact-real"),),
        rejected_memory_refs=(),
        downstream_decision_ref="decision-real",
        continuity_result="passed",
    )


def _symbolic_bundle() -> NeuroSymbolicEvidenceBundle:
    return NeuroSymbolicEvidenceBundle(
        header=_header("symbolic-real", mechanism="symbolic_projector"),
        proposal_signal_mode="deterministic_only",
        proposal_ref=_artifact("proposal-adversarial"),
        model_observation_refs=(),
        constraint_results=(
            ConstraintResult(
                constraint_id="privacy",
                passed=False,
                reason_code="placement_forbidden",
                message="cloud placement rejected",
                evidence_refs=("privacy-policy",),
            ),
        ),
        projected_delta_ref="",
        commit_or_no_commit=FrozenDict(
            {
                "result": "rejected",
                "decision_disposition": "reject",
                "canonical_commit_present": False,
                "unsafe_commit": False,
                "projector_bypass_production_reachable": False,
            }
        ),
        permission_ref="permission-rejected",
        lease_ref="lease-not-created",
        verification_ref="verification-reject",
    )


def _dispatch(location: str, index: int) -> PhysicalDispatchReceipt:
    provider = (
        {
            "provider_id": "provider-real",
            "model_id": "model-real",
            "request_id": "request-real",
            "provider_attempt_id": "provider-attempt-real",
        }
        if location == "cloud"
        else {}
    )
    return PhysicalDispatchReceipt(
        header=_header(
            f"dispatch-{index}",
            created_at=f"2026-07-30T09:02:0{index}Z",
            mechanism="ResourceScheduler",
        ),
        placement_decision_id=f"placement-{index}",
        alternatives=("local", "edge", "cloud"),
        input_signals=FrozenDict({"selected_location": location}),
        worker_manifest_ref=_artifact(f"manifest-{index}"),
        lease_id=f"lease-{index}",
        physical_attempt_id=f"attempt-{index}",
        physical_identity=FrozenDict(
            {"location": location, "pid": 1000 + index}
        ),
        call_receipt=_artifact(f"call-{index}"),
        artifact_ref=_artifact(f"dispatch-artifact-{index}"),
        verifier_ref=_artifact(f"dispatch-verifier-{index}"),
        privacy_class="internal",
        allowed_placements=("local", "edge", "cloud"),
        permission_ref=f"permission-{index}",
        simulated=False,
        semantic_only=False,
        privacy_evidence=FrozenDict(
            {"selected_placement": location, "payload_redacted": True}
        ),
        provider_evidence=FrozenDict(provider),
    )


def test_structured_report_is_derived_from_real_contract_receipts() -> None:
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
    readiness = {
        "schema": "zyra.mechanism-evidence-readiness-report/v1",
        "report_digest": "readiness-real",
        "no_policy_training_audit": {"passed": True},
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
    }
    resolver = InMemoryCanonicalReceiptResolver(
        {
            COMMUNICATION_RECEIPTS: _communication_receipts(),
            TOPOLOGY_PROPOSALS: (proposal,),
            POLICY_DECISIONS: (decision,),
            POLICY_OUTCOMES: (outcome,),
            READINESS_REPORTS: (readiness,),
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
    report = Phase2MetricReportBuilder().build(
        (
            RunMetricInput(
                run_id="run-real-receipts",
                task_id="task-real-receipts",
                scenario_id="cross-domain-long-run",
                mechanism_profile="phase2_strongest_v1",
                receipt_resolver=resolver,
                task_succeeded=True,
                effective_transition_count=3,
            ),
        )
    )
    run = report.runs[0]
    assert run.metrics["communication.normalized_entropy"].value == 1.0
    assert run.metrics["topology.adaptation_latency_ms"].value == 250.0
    assert run.metrics["readiness.status_score"].value == 1.0
    assert run.metrics["continuity.critical_fact_recall"].value == 1.0
    assert run.metrics["symbolic.adversarial_reject_ratio"].value == 1.0
    assert run.metrics["symbolic.unsafe_commit_count"].value == 0.0
    assert run.metrics["operator.early_exit_true_positive_ratio"].value == 1.0
    assert run.metrics["operator.executed_depth"].value == 2.0
    assert run.metrics["dispatch.local_real_receipt_completeness"].value == 1.0
    assert run.metrics["dispatch.edge_real_receipt_completeness"].value == 1.0
    assert run.metrics["dispatch.cloud_real_receipt_completeness"].value == 1.0
    assert run.metrics["dispatch.provider_receipt_coverage"].value == 1.0
    assert run.metrics["dispatch.causal_chain_completeness"].value == 1.0
    assert run.metrics["evidence.canonical_transition_count"].reasons == (
        "evidence_volume_only",
    )
    assert report.aggregate.failed_run_count == 0
    assert report.digest
    assert all(
        value.status in {MetricStatus.OBSERVED, MetricStatus.NOT_APPLICABLE}
        for value in run.metrics.values()
    )
