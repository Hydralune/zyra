from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

from zyra_orchestration.graph_custody import (
    GraphConflictStrategy,
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
    PolicyBudget,
    PolicyDecisionDisposition,
    PolicyDeltaBuilder,
    PolicyInputSnapshot,
    PolicyNodeSnapshot,
    TelemetryObservation,
    TopologyConstraintProjector,
    TopologyOperation,
    TopologyOperationKind,
    TopologyProposalArtifact,
)


NOW = "2026-07-29T12:00:00Z"


def _header(
    contract_id: str,
    *,
    mechanism: str = "ARG",
    idempotency_key: str = "",
) -> ContractHeader:
    return ContractHeader(
        contract_id=contract_id,
        created_at=NOW,
        source_event_id="event-policy-source",
        correlation_id="correlation-policy",
        causation_id="causation-policy",
        mechanism_id=mechanism,
        mechanism_version="deterministic-adapter-v1",
        input_version="owner-snapshot-v1",
        idempotency_key=idempotency_key or f"idempotency:{contract_id}",
    )


def _custody(path: Path) -> GraphStateCustody:
    store = GraphStateStore(path)
    store.initialize()
    custody = GraphStateCustody(store)
    custody.create(graph_id_value="graph-policy", run_id="run-policy")
    return custody


def _environment(*, fresh_until: str = "2026-07-29T12:05:00Z") -> EnvironmentSnapshot:
    return EnvironmentSnapshot(
        header=_header("environment", mechanism="ResourceScheduler"),
        observed_at=NOW,
        observations=(
            TelemetryObservation(
                observation_id="observation-worker-edge",
                resource_id="worker-edge",
                category="worker",
                observed_at=NOW,
                fresh_until=fresh_until,
                confidence=1.0,
                observation_source="ResourceScheduler.worker_pool_api_projection",
                source_event_id="event-policy-source",
                physical_runtime_id="edge-process-17",
                location="edge",
                available=True,
                healthy=True,
                capacity_available=2,
                lease_available=True,
                privacy_classes=("internal",),
                allowed_placements=("edge",),
            ),
        ),
        required_categories=("worker",),
    )


