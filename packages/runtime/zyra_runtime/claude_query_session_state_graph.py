from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable


class QuerySessionStateGraphStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class QuerySessionStateGraphSeverity(StrEnum):
    PASS = "pass"
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class QuerySessionStateGraphSurface(StrEnum):
    SESSION_STORE = "session_store"
    INPUT = "input"
    CONTEXT = "context"
    CONTROL = "control"
    CHECKPOINT = "checkpoint"
    QUERY_ENTRY = "query_entry"
    HANDOFF = "handoff"
    EVENT_FLOW = "event_flow"
    CUSTODY = "custody"
    QUERY_ENGINE = "query_engine"
    TRANSCRIPT = "transcript"
    LIFECYCLE = "lifecycle"


class QuerySessionStateNodeKind(StrEnum):
    SESSION_SEED = "session_seed"
    INPUT_ACCEPTED = "input_accepted"
    CONTEXT_SNAPSHOT = "context_snapshot"
    CONTROL_STATE = "control_state"
    CHECKPOINT = "checkpoint"
    ENTRY_PACKET = "entry_packet"
    HANDOFF_CONTRACT = "handoff_contract"
    CUSTODY = "custody"
    QUERY_STARTED = "query_started"
    ENGINE_STREAM = "engine_stream"
    TRANSCRIPT = "transcript"
    LIFECYCLE = "lifecycle"
    BLOCKED_TERMINAL = "blocked_terminal"


class QuerySessionStateEdgeKind(StrEnum):
    OWNS = "owns"
    PRECEDES = "precedes"
    RESTORES = "restores"
    GATES = "gates"
    HANDS_OFF = "hands_off"
    STARTS = "starts"
    STREAMS = "streams"
    MATERIALIZES = "materializes"
    TERMINATES = "terminates"


