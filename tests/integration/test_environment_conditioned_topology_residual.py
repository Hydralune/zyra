from __future__ import annotations

import json
from pathlib import Path

from zyra_orchestration.graph_custody import GraphStateCustody, GraphStateStore
from zyra_orchestration.topology_policy import (
    ContractHeader,
    EnvironmentSnapshot,
    FrozenDict,
    GraphSnapshotRef,
    MechanismEvidenceReadinessReportRef,
    PolicyBudget,
    PolicyEvidencePublisher,
    PolicyInputSnapshot,
    TelemetryObservation,
    TopologyOperation,
    TopologyOperationKind,
    TopologyProposalArtifact,
    canonical_digest,
)
from zyra_orchestration.topology_policy.condition import (
    CARDResidualCorrectorConfig,
    CARDTopologyRuntime,
)
from zyra_runtime import LocalArtifactStore


ROOT = Path(__file__).resolve().parents[2]
NOW = "2026-07-30T10:00:00Z"
INPUT_PRECHECK_DIGEST = (
    "82ad9a84a55506223fce2133168b7df2f0ae487aa7288fe5ede5c58869c1bbde"
)


def _header(contract_id: str, mechanism: str) -> ContractHeader:
    return ContractHeader(
        contract_id=contract_id,
        created_at=NOW,
        source_event_id="event-card-integration-telemetry",
        correlation_id="correlation-card-integration",
        causation_id="continuity-gate-card-integration",
        mechanism_id=mechanism,
        mechanism_version="v1",
        input_version="v1",
        idempotency_key=f"idempotency:{contract_id}",
    )


def _custody(tmp_path: Path) -> GraphStateCustody:
    store = GraphStateStore(tmp_path / "graph.sqlite3")
    store.initialize()
    custody = GraphStateCustody(store)
    custody.create(
        graph_id_value="graph-card-integration",
        run_id="run-card-integration",
    )
    return custody


def _environment(
    *,
    target_available: bool = True,
    target_network: str = "connected",
    target_location: str = "cloud",
) -> EnvironmentSnapshot:
    observations = []
    for worker_id, location, capabilities, latency, load, cost, available, network in (
        (
            "worker-source",
            "local",
            ("execution", "artifact_production"),
            30,
            0.1,
            0.01,
            True,
            "connected",
        ),
        (
            "worker-target",
            target_location,
            ("verification", "review"),
            900,
            0.7,
            0.6,
            target_available,
            target_network,
        ),
    ):
        observations.append(
            TelemetryObservation(
                observation_id=f"observation-{worker_id}-{location}",
                resource_id=worker_id,
                category="worker",
                observed_at=NOW,
                fresh_until="2026-07-30T10:10:00Z",
                confidence=1.0,
                observation_source=(
                    "ResourceScheduler.worker_pool_api_projection"
                ),
                source_event_id="event-card-integration-telemetry",
                physical_runtime_id=f"{location}-runtime-{worker_id}",
                location=location,
                available=available,
                healthy=available,
                load=load,
                capacity_available=1 if available else 0,
                lease_available=available,
                recent_failures=0 if available else 3,
                latency_p50_ms=latency / 2,
                latency_p95_ms=latency,
                cost_usd=cost,
                privacy_classes=("internal", "sensitive"),
                allowed_placements=(location,),
                attributes=FrozenDict(
                    {
                        "capabilities": list(capabilities),
                        "queue_depth": 0,
                        "recent_successes": 5,
                        "prompt_tokens": 100,
                        "completion_tokens": 30,
                        "network_state": network,
                        "fault": not available,
                        "requirement_change": False,
                        "compact": False,
                        "recovery": not available,
                    }
                ),
            )
        )
    return EnvironmentSnapshot(
        header=_header("environment-card-integration", "ResourceScheduler"),
        observed_at=NOW,
        observations=tuple(observations),
        required_categories=("worker",),
    )


