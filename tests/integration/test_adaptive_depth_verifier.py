from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from zyra_core import ArtifactKind, PlanNodeStatus, TaskState, create_task_state
from zyra_orchestration.topology_policy import (
    ContractHeader,
    EnvironmentSnapshot,
    FrozenDict,
    GraphSnapshotRef,
    MemoryContinuityReceipt,
    PolicyBudget,
    PolicyInputSnapshot,
    StableArtifactRef,
    canonical_digest,
)
from zyra_runtime import (
    LocalArtifactStore,
    PermissionRequestQueue,
    PermissionStateStore,
)
from zyra_runtime.permission.canonical import (
    arguments_digest,
    build_request_fingerprint,
)
from zyra_runtime.permission.models import (
    PermissionRequestRecord,
    PermissionScope,
    PermissionScopeKind,
    ToolIdentity,
)
from zyra_scheduler import (
    AdaptiveDepthRuntime,
    CanonicalExitSnapshotBuilder,
    DeterministicEarlyExitGate,
    EarlyExitGateConfig,
    ExitDecision,
    ExitPosteriorResult,
    LayerExecutionReceipt,
    OperatorCandidate,
    OperatorLayerProposal,
    OperatorScoreComponents,
    OperatorSelectionProposal,
    build_final_verifier_decision,
    build_operator_execution_decision,
    minimum_operator_path,
)
from zyra_scheduler.recovery_runtime import (
    CheckpointCommitRequest,
    CheckpointCommitRuntime,
    RecoveryCheckpoint,
    RecoveryPlanStore,
    RecoveryRefs,
    SideEffectFenceRuntime,
)


ROOT = Path(__file__).resolve().parents[2]
NOW = "2026-07-30T08:00:00Z"
FRESH_UNTIL = "2026-07-30T08:05:00Z"
REQUIREMENT_REVISION = "requirement-r1"
OBLIGATIONS = (
    "implement required artifact",
    "verify required artifact",
)


def _header(
    contract_id: str,
    mechanism_id: str,
    *,
    causation_id: str = "commit-early-exit",
    configuration_digest: str = "",
) -> ContractHeader:
    return ContractHeader(
        contract_id=contract_id,
        created_at=NOW,
        source_event_id="event-early-exit-trigger",
        correlation_id="correlation-early-exit",
        causation_id=causation_id,
        mechanism_id=mechanism_id,
        mechanism_version="deterministic-v1",
        input_version="v1",
        idempotency_key=f"idempotency:{contract_id}",
        configuration_digest=configuration_digest,
    )


def _task() -> TaskState:
    task = create_task_state("Implement and verify the required artifact.")
    task.constraints.requirements[:] = [OBLIGATIONS[0]]
    task.constraints.success_criteria[:] = [OBLIGATIONS[1]]
    task.constraints.required_artifacts[:] = ["deliverable"]
    task.metadata["requirement_revision"] = REQUIREMENT_REVISION
    return task


def _policy_input(
    task: TaskState,
    *,
    requirement_revision: str = REQUIREMENT_REVISION,
    obligations: tuple[str, ...] = OBLIGATIONS,
) -> PolicyInputSnapshot:
    return PolicyInputSnapshot(
        header=_header("policy-input-early-exit", "PolicyInputSnapshotBuilder"),
        run_id=task.run_id,
        task_id=task.task_id,
        phase="execution",
        requirement_revision=requirement_revision,
        graph=GraphSnapshotRef(
            graph_id="graph-early-exit",
            run_id=task.run_id,
            revision=4,
            signature=canonical_digest(("graph-early-exit", 4)),
            commit_id="commit-early-exit",
        ),
        nodes=(),
        registered_roles=("implementer", "verifier"),
        registered_capabilities=("artifact-production", "verification"),
        unresolved_obligations=obligations,
        registry_versions=FrozenDict(
            {
                "graph_state_custody": "v1",
                "resource_scheduler": "v1",
                "permission_runtime": "v1",
            }
        ),
        environment=EnvironmentSnapshot(
            header=_header(
                "environment-early-exit",
                "EnvironmentSnapshotBuilder",
            ),
            observed_at=NOW,
            observations=(),
        ),
        memory_refs=(),
        readiness_refs=(),
        budget=PolicyBudget(
            remaining_tokens=10_000,
            remaining_cost_usd=5,
            remaining_time_ms=120_000,
            max_communication_bytes=32_000,
            max_fan_out=4,
            max_topology_churn=4,
            minimum_dwell_seconds=0,
        ),
        allowed_permissions=("worker.dispatch",),
        allowed_placements=("local",),
        privacy_class="internal",
        last_topology_change_at="2026-07-30T07:00:00Z",
    )


