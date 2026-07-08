from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable


class QuerySessionHandoffStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class QuerySessionHandoffSeverity(StrEnum):
    PASS = "pass"
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class QuerySessionHandoffSurface(StrEnum):
    PACKET = "packet"
    MESSAGES = "messages"
    CONTEXT = "context"
    CONTROL = "control"
    PERMISSION = "permission"
    TOOLS = "tools"
    CHECKPOINT = "checkpoint"
    RESUME = "resume"
    DOWNSTREAM = "downstream"
    EVENT_STREAM = "event_stream"


@dataclass(frozen=True, slots=True)
class QuerySessionHandoffPort:
    name: str
    owner_unit: str
    target_unit: str
    surface: QuerySessionHandoffSurface
    required: bool
    present: bool
    stable: bool
    value_summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return (not self.required or self.present) and self.stable

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "owner_unit": self.owner_unit,
            "target_unit": self.target_unit,
            "surface": str(self.surface),
            "required": self.required,
            "present": self.present,
            "stable": self.stable,
            "ok": self.ok,
            "value_summary": self.value_summary,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QuerySessionHandoffFinding:
    code: str
    severity: QuerySessionHandoffSeverity
    surface: QuerySessionHandoffSurface
    message: str
    port_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == QuerySessionHandoffSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "port_name": self.port_name,
            "blocking": self.blocking,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QuerySessionHandoffReport:
    report_id: str
    status: QuerySessionHandoffStatus
    session_id: str
    worker_request_id: str
    packet_id: str
    owner_unit: str
    target_unit: str
    ports: tuple[QuerySessionHandoffPort, ...]
    findings: tuple[QuerySessionHandoffFinding, ...]
    packet_schema: str
    route: str
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {QuerySessionHandoffStatus.READY, QuerySessionHandoffStatus.DEGRADED}

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == QuerySessionHandoffSeverity.WARNING)

    @property
    def first_blocker_code(self) -> str:
        for finding in self.findings:
            if finding.blocking:
                return finding.code
        return ""

    @property
    def ready_port_count(self) -> int:
        return sum(1 for port in self.ports if port.ok)

    @property
    def required_port_count(self) -> int:
        return sum(1 for port in self.ports if port.required)

    def port(self, name: str) -> QuerySessionHandoffPort | None:
        for port in self.ports:
            if port.name == name:
                return port
        return None

    def metadata_values(self) -> dict[str, str]:
        return {
            "query_handoff_report_id": self.report_id,
            "query_handoff_ok": str(self.ok).lower(),
            "query_handoff_status": str(self.status),
            "query_handoff_packet_id": self.packet_id,
            "query_handoff_owner_unit": self.owner_unit,
            "query_handoff_target_unit": self.target_unit,
            "query_handoff_packet_schema": self.packet_schema,
            "query_handoff_route": self.route,
            "query_handoff_port_count": str(len(self.ports)),
            "query_handoff_required_port_count": str(self.required_port_count),
            "query_handoff_ready_port_count": str(self.ready_port_count),
            "query_handoff_blockers": str(self.blocker_count),
            "query_handoff_warnings": str(self.warning_count),
            "query_handoff_first_blocker": self.first_blocker_code,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "status": str(self.status),
            "ok": self.ok,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "packet_id": self.packet_id,
            "owner_unit": self.owner_unit,
            "target_unit": self.target_unit,
            "packet_schema": self.packet_schema,
            "route": self.route,
            "ports": [port.to_dict() for port in self.ports],
            "findings": [finding.to_dict() for finding in self.findings],
            "blocker_count": self.blocker_count,
            "warning_count": self.warning_count,
            "first_blocker_code": self.first_blocker_code,
            "ready_port_count": self.ready_port_count,
            "required_port_count": self.required_port_count,
            "created_at": self.created_at,
            "metadata": to_jsonable(self.metadata),
        }


