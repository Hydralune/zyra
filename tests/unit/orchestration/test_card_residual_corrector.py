from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from zyra_orchestration.graph_custody import GraphStateSnapshot
from zyra_orchestration.topology_policy import (
    ContractHeader,
    EnvironmentSnapshot,
    FrozenDict,
    GraphSnapshotRef,
    MechanismEvidenceReadinessReportRef,
    PolicyBudget,
    PolicyInputSnapshot,
    TelemetryObservation,
    TopologyOperation,
    TopologyOperationKind,
    TopologyProposalArtifact,
)
from zyra_orchestration.topology_policy.condition import (
    CARDEnvironmentEncoder,
    CARDEnvironmentError,
    CARDHysteresisController,
    CARDReplacementCandidate,
    CARDResidualCorrector,
    CARDResidualCorrectorConfig,
)


ROOT = Path(__file__).resolve().parents[3]
NOW = "2026-07-30T08:00:00Z"
REPORT_DIGEST = "a" * 64


def _header(contract_id: str, mechanism_id: str) -> ContractHeader:
    return ContractHeader(
        contract_id=contract_id,
        created_at=NOW,
        source_event_id="event-card-telemetry",
        correlation_id="correlation-card",
        causation_id="arg-proposal-source",
        mechanism_id=mechanism_id,
        mechanism_version="v1",
        input_version="v1",
        idempotency_key=f"idempotency:{contract_id}",
    )


def _worker_observation(
    worker_id: str,
    *,
    location: str,
    capabilities: tuple[str, ...],
    latency_p50_ms: float,
    latency_p95_ms: float,
    load: float,
    cost_usd: float,
    available: bool = True,
    healthy: bool = True,
    observed_at: str = NOW,
    fresh_until: str = "2026-07-30T08:10:00Z",
    allowed_placements: tuple[str, ...] | None = None,
    privacy_classes: tuple[str, ...] = ("internal", "sensitive"),
    physical_runtime_id: str | None = None,
    linked_resource_ids: tuple[str, ...] = (),
    network_state: str = "connected",
) -> TelemetryObservation:
    return TelemetryObservation(
        observation_id=f"observation-{worker_id}-{location}",
        resource_id=worker_id,
        category="worker",
        observed_at=observed_at,
        fresh_until=fresh_until,
        confidence=1.0,
        observation_source="ResourceScheduler.worker_pool_api_projection",
        source_event_id="event-card-telemetry",
        physical_runtime_id=(
            physical_runtime_id
            if physical_runtime_id is not None
            else f"{location}-runtime-{worker_id}"
        ),
        location=location,
        available=available,
        healthy=healthy,
        load=load,
        capacity_available=2 if available else 0,
        lease_available=available,
        recent_failures=0 if healthy else 2,
        latency_p50_ms=latency_p50_ms,
        latency_p95_ms=latency_p95_ms,
        cost_usd=cost_usd,
        privacy_classes=privacy_classes,
        allowed_placements=allowed_placements or (location,),
        attributes=FrozenDict(
            {
                "capabilities": list(capabilities),
                "queue_depth": 0,
                "recent_successes": 8,
                "prompt_tokens": 120,
                "completion_tokens": 40,
                "network_state": network_state,
                "linked_resource_ids": list(linked_resource_ids),
                "fault": not healthy,
                "requirement_change": False,
                "compact": False,
                "recovery": False,
            }
        ),
    )


def _linked_observation(
    resource_id: str,
    category: str,
    *,
    cost_usd: float = 0,
    latency_p95_ms: float = 0,
) -> TelemetryObservation:
    return TelemetryObservation(
        observation_id=f"observation-{category}-{resource_id}",
        resource_id=resource_id,
        category=category,
        observed_at=NOW,
        fresh_until="2026-07-30T08:10:00Z",
        confidence=0.95,
        observation_source=f"{category}.registry.telemetry",
        source_event_id="event-card-telemetry",
        physical_runtime_id=f"{category}-runtime-{resource_id}",
        location="cloud",
        available=True,
        healthy=True,
        load=0.2,
        capacity_available=10,
        lease_available=True,
        latency_p50_ms=latency_p95_ms / 2,
        latency_p95_ms=latency_p95_ms,
        cost_usd=cost_usd,
        privacy_classes=("internal",),
        allowed_placements=("cloud",),
        attributes=FrozenDict(
            {
                "prompt_tokens": 500,
                "completion_tokens": 200,
                "network_state": "connected",
            }
        ),
    )


