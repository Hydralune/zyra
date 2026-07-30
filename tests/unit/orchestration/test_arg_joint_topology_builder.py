from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from zyra_orchestration.graph_custody import (
    GraphDeltaBuilder,
    GraphEdge,
    GraphNode,
    GraphStateSnapshot,
)
from zyra_orchestration.topology_policy import (
    ContractHeader,
    EnvironmentSnapshot,
    FrozenDict,
    GraphSnapshotRef,
    MechanismEvidenceReadinessReportRef,
    PolicyBudget,
    PolicyInputSnapshot,
    PolicyNodeSnapshot,
    StableArtifactRef,
    TelemetryObservation,
    TopologyOperationKind,
)
from zyra_orchestration.topology_policy.arg import (
    ARGCatalogError,
    ARGInputEncoder,
    ARGInputError,
    ARGJointBuilder,
    ARGJointBuilderConfig,
    ARGRoleCatalogBuilder,
    ARGTopologyRuntime,
)
from zyra_scheduler.worker_pool import (
    ResourceVector,
    WorkerCapabilityManifest,
    WorkerLocation,
)


ROOT = Path(__file__).resolve().parents[3]
NOW = "2026-07-30T08:00:00Z"
READY_DIGEST = "a" * 64


def _header(contract_id: str, mechanism_id: str) -> ContractHeader:
    return ContractHeader(
        contract_id=contract_id,
        created_at=NOW,
        source_event_id="event-arg-input",
        correlation_id="correlation-arg",
        causation_id="continuity-gate-arg",
        mechanism_id=mechanism_id,
        mechanism_version="v1",
        input_version="v1",
        idempotency_key=f"idempotency:{contract_id}",
    )


def _worker(
    worker_id: str,
    role_id: str,
    capabilities: tuple[str, ...],
    *,
    worker_kind: str | None = None,
    tool_ids: tuple[str, ...] = (),
    model_ids: tuple[str, ...] = (),
) -> WorkerCapabilityManifest:
    return WorkerCapabilityManifest(
        worker_id=worker_id,
        worker_kind=worker_kind or f"{role_id}_runtime",
        location=WorkerLocation.LOCAL,
        backend_ids=(f"backend-{worker_id}",),
        backend_kinds=("local_process",),
        capabilities=capabilities,
        tool_ids=tool_ids,
        resource_capacity=ResourceVector(process_slots=2),
        constraints={
            "required_permissions": ["graph.write"],
            "allowed_placements": ["local"],
            "model_ids": list(model_ids),
        },
        labels={
            "arg_role": role_id,
            "arg_role_display_name": role_id.replace("_", " ").title(),
        },
    )


def _workers() -> tuple[WorkerCapabilityManifest, ...]:
    return (
        _worker(
            "worker-planner",
            "planner",
            ("planning", "research", "task_analysis", "decomposition"),
            worker_kind="planner_runtime",
            tool_ids=("repo.read",),
            model_ids=("reasoner-v1",),
        ),
        _worker(
            "worker-executor",
            "executor",
            ("execution", "coding", "tool_use", "artifact_production"),
        ),
        _worker(
            "worker-verifier",
            "verifier",
            ("verification", "testing", "review", "quality"),
        ),
        _worker(
            "worker-recovery",
            "recovery",
            ("recovery", "fault_diagnosis", "checkpoint_restore", "repair"),
        ),
    )


def _environment(
    workers: tuple[WorkerCapabilityManifest, ...],
) -> EnvironmentSnapshot:
    return EnvironmentSnapshot(
        header=_header("environment-arg", "ResourceScheduler"),
        observed_at=NOW,
        observations=tuple(
            TelemetryObservation(
                observation_id=f"observation-{worker.worker_id}",
                resource_id=worker.worker_id,
                category="worker",
                observed_at=NOW,
                fresh_until="2026-07-30T08:10:00Z",
                confidence=1.0,
                observation_source="ResourceScheduler.worker_pool_api_projection",
                source_event_id="event-arg-input",
                physical_runtime_id=f"process-{worker.worker_id}",
                location="local",
                available=True,
                healthy=True,
                capacity_available=2,
                lease_available=True,
                privacy_classes=("internal",),
                allowed_placements=("local",),
                attributes=FrozenDict({"manifest_digest": worker.digest}),
            )
            for worker in workers
        ),
        required_categories=("worker",),
    )


def _skill_registry() -> dict:
    return {
        "active_by_qualified_name": {
            "planning/repository-analysis": "skill-ref-planning"
        },
        "revisions": {
            "skill-ref-planning": {
                "metadata": {
                    "name": "repository-analysis",
                    "description": "Analyze repository requirements and dependencies",
                    "when_to_use": "planning and research",
                    "preferred_runtime": "planner_runtime",
                    "allowed_tools": [
                        {"name": "repo.read", "namespace": "builtin"}
                    ],
                }
            }
        },
    }


