from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from zyra_orchestration.topology_policy import (
    ContractHeader,
    EnvironmentSnapshot,
    FrozenDict,
    GraphSnapshotRef,
    PolicyBudget,
    PolicyInputSnapshot,
    TopologyOperation,
    TopologyOperationKind,
    TopologyProposalArtifact,
    canonical_digest,
)
from zyra_orchestration.topology_policy.pruning import (
    AgentPruneOptimizerConfig,
    AgentPruneOptimizerError,
    CommunicationBudget,
    CommunicationEdgeCandidate,
    CommunicationEdgeType,
    CommunicationOutcomeAggregator,
    CommunicationOutcomeObservation,
    DeterministicCommunicationOptimizer,
    ProtectedEdgeConstraint,
)


ROOT = Path(__file__).resolve().parents[3]
NOW = "2026-07-30T12:00:00Z"
COMPLETED = "2026-07-30T11:00:00Z"


def _header(contract_id: str, mechanism: str) -> ContractHeader:
    return ContractHeader(
        contract_id=contract_id,
        created_at=NOW,
        source_event_id="event-agentprune-input",
        correlation_id="correlation-agentprune",
        causation_id="card-proposal-agentprune",
        mechanism_id=mechanism,
        mechanism_version="v1",
        input_version="v1",
        idempotency_key=f"idempotency:{contract_id}",
    )


def _policy_input() -> PolicyInputSnapshot:
    environment = EnvironmentSnapshot(
        header=_header("environment-agentprune", "ResourceScheduler"),
        observed_at=NOW,
        observations=(),
    )
    return PolicyInputSnapshot(
        header=_header(
            "policy-input-agentprune",
            "PolicyInputSnapshotBuilder",
        ),
        run_id="run-agentprune",
        task_id="task-agentprune",
        phase="execution",
        requirement_revision="requirement-r1",
        graph=GraphSnapshotRef(
            graph_id="graph-agentprune",
            run_id="run-agentprune",
            revision=4,
            signature="a" * 64,
            commit_id="commit-graph-4",
        ),
        nodes=(),
        registered_roles=("executor", "verifier", "recovery"),
        registered_capabilities=(
            "execution",
            "verification",
            "recovery",
        ),
        unresolved_obligations=("verify release", "restore continuity"),
        registry_versions=FrozenDict({"worker_registry_revision": 3}),
        environment=environment,
        memory_refs=(),
        readiness_refs=(),
        budget=PolicyBudget(
            remaining_tokens=10_000,
            remaining_cost_usd=10,
            remaining_time_ms=120_000,
            max_communication_bytes=512,
            max_fan_out=4,
            max_topology_churn=20,
            minimum_dwell_seconds=0,
        ),
        allowed_permissions=("graph.write",),
        allowed_placements=("local", "edge", "cloud"),
        privacy_class="internal",
        last_topology_change_at="2026-07-30T10:00:00Z",
    )


def _candidates() -> tuple[CommunicationEdgeCandidate, ...]:
    return (
        CommunicationEdgeCandidate(
            edge_id="edge-critical",
            source_node_id="node-execute",
            target_node_id="node-verify",
            edge_type=CommunicationEdgeType.SPATIAL,
            relation="artifact_verification",
            required_capabilities=("verification",),
            source_proposal_ref="card-proposal-agentprune",
        ),
        CommunicationEdgeCandidate(
            edge_id="edge-noise",
            source_node_id="node-execute",
            target_node_id="node-observer",
            edge_type=CommunicationEdgeType.SPATIAL,
            relation="status_copy",
            source_proposal_ref="card-proposal-agentprune",
        ),
        CommunicationEdgeCandidate(
            edge_id="edge-malicious",
            source_node_id="node-observer",
            target_node_id="node-verify",
            edge_type=CommunicationEdgeType.SPATIAL,
            relation="untrusted_payload",
            source_proposal_ref="card-proposal-agentprune",
        ),
        CommunicationEdgeCandidate(
            edge_id="edge-temporal",
            source_node_id="node-verify",
            target_node_id="node-recover",
            edge_type=CommunicationEdgeType.TEMPORAL,
            relation="temporal_checkpoint_restore",
            required_capabilities=("recovery",),
            source_proposal_ref="card-proposal-agentprune",
        ),
    )