@dataclass(frozen=True, slots=True)
class QuerySessionStateNode:
    node_id: str
    kind: QuerySessionStateNodeKind
    label: str
    present: bool
    ok: bool
    sequence: int = 0
    surface: QuerySessionStateGraphSurface = QuerySessionStateGraphSurface.LIFECYCLE
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": str(self.kind),
            "label": self.label,
            "present": self.present,
            "ok": self.ok,
            "sequence": self.sequence,
            "surface": str(self.surface),
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QuerySessionStateEdge:
    edge_id: str
    kind: QuerySessionStateEdgeKind
    source_node_id: str
    target_node_id: str
    present: bool
    ok: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "kind": str(self.kind),
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "present": self.present,
            "ok": self.ok,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QuerySessionStateGraphFinding:
    code: str
    severity: QuerySessionStateGraphSeverity
    surface: QuerySessionStateGraphSurface
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == QuerySessionStateGraphSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "blocking": self.blocking,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QuerySessionStateGraphReport:
    report_id: str
    status: QuerySessionStateGraphStatus
    session_id: str
    worker_request_id: str
    nodes: tuple[QuerySessionStateNode, ...]
    edges: tuple[QuerySessionStateEdge, ...]
    findings: tuple[QuerySessionStateGraphFinding, ...]
    blocked_before_engine: bool
    engine_stream_expected: bool
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {QuerySessionStateGraphStatus.READY, QuerySessionStateGraphStatus.DEGRADED}

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == QuerySessionStateGraphSeverity.WARNING)

    @property
    def first_blocker_code(self) -> str:
        for finding in self.findings:
            if finding.blocking:
                return finding.code
        return ""

    @property
    def ready_node_count(self) -> int:
        return sum(1 for node in self.nodes if node.present and node.ok)

    @property
    def ready_edge_count(self) -> int:
        return sum(1 for edge in self.edges if edge.present and edge.ok)

    def node(self, kind: QuerySessionStateNodeKind) -> QuerySessionStateNode | None:
        for node in self.nodes:
            if node.kind == kind:
                return node
        return None

    def metadata_values(self) -> dict[str, str]:
        return {
            "query_state_graph_report_id": self.report_id,
            "query_state_graph_ok": str(self.ok).lower(),
            "query_state_graph_status": str(self.status),
            "query_state_graph_blockers": str(self.blocker_count),
            "query_state_graph_warnings": str(self.warning_count),
            "query_state_graph_first_blocker": self.first_blocker_code,
            "query_state_graph_node_count": str(len(self.nodes)),
            "query_state_graph_edge_count": str(len(self.edges)),
            "query_state_graph_ready_node_count": str(self.ready_node_count),
            "query_state_graph_ready_edge_count": str(self.ready_edge_count),
            "query_state_graph_blocked_before_engine": str(self.blocked_before_engine).lower(),
            "query_state_graph_engine_stream_expected": str(self.engine_stream_expected).lower(),
            "query_state_graph_has_stream_node": str(
                bool(self.node(QuerySessionStateNodeKind.ENGINE_STREAM))
                and bool(self.node(QuerySessionStateNodeKind.ENGINE_STREAM).present)  # type: ignore[union-attr]
            ).lower(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "status": str(self.status),
            "ok": self.ok,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "findings": [finding.to_dict() for finding in self.findings],
            "blocker_count": self.blocker_count,
            "warning_count": self.warning_count,
            "first_blocker_code": self.first_blocker_code,
            "ready_node_count": self.ready_node_count,
            "ready_edge_count": self.ready_edge_count,
            "blocked_before_engine": self.blocked_before_engine,
            "engine_stream_expected": self.engine_stream_expected,
            "created_at": self.created_at,
            "metadata": to_jsonable(self.metadata),
        }


class QuerySessionStateGraphRuntime:
    """Builds a causal state graph for query session lifecycle integration."""

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        store_records: Sequence[Mapping[str, Any]],
        packet: Mapping[str, Any],
        integration_report: Mapping[str, Any],
        handoff_report: Mapping[str, Any],
        custody_report: Mapping[str, Any],
        event_flow_report: Mapping[str, Any],
        events: Sequence[EventRecord],
        blocked_before_engine: bool,
        engine_stream_expected: bool,
    ) -> QuerySessionStateGraphReport:
        phases = query_session_phases(events)
        nodes = tuple(
            build_state_nodes(
                store_records=store_records,
                packet=packet,
                integration_report=integration_report,
                handoff_report=handoff_report,
                custody_report=custody_report,
                event_flow_report=event_flow_report,
                phases=phases,
                blocked_before_engine=blocked_before_engine,
                engine_stream_expected=engine_stream_expected,
            )
        )
        edges = tuple(build_state_edges(nodes, packet=packet, phases=phases, blocked_before_engine=blocked_before_engine))
        findings = [
            *validate_state_nodes(nodes, blocked_before_engine=blocked_before_engine, engine_stream_expected=engine_stream_expected),
            *validate_state_edges(edges),
            *validate_state_phase_consistency(nodes, phases, blocked_before_engine=blocked_before_engine),
            *validate_state_identity(nodes, packet, session_id=session_id, worker_request_id=worker_request_id),
        ]
        status = state_graph_status_from_findings(findings)
        return QuerySessionStateGraphReport(
            report_id=new_id("qgraph"),
            status=status,
            session_id=session_id,
            worker_request_id=worker_request_id,
            nodes=nodes,
            edges=edges,
            findings=tuple(findings),
            blocked_before_engine=blocked_before_engine,
            engine_stream_expected=engine_stream_expected,
            metadata={
                "source_path": "src/QueryEngine.ts",
                "target_path": "packages/runtime/zyra_runtime/claude_query_session_state_graph.py",
                "owner_unit": "M1-02B",
            },
        )

    def event_for_report(
        self,
        report: QuerySessionStateGraphReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None = None,
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "phase": "query_state_graph",
                    "session_id": report.session_id,
                    "worker_request_id": report.worker_request_id,
                    "ok": report.ok,
                    "status": str(report.status),
                    "node_count": len(report.nodes),
                    "edge_count": len(report.edges),
                    "ready_node_count": report.ready_node_count,
                    "ready_edge_count": report.ready_edge_count,
                    "blocked_before_engine": report.blocked_before_engine,
                    "engine_stream_expected": report.engine_stream_expected,
                    "blocker_count": report.blocker_count,
                    "first_blocker_code": report.first_blocker_code,
                }
            },
        )


