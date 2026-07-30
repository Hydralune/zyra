from __future__ import annotations

import copy
import json
from dataclasses import replace
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
    canonical_digest,
)
from zyra_orchestration.topology_policy.arg import (
    ARGJointBuilderConfig,
    ARGModelObservation,
    ARGRoleCatalogBuilder,
    ARGTopologyRuntime,
)
from zyra_runtime import LocalArtifactStore
from zyra_scheduler.worker_pool import (
    ResourceVector,
    WorkerCapabilityManifest,
    WorkerLocation,
)


ROOT = Path(__file__).resolve().parents[2]
NOW = "2026-07-30T09:00:00Z"
INPUT_PRECHECK_DIGEST = (
    "82ad9a84a55506223fce2133168b7df2f0ae487aa7288fe5ede5c58869c1bbde"
)


def _header(contract_id: str, mechanism: str) -> ContractHeader:
    return ContractHeader(
        contract_id=contract_id,
        created_at=NOW,
        source_event_id="event-arg-runtime-source",
        correlation_id="correlation-arg-runtime",
        causation_id="continuity-gate-runtime",
        mechanism_id=mechanism,
        mechanism_version="v1",
        input_version="v1",
        idempotency_key=f"idempotency:{contract_id}",
    )


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


def _inputs():
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
    environment = EnvironmentSnapshot(
        header=_header("environment-runtime", "ResourceScheduler"),
        observed_at=NOW,
        observations=tuple(
            TelemetryObservation(
                observation_id=f"observation-{worker.worker_id}",
                resource_id=worker.worker_id,
                category="worker",
                observed_at=NOW,
                fresh_until="2026-07-30T09:10:00Z",
                confidence=1.0,
                observation_source="ResourceScheduler.worker_pool_api_projection",
                source_event_id="event-arg-runtime-source",
                physical_runtime_id=f"process-{worker.worker_id}",
                location="local",
                available=True,
                healthy=True,
                capacity_available=1,
                lease_available=True,
                privacy_classes=("internal",),
                allowed_placements=("local",),
            )
            for worker in workers
        ),
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
            for role in ("planning", "execution", "verification", "recovery")
        ),
        source_versions={"worker_registry_revision": 1},
    )
    return workers, environment, catalog


def _custody(tmp_path: Path) -> GraphStateCustody:
    store = GraphStateStore(tmp_path / "graph.sqlite3")
    store.initialize()
    custody = GraphStateCustody(store)
    custody.create(graph_id_value="graph-arg-runtime", run_id="run-arg-runtime")
    return custody