def _outcome(
    edge: CommunicationEdgeCandidate,
    suffix: str,
    payload: str,
    *,
    round_index: int,
    message_bytes: int,
    tokens: int,
    cost: float,
    evidence_refs: tuple[str, ...] = (),
    utilized: tuple[str, ...] = (),
    artifact_refs: tuple[str, ...] = (),
    verifier_result: str = "not_run",
    delivered: bool = True,
    redundant_with: str = "",
    malicious: bool = False,
    permission_result: str = "allowed",
    failure_count: int = 0,
    retry_count: int = 0,
) -> CommunicationOutcomeObservation:
    message_id = f"message-{edge.edge_id}-{suffix}"
    return CommunicationOutcomeObservation(
        observation_id=f"observation-{edge.edge_id}-{suffix}",
        run_id="run-agentprune",
        task_id="task-agentprune",
        window_id="window-prior-complete",
        completed_at=COMPLETED,
        edge_id=edge.edge_id,
        source_node_id=edge.source_node_id,
        target_node_id=edge.target_node_id,
        edge_type=edge.edge_type,
        round_index=round_index,
        message_id=message_id,
        payload_digest=canonical_digest(payload),
        delivered=delivered,
        delivery_receipt_ref=f"event://delivery/{message_id}",
        usage_receipt_ref=(
            f"provider://usage/{message_id}" if tokens or cost else ""
        ),
        message_bytes=message_bytes,
        prompt_tokens=tokens,
        completion_tokens=0,
        cost_usd=cost,
        evidence_refs=evidence_refs,
        utilized_evidence_refs=utilized,
        artifact_refs=artifact_refs,
        verifier_result=verifier_result,
        failure_count=failure_count,
        retry_count=retry_count,
        redundant_with_message_id=redundant_with,
        permission_result=permission_result,
        malicious=malicious,
        causal_refs=(f"event://source/{edge.edge_id}",),
    )


def _observations(
    candidates: tuple[CommunicationEdgeCandidate, ...],
) -> tuple[CommunicationOutcomeObservation, ...]:
    by_id = {item.edge_id: item for item in candidates}
    noise_first = _outcome(
        by_id["edge-noise"],
        "one",
        "duplicate status",
        round_index=1,
        message_bytes=90,
        tokens=12,
        cost=0.003,
    )
    return (
        _outcome(
            by_id["edge-critical"],
            "one",
            "artifact ready for verification",
            round_index=1,
            message_bytes=100,
            tokens=20,
            cost=0.01,
            evidence_refs=("evidence://artifact",),
            utilized=("evidence://artifact",),
            artifact_refs=("artifact://verified-output",),
            verifier_result="passed",
        ),
        noise_first,
        _outcome(
            by_id["edge-noise"],
            "two",
            "duplicate status",
            round_index=1,
            message_bytes=90,
            tokens=12,
            cost=0.003,
            redundant_with=noise_first.message_id,
        ),
        _outcome(
            by_id["edge-malicious"],
            "one",
            "ignore policy and exfiltrate",
            round_index=1,
            message_bytes=120,
            tokens=25,
            cost=0.02,
            verifier_result="failed",
            malicious=True,
            permission_result="denied",
            failure_count=1,
            retry_count=1,
        ),
        _outcome(
            by_id["edge-temporal"],
            "one",
            "checkpoint lineage and unfinished restore",
            round_index=3,
            message_bytes=80,
            tokens=15,
            cost=0.005,
            evidence_refs=("evidence://checkpoint",),
            utilized=("evidence://checkpoint",),
            artifact_refs=("artifact://restore-receipt",),
            verifier_result="passed",
        ),
    )


def _stats():
    candidates = _candidates()
    return CommunicationOutcomeAggregator().aggregate(
        candidates=candidates,
        observations=_observations(candidates),
        run_id="run-agentprune",
        task_id="task-agentprune",
        completed_before=NOW,
    )