def _environment(
    *,
    worker_b_available: bool = True,
    worker_b_healthy: bool = True,
    worker_b_fresh_until: str = "2026-07-30T08:10:00Z",
    worker_b_runtime: str | None = None,
    worker_b_network: str = "connected",
) -> EnvironmentSnapshot:
    return EnvironmentSnapshot(
        header=_header("environment-card", "ResourceScheduler"),
        observed_at=NOW,
        observations=(
            _worker_observation(
                "worker-a",
                location="local",
                capabilities=("research", "execution"),
                latency_p50_ms=20,
                latency_p95_ms=40,
                load=0.1,
                cost_usd=0.01,
                allowed_placements=("local",),
            ),
            _worker_observation(
                "worker-b",
                location="cloud",
                capabilities=("verification", "model_reasoning"),
                latency_p50_ms=700,
                latency_p95_ms=1800,
                load=0.8,
                cost_usd=0.8,
                available=worker_b_available,
                healthy=worker_b_healthy,
                observed_at=(
                    "2026-07-30T07:58:00Z"
                    if worker_b_fresh_until < NOW
                    else NOW
                ),
                fresh_until=worker_b_fresh_until,
                physical_runtime_id=worker_b_runtime,
                allowed_placements=("cloud",),
                linked_resource_ids=("provider-b", "model-b", "tool-b"),
                network_state=worker_b_network,
            ),
            _worker_observation(
                "worker-c",
                location="edge",
                capabilities=("recovery", "artifact_production"),
                latency_p50_ms=80,
                latency_p95_ms=180,
                load=0.25,
                cost_usd=0.05,
                allowed_placements=("edge",),
            ),
            _linked_observation(
                "provider-b",
                "provider",
                cost_usd=0.25,
                latency_p95_ms=900,
            ),
            _linked_observation(
                "model-b",
                "model",
                cost_usd=0.1,
                latency_p95_ms=300,
            ),
            _linked_observation("tool-b", "tool"),
        ),
        required_categories=("worker",),
    )


def _graph() -> GraphStateSnapshot:
    return GraphStateSnapshot(
        graph_id="graph-card",
        run_id="run-card",
        revision=0,
        nodes=(),
        edges=(),
        metadata={
            "graph_state_owner": "GraphStateCustody",
            "topology_owner": "DynamicTopologyRuntime",
        },
        created_at=NOW,
    )


def _policy_input(
    *,
    graph: GraphStateSnapshot,
    environment: EnvironmentSnapshot,
    allowed_placements: tuple[str, ...] = ("local", "edge", "cloud"),
    privacy_class: str = "internal",
) -> PolicyInputSnapshot:
    return PolicyInputSnapshot(
        header=_header("policy-input-card", "PolicyInputSnapshotBuilder"),
        run_id=graph.run_id,
        task_id="task-card",
        phase="execution",
        requirement_revision="requirement-r1",
        graph=GraphSnapshotRef(
            graph_id=graph.graph_id,
            run_id=graph.run_id,
            revision=graph.revision,
            signature=graph.signature,
            commit_id=graph.commit_id,
        ),
        nodes=(),
        registered_roles=("researcher", "verifier", "recovery"),
        registered_capabilities=(
            "research",
            "execution",
            "verification",
            "recovery",
            "artifact_production",
        ),
        unresolved_obligations=("execute", "verify", "recover"),
        registry_versions=FrozenDict(
            {
                "worker_registry_revision": 3,
                "provider_registry_revision": 2,
                "model_registry_revision": 4,
                "tool_registry_revision": 5,
            }
        ),
        environment=environment,
        memory_refs=(),
        readiness_refs=(
            MechanismEvidenceReadinessReportRef(
                header=_header("readiness-card", "card"),
                report_ref="artifact://card-readiness",
                report_digest=REPORT_DIGEST,
                readiness_stage="implementation_validated",
                status="deterministic_ready",
            ),
        ),
        budget=PolicyBudget(
            remaining_tokens=10_000,
            remaining_cost_usd=10,
            remaining_time_ms=120_000,
            max_communication_bytes=16_384,
            max_fan_out=4,
            max_topology_churn=20,
            minimum_dwell_seconds=0,
        ),
        allowed_permissions=("graph.write",),
        allowed_placements=allowed_placements,
        privacy_class=privacy_class,
        last_topology_change_at="2026-07-30T07:00:00Z",
    )


