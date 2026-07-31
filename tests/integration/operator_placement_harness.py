from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from zyra_core import ArtifactKind, PlanNodeStatus, TaskState, create_task_state, now_iso
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
from zyra_runtime import LocalArtifactStore, PermissionRequestQueue, PermissionStateStore
from zyra_scheduler import (
    AdaptiveDepthRuntime,
    CanonicalExitSnapshotBuilder,
    DeterministicEarlyExitGate,
    EarlyExitGateConfig,
    MaasReadinessResolution,
    OperatorCallResult,
    OperatorCatalog,
    OperatorLayerProposal,
    OperatorPlacementLeaseConfig,
    OperatorPlacementLeaseRuntime,
    OperatorProfile,
    OperatorScoreComponents,
    OperatorSelectionProposal,
    OperatorType,
    ResourceLocation,
    ResourceScheduler,
    WorkerBackendKind,
    WorkerManifest,
    WorkerPool,
    build_final_verifier_decision,
)
from zyra_scheduler.operator_policy.selector import OperatorCandidate
from zyra_scheduler.recovery_runtime import (
    CheckpointCommitRequest,
    CheckpointCommitRuntime,
    RecoveryCheckpoint,
    RecoveryPlanStore,
    RecoveryRefs,
    SideEffectFenceRuntime,
)
from zyra_scheduler.worker_pool import (
    BackendCapability,
    ResourceVector,
    WorkerPoolFoundationRuntime,
)
from zyra_scheduler.worker_pool.models import AttemptState, LeaseState


ROOT = Path(__file__).resolve().parents[2]
SECRET = b"p2-s04-03-worker-pool-secret-32-bytes"
OBLIGATIONS = ("produce deliverable", "verify deliverable")
REQUIREMENT_REVISION = "requirement-operator-r1"


def _iso_after(seconds: float) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).isoformat().replace("+00:00", "Z")


def _header(
    contract_id: str,
    mechanism_id: str,
    *,
    mechanism_version: str = "deterministic-v1",
    causation_id: str = "graph-commit-operator",
    configuration_digest: str = "",
) -> ContractHeader:
    return ContractHeader(
        contract_id=contract_id,
        created_at=now_iso(),
        source_event_id="event-operator-mechanism-input",
        correlation_id="correlation-operator-path",
        causation_id=causation_id,
        mechanism_id=mechanism_id,
        mechanism_version=mechanism_version,
        input_version="v1",
        idempotency_key=f"idempotency:{contract_id}",
        configuration_digest=configuration_digest,
    )


def task_state() -> TaskState:
    task = create_task_state("Produce and verify a deterministic deliverable.")
    task.constraints.requirements[:] = [OBLIGATIONS[0]]
    task.constraints.success_criteria[:] = [OBLIGATIONS[1]]
    task.constraints.required_artifacts[:] = ["deliverable"]
    task.metadata["requirement_revision"] = REQUIREMENT_REVISION
    return task


def policy_input(task: TaskState) -> PolicyInputSnapshot:
    return PolicyInputSnapshot(
        header=_header("policy-input-operator", "PolicyInputSnapshotBuilder"),
        run_id=task.run_id,
        task_id=task.task_id,
        phase="execution",
        requirement_revision=REQUIREMENT_REVISION,
        graph=GraphSnapshotRef(
            graph_id="graph-operator",
            run_id=task.run_id,
            revision=7,
            signature=canonical_digest(("graph-operator", 7)),
            commit_id="graph-commit-operator",
        ),
        nodes=(),
        registered_roles=("producer", "verifier"),
        registered_capabilities=("artifact-production", "verification"),
        unresolved_obligations=OBLIGATIONS,
        registry_versions=FrozenDict(
            {
                "graph_state_custody": "v1",
                "resource_scheduler": "v1",
                "worker_pool": "v1",
                "permission_runtime": "v1",
            }
        ),
        environment=EnvironmentSnapshot(
            header=_header("environment-operator", "EnvironmentSnapshotBuilder"),
            observed_at=now_iso(),
            observations=(),
        ),
        memory_refs=(),
        readiness_refs=(),
        budget=PolicyBudget(
            remaining_tokens=20_000,
            remaining_cost_usd=5,
            remaining_time_ms=180_000,
            max_communication_bytes=64_000,
            max_fan_out=4,
            max_topology_churn=4,
            minimum_dwell_seconds=0,
        ),
        allowed_permissions=("worker.dispatch", "tool.invoke"),
        allowed_placements=("local",),
        privacy_class="internal",
        last_topology_change_at=_iso_after(-300),
    )


