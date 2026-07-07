from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from zyra_core import EventRecord, EventType, to_jsonable

from .extraction_rules import RuleDecision, audit_extraction_plan
from .ledger_accounting import build_accounting_report
from .ledger_models import InternalizationLedgerEntry
from .ledger_policy import CountVerdict, classify_path
from .ledger_reachability import build_reachability_report
from .ledger_store import InternalizationLedger, load_project_ledger
from .source_extraction import ExtractionPlan, claude_code_m1_01b_plan


class LineageNodeKind(StrEnum):
    SOURCE_FILE = "source_file"
    TARGET_FILE = "target_file"
    RUNTIME_ENTRY = "runtime_entry"
    TEST_ENTRY = "test_entry"
    CONTROL_COMMAND = "control_command"
    EVENT_TYPE = "event_type"
    ARTIFACT_KIND = "artifact_kind"
    WORKER_RUNTIME = "worker_runtime"


class LineageEdgeKind(StrEnum):
    EXTRACTS_TO = "extracts_to"
    VERIFIED_BY = "verified_by"
    REACHES_RUNTIME = "reaches_runtime"
    PRODUCES_EVENT = "produces_event"
    EXPOSES_COMMAND = "exposes_command"
    WRITES_ARTIFACT = "writes_artifact"
    ROUTES_WORKER = "routes_worker"
    DERIVED_FROM = "derived_from"


class LineageRisk(StrEnum):
    OK = "ok"
    REVIEW = "review"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class LineageNode:
    node_id: str
    kind: LineageNodeKind
    label: str
    path: str = ""
    owner_unit: str = ""
    source_repo: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class LineageEdge:
    from_node: str
    to_node: str
    kind: LineageEdgeKind
    evidence: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class LineageFinding:
    code: str
    risk: LineageRisk
    message: str
    node_id: str = ""
    ledger_id: str = ""
    path: str = ""
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class TargetCustody:
    target_path: str
    role: str
    verdict: str
    surface: str
    exists: bool
    required_for_main_path: bool
    ledger_ids: list[str] = field(default_factory=list)
    source_paths: list[str] = field(default_factory=list)

    @property
    def effective(self) -> bool:
        return self.verdict == str(CountVerdict.EFFECTIVE)

    @property
    def source_pool(self) -> bool:
        return self.role == "source_pool"

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class SourceTargetLineageReport:
    project_root: Path
    owner_unit: str
    source_repo: str
    nodes: list[LineageNode]
    edges: list[LineageEdge]
    custody: list[TargetCustody]
    findings: list[LineageFinding] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(finding.risk in {LineageRisk.ERROR, LineageRisk.BLOCKER} for finding in self.findings)

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.risk == LineageRisk.BLOCKER)

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.risk == LineageRisk.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.risk == LineageRisk.WARNING)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "project_root": str(self.project_root),
            "owner_unit": self.owner_unit,
            "source_repo": self.source_repo,
            "summary": dict(self.summary),
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "custody": [item.to_dict() for item in self.custody],
            "findings": [finding.to_dict() for finding in self.findings],
            "blocker_count": self.blocker_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
        }