def _arg_base(
    policy_input: PolicyInputSnapshot,
    *,
    reciprocal: bool = True,
    include_temporal: bool = True,
    include_edges: bool = True,
) -> TopologyProposalArtifact:
    node_values = (
        ("node-a", "researcher", "worker-a", ("research", "execution"), "local"),
        (
            "node-b",
            "verifier",
            "worker-b",
            ("verification", "model_reasoning"),
            "cloud",
        ),
        (
            "node-c",
            "recovery",
            "worker-c",
            ("recovery", "artifact_production"),
            "edge",
        ),
    )
    incident = {
        "node-a": [],
        "node-b": [],
        "node-c": [],
    }
    if include_edges:
        incident["node-b"].append(
            {
                "edge_id": "edge-a-b",
                "source_node_id": "node-a",
                "target_node_id": "node-b",
                "relation": "arg_dependency",
                "edge_type": "spatial",
                "persisted": True,
                "required_capabilities": ["verification"],
                "reason": "ARG base A to B",
            }
        )
        if reciprocal:
            incident["node-a"].append(
                {
                    "edge_id": "edge-b-a",
                    "source_node_id": "node-b",
                    "target_node_id": "node-a",
                    "relation": "arg_dependency",
                    "edge_type": "spatial",
                    "persisted": True,
                    "required_capabilities": ["execution"],
                    "reason": "ARG base B to A",
                }
            )
        if include_temporal:
            incident["node-c"].append(
                {
                    "edge_id": "edge-b-c-temporal",
                    "source_node_id": "node-b",
                    "target_node_id": "node-c",
                    "relation": "temporal_memory",
                    "edge_type": "temporal",
                    "persisted": True,
                    "required_capabilities": ["recovery"],
                    "reason": "ARG temporal B to C",
                }
            )
    steps = [
        {
            "step_index": index,
            "token": role,
            "role_id": role,
            "node_id": node_id,
            "binding_id": f"binding-{worker}",
            "worker_id": worker,
            "capabilities": list(capabilities),
            "incident_edges": incident[node_id],
            "score": {"total": 1.0},
            "state_digest": f"state-{index}",
            "cold_start": False,
            "reasons": ["ARG joint role-node-edge"],
        }
        for index, (node_id, role, worker, capabilities, _) in enumerate(
            node_values
        )
    ]
    operations = [
        TopologyOperation(
            kind=TopologyOperationKind.ADD_NODE,
            entity_id=node_id,
            value=FrozenDict(
                {
                    "role": role,
                    "capabilities": list(capabilities),
                    "dependencies": [],
                }
            ),
            required_permissions=("graph.write",),
            requested_placement=placement,
            resource_id=worker,
            required_capacity=1,
            reason="ARG joint role-node",
        )
        for node_id, role, worker, capabilities, placement in node_values
    ]
    for values in incident.values():
        for edge in values:
            operations.append(
                TopologyOperation(
                    kind=TopologyOperationKind.ADD_EDGE,
                    entity_id=edge["edge_id"],
                    value=FrozenDict(edge),
                    required_permissions=("graph.write",),
                    communication_bytes=96,
                    reason=edge["reason"],
                )
            )
    return TopologyProposalArtifact(
        header=_header("arg-proposal-card-input", "arg_designer"),
        proposal_id="arg-proposal-card-input",
        input_snapshot_digest=policy_input.digest,
        base_graph=policy_input.graph,
        operations=tuple(operations),
        expected_outcome=FrozenDict(
            {
                "mechanism": "ARG deterministic joint role-node-edge",
                "requirement_revision": policy_input.requirement_revision,
                "joint_hypothesis": {
                    "hypothesis_id": "arg-hypothesis-card",
                    "steps": steps,
                    "end_reason": "obligations_covered",
                },
            }
        ),
        alternatives=(),
        reasons=("ARG base topology",),
        constraint_assumptions=("GraphStateCustody owns commits",),
        expires_at="2026-07-30T08:05:00Z",
        fallback_profile="phase1_deterministic_baseline",
    )


def _encode_and_correct(
    *,
    environment: EnvironmentSnapshot | None = None,
    allowed_placements: tuple[str, ...] = ("local", "edge", "cloud"),
    reciprocal: bool = True,
    include_temporal: bool = True,
    include_edges: bool = True,
    candidates: tuple[CARDReplacementCandidate, ...] = (),
    hysteresis_state=None,
):
    config = CARDResidualCorrectorConfig.load(
        ROOT / "config" / "phase2" / "card-directional-residual.json"
    )
    graph = _graph()
    selected_environment = environment or _environment()
    policy_input = _policy_input(
        graph=graph,
        environment=selected_environment,
        allowed_placements=allowed_placements,
    )
    arg_base = _arg_base(
        policy_input,
        reciprocal=reciprocal,
        include_temporal=include_temporal,
        include_edges=include_edges,
    )
    encoded = CARDEnvironmentEncoder().encode(
        policy_input=policy_input,
        arg_base=arg_base,
        mechanism_version=config.mechanism_version,
        configuration_digest=config.digest,
        required_categories=config.required_categories,
        optional_categories=config.optional_categories,
        required_observation_fields=config.required_observation_fields,
        minimum_confidence=config.minimum_confidence,
        stale_confidence_multiplier=config.stale_confidence_multiplier,
        replacement_candidates=candidates,
    )
    correction = CARDResidualCorrector(config).correct(
        encoded,
        hysteresis_state=hysteresis_state,
    )
    return config, graph, policy_input, arg_base, encoded, correction


