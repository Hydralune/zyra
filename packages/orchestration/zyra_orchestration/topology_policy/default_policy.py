from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zyra_core import EventRecord, PlanNode, TaskState

from ..graph_custody import (
    GraphConflictStrategy,
    GraphStateCustody,
    GraphStateSnapshot,
)
from .arg import (
    ARGModelObservation,
    ARGRoleCatalog,
    ARGTopologyRuntime,
)
from .composer import (
    TopologyComposerConfig,
    TopologyLayerSwitches,
    TopologyPolicyComposer,
)
from .condition import (
    CARDEdgeHysteresisState,
    CARDReplacementCandidate,
    CARDTopologyRuntime,
)
from .contracts import PolicyInputSnapshot, thaw_json
from .evidence import PolicyEvidencePublisher
from .pruning import (
    AgentPruneRuntime,
    CommunicationBudget,
    CommunicationOutcomeObservation,
    ProtectedEdgeConstraint,
)
from .projector import TopologyConstraintProjector
from .registry import (
    MechanismRunPin,
    ResolutionPurpose,
    ValidationManifest,
)
from .runtime import (
    BaselineExecutor,
    MechanismInvocation,
    PolicyRuntimeError,
    PolicyRuntimeResult,
    TopologyComposerRuntime,
    TopologyComposerRuntimeResult,
    TopologyPolicyRuntime,
)


@dataclass(frozen=True, slots=True)
class DefaultTopologyPolicyRequest:
    policy_input: PolicyInputSnapshot
    current_graph: GraphStateSnapshot
    task_summary: str
    role_catalog: ARGRoleCatalog
    communication_observations: tuple[
        CommunicationOutcomeObservation,
        ...,
    ]
    protections: tuple[ProtectedEdgeConstraint, ...]
    communication_budget: CommunicationBudget
    mechanism_epoch: str
    trigger_kind: str
    purpose: ResolutionPurpose = ResolutionPurpose.NORMAL
    version: str | None = None
    validation_manifest: ValidationManifest | None = None
    existing_pin: MechanismRunPin | None = None
    switches: TopologyLayerSwitches = TopologyLayerSwitches()
    branch_delta: Any | None = None
    recent_recovery_outcome: Mapping[str, Any] | None = None
    model_observation: ARGModelObservation | None = None
    replacement_candidates: tuple[CARDReplacementCandidate, ...] = ()
    hysteresis_state: Mapping[
        str,
        CARDEdgeHysteresisState,
    ] | None = None
    prior_pruning_mask: Any | None = None
    conflict_strategy: GraphConflictStrategy = GraphConflictStrategy.REPLAN
    recovery_causal_refs: tuple[str, ...] = ()
    verification_ref: str = "GraphStateCustody.validate_graph"


@dataclass(frozen=True, slots=True)
class DefaultTopologyPolicyResult:
    policy_result: PolicyRuntimeResult
    topology_result: TopologyComposerRuntimeResult | None
    layer_event_ids: tuple[str, ...]

    @property
    def used_baseline(self) -> bool:
        return (
            self.policy_result.execution_receipt.actual_profile_id
            == "phase1_deterministic_baseline"
        )

    @property
    def committed(self) -> bool:
        return (
            self.topology_result is not None
            and self.topology_result.committed
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pin": self.policy_result.pin.to_dict(),
            "actual_decision": thaw_json(
                self.policy_result.actual_decision
            ),
            "execution_receipt": (
                self.policy_result.execution_receipt.to_dict()
            ),
            "diagnostic_receipt": (
                self.policy_result.diagnostic_receipt.to_dict()
                if self.policy_result.diagnostic_receipt is not None
                else None
            ),
            "topology_result": (
                self.topology_result.to_dict()
                if self.topology_result is not None
                else None
            ),
            "layer_event_ids": list(self.layer_event_ids),
            "used_baseline": self.used_baseline,
            "committed": self.committed,
        }