def logical_scheduler(*, b_first: bool = False) -> ResourceScheduler:
    worker_ids = ("worker-b", "worker-a") if b_first else ("worker-a", "worker-b")
    manifests = [
        WorkerManifest(
            worker_id=worker_id,
            display_name=worker_id,
            runtime_worker=worker_id,
            location=ResourceLocation.LOCAL,
            backend=WorkerBackendKind.LOCAL_PROCESS,
            capabilities=[
                "artifact-production",
                "verification",
                "worker.dispatch",
                "tool.invoke",
            ],
            tools=["produce-tool", "optional-three", "optional-four"],
            models=["local-deterministic"],
            privacy_level="sensitive_ok",
            max_concurrency=4,
            latency_ms=10,
        )
        for worker_id in worker_ids
    ]
    return ResourceScheduler(WorkerPool(manifests))


def physical_pool(
    path: Path,
    *,
    process_slots: int = 4,
) -> WorkerPoolFoundationRuntime:
    pool = WorkerPoolFoundationRuntime(
        path,
        attestation_secret=SECRET,
        default_lease_ttl_seconds=30,
    )
    for worker_id in ("worker-a", "worker-b"):
        pool.register_local_worker(
            worker_id=worker_id,
            worker_kind="operator-worker",
            backend=BackendCapability(
                backend_id=f"local-{worker_id}",
                backend_kind="local_process",
                enabled=True,
                healthy=True,
                capabilities=("artifact-production", "verification"),
                tool_ids=("produce-tool", "optional-three", "optional-four"),
            ),
            capabilities=("artifact-production", "verification"),
            tool_ids=("produce-tool", "optional-three", "optional-four"),
            resources=ResourceVector(
                cpu_cores=2,
                memory_mb=1024,
                process_slots=process_slots,
            ),
        )
        pool.heartbeat_local_worker(worker_id, sequence=1)
    return pool


def _profile(
    operator_id: str,
    operator_type: OperatorType,
    *,
    source_ref: str,
    capabilities: tuple[str, ...],
    permissions: tuple[str, ...],
    verifier: bool = False,
) -> OperatorProfile:
    return OperatorProfile(
        operator_id=operator_id,
        operator_type=operator_type,
        version="1",
        display_name=operator_id,
        description=f"deterministic profile {operator_id}",
        capabilities=capabilities,
        input_contract=("task", "lease_candidate"),
        output_contract=("worker_result", "artifact_refs"),
        required_permissions=permissions,
        allowed_locations=("local",),
        allowed_privacy_classes=("internal", "project", "sensitive"),
        estimated_tokens=100,
        estimated_cost_usd=0.001,
        estimated_latency_ms=5,
        health_status="healthy",
        available_capacity=4,
        verifier_contracts=(
            ("final_verifier", "worker_result_verifier")
            if verifier
            else ("worker_result_verifier",)
        ),
        minimum_evidence_contract=("worker_lease", "worker_result"),
        cold_start=False,
        confidence=0.98,
        outcome_count=5,
        source_registry="test-live-registry",
        source_registry_version="1",
        source_ref=source_ref,
        metadata=FrozenDict(),
    )