def build_state_nodes(
    *,
    store_records: Sequence[Mapping[str, Any]],
    packet: Mapping[str, Any],
    integration_report: Mapping[str, Any],
    handoff_report: Mapping[str, Any],
    custody_report: Mapping[str, Any],
    event_flow_report: Mapping[str, Any],
    phases: Sequence[str],
    blocked_before_engine: bool,
    engine_stream_expected: bool,
) -> Iterable[QuerySessionStateNode]:
    record_types = [str(record.get("record_type") or "") for record in store_records]
    first_sequence = first_record_sequence(store_records)
    packet_ok = packet.get("ok") is True
    integration_ok = integration_report.get("ok") is True
    handoff_ok = handoff_report.get("ok") is True
    custody_ok = custody_report.get("ok") is True
    event_flow_ok = event_flow_report.get("ok") is True
    yield QuerySessionStateNode(
        node_id="session_seed",
        kind=QuerySessionStateNodeKind.SESSION_SEED,
        label="CodeWorker session seed",
        present="session_seed" in record_types,
        ok="session_seed" in record_types,
        sequence=first_sequence.get("session_seed", 0),
        surface=QuerySessionStateGraphSurface.SESSION_STORE,
        metadata={"record_count": record_types.count("session_seed")},
    )
    yield QuerySessionStateNode(
        node_id="input_accepted",
        kind=QuerySessionStateNodeKind.INPUT_ACCEPTED,
        label="Accepted user input",
        present="input_accepted" in record_types,
        ok="input_accepted" in record_types,
        sequence=first_sequence.get("input_accepted", 0),
        surface=QuerySessionStateGraphSurface.INPUT,
        metadata={"record_count": record_types.count("input_accepted")},
    )
    yield QuerySessionStateNode(
        node_id="context_snapshot",
        kind=QuerySessionStateNodeKind.CONTEXT_SNAPSHOT,
        label="ContextAssemblyRuntime snapshot",
        present="context_snapshot" in record_types or bool(_as_mapping(packet.get("context")).get("fingerprint")),
        ok=_as_mapping(packet.get("context")).get("ok") is not False
        and bool(_as_mapping(packet.get("context")).get("fingerprint")),
        sequence=first_sequence.get("context_snapshot", 0),
        surface=QuerySessionStateGraphSurface.CONTEXT,
        metadata={"fingerprint": _as_mapping(packet.get("context")).get("fingerprint")},
    )
    yield QuerySessionStateNode(
        node_id="control_state",
        kind=QuerySessionStateNodeKind.CONTROL_STATE,
        label="Query control state",
        present="query_control_state" in record_types or bool(packet.get("control")),
        ok=_as_mapping(packet.get("control")).get("ok") is True,
        sequence=first_sequence.get("query_control_state", 0),
        surface=QuerySessionStateGraphSurface.CONTROL,
        metadata=_as_mapping(packet.get("control")),
    )
    yield QuerySessionStateNode(
        node_id="checkpoint",
        kind=QuerySessionStateNodeKind.CHECKPOINT,
        label="Pre-query checkpoint",
        present="query_checkpoint" in record_types or bool(packet.get("checkpoint")),
        ok=_as_mapping(packet.get("checkpoint")).get("ok") is not False,
        sequence=first_sequence.get("query_checkpoint", 0),
        surface=QuerySessionStateGraphSurface.CHECKPOINT,
        metadata=_as_mapping(packet.get("checkpoint")),
    )
    yield QuerySessionStateNode(
        node_id="entry_packet",
        kind=QuerySessionStateNodeKind.ENTRY_PACKET,
        label="QueryEntryPacket",
        present="query_entry_packet" in record_types or bool(packet.get("packet_id")),
        ok=packet_ok and integration_ok,
        sequence=first_sequence.get("query_entry_packet", 0),
        surface=QuerySessionStateGraphSurface.QUERY_ENTRY,
        metadata={"packet_id": packet.get("packet_id"), "route": packet.get("route"), "block_reason": packet.get("block_reason")},
    )
    yield QuerySessionStateNode(
        node_id="handoff_contract",
        kind=QuerySessionStateNodeKind.HANDOFF_CONTRACT,
        label="M1-02C handoff contract",
        present=bool(handoff_report.get("report_id")),
        ok=handoff_ok,
        surface=QuerySessionStateGraphSurface.HANDOFF,
        metadata={"target_unit": handoff_report.get("target_unit"), "ready_port_count": handoff_report.get("ready_port_count")},
    )
    yield QuerySessionStateNode(
        node_id="custody",
        kind=QuerySessionStateNodeKind.CUSTODY,
        label="Store custody report",
        present=bool(custody_report.get("report_id")),
        ok=custody_ok,
        surface=QuerySessionStateGraphSurface.CUSTODY,
        metadata={"record_count": custody_report.get("record_count"), "engine_attached": custody_report.get("engine_attached")},
    )
    yield QuerySessionStateNode(
        node_id="query_started",
        kind=QuerySessionStateNodeKind.QUERY_STARTED,
        label="query_started event",
        present="query_started" in phases,
        ok=("query_started" in phases) is (not blocked_before_engine),
        surface=QuerySessionStateGraphSurface.EVENT_FLOW,
        metadata={"phase_index": phase_index(phases, "query_started")},
    )
    yield QuerySessionStateNode(
        node_id="engine_stream",
        kind=QuerySessionStateNodeKind.ENGINE_STREAM,
        label="QueryEngine stream_request_start",
        present="stream_request_start" in phases,
        ok=("stream_request_start" in phases) is engine_stream_expected,
        surface=QuerySessionStateGraphSurface.QUERY_ENGINE,
        metadata={"phase_index": phase_index(phases, "stream_request_start")},
    )
    yield QuerySessionStateNode(
        node_id="transcript",
        kind=QuerySessionStateNodeKind.TRANSCRIPT,
        label="Transcript mapping",
        present="transcript_event_mapping" in phases,
        ok=("transcript_event_mapping" in phases) is engine_stream_expected,
        surface=QuerySessionStateGraphSurface.TRANSCRIPT,
        metadata={"phase_index": phase_index(phases, "transcript_event_mapping")},
    )
    yield QuerySessionStateNode(
        node_id="lifecycle",
        kind=QuerySessionStateNodeKind.LIFECYCLE,
        label="Session lifecycle state",
        present="session_lifecycle_state" in phases,
        ok="session_lifecycle_state" in phases and event_flow_ok,
        surface=QuerySessionStateGraphSurface.LIFECYCLE,
        metadata={"event_flow_ok": event_flow_ok},
    )
    yield QuerySessionStateNode(
        node_id="blocked_terminal",
        kind=QuerySessionStateNodeKind.BLOCKED_TERMINAL,
        label="Blocked terminal control state",
        present=blocked_before_engine,
        ok=not blocked_before_engine
        or any(phase in phases for phase in ("query_cancelled", "query_interrupted", "query_stale_context_blocked", "query_entry_store_blocked")),
        surface=QuerySessionStateGraphSurface.CONTROL,
        metadata={"blocked_phases": [phase for phase in phases if phase.startswith("query_") and "blocked" in phase or phase in {"query_cancelled", "query_interrupted"}]},
    )


