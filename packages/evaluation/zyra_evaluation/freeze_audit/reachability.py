from __future__ import annotations

import heapq
import re
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .model import (
    AuditCatalog,
    AuditSection,
    Disposition,
    EdgeKind,
    EntrypointSpec,
    EntrySurface,
    EvidencePointer,
    Finding,
    GraphEdge,
    GraphNode,
    NodeKind,
    OwnerContract,
    RuleSwitches,
    Severity,
    SourceRef,
    deduplicate_findings,
    finding,
    section,
    stable_digest,
)
from .python_graph import PythonGraphResult
from .script_graph import ScriptGraphResult


NON_DEFAULT_MARKERS = frozenset(
    {
        "test",
        "tests",
        "fixture",
        "fixtures",
        "mock",
        "mocks",
        "example",
        "examples",
        "demo",
        "demos",
        "health",
        "doctor",
        "ledger",
        "source-map",
        "source_map",
        "report",
        "reports",
        "replay",
        "benchmark",
        "benchmarks",
    }
)
ENTRYPOINT_SCHEMA = "zyra.default-entry-reachability/v1"


@dataclass(frozen=True, slots=True)
class PathResult:
    reachable: bool
    nodes: tuple[str, ...] = ()
    edges: tuple[GraphEdge, ...] = ()
    reason: str = ""

    @property
    def length(self) -> int:
        return len(self.edges)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "nodes": list(self.nodes),
            "edges": [item.to_dict() for item in self.edges],
            "length": self.length,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ReferenceObservation:
    reference: SourceRef
    node_id: str
    exists: bool
    executable: bool
    production: bool
    selector_found: bool
    alternatives: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        if not self.reference.required and not self.exists:
            return True
        return (
            self.exists
            and self.executable
            and self.production
            and self.selector_found
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference.to_dict(),
            "node_id": self.node_id,
            "exists": self.exists,
            "executable": self.executable,
            "production": self.production,
            "selector_found": self.selector_found,
            "alternatives": list(self.alternatives),
            "valid": self.valid,
        }


@dataclass(frozen=True, slots=True)
class EntryObservation:
    domain: str
    entry_id: str
    surface: str
    default: bool
    reference: ReferenceObservation
    root_node: str
    route_or_command_valid: bool
    owner_path: PathResult
    writer_paths: tuple[PathResult, ...]
    declared_trace_valid: bool
    declared_trace: tuple[str, ...]
    non_default_only: bool
    fallback_bypass: tuple[str, ...]

    @property
    def reachable(self) -> bool:
        return (
            self.default
            and self.reference.valid
            and self.route_or_command_valid
            and self.declared_trace_valid
            and self.owner_path.reachable
            and all(item.reachable for item in self.writer_paths)
            and not self.non_default_only
            and not self.fallback_bypass
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "entry_id": self.entry_id,
            "surface": self.surface,
            "default": self.default,
            "reference": self.reference.to_dict(),
            "root_node": self.root_node,
            "route_or_command_valid": self.route_or_command_valid,
            "owner_path": self.owner_path.to_dict(),
            "writer_paths": [item.to_dict() for item in self.writer_paths],
            "declared_trace_valid": self.declared_trace_valid,
            "declared_trace": list(self.declared_trace),
            "non_default_only": self.non_default_only,
            "fallback_bypass": list(self.fallback_bypass),
            "reachable": self.reachable,
        }