class SourceTargetLineageGraph:
    def __init__(self) -> None:
        self._nodes: dict[str, LineageNode] = {}
        self._edges: list[LineageEdge] = []

    def add_node(self, node: LineageNode) -> LineageNode:
        existing = self._nodes.get(node.node_id)
        if existing:
            metadata = {**existing.metadata, **node.metadata}
            self._nodes[node.node_id] = LineageNode(
                node_id=existing.node_id,
                kind=existing.kind,
                label=existing.label or node.label,
                path=existing.path or node.path,
                owner_unit=existing.owner_unit or node.owner_unit,
                source_repo=existing.source_repo or node.source_repo,
                metadata=metadata,
            )
        else:
            self._nodes[node.node_id] = node
        return self._nodes[node.node_id]

    def add_edge(self, edge: LineageEdge) -> None:
        if edge.from_node not in self._nodes or edge.to_node not in self._nodes:
            raise ValueError(f"lineage edge references unknown node: {edge.from_node} -> {edge.to_node}")
        if edge not in self._edges:
            self._edges.append(edge)

    def nodes(self) -> list[LineageNode]:
        return list(self._nodes.values())

    def edges(self) -> list[LineageEdge]:
        return list(self._edges)

    def outgoing(self, node_id: str, *, kind: LineageEdgeKind | None = None) -> list[LineageEdge]:
        return [edge for edge in self._edges if edge.from_node == node_id and (kind is None or edge.kind == kind)]

    def incoming(self, node_id: str, *, kind: LineageEdgeKind | None = None) -> list[LineageEdge]:
        return [edge for edge in self._edges if edge.to_node == node_id and (kind is None or edge.kind == kind)]

    def reachable_from(self, node_id: str) -> list[str]:
        seen: set[str] = set()
        queue: deque[str] = deque([node_id])
        while queue:
            current = queue.popleft()
            for edge in self.outgoing(current):
                if edge.to_node in seen:
                    continue
                seen.add(edge.to_node)
                queue.append(edge.to_node)
        return sorted(seen)

    def has_cycle(self) -> bool:
        visiting: set[str] = set()
        visited: set[str] = set()

        def walk(node_id: str) -> bool:
            if node_id in visiting:
                return True
            if node_id in visited:
                return False
            visiting.add(node_id)
            for edge in self.outgoing(node_id):
                if walk(edge.to_node):
                    return True
            visiting.remove(node_id)
            visited.add(node_id)
            return False

        return any(walk(node.node_id) for node in self.nodes())


def build_m1_01b_lineage_report(
    project_root: str | Path,
    *,
    source_workspace_root: str | Path | None = None,
    ledger: InternalizationLedger | None = None,
    plan: ExtractionPlan | None = None,
) -> SourceTargetLineageReport:
    root = Path(project_root).resolve()
    source_root = Path(source_workspace_root or root.parent).resolve()
    active_plan = plan or claude_code_m1_01b_plan(project_root=root, source_workspace_root=source_root, dry_run=True)
    active_ledger = ledger or load_project_ledger(root, bootstrap=True)
    entries = active_ledger.by_owner_unit("M1-01B")
    graph = SourceTargetLineageGraph()
    audit = audit_extraction_plan(active_plan)
    accounting = build_accounting_report(root, active_ledger, owner_unit="M1-01B", include_entries=False)
    reachability = build_reachability_report(root, active_ledger, owner_unit="M1-01B", include_entries=False, strict_audit=False)
    _add_rule_nodes(graph, audit.evaluations)
    _add_ledger_nodes(graph, root, entries)
    findings = _lineage_findings(root, graph, entries, audit_ok=audit.ok, accounting_ok=accounting.ok, reachability_ok=reachability.ok)
    custody = _build_custody(root, entries)
    summary = _summary(graph, custody, findings)
    summary.update(
        {
            "rule_audit_ok": audit.ok,
            "accounting_ok": accounting.ok,
            "reachability_ok": reachability.ok,
            "ledger_entries": len(entries),
            "included_sources": audit.summary()["included_files"],
        }
    )
    return SourceTargetLineageReport(
        project_root=root,
        owner_unit="M1-01B",
        source_repo=active_plan.source_repo,
        nodes=graph.nodes(),
        edges=graph.edges(),
        custody=custody,
        findings=findings,
        summary=summary,
    )


def lineage_payload(report: SourceTargetLineageReport) -> dict[str, Any]:
    return report.to_dict()