def catalog(*, single_layer: bool = False) -> OperatorCatalog:
    if single_layer:
        entries = (
            _profile(
                "tool:produce-tool",
                OperatorType.TOOL,
                source_ref="produce-tool",
                capabilities=("artifact-production", "verification"),
                permissions=("tool.invoke",),
                verifier=True,
            ),
        )
    else:
        entries = (
            _profile(
                "tool:produce-tool",
                OperatorType.TOOL,
                source_ref="produce-tool",
                capabilities=("artifact-production",),
                permissions=("tool.invoke",),
            ),
            _profile(
                "worker:worker-b",
                OperatorType.WORKER,
                source_ref="worker-b",
                capabilities=("artifact-production", "verification"),
                permissions=("worker.dispatch",),
                verifier=True,
            ),
            _profile(
                "tool:optional-three",
                OperatorType.TOOL,
                source_ref="optional-three",
                capabilities=("verification",),
                permissions=("tool.invoke",),
            ),
            _profile(
                "tool:optional-four",
                OperatorType.TOOL,
                source_ref="optional-four",
                capabilities=("verification",),
                permissions=("tool.invoke",),
            ),
        )
    return OperatorCatalog(
        entries=entries,
        source_versions=FrozenDict({"live_registry": "1"}),
        generation=1,
        built_at=now_iso(),
    )


def _candidate(
    profile: OperatorProfile,
    *,
    verifier: bool,
    score: float,
) -> OperatorCandidate:
    return OperatorCandidate(
        operator_id=profile.operator_id,
        operator_type=profile.operator_type.value,
        version=profile.version,
        profile_digest=profile.digest,
        score=score,
        score_components=OperatorScoreComponents(
            capability_obligation_coverage=10_000,
            permission=10_000,
            health=10_000,
            cost=9_000,
            latency=9_000,
            verifier_necessity=10_000 if verifier else 5_000,
            semantic_match=10_000,
            confidence=9_800,
        ),
        reasons=("deterministic MaAS proposal",),
        encoding_digest=canonical_digest(("operator-encoding", profile.operator_id)),
        cold_start=False,
        confidence=0.98,
        estimated_tokens=profile.estimated_tokens,
        estimated_cost_usd=profile.estimated_cost_usd,
        estimated_latency_ms=profile.estimated_latency_ms,
    )


def proposal(
    policy: PolicyInputSnapshot,
    selected_catalog: OperatorCatalog,
    *,
    single_layer: bool = False,
) -> OperatorSelectionProposal:
    if single_layer:
        profile = selected_catalog.get("tool:produce-tool")
        assert profile is not None
        candidates = ((_candidate(profile, verifier=True, score=9_500.0),),)
    else:
        specs = (
            ("tool:produce-tool", False, 8_000.0),
            ("worker:worker-b", True, 10_000.0),
            ("tool:optional-three", False, 8_000.0),
            ("tool:optional-four", False, 8_000.0),
        )
        candidates = tuple(
            (
                _candidate(
                    selected_catalog.get(operator_id),  # type: ignore[arg-type]
                    verifier=verifier,
                    score=score,
                ),
            )
            for operator_id, verifier, score in specs
        )
    layers = tuple(
        OperatorLayerProposal(
            layer_index=index,
            candidates=items,
            reason=f"MaAS adaptive layer {index}",
        )
        for index, items in enumerate(candidates, start=1)
    )
    return OperatorSelectionProposal(
        header=_header(
            "proposal-operator",
            "maas",
            mechanism_version="maas_operator_selector_v1",
        ),
        proposal_id="proposal-operator",
        input_snapshot_digest=policy.digest,
        requirement_revision=policy.requirement_revision,
        committed_graph_id=policy.graph.graph_id,
        committed_graph_revision=policy.graph.revision,
        committed_graph_signature=policy.graph.signature,
        committed_graph_commit_id=policy.graph.commit_id,
        catalog_version=selected_catalog.catalog_version,
        catalog_digest=selected_catalog.digest,
        context_digest=canonical_digest(("operator-context", policy.digest)),
        encoder_profile="deterministic_semantic_v1",
        encoder_observation_digest=canonical_digest(
            ("operator-observation", policy.digest)
        ),
        layers=layers,
        alternatives=(),
        filter_verdicts=(),
        expected_breadth=1,
        expected_depth=len(layers),
        reasons=("deterministic operator candidate set",),
        expires_at=_iso_after(600),
        fallback_profile="phase1_resource_scheduler_baseline",
    )