def _policy_input(
    *,
    graph,
    environment,
    catalog,
    report_digest: str,
    readiness_stage: str,
    readiness_status: str,
    phase: str = "execution",
    requirement_revision: str = "requirement-r1",
) -> PolicyInputSnapshot:
    return PolicyInputSnapshot(
        header=_header(
            f"policy-input-{phase}-{requirement_revision}",
            "PolicyInputSnapshotBuilder",
        ),
        run_id=graph.run_id,
        task_id="task-arg-runtime",
        phase=phase,
        requirement_revision=requirement_revision,
        graph=GraphSnapshotRef(
            graph_id=graph.graph_id,
            run_id=graph.run_id,
            revision=graph.revision,
            signature=graph.signature,
            commit_id=graph.commit_id,
        ),
        nodes=(),
        registered_roles=tuple(item.role_id for item in catalog.profiles),
        registered_capabilities=tuple(
            capability
            for item in catalog.profiles
            for capability in item.capabilities
        ),
        unresolved_obligations=(
            f"{phase} the artifact",
            "produce and verify deliverable",
        ),
        registry_versions=FrozenDict(
            {
                "arg_role_catalog": catalog.catalog_version,
                "arg_role_catalog_digest": catalog.digest,
                **dict(catalog.source_versions),
            }
        ),
        environment=environment,
        memory_refs=(),
        readiness_refs=(
            MechanismEvidenceReadinessReportRef(
                header=_header("arg-readiness-runtime", "arg_designer"),
                report_ref="artifact://arg-implementation-readiness",
                report_digest=report_digest,
                readiness_stage=readiness_stage,
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
        allowed_placements=("local",),
        privacy_class="internal",
        last_topology_change_at="2026-07-30T08:00:00Z",
    )


def _report(
    tmp_path: Path,
    *,
    status: str,
    config: ARGJointBuilderConfig,
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
    report["slice_id"] = "P2-S03-01"
    report["readiness_stage"] = "implementation_validated"
    report["supersedes_report_digest"] = INPUT_PRECHECK_DIGEST
    report["activation_allowed"] = False
    report["activation_reason"] = (
        "P2-S03-01 validates ARG only; default activation remains closed"
    )
    for mechanism_id, mechanism in report["mechanisms"].items():
        mechanism["readiness_stage"] = "implementation_validated"
        if mechanism_id != "arg_designer":
            mechanism["status"] = "unavailable"
            report["mechanism_statuses"][mechanism_id] = "unavailable"
    arg = report["mechanisms"]["arg_designer"]
    arg["status"] = status
    report["mechanism_statuses"]["arg_designer"] = status
    arg["implementation_validation"] = {
        "passed": status == "deterministic_ready",
        "mechanism_version": config.mechanism_version,
        "configuration_digest": config.digest,
        "catalog_schema_version": config.catalog_schema_version,
        "canonical_mutation_attempted": False,
        "training_sample_count": 0,
    }
    report.pop("report_digest", None)
    report["report_digest"] = canonical_digest(report)
    path = tmp_path / f"readiness-{status}.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path, report["report_digest"]


def test_validation_runtime_publishes_joint_proposal_without_committing_graph(
    tmp_path: Path,
) -> None:
    custody = _custody(tmp_path)
    graph = custody.current("graph-arg-runtime")
    _, environment, catalog = _inputs()
    config = ARGJointBuilderConfig.load(
        ROOT / "config" / "phase2" / "arg-joint-topology.json"
    )
    report_path, report_digest = _report(
        tmp_path,
        status="deterministic_ready",
        config=config,
    )
    policy_input = _policy_input(
        graph=graph,
        environment=environment,
        catalog=catalog,
        report_digest=report_digest,
        readiness_stage="implementation_validated",
        readiness_status="deterministic_ready",
    )
    events = []
    publisher = PolicyEvidencePublisher(
        LocalArtifactStore(tmp_path / "artifacts"),
        admit_event=events.append,
    )
    runtime = ARGTopologyRuntime.from_repository(
        ROOT,
        report_path=report_path,
        evidence_publisher=publisher,
        admit_event=events.append,
    )

    first = runtime.execute(
        policy_input=policy_input,
        task_summary="Implement and verify the deliverable",
        current_graph=graph,
        role_catalog=catalog,
    )
    second = runtime.execute(
        policy_input=policy_input,
        task_summary="Implement and verify the deliverable",
        current_graph=graph,
        role_catalog=catalog,
        publish=False,
    )

    assert first.mode == "validation"
    assert first.proposal is not None
    assert first.published_evidence is not None
    assert publisher.replay(first.published_evidence.artifact).digest == first.proposal.digest
    assert first.proposal.digest == second.proposal.digest
    assert first.hypothesis.to_dict() == second.hypothesis.to_dict()
    assert first.canonical_graph_unchanged is True
    assert custody.current(graph.graph_id).revision == 0
    assert all(
        step.incident_edges for step in first.hypothesis.role_steps
    )
    assert any(
        item.payload.get("canonical_mutation_attempted") is False
        for item in events
    )


def test_evidence_only_emits_diagnostic_proposal_and_unavailable_uses_baseline(
    tmp_path: Path,
) -> None:
    custody = _custody(tmp_path)
    graph = custody.current("graph-arg-runtime")
    _, environment, catalog = _inputs()
    config = ARGJointBuilderConfig.load(
        ROOT / "config" / "phase2" / "arg-joint-topology.json"
    )

    evidence_path, evidence_digest = _report(
        tmp_path,
        status="evidence_only",
        config=config,
    )
    evidence_input = _policy_input(
        graph=graph,
        environment=environment,
        catalog=catalog,
        report_digest=evidence_digest,
        readiness_stage="implementation_validated",
        readiness_status="evidence_only",
    )
    evidence = ARGTopologyRuntime.from_repository(
        ROOT,
        report_path=evidence_path,
    ).execute(
        policy_input=evidence_input,
        task_summary="Implement and verify the deliverable",
        current_graph=graph,
        role_catalog=catalog,
    )
    assert evidence.mode == "diagnostic"
    assert evidence.proposal is not None
    assert evidence.readiness.canonical_mutation_allowed is False
    assert evidence.canonical_graph_unchanged is True

    unavailable_path, unavailable_digest = _report(
        tmp_path,
        status="unavailable",
        config=config,
    )
    unavailable_input = _policy_input(
        graph=graph,
        environment=environment,
        catalog=catalog,
        report_digest=unavailable_digest,
        readiness_stage="implementation_validated",
        readiness_status="unavailable",
    )
    unavailable = ARGTopologyRuntime.from_repository(
        ROOT,
        report_path=unavailable_path,
    ).execute(
        policy_input=unavailable_input,
        task_summary="Implement and verify the deliverable",
        current_graph=graph,
        role_catalog=catalog,
    )
    assert unavailable.mode == "baseline"
    assert unavailable.proposal is None
    assert unavailable.degraded is True
    assert unavailable.baseline_decision["profile_id"] == "phase1_deterministic_baseline"
    assert unavailable.baseline_decision["phase_conditioned_joint_arg"] is False
    assert custody.current(graph.graph_id).revision == 0


def test_disable_catalog_drift_config_corruption_and_model_anomaly_degrade_explicitly(
    tmp_path: Path,
) -> None:
    custody = _custody(tmp_path)
    graph = custody.current("graph-arg-runtime")
    _, environment, catalog = _inputs()
    config = ARGJointBuilderConfig.load(
        ROOT / "config" / "phase2" / "arg-joint-topology.json"
    )
    report_path, report_digest = _report(
        tmp_path,
        status="deterministic_ready",
        config=config,
    )
    policy_input = _policy_input(
        graph=graph,
        environment=environment,
        catalog=catalog,
        report_digest=report_digest,
        readiness_stage="implementation_validated",
        readiness_status="deterministic_ready",
    )
    runtime = ARGTopologyRuntime.from_repository(ROOT, report_path=report_path)
    enabled = runtime.execute(
        policy_input=policy_input,
        task_summary="Implement and verify the deliverable",
        current_graph=graph,
        role_catalog=catalog,
        publish=False,
    )
    disabled = runtime.execute(
        policy_input=policy_input,
        task_summary="Implement and verify the deliverable",
        current_graph=graph,
        role_catalog=catalog,
        enabled=False,
        publish=False,
    )
    assert enabled.proposal is not None
    assert disabled.proposal is None
    assert disabled.degraded_reason == "arg_disabled"
    assert enabled.to_dict() != disabled.to_dict()

    drifted_input = replace(
        policy_input,
        registry_versions=FrozenDict(
            {
                "arg_role_catalog": "arg-role-catalog-v1:drifted",
                "arg_role_catalog_digest": "0" * 64,
            }
        ),
    )
    drifted = runtime.execute(
        policy_input=drifted_input,
        task_summary="Implement and verify the deliverable",
        current_graph=graph,
        role_catalog=catalog,
        publish=False,
    )
    assert drifted.degraded_reason == "input_catalog_drift"

    malformed_config = tmp_path / "arg-config-corrupt.json"
    malformed_config.write_text("{", encoding="utf-8")
    corrupt = ARGTopologyRuntime.from_repository(
        ROOT,
        config_path=malformed_config,
        report_path=report_path,
    ).execute(
        policy_input=policy_input,
        task_summary="Implement and verify the deliverable",
        current_graph=graph,
        role_catalog=catalog,
        publish=False,
    )
    assert corrupt.mode == "baseline"
    assert corrupt.degraded_reason.startswith("arg_config")

    observation = ARGModelObservation(
        model_id="fixed-model",
        model_version="v1",
        input_digest="c" * 64,
        output_digest="d" * 64,
        observation_ref="artifact://fixed-model-observation",
        semantic_terms=("execution",),
    )
    model_anomaly = runtime.execute(
        policy_input=policy_input,
        task_summary="Implement and verify the deliverable",
        current_graph=graph,
        role_catalog=catalog,
        model_observation=observation,
        publish=False,
    )
    assert model_anomaly.mode == "baseline"
    assert model_anomaly.degraded_reason == "model_observation_input_mismatch"
    assert custody.current(graph.graph_id).revision == 0


def test_requirement_and_phase_revision_change_real_graph_candidates_and_old_is_rejected(
    tmp_path: Path,
) -> None:
    custody = _custody(tmp_path)
    graph = custody.current("graph-arg-runtime")
    _, environment, catalog = _inputs()
    config = ARGJointBuilderConfig.load(
        ROOT / "config" / "phase2" / "arg-joint-topology.json"
    )
    report_path, report_digest = _report(
        tmp_path,
        status="deterministic_ready",
        config=config,
    )
    runtime = ARGTopologyRuntime.from_repository(ROOT, report_path=report_path)
    planning_input = _policy_input(
        graph=graph,
        environment=environment,
        catalog=catalog,
        report_digest=report_digest,
        readiness_stage="implementation_validated",
        readiness_status="deterministic_ready",
        phase="planning",
        requirement_revision="requirement-r1",
    )
    recovery_input = _policy_input(
        graph=graph,
        environment=environment,
        catalog=catalog,
        report_digest=report_digest,
        readiness_stage="implementation_validated",
        readiness_status="deterministic_ready",
        phase="recovery",
        requirement_revision="requirement-r2",
    )
    planning = runtime.execute(
        policy_input=planning_input,
        task_summary="Plan the delivery",
        current_graph=graph,
        role_catalog=catalog,
        publish=False,
    )
    recovery = runtime.execute(
        policy_input=recovery_input,
        task_summary="Recover the failed delivery",
        current_graph=graph,
        role_catalog=catalog,
        recent_recovery_outcome={"fault_id": "fault-1", "recovered": False},
        publish=False,
    )

    assert planning.proposal is not None and recovery.proposal is not None
    assert planning.proposal.digest != recovery.proposal.digest
    assert planning.hypothesis.role_steps[0].role_id == "planning_role"
    assert recovery.hypothesis.role_steps[0].role_id == "recovery_role"
    assert [item.to_dict() for item in planning.proposal.operations] != [
        item.to_dict() for item in recovery.proposal.operations
    ]
    try:
        ARGTopologyRuntime.validate_proposal(
            policy_input=recovery_input,
            role_catalog=catalog,
            proposal=planning.proposal,
        )
    except ValueError as error:
        assert "old input" in str(error)
    else:  # pragma: no cover - explicit fail-closed assertion
        raise AssertionError("old requirement proposal was accepted")
    assert custody.current(graph.graph_id).revision == 0