def test_directional_scores_use_real_feature_lineage_and_separate_edge_types() -> None:
    config, _, policy_input, arg_base, encoded, correction = _encode_and_correct()
    forward = next(
        item
        for item in correction.decisions
        if item.source_node_id == "node-a" and item.target_node_id == "node-b"
    )
    reverse = next(
        item
        for item in correction.decisions
        if item.source_node_id == "node-b" and item.target_node_id == "node-a"
    )
    temporal = next(
        item for item in correction.decisions if item.edge_type == "temporal"
    )

    assert forward.score.total != reverse.score.total
    assert forward.score.latency < reverse.score.latency
    assert reverse.action == "reweight"
    assert set(forward.feature_contributions) == {
        "capability",
        "health",
        "latency",
        "load",
        "cost",
        "privacy",
        "freshness",
    }
    assert {
        item["category"] for item in forward.feature_lineage
    } >= {"worker", "provider", "model", "tool"}
    assert temporal.edge_id in correction.effective_temporal_edge_ids
    assert encoded.environment_snapshot_digest == policy_input.environment.digest
    assert correction.arg_base_proposal_digest == arg_base.digest
    proposal = CARDResidualCorrector(config).build_proposal(
        policy_input=policy_input,
        arg_base=arg_base,
        encoded=encoded,
        correction=correction,
        readiness_stage="implementation_validated",
        readiness_status="deterministic_ready",
        readiness_report_digest=REPORT_DIGEST,
    )
    assert proposal is not None
    assert proposal.header.causation_id == arg_base.proposal_id
    assert proposal.expected_outcome["correction_digest"] == correction.digest


def test_failure_disconnect_and_privacy_remove_directional_arg_edges() -> None:
    failed = _encode_and_correct(
        environment=_environment(
            worker_b_available=False,
            worker_b_healthy=False,
        )
    )[-1]
    failed_edges = {
        item.edge_id: item
        for item in failed.decisions
        if "node-b" in {item.source_node_id, item.target_node_id}
    }
    assert failed_edges
    assert all(item.action == "drop" for item in failed_edges.values())
    assert all(
        any("unavailable" in reason for reason in item.rejection_reasons)
        for item in failed_edges.values()
    )

    disconnected = _encode_and_correct(
        environment=_environment(worker_b_network="disconnected")
    )[-1]
    assert any(
        item.action == "drop"
        and any("network_disconnected" in reason for reason in item.rejection_reasons)
        for item in disconnected.decisions
    )

    privacy = _encode_and_correct(allowed_placements=("local", "edge"))[-1]
    cloud_edges = [
        item
        for item in privacy.decisions
        if "node-b" in {item.source_node_id, item.target_node_id}
    ]
    assert cloud_edges
    assert all(item.action == "drop" for item in cloud_edges)
    assert all(
        "privacy_or_placement_forbidden" in item.rejection_reasons
        for item in cloud_edges
    )