def selector_result(proposal_value: OperatorSelectionProposal) -> Any:
    readiness_digest = canonical_digest(("P2-S04-03", "maas-readiness"))
    return SimpleNamespace(
        mode="validation",
        readiness=MaasReadinessResolution(
            stage="implementation_validated",
            status="deterministic_ready",
            mode="validation",
            report_ref="evidence://P2-S04-03/readiness",
            report_digest=readiness_digest,
            scheduler_input_allowed=True,
            placement_change_allowed=True,
            fallback_profile="phase1_resource_scheduler_baseline",
            reason="P2-S04-03 explicit validation",
        ),
        proposal=proposal_value,
        scheduler_input=proposal_value.scheduler_input().bind_task(
            run_id=proposal_value.header.correlation_id,
            task_id="placeholder",
        ),
        degraded=False,
        degraded_reason="",
    )


def continuity_receipt(policy: PolicyInputSnapshot) -> MemoryContinuityReceipt:
    return MemoryContinuityReceipt(
        header=_header(
            "continuity-operator",
            "MemoryContinuityVerifier",
            causation_id="checkpoint-operator",
        ),
        before_digest=canonical_digest(("continuity", "before")),
        after_digest=canonical_digest(("continuity", "after")),
        requirement_revision=policy.requirement_revision,
        critical_fact_results=FrozenDict(),
        obligation_results=FrozenDict(
            {
                "consumed_ids": list(policy.unresolved_obligations),
                "missing_consumption": [],
                "stale_requirement_execution": False,
            }
        ),
        provenance_refs=(),
        rejected_memory_refs=(),
        downstream_decision_ref="decision-memory-operator",
        continuity_result="passed",
    )


def checkpoint(
    task: TaskState,
    store: RecoveryPlanStore,
) -> RecoveryCheckpoint:
    fences = SideEffectFenceRuntime(store)
    fence, _ = fences.reserve(
        run_id=task.run_id,
        task_id=task.task_id,
        operation="write-deliverable",
        request={"artifact": "deliverable"},
        idempotency_key="write-deliverable-operator-r1",
    )
    fences.commit(
        fence.fence_key,
        response={"artifact": "deliverable", "committed": True},
        receipt_ref="artifact-owner-receipt",
    )
    selected, _, _ = CheckpointCommitRuntime(store).commit(
        CheckpointCommitRequest(
            refs=RecoveryRefs(
                run_id=task.run_id,
                task_id=task.task_id,
                session_id="session-operator",
            ),
            workflow_signature="workflow-operator-v1",
            graph_signature="graph-operator-v1",
            topology_signature="topology-operator-v1",
            owner_refs={
                "task": "TaskState",
                "artifact": "LocalArtifactStore",
                "permission": "PermissionStateStore",
                "side_effect": "RecoveryPlanStore",
            },
            version_refs={"requirement_revision": REQUIREMENT_REVISION},
            side_effect_fence_keys=(fence.fence_key,),
            state_payload={"phase": "operator-placement"},
            metadata={"requirement_revision": REQUIREMENT_REVISION},
        )
    )
    return selected


@dataclass
class EligibilityPort:
    builder: CanonicalExitSnapshotBuilder
    task: TaskState
    policy: PolicyInputSnapshot
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
            policy_input=self.policy,
            proposal=self.proposal,
            checkpoint=self.checkpoint,
            continuity_receipt=self.continuity,
            required_artifact_ids=("deliverable",),
            minimum_operator_refs=minimum_operator_refs,
            minimum_verification_refs=minimum_verification_refs,
            remaining_candidates=remaining_candidates,
        )


class VerifierOwner:
    def __init__(self) -> None:
        self.receipts: dict[str, dict[str, Any]] = {}

    def record(self, decision: Any) -> None:
        self.receipts[str(decision.metadata["verifier_receipt_ref"])] = dict(
            decision.metadata
        )

    def resolve_final_verifier_receipt(self, receipt_ref: str):
        return self.receipts.get(receipt_ref)