class QuerySessionHandoffContractRuntime:
    """Validates the 02B to 02C query-entry handoff contract."""

    def build_report(self, packet_payload: Mapping[str, Any]) -> QuerySessionHandoffReport:
        packet = _as_mapping(packet_payload)
        handoff = _as_mapping(packet.get("handoff"))
        owner_unit = str(handoff.get("owner_unit") or "M1-02B")
        target_unit = str(handoff.get("target_unit") or "M1-02C")
        ports = tuple(default_query_handoff_ports(packet, owner_unit=owner_unit, target_unit=target_unit))
        findings = [
            *validate_handoff_ports(ports),
            *validate_packet_identity(packet, handoff),
            *validate_message_payload(packet),
            *validate_context_payload(packet),
            *validate_control_payload(packet),
            *validate_downstream_claim(packet, handoff),
        ]
        status = query_handoff_status_from_findings(findings)
        return QuerySessionHandoffReport(
            report_id=new_id("qhandr"),
            status=status,
            session_id=str(packet.get("session_id") or ""),
            worker_request_id=str(packet.get("worker_request_id") or ""),
            packet_id=str(packet.get("packet_id") or ""),
            owner_unit=owner_unit,
            target_unit=target_unit,
            ports=ports,
            findings=tuple(findings),
            packet_schema=str(handoff.get("packet_schema") or ""),
            route=str(packet.get("route") or ""),
            metadata={
                "source_path": "src/query.ts",
                "target_path": "packages/runtime/zyra_runtime/claude_query_session_handoff_contract.py",
                "owner_unit": "M1-02B",
            },
        )

    def event_for_report(
        self,
        report: QuerySessionHandoffReport,
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
                    "phase": "query_handoff_contract",
                    "session_id": report.session_id,
                    "worker_request_id": report.worker_request_id,
                    "ok": report.ok,
                    "status": str(report.status),
                    "packet_id": report.packet_id,
                    "owner_unit": report.owner_unit,
                    "target_unit": report.target_unit,
                    "packet_schema": report.packet_schema,
                    "ready_port_count": report.ready_port_count,
                    "required_port_count": report.required_port_count,
                    "blocker_count": report.blocker_count,
                    "first_blocker_code": report.first_blocker_code,
                }
            },
        )


def default_query_handoff_ports(
    packet: Mapping[str, Any],
    *,
    owner_unit: str,
    target_unit: str,
) -> Iterable[QuerySessionHandoffPort]:
    messages = _as_sequence(packet.get("messages"))
    context = _as_mapping(packet.get("context"))
    tools = _as_mapping(packet.get("tools"))
    permission = _as_mapping(packet.get("permission"))
    control = _as_mapping(packet.get("control"))
    checkpoint = _as_mapping(packet.get("checkpoint"))
    resume = _as_mapping(packet.get("resume"))
    handoff = _as_mapping(packet.get("handoff"))
    yield QuerySessionHandoffPort(
        name="QueryEntryPacket.packet_id",
        owner_unit=owner_unit,
        target_unit=target_unit,
        surface=QuerySessionHandoffSurface.PACKET,
        required=True,
        present=bool(packet.get("packet_id")),
        stable=bool(packet.get("packet_id")),
        value_summary=str(packet.get("packet_id") or ""),
    )
    yield QuerySessionHandoffPort(
        name="QueryEntryPacket.messages",
        owner_unit=owner_unit,
        target_unit=target_unit,
        surface=QuerySessionHandoffSurface.MESSAGES,
        required=True,
        present=bool(messages),
        stable=all(_message_ready(message) for message in messages if isinstance(message, Mapping)),
        value_summary=str(len(messages)),
        metadata={"message_count": len(messages)},
    )
    yield QuerySessionHandoffPort(
        name="QueryEntryPacket.context",
        owner_unit=owner_unit,
        target_unit=target_unit,
        surface=QuerySessionHandoffSurface.CONTEXT,
        required=True,
        present=bool(context),
        stable=bool(context.get("fingerprint")) and context.get("ok") is not False,
        value_summary=str(context.get("fingerprint") or ""),
        metadata={"snapshot_id": context.get("snapshot_id"), "active_chars": context.get("active_chars")},
    )
    yield QuerySessionHandoffPort(
        name="QueryEntryPacket.control",
        owner_unit=owner_unit,
        target_unit=target_unit,
        surface=QuerySessionHandoffSurface.CONTROL,
        required=True,
        present=bool(control),
        stable=control.get("ok") is True and control.get("blocks_query") is not True,
        value_summary=str(control.get("status") or ""),
        metadata={"active_actions": control.get("active_actions"), "transition_count": control.get("transition_count")},
    )
    yield QuerySessionHandoffPort(
        name="QueryEntryPacket.permission",
        owner_unit=owner_unit,
        target_unit=target_unit,
        surface=QuerySessionHandoffSurface.PERMISSION,
        required=True,
        present=bool(permission),
        stable=bool(permission.get("permission_mode")) and permission.get("ok") is not False,
        value_summary=str(permission.get("permission_mode") or ""),
        metadata={"denial_count": permission.get("denial_count")},
    )
    yield QuerySessionHandoffPort(
        name="QueryEntryPacket.tools",
        owner_unit=owner_unit,
        target_unit=target_unit,
        surface=QuerySessionHandoffSurface.TOOLS,
        required=True,
        present=bool(tools),
        stable=int(tools.get("tool_count") or 0) > 0,
        value_summary=str(tools.get("tool_count") or 0),
        metadata={"planned_tool_count": tools.get("planned_tool_count")},
    )
    yield QuerySessionHandoffPort(
        name="QueryEntryPacket.checkpoint",
        owner_unit=owner_unit,
        target_unit=target_unit,
        surface=QuerySessionHandoffSurface.CHECKPOINT,
        required=False,
        present=bool(checkpoint),
        stable=checkpoint.get("ok") is not False,
        value_summary=str(checkpoint.get("status") or ""),
        metadata={"artifact_id": checkpoint.get("artifact_id")},
    )
    yield QuerySessionHandoffPort(
        name="QueryEntryPacket.resume",
        owner_unit=owner_unit,
        target_unit=target_unit,
        surface=QuerySessionHandoffSurface.RESUME,
        required=False,
        present=bool(resume),
        stable=resume.get("ok") is not False,
        value_summary=str(resume.get("requested") or False).lower(),
        metadata={"parent_uuid": resume.get("parent_uuid"), "resume_token": resume.get("resume_token")},
    )
    yield QuerySessionHandoffPort(
        name="QueryEntryPacket.handoff",
        owner_unit=owner_unit,
        target_unit=target_unit,
        surface=QuerySessionHandoffSurface.DOWNSTREAM,
        required=True,
        present=bool(handoff),
        stable=handoff.get("ok") is True and str(handoff.get("target_unit") or "") == target_unit,
        value_summary=str(handoff.get("packet_schema") or ""),
        metadata={"runtime_ports": handoff.get("runtime_ports"), "event_phases": handoff.get("event_phases")},
    )