def _policy_input(
    *,
    graph,
    environment: EnvironmentSnapshot,
    report_digest: str,
    readiness_status: str,
    allowed_placements: tuple[str, ...] = ("local", "cloud"),
) -> PolicyInputSnapshot:
    return PolicyInputSnapshot(
        header=_header("policy-input-card-integration", "PolicyInputSnapshotBuilder"),
        run_id=graph.run_id,
        task_id="task-card-integration",
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
        registered_roles=("executor", "verifier"),
        registered_capabilities=(
            "execution",
            "artifact_production",
            "verification",
            "review",
        ),
        unresolved_obligations=("execute artifact", "verify artifact"),
        registry_versions=FrozenDict(
            {
                "worker_registry_revision": 5,
                "provider_registry_revision": 3,
            }
        ),
        environment=environment,
        memory_refs=(),
        readiness_refs=(
            MechanismEvidenceReadinessReportRef(
                header=_header("card-readiness-ref-integration", "card"),
                report_ref="artifact://card-implementation-readiness",
                report_digest=report_digest,
                readiness_stage="implementation_validated",
                status=readiness_status,
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
        privacy_class="internal",
        last_topology_change_at="2026-07-30T09:00:00Z",
    )


def _arg_base(policy_input: PolicyInputSnapshot) -> TopologyProposalArtifact:
    steps = (
        {
            "step_index": 0,
            "token": "executor",
            "role_id": "executor",
            "node_id": "node-source",
            "binding_id": "binding-source",
            "worker_id": "worker-source",
            "capabilities": ["execution", "artifact_production"],
            "incident_edges": [],
            "score": {"total": 1.0},
            "state_digest": "state-source",
            "cold_start": False,
            "reasons": ["ARG source"],
        },
        {
            "step_index": 1,
            "token": "verifier",
            "role_id": "verifier",
            "node_id": "node-target",
            "binding_id": "binding-target",
            "worker_id": "worker-target",
            "capabilities": ["verification", "review"],
            "incident_edges": [
                {
                    "edge_id": "edge-source-target",
                    "source_node_id": "node-source",
                    "target_node_id": "node-target",
                    "relation": "arg_dependency",
                    "edge_type": "spatial",
                    "persisted": True,
                    "required_capabilities": ["verification"],
                    "reason": "ARG execution to verification dependency",
                }
            ],
            "score": {"total": 1.0},
            "state_digest": "state-target",
            "cold_start": False,
            "reasons": ["ARG target"],
        },
    )
    operations = (
        TopologyOperation(
            kind=TopologyOperationKind.ADD_NODE,
            entity_id="node-source",
            value=FrozenDict(
                {
                    "role": "executor",
                    "capabilities": ["execution", "artifact_production"],
                    "dependencies": [],
                }
            ),
            required_permissions=("graph.write",),
            requested_placement="local",
            resource_id="worker-source",
            required_capacity=1,
            reason="ARG source node",
        ),
        TopologyOperation(
            kind=TopologyOperationKind.ADD_NODE,
            entity_id="node-target",
            value=FrozenDict(
                {
                    "role": "verifier",
                    "capabilities": ["verification", "review"],
                    "dependencies": ["node-source"],
                }
            ),
            required_permissions=("graph.write",),
            requested_placement="cloud",
            resource_id="worker-target",
            required_capacity=1,
            reason="ARG target node",
        ),
        TopologyOperation(
            kind=TopologyOperationKind.ADD_EDGE,
            entity_id="edge-source-target",
            value=FrozenDict(
                {
                    "source_node_id": "node-source",
                    "target_node_id": "node-target",
                    "relation": "arg_dependency",
                    "edge_type": "spatial",
                    "required_capabilities": ["verification"],
                }
            ),
            required_permissions=("graph.write",),
            communication_bytes=96,
            reason="ARG execution to verification dependency",
        ),
    )
    return TopologyProposalArtifact(
        header=_header("arg-base-card-integration", "arg_designer"),
        proposal_id="arg-base-card-integration",
        input_snapshot_digest=policy_input.digest,
        base_graph=policy_input.graph,
        operations=operations,
        expected_outcome=FrozenDict(
            {
                "mechanism": "ARG deterministic joint role-node-edge",
                "requirement_revision": policy_input.requirement_revision,
                "joint_hypothesis": {
                    "hypothesis_id": "arg-hypothesis-card-integration",
                    "steps": list(steps),
                    "end_reason": "obligations_covered",
                },
            }
        ),
        alternatives=(),
        reasons=("ARG base topology",),
        constraint_assumptions=("GraphStateCustody owns commits",),
        expires_at="2026-07-30T10:05:00Z",
        fallback_profile="phase1_deterministic_baseline",
    )


def _report(
    tmp_path: Path,
    *,
    status: str,
    config: CARDResidualCorrectorConfig,
) -> tuple[Path, str]:
    report = json.loads(
        (
            ROOT
            / "docs"
            / "reviews"
            / "phase2"
            / "MechanismEvidenceReadinessReport.json"
        ).read_text(encoding="utf-8")
    )
    report["slice_id"] = "P2-S03-02"
    report["readiness_stage"] = "implementation_validated"
    report["supersedes_report_digest"] = INPUT_PRECHECK_DIGEST
    report["activation_allowed"] = False
    report["activation_reason"] = (
        "P2-S03-02 validates CARD only; default activation remains closed"
    )
    for mechanism_id, mechanism in report["mechanisms"].items():
        mechanism["readiness_stage"] = "implementation_validated"
        if mechanism_id not in {"arg_designer", "card"}:
            mechanism["status"] = "unavailable"
            report["mechanism_statuses"][mechanism_id] = "unavailable"
    report["mechanisms"]["arg_designer"]["status"] = "deterministic_ready"
    report["mechanism_statuses"]["arg_designer"] = "deterministic_ready"
    card = report["mechanisms"]["card"]
    card["status"] = status
    report["mechanism_statuses"]["card"] = status
    card["implementation_validation"] = {
        "passed": status == "deterministic_ready",
        "mechanism_version": config.mechanism_version,
        "configuration_digest": config.digest,
        "environment_schema_version": config.environment_schema_version,
        "encoded_schema_version": config.encoded_schema_version,
        "residual_schema_version": config.residual_schema_version,
        "canonical_mutation_attempted": False,
        "training_sample_count": 0,
    }
    report.pop("report_digest", None)
    report["report_digest"] = canonical_digest(report)
    path = tmp_path / f"card-readiness-{status}.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path, report["report_digest"]


def test_validation_runtime_publishes_real_environment_residual_without_graph_commit(
    tmp_path: Path,
) -> None:
    custody = _custody(tmp_path)
    graph = custody.current("graph-card-integration")
    config = CARDResidualCorrectorConfig.load(
        ROOT / "config" / "phase2" / "card-directional-residual.json"
    )
    report_path, report_digest = _report(
        tmp_path,
        status="deterministic_ready",
        config=config,
    )
    policy_input = _policy_input(
        graph=graph,
        environment=_environment(),
        report_digest=report_digest,
        readiness_status="deterministic_ready",
    )
    arg_base = _arg_base(policy_input)
    events = []
    publisher = PolicyEvidencePublisher(
        LocalArtifactStore(tmp_path / "artifacts"),
        admit_event=events.append,
    )
    runtime = CARDTopologyRuntime.from_repository(
        ROOT,
        report_path=report_path,
        evidence_publisher=publisher,
        admit_event=events.append,
    )

    first = runtime.execute(
        policy_input=policy_input,
        arg_base=arg_base,
        current_graph=graph,
    )
    second = runtime.execute(
        policy_input=policy_input,
        arg_base=arg_base,
        current_graph=graph,
        publish=False,
    )

    assert first.mode == "validation"
    assert first.correction is not None
    assert first.correction_proposal is not None
    assert first.published_evidence is not None
    assert (
        publisher.replay(first.published_evidence.artifact).digest
        == first.correction_proposal.digest
    )
    assert first.correction.digest == second.correction.digest
    assert first.correction.missing_optional_categories == (
        "model",
        "provider",
        "tool",
    )
    assert first.composer_residual_eligible is True
    assert first.canonical_graph_unchanged is True
    assert custody.current(graph.graph_id).revision == 0
    assert any(
        item.payload.get("canonical_mutation_attempted") is False
        for item in events
    )


def test_failure_and_privacy_change_directional_residual_while_card_disable_does_not(
    tmp_path: Path,
) -> None:
    custody = _custody(tmp_path)
    graph = custody.current("graph-card-integration")
    config = CARDResidualCorrectorConfig.load(
        ROOT / "config" / "phase2" / "card-directional-residual.json"
    )
    report_path, report_digest = _report(
        tmp_path,
        status="deterministic_ready",
        config=config,
    )
    runtime = CARDTopologyRuntime.from_repository(
        ROOT,
        report_path=report_path,
    )

    healthy_input = _policy_input(
        graph=graph,
        environment=_environment(),
        report_digest=report_digest,
        readiness_status="deterministic_ready",
    )
    healthy_arg = _arg_base(healthy_input)
    healthy = runtime.execute(
        policy_input=healthy_input,
        arg_base=healthy_arg,
        current_graph=graph,
        publish=False,
    )

    failed_input = _policy_input(
        graph=graph,
        environment=_environment(
            target_available=False,
            target_network="disconnected",
        ),
        report_digest=report_digest,
        readiness_status="deterministic_ready",
    )
    failed_arg = _arg_base(failed_input)
    failed = runtime.execute(
        policy_input=failed_input,
        arg_base=failed_arg,
        current_graph=graph,
        publish=False,
    )
    disabled = runtime.execute(
        policy_input=failed_input,
        arg_base=failed_arg,
        current_graph=graph,
        enabled=False,
        publish=False,
    )

    assert healthy.correction is not None
    assert failed.correction is not None
    assert healthy.correction.digest != failed.correction.digest
    failed_edge = failed.correction.decisions[0]
    assert failed_edge.action == "drop"
    assert "target_unavailable" in failed_edge.rejection_reasons
    assert disabled.mode == "baseline"
    assert disabled.correction is None
    assert disabled.effective_spatial_edge_ids == ("edge-source-target",)
    assert disabled.degraded_reason == "card_disabled"

    private_input = _policy_input(
        graph=graph,
        environment=_environment(),
        report_digest=report_digest,
        readiness_status="deterministic_ready",
        allowed_placements=("local",),
    )
    private_arg = _arg_base(private_input)
    private = runtime.execute(
        policy_input=private_input,
        arg_base=private_arg,
        current_graph=graph,
        publish=False,
    )
    assert private.correction.decisions[0].action == "drop"
    assert (
        "privacy_or_placement_forbidden"
        in private.correction.decisions[0].rejection_reasons
    )
    assert custody.current(graph.graph_id).revision == 0


def test_evidence_only_observes_diff_but_keeps_arg_commit_input_and_unavailable_baselines(
    tmp_path: Path,
) -> None:
    custody = _custody(tmp_path)
    graph = custody.current("graph-card-integration")
    config = CARDResidualCorrectorConfig.load(
        ROOT / "config" / "phase2" / "card-directional-residual.json"
    )

    evidence_path, evidence_digest = _report(
        tmp_path,
        status="evidence_only",
        config=config,
    )
    evidence_input = _policy_input(
        graph=graph,
        environment=_environment(target_available=False),
        report_digest=evidence_digest,
        readiness_status="evidence_only",
    )
    evidence_arg = _arg_base(evidence_input)
    evidence = CARDTopologyRuntime.from_repository(
        ROOT,
        report_path=evidence_path,
    ).execute(
        policy_input=evidence_input,
        arg_base=evidence_arg,
        current_graph=graph,
        publish=False,
    )
    assert evidence.mode == "diagnostic"
    assert evidence.correction is not None
    assert evidence.correction.decisions[0].action == "drop"
    assert evidence.composer_residual_eligible is False
    assert evidence.commit_input_proposal_digest == evidence_arg.digest
    assert evidence.effective_spatial_edge_ids == ("edge-source-target",)

    unavailable_path, unavailable_digest = _report(
        tmp_path,
        status="unavailable",
        config=config,
    )
    unavailable_input = _policy_input(
        graph=graph,
        environment=_environment(target_available=False),
        report_digest=unavailable_digest,
        readiness_status="unavailable",
    )
    unavailable_arg = _arg_base(unavailable_input)
    unavailable = CARDTopologyRuntime.from_repository(
        ROOT,
        report_path=unavailable_path,
    ).execute(
        policy_input=unavailable_input,
        arg_base=unavailable_arg,
        current_graph=graph,
        publish=False,
    )
    assert unavailable.mode == "baseline"
    assert unavailable.correction is None
    assert unavailable.commit_input_proposal_digest == unavailable_arg.digest
    assert unavailable.effective_spatial_edge_ids == ("edge-source-target",)
    assert custody.current(graph.graph_id).revision == 0
