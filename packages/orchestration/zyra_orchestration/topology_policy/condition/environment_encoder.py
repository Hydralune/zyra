from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..contracts import (
    FrozenDict,
    PolicyInputSnapshot,
    TelemetryObservation,
    TopologyProposalArtifact,
    canonical_digest,
    thaw_json,
)


CARD_ENVIRONMENT_FEATURE_SCHEMA = "zyra.card-environment-features/v1"


class CARDEnvironmentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(value)
    return (value,)


def _tokens(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        selected: Iterable[Any] = value.keys()
    elif isinstance(value, (str, bytes)):
        selected = (value,)
    else:
        selected = value
    return tuple(
        sorted(
            {
                str(item).strip().lower()
                for item in selected
                if str(item).strip()
            }
        )
    )


def _boolean(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "connected"}


@dataclass(frozen=True, slots=True)
class CARDObservationFeature:
    observation_id: str
    resource_id: str
    category: str
    observed_at: str
    fresh_until: str
    age_seconds: float
    stale: bool
    confidence: float
    effective_confidence: float
    observation_source: str
    source_event_id: str
    physical_runtime_id: str
    location: str
    available: bool
    healthy: bool
    network_connected: bool
    load: float
    queue_depth: float
    capacity_available: float
    lease_available: bool
    recent_successes: int
    recent_failures: int
    latency_p50_ms: float
    latency_p95_ms: float
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    privacy_classes: tuple[str, ...]
    allowed_placements: tuple[str, ...]
    capabilities: tuple[str, ...]
    missing_required: tuple[str, ...]
    missing_optional: tuple[str, ...]
    linked_resource_ids: tuple[str, ...]
    trigger_flags: tuple[str, ...]
    attributes: FrozenDict

    def to_dict(self) -> dict[str, Any]:
        return thaw_json(self)


@dataclass(frozen=True, slots=True)
class CARDNodeFeature:
    node_id: str
    role_id: str
    worker_id: str
    capabilities: tuple[str, ...]
    requested_placement: str
    observation_ids: tuple[str, ...]
    observations: tuple[CARDObservationFeature, ...]

    @property
    def primary(self) -> CARDObservationFeature:
        return self.observations[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "role_id": self.role_id,
            "worker_id": self.worker_id,
            "capabilities": list(self.capabilities),
            "requested_placement": self.requested_placement,
            "observation_ids": list(self.observation_ids),
            "observations": [item.to_dict() for item in self.observations],
        }


@dataclass(frozen=True, slots=True)
class CARDBaseEdge:
    edge_id: str
    source_node_id: str
    target_node_id: str
    relation: str
    edge_type: str
    required_capabilities: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        if self.edge_type not in {"spatial", "temporal"}:
            raise CARDEnvironmentError(
                "card_edge_type_invalid",
                f"CARD edge {self.edge_id} has unsupported type {self.edge_type}",
            )

    def to_dict(self) -> dict[str, Any]:
        return thaw_json(self)


@dataclass(frozen=True, slots=True)
class CARDReplacementCandidate:
    candidate_id: str
    source_node_id: str
    target_node_id: str
    relation: str
    edge_type: str
    required_capabilities: tuple[str, ...]
    constraint_ref: str
    reason: str

    def __post_init__(self) -> None:
        if not self.candidate_id or not self.source_node_id or not self.target_node_id:
            raise CARDEnvironmentError(
                "card_candidate_identity_missing",
                "replacement candidates require stable identity and endpoints",
            )
        if self.source_node_id == self.target_node_id:
            raise CARDEnvironmentError(
                "card_candidate_self_loop",
                "CARD replacement candidates cannot be self loops",
            )
        if self.edge_type not in {"spatial", "temporal"}:
            raise CARDEnvironmentError(
                "card_candidate_type_invalid",
                "CARD replacement candidate type must be spatial or temporal",
            )
        if not self.constraint_ref:
            raise CARDEnvironmentError(
                "card_candidate_constraint_missing",
                "CARD can add only an upstream constrained replacement candidate",
            )

    def to_dict(self) -> dict[str, Any]:
        return thaw_json(self)


@dataclass(frozen=True, slots=True)
class CARDEncodedEnvironment:
    schema_version: str
    mechanism_version: str
    configuration_digest: str
    policy_input: PolicyInputSnapshot
    arg_base_proposal_id: str
    arg_base_proposal_digest: str
    environment_snapshot_digest: str
    observed_at: str
    observations: tuple[CARDObservationFeature, ...]
    nodes: tuple[CARDNodeFeature, ...]
    base_edges: tuple[CARDBaseEdge, ...]
    replacement_candidates: tuple[CARDReplacementCandidate, ...]
    missing_optional_categories: tuple[str, ...]
    trigger_flags: tuple[str, ...]

    @property
    def node_map(self) -> Mapping[str, CARDNodeFeature]:
        return {item.node_id: item for item in self.nodes}

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mechanism_version": self.mechanism_version,
            "configuration_digest": self.configuration_digest,
            "policy_input_digest": self.policy_input.digest,
            "arg_base_proposal_id": self.arg_base_proposal_id,
            "arg_base_proposal_digest": self.arg_base_proposal_digest,
            "environment_snapshot_digest": self.environment_snapshot_digest,
            "observed_at": self.observed_at,
            "observations": [item.to_dict() for item in self.observations],
            "nodes": [item.to_dict() for item in self.nodes],
            "base_edges": [item.to_dict() for item in self.base_edges],
            "replacement_candidates": [
                item.to_dict() for item in self.replacement_candidates
            ],
            "missing_optional_categories": list(self.missing_optional_categories),
            "trigger_flags": list(self.trigger_flags),
        }


