from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from .ledger_models import InternalizationLedgerEntry, LedgerLifecycle, MainPathStatus, MigrationStrategy, to_jsonable
from .ledger_policy import classify_path
from .ledger_store import InternalizationLedger


class EvidenceNodeKind(StrEnum):
    LEDGER_ENTRY = "ledger_entry"
    SOURCE_REPOSITORY = "source_repository"
    SOURCE_PATH = "source_path"
    TARGET_PATH = "target_path"
    TEST_PATH = "test_path"
    RUNTIME_MODULE = "runtime_module"
    RUNTIME_COMMAND = "runtime_command"
    API_ROUTE = "api_route"
    EVENT_TYPE = "event_type"
    CONTROL_COMMAND = "control_command"
    ARTIFACT_KIND = "artifact_kind"
    WORKER_RUNTIME = "worker_runtime"
    UI_PANEL = "ui_panel"
    OWNER_UNIT = "owner_unit"
    MILESTONE = "milestone"
    STATE_STORE = "state_store"
    LINE_BUCKET = "line_bucket"


class EvidenceEdgeKind(StrEnum):
    SOURCED_FROM = "sourced_from"
    DECLARES_SOURCE_PATH = "declares_source_path"
    MATERIALIZES_TARGET = "materializes_target"
    VERIFIED_BY_TEST = "verified_by_test"
    ENTERS_RUNTIME_MODULE = "enters_runtime_module"
    ENTERS_RUNTIME_COMMAND = "enters_runtime_command"
    EXPOSES_API_ROUTE = "exposes_api_route"
    EMITS_EVENT_TYPE = "emits_event_type"
    HANDLES_CONTROL_COMMAND = "handles_control_command"
    PRODUCES_ARTIFACT_KIND = "produces_artifact_kind"
    RUNS_WORKER_RUNTIME = "runs_worker_runtime"
    RENDERS_UI_PANEL = "renders_ui_panel"
    OWNED_BY_UNIT = "owned_by_unit"
    OWNED_BY_MILESTONE = "owned_by_milestone"
    PERSISTS_STATE_IN = "persists_state_in"
    CLASSIFIED_AS_BUCKET = "classified_as_bucket"
    SHARES_TARGET_WITH = "shares_target_with"
    DEPENDS_ON_ENTRY = "depends_on_entry"
    DOWNSTREAM_TO_ENTRY = "downstream_to_entry"


class EvidenceSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class EvidenceCode(StrEnum):
    GRAPH_BUILT = "GRAPH_BUILT"
    ENTRY_WITHOUT_TARGET = "ENTRY_WITHOUT_TARGET"
    ENTRY_WITHOUT_TEST = "ENTRY_WITHOUT_TEST"
    ENTRY_WITHOUT_RUNTIME = "ENTRY_WITHOUT_RUNTIME"
    CONNECTED_WITHOUT_MAIN_PATH_EDGE = "CONNECTED_WITHOUT_MAIN_PATH_EDGE"
    TARGET_NOT_MATERIALIZED = "TARGET_NOT_MATERIALIZED"
    TARGET_SHARED_WITHOUT_BOUNDARY = "TARGET_SHARED_WITHOUT_BOUNDARY"
    SOURCE_REPO_UNCOVERED = "SOURCE_REPO_UNCOVERED"
    OWNER_UNIT_UNCOVERED = "OWNER_UNIT_UNCOVERED"
    VENDOR_BUCKET_REQUIRES_REVIEW = "VENDOR_BUCKET_REQUIRES_REVIEW"
    DISCONNECT_IMPACT_RECORDED = "DISCONNECT_IMPACT_RECORDED"
    EVIDENCE_COMPONENT_ISOLATED = "EVIDENCE_COMPONENT_ISOLATED"
    MAIN_PATH_SURFACE_RECORDED = "MAIN_PATH_SURFACE_RECORDED"


