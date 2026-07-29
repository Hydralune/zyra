from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from zyra_core import (
    ArtifactKind,
    EventRecord,
    EventType,
    create_task_state,
    now_iso,
)
from zyra_evaluation.policy_benchmark.continuity import (
    build_continuity_evidence_report,
    require_continuity_hard_gates,
)
from zyra_memory import MemoryFabric, MemoryLayer, MemoryRecord, SQLiteStore
from zyra_orchestration.deployment.dispatch import DeploymentDispatchRuntime
from zyra_orchestration.deployment.handoff import CheckpointHandoffRuntime
from zyra_orchestration.deployment.models import DeploymentProfile
from zyra_orchestration.deployment.placement import (
    PlacementContext,
    PlacementPolicyRuntime,
)
from zyra_orchestration.deployment.process_manager import DeploymentProcessManager
from zyra_orchestration.deployment.profiles import ProfileCatalog
from zyra_orchestration.deployment.state_store import DeploymentStateStore
from zyra_orchestration.graph_custody import (
    GraphNode,
    GraphStateCustody,
    GraphStateStore,
)
from zyra_orchestration.topology_policy import (
    ContractHeader,
    ContinuityTransition,
    ContinuityTransitionKind,
    DownstreamMemoryUsage,
    EnvironmentSnapshot,
    FrozenDict,
    MechanismEvidenceReadinessReportRef,
    MemoryContinuityError,
    MemoryContinuityVerifier,
    PolicyBudget,
    PolicyEvidencePublisher,
    TelemetryObservation,
    canonical_digest,
)
from zyra_runtime import LocalArtifactStore
from tests.integration.test_deployment_node_processes import (
    PROJECT_ROOT,
    _free_port_block,
    _workload,
)


NOW = "2026-07-30T08:00:00Z"


def _header(
    contract_id: str,
    *,
    causation_id: str,
    mechanism: str = "MemoryContinuityVerifier",
) -> ContractHeader:
    return ContractHeader(
        contract_id=contract_id,
        created_at=NOW,
        source_event_id="event-continuity-source",
        correlation_id="correlation-continuity",
        causation_id=causation_id,
        mechanism_id=mechanism,
        mechanism_version="deterministic-v1",
        input_version="memory-fabric-canonical-v1",
        idempotency_key=f"idempotency:{contract_id}",
        configuration_digest="c" * 64,
    )


def _task_and_memory(tmp_path: Path):
    store = SQLiteStore(tmp_path / "memory.sqlite3")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    task = create_task_state(
        "Preserve the signed release checksum across compact, handoff, and restart."
    )
    task.constraints.requirements.append("Ship only the signed release r1.")
    completed = artifacts.write_text(
        run_id=task.run_id,
        task_id=task.task_id,
        content="artifact already completed once",
        title="completed release manifest",
        kind=ArtifactKind.STRUCTURED_DATA,
        extension=".json",
        producer_node_id=task.root_node_id,
    )
    task.artifacts.append(completed)
    source_event = EventRecord(
        run_id=task.run_id,
        task_id=task.task_id,
        event_id="event-critical-release-checksum",
        event_type=EventType.AGENT_MESSAGE,
        node_id=task.root_node_id,
        payload={
            "summary": "Verified signed checksum is sha256:release-r1.",
            "artifact_id": completed.artifact_id,
        },
    )
    store.append_event(source_event)
    store.save_checkpoint(task)
    content = {
        "fact": "signed release checksum",
        "value": "sha256:release-r1",
        "requirement_revision": "requirement-r1",
    }
    fact = MemoryRecord(
        memory_id="memory-critical-release-checksum",
        run_id=task.run_id,
        task_id=task.task_id,
        layer=MemoryLayer.SEMANTIC,
        source_type="event_log",
        source_id=source_event.event_id,
        node_id=task.root_node_id,
        summary="The signed release checksum is sha256:release-r1.",
        content=content,
        artifact_ids=[completed.artifact_id],
        evidence_ids=[source_event.event_id],
        score=1.0,
        metadata={
            "continuity_status": "active",
            "continuity_version": "requirement-r1/fact-v1",
            "content_digest": canonical_digest(content),
            "requirement_revision": "requirement-r1",
        },
    )
    store.save_memory_records([fact])
    return task, store, artifacts, completed, fact, source_event