def _candidate(
    index: int,
    *,
    verifier: bool = False,
) -> OperatorCandidate:
    operator_id = (
        "worker:final-verifier" if verifier else f"worker:operator-{index}"
    )
    return OperatorCandidate(
        operator_id=operator_id,
        operator_type="worker",
        version="1",
        profile_digest=canonical_digest(("profile", operator_id)),
        score=1.0 - index / 100,
        score_components=OperatorScoreComponents(
            capability_obligation_coverage=10_000,
            permission=10_000,
            health=10_000,
            cost=8_000,
            latency=8_000,
            verifier_necessity=10_000 if verifier else 5_000,
            semantic_match=10_000,
            confidence=9_500,
        ),
        reasons=("deterministic test proposal",),
        encoding_digest=canonical_digest(("encoding", operator_id)),
        cold_start=False,
        confidence=0.95,
        estimated_tokens=100,
        estimated_cost_usd=0.01,
        estimated_latency_ms=25,
    )


def _proposal(
    policy_input: PolicyInputSnapshot,
    *,
    proposal_id: str = "proposal-early-exit",
) -> OperatorSelectionProposal:
    candidates = (
        _candidate(1),
        _candidate(2, verifier=True),
        _candidate(3),
        _candidate(4),
    )
    return OperatorSelectionProposal(
        header=_header(proposal_id, "MaasOperatorSelector"),
        proposal_id=proposal_id,
        input_snapshot_digest=policy_input.digest,
        requirement_revision=policy_input.requirement_revision,
        committed_graph_id=policy_input.graph.graph_id,
        committed_graph_revision=policy_input.graph.revision,
        committed_graph_signature=policy_input.graph.signature,
        committed_graph_commit_id=policy_input.graph.commit_id,
        catalog_version="catalog-v1",
        catalog_digest=canonical_digest(("catalog", "v1")),
        context_digest=canonical_digest(("context", policy_input.digest)),
        encoder_profile="deterministic_semantic_v1",
        encoder_observation_digest=canonical_digest(("encoder", "observed")),
        layers=tuple(
            OperatorLayerProposal(
                layer_index=index,
                candidates=(candidate,),
                reason=f"adaptive depth layer {index}",
            )
            for index, candidate in enumerate(candidates, start=1)
        ),
        alternatives=(),
        filter_verdicts=(),
        expected_breadth=1,
        expected_depth=4,
        reasons=("validation-only adaptive-depth proposal",),
        expires_at=FRESH_UNTIL,
        fallback_profile="deterministic_continue",
    )


def _continuity_receipt(
    policy_input: PolicyInputSnapshot,
) -> MemoryContinuityReceipt:
    return MemoryContinuityReceipt(
        header=_header(
            "continuity-early-exit",
            "MemoryContinuityVerifier",
            causation_id="checkpoint-early-exit",
        ),
        before_digest=canonical_digest(("continuity", "before")),
        after_digest=canonical_digest(("continuity", "after")),
        requirement_revision=policy_input.requirement_revision,
        critical_fact_results=FrozenDict(),
        obligation_results=FrozenDict(
            {
                "consumed_ids": list(policy_input.unresolved_obligations),
                "missing_consumption": [],
                "stale_requirement_execution": False,
            }
        ),
        provenance_refs=(),
        rejected_memory_refs=(),
        downstream_decision_ref="decision-memory-consumption",
        continuity_result="passed",
    )


def _checkpoint(
    task: TaskState,
    store: RecoveryPlanStore,
    *,
    requirement_revision: str = REQUIREMENT_REVISION,
) -> RecoveryCheckpoint:
    fences = SideEffectFenceRuntime(store)
    fence, _ = fences.reserve(
        run_id=task.run_id,
        task_id=task.task_id,
        operation="write-deliverable",
        request={"artifact": "deliverable"},
        idempotency_key="write-deliverable-r1",
    )
    fences.commit(
        fence.fence_key,
        response={"artifact": "deliverable", "committed": True},
        receipt_ref="artifact-owner-receipt",
    )
    checkpoint, _, _ = CheckpointCommitRuntime(store).commit(
        CheckpointCommitRequest(
            refs=RecoveryRefs(
                run_id=task.run_id,
                task_id=task.task_id,
                session_id="session-early-exit",
            ),
            workflow_signature="workflow-early-exit-v1",
            graph_signature="graph-early-exit-v1",
            topology_signature="topology-early-exit-v1",
            owner_refs={
                "task": "TaskState",
                "artifact": "LocalArtifactStore",
                "permission": "PermissionStateStore",
                "side_effect": "RecoveryPlanStore",
            },
            version_refs={"requirement_revision": requirement_revision},
            side_effect_fence_keys=(fence.fence_key,),
            state_payload={"phase": "adaptive-depth"},
            metadata={"requirement_revision": requirement_revision},
        )
    )
    return checkpoint


