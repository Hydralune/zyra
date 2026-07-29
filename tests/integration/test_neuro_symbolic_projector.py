from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from zyra_core import EventRecord, EventType
from zyra_evaluation.policy_benchmark.neuro_symbolic import (
    NeuroSymbolicEvidenceBuilder,
    NeuroSymbolicEvidenceError,
    PretrainedModelObservation,
    summarize_neuro_symbolic_bundles,
)
from zyra_orchestration.graph_custody import (
    GraphDeltaBuilder,
    GraphNode,
    GraphStateCustody,
    GraphStateStore,
)
from zyra_orchestration.topology_policy import (
    ContractHeader,
    EnvironmentSnapshot,
    FrozenDict,
    GraphSnapshotRef,
    MechanismEvidenceReadinessReportRef,
    NoPolicyTrainingValidator,
    PolicyBudget,
    PolicyDecisionDisposition,
    PolicyDeltaBuilder,
    PolicyEvidencePublisher,
    PolicyInputSnapshot,
    PolicyNodeSnapshot,
    PolicyOutcome,
    StableArtifactRef,
    TelemetryObservation,
    TopologyConstraintProjector,
    TopologyOperation,
    TopologyOperationKind,
    TopologyProposalArtifact,
    canonical_digest,
)
from zyra_runtime import LocalArtifactStore


NOW = "2026-07-30T08:00:00Z"


def _header(
    contract_id: str,
    *,
    mechanism: str = "ARG",
    causation_id: str = "event-neuro-symbolic-source",
    idempotency_key: str = "",
) -> ContractHeader:
    return ContractHeader(
        contract_id=contract_id,
        created_at=NOW,
        source_event_id="event-neuro-symbolic-source",
        correlation_id="correlation-neuro-symbolic",
        causation_id=causation_id,
        mechanism_id=mechanism,
        mechanism_version="arg-deterministic-v1",
        input_version="policy-input-v1",
        idempotency_key=idempotency_key or f"idempotency:{contract_id}",
        configuration_digest="d" * 64,
    )


def _custody(path: Path) -> GraphStateCustody:
    store = GraphStateStore(path)
    store.initialize()
    custody = GraphStateCustody(store)
    custody.create(graph_id_value="graph-neuro-symbolic", run_id="run-neuro-symbolic")
    return custody


def _environment(
    *,
    lease_available: bool = True,
    fresh_until: str = "2026-07-30T09:00:00Z",
) -> EnvironmentSnapshot:
    return EnvironmentSnapshot(
        header=_header("environment", mechanism="ResourceScheduler"),
        observed_at=NOW,
        observations=(
            TelemetryObservation(
                observation_id="observation-edge",
                resource_id="worker-edge",
                category="worker",
                observed_at=NOW,
                fresh_until=fresh_until,
                confidence=1.0,
                observation_source="ResourceScheduler.worker_pool_api_projection",
                source_event_id="event-neuro-symbolic-source",
                physical_runtime_id="edge-process-23",
                location="edge",
                available=True,
                healthy=True,
                capacity_available=2,
                lease_available=lease_available,
                privacy_classes=("internal",),
                allowed_placements=("edge",),
            ),
        ),
        required_categories=("worker",),
    )


def _input(
    custody: GraphStateCustody,
    *,
    environment: EnvironmentSnapshot | None = None,
) -> PolicyInputSnapshot:
    current = custody.current("graph-neuro-symbolic")
    return PolicyInputSnapshot(
        header=_header("policy-input", mechanism="PolicyInputSnapshotBuilder"),
        run_id=current.run_id,
        task_id="task-neuro-symbolic",
        phase="phase2",
        requirement_revision="requirement-r1",
        graph=GraphSnapshotRef(
            graph_id=current.graph_id,
            run_id=current.run_id,
            revision=current.revision,
            signature=current.signature,
            commit_id=current.commit_id,
        ),
        nodes=tuple(
            PolicyNodeSnapshot(
                node_id=node.node_id,
                role=node.role,
                capabilities=node.capabilities,
                dependencies=node.dependencies,
                state=node.state.value,
                revision=node.revision,
            )
            for node in current.nodes
        ),
        registered_roles=("worker",),
        registered_capabilities=("execute",),
        unresolved_obligations=("produce-verified-artifact",),
        registry_versions=FrozenDict({"capability": "r1"}),
        environment=environment or _environment(),
        memory_refs=(),
        readiness_refs=(
            MechanismEvidenceReadinessReportRef(
                header=_header("readiness"),
                report_ref="artifact://readiness/arg",
                report_digest="a" * 64,
                readiness_stage="activation_ready",
                status="deterministic_ready",
            ),
        ),
        budget=PolicyBudget(
            remaining_tokens=1000,
            remaining_cost_usd=1,
            remaining_time_ms=10000,
            max_communication_bytes=4096,
            max_fan_out=4,
            max_topology_churn=4,
            minimum_dwell_seconds=0,
        ),
        allowed_permissions=("graph.write",),
        allowed_placements=("edge",),
        privacy_class="internal",
        last_topology_change_at="2026-07-30T07:00:00Z",
    )


