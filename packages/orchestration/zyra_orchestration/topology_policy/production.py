from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from zyra_core import (
    ArtifactKind,
    EventRecord,
    EventType,
    PlanNode,
    PlanNodeStatus,
    TaskState,
    now_iso,
    to_jsonable,
)
from zyra_scheduler.operator_policy import (
    AdaptiveDepthCostReceipt,
    AdaptiveDepthRuntime,
    CanonicalExitSnapshotBuilder,
    DeterministicEarlyExitGate,
    EarlyExitGateConfig,
    LayerExecutionReceipt,
    MaasOperatorPolicyRuntime,
    OperatorCandidateSet,
    OperatorCatalogBuilder,
    OperatorLayerProposal,
    OperatorLeaseExecutionContext,
    build_operator_execution_decision,
)
from zyra_scheduler.worker_pool import ExecutionOutcome
from zyra_scheduler.pool import default_worker_manifests
from zyra_scheduler.recovery_runtime import (
    CheckpointCommitRequest,
    CheckpointCommitRuntime,
)
from zyra_scheduler.recovery_runtime.contracts import RecoveryRefs

from .arg import ARGRoleCatalogBuilder
from .contracts import (
    ContractHeader,
    EnvironmentSnapshot,
    FrozenDict,
    MechanismEvidenceReadinessReportRef,
    NeuroSymbolicEvidenceBundle,
    PolicyDecisionReceipt,
    PolicyBudget,
    StableArtifactRef,
    TelemetryObservation,
    PhysicalDispatchReceipt,
    TopologyProposalArtifact,
    canonical_digest,
)
from .evidence import PolicyEvidencePublisher
from .continuity import (
    ContinuityTransition,
    ContinuityTransitionKind,
    DownstreamMemoryUsage,
    MemoryContinuityVerifier,
)
from .condition import CARDEdgeHysteresisState, CARDReplacementCandidate
from .default_policy import DefaultTopologyPolicy, DefaultTopologyPolicyRequest
from .pruning import (
    CommunicationBudget,
    CommunicationEdgeType,
    CommunicationOutcomeObservation,
)
from .snapshot import EnvironmentSnapshotBuilder