class CodeGraph:
    def __init__(
        self,
        nodes: Iterable[GraphNode],
        edges: Iterable[GraphEdge],
    ) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[str, GraphEdge] = {}
        self.forward: dict[str, list[GraphEdge]] = defaultdict(list)
        self.reverse: dict[str, list[GraphEdge]] = defaultdict(list)
        self.by_ref: dict[str, list[str]] = defaultdict(list)
        self.by_path: dict[str, list[str]] = defaultdict(list)
        for node in nodes:
            self.add_node(node)
        for edge in edges:
            self.add_edge(edge)

    def add_node(self, node: GraphNode) -> None:
        existing = self.nodes.get(node.node_id)
        if existing is not None and existing != node:
            raise ValueError(f"graph node identity collision: {node.node_id}")
        self.nodes[node.node_id] = node
        if node.node_id not in self.by_ref[node.ref_key]:
            self.by_ref[node.ref_key].append(node.node_id)
        if node.node_id not in self.by_path[node.path]:
            self.by_path[node.path].append(node.node_id)

    def add_edge(self, edge: GraphEdge) -> None:
        if edge.source not in self.nodes or edge.target not in self.nodes:
            return
        existing = self.edges.get(edge.fingerprint)
        if existing is not None:
            return
        self.edges[edge.fingerprint] = edge
        self.forward[edge.source].append(edge)
        self.reverse[edge.target].append(edge)

    def resolve_ref(self, reference: SourceRef) -> ReferenceObservation:
        exact_key = reference.key
        candidates = list(self.by_ref.get(exact_key, ()))
        if reference.symbol and not candidates:
            candidates = [
                node_id
                for node_id in self.by_path.get(reference.path, ())
                if self.nodes[node_id].symbol == reference.symbol
                or self.nodes[node_id].symbol.endswith(f".{reference.symbol}")
                or self.nodes[node_id].symbol.rsplit(".", 1)[-1]
                == reference.symbol.rsplit(".", 1)[-1]
            ]
        if not reference.symbol and not candidates:
            candidates = [
                node_id
                for node_id in self.by_path.get(reference.path, ())
                if self.nodes[node_id].kind is NodeKind.FILE
            ]
        selected_id = self._best_candidate(candidates, reference)
        if not selected_id:
            return ReferenceObservation(
                reference=reference,
                node_id="",
                exists=False,
                executable=False,
                production=False,
                selector_found=False,
                alternatives=tuple(sorted(candidates)),
            )
        node = self.nodes[selected_id]
        production = bool(node.attributes.get("production", True))
        selector_found = (
            not reference.selector
            or reference.selector.casefold()
            in str(node.attributes).casefold()
        )
        return ReferenceObservation(
            reference=reference,
            node_id=selected_id,
            exists=True,
            executable=node.executable,
            production=production,
            selector_found=selector_found,
            alternatives=tuple(
                sorted(item for item in candidates if item != selected_id)
            ),
        )

    def shortest_path(
        self,
        source: str,
        target: str,
        *,
        max_depth: int = 64,
        allowed_kinds: frozenset[EdgeKind] | None = None,
        forbidden_nodes: frozenset[str] = frozenset(),
    ) -> PathResult:
        if source not in self.nodes:
            return PathResult(False, reason=f"source node missing: {source}")
        if target not in self.nodes:
            return PathResult(False, reason=f"target node missing: {target}")
        if source == target:
            return PathResult(True, nodes=(source,))
        allowed = allowed_kinds or frozenset(EdgeKind)
        queue: deque[tuple[str, int]] = deque([(source, 0)])
        parents: dict[str, tuple[str, GraphEdge]] = {}
        visited = {source}
        while queue:
            current, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for edge in sorted(
                self.forward.get(current, ()),
                key=lambda item: (
                    item.target,
                    item.kind.value,
                    item.path,
                    item.line,
                ),
            ):
                if edge.kind not in allowed or not edge.verified:
                    continue
                if edge.target in forbidden_nodes or edge.target in visited:
                    continue
                parents[edge.target] = (current, edge)
                if edge.target == target:
                    return self._reconstruct(source, target, parents)
                visited.add(edge.target)
                queue.append((edge.target, depth + 1))
        return PathResult(False, reason="no verified executable graph path")

    def reachable_from(
        self,
        roots: Iterable[str],
        *,
        max_depth: int = 128,
        forbidden_nodes: frozenset[str] = frozenset(),
    ) -> frozenset[str]:
        visited: set[str] = {
            item
            for item in roots
            if item in self.nodes and item not in forbidden_nodes
        }
        queue: deque[tuple[str, int]] = deque((item, 0) for item in sorted(visited))
        while queue:
            current, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for edge in self.forward.get(current, ()):
                if (
                    not edge.verified
                    or edge.target in forbidden_nodes
                    or edge.target in visited
                ):
                    continue
                visited.add(edge.target)
                queue.append((edge.target, depth + 1))
        return frozenset(visited)

    def strongly_connected_components(self) -> tuple[tuple[str, ...], ...]:
        index = 0
        indices: dict[str, int] = {}
        lowlink: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        components: list[tuple[str, ...]] = []

        def connect(node_id: str) -> None:
            nonlocal index
            indices[node_id] = index
            lowlink[node_id] = index
            index += 1
            stack.append(node_id)
            on_stack.add(node_id)
            for edge in self.forward.get(node_id, ()):
                target = edge.target
                if target not in indices:
                    connect(target)
                    lowlink[node_id] = min(lowlink[node_id], lowlink[target])
                elif target in on_stack:
                    lowlink[node_id] = min(lowlink[node_id], indices[target])
            if lowlink[node_id] != indices[node_id]:
                return
            component: list[str] = []
            while stack:
                item = stack.pop()
                on_stack.remove(item)
                component.append(item)
                if item == node_id:
                    break
            components.append(tuple(sorted(component)))

        for node_id in sorted(self.nodes):
            if node_id not in indices:
                connect(node_id)
        return tuple(sorted(components, key=lambda item: (len(item), item)))

    def digest(self) -> str:
        return stable_digest(
            {
                "nodes": [self.nodes[key].to_dict() for key in sorted(self.nodes)],
                "edges": [
                    self.edges[key].to_dict() for key in sorted(self.edges)
                ],
            }
        )

    @staticmethod
    def _best_candidate(
        candidates: Sequence[str],
        reference: SourceRef,
    ) -> str:
        if not candidates:
            return ""
        exact_symbol = [
            item
            for item in candidates
            if item.endswith(f"#{reference.symbol}")
        ]
        if len(exact_symbol) == 1:
            return exact_symbol[0]
        executable = [
            item
            for item in candidates
            if not item.startswith(("event:", "mutation:"))
        ]
        return sorted(executable or candidates)[0]

    @staticmethod
    def _reconstruct(
        source: str,
        target: str,
        parents: Mapping[str, tuple[str, GraphEdge]],
    ) -> PathResult:
        nodes = [target]
        edges: list[GraphEdge] = []
        current = target
        while current != source:
            prior, edge = parents[current]
            edges.append(edge)
            nodes.append(prior)
            current = prior
        nodes.reverse()
        edges.reverse()
        return PathResult(True, nodes=tuple(nodes), edges=tuple(edges))


