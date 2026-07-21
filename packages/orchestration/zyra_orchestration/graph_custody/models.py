from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field, fields, is_dataclass, replace
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

from zyra_core import now_iso


TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,191}$")


def require_token(value: str, label: str) -> str:
    token = str(value or "").strip()
    if not token or not TOKEN_PATTERN.fullmatch(token):
        raise ValueError(f"{label} is missing or invalid")
    return token


def sorted_tokens(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({require_token(str(value), "token") for value in values if str(value or "").strip()}))


def primitive(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {item.name: primitive(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [primitive(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(primitive(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def graph_id(prefix: str) -> str:
    return f"{require_token(prefix, 'id prefix')}_{uuid4().hex}"


class GraphMutationKind(StrEnum):
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


class GraphConflictKind(StrEnum):
    STALE_BASE = "stale_base"
    READ_WRITE = "read_write"
    WRITE_WRITE = "write_write"
    MISSING_ENTITY = "missing_entity"
    ENTITY_EXISTS = "entity_exists"
    DEPENDENCY_VIOLATION = "dependency_violation"
    CYCLE_DETECTED = "cycle_detected"
    IDEMPOTENCY_MISMATCH = "idempotency_mismatch"
    INVALID_MUTATION = "invalid_mutation"


class GraphConflictStrategy(StrEnum):
    SERIALIZE = "serialize"
    REBASE = "rebase"
    REPLAN = "replan"


class GraphCommitStatus(StrEnum):
    COMMITTED = "committed"
    REBASED = "rebased"
    REPLAYED = "replayed"
    CONFLICTED = "conflicted"
    REPLAN_REQUIRED = "replan_required"


class NodeExecutionState(StrEnum):
    PLANNED = "planned"
    READY = "ready"
    LEASED = "leased"
    RUNNING = "running"
    BLOCKED = "blocked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class GraphNode:
    node_id: str
    role: str
    capabilities: tuple[str, ...]
    dependencies: tuple[str, ...] = ()
    state: NodeExecutionState = NodeExecutionState.PLANNED
    logical_task_id: str = ""
    physical_attempt_ref: str = ""
    worker_lease_ref: str = ""
    workspace_ref: str = ""
    backend_route_ref: str = ""
    artifact_refs: tuple[str, ...] = ()
    labels: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    revision: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", require_token(self.node_id, "node_id"))
        object.__setattr__(self, "role", require_token(self.role, "role"))
        object.__setattr__(self, "capabilities", sorted_tokens(self.capabilities))
        object.__setattr__(self, "dependencies", sorted_tokens(self.dependencies))
        object.__setattr__(self, "artifact_refs", sorted_tokens(self.artifact_refs))
        object.__setattr__(self, "labels", {require_token(str(k), "label key"): str(v) for k, v in self.labels.items()})
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))
        object.__setattr__(self, "revision", max(1, int(self.revision)))
        if self.node_id in self.dependencies:
            raise ValueError("graph node cannot depend on itself")

    @property
    def terminal(self) -> bool:
        return self.state in {
            NodeExecutionState.SUCCEEDED,
            NodeExecutionState.FAILED,
            NodeExecutionState.CANCELLED,
            NodeExecutionState.SUPERSEDED,
        }

    def revise(self, **changes: Any) -> "GraphNode":
        return replace(self, revision=self.revision + 1, updated_at=now_iso(), **changes)

    def to_dict(self) -> dict[str, Any]:
        return dict(primitive(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GraphNode":
        data = dict(value)
        return cls(
            node_id=str(data.get("node_id") or ""),
            role=str(data.get("role") or ""),
            capabilities=tuple(str(item) for item in data.get("capabilities") or ()),
            dependencies=tuple(str(item) for item in data.get("dependencies") or ()),
            state=_enum(NodeExecutionState, data.get("state"), NodeExecutionState.PLANNED),
            logical_task_id=str(data.get("logical_task_id") or ""),
            physical_attempt_ref=str(data.get("physical_attempt_ref") or ""),
            worker_lease_ref=str(data.get("worker_lease_ref") or ""),
            workspace_ref=str(data.get("workspace_ref") or ""),
            backend_route_ref=str(data.get("backend_route_ref") or ""),
            artifact_refs=tuple(str(item) for item in data.get("artifact_refs") or ()),
            labels={str(k): str(v) for k, v in dict(data.get("labels") or {}).items()},
            metadata=dict(data.get("metadata") or {}),
            created_at=str(data.get("created_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
            revision=int(data.get("revision") or 1),
        )


@dataclass(frozen=True, slots=True)
class GraphEdge:
    edge_id: str
    source_node_id: str
    target_node_id: str
    relation: str
    required_capabilities: tuple[str, ...] = ()
    condition: Mapping[str, Any] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    revision: int = 1

    def __post_init__(self) -> None:
        for name in ("edge_id", "source_node_id", "target_node_id", "relation"):
            object.__setattr__(self, name, require_token(getattr(self, name), name))
        if self.source_node_id == self.target_node_id:
            raise ValueError("graph self edges are not supported")
        object.__setattr__(self, "required_capabilities", sorted_tokens(self.required_capabilities))
        object.__setattr__(self, "condition", copy.deepcopy(dict(self.condition)))
        object.__setattr__(self, "labels", {require_token(str(k), "label key"): str(v) for k, v in self.labels.items()})
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))
        object.__setattr__(self, "revision", max(1, int(self.revision)))

    def revise(self, **changes: Any) -> "GraphEdge":
        return replace(self, revision=self.revision + 1, updated_at=now_iso(), **changes)

    def to_dict(self) -> dict[str, Any]:
        return dict(primitive(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GraphEdge":
        data = dict(value)
        return cls(
            edge_id=str(data.get("edge_id") or ""),
            source_node_id=str(data.get("source_node_id") or ""),
            target_node_id=str(data.get("target_node_id") or ""),
            relation=str(data.get("relation") or ""),
            required_capabilities=tuple(str(item) for item in data.get("required_capabilities") or ()),
            condition=dict(data.get("condition") or {}),
            labels={str(k): str(v) for k, v in dict(data.get("labels") or {}).items()},
            metadata=dict(data.get("metadata") or {}),
            created_at=str(data.get("created_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
            revision=int(data.get("revision") or 1),
        )


@dataclass(frozen=True, slots=True)
class GraphStateSnapshot:
    graph_id: str
    run_id: str
    revision: int
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    parent_revision: int = 0
    commit_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    signature: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "graph_id", require_token(self.graph_id, "graph_id"))
        object.__setattr__(self, "run_id", require_token(self.run_id, "run_id"))
        object.__setattr__(self, "revision", max(0, int(self.revision)))
        object.__setattr__(self, "parent_revision", max(0, int(self.parent_revision)))
        node_ids = [item.node_id for item in self.nodes]
        edge_ids = [item.edge_id for item in self.edges]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("graph snapshot has duplicate node ids")
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("graph snapshot has duplicate edge ids")
        object.__setattr__(self, "nodes", tuple(sorted(self.nodes, key=lambda item: item.node_id)))
        object.__setattr__(self, "edges", tuple(sorted(self.edges, key=lambda item: item.edge_id)))
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))
        expected = self.compute_signature()
        if self.signature and self.signature != expected:
            raise ValueError("graph snapshot signature does not match immutable content")
        object.__setattr__(self, "signature", expected)

    @property
    def node_map(self) -> Mapping[str, GraphNode]:
        return {item.node_id: item for item in self.nodes}

    @property
    def edge_map(self) -> Mapping[str, GraphEdge]:
        return {item.edge_id: item for item in self.edges}

    def compute_signature(self) -> str:
        return digest(
            {
                "graph_id": self.graph_id,
                "run_id": self.run_id,
                "revision": self.revision,
                "parent_revision": self.parent_revision,
                "commit_id": self.commit_id,
                "nodes": self.nodes,
                "edges": self.edges,
                "metadata": self.metadata,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(primitive(self))

    @classmethod
    def empty(cls, graph_id: str, run_id: str, *, metadata: Mapping[str, Any] | None = None) -> "GraphStateSnapshot":
        return cls(
            graph_id=graph_id,
            run_id=run_id,
            revision=0,
            nodes=(),
            edges=(),
            metadata={
                **dict(metadata or {}),
                "graph_state_owner": "GraphStateCustody",
                "topology_owner": "DynamicTopologyRuntime",
            },
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GraphStateSnapshot":
        data = dict(value)
        return cls(
            graph_id=str(data.get("graph_id") or ""),
            run_id=str(data.get("run_id") or ""),
            revision=int(data.get("revision") or 0),
            nodes=tuple(GraphNode.from_dict(item) for item in data.get("nodes") or ()),
            edges=tuple(GraphEdge.from_dict(item) for item in data.get("edges") or ()),
            parent_revision=int(data.get("parent_revision") or 0),
            commit_id=str(data.get("commit_id") or ""),
            metadata=dict(data.get("metadata") or {}),
            created_at=str(data.get("created_at") or now_iso()),
            signature=str(data.get("signature") or ""),
        )


@dataclass(frozen=True, slots=True)
class GraphMutation:
    kind: GraphMutationKind
    entity_id: str
    value: Mapping[str, Any] = field(default_factory=dict)
    mutation_id: str = field(default_factory=lambda: graph_id("mutation"))
    expected_entity_revision: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "entity_id", require_token(self.entity_id, "entity_id"))
        object.__setattr__(self, "value", copy.deepcopy(dict(self.value)))
        if self.expected_entity_revision is not None and self.expected_entity_revision < 1:
            raise ValueError("expected entity revision must be positive")

    @property
    def write_keys(self) -> tuple[str, ...]:
        if self.kind in {
            GraphMutationKind.ADD_NODE,
            GraphMutationKind.REMOVE_NODE,
            GraphMutationKind.REPLACE_NODE,
            GraphMutationKind.SET_NODE_ROLE,
            GraphMutationKind.SET_NODE_CAPABILITIES,
            GraphMutationKind.SET_NODE_DEPENDENCIES,
        }:
            return (f"node:{self.entity_id}",)
        if self.kind in {
            GraphMutationKind.ADD_EDGE,
            GraphMutationKind.REMOVE_EDGE,
            GraphMutationKind.REPLACE_EDGE,
        }:
            return (f"edge:{self.entity_id}",)
        return (f"metadata:{self.entity_id}",)

    def to_dict(self) -> dict[str, Any]:
        return dict(primitive(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GraphMutation":
        data = dict(value)
        expected = data.get("expected_entity_revision")
        return cls(
            kind=_enum(GraphMutationKind, data.get("kind"), GraphMutationKind.SET_GRAPH_METADATA),
            entity_id=str(data.get("entity_id") or ""),
            value=dict(data.get("value") or {}),
            mutation_id=str(data.get("mutation_id") or graph_id("mutation")),
            expected_entity_revision=int(expected) if expected is not None else None,
        )


@dataclass(frozen=True, slots=True)
class BranchGraphDelta:
    graph_id: str
    run_id: str
    branch_id: str
    base_revision: int
    base_signature: str
    mutations: tuple[GraphMutation, ...]
    read_set: tuple[str, ...]
    write_set: tuple[str, ...]
    causation_id: str
    correlation_id: str
    idempotency_key: str
    actor_id: str
    delta_id: str = field(default_factory=lambda: graph_id("delta"))
    created_at: str = field(default_factory=now_iso)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    content_digest: str = ""

    def __post_init__(self) -> None:
        for name in ("graph_id", "run_id", "branch_id", "actor_id"):
            object.__setattr__(self, name, require_token(getattr(self, name), name))
        object.__setattr__(self, "base_revision", max(0, int(self.base_revision)))
        object.__setattr__(self, "read_set", sorted_tokens(self.read_set))
        computed_write_set = sorted_tokens(key for mutation in self.mutations for key in mutation.write_keys)
        supplied = sorted_tokens(self.write_set)
        if supplied and supplied != computed_write_set:
            raise ValueError("delta write set does not match mutations")
        object.__setattr__(self, "write_set", computed_write_set)
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", digest((self.graph_id, self.branch_id, self.base_revision, self.mutations)))
        expected = self.compute_digest()
        if self.content_digest and self.content_digest != expected:
            raise ValueError("graph delta digest does not match immutable content")
        object.__setattr__(self, "content_digest", expected)

    def compute_digest(self) -> str:
        return digest(
            {
                "graph_id": self.graph_id,
                "run_id": self.run_id,
                "branch_id": self.branch_id,
                "base_revision": self.base_revision,
                "base_signature": self.base_signature,
                "mutations": self.mutations,
                "read_set": self.read_set,
                "write_set": self.write_set,
                "causation_id": self.causation_id,
                "correlation_id": self.correlation_id,
                "idempotency_key": self.idempotency_key,
                "actor_id": self.actor_id,
                "metadata": self.metadata,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(primitive(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BranchGraphDelta":
        data = dict(value)
        return cls(
            graph_id=str(data.get("graph_id") or ""),
            run_id=str(data.get("run_id") or ""),
            branch_id=str(data.get("branch_id") or ""),
            base_revision=int(data.get("base_revision") or 0),
            base_signature=str(data.get("base_signature") or ""),
            mutations=tuple(GraphMutation.from_dict(item) for item in data.get("mutations") or ()),
            read_set=tuple(str(item) for item in data.get("read_set") or ()),
            write_set=tuple(str(item) for item in data.get("write_set") or ()),
            causation_id=str(data.get("causation_id") or ""),
            correlation_id=str(data.get("correlation_id") or ""),
            idempotency_key=str(data.get("idempotency_key") or ""),
            actor_id=str(data.get("actor_id") or ""),
            delta_id=str(data.get("delta_id") or graph_id("delta")),
            created_at=str(data.get("created_at") or now_iso()),
            metadata=dict(data.get("metadata") or {}),
            content_digest=str(data.get("content_digest") or ""),
        )


@dataclass(frozen=True, slots=True)
class GraphConflict:
    kind: GraphConflictKind
    key: str
    message: str
    base_revision: int
    current_revision: int
    conflicting_delta_ids: tuple[str, ...] = ()
    recoverable: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(primitive(self))


@dataclass(frozen=True, slots=True)
class GraphCommitReceipt:
    graph_id: str
    delta_id: str
    status: GraphCommitStatus
    strategy: GraphConflictStrategy
    base_revision: int
    committed_revision: int
    snapshot_signature: str
    commit_id: str = field(default_factory=lambda: graph_id("graph_commit"))
    conflicts: tuple[GraphConflict, ...] = ()
    rebased_from_revision: int | None = None
    created_at: str = field(default_factory=now_iso)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def committed(self) -> bool:
        return self.status in {
            GraphCommitStatus.COMMITTED,
            GraphCommitStatus.REBASED,
            GraphCommitStatus.REPLAYED,
        }

    def to_dict(self) -> dict[str, Any]:
        return dict(primitive(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GraphCommitReceipt":
        data = dict(value)
        rebased = data.get("rebased_from_revision")
        return cls(
            graph_id=str(data.get("graph_id") or ""),
            delta_id=str(data.get("delta_id") or ""),
            status=_enum(GraphCommitStatus, data.get("status"), GraphCommitStatus.CONFLICTED),
            strategy=_enum(GraphConflictStrategy, data.get("strategy"), GraphConflictStrategy.SERIALIZE),
            base_revision=int(data.get("base_revision") or 0),
            committed_revision=int(data.get("committed_revision") or 0),
            snapshot_signature=str(data.get("snapshot_signature") or ""),
            commit_id=str(data.get("commit_id") or graph_id("graph_commit")),
            conflicts=tuple(
                GraphConflict(
                    kind=_enum(GraphConflictKind, item.get("kind"), GraphConflictKind.INVALID_MUTATION),
                    key=str(item.get("key") or ""),
                    message=str(item.get("message") or ""),
                    base_revision=int(item.get("base_revision") or 0),
                    current_revision=int(item.get("current_revision") or 0),
                    conflicting_delta_ids=tuple(str(value) for value in item.get("conflicting_delta_ids") or ()),
                    recoverable=bool(item.get("recoverable", True)),
                    metadata=dict(item.get("metadata") or {}),
                )
                for item in data.get("conflicts") or ()
            ),
            rebased_from_revision=int(rebased) if rebased is not None else None,
            created_at=str(data.get("created_at") or now_iso()),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class GraphVersionRef:
    graph_id: str
    run_id: str
    revision: int
    signature: str
    commit_id: str

    def to_dict(self) -> dict[str, Any]:
        return dict(primitive(self))


@dataclass(frozen=True, slots=True)
class GraphCustodyMap:
    topology_signature_owner: str = "GraphStateCustody"
    topology_revision_owner: str = "GraphStateCustody"
    node_dependency_owner: str = "DynamicTopologyRuntime"
    graph_snapshot_owner: str = "GraphStateCustody"
    graph_delta_owner: str = "GraphStateCustody"
    logical_task_owner: str = "typescript.AgentTaskRuntime"
    physical_attempt_owner: str = "WorkerLeaseManager"
    worker_lease_owner: str = "WorkerLeaseStore"
    workspace_owner: str = "WorkspaceManagerRuntime"
    backend_route_owner: str = "BackendRegistry"
    inflight_reference_owner: str = "RuntimeEventStore"
    checkpoint_reference_owner: str = "CheckpointRuntime-M1-07C"

    def to_dict(self) -> dict[str, Any]:
        return dict(primitive(self))


def _enum(enum_type: type[StrEnum], value: Any, default: Any) -> Any:
    try:
        return enum_type(str(value))
    except (TypeError, ValueError):
        return default