@dataclass
class _VerifierOwner:
    receipts: dict[str, dict]

    def record(self, decision) -> None:
        self.receipts[str(decision.metadata["verifier_receipt_ref"])] = dict(
            decision.metadata
        )

    def resolve_final_verifier_receipt(self, receipt_ref: str):
        return self.receipts.get(receipt_ref)


@dataclass
class _ExecutionPort:
    task: TaskState
    artifacts: LocalArtifactStore
    policy_input: PolicyInputSnapshot
    verifier_owner: _VerifierOwner

    def execute_layer(
        self,
        *,
        proposal: OperatorSelectionProposal,
        layer: OperatorLayerProposal,
    ) -> LayerExecutionReceipt:
        del proposal
        operator_refs = tuple(
            f"{candidate.operator_id}@{candidate.version}"
            for candidate in layer.candidates
        )
        owner_receipt_ref = f"resource-scheduler://attempt/layer-{layer.layer_index}"
        verification_refs: tuple[str, ...] = ()
        stable_artifacts: tuple[StableArtifactRef, ...] = ()
        execution = build_operator_execution_decision(
            task=self.task,
            layer_index=layer.layer_index,
            candidates=layer.candidates,
            owner_receipt_ref=owner_receipt_ref,
            actual_tokens=40,
            actual_cost_usd=0.004,
            actual_latency_ms=10,
            verification_refs=(
                ("final_verifier",) if layer.layer_index == 2 else ()
            ),
            created_at=NOW,
        )
        self.task.decisions.append(execution)
        if layer.layer_index == 2:
            artifact = self.artifacts.write_text(
                run_id=self.task.run_id,
                task_id=self.task.task_id,
                content="verified deliverable",
                title="deliverable",
                kind=ArtifactKind.TEXT,
                extension=".txt",
                producer_node_id=self.task.root_node_id,
            )
            self.task.artifacts.append(artifact)
            self.task.status = PlanNodeStatus.COMPLETED
            for node in self.task.plan_nodes.values():
                node.status = PlanNodeStatus.COMPLETED
            observed = self.artifacts.verify(artifact)
            stable_artifacts = (
                StableArtifactRef(
                    ref_id=artifact.artifact_id,
                    uri=artifact.uri,
                    digest=observed.sha256,
                    media_type=str(
                        artifact.metadata.get("content_type")
                        or "application/octet-stream"
                    ),
                ),
            )
            final_verifier = build_final_verifier_decision(
                task=self.task,
                requirement_revision=self.policy_input.requirement_revision,
                expected_obligation_ids=self.policy_input.unresolved_obligations,
                verified_artifact_refs=stable_artifacts,
                passed=True,
                verifier_version="final-verifier-v1",
                verifier_receipt_ref="verifier://final/receipt-r1",
                verified_at=NOW,
                fresh_until=FRESH_UNTIL,
            )
            self.verifier_owner.record(final_verifier)
            self.task.decisions.append(final_verifier)
            verification_refs = ("verifier://final/receipt-r1",)
        return LayerExecutionReceipt(
            layer_index=layer.layer_index,
            operator_refs=operator_refs,
            owner_receipt_ref=owner_receipt_ref,
            decision_refs=(execution.decision_id,),
            artifact_refs=stable_artifacts,
            verification_refs=verification_refs,
            actual_tokens=40,
            actual_cost_usd=0.004,
            actual_latency_ms=10,
        )


@dataclass
class _EligibilityPort:
    builder: CanonicalExitSnapshotBuilder
    task: TaskState
    policy_input: PolicyInputSnapshot
    proposal: OperatorSelectionProposal
    checkpoint: RecoveryCheckpoint
    continuity: MemoryContinuityReceipt

    def capture(
        self,
        *,
        remaining_candidates: Sequence[OperatorCandidate],
        minimum_operator_refs: Sequence[str],
        minimum_verification_refs: Sequence[str],
    ):
        return self.builder.capture(
            task=self.task,
            policy_input=self.policy_input,
            proposal=self.proposal,
            checkpoint=self.checkpoint,
            continuity_receipt=self.continuity,
            required_artifact_ids=("deliverable",),
            minimum_operator_refs=minimum_operator_refs,
            minimum_verification_refs=minimum_verification_refs,
            remaining_candidates=remaining_candidates,
            observed_at=NOW,
        )