@dataclass(frozen=True, slots=True)
class ReachabilityResult:
    graph: CodeGraph
    observations: tuple[EntryObservation, ...]
    section: AuditSection

    @property
    def reachable_entry_ids(self) -> frozenset[str]:
        return frozenset(item.entry_id for item in self.observations if item.reachable)


class ReachabilityAuditor:
    def __init__(
        self,
        project_root: str | Path,
        *,
        switches: RuleSwitches | None = None,
    ) -> None:
        self.root = Path(project_root).resolve(strict=False)
        self.switches = switches or RuleSwitches()
        self._source_cache: dict[str, str] = {}

    def audit(
        self,
        catalog: AuditCatalog,
        python: PythonGraphResult,
        script: ScriptGraphResult,
    ) -> ReachabilityResult:
        graph = CodeGraph(
            (*python.nodes, *script.nodes),
            (*python.edges, *script.edges),
        )
        findings: list[Finding] = []
        evidence: list[EvidencePointer] = []
        observations: list[EntryObservation] = []
        if not self.switches.reachability:
            return ReachabilityResult(
                graph=graph,
                observations=(),
                section=section(
                    "default_reachability",
                    metrics={
                        "rule_enabled": False,
                        "nodes": len(graph.nodes),
                        "edges": len(graph.edges),
                        "graph_digest": graph.digest(),
                    },
                ),
            )
        for owner_contract in catalog.owners:
            owner_observation = graph.resolve_ref(owner_contract.owner)
            writer_observations = tuple(
                graph.resolve_ref(item) for item in owner_contract.writers
            )
            for entry in owner_contract.entries:
                observation = self._observe_entry(
                    graph,
                    owner_contract,
                    entry,
                    owner_observation,
                    writer_observations,
                )
                observations.append(observation)
                findings.extend(self._findings_for_entry(observation))
                evidence.append(
                    EvidencePointer(
                        kind="default_entry_trace",
                        path=entry.reference.path,
                        symbol=entry.reference.symbol,
                        attributes={
                            "domain": owner_contract.domain,
                            "entry_id": entry.entry_id,
                            "surface": entry.surface.value,
                            "reachable": observation.reachable,
                            "nodes": list(observation.owner_path.nodes),
                        },
                    )
                )
        findings.extend(self._domain_coverage(catalog, observations))
        roots = [
            item.root_node
            for item in observations
            if item.default and item.root_node in graph.nodes
        ]
        reachable_nodes = graph.reachable_from(roots)
        findings.extend(self._dead_catalog_refs(catalog, graph, reachable_nodes))
        metrics = {
            "schema": ENTRYPOINT_SCHEMA,
            "rule_enabled": True,
            "nodes": len(graph.nodes),
            "edges": len(graph.edges),
            "graph_digest": graph.digest(),
            "strongly_connected_components": len(
                graph.strongly_connected_components()
            ),
            "entry_count": len(observations),
            "default_entry_count": sum(item.default for item in observations),
            "reachable_entry_count": sum(item.reachable for item in observations),
            "unreachable_entry_count": sum(
                item.default and not item.reachable for item in observations
            ),
            "surface_counts": dict(
                sorted(Counter(item.surface for item in observations).items())
            ),
            "reachable_node_count": len(reachable_nodes),
        }
        return ReachabilityResult(
            graph=graph,
            observations=tuple(observations),
            section=section(
                "default_reachability",
                metrics=metrics,
                findings=findings,
                evidence=evidence,
                records=(item.to_dict() for item in observations),
            ),
        )

    def _observe_entry(
        self,
        graph: CodeGraph,
        owner_contract: OwnerContract,
        entry: EntrypointSpec,
        owner_observation: ReferenceObservation,
        writer_observations: Sequence[ReferenceObservation],
    ) -> EntryObservation:
        reference = graph.resolve_ref(entry.reference)
        root_node = self._entry_node(graph, entry, reference)
        route_or_command_valid = self._surface_valid(entry)
        trace_nodes, trace_valid = self._materialize_declared_trace(
            graph,
            entry,
            root_node,
        )
        owner_path = self._path_with_declared_fallback(
            graph,
            root_node,
            owner_observation.node_id,
            trace_nodes,
        )
        writer_paths = tuple(
            self._path_with_declared_fallback(
                graph,
                owner_observation.node_id or root_node,
                writer.node_id,
                trace_nodes,
            )
            for writer in writer_observations
            if writer.reference.required
        )
        non_default_only = self._non_default_only(entry, owner_path, graph)
        fallback_bypass = self._fallback_bypass(
            graph,
            root_node,
            owner_observation.node_id,
            owner_contract,
        )
        return EntryObservation(
            domain=owner_contract.domain,
            entry_id=entry.entry_id,
            surface=entry.surface.value,
            default=entry.default,
            reference=reference,
            root_node=root_node,
            route_or_command_valid=route_or_command_valid,
            owner_path=owner_path,
            writer_paths=writer_paths,
            declared_trace_valid=trace_valid,
            declared_trace=trace_nodes,
            non_default_only=non_default_only,
            fallback_bypass=fallback_bypass,
        )

    def _entry_node(
        self,
        graph: CodeGraph,
        entry: EntrypointSpec,
        reference: ReferenceObservation,
    ) -> str:
        node_id = f"entry:{entry.entry_id}"
        graph.add_node(
            GraphNode(
                node_id=node_id,
                kind=NodeKind.ENTRYPOINT,
                language=entry.reference.language,
                path=entry.reference.path,
                symbol=entry.reference.symbol,
                executable=reference.executable,
                attributes={
                    "surface": entry.surface.value,
                    "default": entry.default,
                    "command": entry.command,
                    "route": entry.route,
                    "method": entry.method,
                    "production": reference.production,
                },
            )
        )
        if reference.node_id:
            graph.add_edge(
                GraphEdge(
                    source=node_id,
                    target=reference.node_id,
                    kind=(
                        EdgeKind.ROUTES
                        if entry.surface is EntrySurface.API
                        else EdgeKind.BOOTS
                    ),
                    path=entry.reference.path,
                    verified=self._surface_valid(entry),
                    attributes={
                        "entry_id": entry.entry_id,
                        "surface": entry.surface.value,
                    },
                )
            )
        return node_id

    def _materialize_declared_trace(
        self,
        graph: CodeGraph,
        entry: EntrypointSpec,
        root_node: str,
    ) -> tuple[tuple[str, ...], bool]:
        node_ids: list[str] = [root_node]
        valid = True
        previous_ref = entry.reference
        previous_node = graph.resolve_ref(entry.reference).node_id
        for reference in entry.trace:
            observation = graph.resolve_ref(reference)
            if not observation.valid:
                valid = False
                node_ids.append(observation.node_id or f"missing:{reference.key}")
                previous_ref = reference
                previous_node = observation.node_id
                continue
            node_ids.append(observation.node_id)
            if previous_node:
                actual = graph.shortest_path(previous_node, observation.node_id)
                if not actual.reachable:
                    verified = self._verify_declared_edge(previous_ref, reference)
                    graph.add_edge(
                        GraphEdge(
                            source=previous_node,
                            target=observation.node_id,
                            kind=EdgeKind.DECLARED,
                            path=previous_ref.path,
                            verified=verified,
                            attributes={
                                "entry_id": entry.entry_id,
                                "from_ref": previous_ref.key,
                                "to_ref": reference.key,
                            },
                        )
                    )
                    valid = valid and verified
            previous_ref = reference
            previous_node = observation.node_id
        return tuple(node_ids), valid

    def _verify_declared_edge(
        self,
        source: SourceRef,
        target: SourceRef,
    ) -> bool:
        if source.path == target.path:
            return self._reference_present(source) and self._reference_present(target)
        text = self._read(source.path)
        if not text:
            return False
        candidates = {
            target.symbol,
            target.symbol.rsplit(".", 1)[-1] if target.symbol else "",
            Path(target.path).stem,
            target.path.replace("\\", "/"),
        }
        target_module = ".".join(
            PurePosixPath(target.path).with_suffix("").parts[-3:]
        )
        candidates.add(target_module)
        return any(candidate and candidate in text for candidate in candidates)

    def _path_with_declared_fallback(
        self,
        graph: CodeGraph,
        source: str,
        target: str,
        trace_nodes: Sequence[str],
    ) -> PathResult:
        if not source or not target:
            return PathResult(False, reason="source or target reference unresolved")
        direct = graph.shortest_path(source, target)
        if direct.reachable:
            return direct
        if source in trace_nodes and target in trace_nodes:
            start = trace_nodes.index(source)
            end = trace_nodes.index(target)
            if start <= end:
                selected = trace_nodes[start : end + 1]
                edges: list[GraphEdge] = []
                for left, right in zip(selected, selected[1:], strict=False):
                    candidates = [
                        item
                        for item in graph.forward.get(left, ())
                        if item.target == right and item.verified
                    ]
                    if not candidates:
                        return PathResult(
                            False,
                            reason="declared trace has an unverified edge",
                        )
                    edges.append(sorted(candidates, key=lambda item: item.kind.value)[0])
                return PathResult(
                    True,
                    nodes=tuple(selected),
                    edges=tuple(edges),
                )
        return direct

    def _surface_valid(self, entry: EntrypointSpec) -> bool:
        text = self._read(entry.reference.path)
        if not text:
            return False
        if entry.surface is EntrySurface.CLI:
            pyproject = self._read("pyproject.toml")
            executable = entry.command.split()[0]
            return (
                executable in pyproject
                or "__main__" in text
                or "ArgumentParser" in text
                or "main(" in text
            )
        if entry.surface is EntrySurface.API:
            route_pattern = re.escape(entry.route.rstrip("/"))
            return (
                re.search(route_pattern, text) is not None
                and (
                    entry.method.casefold() in text.casefold()
                    or f"do_{entry.method.upper()}" in text
                )
            )
        if entry.surface is EntrySurface.WEB:
            return any(
                marker in text
                for marker in (
                    "createRoot",
                    "hydrateRoot",
                    "render(",
                    "addEventListener",
                    entry.reference.symbol,
                )
                if marker
            )
        return bool(
            entry.reference.symbol
            and entry.reference.symbol in text
            and any(marker in text for marker in ("run", "start", "execute", "dispatch"))
        )

    def _reference_present(self, reference: SourceRef) -> bool:
        text = self._read(reference.path)
        if not text:
            return False
        if reference.symbol:
            candidates = {
                reference.symbol,
                reference.symbol.rsplit(".", 1)[-1],
            }
            if not any(item in text for item in candidates):
                return False
        return not reference.selector or reference.selector in text

    def _non_default_only(
        self,
        entry: EntrypointSpec,
        path: PathResult,
        graph: CodeGraph,
    ) -> bool:
        values = [
            entry.entry_id,
            entry.reference.path,
            entry.reference.symbol,
            entry.command,
            entry.route,
        ]
        values.extend(
            graph.nodes[node_id].path
            for node_id in path.nodes
            if node_id in graph.nodes
        )
        tokens = {
            token
            for value in values
            for token in re.split(r"[^A-Za-z0-9_-]+", value.casefold())
            if token
        }
        if entry.surface is EntrySurface.API and entry.route not in {
            "/health",
            "/doctor",
            "/status",
        }:
            tokens.discard("api")
        return bool(tokens & NON_DEFAULT_MARKERS) and not any(
            token in tokens
            for token in {"main", "runtime", "worker", "task", "session"}
        )

    def _fallback_bypass(
        self,
        graph: CodeGraph,
        root_node: str,
        owner_node: str,
        contract: OwnerContract,
    ) -> tuple[str, ...]:
        if not contract.fallback_refs or not root_node:
            return ()
        forbidden = frozenset({owner_node}) if owner_node else frozenset()
        reachable = graph.reachable_from(
            (root_node,),
            forbidden_nodes=forbidden,
        )
        bypass: list[str] = []
        for reference in contract.fallback_refs:
            observation = graph.resolve_ref(reference)
            if observation.node_id and observation.node_id in reachable:
                bypass.append(reference.key)
        return tuple(sorted(bypass))

    def _findings_for_entry(
        self,
        observation: EntryObservation,
    ) -> tuple[Finding, ...]:
        if not observation.default:
            return ()
        findings: list[Finding] = []
        common = {
            "domain": observation.domain,
            "path": observation.reference.reference.path,
            "owner_unit": "M3-01B",
            "disposition": Disposition.BLOCK_RELEASE,
            "default_path_impact": (
                f"Default {observation.surface} entry {observation.entry_id} "
                "cannot prove the selected canonical owner."
            ),
        }
        if not observation.reference.exists:
            findings.append(
                finding(
                    "default_entry_reference_missing",
                    f"Default entry reference is missing: {observation.entry_id}.",
                    "reachability",
                    severity=Severity.BLOCKER,
                    remediation="Restore the real product entry or update the authority.",
                    **common,
                )
            )
        elif not observation.reference.executable:
            findings.append(
                finding(
                    "default_entry_not_executable",
                    f"Default entry has no executable code: {observation.entry_id}.",
                    "reachability",
                    severity=Severity.BLOCKER,
                    remediation=(
                        "Point the entry at executable behavior rather than a DTO, "
                        "manifest, replay, or static declaration."
                    ),
                    **common,
                )
            )
        elif not observation.reference.production:
            findings.append(
                finding(
                    "default_entry_nonproduction_only",
                    f"Default entry resolves only to a non-production path: {observation.entry_id}.",
                    "reachability",
                    severity=Severity.BLOCKER,
                    remediation="Wire the capability into packaged production code.",
                    **common,
                )
            )
        if not observation.route_or_command_valid:
            findings.append(
                finding(
                    "default_surface_binding_missing",
                    (
                        f"Default {observation.surface} binding is not declared "
                        f"by product source: {observation.entry_id}."
                    ),
                    "reachability",
                    severity=Severity.BLOCKER,
                    remediation=(
                        "Register the packaged command, API route, Web bootstrap, "
                        "or worker execution entry."
                    ),
                    **common,
                )
            )
        if not observation.declared_trace_valid:
            findings.append(
                finding(
                    "default_trace_edge_unverified",
                    f"Declared trace contains an unverified edge: {observation.entry_id}.",
                    "reachability",
                    severity=Severity.BLOCKER,
                    remediation=(
                        "Connect adjacent runtime modules through an executable "
                        "import/call and remove hand-written-only trace claims."
                    ),
                    attributes={"trace": list(observation.declared_trace)},
                    **common,
                )
            )
        if not observation.owner_path.reachable:
            findings.append(
                finding(
                    "canonical_owner_default_unreachable",
                    f"Canonical owner is unreachable from {observation.entry_id}.",
                    "reachability",
                    severity=Severity.BLOCKER,
                    remediation=(
                        "Wire the selected owner into the real default path or "
                        "remove the dead capability claim."
                    ),
                    attributes={"reason": observation.owner_path.reason},
                    **common,
                )
            )
        for writer_path in observation.writer_paths:
            if writer_path.reachable:
                continue
            findings.append(
                finding(
                    "canonical_write_default_unreachable",
                    f"Canonical write path is unreachable from {observation.entry_id}.",
                    "reachability",
                    severity=Severity.BLOCKER,
                    remediation=(
                        "Connect the owner to its durable write path without "
                        "event-only, projection-only, or fallback-only success."
                    ),
                    attributes={"reason": writer_path.reason},
                    **common,
                )
            )
        if observation.non_default_only:
            findings.append(
                finding(
                    "default_trace_test_demo_health_only",
                    (
                        f"Entry trace for {observation.entry_id} is satisfied only "
                        "by test/demo/health/ledger/replay surfaces."
                    ),
                    "reachability",
                    severity=Severity.BLOCKER,
                    remediation="Connect a packaged CLI/API/Web/worker path.",
                    **common,
                )
            )
        if observation.fallback_bypass:
            findings.append(
                finding(
                    "fallback_can_take_over_owner",
                    (
                        f"Fallback can be reached while owner is disconnected: "
                        f"{observation.entry_id}."
                    ),
                    "reachability",
                    severity=Severity.BLOCKER,
                    remediation=(
                        "Make owner loss fail closed or route through an explicit "
                        "recovery transition that cannot impersonate the owner."
                    ),
                    attributes={"fallbacks": list(observation.fallback_bypass)},
                    **common,
                )
            )
        return deduplicate_findings(findings)

    def _domain_coverage(
        self,
        catalog: AuditCatalog,
        observations: Sequence[EntryObservation],
    ) -> tuple[Finding, ...]:
        findings: list[Finding] = []
        by_domain: dict[str, list[EntryObservation]] = defaultdict(list)
        for item in observations:
            by_domain[item.domain].append(item)
        for domain in catalog.required_domains:
            defaults = [item for item in by_domain.get(domain, ()) if item.default]
            if defaults and any(item.reachable for item in defaults):
                continue
            findings.append(
                finding(
                    "state_domain_no_reachable_default_entry",
                    f"State domain has no reachable default entry: {domain}.",
                    "reachability",
                    severity=Severity.BLOCKER,
                    domain=domain,
                    owner_unit="M3-01B",
                    disposition=Disposition.BLOCK_RELEASE,
                    default_path_impact=(
                        "The capability is dead, test-only, or bypassed in the "
                        "packaged product."
                    ),
                    remediation=(
                        "Provide at least one verified CLI/API/Web/worker path to "
                        "the canonical owner and write effect."
                    ),
                )
            )
        return deduplicate_findings(findings)

    def _dead_catalog_refs(
        self,
        catalog: AuditCatalog,
        graph: CodeGraph,
        reachable_nodes: frozenset[str],
    ) -> tuple[Finding, ...]:
        findings: list[Finding] = []
        for owner_contract in catalog.owners:
            for reference in (
                owner_contract.owner,
                owner_contract.store,
                *owner_contract.writers,
            ):
                observation = graph.resolve_ref(reference)
                if not observation.valid or observation.node_id in reachable_nodes:
                    continue
                findings.append(
                    finding(
                        "catalog_runtime_reference_dead_code",
                        (
                            f"Cataloged canonical runtime is not reachable from "
                            f"any default root: {reference.key}."
                        ),
                        "reachability",
                        severity=Severity.BLOCKER,
                        domain=owner_contract.domain,
                        path=reference.path,
                        owner_unit="M3-01B",
                        disposition=Disposition.BLOCK_RELEASE,
                        default_path_impact=(
                            "File existence or import smoke is the only evidence."
                        ),
                        remediation=(
                            "Connect the runtime to a default entry or remove the "
                            "dead owner/write claim."
                        ),
                    )
                )
        return deduplicate_findings(findings)

    def _read(self, relative: str) -> str:
        normalized = relative.replace("\\", "/")
        if normalized in self._source_cache:
            return self._source_cache[normalized]
        path = (self.root / normalized).resolve(strict=False)
        try:
            path.relative_to(self.root)
            text = path.read_text(encoding="utf-8")
        except (ValueError, OSError, UnicodeDecodeError):
            text = ""
        self._source_cache[normalized] = text
        return text
