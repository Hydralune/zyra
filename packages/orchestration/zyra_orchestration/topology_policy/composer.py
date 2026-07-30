from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from zyra_core import EventRecord, EventType

from ..graph_custody import GraphStateSnapshot
from .arg import ARGTopologyRuntimeResult
from .condition import CARDTopologyRuntimeResult
from .contracts import (
    ContractHeader,
    FrozenDict,
    PolicyInputSnapshot,
    StableArtifactRef,
    TopologyOperation,
    TopologyOperationKind,
    TopologyProposalArtifact,
    canonical_digest,
    thaw_json,
)
from .pruning import AgentPruneRuntimeResult


TOPOLOGY_COMPOSER_CONFIG_SCHEMA = "zyra.topology-composer-config/v1"
COMPOSER_MECHANISM_ID = "phase2_topology_composer"


class TopologyCompositionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TopologyLayerSwitches:
    arg_enabled: bool = True
    card_enabled: bool = True
    agentprune_enabled: bool = True
    policy_enabled: bool = True

    def enabled(self, mechanism_id: str) -> bool:
        return {
            "arg_designer": self.arg_enabled,
            "card": self.card_enabled,
            "agentprune": self.agentprune_enabled,
        }[mechanism_id]

    def to_dict(self) -> dict[str, bool]:
        return {
            "arg_designer": self.arg_enabled,
            "card": self.card_enabled,
            "agentprune": self.agentprune_enabled,
            "policy": self.policy_enabled,
        }


@dataclass(frozen=True, slots=True)
class TopologyComposerConfig:
    mechanism_id: str
    mechanism_version: str
    profile_id: str
    fallback_profile: str
    required_layers: tuple[str, ...]
    proposal_ttl_seconds: int
    maximum_operations: int
    maximum_commits_per_window: int
    churn_window_seconds: int
    minimum_dwell_seconds: int
    no_policy_training: FrozenDict
    digest: str
    path: str = ""

    @classmethod
    def load(cls, path: Path) -> TopologyComposerConfig:
        selected = path.resolve()
        try:
            value = json.loads(selected.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TopologyCompositionError(
                "composer_config_invalid",
                f"topology composer configuration is missing or corrupt: {selected}",
            ) from exc
        if (
            not isinstance(value, Mapping)
            or value.get("schema") != TOPOLOGY_COMPOSER_CONFIG_SCHEMA
            or value.get("mechanism_id") != COMPOSER_MECHANISM_ID
        ):
            raise TopologyCompositionError(
                "composer_config_schema_invalid",
                "unsupported topology composer configuration",
            )
        required = tuple(str(item) for item in value.get("required_layers") or ())
        if required != ("arg_designer", "card", "agentprune"):
            raise TopologyCompositionError(
                "composer_required_layers_invalid",
                "the strongest topology composer requires ARG, CARD, and AgentPrune in order",
            )
        no_training = value.get("no_policy_training")
        if not isinstance(no_training, Mapping) or (
            no_training.get("training_allowed") is not False
            or no_training.get("sampling_allowed") is not False
            or no_training.get("policy_gradient") is not False
            or no_training.get("mutable_learned_parameters") not in ([], ())
            or no_training.get("datasets") not in ([], ())
            or no_training.get("checkpoints") not in ([], ())
        ):
            raise TopologyCompositionError(
                "composer_policy_training_forbidden",
                "topology composition must remain deterministic and training-free",
            )
        positive = {
            name: int(value.get(name) or 0)
            for name in (
                "proposal_ttl_seconds",
                "maximum_operations",
                "maximum_commits_per_window",
                "churn_window_seconds",
                "minimum_dwell_seconds",
            )
        }
        if any(item <= 0 for item in positive.values()):
            raise TopologyCompositionError(
                "composer_limits_invalid",
                "topology composer limits must be positive",
            )
        return cls(
            mechanism_id=COMPOSER_MECHANISM_ID,
            mechanism_version=_required(
                value.get("mechanism_version"),
                "mechanism_version",
            ),
            profile_id=_required(value.get("profile_id"), "profile_id"),
            fallback_profile=_required(
                value.get("fallback_profile"),
                "fallback_profile",
            ),
            required_layers=required,
            proposal_ttl_seconds=positive["proposal_ttl_seconds"],
            maximum_operations=positive["maximum_operations"],
            maximum_commits_per_window=positive[
                "maximum_commits_per_window"
            ],
            churn_window_seconds=positive["churn_window_seconds"],
            minimum_dwell_seconds=positive["minimum_dwell_seconds"],
            no_policy_training=FrozenDict(no_training),
            digest=canonical_digest(value),
            path=str(selected),
        )


@dataclass(frozen=True, slots=True)
class LayerCompositionRecord:
    mechanism_id: str
    mode: str
    readiness_stage: str
    readiness_status: str
    readiness_report_digest: str
    proposal_id: str
    proposal_digest: str
    enabled: bool
    affected_commit: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mechanism_id": self.mechanism_id,
            "mode": self.mode,
            "readiness_stage": self.readiness_stage,
            "readiness_status": self.readiness_status,
            "readiness_report_digest": self.readiness_report_digest,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "enabled": self.enabled,
            "affected_commit": self.affected_commit,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class TopologyCompositionResult:
    proposal: TopologyProposalArtifact | None
    layers: tuple[LayerCompositionRecord, ...]
    projection_differences: tuple[str, ...]
    effective_spatial_edge_ids: tuple[str, ...]
    effective_temporal_edge_ids: tuple[str, ...]
    degraded: bool
    degraded_reason: str
    fallback_profile: str
    replan_required: bool
    events: tuple[EventRecord, ...]

    @property
    def commit_eligible(self) -> bool:
        return self.proposal is not None and not self.degraded

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal": (
                self.proposal.to_dict()
                if self.proposal is not None
                else None
            ),
            "layers": [item.to_dict() for item in self.layers],
            "projection_differences": list(self.projection_differences),
            "effective_spatial_edge_ids": list(
                self.effective_spatial_edge_ids
            ),
            "effective_temporal_edge_ids": list(
                self.effective_temporal_edge_ids
            ),
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "fallback_profile": self.fallback_profile,
            "replan_required": self.replan_required,
            "commit_eligible": self.commit_eligible,
        }