def _operation(node_id: str = "node-worker") -> TopologyOperation:
    return TopologyOperation(
        kind=TopologyOperationKind.ADD_NODE,
        entity_id=node_id,
        value=FrozenDict(
            {
                "role": "worker",
                "capabilities": ["execute"],
                "dependencies": [],
            }
        ),
        required_permissions=("graph.write",),
        requested_placement="edge",
        resource_id="worker-edge",
        required_capacity=1,
        communication_bytes=64,
        reason="deterministic mechanism proposal",
    )


def _proposal(
    policy_input: PolicyInputSnapshot,
    *,
    proposal_id: str = "proposal-neuro-symbolic",
    operation: TopologyOperation | None = None,
    expected_outcome: FrozenDict | None = None,
) -> TopologyProposalArtifact:
    return TopologyProposalArtifact(
        header=_header(
            proposal_id,
            idempotency_key=f"key:{proposal_id}",
        ),
        proposal_id=proposal_id,
        input_snapshot_digest=policy_input.digest,
        base_graph=policy_input.graph,
        operations=(operation or _operation(),),
        expected_outcome=expected_outcome
        or FrozenDict(
            {
                "tokens": 10,
                "cost_usd": 0.01,
                "time_ms": 10,
                "confidence": 0.9,
            }
        ),
        alternatives=(),
        reasons=("deterministic ARG proposal",),
        constraint_assumptions=("permission and lease are preflight observations",),
        expires_at="2026-07-30T09:00:00Z",
        fallback_profile="phase1_deterministic_baseline",
    )


def _publish_proposal(
    proposal: TopologyProposalArtifact,
    tmp_path: Path,
) -> tuple[PolicyEvidencePublisher, StableArtifactRef, list[EventRecord]]:
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    events: list[EventRecord] = []
    publisher = PolicyEvidencePublisher(artifacts, admit_event=events.append)
    published = publisher.publish(
        proposal,
        run_id="run-neuro-symbolic",
        task_id="task-neuro-symbolic",
    )
    return publisher, published.artifact_ref, events


def test_real_projector_commit_builds_complete_deterministic_symbolic_bundle(
    tmp_path: Path,
) -> None:
    custody = _custody(tmp_path / "graph.sqlite3")
    policy_input = _input(custody)
    proposal = _proposal(policy_input)
    projection = TopologyConstraintProjector(custody).execute(
        policy_input,
        proposal,
        decision_id="decision-neuro-symbolic",
        evaluated_at=NOW,
    )
    assert projection.receipt.disposition is PolicyDecisionDisposition.ACCEPT
    assert projection.commit is not None
    assert custody.current("graph-neuro-symbolic").revision == 1

    publisher, proposal_ref, events = _publish_proposal(proposal, tmp_path)
    outcome = PolicyOutcome(
        header=_header(
            "outcome-neuro-symbolic",
            mechanism="PolicyOutcomeVerifier",
            causation_id=projection.receipt.header.contract_id,
        ),
        proposal_ref=proposal.header.contract_id,
        decision_ref=projection.receipt.header.contract_id,
        commit_ref=projection.commit.receipt.commit_id,
        verifier_result="passed",
        artifact_refs=(),
        metrics=FrozenDict({"artifact_complete": True, "unsafe_commit": False}),
        permission_result="allowed:graph.write",
        recovery_result="not_required",
        causal_refs=(
            proposal.header.contract_id,
            projection.receipt.header.contract_id,
            projection.commit.receipt.commit_id,
        ),
    )
    published_outcome = publisher.publish(
        outcome,
        run_id=policy_input.run_id,
        task_id=policy_input.task_id,
    )
    bundle = NeuroSymbolicEvidenceBuilder().build(
        header=_header(
            "bundle-neuro-symbolic",
            mechanism="NeuroSymbolicEvidenceBuilder",
            causation_id=outcome.header.contract_id,
        ),
        proposal=proposal,
        proposal_ref=proposal_ref,
        projection=projection,
        attack_class="none",
        proposal_signal_mode="deterministic_only",
        permission_ref="permission-snapshot:event-neuro-symbolic-source",
        lease_ref="lease-observation:worker-edge",
        verification_ref=published_outcome.artifact_ref.ref_id,
        outcome_ref=published_outcome.artifact_ref.ref_id,
        artifact_refs=(published_outcome.artifact_ref,),
    )
    published_bundle = publisher.publish(
        bundle,
        run_id=policy_input.run_id,
        task_id=policy_input.task_id,
    )
    replayed = publisher.replay(published_bundle.artifact)
    assert replayed.digest == bundle.digest
    assert bundle.commit_or_no_commit["canonical_commit_present"] is True
    assert bundle.commit_or_no_commit["unsafe_commit"] is False
    assert bundle.commit_or_no_commit["result"] == "accepted"
    assert events[-1].event_type is EventType.EVALUATION
    summary = summarize_neuro_symbolic_bundles([bundle])
    assert summary["hard_gates_passed"] is True
    assert summary["unsafe_commit_count"] == 0