@dataclass
class _Harness:
    task: TaskState
    policy_input: PolicyInputSnapshot
    proposal: OperatorSelectionProposal
    continuity: MemoryContinuityReceipt
    checkpoint: RecoveryCheckpoint
    recovery_store: RecoveryPlanStore
    artifacts: LocalArtifactStore
    permission_queue: PermissionRequestQueue
    gate: DeterministicEarlyExitGate
    builder: CanonicalExitSnapshotBuilder
    verifier_owner: _VerifierOwner


def _harness(tmp_path: Path) -> _Harness:
    task = _task()
    policy_input = _policy_input(task)
    proposal = _proposal(policy_input)
    continuity = _continuity_receipt(policy_input)
    recovery_store = RecoveryPlanStore(tmp_path / "recovery.sqlite3")
    checkpoint = _checkpoint(task, recovery_store)
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    permission_queue = PermissionRequestQueue(
        PermissionStateStore(tmp_path / "permission.json"),
        "session-early-exit",
    )
    config = EarlyExitGateConfig.load(
        ROOT / "config" / "phase2" / "maas-early-exit.json"
    )
    gate = DeterministicEarlyExitGate(config)
    verifier_owner = _VerifierOwner({})
    builder = CanonicalExitSnapshotBuilder(
        config=config,
        artifact_store=artifacts,
        permission_queue=permission_queue,
        side_effect_store=recovery_store,
        final_verifier_owner=verifier_owner,
    )
    return _Harness(
        task=task,
        policy_input=policy_input,
        proposal=proposal,
        continuity=continuity,
        checkpoint=checkpoint,
        recovery_store=recovery_store,
        artifacts=artifacts,
        permission_queue=permission_queue,
        gate=gate,
        builder=builder,
        verifier_owner=verifier_owner,
    )


def _execute(harness: _Harness, *, enabled: bool):
    return AdaptiveDepthRuntime(harness.gate).execute(
        proposal=harness.proposal,
        execution_port=_ExecutionPort(
            harness.task,
            harness.artifacts,
            harness.policy_input,
            harness.verifier_owner,
        ),
        eligibility_port=_EligibilityPort(
            harness.builder,
            harness.task,
            harness.policy_input,
            harness.proposal,
            harness.checkpoint,
            harness.continuity,
        ),
        early_exit_enabled=enabled,
        evaluated_at=NOW,
    )