def _catalog(
    workers: tuple[WorkerCapabilityManifest, ...],
    environment: EnvironmentSnapshot,
):
    return ARGRoleCatalogBuilder().build(
        worker_manifests=workers,
        environment=environment,
        skill_registry=_skill_registry(),
        tool_registry=(
            {
                "tool_id": "repo.read",
                "enabled": True,
                "capabilities": ["repository_inspection"],
                "description": "Read repository source and dependencies",
            },
        ),
        model_registry=(
            {
                "providerId": "provider-a",
                "modelId": "reasoner-v1",
                "displayName": "Reasoner",
                "family": "reasoning",
                "status": "active",
                "enabled": True,
                "capabilities": {
                    "input": ["text"],
                    "output": ["text"],
                    "reasoning": True,
                    "tools": True,
                },
            },
        ),
        source_versions={
            "worker_registry_revision": 4,
            "skill_registry_generation": 2,
            "tool_registry_revision": 7,
            "model_registry_revision": 3,
        },
    )


def _graph(
    *,
    old_arg_node: bool = False,
    broad_predecessors: bool = False,
) -> GraphStateSnapshot:
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    if broad_predecessors:
        nodes.extend(
            [
                GraphNode(
                    node_id="node-root",
                    role="existing_root",
                    capabilities=("artifact_production", "execution"),
                ),
                GraphNode(
                    node_id="node-noise-1",
                    role="existing_noise_1",
                    capabilities=("logging",),
                ),
                GraphNode(
                    node_id="node-noise-2",
                    role="existing_noise_2",
                    capabilities=("metrics",),
                ),
                GraphNode(
                    node_id="node-noise-3",
                    role="existing_noise_3",
                    capabilities=("formatting",),
                ),
                GraphNode(
                    node_id="node-noise-4",
                    role="existing_noise_4",
                    capabilities=("notification",),
                ),
            ]
        )
    if old_arg_node:
        nodes.append(
            GraphNode(
                node_id="arg-node-old",
                role="obsolete_role",
                capabilities=("obsolete",),
                labels={"arg_owner": "arg_designer"},
                metadata={"arg_owner": "arg_designer"},
            )
        )
    return GraphStateSnapshot(
        graph_id="graph-arg",
        run_id="run-arg",
        revision=0,
        nodes=tuple(nodes),
        edges=tuple(edges),
        metadata={
            "graph_state_owner": "GraphStateCustody",
            "topology_owner": "DynamicTopologyRuntime",
        },
        created_at=NOW,
    )