def test_pending_side_effect_and_model_signal_contract_fail_closed(
    tmp_path: Path,
) -> None:
    custody = _custody(tmp_path / "graph.sqlite3")
    policy_input = _input(custody)
    proposal = _proposal(
        policy_input,
        proposal_id="proposal-pending-side-effect",
        expected_outcome=FrozenDict(
            {
                "tokens": 10,
                "cost_usd": 0.01,
                "time_ms": 10,
                "pending_side_effects": ["tool-call-unsettled"],
            }
        ),
    )
    projection = TopologyConstraintProjector(custody).execute(
        policy_input,
        proposal,
        decision_id="decision-pending-side-effect",
        evaluated_at=NOW,
    )
    reasons = {
        item.reason_code for item in projection.receipt.constraint_results
    }
    assert "pending_side_effect_unsettled" in reasons
    assert projection.receipt.disposition is PolicyDecisionDisposition.REJECT
    assert projection.commit is None
    assert custody.current("graph-neuro-symbolic").revision == 0

    _publisher, proposal_ref, _events = _publish_proposal(proposal, tmp_path)
    builder = NeuroSymbolicEvidenceBuilder()
    bundle = builder.build(
        header=_header(
            "bundle-pending-side-effect",
            mechanism="NeuroSymbolicEvidenceBuilder",
        ),
        proposal=proposal,
        proposal_ref=proposal_ref,
        projection=projection,
        attack_class="pending_side_effect",
        proposal_signal_mode="deterministic_only",
        permission_ref="permission-preflight:allowed",
        lease_ref="lease-not-issued:projector-rejected",
        verification_ref="verification:no-commit",
        outcome_ref="outcome:no-commit",
    )
    assert bundle.commit_or_no_commit["canonical_commit_present"] is False
    assert bundle.commit_or_no_commit["result"] == "forced_baseline"
    assert bundle.commit_or_no_commit["unsafe_commit"] is False

    with pytest.raises(NeuroSymbolicEvidenceError, match="requires fixed model"):
        builder.build(
            header=_header(
                "bundle-fake-neural",
                mechanism="NeuroSymbolicEvidenceBuilder",
            ),
            proposal=proposal,
            proposal_ref=proposal_ref,
            projection=projection,
            attack_class="pending_side_effect",
            proposal_signal_mode="pretrained_model_assisted",
            permission_ref="permission-preflight:allowed",
            lease_ref="lease-not-issued:projector-rejected",
            verification_ref="verification:no-commit",
            outcome_ref="outcome:no-commit",
        )

    model_ref = StableArtifactRef(
        ref_id="model-observation",
        uri="artifact://model-observation",
        digest="b" * 64,
    )
    input_ref = StableArtifactRef(
        ref_id="model-input",
        uri="artifact://model-input",
        digest="c" * 64,
    )
    output_ref = StableArtifactRef(
        ref_id="model-output",
        uri="artifact://model-output",
        digest="d" * 64,
    )
    model = PretrainedModelObservation(
        model_id="fixed-embedding-model",
        model_version="2026-07-read-only",
        input_ref=input_ref,
        output_ref=output_ref,
        observation_ref=model_ref,
        confidence=0.8,
    )
    assisted = builder.build(
        header=_header(
            "bundle-real-model-receipt",
            mechanism="NeuroSymbolicEvidenceBuilder",
        ),
        proposal=proposal,
        proposal_ref=proposal_ref,
        projection=projection,
        attack_class="pending_side_effect",
        proposal_signal_mode="pretrained_model_assisted",
        permission_ref="permission-preflight:allowed",
        lease_ref="lease-not-issued:projector-rejected",
        verification_ref="verification:no-commit",
        outcome_ref="outcome:no-commit",
        model_observations=(model,),
    )
    assert assisted.model_observation_refs == (model_ref,)
    assert (
        assisted.commit_or_no_commit["pretrained_model_observations"][0][
            "model_version"
        ]
        == "2026-07-read-only"
    )


