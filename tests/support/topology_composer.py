from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zyra_orchestration.graph_custody import GraphStateCustody, GraphStateStore
from zyra_orchestration.topology_policy import (
    ContractHeader,
    DefaultTopologyPolicy,
    DefaultTopologyPolicyRequest,
    EnvironmentSnapshot,
    FrozenDict,
    GraphSnapshotRef,
    MechanismEvidenceReadinessReportRef,
    PolicyBudget,
    PolicyInputSnapshot,
    PolicyNodeSnapshot,
    StableArtifactRef,
    TelemetryObservation,
    canonical_digest,
)
from zyra_orchestration.topology_policy.arg import (
    ARGRoleCatalog,
    ARGRoleCatalogBuilder,
)
from zyra_orchestration.topology_policy.condition import (
    CARDEdgeHysteresisState,
    CARDReplacementCandidate,
)
from zyra_orchestration.topology_policy.pruning import (
    CommunicationBudget,
    CommunicationEdgeType,
    CommunicationOutcomeObservation,
)
from zyra_orchestration.topology_policy.registry import (
    ResolutionPurpose,
    ValidationManifest,
)
from zyra_scheduler.worker_pool import (
    ResourceVector,
    WorkerCapabilityManifest,
    WorkerLocation,
)


ROOT = Path(__file__).resolve().parents[2]
_IMPORTED_AT = datetime.now(UTC)
NOW = _IMPORTED_AT.isoformat(timespec="milliseconds").replace("+00:00", "Z")
COMPLETED = (_IMPORTED_AT - timedelta(minutes=5)).isoformat(
    timespec="milliseconds"
).replace("+00:00", "Z")
FRESH_UNTIL = (_IMPORTED_AT + timedelta(minutes=10)).isoformat(
    timespec="milliseconds"
).replace("+00:00", "Z")
LAST_TOPOLOGY_CHANGE = (_IMPORTED_AT - timedelta(hours=1)).isoformat(
    timespec="milliseconds"
).replace("+00:00", "Z")
REPORT_PATHS = {
    "arg_designer": ROOT
    / "docs"
    / "release"
    / "phase2"
    / "activation-readiness.json",
    "card": ROOT
    / "docs"
    / "release"
    / "phase2"
    / "activation-readiness.json",
    "agentprune": ROOT
    / "docs"
    / "release"
    / "phase2"
    / "activation-readiness.json",
}


def header(
    contract_id: str,
    mechanism_id: str,
    *,
    source_event_id: str = "event-topology-trigger",
    causation_id: str = "continuity-verdict-topology",
) -> ContractHeader:
    return ContractHeader(
        contract_id=contract_id,
        created_at=NOW,
        source_event_id=source_event_id,
        correlation_id="correlation-topology-composer",
        causation_id=causation_id,
        mechanism_id=mechanism_id,
        mechanism_version="v1",
        input_version="v1",
        idempotency_key=f"idempotency:{contract_id}",
    )


def readiness_digests() -> dict[str, str]:
    values = {}
    for mechanism_id, path in REPORT_PATHS.items():
        report = json.loads(path.read_text(encoding="utf-8"))
        values[mechanism_id] = str(report["report_digest"])
    return values


def custody(tmp_path: Path, *, suffix: str = "composer") -> GraphStateCustody:
    store = GraphStateStore(tmp_path / f"graph-{suffix}.sqlite3")
    store.initialize()
    selected = GraphStateCustody(store)
    selected.create(
        graph_id_value=f"graph-{suffix}",
        run_id=f"run-{suffix}",
    )
    return selected


def _worker(
    worker_id: str,
    role: str,
    capabilities: tuple[str, ...],
) -> WorkerCapabilityManifest:
    return WorkerCapabilityManifest(
        worker_id=worker_id,
        worker_kind=f"{role}_runtime",
        location=WorkerLocation.LOCAL,
        backend_ids=(f"backend-{worker_id}",),
        backend_kinds=("local_process",),
        capabilities=capabilities,
        tool_ids=(f"tool-{role}",),
        resource_capacity=ResourceVector(process_slots=1),
        constraints={
            "required_permissions": ["graph.write"],
            "allowed_placements": ["local"],
        },
        labels={"arg_role": role},
    )


