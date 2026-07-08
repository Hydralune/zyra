from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID
from .tool_runtime_output_store import ToolOutputStoreSnapshot
from .tool_runtime_result_context import ToolResultContextReport


class ToolBudgetChainNodeKind(StrEnum):
    RAW_RESULT = "raw_result"
    BUDGET_DECISION = "budget_decision"
    BOUNDED_RESULT = "bounded_result"
    CONTEXT_LEDGER = "context_ledger"
    OUTPUT_STORE = "output_store"
    RESULT_CONTEXT = "result_context"
    ARTIFACT = "artifact"


class ToolBudgetChainStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    EMPTY = "empty"
    BLOCKED = "blocked"


class ToolBudgetChainSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ToolBudgetChainSurface(StrEnum):
    RECEIPT = "receipt"
    BUDGET = "budget"
    CONTEXT = "context"
    OUTPUT_STORE = "output_store"
    RESULT_CONTEXT = "result_context"
    ARTIFACT = "artifact"


@dataclass(frozen=True, slots=True)
class ToolBudgetChainNode:
    node_id: str
    kind: ToolBudgetChainNodeKind
    tool_call_id: str
    tool_name: str = ""
    chars: int = 0
    artifact_id: str = ""
    present: bool = True
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def externalized(self) -> bool:
        return bool(self.artifact_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": str(self.kind),
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "chars": self.chars,
            "artifact_id": self.artifact_id,
            "present": self.present,
            "externalized": self.externalized,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolBudgetChainEdge:
    edge_id: str
    from_node_id: str
    to_node_id: str
    tool_call_id: str
    required: bool
    satisfied: bool
    reason: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.required and not self.satisfied

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "from_node_id": self.from_node_id,
            "to_node_id": self.to_node_id,
            "tool_call_id": self.tool_call_id,
            "required": self.required,
            "satisfied": self.satisfied,
            "blocking": self.blocking,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolBudgetChainFinding:
    code: str
    severity: ToolBudgetChainSeverity
    surface: ToolBudgetChainSurface
    message: str
    tool_call_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolBudgetChainSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "tool_call_id": self.tool_call_id,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolBudgetChainReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    nodes: tuple[ToolBudgetChainNode, ...]
    edges: tuple[ToolBudgetChainEdge, ...]
    findings: tuple[ToolBudgetChainFinding, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings) and not any(edge.blocking for edge in self.edges)

    @property
    def status(self) -> ToolBudgetChainStatus:
        if any(finding.blocking for finding in self.findings) or any(edge.blocking for edge in self.edges):
            return ToolBudgetChainStatus.BLOCKED
        if not self.nodes:
            return ToolBudgetChainStatus.EMPTY
        if self.findings:
            return ToolBudgetChainStatus.DEGRADED
        return ToolBudgetChainStatus.READY

    @property
    def tool_call_count(self) -> int:
        return len({node.tool_call_id for node in self.nodes if node.tool_call_id})

    @property
    def budgeted_count(self) -> int:
        return len({node.tool_call_id for node in self.nodes if node.kind == ToolBudgetChainNodeKind.BUDGET_DECISION and node.externalized})

    @property
    def artifact_count(self) -> int:
        return len({node.artifact_id for node in self.nodes if node.artifact_id})

    @property
    def missing_required_edges(self) -> int:
        return sum(1 for edge in self.edges if edge.blocking)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.tool_budget_chain.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "tool_call_count": self.tool_call_count,
            "budgeted_count": self.budgeted_count,
            "artifact_count": self.artifact_count,
            "missing_required_edges": self.missing_required_edges,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_budget_chain_report_id": self.report_id,
            "tool_budget_chain_owner_unit": self.owner_unit,
            "tool_budget_chain_runtime_id": self.runtime_id,
            "tool_budget_chain_ok": str(self.ok).lower(),
            "tool_budget_chain_status": str(self.status),
            "tool_budget_chain_nodes": str(len(self.nodes)),
            "tool_budget_chain_edges": str(len(self.edges)),
            "tool_budget_chain_tool_calls": str(self.tool_call_count),
            "tool_budget_chain_budgeted": str(self.budgeted_count),
            "tool_budget_chain_artifacts": str(self.artifact_count),
            "tool_budget_chain_missing_edges": str(self.missing_required_edges),
            "tool_budget_chain_findings": str(len(self.findings)),
        }


class ToolBudgetChainRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        receipts: Sequence[Mapping[str, Any]],
        context_snapshots: Sequence[Mapping[str, Any]],
        output_store_snapshot: ToolOutputStoreSnapshot | None,
        result_context_report: ToolResultContextReport | None,
    ) -> ToolBudgetChainReport:
        nodes: list[ToolBudgetChainNode] = []
        context_index = _context_budget_index(context_snapshots)
        output_index = _output_store_index(output_store_snapshot)
        result_index = _result_context_index(result_context_report)
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                continue
            nodes.extend(self._nodes_for_receipt(receipt, context_index, output_index, result_index))
        edges = self._edges(nodes)
        findings = self._findings(nodes, edges)
        return ToolBudgetChainReport(
            report_id=new_id("toolbudgetchain"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            nodes=tuple(nodes),
            edges=tuple(edges),
            findings=tuple(findings),
        )

    def event_for_report(
        self,
        report: ToolBudgetChainReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": report.session_id,
                    "worker_request_id": report.worker_request_id,
                    "phase": "tool_budget_chain",
                    "tool_budget_chain": report.to_dict(),
                }
            },
        )

    def _nodes_for_receipt(
        self,
        receipt: Mapping[str, Any],
        context_index: Mapping[str, Mapping[str, Any]],
        output_index: Mapping[str, Mapping[str, Any]],
        result_index: Mapping[str, Mapping[str, Any]],
    ) -> list[ToolBudgetChainNode]:
        request = receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}
        raw_result = receipt.get("raw_result") if isinstance(receipt.get("raw_result"), Mapping) else {}
        bounded_result = receipt.get("bounded_result") if isinstance(receipt.get("bounded_result"), Mapping) else {}
        decision = receipt.get("budget_decision") if isinstance(receipt.get("budget_decision"), Mapping) else {}
        budget_receipt = receipt.get("budget_receipt") if isinstance(receipt.get("budget_receipt"), Mapping) else {}
        tool_call_id = str(request.get("tool_call_id") or bounded_result.get("tool_call_id") or raw_result.get("tool_call_id") or "")
        tool_name = str(request.get("tool_name") or "")
        raw_chars = _result_chars(raw_result)
        bounded_chars = _result_chars(bounded_result)
        artifact_id = str(decision.get("artifact_id") or "")
        nodes = [
            ToolBudgetChainNode(
                node_id=_node_id(tool_call_id, ToolBudgetChainNodeKind.RAW_RESULT),
                kind=ToolBudgetChainNodeKind.RAW_RESULT,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                chars=raw_chars,
                present=bool(raw_result),
                metadata={"ok": str(raw_result.get("ok") is True).lower(), "error": str(raw_result.get("error") or "")},
            ),
            ToolBudgetChainNode(
                node_id=_node_id(tool_call_id, ToolBudgetChainNodeKind.BUDGET_DECISION),
                kind=ToolBudgetChainNodeKind.BUDGET_DECISION,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                chars=_int(decision.get("original_chars") or budget_receipt.get("payload_chars") or raw_chars),
                artifact_id=artifact_id,
                present=bool(decision),
                metadata={"applied": str(decision.get("applied") is True).lower(), "reason": str(decision.get("reason") or "")},
            ),
            ToolBudgetChainNode(
                node_id=_node_id(tool_call_id, ToolBudgetChainNodeKind.BOUNDED_RESULT),
                kind=ToolBudgetChainNodeKind.BOUNDED_RESULT,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                chars=bounded_chars,
                artifact_id=str(_bounded_artifact_id(bounded_result) or artifact_id),
                present=bool(bounded_result),
                metadata={"ok": str(bounded_result.get("ok") is True).lower(), "error": str(bounded_result.get("error") or "")},
            ),
        ]
        context = context_index.get(tool_call_id) or context_index.get(f"artifact:{artifact_id}")
        nodes.append(
            ToolBudgetChainNode(
                node_id=_node_id(tool_call_id, ToolBudgetChainNodeKind.CONTEXT_LEDGER),
                kind=ToolBudgetChainNodeKind.CONTEXT_LEDGER,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                artifact_id=str((context or {}).get("artifact_id") or ""),
                present=context is not None or decision.get("applied") is not True,
                metadata={str(k): str(v) for k, v in dict(context or {}).items()},
            )
        )
        output = output_index.get(tool_call_id)
        nodes.append(
            ToolBudgetChainNode(
                node_id=_node_id(tool_call_id, ToolBudgetChainNodeKind.OUTPUT_STORE),
                kind=ToolBudgetChainNodeKind.OUTPUT_STORE,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                chars=_int((output or {}).get("output_chars")),
                artifact_id=str((output or {}).get("externalized_artifact_id") or ""),
                present=output is not None,
                metadata={str(k): str(v) for k, v in dict(output or {}).items() if k in {"kind", "entry_id", "budget_applied"}},
            )
        )
        projection = result_index.get(tool_call_id)
        nodes.append(
            ToolBudgetChainNode(
                node_id=_node_id(tool_call_id, ToolBudgetChainNodeKind.RESULT_CONTEXT),
                kind=ToolBudgetChainNodeKind.RESULT_CONTEXT,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                chars=_int((projection or {}).get("inline_chars")),
                artifact_id=str((projection or {}).get("externalized_artifact_id") or ""),
                present=projection is not None,
                metadata={str(k): str(v) for k, v in dict(projection or {}).items() if k in {"kind", "budget_applied", "raw_output_blocked"}},
            )
        )
        for artifact in _artifact_ids(bounded_result, decision, output, projection):
            nodes.append(
                ToolBudgetChainNode(
                    node_id=f"{_node_id(tool_call_id, ToolBudgetChainNodeKind.ARTIFACT)}:{artifact}",
                    kind=ToolBudgetChainNodeKind.ARTIFACT,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    artifact_id=artifact,
                    present=True,
                )
            )
        return nodes

    def _edges(self, nodes: Sequence[ToolBudgetChainNode]) -> list[ToolBudgetChainEdge]:
        by_tool: dict[str, dict[ToolBudgetChainNodeKind, list[ToolBudgetChainNode]]] = {}
        for node in nodes:
            by_tool.setdefault(node.tool_call_id, {}).setdefault(node.kind, []).append(node)
        edges: list[ToolBudgetChainEdge] = []
        for tool_call_id, node_map in by_tool.items():
            raw = _first(node_map, ToolBudgetChainNodeKind.RAW_RESULT)
            decision = _first(node_map, ToolBudgetChainNodeKind.BUDGET_DECISION)
            bounded = _first(node_map, ToolBudgetChainNodeKind.BOUNDED_RESULT)
            context = _first(node_map, ToolBudgetChainNodeKind.CONTEXT_LEDGER)
            output = _first(node_map, ToolBudgetChainNodeKind.OUTPUT_STORE)
            projection = _first(node_map, ToolBudgetChainNodeKind.RESULT_CONTEXT)
            budgeted = decision is not None and decision.externalized
            edges.append(_edge(raw, decision, tool_call_id, required=True, reason="raw result must be measured by budget decision"))
            edges.append(_edge(decision, bounded, tool_call_id, required=True, reason="budget decision must produce bounded result"))
            edges.append(_edge(bounded, output, tool_call_id, required=True, reason="bounded result must be persisted in output store"))
            edges.append(_edge(bounded, projection, tool_call_id, required=True, reason="bounded result must be projected to next context"))
            edges.append(_edge(decision, context, tool_call_id, required=budgeted, reason="budgeted result must leave context ledger"))
            for artifact in node_map.get(ToolBudgetChainNodeKind.ARTIFACT, []):
                edges.append(_edge(decision, artifact, tool_call_id, required=budgeted, reason="externalized budget decision must link artifact"))
                edges.append(_edge(projection, artifact, tool_call_id, required=budgeted, reason="result context must expose externalized artifact"))
        return edges

    def _findings(
        self,
        nodes: Sequence[ToolBudgetChainNode],
        edges: Sequence[ToolBudgetChainEdge],
    ) -> list[ToolBudgetChainFinding]:
        findings: list[ToolBudgetChainFinding] = []
        if not nodes:
            return findings
        for edge in edges:
            if edge.blocking:
                findings.append(
                    ToolBudgetChainFinding(
                        code="TOOL_BUDGET_CHAIN_EDGE_MISSING",
                        severity=ToolBudgetChainSeverity.BLOCKER,
                        surface=ToolBudgetChainSurface.BUDGET,
                        message=edge.reason,
                        tool_call_id=edge.tool_call_id,
                        metadata={"edge_id": edge.edge_id, "from": edge.from_node_id, "to": edge.to_node_id},
                    )
                )
        for node in nodes:
            if node.kind in {ToolBudgetChainNodeKind.OUTPUT_STORE, ToolBudgetChainNodeKind.RESULT_CONTEXT} and not node.present:
                findings.append(
                    ToolBudgetChainFinding(
                        code="TOOL_BUDGET_CHAIN_NODE_MISSING",
                        severity=ToolBudgetChainSeverity.BLOCKER,
                        surface=ToolBudgetChainSurface.RESULT_CONTEXT if node.kind == ToolBudgetChainNodeKind.RESULT_CONTEXT else ToolBudgetChainSurface.OUTPUT_STORE,
                        message=f"Required budget chain node is missing: {node.kind}.",
                        tool_call_id=node.tool_call_id,
                    )
                )
        return findings