def test_projector_bypass_is_test_only_counterfactual_and_production_unreachable(
    tmp_path: Path,
) -> None:
    custody = _custody(tmp_path / "graph.sqlite3")
    policy_input = _input(custody)
    privacy_attack = replace(
        _operation("node-forbidden-cloud"),
        requested_placement="cloud",
    )
    proposal = _proposal(
        policy_input,
        proposal_id="proposal-privacy-attack",
        operation=privacy_attack,
    )
    with pytest.raises(ValueError, match="test-only"):
        TopologyConstraintProjector(custody, enabled=False)
    disabled = TopologyConstraintProjector(
        custody,
        enabled=False,
        test_mode=True,
    ).execute(
        policy_input,
        proposal,
        decision_id="decision-disabled-projector",
        evaluated_at=NOW,
    )
    assert disabled.receipt.disposition is PolicyDecisionDisposition.REJECT
    assert custody.current("graph-neuro-symbolic").revision == 0

    with pytest.raises(PermissionError, match="internal"):
        PolicyDeltaBuilder().build(
            current_snapshot=custody.current("graph-neuro-symbolic"),
            policy_input=policy_input,
            proposal=proposal,
            decision_id="offline-direct-adapter-attempt",
        )

    # Offline mutation harness: deliberately omit the projector and show why
    # privacy/budget constraints must remain in front of canonical custody.
    raw = GraphDeltaBuilder(
        custody.current("graph-neuro-symbolic"),
        branch_id="test-only-projector-bypass",
        actor_id="test-only-mutation-harness",
        causation_id="offline-counterfactual",
        correlation_id="offline-counterfactual",
        idempotency_key="offline-counterfactual",
        metadata={"test_only": True, "release_runtime_reachable": False},
    )
    raw.add_node(
        GraphNode(
            node_id="node-forbidden-cloud",
            role="worker",
            capabilities=("execute",),
            metadata=FrozenDict(
                {
                    "requested_placement": "cloud",
                    "privacy_class": "internal",
                    "permission_checked": False,
                }
            ),
        )
    )
    counterfactual = custody.commit(raw.build())
    assert counterfactual.receipt.committed is True
    assert custody.current("graph-neuro-symbolic").revision == 1

    package = (
        Path(__file__).resolve().parents[2]
        / "packages"
        / "orchestration"
        / "zyra_orchestration"
        / "topology_policy"
    )
    authorization_users: list[str] = []
    bypass_tokens: list[str] = []
    for source in package.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        if "_PROJECTOR_AUTHORIZATION" in text:
            authorization_users.append(source.name)
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and "bypass" in node.id.lower():
                bypass_tokens.append(f"{source.name}:{node.id}")
    assert sorted(authorization_users) == ["delta_builder.py", "projector.py"]
    assert bypass_tokens == []


def test_slice_release_paths_pass_no_policy_training_audit() -> None:
    root = Path(__file__).resolve().parents[2]
    NoPolicyTrainingValidator().validate_release_inputs(
        root,
        release_paths=(
            "packages/memory/zyra_memory/fabric.py",
            "packages/orchestration/zyra_orchestration/topology_policy/continuity.py",
            "packages/orchestration/zyra_orchestration/topology_policy/projector.py",
            "packages/orchestration/zyra_orchestration/topology_policy/delta_builder.py",
            "packages/evaluation/zyra_evaluation/policy_benchmark/continuity.py",
            "packages/evaluation/zyra_evaluation/policy_benchmark/neuro_symbolic.py",
            "packages/evaluation/zyra_evaluation/policy_benchmark/data/continuity_neuro_symbolic_adversarial_v1.json",
        ),
        dependencies=(),
    )