def test_verifier_gate_exits_only_after_real_owner_evidence(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    result = _execute(harness, enabled=True)

    assert result.exited_early is True
    assert len(result.executed_layers) == 2
    assert result.final_decision.decision is ExitDecision.EXIT
    assert (
        result.final_decision.posterior_result
        is ExitPosteriorResult.TRUE_EXIT
    )
    assert result.cost_receipt.task_completed is True
    assert result.cost_receipt.verifier_passed is True
    assert result.cost_receipt.artifact_complete is True
    assert result.cost_receipt.avoided_operator_refs == (
        "worker:operator-3@1",
        "worker:operator-4@1",
    )
    assert result.cost_receipt.estimated_avoided_tokens == 200
    assert result.policy_outcome.verifier_result == "passed"
    assert result.policy_outcome.permission_result == "settled"
    assert result.policy_outcome.recovery_result == "not_required"
    assert len(result.policy_outcome.artifact_refs) == 1
    assert result.events[-1].payload["posterior_result"] == "true_exit"


def test_self_signed_final_verifier_without_owner_receipt_forces_continue(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    completed = _execute(harness, enabled=True)
    harness.verifier_owner.receipts.clear()
    minimum_operators, minimum_verification = minimum_operator_path(
        harness.proposal
    )

    snapshot = harness.builder.capture(
        task=harness.task,
        policy_input=harness.policy_input,
        proposal=harness.proposal,
        checkpoint=harness.checkpoint,
        continuity_receipt=harness.continuity,
        required_artifact_ids=("deliverable",),
        minimum_operator_refs=minimum_operators,
        minimum_verification_refs=minimum_verification,
        remaining_candidates=harness.proposal.candidates[2:],
        observed_at=NOW,
    )
    decision = harness.gate.evaluate(snapshot, evaluated_at=NOW)

    assert completed.final_decision.decision is ExitDecision.EXIT
    assert snapshot.final_verifier_passed is False
    assert decision.decision is ExitDecision.CONTINUE
    assert "final_verifier_passed" in decision.failed_conditions


def test_cancelled_node_critical_obligation_forces_continue(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    cancelled_obligation = OBLIGATIONS[1]
    harness.task.plan_nodes[harness.task.root_node_id].completion_criteria = [
        cancelled_obligation
    ]
    completed = _execute(harness, enabled=True)
    harness.task.plan_nodes[
        harness.task.root_node_id
    ].status = PlanNodeStatus.CANCELLED
    minimum_operators, minimum_verification = minimum_operator_path(
        harness.proposal
    )

    snapshot = harness.builder.capture(
        task=harness.task,
        policy_input=harness.policy_input,
        proposal=harness.proposal,
        checkpoint=harness.checkpoint,
        continuity_receipt=harness.continuity,
        required_artifact_ids=("deliverable",),
        minimum_operator_refs=minimum_operators,
        minimum_verification_refs=minimum_verification,
        remaining_candidates=harness.proposal.candidates[2:],
        observed_at=NOW,
    )
    decision = harness.gate.evaluate(snapshot, evaluated_at=NOW)

    assert completed.final_decision.decision is ExitDecision.EXIT
    assert set(snapshot.unresolved_critical_obligation_ids) == set(
        OBLIGATIONS
    )
    assert cancelled_obligation in snapshot.unresolved_critical_obligation_ids
    assert decision.decision is ExitDecision.CONTINUE
    assert "critical_obligations_resolved" in decision.failed_conditions


def test_disabled_gate_executes_full_depth_with_same_completed_outcome(
    tmp_path: Path,
) -> None:
    enabled = _execute(_harness(tmp_path / "enabled"), enabled=True)
    disabled = _execute(_harness(tmp_path / "disabled"), enabled=False)

    assert enabled.exited_early is True
    assert disabled.exited_early is False
    assert len(enabled.executed_layers) == 2
    assert len(disabled.executed_layers) == 4
    assert disabled.final_decision.decision is ExitDecision.CONTINUE
    assert (
        disabled.final_decision.posterior_result
        is ExitPosteriorResult.NOT_EXITED
    )
    assert disabled.cost_receipt.task_completed is True
    assert disabled.policy_outcome.verifier_result == "passed"
    assert enabled.cost_receipt.actual_tokens < disabled.cost_receipt.actual_tokens
    assert (
        enabled.cost_receipt.actual_cost_usd
        < disabled.cost_receipt.actual_cost_usd
    )
    assert (
        enabled.cost_receipt.actual_latency_ms
        < disabled.cost_receipt.actual_latency_ms
    )


def test_canonical_pending_permission_owner_forces_continue(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    result = _execute(harness, enabled=True)
    identity = ToolIdentity(
        namespace="builtin",
        name="write-deliverable",
        version="1",
        schema_digest="schema-v1",
    )
    digest = arguments_digest({"artifact": "deliverable"})
    fingerprint = build_request_fingerprint(
        identity,
        digest,
        session_id=harness.permission_queue.session_id,
        tool_use_id="tool-use-pending",
        run_id=harness.task.run_id,
        task_id=harness.task.task_id,
    )
    scope = PermissionScope(
        PermissionScopeKind.ACTION,
        session_id=harness.permission_queue.session_id,
        task_id=harness.task.task_id,
        run_id=harness.task.run_id,
        tool_namespace=identity.namespace,
        tool_name=identity.name,
        argument_digest=digest,
        request_fingerprint=fingerprint,
    )
    harness.permission_queue.create(
        PermissionRequestRecord(
            request_id="permission-pending-early-exit",
            session_id=harness.permission_queue.session_id,
            task_id=harness.task.task_id,
            run_id=harness.task.run_id,
            tool_use_id="tool-use-pending",
            tool_identity=identity,
            arguments_digest=digest,
            request_fingerprint=fingerprint,
            scope=scope,
            expires_at="2099-01-01T00:00:00+00:00",
            reason_code="risk.write_review",
            reason="pending write requires a canonical permission decision",
        )
    )
    minimum_operators, minimum_verification = minimum_operator_path(
        harness.proposal
    )
    snapshot = harness.builder.capture(
        task=harness.task,
        policy_input=harness.policy_input,
        proposal=harness.proposal,
        checkpoint=harness.checkpoint,
        continuity_receipt=harness.continuity,
        required_artifact_ids=("deliverable",),
        minimum_operator_refs=minimum_operators,
        minimum_verification_refs=minimum_verification,
        remaining_candidates=harness.proposal.candidates[2:],
        observed_at=NOW,
    )
    decision = harness.gate.evaluate(snapshot, evaluated_at=NOW)

    assert result.final_decision.decision is ExitDecision.EXIT
    assert snapshot.permission_pending_ids == (
        "permission-pending-early-exit",
    )
    assert decision.decision is ExitDecision.CONTINUE
    assert "permission_settled" in decision.failed_conditions
