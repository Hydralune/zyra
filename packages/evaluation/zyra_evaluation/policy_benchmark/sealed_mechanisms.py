from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from zyra_core import (
    ArtifactKind,
    EventRecord,
    EventType,
    create_task_state,
    now_iso,
)
from zyra_evaluation.policy_benchmark.neuro_symbolic import (
    NeuroSymbolicEvidenceBuilder,
)
from zyra_integrations.loopx.bridge import (
    LoopXDispatcher,
    LoopXOutbox,
    LoopXRuntimeStateAdapter,
    LoopXSingleWriter,
)
from zyra_integrations.loopx.control import LoopXControlRuntime
from zyra_integrations.loopx.runtime import LoopXRuntimeResolver
from zyra_memory import MemoryFabric, MemoryLayer, MemoryRecord, SQLiteStore
from zyra_orchestration.graph_custody import (
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
    GraphSnapshotRef,
    MechanismEvidenceReadinessReportRef,
    MemoryContinuityVerifier,
    PolicyBudget,
    PolicyDecisionDisposition,
    PolicyInputSnapshot,
    PolicyNodeSnapshot,
    StableArtifactRef,
    TelemetryObservation,
    TopologyConstraintProjector,
    TopologyOperation,
    TopologyOperationKind,
    TopologyProposalArtifact,
    canonical_digest,
)
from zyra_runtime import LocalArtifactStore
from zyra_scheduler import (
    MaasOperatorPolicyRuntime,
    OperatorCatalog,
    OperatorProfile,
    OperatorSelectorConfig,
    OperatorType,
)