def _input(
    *,
    graph: GraphStateSnapshot,
    environment: EnvironmentSnapshot,
    catalog,
    phase: str,
    requirement_revision: str,
    obligations: tuple[str, ...],
    permissions: tuple[str, ...] = ("graph.write",),
    readiness_status: str = "deterministic_ready",
) -> PolicyInputSnapshot:
    graph_roles = tuple(node.role for node in graph.nodes)
    graph_capabilities = tuple(
        capability for node in graph.nodes for capability in node.capabilities
    )
    return PolicyInputSnapshot(
        header=_header(
            f"policy-input-{phase}-{requirement_revision}",
            "PolicyInputSnapshotBuilder",
        ),
        run_id=graph.run_id,
        task_id="task-arg",
        phase=phase,
        requirement_revision=requirement_revision,
        graph=GraphSnapshotRef(
            graph_id=graph.graph_id,
            run_id=graph.run_id,
            revision=graph.revision,
            signature=graph.signature,
            commit_id=graph.commit_id,
        ),
        nodes=tuple(
            PolicyNodeSnapshot(
                node_id=node.node_id,
                role=node.role,
                capabilities=node.capabilities,
                dependencies=node.dependencies,
                state=node.state.value,
                revision=node.revision,
                labels=FrozenDict(node.labels),
                metadata=FrozenDict(node.metadata),
            )
            for node in graph.nodes
        ),
        registered_roles=tuple(
            sorted({*graph_roles, *(item.role_id for item in catalog.profiles)})
        ),
        registered_capabilities=tuple(
            sorted(
                {
                    *graph_capabilities,
                    *(
                        capability
                        for item in catalog.profiles
                        for capability in item.capabilities
                    ),
                }
            )
        ),
        unresolved_obligations=obligations,
        registry_versions=FrozenDict(
            {
                "arg_role_catalog": catalog.catalog_version,
                "arg_role_catalog_digest": catalog.digest,
                **dict(catalog.source_versions),
            }
        ),
        environment=environment,
        memory_refs=(
            StableArtifactRef(
                ref_id="memory-continuity-fact",
                uri="artifact://memory-continuity-fact",
                digest="b" * 64,
            ),
        ),
        readiness_refs=(
            MechanismEvidenceReadinessReportRef(
                header=_header("arg-readiness-ref", "arg_designer"),
                report_ref="artifact://arg-readiness",
                report_digest=READY_DIGEST,
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
        allowed_permissions=permissions,
        allowed_placements=("local",),
        privacy_class="internal",
        last_topology_change_at="2026-07-30T07:00:00Z",
    )


def _build(
    *,
    phase: str,
    obligations: tuple[str, ...],
    requirement_revision: str = "requirement-r1",
    graph: GraphStateSnapshot | None = None,
    branch_delta=None,
):
    config = ARGJointBuilderConfig.load(
        ROOT / "config" / "phase2" / "arg-joint-topology.json"
    )
    workers = _workers()
    environment = _environment(workers)
    catalog = _catalog(workers, environment)
    selected_graph = graph or _graph()
    policy_input = _input(
        graph=selected_graph,
        environment=environment,
        catalog=catalog,
        phase=phase,
        requirement_revision=requirement_revision,
        obligations=obligations,
    )
    encoded = ARGInputEncoder().encode(
        policy_input=policy_input,
        task_summary="Deliver a verified software artifact and recover from faults",
        current_graph=selected_graph,
        role_catalog=catalog,
        readiness_stage="implementation_validated",
        readiness_status="deterministic_ready",
        readiness_report_digest=READY_DIGEST,
        mechanism_version=config.mechanism_version,
        configuration_digest=config.digest,
        phase_affinity=dict(config.phase_capability_affinity),
        branch_delta=branch_delta,
    )
    return ARGJointBuilder(config).build(encoded), policy_input, catalog, config


def test_catalog_consumes_real_registries_and_cold_start_requires_no_enum_change() -> None:
    base_workers = _workers()[:3]
    base_environment = _environment(base_workers)
    base = _catalog(base_workers, base_environment)
    planner = base.profile("planner")

    assert "repository_inspection" in planner.capabilities
    assert "model_reasoning" in planner.capabilities
    assert planner.bindings[0].skill_ids == ("planning/repository-analysis",)
    assert planner.bindings[0].tool_ids == ("repo.read",)
    assert planner.manifest_digests == (_workers()[0].digest,)

    cold_worker = _workers()[3]
    expanded_workers = (*base_workers, cold_worker)
    expanded = _catalog(expanded_workers, _environment(expanded_workers))
    assert base.catalog_version != expanded.catalog_version
    assert expanded.profile("recovery").bindings[0].worker_id == "worker-recovery"

    with pytest.raises(ARGCatalogError, match="unknown"):
        expanded.profile("invented-role-without-capability")
    with pytest.raises(ARGCatalogError, match="at least one"):
        ARGRoleCatalogBuilder().build(
            worker_manifests=({"worker_id": "bad", "worker_kind": "bad"},),
            environment=_environment(()),
        )


def test_phase_changes_joint_role_node_edge_hypothesis_with_explicit_end() -> None:
    results = {}
    for phase in ("planning", "execution", "verification", "recovery"):
        built, _, catalog, _ = _build(
            phase=phase,
            obligations=(
                f"{phase} the deliverable",
                "produce and verify artifact",
            ),
        )
        role_ids = tuple(item.role_id for item in built.hypothesis.role_steps)
        results[phase] = role_ids
        assert built.hypothesis.steps[-1].token == "ARG_END_V1"
        assert built.hypothesis.steps[-1].incident_edges[0].relation == "arg_end"
        assert all(item.incident_edges for item in built.hypothesis.role_steps)
        assert all(item.binding_id for item in built.hypothesis.role_steps)
        assert all(catalog.profile(item.role_id) for item in built.hypothesis.role_steps)
        assert any(
            item.kind is TopologyOperationKind.ADD_NODE
            for item in built.proposal.operations
        )
        assert any(
            item.kind is TopologyOperationKind.ADD_EDGE
            for item in built.proposal.operations
        )
        assert built.proposal.expected_outcome["end_reason"]
        assert built.proposal.reasons[-1].startswith("END=")
        assert len(built.proposal.alternatives) > 0

    assert results["planning"] != results["execution"]
    assert results["execution"] != results["verification"]
    assert results["verification"] != results["recovery"]
    assert results["planning"][0] == "planner"
    assert results["execution"][0] == "executor"
    assert results["verification"][0] == "verifier"
    assert results["recovery"][0] == "recovery"


def test_fixed_snapshot_version_config_is_deterministic_and_dependency_search_is_global() -> None:
    graph = _graph(broad_predecessors=True)
    first, policy_input, catalog, config = _build(
        phase="execution",
        obligations=("execute and produce artifact", "verify artifact"),
        graph=graph,
    )
    second, _, _, _ = _build(
        phase="execution",
        obligations=("execute and produce artifact", "verify artifact"),
        graph=graph,
    )

    assert first.proposal.digest == second.proposal.digest
    assert first.hypothesis.to_dict() == second.hypothesis.to_dict()
    assert first.proposal.header.configuration_digest == config.digest
    assert first.proposal.input_snapshot_digest == policy_input.digest
    assert first.proposal.expected_outcome["role_catalog_digest"] == catalog.digest
    assert any(
        edge.source_node_id == "node-root"
        for step in first.hypothesis.role_steps
        for edge in step.incident_edges
    )


def test_branch_local_graph_delta_participates_without_canonical_mutation() -> None:
    graph = _graph(broad_predecessors=True)
    branch = (
        GraphDeltaBuilder(
            graph,
            branch_id="branch-arg",
            actor_id="arg-test",
            causation_id="continuity-gate-arg",
            correlation_id="correlation-arg",
            idempotency_key="arg-branch-overlay",
        )
        .add_node(
            GraphNode(
                node_id="node-branch-critical",
                role="branch_context",
                capabilities=("artifact_production", "execution"),
            )
        )
        .build()
    )
    built, _, _, _ = _build(
        phase="execution",
        obligations=("execute and produce artifact", "verify artifact"),
        graph=graph,
        branch_delta=branch,
    )

    assert built.proposal.expected_outcome["branch_delta_digest"] == branch.content_digest
    assert any(
        edge.source_node_id == "node-branch-critical"
        and "branch-local" in edge.reason
        for step in built.hypothesis.role_steps
        for edge in step.incident_edges
    )
    assert "node-branch-critical" not in graph.node_map
    assert graph.revision == 0


def test_requirement_revision_terminates_obsolete_arg_state_and_rejects_old_proposal() -> None:
    graph = _graph(old_arg_node=True)
    old, old_input, old_catalog, _ = _build(
        phase="planning",
        obligations=("plan repository work",),
        requirement_revision="requirement-r1",
        graph=graph,
    )
    new, new_input, new_catalog, _ = _build(
        phase="recovery",
        obligations=("repair fault and restore checkpoint", "verify recovery"),
        requirement_revision="requirement-r2",
        graph=graph,
    )

    assert old.proposal.digest != new.proposal.digest
    assert any(
        item.kind is TopologyOperationKind.REMOVE_NODE
        and item.entity_id == "arg-node-old"
        for item in new.proposal.operations
    )
    assert old.hypothesis.role_steps[0].role_id == "planner"
    assert new.hypothesis.role_steps[0].role_id == "recovery"
    with pytest.raises(ValueError, match="old input"):
        ARGTopologyRuntime.validate_proposal(
            policy_input=new_input,
            role_catalog=new_catalog,
            proposal=old.proposal,
        )
    ARGTopologyRuntime.validate_proposal(
        policy_input=old_input,
        role_catalog=old_catalog,
        proposal=old.proposal,
    )


def test_permission_or_catalog_drift_fails_closed_before_expansion() -> None:
    config = ARGJointBuilderConfig.load(
        ROOT / "config" / "phase2" / "arg-joint-topology.json"
    )
    workers = _workers()
    environment = _environment(workers)
    catalog = _catalog(workers, environment)
    graph = _graph()
    denied = _input(
        graph=graph,
        environment=environment,
        catalog=catalog,
        phase="execution",
        requirement_revision="requirement-r1",
        obligations=("execute artifact",),
        permissions=("tool.read",),
    )
    with pytest.raises(ARGInputError, match="eligible"):
        ARGInputEncoder().encode(
            policy_input=denied,
            task_summary="execute artifact",
            current_graph=graph,
            role_catalog=catalog,
            readiness_stage="implementation_validated",
            readiness_status="deterministic_ready",
            readiness_report_digest=READY_DIGEST,
            mechanism_version=config.mechanism_version,
            configuration_digest=config.digest,
            phase_affinity=dict(config.phase_capability_affinity),
        )

    drifted = replace(
        _input(
            graph=graph,
            environment=environment,
            catalog=catalog,
            phase="execution",
            requirement_revision="requirement-r1",
            obligations=("execute artifact",),
        ),
        registry_versions=FrozenDict(
            {
                "arg_role_catalog": "arg-role-catalog-v1:stale",
                "arg_role_catalog_digest": "0" * 64,
            }
        ),
    )
    with pytest.raises(ARGInputError, match="catalog"):
        ARGInputEncoder().encode(
            policy_input=drifted,
            task_summary="execute artifact",
            current_graph=graph,
            role_catalog=catalog,
            readiness_stage="implementation_validated",
            readiness_status="deterministic_ready",
            readiness_report_digest=READY_DIGEST,
            mechanism_version=config.mechanism_version,
            configuration_digest=config.digest,
            phase_affinity=dict(config.phase_capability_affinity),
        )