def build_state_edges(
    nodes: Sequence[QuerySessionStateNode],
    *,
    packet: Mapping[str, Any],
    phases: Sequence[str],
    blocked_before_engine: bool,
) -> Iterable[QuerySessionStateEdge]:
    lookup = {node.node_id: node for node in nodes}
    for source, target, kind in (
        ("session_seed", "input_accepted", QuerySessionStateEdgeKind.OWNS),
        ("input_accepted", "context_snapshot", QuerySessionStateEdgeKind.PRECEDES),
        ("context_snapshot", "control_state", QuerySessionStateEdgeKind.PRECEDES),
        ("control_state", "entry_packet", QuerySessionStateEdgeKind.GATES),
        ("entry_packet", "handoff_contract", QuerySessionStateEdgeKind.HANDS_OFF),
        ("handoff_contract", "custody", QuerySessionStateEdgeKind.OWNS),
        ("entry_packet", "query_started", QuerySessionStateEdgeKind.STARTS),
        ("query_started", "engine_stream", QuerySessionStateEdgeKind.STREAMS),
        ("engine_stream", "transcript", QuerySessionStateEdgeKind.MATERIALIZES),
        ("transcript", "lifecycle", QuerySessionStateEdgeKind.MATERIALIZES),
    ):
        source_node = lookup.get(source)
        target_node = lookup.get(target)
        present = bool(source_node and target_node and source_node.present and target_node.present)
        ok = bool(source_node and target_node and source_node.ok and target_node.ok)
        if blocked_before_engine and target in {"query_started", "engine_stream", "transcript"}:
            ok = bool(source_node and source_node.present) and not bool(target_node and target_node.present)
            present = bool(source_node and source_node.present)
        yield QuerySessionStateEdge(
            edge_id=f"{source}->{target}",
            kind=kind,
            source_node_id=source,
            target_node_id=target,
            present=present,
            ok=ok,
            metadata={
                "packet_id": packet.get("packet_id"),
                "source_present": bool(source_node and source_node.present),
                "target_present": bool(target_node and target_node.present),
                "query_started_index": phase_index(phases, "query_started"),
                "stream_index": phase_index(phases, "stream_request_start"),
            },
        )
    blocked_node = lookup.get("blocked_terminal")
    control_node = lookup.get("control_state")
    if blocked_before_engine:
        yield QuerySessionStateEdge(
            edge_id="control_state->blocked_terminal",
            kind=QuerySessionStateEdgeKind.TERMINATES,
            source_node_id="control_state",
            target_node_id="blocked_terminal",
            present=bool(control_node and blocked_node and control_node.present and blocked_node.present),
            ok=bool(control_node and blocked_node and control_node.present and blocked_node.ok),
            metadata={"blocked_before_engine": True},
        )