class DefaultTopologyPolicy:
    """Default trigger entry with explicit strongest activation and rollback."""

    def __init__(
        self,
        *,
        policy_runtime: TopologyPolicyRuntime,
        arg_runtime: ARGTopologyRuntime,
        card_runtime: CARDTopologyRuntime,
        pruning_runtime: AgentPruneRuntime,
        composer_runtime: TopologyComposerRuntime,
        admit_event: Callable[[EventRecord], None] | None = None,
    ) -> None:
        self.policy_runtime = policy_runtime
        self.arg_runtime = arg_runtime
        self.card_runtime = card_runtime
        self.pruning_runtime = pruning_runtime
        self.composer_runtime = composer_runtime
        self.admit_event = admit_event or (lambda event: None)

    @classmethod
    def from_repository(
        cls,
        repository_root: Path,
        *,
        custody: GraphStateCustody,
        baseline_executor: BaselineExecutor,
        baseline_reference: BaselineExecutor | None = None,
        owner_probe: Callable[[], Mapping[str, Any]] | None = None,
        evidence_publisher: PolicyEvidencePublisher | None = None,
        admit_event: Callable[[EventRecord], None] | None = None,
        registry_config_path: Path | None = None,
        composer_config_path: Path | None = None,
        arg_report_path: Path | None = None,
        card_report_path: Path | None = None,
        pruning_report_path: Path | None = None,
    ) -> DefaultTopologyPolicy:
        root = repository_root.resolve()
        sink = admit_event or (lambda event: None)
        activation_readiness_report = (
            root
            / "docs"
            / "release"
            / "phase2"
            / "activation-readiness.json"
        )
        config = TopologyComposerConfig.load(
            (
                composer_config_path.resolve()
                if composer_config_path is not None
                else root
                / "config"
                / "phase2"
                / "topology-composer.json"
            )
        )
        policy_runtime = TopologyPolicyRuntime.from_repository(
            root,
            baseline_executor=baseline_executor,
            baseline_reference=baseline_reference,
            owner_probe=owner_probe,
            admit_event=sink,
            config_path=registry_config_path,
        )
        arg_runtime = ARGTopologyRuntime.from_repository(
            root,
            report_path=arg_report_path or activation_readiness_report,
            evidence_publisher=evidence_publisher,
            admit_event=sink,
        )
        card_runtime = CARDTopologyRuntime.from_repository(
            root,
            report_path=card_report_path or activation_readiness_report,
            evidence_publisher=evidence_publisher,
            admit_event=sink,
        )
        pruning_runtime = AgentPruneRuntime.from_repository(
            root,
            report_path=pruning_report_path or activation_readiness_report,
            evidence_publisher=evidence_publisher,
            admit_event=sink,
        )
        composer_runtime = TopologyComposerRuntime(
            composer=TopologyPolicyComposer(config),
            projector=TopologyConstraintProjector(custody),
            evidence_publisher=evidence_publisher,
            admit_event=sink,
        )
        return cls(
            policy_runtime=policy_runtime,
            arg_runtime=arg_runtime,
            card_runtime=card_runtime,
            pruning_runtime=pruning_runtime,
            composer_runtime=composer_runtime,
            admit_event=sink,
        )

    def execute(
        self,
        request: DefaultTopologyPolicyRequest,
    ) -> DefaultTopologyPolicyResult:
        topology_result: TopologyComposerRuntimeResult | None = None
        layer_events: list[EventRecord] = []

        def execute_mechanism(
            invocation: MechanismInvocation,
        ) -> Mapping[str, Any]:
            nonlocal topology_result
            if not request.switches.policy_enabled:
                raise PolicyRuntimeError(
                    "phase2_topology_policy_disabled",
                    "the Phase 2 topology policy is explicitly disabled",
                )
            arg_result = self.arg_runtime.execute(
                policy_input=request.policy_input,
                task_summary=request.task_summary,
                current_graph=request.current_graph,
                role_catalog=request.role_catalog,
                branch_delta=request.branch_delta,
                recent_recovery_outcome=(
                    request.recent_recovery_outcome
                ),
                model_observation=request.model_observation,
                enabled=request.switches.arg_enabled,
            )
            layer_events.extend(arg_result.events)
            if arg_result.proposal is None:
                raise PolicyRuntimeError(
                    arg_result.degraded_reason
                    or "arg_proposal_unavailable",
                    "ARG did not produce a composable proposal",
                )
            card_result = self.card_runtime.execute(
                policy_input=request.policy_input,
                arg_base=arg_result.proposal,
                current_graph=request.current_graph,
                replacement_candidates=(
                    request.replacement_candidates
                ),
                hysteresis_state=request.hysteresis_state,
                enabled=request.switches.card_enabled,
            )
            layer_events.extend(card_result.events)
            upstream = (
                card_result.correction_proposal
                if card_result.correction_proposal is not None
                else arg_result.proposal
            )
            pruning_result = self.pruning_runtime.execute(
                policy_input=request.policy_input,
                card_result=card_result,
                upstream_proposal=upstream,
                current_graph=request.current_graph,
                observations=request.communication_observations,
                protections=request.protections,
                budget=request.communication_budget,
                mechanism_epoch=request.mechanism_epoch,
                prior_mask=request.prior_pruning_mask,
                enabled=request.switches.agentprune_enabled,
            )
            layer_events.extend(pruning_result.events)
            scheduler_refs = tuple(
                sorted(
                    {
                        item.source_event_id
                        for item in request.policy_input.environment.observations
                        if item.source_event_id
                    }
                )
            )
            topology_result = self.composer_runtime.execute(
                mode=invocation.mode,
                trigger_kind=request.trigger_kind,
                policy_input=request.policy_input,
                current_graph=request.current_graph,
                arg_result=arg_result,
                card_result=card_result,
                pruning_result=pruning_result,
                switches=request.switches,
                conflict_strategy=request.conflict_strategy,
                scheduler_causal_refs=scheduler_refs,
                recovery_causal_refs=request.recovery_causal_refs,
                verification_ref=request.verification_ref,
            )
            if topology_result.degraded:
                raise PolicyRuntimeError(
                    topology_result.degraded_reason
                    or "topology_composer_degraded",
                    "the unified topology path requires explicit baseline",
                )
            return topology_result.to_dict()

        result = self.policy_runtime.execute(
            run_id=request.policy_input.run_id,
            task_id=request.policy_input.task_id,
            input_snapshot=request.policy_input,
            family="topology_policy",
            purpose=request.purpose,
            version=request.version,
            validation_manifest=request.validation_manifest,
            existing_pin=request.existing_pin,
            mechanism_executor=execute_mechanism,
        )
        return DefaultTopologyPolicyResult(
            policy_result=result,
            topology_result=topology_result,
            layer_event_ids=tuple(item.event_id for item in layer_events),
        )


TopologyPolicyRequestBuilder = Callable[
    [TaskState, PlanNode | None, EventRecord | None],
    DefaultTopologyPolicyRequest,
]


class TopologyPolicyTriggerAdapter:
    """Bounded hook consumed by the existing symbolic topology trigger."""

    def __init__(
        self,
        policy: DefaultTopologyPolicy,
        *,
        request_builder: TopologyPolicyRequestBuilder,
    ) -> None:
        self.policy = policy
        self.request_builder = request_builder

    def __call__(
        self,
        state: TaskState,
        node: PlanNode | None,
        cause_event: EventRecord | None,
    ) -> Mapping[str, Any]:
        request = self.request_builder(state, node, cause_event)
        if (
            request.policy_input.run_id != state.run_id
            or request.policy_input.task_id != state.task_id
        ):
            raise PolicyRuntimeError(
                "topology_trigger_scope_mismatch",
                "topology policy request belongs to another task",
            )
        return self.policy.execute(request).to_dict()


__all__ = [
    "DefaultTopologyPolicy",
    "DefaultTopologyPolicyRequest",
    "DefaultTopologyPolicyResult",
    "TopologyPolicyRequestBuilder",
    "TopologyPolicyTriggerAdapter",
]