def _obligations(task) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                *task.constraints.requirements,
                *task.constraints.success_criteria,
            }
        )
    )


def _environment() -> EnvironmentSnapshot:
    return EnvironmentSnapshot(
        header=_header(
            "environment-continuity",
            causation_id="event-continuity-source",
            mechanism="ResourceScheduler",
        ),
        observed_at=NOW,
        observations=(
            TelemetryObservation(
                observation_id="worker-local-observation",
                resource_id="worker-local",
                category="worker",
                observed_at=NOW,
                fresh_until="2026-07-30T09:00:00Z",
                confidence=1.0,
                observation_source="ResourceScheduler.worker_pool_api_projection",
                source_event_id="event-continuity-source",
                physical_runtime_id="process-after-restart",
                location="local",
                available=True,
                healthy=True,
                capacity_available=1,
                lease_available=True,
                privacy_classes=("internal",),
                allowed_placements=("local",),
            ),
        ),
        required_categories=("worker",),
    )


def _readiness() -> tuple[MechanismEvidenceReadinessReportRef, ...]:
    return (
        MechanismEvidenceReadinessReportRef(
            header=_header(
                "readiness-continuity",
                causation_id="event-continuity-source",
                mechanism="ARG",
            ),
            report_ref="artifact://readiness/arg",
            report_digest="a" * 64,
            readiness_stage="activation_ready",
            status="deterministic_ready",
        ),
    )


def _graph(task, path: Path) -> GraphStateCustody:
    graph_store = GraphStateStore(path)
    graph_store.initialize()
    custody = GraphStateCustody(graph_store)
    custody.create(graph_id_value="graph-continuity", run_id=task.run_id)
    return custody