@dataclass(slots=True)
class EvidenceNode:
    node_id: str
    kind: EvidenceNodeKind
    label: str
    exists: bool = True
    owner_unit: str = ""
    source_repo: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class EvidenceEdge:
    source: str
    target: str
    kind: EvidenceEdgeKind
    ledger_id: str = ""
    required: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def edge_id(self) -> str:
        return f"{self.source}->{self.kind}->{self.target}"

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["edge_id"] = self.edge_id
        return payload


@dataclass(slots=True)
class EvidenceFinding:
    code: EvidenceCode
    severity: EvidenceSeverity
    message: str
    ledger_id: str = ""
    node_id: str = ""
    path: str = ""
    owner_unit: str = ""
    source_repo: str = ""
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity in {EvidenceSeverity.ERROR, EvidenceSeverity.BLOCKER}

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class EvidenceComponent:
    component_id: str
    node_count: int
    edge_count: int
    kinds: dict[str, int]
    owner_units: list[str]
    source_repos: list[str]
    entry_ids: list[str]
    isolated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class TargetImpact:
    target_path: str
    exists: bool
    ledger_ids: list[str]
    source_repos: list[str]
    owner_units: list[str]
    connected_entries: int
    productized_entries: int
    shared: bool
    bucket_verdict: str

    @property
    def risk(self) -> str:
        if not self.exists and self.connected_entries:
            return "blocker"
        if self.shared and len(self.source_repos) > 1:
            return "warning"
        if self.bucket_verdict == "review":
            return "review"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["risk"] = self.risk
        return payload


@dataclass(slots=True)
class EvidenceGraphReport:
    ok: bool
    owner_unit: str
    total_entries: int
    nodes: list[EvidenceNode]
    edges: list[EvidenceEdge]
    components: list[EvidenceComponent]
    target_impacts: list[TargetImpact]
    findings: list[EvidenceFinding]
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == EvidenceSeverity.BLOCKER)

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == EvidenceSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == EvidenceSeverity.WARNING)

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["blocker_count"] = self.blocker_count
        payload["error_count"] = self.error_count
        payload["warning_count"] = self.warning_count
        return payload


CONNECTED_STATUSES = {
    MainPathStatus.API_CONNECTED,
    MainPathStatus.EVENT_LOG_CONNECTED,
    MainPathStatus.CONTROL_COMMAND_CONNECTED,
    MainPathStatus.WORKER_RUNTIME_CONNECTED,
    MainPathStatus.UI_CONNECTED,
    MainPathStatus.TESTED_MAIN_PATH,
}

PRODUCTIZED_LIFECYCLES = {LedgerLifecycle.INTERNALIZED, LedgerLifecycle.PRODUCTIZED}
MATERIALIZED_LIFECYCLES = {LedgerLifecycle.ACTIVE, LedgerLifecycle.INTERNALIZED, LedgerLifecycle.PRODUCTIZED}


def build_evidence_graph_report(
    project_root: Path,
    ledger: InternalizationLedger,
    *,
    owner_unit: str = "",
    include_nodes: bool = True,
) -> EvidenceGraphReport:
    entries = ledger.by_owner_unit(owner_unit) if owner_unit else ledger.entries()
    builder = EvidenceGraphBuilder(project_root)
    for entry in entries:
        builder.add_entry(entry)
    builder.add_shared_target_edges()
    nodes = list(builder.nodes.values())
    edges = builder.edges
    components = build_components(nodes, edges)
    target_impacts = build_target_impacts(project_root, entries)
    findings = build_evidence_findings(project_root, entries, nodes, edges, components, target_impacts, owner_unit)
    ok = not any(finding.blocking for finding in findings)
    return EvidenceGraphReport(
        ok=ok,
        owner_unit=owner_unit or "all",
        total_entries=len(entries),
        nodes=nodes if include_nodes else [],
        edges=edges if include_nodes else [],
        components=components,
        target_impacts=target_impacts,
        findings=findings,
        summary=evidence_summary(nodes, edges, components, target_impacts, findings, owner_unit),
    )