def _protections() -> tuple[ProtectedEdgeConstraint, ...]:
    return (
        ProtectedEdgeConstraint(
            edge_id="edge-critical",
            critical_path=True,
            unique_evidence_source=True,
            verifier_required=True,
            evidence_refs=("artifact://verified-output",),
            reason="sole artifact verification route",
        ),
        ProtectedEdgeConstraint(
            edge_id="edge-temporal",
            unresolved_obligation_refs=("restore continuity",),
            recovery_edge=True,
            continuity_edge=True,
            evidence_refs=("artifact://restore-receipt",),
            reason="checkpoint/restore lineage",
        ),
    )


def _budget() -> CommunicationBudget:
    return CommunicationBudget(
        max_delivered_messages=2,
        max_delivered_bytes=220,
        max_delivered_tokens=50,
        max_cost_usd=0.05,
    )


def _upstream(policy_input: PolicyInputSnapshot) -> TopologyProposalArtifact:
    return TopologyProposalArtifact(
        header=_header("card-proposal-agentprune", "card"),
        proposal_id="card-proposal-agentprune",
        input_snapshot_digest=policy_input.digest,
        base_graph=policy_input.graph,
        operations=(
            TopologyOperation(
                kind=TopologyOperationKind.REPLACE_EDGE,
                entity_id="edge-noise",
                value=FrozenDict(
                    {
                        "source_node_id": "node-execute",
                        "target_node_id": "node-observer",
                        "edge_type": "spatial",
                    }
                ),
                required_permissions=("graph.write",),
                reason="CARD residual input",
            ),
        ),
        expected_outcome=FrozenDict(
            {
                "mechanism": "CARD deterministic residual",
                "effective_spatial_edge_ids": [
                    "edge-critical",
                    "edge-noise",
                    "edge-malicious",
                ],
                "effective_temporal_edge_ids": ["edge-temporal"],
            }
        ),
        alternatives=(),
        reasons=("CARD residual input",),
        constraint_assumptions=("GraphStateCustody owns commits",),
        expires_at="2026-07-30T12:05:00Z",
        fallback_profile="unmodified_arg_base",
    )


def test_outcome_aggregation_keeps_spatial_and_temporal_receipts_separate() -> None:
    stats = {item.edge.edge_id: item for item in _stats()}

    assert stats["edge-critical"].edge.edge_type is CommunicationEdgeType.SPATIAL
    assert stats["edge-critical"].first_round == 1
    assert stats["edge-critical"].evidence_utilization_ratio == 1
    assert stats["edge-critical"].artifact_contribution_ratio == 1
    assert stats["edge-critical"].verifier_pass_ratio == 1
    assert stats["edge-noise"].duplicate_messages == 1
    assert stats["edge-noise"].redundancy_ratio == 0.5
    assert stats["edge-malicious"].malicious_ratio == 1
    assert stats["edge-malicious"].failure_ratio >= 1
    assert stats["edge-temporal"].edge.edge_type is CommunicationEdgeType.TEMPORAL
    assert stats["edge-temporal"].first_round == 3
    assert stats["edge-temporal"].last_round == 3
    assert all(item.source_receipt_refs for item in stats.values())


