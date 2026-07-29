from __future__ import annotations

import hashlib
import json
import types
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, ClassVar, TypeVar, Union, get_args, get_origin, get_type_hints


JsonScalar = str | int | float | bool | None
# Runtime recursion in a type alias is not portable across every supported
# Python minor. Public constructors still validate the concrete JSON domain.
FrozenJson = Any


class PolicyContractError(ValueError):
    """A policy contract cannot be admitted or replayed safely."""


class UnsupportedPolicySchema(PolicyContractError):
    pass


class PolicyDigestMismatch(PolicyContractError):
    pass


class FrozenDict(Mapping[str, FrozenJson]):
    """Small immutable, deterministically ordered JSON mapping."""

    __slots__ = ("_items", "_lookup")

    def __init__(self, value: Mapping[str, Any] | None = None) -> None:
        items = tuple(
            sorted(
                ((str(key), freeze_json(item)) for key, item in dict(value or {}).items()),
                key=lambda pair: pair[0],
            )
        )
        object.__setattr__(self, "_items", items)
        object.__setattr__(self, "_lookup", MappingProxyType(dict(items)))

    def __setattr__(self, name: str, value: Any) -> None:
        if hasattr(self, name):
            raise TypeError("FrozenDict is immutable")
        object.__setattr__(self, name, value)

    def __getitem__(self, key: str) -> FrozenJson:
        return self._lookup[key]

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"FrozenDict({dict(self._items)!r})"


def freeze_json(value: Any) -> FrozenJson:
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return FrozenDict({item.name: freeze_json(getattr(value, item.name)) for item in fields(value)})
    if isinstance(value, Mapping):
        return FrozenDict(value)
    if isinstance(value, (set, frozenset)):
        frozen = [freeze_json(item) for item in value]
        return tuple(sorted(frozen, key=lambda item: canonical_json(item)))
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"policy contract contains a non-JSON value: {type(value).__name__}")