def _input(
    custody: GraphStateCustody,
    *,
    readiness: str = "deterministic_ready",
    environment: EnvironmentSnapshot | None = None,
    budget: PolicyBudget | None = None,
) -> PolicyInputSnapshot:
    current = custody.current("graph-policy")
    return PolicyInputSnapshot(
        header=_header(f"input-r{current.revision}", mechanism="PolicyInputSnapshotBuilder"),
        run_id=current.run_id,
        task_id="task-policy",
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
        registered_roles=("worker", "reviewer"),
        registered_capabilities=("execute", "verify"),
        unresolved_obligations=("produce-artifact",),
        registry_versions=FrozenDict({"role_registry": "r1", "capability_registry": "r1"}),
        environment=environment or _environment(),
        memory_refs=(),
        readiness_refs=(
            MechanismEvidenceReadinessReportRef(
                header=_header("readiness"),
                report_ref="artifact://readiness",
                report_digest="a" * 64,
                readiness_stage=(
                    "activation_ready" if readiness == "deterministic_ready" else "input_precheck"
                ),
                status=readiness,
            ),
        ),
        budget=budget
        or PolicyBudget(
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
        last_topology_change_at="2026-07-29T11:00:00Z",
    )


def _proposal(
    policy_input: PolicyInputSnapshot,
    operations: tuple[TopologyOperation, ...],
    *,
    proposal_id: str = "proposal-policy",
    idempotency_key: str = "policy-key",
    expires_at: str = "2026-07-29T12:05:00Z",
    expected_outcome: FrozenDict | None = None,
) -> TopologyProposalArtifact:
    return TopologyProposalArtifact(
        header=_header(
            proposal_id,
            idempotency_key=idempotency_key,
        ),
        proposal_id=proposal_id,
        input_snapshot_digest=policy_input.digest,
        base_graph=policy_input.graph,
        operations=operations,
        expected_outcome=expected_outcome
        or FrozenDict({"tokens": 10, "cost_usd": 0.01, "time_ms": 10}),
        alternatives=(),
        reasons=("deterministic topology proposal",),
        constraint_assumptions=(),
        expires_at=expires_at,
        fallback_profile="phase1_deterministic_baseline",
    )


def _add_node(node_id: str, *, role: str = "worker", dependencies=()) -> TopologyOperation:
    return TopologyOperation(
        kind=TopologyOperationKind.ADD_NODE,
        entity_id=node_id,
        value=FrozenDict(
            {
                "role": role,
                "capabilities": ["execute"],
                "dependencies": list(dependencies),
            }
        ),
        required_permissions=("graph.write",),
        requested_placement="edge",
        resource_id="worker-edge",
        required_capacity=1,
        communication_bytes=64,
        reason="add an execution role",
    )


def _reason_codes(result) -> set[str]:
    return {item.reason_code for item in result.receipt.constraint_results}


def test_policy_proposal_is_projected_committed_and_idempotently_replayed(tmp_path: Path) -> None:
    custody = _custody(tmp_path / "graph.sqlite3")
    policy_input = _input(custody)
    proposal = _proposal(policy_input, (_add_node("node-a"),))
    projector = TopologyConstraintProjector(custody)

    accepted = projector.execute(
        policy_input,
        proposal,
        decision_id="decision-accepted",
        evaluated_at=NOW,
    )
    assert accepted.receipt.disposition is PolicyDecisionDisposition.ACCEPT
    assert accepted.commit is not None
    assert accepted.delta is not None
    assert accepted.delta.metadata["policy_path"].endswith(
        "constraint_projector->GraphDeltaBuilder"
    )
    assert custody.current("graph-policy").node_map["node-a"].role == "worker"
    assert custody.current("graph-policy").revision == 1

    replay = projector.execute(
        policy_input,
        proposal,
        decision_id="decision-replay",
        evaluated_at=NOW,
    )
    assert replay.receipt.disposition is PolicyDecisionDisposition.REPLAY
    assert "idempotent_replay" in _reason_codes(replay)
    assert replay.commit is None
    assert replay.delta is not None
    assert replay.delta.delta_id == accepted.delta.delta_id
    assert replay.delta.content_digest == accepted.delta.content_digest
    assert custody.current("graph-policy").revision == 1


def test_stale_unknown_expired_unready_and_disabled_paths_fail_closed(tmp_path: Path) -> None:
    custody = _custody(tmp_path / "graph.sqlite3")
    projector = TopologyConstraintProjector(custody)

    original_input = _input(custody)
    first = _proposal(original_input, (_add_node("node-a"),), idempotency_key="first")
    assert projector.execute(
        original_input,
        first,
        decision_id="decision-first",
        evaluated_at=NOW,
    ).receipt.accepted

    stale = projector.execute(
        original_input,
        _proposal(original_input, (_add_node("node-b"),), idempotency_key="stale"),
        decision_id="decision-stale",
        evaluated_at=NOW,
    )
    assert stale.receipt.disposition is PolicyDecisionDisposition.REJECT
    assert "stale_or_mismatched_snapshot" in _reason_codes(stale)

    current_input = _input(custody)
    unknown = projector.execute(
        current_input,
        _proposal(
            current_input,
            (_add_node("node-unknown", role="unregistered-role"),),
            idempotency_key="unknown",
        ),
        decision_id="decision-unknown",
        evaluated_at=NOW,
    )
    assert "unknown_role_or_capability" in _reason_codes(unknown)

    expired = projector.execute(
        current_input,
        _proposal(
                current_input,
                (_add_node("node-expired"),),
                idempotency_key="expired",
                expires_at="2026-07-29T12:01:00Z",
            ),
            decision_id="decision-expired",
            evaluated_at="2026-07-29T12:02:00Z",
        )
    assert "proposal_expired" in _reason_codes(expired)

    unavailable_input = _input(custody, readiness="unavailable")
    unavailable = projector.execute(
        unavailable_input,
        _proposal(
            unavailable_input,
            (_add_node("node-unavailable"),),
            idempotency_key="unavailable",
        ),
        decision_id="decision-unavailable",
        evaluated_at=NOW,
    )
    assert "mechanism_not_activation_ready" in _reason_codes(unavailable)

    premature_ref = replace(
        current_input.readiness_refs[0],
        readiness_stage="input_precheck",
    )
    premature_input = replace(current_input, readiness_refs=(premature_ref,))
    premature = projector.execute(
        premature_input,
        _proposal(
            premature_input,
            (_add_node("node-premature"),),
            idempotency_key="premature",
        ),
        decision_id="decision-premature",
        evaluated_at=NOW,
    )
    assert "mechanism_not_activation_ready" in _reason_codes(premature)

    forged_nodes = tuple(
        replace(node, role="reviewer") if node.node_id == "node-a" else node
        for node in current_input.nodes
    )
    forged_input = replace(current_input, nodes=forged_nodes)
    forged = projector.execute(
        forged_input,
        _proposal(
            forged_input,
            (_add_node("node-forged"),),
            idempotency_key="forged",
        ),
        decision_id="decision-forged",
        evaluated_at=NOW,
    )
    assert "stale_or_mismatched_snapshot" in _reason_codes(forged)

    disabled = TopologyConstraintProjector(
        custody,
        enabled=False,
        test_mode=True,
    ).execute(
        current_input,
        _proposal(
            current_input,
            (_add_node("node-disabled"),),
            idempotency_key="disabled",
        ),
        decision_id="decision-disabled",
        evaluated_at=NOW,
    )
    assert "policy_projector_disabled" in _reason_codes(disabled)
    assert disabled.receipt.fallback_profile == "phase1_deterministic_baseline"
    assert custody.current("graph-policy").revision == 1


def test_permission_budget_capacity_cycle_and_idempotency_mismatch_are_rejected(
    tmp_path: Path,
) -> None:
    custody = _custody(tmp_path / "graph.sqlite3")
    projector = TopologyConstraintProjector(custody)
    policy_input = _input(custody)

    denied_operation = replace(
        _add_node("node-denied"),
        required_permissions=("graph.admin",),
    )
    denied = projector.execute(
        policy_input,
        _proposal(policy_input, (denied_operation,), idempotency_key="denied"),
        decision_id="decision-denied",
        evaluated_at=NOW,
    )
    assert "permission_or_placement_denied" in _reason_codes(denied)

    undeclared = replace(_add_node("node-undeclared"), required_permissions=())
    missing_permission = projector.execute(
        policy_input,
        _proposal(policy_input, (undeclared,), idempotency_key="undeclared"),
        decision_id="decision-undeclared",
        evaluated_at=NOW,
    )
    assert "permission_or_placement_denied" in _reason_codes(missing_permission)

    no_resource = replace(_add_node("node-no-resource"), resource_id="")
    missing_resource = projector.execute(
        policy_input,
        _proposal(policy_input, (no_resource,), idempotency_key="no-resource"),
        decision_id="decision-no-resource",
        evaluated_at=NOW,
    )
    assert "permission_or_placement_denied" in _reason_codes(missing_resource)

    over_capacity = replace(_add_node("node-capacity"), required_capacity=3)
    capacity = projector.execute(
        policy_input,
        _proposal(policy_input, (over_capacity,), idempotency_key="capacity"),
        decision_id="decision-capacity",
        evaluated_at=NOW,
    )
    assert "capacity_lease_or_telemetry_invalid" in _reason_codes(capacity)

    budget = projector.execute(
        policy_input,
        _proposal(
            policy_input,
            (_add_node("node-budget"),),
            idempotency_key="budget",
            expected_outcome=FrozenDict({"tokens": 1001, "cost_usd": 0.01, "time_ms": 10}),
        ),
        decision_id="decision-budget",
        evaluated_at=NOW,
    )
    assert "budget_exceeded" in _reason_codes(budget)

    cycle = projector.execute(
        policy_input,
        _proposal(
            policy_input,
            (
                _add_node("node-cycle-a", dependencies=("node-cycle-b",)),
                _add_node("node-cycle-b", dependencies=("node-cycle-a",)),
            ),
            idempotency_key="cycle",
        ),
        decision_id="decision-cycle",
        evaluated_at=NOW,
    )
    assert "graph_constraint_rejected" in _reason_codes(cycle)
    assert custody.current("graph-policy").revision == 0

    accepted = projector.execute(
        policy_input,
        _proposal(policy_input, (_add_node("node-a"),), idempotency_key="duplicate"),
        decision_id="decision-duplicate-a",
        evaluated_at=NOW,
    )
    assert accepted.receipt.disposition is PolicyDecisionDisposition.ACCEPT
    current_input = _input(custody)
    mismatch = projector.execute(
        current_input,
        _proposal(
            current_input,
            (
                TopologyOperation(
                    kind=TopologyOperationKind.SET_GRAPH_METADATA,
                    entity_id="different-content",
                    value=FrozenDict({"value": True}),
                    required_permissions=("graph.write",),
                    communication_bytes=1,
                ),
            ),
            idempotency_key="duplicate",
            proposal_id="proposal-different",
        ),
        decision_id="decision-duplicate-b",
        evaluated_at=NOW,
    )
    assert "idempotency_mismatch" in _reason_codes(mismatch)
    assert custody.current("graph-policy").revision == 1


def test_real_graph_commit_receipts_distinguish_rebase_and_conflict(tmp_path: Path) -> None:
    class RacingDeltaBuilder(PolicyDeltaBuilder):
        def __init__(self, custody: GraphStateCustody, *, conflict: bool) -> None:
            self.custody = custody
            self.conflict = conflict

        def build(self, **kwargs):
            delta = super().build(**kwargs)
            branch = self.custody.branch(
                "graph-policy",
                branch_id=f"race:{'conflict' if self.conflict else 'rebase'}",
                actor_id="race-test",
                causation_id="race-test",
                idempotency_key=f"race:{'conflict' if self.conflict else 'rebase'}",
            )
            if self.conflict:
                branch.add_node(
                    GraphNode(
                        node_id="node-policy",
                        role="worker",
                        capabilities=("execute",),
                    )
                )
            else:
                branch.set_metadata("disjoint-race", True)
            assert self.custody.commit(branch.build()).receipt.committed
            return delta

    rebase_custody = _custody(tmp_path / "rebase.sqlite3")
    rebase_input = _input(rebase_custody)
    rebase = TopologyConstraintProjector(
        rebase_custody,
        delta_builder=RacingDeltaBuilder(rebase_custody, conflict=False),
    ).execute(
        rebase_input,
        _proposal(rebase_input, (_add_node("node-policy"),), idempotency_key="rebase"),
        decision_id="decision-rebase",
        evaluated_at=NOW,
        strategy=GraphConflictStrategy.REBASE,
    )
    assert rebase.receipt.disposition is PolicyDecisionDisposition.REBASE
    assert rebase.commit is not None
    assert rebase_custody.current("graph-policy").revision == 2

    conflict_custody = _custody(tmp_path / "conflict.sqlite3")
    conflict_input = _input(conflict_custody)
    conflict = TopologyConstraintProjector(
        conflict_custody,
        delta_builder=RacingDeltaBuilder(conflict_custody, conflict=True),
    ).execute(
        conflict_input,
        _proposal(
            conflict_input,
            (_add_node("node-policy"),),
            idempotency_key="conflict",
        ),
        decision_id="decision-conflict",
        evaluated_at=NOW,
    )
    assert conflict.receipt.disposition is PolicyDecisionDisposition.CONFLICT
    assert conflict.receipt.fallback_profile == "phase1_deterministic_baseline"
    assert conflict.commit is not None
    assert conflict_custody.current("graph-policy").revision == 1


def test_only_policy_delta_adapter_imports_canonical_graph_delta_builder() -> None:
    package = (
        Path(__file__).resolve().parents[2]
        / "packages"
        / "orchestration"
        / "zyra_orchestration"
        / "topology_policy"
    )
    offenders = []
    for source in package.glob("*.py"):
        if source.name == "delta_builder.py":
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(
                alias.name == "GraphDeltaBuilder" for alias in node.names
            ):
                offenders.append(source.name)
    assert offenders == []
