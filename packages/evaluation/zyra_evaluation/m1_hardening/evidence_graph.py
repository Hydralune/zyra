from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .contracts import EvidencePointer, Finding, GateResult, GateStatus, Severity


@dataclass(frozen=True, slots=True)
class CausalNode:
    node_id: str
    kind: str
    semantic_family: str
    task_id: str
    run_id: str
    sequence: int
    revision_before: int | None
    revision_after: int | None
    payload_digest: str
    canonical: bool
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "semantic_family": self.semantic_family,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "revision_before": self.revision_before,
            "revision_after": self.revision_after,
            "payload_digest": self.payload_digest,
            "canonical": self.canonical,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CausalEdge:
    source_id: str
    target_id: str
    relation: str
    explicit: bool
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation": self.relation,
            "explicit": self.explicit,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class CausalEvidenceGraph:
    nodes: dict[str, CausalNode] = field(default_factory=dict)
    edges: list[CausalEdge] = field(default_factory=list)
    duplicate_node_ids: list[str] = field(default_factory=list)
    missing_canonical_ids: list[int] = field(default_factory=list)
    unresolved_edges: list[CausalEdge] = field(default_factory=list)

    def add_node(self, node: CausalNode) -> None:
        if node.node_id in self.nodes:
            self.duplicate_node_ids.append(node.node_id)
            return
        self.nodes[node.node_id] = node

    def add_edge(self, edge: CausalEdge) -> None:
        self.edges.append(edge)
        if edge.source_id not in self.nodes or edge.target_id not in self.nodes:
            self.unresolved_edges.append(edge)

    def adjacency(self, *, relations: Iterable[str] = ()) -> dict[str, set[str]]:
        allowed = set(relations)
        result: dict[str, set[str]] = defaultdict(set)
        for edge in self.edges:
            if allowed and edge.relation not in allowed:
                continue
            if edge.source_id in self.nodes and edge.target_id in self.nodes:
                result[edge.source_id].add(edge.target_id)
        return result

    def reverse_adjacency(self, *, relations: Iterable[str] = ()) -> dict[str, set[str]]:
        result: dict[str, set[str]] = defaultdict(set)
        for source, targets in self.adjacency(relations=relations).items():
            for target in targets:
                result[target].add(source)
        return result

    def roots(self) -> tuple[str, ...]:
        incoming = self.reverse_adjacency()
        return tuple(sorted(node_id for node_id in self.nodes if not incoming.get(node_id)))

    def leaves(self) -> tuple[str, ...]:
        outgoing = self.adjacency()
        return tuple(sorted(node_id for node_id in self.nodes if not outgoing.get(node_id)))

    def reachable(self, sources: Iterable[str], *, relations: Iterable[str] = ()) -> set[str]:
        graph = self.adjacency(relations=relations)
        seen: set[str] = set()
        queue = deque(source for source in sources if source in self.nodes)
        while queue:
            node_id = queue.popleft()
            if node_id in seen:
                continue
            seen.add(node_id)
            queue.extend(sorted(graph.get(node_id, set()) - seen))
        return seen

    def strongly_connected_components(self) -> tuple[tuple[str, ...], ...]:
        graph = self.adjacency()
        index = 0
        indices: dict[str, int] = {}
        low: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        components: list[tuple[str, ...]] = []

        def visit(node_id: str) -> None:
            nonlocal index
            indices[node_id] = index
            low[node_id] = index
            index += 1
            stack.append(node_id)
            on_stack.add(node_id)
            for target in graph.get(node_id, set()):
                if target not in indices:
                    visit(target)
                    low[node_id] = min(low[node_id], low[target])
                elif target in on_stack:
                    low[node_id] = min(low[node_id], indices[target])
            if low[node_id] != indices[node_id]:
                return
            component: list[str] = []
            while stack:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node_id:
                    break
            components.append(tuple(sorted(component)))

        for node_id in sorted(self.nodes):
            if node_id not in indices:
                visit(node_id)
        return tuple(sorted(components))

    def to_dict(self, *, include_nodes: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "duplicate_node_ids": sorted(set(self.duplicate_node_ids)),
            "missing_canonical_id_indexes": list(self.missing_canonical_ids),
            "unresolved_edges": [item.to_dict() for item in self.unresolved_edges],
            "roots": list(self.roots()),
            "leaves": list(self.leaves()),
            "edges": [item.to_dict() for item in self.edges],
        }
        if include_nodes:
            value["nodes"] = [self.nodes[node_id].to_dict() for node_id in sorted(self.nodes)]
        return value