class Phase2ProductionPolicyError(RuntimeError):
    """The active strongest profile cannot be composed from canonical owners."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "phase2_production_policy_error",
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.metadata = dict(metadata or {})


@dataclass(frozen=True, slots=True)
class _PhysicalWorkerRun:
    """Task-graph projection of one canonical physical operator call."""

    worker_result: Any
    event_records: list[EventRecord]


@dataclass(slots=True)
class _ProductionEligibilityPort:
    builder: CanonicalExitSnapshotBuilder
    task: TaskState
    policy_input: Any
    proposal: Any
    checkpoint: Any
    continuity_receipt: Any
    required_artifact_ids: tuple[str, ...]

    def capture(
        self,
        *,
        remaining_candidates: Sequence[Any],
        minimum_operator_refs: Sequence[str],
        minimum_verification_refs: Sequence[str],
    ) -> Any:
        return self.builder.capture(
            task=self.task,
            policy_input=self.policy_input,
            proposal=self.proposal,
            checkpoint=self.checkpoint,
            continuity_receipt=self.continuity_receipt,
            required_artifact_ids=self.required_artifact_ids,
            minimum_operator_refs=minimum_operator_refs,
            minimum_verification_refs=minimum_verification_refs,
            remaining_candidates=remaining_candidates,
        )


class Phase2StrongestProductionBridge:
    """Production composition root for topology proposals and MaAS candidates.

    The bridge owns no graph, memory, scheduler, permission, lease, or task
    state. It snapshots the existing owners, asks the deterministic mechanisms
    for proposals, and hands the typed candidate set to ResourceScheduler.
    """

    def __init__(
        self,
        repository_root: str | Path,
        *,
        worker_pool_api: Any,
        memory_fabric: Any,
        resource_scheduler: Any,
        admit_event: Callable[[EventRecord], None] | None = None,
        policy_evidence_event_sink: (
            Callable[[EventRecord], None] | None
        ) = None,
        communication_outcome_provider: (
            Callable[[TaskState], Sequence[Mapping[str, Any]]] | None
        ) = None,
        communication_outcome_recorder: (
            Callable[
                [TaskState, Sequence[Any], EventRecord | None],
                Sequence[Mapping[str, Any]],
            ]
            | None
        ) = None,
        permission_decision_provider: (
            Callable[
                [TaskState, Sequence[str], EventRecord | None],
                Mapping[str, Any],
            ]
            | None
        ) = None,
        artifact_store: Any | None = None,
        permission_queue_provider: Callable[[TaskState], Any] | None = None,
        recovery_store: Any | None = None,
        final_verifier_owner: Any | None = None,
        physical_dispatch_factory: (
            Callable[[TaskState, Mapping[str, Any], Mapping[str, Any]], Any]
            | None
        ) = None,
        early_exit_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.worker_pool_api = worker_pool_api
        self.memory_fabric = memory_fabric
        self.resource_scheduler = resource_scheduler
        self.admit_event = admit_event or (lambda event: None)
        self.communication_outcome_provider = (
            communication_outcome_provider or (lambda state: ())
        )
        self.communication_outcome_recorder = communication_outcome_recorder
        self.permission_decision_provider = permission_decision_provider
        self.artifact_store = artifact_store
        self.evidence_publisher = (
            PolicyEvidencePublisher(
                artifact_store,
                admit_event=policy_evidence_event_sink,
            )
            if artifact_store is not None
            and policy_evidence_event_sink is not None
            else None
        )
        self.permission_queue_provider = permission_queue_provider
        self.recovery_store = recovery_store
        self.final_verifier_owner = final_verifier_owner
        self.physical_dispatch_factory = physical_dispatch_factory
        self.early_exit_enabled = early_exit_enabled or (lambda: True)
        self.fail_closed = True
        self._route_contexts: dict[tuple[str, str], dict[str, Any]] = {}
        self.readiness_path = (
            self.repository_root
            / "docs"
            / "release"
            / "phase2"
            / "activation-readiness.json"
        )
        baseline = lambda frozen: {
            "profile_id": "phase1_deterministic_baseline",
            "input_digest": canonical_digest(frozen),
            "route_owner": "ResourceScheduler",
        }
        self.topology_policy = DefaultTopologyPolicy.from_repository(
            self.repository_root,
            custody=self.worker_pool_api.graph_custody,
            baseline_executor=baseline,
            baseline_reference=baseline,
            owner_probe=lambda: {
                "graph_owner": "GraphStateCustody",
                "placement_owner": "ResourceScheduler",
                "lease_owner": "WorkerPoolFoundationRuntime",
            },
            evidence_publisher=self.evidence_publisher,
            admit_event=self.admit_event,
        )
        self.operator_policy = MaasOperatorPolicyRuntime.from_repository(
            self.repository_root,
            report_path=self.readiness_path,
            admit_event=self.admit_event,
        )
        self.early_exit_config = EarlyExitGateConfig.load(
            self.repository_root / "config" / "phase2" / "maas-early-exit.json"
        )
        self.adaptive_depth = AdaptiveDepthRuntime(
            DeterministicEarlyExitGate(self.early_exit_config),
            admit_event=self.admit_event,
        )

    def __call__(
        self,
        state: TaskState,
        node: PlanNode | None,
        cause_event: EventRecord | None,
    ) -> Mapping[str, Any]:
        self.worker_pool_api.ensure_default_local_worker()
        loopx_pre_control = self._loopx_pre_control_input(state)
        graph_id = self.worker_pool_api.ensure_task_graph(state)
        current = self.worker_pool_api.graph_custody.current(graph_id)
        continuation = self._adaptive_depth_continuation_projection(
            state=state,
            source_event_id=(
                cause_event.event_id
                if cause_event is not None
                else f"task-route:{state.run_id}:{state.task_id}"
            ),
        )
        if continuation is not None:
            return continuation
        source_event_id = (
            cause_event.event_id
            if cause_event is not None
            else f"task-route:{state.run_id}:{state.task_id}"
        )
        projection = self.worker_pool_api.pool.api_projection()
        permission_receipt = self._permission_receipt(
            state=state,
            requested=("graph.write", "worker.dispatch"),
            cause_event=cause_event,
        )
        allowed_permissions = tuple(
            str(item)
            for item in permission_receipt.get("allowed_permissions") or ()
        )
        environment = self._environment(
            state=state,
            projection=projection,
            source_event_id=source_event_id,
        )
        role_manifests, environment = self._logical_role_projection(
            environment
        )
        role_catalog = ARGRoleCatalogBuilder().build(
            worker_manifests=role_manifests,
            environment=environment,
            source_versions={
                "worker_pool_revision": int(projection.get("revision") or 0),
            },
        )
        continuity, requirement_revision = self._continuity_gate(
            state=state,
            graph=current,
            source_event_id=source_event_id,
        )
        readiness_refs, readiness_digest = self._readiness_refs(
            source_event_id=source_event_id,
        )
        policy_input = self._policy_input(
            state=state,
            graph=current,
            environment=environment,
            role_catalog=role_catalog,
            continuity=continuity,
            requirement_revision=requirement_revision,
            readiness_refs=readiness_refs,
            source_event_id=source_event_id,
            allowed_permissions=allowed_permissions,
            loopx_pre_control=loopx_pre_control,
        )
        loopx_consumption = self._loopx_consumption_projection(
            loopx_pre_control,
            topology_policy_input_digest=policy_input.digest,
        )
        preview_arg = self.topology_policy.arg_runtime.execute(
            policy_input=policy_input,
            task_summary=self._task_summary(state, node),
            current_graph=current,
            role_catalog=role_catalog,
            publish=False,
        )
        replacement_candidates, hysteresis_state = self._card_condition_inputs(
            preview_arg=preview_arg,
            cause_event=cause_event,
        )
        preview_card = (
            self.topology_policy.card_runtime.execute(
                policy_input=policy_input,
                arg_base=preview_arg.proposal,
                current_graph=current,
                replacement_candidates=replacement_candidates,
                hysteresis_state=hysteresis_state,
                publish=False,
            )
            if preview_arg.proposal is not None
            else None
        )
        preview_candidates = (
            self.topology_policy.pruning_runtime.communication_candidates(
                card_result=preview_card,
                upstream_proposal=(
                    preview_card.correction_proposal
                    or preview_arg.proposal
                ),
            )
            if preview_card is not None
            and not preview_card.degraded
            and preview_arg.proposal is not None
            else ()
        )
        communication_observations = self._communication_observations(
            state=state,
            candidates=preview_candidates,
            completed_before=policy_input.header.created_at,
            maximum_age_seconds=(
                self.topology_policy.pruning_runtime.config
                .maximum_outcome_age_seconds
                if self.topology_policy.pruning_runtime.config is not None
                else 0
            ),
        )
        recovery_dispatch = any(
            isinstance(item, Mapping)
            and str(item.get("phase") or "") == "prepared"
            for item in (
                state.metadata.get("recovery_continuation_fences") or {}
            ).values()
        )
        topology = self.topology_policy.execute(
            DefaultTopologyPolicyRequest(
                policy_input=policy_input,
                current_graph=current,
                task_summary=self._task_summary(state, node),
                role_catalog=role_catalog,
                communication_observations=communication_observations,
                protections=(),
                communication_budget=CommunicationBudget(
                    max_delivered_messages=policy_input.budget.max_fan_out,
                    max_delivered_bytes=policy_input.budget.max_communication_bytes,
                    max_delivered_tokens=policy_input.budget.remaining_tokens,
                    max_cost_usd=policy_input.budget.remaining_cost_usd,
                ),
                mechanism_epoch=(
                    f"epoch:{requirement_revision}:graph:{current.revision}:"
                    f"{current.signature[:16]}:candidates:"
                    f"{canonical_digest([item.to_dict() for item in preview_candidates])[:16]}"
                ),
                trigger_kind="recovery" if recovery_dispatch else "task_ready",
                recovery_causal_refs=(
                    (source_event_id,)
                    if cause_event is not None
                    else ()
                ),
                replacement_candidates=replacement_candidates,
                hysteresis_state=hysteresis_state,
            )
        )
        result = topology.to_dict()
        topology_projection = result.get("topology_result")
        if isinstance(topology_projection, dict):
            composition_projection = topology_projection.get("composition")
            if isinstance(composition_projection, dict):
                composition_projection["policy_input_digest"] = (
                    policy_input.digest
                )
        topology_policy_artifact_refs = {
            str(
                item.artifact.metadata.get("policy_contract_kind") or ""
            ): item.artifact_ref.to_dict()
            for item in (
                topology.topology_result.published_evidence
                if topology.topology_result is not None
                else ()
            )
        }
        if topology.used_baseline or not topology.committed:
            delivered_for_next_window = ()
            if (
                preview_candidates
                and not self._communication_coverage_complete(
                    candidates=preview_candidates,
                    observations=communication_observations,
                )
                and self.communication_outcome_recorder is not None
            ):
                delivered_for_next_window = tuple(
                    self.communication_outcome_recorder(
                        state,
                        preview_candidates,
                        cause_event,
                    )
                )
            return {
                **result,
                "operator_selection": {
                    "mode": "baseline",
                    "degraded": True,
                    "degraded_reason": "topology_strongest_not_committed",
                },
                "operator_candidate_set": None,
                "readiness_report_digest": readiness_digest,
                "permission_receipt": dict(permission_receipt),
                "loopx_pre_control": loopx_consumption,
                "communication_candidate_edges": [
                    item.to_dict() for item in preview_candidates
                ],
                "communication_outcome_count": len(
                    communication_observations
                ),
                "communication_outcome_coverage_complete": (
                    self._communication_coverage_complete(
                        candidates=preview_candidates,
                        observations=communication_observations,
                    )
                ),
                "condition_preview": (
                    preview_card.to_dict()
                    if preview_card is not None
                    else None
                ),
                "reroute_required": bool(delivered_for_next_window),
                "reroute_reason": (
                    "prior_actual_communication_window_committed"
                    if delivered_for_next_window
                    else ""
                ),
            }

        topology_ref = self.worker_pool_api.topology.version_ref(
            graph_id
        ).to_dict()
        state.metadata["dynamic_graph_ref"] = topology_ref
        committed_graph = self.worker_pool_api.graph_custody.current(graph_id)
        operator_input = self._policy_input(
            state=state,
            graph=committed_graph,
            environment=environment,
            role_catalog=role_catalog,
            continuity=continuity,
            requirement_revision=requirement_revision,
            readiness_refs=readiness_refs,
            source_event_id=source_event_id,
            allowed_permissions=allowed_permissions,
            loopx_pre_control=loopx_pre_control,
        )
        loopx_consumption = {
            **loopx_consumption,
            "operator_policy_input_digest": operator_input.digest,
        }
        scheduler_health = self.resource_scheduler.worker_pool.health_snapshot(
            state=state,
        )
        physical_worker_ids = {
            str(item.get("worker_id") or item.get("id") or "")
            for item in projection.get("workers") or ()
            if isinstance(item, Mapping)
        }
        executable_manifests = tuple(
            item
            for item in self.resource_scheduler.worker_pool.manifests()
            if item.worker_id in physical_worker_ids
        )
        recovery_worker_route = state.metadata.get("recovery_worker_route")
        if isinstance(recovery_worker_route, Mapping):
            preferred_worker_id = str(
                recovery_worker_route.get("preferred_worker_id") or ""
            )
            avoided_worker_ids = {
                str(item)
                for item in recovery_worker_route.get("avoided_worker_ids") or ()
                if str(item)
            }
            if preferred_worker_id:
                executable_manifests = tuple(
                    item
                    for item in executable_manifests
                    if item.worker_id == preferred_worker_id
                    and item.worker_id not in avoided_worker_ids
                )
                if not executable_manifests:
                    raise Phase2ProductionPolicyError(
                        "canonical recovery worker route has no registered "
                        f"physical manifest: {preferred_worker_id}"
                    )
            elif avoided_worker_ids:
                executable_manifests = tuple(
                    item
                    for item in executable_manifests
                    if item.worker_id not in avoided_worker_ids
                )
        if not executable_manifests:
            raise Phase2ProductionPolicyError(
                "no ResourceScheduler manifest is backed by a registered physical worker"
            )
        operator_catalog = OperatorCatalogBuilder().build(
            worker_manifests=executable_manifests,
            worker_health=scheduler_health,
            built_at=now_iso(),
        )
        selected = self.operator_policy.execute(
            policy_input=operator_input,
            query=self._task_summary(state, node),
            catalog=operator_catalog,
        )
        if selected.proposal is None or selected.scheduler_input is None:
            return {
                **result,
                "operator_selection": selected.to_dict(),
                "operator_candidate_set": None,
                "readiness_report_digest": readiness_digest,
                "permission_receipt": dict(permission_receipt),
                "loopx_pre_control": loopx_consumption,
            }
        executed_refs = {
            str(item)
            for item in state.metadata.get("phase2_executed_operator_refs") or ()
            if str(item)
        }
        proposal = selected.proposal
        if executed_refs:
            remaining_layers = tuple(
                replace(
                    layer,
                    candidates=tuple(
                        candidate
                        for candidate in layer.candidates
                        if f"{candidate.operator_id}@{candidate.version}"
                        not in executed_refs
                    ),
                )
                for layer in proposal.layers
            )
            remaining_layers = tuple(
                replace(layer, layer_index=index)
                for index, layer in enumerate(
                    (item for item in remaining_layers if item.candidates),
                    start=1,
                )
            )
            if not remaining_layers:
                raise Phase2ProductionPolicyError(
                    "adaptive-depth continuation has no unexecuted MaAS candidate"
                )
            proposal = replace(
                proposal,
                layers=remaining_layers,
                expected_depth=len(remaining_layers),
                expected_breadth=max(
                    len(item.candidates) for item in remaining_layers
                ),
                reasons=(
                    *proposal.reasons,
                    "excluded canonical operator refs already executed",
                ),
            )
        candidate_set = OperatorCandidateSet.build(
            policy_input=operator_input,
            proposal=proposal,
            catalog=operator_catalog,
            integration_mechanism_version="operator_placement_lease_v1",
            mechanism_receipt_ref=selected.readiness.report_digest,
            mode=selected.mode,
        )
        state.metadata["phase2_final_verifier_scope"] = {
            "requirement_revision": operator_input.requirement_revision,
            "expected_obligation_ids": list(
                operator_input.unresolved_obligations
            ),
            "proposal_id": proposal.proposal_id,
            "proposal_digest": proposal.digest,
        }
        route_context = {
            "policy_input": operator_input,
            "proposal": proposal,
            "full_proposal": proposal,
            "continuity_gate": continuity,
            "requirement_revision": requirement_revision,
            "source_event_id": source_event_id,
            "candidate_set": candidate_set,
            "selected_operator_refs": (),
            "preexisting_artifact_ids": tuple(
                item.artifact_id for item in state.artifacts if item.artifact_id
            ),
            "prior_executed_operator_refs": tuple(sorted(executed_refs)),
            "operator_selection": selected.to_dict(),
            "topology_projection": dict(result),
            "topology_policy_artifact_refs": topology_policy_artifact_refs,
            "permission_receipt": dict(permission_receipt),
            "loopx_pre_control": loopx_consumption,
            "readiness_report_digest": readiness_digest,
        }
        self._route_contexts[(state.run_id, state.task_id)] = route_context
        return {
            **result,
            "operator_selection": selected.to_dict(),
            "operator_candidate_set": candidate_set.to_dict(),
            "readiness_report_digest": readiness_digest,
            "permission_receipt": dict(permission_receipt),
            "loopx_pre_control": loopx_consumption,
            "owner_boundary": {
                "graph": "GraphStateCustody",
                "placement": "ResourceScheduler",
                "lease": "WorkerPoolFoundationRuntime",
            },
            "communication_candidate_edges": [
                item.to_dict() for item in preview_candidates
            ],
            "communication_outcome_count": len(communication_observations),
            "condition_preview": (
                preview_card.to_dict()
                if preview_card is not None
                else None
            ),
        }

    def _adaptive_depth_continuation_projection(
        self,
        *,
        state: TaskState,
        source_event_id: str,
    ) -> Mapping[str, Any] | None:
        executed_refs = {
            str(item)
            for item in state.metadata.get("phase2_executed_operator_refs") or ()
            if str(item)
        }
        context = self._route_contexts.get((state.run_id, state.task_id))
        if not executed_refs or context is None:
            return None
        full_proposal = context.get("full_proposal")
        original_candidate_set = context.get("candidate_set")
        if full_proposal is None or original_candidate_set is None:
            raise Phase2ProductionPolicyError(
                "adaptive-depth continuation lost its canonical proposal"
            )
        remaining_layers = tuple(
            replace(
                layer,
                candidates=tuple(
                    candidate
                    for candidate in layer.candidates
                    if f"{candidate.operator_id}@{candidate.version}"
                    not in executed_refs
                ),
            )
            for layer in full_proposal.layers
        )
        remaining_layers = tuple(
            item for item in remaining_layers if item.candidates
        )
        if not remaining_layers:
            raise Phase2ProductionPolicyError(
                "adaptive-depth continuation has no unexecuted MaAS candidate"
            )
        proposal = replace(
            full_proposal,
            layers=remaining_layers,
            expected_depth=len(remaining_layers),
            expected_breadth=max(len(item.candidates) for item in remaining_layers),
            reasons=(
                *full_proposal.reasons,
                "continued from the canonical executed operator prefix",
            ),
        )
        remaining_candidates = tuple(
            item
            for item in original_candidate_set.candidates
            if f"{item.operator_id}@{item.version}" not in executed_refs
        )
        candidate_set = replace(
            original_candidate_set,
            proposal_digest=proposal.digest,
            candidates=remaining_candidates,
            expected_depth=len(remaining_layers),
            expected_breadth=max(len(item.candidates) for item in remaining_layers),
        )
        context.update(
            {
                "proposal": proposal,
                "candidate_set": candidate_set,
                "selected_operator_refs": (),
                "prior_executed_operator_refs": tuple(sorted(executed_refs)),
                "preexisting_artifact_ids": tuple(
                    item.artifact_id for item in state.artifacts if item.artifact_id
                ),
                "source_event_id": source_event_id,
            }
        )
        context.pop("physical_execution_receipt", None)
        state.metadata["phase2_final_verifier_scope"] = {
            "requirement_revision": proposal.requirement_revision,
            "expected_obligation_ids": list(
                context["policy_input"].unresolved_obligations
            ),
            "proposal_id": proposal.proposal_id,
            "proposal_digest": proposal.digest,
        }
        return {
            **dict(context.get("topology_projection") or {}),
            "operator_selection": dict(context.get("operator_selection") or {}),
            "operator_candidate_set": candidate_set.to_dict(),
            "readiness_report_digest": str(
                context.get("readiness_report_digest") or ""
            ),
            "permission_receipt": dict(context.get("permission_receipt") or {}),
            "loopx_pre_control": dict(
                context.get("loopx_pre_control") or {}
            ),
            "owner_boundary": {
                "graph": "GraphStateCustody",
                "placement": "ResourceScheduler",
                "lease": "WorkerPoolFoundationRuntime",
            },
            "adaptive_depth_continuation": True,
            "executed_operator_refs": sorted(executed_refs),
            "reroute_required": False,
        }

    def bind_resource_decision(
        self,
        state: TaskState,
        node: PlanNode | None,
        decision: Any,
        topology_policy: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        candidate_set = topology_policy.get("operator_candidate_set")
        permission_receipt = topology_policy.get("permission_receipt")
        if not isinstance(permission_receipt, Mapping):
            raise Phase2ProductionPolicyError(
                "canonical permission receipt is missing before placement"
            )
        permission_unsigned = dict(permission_receipt)
        permission_digest = str(
            permission_unsigned.pop("receipt_digest", "")
        )
        if (
            permission_digest != canonical_digest(permission_unsigned)
            or permission_receipt.get("effect") != "allow"
            or "typescript" not in str(
                permission_receipt.get("canonical_owner") or ""
            ).casefold()
        ):
            raise Phase2ProductionPolicyError(
                "canonical permission receipt is invalid or not allowed"
            )
        loopx_consumption = topology_policy.get("loopx_pre_control")
        loopx_consumption = (
            loopx_consumption
            if isinstance(loopx_consumption, Mapping)
            else {}
        )
        loopx_digest = str(loopx_consumption.get("receipt_digest") or "")
        loopx_required = bool(
            state.metadata.get("formal_benchmark")
            or state.metadata.get("sealed_autonomous")
        )
        if loopx_required and (
            not loopx_digest
            or loopx_consumption.get("continuation_allowed") is not True
            or loopx_consumption.get("consumed_before_topology") is not True
            or not str(
                loopx_consumption.get("topology_policy_input_digest") or ""
            )
            or not str(
                loopx_consumption.get("operator_policy_input_digest") or ""
            )
        ):
            raise Phase2ProductionPolicyError(
                "ResourceScheduler received no consumed LoopX pre-control input"
            )
        decision_receipt = (
            (topology_policy.get("topology_result") or {}).get(
                "decision_receipt"
            )
            or {}
        )
        decision_payload = (
            decision_receipt.get("payload")
            if isinstance(decision_receipt.get("payload"), Mapping)
            else decision_receipt
        )
        graph_commit = (
            decision_payload.get("graph_commit")
            if isinstance(decision_payload.get("graph_commit"), Mapping)
            else {}
        )
        canonical_topology_commit_id = str(
            graph_commit.get("commit_id")
            or (state.metadata.get("dynamic_graph_ref") or {}).get(
                "commit_id"
            )
            or ""
        )
        if candidate_set is not None:
            consumed = bool(
                decision.signals.metadata.get(
                    "operator_candidate_contract_consumed",
                    False,
                )
            )
            placement = decision.metadata.get("operator_placement") or {}
            if not consumed or placement.get("route_mode") == "degraded_baseline":
                raise Phase2ProductionPolicyError(
                    "ResourceScheduler did not consume an executable MaAS candidate set"
                )
        previous = dict(state.metadata.get("worker_pool") or {})
        previous_lease_id = str(previous.get("lease_id") or "")
        if previous_lease_id:
            prior = self.worker_pool_api.pool.store.get_lease(previous_lease_id)
            if prior is not None and not prior.terminal:
                self.worker_pool_api.pool.leases.cancel(
                    previous_lease_id,
                    reason=(
                        "superseded by ResourceScheduler decision "
                        f"{decision.decision_id}"
                    ),
                )
                self.worker_pool_api.cancel_task_graph_binding(
                    state,
                    reason=(
                        "superseded by ResourceScheduler decision "
                        f"{decision.decision_id}"
                    ),
                    actor_id="phase2-resource-scheduler",
                    causation_id=(
                        f"resource-scheduler-supersede:{previous_lease_id}:"
                        f"{decision.decision_id}"
                    ),
                )
        acquisition = self.worker_pool_api.acquire_for_task(
            state,
            payload={
                "required_capabilities": ["agent_task"],
                "preferred_worker_ids": [decision.selected_manifest_id],
                "locations": [decision.selected_location.value],
                "idempotency_key": (
                    f"operator-placement:{state.task_id}:{decision.decision_id}"
                ),
                "ttl_seconds": 3600.0,
                "causal_binding": {
                    "permission_receipt_id": str(
                        permission_receipt.get("decision_id") or ""
                    ),
                    "permission_receipt_digest": permission_digest,
                    "resource_decision_id": decision.decision_id,
                    "operator_candidate_set_digest": str(
                        (candidate_set or {}).get("candidate_set_digest")
                        or "baseline:phase1_deterministic_baseline"
                    ),
                    "topology_commit_id": str(
                        canonical_topology_commit_id
                    ),
                    "loopx_pre_control_digest": loopx_digest,
                },
            },
        )
        scheduler_manifest = self.resource_scheduler.worker_pool.by_id(
            decision.selected_manifest_id
        )
        acquired_identity_checks = {
            "scheduler_manifest_present": scheduler_manifest is not None,
            "selected_manifest_exact": bool(
                scheduler_manifest is not None
                and acquisition.worker.worker_id
                == acquisition.manifest.worker_id
                == decision.selected_manifest_id
            ),
            "selected_location_exact": bool(
                scheduler_manifest is not None
                and acquisition.worker.location.value
                == acquisition.manifest.location.value
                == scheduler_manifest.location.value
                == decision.selected_location.value
            ),
            "selected_backend_exact": bool(
                scheduler_manifest is not None
                and scheduler_manifest.backend == decision.selected_backend
                and acquisition.lease.backend_id
                == acquisition.worker.backend_id
                and acquisition.lease.backend_id
                in acquisition.manifest.backend_ids
            ),
            "lease_attempt_exact": bool(
                acquisition.lease.attempt_id == acquisition.attempt.attempt_id
                and acquisition.lease.worker_id == acquisition.worker.worker_id
            ),
            "manifest_digest_exact": bool(
                acquisition.worker.manifest_digest
                == acquisition.manifest.digest
            ),
        }
        if not all(acquired_identity_checks.values()):
            self.worker_pool_api.pool.leases.cancel(
                acquisition.lease.lease_id,
                reason="ResourceScheduler selection and acquired lease identity diverged",
            )
            self.worker_pool_api.cancel_task_graph_binding(
                state,
                reason=(
                    "ResourceScheduler selection and acquired lease identity "
                    "diverged"
                ),
                actor_id="phase2-resource-scheduler",
                causation_id=(
                    "resource-scheduler-identity-rejected:"
                    f"{acquisition.lease.lease_id}"
                ),
            )
            raise Phase2ProductionPolicyError(
                "acquired lease does not exactly match the ResourceScheduler selection: "
                + ",".join(
                    name
                    for name, passed in acquired_identity_checks.items()
                    if not passed
                )
            )
        physical_graph_ref = self.worker_pool_api.topology.version_ref(
            str(state.metadata.get("dynamic_graph_id") or "")
        ).to_dict()
        state.metadata["dynamic_graph_ref"] = physical_graph_ref
        binding = {
            "schema": "zyra.production-operator-placement-binding/v1",
            "resource_decision_id": decision.decision_id,
            "selected_manifest_id": decision.selected_manifest_id,
            "selected_worker": decision.selected_worker,
            "selected_location": decision.selected_location.value,
            "selected_backend": decision.selected_backend.value,
            "candidate_set_digest": str(
                (candidate_set or {}).get("candidate_set_digest") or ""
            ),
            "attempt_id": acquisition.attempt.attempt_id,
            "lease_id": acquisition.lease.lease_id,
            "worker_id": acquisition.worker.worker_id,
            "backend_id": acquisition.lease.backend_id,
            "worker_location": acquisition.worker.location.value,
            "worker_process_identity": acquisition.worker.process_identity,
            "worker_endpoint": acquisition.worker.endpoint,
            "worker_deployment_node_id": str(
                acquisition.worker.metadata.get("deployment_node_id") or ""
            ),
            "worker_deployment_generation_id": str(
                acquisition.worker.metadata.get("deployment_generation_id")
                or ""
            ),
            "worker_manifest_digest": acquisition.manifest.digest,
            "worker_manifest_backend_ids": list(
                acquisition.manifest.backend_ids
            ),
            "lease_attempt_id": acquisition.lease.attempt_id,
            "lease_worker_id": acquisition.lease.worker_id,
            "lease_fence_epoch": acquisition.lease.fence_epoch,
            "topology_commit_id": str(
                canonical_topology_commit_id
            ),
            "physical_binding_commit_id": str(
                physical_graph_ref.get("commit_id") or ""
            ),
            "permission_receipt_id": str(
                permission_receipt.get("decision_id") or ""
            ),
            "permission_receipt_digest": permission_digest,
            "loopx_pre_control_digest": loopx_digest,
            "loopx_topology_policy_input_digest": str(
                loopx_consumption.get("topology_policy_input_digest") or ""
            ),
            "loopx_operator_policy_input_digest": str(
                loopx_consumption.get("operator_policy_input_digest") or ""
            ),
            "placement_owner": "ResourceScheduler",
            "lease_owner": "WorkerPoolFoundationRuntime",
            "acquired_identity_checks": acquired_identity_checks,
            "causation_order": [
                *(["loopx_pre_control"] if loopx_digest else []),
                "operator_candidate_set",
                "resource_decision",
                "worker_lease",
                "physical_attempt",
            ],
        }
        binding["binding_digest"] = canonical_digest(binding)
        state.metadata["operator_placement_binding"] = binding
        route_context = self._route_contexts.get(
            (state.run_id, state.task_id)
        )
        if route_context is not None:
            placement = decision.metadata.get("operator_placement") or {}
            route_context["selected_operator_refs"] = tuple(
                str(item)
                for item in placement.get("selected_operator_refs") or ()
                if str(item)
            )
            route_context["resource_decision_id"] = decision.decision_id
            route_context["lease_private"] = {
                "fence_token": acquisition.lease.fence_token,
                "fence_epoch": acquisition.lease.fence_epoch,
                "lease_acquired_at": acquisition.lease.acquired_at,
                "attempt_started_at": acquisition.attempt.started_at,
                "manifest_digest": acquisition.manifest.digest,
            }
        return binding

    def validate_execution_placement(
        self,
        state: TaskState,
        node: PlanNode | None,
    ) -> Mapping[str, Any]:
        binding = dict(state.metadata.get("operator_placement_binding") or {})
        projection = dict(state.metadata.get("worker_pool") or {})
        causality = dict(
            projection.get("operator_placement_causality") or {}
        )
        permission = dict(
            state.metadata.get("phase2_policy_permission_receipt") or {}
        )
        permission_unsigned = dict(permission)
        permission_digest = str(
            permission_unsigned.pop("receipt_digest", "")
        )
        valid_until = str(permission.get("valid_until") or "")
        now = datetime.now(UTC)
        try:
            permission_fresh = now <= datetime.fromisoformat(
                valid_until.replace("Z", "+00:00")
            ).astimezone(UTC)
        except (TypeError, ValueError):
            permission_fresh = False
        lease_id = str(binding.get("lease_id") or "")
        lease = self.worker_pool_api.pool.store.get_lease(lease_id)
        worker = (
            self.worker_pool_api.pool.store.get_worker(lease.worker_id)
            if lease is not None
            else None
        )
        manifest = (
            self.worker_pool_api.pool.store.latest_manifest(lease.worker_id)
            if lease is not None
            else None
        )
        binding_unsigned = dict(binding)
        supplied_binding_digest = str(
            binding_unsigned.pop("binding_digest", "")
        )
        lease_causality = (
            dict(lease.metadata.get("operator_placement_causality") or {})
            if lease is not None
            and isinstance(lease.metadata, Mapping)
            else {}
        )
        selected_decision = (
            dict(node.metadata.get("resource_decision") or {})
            if node is not None
            else {}
        )
        loopx_value = state.metadata.get("phase2_loopx_pre_control")
        loopx_value = loopx_value if isinstance(loopx_value, Mapping) else {}
        loopx_unsigned = dict(loopx_value)
        loopx_digest = str(loopx_unsigned.pop("receipt_digest", ""))
        loopx_required = bool(
            state.metadata.get("formal_benchmark")
            or state.metadata.get("sealed_autonomous")
        )
        checks = {
            "binding_present": bool(binding),
            "binding_digest_valid": bool(
                supplied_binding_digest
                and supplied_binding_digest
                == canonical_digest(binding_unsigned)
            ),
            "permission_digest_valid": (
                permission_digest == canonical_digest(permission_unsigned)
            ),
            "permission_allowed": permission.get("effect") == "allow",
            "permission_fresh": permission_fresh,
            "lease_active": bool(
                lease is not None and not lease.terminal and not lease.expired_at()
            ),
            "lease_scope_matches": bool(
                lease is not None
                and lease.run_id == state.run_id
                and lease.task_id == state.task_id
            ),
            "lease_attempt_identity_exact": bool(
                lease is not None
                and binding.get("attempt_id") == lease.attempt_id
                and binding.get("lease_attempt_id") == lease.attempt_id
                and binding.get("worker_id") == lease.worker_id
                and binding.get("lease_worker_id") == lease.worker_id
                and int(binding.get("lease_fence_epoch") or -1)
                == lease.fence_epoch
            ),
            "worker_identity_exact": bool(
                lease is not None
                and worker is not None
                and binding.get("worker_id") == worker.worker_id
                and binding.get("backend_id")
                == lease.backend_id
                == worker.backend_id
                and binding.get("worker_location") == worker.location.value
                == binding.get("selected_location")
                and binding.get("worker_process_identity")
                == worker.process_identity
                and binding.get("worker_endpoint") == worker.endpoint
                and binding.get("worker_deployment_node_id")
                == str(worker.metadata.get("deployment_node_id") or "")
                and binding.get("worker_deployment_generation_id")
                == str(
                    worker.metadata.get("deployment_generation_id") or ""
                )
            ),
            "manifest_identity_exact": bool(
                worker is not None
                and manifest is not None
                and binding.get("selected_manifest_id")
                == worker.worker_id
                == manifest.worker_id
                and binding.get("worker_manifest_digest")
                == worker.manifest_digest
                == manifest.digest
                and binding.get("backend_id") in manifest.backend_ids
                and sorted(binding.get("worker_manifest_backend_ids") or ())
                == sorted(manifest.backend_ids)
            ),
            "resource_decision_bound": bool(
                binding.get("resource_decision_id")
                and binding.get("resource_decision_id")
                == causality.get("resource_decision_id")
                == selected_decision.get("decision_id")
            ),
            "resource_selection_exact": bool(
                binding.get("selected_manifest_id")
                == selected_decision.get("selected_manifest_id")
                and binding.get("selected_worker")
                == selected_decision.get("selected_worker")
                and binding.get("selected_location")
                == selected_decision.get("selected_location")
                and binding.get("selected_backend")
                == selected_decision.get("selected_backend")
            ),
            "permission_bound": bool(
                permission_digest
                and permission_digest
                == binding.get("permission_receipt_digest")
                == causality.get("permission_receipt_digest")
            ),
            "candidate_bound": bool(
                causality.get("operator_candidate_set_digest")
                and causality.get("operator_candidate_set_digest")
                == (
                    binding.get("candidate_set_digest")
                    or "baseline:phase1_deterministic_baseline"
                )
            ),
            "topology_bound": bool(
                binding.get("topology_commit_id")
                and binding.get("topology_commit_id")
                == causality.get("topology_commit_id")
            ),
            "lease_causality_exact": bool(
                lease_causality and lease_causality == causality
            ),
            "loopx_pre_control_bound": (
                not loopx_required
                or bool(
                    loopx_digest
                    and loopx_digest == canonical_digest(loopx_unsigned)
                    and loopx_digest
                    == binding.get("loopx_pre_control_digest")
                    == causality.get("loopx_pre_control_digest")
                    and next(
                        iter(binding.get("causation_order") or ()),
                        None,
                    )
                    == "loopx_pre_control"
                )
            ),
        }
        if not all(checks.values()):
            raise Phase2ProductionPolicyError(
                "execution placement freshness/fence failed: "
                + ",".join(
                    key for key, value in checks.items() if not value
                )
            )
        return {
            "schema": "zyra.production-execution-placement-gate/v1",
            "checks": checks,
            "lease_id": lease_id,
            "resource_decision_id": str(
                binding.get("resource_decision_id") or ""
            ),
            "permission_receipt_digest": permission_digest,
        }

    def execute_physical_operator(
        self,
        state: TaskState,
        node: PlanNode,
    ) -> tuple[_PhysicalWorkerRun, str]:
        """Execute the selected MaAS operator on the leased physical node.

        The deployment-node call is the operator side effect itself.  The
        task-graph WorkerResult and its canonical artifact are projections of
        that call; no logical worker is run first and no proof workload is
        emitted afterwards.
        """

        if self.physical_dispatch_factory is None or self.artifact_store is None:
            raise Phase2ProductionPolicyError(
                "production physical execution owners are incomplete"
            )
        binding = dict(state.metadata.get("operator_placement_binding") or {})
        try:
            self.validate_execution_placement(state, node)
        except Exception as error:
            lease_id = str(binding.get("lease_id") or "")
            lease = self.worker_pool_api.pool.store.get_lease(lease_id)
            if lease is not None and not lease.terminal:
                cancelled, attempt = self.worker_pool_api.pool.leases.cancel(
                    lease_id,
                    reason="physical execution placement gate rejected the lease",
                )
                failure = {
                    "schema": "zyra.production-physical-failure-receipt/v1",
                    "lease_id": cancelled.lease_id,
                    "attempt_id": attempt.attempt_id,
                    "outcome": "rejected",
                    "terminal": cancelled.terminal and attempt.terminal,
                    "side_effect_started": False,
                    "automatic_execution_retry_allowed": True,
                    "error_code": str(
                        getattr(error, "code", "") or type(error).__name__
                    ),
                }
                state.metadata["physical_execution_failure_receipt"] = failure
                self.worker_pool_api.cancel_task_graph_binding(
                    state,
                    reason=(
                        "physical execution placement gate rejected the lease"
                    ),
                    actor_id="phase2-physical-execution",
                    causation_id=f"placement-rejected:{cancelled.lease_id}",
                )
            else:
                failure = {
                    "schema": "zyra.production-physical-failure-receipt/v1",
                    "lease_id": lease_id,
                    "outcome": "rejected",
                    "terminal": bool(lease is not None and lease.terminal),
                    "side_effect_started": False,
                    "automatic_execution_retry_allowed": True,
                }
                state.metadata[
                    "physical_execution_failure_receipt"
                ] = failure
                self.worker_pool_api.reconcile_task_graph_binding(
                    state,
                    reason=(
                        "physical execution placement gate found a missing "
                        "or terminal lease"
                    ),
                    actor_id="phase2-physical-execution",
                    causation_id=(
                        f"placement-rejected-terminal:{lease_id or 'missing'}"
                    ),
                )
            raise Phase2ProductionPolicyError(
                "physical execution placement gate rejected and closed the lease",
                code="phase2_physical_placement_rejected",
                metadata={
                    "physical_execution_failure_receipt": failure,
                    "automatic_execution_retry_allowed": True,
                },
            ) from error
        route_context = self._route_contexts.get((state.run_id, state.task_id))
        if route_context is None:
            last_route = dict(state.metadata.get("last_topology_route") or {})
            last_policy = dict(last_route.get("topology_policy") or {})
            topology_result = dict(
                last_policy.get("topology_result") or {}
            )
            candidate_edges = tuple(
                item
                for item in (
                    last_policy.get("communication_candidate_edges") or ()
                )
                if isinstance(item, Mapping)
            )
            operator_selection = dict(
                last_policy.get("operator_selection") or {}
            )
            raise Phase2ProductionPolicyError(
                "physical dispatch lost its route context after topology route: "
                f"committed={bool(last_policy.get('committed'))}, "
                "candidate_set="
                f"{last_policy.get('operator_candidate_set') is not None}, "
                f"degraded={bool(last_policy.get('degraded'))}, "
                f"reason={str(last_policy.get('degraded_reason') or '')[:160]}, "
                "topology_reason="
                f"{str(topology_result.get('degraded_reason') or '')[:240]}, "
                "candidate_edges="
                f"{','.join(str(item.get('edge_id') or '') for item in candidate_edges)[:320]}, "
                "outcome_count="
                f"{int(last_policy.get('communication_outcome_count') or 0)}, "
                "operator_mode="
                f"{str(operator_selection.get('mode') or '')[:80]}, "
                "operator_reason="
                f"{str(operator_selection.get('degraded_reason') or '')[:240]}"
            )
        lease_private = dict(route_context.get("lease_private") or {})
        selected_refs = tuple(
            str(item)
            for item in route_context.get("selected_operator_refs") or ()
            if str(item)
        )
        if len(selected_refs) != 1 or not lease_private.get("fence_token"):
            raise Phase2ProductionPolicyError(
                "physical dispatch requires one exactly bound operator and lease"
            )
        candidate_set = route_context["candidate_set"]
        policy_input = route_context["policy_input"]
        selected_ref = selected_refs[0]
        selected_candidate = candidate_set.candidate(selected_ref)
        selected_manifest = self.resource_scheduler.worker_pool.by_id(
            str(binding.get("worker_id") or "")
        )
        if selected_manifest is None:
            raise Phase2ProductionPolicyError(
                "physical dispatch lost its ResourceScheduler worker manifest"
            )
        layer_index = (
            len(route_context.get("prior_executed_operator_refs") or ()) + 1
        )
        recovery_execution = state.metadata.get(
            "recovery_continuation_session"
        )
        recovery_plan_id = (
            str(recovery_execution.get("plan_id") or "")
            if isinstance(recovery_execution, Mapping)
            else ""
        )
        operator_idempotency_key = (
            f"production-physical:{state.task_id}:"
            f"{canonical_digest((policy_input.requirement_revision, recovery_plan_id))[:16]}:"
            f"{selected_ref}:{layer_index}"
        )
        started_attempt = self.worker_pool_api.pool.leases.start_attempt(
            str(binding.get("lease_id") or ""),
            worker_id=str(binding.get("worker_id") or ""),
            fence_token=str(lease_private.get("fence_token") or ""),
            fence_epoch=int(lease_private.get("fence_epoch") or 0),
            backend_dispatch_id=(
                f"physical-operator:{selected_ref}:layer:{layer_index}"
            ),
        )
        execution_context = OperatorLeaseExecutionContext(
            run_id=state.run_id,
            task_id=state.task_id,
            operator_ref=selected_ref,
            operator_type=selected_candidate.operator_type,
            layer_index=layer_index,
            placement_decision_id=str(
                binding.get("resource_decision_id") or ""
            ),
            placement_location=str(
                binding.get("selected_location") or "local"
            ),
            candidate_set_digest=candidate_set.digest,
            policy_input_digest=policy_input.digest,
            graph_signature=policy_input.graph.signature,
            catalog_version=candidate_set.catalog_version,
            catalog_digest=candidate_set.catalog_digest,
            mechanism_version="operator_placement_lease_v1",
            permission_digest=str(
                binding.get("permission_receipt_digest") or ""
            ),
            operator_idempotency_key=operator_idempotency_key,
            attempt_id=str(binding.get("attempt_id") or ""),
            lease_id=str(binding.get("lease_id") or ""),
            worker_id=str(binding.get("worker_id") or ""),
            manifest_digest=str(lease_private.get("manifest_digest") or ""),
            fence_epoch=int(lease_private.get("fence_epoch") or 0),
            fence_token=str(lease_private.get("fence_token") or ""),
            lease_acquired_at=str(
                lease_private.get("lease_acquired_at") or now_iso()
            ),
            attempt_started_at=str(started_attempt.started_at or now_iso()),
            call_started_at=now_iso(),
        )
        operator_task = {
            "schema": "zyra.production-physical-operator-task/v1",
            "run_id": state.run_id,
            "task_id": state.task_id,
            "goal": state.user_goal,
            "requirement_revision": policy_input.requirement_revision,
            "operator_ref": selected_ref,
            "operator": selected_candidate.to_dict(),
            "operator_runtime": selected_manifest.runtime_worker,
            "layer_index": layer_index,
            "candidate_set_digest": candidate_set.digest,
            "policy_input_digest": policy_input.digest,
            "operator_idempotency_key": operator_idempotency_key,
            "operator_adapter_enabled": str(
                os.environ.get("ZYRA_DISABLE_PHASE2_OPERATOR_ADAPTER") or ""
            ).strip().casefold()
            not in {"1", "true", "all", selected_ref.casefold()},
            "physical_worker_binding": {
                "worker_id": str(binding.get("worker_id") or ""),
                "backend_id": str(binding.get("backend_id") or ""),
                "manifest_digest": str(
                    binding.get("worker_manifest_digest") or ""
                ),
                "process_identity": str(
                    binding.get("worker_process_identity") or ""
                ),
                "endpoint": str(binding.get("worker_endpoint") or ""),
                "node_id": str(
                    binding.get("worker_deployment_node_id") or ""
                ),
                "generation_id": str(
                    binding.get("worker_deployment_generation_id") or ""
                ),
            },
        }
        try:
            port = self.physical_dispatch_factory(
                state,
                binding,
                operator_task,
            )
            port.prepare(execution_context)
        except Exception as error:
            failure = self._close_physical_execution_failure(
                state=state,
                binding=binding,
                route_context=route_context,
                error=error,
                side_effect_started=False,
            )
            raise Phase2ProductionPolicyError(
                "physical operator preflight failed and its lease was closed",
                code="phase2_physical_operator_preflight_failed",
                metadata={
                    "physical_execution_failure_receipt": failure,
                    "automatic_execution_retry_allowed": True,
                },
            ) from error
        try:
            call_result = port.execute(execution_context)
        except Exception as error:
            error_metadata = dict(getattr(error, "metadata", {}) or {})
            side_effect_started = bool(
                error_metadata.get("side_effect_started")
            )
            failure = self._close_physical_execution_failure(
                state=state,
                binding=binding,
                route_context=route_context,
                error=error,
                side_effect_started=side_effect_started,
            )
            raise Phase2ProductionPolicyError(
                "physical operator dispatch failed with a canonical terminal receipt",
                code=(
                    "phase2_physical_operator_outcome_unknown"
                    if side_effect_started
                    else "phase2_physical_operator_dispatch_rejected"
                ),
                metadata={
                    "physical_execution_failure_receipt": failure,
                    "physical_dispatch_error": error_metadata,
                    "automatic_execution_retry_allowed": not side_effect_started,
                    "reconcile_before_retry": side_effect_started,
                },
            ) from error
        if not port.receipts or not port.validation_reports:
            failure = self._close_physical_execution_failure(
                state=state,
                binding=binding,
                route_context=route_context,
                error=RuntimeError("physical dispatch receipt missing"),
                side_effect_started=True,
            )
            raise Phase2ProductionPolicyError(
                "physical dispatch did not produce a complete receipt",
                code="phase2_physical_dispatch_receipt_missing",
                metadata={
                    "physical_execution_failure_receipt": failure,
                    "automatic_execution_retry_allowed": False,
                    "reconcile_before_retry": True,
                },
            )
        physical_dispatch = port.receipts[-1].to_dict()
        physical_validation = port.validation_reports[-1].to_dict()
        if physical_validation.get("real_gate_closed") is not True:
            failure = self._close_physical_execution_failure(
                state=state,
                binding=binding,
                route_context=route_context,
                error=RuntimeError("physical dispatch real gate did not close"),
                side_effect_started=True,
            )
            raise Phase2ProductionPolicyError(
                "physical dispatch real gate did not close: "
                + ",".join(physical_validation.get("blockers") or ()),
                code="phase2_physical_dispatch_gate_failed",
                metadata={
                    "physical_execution_failure_receipt": failure,
                    "automatic_execution_retry_allowed": False,
                    "reconcile_before_retry": True,
                },
            )
        physical_payload = dict(physical_dispatch.get("payload") or {})
        input_signals = dict(physical_payload.get("input_signals") or {})
        execution_output = dict(
            call_result.metadata.get("execution_output") or {}
        )
        execution_body = dict(
            execution_output.get("operator_execution_body") or {}
        )
        contract_outputs = dict(
            execution_output.get("contract_outputs") or {}
        )
        domain_artifact = dict(
            execution_output.get("domain_artifact") or {}
        )
        physical_identity = dict(
            physical_payload.get("physical_identity") or {}
        )
        runtime_evidence = dict(
            physical_payload.get("runtime_evidence") or {}
        )
        observed_payload_digest = str(
            execution_output.get("task_payload_digest") or ""
        ).removeprefix("sha256:")
        expected_payload_digest = canonical_digest(operator_task)
        observed_execution_digest = str(
            execution_output.get("operator_execution_digest") or ""
        ).removeprefix("sha256:")
        observed_contract_digest = str(
            execution_body.get("contract_outputs_digest") or ""
        ).removeprefix("sha256:")
        domain_content = str(domain_artifact.get("content") or "")
        observed_domain_digest = str(
            domain_artifact.get("content_digest") or ""
        ).removeprefix("sha256:")
        expected_output_contract = tuple(selected_candidate.output_contract)
        execution_checks = {
            "operation_is_operator_task": (
                input_signals.get("workload_operation")
                == "phase2-operator-execution"
            ),
            "operator_ref_exact": (
                input_signals.get("operator_ref")
                == execution_output.get("operator_ref")
                == selected_ref
            ),
            "layer_exact": (
                int(input_signals.get("layer_index") or 0)
                == int(execution_output.get("layer_index") or 0)
                == layer_index
            ),
            "payload_digest_exact": (
                input_signals.get("task_payload_digest")
                == observed_payload_digest
                == expected_payload_digest
            ),
            "execution_digest_valid": bool(
                execution_body
                and observed_execution_digest
                == canonical_digest(execution_body)
            ),
            "contract_outputs_valid": bool(
                contract_outputs
                and observed_contract_digest
                == canonical_digest(contract_outputs)
                and sorted(contract_outputs) == sorted(expected_output_contract)
                and sorted(
                    execution_body.get("fulfilled_output_contract") or ()
                )
                == sorted(expected_output_contract)
                and execution_output.get("output_contract_fulfilled") is True
            ),
            "domain_artifact_valid": bool(
                domain_content
                and observed_domain_digest == canonical_digest(domain_content)
                and str(execution_body.get("domain_artifact_digest") or "")
                .removeprefix("sha256:")
                == observed_domain_digest
            ),
            "domain_effect_performed": (
                execution_output.get("domain_effect_performed") is True
                and bool(execution_output.get("operator_adapter_id"))
            ),
            "leased_physical_process_exact": bool(
                binding.get("worker_process_identity")
                == input_signals.get("leased_worker_process_identity")
                == physical_identity.get("failure_boundary_id")
                == runtime_evidence.get("failure_boundary_id")
            ),
            "leased_physical_endpoint_exact": bool(
                binding.get("worker_endpoint")
                == input_signals.get("leased_worker_endpoint")
                == physical_identity.get("endpoint")
                == runtime_evidence.get("network_endpoint")
            ),
            "leased_node_generation_exact": bool(
                binding.get("worker_deployment_node_id")
                == input_signals.get("leased_worker_node_id")
                == physical_identity.get("node_id")
                and binding.get("worker_deployment_generation_id")
                == input_signals.get("leased_worker_generation_id")
                == physical_identity.get("generation_id")
            ),
        }
        if not all(execution_checks.values()):
            failure = self._close_physical_execution_failure(
                state=state,
                binding=binding,
                route_context=route_context,
                error=RuntimeError("physical operator output binding failed"),
                side_effect_started=True,
            )
            raise Phase2ProductionPolicyError(
                "physical operator output is not exactly bound to its request: "
                + ",".join(
                    name for name, passed in execution_checks.items() if not passed
                )
                + " "
                + json.dumps(
                    {
                        "signal_payload_digest": input_signals.get(
                            "task_payload_digest"
                        ),
                        "output_payload_digest": execution_output.get(
                            "task_payload_digest"
                        ),
                        "expected_payload_digest": expected_payload_digest,
                    },
                    sort_keys=True,
                ),
                code="phase2_physical_operator_output_binding_failed",
                metadata={
                    "physical_execution_failure_receipt": failure,
                    "automatic_execution_retry_allowed": False,
                    "reconcile_before_retry": True,
                    "failed_execution_checks": [
                        name
                        for name, passed in execution_checks.items()
                        if not passed
                    ],
                },
            )
        try:
            artifact = self.artifact_store.write_text(
                run_id=state.run_id,
                task_id=state.task_id,
                content=domain_content,
                title=str(
                    domain_artifact.get("title")
                    or f"Physical operator result: {selected_ref}"
                ),
                kind=ArtifactKind(
                    str(domain_artifact.get("kind") or "structured_data")
                ),
                extension=str(domain_artifact.get("extension") or ".json"),
                producer_node_id=node.node_id,
                metadata={
                    "content_type": str(
                        domain_artifact.get("media_type")
                        or "application/octet-stream"
                    ),
                    "operator_ref": selected_ref,
                    "layer_index": layer_index,
                    "operator_adapter_id": str(
                        execution_output.get("operator_adapter_id") or ""
                    ),
                    "domain_result_kind": str(
                        (execution_output.get("domain_result") or {}).get("kind")
                        or ""
                    ),
                    "domain_output_digest": observed_domain_digest,
                    "operator_output_contract": list(expected_output_contract),
                    "operator_output_contract_fulfilled": True,
                    "physical_call_ref": call_result.call_ref,
                    "physical_receipt_digest": str(
                        physical_dispatch.get("digest") or ""
                    ),
                    "operator_execution_digest": str(
                        execution_output.get("operator_execution_digest") or ""
                    ),
                    "node_artifact_digest": str(
                        (physical_payload.get("artifact_ref") or {}).get(
                            "digest"
                        )
                        or ""
                    ),
                },
            )
        except Exception as error:
            failure = self._close_physical_execution_failure(
                state=state,
                binding=binding,
                route_context=route_context,
                error=error,
                side_effect_started=True,
            )
            raise Phase2ProductionPolicyError(
                "physical operator completed but canonical artifact commit failed",
                code="phase2_physical_artifact_commit_failed",
                metadata={
                    "physical_execution_failure_receipt": failure,
                    "automatic_execution_retry_allowed": False,
                    "reconcile_before_retry": True,
                },
            ) from error
        event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=node.node_id,
            event_type=EventType.ARTIFACT_WRITTEN,
            payload={
                "schema": "zyra.production-physical-operator-executed/v1",
                "operator_ref": selected_ref,
                "layer_index": layer_index,
                "operator_idempotency_key": operator_idempotency_key,
                "artifact_id": artifact.artifact_id,
                "physical_dispatch_receipt": physical_dispatch,
                "physical_dispatch_validation": physical_validation,
                "execution_checks": execution_checks,
                "operator_adapter_id": execution_output.get(
                    "operator_adapter_id"
                ),
                "domain_result": execution_output.get("domain_result"),
                "fulfilled_output_contract": sorted(contract_outputs),
            },
        )
        from zyra_runtime import WorkerResult

        worker_result = WorkerResult(
            request_id=str(call_result.call_ref),
            ok=True,
            summary=str(
                execution_output.get("summary")
                or call_result.summary
            ),
            artifacts=[artifact],
            events=[to_jsonable(event)],
            completed_at=call_result.call_finished_at,
            metadata={
                "operator_ref": selected_ref,
                "layer_index": str(layer_index),
                "operator_idempotency_key": operator_idempotency_key,
                "physical_call_ref": call_result.call_ref,
                "physical_dispatch_receipt_digest": str(
                    physical_dispatch.get("digest") or ""
                ),
                "operator_execution_digest": str(
                    execution_output.get("operator_execution_digest") or ""
                ),
                "operator_adapter_id": str(
                    execution_output.get("operator_adapter_id") or ""
                ),
                "domain_result_kind": str(
                    (execution_output.get("domain_result") or {}).get("kind")
                    or ""
                ),
                "domain_output_digest": observed_domain_digest,
                "output_contract_fulfilled": "true",
            },
        )
        route_context["physical_dispatch_receipt"] = physical_dispatch
        route_context["physical_dispatch_validation"] = physical_validation
        route_context["operator_call_result"] = call_result
        route_context["operator_execution_checks"] = execution_checks
        return (
            _PhysicalWorkerRun(
                worker_result=worker_result,
                event_records=[event],
            ),
            str(binding.get("worker_id") or "physical-operator-worker"),
        )

    def _close_physical_execution_failure(
        self,
        *,
        state: TaskState,
        binding: Mapping[str, Any],
        route_context: Mapping[str, Any],
        error: BaseException,
        side_effect_started: bool,
    ) -> Mapping[str, Any]:
        lease_private = dict(route_context.get("lease_private") or {})
        physical_dispatch_digest = str(
            dict(route_context.get("physical_dispatch_receipt") or {}).get(
                "digest"
            )
            or ""
        )
        lease_id = str(binding.get("lease_id") or "")
        lease = self.worker_pool_api.pool.store.get_lease(lease_id)
        if lease is None:
            selected = {
                "schema": "zyra.production-physical-failure-receipt/v1",
                "lease_id": lease_id,
                "terminal": True,
                "error_code": "lease_missing",
                "side_effect_started": side_effect_started,
                "physical_dispatch_receipt_digest": (
                    physical_dispatch_digest
                ),
            }
            state.metadata["physical_execution_failure_receipt"] = dict(
                selected
            )
            self.worker_pool_api.cancel_task_graph_binding(
                state,
                reason="physical execution lease was missing",
                actor_id="phase2-physical-execution",
                causation_id=f"physical-failure-missing:{lease_id}",
            )
            if isinstance(route_context, dict):
                route_context["physical_execution_failure_receipt"] = dict(
                    selected
                )
            return selected
        existing = next(
            (
                item
                for item in self.worker_pool_api.pool.store.receipts_for_task(
                    state.task_id
                )
                if item.attempt_id == lease.attempt_id
            ),
            None,
        )
        if existing is not None:
            selected = existing.to_dict()
        elif lease.terminal:
            selected = {
                "schema": "zyra.production-physical-failure-receipt/v1",
                "lease_id": lease_id,
                "attempt_id": lease.attempt_id,
                "terminal": True,
                "lease_state": lease.state.value,
                "side_effect_started": side_effect_started,
                "physical_dispatch_receipt_digest": (
                    physical_dispatch_digest
                ),
            }
        else:
            receipt = self.worker_pool_api.pool.leases.complete(
                lease_id,
                worker_id=str(binding.get("worker_id") or ""),
                fence_token=str(lease_private.get("fence_token") or ""),
                fence_epoch=int(lease_private.get("fence_epoch") or 0),
                outcome=(
                    ExecutionOutcome.FAILED
                    if side_effect_started
                    else ExecutionOutcome.REJECTED
                ),
                summary=(
                    "Physical operator outcome requires reconciliation."
                    if side_effect_started
                    else "Physical operator was rejected before side effects."
                ),
                error_code=str(
                    getattr(error, "code", "")
                    or type(error).__name__
                ),
                error_message=str(error),
                metadata={
                    "schema": "zyra.production-physical-failure-receipt/v1",
                    "side_effect_started": side_effect_started,
                    "outcome_unknown": side_effect_started,
                    "automatic_execution_retry_allowed": not side_effect_started,
                    "reconcile_before_retry": side_effect_started,
                    "resource_decision_id": str(
                        binding.get("resource_decision_id") or ""
                    ),
                    "physical_dispatch_receipt_digest": (
                        physical_dispatch_digest
                    ),
                    "cause_metadata": dict(
                        getattr(error, "metadata", {}) or {}
                    ),
                },
            )
            selected = receipt.to_dict()
        state.metadata["physical_execution_failure_receipt"] = dict(selected)
        self.worker_pool_api.complete_task_graph_binding(
            state,
            succeeded=False,
            reason=(
                "physical execution failed: "
                f"{str(getattr(error, 'code', '') or type(error).__name__)}"
            ),
            outcome_ref=str(selected.get("receipt_id") or ""),
            actor_id="phase2-physical-execution",
            causation_id=f"physical-failure:{lease_id}",
        )
        if isinstance(route_context, dict):
            route_context["physical_execution_failure_receipt"] = dict(selected)
        return selected

    def record_execution_outcome(
        self,
        state: TaskState,
        node: PlanNode,
        worker_run: Any,
    ) -> Mapping[str, Any]:
        """Close the already executed physical attempt without a second call."""

        route_context = self._route_contexts.get((state.run_id, state.task_id))
        if route_context is None:
            raise Phase2ProductionPolicyError(
                "physical completion lost its route context"
            )
        physical_dispatch = dict(
            route_context.get("physical_dispatch_receipt") or {}
        )
        physical_validation = dict(
            route_context.get("physical_dispatch_validation") or {}
        )
        result_metadata = dict(worker_run.worker_result.metadata or {})
        if (
            not physical_dispatch
            or physical_validation.get("real_gate_closed") is not True
            or result_metadata.get("physical_dispatch_receipt_digest")
            != physical_dispatch.get("digest")
        ):
            raise Phase2ProductionPolicyError(
                "physical worker result is not bound to the canonical dispatch receipt"
            )
        worker_events = tuple(
            str(item.event_id)
            for item in worker_run.event_records
            if str(getattr(item, "event_id", ""))
        )
        gateway_receipt_ref = str(
            (
                (physical_dispatch.get("payload") or {}).get("call_receipt")
                or {}
            ).get("uri")
            or ""
        )
        binding = dict(state.metadata.get("operator_placement_binding") or {})
        memory_record_ids: list[str] = []
        memory_mutation_receipt: dict[str, Any] = {}
        if result_metadata.get("domain_result_kind") == "memory_continuity":
            try:
                before_records = tuple(
                    self.memory_fabric.canonical_task_records(state.task_id)
                )
                memory_snapshot = self.memory_fabric.refresh_task_memory(
                    state,
                    [to_jsonable(item) for item in worker_run.event_records],
                    persist=True,
                )
                after_records = tuple(
                    self.memory_fabric.canonical_task_records(state.task_id)
                )
                snapshot_by_id = {
                    str(item.memory_id): canonical_digest(to_jsonable(item))
                    for item in memory_snapshot.records
                    if str(item.memory_id)
                }
                after_by_id = {
                    str(item.memory_id): canonical_digest(to_jsonable(item))
                    for item in after_records
                    if str(item.memory_id)
                }
                before_ids = {
                    str(item.memory_id)
                    for item in before_records
                    if str(item.memory_id)
                }
                readback_exact = bool(snapshot_by_id) and all(
                    after_by_id.get(memory_id) == record_digest
                    for memory_id, record_digest in snapshot_by_id.items()
                )
                memory_record_ids = sorted(snapshot_by_id)
                if not readback_exact:
                    raise RuntimeError(
                        "MemoryFabric owner read-back did not match the committed mutation"
                    )
                receipt_body = {
                    "schema": "zyra.memory-owner-mutation-receipt/v1",
                    "owner": "MemoryFabric",
                    "run_id": state.run_id,
                    "task_id": state.task_id,
                    "operator_ref": str(
                        result_metadata.get("operator_ref") or ""
                    ),
                    "operator_execution_digest": str(
                        result_metadata.get("operator_execution_digest") or ""
                    ),
                    "physical_dispatch_receipt_digest": str(
                        physical_dispatch.get("digest") or ""
                    ),
                    "before_record_count": len(before_records),
                    "before_records_digest": canonical_digest(
                        sorted(
                            (
                                str(item.memory_id),
                                canonical_digest(to_jsonable(item)),
                            )
                            for item in before_records
                            if str(item.memory_id)
                        )
                    ),
                    "committed_record_ids": memory_record_ids,
                    "new_record_ids": sorted(
                        set(memory_record_ids).difference(before_ids)
                    ),
                    "canonical_delta_digest": canonical_digest(
                        sorted(snapshot_by_id.items())
                    ),
                    "after_record_count": len(after_records),
                    "after_records_digest": canonical_digest(
                        sorted(after_by_id.items())
                    ),
                    "readback_verified": True,
                    "committed": True,
                }
                memory_mutation_receipt = dict(receipt_body)
                memory_mutation_receipt["receipt_digest"] = canonical_digest(
                    receipt_body
                )
            except Exception as error:
                failure = self._close_physical_execution_failure(
                    state=state,
                    binding=binding,
                    route_context=route_context,
                    error=error,
                    side_effect_started=True,
                )
                raise Phase2ProductionPolicyError(
                    "MemoryFabric owner mutation failed after physical execution",
                    code="phase2_memory_owner_commit_failed",
                    metadata={
                        "physical_execution_failure_receipt": failure,
                        "automatic_execution_retry_allowed": False,
                        "reconcile_before_retry": True,
                    },
                ) from error
        published_dispatch_ref: dict[str, Any] = {}
        if self.evidence_publisher is not None:
            try:
                published_dispatch = self.evidence_publisher.publish(
                    PhysicalDispatchReceipt.from_dict(physical_dispatch),
                    run_id=state.run_id,
                    task_id=state.task_id,
                )
                published_dispatch_ref = (
                    published_dispatch.artifact_ref.to_dict()
                )
            except Exception as error:
                publication_error = Phase2ProductionPolicyError(
                    "physical dispatch evidence publication failed after "
                    "physical execution",
                    code="phase2_physical_evidence_publish_failed",
                    metadata={
                        "physical_dispatch_receipt_digest": str(
                            physical_dispatch.get("digest") or ""
                        ),
                        "automatic_execution_retry_allowed": False,
                        "reconcile_before_retry": True,
                        "publication_error": type(error).__name__,
                    },
                )
                failure = self._close_physical_execution_failure(
                    state=state,
                    binding=binding,
                    route_context=route_context,
                    error=publication_error,
                    side_effect_started=True,
                )
                publication_error.metadata[
                    "physical_execution_failure_receipt"
                ] = failure
                raise publication_error from error
        receipt = self.worker_pool_api.finalize_task(
            state,
            success=bool(worker_run.worker_result.ok),
            summary=str(worker_run.worker_result.summary),
            event_refs=worker_events,
            gateway_receipt_ref=gateway_receipt_ref,
            metadata={
                "schema": "zyra.phase2-physical-attempt-completion/v2",
                "resource_decision_id": str(
                    binding.get("resource_decision_id") or ""
                ),
                "operator_candidate_set_digest": str(
                    binding.get("candidate_set_digest") or ""
                ),
                "permission_receipt_digest": str(
                    binding.get("permission_receipt_digest") or ""
                ),
                "topology_commit_id": str(
                    binding.get("topology_commit_id") or ""
                ),
                "physical_dispatch_receipt_digest": str(
                    physical_dispatch.get("digest") or ""
                ),
                "operator_ref": str(result_metadata.get("operator_ref") or ""),
                "operator_idempotency_key": str(
                    result_metadata.get("operator_idempotency_key") or ""
                ),
                "memory_mutation_receipt": memory_mutation_receipt,
                "physical_dispatch_policy_artifact_ref": (
                    published_dispatch_ref
                ),
            },
        )
        if not isinstance(receipt, Mapping):
            raise Phase2ProductionPolicyError(
                "physical worker lease did not return a completion receipt"
            )
        selected = dict(receipt)
        selected["physical_dispatch_receipt"] = physical_dispatch
        selected["physical_dispatch_validation"] = physical_validation
        state.metadata.setdefault("physical_dispatch_receipts", []).append(
            physical_dispatch
        )
        if published_dispatch_ref:
            selected["physical_dispatch_policy_artifact_ref"] = dict(
                published_dispatch_ref
            )
        if memory_mutation_receipt:
            selected["memory_mutation_receipt"] = dict(
                memory_mutation_receipt
            )
        operator_call_result = route_context.get("operator_call_result")
        if operator_call_result is None:
            raise Phase2ProductionPolicyError(
                "physical operator call result is missing before layer custody"
            )
        layer_record = {
            "schema": "zyra.production-operator-execution-layer/v1",
            "layer_index": int(result_metadata.get("layer_index") or 0),
            "operator_ref": str(result_metadata.get("operator_ref") or ""),
            "operator_idempotency_key": str(
                result_metadata.get("operator_idempotency_key") or ""
            ),
            "resource_decision_id": str(
                binding.get("resource_decision_id") or ""
            ),
            "lease_id": str(binding.get("lease_id") or ""),
            "attempt_id": str(binding.get("attempt_id") or ""),
            "worker_id": str(binding.get("worker_id") or ""),
            "backend_id": str(binding.get("backend_id") or ""),
            "worker_manifest_digest": str(
                binding.get("worker_manifest_digest") or ""
            ),
            "physical_call_ref": str(
                result_metadata.get("physical_call_ref") or ""
            ),
            "physical_dispatch_receipt_digest": str(
                physical_dispatch.get("digest") or ""
            ),
            "operator_execution_digest": str(
                result_metadata.get("operator_execution_digest") or ""
            ),
            "operator_adapter_id": str(
                result_metadata.get("operator_adapter_id") or ""
            ),
            "domain_result_kind": str(
                result_metadata.get("domain_result_kind") or ""
            ),
            "domain_output_digest": str(
                result_metadata.get("domain_output_digest") or ""
            ),
            "output_contract_fulfilled": (
                result_metadata.get("output_contract_fulfilled") == "true"
            ),
            "memory_record_ids": memory_record_ids,
            "memory_mutation_receipt": memory_mutation_receipt,
            "physical_node_artifact_ref": str(
                (
                    (physical_dispatch.get("payload") or {}).get(
                        "artifact_ref"
                    )
                    or {}
                ).get("ref_id")
                or ""
            ),
            "canonical_artifact_ids": [
                str(item.artifact_id)
                for item in worker_run.worker_result.artifacts
                if str(item.artifact_id)
            ],
            "worker_pool_receipt_id": str(
                selected.get("receipt_id") or ""
            ),
            "actual_tokens": int(operator_call_result.actual_tokens),
            "actual_cost_usd": float(operator_call_result.actual_cost_usd),
            "actual_latency_ms": int(operator_call_result.actual_latency_ms),
        }
        layer_record["layer_digest"] = canonical_digest(layer_record)
        state.metadata.setdefault("phase2_operator_execution_layers", []).append(
            layer_record
        )
        state.metadata["worker_pool_receipt"] = dict(selected)
        route_context["physical_execution_receipt"] = selected
        return selected

    def evaluate_completion(
        self,
        state: TaskState,
        events: Sequence[EventRecord],
    ) -> Mapping[str, Any]:
        """Run the MaAS early-exit hard gate at the production safe point."""

        if (
            self.artifact_store is None
            or self.permission_queue_provider is None
            or self.recovery_store is None
            or self.final_verifier_owner is None
        ):
            raise Phase2ProductionPolicyError(
                "production early-exit canonical owner binding is incomplete"
            )
        context = self._route_contexts.get((state.run_id, state.task_id))
        if context is None:
            raise Phase2ProductionPolicyError(
                "production early-exit route context is unavailable"
            )
        proposal = context["proposal"]
        policy_input = context["policy_input"]
        selected_refs = tuple(context.get("selected_operator_refs") or ())
        if not selected_refs:
            raise Phase2ProductionPolicyError(
                "ResourceScheduler did not bind an executed MaAS operator"
            )
        by_ref = {
            f"{item.operator_id}@{item.version}": item
            for item in proposal.candidates
        }
        executed_ref = next(
            (item for item in selected_refs if item in by_ref),
            "",
        )
        if not executed_ref:
            raise Phase2ProductionPolicyError(
                "physical execution is not a member of the MaAS proposal"
            )
        executed_candidate = by_ref[executed_ref]
        ordered_candidates = (
            executed_candidate,
            *tuple(
                item
                for item in proposal.candidates
                if f"{item.operator_id}@{item.version}" != executed_ref
            ),
        )
        execution_layers = tuple(
            OperatorLayerProposal(
                layer_index=index,
                candidates=(candidate,),
                reason=(
                    "ResourceScheduler canonical physical prefix"
                    if index == 1
                    else "MaAS remaining depth pending early-exit verdict"
                ),
            )
            for index, candidate in enumerate(ordered_candidates, start=1)
        )
        physical_receipt = dict(
            context.get("physical_execution_receipt") or {}
        )
        physical_receipt_id = str(
            physical_receipt.get("receipt_id") or ""
        )
        if (
            not physical_receipt_id
            or physical_receipt.get("outcome") != "succeeded"
        ):
            raise Phase2ProductionPolicyError(
                "successful canonical physical execution receipt is missing"
            )
        prior_executed = tuple(
            str(item)
            for item in context.get("prior_executed_operator_refs") or ()
            if str(item)
        )
        operator_call_result = context.get("operator_call_result")
        if (
            operator_call_result is None
            or operator_call_result.operator_ref != executed_ref
            or not operator_call_result.artifact_refs
            or not operator_call_result.verification_refs
        ):
            raise Phase2ProductionPolicyError(
                "physical operator call result is absent or belongs to another operator"
            )
        operator_decision = build_operator_execution_decision(
            task=state,
            layer_index=len(prior_executed) + 1,
            candidates=(executed_candidate,),
            owner_receipt_ref=physical_receipt_id,
            actual_tokens=operator_call_result.actual_tokens,
            actual_cost_usd=operator_call_result.actual_cost_usd,
            actual_latency_ms=operator_call_result.actual_latency_ms,
            verification_refs=operator_call_result.verification_refs,
        )
        state.decisions.append(operator_decision)

        required_artifact_ids = tuple(
            sorted(
                {
                    str(item.artifact_id)
                    for item in state.artifacts
                    if str(item.artifact_id)
                }
            )
        )
        if not required_artifact_ids:
            raise Phase2ProductionPolicyError(
                "production early-exit requires a canonical artifact"
            )
        final_verifier = next(
            (
                item
                for item in reversed(state.decisions)
                if item.decision_type == "final_verifier"
            ),
            None,
        )
        if final_verifier is None:
            raise Phase2ProductionPolicyError(
                "independent final verifier verdict is missing"
            )
        verifier_metadata = dict(final_verifier.metadata)
        verifier_ref = str(
            verifier_metadata.get("verifier_receipt_ref") or ""
        )
        resolve = getattr(
            self.final_verifier_owner,
            "resolve_final_verifier_receipt",
            None,
        )
        resolved_verifier = resolve(verifier_ref) if callable(resolve) else None
        if (
            not verifier_ref
            or not isinstance(resolved_verifier, Mapping)
            or dict(resolved_verifier) != verifier_metadata
        ):
            raise Phase2ProductionPolicyError(
                "independent final verifier owner receipt is missing or drifted"
            )
        graph = self.worker_pool_api.graph_custody.current(
            str(state.metadata.get("dynamic_graph_id") or "")
        )
        checkpoint_runtime = CheckpointCommitRuntime(self.recovery_store)
        head = self.recovery_store.checkpoint_head(state.task_id)
        session_id = (
            head.session_id
            if head is not None
            else str(
                state.metadata.get("query_session_id")
                or f"task:{state.task_id}"
            )
        )
        workflow_signature = (
            head.workflow_signature
            if head is not None
            else canonical_digest(
                {
                    "task_id": state.task_id,
                    "graph_version": state.metadata.get("graph_version"),
                    "stage_order": state.metadata.get("stage_order") or (),
                }
            )
        )
        checkpoint, checkpoint_receipt, _ = checkpoint_runtime.commit(
            CheckpointCommitRequest(
                refs=RecoveryRefs(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    session_id=session_id,
                ),
                workflow_signature=workflow_signature,
                graph_signature=graph.signature,
                topology_signature=graph.signature,
                owner_refs={
                    "task": "TaskState",
                    "artifact": "LocalArtifactStore",
                    "permission": "typescript.PermissionCoordinator",
                    "side_effect": "RecoveryPlanStore",
                    "graph": "GraphStateCustody",
                    "placement": "ResourceScheduler",
                    "lease": "WorkerPoolFoundationRuntime",
                    "verifier": verifier_ref,
                },
                version_refs={
                    "requirement_revision": policy_input.requirement_revision,
                    "graph_revision": graph.revision,
                    "operator_proposal_digest": proposal.digest,
                },
                committed_refs=(
                    {"physical_execution_receipt": physical_receipt_id},
                    {"final_verifier_receipt": verifier_ref},
                ),
                completed_step_ids=tuple(
                    str(item)
                    for item in state.metadata.get("stage_order") or ()
                    if str(item)
                ),
                state_payload={
                    "phase": "phase2-production-completion-gate",
                    "task_status": state.status.value,
                },
                metadata={
                    "requirement_revision": policy_input.requirement_revision,
                    "canonical_early_exit_safe_point": True,
                },
            )
        )
        continuity_gate = context["continuity_gate"]
        preexisting = set(context.get("preexisting_artifact_ids") or ())
        continuity = MemoryContinuityVerifier(
            self.memory_fabric
        ).finalize_after_decision(
            continuity_gate,
            DownstreamMemoryUsage(
                decision_ref=operator_decision.decision_id,
                requirement_revision=policy_input.requirement_revision,
                critical_fact_ids=tuple(
                    item.ref_id for item in continuity_gate.accepted_fact_refs
                ),
                obligation_ids=tuple(policy_input.unresolved_obligations),
                source_event_refs=tuple(
                    item.event_id for item in events if item.event_id
                ),
                produced_artifact_ids=tuple(
                    item
                    for item in required_artifact_ids
                    if item not in preexisting
                ),
                worker_id=str(physical_receipt.get("worker_id") or ""),
                node_id=str(
                    next(
                        (
                            item.node_id
                            for item in state.plan_nodes.values()
                            if str(item.metadata.get("stage") or "")
                            == "execute"
                        ),
                        state.root_node_id,
                    )
                ),
            ),
            header=self._header(
                contract_id=(
                    "continuity-final:"
                    + canonical_digest(
                        (physical_receipt_id, checkpoint.checkpoint_id)
                    )[:24]
                ),
                mechanism_id="MemoryContinuityVerifier",
                source_event_id=str(context["source_event_id"]),
                causation_id=physical_receipt_id,
            ),
        )
        if not continuity.passed:
            raise Phase2ProductionPolicyError(
                "post-dispatch memory continuity failed: "
                + ",".join(continuity.reason_codes)
            )
        published_continuity_ref: dict[str, Any] = {}
        if self.evidence_publisher is not None:
            published_continuity = self.evidence_publisher.publish(
                continuity.receipt,
                run_id=state.run_id,
                task_id=state.task_id,
            )
            published_continuity_ref = (
                published_continuity.artifact_ref.to_dict()
            )
        symbolic_bundle_ref = self._publish_symbolic_bundle(
            state=state,
            context=context,
            physical_execution_receipt=physical_receipt,
            verifier_ref=verifier_ref,
        )
        builder = CanonicalExitSnapshotBuilder(
            config=self.early_exit_config,
            artifact_store=self.artifact_store,
            permission_queue=self.permission_queue_provider(state),
            side_effect_store=self.recovery_store,
            final_verifier_owner=self.final_verifier_owner,
        )
        eligibility = _ProductionEligibilityPort(
            builder=builder,
            task=state,
            policy_input=policy_input,
            proposal=proposal,
            checkpoint=checkpoint,
            continuity_receipt=continuity.receipt,
            required_artifact_ids=required_artifact_ids,
        )
        layer_receipt = LayerExecutionReceipt(
            layer_index=1,
            operator_refs=(executed_ref,),
            owner_receipt_ref=physical_receipt_id,
            decision_refs=(operator_decision.decision_id,),
            artifact_refs=tuple(operator_call_result.artifact_refs),
            verification_refs=tuple(
                dict.fromkeys(
                    (*operator_call_result.verification_refs, verifier_ref)
                )
            ),
            actual_tokens=operator_call_result.actual_tokens,
            actual_cost_usd=operator_call_result.actual_cost_usd,
            actual_latency_ms=operator_call_result.actual_latency_ms,
        )
        early_exit_enabled = bool(self.early_exit_enabled())
        snapshot, decision, decision_event = (
            self.adaptive_depth.evaluate_executed_prefix(
                proposal=proposal,
                execution_layers=execution_layers,
                executed_receipts=(layer_receipt,),
                eligibility_port=eligibility,
                early_exit_enabled=early_exit_enabled,
            )
        )
        binding = self.adaptive_depth.gate.checkpoint_binding(
            snapshot,
            decision,
        )
        accumulated_executed = tuple(
            dict.fromkeys((*prior_executed, executed_ref))
        )
        state.metadata["phase2_executed_operator_refs"] = list(
            accumulated_executed
        )
        hard_condition_names = {
            "snapshot_fresh",
            "canonical_owner_evidence_complete",
            "final_verifier_passed",
            "critical_obligations_resolved",
            "required_artifacts_complete",
            "permission_settled",
            "side_effects_settled",
            "minimum_operator_path_executed",
            "checkpoint_requirement_current",
            "memory_continuity_passed",
        }
        condition_map = {
            item.condition_id: item.passed for item in decision.conditions
        }
        hard_conditions_passed = bool(
            hard_condition_names.issubset(condition_map)
            and all(condition_map[name] for name in hard_condition_names)
        )
        full_proposal = context.get("full_proposal") or proposal
        proposed_operator_refs = tuple(
            dict.fromkeys(
                f"{candidate.operator_id}@{candidate.version}"
                for layer in full_proposal.layers
                for candidate in layer.candidates
            )
        )
        executed_set = set(accumulated_executed)
        layer_records = tuple(
            item
            for item in state.metadata.get(
                "phase2_operator_execution_layers"
            )
            or ()
            if isinstance(item, Mapping)
        )
        adaptive_depth_receipt = AdaptiveDepthCostReceipt(
            proposal_id=full_proposal.proposal_id,
            proposal_digest=full_proposal.digest,
            decision_ref=decision.decision_id,
            proposed_depth=len(full_proposal.layers),
            executed_depth=len(layer_records),
            proposed_operator_count=len(proposed_operator_refs),
            executed_operator_count=len(executed_set),
            avoided_operator_refs=tuple(
                ref for ref in proposed_operator_refs if ref not in executed_set
            ),
            actual_tokens=sum(
                int(item.get("actual_tokens") or 0)
                for item in layer_records
            ),
            estimated_avoided_tokens=int(decision.avoided_tokens),
            actual_cost_usd=sum(
                float(item.get("actual_cost_usd") or 0.0)
                for item in layer_records
            ),
            estimated_avoided_cost_usd=float(decision.avoided_cost_usd),
            actual_latency_ms=sum(
                int(item.get("actual_latency_ms") or 0)
                for item in layer_records
            ),
            task_completed=state.status is PlanNodeStatus.COMPLETED,
            verifier_passed=hard_conditions_passed,
            artifact_complete=not snapshot.invalid_artifact_ids,
        )
        return {
            "schema": "zyra.production-adaptive-depth-completion-gate/v1",
            "decision": decision.decision.value,
            "decision_id": decision.decision_id,
            "decision_digest": decision.digest,
            "failed_conditions": list(decision.failed_conditions),
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_digest": snapshot.digest,
            "remaining_operator_count": len(
                tuple(
                    ref
                    for ref in proposed_operator_refs
                    if ref not in executed_set
                )
            ),
            "executed_operator_refs": list(accumulated_executed),
            "hard_conditions_passed": hard_conditions_passed,
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_receipt": checkpoint_receipt.to_dict(),
            "checkpoint_binding": binding.to_dict(),
            "continuity_receipt": continuity.receipt.to_dict(),
            "continuity_policy_artifact_ref": published_continuity_ref,
            "symbolic_bundle_policy_artifact_ref": symbolic_bundle_ref,
            "final_verifier_receipt_ref": verifier_ref,
            "physical_execution_receipt_ref": physical_receipt_id,
            "adaptive_depth_event_id": decision_event.event_id,
            "early_exit_receipt": decision.to_dict(),
            "adaptive_depth_receipt": adaptive_depth_receipt.to_dict(),
            "early_exit_enabled": early_exit_enabled,
            "canonical_owner_bypass": False,
        }

    def _publish_symbolic_bundle(
        self,
        *,
        state: TaskState,
        context: Mapping[str, Any],
        physical_execution_receipt: Mapping[str, Any],
        verifier_ref: str,
    ) -> Mapping[str, Any]:
        if self.evidence_publisher is None:
            raise Phase2ProductionPolicyError(
                "production symbolic bundle publisher is unavailable"
            )
        topology = dict(context.get("topology_projection") or {})
        topology_result = dict(topology.get("topology_result") or {})
        composition = dict(topology_result.get("composition") or {})
        proposal_document = dict(composition.get("proposal") or {})
        decision_document = dict(
            topology_result.get("decision_receipt") or {}
        )
        outcome_document = dict(topology_result.get("outcome") or {})
        if not proposal_document or not decision_document or not outcome_document:
            raise Phase2ProductionPolicyError(
                "production symbolic inputs are incomplete"
            )
        proposal = TopologyProposalArtifact.from_dict(proposal_document)
        decision = PolicyDecisionReceipt.from_dict(decision_document)
        failed_constraints = tuple(
            item for item in decision.constraint_results if not item.passed
        )
        commit = dict(decision.graph_commit)
        commit_present = bool(commit)
        if commit_present and failed_constraints:
            raise Phase2ProductionPolicyError(
                "failed symbolic constraint reached canonical commit"
            )
        disposition = decision.disposition.value
        result = (
            "rejected"
            if disposition in {"reject", "conflict"}
            else "repaired"
            if disposition in {"project", "rebase"}
            else "accepted"
        )
        refs = dict(context.get("topology_policy_artifact_refs") or {})
        proposal_ref_value = refs.get("topology_proposal_artifact")
        proposal_ref = (
            StableArtifactRef.from_mapping(proposal_ref_value)
            if isinstance(proposal_ref_value, Mapping)
            else StableArtifactRef(
                ref_id=proposal.proposal_id,
                uri=f"policy-contract://{proposal.proposal_id}",
                digest=proposal.digest,
            )
        )
        physical_contract = dict(
            physical_execution_receipt.get("physical_dispatch_receipt") or {}
        )
        physical_payload = dict(physical_contract.get("payload") or {})
        physical_verifier = dict(physical_payload.get("verifier_ref") or {})
        bundle = NeuroSymbolicEvidenceBundle(
            header=self._header(
                contract_id=(
                    "production-symbolic:"
                    + canonical_digest(
                        (proposal.digest, decision.digest, physical_contract.get("digest"))
                    )[:24]
                ),
                mechanism_id="phase2_symbolic_projector",
                source_event_id=str(context.get("source_event_id") or ""),
                causation_id=decision.header.contract_id,
            ),
            proposal_signal_mode="deterministic_only",
            proposal_ref=proposal_ref,
            model_observation_refs=(),
            constraint_results=decision.constraint_results,
            projected_delta_ref=decision.delta_id or "no-delta",
            commit_or_no_commit=FrozenDict(
                {
                    "result": result,
                    "decision_disposition": disposition,
                    "proposal_digest": proposal.digest,
                    "decision_digest": decision.digest,
                    "outcome_digest": str(outcome_document.get("digest") or ""),
                    "physical_dispatch_digest": str(
                        physical_contract.get("digest") or ""
                    ),
                    "canonical_commit_present": commit_present,
                    "canonical_commit": commit,
                    "constraint_failure_codes": [
                        item.reason_code for item in failed_constraints
                    ],
                    "unsafe_commit": False,
                    "projector_bypass_production_reachable": False,
                    "symbolic_owner_controls_commit": True,
                }
            ),
            permission_ref=str(physical_payload.get("permission_ref") or ""),
            lease_ref=str(physical_payload.get("lease_id") or ""),
            verification_ref=str(
                physical_verifier.get("uri")
                or physical_verifier.get("ref_id")
                or verifier_ref
            ),
        )
        published = self.evidence_publisher.publish(
            bundle,
            run_id=state.run_id,
            task_id=state.task_id,
        )
        state.metadata.setdefault("phase2_symbolic_bundles", []).append(
            bundle.to_dict()
        )
        return published.artifact_ref.to_dict()

    def _environment(
        self,
        *,
        state: TaskState,
        projection: Mapping[str, Any],
        source_event_id: str,
    ) -> Any:
        created_at = now_iso()
        header = self._header(
            contract_id=f"environment:{state.run_id}:{canonical_digest(projection)[:20]}",
            mechanism_id="ResourceScheduler",
            source_event_id=source_event_id,
            causation_id=source_event_id,
            created_at=created_at,
        )
        return EnvironmentSnapshotBuilder.from_worker_pool_projection(
            projection,
            header=header,
        )

    def _communication_observations(
        self,
        *,
        state: TaskState,
        candidates: Sequence[Any],
        completed_before: str,
        maximum_age_seconds: int,
    ) -> tuple[CommunicationOutcomeObservation, ...]:
        by_id = {item.edge_id: item for item in candidates}
        by_shape: dict[tuple[str, str, str], list[Any]] = {}
        for item in candidates:
            by_shape.setdefault(
                (
                    item.source_node_id,
                    item.target_node_id,
                    item.edge_type.value,
                ),
                [],
            ).append(item)
        observations = []
        for raw in self.communication_outcome_provider(state):
            if not isinstance(raw, Mapping):
                continue
            if (
                str(raw.get("run_id") or "") != state.run_id
                or str(raw.get("task_id") or "") != state.task_id
            ):
                raise Phase2ProductionPolicyError(
                    "communication outcome scope differs from the current task"
                )
            raw_edge_id = str(raw.get("edge_id") or "")
            raw_shape = (
                str(raw.get("source_node_id") or ""),
                str(raw.get("target_node_id") or ""),
                str(raw.get("edge_type") or ""),
            )
            candidate = by_id.get(raw_edge_id)
            if candidate is not None and raw_shape != (
                candidate.source_node_id,
                candidate.target_node_id,
                candidate.edge_type.value,
            ):
                raise Phase2ProductionPolicyError(
                    "communication outcome edge identity and endpoints differ"
                )
            if candidate is None:
                shape_matches = tuple(by_shape.get(raw_shape, ()))
                if len(shape_matches) > 1:
                    raise Phase2ProductionPolicyError(
                        "communication outcome shape maps to multiple candidates"
                    )
                candidate = shape_matches[0] if shape_matches else None
            if candidate is None:
                continue
            if not self._communication_outcome_is_prior_and_fresh(
                completed_at=str(raw.get("completed_at") or ""),
                completed_before=completed_before,
                maximum_age_seconds=maximum_age_seconds,
            ):
                continue
            observations.append(
                CommunicationOutcomeObservation(
                    observation_id=str(raw.get("observation_id") or ""),
                    run_id=str(raw.get("run_id") or ""),
                    task_id=str(raw.get("task_id") or ""),
                    window_id=str(raw.get("window_id") or ""),
                    completed_at=str(raw.get("completed_at") or ""),
                    edge_id=candidate.edge_id,
                    source_node_id=str(raw.get("source_node_id") or ""),
                    target_node_id=str(raw.get("target_node_id") or ""),
                    edge_type=CommunicationEdgeType(
                        str(raw.get("edge_type") or "")
                    ),
                    round_index=int(raw.get("round_index") or 0),
                    message_id=str(raw.get("message_id") or ""),
                    payload_digest=str(raw.get("payload_digest") or ""),
                    delivered=bool(raw.get("delivered")),
                    delivery_receipt_ref=str(
                        raw.get("delivery_receipt_ref") or ""
                    ),
                    usage_receipt_ref=str(
                        raw.get("usage_receipt_ref") or ""
                    ),
                    message_bytes=int(raw.get("message_bytes") or 0),
                    prompt_tokens=int(raw.get("prompt_tokens") or 0),
                    completion_tokens=int(
                        raw.get("completion_tokens") or 0
                    ),
                    cost_usd=float(raw.get("cost_usd") or 0),
                    evidence_refs=tuple(raw.get("evidence_refs") or ()),
                    utilized_evidence_refs=tuple(
                        raw.get("utilized_evidence_refs") or ()
                    ),
                    artifact_refs=tuple(raw.get("artifact_refs") or ()),
                    verifier_result=str(
                        raw.get("verifier_result") or "not_run"
                    ),
                    failure_count=int(raw.get("failure_count") or 0),
                    retry_count=int(raw.get("retry_count") or 0),
                    redundant_with_message_id=str(
                        raw.get("redundant_with_message_id") or ""
                    ),
                    permission_result=str(
                        raw.get("permission_result") or "allowed"
                    ),
                    malicious=bool(raw.get("malicious")),
                    causal_refs=tuple(raw.get("causal_refs") or ()),
                )
            )
        return tuple(observations)

    @staticmethod
    def _communication_outcome_is_prior_and_fresh(
        *,
        completed_at: str,
        completed_before: str,
        maximum_age_seconds: int,
    ) -> bool:
        try:
            completed_value = datetime.fromisoformat(
                completed_at.replace("Z", "+00:00")
            )
            cutoff_value = datetime.fromisoformat(
                completed_before.replace("Z", "+00:00")
            )
        except (AttributeError, ValueError) as error:
            raise Phase2ProductionPolicyError(
                "communication outcome timestamp is invalid"
            ) from error
        if completed_value.tzinfo is None or cutoff_value.tzinfo is None:
            raise Phase2ProductionPolicyError(
                "communication outcome timestamp requires a timezone"
            )
        completed = completed_value.astimezone(UTC)
        cutoff = cutoff_value.astimezone(UTC)
        age_seconds = (cutoff - completed).total_seconds()
        return age_seconds > 0 and (
            maximum_age_seconds <= 0
            or age_seconds <= maximum_age_seconds
        )

    @staticmethod
    def _communication_coverage_complete(
        *,
        candidates: Sequence[Any],
        observations: Sequence[CommunicationOutcomeObservation],
    ) -> bool:
        candidate_edge_ids = {
            str(item.edge_id) for item in candidates if str(item.edge_id)
        }
        observed_edge_ids = {
            str(item.edge_id) for item in observations if str(item.edge_id)
        }
        return bool(candidate_edge_ids) and candidate_edge_ids.issubset(
            observed_edge_ids
        )

    def _logical_role_projection(
        self,
        environment: EnvironmentSnapshot,
    ) -> tuple[tuple[Mapping[str, Any], ...], EnvironmentSnapshot]:
        physical_by_location = {
            item.location: item
            for item in environment.observations
            if item.category == "worker"
            and item.available
            and item.healthy
            and not item.missing_fields
        }
        manifests = []
        logical_observations = []
        # ARG roles are logical capabilities, so multiple local roles may be
        # backed by one healthy physical runtime. Placement candidates remain
        # separately restricted to the ResourceScheduler's executable pool.
        logical_catalog = {
            item.worker_id: item for item in default_worker_manifests()
        }
        logical_catalog.update(
            {
                item.worker_id: item
                for item in self.resource_scheduler.worker_pool.manifests()
            }
        )
        for item in logical_catalog.values():
            location = item.location.value
            physical = physical_by_location.get(location)
            if physical is None:
                continue
            manifest = {
                "worker_id": item.worker_id,
                "worker_kind": item.runtime_worker,
                "location": location,
                "capabilities": list(item.capabilities),
                "tool_ids": list(item.tools),
                "model_ids": list(item.models),
                "enabled": item.enabled,
                "constraints": {
                    "required_permissions": ["graph.write"],
                    "allowed_placements": [location],
                },
                "labels": {"arg_role": f"role.{item.worker_id}"},
                "metadata": {
                    "physical_worker_id": physical.resource_id,
                    "physical_runtime_id": physical.physical_runtime_id,
                    "projection_owner": "ResourceScheduler",
                },
            }
            manifest["digest"] = canonical_digest(manifest)
            manifests.append(manifest)
            logical_observations.append(
                replace(
                    physical,
                    observation_id=(
                        "logical-" + canonical_digest(
                            (
                                physical.observation_id,
                                item.worker_id,
                                manifest["digest"],
                            )
                        )[:24]
                    ),
                    resource_id=item.worker_id,
                    observation_source=(
                        "ResourceScheduler.logical_manifest_projection"
                    ),
                    attributes=FrozenDict(
                        {
                            **dict(physical.attributes),
                            "physical_worker_id": physical.resource_id,
                            "logical_worker_id": item.worker_id,
                            "capabilities": list(item.capabilities),
                            "tools": list(item.tools),
                            "models": list(item.models),
                        }
                    ),
                )
            )
        return (
            tuple(manifests),
            EnvironmentSnapshot(
                header=environment.header,
                observed_at=environment.observed_at,
                observations=(
                    *environment.observations,
                    *logical_observations,
                ),
                required_categories=environment.required_categories,
                missing_categories=environment.missing_categories,
            ),
        )

    @staticmethod
    def _card_condition_inputs(
        *,
        preview_arg: Any,
        cause_event: EventRecord | None,
    ) -> tuple[
        tuple[CARDReplacementCandidate, ...],
        Mapping[str, CARDEdgeHysteresisState],
    ]:
        payload = (
            cause_event.payload
            if cause_event is not None
            and isinstance(cause_event.payload, Mapping)
            else {}
        )
        if (
            payload.get("schema")
            != "zyra.phase2-temporal-handoff-receipt/v1"
            or payload.get("acknowledged") is not True
            or preview_arg.hypothesis is None
            or len(preview_arg.hypothesis.role_steps) < 2
        ):
            return (), {}
        source, target = preview_arg.hypothesis.role_steps[:2]
        candidate_id = "card-temporal-" + canonical_digest(
            (
                cause_event.event_id,
                source.node_id,
                target.node_id,
                payload.get("checkpoint_ref"),
            )
        )[:20]
        candidate = CARDReplacementCandidate(
            candidate_id=candidate_id,
            source_node_id=source.node_id,
            target_node_id=target.node_id,
            relation="temporal_checkpoint_handoff",
            edge_type="temporal",
            required_capabilities=tuple(target.capabilities[:1]),
            constraint_ref=str(
                payload.get("checkpoint_ref") or cause_event.event_id
            ),
            reason="canonical temporal handoff receipt conditions CARD",
        )
        return (
            (candidate,),
            {
                candidate_id: CARDEdgeHysteresisState(
                    edge_id=candidate_id,
                    active=False,
                    # A never-materialized candidate has no prior switch;
                    # message creation time is not topology change time.
                    last_changed_at="1970-01-01T00:00:00Z",
                    last_score=0,
                    pending_action="add",
                    confirmations=1,
                )
            },
        )

    def _continuity_gate(
        self,
        *,
        state: TaskState,
        graph: Any,
        source_event_id: str,
    ) -> tuple[Any, str]:
        obligations = self._obligations(state)
        requirement_revision = str(
            state.metadata.get("requirement_revision")
            or f"requirements:{canonical_digest(obligations)}"
        )
        verifier = MemoryContinuityVerifier(self.memory_fabric)
        records = tuple(self.memory_fabric.canonical_task_records(state.task_id))
        critical_fact_ids = tuple(
            str(getattr(item, "memory_id", ""))
            for item in records
            if str(getattr(item, "memory_id", ""))
        )
        completed_artifacts = tuple(
            item.artifact_id for item in state.artifacts if item.artifact_id
        )
        before = verifier.capture(
            run_id=state.run_id,
            task_id=state.task_id,
            requirement_revision=requirement_revision,
            obligation_ids=obligations,
            critical_fact_ids=critical_fact_ids,
            completed_artifact_ids=completed_artifacts,
        )
        transition = ContinuityTransition(
            transition_id=f"policy-projection:{state.run_id}:{graph.revision}",
            kind=ContinuityTransitionKind.NODE_REPLACEMENT,
            owner_receipt_ref=graph.commit_id,
            source_scope=f"task-state:{state.task_id}",
            target_scope=f"graph-state:{graph.graph_id}:{graph.revision}",
            acknowledged=True,
            acknowledged_requirement_revision=requirement_revision,
            acknowledged_obligation_digest=before.obligation_digest,
            acknowledged_fact_ids=tuple(
                item.ref_id for item in before.critical_fact_refs
            ),
        )
        gate = verifier.verify_before_policy(
            before,
            transition=transition,
            current_requirement_revision=requirement_revision,
            current_obligation_ids=obligations,
            critical_fact_ids=critical_fact_ids,
            completed_artifact_ids=completed_artifacts,
        )
        if not gate.passed:
            raise Phase2ProductionPolicyError(
                "memory continuity gate failed: " + ",".join(gate.reason_codes)
            )
        return gate, requirement_revision

    def _readiness_refs(
        self,
        *,
        source_event_id: str,
    ) -> tuple[tuple[MechanismEvidenceReadinessReportRef, ...], str]:
        report = json.loads(self.readiness_path.read_text(encoding="utf-8"))
        supplied = str(report.get("report_digest") or "")
        unsigned = dict(report)
        unsigned.pop("report_digest", None)
        if supplied != canonical_digest(unsigned):
            raise Phase2ProductionPolicyError(
                "activation readiness report digest mismatch"
            )
        stage = str(report.get("readiness_stage") or "")
        mechanisms = report.get("mechanisms")
        if not isinstance(mechanisms, Mapping):
            raise Phase2ProductionPolicyError(
                "activation readiness mechanisms are missing"
            )
        refs = []
        for mechanism_id in (
            "loopx",
            "arg_designer",
            "card",
            "agentprune",
            "maas",
        ):
            mechanism = mechanisms.get(mechanism_id)
            if not isinstance(mechanism, Mapping):
                raise Phase2ProductionPolicyError(
                    f"activation readiness is missing {mechanism_id}"
                )
            status = str(mechanism.get("status") or "")
            mechanism_stage = str(mechanism.get("readiness_stage") or stage)
            if (
                mechanism_stage != "activation_ready"
                or status != "deterministic_ready"
            ):
                raise Phase2ProductionPolicyError(
                    f"{mechanism_id} is not activation-ready"
                )
            refs.append(
                MechanismEvidenceReadinessReportRef(
                    header=self._header(
                        contract_id=f"readiness:{mechanism_id}:{supplied[:20]}",
                        mechanism_id=mechanism_id,
                        source_event_id=source_event_id,
                        causation_id=f"readiness-report:{supplied}",
                    ),
                    report_ref=self.readiness_path.relative_to(
                        self.repository_root
                    ).as_posix(),
                    report_digest=supplied,
                    readiness_stage=mechanism_stage,
                    status=status,
                )
            )
        return tuple(refs), supplied

    def _loopx_pre_control_input(
        self,
        state: TaskState,
    ) -> dict[str, Any]:
        value = state.metadata.get("phase2_loopx_pre_control")
        formal = bool(
            state.metadata.get("formal_benchmark")
            or state.metadata.get("sealed_autonomous")
        )
        if not isinstance(value, Mapping):
            if formal:
                raise Phase2ProductionPolicyError(
                    "formal strongest execution has no LoopX pre-control receipt"
                )
            return {}
        selected = dict(value)
        unsigned = dict(selected)
        supplied = str(unsigned.pop("receipt_digest", ""))
        checks = selected.get("checks")
        checks = checks if isinstance(checks, Mapping) else {}
        continuation = selected.get("continuation")
        continuation = (
            continuation if isinstance(continuation, Mapping) else {}
        )
        permission = selected.get("permission_receipt")
        permission = permission if isinstance(permission, Mapping) else {}
        permission_unsigned = dict(permission)
        permission_digest = str(
            permission_unsigned.pop("receipt_digest", "")
        )
        validation = selected.get("validation")
        validation = validation if isinstance(validation, Mapping) else {}
        if (
            selected.get("schema")
            != "zyra.phase2-production-loopx-pre-control/v1"
            or selected.get("run_id") != state.run_id
            or selected.get("task_id") != state.task_id
            or not supplied
            or supplied != canonical_digest(unsigned)
            or not checks
            or not all(item is True for item in checks.values())
            or continuation.get("allowed") is not True
            or not str(selected.get("canonical_commit_id") or "")
            or permission.get("schema")
            != "zyra.phase2-policy-permission-receipt/v1"
            or permission.get("run_id") != state.run_id
            or permission.get("task_id") != state.task_id
            or permission.get("effect") != "allow"
            or "typescript"
            not in str(permission.get("canonical_owner") or "").casefold()
            or not {"graph.write", "worker.dispatch"}.issubset(
                set(permission.get("allowed_permissions") or ())
            )
            or not permission_digest
            or permission_digest != canonical_digest(permission_unsigned)
            or validation.get("permission_allowed") is not True
            or validation.get("permission_receipt_id")
            != permission.get("decision_id")
        ):
            raise Phase2ProductionPolicyError(
                "LoopX pre-control receipt is invalid or denies continuation"
            )
        return selected

    @staticmethod
    def _loopx_consumption_projection(
        value: Mapping[str, Any],
        *,
        topology_policy_input_digest: str,
    ) -> dict[str, Any]:
        if not value:
            return {}
        return {
            "schema": "zyra.production-loopx-consumption/v1",
            "receipt_digest": value.get("receipt_digest"),
            "goal_id": value.get("goal_id"),
            "permission_receipt_digest": (
                (value.get("permission_receipt") or {}).get(
                    "receipt_digest"
                )
                if isinstance(value.get("permission_receipt"), Mapping)
                else ""
            ),
            "continuation_allowed": (
                (value.get("continuation") or {}).get("allowed") is True
                if isinstance(value.get("continuation"), Mapping)
                else False
            ),
            "topology_policy_input_digest": topology_policy_input_digest,
            "consumed_before_topology": True,
        }

    def _policy_input(
        self,
        *,
        state: TaskState,
        graph: Any,
        environment: Any,
        role_catalog: Any,
        continuity: Any,
        requirement_revision: str,
        readiness_refs: tuple[MechanismEvidenceReadinessReportRef, ...],
        source_event_id: str,
        allowed_permissions: tuple[str, ...],
        loopx_pre_control: Mapping[str, Any],
    ) -> Any:
        budget = self._budget(state)
        verifier = MemoryContinuityVerifier(self.memory_fabric)
        return verifier.build_policy_input(
            continuity,
            task=state,
            graph=graph,
            environment=environment,
            header=self._header(
                contract_id=(
                    f"policy-input:{state.run_id}:{graph.revision}:"
                    f"{continuity.gate_id[-12:]}"
                ),
                mechanism_id="PolicyInputSnapshotBuilder",
                source_event_id=source_event_id,
                causation_id=continuity.gate_id,
            ),
            budget=budget,
            readiness_refs=readiness_refs,
            registry_versions={
                "arg_role_catalog": role_catalog.catalog_version,
                "arg_role_catalog_digest": role_catalog.digest,
                "loopx_pre_control_digest": str(
                    loopx_pre_control.get("receipt_digest") or "not-required"
                ),
                "loopx_goal_id": str(
                    loopx_pre_control.get("goal_id") or "not-required"
                ),
                **dict(role_catalog.source_versions),
            },
            phase=str(
                (state.metadata.get("runtime_hints") or {}).get("phase")
                or "execution"
            ),
            registered_roles=tuple(
                item.role_id for item in role_catalog.profiles
            ),
            registered_capabilities=tuple(
                capability
                for item in role_catalog.profiles
                for capability in item.capabilities
            ),
            allowed_permissions=allowed_permissions,
            allowed_placements=tuple(
                sorted(
                    {
                        observation.location
                        for observation in environment.observations
                        if observation.available and observation.healthy
                    }
                )
            ),
            privacy_class=str(
                state.metadata.get("privacy_class") or "internal"
            ),
            last_topology_change_at=(
                "1970-01-01T00:00:00Z"
                if str(graph.metadata.get("last_branch_id") or "")
                in {
                    "api-task-bootstrap",
                    f"sealed-loopx-pre-control:{state.task_id}",
                }
                else graph.created_at
            ),
        )

    def _permission_receipt(
        self,
        *,
        state: TaskState,
        requested: tuple[str, ...],
        cause_event: EventRecord | None,
    ) -> Mapping[str, Any]:
        if self.permission_decision_provider is None:
            raise Phase2ProductionPolicyError(
                "canonical permission decision provider is unavailable"
            )
        receipt = dict(
            self.permission_decision_provider(
                state,
                requested,
                cause_event,
            )
        )
        unsigned = dict(receipt)
        supplied = str(unsigned.pop("receipt_digest", ""))
        allowed = {
            str(item) for item in receipt.get("allowed_permissions") or ()
        }
        if (
            supplied != canonical_digest(unsigned)
            or receipt.get("effect") != "allow"
            or "typescript"
            not in str(receipt.get("canonical_owner") or "").casefold()
            or not set(requested).issubset(allowed)
        ):
            raise Phase2ProductionPolicyError(
                "canonical permission owner did not allow the strongest path"
            )
        return receipt

    @staticmethod
    def _budget(state: TaskState) -> PolicyBudget:
        raw = state.metadata.get("execution_budget")
        value = dict(raw) if isinstance(raw, Mapping) else {}
        return PolicyBudget(
            remaining_tokens=int(value.get("remaining_tokens") or 16_384),
            remaining_cost_usd=float(value.get("remaining_cost_usd") or 10.0),
            remaining_time_ms=int(value.get("remaining_time_ms") or 120_000),
            max_communication_bytes=int(
                value.get("max_communication_bytes") or 65_536
            ),
            max_fan_out=int(value.get("max_fan_out") or 4),
            max_topology_churn=int(value.get("max_topology_churn") or 32),
            minimum_dwell_seconds=int(
                value.get("minimum_dwell_seconds") or 0
            ),
        )

    @staticmethod
    def _obligations(state: TaskState) -> tuple[str, ...]:
        terminal = {
            PlanNodeStatus.COMPLETED,
            PlanNodeStatus.FAILED,
            PlanNodeStatus.CANCELLED,
            PlanNodeStatus.SUPERSEDED,
        }
        values = [
            *state.constraints.requirements,
            *state.constraints.success_criteria,
        ]
        for item in state.plan_nodes.values():
            if item.status not in terminal:
                values.extend(item.constraints.requirements)
                values.extend(item.completion_criteria)
        return tuple(sorted({str(item) for item in values if str(item)}))

    @staticmethod
    def _task_summary(state: TaskState, node: PlanNode | None) -> str:
        values = [state.user_goal]
        if node is not None:
            values.extend((node.title, node.description, node.summary))
        return "\n".join(str(item) for item in values if str(item))

    @staticmethod
    def _header(
        *,
        contract_id: str,
        mechanism_id: str,
        source_event_id: str,
        causation_id: str,
        created_at: str | None = None,
    ) -> ContractHeader:
        return ContractHeader(
            contract_id=contract_id,
            created_at=created_at or now_iso(),
            source_event_id=source_event_id,
            correlation_id=source_event_id,
            causation_id=causation_id,
            mechanism_id=mechanism_id,
            mechanism_version="phase2_strongest_v1",
            input_version="v1",
            idempotency_key=f"{mechanism_id}:{contract_id}",
        )


__all__ = [
    "Phase2ProductionPolicyError",
    "Phase2StrongestProductionBridge",
]