def environment_and_catalog(
    *,
    faulted_worker_id: str = "",
    requirement_change: bool = False,
) -> tuple[EnvironmentSnapshot, ARGRoleCatalog]:
    workers = (
        _worker(
            "worker-planning",
            "planning_role",
            ("planning", "research", "decomposition"),
        ),
        _worker(
            "worker-execution",
            "execution_role",
            ("execution", "coding", "artifact_production"),
        ),
        _worker(
            "worker-verification",
            "verification_role",
            ("verification", "testing", "review"),
        ),
        _worker(
            "worker-recovery",
            "recovery_role",
            ("recovery", "fault_diagnosis", "checkpoint_restore"),
        ),
    )
    observations = []
    for index, worker in enumerate(workers):
        faulted = worker.worker_id == faulted_worker_id
        observations.append(
            TelemetryObservation(
                observation_id=f"observation-{worker.worker_id}",
                resource_id=worker.worker_id,
                category="worker",
                observed_at=NOW,
                fresh_until=FRESH_UNTIL,
                confidence=1.0,
                observation_source=(
                    "ResourceScheduler.worker_pool_api_projection"
                ),
                source_event_id=f"event-scheduler-{worker.worker_id}",
                physical_runtime_id=f"process-{worker.worker_id}",
                location="local",
                available=not faulted,
                healthy=not faulted,
                load=0.2 + index * 0.05,
                capacity_available=0 if faulted else 1,
                lease_available=not faulted,
                recent_failures=3 if faulted else 0,
                latency_p50_ms=20 + index * 5,
                latency_p95_ms=40 + index * 10,
                cost_usd=0.01,
                privacy_classes=("internal",),
                allowed_placements=("local",),
                attributes=FrozenDict(
                    {
                        "capabilities": list(worker.capabilities),
                        "queue_depth": index,
                        "recent_successes": 0 if faulted else 5,
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "network_state": (
                            "disconnected" if faulted else "connected"
                        ),
                        "fault": faulted,
                        "requirement_change": requirement_change,
                        "compact": False,
                        "recovery": faulted,
                    }
                ),
            )
        )
    environment = EnvironmentSnapshot(
        header=header("environment-topology-composer", "ResourceScheduler"),
        observed_at=NOW,
        observations=tuple(observations),
        required_categories=("worker",),
    )
    catalog = ARGRoleCatalogBuilder().build(
        worker_manifests=workers,
        environment=environment,
        tool_registry=(
            {
                "tool_id": f"tool-{role}",
                "enabled": True,
                "capabilities": [role],
            }
            for role in (
                "planning",
                "execution",
                "verification",
                "recovery",
            )
        ),
        source_versions={"worker_registry_revision": 7},
    )
    return environment, catalog


def policy_input(
    *,
    graph,
    environment: EnvironmentSnapshot,
    catalog: ARGRoleCatalog,
    phase: str = "execution",
    requirement_revision: str = "requirement-r1",
    unresolved_obligations: tuple[str, ...] | None = None,
    allowed_permissions: tuple[str, ...] = ("graph.write",),
    max_topology_churn: int = 32,
    max_communication_bytes: int = 16_384,
    max_fan_out: int = 4,
    readiness_stage: str = "activation_ready",
) -> PolicyInputSnapshot:
    digests = readiness_digests()
    return PolicyInputSnapshot(
        header=header(
            f"policy-input-{phase}-{requirement_revision}",
            "PolicyInputSnapshotBuilder",
        ),
        run_id=graph.run_id,
        task_id="task-topology-composer",
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
        registered_roles=tuple(item.role_id for item in catalog.profiles),
        registered_capabilities=tuple(
            capability
            for item in catalog.profiles
            for capability in item.capabilities
        ),
        unresolved_obligations=(
            unresolved_obligations
            if unresolved_obligations is not None
            else (
                f"{phase} the artifact",
                "produce and verify the requested deliverable",
            )
        ),
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
                ref_id="memory-continuity-r1",
                uri="urn:zyra:memory:continuity-r1",
                digest=canonical_digest(
                    ("continuity-r1", requirement_revision)
                ),
                media_type=(
                    "application/vnd.zyra.memory-continuity+json"
                ),
            ),
        ),
        readiness_refs=tuple(
            MechanismEvidenceReadinessReportRef(
                header=header(
                    f"readiness-{mechanism_id}",
                    mechanism_id,
                ),
                report_ref=str(REPORT_PATHS[mechanism_id].relative_to(ROOT)),
                report_digest=digests[mechanism_id],
                readiness_stage=readiness_stage,
                status="deterministic_ready",
            )
            for mechanism_id in ("arg_designer", "card", "agentprune")
        ),
        budget=PolicyBudget(
            remaining_tokens=10_000,
            remaining_cost_usd=10,
            remaining_time_ms=120_000,
            max_communication_bytes=max_communication_bytes,
            max_fan_out=max_fan_out,
            max_topology_churn=max_topology_churn,
            minimum_dwell_seconds=0,
        ),
        allowed_permissions=allowed_permissions,
        allowed_placements=("local",),
        privacy_class="internal",
        last_topology_change_at=LAST_TOPOLOGY_CHANGE,
    )