def thaw_json(value: Any) -> Any:
    if isinstance(value, FrozenDict):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw_json(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {item.name: thaw_json(getattr(value, item.name)) for item in fields(value)}
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        thaw_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def parse_timestamp(value: str, label: str) -> datetime:
    rendered = required_text(value, label)
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError as error:
        raise PolicyContractError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise PolicyContractError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def required_text(value: Any, label: str) -> str:
    rendered = str(value or "").strip()
    if not rendered:
        raise PolicyContractError(f"{label} is required")
    return rendered


def required_sha256(value: Any, label: str) -> str:
    rendered = required_text(value, label).lower()
    if len(rendered) != 64 or any(character not in "0123456789abcdef" for character in rendered):
        raise PolicyContractError(f"{label} must be a SHA-256 hex digest")
    return rendered


def optional_text(value: Any) -> str:
    return str(value or "").strip()


def normalized_tokens(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(sorted({required_text(item, "token") for item in values}))


@dataclass(frozen=True, slots=True)
class ContractHeader:
    contract_id: str
    created_at: str
    source_event_id: str
    correlation_id: str
    causation_id: str
    mechanism_id: str
    mechanism_version: str
    input_version: str
    idempotency_key: str
    configuration_digest: str = ""

    def __post_init__(self) -> None:
        for name in (
            "contract_id",
            "source_event_id",
            "correlation_id",
            "causation_id",
            "mechanism_id",
            "mechanism_version",
            "input_version",
            "idempotency_key",
        ):
            object.__setattr__(self, name, required_text(getattr(self, name), name))
        parse_timestamp(self.created_at, "created_at")
        configuration_digest = optional_text(self.configuration_digest)
        if configuration_digest:
            configuration_digest = required_sha256(
                configuration_digest,
                "configuration_digest",
            )
        object.__setattr__(self, "configuration_digest", configuration_digest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "created_at": self.created_at,
            "source_event_id": self.source_event_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "mechanism_id": self.mechanism_id,
            "mechanism_version": self.mechanism_version,
            "input_version": self.input_version,
            "idempotency_key": self.idempotency_key,
            "configuration_digest": self.configuration_digest,
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        legacy_seed: str = "",
    ) -> "ContractHeader":
        data = dict(value)
        seed = legacy_seed or canonical_digest(data)
        return cls(
            contract_id=str(data.get("contract_id") or f"legacy-contract-{seed[:20]}"),
            created_at=str(data.get("created_at") or "1970-01-01T00:00:00Z"),
            source_event_id=str(data.get("source_event_id") or f"legacy-event-{seed[:20]}"),
            correlation_id=str(data.get("correlation_id") or f"legacy-correlation-{seed[:20]}"),
            causation_id=str(data.get("causation_id") or f"legacy-causation-{seed[:20]}"),
            mechanism_id=str(data.get("mechanism_id") or "legacy"),
            mechanism_version=str(data.get("mechanism_version") or "legacy-v0"),
            input_version=str(data.get("input_version") or "legacy-v0"),
            idempotency_key=str(data.get("idempotency_key") or f"legacy-idempotency-{seed}"),
            configuration_digest=str(data.get("configuration_digest") or ""),
        )


@dataclass(frozen=True, slots=True)
class StableArtifactRef:
    ref_id: str
    uri: str
    digest: str
    media_type: str = "application/json"

    def __post_init__(self) -> None:
        object.__setattr__(self, "ref_id", required_text(self.ref_id, "ref_id"))
        object.__setattr__(self, "uri", required_text(self.uri, "uri"))
        digest_value = required_sha256(self.digest, "digest")
        object.__setattr__(self, "digest", digest_value)

    def to_dict(self) -> dict[str, Any]:
        return thaw_json(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StableArtifactRef":
        return cls(
            ref_id=str(value.get("ref_id") or value.get("artifact_id") or ""),
            uri=str(value.get("uri") or value.get("path") or ""),
            digest=str(value.get("digest") or value.get("sha256") or ""),
            media_type=str(value.get("media_type") or "application/json"),
        )


@dataclass(frozen=True, slots=True)
class GraphSnapshotRef:
    graph_id: str
    run_id: str
    revision: int
    signature: str
    commit_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "graph_id", required_text(self.graph_id, "graph_id"))
        object.__setattr__(self, "run_id", required_text(self.run_id, "run_id"))
        object.__setattr__(self, "revision", max(0, int(self.revision)))
        object.__setattr__(
            self,
            "signature",
            required_sha256(self.signature, "graph signature"),
        )

    def to_dict(self) -> dict[str, Any]:
        return thaw_json(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "GraphSnapshotRef":
        return cls(
            graph_id=str(value.get("graph_id") or ""),
            run_id=str(value.get("run_id") or ""),
            revision=int(value.get("revision") or 0),
            signature=str(value.get("signature") or ""),
            commit_id=str(value.get("commit_id") or ""),
        )


@dataclass(frozen=True, slots=True)
class PolicyNodeSnapshot:
    node_id: str
    role: str
    capabilities: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    state: str = "planned"
    revision: int = 1
    labels: FrozenDict = field(default_factory=FrozenDict)
    metadata: FrozenDict = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", required_text(self.node_id, "node_id"))
        object.__setattr__(self, "role", required_text(self.role, "role"))
        object.__setattr__(self, "capabilities", normalized_tokens(self.capabilities))
        object.__setattr__(self, "dependencies", normalized_tokens(self.dependencies))
        object.__setattr__(self, "revision", max(1, int(self.revision)))
        object.__setattr__(self, "labels", FrozenDict(self.labels))
        object.__setattr__(self, "metadata", FrozenDict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return thaw_json(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PolicyNodeSnapshot":
        return cls(
            node_id=str(value.get("node_id") or ""),
            role=str(value.get("role") or ""),
            capabilities=tuple(value.get("capabilities") or ()),
            dependencies=tuple(value.get("dependencies") or ()),
            state=str(value.get("state") or "planned"),
            revision=int(value.get("revision") or 1),
            labels=FrozenDict(_mapping(value.get("labels"))),
            metadata=FrozenDict(_mapping(value.get("metadata"))),
        )


@dataclass(frozen=True, slots=True)
class PolicyBudget:
    remaining_tokens: int
    remaining_cost_usd: float
    remaining_time_ms: int
    max_communication_bytes: int
    max_fan_out: int
    max_topology_churn: int
    minimum_dwell_seconds: int

    def __post_init__(self) -> None:
        for name in (
            "remaining_tokens",
            "remaining_time_ms",
            "max_communication_bytes",
            "max_fan_out",
            "max_topology_churn",
            "minimum_dwell_seconds",
        ):
            object.__setattr__(self, name, max(0, int(getattr(self, name))))
        object.__setattr__(self, "remaining_cost_usd", max(0.0, float(self.remaining_cost_usd)))

    def to_dict(self) -> dict[str, Any]:
        return thaw_json(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PolicyBudget":
        return cls(
            remaining_tokens=int(value.get("remaining_tokens") or 0),
            remaining_cost_usd=float(value.get("remaining_cost_usd") or 0),
            remaining_time_ms=int(value.get("remaining_time_ms") or 0),
            max_communication_bytes=int(value.get("max_communication_bytes") or 0),
            max_fan_out=int(value.get("max_fan_out") or 0),
            max_topology_churn=int(value.get("max_topology_churn") or 0),
            minimum_dwell_seconds=int(value.get("minimum_dwell_seconds") or 0),
        )


@dataclass(frozen=True, slots=True)
class TelemetryObservation:
    observation_id: str
    resource_id: str
    category: str
    observed_at: str
    fresh_until: str
    confidence: float
    observation_source: str
    source_event_id: str
    physical_runtime_id: str
    location: str
    available: bool
    healthy: bool
    load: float = 0.0
    capacity_available: float = 0.0
    lease_available: bool = True
    recent_failures: int = 0
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    cost_usd: float = 0.0
    privacy_classes: tuple[str, ...] = ()
    allowed_placements: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    attributes: FrozenDict = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        for name in (
            "observation_id",
            "resource_id",
            "category",
            "observation_source",
            "source_event_id",
            "physical_runtime_id",
            "location",
        ):
            object.__setattr__(self, name, required_text(getattr(self, name), name))
        observed = parse_timestamp(self.observed_at, "observed_at")
        fresh_until = parse_timestamp(self.fresh_until, "fresh_until")
        if fresh_until < observed:
            raise PolicyContractError("fresh_until cannot precede observed_at")
        confidence = float(self.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise PolicyContractError("observation confidence must be between zero and one")
        object.__setattr__(self, "confidence", confidence)
        for name in (
            "load",
            "capacity_available",
            "latency_p50_ms",
            "latency_p95_ms",
            "cost_usd",
        ):
            object.__setattr__(self, name, max(0.0, float(getattr(self, name))))
        object.__setattr__(self, "recent_failures", max(0, int(self.recent_failures)))
        object.__setattr__(self, "privacy_classes", normalized_tokens(self.privacy_classes))
        object.__setattr__(self, "allowed_placements", normalized_tokens(self.allowed_placements))
        object.__setattr__(self, "missing_fields", normalized_tokens(self.missing_fields))
        object.__setattr__(self, "attributes", FrozenDict(self.attributes))

    def to_dict(self) -> dict[str, Any]:
        return thaw_json(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TelemetryObservation":
        return cls(
            observation_id=str(value.get("observation_id") or ""),
            resource_id=str(value.get("resource_id") or ""),
            category=str(value.get("category") or ""),
            observed_at=str(value.get("observed_at") or ""),
            fresh_until=str(value.get("fresh_until") or ""),
            confidence=float(value.get("confidence") if value.get("confidence") is not None else 0),
            observation_source=str(value.get("observation_source") or ""),
            source_event_id=str(value.get("source_event_id") or ""),
            physical_runtime_id=str(value.get("physical_runtime_id") or ""),
            location=str(value.get("location") or ""),
            available=bool(value.get("available", False)),
            healthy=bool(value.get("healthy", False)),
            load=float(value.get("load") or 0),
            capacity_available=float(value.get("capacity_available") or 0),
            lease_available=bool(value.get("lease_available", False)),
            recent_failures=int(value.get("recent_failures") or 0),
            latency_p50_ms=float(value.get("latency_p50_ms") or 0),
            latency_p95_ms=float(value.get("latency_p95_ms") or 0),
            cost_usd=float(value.get("cost_usd") or 0),
            privacy_classes=tuple(value.get("privacy_classes") or ()),
            allowed_placements=tuple(value.get("allowed_placements") or ()),
            missing_fields=tuple(value.get("missing_fields") or ()),
            attributes=FrozenDict(_mapping(value.get("attributes"))),
        )


class TopologyOperationKind(StrEnum):
    ADD_NODE = "add_node"
    REMOVE_NODE = "remove_node"
    REPLACE_NODE = "replace_node"
    SET_NODE_ROLE = "set_node_role"
    SET_NODE_CAPABILITIES = "set_node_capabilities"
    SET_NODE_DEPENDENCIES = "set_node_dependencies"
    ADD_EDGE = "add_edge"
    REMOVE_EDGE = "remove_edge"
    REPLACE_EDGE = "replace_edge"
    SET_GRAPH_METADATA = "set_graph_metadata"
    REMOVE_GRAPH_METADATA = "remove_graph_metadata"


@dataclass(frozen=True, slots=True)
class TopologyOperation:
    kind: TopologyOperationKind
    entity_id: str
    value: FrozenDict = field(default_factory=FrozenDict)
    expected_entity_revision: int | None = None
    required_permissions: tuple[str, ...] = ()
    requested_placement: str = ""
    resource_id: str = ""
    required_capacity: float = 0.0
    communication_bytes: int = 0
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", TopologyOperationKind(self.kind))
        object.__setattr__(self, "entity_id", required_text(self.entity_id, "operation entity_id"))
        object.__setattr__(self, "value", FrozenDict(self.value))
        if self.expected_entity_revision is not None and int(self.expected_entity_revision) < 1:
            raise PolicyContractError("expected_entity_revision must be positive")
        object.__setattr__(self, "required_permissions", normalized_tokens(self.required_permissions))
        object.__setattr__(self, "requested_placement", optional_text(self.requested_placement))
        object.__setattr__(self, "resource_id", optional_text(self.resource_id))
        object.__setattr__(self, "required_capacity", max(0.0, float(self.required_capacity)))
        object.__setattr__(self, "communication_bytes", max(0, int(self.communication_bytes)))
        object.__setattr__(self, "reason", optional_text(self.reason))

    def to_dict(self) -> dict[str, Any]:
        return thaw_json(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TopologyOperation":
        expected = value.get("expected_entity_revision")
        return cls(
            kind=TopologyOperationKind(str(value.get("kind") or "")),
            entity_id=str(value.get("entity_id") or ""),
            value=FrozenDict(_mapping(value.get("value"))),
            expected_entity_revision=int(expected) if expected is not None else None,
            required_permissions=tuple(value.get("required_permissions") or ()),
            requested_placement=str(value.get("requested_placement") or ""),
            resource_id=str(value.get("resource_id") or ""),
            required_capacity=float(value.get("required_capacity") or 0),
            communication_bytes=int(value.get("communication_bytes") or 0),
            reason=str(value.get("reason") or ""),
        )


class PolicyDecisionDisposition(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    PROJECT = "project"
    REBASE = "rebase"
    CONFLICT = "conflict"
    REPLAY = "replay"
    DIAGNOSTIC_ONLY = "diagnostic_only"


@dataclass(frozen=True, slots=True)
class ConstraintResult:
    constraint_id: str
    passed: bool
    reason_code: str
    message: str
    evidence_refs: tuple[str, ...] = ()
    details: FrozenDict = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "constraint_id", required_text(self.constraint_id, "constraint_id"))
        object.__setattr__(self, "reason_code", required_text(self.reason_code, "reason_code"))
        object.__setattr__(self, "message", required_text(self.message, "constraint message"))
        object.__setattr__(self, "evidence_refs", normalized_tokens(self.evidence_refs))
        object.__setattr__(self, "details", FrozenDict(self.details))

    def to_dict(self) -> dict[str, Any]:
        return thaw_json(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ConstraintResult":
        return cls(
            constraint_id=str(value.get("constraint_id") or ""),
            passed=bool(value.get("passed", False)),
            reason_code=str(value.get("reason_code") or ""),
            message=str(value.get("message") or ""),
            evidence_refs=tuple(value.get("evidence_refs") or ()),
            details=FrozenDict(_mapping(value.get("details"))),
        )


ContractT = TypeVar("ContractT", bound="PolicyContract")


@dataclass(frozen=True, slots=True)
class PolicyContract:
    header: ContractHeader

    SCHEMA_VERSION: ClassVar[str] = ""
    LEGACY_SCHEMA_VERSIONS: ClassVar[tuple[str, ...]] = ()
    CONTRACT_KIND: ClassVar[str] = ""

    def payload_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    def canonical_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "contract_kind": self.CONTRACT_KIND,
            **self.header.to_dict(),
            "payload": self.payload_dict(),
        }

    @property
    def canonical_serialization(self) -> str:
        return canonical_json(self.canonical_data())

    @property
    def digest(self) -> str:
        return canonical_digest(self.canonical_data())

    def to_dict(self) -> dict[str, Any]:
        return {**self.canonical_data(), "digest": self.digest}

    def _verify_supplied_digest(self, supplied: str) -> None:
        if supplied and supplied != self.digest:
            raise PolicyDigestMismatch(
                f"{self.CONTRACT_KIND} digest mismatch: expected {self.digest}, received {supplied}"
            )


@dataclass(frozen=True, slots=True)
class MechanismEvidenceReadinessReportRef(PolicyContract):
    report_ref: str
    report_digest: str
    readiness_stage: str
    status: str

    SCHEMA_VERSION = "zyra.mechanism-evidence-readiness-report-ref/v1"
    LEGACY_SCHEMA_VERSIONS = ("zyra.mechanism-evidence-readiness-report-ref/v0",)
    CONTRACT_KIND = "mechanism_evidence_readiness_report_ref"

    def __post_init__(self) -> None:
        object.__setattr__(self, "report_ref", required_text(self.report_ref, "report_ref"))
        digest_value = required_sha256(self.report_digest, "report_digest")
        object.__setattr__(self, "report_digest", digest_value)
        if self.readiness_stage not in {"input_precheck", "implementation_validated", "activation_ready"}:
            raise PolicyContractError("unknown readiness_stage")
        if self.status not in {"deterministic_ready", "evidence_only", "unavailable"}:
            raise PolicyContractError("unknown readiness status")

    def payload_dict(self) -> dict[str, Any]:
        return {
            "report_ref": self.report_ref,
            "report_digest": self.report_digest,
            "readiness_stage": self.readiness_stage,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MechanismEvidenceReadinessReportRef":
        header, payload, supplied = _contract_parts(cls, value)
        result = cls(
            header=header,
            report_ref=str(payload.get("report_ref") or ""),
            report_digest=str(payload.get("report_digest") or ""),
            readiness_stage=str(payload.get("readiness_stage") or payload.get("stage") or ""),
            status=str(payload.get("status") or ""),
        )
        result._verify_supplied_digest(supplied)
        return result


@dataclass(frozen=True, slots=True)
class EnvironmentSnapshot(PolicyContract):
    observed_at: str
    observations: tuple[TelemetryObservation, ...]
    required_categories: tuple[str, ...] = ()
    missing_categories: tuple[str, ...] = ()

    SCHEMA_VERSION = "zyra.environment-snapshot/v1"
    LEGACY_SCHEMA_VERSIONS = ("zyra.environment-snapshot/v0",)
    CONTRACT_KIND = "environment_snapshot"

    def __post_init__(self) -> None:
        parse_timestamp(self.observed_at, "environment observed_at")
        object.__setattr__(
            self,
            "observations",
            tuple(sorted(self.observations, key=lambda item: (item.category, item.resource_id, item.observation_id))),
        )
        object.__setattr__(self, "required_categories", normalized_tokens(self.required_categories))
        object.__setattr__(self, "missing_categories", normalized_tokens(self.missing_categories))

    def payload_dict(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at,
            "observations": [item.to_dict() for item in self.observations],
            "required_categories": list(self.required_categories),
            "missing_categories": list(self.missing_categories),
        }

    @property
    def observation_by_resource(self) -> Mapping[str, TelemetryObservation]:
        return {item.resource_id: item for item in self.observations}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EnvironmentSnapshot":
        header, payload, supplied = _contract_parts(cls, value)
        result = cls(
            header=header,
            observed_at=str(payload.get("observed_at") or header.created_at),
            observations=tuple(
                TelemetryObservation.from_mapping(_mapping(item))
                for item in _sequence(payload.get("observations"))
            ),
            required_categories=tuple(_sequence(payload.get("required_categories"))),
            missing_categories=tuple(_sequence(payload.get("missing_categories"))),
        )
        result._verify_supplied_digest(supplied)
        return result


@dataclass(frozen=True, slots=True)
class PolicyInputSnapshot(PolicyContract):
    run_id: str
    task_id: str
    phase: str
    requirement_revision: str
    graph: GraphSnapshotRef
    nodes: tuple[PolicyNodeSnapshot, ...]
    registered_roles: tuple[str, ...]
    registered_capabilities: tuple[str, ...]
    unresolved_obligations: tuple[str, ...]
    registry_versions: FrozenDict
    environment: EnvironmentSnapshot
    memory_refs: tuple[StableArtifactRef, ...]
    readiness_refs: tuple[MechanismEvidenceReadinessReportRef, ...]
    budget: PolicyBudget
    allowed_permissions: tuple[str, ...]
    allowed_placements: tuple[str, ...]
    privacy_class: str
    last_topology_change_at: str

    SCHEMA_VERSION = "zyra.policy-input-snapshot/v1"
    LEGACY_SCHEMA_VERSIONS = ("zyra.policy-input-snapshot/v0",)
    CONTRACT_KIND = "policy_input_snapshot"

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", required_text(self.run_id, "run_id"))
        object.__setattr__(self, "task_id", required_text(self.task_id, "task_id"))
        object.__setattr__(self, "phase", required_text(self.phase, "phase"))
        object.__setattr__(
            self,
            "requirement_revision",
            required_text(self.requirement_revision, "requirement_revision"),
        )
        if self.graph.run_id != self.run_id:
            raise PolicyContractError("policy input graph belongs to another run")
        object.__setattr__(self, "nodes", tuple(sorted(self.nodes, key=lambda item: item.node_id)))
        object.__setattr__(self, "registered_roles", normalized_tokens(self.registered_roles))
        object.__setattr__(
            self,
            "registered_capabilities",
            normalized_tokens(self.registered_capabilities),
        )
        object.__setattr__(
            self,
            "unresolved_obligations",
            normalized_tokens(self.unresolved_obligations),
        )
        object.__setattr__(self, "registry_versions", FrozenDict(self.registry_versions))
        object.__setattr__(
            self,
            "memory_refs",
            tuple(sorted(self.memory_refs, key=lambda item: item.ref_id)),
        )
        object.__setattr__(
            self,
            "readiness_refs",
            tuple(sorted(self.readiness_refs, key=lambda item: item.header.mechanism_id)),
        )
        object.__setattr__(self, "allowed_permissions", normalized_tokens(self.allowed_permissions))
        object.__setattr__(self, "allowed_placements", normalized_tokens(self.allowed_placements))
        object.__setattr__(self, "privacy_class", required_text(self.privacy_class, "privacy_class"))
        parse_timestamp(self.last_topology_change_at, "last_topology_change_at")

    def payload_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "phase": self.phase,
            "requirement_revision": self.requirement_revision,
            "graph": self.graph.to_dict(),
            "nodes": [item.to_dict() for item in self.nodes],
            "registered_roles": list(self.registered_roles),
            "registered_capabilities": list(self.registered_capabilities),
            "unresolved_obligations": list(self.unresolved_obligations),
            "registry_versions": thaw_json(self.registry_versions),
            "environment": self.environment.to_dict(),
            "memory_refs": [item.to_dict() for item in self.memory_refs],
            "readiness_refs": [item.to_dict() for item in self.readiness_refs],
            "budget": self.budget.to_dict(),
            "allowed_permissions": list(self.allowed_permissions),
            "allowed_placements": list(self.allowed_placements),
            "privacy_class": self.privacy_class,
            "last_topology_change_at": self.last_topology_change_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PolicyInputSnapshot":
        header, payload, supplied = _contract_parts(cls, value)
        result = cls(
            header=header,
            run_id=str(payload.get("run_id") or ""),
            task_id=str(payload.get("task_id") or ""),
            phase=str(payload.get("phase") or ""),
            requirement_revision=str(payload.get("requirement_revision") or ""),
            graph=GraphSnapshotRef.from_mapping(_mapping(payload.get("graph"))),
            nodes=tuple(
                PolicyNodeSnapshot.from_mapping(_mapping(item))
                for item in _sequence(payload.get("nodes"))
            ),
            registered_roles=tuple(_sequence(payload.get("registered_roles"))),
            registered_capabilities=tuple(_sequence(payload.get("registered_capabilities"))),
            unresolved_obligations=tuple(_sequence(payload.get("unresolved_obligations"))),
            registry_versions=FrozenDict(_mapping(payload.get("registry_versions"))),
            environment=EnvironmentSnapshot.from_dict(_mapping(payload.get("environment"))),
            memory_refs=tuple(
                StableArtifactRef.from_mapping(_mapping(item))
                for item in _sequence(payload.get("memory_refs"))
            ),
            readiness_refs=tuple(
                MechanismEvidenceReadinessReportRef.from_dict(_mapping(item))
                for item in _sequence(payload.get("readiness_refs"))
            ),
            budget=PolicyBudget.from_mapping(_mapping(payload.get("budget"))),
            allowed_permissions=tuple(_sequence(payload.get("allowed_permissions"))),
            allowed_placements=tuple(_sequence(payload.get("allowed_placements"))),
            privacy_class=str(payload.get("privacy_class") or "internal"),
            last_topology_change_at=str(
                payload.get("last_topology_change_at") or "1970-01-01T00:00:00Z"
            ),
        )
        result._verify_supplied_digest(supplied)
        return result


@dataclass(frozen=True, slots=True)
class TopologyProposalArtifact(PolicyContract):
    proposal_id: str
    input_snapshot_digest: str
    base_graph: GraphSnapshotRef
    operations: tuple[TopologyOperation, ...]
    expected_outcome: FrozenDict
    alternatives: tuple[StableArtifactRef, ...]
    reasons: tuple[str, ...]
    constraint_assumptions: tuple[str, ...]
    expires_at: str
    fallback_profile: str

    SCHEMA_VERSION = "zyra.topology-proposal-artifact/v1"
    LEGACY_SCHEMA_VERSIONS = ("zyra.topology-proposal-artifact/v0",)
    CONTRACT_KIND = "topology_proposal_artifact"

    def __post_init__(self) -> None:
        object.__setattr__(self, "proposal_id", required_text(self.proposal_id, "proposal_id"))
        input_digest = required_sha256(
            self.input_snapshot_digest,
            "input_snapshot_digest",
        )
        object.__setattr__(self, "input_snapshot_digest", input_digest)
        object.__setattr__(
            self,
            "operations",
            tuple(sorted(self.operations, key=lambda item: (item.kind.value, item.entity_id))),
        )
        if not self.operations:
            raise PolicyContractError("topology proposal must contain an operation")
        object.__setattr__(self, "expected_outcome", FrozenDict(self.expected_outcome))
        object.__setattr__(
            self,
            "alternatives",
            tuple(sorted(self.alternatives, key=lambda item: item.ref_id)),
        )
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons))
        object.__setattr__(
            self,
            "constraint_assumptions",
            tuple(str(item) for item in self.constraint_assumptions),
        )
        parse_timestamp(self.expires_at, "expires_at")
        object.__setattr__(
            self,
            "fallback_profile",
            required_text(self.fallback_profile, "fallback_profile"),
        )

    def payload_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "input_snapshot_digest": self.input_snapshot_digest,
            "base_graph": self.base_graph.to_dict(),
            "operations": [item.to_dict() for item in self.operations],
            "expected_outcome": thaw_json(self.expected_outcome),
            "alternatives": [item.to_dict() for item in self.alternatives],
            "reasons": list(self.reasons),
            "constraint_assumptions": list(self.constraint_assumptions),
            "expires_at": self.expires_at,
            "fallback_profile": self.fallback_profile,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TopologyProposalArtifact":
        header, payload, supplied = _contract_parts(cls, value)
        result = cls(
            header=header,
            proposal_id=str(payload.get("proposal_id") or header.contract_id),
            input_snapshot_digest=str(payload.get("input_snapshot_digest") or ""),
            base_graph=GraphSnapshotRef.from_mapping(
                _mapping(payload.get("base_graph") or payload.get("base_graph_version"))
            ),
            operations=tuple(
                TopologyOperation.from_mapping(_mapping(item))
                for item in _sequence(payload.get("operations"))
            ),
            expected_outcome=FrozenDict(
                _mapping(payload.get("expected_outcome") or payload.get("expected_metrics"))
            ),
            alternatives=tuple(
                StableArtifactRef.from_mapping(_mapping(item))
                for item in _sequence(payload.get("alternatives"))
            ),
            reasons=tuple(_sequence(payload.get("reasons"))),
            constraint_assumptions=tuple(_sequence(payload.get("constraint_assumptions"))),
            expires_at=str(payload.get("expires_at") or header.created_at),
            fallback_profile=str(
                payload.get("fallback_profile") or "phase1_deterministic_baseline"
            ),
        )
        result._verify_supplied_digest(supplied)
        return result


@dataclass(frozen=True, slots=True)
class PolicyDecisionReceipt(PolicyContract):
    decision_id: str
    proposal_id: str
    proposal_digest: str
    disposition: PolicyDecisionDisposition
    constraint_results: tuple[ConstraintResult, ...]
    projected_operations: tuple[TopologyOperation, ...] = ()
    projection_differences: tuple[str, ...] = ()
    delta_id: str = ""
    delta_digest: str = ""
    graph_commit: FrozenDict = field(default_factory=FrozenDict)
    fallback_profile: str = ""
    fallback_reason: str = ""

    SCHEMA_VERSION = "zyra.policy-decision-receipt/v1"
    LEGACY_SCHEMA_VERSIONS = ("zyra.policy-decision-receipt/v0",)
    CONTRACT_KIND = "policy_decision_receipt"

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", required_text(self.decision_id, "decision_id"))
        object.__setattr__(self, "proposal_id", required_text(self.proposal_id, "proposal_id"))
        proposal_digest = required_sha256(self.proposal_digest, "proposal_digest")
        object.__setattr__(self, "proposal_digest", proposal_digest)
        object.__setattr__(self, "disposition", PolicyDecisionDisposition(self.disposition))
        object.__setattr__(
            self,
            "constraint_results",
            tuple(sorted(self.constraint_results, key=lambda item: item.constraint_id)),
        )
        object.__setattr__(
            self,
            "projected_operations",
            tuple(sorted(self.projected_operations, key=lambda item: (item.kind.value, item.entity_id))),
        )
        object.__setattr__(self, "projection_differences", tuple(self.projection_differences))
        object.__setattr__(self, "graph_commit", FrozenDict(self.graph_commit))
        if self.delta_digest:
            object.__setattr__(
                self,
                "delta_digest",
                required_sha256(self.delta_digest, "delta_digest"),
            )

    @property
    def accepted(self) -> bool:
        return self.disposition in {
            PolicyDecisionDisposition.ACCEPT,
            PolicyDecisionDisposition.PROJECT,
            PolicyDecisionDisposition.REBASE,
            PolicyDecisionDisposition.REPLAY,
        }

    def payload_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "disposition": self.disposition.value,
            "constraint_results": [item.to_dict() for item in self.constraint_results],
            "projected_operations": [item.to_dict() for item in self.projected_operations],
            "projection_differences": list(self.projection_differences),
            "delta_id": self.delta_id,
            "delta_digest": self.delta_digest,
            "graph_commit": thaw_json(self.graph_commit),
            "fallback_profile": self.fallback_profile,
            "fallback_reason": self.fallback_reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PolicyDecisionReceipt":
        header, payload, supplied = _contract_parts(cls, value)
        result = cls(
            header=header,
            decision_id=str(payload.get("decision_id") or header.contract_id),
            proposal_id=str(payload.get("proposal_id") or ""),
            proposal_digest=str(payload.get("proposal_digest") or ""),
            disposition=PolicyDecisionDisposition(str(payload.get("disposition") or "reject")),
            constraint_results=tuple(
                ConstraintResult.from_mapping(_mapping(item))
                for item in _sequence(payload.get("constraint_results"))
            ),
            projected_operations=tuple(
                TopologyOperation.from_mapping(_mapping(item))
                for item in _sequence(payload.get("projected_operations"))
            ),
            projection_differences=tuple(_sequence(payload.get("projection_differences"))),
            delta_id=str(payload.get("delta_id") or ""),
            delta_digest=str(payload.get("delta_digest") or ""),
            graph_commit=FrozenDict(_mapping(payload.get("graph_commit"))),
            fallback_profile=str(payload.get("fallback_profile") or ""),
            fallback_reason=str(payload.get("fallback_reason") or ""),
        )
        result._verify_supplied_digest(supplied)
        return result


@dataclass(frozen=True, slots=True)
class PolicyOutcome(PolicyContract):
    proposal_ref: str
    decision_ref: str
    commit_ref: str
    verifier_result: str
    artifact_refs: tuple[StableArtifactRef, ...]
    metrics: FrozenDict
    permission_result: str
    recovery_result: str
    causal_refs: tuple[str, ...]

    SCHEMA_VERSION = "zyra.policy-outcome/v1"
    LEGACY_SCHEMA_VERSIONS = ("zyra.policy-outcome/v0",)
    CONTRACT_KIND = "policy_outcome"

    def __post_init__(self) -> None:
        object.__setattr__(self, "proposal_ref", required_text(self.proposal_ref, "proposal_ref"))
        object.__setattr__(self, "decision_ref", required_text(self.decision_ref, "decision_ref"))
        object.__setattr__(
            self,
            "artifact_refs",
            tuple(sorted(self.artifact_refs, key=lambda item: item.ref_id)),
        )
        object.__setattr__(self, "metrics", FrozenDict(self.metrics))
        object.__setattr__(self, "causal_refs", normalized_tokens(self.causal_refs))

    def payload_dict(self) -> dict[str, Any]:
        return {
            "proposal_ref": self.proposal_ref,
            "decision_ref": self.decision_ref,
            "commit_ref": self.commit_ref,
            "verifier_result": self.verifier_result,
            "artifact_refs": [item.to_dict() for item in self.artifact_refs],
            "metrics": thaw_json(self.metrics),
            "permission_result": self.permission_result,
            "recovery_result": self.recovery_result,
            "causal_refs": list(self.causal_refs),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PolicyOutcome":
        header, payload, supplied = _contract_parts(cls, value)
        result = cls(
            header=header,
            proposal_ref=str(payload.get("proposal_ref") or ""),
            decision_ref=str(payload.get("decision_ref") or ""),
            commit_ref=str(payload.get("commit_ref") or ""),
            verifier_result=str(payload.get("verifier_result") or ""),
            artifact_refs=tuple(
                StableArtifactRef.from_mapping(_mapping(item))
                for item in _sequence(payload.get("artifact_refs"))
            ),
            metrics=FrozenDict(_mapping(payload.get("metrics"))),
            permission_result=str(payload.get("permission_result") or ""),
            recovery_result=str(payload.get("recovery_result") or ""),
            causal_refs=tuple(_sequence(payload.get("causal_refs"))),
        )
        result._verify_supplied_digest(supplied)
        return result


@dataclass(frozen=True, slots=True)
class MemoryContinuityReceipt(PolicyContract):
    before_digest: str
    after_digest: str
    requirement_revision: str
    critical_fact_results: FrozenDict
    obligation_results: FrozenDict
    provenance_refs: tuple[StableArtifactRef, ...]
    rejected_memory_refs: tuple[StableArtifactRef, ...]
    downstream_decision_ref: str
    continuity_result: str

    SCHEMA_VERSION = "zyra.memory-continuity-receipt/v1"
    LEGACY_SCHEMA_VERSIONS = ("zyra.memory-continuity-receipt/v0",)
    CONTRACT_KIND = "memory_continuity_receipt"

    def __post_init__(self) -> None:
        for name in ("before_digest", "after_digest"):
            object.__setattr__(self, name, required_sha256(getattr(self, name), name))
        object.__setattr__(self, "critical_fact_results", FrozenDict(self.critical_fact_results))
        object.__setattr__(self, "obligation_results", FrozenDict(self.obligation_results))
        object.__setattr__(
            self,
            "provenance_refs",
            tuple(sorted(self.provenance_refs, key=lambda item: item.ref_id)),
        )
        object.__setattr__(
            self,
            "rejected_memory_refs",
            tuple(sorted(self.rejected_memory_refs, key=lambda item: item.ref_id)),
        )

    def payload_dict(self) -> dict[str, Any]:
        return {
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "requirement_revision": self.requirement_revision,
            "critical_fact_results": thaw_json(self.critical_fact_results),
            "obligation_results": thaw_json(self.obligation_results),
            "provenance_refs": [item.to_dict() for item in self.provenance_refs],
            "rejected_memory_refs": [item.to_dict() for item in self.rejected_memory_refs],
            "downstream_decision_ref": self.downstream_decision_ref,
            "continuity_result": self.continuity_result,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MemoryContinuityReceipt":
        header, payload, supplied = _contract_parts(cls, value)
        result = cls(
            header=header,
            before_digest=str(payload.get("before_digest") or ""),
            after_digest=str(payload.get("after_digest") or ""),
            requirement_revision=str(payload.get("requirement_revision") or ""),
            critical_fact_results=FrozenDict(_mapping(payload.get("critical_fact_results"))),
            obligation_results=FrozenDict(_mapping(payload.get("obligation_results"))),
            provenance_refs=tuple(
                StableArtifactRef.from_mapping(_mapping(item))
                for item in _sequence(payload.get("provenance_refs"))
            ),
            rejected_memory_refs=tuple(
                StableArtifactRef.from_mapping(_mapping(item))
                for item in _sequence(payload.get("rejected_memory_refs"))
            ),
            downstream_decision_ref=str(payload.get("downstream_decision_ref") or ""),
            continuity_result=str(payload.get("continuity_result") or ""),
        )
        result._verify_supplied_digest(supplied)
        return result


@dataclass(frozen=True, slots=True)
class NeuroSymbolicEvidenceBundle(PolicyContract):
    proposal_signal_mode: str
    proposal_ref: StableArtifactRef
    model_observation_refs: tuple[StableArtifactRef, ...]
    constraint_results: tuple[ConstraintResult, ...]
    projected_delta_ref: str
    commit_or_no_commit: FrozenDict
    permission_ref: str
    lease_ref: str
    verification_ref: str

    SCHEMA_VERSION = "zyra.neuro-symbolic-evidence-bundle/v1"
    LEGACY_SCHEMA_VERSIONS = ("zyra.neuro-symbolic-evidence-bundle/v0",)
    CONTRACT_KIND = "neuro_symbolic_evidence_bundle"

    def __post_init__(self) -> None:
        if self.proposal_signal_mode not in {
            "deterministic_only",
            "pretrained_model_assisted",
            "mixed",
        }:
            raise PolicyContractError("invalid proposal_signal_mode")
        object.__setattr__(
            self,
            "model_observation_refs",
            tuple(sorted(self.model_observation_refs, key=lambda item: item.ref_id)),
        )
        object.__setattr__(
            self,
            "constraint_results",
            tuple(sorted(self.constraint_results, key=lambda item: item.constraint_id)),
        )
        object.__setattr__(self, "commit_or_no_commit", FrozenDict(self.commit_or_no_commit))

    def payload_dict(self) -> dict[str, Any]:
        return {
            "proposal_signal_mode": self.proposal_signal_mode,
            "proposal_ref": self.proposal_ref.to_dict(),
            "model_observation_refs": [item.to_dict() for item in self.model_observation_refs],
            "constraint_results": [item.to_dict() for item in self.constraint_results],
            "projected_delta_ref": self.projected_delta_ref,
            "commit_or_no_commit": thaw_json(self.commit_or_no_commit),
            "permission_ref": self.permission_ref,
            "lease_ref": self.lease_ref,
            "verification_ref": self.verification_ref,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NeuroSymbolicEvidenceBundle":
        header, payload, supplied = _contract_parts(cls, value)
        result = cls(
            header=header,
            proposal_signal_mode=str(payload.get("proposal_signal_mode") or ""),
            proposal_ref=StableArtifactRef.from_mapping(_mapping(payload.get("proposal_ref"))),
            model_observation_refs=tuple(
                StableArtifactRef.from_mapping(_mapping(item))
                for item in _sequence(payload.get("model_observation_refs"))
            ),
            constraint_results=tuple(
                ConstraintResult.from_mapping(_mapping(item))
                for item in _sequence(payload.get("constraint_results"))
            ),
            projected_delta_ref=str(payload.get("projected_delta_ref") or ""),
            commit_or_no_commit=FrozenDict(_mapping(payload.get("commit_or_no_commit"))),
            permission_ref=str(payload.get("permission_ref") or ""),
            lease_ref=str(payload.get("lease_ref") or ""),
            verification_ref=str(payload.get("verification_ref") or ""),
        )
        result._verify_supplied_digest(supplied)
        return result


@dataclass(frozen=True, slots=True)
class PhysicalDispatchReceipt(PolicyContract):
    placement_decision_id: str
    alternatives: tuple[str, ...]
    input_signals: FrozenDict
    worker_manifest_ref: StableArtifactRef
    lease_id: str
    physical_attempt_id: str
    physical_identity: FrozenDict
    call_receipt: StableArtifactRef
    artifact_ref: StableArtifactRef
    verifier_ref: StableArtifactRef
    privacy_class: str
    allowed_placements: tuple[str, ...]
    permission_ref: str
    simulated: bool = False

    SCHEMA_VERSION = "zyra.physical-dispatch-receipt/v1"
    LEGACY_SCHEMA_VERSIONS = ("zyra.physical-dispatch-receipt/v0",)
    CONTRACT_KIND = "physical_dispatch_receipt"

    def __post_init__(self) -> None:
        for name in ("placement_decision_id", "lease_id", "physical_attempt_id"):
            object.__setattr__(self, name, required_text(getattr(self, name), name))
        object.__setattr__(self, "alternatives", normalized_tokens(self.alternatives))
        object.__setattr__(self, "input_signals", FrozenDict(self.input_signals))
        object.__setattr__(self, "physical_identity", FrozenDict(self.physical_identity))
        object.__setattr__(self, "allowed_placements", normalized_tokens(self.allowed_placements))

    def payload_dict(self) -> dict[str, Any]:
        return {
            "placement_decision_id": self.placement_decision_id,
            "alternatives": list(self.alternatives),
            "input_signals": thaw_json(self.input_signals),
            "worker_manifest_ref": self.worker_manifest_ref.to_dict(),
            "lease_id": self.lease_id,
            "physical_attempt_id": self.physical_attempt_id,
            "physical_identity": thaw_json(self.physical_identity),
            "call_receipt": self.call_receipt.to_dict(),
            "artifact_ref": self.artifact_ref.to_dict(),
            "verifier_ref": self.verifier_ref.to_dict(),
            "privacy_class": self.privacy_class,
            "allowed_placements": list(self.allowed_placements),
            "permission_ref": self.permission_ref,
            "simulated": self.simulated,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PhysicalDispatchReceipt":
        header, payload, supplied = _contract_parts(cls, value)
        result = cls(
            header=header,
            placement_decision_id=str(payload.get("placement_decision_id") or ""),
            alternatives=tuple(_sequence(payload.get("alternatives"))),
            input_signals=FrozenDict(_mapping(payload.get("input_signals"))),
            worker_manifest_ref=StableArtifactRef.from_mapping(
                _mapping(payload.get("worker_manifest_ref"))
            ),
            lease_id=str(payload.get("lease_id") or ""),
            physical_attempt_id=str(payload.get("physical_attempt_id") or ""),
            physical_identity=FrozenDict(_mapping(payload.get("physical_identity"))),
            call_receipt=StableArtifactRef.from_mapping(_mapping(payload.get("call_receipt"))),
            artifact_ref=StableArtifactRef.from_mapping(_mapping(payload.get("artifact_ref"))),
            verifier_ref=StableArtifactRef.from_mapping(_mapping(payload.get("verifier_ref"))),
            privacy_class=str(payload.get("privacy_class") or ""),
            allowed_placements=tuple(_sequence(payload.get("allowed_placements"))),
            permission_ref=str(payload.get("permission_ref") or ""),
            simulated=bool(payload.get("simulated", False)),
        )
        result._verify_supplied_digest(supplied)
        return result


POLICY_CONTRACT_TYPES: tuple[type[PolicyContract], ...] = (
    MechanismEvidenceReadinessReportRef,
    PolicyInputSnapshot,
    EnvironmentSnapshot,
    TopologyProposalArtifact,
    PolicyDecisionReceipt,
    PolicyOutcome,
    MemoryContinuityReceipt,
    NeuroSymbolicEvidenceBundle,
    PhysicalDispatchReceipt,
)

_POLICY_SCHEMA_READERS: dict[str, type[PolicyContract]] = {
    schema: contract_type
    for contract_type in POLICY_CONTRACT_TYPES
    for schema in (contract_type.SCHEMA_VERSION, *contract_type.LEGACY_SCHEMA_VERSIONS)
}


def parse_policy_contract(value: Mapping[str, Any]) -> PolicyContract:
    schema = str(value.get("schema_version") or value.get("schema") or "")
    contract_type = _POLICY_SCHEMA_READERS.get(schema)
    if contract_type is None:
        raise UnsupportedPolicySchema(f"unsupported policy contract schema: {schema or '<missing>'}")
    reader = getattr(contract_type, "from_dict", None)
    if not callable(reader):
        raise UnsupportedPolicySchema(f"policy contract has no reader: {schema}")
    return reader(value)


def policy_contract_schema_catalog() -> dict[str, Any]:
    return {
        "schema": "zyra.policy-contract-catalog/v1",
        "forward_compatibility": "unknown future schema versions are rejected",
        "same_version_extensions": "unknown additive fields are ignored when reading known versions",
        "legacy_compatibility": "declared v0 schemas are read and normalized to canonical v1",
        "canonical_serialization": "UTF-8 JSON, sorted keys, compact separators, NaN forbidden",
        "digest": "SHA-256 over canonical v1 data without the digest field",
        "contracts": [
            {
                "contract_kind": item.CONTRACT_KIND,
                "schema_version": item.SCHEMA_VERSION,
                "readable_legacy_versions": list(item.LEGACY_SCHEMA_VERSIONS),
                "required_header_fields": [
                    "contract_id",
                    "created_at",
                    "source_event_id",
                    "correlation_id",
                    "causation_id",
                    "mechanism_id",
                    "mechanism_version",
                    "input_version",
                    "idempotency_key",
                ],
                "json_schema": policy_contract_json_schema(item),
            }
            for item in POLICY_CONTRACT_TYPES
        ],
    }


def policy_contract_json_schema(contract_type: type[PolicyContract]) -> dict[str, Any]:
    if contract_type not in POLICY_CONTRACT_TYPES:
        raise UnsupportedPolicySchema("JSON schema requested for an unknown policy contract")
    payload_hints = get_type_hints(contract_type)
    payload_fields = [item for item in fields(contract_type) if item.name != "header"]
    header_hints = get_type_hints(ContractHeader)
    header_properties = {
        item.name: _json_schema_for_type(header_hints[item.name])
        for item in fields(ContractHeader)
    }
    header_properties["created_at"] = {"type": "string", "format": "date-time"}
    header_properties["configuration_digest"] = {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$|^$",
    }
    properties: dict[str, Any] = {
        "schema_version": {"const": contract_type.SCHEMA_VERSION},
        "contract_kind": {"const": contract_type.CONTRACT_KIND},
        **header_properties,
        "payload": {
            "type": "object",
            "required": [item.name for item in payload_fields],
            "properties": {
                item.name: _json_schema_for_type(payload_hints[item.name])
                for item in payload_fields
            },
            "additionalProperties": True,
        },
        "digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    }
    required_headers = [
        item.name
        for item in fields(ContractHeader)
        if item.default is MISSING and item.default_factory is MISSING
    ]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://schemas.zyra.local/{contract_type.SCHEMA_VERSION}",
        "title": contract_type.CONTRACT_KIND,
        "type": "object",
        "required": [
            "schema_version",
            "contract_kind",
            *required_headers,
            "payload",
            "digest",
        ],
        "properties": properties,
        "additionalProperties": True,
    }


def policy_contract_json_schemas() -> dict[str, dict[str, Any]]:
    return {
        contract_type.SCHEMA_VERSION: policy_contract_json_schema(contract_type)
        for contract_type in POLICY_CONTRACT_TYPES
    }


def _json_schema_for_type(annotation: Any) -> dict[str, Any]:
    if annotation is Any:
        return {}
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in {Union, types.UnionType}:
        return {"anyOf": [_json_schema_for_type(item) for item in args]}
    if origin in {tuple, list, set, frozenset, Sequence, Iterable}:
        item_type = args[0] if args else Any
        return {"type": "array", "items": _json_schema_for_type(item_type)}
    if origin in {dict, Mapping}:
        return {"type": "object", "additionalProperties": True}
    if annotation is FrozenDict:
        return {"type": "object", "additionalProperties": True}
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is type(None):
        return {"type": "null"}
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return {"type": "string", "enum": [item.value for item in annotation]}
    if isinstance(annotation, type) and is_dataclass(annotation):
        hints = get_type_hints(annotation)
        nested_fields = list(fields(annotation))
        return {
            "type": "object",
            "required": [item.name for item in nested_fields],
            "properties": {
                item.name: _json_schema_for_type(hints[item.name])
                for item in nested_fields
            },
            "additionalProperties": True,
        }
    return {}


def _contract_parts(
    contract_type: type[PolicyContract],
    value: Mapping[str, Any],
) -> tuple[ContractHeader, Mapping[str, Any], str]:
    data = dict(value)
    schema = str(data.get("schema_version") or data.get("schema") or "")
    if schema not in {contract_type.SCHEMA_VERSION, *contract_type.LEGACY_SCHEMA_VERSIONS}:
        raise UnsupportedPolicySchema(
            f"{contract_type.CONTRACT_KIND} cannot read schema {schema or '<missing>'}"
        )
    if schema == contract_type.SCHEMA_VERSION:
        required_header_fields = (
            "contract_id",
            "created_at",
            "source_event_id",
            "correlation_id",
            "causation_id",
            "mechanism_id",
            "mechanism_version",
            "input_version",
            "idempotency_key",
        )
        missing = [name for name in required_header_fields if not str(data.get(name) or "").strip()]
        if missing:
            raise PolicyContractError(
                f"{contract_type.CONTRACT_KIND} is missing current-schema header fields: "
                + ", ".join(missing)
            )
    supplied = str(data.get("digest") or "")
    payload_value = data.get("payload")
    if isinstance(payload_value, Mapping):
        payload = dict(payload_value)
    elif schema in contract_type.LEGACY_SCHEMA_VERSIONS:
        header_names = {item.name for item in fields(ContractHeader)}
        payload = {
            key: item
            for key, item in data.items()
            if key not in header_names | {"schema", "schema_version", "contract_kind", "digest"}
        }
    else:
        raise PolicyContractError(f"{contract_type.CONTRACT_KIND} payload must be an object")
    seed = canonical_digest({"schema": schema, "payload": payload})
    return ContractHeader.from_mapping(data, legacy_seed=seed), payload, supplied


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()
