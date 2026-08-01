from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

from .models import (
    BranchGraphDelta,
    GraphCommitReceipt,
    GraphCommitStatus,
    GraphConflict,
    GraphConflictKind,
    GraphConflictStrategy,
    GraphCustodyMap,
    GraphEdge,
    GraphMutation,
    GraphMutationKind,
    GraphNode,
    GraphStateSnapshot,
    GraphVersionRef,
    NodeExecutionState,
    digest,
    graph_id,
    now_iso,
    sorted_tokens,
)
from .store import GraphStateStore, GraphStoreConflict


class GraphMutationRejected(RuntimeError):
    def __init__(self, conflicts: Sequence[GraphConflict]) -> None:
        self.conflicts = tuple(conflicts)
        super().__init__("; ".join(item.message for item in conflicts) or "graph mutation rejected")


@dataclass(frozen=True, slots=True)
class GraphCommitResult:
    receipt: GraphCommitReceipt
    snapshot: GraphStateSnapshot
    delta: BranchGraphDelta

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt": self.receipt.to_dict(),
            "snapshot": self.snapshot.to_dict(),
            "delta": self.delta.to_dict(),
        }


class GraphDeltaBuilder:
    def __init__(
        self,
        snapshot: GraphStateSnapshot,
        *,
        branch_id: str,
        actor_id: str,
        causation_id: str,
        correlation_id: str = "",
        idempotency_key: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.branch_id = branch_id
        self.actor_id = actor_id
        self.causation_id = causation_id
        self.correlation_id = correlation_id
        self.idempotency_key = idempotency_key
        self.metadata = dict(metadata or {})
        self._mutations: list[GraphMutation] = []
        self._read_set: set[str] = set()

    def read_node(self, node_id: str) -> GraphNode | None:
        self._read_set.add(f"node:{node_id}")
        return self.snapshot.node_map.get(node_id)

    def read_edge(self, edge_id: str) -> GraphEdge | None:
        self._read_set.add(f"edge:{edge_id}")
        return self.snapshot.edge_map.get(edge_id)

    def read_metadata(self, key: str) -> Any:
        self._read_set.add(f"metadata:{key}")
        return copy.deepcopy(self.snapshot.metadata.get(key))

    def add_node(self, node: GraphNode) -> "GraphDeltaBuilder":
        self._mutations.append(
            GraphMutation(
                kind=GraphMutationKind.ADD_NODE,
                entity_id=node.node_id,
                value=node.to_dict(),
            )
        )
        return self

    def remove_node(self, node_id: str, *, expected_revision: int | None = None) -> "GraphDeltaBuilder":
        self._mutations.append(
            GraphMutation(
                kind=GraphMutationKind.REMOVE_NODE,
                entity_id=node_id,
                expected_entity_revision=expected_revision,
            )
        )
        return self

    def replace_node(self, node: GraphNode, *, expected_revision: int | None = None) -> "GraphDeltaBuilder":
        self._mutations.append(
            GraphMutation(
                kind=GraphMutationKind.REPLACE_NODE,
                entity_id=node.node_id,
                value=node.to_dict(),
                expected_entity_revision=expected_revision,
            )
        )
        return self

    def set_role(self, node_id: str, role: str, *, expected_revision: int | None = None) -> "GraphDeltaBuilder":
        self._mutations.append(
            GraphMutation(
                kind=GraphMutationKind.SET_NODE_ROLE,
                entity_id=node_id,
                value={"role": role},
                expected_entity_revision=expected_revision,
            )
        )
        return self

    def set_capabilities(
        self,
        node_id: str,
        capabilities: Iterable[str],
        *,
        expected_revision: int | None = None,
    ) -> "GraphDeltaBuilder":
        self._mutations.append(
            GraphMutation(
                kind=GraphMutationKind.SET_NODE_CAPABILITIES,
                entity_id=node_id,
                value={"capabilities": list(sorted_tokens(capabilities))},
                expected_entity_revision=expected_revision,
            )
        )
        return self

    def set_dependencies(
        self,
        node_id: str,
        dependencies: Iterable[str],
        *,
        expected_revision: int | None = None,
    ) -> "GraphDeltaBuilder":
        self._mutations.append(
            GraphMutation(
                kind=GraphMutationKind.SET_NODE_DEPENDENCIES,
                entity_id=node_id,
                value={"dependencies": list(sorted_tokens(dependencies))},
                expected_entity_revision=expected_revision,
            )
        )
        return self

    def add_edge(self, edge: GraphEdge) -> "GraphDeltaBuilder":
        self._mutations.append(
            GraphMutation(
                kind=GraphMutationKind.ADD_EDGE,
                entity_id=edge.edge_id,
                value=edge.to_dict(),
            )
        )
        return self

    def remove_edge(self, edge_id: str, *, expected_revision: int | None = None) -> "GraphDeltaBuilder":
        self._mutations.append(
            GraphMutation(
                kind=GraphMutationKind.REMOVE_EDGE,
                entity_id=edge_id,
                expected_entity_revision=expected_revision,
            )
        )
        return self

    def replace_edge(self, edge: GraphEdge, *, expected_revision: int | None = None) -> "GraphDeltaBuilder":
        self._mutations.append(
            GraphMutation(
                kind=GraphMutationKind.REPLACE_EDGE,
                entity_id=edge.edge_id,
                value=edge.to_dict(),
                expected_entity_revision=expected_revision,
            )
        )
        return self

    def set_metadata(self, key: str, value: Any) -> "GraphDeltaBuilder":
        self._mutations.append(
            GraphMutation(
                kind=GraphMutationKind.SET_GRAPH_METADATA,
                entity_id=key,
                value={"value": copy.deepcopy(value)},
            )
        )
        return self

    def remove_metadata(self, key: str) -> "GraphDeltaBuilder":
        self._mutations.append(
            GraphMutation(
                kind=GraphMutationKind.REMOVE_GRAPH_METADATA,
                entity_id=key,
            )
        )
        return self

    def build(self) -> BranchGraphDelta:
        if not self._mutations:
            raise ValueError("graph delta must contain at least one mutation")
        return BranchGraphDelta(
            graph_id=self.snapshot.graph_id,
            run_id=self.snapshot.run_id,
            branch_id=self.branch_id,
            base_revision=self.snapshot.revision,
            base_signature=self.snapshot.signature,
            mutations=tuple(self._mutations),
            read_set=tuple(self._read_set),
            write_set=(),
            causation_id=self.causation_id,
            correlation_id=self.correlation_id,
            idempotency_key=self.idempotency_key,
            actor_id=self.actor_id,
            metadata={
                **self.metadata,
                "snapshot_owner": "GraphStateCustody",
                "mutation_owner": "DynamicTopologyRuntime",
            },
        )


class GraphStateCustody:
    """Owns immutable graph snapshots, branch deltas, conflicts, and commits."""

    def __init__(self, store: GraphStateStore) -> None:
        self.store = store
        self.ownership = GraphCustodyMap()

    def create(self, *, graph_id_value: str, run_id: str, metadata: Mapping[str, Any] | None = None) -> GraphStateSnapshot:
        snapshot = GraphStateSnapshot.empty(
            graph_id_value,
            run_id,
            metadata={**self.ownership.to_dict(), **dict(metadata or {})},
        )
        return self.store.create_graph(snapshot)

    def current(self, graph_id_value: str) -> GraphStateSnapshot:
        return self.store.current(graph_id_value)

    def branch(
        self,
        graph_id_value: str,
        *,
        branch_id: str,
        actor_id: str,
        causation_id: str,
        correlation_id: str = "",
        idempotency_key: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> GraphDeltaBuilder:
        return GraphDeltaBuilder(
            self.current(graph_id_value),
            branch_id=branch_id,
            actor_id=actor_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )

    def commit(
        self,
        delta: BranchGraphDelta,
        *,
        strategy: GraphConflictStrategy = GraphConflictStrategy.SERIALIZE,
    ) -> GraphCommitResult:
        existing = self.store.commit_for_delta(delta.graph_id, delta.delta_id)
        if existing is not None:
            snapshot = self.store.snapshot(delta.graph_id, existing.committed_revision) or self.store.current(delta.graph_id)
            replayed = replace(existing, status=GraphCommitStatus.REPLAYED)
            return GraphCommitResult(receipt=replayed, snapshot=snapshot, delta=delta)
        stored_delta = self.store.save_delta(delta)
        current = self.store.current(delta.graph_id)
        conflicts = self.detect_conflicts(stored_delta, current)
        if conflicts:
            if strategy is GraphConflictStrategy.REBASE and self._rebaseable(conflicts):
                applied = self.apply(current, stored_delta)
                status = GraphCommitStatus.REBASED
                rebased_from = stored_delta.base_revision
            else:
                status = (
                    GraphCommitStatus.REPLAN_REQUIRED
                    if strategy is GraphConflictStrategy.REPLAN
                    else GraphCommitStatus.CONFLICTED
                )
                receipt = GraphCommitReceipt(
                    graph_id=delta.graph_id,
                    delta_id=delta.delta_id,
                    status=status,
                    strategy=strategy,
                    base_revision=delta.base_revision,
                    committed_revision=current.revision,
                    snapshot_signature=current.signature,
                    conflicts=conflicts,
                    metadata={"head_revision": current.revision},
                )
                persisted = self.store.save_receipt(receipt)
                return GraphCommitResult(receipt=persisted, snapshot=current, delta=stored_delta)
        else:
            applied = self.apply(current, stored_delta)
            if (
                strategy is GraphConflictStrategy.REBASE
                and stored_delta.base_revision < current.revision
            ):
                # A stale but disjoint branch still rebases onto the current
                # immutable head.  Label the receipt explicitly so callers can
                # distinguish this path from an in-order serialized commit.
                status = GraphCommitStatus.REBASED
                rebased_from = stored_delta.base_revision
            else:
                status = GraphCommitStatus.COMMITTED
                rebased_from = None
        commit_id = graph_id("graph_commit")
        snapshot = GraphStateSnapshot(
            graph_id=current.graph_id,
            run_id=current.run_id,
            revision=current.revision + 1,
            parent_revision=current.revision,
            commit_id=commit_id,
            nodes=applied.nodes,
            edges=applied.edges,
            metadata={
                **dict(applied.metadata),
                "last_delta_id": stored_delta.delta_id,
                "last_branch_id": stored_delta.branch_id,
                "last_actor_id": stored_delta.actor_id,
            },
        )
        receipt = GraphCommitReceipt(
            graph_id=delta.graph_id,
            delta_id=delta.delta_id,
            status=status,
            strategy=strategy,
            base_revision=delta.base_revision,
            committed_revision=snapshot.revision,
            snapshot_signature=snapshot.signature,
            commit_id=commit_id,
            conflicts=conflicts if status is GraphCommitStatus.REBASED else (),
            rebased_from_revision=rebased_from,
            metadata={
                "causation_id": delta.causation_id,
                "correlation_id": delta.correlation_id,
                "read_set": list(delta.read_set),
                "write_set": list(delta.write_set),
            },
        )
        try:
            persisted = self.store.commit(
                expected_head_revision=current.revision,
                snapshot=snapshot,
                delta=stored_delta,
                receipt=receipt,
            )
        except GraphStoreConflict:
            newest = self.store.current(delta.graph_id)
            raced = self.detect_conflicts(stored_delta, newest)
            receipt = GraphCommitReceipt(
                graph_id=delta.graph_id,
                delta_id=delta.delta_id,
                status=GraphCommitStatus.CONFLICTED,
                strategy=GraphConflictStrategy.SERIALIZE,
                base_revision=delta.base_revision,
                committed_revision=newest.revision,
                snapshot_signature=newest.signature,
                conflicts=raced
                or (
                    GraphConflict(
                        kind=GraphConflictKind.STALE_BASE,
                        key="graph:head",
                        message="graph head changed during atomic commit",
                        base_revision=delta.base_revision,
                        current_revision=newest.revision,
                    ),
                ),
            )
            persisted = self.store.save_receipt(receipt)
            return GraphCommitResult(receipt=persisted, snapshot=newest, delta=stored_delta)
        return GraphCommitResult(receipt=persisted, snapshot=snapshot, delta=stored_delta)

    def detect_conflicts(
        self,
        delta: BranchGraphDelta,
        current: GraphStateSnapshot,
    ) -> tuple[GraphConflict, ...]:
        conflicts: list[GraphConflict] = []
        if delta.base_revision > current.revision:
            conflicts.append(
                GraphConflict(
                    kind=GraphConflictKind.STALE_BASE,
                    key="graph:head",
                    message="delta base revision is ahead of graph head",
                    base_revision=delta.base_revision,
                    current_revision=current.revision,
                    recoverable=False,
                )
            )
            return tuple(conflicts)
        base = self.store.snapshot(delta.graph_id, delta.base_revision)
        if base is None or base.signature != delta.base_signature:
            conflicts.append(
                GraphConflict(
                    kind=GraphConflictKind.STALE_BASE,
                    key="graph:head",
                    message="delta base snapshot identity is unavailable or mismatched",
                    base_revision=delta.base_revision,
                    current_revision=current.revision,
                    recoverable=False,
                )
            )
        if delta.base_revision < current.revision:
            writes = self.store.writes_after(delta.graph_id, delta.base_revision)
            delta_reads = set(delta.read_set)
            delta_writes = set(delta.write_set)
            for key, delta_ids in sorted(writes.items()):
                if key in delta_writes:
                    conflicts.append(
                        GraphConflict(
                            kind=GraphConflictKind.WRITE_WRITE,
                            key=key,
                            message=f"branch write conflicts with revisions after {delta.base_revision}",
                            base_revision=delta.base_revision,
                            current_revision=current.revision,
                            conflicting_delta_ids=delta_ids,
                        )
                    )
                elif key in delta_reads:
                    conflicts.append(
                        GraphConflict(
                            kind=GraphConflictKind.READ_WRITE,
                            key=key,
                            message=f"branch read became stale after revision {delta.base_revision}",
                            base_revision=delta.base_revision,
                            current_revision=current.revision,
                            conflicting_delta_ids=delta_ids,
                        )
                    )
        structural = self.validate_mutations(current, delta.mutations)
        conflicts.extend(structural)
        return tuple(_dedupe_conflicts(conflicts))

    def apply(
        self,
        snapshot: GraphStateSnapshot,
        delta: BranchGraphDelta,
    ) -> GraphStateSnapshot:
        conflicts = self.validate_mutations(snapshot, delta.mutations)
        if conflicts:
            raise GraphMutationRejected(conflicts)
        nodes = dict(snapshot.node_map)
        edges = dict(snapshot.edge_map)
        metadata = copy.deepcopy(dict(snapshot.metadata))
        for mutation in delta.mutations:
            self._apply_mutation(nodes, edges, metadata, mutation)
        validation = self.validate_graph(nodes, edges)
        if validation:
            raise GraphMutationRejected(validation)
        return GraphStateSnapshot(
            graph_id=snapshot.graph_id,
            run_id=snapshot.run_id,
            revision=snapshot.revision,
            parent_revision=snapshot.parent_revision,
            commit_id=snapshot.commit_id,
            nodes=tuple(nodes.values()),
            edges=tuple(edges.values()),
            metadata=metadata,
            created_at=snapshot.created_at,
        )

    def validate_mutations(
        self,
        snapshot: GraphStateSnapshot,
        mutations: Sequence[GraphMutation],
    ) -> tuple[GraphConflict, ...]:
        nodes = dict(snapshot.node_map)
        edges = dict(snapshot.edge_map)
        metadata = copy.deepcopy(dict(snapshot.metadata))
        conflicts: list[GraphConflict] = []
        for mutation in mutations:
            entity = nodes.get(mutation.entity_id) or edges.get(mutation.entity_id)
            if mutation.expected_entity_revision is not None:
                if entity is None or entity.revision != mutation.expected_entity_revision:
                    conflicts.append(
                        GraphConflict(
                            kind=GraphConflictKind.STALE_BASE,
                            key=mutation.write_keys[0],
                            message="entity revision precondition failed",
                            base_revision=snapshot.revision,
                            current_revision=snapshot.revision,
                            metadata={
                                "expected_entity_revision": mutation.expected_entity_revision,
                                "actual_entity_revision": getattr(entity, "revision", None),
                            },
                        )
                    )
                    continue
            try:
                self._apply_mutation(nodes, edges, metadata, mutation)
            except GraphMutationRejected as error:
                conflicts.extend(error.conflicts)
            except (KeyError, TypeError, ValueError) as error:
                conflicts.append(
                    GraphConflict(
                        kind=GraphConflictKind.INVALID_MUTATION,
                        key=mutation.write_keys[0],
                        message=f"{mutation.kind.value} rejected: {error}",
                        base_revision=snapshot.revision,
                        current_revision=snapshot.revision,
                    )
                )
        conflicts.extend(self.validate_graph(nodes, edges))
        return tuple(_dedupe_conflicts(conflicts))

    def validate_graph(
        self,
        nodes: Mapping[str, GraphNode],
        edges: Mapping[str, GraphEdge],
    ) -> tuple[GraphConflict, ...]:
        conflicts: list[GraphConflict] = []
        for node in nodes.values():
            for dependency in node.dependencies:
                if dependency not in nodes:
                    conflicts.append(
                        GraphConflict(
                            kind=GraphConflictKind.DEPENDENCY_VIOLATION,
                            key=f"node:{node.node_id}",
                            message=f"node dependency does not exist: {dependency}",
                            base_revision=0,
                            current_revision=0,
                        )
                    )
        for edge in edges.values():
            if edge.source_node_id not in nodes or edge.target_node_id not in nodes:
                conflicts.append(
                    GraphConflict(
                        kind=GraphConflictKind.DEPENDENCY_VIOLATION,
                        key=f"edge:{edge.edge_id}",
                        message="edge endpoint does not exist",
                        base_revision=0,
                        current_revision=0,
                    )
                )
        cycle = _find_cycle(nodes, edges)
        if cycle:
            conflicts.append(
                GraphConflict(
                    kind=GraphConflictKind.CYCLE_DETECTED,
                    key="graph:dependencies",
                    message=f"dynamic topology contains dependency cycle: {' -> '.join(cycle)}",
                    base_revision=0,
                    current_revision=0,
                    recoverable=False,
                )
            )
        return tuple(conflicts)

    @staticmethod
    def _rebaseable(conflicts: Sequence[GraphConflict]) -> bool:
        return not any(
            conflict.kind
            in {
                GraphConflictKind.WRITE_WRITE,
                GraphConflictKind.READ_WRITE,
                GraphConflictKind.MISSING_ENTITY,
                GraphConflictKind.ENTITY_EXISTS,
                GraphConflictKind.DEPENDENCY_VIOLATION,
                GraphConflictKind.CYCLE_DETECTED,
                GraphConflictKind.INVALID_MUTATION,
            }
            or not conflict.recoverable
            for conflict in conflicts
        )

    def _apply_mutation(
        self,
        nodes: dict[str, GraphNode],
        edges: dict[str, GraphEdge],
        metadata: dict[str, Any],
        mutation: GraphMutation,
    ) -> None:
        kind = mutation.kind
        entity_id = mutation.entity_id
        if kind is GraphMutationKind.ADD_NODE:
            if entity_id in nodes:
                raise self._mutation_error(GraphConflictKind.ENTITY_EXISTS, f"node:{entity_id}", "node already exists")
            node = GraphNode.from_dict(mutation.value)
            if node.node_id != entity_id:
                raise ValueError("node mutation entity id differs from payload")
            nodes[entity_id] = node
            return
        if kind is GraphMutationKind.REMOVE_NODE:
            if entity_id not in nodes:
                raise self._mutation_error(GraphConflictKind.MISSING_ENTITY, f"node:{entity_id}", "node does not exist")
            referencing = [
                edge.edge_id
                for edge in edges.values()
                if edge.source_node_id == entity_id or edge.target_node_id == entity_id
            ]
            dependents = [node.node_id for node in nodes.values() if entity_id in node.dependencies]
            if referencing or dependents:
                raise self._mutation_error(
                    GraphConflictKind.DEPENDENCY_VIOLATION,
                    f"node:{entity_id}",
                    "node cannot be removed while edges or dependencies reference it",
                    {"edge_ids": referencing, "dependent_node_ids": dependents},
                )
            nodes.pop(entity_id)
            return
        if kind is GraphMutationKind.REPLACE_NODE:
            current = self._require_node(nodes, entity_id)
            replacement = GraphNode.from_dict(mutation.value)
            if replacement.node_id != entity_id:
                raise ValueError("replacement node id differs from mutation entity id")
            nodes[entity_id] = replace(
                replacement,
                revision=current.revision + 1,
                created_at=current.created_at,
                updated_at=now_iso(),
            )
            return
        if kind is GraphMutationKind.SET_NODE_ROLE:
            current = self._require_node(nodes, entity_id)
            nodes[entity_id] = current.revise(role=str(mutation.value.get("role") or ""))
            return
        if kind is GraphMutationKind.SET_NODE_CAPABILITIES:
            current = self._require_node(nodes, entity_id)
            nodes[entity_id] = current.revise(
                capabilities=tuple(str(item) for item in mutation.value.get("capabilities") or ())
            )
            return
        if kind is GraphMutationKind.SET_NODE_DEPENDENCIES:
            current = self._require_node(nodes, entity_id)
            nodes[entity_id] = current.revise(
                dependencies=tuple(str(item) for item in mutation.value.get("dependencies") or ())
            )
            return
        if kind is GraphMutationKind.ADD_EDGE:
            if entity_id in edges:
                raise self._mutation_error(GraphConflictKind.ENTITY_EXISTS, f"edge:{entity_id}", "edge already exists")
            edge = GraphEdge.from_dict(mutation.value)
            if edge.edge_id != entity_id:
                raise ValueError("edge mutation entity id differs from payload")
            if edge.source_node_id not in nodes or edge.target_node_id not in nodes:
                raise self._mutation_error(
                    GraphConflictKind.DEPENDENCY_VIOLATION,
                    f"edge:{entity_id}",
                    "edge endpoint does not exist",
                )
            edges[entity_id] = edge
            return
        if kind is GraphMutationKind.REMOVE_EDGE:
            if entity_id not in edges:
                raise self._mutation_error(GraphConflictKind.MISSING_ENTITY, f"edge:{entity_id}", "edge does not exist")
            edges.pop(entity_id)
            return
        if kind is GraphMutationKind.REPLACE_EDGE:
            current = self._require_edge(edges, entity_id)
            replacement = GraphEdge.from_dict(mutation.value)
            if replacement.edge_id != entity_id:
                raise ValueError("replacement edge id differs from mutation entity id")
            edges[entity_id] = replace(
                replacement,
                revision=current.revision + 1,
                created_at=current.created_at,
                updated_at=now_iso(),
            )
            return
        if kind is GraphMutationKind.SET_GRAPH_METADATA:
            metadata[entity_id] = copy.deepcopy(mutation.value.get("value"))
            return
        if kind is GraphMutationKind.REMOVE_GRAPH_METADATA:
            metadata.pop(entity_id, None)
            return
        raise ValueError(f"unsupported graph mutation kind: {kind}")

    @staticmethod
    def _require_node(nodes: Mapping[str, GraphNode], node_id: str) -> GraphNode:
        if node_id not in nodes:
            raise GraphStateCustody._mutation_error(
                GraphConflictKind.MISSING_ENTITY,
                f"node:{node_id}",
                "node does not exist",
            )
        return nodes[node_id]

    @staticmethod
    def _require_edge(edges: Mapping[str, GraphEdge], edge_id: str) -> GraphEdge:
        if edge_id not in edges:
            raise GraphStateCustody._mutation_error(
                GraphConflictKind.MISSING_ENTITY,
                f"edge:{edge_id}",
                "edge does not exist",
            )
        return edges[edge_id]

    @staticmethod
    def _mutation_error(
        kind: GraphConflictKind,
        key: str,
        message: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> GraphMutationRejected:
        return GraphMutationRejected(
            (
                GraphConflict(
                    kind=kind,
                    key=key,
                    message=message,
                    base_revision=0,
                    current_revision=0,
                    metadata=dict(metadata or {}),
                ),
            )
        )


class DynamicTopologyRuntime:
    """Runtime topology mutator; no node, edge, role, or capability is precompiled."""

    def __init__(self, custody: GraphStateCustody) -> None:
        self.custody = custody

    def add_node(
        self,
        graph_id_value: str,
        node: GraphNode,
        *,
        actor_id: str,
        causation_id: str,
        branch_id: str = "topology-main",
        strategy: GraphConflictStrategy = GraphConflictStrategy.SERIALIZE,
    ) -> GraphCommitResult:
        delta = self.custody.branch(
            graph_id_value,
            branch_id=branch_id,
            actor_id=actor_id,
            causation_id=causation_id,
            idempotency_key=digest(("add_node", node.to_dict(), causation_id)),
        ).add_node(node).build()
        return self.custody.commit(delta, strategy=strategy)

    def remove_node(
        self,
        graph_id_value: str,
        node_id: str,
        *,
        actor_id: str,
        causation_id: str,
        branch_id: str = "topology-main",
    ) -> GraphCommitResult:
        snapshot = self.custody.current(graph_id_value)
        current = snapshot.node_map.get(node_id)
        delta = GraphDeltaBuilder(
            snapshot,
            branch_id=branch_id,
            actor_id=actor_id,
            causation_id=causation_id,
            idempotency_key=digest(("remove_node", node_id, causation_id)),
        ).remove_node(node_id, expected_revision=current.revision if current else None).build()
        return self.custody.commit(delta)

    def replace_node(
        self,
        graph_id_value: str,
        node: GraphNode,
        *,
        actor_id: str,
        causation_id: str,
        branch_id: str = "topology-main",
    ) -> GraphCommitResult:
        snapshot = self.custody.current(graph_id_value)
        current = snapshot.node_map.get(node.node_id)
        delta = GraphDeltaBuilder(
            snapshot,
            branch_id=branch_id,
            actor_id=actor_id,
            causation_id=causation_id,
            idempotency_key=digest(("replace_node", node.to_dict(), causation_id)),
        ).replace_node(node, expected_revision=current.revision if current else None).build()
        return self.custody.commit(delta)

    def add_edge(
        self,
        graph_id_value: str,
        edge: GraphEdge,
        *,
        actor_id: str,
        causation_id: str,
        branch_id: str = "topology-main",
    ) -> GraphCommitResult:
        delta = self.custody.branch(
            graph_id_value,
            branch_id=branch_id,
            actor_id=actor_id,
            causation_id=causation_id,
            idempotency_key=digest(("add_edge", edge.to_dict(), causation_id)),
        ).add_edge(edge).build()
        return self.custody.commit(delta)

    def remove_edge(
        self,
        graph_id_value: str,
        edge_id: str,
        *,
        actor_id: str,
        causation_id: str,
        branch_id: str = "topology-main",
    ) -> GraphCommitResult:
        snapshot = self.custody.current(graph_id_value)
        current = snapshot.edge_map.get(edge_id)
        delta = GraphDeltaBuilder(
            snapshot,
            branch_id=branch_id,
            actor_id=actor_id,
            causation_id=causation_id,
            idempotency_key=digest(("remove_edge", edge_id, causation_id)),
        ).remove_edge(edge_id, expected_revision=current.revision if current else None).build()
        return self.custody.commit(delta)

    def set_role(
        self,
        graph_id_value: str,
        node_id: str,
        role: str,
        *,
        actor_id: str,
        causation_id: str,
    ) -> GraphCommitResult:
        snapshot = self.custody.current(graph_id_value)
        current = snapshot.node_map.get(node_id)
        builder = GraphDeltaBuilder(
            snapshot,
            branch_id="topology-main",
            actor_id=actor_id,
            causation_id=causation_id,
            idempotency_key=digest(("set_role", node_id, role, causation_id)),
        )
        builder.read_node(node_id)
        return self.custody.commit(
            builder.set_role(
                node_id,
                role,
                expected_revision=current.revision if current else None,
            ).build()
        )

    def set_capabilities(
        self,
        graph_id_value: str,
        node_id: str,
        capabilities: Iterable[str],
        *,
        actor_id: str,
        causation_id: str,
    ) -> GraphCommitResult:
        snapshot = self.custody.current(graph_id_value)
        current = snapshot.node_map.get(node_id)
        builder = GraphDeltaBuilder(
            snapshot,
            branch_id="topology-main",
            actor_id=actor_id,
            causation_id=causation_id,
            idempotency_key=digest(("set_capabilities", node_id, tuple(capabilities), causation_id)),
        )
        builder.read_node(node_id)
        return self.custody.commit(
            builder.set_capabilities(
                node_id,
                capabilities,
                expected_revision=current.revision if current else None,
            ).build()
        )

    def bind_physical_attempt(
        self,
        graph_id_value: str,
        node_id: str,
        *,
        physical_attempt_ref: str,
        worker_lease_ref: str,
        backend_route_ref: str,
        actor_id: str,
        causation_id: str,
    ) -> GraphCommitResult:
        snapshot = self.custody.current(graph_id_value)
        node = snapshot.node_map.get(node_id)
        if node is None:
            raise KeyError(node_id)
        replacement = node.revise(
            physical_attempt_ref=physical_attempt_ref,
            worker_lease_ref=worker_lease_ref,
            backend_route_ref=backend_route_ref,
            state=NodeExecutionState.LEASED,
            metadata={
                **dict(node.metadata),
                "physical_attempt_owner": "WorkerLeaseManager",
                "lease_reference_only": True,
            },
        )
        return self.replace_node(
            graph_id_value,
            replacement,
            actor_id=actor_id,
            causation_id=causation_id,
        )

    def cancel_physical_attempt(
        self,
        graph_id_value: str,
        node_id: str,
        *,
        physical_attempt_ref: str,
        worker_lease_ref: str,
        reason: str,
        actor_id: str,
        causation_id: str,
    ) -> GraphCommitResult:
        """Make one exact physical binding terminal after acquisition rollback.

        The reference checks fence this compensation from cancelling a newer
        binding if another writer advanced the canonical node first.
        """

        snapshot = self.custody.current(graph_id_value)
        node = snapshot.node_map.get(node_id)
        if node is None:
            raise KeyError(node_id)
        if (
            node.physical_attempt_ref != physical_attempt_ref
            or node.worker_lease_ref != worker_lease_ref
        ):
            raise RuntimeError(
                "dynamic graph physical attempt compensation was fenced"
            )
        replacement = node.revise(
            state=NodeExecutionState.CANCELLED,
            metadata={
                **dict(node.metadata),
                "physical_attempt_terminal": True,
                "physical_attempt_terminal_reason": str(reason),
            },
        )
        builder = GraphDeltaBuilder(
            snapshot,
            branch_id="topology-main",
            actor_id=actor_id,
            causation_id=causation_id,
            idempotency_key=digest(
                (
                    "cancel_physical_attempt",
                    node_id,
                    physical_attempt_ref,
                    worker_lease_ref,
                    causation_id,
                )
            ),
        )
        builder.read_node(node_id)
        return self.custody.commit(
            builder.replace_node(
                replacement,
                expected_revision=node.revision,
            ).build()
        )

    def version_ref(self, graph_id_value: str) -> GraphVersionRef:
        snapshot = self.custody.current(graph_id_value)
        return GraphVersionRef(
            graph_id=snapshot.graph_id,
            run_id=snapshot.run_id,
            revision=snapshot.revision,
            signature=snapshot.signature,
            commit_id=snapshot.commit_id,
        )


def _find_cycle(
    nodes: Mapping[str, GraphNode],
    edges: Mapping[str, GraphEdge],
) -> tuple[str, ...]:
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    for node in nodes.values():
        for dependency in node.dependencies:
            adjacency.setdefault(dependency, set()).add(node.node_id)
    for edge in edges.values():
        if edge.relation in {"depends_on", "precedes", "handoff"}:
            adjacency.setdefault(edge.source_node_id, set()).add(edge.target_node_id)
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(node_id: str) -> tuple[str, ...]:
        if node_id in visiting:
            index = stack.index(node_id)
            return tuple((*stack[index:], node_id))
        if node_id in visited:
            return ()
        visiting.add(node_id)
        stack.append(node_id)
        for target in sorted(adjacency.get(node_id, ())):
            cycle = visit(target)
            if cycle:
                return cycle
        stack.pop()
        visiting.remove(node_id)
        visited.add(node_id)
        return ()

    for node_id in sorted(nodes):
        cycle = visit(node_id)
        if cycle:
            return cycle
    return ()


def _dedupe_conflicts(conflicts: Sequence[GraphConflict]) -> tuple[GraphConflict, ...]:
    result: list[GraphConflict] = []
    seen: set[tuple[str, str, str]] = set()
    for conflict in conflicts:
        key = (conflict.kind.value, conflict.key, conflict.message)
        if key not in seen:
            seen.add(key)
            result.append(conflict)
    return tuple(result)