def policy(
    *,
    graph_custody: GraphStateCustody,
    events: list | None = None,
) -> DefaultTopologyPolicy:
    event_values = events if events is not None else []
    return DefaultTopologyPolicy.from_repository(
        ROOT,
        custody=graph_custody,
        baseline_executor=lambda frozen: {
            "profile_id": "phase1_deterministic_baseline",
            "input_digest": canonical_digest(frozen),
            "route_owner": "ResourceScheduler",
        },
        baseline_reference=lambda frozen: {
            "profile_id": "phase1_deterministic_baseline",
            "input_digest": canonical_digest(frozen),
            "route_owner": "ResourceScheduler",
        },
        owner_probe=lambda: {"canonical_owner": "GraphStateCustody"},
        admit_event=event_values.append,
    )


def communication_inputs(
    *,
    policy_value: DefaultTopologyPolicy,
    input_snapshot: PolicyInputSnapshot,
    current_graph,
    catalog: ARGRoleCatalog,
) -> tuple[
    tuple[CommunicationOutcomeObservation, ...],
    tuple[CARDReplacementCandidate, ...],
    dict[str, CARDEdgeHysteresisState],
]:
    arg_result = policy_value.arg_runtime.execute(
        policy_input=input_snapshot,
        task_summary="execute, verify, and recover the requested artifact",
        current_graph=current_graph,
        role_catalog=catalog,
        enabled=True,
        publish=False,
    )
    assert arg_result.proposal is not None, arg_result.degraded_reason
    assert arg_result.hypothesis is not None
    assert len(arg_result.hypothesis.steps) >= 2
    source = arg_result.hypothesis.steps[0]
    target = arg_result.hypothesis.steps[1]
    temporal_edge_id = (
        "card-temporal-"
        + canonical_digest(
            (
                input_snapshot.requirement_revision,
                input_snapshot.phase,
                source.node_id,
                target.node_id,
            )
        )[:16]
    )
    replacements = (
        CARDReplacementCandidate(
            candidate_id=temporal_edge_id,
            source_node_id=source.node_id,
            target_node_id=target.node_id,
            relation="temporal_checkpoint_restore",
            edge_type="temporal",
            required_capabilities=tuple(target.capabilities[:1]),
            constraint_ref="requirement:continuity-recovery",
            reason="typed temporal lane for composer validation",
        ),
    )
    hysteresis = {
        temporal_edge_id: CARDEdgeHysteresisState(
            edge_id=temporal_edge_id,
            active=False,
            last_changed_at=LAST_TOPOLOGY_CHANGE,
            last_score=0,
            pending_action="add",
            confirmations=1,
        )
    }
    card_result = policy_value.card_runtime.execute(
        policy_input=input_snapshot,
        arg_base=arg_result.proposal,
        current_graph=current_graph,
        replacement_candidates=replacements,
        hysteresis_state=hysteresis,
        enabled=True,
        publish=False,
    )
    assert card_result.correction is not None, card_result.degraded_reason
    spatial = set(card_result.effective_spatial_edge_ids)
    temporal = set(card_result.effective_temporal_edge_ids)
    observations = []
    for index, decision in enumerate(card_result.correction.decisions):
        effective = (
            decision.edge_id in temporal
            if decision.edge_type == "temporal"
            else decision.edge_id in spatial
        )
        if not effective:
            continue
        observations.append(
            CommunicationOutcomeObservation(
                observation_id=f"communication-observation-{index}",
                run_id=input_snapshot.run_id,
                task_id=input_snapshot.task_id,
                window_id="window-before-composition",
                completed_at=COMPLETED,
                edge_id=decision.edge_id,
                source_node_id=decision.source_node_id,
                target_node_id=decision.target_node_id,
                edge_type=CommunicationEdgeType(decision.edge_type),
                round_index=index,
                message_id=f"message-{index}",
                payload_digest=canonical_digest(
                    (decision.edge_id, index)
                ),
                delivered=True,
                delivery_receipt_ref=f"receipt-delivery-{index}",
                usage_receipt_ref=f"receipt-usage-{index}",
                message_bytes=96,
                prompt_tokens=20,
                completion_tokens=10,
                cost_usd=0.01,
                evidence_refs=(f"evidence-{index}",),
                utilized_evidence_refs=(f"evidence-{index}",),
                artifact_refs=(f"artifact-{index}",),
                verifier_result="passed",
                causal_refs=("event-topology-trigger",),
            )
        )
    assert observations
    return tuple(observations), replacements, hysteresis