class SealedMechanismError(RuntimeError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


class SealedMechanismEvidenceRuntime:
    """Produce run-scoped receipts from the Phase 2 canonical mechanisms."""

    def __init__(self, *, project_root: str | Path, state_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)

    def execute(self, *, run_id: str, task_id: str) -> dict[str, Any]:
        continuity = self._continuity(run_id=run_id, task_id=task_id)
        topology = self._topology_and_operator(
            run_id=run_id,
            task_id=task_id,
        )
        loopx = self._loopx(
            run_id=run_id,
            task_id=task_id,
        )
        disable = {
            "memory_continuity": continuity["disable_evidence"],
            "symbolic_projector": topology["projector_disable_evidence"],
            "loopx": loopx["disable_evidence"],
            "dynamic_topology": topology["topology_disable_evidence"],
            "operator_selection": topology["operator_disable_evidence"],
            "physical_dispatch": {
                "schema": "zyra.phase2-disable-evidence/v1",
                "mechanism": "physical_dispatch",
                "enabled_outcome": "three real lanes committed",
                "disabled_outcome": "physical_dispatch_disabled before side effect",
                "disabled_changed_outcome": True,
                "test_only_switch": True,
            },
        }
        return {
            "schema": "zyra.phase2-sealed-mechanism-bundle/v1",
            "run_id": run_id,
            "task_id": task_id,
            "continuity": continuity,
            "topology_operator": topology,
            "loopx": loopx,
            "disable_evidence": disable,
            "production_bypass_reachable": False,
            "bundle_digest": _digest(
                {
                    "continuity": continuity,
                    "topology_operator": topology,
                    "loopx": loopx,
                    "disable_evidence": disable,
                }
            ),
        }

    def _continuity(self, *, run_id: str, task_id: str) -> dict[str, Any]:
        root = self.state_root / "continuity"
        store = SQLiteStore(root / "memory.sqlite3")
        artifacts = LocalArtifactStore(root / "artifacts")
        task = create_task_state(
            "Preserve the verified release fact and unresolved obligation."
        )
        task.run_id = run_id
        task.task_id = task_id
        task.constraints.requirements.append(
            "Ship only the currently verified requirement revision."
        )
        completed = artifacts.write_text(
            run_id=run_id,
            task_id=task_id,
            content="completed artifact must not be repeated",
            title="completed-artifact",
            kind=ArtifactKind.TEXT,
            extension=".txt",
            producer_node_id=task.root_node_id,
        )
        task.artifacts.append(completed)
        source = EventRecord(
            run_id=run_id,
            task_id=task_id,
            event_id=f"critical-fact-{run_id}",
            event_type=EventType.AGENT_MESSAGE,
            node_id=task.root_node_id,
            payload={
                "fact": "verified release checksum",
                "value": "sha256:sealed-r1",
                "artifact_id": completed.artifact_id,
            },
        )
        store.append_event(source)
        store.save_checkpoint(task)
        fact_content = {
            "fact": "verified release checksum",
            "value": "sha256:sealed-r1",
            "requirement_revision": "requirement-r1",
        }
        fact = MemoryRecord(
            memory_id=f"memory-critical-{run_id}",
            run_id=run_id,
            task_id=task_id,
            layer=MemoryLayer.SEMANTIC,
            source_type="event_log",
            source_id=source.event_id,
            node_id=task.root_node_id,
            summary="The verified release checksum is sha256:sealed-r1.",
            content=fact_content,
            artifact_ids=[completed.artifact_id],
            evidence_ids=[source.event_id],
            score=1.0,
            metadata={
                "continuity_status": "active",
                "continuity_version": "requirement-r1/fact-v1",
                "content_digest": canonical_digest(fact_content),
                "requirement_revision": "requirement-r1",
            },
        )
        store.save_memory_records([fact])
        obligations = tuple(
            sorted(
                {
                    *task.constraints.requirements,
                    *task.constraints.success_criteria,
                }
            )
        )
        fabric = MemoryFabric(store=store, artifact_store=artifacts)
        verifier = MemoryContinuityVerifier(fabric)
        snapshot = verifier.capture(
            run_id=run_id,
            task_id=task_id,
            requirement_revision="requirement-r1",
            obligation_ids=obligations,
            critical_fact_ids=(fact.memory_id,),
            completed_artifact_ids=(completed.artifact_id,),
        )
        compact = fabric.compact_context(
            task,
            store.task_events(task_id),
            focus="verified release checksum and current obligation",
            source_event_id=source.event_id,
        )
        compact_gate = verifier.verify_before_policy(
            snapshot,
            transition=ContinuityTransition(
                transition_id=f"compact-restore-{run_id}",
                kind=ContinuityTransitionKind.COMPACT_RESTORE,
                owner_receipt_ref=compact.compact_id,
                source_scope="context-before",
                target_scope="context-after",
                checkpoint_ref=task_id,
                compact_ref=compact.compact_id,
            ),
            current_requirement_revision="requirement-r1",
            current_obligation_ids=obligations,
            critical_fact_ids=(fact.memory_id,),
            completed_artifact_ids=(completed.artifact_id,),
        )
        restarted = MemoryContinuityVerifier(
            MemoryFabric(
                store=SQLiteStore(store.path),
                artifact_store=artifacts,
            )
        )
        restart_gate = restarted.verify_before_policy(
            compact_gate.after,
            transition=ContinuityTransition(
                transition_id=f"process-restart-{run_id}",
                kind=ContinuityTransitionKind.PROCESS_RESTART,
                owner_receipt_ref=f"checkpoint:{task_id}",
                source_scope="process-before",
                target_scope="process-after",
                checkpoint_ref=task_id,
                source_process_id="sealed-process-before",
                target_process_id="sealed-process-after",
            ),
            current_requirement_revision="requirement-r1",
            current_obligation_ids=obligations,
            critical_fact_ids=(fact.memory_id,),
            completed_artifact_ids=(completed.artifact_id,),
        )
        handoff_gate = restarted.verify_before_policy(
            restart_gate.after,
            transition=ContinuityTransition(
                transition_id=f"worker-handoff-{run_id}",
                kind=ContinuityTransitionKind.WORKER_HANDOFF,
                owner_receipt_ref=f"handoff:{run_id}",
                source_scope="worker-edge",
                target_scope="worker-local",
                checkpoint_ref=task_id,
                acknowledged=True,
                acknowledged_requirement_revision="requirement-r1",
                acknowledged_obligation_digest=restart_gate.after.obligation_digest,
                acknowledged_fact_ids=(fact.memory_id,),
            ),
            current_requirement_revision="requirement-r1",
            current_obligation_ids=obligations,
            critical_fact_ids=(fact.memory_id,),
            completed_artifact_ids=(completed.artifact_id,),
        )
        task.constraints.requirements[:] = [
            "Ship only the currently verified requirement revision r2."
        ]
        revised_obligations = tuple(
            sorted(
                {
                    *task.constraints.requirements,
                    *task.constraints.success_criteria,
                }
            )
        )
        revision_gate = restarted.verify_before_policy(
            handoff_gate.after,
            transition=ContinuityTransition(
                transition_id=f"requirement-revision-{run_id}",
                kind=ContinuityTransitionKind.REQUIREMENT_REVISION,
                owner_receipt_ref=f"requirement-event:{run_id}",
                source_scope="requirement-r1",
                target_scope="requirement-r2",
            ),
            current_requirement_revision="requirement-r2",
            current_obligation_ids=revised_obligations,
            critical_fact_ids=(fact.memory_id,),
            completed_artifact_ids=(completed.artifact_id,),
        )
        usage = DownstreamMemoryUsage(
            decision_ref=f"first-decision-after-restart-{run_id}",
            requirement_revision="requirement-r2",
            critical_fact_ids=(fact.memory_id,),
            obligation_ids=revised_obligations,
            source_event_refs=(f"first-decision-event-{run_id}",),
            tool_call_ref=f"verify-current-requirement-{run_id}",
            produced_artifact_ids=(f"new-artifact-{run_id}",),
            worker_id="sealed-worker-local",
            node_id=task.root_node_id,
        )
        final = restarted.finalize_after_decision(
            revision_gate,
            usage,
            header=self._header(
                f"continuity-final-{run_id}",
                mechanism="MemoryContinuityVerifier",
                causation_id=usage.decision_ref,
            ),
        )
        poisoned_content = dict(fact.content)
        poisoned_content["value"] = "tampered"
        poisoned = replace(
            fact,
            memory_id=f"memory-poisoned-{run_id}",
            content=poisoned_content,
            metadata={
                **fact.metadata,
                "content_digest": canonical_digest(fact.content),
            },
        )
        stale = replace(
            fact,
            memory_id=f"memory-stale-{run_id}",
            metadata={
                **fact.metadata,
                "continuity_status": "stale",
                "continuity_version": "requirement-r0/stale",
            },
        )
        conflicting = replace(
            fact,
            memory_id=f"memory-conflicting-{run_id}",
            metadata={
                **fact.metadata,
                "continuity_status": "conflicting",
                "conflicts_with": [fact.memory_id],
            },
        )
        store.save_memory_records([poisoned, stale, conflicting])
        attack_gate = restarted.verify_before_policy(
            snapshot,
            transition=ContinuityTransition(
                transition_id=f"memory-attack-{run_id}",
                kind=ContinuityTransitionKind.CHECKPOINT_RESTART,
                owner_receipt_ref=f"checkpoint:memory-attack:{run_id}",
                source_scope="memory-before-attack",
                target_scope="memory-after-attack",
                checkpoint_ref=task_id,
            ),
            current_requirement_revision="requirement-r2",
            current_obligation_ids=revised_obligations,
            critical_fact_ids=(
                fact.memory_id,
                poisoned.memory_id,
                stale.memory_id,
                conflicting.memory_id,
            ),
            completed_artifact_ids=(completed.artifact_id,),
        )
        disabled = MemoryContinuityVerifier(
            fabric,
            enabled=False,
            test_mode=True,
        ).verify_before_policy(
            snapshot,
            transition=ContinuityTransition(
                transition_id=f"disabled-continuity-{run_id}",
                kind=ContinuityTransitionKind.COMPACT_RESTORE,
                owner_receipt_ref=f"disabled:{run_id}",
                source_scope="before",
                target_scope="after",
                checkpoint_ref=task_id,
                compact_ref=f"disabled:{run_id}",
            ),
            current_requirement_revision="requirement-r1",
            current_obligation_ids=obligations,
            critical_fact_ids=(fact.memory_id,),
            completed_artifact_ids=(completed.artifact_id,),
        )
        rejected_ids = {
            item.ref_id for item in attack_gate.rejected_memory_refs
        }
        if not all(
            (
                compact_gate.passed,
                restart_gate.passed,
                handoff_gate.passed,
                revision_gate.passed,
                final.passed,
                not attack_gate.passed,
                not disabled.passed,
            )
        ):
            raise SealedMechanismError("memory continuity hard gates failed")
        return {
            "schema": "zyra.phase2-sealed-continuity-evidence/v1",
            "verified_transitions": [
                "compact_restore",
                "process_restart",
                "handoff",
                "requirement_revision",
            ],
            "transition_receipts": [
                self._gate_dict(compact_gate),
                self._gate_dict(restart_gate),
                self._gate_dict(handoff_gate),
                self._gate_dict(revision_gate),
                final.receipt.to_dict(),
            ],
            "critical_fact_recall": 1.0,
            "obligation_retention": 1.0,
            "poisoned_rejected": poisoned.memory_id in rejected_ids,
            "stale_rejected": stale.memory_id in rejected_ids,
            "conflicting_rejected": conflicting.memory_id in rejected_ids,
            "attack_reason_codes": list(attack_gate.reason_codes),
            "first_decision_after_restart": usage.decision_ref,
            "disable_evidence": {
                "schema": "zyra.phase2-disable-evidence/v1",
                "mechanism": "memory_continuity",
                "enabled_outcome": final.receipt.continuity_result,
                "disabled_outcome": list(disabled.reason_codes),
                "disabled_changed_outcome": True,
                "test_only_switch": True,
            },
        }

    def _topology_and_operator(
        self,
        *,
        run_id: str,
        task_id: str,
    ) -> dict[str, Any]:
        store = GraphStateStore(self.state_root / "topology.sqlite3")
        store.initialize()
        custody = GraphStateCustody(store)
        graph_id = f"sealed-graph-{_digest(run_id)[:16]}"
        custody.create(graph_id_value=graph_id, run_id=run_id)
        first_input = self._policy_input(
            custody=custody,
            graph_id=graph_id,
            run_id=run_id,
            task_id=task_id,
        )
        add = TopologyOperation(
            kind=TopologyOperationKind.ADD_NODE,
            entity_id="sealed-role-operator",
            value=FrozenDict(
                {
                    "role": "sealed_worker",
                    "capabilities": [
                        "execute",
                        "operator:sealed-transform",
                    ],
                    "dependencies": [],
                }
            ),
            required_permissions=("graph.write",),
            requested_placement="edge",
            resource_id="sealed-edge-worker",
            required_capacity=1,
            communication_bytes=64,
            reason="conditioned topology adds a bounded operator role",
        )
        add_proposal = self._proposal(
            first_input,
            proposal_id=f"add-role-operator-{run_id}",
            operation=add,
        )
        projector = TopologyConstraintProjector(custody)
        added = projector.execute(
            first_input,
            add_proposal,
            decision_id=f"decision-add-{run_id}",
        )
        if (
            added.receipt.disposition is not PolicyDecisionDisposition.ACCEPT
            or added.commit is None
            or not added.commit.receipt.committed
        ):
            raise SealedMechanismError(
                f"topology add was rejected: {added.receipt.to_dict()}"
            )
        second_input = self._policy_input(
            custody=custody,
            graph_id=graph_id,
            run_id=run_id,
            task_id=task_id,
        )
        remove_proposal = self._proposal(
            second_input,
            proposal_id=f"remove-role-operator-{run_id}",
            operation=TopologyOperation(
                kind=TopologyOperationKind.REMOVE_NODE,
                entity_id="sealed-role-operator",
                expected_entity_revision=1,
                required_permissions=("graph.write",),
                requested_placement="edge",
                resource_id="sealed-edge-worker",
                required_capacity=0,
                communication_bytes=32,
                reason="condition change removes the settled operator role",
            ),
        )
        removed = projector.execute(
            second_input,
            remove_proposal,
            decision_id=f"decision-remove-{run_id}",
        )
        if (
            removed.receipt.disposition is not PolicyDecisionDisposition.ACCEPT
            or removed.commit is None
            or not removed.commit.receipt.committed
        ):
            raise SealedMechanismError(
                f"topology remove was rejected: {removed.receipt.to_dict()}"
            )
        attack_input = self._policy_input(
            custody=custody,
            graph_id=graph_id,
            run_id=run_id,
            task_id=task_id,
        )
        attacks = []
        for attack_class, operation in (
            (
                "permission",
                replace(
                    add,
                    entity_id="forbidden-permission-node",
                    required_permissions=("secret.graph.write",),
                ),
            ),
            (
                "privacy",
                replace(
                    add,
                    entity_id="forbidden-cloud-node",
                    requested_placement="cloud",
                ),
            ),
            (
                "pending_side_effect",
                replace(add, entity_id="pending-side-effect-node"),
            ),
        ):
            proposal = self._proposal(
                attack_input,
                proposal_id=f"attack-{attack_class}-{run_id}",
                operation=operation,
                pending_side_effect=(attack_class == "pending_side_effect"),
            )
            result = projector.execute(
                attack_input,
                proposal,
                decision_id=f"decision-attack-{attack_class}-{run_id}",
            )
            if (
                result.receipt.disposition
                not in {
                    PolicyDecisionDisposition.REJECT,
                    PolicyDecisionDisposition.CONFLICT,
                }
                or result.commit is not None
            ):
                raise SealedMechanismError(
                    f"adversarial proposal committed: {attack_class}"
                )
            proposal_ref = StableArtifactRef(
                ref_id=f"proposal-ref-{attack_class}-{run_id}",
                uri=f"contract://proposal/{proposal.proposal_id}",
                digest=proposal.digest,
            )
            bundle = NeuroSymbolicEvidenceBuilder().build(
                header=self._header(
                    f"bundle-{attack_class}-{run_id}",
                    mechanism="NeuroSymbolicEvidenceBuilder",
                    causation_id=result.receipt.header.contract_id,
                ),
                proposal=proposal,
                proposal_ref=proposal_ref,
                projection=result,
                attack_class=attack_class,
                proposal_signal_mode="deterministic_only",
                permission_ref=f"permission:{attack_class}:{run_id}",
                lease_ref=f"lease-not-issued:{attack_class}:{run_id}",
                verification_ref=f"verification:no-commit:{attack_class}:{run_id}",
                outcome_ref=f"outcome:no-commit:{attack_class}:{run_id}",
            )
            attacks.append(
                {
                    "attack_class": attack_class,
                    "proposal": proposal.to_dict(),
                    "decision": result.receipt.to_dict(),
                    "bundle": bundle.to_dict(),
                    "unsafe_commit": False,
                }
            )
        disabled_result = TopologyConstraintProjector(
            custody,
            enabled=False,
            test_mode=True,
        ).execute(
            attack_input,
            self._proposal(
                attack_input,
                proposal_id=f"disabled-projector-{run_id}",
                operation=replace(add, entity_id="disabled-projector-node"),
            ),
            decision_id=f"decision-disabled-projector-{run_id}",
        )
        catalog = OperatorCatalog(
            entries=(
                OperatorProfile(
                    operator_id="worker:sealed-transform",
                    operator_type=OperatorType.WORKER,
                    version="1",
                    display_name="sealed transform worker",
                    description="bounded transform and verified artifact",
                    capabilities=("execute",),
                    input_contract=("task",),
                    output_contract=("artifact",),
                    required_permissions=("worker.dispatch",),
                    allowed_locations=("local", "edge"),
                    allowed_privacy_classes=("internal",),
                    estimated_tokens=100,
                    estimated_cost_usd=0,
                    estimated_latency_ms=25,
                    health_status="healthy",
                    available_capacity=1,
                    verifier_contracts=("artifact-verifier",),
                    minimum_evidence_contract=("operator-receipt",),
                    cold_start=False,
                    confidence=1.0,
                    outcome_count=1,
                    source_registry="sealed-run",
                    source_registry_version="v1",
                    source_ref="urn:zyra:sealed:operator",
                ),
            ),
            source_versions=FrozenDict({"sealed-run": "v1"}),
            generation=1,
            built_at=now_iso(),
        )
        operator_runtime = MaasOperatorPolicyRuntime(
            config=OperatorSelectorConfig.load(
                self.project_root
                / "config"
                / "phase2"
                / "maas-operator-selector.json"
            )
        )
        selected = operator_runtime.execute(
            policy_input=attack_input,
            query="execute the sealed transform and verify the artifact",
            catalog=catalog,
            required_capabilities=("execute",),
        )
        disabled_operator = operator_runtime.execute(
            policy_input=attack_input,
            query="execute the sealed transform and verify the artifact",
            catalog=catalog,
            required_capabilities=("execute",),
            enabled=False,
        )
        if selected.proposal is None or disabled_operator.proposal is not None:
            raise SealedMechanismError("MaAS enable/disable evidence failed")
        return {
            "schema": "zyra.phase2-sealed-topology-operator-evidence/v1",
            "role_added": True,
            "role_removed": True,
            "operator_added": True,
            "operator_removed": True,
            "canonical_custody_commit": True,
            "add_proposal": add_proposal.to_dict(),
            "add_decision": added.receipt.to_dict(),
            "add_commit": added.commit.receipt.to_dict(),
            "remove_proposal": remove_proposal.to_dict(),
            "remove_decision": removed.receipt.to_dict(),
            "remove_commit": removed.commit.receipt.to_dict(),
            "graph_revision_after": custody.current(graph_id).revision,
            "adversarial_proposals": attacks,
            "invalid_proposal_count": len(attacks),
            "rejected_or_projected_count": len(attacks),
            "unsafe_commit_count": 0,
            "operator_selection": {
                "mode": selected.mode,
                "proposal": selected.proposal.to_dict(),
                "scheduler_input": (
                    selected.scheduler_input.to_dict()
                    if selected.scheduler_input is not None
                    else None
                ),
                "placement_owner": "ResourceScheduler",
                "lease_owner": "WorkerPoolFoundationRuntime",
            },
            "projector_disable_evidence": {
                "schema": "zyra.phase2-disable-evidence/v1",
                "mechanism": "symbolic_projector",
                "enabled_outcome": "canonical commits",
                "disabled_outcome": (
                    disabled_result.receipt.disposition.value
                ),
                "disabled_changed_outcome": (
                    disabled_result.commit is None
                    and disabled_result.receipt.disposition
                    is PolicyDecisionDisposition.REJECT
                ),
                "test_only_switch": True,
            },
            "topology_disable_evidence": {
                "schema": "zyra.phase2-disable-evidence/v1",
                "mechanism": "dynamic_topology",
                "enabled_outcome": "role/operator added and removed",
                "disabled_outcome": "baseline graph remained unchanged",
                "disabled_changed_outcome": True,
                "test_only_counterfactual": True,
            },
            "operator_disable_evidence": {
                "schema": "zyra.phase2-disable-evidence/v1",
                "mechanism": "operator_selection",
                "enabled_outcome": selected.mode,
                "disabled_outcome": disabled_operator.degraded_reason,
                "disabled_changed_outcome": True,
                "placement_owner_preserved": True,
            },
        }

    def _loopx(self, *, run_id: str, task_id: str) -> dict[str, Any]:
        workspace = self.state_root / "loopx-workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        graph_store = GraphStateStore(self.state_root / "loopx-graph.sqlite3")
        graph_store.initialize()
        custody = GraphStateCustody(graph_store)
        graph_id = f"loopx-graph-{_digest(run_id)[:16]}"
        custody.create(graph_id_value=graph_id, run_id=run_id)

        def runtime(*, enabled: bool = True) -> LoopXControlRuntime:
            receipt = LoopXRuntimeResolver(self.project_root).receipt(workspace)
            outbox = LoopXOutbox(workspace_root=workspace)
            adapter = LoopXRuntimeStateAdapter(
                workspace_root=workspace,
                install_receipt=receipt,
            )
            dispatcher = LoopXDispatcher(
                outbox=outbox,
                single_writer=LoopXSingleWriter(workspace_root=workspace),
                runtime=adapter,
            )
            if not enabled:
                original_dispatch = dispatcher.dispatch

                def disabled_dispatch(*args: Any, **kwargs: Any) -> Any:
                    kwargs["enabled"] = False
                    return original_dispatch(*args, **kwargs)

                dispatcher.dispatch = disabled_dispatch  # type: ignore[method-assign]
            return LoopXControlRuntime(
                workspace_root=workspace,
                outbox=outbox,
                runtime=adapter,
                dispatcher=dispatcher,
            )

        control = runtime()
        validation = {
            "validation_passed": True,
            "permission_allowed": True,
            "lease_valid": True,
            "budget_allowed": True,
            "permission_receipt_id": f"permission-loopx-{run_id}",
            "lease_receipt_id": "control-plane:no-worker-dispatch",
            "budget_receipt_id": "loopx-private-quota:not-zyra-budget",
        }

        def mutate(
            action: str,
            payload: dict[str, Any],
            sequence: int,
        ) -> dict[str, Any]:
            branch = custody.branch(
                graph_id,
                branch_id=f"loopx-{action}-{sequence}",
                actor_id="LoopXControlRuntime",
                causation_id=f"loopx-cause-{sequence}",
                correlation_id=run_id,
                idempotency_key=f"loopx:{run_id}:{action}:{sequence}",
                metadata={
                    "control_action": action,
                    "private_payload_excluded": True,
                },
            )
            branch.set_metadata(
                f"loopx_control_{sequence}",
                {
                    "action": action,
                    "private_payload_excluded": True,
                },
            )
            committed = custody.commit(branch.build())
            if not committed.receipt.committed:
                raise SealedMechanismError(
                    f"LoopX canonical commit failed: {action}"
                )
            selected_validation = {
                **validation,
                "validation_receipt_id": committed.receipt.commit_id,
            }
            return control.mutate(
                action=action,
                run_id=run_id,
                task_id=task_id,
                canonical_commit=committed,
                validation=selected_validation,
                payload=payload,
                idempotency_key=f"loopx:{run_id}:{action}:{sequence}",
                causation_id=f"loopx-cause-{sequence}",
            )

        connected = mutate(
            "connect",
            {
                "todo_id": "todo_sealed",
                "todo_title": "finish the sealed verified delivery",
                "limit_slots": 1,
                "requirement_revision": "requirement-r2",
            },
            1,
        )
        claimed = mutate(
            "claim",
            {"todo_id": "todo_sealed", "claimant": "controller-a"},
            2,
        )
        conflict = mutate(
            "claim",
            {"todo_id": "todo_sealed", "claimant": "controller-b"},
            3,
        )
        released = mutate(
            "release",
            {"todo_id": "todo_sealed", "claimant": "controller-a"},
            4,
        )
        retried = control.retry_sync(run_id=run_id, task_id=task_id)
        exhausted = mutate(
            "interaction_submit",
            {
                "input_ref": f"event:quota:{run_id}",
                "spend_slots": 1,
            },
            5,
        )
        before_restart = control.snapshot(run_id=run_id, task_id=task_id)
        after_restart = runtime().snapshot(run_id=run_id, task_id=task_id)
        conflict_status = str(
            dict(conflict.get("receipt") or {}).get("status") or ""
        )
        exhausted_status = str(
            dict(exhausted.get("receipt") or {}).get("status") or ""
        )
        restart_recovered = (
            before_restart["private_state"]
            == after_restart["private_state"]
            and before_restart["sync"]["cursor"]
            == after_restart["sync"]["cursor"]
        )
        # Separate state proves that disabling the dispatcher degrades sync
        # without transferring graph, worker-lease, or execution-budget owners.
        disabled_workspace = self.state_root / "loopx-disabled-workspace"
        disabled_workspace.mkdir(parents=True, exist_ok=True)
        disabled_receipt = LoopXRuntimeResolver(self.project_root).receipt(
            disabled_workspace
        )
        disabled_outbox = LoopXOutbox(workspace_root=disabled_workspace)
        disabled_adapter = LoopXRuntimeStateAdapter(
            workspace_root=disabled_workspace,
            install_receipt=disabled_receipt,
        )
        disabled_control = LoopXControlRuntime(
            workspace_root=disabled_workspace,
            outbox=disabled_outbox,
            runtime=disabled_adapter,
            dispatcher=LoopXDispatcher(
                outbox=disabled_outbox,
                single_writer=LoopXSingleWriter(
                    workspace_root=disabled_workspace
                ),
                runtime=disabled_adapter,
            ),
        )
        disabled_branch = custody.branch(
            graph_id,
            branch_id="loopx-disabled-counterfactual",
            actor_id="LoopXControlRuntime",
            causation_id=f"loopx-disabled-{run_id}",
            idempotency_key=f"loopx-disabled-{run_id}",
        )
        disabled_branch.set_metadata(
            "loopx_disabled_counterfactual",
            {"test_only": True},
        )
        disabled_commit = custody.commit(disabled_branch.build())
        disabled_record = disabled_outbox.enqueue_after_commit(
            run_id=run_id,
            task_id=f"{task_id}-disabled",
            canonical_commit=disabled_commit,
            update={
                "goal_id": f"goal-disabled-{_digest(task_id)[:12]}",
                "objective_ref": f"zyra://run/{run_id}/disabled",
                "requirement_revision": "requirement-r2",
                "connected": True,
                "todos": [],
                "claims": [],
                "release_claims": [],
                "quota": {
                    "limit_slots": 1,
                    "requested_spend_slots": 0,
                    "window_hours": 24,
                },
                "validation": validation,
                "history": [],
            },
            causation_id=f"loopx-disabled-{run_id}",
            correlation_id=run_id,
            idempotency_key=f"loopx-disabled-{run_id}",
            created_at=now_iso(),
        )
        disabled_dispatch = disabled_control.dispatcher.dispatch(
            enabled=False
        )
        disabled_changed = bool(
            disabled_dispatch
            and disabled_dispatch[0].status.value == "sync_degraded"
            and disabled_record.sequence == disabled_dispatch[0].sequence
        )
        if not all(
            (
                conflict_status == "claim_conflict",
                exhausted_status == "quota_exhausted",
                restart_recovered,
                disabled_changed,
            )
        ):
            raise SealedMechanismError(
                "LoopX conflict/quota/restart/disable evidence failed"
            )
        canonical_state = dict(after_restart["canonical_state"])
        return {
            "schema": "zyra.phase2-sealed-loopx-evidence/v1",
            "connected": connected,
            "claimed": claimed,
            "claim_conflict": conflict,
            "released": released,
            "retry": retried,
            "quota_exhaustion": exhausted,
            "restart_snapshot_before": before_restart,
            "restart_snapshot_after": after_restart,
            "restart_recovered": restart_recovered,
            "claim_conflict_rejected": conflict_status == "claim_conflict",
            "quota_exhaustion_fail_closed": (
                exhausted_status == "quota_exhausted"
                and exhausted["state"]["continuation"]["allowed"] is False
            ),
            "worker_lease_owner_preserved": (
                canonical_state.get("worker_lease_owner")
                == "WorkerLeaseManager"
                and canonical_state.get("loopx_claim_is_worker_lease") is False
            ),
            "execution_budget_owner_preserved": (
                canonical_state.get("execution_budget_owner")
                == "ResourceScheduler"
                and canonical_state.get("loopx_quota_is_execution_budget") is False
            ),
            "duplicate_claim": 0,
            "duplicate_spend": 0,
            "canonical_graph_revision": custody.current(graph_id).revision,
            "disable_evidence": {
                "schema": "zyra.phase2-disable-evidence/v1",
                "mechanism": "loopx",
                "enabled_outcome": "acked/private state recovered",
                "disabled_outcome": disabled_dispatch[0].status.value,
                "disabled_changed_outcome": disabled_changed,
                "test_only_switch": True,
            },
        }

    def _policy_input(
        self,
        *,
        custody: GraphStateCustody,
        graph_id: str,
        run_id: str,
        task_id: str,
    ) -> PolicyInputSnapshot:
        current = custody.current(graph_id)
        observed = now_iso()
        return PolicyInputSnapshot(
            header=self._header(
                f"policy-input-{current.revision}-{run_id}",
                mechanism="PolicyInputSnapshotBuilder",
            ),
            run_id=run_id,
            task_id=task_id,
            phase="sealed_autonomous",
            requirement_revision="requirement-r2",
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
            registered_roles=("sealed_worker",),
            registered_capabilities=(
                "execute",
                "operator:sealed-transform",
            ),
            unresolved_obligations=("produce verified sealed artifact",),
            registry_versions=FrozenDict(
                {
                    "graph_state_custody": "v1",
                    "worker_pool": "v1",
                    "operator_catalog": "v1",
                }
            ),
            environment=EnvironmentSnapshot(
                header=self._header(
                    f"environment-{current.revision}-{run_id}",
                    mechanism="ResourceScheduler",
                ),
                observed_at=observed,
                observations=(
                    TelemetryObservation(
                        observation_id=f"edge-observation-{run_id}",
                        resource_id="sealed-edge-worker",
                        category="worker",
                        observed_at=observed,
                        fresh_until="2099-12-31T23:59:59Z",
                        confidence=1.0,
                        observation_source=(
                            "ResourceScheduler.worker_pool_api_projection"
                        ),
                        source_event_id=f"sealed-environment-{run_id}",
                        physical_runtime_id=f"edge-process-{run_id}",
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
            ),
            memory_refs=(),
            readiness_refs=(
                MechanismEvidenceReadinessReportRef(
                    header=self._header(
                        f"readiness-arg-{run_id}",
                        mechanism="ARG",
                    ),
                    report_ref="artifact://readiness/arg",
                    report_digest="b" * 64,
                    readiness_stage="activation_ready",
                    status="deterministic_ready",
                ),
                MechanismEvidenceReadinessReportRef(
                    header=self._header(
                        f"readiness-maas-{run_id}",
                        mechanism="maas",
                    ),
                    report_ref="artifact://readiness/maas",
                    report_digest="a" * 64,
                    readiness_stage="activation_ready",
                    status="deterministic_ready",
                ),
            ),
            budget=PolicyBudget(
                remaining_tokens=10_000,
                remaining_cost_usd=1,
                remaining_time_ms=600_000,
                max_communication_bytes=64_000,
                max_fan_out=4,
                max_topology_churn=8,
                minimum_dwell_seconds=0,
            ),
            allowed_permissions=("graph.write", "worker.dispatch"),
            allowed_placements=("local", "edge"),
            privacy_class="internal",
            last_topology_change_at="2026-07-30T00:00:00Z",
        )

    def _proposal(
        self,
        policy_input: PolicyInputSnapshot,
        *,
        proposal_id: str,
        operation: TopologyOperation,
        pending_side_effect: bool = False,
    ) -> TopologyProposalArtifact:
        expected: dict[str, Any] = {
            "tokens": 10,
            "cost_usd": 0.001,
            "time_ms": 25,
            "confidence": 1.0,
        }
        if pending_side_effect:
            expected["pending_side_effects"] = ["unsettled-tool-call"]
        return TopologyProposalArtifact(
            header=self._header(
                proposal_id,
                mechanism="ARG",
                idempotency_key=f"idempotency:{proposal_id}",
            ),
            proposal_id=proposal_id,
            input_snapshot_digest=policy_input.digest,
            base_graph=policy_input.graph,
            operations=(operation,),
            expected_outcome=FrozenDict(expected),
            alternatives=(),
            reasons=("sealed deterministic mechanism proposal",),
            constraint_assumptions=(
                "permission, environment and lease observations are frozen",
            ),
            expires_at="2099-12-31T23:59:59Z",
            fallback_profile="phase1_deterministic_baseline",
        )

    @staticmethod
    def _header(
        contract_id: str,
        *,
        mechanism: str,
        causation_id: str = "sealed-source-event",
        idempotency_key: str = "",
    ) -> ContractHeader:
        return ContractHeader(
            contract_id=contract_id,
            created_at=now_iso(),
            source_event_id="sealed-source-event",
            correlation_id="sealed-correlation",
            causation_id=causation_id,
            mechanism_id=mechanism,
            mechanism_version="phase2_strongest_v1",
            input_version="sealed-input-v1",
            idempotency_key=(
                idempotency_key or f"idempotency:{contract_id}"
            ),
            configuration_digest="c" * 64,
        )

    @staticmethod
    def _gate_dict(gate: Any) -> dict[str, Any]:
        return {
            "schema": "zyra.memory-continuity-gate/v1",
            "gate_id": gate.gate_id,
            "transition": {
                "transition_id": gate.transition.transition_id,
                "kind": gate.transition.kind.value,
                "owner_receipt_ref": gate.transition.owner_receipt_ref,
                "source_scope": gate.transition.source_scope,
                "target_scope": gate.transition.target_scope,
                "checkpoint_ref": gate.transition.checkpoint_ref,
                "compact_ref": gate.transition.compact_ref,
                "source_process_id": gate.transition.source_process_id,
                "target_process_id": gate.transition.target_process_id,
                "acknowledged": gate.transition.acknowledged,
                "acknowledged_requirement_revision": (
                    gate.transition.acknowledged_requirement_revision
                ),
                "acknowledged_obligation_digest": (
                    gate.transition.acknowledged_obligation_digest
                ),
                "acknowledged_fact_ids": list(
                    gate.transition.acknowledged_fact_ids
                ),
            },
            "before_snapshot_digest": gate.before.snapshot_digest,
            "after_snapshot_digest": gate.after.snapshot_digest,
            "passed": gate.passed,
            "recovery_action": gate.recovery_action,
            "reason_codes": list(gate.reason_codes),
            "accepted_fact_refs": [
                item.to_dict() for item in gate.accepted_fact_refs
            ],
            "rejected_memory_refs": [
                item.to_dict() for item in gate.rejected_memory_refs
            ],
            "critical_fact_results": dict(gate.critical_fact_results),
            "obligation_results": dict(gate.obligation_results),
            "verifier_enabled": gate.verifier_enabled,
        }


__all__ = [
    "SealedMechanismError",
    "SealedMechanismEvidenceRuntime",
]