def validate_handoff_ports(ports: Sequence[QuerySessionHandoffPort]) -> Iterable[QuerySessionHandoffFinding]:
    for port in ports:
        if not port.present and port.required:
            yield QuerySessionHandoffFinding(
                code=f"{_code_name(port.name)}_missing",
                severity=QuerySessionHandoffSeverity.BLOCKER,
                surface=port.surface,
                port_name=port.name,
                message=f"Required query handoff port is missing: {port.name}.",
                metadata=port.to_dict(),
            )
        elif not port.stable and port.required:
            yield QuerySessionHandoffFinding(
                code=f"{_code_name(port.name)}_unstable",
                severity=QuerySessionHandoffSeverity.BLOCKER,
                surface=port.surface,
                port_name=port.name,
                message=f"Required query handoff port is present but not stable: {port.name}.",
                metadata=port.to_dict(),
            )
        elif port.present and not port.stable:
            yield QuerySessionHandoffFinding(
                code=f"{_code_name(port.name)}_degraded",
                severity=QuerySessionHandoffSeverity.WARNING,
                surface=port.surface,
                port_name=port.name,
                message=f"Optional query handoff port is degraded: {port.name}.",
                metadata=port.to_dict(),
            )
        else:
            yield QuerySessionHandoffFinding(
                code=f"{_code_name(port.name)}_ready",
                severity=QuerySessionHandoffSeverity.PASS,
                surface=port.surface,
                port_name=port.name,
                message=f"Query handoff port is ready: {port.name}.",
                metadata=port.to_dict(),
            )


def validate_packet_identity(
    packet: Mapping[str, Any],
    handoff: Mapping[str, Any],
) -> Iterable[QuerySessionHandoffFinding]:
    if not packet.get("session_id") or not packet.get("worker_request_id"):
        yield QuerySessionHandoffFinding(
            code="query_handoff_identity_missing",
            severity=QuerySessionHandoffSeverity.BLOCKER,
            surface=QuerySessionHandoffSurface.PACKET,
            message="Query entry packet lacks session_id or worker_request_id.",
        )
    else:
        yield QuerySessionHandoffFinding(
            code="query_handoff_identity_ready",
            severity=QuerySessionHandoffSeverity.PASS,
            surface=QuerySessionHandoffSurface.PACKET,
            message="Query entry packet identity is ready for downstream handoff.",
        )
    if str(handoff.get("owner_unit") or "") != "M1-02B":
        yield QuerySessionHandoffFinding(
            code="query_handoff_owner_unit_mismatch",
            severity=QuerySessionHandoffSeverity.BLOCKER,
            surface=QuerySessionHandoffSurface.DOWNSTREAM,
            message="Query entry handoff owner unit is not M1-02B.",
            metadata={"owner_unit": handoff.get("owner_unit")},
        )
    if str(handoff.get("target_unit") or "") != "M1-02C":
        yield QuerySessionHandoffFinding(
            code="query_handoff_target_unit_mismatch",
            severity=QuerySessionHandoffSeverity.BLOCKER,
            surface=QuerySessionHandoffSurface.DOWNSTREAM,
            message="Query entry handoff target unit is not M1-02C.",
            metadata={"target_unit": handoff.get("target_unit")},
        )