class CausalEvidenceGraphBuilder:
    _FAMILY_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("permission", ("permission", "approval", "deny", "allow")),
        ("tool", ("tool", "action", "command")),
        ("artifact", ("artifact", "patch", "file", "diff")),
        ("memory", ("memory", "compact", "restore", "retrieval")),
        ("route", ("route", "dispatch", "placement", "backend", "provider")),
        ("worker", ("worker", "lease", "heartbeat", "subagent")),
        ("fault", ("fault", "crash", "timeout", "failure", "watchdog")),
        ("recovery", ("recovery", "retry", "resume", "replan", "fallback")),
        ("topology", ("topology", "graph", "node", "edge", "branch", "delta")),
        ("control", ("control", "requirement", "change", "cancel", "pause")),
        ("task", ("task", "run", "session", "query")),
    )

    def build(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        task: Mapping[str, Any] | None = None,
    ) -> CausalEvidenceGraph:
        graph = CausalEvidenceGraph()
        normalized: list[tuple[CausalNode, Mapping[str, Any]]] = []
        task_value = task or {}
        default_task = str(task_value.get("task_id") or "")
        default_run = str(task_value.get("run_id") or "")
        for index, event in enumerate(events):
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            raw_id = str(event.get("event_id") or event.get("id") or "").strip()
            canonical = bool(raw_id)
            if not raw_id:
                raw_id = self._derived_event_id(event, index)
                graph.missing_canonical_ids.append(index)
            kind = str(event.get("event_type") or event.get("type") or payload.get("event_type") or "unknown")
            task_id = str(event.get("task_id") or payload.get("task_id") or default_task)
            run_id = str(event.get("run_id") or payload.get("run_id") or default_run)
            before, after = self._revisions(event, payload)
            node = CausalNode(
                node_id=raw_id,
                kind=kind,
                semantic_family=self._family(kind, payload),
                task_id=task_id,
                run_id=run_id,
                sequence=self._integer(event.get("sequence") or payload.get("sequence"), default=index + 1),
                revision_before=before,
                revision_after=after,
                payload_digest=self._digest(payload),
                canonical=canonical,
                metadata={
                    "index": index,
                    "actor_id": event.get("actor_id") or payload.get("actor_id") or "",
                    "request_id": event.get("request_id") or payload.get("request_id") or "",
                },
            )
            graph.add_node(node)
            normalized.append((node, event))
        self._explicit_edges(graph, normalized)
        self._sequence_edges(graph, normalized)
        self._identity_edges(graph, normalized)
        return graph

    def _explicit_edges(
        self,
        graph: CausalEvidenceGraph,
        normalized: Sequence[tuple[CausalNode, Mapping[str, Any]]],
    ) -> None:
        for node, event in normalized:
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            relations = (
                ("caused_by", ("causation_id", "cause_event_id", "parent_event_id")),
                ("correlated_with", ("correlation_id", "request_event_id")),
                ("recovered_from", ("fault_event_id", "failure_event_id")),
                ("restored_from", ("checkpoint_event_id", "compact_event_id")),
                ("result_of", ("tool_event_id", "dispatch_event_id", "action_event_id")),
            )
            for relation, keys in relations:
                for key in keys:
                    source = str(event.get(key) or payload.get(key) or "").strip()
                    if source and source != node.node_id:
                        graph.add_edge(
                            CausalEdge(
                                source_id=source,
                                target_id=node.node_id,
                                relation=relation,
                                explicit=True,
                                metadata={"field": key},
                            )
                        )
                        break

    def _sequence_edges(
        self,
        graph: CausalEvidenceGraph,
        normalized: Sequence[tuple[CausalNode, Mapping[str, Any]]],
    ) -> None:
        by_stream: dict[tuple[str, str], list[CausalNode]] = defaultdict(list)
        for node, _event in normalized:
            by_stream[(node.run_id, node.task_id)].append(node)
        for stream, nodes in by_stream.items():
            ordered = sorted(nodes, key=lambda node: (node.sequence, int(node.metadata.get("index") or 0)))
            for previous, current in zip(ordered, ordered[1:]):
                graph.add_edge(
                    CausalEdge(
                        source_id=previous.node_id,
                        target_id=current.node_id,
                        relation="sequence",
                        explicit=False,
                        metadata={"stream": list(stream)},
                    )
                )

    def _identity_edges(
        self,
        graph: CausalEvidenceGraph,
        normalized: Sequence[tuple[CausalNode, Mapping[str, Any]]],
    ) -> None:
        previous_by_identity: dict[tuple[str, str], CausalNode] = {}
        for node, event in sorted(normalized, key=lambda item: item[0].sequence):
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            identities = self._identities(event, payload)
            for kind, identity in identities:
                key = (kind, identity)
                previous = previous_by_identity.get(key)
                if previous and previous.node_id != node.node_id:
                    graph.add_edge(
                        CausalEdge(
                            source_id=previous.node_id,
                            target_id=node.node_id,
                            relation=f"same_{kind}",
                            explicit=False,
                            metadata={"identity": identity},
                        )
                    )
                previous_by_identity[key] = node

    @staticmethod
    def _identities(event: Mapping[str, Any], payload: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
        values: list[tuple[str, str]] = []
        for kind, keys in (
            ("tool_call", ("tool_call_id", "call_id")),
            ("artifact", ("artifact_id", "artifact_ref")),
            ("worker", ("worker_id", "agent_id")),
            ("lease", ("lease_id",)),
            ("route", ("route_id", "dispatch_id")),
            ("fault", ("fault_id", "signal_id")),
            ("recovery", ("recovery_id", "plan_id")),
            ("checkpoint", ("checkpoint_id",)),
            ("permission", ("permission_id", "decision_id")),
        ):
            value = next((str(event.get(key) or payload.get(key) or "").strip() for key in keys if event.get(key) or payload.get(key)), "")
            if value:
                values.append((kind, value))
        return tuple(values)

    def _family(self, kind: str, payload: Mapping[str, Any]) -> str:
        text = f"{kind} {payload.get('kind', '')} {payload.get('operation', '')}".lower()
        for family, markers in self._FAMILY_MARKERS:
            if any(marker in text for marker in markers):
                return family
        return "other"

    @staticmethod
    def _revisions(event: Mapping[str, Any], payload: Mapping[str, Any]) -> tuple[int | None, int | None]:
        before = CausalEvidenceGraphBuilder._optional_integer(
            event.get("before_revision") or payload.get("before_revision") or payload.get("base_revision")
        )
        after = CausalEvidenceGraphBuilder._optional_integer(
            event.get("after_revision") or payload.get("after_revision") or payload.get("revision")
        )
        return before, after

    @staticmethod
    def _integer(value: Any, *, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _optional_integer(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _digest(value: Any) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _derived_event_id(self, event: Mapping[str, Any], index: int) -> str:
        return "derived-" + self._digest({"index": index, "event": event})[:24]


class CausalEvidenceAnalyzer:
    def analyze(self, graph: CausalEvidenceGraph) -> dict[str, Any]:
        families = Counter(node.semantic_family for node in graph.nodes.values())
        relations = Counter(edge.relation for edge in graph.edges)
        canonical = sum(1 for node in graph.nodes.values() if node.canonical)
        cycles = [component for component in graph.strongly_connected_components() if len(component) > 1]
        revision_errors = self._revision_errors(graph)
        sequence_errors = self._sequence_errors(graph)
        cross_task_edges = self._cross_task_edges(graph)
        semantic_chains = self._semantic_chains(graph)
        roots = graph.roots()
        reachable = graph.reachable(roots)
        orphans = sorted(set(graph.nodes) - reachable)
        return {
            "node_count": len(graph.nodes),
            "edge_count": len(graph.edges),
            "canonical_node_count": canonical,
            "derived_node_count": len(graph.nodes) - canonical,
            "family_counts": dict(sorted(families.items())),
            "relation_counts": dict(sorted(relations.items())),
            "duplicate_node_ids": sorted(set(graph.duplicate_node_ids)),
            "unresolved_edge_count": len(graph.unresolved_edges),
            "unresolved_edges": [edge.to_dict() for edge in graph.unresolved_edges],
            "cycle_count": len(cycles),
            "cycles": [list(component) for component in cycles[:50]],
            "revision_errors": revision_errors,
            "sequence_errors": sequence_errors,
            "cross_task_edges": cross_task_edges,
            "roots": list(roots),
            "leaves": list(graph.leaves()),
            "orphan_nodes": orphans,
            "semantic_chains": semantic_chains,
        }

    @staticmethod
    def _revision_errors(graph: CausalEvidenceGraph) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        for node in graph.nodes.values():
            before = node.revision_before
            after = node.revision_after
            if before is None or after is None:
                continue
            if after <= before:
                errors.append(
                    {
                        "node_id": node.node_id,
                        "before_revision": before,
                        "after_revision": after,
                        "kind": node.kind,
                    }
                )
        return errors

    @staticmethod
    def _sequence_errors(graph: CausalEvidenceGraph) -> list[dict[str, Any]]:
        by_stream: dict[tuple[str, str], list[CausalNode]] = defaultdict(list)
        for node in graph.nodes.values():
            by_stream[(node.run_id, node.task_id)].append(node)
        errors: list[dict[str, Any]] = []
        for stream, nodes in by_stream.items():
            seen: set[int] = set()
            previous = 0
            for node in sorted(nodes, key=lambda item: int(item.metadata.get("index") or 0)):
                if node.sequence in seen:
                    errors.append({"stream": list(stream), "node_id": node.node_id, "error": "duplicate_sequence"})
                if node.sequence < previous:
                    errors.append({"stream": list(stream), "node_id": node.node_id, "error": "sequence_regression"})
                seen.add(node.sequence)
                previous = max(previous, node.sequence)
        return errors

    @staticmethod
    def _cross_task_edges(graph: CausalEvidenceGraph) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        for edge in graph.edges:
            source = graph.nodes.get(edge.source_id)
            target = graph.nodes.get(edge.target_id)
            if not source or not target:
                continue
            if source.run_id and target.run_id and source.run_id != target.run_id:
                errors.append({**edge.to_dict(), "error": "cross_run_edge"})
            elif source.task_id and target.task_id and source.task_id != target.task_id and edge.relation == "sequence":
                errors.append({**edge.to_dict(), "error": "cross_task_sequence"})
        return errors

    def _semantic_chains(self, graph: CausalEvidenceGraph) -> dict[str, Any]:
        by_family: dict[str, set[str]] = defaultdict(set)
        for node in graph.nodes.values():
            by_family[node.semantic_family].add(node.node_id)
        graph_map = graph.adjacency()
        chains = {
            "permission_to_tool": self._path_exists(graph_map, by_family["permission"], by_family["tool"]),
            "tool_to_artifact": self._path_exists(graph_map, by_family["tool"], by_family["artifact"]),
            "fault_to_recovery": self._path_exists(graph_map, by_family["fault"], by_family["recovery"]),
            "route_to_worker": self._path_exists(graph_map, by_family["route"], by_family["worker"]),
            "control_to_task": self._path_exists(graph_map, by_family["control"], by_family["task"]),
            "memory_to_task": self._path_exists(graph_map, by_family["memory"], by_family["task"]),
        }
        return {
            "observed": chains,
            "observed_count": sum(chains.values()),
            "applicable_count": sum(
                1
                for left, right in (
                    ("permission", "tool"),
                    ("tool", "artifact"),
                    ("fault", "recovery"),
                    ("route", "worker"),
                    ("control", "task"),
                    ("memory", "task"),
                )
                if by_family[left] and by_family[right]
            ),
        }

    @staticmethod
    def _path_exists(graph: Mapping[str, set[str]], sources: set[str], targets: set[str]) -> bool:
        if not sources or not targets:
            return False
        queue = deque(sources)
        seen: set[str] = set()
        while queue:
            node_id = queue.popleft()
            if node_id in seen:
                continue
            if node_id in targets and node_id not in sources:
                return True
            seen.add(node_id)
            queue.extend(graph.get(node_id, set()) - seen)
        return False


class CausalEvidenceGraphGate:
    def __init__(self) -> None:
        self.builder = CausalEvidenceGraphBuilder()
        self.analyzer = CausalEvidenceAnalyzer()

    def evaluate(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        task: Mapping[str, Any] | None = None,
        final_completion: bool = False,
    ) -> GateResult:
        result = GateResult(
            gate_id="causal-evidence",
            status=GateStatus.NOT_RUN,
            summary="Canonical event causality, revision, state-effect and cross-module lineage audit.",
        )
        graph = self.builder.build(events, task=task)
        analysis = self.analyzer.analyze(graph)
        if not events:
            result.add(
                Finding(
                    code="causal.no_event_trace",
                    severity=Severity.BLOCKER if final_completion else Severity.WARNING,
                    summary="No canonical event trace was supplied to the evidence graph.",
                )
            )
            result.limitations.append("A real task trace is required to close cross-module event causality.")
        if graph.duplicate_node_ids:
            result.add(
                Finding(
                    code="causal.duplicate_event_id",
                    severity=Severity.BLOCKER,
                    summary="Canonical event ids are not unique.",
                    detail=", ".join(sorted(set(graph.duplicate_node_ids))[:20]),
                )
            )
        if graph.missing_canonical_ids:
            result.add(
                Finding(
                    code="causal.event_id_missing",
                    severity=Severity.BLOCKER if final_completion else Severity.ERROR,
                    summary="Events required derived audit ids because canonical event ids were missing.",
                    detail=f"count={len(graph.missing_canonical_ids)}",
                )
            )
        if analysis["revision_errors"]:
            result.add(
                Finding(
                    code="causal.revision_not_advanced",
                    severity=Severity.BLOCKER,
                    summary="An event claims a state transition without advancing its revision.",
                    detail=f"count={len(analysis['revision_errors'])}",
                )
            )
        if analysis["sequence_errors"]:
            result.add(
                Finding(
                    code="causal.sequence_invalid",
                    severity=Severity.ERROR,
                    summary="A run/task event stream has duplicate or regressing sequence numbers.",
                    detail=f"count={len(analysis['sequence_errors'])}",
                )
            )
        if analysis["cross_task_edges"]:
            result.add(
                Finding(
                    code="causal.cross_stream_edge_invalid",
                    severity=Severity.BLOCKER,
                    summary="A causal/sequence edge crosses canonical run or task boundaries incorrectly.",
                    detail=f"count={len(analysis['cross_task_edges'])}",
                )
            )
        if analysis["cycle_count"]:
            result.add(
                Finding(
                    code="causal.cycle_detected",
                    severity=Severity.ERROR,
                    summary="The canonical causal graph contains a cycle.",
                    detail=f"count={analysis['cycle_count']}",
                )
            )
        if graph.unresolved_edges:
            result.add(
                Finding(
                    code="causal.unresolved_reference",
                    severity=Severity.BLOCKER if final_completion else Severity.WARNING,
                    summary="Explicit causal references do not resolve inside the supplied trace.",
                    detail=f"count={len(graph.unresolved_edges)}",
                )
            )
        semantic = analysis["semantic_chains"]
        if semantic["applicable_count"] and not semantic["observed_count"]:
            result.add(
                Finding(
                    code="causal.semantic_chain_missing",
                    severity=Severity.BLOCKER if final_completion else Severity.WARNING,
                    summary="Applicable cross-module semantic families have no connected effect chain.",
                )
            )
        result.metrics.update({"analysis": analysis, "graph": graph.to_dict(include_nodes=True)})
        result.evidence.extend(
            EvidencePointer(
                kind="canonical_event",
                location=node.node_id,
                summary=f"{node.semantic_family}:{node.kind}",
                metadata={"sequence": node.sequence, "task_id": node.task_id, "run_id": node.run_id},
            )
            for node in sorted(graph.nodes.values(), key=lambda value: value.sequence)[:5000]
            if node.canonical
        )
        return result.finish(default_partial=not final_completion)