def test_compact_restore_and_process_restart_gate_policy_input_and_usage(
    tmp_path: Path,
) -> None:
    task, store, artifacts, completed, fact, source_event = _task_and_memory(tmp_path)
    first_fabric = MemoryFabric(store=store, artifact_store=artifacts)
    first_verifier = MemoryContinuityVerifier(first_fabric)
    before = first_verifier.capture(
        run_id=task.run_id,
        task_id=task.task_id,
        requirement_revision="requirement-r1",
        obligation_ids=_obligations(task),
        critical_fact_ids=(fact.memory_id,),
        completed_artifact_ids=(completed.artifact_id,),
    )
    compact = first_fabric.compact_context(
        task,
        store.task_events(task.task_id),
        focus="signed checksum and unresolved release obligation",
        source_event_id=source_event.event_id,
    )

    restarted_fabric = MemoryFabric(store=SQLiteStore(store.path), artifact_store=artifacts)
    restarted_verifier = MemoryContinuityVerifier(restarted_fabric)
    compact_gate = restarted_verifier.verify_before_policy(
        before,
        transition=ContinuityTransition(
            transition_id="transition-compact-restore",
            kind=ContinuityTransitionKind.COMPACT_RESTORE,
            owner_receipt_ref=compact.compact_id,
            source_scope="context-epoch-1",
            target_scope="context-epoch-2",
            checkpoint_ref=task.task_id,
            compact_ref=compact.compact_id,
        ),
        current_requirement_revision="requirement-r1",
        current_obligation_ids=_obligations(task),
        critical_fact_ids=(fact.memory_id,),
        completed_artifact_ids=(completed.artifact_id,),
    )
    assert compact_gate.passed is True

    restart_gate = restarted_verifier.verify_before_policy(
        compact_gate.after,
        transition=ContinuityTransition(
            transition_id="transition-process-restart",
            kind=ContinuityTransitionKind.PROCESS_RESTART,
            owner_receipt_ref=f"checkpoint:{task.task_id}",
            source_scope="runtime-before",
            target_scope="runtime-after",
            checkpoint_ref=task.task_id,
            source_process_id="process-before-restart",
            target_process_id="process-after-restart",
        ),
        current_requirement_revision="requirement-r1",
        current_obligation_ids=_obligations(task),
        critical_fact_ids=(fact.memory_id,),
        completed_artifact_ids=(completed.artifact_id,),
    )
    assert restart_gate.passed is True

    custody = _graph(task, tmp_path / "graph.sqlite3")
    policy_input = restarted_verifier.build_policy_input(
        restart_gate,
        task=task,
        graph=custody.current("graph-continuity"),
        environment=_environment(),
        header=_header(
            "policy-input-after-restart",
            causation_id=restart_gate.gate_id,
            mechanism="PolicyInputSnapshotBuilder",
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
        readiness_refs=_readiness(),
        registry_versions={"memory_fabric": "canonical-v1"},
        registered_roles=("worker",),
        registered_capabilities=("verify",),
        allowed_permissions=("graph.write",),
        allowed_placements=("local",),
    )
    assert policy_input.requirement_revision == "requirement-r1"
    assert [item.ref_id for item in policy_input.memory_refs] == [fact.memory_id]

    usage = DownstreamMemoryUsage(
        decision_ref="decision-first-after-restart",
        requirement_revision="requirement-r1",
        critical_fact_ids=(fact.memory_id,),
        obligation_ids=_obligations(task),
        source_event_refs=("event-first-decision-after-restart",),
        tool_call_ref="tool-call-verify-signed-release",
        worker_id="worker-after-restart",
        node_id=task.root_node_id,
    )
    final = restarted_verifier.finalize_after_decision(
        restart_gate,
        usage,
        header=_header(
            "memory-continuity-after-restart",
            causation_id=usage.decision_ref,
        ),
    )
    assert final.passed is True
    assert final.receipt.continuity_result == "passed"
    assert (
        final.receipt.critical_fact_results[fact.memory_id]["consumed"]
        is True
    )

    events: list[EventRecord] = []
    publisher = PolicyEvidencePublisher(artifacts, admit_event=events.append)
    published = publisher.publish(
        final.receipt,
        run_id=task.run_id,
        task_id=task.task_id,
        producer_node_id=task.root_node_id,
    )
    replayed = publisher.replay(published.artifact)
    assert replayed.digest == final.receipt.digest
    assert events[0].event_type is EventType.CONSTRAINT_CHECK
    report = build_continuity_evidence_report([final.receipt])
    require_continuity_hard_gates(report)
    assert report.critical_fact_recall == 1.0
    assert report.obligation_retention == 1.0


def test_requirement_revision_and_unsafe_memory_fail_closed(tmp_path: Path) -> None:
    task, store, artifacts, completed, fact, _source_event = _task_and_memory(tmp_path)
    verifier = MemoryContinuityVerifier(
        MemoryFabric(store=store, artifact_store=artifacts)
    )
    before = verifier.capture(
        run_id=task.run_id,
        task_id=task.task_id,
        requirement_revision="requirement-r1",
        obligation_ids=_obligations(task),
        critical_fact_ids=(fact.memory_id,),
        completed_artifact_ids=(completed.artifact_id,),
    )
    task.constraints.requirements[:] = ["Ship only the signed release r2."]
    poisoned_content = dict(fact.content)
    poisoned_content["value"] = "tampered"
    poisoned = replace(
        fact,
        memory_id="memory-poisoned-release-checksum",
        content=poisoned_content,
        metadata={
            **fact.metadata,
            "continuity_status": "active",
            "content_digest": canonical_digest(fact.content),
        },
    )
    superseded = replace(
        fact,
        memory_id="memory-superseded-requirement",
        metadata={
            **fact.metadata,
            "continuity_status": "superseded",
            "continuity_version": "requirement-r1/superseded",
        },
    )
    stale = replace(
        fact,
        memory_id="memory-stale-release-checksum",
        metadata={
            **fact.metadata,
            "continuity_status": "stale",
            "continuity_version": "requirement-r0/stale",
        },
    )
    conflicting = replace(
        fact,
        memory_id="memory-conflicting-release-checksum",
        metadata={
            **fact.metadata,
            "continuity_status": "conflicting",
            "conflicts_with": [fact.memory_id],
            "continuity_version": "requirement-r1/conflicting",
        },
    )
    store.save_memory_records([poisoned, superseded, stale, conflicting])
    gate = verifier.verify_before_policy(
        before,
        transition=ContinuityTransition(
            transition_id="transition-requirement-r2",
            kind=ContinuityTransitionKind.CHECKPOINT_RESTART,
            owner_receipt_ref="checkpoint:requirement-r2",
            source_scope="requirement-r1",
            target_scope="requirement-r2",
            checkpoint_ref=task.task_id,
        ),
        current_requirement_revision="requirement-r2",
        current_obligation_ids=_obligations(task),
        critical_fact_ids=(
            fact.memory_id,
            poisoned.memory_id,
            superseded.memory_id,
            stale.memory_id,
            conflicting.memory_id,
        ),
        completed_artifact_ids=(completed.artifact_id,),
    )
    assert gate.passed is False
    assert "unsafe_memory_rejected" in gate.reason_codes
    assert "critical_fact_missing" in gate.reason_codes
    assert {item.ref_id for item in gate.rejected_memory_refs} == {
        poisoned.memory_id,
        superseded.memory_id,
        stale.memory_id,
        conflicting.memory_id,
    }
    with pytest.raises(MemoryContinuityError) as error:
        verifier.build_policy_input(
            gate,
            task=task,
            graph=_graph(task, tmp_path / "rejected-graph.sqlite3").current(
                "graph-continuity"
            ),
            environment=_environment(),
            header=_header("blocked-input", causation_id=gate.gate_id),
            budget=PolicyBudget(1, 0, 1, 1, 1, 1, 0),
            readiness_refs=_readiness(),
            registry_versions={},
        )
    assert error.value.action in {"recover", "replan"}

    clean_gate = verifier.verify_before_policy(
        before,
        transition=ContinuityTransition(
            transition_id="transition-current-r2",
            kind=ContinuityTransitionKind.REQUIREMENT_REVISION,
            owner_receipt_ref="event:requirement-r2",
            source_scope="requirement-r1",
            target_scope="requirement-r2",
        ),
        current_requirement_revision="requirement-r2",
        current_obligation_ids=_obligations(task),
        critical_fact_ids=(fact.memory_id,),
        completed_artifact_ids=(completed.artifact_id,),
    )
    assert clean_gate.passed is True
    stale_usage = DownstreamMemoryUsage(
        decision_ref="decision-stale-r1",
        requirement_revision="requirement-r1",
        critical_fact_ids=(fact.memory_id,),
        obligation_ids=_obligations(task),
        source_event_refs=("event-stale-decision",),
        tool_call_ref="tool-call-stale-requirement",
    )
    stale_result = verifier.finalize_after_decision(
        clean_gate,
        stale_usage,
        header=_header(
            "continuity-stale-requirement",
            causation_id=stale_usage.decision_ref,
        ),
    )
    assert stale_result.passed is False
    assert "stale_requirement_execution" in stale_result.reason_codes


def test_real_worker_handoff_acknowledges_memory_and_does_not_repeat_artifact(
    tmp_path: Path,
) -> None:
    task, memory_store, artifacts, completed, fact, _ = _task_and_memory(tmp_path)
    verifier = MemoryContinuityVerifier(
        MemoryFabric(store=memory_store, artifact_store=artifacts)
    )
    before = verifier.capture(
        run_id=task.run_id,
        task_id=task.task_id,
        requirement_revision="requirement-r1",
        obligation_ids=_obligations(task),
        critical_fact_ids=(fact.memory_id,),
        completed_artifact_ids=(completed.artifact_id,),
    )

    base_port = _free_port_block()
    deployment_root = tmp_path / "deployment"
    deployment_store = DeploymentStateStore(
        deployment_root / "deployment.sqlite3"
    )
    catalog = ProfileCatalog.defaults(
        PROJECT_ROOT,
        host="127.0.0.1",
        base_port=base_port,
        environment={},
    )
    manager = DeploymentProcessManager(
        project_root=PROJECT_ROOT,
        state_root=deployment_root,
        store=deployment_store,
    )
    clients = {}
    try:
        for profile in (DeploymentProfile.DEVICE, DeploymentProfile.EDGE):
            _record, client, health = manager.start_node(catalog.policy(profile))
            assert health["profile"] == profile.value
            clients[profile] = client
        observations = {
            profile: client.observation()
            for profile, client in clients.items()
        }
        workload = _workload(
            DeploymentProfile.EDGE,
            task_id=task.task_id,
            run_id=task.run_id,
        )
        placement = PlacementPolicyRuntime(catalog, deployment_store, environment={})
        decision = placement.decide(
            workload,
            PlacementContext(observations=observations),
        )
        assert decision.selected_profile is DeploymentProfile.EDGE
        source_receipt = DeploymentDispatchRuntime(deployment_store).dispatch(
            workload,
            decision,
            clients[DeploymentProfile.EDGE],
        )
        handoff = CheckpointHandoffRuntime(
            deployment_store,
            canonical_verifier=lambda **_: {"ready": True},
        ).handoff(
            workload=workload,
            source_receipt=source_receipt,
            target_attempt_id="attempt-device-after-handoff",
            source_client=clients[DeploymentProfile.EDGE],
            target_client=clients[DeploymentProfile.DEVICE],
            target_profile=DeploymentProfile.DEVICE,
        )
        assert handoff.verified is True
    finally:
        manager.stop_all(timeout_seconds=5)

    gate = verifier.verify_before_policy(
        before,
        transition=ContinuityTransition(
            transition_id=handoff.handoff_id,
            kind=ContinuityTransitionKind.WORKER_HANDOFF,
            owner_receipt_ref=handoff.handoff_id,
            source_scope=handoff.source_attempt_id,
            target_scope=handoff.target_attempt_id,
            checkpoint_ref=handoff.canonical_checkpoint_ref,
            acknowledged=True,
            acknowledged_requirement_revision="requirement-r1",
            acknowledged_obligation_digest=before.obligation_digest,
            acknowledged_fact_ids=(fact.memory_id,),
        ),
        current_requirement_revision="requirement-r1",
        current_obligation_ids=_obligations(task),
        critical_fact_ids=(fact.memory_id,),
        completed_artifact_ids=(completed.artifact_id,),
    )
    assert gate.passed is True
    usage = DownstreamMemoryUsage(
        decision_ref="decision-device-after-handoff",
        requirement_revision="requirement-r1",
        critical_fact_ids=(fact.memory_id,),
        obligation_ids=_obligations(task),
        source_event_refs=("event-device-first-decision",),
        tool_call_ref="tool-call-device-verify",
        produced_artifact_ids=("artifact-new-device-result",),
        worker_id="device",
        node_id=task.root_node_id,
    )
    final = verifier.finalize_after_decision(
        gate,
        usage,
        header=_header(
            "continuity-worker-handoff",
            causation_id=usage.decision_ref,
        ),
    )
    assert final.passed is True
    assert final.receipt.obligation_results["duplicate_work_artifact_ids"] == ()

    duplicate = verifier.finalize_after_decision(
        gate,
        replace(
            usage,
            decision_ref="decision-device-duplicate",
            produced_artifact_ids=(completed.artifact_id,),
        ),
        header=_header(
            "continuity-worker-handoff-duplicate",
            causation_id="decision-device-duplicate",
        ),
    )
    assert duplicate.passed is False
    assert "duplicate_completed_artifact" in duplicate.reason_codes


def test_node_replacement_and_checkpoint_restart_keep_continuity(
    tmp_path: Path,
) -> None:
    task, store, artifacts, completed, fact, _ = _task_and_memory(tmp_path)
    verifier = MemoryContinuityVerifier(
        MemoryFabric(store=store, artifact_store=artifacts)
    )
    before = verifier.capture(
        run_id=task.run_id,
        task_id=task.task_id,
        requirement_revision="requirement-r1",
        obligation_ids=_obligations(task),
        critical_fact_ids=(fact.memory_id,),
        completed_artifact_ids=(completed.artifact_id,),
    )
    custody = _graph(task, tmp_path / "replacement-graph.sqlite3")
    seed = custody.branch(
        "graph-continuity",
        branch_id="seed-node",
        actor_id="DynamicTopologyRuntime",
        causation_id="event-seed-node",
        idempotency_key="seed-node",
    )
    seed.add_node(
        GraphNode(
            node_id="worker-before",
            role="worker",
            capabilities=("verify",),
        )
    )
    assert custody.commit(seed.build()).receipt.committed
    replacement = custody.branch(
        "graph-continuity",
        branch_id="replace-node",
        actor_id="DynamicTopologyRuntime",
        causation_id="event-node-lost",
        idempotency_key="replace-node",
    )
    replacement.remove_node("worker-before", expected_revision=1)
    replacement.add_node(
        GraphNode(
            node_id="worker-after",
            role="worker",
            capabilities=("verify",),
        )
    )
    replacement_commit = custody.commit(replacement.build())
    assert replacement_commit.receipt.committed

    node_gate = verifier.verify_before_policy(
        before,
        transition=ContinuityTransition(
            transition_id="transition-node-replacement",
            kind=ContinuityTransitionKind.NODE_REPLACEMENT,
            owner_receipt_ref=replacement_commit.receipt.commit_id,
            source_scope="worker-before",
            target_scope="worker-after",
            acknowledged=True,
            acknowledged_requirement_revision="requirement-r1",
            acknowledged_obligation_digest=before.obligation_digest,
            acknowledged_fact_ids=(fact.memory_id,),
        ),
        current_requirement_revision="requirement-r1",
        current_obligation_ids=_obligations(task),
        critical_fact_ids=(fact.memory_id,),
        completed_artifact_ids=(completed.artifact_id,),
    )
    assert node_gate.passed is True

    restarted = MemoryContinuityVerifier(
        MemoryFabric(store=SQLiteStore(store.path), artifact_store=artifacts)
    )
    checkpoint_gate = restarted.verify_before_policy(
        node_gate.after,
        transition=ContinuityTransition(
            transition_id="transition-checkpoint-restart",
            kind=ContinuityTransitionKind.CHECKPOINT_RESTART,
            owner_receipt_ref=f"checkpoint:{task.task_id}",
            source_scope="worker-after-before-restart",
            target_scope="worker-after-after-restart",
            checkpoint_ref=task.task_id,
        ),
        current_requirement_revision="requirement-r1",
        current_obligation_ids=_obligations(task),
        critical_fact_ids=(fact.memory_id,),
        completed_artifact_ids=(completed.artifact_id,),
    )
    assert checkpoint_gate.passed is True
    assert {
        item.ref_id for item in checkpoint_gate.accepted_fact_refs
    } == {fact.memory_id}


def test_continuity_disable_switch_is_test_only_and_disconnects_restore(
    tmp_path: Path,
) -> None:
    task, store, artifacts, completed, fact, _ = _task_and_memory(tmp_path)
    fabric = MemoryFabric(store=store, artifact_store=artifacts)
    with pytest.raises(ValueError, match="test-only"):
        MemoryContinuityVerifier(fabric, enabled=False)
    disabled = MemoryContinuityVerifier(
        fabric,
        enabled=False,
        test_mode=True,
    )
    before = disabled.capture(
        run_id=task.run_id,
        task_id=task.task_id,
        requirement_revision="requirement-r1",
        obligation_ids=_obligations(task),
        critical_fact_ids=(fact.memory_id,),
        completed_artifact_ids=(completed.artifact_id,),
    )
    gate = disabled.verify_before_policy(
        before,
        transition=ContinuityTransition(
            transition_id="transition-disabled-restore",
            kind=ContinuityTransitionKind.COMPACT_RESTORE,
            owner_receipt_ref="compact-disabled",
            source_scope="before",
            target_scope="after",
            checkpoint_ref=task.task_id,
            compact_ref="compact-disabled",
        ),
        current_requirement_revision="requirement-r1",
        current_obligation_ids=_obligations(task),
        critical_fact_ids=(fact.memory_id,),
        completed_artifact_ids=(completed.artifact_id,),
    )
    assert gate.passed is False
    assert gate.recovery_action == "recover"
    assert gate.reason_codes == ("continuity_verifier_disabled",)