def test_stale_and_missing_telemetry_fail_closed_with_explicit_missingness() -> None:
    stale = _encode_and_correct(
        environment=_environment(
            worker_b_fresh_until="2026-07-30T07:59:00Z"
        )
    )[-1]
    affected = [
        item
        for item in stale.decisions
        if "node-b" in {item.source_node_id, item.target_node_id}
    ]
    assert affected
    assert all(item.action in {"hold", "reject"} for item in affected)
    assert all(item.confidence < 0.5 for item in affected)
    assert all(
        any(reason.startswith("stale_telemetry") for reason in item.rejection_reasons)
        for item in affected
    )

    config = CARDResidualCorrectorConfig.load(
        ROOT / "config" / "phase2" / "card-directional-residual.json"
    )
    graph = _graph()
    environment = _environment(worker_b_runtime="unresolved:worker-b")
    policy_input = _policy_input(graph=graph, environment=environment)
    arg_base = _arg_base(policy_input)
    with pytest.raises(CARDEnvironmentError, match="missing required"):
        CARDEnvironmentEncoder().encode(
            policy_input=policy_input,
            arg_base=arg_base,
            mechanism_version=config.mechanism_version,
            configuration_digest=config.digest,
            required_categories=config.required_categories,
            optional_categories=config.optional_categories,
            required_observation_fields=config.required_observation_fields,
            minimum_confidence=config.minimum_confidence,
            stale_confidence_multiplier=config.stale_confidence_multiplier,
        )

    complete_environment = _environment()
    missing_telemetry = replace(
        complete_environment,
        observations=tuple(
            replace(item, missing_fields=("telemetry",))
            if item.resource_id == "worker-a"
            else item
            for item in complete_environment.observations
        ),
    )
    missing_input = _policy_input(
        graph=graph,
        environment=missing_telemetry,
    )
    missing_arg = _arg_base(missing_input)
    with pytest.raises(CARDEnvironmentError, match="missing required"):
        CARDEnvironmentEncoder().encode(
            policy_input=missing_input,
            arg_base=missing_arg,
            mechanism_version=config.mechanism_version,
            configuration_digest=config.digest,
            required_categories=config.required_categories,
            optional_categories=config.optional_categories,
            required_observation_fields=config.required_observation_fields,
            minimum_confidence=config.minimum_confidence,
            stale_confidence_multiplier=config.stale_confidence_multiplier,
        )


def test_hysteresis_requires_confirmation_and_stable_noise_does_not_flip_edges() -> None:
    dwell_blocked = CARDHysteresisController.evaluate(
        edge_id="edge-dwell",
        requested_action="drop",
        score=-0.8,
        observed_at=NOW,
        active_before=True,
        last_topology_change_at=NOW,
        state=None,
        hard_constraint=False,
        minimum_dwell_seconds=30,
        switch_confirmations=1,
        reweight_epsilon=0.025,
        switch_cost=0.12,
    )
    assert dwell_blocked.applied_action == "hold"
    assert dwell_blocked.reason_code == "minimum_dwell_not_met"
    assert dwell_blocked.switch_cost == 0.12

    candidate = CARDReplacementCandidate(
        candidate_id="candidate-b-a",
        source_node_id="node-b",
        target_node_id="node-a",
        relation="card_replacement",
        edge_type="spatial",
        required_capabilities=("execution",),
        constraint_ref="constraint://acyclic-capability-checked",
        reason="upstream constrained alternative",
    )
    first = _encode_and_correct(
        reciprocal=False,
        candidates=(candidate,),
    )
    first_candidate = next(
        item for item in first[-1].decisions if item.edge_id == candidate.candidate_id
    )
    assert first_candidate.requested_action == "add"
    assert first_candidate.action == "hold"
    assert first_candidate.hysteresis.reason_code == "switch_confirmation_pending"

    second = _encode_and_correct(
        reciprocal=False,
        candidates=(candidate,),
        hysteresis_state={
            candidate.candidate_id: first_candidate.hysteresis.next_state
        },
    )[-1]
    second_candidate = next(
        item for item in second.decisions if item.edge_id == candidate.candidate_id
    )
    assert second_candidate.action == "add"
    assert second_candidate.active_after is True

    stable_state = {
        item.edge_id: replace(
            item.hysteresis.next_state,
            last_score=item.score.total + 0.01,
        )
        for item in first[-1].decisions
        if not item.candidate
    }
    stable = _encode_and_correct(
        reciprocal=False,
        candidates=(),
        hysteresis_state=stable_state,
    )[-1]
    assert all(item.action not in {"add", "drop"} for item in stable.decisions)


def test_fixed_snapshot_config_and_state_are_deterministic_and_empty_arg_cannot_expand() -> None:
    first = _encode_and_correct()
    second = _encode_and_correct()
    assert first[4].digest == second[4].digest
    assert first[5].digest == second[5].digest
    assert first[5].to_dict() == second[5].to_dict()

    config = first[0]
    policy_input = first[2]
    empty_arg = _arg_base(policy_input, include_edges=False)
    with pytest.raises(CARDEnvironmentError, match="cannot invent"):
        CARDEnvironmentEncoder().encode(
            policy_input=policy_input,
            arg_base=empty_arg,
            mechanism_version=config.mechanism_version,
            configuration_digest=config.digest,
            required_categories=config.required_categories,
            optional_categories=config.optional_categories,
            required_observation_fields=config.required_observation_fields,
            minimum_confidence=config.minimum_confidence,
            stale_confidence_multiplier=config.stale_confidence_multiplier,
        )
