from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from zyra_core import create_task_state
from zyra_orchestration.graph_custody import GraphStateSnapshot
from zyra_orchestration.topology_policy import (
    ContractHeader,
    ConstraintResult,
    EnvironmentSnapshot,
    EnvironmentSnapshotBuilder,
    FrozenDict,
    GraphSnapshotRef,
    MechanismEvidenceReadinessReportRef,
    MemoryContinuityReceipt,
    NeuroSymbolicEvidenceBundle,
    PhysicalDispatchReceipt,
    PolicyBudget,
    PolicyContractError,
    PolicyDecisionDisposition,
    PolicyDecisionReceipt,
    PolicyDigestMismatch,
    PolicyInputSnapshot,
    PolicyInputSnapshotBuilder,
    PolicyNodeSnapshot,
    PolicyOutcome,
    StableArtifactRef,
    TelemetryObservation,
    TopologyOperation,
    TopologyOperationKind,
    TopologyProposalArtifact,
    UnsupportedPolicySchema,
    parse_policy_contract,
    policy_contract_json_schemas,
    policy_contract_schema_catalog,
)


NOW = "2026-07-29T12:00:00Z"
DIGEST = "a" * 64


def _header(contract_id: str, *, mechanism: str = "ARG") -> ContractHeader:
    return ContractHeader(
        contract_id=contract_id,
        created_at=NOW,
        source_event_id="event-source",
        correlation_id="correlation-policy",
        causation_id="causation-policy",
        mechanism_id=mechanism,
        mechanism_version="deterministic-adapter-v1",
        input_version="owner-snapshot-v1",
        idempotency_key=f"idempotency:{contract_id}",
        configuration_digest="b" * 64,
    )


def _artifact(ref_id: str = "artifact-a") -> StableArtifactRef:
    return StableArtifactRef(
        ref_id=ref_id,
        uri=f"artifact://{ref_id}",
        digest=DIGEST,
    )


def _readiness() -> MechanismEvidenceReadinessReportRef:
    return MechanismEvidenceReadinessReportRef(
        header=_header("readiness"),
        report_ref="artifact://readiness",
        report_digest=DIGEST,
        readiness_stage="activation_ready",
        status="deterministic_ready",
    )