def validate_state_nodes(
    nodes: Sequence[QuerySessionStateNode],
    *,
    blocked_before_engine: bool,
    engine_stream_expected: bool,
) -> Iterable[QuerySessionStateGraphFinding]:
    for node in nodes:
        required = node.kind not in {QuerySessionStateNodeKind.CHECKPOINT, QuerySessionStateNodeKind.BLOCKED_TERMINAL}
        if blocked_before_engine and node.kind in {
            QuerySessionStateNodeKind.QUERY_STARTED,
            QuerySessionStateNodeKind.ENGINE_STREAM,
            QuerySessionStateNodeKind.TRANSCRIPT,
        }:
            required = False
        if not engine_stream_expected and node.kind in {QuerySessionStateNodeKind.ENGINE_STREAM, QuerySessionStateNodeKind.TRANSCRIPT}:
            required = False
        if required and not node.present:
            yield QuerySessionStateGraphFinding(
                code=f"{node.kind}_missing",
                severity=QuerySessionStateGraphSeverity.BLOCKER,
                surface=node.surface,
                message=f"Required query session state node is missing: {node.label}.",
                metadata=node.to_dict(),
            )
        elif node.present and not node.ok:
            severity = QuerySessionStateGraphSeverity.BLOCKER if required else QuerySessionStateGraphSeverity.WARNING
            yield QuerySessionStateGraphFinding(
                code=f"{node.kind}_not_ok",
                severity=severity,
                surface=node.surface,
                message=f"Query session state node is present but not ok: {node.label}.",
                metadata=node.to_dict(),
            )
        elif node.present:
            yield QuerySessionStateGraphFinding(
                code=f"{node.kind}_ready",
                severity=QuerySessionStateGraphSeverity.PASS,
                surface=node.surface,
                message=f"Query session state node is ready: {node.label}.",
                metadata={"node_id": node.node_id},
            )