def lineage_event_records(report: SourceTargetLineageReport, *, run_id: str = "m1-01b", task_id: str = "lineage") -> list[EventRecord]:
    return [
        EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id="source-target-lineage",
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "source_target_lineage": {
                    "ok": report.ok,
                    "summary": report.summary,
                    "finding_count": len(report.findings),
                    "node_count": len(report.nodes),
                    "edge_count": len(report.edges),
                }
            },
        )
    ]


def assert_lineage_ok(report: SourceTargetLineageReport) -> None:
    if report.ok:
        return
    blocking = "\n".join(
        f"- {finding.risk} {finding.code}: {finding.message}"
        for finding in report.findings
        if finding.risk in {LineageRisk.ERROR, LineageRisk.BLOCKER}
    )
    raise AssertionError(f"M1-01B lineage failed:\n{blocking}")


def _add_rule_nodes(graph: SourceTargetLineageGraph, evaluations: Iterable[Any]) -> None:
    for evaluation in evaluations:
        if evaluation.decision not in {RuleDecision.INCLUDE, RuleDecision.REVIEW}:
            continue
        source_id = _source_node_id(evaluation.source_repo, evaluation.repo_path)
        graph.add_node(
            LineageNode(
                node_id=source_id,
                kind=LineageNodeKind.SOURCE_FILE,
                label=evaluation.repo_path,
                path=evaluation.repo_path,
                source_repo=evaluation.source_repo,
                owner_unit="M1-01B",
                metadata={
                    "decision": str(evaluation.decision),
                    "reason": str(evaluation.reason),
                    "message": evaluation.message,
                    "countable": evaluation.countable,
                },
            )
        )


def _add_ledger_nodes(graph: SourceTargetLineageGraph, project_root: Path, entries: Iterable[InternalizationLedgerEntry]) -> None:
    for entry in entries:
        source_id = _source_node_id(entry.source_repo, entry.source_path)
        graph.add_node(
            LineageNode(
                node_id=source_id,
                kind=LineageNodeKind.SOURCE_FILE,
                label=entry.source_path,
                path=entry.source_path,
                owner_unit=entry.owner_unit,
                source_repo=entry.source_repo,
                metadata={"ledger_id": entry.ledger_id, "capability_name": entry.capability_name},
            )
        )
        for binding in entry.target_bindings:
            classification = classify_path(binding.target_path)
            target_id = _target_node_id(binding.target_path)
            graph.add_node(
                LineageNode(
                    node_id=target_id,
                    kind=LineageNodeKind.TARGET_FILE,
                    label=binding.target_path,
                    path=binding.target_path,
                    owner_unit=entry.owner_unit,
                    source_repo=entry.source_repo,
                    metadata={
                        "role": binding.role,
                        "verdict": str(classification.verdict),
                        "surface": str(classification.surface),
                        "exists": (project_root / classification.normalized_path).exists(),
                    },
                )
            )
            graph.add_edge(
                LineageEdge(
                    from_node=source_id,
                    to_node=target_id,
                    kind=LineageEdgeKind.EXTRACTS_TO if binding.role == "source_pool" else LineageEdgeKind.DERIVED_FROM,
                    evidence=entry.ledger_id,
                    metadata={"role": binding.role, "required_for_main_path": binding.required_for_main_path},
                )
            )
        if not entry.runtime_entry.is_empty():
            runtime_id = f"runtime:{entry.runtime_entry.module}:{entry.runtime_entry.function}"
            graph.add_node(
                LineageNode(
                    node_id=runtime_id,
                    kind=LineageNodeKind.RUNTIME_ENTRY,
                    label=entry.runtime_entry.function or entry.runtime_entry.module,
                    owner_unit=entry.owner_unit,
                    metadata=to_jsonable(entry.runtime_entry),
                )
            )
            graph.add_edge(LineageEdge(source_id, runtime_id, LineageEdgeKind.REACHES_RUNTIME, evidence=entry.ledger_id))
        for test in entry.test_entries:
            test_id = f"test:{test.path}"
            graph.add_node(
                LineageNode(
                    node_id=test_id,
                    kind=LineageNodeKind.TEST_ENTRY,
                    label=test.path,
                    path=test.path,
                    owner_unit=entry.owner_unit,
                    metadata=to_jsonable(test),
                )
            )
            graph.add_edge(LineageEdge(source_id, test_id, LineageEdgeKind.VERIFIED_BY, evidence=entry.ledger_id))
        for command in entry.main_path.control_commands:
            command_id = f"command:{command}"
            graph.add_node(LineageNode(command_id, LineageNodeKind.CONTROL_COMMAND, command, owner_unit=entry.owner_unit))
            graph.add_edge(LineageEdge(source_id, command_id, LineageEdgeKind.EXPOSES_COMMAND, evidence=entry.ledger_id))
        for event_type in entry.main_path.event_types:
            event_id = f"event:{event_type}"
            graph.add_node(LineageNode(event_id, LineageNodeKind.EVENT_TYPE, event_type, owner_unit=entry.owner_unit))
            graph.add_edge(LineageEdge(source_id, event_id, LineageEdgeKind.PRODUCES_EVENT, evidence=entry.ledger_id))
        for artifact_kind in entry.main_path.artifact_kinds:
            artifact_id = f"artifact:{artifact_kind}"
            graph.add_node(LineageNode(artifact_id, LineageNodeKind.ARTIFACT_KIND, artifact_kind, owner_unit=entry.owner_unit))
            graph.add_edge(LineageEdge(source_id, artifact_id, LineageEdgeKind.WRITES_ARTIFACT, evidence=entry.ledger_id))
        if entry.main_path.worker_runtime:
            worker_id = f"worker:{entry.main_path.worker_runtime}"
            graph.add_node(LineageNode(worker_id, LineageNodeKind.WORKER_RUNTIME, entry.main_path.worker_runtime, owner_unit=entry.owner_unit))
            graph.add_edge(LineageEdge(source_id, worker_id, LineageEdgeKind.ROUTES_WORKER, evidence=entry.ledger_id))