def validate_message_payload(packet: Mapping[str, Any]) -> Iterable[QuerySessionHandoffFinding]:
    messages = _as_sequence(packet.get("messages"))
    if not messages:
        yield QuerySessionHandoffFinding(
            code="query_handoff_messages_empty",
            severity=QuerySessionHandoffSeverity.BLOCKER,
            surface=QuerySessionHandoffSurface.MESSAGES,
            message="Query entry handoff has no messages.",
        )
        return
    missing_content = [
        index
        for index, message in enumerate(messages, start=1)
        if isinstance(message, Mapping) and not str(message.get("content") or "")
    ]
    if missing_content:
        yield QuerySessionHandoffFinding(
            code="query_handoff_message_content_missing",
            severity=QuerySessionHandoffSeverity.BLOCKER,
            surface=QuerySessionHandoffSurface.MESSAGES,
            message="Query entry handoff contains messages without content.",
            metadata={"indices": missing_content[:16]},
        )
    parents = {str(message.get("parent_uuid") or "") for message in messages if isinstance(message, Mapping)}
    if not any(parents):
        yield QuerySessionHandoffFinding(
            code="query_handoff_parent_uuid_missing",
            severity=QuerySessionHandoffSeverity.WARNING,
            surface=QuerySessionHandoffSurface.MESSAGES,
            message="Messages do not expose parent_uuid; resume may be degraded.",
        )
    else:
        yield QuerySessionHandoffFinding(
            code="query_handoff_messages_ready",
            severity=QuerySessionHandoffSeverity.PASS,
            surface=QuerySessionHandoffSurface.MESSAGES,
            message="Query entry messages are ready for downstream tool loop.",
            metadata={"message_count": len(messages), "parent_uuid_count": len([value for value in parents if value])},
        )


def validate_context_payload(packet: Mapping[str, Any]) -> Iterable[QuerySessionHandoffFinding]:
    context = _as_mapping(packet.get("context"))
    if context.get("ok") is False:
        yield QuerySessionHandoffFinding(
            code="query_handoff_context_not_ok",
            severity=QuerySessionHandoffSeverity.BLOCKER,
            surface=QuerySessionHandoffSurface.CONTEXT,
            message="Context envelope is not ok.",
            metadata=context,
        )
    if not context.get("fingerprint"):
        yield QuerySessionHandoffFinding(
            code="query_handoff_context_fingerprint_missing",
            severity=QuerySessionHandoffSeverity.BLOCKER,
            surface=QuerySessionHandoffSurface.CONTEXT,
            message="Context envelope does not expose a fingerprint.",
            metadata=context,
        )
    if int(context.get("message_count") or 0) == 0:
        yield QuerySessionHandoffFinding(
            code="query_handoff_context_messages_missing",
            severity=QuerySessionHandoffSeverity.WARNING,
            surface=QuerySessionHandoffSurface.CONTEXT,
            message="Context envelope has no derived messages.",
            metadata=context,
        )


def validate_control_payload(packet: Mapping[str, Any]) -> Iterable[QuerySessionHandoffFinding]:
    control = _as_mapping(packet.get("control"))
    if control.get("blocks_query") is True:
        yield QuerySessionHandoffFinding(
            code="query_handoff_control_blocks_query",
            severity=QuerySessionHandoffSeverity.BLOCKER,
            surface=QuerySessionHandoffSurface.CONTROL,
            message="Control state blocks query entry and cannot be handed to 02C as runnable.",
            metadata=control,
        )
    elif control.get("ok") is True:
        yield QuerySessionHandoffFinding(
            code="query_handoff_control_ready",
            severity=QuerySessionHandoffSeverity.PASS,
            surface=QuerySessionHandoffSurface.CONTROL,
            message="Control state permits downstream handoff.",
            metadata=control,
        )
    else:
        yield QuerySessionHandoffFinding(
            code="query_handoff_control_not_ready",
            severity=QuerySessionHandoffSeverity.BLOCKER,
            surface=QuerySessionHandoffSurface.CONTROL,
            message="Control state is not ready.",
            metadata=control,
        )