def validate_state_edges(edges: Sequence[QuerySessionStateEdge]) -> Iterable[QuerySessionStateGraphFinding]:
    for edge in edges:
        if not edge.present:
            yield QuerySessionStateGraphFinding(
                code=f"{edge.edge_id.replace('->', '_to_')}_missing",
                severity=QuerySessionStateGraphSeverity.WARNING,
                surface=QuerySessionStateGraphSurface.LIFECYCLE,
                message=f"Query session state edge is not observable: {edge.edge_id}.",
                metadata=edge.to_dict(),
            )
        elif not edge.ok:
            yield QuerySessionStateGraphFinding(
                code=f"{edge.edge_id.replace('->', '_to_')}_not_ok",
                severity=QuerySessionStateGraphSeverity.BLOCKER,
                surface=QuerySessionStateGraphSurface.LIFECYCLE,
                message=f"Query session state edge is present but not valid: {edge.edge_id}.",
                metadata=edge.to_dict(),
            )
        else:
            yield QuerySessionStateGraphFinding(
                code=f"{edge.edge_id.replace('->', '_to_')}_ready",
                severity=QuerySessionStateGraphSeverity.PASS,
                surface=QuerySessionStateGraphSurface.LIFECYCLE,
                message=f"Query session state edge is valid: {edge.edge_id}.",
            )


def validate_state_phase_consistency(
    nodes: Sequence[QuerySessionStateNode],
    phases: Sequence[str],
    *,
    blocked_before_engine: bool,
) -> Iterable[QuerySessionStateGraphFinding]:
    query_started_index = phase_index(phases, "query_started")
    stream_index = phase_index(phases, "stream_request_start")
    if stream_index is not None and query_started_index is None:
        yield QuerySessionStateGraphFinding(
            code="state_graph_stream_without_query_started",
            severity=QuerySessionStateGraphSeverity.BLOCKER,
            surface=QuerySessionStateGraphSurface.QUERY_ENGINE,
            message="State graph observed stream_request_start without query_started.",
        )
    if stream_index is not None and query_started_index is not None and stream_index < query_started_index:
        yield QuerySessionStateGraphFinding(
            code="state_graph_stream_before_query_started",
            severity=QuerySessionStateGraphSeverity.BLOCKER,
            surface=QuerySessionStateGraphSurface.QUERY_ENGINE,
            message="State graph observed QueryEngine stream before query_started.",
            metadata={"query_started_index": query_started_index, "stream_index": stream_index},
        )
    if blocked_before_engine and stream_index is not None:
        yield QuerySessionStateGraphFinding(
            code="state_graph_blocked_path_streamed",
            severity=QuerySessionStateGraphSeverity.BLOCKER,
            surface=QuerySessionStateGraphSurface.CONTROL,
            message="Blocked query session path still reached QueryEngine stream.",
        )
    lifecycle_node = next((node for node in nodes if node.kind == QuerySessionStateNodeKind.LIFECYCLE), None)
    if lifecycle_node is not None and lifecycle_node.present and phase_index(phases, "session_lifecycle_state") is None:
        yield QuerySessionStateGraphFinding(
            code="state_graph_lifecycle_index_missing",
            severity=QuerySessionStateGraphSeverity.WARNING,
            surface=QuerySessionStateGraphSurface.LIFECYCLE,
            message="Lifecycle node is present but no session_lifecycle_state phase index is available.",
        )


