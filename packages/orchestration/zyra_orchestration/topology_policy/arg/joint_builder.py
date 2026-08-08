from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

from ...graph_custody import GraphMutationKind
from ..contracts import (
    ContractHeader,
    FrozenDict,
    StableArtifactRef,
    TopologyOperation,
    TopologyOperationKind,
    TopologyProposalArtifact,
    canonical_digest,
)
from .encoder import ARGEncodedInput, ARGEncodedRole


ARG_CONFIG_SCHEMA = "zyra.arg-joint-topology-config/v1"


class ARGBuilderError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _tokens(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        selected: Sequence[Any] = (value,)
    elif isinstance(value, Mapping):
        selected = tuple(value.keys())
    else:
        selected = tuple(value)
    return tuple(sorted({str(item).strip().lower() for item in selected if str(item).strip()}))


def _round(value: float) -> float:
    return round(float(value), 8)


@dataclass(frozen=True, slots=True)
class ARGJointBuilderConfig:
    mechanism_id: str
    mechanism_version: str
    catalog_schema_version: str
    input_schema_version: str
    start_token: str
    end_token: str
    fallback_profile: str
    input_precheck_report_digest: str
    candidate_cap: int
    beam_cap: int
    minimum_nodes: int
    maximum_nodes: int
    maximum_incident_edges_per_node: int
    proposal_ttl_seconds: int
    estimated_node_tokens: int
    estimated_edge_tokens: int
    estimated_node_time_ms: int
    estimated_edge_time_ms: int
    estimated_edge_bytes: int
    score_weights: FrozenDict
    phase_capability_affinity: FrozenDict
    critical_path_capabilities: tuple[str, ...]
    recovery_capabilities: tuple[str, ...]
    no_policy_training: FrozenDict
    digest: str
    path: str = ""

    @classmethod
    def load(cls, path: Path) -> ARGJointBuilderConfig:
        selected_path = path.resolve()
        try:
            value = json.loads(selected_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ARGBuilderError(
                "arg_config_invalid",
                f"ARG configuration is missing or corrupt: {selected_path}",
            ) from exc
        if not isinstance(value, Mapping) or value.get("schema") != ARG_CONFIG_SCHEMA:
            raise ARGBuilderError(
                "arg_config_schema_invalid",
                "unsupported ARG joint topology configuration",
            )
        no_training = value.get("no_policy_training")
        if not isinstance(no_training, Mapping):
            raise ARGBuilderError(
                "arg_config_training_declaration_missing",
                "ARG configuration requires a no-policy-training declaration",
            )
        if (
            no_training.get("training_allowed") is not False
            or no_training.get("sampling_allowed") is not False
            or no_training.get("datasets") not in ([], ())
            or no_training.get("checkpoints") not in ([], ())
            or no_training.get("mutable_learned_parameters") not in ([], ())
        ):
            raise ARGBuilderError(
                "arg_config_training_forbidden",
                "ARG configuration cannot enable sampling, training, datasets, checkpoints, or learned parameters",
            )
        weights = value.get("score_weights")
        phases = value.get("phase_capability_affinity")
        if not isinstance(weights, Mapping) or not isinstance(phases, Mapping):
            raise ARGBuilderError(
                "arg_config_scoring_invalid",
                "ARG scoring weights and phase affinity are required",
            )
        required_weights = {
            "obligation_coverage",
            "capability_fit",
            "dependency_reachability",
            "critical_path",
            "communication_cost",
            "switch_cost",
            "recovery_value",
        }
        if set(weights) != required_weights or any(float(item) < 0 for item in weights.values()):
            raise ARGBuilderError(
                "arg_config_scoring_invalid",
                "ARG scoring weights must be the fixed non-negative seven-term set",
            )
        candidate_cap = int(value.get("candidate_cap") or 0)
        beam_cap = int(value.get("beam_cap") or 0)
        minimum_nodes = int(value.get("minimum_nodes") or 0)
        maximum_nodes = int(value.get("maximum_nodes") or 0)
        if (
            candidate_cap < 1
            or beam_cap < 1
            or minimum_nodes < 1
            or maximum_nodes < minimum_nodes
            or maximum_nodes > candidate_cap
        ):
            raise ARGBuilderError(
                "arg_config_search_bounds_invalid",
                "ARG candidate, beam, and node caps are invalid",
            )
        report_digest = str(value.get("input_precheck_report_digest") or "")
        if len(report_digest) != 64:
            raise ARGBuilderError(
                "arg_config_readiness_digest_invalid",
                "ARG input-precheck report digest must be SHA-256",
            )
        return cls(
            mechanism_id=str(value.get("mechanism_id") or ""),
            mechanism_version=str(value.get("mechanism_version") or ""),
            catalog_schema_version=str(value.get("catalog_schema_version") or ""),
            input_schema_version=str(value.get("input_schema_version") or ""),
            start_token=str(value.get("start_token") or ""),
            end_token=str(value.get("end_token") or ""),
            fallback_profile=str(value.get("fallback_profile") or ""),
            input_precheck_report_digest=report_digest,
            candidate_cap=candidate_cap,
            beam_cap=beam_cap,
            minimum_nodes=minimum_nodes,
            maximum_nodes=maximum_nodes,
            maximum_incident_edges_per_node=max(
                1, int(value.get("maximum_incident_edges_per_node") or 1)
            ),
            proposal_ttl_seconds=max(1, int(value.get("proposal_ttl_seconds") or 1)),
            estimated_node_tokens=max(0, int(value.get("estimated_node_tokens") or 0)),
            estimated_edge_tokens=max(0, int(value.get("estimated_edge_tokens") or 0)),
            estimated_node_time_ms=max(0, int(value.get("estimated_node_time_ms") or 0)),
            estimated_edge_time_ms=max(0, int(value.get("estimated_edge_time_ms") or 0)),
            estimated_edge_bytes=max(0, int(value.get("estimated_edge_bytes") or 0)),
            score_weights=FrozenDict(
                {key: float(item) for key, item in weights.items()}
            ),
            phase_capability_affinity=FrozenDict(phases),
            critical_path_capabilities=_tokens(value.get("critical_path_capabilities")),
            recovery_capabilities=_tokens(value.get("recovery_capabilities")),
            no_policy_training=FrozenDict(no_training),
            digest=canonical_digest(value),
            path=selected_path.as_posix(),
        )


@dataclass(frozen=True, slots=True)
class ARGScoreComponents:
    obligation_coverage: float
    capability_fit: float
    dependency_reachability: float
    critical_path: float
    communication_cost: float
    switch_cost: float
    recovery_value: float
    total: float

    def to_dict(self) -> dict[str, float]:
        return {
            "obligation_coverage": _round(self.obligation_coverage),
            "capability_fit": _round(self.capability_fit),
            "dependency_reachability": _round(self.dependency_reachability),
            "critical_path": _round(self.critical_path),
            "communication_cost": _round(self.communication_cost),
            "switch_cost": _round(self.switch_cost),
            "recovery_value": _round(self.recovery_value),
            "total": _round(self.total),
        }


@dataclass(frozen=True, slots=True)
class ARGIncidentEdge:
    source_node_id: str
    target_node_id: str
    relation: str
    persisted: bool
    required_capabilities: tuple[str, ...]
    reason: str

    @property
    def edge_id(self) -> str:
        return "arg_edge_" + canonical_digest(
            (
                self.source_node_id,
                self.target_node_id,
                self.relation,
                self.required_capabilities,
            )
        )[:20]

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "relation": self.relation,
            "persisted": self.persisted,
            "required_capabilities": list(self.required_capabilities),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ARGJointStep:
    step_index: int
    token: str
    role_id: str
    node_id: str
    binding_id: str
    worker_id: str
    capabilities: tuple[str, ...]
    incident_edges: tuple[ARGIncidentEdge, ...]
    score: ARGScoreComponents
    state_digest: str
    cold_start: bool
    reasons: tuple[str, ...]

    @property
    def is_end(self) -> bool:
        return not self.role_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_index": self.step_index,
            "token": self.token,
            "role_id": self.role_id,
            "node_id": self.node_id,
            "binding_id": self.binding_id,
            "worker_id": self.worker_id,
            "capabilities": list(self.capabilities),
            "incident_edges": [item.to_dict() for item in self.incident_edges],
            "score": self.score.to_dict(),
            "state_digest": self.state_digest,
            "cold_start": self.cold_start,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class ARGJointHypothesis:
    hypothesis_id: str
    start_token: str
    end_token: str
    steps: tuple[ARGJointStep, ...]
    covered_obligations: tuple[str, ...]
    uncovered_obligations: tuple[str, ...]
    total_score: float
    end_reason: str

    @property
    def role_steps(self) -> tuple[ARGJointStep, ...]:
        return tuple(item for item in self.steps if not item.is_end)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "start_token": self.start_token,
            "end_token": self.end_token,
            "steps": [item.to_dict() for item in self.steps],
            "covered_obligations": list(self.covered_obligations),
            "uncovered_obligations": list(self.uncovered_obligations),
            "total_score": _round(self.total_score),
            "end_reason": self.end_reason,
        }


@dataclass(frozen=True, slots=True)
class ARGJointBuildResult:
    proposal: TopologyProposalArtifact
    hypothesis: ARGJointHypothesis
    alternatives: tuple[ARGJointHypothesis, ...]


@dataclass(frozen=True, slots=True)
class _BeamState:
    steps: tuple[ARGJointStep, ...]
    used_roles: tuple[str, ...]
    covered_obligations: tuple[str, ...]
    total_score: float
    state_digest: str

    @property
    def selected_node_ids(self) -> tuple[str, ...]:
        return tuple(item.node_id for item in self.steps)


class ARGJointBuilder:
    """Cropped ARG control flow with deterministic joint role-node-edge expansion."""

    def __init__(self, config: ARGJointBuilderConfig) -> None:
        self.config = config

    def build(self, encoded: ARGEncodedInput) -> ARGJointBuildResult:
        if encoded.mechanism_version != self.config.mechanism_version:
            raise ARGBuilderError(
                "arg_mechanism_version_drift",
                "encoded ARG mechanism version differs from configuration",
            )
        if encoded.configuration_digest != self.config.digest:
            raise ARGBuilderError(
                "arg_configuration_digest_drift",
                "encoded ARG configuration digest differs from installed configuration",
            )
        candidates = tuple(
            sorted(
                encoded.eligible_roles,
                key=lambda item: (
                    -self._role_pre_score(encoded, item),
                    item.role_id,
                    item.binding.binding_id,
                ),
            )[: self.config.candidate_cap]
        )
        if not candidates:
            raise ARGBuilderError(
                "arg_candidate_empty",
                "ARG has no deterministic capability-bound candidate",
            )
        target_nodes = min(
            self.config.maximum_nodes,
            max(
                self.config.minimum_nodes,
                min(len(candidates), max(1, len(encoded.policy_input.unresolved_obligations))),
            ),
        )
        initial_digest = canonical_digest(
            {
                "input_digest": encoded.digest,
                "start_token": self.config.start_token,
                "candidate_roles": [item.role_id for item in candidates],
            }
        )
        beams = (
            _BeamState(
                steps=(),
                used_roles=(),
                covered_obligations=(),
                total_score=0.0,
                state_digest=initial_digest,
            ),
        )
        for step_index in range(target_nodes):
            expanded: list[_BeamState] = []
            for state in beams:
                for role in candidates:
                    if role.role_id in state.used_roles:
                        continue
                    step = self._expand_step(
                        encoded,
                        state,
                        role,
                        step_index,
                        candidates=candidates,
                    )
                    covered = tuple(
                        sorted(
                            set(state.covered_obligations).union(
                                role.obligation_matches
                            )
                        )
                    )
                    expanded.append(
                        _BeamState(
                            steps=(*state.steps, step),
                            used_roles=(*state.used_roles, role.role_id),
                            covered_obligations=covered,
                            total_score=_round(state.total_score + step.score.total),
                            state_digest=step.state_digest,
                        )
                    )
            if not expanded:
                break
            beams = tuple(
                sorted(
                    expanded,
                    key=lambda item: (
                        -item.total_score,
                        -len(item.covered_obligations),
                        item.used_roles,
                        item.selected_node_ids,
                        item.state_digest,
                    ),
                )[: self.config.beam_cap]
            )
        if not beams or not beams[0].steps:
            raise ARGBuilderError(
                "arg_expansion_empty",
                "ARG joint expansion produced no role-node-edge step",
            )

        ended = tuple(self._end_hypothesis(encoded, item, candidates) for item in beams)
        ranked = tuple(
            sorted(
                ended,
                key=lambda item: (
                    -item.total_score,
                    len(item.uncovered_obligations),
                    tuple(step.role_id for step in item.role_steps),
                    item.hypothesis_id,
                ),
            )
        )
        selected = ranked[0]
        alternatives = ranked[1 : self.config.beam_cap]
        proposal = self._proposal(encoded, selected, alternatives)
        return ARGJointBuildResult(
            proposal=proposal,
            hypothesis=selected,
            alternatives=alternatives,
        )

    def _role_pre_score(self, encoded: ARGEncodedInput, role: ARGEncodedRole) -> float:
        obligation = len(role.obligation_matches) / max(
            1, len(encoded.policy_input.unresolved_obligations)
        )
        phase = len(role.phase_affinity) / max(1, len(encoded.phase_terms))
        recovery = self._recovery_value(encoded, role)
        switch = 1.0 if role.cold_start else 0.0
        weights = self.config.score_weights
        return _round(
            obligation * float(weights["obligation_coverage"])
            + phase * float(weights["capability_fit"])
            + recovery * float(weights["recovery_value"])
            - switch * float(weights["switch_cost"])
        )

    def _role_node_id(
        self,
        encoded: ARGEncodedInput,
        role: ARGEncodedRole,
    ) -> str:
        """Resolve the node id this role expands into.

        A role that already owns a node in the canonical graph re-expands that
        exact node, so a replan revises the committed topology instead of
        minting a parallel one.
        """

        existing = sorted(
            (
                item
                for item in encoded.current_graph.nodes
                if item.role == role.role_id
                and not item.terminal
                and set(role.profile.capabilities).issubset(
                    set(encoded.policy_input.registered_capabilities)
                )
            ),
            key=lambda item: item.node_id,
        )
        if existing:
            return existing[0].node_id
        return "arg_node_" + canonical_digest(
            (
                encoded.policy_input.task_id,
                role.role_id,
                role.binding.manifest_digest,
            )
        )[:20]

    def _expand_step(
        self,
        encoded: ARGEncodedInput,
        state: _BeamState,
        role: ARGEncodedRole,
        step_index: int,
        *,
        candidates: Sequence[ARGEncodedRole],
    ) -> ARGJointStep:
        node_id = self._role_node_id(encoded, role)
        edges = self._incident_edges(
            encoded,
            state,
            role,
            node_id,
            candidates=candidates,
        )
        score = self._score(
            encoded,
            state,
            role,
            edges,
            step_index=step_index,
        )
        reasons = (
            (
                f"covers obligations: {', '.join(role.obligation_matches)}"
                if role.obligation_matches
                else "retained as phase/capability support"
            ),
            (
                f"phase affinity: {', '.join(role.phase_affinity)}"
                if role.phase_affinity
                else "no phase-affinity bonus"
            ),
            f"bound to {role.binding.source_kind} manifest {role.binding.manifest_digest[:16]}",
            f"predecessors derived from dependency/capability/budget: {', '.join(item.source_node_id for item in edges)}",
        )
        state_digest = canonical_digest(
            {
                "previous_state_digest": state.state_digest,
                "step_index": step_index,
                "role_id": role.role_id,
                "node_id": node_id,
                "binding_id": role.binding.binding_id,
                "incident_edges": [item.to_dict() for item in edges],
                "score": score.to_dict(),
            }
        )
        return ARGJointStep(
            step_index=step_index,
            token=role.role_id,
            role_id=role.role_id,
            node_id=node_id,
            binding_id=role.binding.binding_id,
            worker_id=role.binding.worker_id,
            capabilities=role.profile.capabilities,
            incident_edges=edges,
            score=score,
            state_digest=state_digest,
            cold_start=role.cold_start,
            reasons=reasons,
        )

    def _incident_edges(
        self,
        encoded: ARGEncodedInput,
        state: _BeamState,
        role: ARGEncodedRole,
        node_id: str,
        *,
        candidates: Sequence[ARGEncodedRole],
    ) -> tuple[ARGIncidentEdge, ...]:
        # Nodes this proposal has not expanded yet cannot be predecessors.  On a
        # replan the canonical graph already holds the nodes these roles
        # re-expand, and a later step draws its edges from the earlier steps --
        # so accepting one as a predecessor here would add the reverse edge too
        # and reject the whole commit as a dependency cycle.  Emitted steps stay
        # eligible below, which keeps every edge pointing along the beam order.
        emitted_roles = {role.role_id, *state.used_roles}
        deferred = {
            self._role_node_id(encoded, item)
            for item in candidates
            if item.role_id not in emitted_roles
        }
        predecessor_values: list[tuple[float, str, tuple[str, ...], str]] = []
        for predecessor_id, capabilities, branch_local in self._predecessor_nodes(
            encoded
        ):
            if predecessor_id == node_id or predecessor_id in deferred:
                continue
            predecessor_values.append(
                (
                    self._predecessor_score(
                        role,
                        capabilities,
                        predecessor_id,
                        encoded,
                    )
                    + (0.15 if branch_local else 0.0),
                    predecessor_id,
                    capabilities,
                    (
                        f"branch-local node {predecessor_id}"
                        if branch_local
                        else f"canonical node {predecessor_id}"
                    ),
                )
            )
        for predecessor in state.steps:
            if predecessor.node_id == node_id:
                continue
            predecessor_values.append(
                (
                    self._predecessor_score(
                        role,
                        predecessor.capabilities,
                        predecessor.node_id,
                        encoded,
                    )
                    + 0.1,
                    predecessor.node_id,
                    predecessor.capabilities,
                    f"prior joint step {predecessor.step_index}",
                )
            )
        limit = min(
            self.config.maximum_incident_edges_per_node,
            max(1, encoded.policy_input.budget.max_fan_out),
            max(
                1,
                encoded.policy_input.budget.max_communication_bytes
                // max(1, self.config.estimated_edge_bytes),
            ),
        )
        selected: list[ARGIncidentEdge] = []
        seen: set[str] = set()
        for score, predecessor, capabilities, reason in sorted(
            predecessor_values,
            key=lambda item: (-item[0], item[1]),
        ):
            if predecessor in seen:
                continue
            seen.add(predecessor)
            required = tuple(
                sorted(
                    set(role.profile.capabilities).intersection(capabilities)
                    or set(role.profile.capabilities[:1])
                )
            )
            selected.append(
                ARGIncidentEdge(
                    source_node_id=predecessor,
                    target_node_id=node_id,
                    relation="arg_dependency",
                    persisted=True,
                    required_capabilities=required,
                    reason=f"{reason}; predecessor_score={_round(score)}",
                )
            )
            if len(selected) >= limit:
                break
        if not selected:
            selected.append(
                ARGIncidentEdge(
                    source_node_id=self.config.start_token,
                    target_node_id=node_id,
                    relation="arg_start",
                    persisted=False,
                    required_capabilities=tuple(role.profile.capabilities[:1]),
                    reason="explicit START edge for the first joint role-node expansion",
                )
            )
        return tuple(selected)

    @staticmethod
    def _predecessor_nodes(
        encoded: ARGEncodedInput,
    ) -> tuple[tuple[str, tuple[str, ...], bool], ...]:
        # Materialize a read-only branch overlay for predecessor scoring.  The
        # canonical snapshot is never mutated; GraphStateCustody remains the
        # only component allowed to commit the delta.
        nodes: dict[str, tuple[tuple[str, ...], bool]] = {
            node.node_id: (tuple(node.capabilities), False)
            for node in encoded.current_graph.nodes
            if not node.terminal
        }
        if encoded.branch_delta is not None:
            for mutation in encoded.branch_delta.mutations:
                if mutation.kind is GraphMutationKind.REMOVE_NODE:
                    nodes.pop(mutation.entity_id, None)
                    continue
                if mutation.kind in {
                    GraphMutationKind.ADD_NODE,
                    GraphMutationKind.REPLACE_NODE,
                    GraphMutationKind.SET_NODE_CAPABILITIES,
                }:
                    capabilities = _tokens(
                        mutation.value.get("capabilities") or ()
                    )
                    if capabilities:
                        nodes[mutation.entity_id] = (capabilities, True)
        return tuple(
            (node_id, capabilities, branch_local)
            for node_id, (capabilities, branch_local) in sorted(nodes.items())
        )

    def _predecessor_score(
        self,
        role: ARGEncodedRole,
        predecessor_capabilities: Sequence[str],
        predecessor_id: str,
        encoded: ARGEncodedInput,
    ) -> float:
        target = set(role.profile.capabilities)
        predecessor = set(predecessor_capabilities)
        shared = len(target.intersection(predecessor)) / max(1, len(target))
        complement = len(target - predecessor) / max(1, len(target))
        dependency_hints = _tokens(
            role.binding.metadata.get("depends_on_capabilities") or ()
        )
        dependency = len(set(dependency_hints).intersection(predecessor)) / max(
            1, len(dependency_hints)
        )
        critical = (
            1.0
            if set(predecessor).intersection(self.config.critical_path_capabilities)
            else 0.0
        )
        existing_dependency = any(
            predecessor_id in node.dependencies
            for node in encoded.current_graph.nodes
        )
        return _round(
            dependency * 3.0
            + critical * 2.0
            + (1.0 if existing_dependency else 0.0)
            + complement
            - shared * 0.25
        )

    def _score(
        self,
        encoded: ARGEncodedInput,
        state: _BeamState,
        role: ARGEncodedRole,
        edges: Sequence[ARGIncidentEdge],
        *,
        step_index: int,
    ) -> ARGScoreComponents:
        uncovered_matches = set(role.obligation_matches) - set(
            state.covered_obligations
        )
        obligation = len(uncovered_matches) / max(
            1, len(encoded.policy_input.unresolved_obligations)
        )
        # ARG's joint sequence is autoregressive: phase fit is most valuable at
        # START and decays by position so permutations do not receive the same
        # score merely because they contain the same roles.
        capability = (
            len(role.phase_affinity)
            / max(1, len(encoded.phase_terms))
            / max(1, step_index + 1)
        )
        reachability = 1.0 if any(item.persisted for item in edges) else 0.5
        critical = (
            1.0
            if set(role.profile.capabilities).intersection(
                self.config.critical_path_capabilities
            )
            else 0.0
        )
        communication = (
            sum(1 for item in edges if item.persisted)
            / max(1, self.config.maximum_incident_edges_per_node)
        )
        switch = 1.0 if role.cold_start else 0.0
        recovery = self._recovery_value(encoded, role)
        weights = self.config.score_weights
        total = (
            obligation * float(weights["obligation_coverage"])
            + capability * float(weights["capability_fit"])
            + reachability * float(weights["dependency_reachability"])
            + critical * float(weights["critical_path"])
            + recovery * float(weights["recovery_value"])
            - communication * float(weights["communication_cost"])
            - switch * float(weights["switch_cost"])
        )
        return ARGScoreComponents(
            obligation_coverage=_round(obligation),
            capability_fit=_round(capability),
            dependency_reachability=_round(reachability),
            critical_path=_round(critical),
            communication_cost=_round(communication),
            switch_cost=_round(switch),
            recovery_value=_round(recovery),
            total=_round(total),
        )

    def _recovery_value(
        self,
        encoded: ARGEncodedInput,
        role: ARGEncodedRole,
    ) -> float:
        phase_recovery = encoded.policy_input.phase.lower() == "recovery"
        capability_recovery = bool(
            set(role.profile.capabilities).intersection(
                self.config.recovery_capabilities
            )
        )
        recovery_outcome = bool(encoded.recent_recovery_outcome)
        return _round(
            min(
                1.0,
                (0.6 if phase_recovery else 0.0)
                + (0.4 if capability_recovery else 0.0)
                + (0.1 if recovery_outcome and capability_recovery else 0.0),
            )
        )

    def _end_hypothesis(
        self,
        encoded: ARGEncodedInput,
        state: _BeamState,
        candidates: Sequence[ARGEncodedRole],
    ) -> ARGJointHypothesis:
        obligations = set(encoded.policy_input.unresolved_obligations)
        covered = set(state.covered_obligations)
        uncovered = tuple(sorted(obligations - covered))
        if not uncovered:
            end_reason = "all_obligations_covered"
        elif len(state.steps) >= self.config.maximum_nodes:
            end_reason = "maximum_nodes_reached"
        elif len(state.used_roles) >= len(candidates):
            end_reason = "eligible_candidates_exhausted"
        elif len(candidates) >= self.config.candidate_cap:
            end_reason = "candidate_cap_reached"
        else:
            end_reason = "bounded_best_hypothesis_selected"
        predecessor = (
            state.steps[-1].node_id if state.steps else self.config.start_token
        )
        end_edge = ARGIncidentEdge(
            source_node_id=predecessor,
            target_node_id=self.config.end_token,
            relation="arg_end",
            persisted=False,
            required_capabilities=(),
            reason=end_reason,
        )
        zero = ARGScoreComponents(0, 0, 0, 0, 0, 0, 0, 0)
        end_state = canonical_digest(
            {
                "previous_state_digest": state.state_digest,
                "end_token": self.config.end_token,
                "end_reason": end_reason,
                "covered": sorted(covered),
                "uncovered": list(uncovered),
            }
        )
        end_step = ARGJointStep(
            step_index=len(state.steps),
            token=self.config.end_token,
            role_id="",
            node_id="",
            binding_id="",
            worker_id="",
            capabilities=(),
            incident_edges=(end_edge,),
            score=zero,
            state_digest=end_state,
            cold_start=False,
            reasons=(f"explicit END: {end_reason}",),
        )
        hypothesis_id = "arg_hypothesis_" + canonical_digest(
            {
                "input_digest": encoded.digest,
                "steps": [item.to_dict() for item in (*state.steps, end_step)],
            }
        )[:20]
        return ARGJointHypothesis(
            hypothesis_id=hypothesis_id,
            start_token=self.config.start_token,
            end_token=self.config.end_token,
            steps=(*state.steps, end_step),
            covered_obligations=tuple(sorted(covered)),
            uncovered_obligations=uncovered,
            total_score=_round(state.total_score),
            end_reason=end_reason,
        )

    def _proposal(
        self,
        encoded: ARGEncodedInput,
        selected: ARGJointHypothesis,
        alternatives: Sequence[ARGJointHypothesis],
    ) -> TopologyProposalArtifact:
        operations = self._operations(encoded, selected)
        alternative_refs = tuple(
            StableArtifactRef(
                ref_id=f"arg-alt-{item.hypothesis_id}",
                uri=f"urn:zyra:arg-alternative:{item.hypothesis_id}",
                digest=canonical_digest(item.to_dict()),
            )
            for item in alternatives
        )
        operation_digest = canonical_digest([item.to_dict() for item in operations])
        proposal_id = "arg_proposal_" + canonical_digest(
            (
                encoded.digest,
                selected.hypothesis_id,
                operation_digest,
                self.config.digest,
            )
        )[:20]
        created_at = encoded.policy_input.header.created_at
        expires_at = (
            datetime_from_iso(created_at)
            + timedelta(seconds=self.config.proposal_ttl_seconds)
        ).isoformat().replace("+00:00", "Z")
        role_steps = selected.role_steps
        persisted_edges = [
            edge
            for step in role_steps
            for edge in step.incident_edges
            if edge.persisted
        ]
        reasons = tuple(
            [
                (
                    f"{step.role_id}->{step.node_id} via {step.worker_id}; "
                    f"score={_round(step.score.total)}"
                )
                for step in role_steps
            ]
            + [f"END={selected.end_reason}"]
        )
        expected = FrozenDict(
            {
                "mechanism": "ARG deterministic joint role-node-edge",
                "requirement_revision": encoded.policy_input.requirement_revision,
                "phase": encoded.policy_input.phase,
                "role_catalog_version": encoded.role_catalog_version,
                "role_catalog_digest": encoded.role_catalog_digest,
                "readiness_stage": encoded.readiness_stage,
                "readiness_status": encoded.readiness_status,
                "readiness_report_digest": encoded.readiness_report_digest,
                "branch_delta_digest": encoded.branch_delta_digest,
                "joint_hypothesis": selected.to_dict(),
                "alternatives": [item.to_dict() for item in alternatives],
                "score_components": [
                    {
                        "role_id": step.role_id,
                        **step.score.to_dict(),
                    }
                    for step in role_steps
                ],
                "end_reason": selected.end_reason,
                "tokens": (
                    len(role_steps) * self.config.estimated_node_tokens
                    + len(persisted_edges) * self.config.estimated_edge_tokens
                ),
                "cost_usd": 0.0,
                "time_ms": (
                    len(role_steps) * self.config.estimated_node_time_ms
                    + len(persisted_edges) * self.config.estimated_edge_time_ms
                ),
                "communication_bytes": (
                    len(persisted_edges) * self.config.estimated_edge_bytes
                ),
                "pending_side_effects": [],
                "proposal_signal_mode": (
                    "pretrained_model_assisted"
                    if encoded.model_observation is not None
                    else "deterministic_only"
                ),
                "model_observation_digest": (
                    encoded.model_observation.output_digest
                    if encoded.model_observation is not None
                    else ""
                ),
            }
        )
        header = ContractHeader(
            contract_id=proposal_id,
            created_at=created_at,
            source_event_id=encoded.policy_input.header.source_event_id,
            correlation_id=encoded.policy_input.header.correlation_id,
            causation_id=encoded.policy_input.header.contract_id,
            mechanism_id=self.config.mechanism_id,
            mechanism_version=self.config.mechanism_version,
            input_version=encoded.schema_version,
            idempotency_key=f"arg:{encoded.digest}:{selected.hypothesis_id}",
            configuration_digest=self.config.digest,
        )
        return TopologyProposalArtifact(
            header=header,
            proposal_id=proposal_id,
            input_snapshot_digest=encoded.policy_input.digest,
            base_graph=encoded.policy_input.graph,
            operations=operations,
            expected_outcome=expected,
            alternatives=alternative_refs,
            reasons=reasons,
            constraint_assumptions=(
                "GraphStateCustody remains the sole canonical graph owner",
                "symbolic projector must revalidate permission, budget, capacity, DAG, and readiness",
                "implementation_validated and evidence_only proposals are non-committing",
                "worker/tool/skill/model registry projections are immutable for this input digest",
            ),
            expires_at=expires_at,
            fallback_profile=self.config.fallback_profile,
        )

    def _operations(
        self,
        encoded: ARGEncodedInput,
        hypothesis: ARGJointHypothesis,
    ) -> tuple[TopologyOperation, ...]:
        operations: list[TopologyOperation] = []
        selected_nodes = {item.node_id for item in hypothesis.role_steps}
        current_nodes = encoded.current_graph.node_map
        current_edges = encoded.current_graph.edge_map
        dependent_nodes = {
            dependency
            for node in encoded.current_graph.nodes
            for dependency in node.dependencies
        }
        obsolete = tuple(
            node
            for node in encoded.current_graph.nodes
            if (
                node.metadata.get("arg_owner") == "arg_designer"
                or node.labels.get("arg_owner") == "arg_designer"
            )
            and node.node_id not in selected_nodes
            and node.node_id not in dependent_nodes
        )
        obsolete_ids = {item.node_id for item in obsolete}
        for edge in encoded.current_graph.edges:
            if (
                edge.source_node_id in obsolete_ids
                or edge.target_node_id in obsolete_ids
            ):
                operations.append(
                    TopologyOperation(
                        kind=TopologyOperationKind.REMOVE_EDGE,
                        entity_id=edge.edge_id,
                        expected_entity_revision=edge.revision,
                        required_permissions=("graph.write",),
                        reason=(
                            "requirement/phase joint hypothesis terminates an obsolete "
                            "ARG-managed incident edge"
                        ),
                    )
                )
        for node in obsolete:
            operations.append(
                TopologyOperation(
                    kind=TopologyOperationKind.REMOVE_NODE,
                    entity_id=node.node_id,
                    expected_entity_revision=node.revision,
                    required_permissions=("graph.write",),
                    reason=(
                        "requirement/phase joint hypothesis terminates an obsolete "
                        "ARG-managed role-node"
                    ),
                )
            )

        for step in hypothesis.role_steps:
            profile = next(
                item for item in encoded.eligible_roles if item.role_id == step.role_id
            )
            binding = profile.binding
            placements = tuple(
                sorted(
                    set(binding.allowed_placements).intersection(
                        encoded.policy_input.allowed_placements
                    )
                )
            )
            placement = placements[0] if placements else ""
            dependencies = tuple(
                sorted(
                    edge.source_node_id
                    for edge in step.incident_edges
                    if edge.persisted
                )
            )
            value = FrozenDict(
                {
                    "role": step.role_id,
                    "capabilities": list(step.capabilities),
                    "dependencies": list(dependencies),
                    "state": "planned",
                    "labels": {
                        "arg_owner": "arg_designer",
                        "arg_phase": encoded.policy_input.phase,
                    },
                    "metadata": {
                        "arg_owner": "arg_designer",
                        "arg_hypothesis_id": hypothesis.hypothesis_id,
                        "arg_binding_id": step.binding_id,
                        "arg_manifest_digest": binding.manifest_digest,
                        "arg_catalog_digest": encoded.role_catalog_digest,
                        "requirement_revision": encoded.policy_input.requirement_revision,
                        "score": step.score.to_dict(),
                        "cold_start": step.cold_start,
                    },
                }
            )
            current = current_nodes.get(step.node_id)
            if current is None:
                operations.append(
                    TopologyOperation(
                        kind=TopologyOperationKind.ADD_NODE,
                        entity_id=step.node_id,
                        value=value,
                        required_permissions=binding.required_permissions,
                        requested_placement=placement,
                        resource_id=binding.worker_id,
                        required_capacity=1.0,
                        reason=step.reasons[0],
                    )
                )
            elif (
                current.role != step.role_id
                or current.capabilities != tuple(sorted(step.capabilities))
                or current.dependencies != dependencies
            ):
                operations.append(
                    TopologyOperation(
                        kind=TopologyOperationKind.REPLACE_NODE,
                        entity_id=step.node_id,
                        value=value,
                        expected_entity_revision=current.revision,
                        required_permissions=binding.required_permissions,
                        requested_placement=placement,
                        resource_id=binding.worker_id,
                        required_capacity=1.0,
                        reason="joint role/capability/dependency state changed",
                    )
                )

        removed_edge_ids = {
            item.entity_id
            for item in operations
            if item.kind is TopologyOperationKind.REMOVE_EDGE
        }
        existing_edge_shapes = {
            (
                edge.source_node_id,
                edge.target_node_id,
                edge.relation,
                edge.required_capabilities,
            )
            for edge in current_edges.values()
            if edge.edge_id not in removed_edge_ids
        }
        for step in hypothesis.role_steps:
            profile = next(
                item for item in encoded.eligible_roles if item.role_id == step.role_id
            )
            binding = profile.binding
            placements = tuple(
                sorted(
                    set(binding.allowed_placements).intersection(
                        encoded.policy_input.allowed_placements
                    )
                )
            )
            placement = placements[0] if placements else ""
            for edge in step.incident_edges:
                if not edge.persisted:
                    continue
                shape = (
                    edge.source_node_id,
                    edge.target_node_id,
                    edge.relation,
                    tuple(sorted(edge.required_capabilities)),
                )
                if shape in existing_edge_shapes:
                    continue
                operations.append(
                    TopologyOperation(
                        kind=TopologyOperationKind.ADD_EDGE,
                        entity_id=edge.edge_id,
                        value=FrozenDict(
                            {
                                "source_node_id": edge.source_node_id,
                                "target_node_id": edge.target_node_id,
                                "relation": edge.relation,
                                "required_capabilities": list(
                                    edge.required_capabilities
                                ),
                                "condition": {
                                    "phase": encoded.policy_input.phase,
                                    "requirement_revision": encoded.policy_input.requirement_revision,
                                },
                                "labels": {
                                    "arg_owner": "arg_designer",
                                },
                                "metadata": {
                                    "arg_owner": "arg_designer",
                                    "arg_hypothesis_id": hypothesis.hypothesis_id,
                                    "arg_catalog_digest": encoded.role_catalog_digest,
                                },
                            }
                        ),
                        required_permissions=("graph.write",),
                        requested_placement=placement,
                        resource_id=binding.worker_id,
                        communication_bytes=self.config.estimated_edge_bytes,
                        reason=edge.reason,
                    )
                )
        if not operations:
            operations.append(
                TopologyOperation(
                    kind=TopologyOperationKind.SET_GRAPH_METADATA,
                    entity_id="arg_base_topology_digest",
                    value=FrozenDict({"value": hypothesis.hypothesis_id}),
                    required_permissions=("graph.write",),
                    reason="stable ARG topology records its deterministic base hypothesis",
                )
            )
        return tuple(operations)


def datetime_from_iso(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