def _environment() -> EnvironmentSnapshot:
    return EnvironmentSnapshot(
        header=_header("environment", mechanism="ResourceScheduler"),
        observed_at=NOW,
        observations=(
            TelemetryObservation(
                observation_id="observation-worker-a",
                resource_id="worker-a",
                category="worker",
                observed_at=NOW,
                fresh_until="2026-07-29T12:05:00Z",
                confidence=1.0,
                observation_source="ResourceScheduler.worker_pool_api_projection",
                source_event_id="event-source",
                physical_runtime_id="process-4242",
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


def _policy_input() -> PolicyInputSnapshot:
    return PolicyInputSnapshot(
        header=_header("input", mechanism="PolicyInputSnapshotBuilder"),
        run_id="run-policy",
        task_id="task-policy",
        phase="phase2",
        requirement_revision="requirement-r1",
        graph=GraphSnapshotRef(
            graph_id="graph-policy",
            run_id="run-policy",
            revision=0,
            signature="c" * 64,
            commit_id="",
        ),
        nodes=(
            PolicyNodeSnapshot(
                node_id="node-existing",
                role="worker",
                capabilities=("execute",),
            ),
        ),
        registered_roles=("worker",),
        registered_capabilities=("execute",),
        unresolved_obligations=("produce-artifact",),
        registry_versions=FrozenDict({"workers": "r1", "skills": "r2"}),
        environment=_environment(),
        memory_refs=(_artifact("memory-a"),),
        readiness_refs=(_readiness(),),
        budget=PolicyBudget(
            remaining_tokens=1000,
            remaining_cost_usd=1,
            remaining_time_ms=10000,
            max_communication_bytes=4096,
            max_fan_out=4,
            max_topology_churn=3,
            minimum_dwell_seconds=0,
        ),
        allowed_permissions=("graph.write",),
        allowed_placements=("edge",),
        privacy_class="internal",
        last_topology_change_at="2026-07-29T11:00:00Z",
    )


def _proposal(policy_input: PolicyInputSnapshot) -> TopologyProposalArtifact:
    return TopologyProposalArtifact(
        header=_header("proposal"),
        proposal_id="proposal-a",
        input_snapshot_digest=policy_input.digest,
        base_graph=policy_input.graph,
        operations=(
            TopologyOperation(
                kind=TopologyOperationKind.SET_GRAPH_METADATA,
                entity_id="policy-marker",
                value=FrozenDict({"value": "enabled"}),
                required_permissions=("graph.write",),
                requested_placement="edge",
                resource_id="worker-a",
                required_capacity=1,
                communication_bytes=64,
                reason="record topology policy decision",
            ),
        ),
        expected_outcome=FrozenDict({"tokens": 10, "cost_usd": 0.01, "time_ms": 20}),
        alternatives=(_artifact("alternative-a"),),
        reasons=("deterministic proposal",),
        constraint_assumptions=("worker telemetry remains fresh",),
        expires_at="2026-07-29T12:05:00Z",
        fallback_profile="phase1_deterministic_baseline",
    )


def test_all_nine_contracts_round_trip_with_stable_canonical_digest() -> None:
    policy_input = _policy_input()
    proposal = _proposal(policy_input)
    check = ConstraintResult(
        constraint_id="permission",
        passed=True,
        reason_code="allowed",
        message="permission accepted",
        evidence_refs=("permission-a",),
    )
    decision = PolicyDecisionReceipt(
        header=_header("decision"),
        decision_id="decision-a",
        proposal_id=proposal.proposal_id,
        proposal_digest=proposal.digest,
        disposition=PolicyDecisionDisposition.ACCEPT,
        constraint_results=(check,),
    )
    outcome = PolicyOutcome(
        header=_header("outcome"),
        proposal_ref=proposal.proposal_id,
        decision_ref=decision.decision_id,
        commit_ref="graph-commit-a",
        verifier_result="passed",
        artifact_refs=(_artifact("outcome-a"),),
        metrics=FrozenDict({"latency_ms": 12}),
        permission_result="allowed",
        recovery_result="not_required",
        causal_refs=("graph-commit-a",),
    )
    memory = MemoryContinuityReceipt(
        header=_header("memory", mechanism="MemoryContinuityVerifier"),
        before_digest="d" * 64,
        after_digest="e" * 64,
        requirement_revision="requirement-r1",
        critical_fact_results=FrozenDict({"fact-a": "preserved"}),
        obligation_results=FrozenDict({"produce-artifact": "preserved"}),
        provenance_refs=(_artifact("memory-provenance"),),
        rejected_memory_refs=(),
        downstream_decision_ref=decision.decision_id,
        continuity_result="passed",
    )
    neuro_symbolic = NeuroSymbolicEvidenceBundle(
        header=_header("neuro-symbolic"),
        proposal_signal_mode="deterministic_only",
        proposal_ref=_artifact("proposal-artifact"),
        model_observation_refs=(),
        constraint_results=(check,),
        projected_delta_ref="delta-a",
        commit_or_no_commit=FrozenDict({"commit_id": "graph-commit-a"}),
        permission_ref="permission-a",
        lease_ref="lease-a",
        verification_ref="verification-a",
    )
    dispatch = PhysicalDispatchReceipt(
        header=_header("dispatch", mechanism="ResourceScheduler"),
        placement_decision_id="placement-a",
        alternatives=("local", "cloud"),
        input_signals=FrozenDict({"latency_p95_ms": 20}),
        worker_manifest_ref=_artifact("manifest-a"),
        lease_id="lease-a",
        physical_attempt_id="attempt-a",
        physical_identity=FrozenDict({"pid": 4242, "location": "edge"}),
        call_receipt=_artifact("call-a"),
        artifact_ref=_artifact("dispatch-artifact-a"),
        verifier_ref=_artifact("dispatch-verifier-a"),
        privacy_class="internal",
        allowed_placements=("edge",),
        permission_ref="permission-a",
        simulated=False,
    )
    contracts = (
        _readiness(),
        policy_input,
        _environment(),
        proposal,
        decision,
        outcome,
        memory,
        neuro_symbolic,
        dispatch,
    )
    assert len(contracts) == 9
    for contract in contracts:
        replayed = parse_policy_contract(contract.to_dict())
        assert replayed.to_dict() == contract.to_dict()
        assert replayed.digest == contract.digest
        assert replayed.canonical_serialization == contract.canonical_serialization


def test_contracts_are_deeply_immutable_and_order_independent() -> None:
    first = FrozenDict({"z": [3, 2], "a": {"b": True}})
    second = FrozenDict({"a": {"b": True}, "z": [3, 2]})
    assert first == second
    policy_input = _policy_input()
    with pytest.raises(FrozenInstanceError):
        policy_input.phase = "mutated"  # type: ignore[misc]
    with pytest.raises(TypeError):
        policy_input.registry_versions["workers"] = "mutated"  # type: ignore[index]
    with pytest.raises(TypeError):
        policy_input.registry_versions._items = ()  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        policy_input.registry_versions._lookup["workers"] = "mutated"  # type: ignore[index]


def test_digest_tampering_missing_headers_and_future_schema_fail_closed() -> None:
    proposal = _proposal(_policy_input())
    tampered = proposal.to_dict()
    tampered["payload"]["proposal_id"] = "tampered"
    with pytest.raises(PolicyDigestMismatch):
        parse_policy_contract(tampered)

    missing_header = proposal.to_dict()
    missing_header.pop("source_event_id")
    missing_header.pop("digest")
    with pytest.raises(PolicyContractError, match="missing current-schema header"):
        parse_policy_contract(missing_header)

    future = proposal.to_dict()
    future["schema_version"] = "zyra.topology-proposal-artifact/v2"
    with pytest.raises(UnsupportedPolicySchema):
        parse_policy_contract(future)

    wrong_kind = proposal.to_dict()
    wrong_kind["contract_kind"] = "policy_outcome"
    wrong_kind.pop("digest")
    with pytest.raises(PolicyContractError, match="cannot carry contract_kind"):
        parse_policy_contract(wrong_kind)


def test_declared_legacy_schema_is_read_and_normalized_to_v1() -> None:
    proposal = _proposal(_policy_input())
    legacy = proposal.payload_dict()
    legacy["schema"] = "zyra.topology-proposal-artifact/v0"
    replayed = parse_policy_contract(legacy)
    assert replayed.SCHEMA_VERSION == "zyra.topology-proposal-artifact/v1"
    assert replayed.proposal_id == proposal.proposal_id
    assert replayed.header.mechanism_id == "legacy"


def test_catalog_declares_versions_digest_and_compatibility_for_nine_contracts() -> None:
    catalog = policy_contract_schema_catalog()
    assert catalog["schema"] == "zyra.policy-contract-catalog/v1"
    assert len(catalog["contracts"]) == 9
    schemas = policy_contract_json_schemas()
    assert len(schemas) == 9
    for item in catalog["contracts"]:
        schema = item["json_schema"]
        assert schema["$schema"].endswith("2020-12/schema")
        assert schema["properties"]["schema_version"]["const"] == item["schema_version"]
        assert schema["properties"]["payload"]["required"]
        assert "idempotency_key" in schema["required"]
    assert {item["contract_kind"] for item in catalog["contracts"]} == {
        "mechanism_evidence_readiness_report_ref",
        "policy_input_snapshot",
        "environment_snapshot",
        "topology_proposal_artifact",
        "policy_decision_receipt",
        "policy_outcome",
        "memory_continuity_receipt",
        "neuro_symbolic_evidence_bundle",
        "physical_dispatch_receipt",
    }


def test_environment_builder_copies_real_worker_pool_projection_shape() -> None:
    projection = {
        "captured_at": NOW,
        "revision": 7,
        "workers": [
            {
                "worker_id": "worker-real",
                "state": "idle",
                "location": "edge",
                "backend_id": "sandbox-gateway",
                "process_identity": "pid-4242",
                "health": {"status": "healthy", "signal_id": "health-a"},
                "manifest": {
                    "capabilities": ["execute", "verify"],
                    "constraints": {"privacy_classes": ["internal"]},
                    "location": "edge",
                },
                "active_leases": [],
                "telemetry": {
                    "observed_at": NOW,
                    "capacity": {"cpu_cores": 2, "process_slots": 2},
                    "allocated": {"cpu_cores": 1, "process_slots": 1},
                    "load_average": 0.25,
                },
            }
        ],
    }
    environment = EnvironmentSnapshotBuilder.from_worker_pool_projection(
        projection,
        header=_header("environment-builder", mechanism="ResourceScheduler"),
    )
    observation = environment.observations[0]
    assert observation.physical_runtime_id == "pid-4242"
    assert observation.capacity_available == 1
    assert observation.lease_available is True
    assert observation.missing_fields == ()
    assert observation.attributes["backend_id"] == "sandbox-gateway"
    projection["workers"][0]["process_identity"] = "mutated"
    assert observation.physical_runtime_id == "pid-4242"


def test_policy_input_builder_requires_continuity_before_reading_owners() -> None:
    task = create_task_state("preserve a long-horizon obligation")
    task.plan_nodes[task.root_node_id].completion_criteria.append("deliver-proof")
    graph = GraphStateSnapshot.empty("graph-builder", task.run_id)
    environment = _environment()
    with pytest.raises(PolicyContractError, match="continuity gate"):
        PolicyInputSnapshotBuilder.build(
            task=task,
            graph=graph,
            environment=environment,
            header=_header(
                "input-builder",
                mechanism="PolicyInputSnapshotBuilder",
            ),
            budget=PolicyBudget(
                remaining_tokens=100,
                remaining_cost_usd=1,
                remaining_time_ms=1000,
                max_communication_bytes=100,
                max_fan_out=2,
                max_topology_churn=2,
                minimum_dwell_seconds=0,
            ),
            readiness_refs=(_readiness(),),
            registry_versions={"role_registry": "r1"},
            registered_roles=("worker",),
            registered_capabilities=("execute",),
        )