class TopologyPolicyComposer:
    """Combines typed layer proposals without owning canonical graph state."""

    def __init__(self, config: TopologyComposerConfig) -> None:
        self.config = config

    def compose(
        self,
        *,
        policy_input: PolicyInputSnapshot,
        current_graph: GraphStateSnapshot,
        arg_result: ARGTopologyRuntimeResult,
        card_result: CARDTopologyRuntimeResult,
        pruning_result: AgentPruneRuntimeResult,
        switches: TopologyLayerSwitches | None = None,
    ) -> TopologyCompositionResult:
        selected_switches = switches or TopologyLayerSwitches()
        events: list[EventRecord] = []
        if not selected_switches.policy_enabled:
            return self._degraded(
                policy_input=policy_input,
                current_graph=current_graph,
                results=(arg_result, card_result, pruning_result),
                switches=selected_switches,
                reason="phase2_topology_policy_disabled",
                replan_required=False,
                events=events,
            )
        try:
            self._validate_snapshot(
                policy_input=policy_input,
                current_graph=current_graph,
                arg_result=arg_result,
                card_result=card_result,
                pruning_result=pruning_result,
            )
            records = self._layer_records(
                arg_result=arg_result,
                card_result=card_result,
                pruning_result=pruning_result,
                switches=selected_switches,
            )
            blockers = tuple(
                item
                for item in records
                if (
                    not item.enabled
                    or item.readiness_status != "deterministic_ready"
                    or item.readiness_stage
                    not in {"implementation_validated", "activation_ready"}
                    or item.mode not in {"validation", "default"}
                )
            )
            if blockers:
                blocker_reason = next(
                    (
                        item.reason
                        for item in blockers
                        if item.reason
                        not in {
                            "eligible",
                            "required_layer_disabled",
                            "diagnostic_or_degraded_output_does_not_affect_commit",
                        }
                    ),
                    "",
                )
                raise TopologyCompositionError(
                    blocker_reason
                    or "strongest_required_layer_not_ready",
                    "required topology layers cannot influence the commit: "
                    + ", ".join(item.mechanism_id for item in blockers),
                )
            if (
                arg_result.proposal is None
                or card_result.correction is None
                or pruning_result.mask is None
            ):
                raise TopologyCompositionError(
                    "composer_layer_output_missing",
                    "the strongest topology composer requires all three typed outputs",
                )
            operations, differences = self._merge_operations(
                current_graph=current_graph,
                arg_proposal=arg_result.proposal,
                card_result=card_result,
                pruning_result=pruning_result,
            )
            if len(operations) > min(
                self.config.maximum_operations,
                policy_input.budget.max_topology_churn,
            ):
                raise TopologyCompositionError(
                    "composer_churn_limit",
                    "composed topology exceeds the configured churn limit",
                )
            proposal = self._proposal(
                policy_input=policy_input,
                arg_result=arg_result,
                card_result=card_result,
                pruning_result=pruning_result,
                operations=operations,
                differences=differences,
                records=records,
            )
        except TopologyCompositionError as exc:
            return self._degraded(
                policy_input=policy_input,
                current_graph=current_graph,
                results=(arg_result, card_result, pruning_result),
                switches=selected_switches,
                reason=exc.code,
                replan_required=exc.code
                in {
                    "composer_snapshot_drift",
                    "composer_mixed_snapshot",
                    "composer_layer_canonical_mutation",
                },
                events=events,
            )
        event = EventRecord(
            run_id=policy_input.run_id,
            task_id=policy_input.task_id,
            event_id="event_topology_composed_" + proposal.digest[:24],
            event_type=EventType.TOPOLOGY_ROUTE,
            payload={
                "schema": "zyra.topology-composition/v1",
                "profile_id": self.config.profile_id,
                "mechanism_id": self.config.mechanism_id,
                "mechanism_version": self.config.mechanism_version,
                "policy_input_digest": policy_input.digest,
                "proposal_id": proposal.proposal_id,
                "proposal_digest": proposal.digest,
                "layer_records": [item.to_dict() for item in records],
                "projection_differences": list(differences),
                "operation_count": len(operations),
                "canonical_mutation_attempted": False,
                "fallback_profile": self.config.fallback_profile,
            },
        )
        events.append(event)
        return TopologyCompositionResult(
            proposal=proposal,
            layers=records,
            projection_differences=differences,
            effective_spatial_edge_ids=(
                pruning_result.effective_spatial_edge_ids
            ),
            effective_temporal_edge_ids=(
                pruning_result.effective_temporal_edge_ids
            ),
            degraded=False,
            degraded_reason="",
            fallback_profile=self.config.fallback_profile,
            replan_required=False,
            events=tuple(events),
        )

    def _merge_operations(
        self,
        *,
        current_graph: GraphStateSnapshot,
        arg_proposal: TopologyProposalArtifact,
        card_result: CARDTopologyRuntimeResult,
        pruning_result: AgentPruneRuntimeResult,
    ) -> tuple[tuple[TopologyOperation, ...], tuple[str, ...]]:
        operations = {
            (item.kind, item.entity_id): item
            for item in arg_proposal.operations
        }
        differences: list[str] = []
        final_nodes = set(current_graph.node_map)
        for operation in arg_proposal.operations:
            if operation.kind is TopologyOperationKind.REMOVE_NODE:
                final_nodes.discard(operation.entity_id)
            elif operation.kind in {
                TopologyOperationKind.ADD_NODE,
                TopologyOperationKind.REPLACE_NODE,
            }:
                final_nodes.add(operation.entity_id)

        base_edge_operations = {
            item.entity_id: item
            for item in arg_proposal.operations
            if item.kind
            in {
                TopologyOperationKind.ADD_EDGE,
                TopologyOperationKind.REPLACE_EDGE,
                TopologyOperationKind.REMOVE_EDGE,
            }
        }
        base_edge_ids = set(current_graph.edge_map) | set(
            base_edge_operations
        )
        correction = card_result.correction
        if correction is None:
            raise TopologyCompositionError(
                "composer_card_correction_missing",
                "CARD correction is required",
            )
        for decision in correction.decisions:
            edge_id = decision.edge_id
            if edge_id not in base_edge_ids and not decision.candidate:
                differences.append(
                    f"card_edge_removed_without_arg_base:{edge_id}"
                )
                continue
            if decision.action == "drop":
                self._drop_edge(
                    operations,
                    current_graph=current_graph,
                    edge_id=edge_id,
                    reason=(
                        "CARD forbidden edge overrides downstream pruning keep"
                    ),
                )
                differences.append(f"card_drop:{edge_id}")
            elif decision.action in {"reweight", "add"}:
                card_operation = self._card_operation(
                    card_result,
                    edge_id,
                )
                if card_operation is None:
                    continue
                existing = base_edge_operations.get(edge_id)
                if (
                    existing is not None
                    and existing.kind is TopologyOperationKind.ADD_EDGE
                ):
                    operations.pop((existing.kind, edge_id), None)
                    operations[
                        (TopologyOperationKind.ADD_EDGE, edge_id)
                    ] = self._overlay_edge(existing, card_operation)
                    differences.append(f"card_reweight_arg_add:{edge_id}")
                elif decision.action == "add":
                    operations[(card_operation.kind, edge_id)] = card_operation
                    differences.append(f"card_add:{edge_id}")
                elif edge_id in current_graph.edge_map:
                    current = current_graph.edge_map[edge_id]
                    operations[
                        (TopologyOperationKind.REPLACE_EDGE, edge_id)
                    ] = replace(
                        card_operation,
                        kind=TopologyOperationKind.REPLACE_EDGE,
                        expected_entity_revision=current.revision,
                    )
                    differences.append(f"card_reweight_canonical:{edge_id}")

        mask = pruning_result.mask
        if mask is None:
            raise TopologyCompositionError(
                "composer_pruning_mask_missing",
                "AgentPrune mask is required",
            )
        for decision in mask.decisions:
            edge_id = decision.edge.edge_id
            if decision.keep:
                continue
            if decision.hard_constraints:
                raise TopologyCompositionError(
                    "composer_protected_edge_drop",
                    f"AgentPrune attempted to drop protected edge {edge_id}",
                )
            self._drop_edge(
                operations,
                current_graph=current_graph,
                edge_id=edge_id,
                reason="AgentPrune deterministic communication mask drop",
            )
            differences.append(f"agentprune_drop:{edge_id}")

        for key, operation in tuple(operations.items()):
            if operation.kind not in {
                TopologyOperationKind.ADD_EDGE,
                TopologyOperationKind.REPLACE_EDGE,
            }:
                continue
            value = thaw_json(operation.value)
            source = str(value.get("source_node_id") or "")
            target = str(value.get("target_node_id") or "")
            if source not in final_nodes or target not in final_nodes:
                operations.pop(key)
                differences.append(
                    f"edge_removed_missing_arg_role_node:{operation.entity_id}"
                )
        selected = tuple(
            sorted(
                operations.values(),
                key=lambda item: (item.kind.value, item.entity_id),
            )
        )
        if not selected:
            raise TopologyCompositionError(
                "composer_no_effect",
                "composition contains no canonical graph operation",
            )
        return selected, tuple(sorted(set(differences)))

    @staticmethod
    def _drop_edge(
        operations: dict[
            tuple[TopologyOperationKind, str],
            TopologyOperation,
        ],
        *,
        current_graph: GraphStateSnapshot,
        edge_id: str,
        reason: str,
    ) -> None:
        removed_add = operations.pop(
            (TopologyOperationKind.ADD_EDGE, edge_id),
            None,
        )
        operations.pop(
            (TopologyOperationKind.REPLACE_EDGE, edge_id),
            None,
        )
        if removed_add is not None or edge_id not in current_graph.edge_map:
            return
        current = current_graph.edge_map[edge_id]
        operations[
            (TopologyOperationKind.REMOVE_EDGE, edge_id)
        ] = TopologyOperation(
            kind=TopologyOperationKind.REMOVE_EDGE,
            entity_id=edge_id,
            expected_entity_revision=current.revision,
            required_permissions=("graph.write",),
            reason=reason,
        )

    @staticmethod
    def _card_operation(
        card_result: CARDTopologyRuntimeResult,
        edge_id: str,
    ) -> TopologyOperation | None:
        proposal = card_result.correction_proposal
        if proposal is None:
            return None
        return next(
            (
                item
                for item in proposal.operations
                if item.entity_id == edge_id
            ),
            None,
        )

    @staticmethod
    def _overlay_edge(
        base: TopologyOperation,
        residual: TopologyOperation,
    ) -> TopologyOperation:
        left = thaw_json(base.value)
        right = thaw_json(residual.value)
        merged = {
            **left,
            "source_node_id": left.get("source_node_id")
            or right.get("source_node_id"),
            "target_node_id": left.get("target_node_id")
            or right.get("target_node_id"),
            "relation": left.get("relation") or right.get("relation"),
            "required_capabilities": left.get("required_capabilities")
            or right.get("required_capabilities")
            or [],
            "condition": {
                **dict(left.get("condition") or {}),
                **dict(right.get("condition") or {}),
            },
            "labels": {
                **dict(left.get("labels") or {}),
                **dict(right.get("labels") or {}),
            },
            "metadata": {
                **dict(left.get("metadata") or {}),
                **dict(right.get("metadata") or {}),
            },
        }
        return replace(
            base,
            value=FrozenDict(merged),
            requested_placement=(
                residual.requested_placement
                or base.requested_placement
            ),
            resource_id=residual.resource_id or base.resource_id,
            required_capacity=max(
                base.required_capacity,
                residual.required_capacity,
            ),
            communication_bytes=max(
                base.communication_bytes,
                residual.communication_bytes,
            ),
            reason=f"{base.reason}; CARD residual: {residual.reason}",
        )

    def _proposal(
        self,
        *,
        policy_input: PolicyInputSnapshot,
        arg_result: ARGTopologyRuntimeResult,
        card_result: CARDTopologyRuntimeResult,
        pruning_result: AgentPruneRuntimeResult,
        operations: tuple[TopologyOperation, ...],
        differences: tuple[str, ...],
        records: tuple[LayerCompositionRecord, ...],
    ) -> TopologyProposalArtifact:
        layer_payload = [item.to_dict() for item in records]
        proposal_id = "topology_composer_" + canonical_digest(
            (
                policy_input.digest,
                [item["proposal_digest"] for item in layer_payload],
                [item.to_dict() for item in operations],
                self.config.digest,
            )
        )[:24]
        created = datetime.fromisoformat(
            policy_input.header.created_at.replace("Z", "+00:00")
        ).astimezone(UTC)
        pruning_mask = pruning_result.mask
        expected_savings = (
            {}
            if pruning_mask is None
            else {
                key: float(pruning_mask.baseline_totals.get(key) or 0)
                - float(pruning_mask.retained_totals.get(key) or 0)
                for key in ("messages", "bytes", "tokens", "cost_usd")
            }
        )
        expected_tokens = max(
            0,
            int(
                (arg_result.proposal or _missing()).expected_outcome.get(
                    "tokens"
                )
                or 0
            ),
        )
        return TopologyProposalArtifact(
            header=ContractHeader(
                contract_id=proposal_id,
                created_at=policy_input.header.created_at,
                source_event_id=policy_input.header.source_event_id,
                correlation_id=policy_input.header.correlation_id,
                causation_id=policy_input.header.contract_id,
                mechanism_id=self.config.mechanism_id,
                mechanism_version=self.config.mechanism_version,
                input_version=PolicyInputSnapshot.SCHEMA_VERSION,
                idempotency_key=(
                    f"topology-composer:{policy_input.run_id}:"
                    f"{policy_input.graph.revision}:{proposal_id}"
                ),
                configuration_digest=self.config.digest,
            ),
            proposal_id=proposal_id,
            input_snapshot_digest=policy_input.digest,
            base_graph=policy_input.graph,
            operations=operations,
            expected_outcome=FrozenDict(
                {
                    "profile_id": self.config.profile_id,
                    "layer_readiness": layer_payload,
                    "arg_proposal_digest": (
                        arg_result.proposal.digest
                        if arg_result.proposal is not None
                        else ""
                    ),
                    "card_correction_digest": (
                        card_result.correction.digest
                        if card_result.correction is not None
                        else ""
                    ),
                    "agentprune_mask_digest": (
                        pruning_result.mask.digest
                        if pruning_result.mask is not None
                        else ""
                    ),
                    "effective_spatial_edge_ids": list(
                        pruning_result.effective_spatial_edge_ids
                    ),
                    "effective_temporal_edge_ids": list(
                        pruning_result.effective_temporal_edge_ids
                    ),
                    "projection_differences": list(differences),
                    "expected_savings": expected_savings,
                    "expected_savings_are_counterfactual": True,
                    "tokens": expected_tokens,
                    "cost_usd": 0.0,
                    "time_ms": len(operations),
                    "pending_side_effects": [],
                    "proposal_signal_mode": "deterministic_only",
                }
            ),
            alternatives=_alternatives(
                arg_result.proposal,
                card_result.correction_proposal,
                pruning_result.pruning_proposal,
            ),
            reasons=tuple(
                (
                    f"{item.mechanism_id} {item.readiness_stage}/"
                    f"{item.readiness_status} affected_commit="
                    f"{str(item.affected_commit).lower()}"
                )
                for item in records
            ),
            constraint_assumptions=(
                "ARG owns the joint role-node-edge base proposal",
                "CARD only corrects ARG edges and forbidden edges override pruning keep",
                "AgentPrune cannot drop critical obligation, continuity, recovery, unique evidence, or verifier edges",
                "TopologyConstraintProjector and PolicyDeltaBuilder are the only proposal-to-delta path",
                "GraphStateCustody remains the sole canonical graph commit owner",
                "implementation_validated layers may commit only in an explicit isolated validation run",
            ),
            expires_at=(
                created
                + timedelta(seconds=self.config.proposal_ttl_seconds)
            ).isoformat().replace("+00:00", "Z"),
            fallback_profile=self.config.fallback_profile,
        )

    @staticmethod
    def _validate_snapshot(
        *,
        policy_input: PolicyInputSnapshot,
        current_graph: GraphStateSnapshot,
        arg_result: ARGTopologyRuntimeResult,
        card_result: CARDTopologyRuntimeResult,
        pruning_result: AgentPruneRuntimeResult,
    ) -> None:
        if (
            policy_input.graph.graph_id != current_graph.graph_id
            or policy_input.graph.revision != current_graph.revision
            or policy_input.graph.signature != current_graph.signature
        ):
            raise TopologyCompositionError(
                "composer_snapshot_drift",
                "canonical graph head differs from the policy input snapshot",
            )
        if not all(
            (
                arg_result.canonical_graph_unchanged,
                card_result.canonical_graph_unchanged,
                pruning_result.canonical_graph_unchanged,
            )
        ):
            raise TopologyCompositionError(
                "composer_layer_canonical_mutation",
                "a proposal layer changed canonical graph state",
            )
        proposals = tuple(
            item
            for item in (
                arg_result.proposal,
                card_result.correction_proposal,
                pruning_result.pruning_proposal,
            )
            if item is not None
        )
        if any(
            item.input_snapshot_digest != policy_input.digest
            or item.base_graph != policy_input.graph
            for item in proposals
        ):
            raise TopologyCompositionError(
                "composer_mixed_snapshot",
                "layer outputs belong to different snapshots or graph revisions",
            )
        if card_result.arg_base_proposal_digest != (
            arg_result.proposal.digest if arg_result.proposal else ""
        ):
            raise TopologyCompositionError(
                "composer_arg_card_lineage_drift",
                "CARD did not consume the selected ARG proposal",
            )
        expected_upstream = (
            card_result.correction_proposal
            if card_result.correction_proposal is not None
            else arg_result.proposal
        )
        if (
            expected_upstream is None
            or pruning_result.upstream_proposal_digest
            != expected_upstream.digest
        ):
            raise TopologyCompositionError(
                "composer_card_pruning_lineage_drift",
                "AgentPrune did not consume the selected CARD/ARG proposal",
            )

    @staticmethod
    def _layer_records(
        *,
        arg_result: ARGTopologyRuntimeResult,
        card_result: CARDTopologyRuntimeResult,
        pruning_result: AgentPruneRuntimeResult,
        switches: TopologyLayerSwitches,
    ) -> tuple[LayerCompositionRecord, ...]:
        values = (
            (
                "arg_designer",
                arg_result.mode,
                arg_result.readiness,
                arg_result.proposal,
                not arg_result.degraded,
                arg_result.degraded_reason,
            ),
            (
                "card",
                card_result.mode,
                card_result.readiness,
                card_result.correction_proposal,
                card_result.composer_residual_eligible,
                card_result.degraded_reason,
            ),
            (
                "agentprune",
                pruning_result.mode,
                pruning_result.readiness,
                pruning_result.pruning_proposal,
                pruning_result.composer_pruning_eligible,
                pruning_result.degraded_reason,
            ),
        )
        return tuple(
            LayerCompositionRecord(
                mechanism_id=mechanism_id,
                mode=mode,
                readiness_stage=readiness.stage,
                readiness_status=readiness.status,
                readiness_report_digest=readiness.report_digest,
                proposal_id=proposal.proposal_id if proposal else "",
                proposal_digest=proposal.digest if proposal else "",
                enabled=switches.enabled(mechanism_id),
                affected_commit=(
                    switches.enabled(mechanism_id)
                    and eligible
                    and readiness.status == "deterministic_ready"
                    and readiness.stage
                    in {"implementation_validated", "activation_ready"}
                ),
                reason=(
                    (
                        degraded_reason
                        or (
                            "required_layer_disabled"
                            if not switches.enabled(mechanism_id)
                            else "diagnostic_or_degraded_output_does_not_affect_commit"
                        )
                    )
                    if not eligible
                    else "eligible"
                ),
            )
            for (
                mechanism_id,
                mode,
                readiness,
                proposal,
                eligible,
                degraded_reason,
            ) in values
        )

    def _degraded(
        self,
        *,
        policy_input: PolicyInputSnapshot,
        current_graph: GraphStateSnapshot,
        results: Sequence[Any],
        switches: TopologyLayerSwitches,
        reason: str,
        replan_required: bool,
        events: list[EventRecord],
    ) -> TopologyCompositionResult:
        arg_result, card_result, pruning_result = results
        records = self._layer_records(
            arg_result=arg_result,
            card_result=card_result,
            pruning_result=pruning_result,
            switches=switches,
        )
        event = EventRecord(
            run_id=policy_input.run_id,
            task_id=policy_input.task_id,
            event_id="event_topology_composer_degraded_" + canonical_digest(
                (
                    policy_input.digest,
                    current_graph.signature,
                    reason,
                    switches.to_dict(),
                )
            )[:24],
            event_type=EventType.RECOVERY_PLANNED,
            payload={
                "schema": "zyra.topology-composition-degraded/v1",
                "mechanism_id": self.config.mechanism_id,
                "reason": reason,
                "fallback_profile": self.config.fallback_profile,
                "silent_fallback": False,
                "replan_required": replan_required,
                "layer_records": [item.to_dict() for item in records],
                "canonical_mutation_attempted": False,
                "graph_revision": current_graph.revision,
            },
        )
        events.append(event)
        return TopologyCompositionResult(
            proposal=None,
            layers=records,
            projection_differences=(),
            effective_spatial_edge_ids=(),
            effective_temporal_edge_ids=(),
            degraded=True,
            degraded_reason=reason,
            fallback_profile=self.config.fallback_profile,
            replan_required=replan_required,
            events=tuple(events),
        )


def _alternatives(
    *proposals: TopologyProposalArtifact | None,
) -> tuple[StableArtifactRef, ...]:
    values: list[StableArtifactRef] = []
    for proposal in proposals:
        if proposal is None:
            continue
        values.append(
            StableArtifactRef(
                ref_id=proposal.proposal_id,
                uri=f"urn:zyra:topology-proposal:{proposal.proposal_id}",
                digest=proposal.digest,
                media_type="application/vnd.zyra.topology-proposal+json",
            )
        )
    return tuple(values)


def _required(value: Any, label: str) -> str:
    selected = str(value or "").strip()
    if not selected:
        raise TopologyCompositionError(
            "composer_config_value_missing",
            f"{label} is required",
        )
    return selected


def _missing() -> Any:
    raise TopologyCompositionError(
        "composer_arg_proposal_missing",
        "ARG proposal is required",
    )


__all__ = [
    "COMPOSER_MECHANISM_ID",
    "TOPOLOGY_COMPOSER_CONFIG_SCHEMA",
    "LayerCompositionRecord",
    "TopologyComposerConfig",
    "TopologyCompositionError",
    "TopologyCompositionResult",
    "TopologyLayerSwitches",
    "TopologyPolicyComposer",
]