class ArtifactCallPort:
    def __init__(
        self,
        *,
        task: TaskState,
        policy: PolicyInputSnapshot,
        artifacts: LocalArtifactStore,
        complete_layer: int,
        pool: WorkerPoolFoundationRuntime,
        verifier_owner: VerifierOwner,
        revoke_in_prepare: bool = False,
        fail_first_worker: str = "",
        fail_after_side_effect: bool = False,
    ) -> None:
        self.task = task
        self.policy = policy
        self.artifacts = artifacts
        self.complete_layer = complete_layer
        self.pool = pool
        self.verifier_owner = verifier_owner
        self.revoke_in_prepare = revoke_in_prepare
        self.fail_first_worker = fail_first_worker
        self.fail_after_side_effect = fail_after_side_effect
        self.failed_workers: set[str] = set()
        self.side_effect_count = 0
        self.prepare_states: list[tuple[str, str]] = []

    def prepare(self, context: Any) -> None:
        lease = self.pool.store.require_lease(context.lease_id)
        attempt = self.pool.store.require_attempt(context.attempt_id)
        self.prepare_states.append((lease.state.value, attempt.state.value))
        if self.revoke_in_prepare:
            self.pool.leases.cancel(
                context.lease_id,
                reason="test revocation immediately before operator call",
            )
            self.revoke_in_prepare = False
        if (
            self.fail_first_worker
            and context.worker_id == self.fail_first_worker
            and context.worker_id not in self.failed_workers
        ):
            self.failed_workers.add(context.worker_id)
            raise RuntimeError("controlled worker failure before operator side effect")

    def execute(self, context: Any) -> OperatorCallResult:
        lease = self.pool.store.require_lease(context.lease_id)
        attempt = self.pool.store.require_attempt(context.attempt_id)
        assert lease.state is LeaseState.ACTIVE
        assert attempt.state is AttemptState.RUNNING
        self.side_effect_count += 1
        if self.fail_after_side_effect:
            self.fail_after_side_effect = False
            raise RuntimeError(
                "controlled failure after entering the side-effect boundary"
            )
        stable: tuple[StableArtifactRef, ...] = ()
        verification: tuple[str, ...] = ()
        if context.layer_index == self.complete_layer:
            artifact = self.artifacts.write_text(
                run_id=self.task.run_id,
                task_id=self.task.task_id,
                content="verified operator placement deliverable",
                title="deliverable",
                kind=ArtifactKind.TEXT,
                extension=".txt",
                producer_node_id=self.task.root_node_id,
            )
            if all(
                item.artifact_id != artifact.artifact_id
                for item in self.task.artifacts
            ):
                self.task.artifacts.append(artifact)
            self.task.status = PlanNodeStatus.COMPLETED
            for node in self.task.plan_nodes.values():
                node.status = PlanNodeStatus.COMPLETED
            observed = self.artifacts.verify(artifact)
            stable = (
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
            verifier_ref = "verifier://operator/final"
            final_verifier = build_final_verifier_decision(
                    task=self.task,
                    requirement_revision=self.policy.requirement_revision,
                    expected_obligation_ids=self.policy.unresolved_obligations,
                    verified_artifact_refs=stable,
                    passed=True,
                    verifier_version="operator-verifier-v1",
                    verifier_receipt_ref=verifier_ref,
                    verified_at=now_iso(),
                    fresh_until=_iso_after(300),
                )
            self.verifier_owner.record(final_verifier)
            self.task.decisions.append(final_verifier)
            verification = (verifier_ref,)
        finished = now_iso()
        return OperatorCallResult(
            operator_ref=context.operator_ref,
            call_ref=f"operator-call://{context.attempt_id}",
            artifact_refs=stable,
            verification_refs=verification,
            actual_tokens=40,
            actual_cost_usd=0.004,
            actual_latency_ms=10,
            summary=f"executed {context.operator_ref}",
            call_finished_at=finished,
            metadata=FrozenDict({"physical_worker_id": context.worker_id}),
        )


@dataclass
class Harness:
    task: TaskState
    policy: PolicyInputSnapshot
    catalog: OperatorCatalog
    proposal: OperatorSelectionProposal
    selector_result: Any
    scheduler: ResourceScheduler
    pool: WorkerPoolFoundationRuntime
    artifacts: LocalArtifactStore
    permission_queue: PermissionRequestQueue
    recovery_store: RecoveryPlanStore
    call_port: ArtifactCallPort
    runtime: OperatorPlacementLeaseRuntime
    adaptive: AdaptiveDepthRuntime
    eligibility: EligibilityPort


def build_harness(
    tmp_path: Path,
    *,
    single_layer: bool = False,
    b_first: bool = False,
    revoke_in_prepare: bool = False,
    fail_first_worker: str = "",
    fail_after_side_effect: bool = False,
) -> Harness:
    task = task_state()
    policy = policy_input(task)
    selected_catalog = catalog(single_layer=single_layer)
    selected_proposal = proposal(
        policy,
        selected_catalog,
        single_layer=single_layer,
    )
    selected_result = selector_result(selected_proposal)
    selected_result.scheduler_input = (
        selected_proposal.scheduler_input().bind_task(
            run_id=task.run_id,
            task_id=task.task_id,
        )
    )
    scheduler = logical_scheduler(b_first=b_first)
    pool = physical_pool(tmp_path / "worker-pool.sqlite3")
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    permission_queue = PermissionRequestQueue(
        PermissionStateStore(tmp_path / "permission.json"),
        "session-operator",
    )
    recovery_store = RecoveryPlanStore(tmp_path / "recovery.sqlite3")
    selected_checkpoint = checkpoint(task, recovery_store)
    continuity = continuity_receipt(policy)
    gate_config = EarlyExitGateConfig.load(
        ROOT / "config" / "phase2" / "maas-early-exit.json"
    )
    verifier_owner = VerifierOwner()
    builder = CanonicalExitSnapshotBuilder(
        config=gate_config,
        artifact_store=artifacts,
        permission_queue=permission_queue,
        side_effect_store=recovery_store,
        final_verifier_owner=verifier_owner,
    )
    call_port = ArtifactCallPort(
        task=task,
        policy=policy,
        artifacts=artifacts,
        complete_layer=1 if single_layer else 2,
        pool=pool,
        verifier_owner=verifier_owner,
        revoke_in_prepare=revoke_in_prepare,
        fail_first_worker=fail_first_worker,
        fail_after_side_effect=fail_after_side_effect,
    )
    runtime = OperatorPlacementLeaseRuntime(
        config=OperatorPlacementLeaseConfig.load(
            ROOT / "config" / "phase2" / "operator-placement-lease.json"
        ),
        scheduler=scheduler,
        pool=pool,
        call_port=call_port,
        permission_queue=permission_queue,
        catalog_provider=lambda: selected_catalog,
    )
    return Harness(
        task=task,
        policy=policy,
        catalog=selected_catalog,
        proposal=selected_proposal,
        selector_result=selected_result,
        scheduler=scheduler,
        pool=pool,
        artifacts=artifacts,
        permission_queue=permission_queue,
        recovery_store=recovery_store,
        call_port=call_port,
        runtime=runtime,
        adaptive=AdaptiveDepthRuntime(DeterministicEarlyExitGate(gate_config)),
        eligibility=EligibilityPort(
            builder=builder,
            task=task,
            policy=policy,
            proposal=selected_proposal,
            checkpoint=selected_checkpoint,
            continuity=continuity,
        ),
    )


def execute_harness(
    harness: Harness,
    *,
    early_exit_enabled: bool = True,
):
    return harness.runtime.execute_task(
        task=harness.task,
        policy_input=harness.policy,
        selector_result=harness.selector_result,
        catalog=harness.catalog,
        adaptive_depth=harness.adaptive,
        eligibility_port=harness.eligibility,
        early_exit_enabled=early_exit_enabled,
    )
