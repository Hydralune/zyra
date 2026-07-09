from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable


M1_02D_RESTORE_CAUSALITY_OWNER_UNIT = "M1-02D"
CODEWORKER_RESTORE_CAUSALITY_RUNTIME_ID = "codeworker_restore_causality_runtime"


class RestoreCausalityStatus(StrEnum):
    READY = "ready"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    BROKEN = "broken"
    DISABLED = "disabled"


class RestoreCausalitySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class RestoreCausalitySurface(StrEnum):
    COMPACT_BOUNDARY = "compact_boundary"
    RESTORE_CONTRACT = "restore_contract"
    CONTEXT_WINDOW = "context_window"
    MODEL_ENVELOPE = "model_envelope"
    TOOL_LOOP = "tool_loop"
    EVENT_LOG = "event_log"


class RestoreCausalityNodeKind(StrEnum):
    COMPACT_BOUNDARY = "compact_boundary"
    RESTORE_CONTRACT = "restore_contract"
    RESTORE_APPLICATION = "restore_application"
    CONTEXT_SECURITY = "context_security"
    MODEL_ENVELOPE = "model_envelope"
    MODEL_STREAM = "model_stream"
    API_RETRY = "api_retry"
    TOOL_BATCH = "tool_batch"
    TOOL_RESULT = "tool_result"
    TASK_API_PROJECTION = "task_api_projection"


class RestoreCausalityEdgeKind(StrEnum):
    CREATED = "created"
    CONSUMED_BY = "consumed_by"
    RESTORES = "restores"
    FEEDS = "feeds"
    OBSERVED_BY = "observed_by"
    RECOVERED_BY = "recovered_by"
    TRIGGERS = "triggers"


@dataclass(frozen=True, slots=True)
class RestoreCausalityFinding:
    code: str
    severity: RestoreCausalitySeverity
    surface: RestoreCausalitySurface
    message: str
    node_id: str = ""
    edge_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == RestoreCausalitySeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "node_id": self.node_id,
            "edge_id": self.edge_id,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RestoreCausalityNode:
    node_id: str
    kind: RestoreCausalityNodeKind
    label: str
    phase: str
    event_id: str = ""
    turn_index: int = 0
    contract_id: str = ""
    boundary_id: str = ""
    application_id: str = ""
    request_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": str(self.kind),
            "label": self.label,
            "phase": self.phase,
            "event_id": self.event_id,
            "turn_index": self.turn_index,
            "contract_id": self.contract_id,
            "boundary_id": self.boundary_id,
            "application_id": self.application_id,
            "request_id": self.request_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RestoreCausalityEdge:
    edge_id: str
    source_node_id: str
    target_node_id: str
    kind: RestoreCausalityEdgeKind
    label: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "kind": str(self.kind),
            "label": self.label,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RestoreCausalityReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    nodes: tuple[RestoreCausalityNode, ...]
    edges: tuple[RestoreCausalityEdge, ...]
    findings: tuple[RestoreCausalityFinding, ...]
    source_decisions: tuple[dict[str, str], ...]
    disabled: bool = False
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not self.disabled and not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> RestoreCausalityStatus:
        if self.disabled:
            return RestoreCausalityStatus.DISABLED
        if any(finding.blocking for finding in self.findings):
            return RestoreCausalityStatus.BROKEN
        if self.findings:
            return RestoreCausalityStatus.DEGRADED
        if self.path_connected:
            return RestoreCausalityStatus.CONNECTED
        return RestoreCausalityStatus.READY

    @property
    def path_connected(self) -> bool:
        compact_nodes = {node.node_id for node in self.nodes if node.kind == RestoreCausalityNodeKind.COMPACT_BOUNDARY}
        model_nodes = {node.node_id for node in self.nodes if node.kind == RestoreCausalityNodeKind.MODEL_ENVELOPE}
        if not compact_nodes or not model_nodes:
            return False
        adjacency: dict[str, set[str]] = {}
        for edge in self.edges:
            adjacency.setdefault(edge.source_node_id, set()).add(edge.target_node_id)
        frontier = list(compact_nodes)
        visited: set[str] = set()
        while frontier:
            node_id = frontier.pop()
            if node_id in visited:
                continue
            visited.add(node_id)
            if node_id in model_nodes:
                return True
            frontier.extend(adjacency.get(node_id, ()))
        return False

    @property
    def compact_node_count(self) -> int:
        return sum(1 for node in self.nodes if node.kind == RestoreCausalityNodeKind.COMPACT_BOUNDARY)

    @property
    def restore_application_count(self) -> int:
        return sum(1 for node in self.nodes if node.kind == RestoreCausalityNodeKind.RESTORE_APPLICATION)

    @property
    def model_envelope_count(self) -> int:
        return sum(1 for node in self.nodes if node.kind == RestoreCausalityNodeKind.MODEL_ENVELOPE)

    @property
    def blocking_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    def metadata(self) -> dict[str, str]:
        return {
            "restore_causality_report_id": self.report_id,
            "restore_causality_owner_unit": self.owner_unit,
            "restore_causality_runtime_id": self.runtime_id,
            "restore_causality_ok": str(self.ok).lower(),
            "restore_causality_status": str(self.status),
            "restore_causality_disabled": str(self.disabled).lower(),
            "restore_causality_nodes": str(len(self.nodes)),
            "restore_causality_edges": str(len(self.edges)),
            "restore_causality_compact_nodes": str(self.compact_node_count),
            "restore_causality_applications": str(self.restore_application_count),
            "restore_causality_model_envelopes": str(self.model_envelope_count),
            "restore_causality_path_connected": str(self.path_connected).lower(),
            "restore_causality_blocking_count": str(self.blocking_count),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.codeworker_restore_causality.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "disabled": self.disabled,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "findings": [finding.to_dict() for finding in self.findings],
            "source_decisions": [dict(item) for item in self.source_decisions],
            "path_connected": self.path_connected,
            "compact_node_count": self.compact_node_count,
            "restore_application_count": self.restore_application_count,
            "model_envelope_count": self.model_envelope_count,
            "metadata": self.metadata(),
            "created_at": self.created_at,
        }


class CodeWorkerRestoreCausalityRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_RESTORE_CAUSALITY_OWNER_UNIT,
        runtime_id: str = CODEWORKER_RESTORE_CAUSALITY_RUNTIME_ID,
        disabled: bool = False,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.disabled = disabled

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        event_records: Sequence[Any],
    ) -> RestoreCausalityReport:
        event_views = [_event_view(event) for event in event_records]
        nodes: list[RestoreCausalityNode] = []
        edges: list[RestoreCausalityEdge] = []
        findings: list[RestoreCausalityFinding] = []
        for event in event_views:
            query_session = _nested(event, "payload", "query_session")
            phase = str(query_session.get("phase") or "")
            if not phase:
                continue
            node = self._node_from_phase(event, query_session, phase)
            if node is not None:
                nodes.append(node)
        indexes = _indexes(nodes)
        edges.extend(self._contract_edges(indexes))
        edges.extend(self._application_edges(indexes))
        edges.extend(self._model_edges(indexes))
        edges.extend(self._recovery_edges(indexes))
        findings.extend(self._findings(nodes, edges))
        if self.disabled:
            findings.append(
                RestoreCausalityFinding(
                    code="RESTORE_CAUSALITY_RUNTIME_DISABLED",
                    severity=RestoreCausalitySeverity.BLOCKER,
                    surface=RestoreCausalitySurface.EVENT_LOG,
                    message="Restore causality runtime is disabled.",
                )
            )
        return RestoreCausalityReport(
            report_id=new_id("restore_causality"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            nodes=tuple(nodes),
            edges=tuple(edges),
            findings=tuple(findings),
            source_decisions=default_restore_causality_source_decisions(),
            disabled=self.disabled,
        )

    def event_for_report(
        self,
        report: RestoreCausalityReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        phase: str = "codeworker_restore_causality",
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
                    "phase": phase,
                    "restore_causality": report.to_dict(),
                }
            },
        )

    def metadata(self, report: RestoreCausalityReport | None = None) -> dict[str, str]:
        if report is None:
            return {
                "restore_causality_ok": str(not self.disabled).lower(),
                "restore_causality_status": str(
                    RestoreCausalityStatus.DISABLED if self.disabled else RestoreCausalityStatus.READY
                ),
                "restore_causality_runtime_id": self.runtime_id,
            }
        return report.metadata()

    def _node_from_phase(
        self,
        event: Mapping[str, Any],
        query_session: Mapping[str, Any],
        phase: str,
    ) -> RestoreCausalityNode | None:
        event_id = str(event.get("event_id") or "")
        turn_index = _safe_int(query_session.get("turn_index") or _nested(query_session, "metadata").get("turn_index"))
        if phase in {"compact_boundary_created", "compact_needed", "compact_restore_contract_pending"}:
            compact_restore = _as_mapping(query_session.get("compact_restore"))
            boundary = _as_mapping(query_session.get("compact_boundary") or compact_restore.get("boundary"))
            contract = _as_mapping(compact_restore.get("restore_contract") or query_session.get("next_turn_restore_contract"))
            return RestoreCausalityNode(
                node_id=new_id("restore_cause_node"),
                kind=RestoreCausalityNodeKind.COMPACT_BOUNDARY,
                label=phase,
                phase=phase,
                event_id=event_id,
                turn_index=turn_index,
                contract_id=str(contract.get("contract_id") or compact_restore.get("restore_contract_id") or ""),
                boundary_id=str(boundary.get("boundary_id") or ""),
                metadata=_metadata_strings({"compact_restore_report_id": compact_restore.get("report_id") or query_session.get("compact_restore_report_id") or ""}),
            )
        if phase == "next_turn_restore_contract":
            contract = _as_mapping(query_session.get("next_turn_restore_contract"))
            return RestoreCausalityNode(
                node_id=new_id("restore_cause_node"),
                kind=RestoreCausalityNodeKind.RESTORE_CONTRACT,
                label="next-turn restore contract",
                phase=phase,
                event_id=event_id,
                turn_index=turn_index,
                contract_id=str(contract.get("contract_id") or ""),
                boundary_id=str(contract.get("boundary_id") or ""),
                metadata={"segments": str(contract.get("restore_segment_count") or "")},
            )
        if phase == "codeworker_restore_context_applied":
            application = _as_mapping(query_session.get("restore_application"))
            return RestoreCausalityNode(
                node_id=new_id("restore_cause_node"),
                kind=RestoreCausalityNodeKind.RESTORE_APPLICATION,
                label="restore application",
                phase=phase,
                event_id=event_id,
                turn_index=_safe_int(application.get("turn_index") or turn_index),
                contract_id=str(application.get("contract_id") or ""),
                boundary_id=str(application.get("boundary_id") or ""),
                application_id=str(application.get("application_id") or ""),
                metadata={
                    "model_messages": str(application.get("model_message_count") or len(_as_list(application.get("messages")))),
                    "context_blocks": str(application.get("context_block_count") or len(_as_list(application.get("blocks")))),
                },
            )
        if phase == "codeworker_restore_context_security":
            snapshot = _as_mapping(query_session.get("context_security"))
            return RestoreCausalityNode(
                node_id=new_id("restore_cause_node"),
                kind=RestoreCausalityNodeKind.CONTEXT_SECURITY,
                label="restore context security",
                phase=phase,
                event_id=event_id,
                turn_index=turn_index,
                metadata={
                    "snapshot_id": str(snapshot.get("snapshot_id") or ""),
                    "ok": str(snapshot.get("ok") or ""),
                    "untrusted": str(snapshot.get("untrusted_count") or ""),
                },
            )
        if phase == "model_stream_report":
            model_stream = _as_mapping(query_session.get("model_stream"))
            envelope = _as_mapping(model_stream.get("envelope"))
            metadata = _as_mapping(envelope.get("metadata"))
            return RestoreCausalityNode(
                node_id=new_id("restore_cause_node"),
                kind=RestoreCausalityNodeKind.MODEL_ENVELOPE,
                label="model envelope",
                phase=phase,
                event_id=event_id,
                turn_index=_safe_int(envelope.get("turn_index") or turn_index),
                contract_id=str(metadata.get("restore_contract_id") or ""),
                application_id=str(metadata.get("restore_application_id") or ""),
                request_id=str(envelope.get("request_id") or ""),
                metadata={
                    "model": str(envelope.get("model") or ""),
                    "restore_model_message_count": str(metadata.get("restore_model_message_count") or "0"),
                    "error_kind": str(model_stream.get("error_kind") or ""),
                },
            )
        if phase == "api_retry_report":
            retry = _as_mapping(query_session.get("api_retry"))
            return RestoreCausalityNode(
                node_id=new_id("restore_cause_node"),
                kind=RestoreCausalityNodeKind.API_RETRY,
                label="api retry",
                phase=phase,
                event_id=event_id,
                turn_index=turn_index,
                metadata={
                    "status": str(retry.get("status") or ""),
                    "fallback_used": str(retry.get("fallback_used") or ""),
                    "retry_count": str(retry.get("retry_count") or ""),
                },
            )
        if phase == "tool_batch_started":
            return RestoreCausalityNode(
                node_id=new_id("restore_cause_node"),
                kind=RestoreCausalityNodeKind.TOOL_BATCH,
                label="tool batch",
                phase=phase,
                event_id=event_id,
                turn_index=turn_index,
                metadata={"batch_index": str(query_session.get("batch_index") or "")},
            )
        if phase == "tool_call_completed":
            return RestoreCausalityNode(
                node_id=new_id("restore_cause_node"),
                kind=RestoreCausalityNodeKind.TOOL_RESULT,
                label=str(query_session.get("tool_name") or "tool result"),
                phase=phase,
                event_id=event_id,
                turn_index=turn_index,
                metadata={
                    "tool_call_id": str(query_session.get("tool_call_id") or ""),
                    "tool_name": str(query_session.get("tool_name") or ""),
                    "ok": str(query_session.get("ok") or ""),
                },
            )
        if phase in {"codeworker_task_api_projection", "compact_state_projection"}:
            return RestoreCausalityNode(
                node_id=new_id("restore_cause_node"),
                kind=RestoreCausalityNodeKind.TASK_API_PROJECTION,
                label=phase,
                phase=phase,
                event_id=event_id,
                turn_index=turn_index,
            )
        return None

    def _contract_edges(self, indexes: Mapping[str, Any]) -> list[RestoreCausalityEdge]:
        edges: list[RestoreCausalityEdge] = []
        for compact in indexes["compact"]:
            contract_id = compact.contract_id
            if not contract_id:
                continue
            for contract in indexes["contracts_by_id"].get(contract_id, []):
                edges.append(_edge(compact, contract, RestoreCausalityEdgeKind.CREATED, "compact creates restore contract"))
        return edges

    def _application_edges(self, indexes: Mapping[str, Any]) -> list[RestoreCausalityEdge]:
        edges: list[RestoreCausalityEdge] = []
        for application in indexes["applications"]:
            for compact in indexes["compact"]:
                if application.contract_id and compact.contract_id == application.contract_id:
                    edges.append(_edge(compact, application, RestoreCausalityEdgeKind.CONSUMED_BY, "contract consumed by restore application"))
            for security in indexes["security"]:
                if security.turn_index == application.turn_index:
                    edges.append(_edge(application, security, RestoreCausalityEdgeKind.OBSERVED_BY, "restore observed by security runtime"))
        return edges

    def _model_edges(self, indexes: Mapping[str, Any]) -> list[RestoreCausalityEdge]:
        edges: list[RestoreCausalityEdge] = []
        for application in indexes["applications"]:
            for model in indexes["models"]:
                if application.application_id and model.application_id == application.application_id:
                    edges.append(_edge(application, model, RestoreCausalityEdgeKind.FEEDS, "restore messages feed model envelope"))
                elif application.contract_id and model.contract_id == application.contract_id:
                    edges.append(_edge(application, model, RestoreCausalityEdgeKind.FEEDS, "restore contract feeds model envelope"))
        for model in indexes["models"]:
            for batch in indexes["tool_batches"]:
                if batch.turn_index == model.turn_index:
                    edges.append(_edge(model, batch, RestoreCausalityEdgeKind.TRIGGERS, "model turn triggers tool batch"))
        for batch in indexes["tool_batches"]:
            for result in indexes["tool_results"]:
                if result.turn_index == batch.turn_index:
                    edges.append(_edge(batch, result, RestoreCausalityEdgeKind.TRIGGERS, "tool batch produces result"))
        return edges

    def _recovery_edges(self, indexes: Mapping[str, Any]) -> list[RestoreCausalityEdge]:
        edges: list[RestoreCausalityEdge] = []
        for model in indexes["models"]:
            for retry in indexes["retries"]:
                if retry.turn_index == model.turn_index:
                    edges.append(_edge(model, retry, RestoreCausalityEdgeKind.RECOVERED_BY, "model stream consumed by retry runtime"))
        return edges

    def _findings(
        self,
        nodes: Sequence[RestoreCausalityNode],
        edges: Sequence[RestoreCausalityEdge],
    ) -> list[RestoreCausalityFinding]:
        compact_nodes = [node for node in nodes if node.kind == RestoreCausalityNodeKind.COMPACT_BOUNDARY]
        applications = [node for node in nodes if node.kind == RestoreCausalityNodeKind.RESTORE_APPLICATION]
        models = [node for node in nodes if node.kind == RestoreCausalityNodeKind.MODEL_ENVELOPE]
        findings: list[RestoreCausalityFinding] = []
        if compact_nodes and not applications:
            findings.append(
                RestoreCausalityFinding(
                    code="COMPACT_BOUNDARY_WITHOUT_RESTORE_APPLICATION",
                    severity=RestoreCausalitySeverity.WARNING,
                    surface=RestoreCausalitySurface.RESTORE_CONTRACT,
                    message="A compact boundary was observed without a later restore application.",
                    node_id=compact_nodes[-1].node_id,
                )
            )
        if applications and not any(edge.kind == RestoreCausalityEdgeKind.FEEDS for edge in edges):
            findings.append(
                RestoreCausalityFinding(
                    code="RESTORE_APPLICATION_NOT_LINKED_TO_MODEL",
                    severity=RestoreCausalitySeverity.BLOCKER,
                    surface=RestoreCausalitySurface.MODEL_ENVELOPE,
                    message="Restore application exists but no causality edge reaches a model envelope.",
                    node_id=applications[-1].node_id,
                )
            )
        restore_sensitive_models = [
            node for node in models if _safe_int(node.metadata.get("restore_model_message_count")) > 0
        ]
        if applications and not restore_sensitive_models:
            findings.append(
                RestoreCausalityFinding(
                    code="MODEL_ENVELOPE_WITHOUT_RESTORE_MESSAGES",
                    severity=RestoreCausalitySeverity.BLOCKER,
                    surface=RestoreCausalitySurface.MODEL_ENVELOPE,
                    message="No model envelope reports restored compact messages.",
                )
            )
        return findings