class CARDEnvironmentEncoder:
    """Detaches real owner telemetry and ARG base topology into CARD features."""

    def encode(
        self,
        *,
        policy_input: PolicyInputSnapshot,
        arg_base: TopologyProposalArtifact,
        mechanism_version: str,
        configuration_digest: str,
        required_categories: Iterable[str],
        optional_categories: Iterable[str],
        required_observation_fields: Iterable[str],
        minimum_confidence: float,
        stale_confidence_multiplier: float,
        replacement_candidates: Iterable[CARDReplacementCandidate] = (),
    ) -> CARDEncodedEnvironment:
        self._validate_arg_binding(policy_input=policy_input, arg_base=arg_base)
        required = _tokens(required_categories)
        optional = _tokens(optional_categories)
        required_fields = set(_tokens(required_observation_fields))
        environment = policy_input.environment
        categories = {item.category.lower() for item in environment.observations}
        missing_required_categories = tuple(
            sorted(
                set(required)
                .union(environment.missing_categories)
                .difference(categories)
            )
        )
        if missing_required_categories:
            raise CARDEnvironmentError(
                "card_required_category_missing",
                "CARD required telemetry categories are absent: "
                + ", ".join(missing_required_categories),
            )

        features = tuple(
            self._observation_feature(
                item,
                snapshot_time=environment.observed_at,
                required_fields=required_fields if item.category == "worker" else set(),
                minimum_confidence=minimum_confidence,
                stale_confidence_multiplier=stale_confidence_multiplier,
            )
            for item in environment.observations
        )
        by_identity = {
            (item.category, item.resource_id): item
            for item in features
        }
        by_resource: dict[str, list[CARDObservationFeature]] = {}
        for item in features:
            by_resource.setdefault(item.resource_id, []).append(item)

        hypothesis = _mapping(arg_base.expected_outcome.get("joint_hypothesis"))
        steps = tuple(
            _mapping(item)
            for item in _sequence(hypothesis.get("steps"))
            if _mapping(item).get("role_id")
        )
        if not steps:
            raise CARDEnvironmentError(
                "card_arg_base_nodes_missing",
                "CARD requires ARG joint role-node steps and cannot generate a graph",
            )
        operation_by_node = {
            item.entity_id: item
            for item in arg_base.operations
            if item.kind.value in {"add_node", "replace_node"}
        }
        nodes: list[CARDNodeFeature] = []
        for step in steps:
            node_id = str(step.get("node_id") or "")
            worker_id = str(step.get("worker_id") or "")
            if not node_id or not worker_id:
                raise CARDEnvironmentError(
                    "card_arg_node_binding_missing",
                    "ARG role-node steps must retain their real worker binding",
                )
            primary = by_identity.get(("worker", worker_id))
            if primary is None:
                raise CARDEnvironmentError(
                    "card_worker_observation_missing",
                    f"CARD has no decision-time worker telemetry for {worker_id}",
                )
            if primary.missing_required:
                raise CARDEnvironmentError(
                    "card_required_feature_missing",
                    f"CARD worker {worker_id} is missing required features: "
                    + ", ".join(primary.missing_required),
                )
            linked = []
            for resource_id in primary.linked_resource_ids:
                linked.extend(by_resource.get(resource_id, ()))
            observations = tuple(
                [primary]
                + sorted(
                    {
                        item.observation_id: item
                        for item in linked
                        if item.observation_id != primary.observation_id
                    }.values(),
                    key=lambda item: (
                        item.category,
                        item.resource_id,
                        item.observation_id,
                    ),
                )
            )
            operation = operation_by_node.get(node_id)
            requested_placement = (
                operation.requested_placement if operation is not None else primary.location
            )
            nodes.append(
                CARDNodeFeature(
                    node_id=node_id,
                    role_id=str(step.get("role_id") or ""),
                    worker_id=worker_id,
                    capabilities=_tokens(step.get("capabilities")),
                    requested_placement=requested_placement,
                    observation_ids=tuple(
                        item.observation_id for item in observations
                    ),
                    observations=observations,
                )
            )

        edges = self._base_edges(steps)
        if not edges:
            raise CARDEnvironmentError(
                "card_arg_base_edges_missing",
                "CARD cannot invent a topology when ARG produced no persisted base edge",
            )
        node_ids = {item.node_id for item in nodes}
        candidates = tuple(
            sorted(
                tuple(replacement_candidates),
                key=lambda item: item.candidate_id,
            )
        )
        base_shapes = {
            (item.source_node_id, item.target_node_id, item.edge_type)
            for item in edges
        }
        for candidate in candidates:
            if (
                candidate.source_node_id not in node_ids
                or candidate.target_node_id not in node_ids
            ):
                raise CARDEnvironmentError(
                    "card_candidate_outside_arg_base",
                    "CARD replacement candidates must connect ARG-owned role nodes",
                )
            shape = (
                candidate.source_node_id,
                candidate.target_node_id,
                candidate.edge_type,
            )
            if shape in base_shapes:
                raise CARDEnvironmentError(
                    "card_candidate_duplicates_arg_base",
                    "replacement candidate duplicates an ARG base edge",
                )

        trigger_flags = tuple(
            sorted(
                {
                    flag
                    for item in features
                    for flag in item.trigger_flags
                }
            )
        )
        return CARDEncodedEnvironment(
            schema_version=CARD_ENVIRONMENT_FEATURE_SCHEMA,
            mechanism_version=mechanism_version,
            configuration_digest=configuration_digest,
            policy_input=policy_input,
            arg_base_proposal_id=arg_base.proposal_id,
            arg_base_proposal_digest=arg_base.digest,
            environment_snapshot_digest=environment.digest,
            observed_at=environment.observed_at,
            observations=tuple(
                sorted(
                    features,
                    key=lambda item: (
                        item.category,
                        item.resource_id,
                        item.observation_id,
                    ),
                )
            ),
            nodes=tuple(sorted(nodes, key=lambda item: item.node_id)),
            base_edges=tuple(sorted(edges, key=lambda item: item.edge_id)),
            replacement_candidates=candidates,
            missing_optional_categories=tuple(
                item for item in optional if item not in categories
            ),
            trigger_flags=trigger_flags,
        )

    @staticmethod
    def _validate_arg_binding(
        *,
        policy_input: PolicyInputSnapshot,
        arg_base: TopologyProposalArtifact,
    ) -> None:
        if arg_base.header.mechanism_id != "arg_designer":
            raise CARDEnvironmentError(
                "card_arg_base_mechanism_invalid",
                "CARD accepts only an ARG base topology proposal",
            )
        if arg_base.input_snapshot_digest != policy_input.digest:
            raise CARDEnvironmentError(
                "card_arg_base_input_stale",
                "CARD rejects an ARG proposal from another policy snapshot",
            )
        if (
            arg_base.base_graph.graph_id != policy_input.graph.graph_id
            or arg_base.base_graph.revision != policy_input.graph.revision
            or arg_base.base_graph.signature != policy_input.graph.signature
        ):
            raise CARDEnvironmentError(
                "card_arg_base_graph_stale",
                "CARD ARG base graph binding differs from the immutable input",
            )

    @staticmethod
    def _base_edges(
        steps: Iterable[Mapping[str, Any]],
    ) -> tuple[CARDBaseEdge, ...]:
        edges: dict[str, CARDBaseEdge] = {}
        for step in steps:
            for edge_value in _sequence(step.get("incident_edges")):
                edge = _mapping(edge_value)
                if edge.get("persisted") is not True:
                    continue
                relation = str(edge.get("relation") or "arg_dependency")
                declared = str(edge.get("edge_type") or "").lower()
                edge_type = (
                    declared
                    if declared in {"spatial", "temporal"}
                    else (
                        "temporal"
                        if relation.lower().startswith("temporal")
                        else "spatial"
                    )
                )
                edge_id = str(edge.get("edge_id") or "")
                if not edge_id:
                    edge_id = "arg_edge_" + canonical_digest(
                        (
                            edge.get("source_node_id"),
                            edge.get("target_node_id"),
                            relation,
                        )
                    )[:20]
                edges[edge_id] = CARDBaseEdge(
                    edge_id=edge_id,
                    source_node_id=str(edge.get("source_node_id") or ""),
                    target_node_id=str(edge.get("target_node_id") or ""),
                    relation=relation,
                    edge_type=edge_type,
                    required_capabilities=_tokens(
                        edge.get("required_capabilities")
                    ),
                    reason=str(edge.get("reason") or "ARG base edge"),
                )
        return tuple(edges.values())

    @staticmethod
    def _observation_feature(
        observation: TelemetryObservation,
        *,
        snapshot_time: str,
        required_fields: set[str],
        minimum_confidence: float,
        stale_confidence_multiplier: float,
    ) -> CARDObservationFeature:
        captured = _time(snapshot_time)
        observed = _time(observation.observed_at)
        fresh_until = _time(observation.fresh_until)
        stale = captured > fresh_until
        confidence = observation.confidence
        effective_confidence = confidence * (
            stale_confidence_multiplier if stale else 1.0
        )
        attributes = _mapping(observation.attributes)
        declared_missing = set(_tokens(observation.missing_fields))
        physical_missing = (
            not observation.physical_runtime_id
            or observation.physical_runtime_id.startswith("unresolved:")
        )
        derived_missing = set()
        if "telemetry" in declared_missing:
            derived_missing.update(
                {
                    "availability",
                    "load_capacity",
                    "latency",
                    "cost",
                }
            )
        if "health" in declared_missing:
            derived_missing.update({"availability", "health"})
        if "location" in declared_missing:
            derived_missing.add("physical_location")
        if physical_missing:
            derived_missing.add("physical_runtime_id")
        if not observation.location:
            derived_missing.add("physical_location")
        if not observation.privacy_classes or not observation.allowed_placements:
            derived_missing.add("privacy")
        if confidence < minimum_confidence:
            derived_missing.add("confidence")
        missing_required = tuple(
            sorted((declared_missing | derived_missing).intersection(required_fields))
        )
        expected_optional = {
            "prompt_tokens",
            "completion_tokens",
            "queue_depth",
            "recent_successes",
            "network_state",
            "fault",
            "requirement_change",
            "compact",
            "recovery",
        }
        missing_optional = tuple(
            sorted(
                item
                for item in expected_optional
                if item not in attributes
            )
        )
        network_state = str(attributes.get("network_state") or "connected").lower()
        network_connected = (
            network_state not in {"disconnected", "offline", "unreachable", "down"}
            and not _boolean(attributes.get("network_disconnected"))
            and not _boolean(attributes.get("disconnected"))
        )
        linked_ids = {
            *_tokens(attributes.get("linked_resource_ids")),
            *_tokens(attributes.get("tool_ids")),
        }
        for key in ("provider_id", "model_id", "tool_id"):
            if attributes.get(key):
                linked_ids.add(str(attributes[key]).strip().lower())
        triggers = tuple(
            sorted(
                flag
                for flag in (
                    "fault" if _boolean(attributes.get("fault")) else "",
                    (
                        "requirement_change"
                        if _boolean(attributes.get("requirement_change"))
                        else ""
                    ),
                    "compact" if _boolean(attributes.get("compact")) else "",
                    "recovery" if _boolean(attributes.get("recovery")) else "",
                    "network_disconnect" if not network_connected else "",
                )
                if flag
            )
        )
        return CARDObservationFeature(
            observation_id=observation.observation_id,
            resource_id=observation.resource_id,
            category=observation.category.lower(),
            observed_at=observation.observed_at,
            fresh_until=observation.fresh_until,
            age_seconds=max(0.0, (captured - observed).total_seconds()),
            stale=stale,
            confidence=confidence,
            effective_confidence=max(0.0, min(1.0, effective_confidence)),
            observation_source=observation.observation_source,
            source_event_id=observation.source_event_id,
            physical_runtime_id=observation.physical_runtime_id,
            location=observation.location.lower(),
            available=observation.available,
            healthy=observation.healthy,
            network_connected=network_connected,
            load=max(0.0, observation.load),
            queue_depth=max(0.0, float(attributes.get("queue_depth") or 0)),
            capacity_available=max(0.0, observation.capacity_available),
            lease_available=observation.lease_available,
            recent_successes=max(0, int(attributes.get("recent_successes") or 0)),
            recent_failures=observation.recent_failures,
            latency_p50_ms=observation.latency_p50_ms,
            latency_p95_ms=observation.latency_p95_ms,
            prompt_tokens=max(0, int(attributes.get("prompt_tokens") or 0)),
            completion_tokens=max(0, int(attributes.get("completion_tokens") or 0)),
            cost_usd=observation.cost_usd,
            privacy_classes=observation.privacy_classes,
            allowed_placements=observation.allowed_placements,
            capabilities=_tokens(attributes.get("capabilities")),
            missing_required=missing_required,
            missing_optional=missing_optional,
            linked_resource_ids=tuple(sorted(linked_ids)),
            trigger_flags=triggers,
            attributes=FrozenDict(attributes),
        )