def _build_custody(project_root: Path, entries: Iterable[InternalizationLedgerEntry]) -> list[TargetCustody]:
    grouped: dict[tuple[str, str], list[InternalizationLedgerEntry]] = defaultdict(list)
    required: dict[tuple[str, str], bool] = {}
    for entry in entries:
        for binding in entry.target_bindings:
            key = (binding.target_path, binding.role)
            grouped[key].append(entry)
            required[key] = required.get(key, False) or binding.required_for_main_path
    custody: list[TargetCustody] = []
    for (target_path, role), owners in sorted(grouped.items()):
        classification = classify_path(target_path)
        custody.append(
            TargetCustody(
                target_path=target_path,
                role=role,
                verdict=str(classification.verdict),
                surface=str(classification.surface),
                exists=classification.is_project_relative and (project_root / classification.normalized_path).exists(),
                required_for_main_path=required[(target_path, role)],
                ledger_ids=sorted({entry.ledger_id for entry in owners}),
                source_paths=sorted({entry.source_path for entry in owners}),
            )
        )
    return custody


def _lineage_findings(
    project_root: Path,
    graph: SourceTargetLineageGraph,
    entries: list[InternalizationLedgerEntry],
    *,
    audit_ok: bool,
    accounting_ok: bool,
    reachability_ok: bool,
) -> list[LineageFinding]:
    findings: list[LineageFinding] = []
    if graph.has_cycle():
        findings.append(
            LineageFinding(
                code="LINEAGE_CYCLE",
                risk=LineageRisk.ERROR,
                message="Lineage graph contains a cycle.",
                remediation="Split source, target, runtime, and verification nodes into directional edges.",
            )
        )
    if not audit_ok:
        findings.append(LineageFinding("RULE_AUDIT_FAILED", LineageRisk.ERROR, "Extraction rule audit has blockers."))
    if not accounting_ok:
        findings.append(LineageFinding("ACCOUNTING_FAILED", LineageRisk.ERROR, "Ledger accounting rejected one or more M1-01B entries."))
    if not reachability_ok:
        findings.append(LineageFinding("REACHABILITY_FAILED", LineageRisk.ERROR, "Ledger reachability rejected one or more M1-01B entries."))
    for entry in entries:
        classifications = [classify_path(path) for path in entry.target_paths]
        if not any(item.verdict == CountVerdict.EFFECTIVE for item in classifications):
            findings.append(
                LineageFinding(
                    code="NO_EFFECTIVE_TARGET",
                    risk=LineageRisk.ERROR,
                    message=f"{entry.ledger_id} has no effective Zyra-owned target.",
                    ledger_id=entry.ledger_id,
                    remediation="Bind the source item to production, script, or behavior-test code under Zyra.",
                )
            )
        if not any(binding.role == "source_pool" for binding in entry.target_bindings):
            findings.append(
                LineageFinding(
                    code="SOURCE_POOL_NOT_DECLARED",
                    risk=LineageRisk.WARNING,
                    message=f"{entry.ledger_id} does not identify the upstream pilot copy as source_pool evidence.",
                    ledger_id=entry.ledger_id,
                )
            )
        for binding in entry.target_bindings:
            classification = classify_path(binding.target_path)
            exists = classification.is_project_relative and (project_root / classification.normalized_path).exists()
            if binding.required_for_main_path and not exists:
                findings.append(
                    LineageFinding(
                        code="REQUIRED_TARGET_MISSING",
                        risk=LineageRisk.ERROR,
                        message=f"Required target does not exist: {binding.target_path}",
                        ledger_id=entry.ledger_id,
                        path=binding.target_path,
                        remediation="Create the Zyra-owned target or mark the binding non-main-path.",
                    )
                )
            if binding.role == "primary" and classification.verdict != CountVerdict.EFFECTIVE:
                findings.append(
                    LineageFinding(
                        code="PRIMARY_TARGET_NOT_EFFECTIVE",
                        risk=LineageRisk.ERROR,
                        message=f"Primary target is not effective code: {binding.target_path}",
                        ledger_id=entry.ledger_id,
                        path=binding.target_path,
                    )
                )
    return sorted(findings, key=lambda item: (str(item.risk), item.code, item.ledger_id, item.path))