def request(
    *,
    policy_value: DefaultTopologyPolicy,
    input_snapshot: PolicyInputSnapshot,
    current_graph,
    catalog: ARGRoleCatalog,
    purpose: ResolutionPurpose = ResolutionPurpose.VALIDATION,
    trigger_kind: str = "task_ready",
) -> DefaultTopologyPolicyRequest:
    observations, replacements, hysteresis = communication_inputs(
        policy_value=policy_value,
        input_snapshot=input_snapshot,
        current_graph=current_graph,
        catalog=catalog,
    )
    return DefaultTopologyPolicyRequest(
        policy_input=input_snapshot,
        current_graph=current_graph,
        task_summary="execute, verify, and recover the requested artifact",
        role_catalog=catalog,
        communication_observations=observations,
        protections=(),
        communication_budget=CommunicationBudget(
            max_delivered_messages=max(1, len(observations)),
            max_delivered_bytes=max(96, 96 * len(observations)),
            max_delivered_tokens=max(30, 30 * len(observations)),
            max_cost_usd=max(0.01, 0.01 * len(observations)),
        ),
        mechanism_epoch=f"epoch-{input_snapshot.requirement_revision}",
        trigger_kind=trigger_kind,
        purpose=purpose,
        version=(
            "phase2_strongest_v1"
            if purpose is ResolutionPurpose.VALIDATION
            else None
        ),
        validation_manifest=(
            ValidationManifest(
                manifest_id="manifest-topology-composer",
                scenario_id="scenario-topology-composer",
                isolated=True,
                purpose="scenario",
            )
            if purpose is ResolutionPurpose.VALIDATION
            else None
        ),
        recovery_causal_refs=(
            ("receipt-recovery-topology",)
            if trigger_kind == "recovery"
            else ()
        ),
        replacement_candidates=replacements,
        hysteresis_state=hysteresis,
    )


def layer_results(
    *,
    policy_value: DefaultTopologyPolicy,
    request_value: DefaultTopologyPolicyRequest,
):
    arg_result = policy_value.arg_runtime.execute(
        policy_input=request_value.policy_input,
        task_summary=request_value.task_summary,
        current_graph=request_value.current_graph,
        role_catalog=request_value.role_catalog,
        branch_delta=request_value.branch_delta,
        recent_recovery_outcome=request_value.recent_recovery_outcome,
        model_observation=request_value.model_observation,
        enabled=request_value.switches.arg_enabled,
        publish=False,
    )
    assert arg_result.proposal is not None, arg_result.degraded_reason
    card_result = policy_value.card_runtime.execute(
        policy_input=request_value.policy_input,
        arg_base=arg_result.proposal,
        current_graph=request_value.current_graph,
        replacement_candidates=request_value.replacement_candidates,
        hysteresis_state=request_value.hysteresis_state,
        enabled=request_value.switches.card_enabled,
        publish=False,
    )
    assert card_result.correction is not None, card_result.degraded_reason
    upstream = card_result.correction_proposal or arg_result.proposal
    pruning_result = policy_value.pruning_runtime.execute(
        policy_input=request_value.policy_input,
        card_result=card_result,
        upstream_proposal=upstream,
        current_graph=request_value.current_graph,
        observations=request_value.communication_observations,
        protections=request_value.protections,
        budget=request_value.communication_budget,
        mechanism_epoch=request_value.mechanism_epoch,
        prior_mask=request_value.prior_pruning_mask,
        enabled=request_value.switches.agentprune_enabled,
        publish=False,
    )
    assert pruning_result.mask is not None, pruning_result.degraded_reason
    return arg_result, card_result, pruning_result


__all__ = [
    "NOW",
    "ROOT",
    "communication_inputs",
    "custody",
    "environment_and_catalog",
    "header",
    "layer_results",
    "policy",
    "policy_input",
    "readiness_digests",
    "request",
]