def validate_state_identity(
    nodes: Sequence[QuerySessionStateNode],
    packet: Mapping[str, Any],
    *,
    session_id: str,
    worker_request_id: str,
) -> Iterable[QuerySessionStateGraphFinding]:
    packet_session = str(packet.get("session_id") or "")
    packet_request = str(packet.get("worker_request_id") or "")
    if packet_session and session_id and packet_session != session_id:
        yield QuerySessionStateGraphFinding(
            code="state_graph_session_id_mismatch",
            severity=QuerySessionStateGraphSeverity.BLOCKER,
            surface=QuerySessionStateGraphSurface.QUERY_ENTRY,
            message="QueryEntryPacket session_id does not match state graph session_id.",
            metadata={"packet_session_id": packet_session, "session_id": session_id},
        )
    if packet_request and worker_request_id and packet_request != worker_request_id:
        yield QuerySessionStateGraphFinding(
            code="state_graph_worker_request_id_mismatch",
            severity=QuerySessionStateGraphSeverity.BLOCKER,
            surface=QuerySessionStateGraphSurface.QUERY_ENTRY,
            message="QueryEntryPacket worker_request_id does not match state graph worker_request_id.",
            metadata={"packet_worker_request_id": packet_request, "worker_request_id": worker_request_id},
        )
    if not any(node.present for node in nodes):
        yield QuerySessionStateGraphFinding(
            code="state_graph_empty",
            severity=QuerySessionStateGraphSeverity.BLOCKER,
            surface=QuerySessionStateGraphSurface.LIFECYCLE,
            message="Query session state graph has no observable nodes.",
        )


def query_session_phases(events: Sequence[EventRecord]) -> tuple[str, ...]:
    phases: list[str] = []
    for event in events:
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        query_payload = payload.get("query_session") if isinstance(payload.get("query_session"), Mapping) else {}
        phase = str(query_payload.get("phase") or "")
        if phase:
            phases.append(phase)
    return tuple(phases)


def first_record_sequence(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for record in records:
        record_type = str(record.get("record_type") or "")
        if "." in record_type:
            record_type = record_type.rsplit(".", 1)[-1]
        sequence = _safe_int(record.get("sequence"))
        if record_type and record_type not in result:
            result[record_type] = sequence
    return result


def phase_index(phases: Sequence[str], phase: str) -> int | None:
    for index, item in enumerate(phases, start=1):
        if item == phase:
            return index
    return None


def state_graph_status_from_findings(
    findings: Sequence[QuerySessionStateGraphFinding],
) -> QuerySessionStateGraphStatus:
    if any(finding.blocking for finding in findings):
        return QuerySessionStateGraphStatus.BLOCKED
    if any(finding.severity == QuerySessionStateGraphSeverity.WARNING for finding in findings):
        return QuerySessionStateGraphStatus.DEGRADED
    return QuerySessionStateGraphStatus.READY


def query_state_graph_metadata(report: QuerySessionStateGraphReport | None) -> dict[str, str]:
    if report is None:
        return {
            "query_state_graph_ok": "false",
            "query_state_graph_status": "missing",
            "query_state_graph_blockers": "1",
        }
    return report.metadata_values()


def render_query_state_graph_markdown(report: QuerySessionStateGraphReport) -> str:
    lines = [
        "## Query Session State Graph",
        "",
        f"- report_id: `{report.report_id}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- status: `{report.status}`",
        f"- node_count: `{len(report.nodes)}`",
        f"- edge_count: `{len(report.edges)}`",
        f"- ready_node_count: `{report.ready_node_count}`",
        f"- ready_edge_count: `{report.ready_edge_count}`",
        f"- blocked_before_engine: `{str(report.blocked_before_engine).lower()}`",
        f"- engine_stream_expected: `{str(report.engine_stream_expected).lower()}`",
        f"- blocker_count: `{report.blocker_count}`",
        "",
        "### Nodes",
        "",
    ]
    lines.extend(
        f"- `{node.kind}` present=`{str(node.present).lower()}` ok=`{str(node.ok).lower()}` sequence=`{node.sequence}`"
        for node in report.nodes
    )
    lines.extend(["", "### Edges", ""])
    lines.extend(
        f"- `{edge.source_node_id}` -> `{edge.target_node_id}` `{edge.kind}` ok=`{str(edge.ok).lower()}`"
        for edge in report.edges
    )
    lines.extend(["", "### Findings", ""])
    lines.extend(
        f"- `{finding.severity}` `{finding.surface}` `{finding.code}`: {finding.message}"
        for finding in report.findings
    )
    return "\n".join(lines)


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