def _summary(graph: SourceTargetLineageGraph, custody: list[TargetCustody], findings: list[LineageFinding]) -> dict[str, Any]:
    node_kinds = Counter(str(node.kind) for node in graph.nodes())
    edge_kinds = Counter(str(edge.kind) for edge in graph.edges())
    custody_roles = Counter(item.role for item in custody)
    custody_verdicts = Counter(item.verdict for item in custody)
    return {
        "ok": not any(finding.risk in {LineageRisk.ERROR, LineageRisk.BLOCKER} for finding in findings),
        "node_count": len(graph.nodes()),
        "edge_count": len(graph.edges()),
        "custody_count": len(custody),
        "effective_target_count": sum(1 for item in custody if item.effective),
        "source_pool_target_count": sum(1 for item in custody if item.source_pool),
        "missing_required_targets": sum(1 for item in custody if item.required_for_main_path and not item.exists),
        "node_kinds": dict(sorted(node_kinds.items())),
        "edge_kinds": dict(sorted(edge_kinds.items())),
        "custody_roles": dict(sorted(custody_roles.items())),
        "custody_verdicts": dict(sorted(custody_verdicts.items())),
        "finding_count": len(findings),
        "blocking_findings": sum(1 for finding in findings if finding.risk in {LineageRisk.ERROR, LineageRisk.BLOCKER}),
    }


def _source_node_id(source_repo: str, source_path: str) -> str:
    return f"source:{source_repo}:{source_path}"


def _target_node_id(target_path: str) -> str:
    return f"target:{target_path}"