def restore_causality_metadata(report: RestoreCausalityReport | None) -> dict[str, str]:
    if report is None:
        return {
            "restore_causality_ok": "false",
            "restore_causality_status": "missing",
            "restore_causality_report_id": "",
        }
    return report.metadata()


def default_restore_causality_source_decisions() -> tuple[dict[str, str], ...]:
    return (
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/compact/compact.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_restore_causality.py",
            "decision": "zyra_module_migrated",
            "capability": "compact boundary causality is linked to next-turn restore application",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/query.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_restore_causality.py",
            "decision": "zyra_module_migrated",
            "capability": "restore applications are linked to model envelopes and tool loop effects",
        },
        {
            "source_repo": "opencode",
            "source_path": "packages/opencode/src/session/events.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_restore_causality.py",
            "decision": "adapter_encapsulated",
            "capability": "event replay can prove compact restore dynamic reachability",
        },
    )


def _indexes(nodes: Sequence[RestoreCausalityNode]) -> dict[str, Any]:
    compact = [node for node in nodes if node.kind == RestoreCausalityNodeKind.COMPACT_BOUNDARY]
    contracts = [node for node in nodes if node.kind == RestoreCausalityNodeKind.RESTORE_CONTRACT]
    applications = [node for node in nodes if node.kind == RestoreCausalityNodeKind.RESTORE_APPLICATION]
    security = [node for node in nodes if node.kind == RestoreCausalityNodeKind.CONTEXT_SECURITY]
    models = [node for node in nodes if node.kind == RestoreCausalityNodeKind.MODEL_ENVELOPE]
    retries = [node for node in nodes if node.kind == RestoreCausalityNodeKind.API_RETRY]
    tool_batches = [node for node in nodes if node.kind == RestoreCausalityNodeKind.TOOL_BATCH]
    tool_results = [node for node in nodes if node.kind == RestoreCausalityNodeKind.TOOL_RESULT]
    contracts_by_id: dict[str, list[RestoreCausalityNode]] = {}
    for node in contracts:
        if node.contract_id:
            contracts_by_id.setdefault(node.contract_id, []).append(node)
    return {
        "compact": compact,
        "contracts": contracts,
        "contracts_by_id": contracts_by_id,
        "applications": applications,
        "security": security,
        "models": models,
        "retries": retries,
        "tool_batches": tool_batches,
        "tool_results": tool_results,
    }


def _edge(
    source: RestoreCausalityNode,
    target: RestoreCausalityNode,
    kind: RestoreCausalityEdgeKind,
    label: str,
) -> RestoreCausalityEdge:
    return RestoreCausalityEdge(
        edge_id=new_id("restore_cause_edge"),
        source_node_id=source.node_id,
        target_node_id=target.node_id,
        kind=kind,
        label=label,
        metadata={
            "source_phase": source.phase,
            "target_phase": target.phase,
            "contract_id": source.contract_id or target.contract_id,
            "turn_index": str(target.turn_index or source.turn_index),
        },
    )


def _event_view(event: Any) -> dict[str, Any]:
    if isinstance(event, EventRecord):
        return to_jsonable(event)
    if isinstance(event, Mapping):
        return dict(event)
    data = to_jsonable(event)
    return data if isinstance(data, dict) else {}


def _nested(value: Mapping[str, Any], *keys: str) -> Mapping[str, Any]:
    current: Any = value
    for key in keys:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return current if isinstance(current, Mapping) else {}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _metadata_strings(value: Mapping[str, Any]) -> dict[str, str]:
    return {str(k): str(v) for k, v in dict(value or {}).items() if v is not None}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