class EvidenceGraphBuilder:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.nodes: dict[str, EvidenceNode] = {}
        self.edges: list[EvidenceEdge] = []
        self.target_to_entries: dict[str, list[str]] = defaultdict(list)

    def add_entry(self, entry: InternalizationLedgerEntry) -> None:
        entry_node = self.node(
            EvidenceNodeKind.LEDGER_ENTRY,
            entry.ledger_id,
            label=entry.capability_name or entry.ledger_id,
            owner_unit=entry.owner_unit,
            source_repo=entry.source_repo,
            metadata={
                "source_path": entry.source_path,
                "lifecycle": str(entry.lifecycle),
                "main_path_status": str(entry.main_path_status),
                "migration_strategy": str(entry.migration_strategy),
            },
        )
        source_repo = self.node(EvidenceNodeKind.SOURCE_REPOSITORY, entry.source_repo, label=entry.source_repo, source_repo=entry.source_repo)
        source_path = self.node(
            EvidenceNodeKind.SOURCE_PATH,
            f"{entry.source_repo}:{entry.source_path}",
            label=entry.source_path,
            source_repo=entry.source_repo,
            metadata={"source_repo": entry.source_repo},
        )
        self.edge(entry_node, source_repo, EvidenceEdgeKind.SOURCED_FROM, entry)
        self.edge(entry_node, source_path, EvidenceEdgeKind.DECLARES_SOURCE_PATH, entry)
        if entry.owner_unit:
            owner = self.node(EvidenceNodeKind.OWNER_UNIT, entry.owner_unit, label=entry.owner_unit, owner_unit=entry.owner_unit)
            self.edge(entry_node, owner, EvidenceEdgeKind.OWNED_BY_UNIT, entry)
        if entry.milestone:
            milestone = self.node(EvidenceNodeKind.MILESTONE, entry.milestone, label=entry.milestone)
            self.edge(entry_node, milestone, EvidenceEdgeKind.OWNED_BY_MILESTONE, entry)
        for target in entry.target_bindings:
            target_id = target.target_path.replace("\\", "/")
            target_node = self.node(
                EvidenceNodeKind.TARGET_PATH,
                target_id,
                label=target_id,
                exists=(self.project_root / target_id).exists(),
                owner_unit=entry.owner_unit,
                source_repo=entry.source_repo,
                metadata={"role": target.role, "required_for_main_path": target.required_for_main_path},
            )
            self.edge(entry_node, target_node, EvidenceEdgeKind.MATERIALIZES_TARGET, entry, required=target.required_for_main_path)
            bucket = classify_path(target_id)
            bucket_node = self.node(EvidenceNodeKind.LINE_BUCKET, f"bucket:{bucket.verdict}:{bucket.surface}", label=f"{bucket.verdict}:{bucket.surface}")
            self.edge(target_node, bucket_node, EvidenceEdgeKind.CLASSIFIED_AS_BUCKET, entry, metadata=bucket.to_dict())
            self.target_to_entries[target_id].append(entry.ledger_id)
        for test in entry.test_entries:
            test_path = test.path.replace("\\", "/")
            test_node = self.node(
                EvidenceNodeKind.TEST_PATH,
                test_path,
                label=test_path,
                exists=(self.project_root / test_path).exists(),
                owner_unit=entry.owner_unit,
                source_repo=entry.source_repo,
                metadata={"kind": test.kind, "required": test.required, "expected_signal": test.expected_signal},
            )
            self.edge(entry_node, test_node, EvidenceEdgeKind.VERIFIED_BY_TEST, entry, required=test.required, metadata={"command": test.command})
        self.add_runtime_edges(entry, entry_node)
        self.add_main_path_edges(entry, entry_node)
        self.add_dependency_edges(entry, entry_node)

    def add_runtime_edges(self, entry: InternalizationLedgerEntry, entry_node: EvidenceNode) -> None:
        runtime = entry.runtime_entry
        if runtime.module:
            module = self.node(EvidenceNodeKind.RUNTIME_MODULE, runtime.module, label=runtime.module, owner_unit=entry.owner_unit, source_repo=entry.source_repo)
            self.edge(entry_node, module, EvidenceEdgeKind.ENTERS_RUNTIME_MODULE, entry, metadata={"function": runtime.function, "protocol": runtime.protocol})
        if runtime.command:
            command = self.node(EvidenceNodeKind.RUNTIME_COMMAND, runtime.command, label=runtime.command, owner_unit=entry.owner_unit, source_repo=entry.source_repo)
            self.edge(entry_node, command, EvidenceEdgeKind.ENTERS_RUNTIME_COMMAND, entry, metadata={"protocol": runtime.protocol})
        for ref in runtime.config_refs:
            state = self.node(EvidenceNodeKind.STATE_STORE, f"config:{ref}", label=ref, owner_unit=entry.owner_unit, source_repo=entry.source_repo)
            self.edge(entry_node, state, EvidenceEdgeKind.PERSISTS_STATE_IN, entry, required=False, metadata={"kind": "config"})
        for ref in runtime.environment_refs:
            state = self.node(EvidenceNodeKind.STATE_STORE, f"env:{ref}", label=ref, owner_unit=entry.owner_unit, source_repo=entry.source_repo)
            self.edge(entry_node, state, EvidenceEdgeKind.PERSISTS_STATE_IN, entry, required=False, metadata={"kind": "environment"})

    def add_main_path_edges(self, entry: InternalizationLedgerEntry, entry_node: EvidenceNode) -> None:
        binding = entry.main_path
        for route in binding.api_routes:
            node = self.node(EvidenceNodeKind.API_ROUTE, normalize_route(route), label=route, owner_unit=entry.owner_unit, source_repo=entry.source_repo)
            self.edge(entry_node, node, EvidenceEdgeKind.EXPOSES_API_ROUTE, entry)
        for event in binding.event_types:
            node = self.node(EvidenceNodeKind.EVENT_TYPE, event, label=event, owner_unit=entry.owner_unit, source_repo=entry.source_repo)
            self.edge(entry_node, node, EvidenceEdgeKind.EMITS_EVENT_TYPE, entry)
        for command in binding.control_commands:
            node = self.node(EvidenceNodeKind.CONTROL_COMMAND, command, label=command, owner_unit=entry.owner_unit, source_repo=entry.source_repo)
            self.edge(entry_node, node, EvidenceEdgeKind.HANDLES_CONTROL_COMMAND, entry)
        for artifact in binding.artifact_kinds:
            node = self.node(EvidenceNodeKind.ARTIFACT_KIND, artifact, label=artifact, owner_unit=entry.owner_unit, source_repo=entry.source_repo)
            self.edge(entry_node, node, EvidenceEdgeKind.PRODUCES_ARTIFACT_KIND, entry)
        if binding.worker_runtime:
            node = self.node(EvidenceNodeKind.WORKER_RUNTIME, binding.worker_runtime, label=binding.worker_runtime, owner_unit=entry.owner_unit, source_repo=entry.source_repo)
            self.edge(entry_node, node, EvidenceEdgeKind.RUNS_WORKER_RUNTIME, entry)
        for panel in binding.ui_panels:
            node = self.node(EvidenceNodeKind.UI_PANEL, panel, label=panel, owner_unit=entry.owner_unit, source_repo=entry.source_repo)
            self.edge(entry_node, node, EvidenceEdgeKind.RENDERS_UI_PANEL, entry)

    def add_dependency_edges(self, entry: InternalizationLedgerEntry, entry_node: EvidenceNode) -> None:
        for dependency in entry.dependencies:
            dependency_node = self.node(EvidenceNodeKind.LEDGER_ENTRY, dependency, label=dependency, owner_unit=entry.owner_unit, source_repo=entry.source_repo, exists=False)
            self.edge(entry_node, dependency_node, EvidenceEdgeKind.DEPENDS_ON_ENTRY, entry, required=False)
        for downstream in entry.downstream_units:
            downstream_node = self.node(EvidenceNodeKind.OWNER_UNIT, downstream, label=downstream, owner_unit=downstream)
            self.edge(entry_node, downstream_node, EvidenceEdgeKind.DOWNSTREAM_TO_ENTRY, entry, required=False)

    def add_shared_target_edges(self) -> None:
        for target, entry_ids in self.target_to_entries.items():
            if len(entry_ids) <= 1:
                continue
            target_node = self.nodes.get(node_id(EvidenceNodeKind.TARGET_PATH, target))
            if target_node is None:
                continue
            for entry_id in entry_ids:
                entry_node = self.nodes.get(node_id(EvidenceNodeKind.LEDGER_ENTRY, entry_id))
                if entry_node is not None:
                    self.edges.append(EvidenceEdge(entry_node.node_id, target_node.node_id, EvidenceEdgeKind.SHARES_TARGET_WITH, ledger_id=entry_id, required=False, metadata={"shared_entry_count": len(entry_ids)}))

    def node(
        self,
        kind: EvidenceNodeKind,
        raw_id: str,
        *,
        label: str,
        exists: bool = True,
        owner_unit: str = "",
        source_repo: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> EvidenceNode:
        key = node_id(kind, raw_id)
        existing = self.nodes.get(key)
        if existing is not None:
            if not exists:
                existing.exists = False
            if owner_unit and not existing.owner_unit:
                existing.owner_unit = owner_unit
            if source_repo and not existing.source_repo:
                existing.source_repo = source_repo
            existing.metadata.update(metadata or {})
            return existing
        node = EvidenceNode(key, kind, label, exists=exists, owner_unit=owner_unit, source_repo=source_repo, metadata=metadata or {})
        self.nodes[key] = node
        return node

    def edge(
        self,
        source: EvidenceNode,
        target: EvidenceNode,
        kind: EvidenceEdgeKind,
        entry: InternalizationLedgerEntry,
        *,
        required: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.edges.append(EvidenceEdge(source.node_id, target.node_id, kind, ledger_id=entry.ledger_id, required=required, metadata=metadata or {}))


def build_components(nodes: list[EvidenceNode], edges: list[EvidenceEdge]) -> list[EvidenceComponent]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    edge_count_by_pair: dict[tuple[str, str], int] = defaultdict(int)
    for edge in edges:
        adjacency[edge.source].add(edge.target)
        adjacency[edge.target].add(edge.source)
        pair = tuple(sorted([edge.source, edge.target]))
        edge_count_by_pair[pair] += 1
    node_map = {node.node_id: node for node in nodes}
    seen: set[str] = set()
    components: list[EvidenceComponent] = []
    for node in nodes:
        if node.node_id in seen:
            continue
        queue = deque([node.node_id])
        seen.add(node.node_id)
        component_nodes: list[str] = []
        while queue:
            current = queue.popleft()
            component_nodes.append(current)
            for neighbor in adjacency.get(current, set()):
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        component_set = set(component_nodes)
        component_edges = [
            edge
            for edge in edges
            if edge.source in component_set and edge.target in component_set
        ]
        kinds: dict[str, int] = {}
        owners: set[str] = set()
        sources: set[str] = set()
        entry_ids: set[str] = set()
        for node_id_value in component_nodes:
            item = node_map[node_id_value]
            kinds[str(item.kind)] = kinds.get(str(item.kind), 0) + 1
            if item.owner_unit:
                owners.add(item.owner_unit)
            if item.source_repo:
                sources.add(item.source_repo)
            if item.kind == EvidenceNodeKind.LEDGER_ENTRY:
                entry_ids.add(item.label if item.label.startswith("ile_") else node_id_value.split(":", 1)[-1])
        components.append(
            EvidenceComponent(
                component_id=f"component-{len(components) + 1}",
                node_count=len(component_nodes),
                edge_count=len(component_edges),
                kinds=dict(sorted(kinds.items())),
                owner_units=sorted(owners),
                source_repos=sorted(sources),
                entry_ids=sorted(entry_ids),
                isolated=len(component_nodes) == 1,
            )
        )
    return components


def build_target_impacts(project_root: Path, entries: Iterable[InternalizationLedgerEntry]) -> list[TargetImpact]:
    by_target: dict[str, list[InternalizationLedgerEntry]] = defaultdict(list)
    for entry in entries:
        for binding in entry.target_bindings:
            by_target[binding.target_path.replace("\\", "/")].append(entry)
    impacts: list[TargetImpact] = []
    for target_path, owners in by_target.items():
        classification = classify_path(target_path)
        impacts.append(
            TargetImpact(
                target_path=target_path,
                exists=(project_root / target_path).exists(),
                ledger_ids=sorted(entry.ledger_id for entry in owners),
                source_repos=sorted({entry.source_repo for entry in owners}),
                owner_units=sorted({entry.owner_unit for entry in owners if entry.owner_unit}),
                connected_entries=sum(1 for entry in owners if entry.main_path_status in CONNECTED_STATUSES),
                productized_entries=sum(1 for entry in owners if entry.lifecycle in PRODUCTIZED_LIFECYCLES),
                shared=len(owners) > 1,
                bucket_verdict=str(classification.verdict),
            )
        )
    return sorted(impacts, key=lambda item: (item.risk, item.target_path))


def build_evidence_findings(
    project_root: Path,
    entries: list[InternalizationLedgerEntry],
    nodes: list[EvidenceNode],
    edges: list[EvidenceEdge],
    components: list[EvidenceComponent],
    target_impacts: list[TargetImpact],
    owner_unit: str,
) -> list[EvidenceFinding]:
    findings: list[EvidenceFinding] = []
    findings.append(
        EvidenceFinding(
            code=EvidenceCode.GRAPH_BUILT,
            severity=EvidenceSeverity.INFO,
            message="Evidence graph was built from ledger entries.",
            metadata={"node_count": len(nodes), "edge_count": len(edges), "component_count": len(components), "owner_unit": owner_unit or "all"},
        )
    )
    for entry in entries:
        findings.extend(entry_evidence_findings(entry))
    for impact in target_impacts:
        if impact.risk == "blocker":
            findings.append(
                EvidenceFinding(
                    code=EvidenceCode.TARGET_NOT_MATERIALIZED,
                    severity=EvidenceSeverity.BLOCKER,
                    message="Connected ledger entry targets a missing path.",
                    path=impact.target_path,
                    remediation="Create the target path or downgrade main_path_status.",
                    metadata=impact.to_dict(),
                )
            )
        elif impact.risk == "warning":
            findings.append(
                EvidenceFinding(
                    code=EvidenceCode.TARGET_SHARED_WITHOUT_BOUNDARY,
                    severity=EvidenceSeverity.WARNING,
                    message="Target path is shared by multiple source repos or owner units.",
                    path=impact.target_path,
                    remediation="Document shared ownership or split target modules.",
                    metadata=impact.to_dict(),
                )
            )
        elif impact.risk == "review":
            findings.append(
                EvidenceFinding(
                    code=EvidenceCode.VENDOR_BUCKET_REQUIRES_REVIEW,
                    severity=EvidenceSeverity.WARNING,
                    message="Target path is classified as review/vendor-like.",
                    path=impact.target_path,
                    remediation="Do not count this target as deep internalization without Zyra-owned behavior evidence.",
                    metadata=impact.to_dict(),
                )
            )
    for component in components:
        if component.isolated:
            findings.append(
                EvidenceFinding(
                    code=EvidenceCode.EVIDENCE_COMPONENT_ISOLATED,
                    severity=EvidenceSeverity.WARNING,
                    message="Evidence graph contains an isolated node.",
                    node_id=component.component_id,
                    remediation="Attach evidence node to a ledger entry or remove stale evidence.",
                    metadata=component.to_dict(),
                )
            )
    findings.extend(coverage_findings(entries, owner_unit))
    return findings


def entry_evidence_findings(entry: InternalizationLedgerEntry) -> list[EvidenceFinding]:
    findings: list[EvidenceFinding] = []
    if not entry.target_bindings:
        findings.append(
            EvidenceFinding(
                code=EvidenceCode.ENTRY_WITHOUT_TARGET,
                severity=EvidenceSeverity.ERROR if entry.main_path_status in CONNECTED_STATUSES else EvidenceSeverity.WARNING,
                message="Ledger entry has no target bindings.",
                ledger_id=entry.ledger_id,
                owner_unit=entry.owner_unit,
                source_repo=entry.source_repo,
                remediation="Add target_bindings or keep entry planned/reference-only.",
            )
        )
    if entry.main_path_status in CONNECTED_STATUSES and not entry.test_entries:
        findings.append(
            EvidenceFinding(
                code=EvidenceCode.ENTRY_WITHOUT_TEST,
                severity=EvidenceSeverity.ERROR,
                message="Connected ledger entry has no behavior test edge.",
                ledger_id=entry.ledger_id,
                owner_unit=entry.owner_unit,
                source_repo=entry.source_repo,
                remediation="Add test_entries that assert runtime/API/CLI/event behavior.",
            )
        )
    if entry.main_path_status in CONNECTED_STATUSES and entry.runtime_entry.is_empty():
        findings.append(
            EvidenceFinding(
                code=EvidenceCode.ENTRY_WITHOUT_RUNTIME,
                severity=EvidenceSeverity.ERROR,
                message="Connected ledger entry has no runtime edge.",
                ledger_id=entry.ledger_id,
                owner_unit=entry.owner_unit,
                source_repo=entry.source_repo,
                remediation="Bind runtime_entry to a Zyra-owned module, command, or protocol.",
            )
        )
    if entry.main_path_status in CONNECTED_STATUSES and entry.main_path.is_empty():
        findings.append(
            EvidenceFinding(
                code=EvidenceCode.CONNECTED_WITHOUT_MAIN_PATH_EDGE,
                severity=EvidenceSeverity.ERROR,
                message="Connected ledger entry has no API/event/control/worker/UI edge.",
                ledger_id=entry.ledger_id,
                owner_unit=entry.owner_unit,
                source_repo=entry.source_repo,
                remediation="Record the production surface that triggers this capability.",
            )
        )
    if not entry.main_path.is_empty():
        findings.append(
            EvidenceFinding(
                code=EvidenceCode.MAIN_PATH_SURFACE_RECORDED,
                severity=EvidenceSeverity.INFO,
                message="Ledger entry records at least one main path surface.",
                ledger_id=entry.ledger_id,
                owner_unit=entry.owner_unit,
                source_repo=entry.source_repo,
            )
        )
    return findings


def coverage_findings(entries: list[InternalizationLedgerEntry], owner_unit: str) -> list[EvidenceFinding]:
    findings: list[EvidenceFinding] = []
    if not entries:
        findings.append(
            EvidenceFinding(
                code=EvidenceCode.OWNER_UNIT_UNCOVERED,
                severity=EvidenceSeverity.ERROR if owner_unit else EvidenceSeverity.WARNING,
                message="No ledger entries were available for evidence graph.",
                owner_unit=owner_unit,
                remediation="Seed source-to-target entries before claiming unit coverage.",
            )
        )
        return findings
    source_repos = {entry.source_repo for entry in entries}
    owner_units = {entry.owner_unit for entry in entries if entry.owner_unit}
    if owner_unit and owner_unit not in owner_units:
        findings.append(
            EvidenceFinding(
                code=EvidenceCode.OWNER_UNIT_UNCOVERED,
                severity=EvidenceSeverity.ERROR,
                message=f"Owner unit {owner_unit} has no evidence graph entries.",
                owner_unit=owner_unit,
                remediation="Add or fix owner_unit on ledger entries.",
            )
        )
    if not source_repos:
        findings.append(
            EvidenceFinding(
                code=EvidenceCode.SOURCE_REPO_UNCOVERED,
                severity=EvidenceSeverity.ERROR,
                message="Evidence graph has no source repositories.",
                remediation="Source repo identity is required for every entry.",
            )
        )
    findings.append(
        EvidenceFinding(
            code=EvidenceCode.DISCONNECT_IMPACT_RECORDED,
            severity=EvidenceSeverity.INFO,
            message="Disconnect impact can be derived from target_impacts and main_path edges.",
            metadata={"source_repo_count": len(source_repos), "owner_unit_count": len(owner_units)},
        )
    )
    return findings


def evidence_summary(
    nodes: list[EvidenceNode],
    edges: list[EvidenceEdge],
    components: list[EvidenceComponent],
    target_impacts: list[TargetImpact],
    findings: list[EvidenceFinding],
    owner_unit: str,
) -> dict[str, Any]:
    by_node_kind: dict[str, int] = {}
    by_edge_kind: dict[str, int] = {}
    by_code: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for node in nodes:
        by_node_kind[str(node.kind)] = by_node_kind.get(str(node.kind), 0) + 1
    for edge in edges:
        by_edge_kind[str(edge.kind)] = by_edge_kind.get(str(edge.kind), 0) + 1
    for finding in findings:
        by_code[str(finding.code)] = by_code.get(str(finding.code), 0) + 1
        by_severity[str(finding.severity)] = by_severity.get(str(finding.severity), 0) + 1
    return {
        "owner_unit": owner_unit or "all",
        "node_count": len(nodes),
        "edge_count": len(edges),
        "component_count": len(components),
        "isolated_components": sum(1 for item in components if item.isolated),
        "missing_targets": sum(1 for item in target_impacts if not item.exists),
        "shared_targets": sum(1 for item in target_impacts if item.shared),
        "connected_target_impacts": sum(1 for item in target_impacts if item.connected_entries),
        "by_node_kind": dict(sorted(by_node_kind.items())),
        "by_edge_kind": dict(sorted(by_edge_kind.items())),
        "findings_by_code": dict(sorted(by_code.items())),
        "findings_by_severity": dict(sorted(by_severity.items())),
    }


def evidence_graph_payload(report: EvidenceGraphReport) -> dict[str, Any]:
    payload = report.to_dict()
    payload["blocking_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.severity in {EvidenceSeverity.ERROR, EvidenceSeverity.BLOCKER}
    ]
    payload["warning_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.severity == EvidenceSeverity.WARNING
    ]
    return payload


def assert_evidence_graph(report: EvidenceGraphReport) -> None:
    if report.ok:
        return
    formatted = "\n".join(
        f"{finding.severity} {finding.code} {finding.ledger_id or finding.path or finding.node_id}: {finding.message}"
        for finding in report.findings
        if finding.blocking
    )
    raise AssertionError(f"Ledger evidence graph failed:\n{formatted}")


def node_id(kind: EvidenceNodeKind, raw_id: str) -> str:
    safe = (raw_id or "unknown").replace("\\", "/").strip()
    return f"{kind}:{safe}"


def normalize_route(route: str) -> str:
    parts = route.strip().split(maxsplit=1)
    if len(parts) == 2:
        return f"{parts[0].upper()} {parts[1]}"
    return route.strip()


def target_impact_for_path(report: EvidenceGraphReport, target_path: str) -> TargetImpact | None:
    normalized = target_path.replace("\\", "/")
    for impact in report.target_impacts:
        if impact.target_path == normalized:
            return impact
    return None


def entry_neighbors(report: EvidenceGraphReport, ledger_id: str) -> dict[str, list[str]]:
    entry_node = node_id(EvidenceNodeKind.LEDGER_ENTRY, ledger_id)
    neighbors: dict[str, list[str]] = defaultdict(list)
    for edge in report.edges:
        if edge.source == entry_node:
            neighbors[str(edge.kind)].append(edge.target)
        elif edge.target == entry_node:
            neighbors[str(edge.kind)].append(edge.source)
    return {key: sorted(values) for key, values in neighbors.items()}