def tool_budget_chain_metadata(report: ToolBudgetChainReport | None) -> dict[str, str]:
    if report is None:
        return {"tool_budget_chain_ok": "false", "tool_budget_chain_nodes": "0"}
    return report.metadata()


def assert_tool_budget_chain_ready(report: ToolBudgetChainReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.findings if finding.blocking)
    raise AssertionError(f"tool budget chain blocked: {blockers or 'missing edge'}")


def render_tool_budget_chain_markdown(report: ToolBudgetChainReport) -> str:
    lines = [
        "## Tool Budget Chain",
        "",
        f"- status: `{report.status}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- nodes: `{len(report.nodes)}`",
        f"- edges: `{len(report.edges)}`",
        f"- budgeted: `{report.budgeted_count}`",
        "",
        "### Findings",
        "",
    ]
    if report.findings:
        lines.extend(f"- `{finding.code}` [{finding.severity}]: {finding.message}" for finding in report.findings)
    else:
        lines.append("- no findings")
    return "\n".join(lines)


def _context_budget_index(context_snapshots: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    for snapshot in context_snapshots:
        if not isinstance(snapshot, Mapping):
            continue
        ledger = snapshot.get("budget_ledger") if isinstance(snapshot.get("budget_ledger"), Sequence) else ()
        for item in ledger:
            if not isinstance(item, Mapping):
                continue
            tool_call_id = str(item.get("tool_call_id") or item.get("key") or item.get("id") or "")
            artifact_id = str(item.get("artifact_id") or "")
            if not tool_call_id and artifact_id:
                tool_call_id = _tool_call_from_artifact_hint(artifact_id)
            if tool_call_id:
                index[tool_call_id] = item
            if artifact_id:
                index[f"artifact:{artifact_id}"] = item
    return index


def _output_store_index(snapshot: ToolOutputStoreSnapshot | None) -> dict[str, Mapping[str, Any]]:
    if snapshot is None:
        return {}
    return {entry.tool_call_id: entry.to_dict() for entry in snapshot.entries}


def _result_context_index(report: ToolResultContextReport | None) -> dict[str, Mapping[str, Any]]:
    if report is None:
        return {}
    return {projection.tool_call_id: projection.to_dict() for projection in report.projections}


def _result_chars(result: Mapping[str, Any]) -> int:
    output = result.get("output")
    try:
        return len(str(to_jsonable(output)))
    except Exception:
        return 0


def _bounded_artifact_id(result: Mapping[str, Any]) -> str:
    output = result.get("output") if isinstance(result.get("output"), Mapping) else {}
    return str(output.get("full_output_artifact_id") or "")


def _artifact_ids(
    result: Mapping[str, Any],
    decision: Mapping[str, Any],
    output: Mapping[str, Any] | None,
    projection: Mapping[str, Any] | None,
) -> list[str]:
    ids: list[str] = []
    for artifact in result.get("artifacts") if isinstance(result.get("artifacts"), Sequence) else ():
        if isinstance(artifact, Mapping):
            artifact_id = str(artifact.get("artifact_id") or "")
        else:
            artifact_id = str(getattr(artifact, "artifact_id", "") or "")
        if artifact_id and artifact_id not in ids:
            ids.append(artifact_id)
    for value in (
        decision.get("artifact_id"),
        _bounded_artifact_id(result),
        (output or {}).get("externalized_artifact_id"),
        (projection or {}).get("externalized_artifact_id"),
    ):
        artifact_id = str(value or "")
        if artifact_id and artifact_id not in ids:
            ids.append(artifact_id)
    projection_artifacts = (projection or {}).get("artifact_ids") if isinstance((projection or {}).get("artifact_ids"), Sequence) else ()
    for artifact_id in projection_artifacts:
        value = str(artifact_id or "")
        if value and value not in ids:
            ids.append(value)
    return ids


def _node_id(tool_call_id: str, kind: ToolBudgetChainNodeKind) -> str:
    return f"{tool_call_id}:{kind}"


def _first(
    mapping: Mapping[ToolBudgetChainNodeKind, Sequence[ToolBudgetChainNode]],
    kind: ToolBudgetChainNodeKind,
) -> ToolBudgetChainNode | None:
    values = mapping.get(kind) or ()
    return values[0] if values else None


def _edge(
    before: ToolBudgetChainNode | None,
    after: ToolBudgetChainNode | None,
    tool_call_id: str,
    *,
    required: bool,
    reason: str,
) -> ToolBudgetChainEdge:
    return ToolBudgetChainEdge(
        edge_id=new_id("toolbudgetedge"),
        from_node_id=before.node_id if before else "",
        to_node_id=after.node_id if after else "",
        tool_call_id=tool_call_id,
        required=required,
        satisfied=before is not None and before.present and after is not None and after.present,
        reason=reason,
    )


def _tool_call_from_artifact_hint(artifact_id: str) -> str:
    return artifact_id.split(":")[0] if ":" in artifact_id else ""


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