def validate_downstream_claim(
    packet: Mapping[str, Any],
    handoff: Mapping[str, Any],
) -> Iterable[QuerySessionHandoffFinding]:
    runtime_ports = tuple(str(item) for item in _as_sequence(handoff.get("runtime_ports")) if item)
    event_phases = tuple(str(item) for item in _as_sequence(handoff.get("event_phases")) if item)
    required_ports = {
        "QueryEntryPacket.messages",
        "QueryEntryPacket.context",
        "QueryEntryPacket.permission",
        "QueryEntryPacket.tools",
        "QueryEntryPacket.control",
    }
    missing_ports = sorted(required_ports.difference(runtime_ports))
    if missing_ports:
        yield QuerySessionHandoffFinding(
            code="query_handoff_runtime_ports_missing",
            severity=QuerySessionHandoffSeverity.BLOCKER,
            surface=QuerySessionHandoffSurface.DOWNSTREAM,
            message="Handoff claim omits required runtime ports for 02C.",
            metadata={"missing_ports": missing_ports, "runtime_ports": runtime_ports},
        )
    required_phases = {"query_entry_packet_ready", "query_started", "query_downstream_handoff_ready"}
    missing_phases = sorted(required_phases.difference(event_phases))
    if missing_phases and packet.get("ok") is True:
        yield QuerySessionHandoffFinding(
            code="query_handoff_event_phases_missing",
            severity=QuerySessionHandoffSeverity.BLOCKER,
            surface=QuerySessionHandoffSurface.EVENT_STREAM,
            message="Handoff claim omits required event phases for 02C.",
            metadata={"missing_phases": missing_phases, "event_phases": event_phases},
        )
    if not missing_ports and not missing_phases:
        yield QuerySessionHandoffFinding(
            code="query_handoff_downstream_claim_ready",
            severity=QuerySessionHandoffSeverity.PASS,
            surface=QuerySessionHandoffSurface.DOWNSTREAM,
            message="02C handoff claim exposes required ports and event phases.",
        )


def query_handoff_status_from_findings(
    findings: Sequence[QuerySessionHandoffFinding],
) -> QuerySessionHandoffStatus:
    if any(finding.blocking for finding in findings):
        return QuerySessionHandoffStatus.BLOCKED
    if any(finding.severity == QuerySessionHandoffSeverity.WARNING for finding in findings):
        return QuerySessionHandoffStatus.DEGRADED
    return QuerySessionHandoffStatus.READY


def query_handoff_metadata(report: QuerySessionHandoffReport | None) -> dict[str, str]:
    if report is None:
        return {
            "query_handoff_ok": "false",
            "query_handoff_status": "missing",
            "query_handoff_blockers": "1",
        }
    return report.metadata_values()


def render_query_handoff_markdown(report: QuerySessionHandoffReport) -> str:
    lines = [
        "## Query Session Handoff Contract",
        "",
        f"- report_id: `{report.report_id}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- status: `{report.status}`",
        f"- packet_id: `{report.packet_id}`",
        f"- owner_unit: `{report.owner_unit}`",
        f"- target_unit: `{report.target_unit}`",
        f"- packet_schema: `{report.packet_schema}`",
        f"- ready_ports: `{report.ready_port_count}/{len(report.ports)}`",
        f"- blocker_count: `{report.blocker_count}`",
        "",
        "### Ports",
        "",
    ]
    lines.extend(
        f"- `{port.name}` `{port.surface}` required=`{str(port.required).lower()}` ok=`{str(port.ok).lower()}`"
        for port in report.ports
    )
    lines.extend(["", "### Findings", ""])
    lines.extend(
        f"- `{finding.severity}` `{finding.surface}` `{finding.code}`: {finding.message}"
        for finding in report.findings
    )
    return "\n".join(lines)


def _message_ready(message: Mapping[str, Any]) -> bool:
    return bool(message.get("role")) and bool(message.get("content")) and bool(message.get("source"))


def _code_name(value: str) -> str:
    return (
        value.replace("QueryEntryPacket.", "")
        .replace(".", "_")
        .replace("-", "_")
        .replace(" ", "_")
        .lower()
    )


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()