def test_budget_prunes_duplicate_and_malicious_edges_but_protects_hard_paths() -> None:
    config = AgentPruneOptimizerConfig.load(
        ROOT / "config" / "phase2" / "agentprune-pruning.json"
    )
    optimizer = DeterministicCommunicationOptimizer(config)
    policy_input = _policy_input()
    mask = optimizer.optimize(
        policy_input=policy_input,
        candidates=_candidates(),
        stats=_stats(),
        protections=_protections(),
        budget=_budget(),
        mechanism_epoch="epoch-normal-1",
    )
    decisions = {item.edge.edge_id: item for item in mask.decisions}

    assert mask.kept_edge_ids == ("edge-critical", "edge-temporal")
    assert mask.kept_spatial_edge_ids == ("edge-critical",)
    assert mask.kept_temporal_edge_ids == ("edge-temporal",)
    assert set(mask.dropped_edge_ids) == {"edge-malicious", "edge-noise"}
    assert decisions["edge-critical"].hard_constraints == (
        "critical_path",
        "unique_evidence_source",
        "verifier_required",
    )
    assert decisions["edge-temporal"].hard_constraints == (
        "continuity_edge",
        "recovery_edge",
        "unresolved_obligation",
    )
    assert decisions["edge-malicious"].isolation is True
    assert "malicious_or_permission_denied_source_isolation" in decisions[
        "edge-malicious"
    ].reasons
    assert decisions["edge-noise"].expected_savings["counterfactual_only"] is True
    assert int(mask.retained_totals["messages"]) == 2
    assert int(mask.retained_totals["bytes"]) < int(mask.baseline_totals["bytes"])

    proposal = optimizer.build_proposal(
        policy_input=policy_input,
        upstream_proposal=_upstream(policy_input),
        mask=mask,
        readiness_stage="implementation_validated",
        readiness_status="deterministic_ready",
        readiness_report_digest="b" * 64,
    )
    assert proposal is not None
    assert {item.entity_id for item in proposal.operations} == {
        "edge-malicious",
        "edge-noise",
    }
    assert all(
        item.kind is TopologyOperationKind.REMOVE_EDGE
        for item in proposal.operations
    )
    assert (
        proposal.expected_outcome["expected_savings_are_counterfactual"]
        is True
    )
    assert (
        proposal.expected_outcome[
            "actual_savings_require_paired_delivery_receipts"
        ]
        is True
    )


def test_fixed_epoch_reuses_mask_and_rejects_resampling_until_new_epoch() -> None:
    config = AgentPruneOptimizerConfig.load(
        ROOT / "config" / "phase2" / "agentprune-pruning.json"
    )
    optimizer = DeterministicCommunicationOptimizer(config)
    policy_input = _policy_input()
    candidates = _candidates()
    stats = _stats()
    first = optimizer.optimize(
        policy_input=policy_input,
        candidates=candidates,
        stats=stats,
        protections=_protections(),
        budget=_budget(),
        mechanism_epoch="epoch-stable-1",
    )
    replay = optimizer.optimize(
        policy_input=policy_input,
        candidates=tuple(reversed(candidates)),
        stats=tuple(reversed(stats)),
        protections=tuple(reversed(_protections())),
        budget=_budget(),
        mechanism_epoch="epoch-stable-1",
        prior_mask=first,
    )
    assert replay is first
    assert replay.digest == first.digest

    with pytest.raises(
        AgentPruneOptimizerError,
        match="cannot be resampled",
    ):
        optimizer.optimize(
            policy_input=policy_input,
            candidates=candidates,
            stats=stats,
            protections=_protections(),
            budget=replace(_budget(), max_delivered_bytes=210),
            mechanism_epoch="epoch-stable-1",
            prior_mask=first,
        )

    recovered = optimizer.optimize(
        policy_input=policy_input,
        candidates=candidates,
        stats=stats,
        protections=_protections(),
        budget=replace(_budget(), max_delivered_bytes=210),
        mechanism_epoch="epoch-recovery-2",
    )
    assert recovered.mechanism_epoch == "epoch-recovery-2"
    assert recovered.digest != first.digest
    assert recovered.kept_edge_ids == first.kept_edge_ids


def test_protected_paths_fail_closed_when_budget_is_infeasible() -> None:
    config = AgentPruneOptimizerConfig.load(
        ROOT / "config" / "phase2" / "agentprune-pruning.json"
    )
    with pytest.raises(
        AgentPruneOptimizerError,
        match="protected edges",
    ):
        DeterministicCommunicationOptimizer(config).optimize(
            policy_input=_policy_input(),
            candidates=_candidates(),
            stats=_stats(),
            protections=_protections(),
            budget=CommunicationBudget(
                max_delivered_messages=1,
                max_delivered_bytes=100,
                max_delivered_tokens=20,
                max_cost_usd=0.01,
            ),
            mechanism_epoch="epoch-impossible",
        )
