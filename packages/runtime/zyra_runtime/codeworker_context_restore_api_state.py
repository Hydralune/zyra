from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable


M1_02D_CONTEXT_RESTORE_API_STATE_OWNER_UNIT = "M1-02D"
CODEWORKER_CONTEXT_RESTORE_API_STATE_RUNTIME_ID = "codeworker_context_restore_api_state_runtime"


class ContextRestoreApiStateStatus(StrEnum):
    READY = "ready"
    EMPTY = "empty"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    DISABLED = "disabled"


class ContextRestoreApiStateSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ContextRestoreApiStateSurface(StrEnum):
    COMPACT_CONTRACT = "compact_contract"
    RESTORE_APPLICATION = "restore_application"
    CONTEXT_SECURITY = "context_security"
    MODEL_ENVELOPE = "model_envelope"
    API_PROJECTION = "api_projection"
    EVENT_LOG = "event_log"


class ContextRestoreCustodyKind(StrEnum):
    COMPACT_REPORT = "compact_report"
    RESTORE_CONTRACT = "restore_contract"
    RESTORE_APPLICATION = "restore_application"
    SECURITY_SNAPSHOT = "security_snapshot"
    MODEL_ENVELOPE = "model_envelope"
    API_PROJECTION = "api_projection"
    SESSION_EVENT = "session_event"


class ContextRestoreCustodyEdgeKind(StrEnum):
    PRODUCES = "produces"
    APPLIES = "applies"
    CLASSIFIES = "classifies"
    INJECTS = "injects"
    PROJECTS = "projects"
    OBSERVES = "observes"


@dataclass(frozen=True, slots=True)
class ContextRestoreApiStateFinding:
    code: str
    severity: ContextRestoreApiStateSeverity
    surface: ContextRestoreApiStateSurface
    message: str
    node_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ContextRestoreApiStateSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "node_id": self.node_id,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ContextRestoreCustodyNode:
    node_id: str
    kind: ContextRestoreCustodyKind
    label: str
    present: bool
    external: bool = False
    trust_level: str = ""
    source_provenance: str = ""
    secret_redaction_state: str = ""
    segment_count: int = 0
    model_message_count: int = 0
    event_phase: str = ""
    event_id: str = ""
    artifact_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def has_security_metadata(self) -> bool:
        if self.trust_level or self.source_provenance or self.secret_redaction_state:
            return True
        return any(
            str(self.metadata.get(key) or "")
            for key in ("trust_level", "source_provenance", "secret_redaction_state")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": str(self.kind),
            "label": self.label,
            "present": self.present,
            "external": self.external,
            "trust_level": self.trust_level,
            "source_provenance": self.source_provenance,
            "secret_redaction_state": self.secret_redaction_state,
            "segment_count": self.segment_count,
            "model_message_count": self.model_message_count,
            "event_phase": self.event_phase,
            "event_id": self.event_id,
            "artifact_id": self.artifact_id,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ContextRestoreCustodyEdge:
    edge_id: str
    kind: ContextRestoreCustodyEdgeKind
    source_node_id: str
    target_node_id: str
    label: str
    present: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def connected(self) -> bool:
        return self.present and bool(self.source_node_id and self.target_node_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "kind": str(self.kind),
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "label": self.label,
            "present": self.present,
            "connected": self.connected,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ContextRestoreIntegrityCheck:
    check_id: str
    surface: ContextRestoreApiStateSurface
    label: str
    passed: bool
    severity: ContextRestoreApiStateSeverity = ContextRestoreApiStateSeverity.ERROR
    details: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return not self.passed and self.severity == ContextRestoreApiStateSeverity.BLOCKER

    def finding(self) -> ContextRestoreApiStateFinding | None:
        if self.passed:
            return None
        return ContextRestoreApiStateFinding(
            code=f"CONTEXT_RESTORE_API_STATE_{self.check_id.upper()}",
            severity=self.severity,
            surface=self.surface,
            message=self.details or self.label,
            metadata={str(k): str(v) for k, v in self.metadata.items()},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "surface": str(self.surface),
            "label": self.label,
            "passed": self.passed,
            "severity": str(self.severity),
            "blocking": self.blocking,
            "details": self.details,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ContextRestoreApiStateReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    nodes: tuple[ContextRestoreCustodyNode, ...]
    edges: tuple[ContextRestoreCustodyEdge, ...]
    checks: tuple[ContextRestoreIntegrityCheck, ...]
    findings: tuple[ContextRestoreApiStateFinding, ...]
    phase_counts: dict[str, int]
    source_decisions: tuple[dict[str, str], ...]
    disabled: bool = False
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not self.disabled and not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ContextRestoreApiStateStatus:
        if self.disabled:
            return ContextRestoreApiStateStatus.DISABLED
        if any(finding.blocking for finding in self.findings):
            return ContextRestoreApiStateStatus.BLOCKED
        if not self.nodes or not any(node.present for node in self.nodes):
            return ContextRestoreApiStateStatus.EMPTY
        if self.findings or any(not check.passed for check in self.checks):
            return ContextRestoreApiStateStatus.DEGRADED
        return ContextRestoreApiStateStatus.READY

    @property
    def present_node_count(self) -> int:
        return sum(1 for node in self.nodes if node.present)

    @property
    def connected_edge_count(self) -> int:
        return sum(1 for edge in self.edges if edge.connected)

    @property
    def blocking_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def degraded_check_count(self) -> int:
        return sum(1 for check in self.checks if not check.passed)

    @property
    def node_kinds_present(self) -> tuple[str, ...]:
        return tuple(str(node.kind) for node in self.nodes if node.present)

    @property
    def missing_required_surfaces(self) -> tuple[str, ...]:
        surfaces: list[str] = []
        for check in self.checks:
            if not check.passed:
                surfaces.append(str(check.surface))
        return tuple(dict.fromkeys(surfaces))

    @property
    def contract_node(self) -> ContextRestoreCustodyNode | None:
        return _first_node(self.nodes, ContextRestoreCustodyKind.RESTORE_CONTRACT)

    @property
    def restore_application_node(self) -> ContextRestoreCustodyNode | None:
        return _first_node(self.nodes, ContextRestoreCustodyKind.RESTORE_APPLICATION)

    @property
    def model_envelope_node(self) -> ContextRestoreCustodyNode | None:
        return _first_node(self.nodes, ContextRestoreCustodyKind.MODEL_ENVELOPE)

    @property
    def api_projection_node(self) -> ContextRestoreCustodyNode | None:
        return _first_node(self.nodes, ContextRestoreCustodyKind.API_PROJECTION)

    @property
    def restore_path_connected(self) -> bool:
        required = {
            ContextRestoreCustodyEdgeKind.PRODUCES,
            ContextRestoreCustodyEdgeKind.APPLIES,
            ContextRestoreCustodyEdgeKind.INJECTS,
        }
        projection = self.api_projection_node
        if projection is not None and projection.present:
            required.add(ContextRestoreCustodyEdgeKind.PROJECTS)
        connected = {edge.kind for edge in self.edges if edge.connected}
        return required.issubset(connected)

    def summary_lines(self) -> tuple[str, ...]:
        contract = self.contract_node
        application = self.restore_application_node
        model = self.model_envelope_node
        projection = self.api_projection_node
        lines = [
            f"status={self.status} ok={str(self.ok).lower()} restore_path_connected={str(self.restore_path_connected).lower()}",
            f"nodes={self.present_node_count}/{len(self.nodes)} edges={self.connected_edge_count}/{len(self.edges)} checks_failed={self.degraded_check_count}",
            f"contract_segments={contract.segment_count if contract else 0} application_messages={application.model_message_count if application else 0} model_restore_messages={model.model_message_count if model else 0}",
            f"api_projection_present={str(bool(projection and projection.present)).lower()} missing_surfaces={','.join(self.missing_required_surfaces)}",
        ]
        return tuple(lines)

    def api_state(self) -> dict[str, Any]:
        contract = self.contract_node
        application = self.restore_application_node
        model = self.model_envelope_node
        projection = self.api_projection_node
        return {
            "status": str(self.status),
            "ok": self.ok,
            "restore_path_connected": self.restore_path_connected,
            "contract": contract.to_dict() if contract else None,
            "application": application.to_dict() if application else None,
            "model_envelope": model.to_dict() if model else None,
            "api_projection": projection.to_dict() if projection else None,
            "missing_required_surfaces": list(self.missing_required_surfaces),
            "summary": list(self.summary_lines()),
        }

    def metadata(self) -> dict[str, str]:
        contract = self.contract_node
        application = self.restore_application_node
        model = self.model_envelope_node
        projection = self.api_projection_node
        return {
            "context_restore_api_state_report_id": self.report_id,
            "context_restore_api_state_owner_unit": self.owner_unit,
            "context_restore_api_state_runtime_id": self.runtime_id,
            "context_restore_api_state_ok": str(self.ok).lower(),
            "context_restore_api_state_status": str(self.status),
            "context_restore_api_state_disabled": str(self.disabled).lower(),
            "context_restore_api_state_nodes": str(len(self.nodes)),
            "context_restore_api_state_present_nodes": str(self.present_node_count),
            "context_restore_api_state_edges": str(len(self.edges)),
            "context_restore_api_state_connected_edges": str(self.connected_edge_count),
            "context_restore_api_state_checks": str(len(self.checks)),
            "context_restore_api_state_degraded_checks": str(self.degraded_check_count),
            "context_restore_api_state_blocking_count": str(self.blocking_count),
            "context_restore_api_state_restore_path_connected": str(self.restore_path_connected).lower(),
            "context_restore_api_state_contract_segments": str(contract.segment_count if contract else 0),
            "context_restore_api_state_application_messages": str(
                application.model_message_count if application else 0
            ),
            "context_restore_api_state_model_restore_messages": str(model.model_message_count if model else 0),
            "context_restore_api_state_projection_present": str(bool(projection and projection.present)).lower(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.context_restore_api_state.v1",
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
            "checks": [check.to_dict() for check in self.checks],
            "findings": [finding.to_dict() for finding in self.findings],
            "phase_counts": dict(self.phase_counts),
            "api_state": self.api_state(),
            "summary": list(self.summary_lines()),
            "source_decisions": [dict(item) for item in self.source_decisions],
            "restore_path_connected": self.restore_path_connected,
            "metadata": self.metadata(),
            "created_at": self.created_at,
        }


class CodeWorkerContextRestoreApiStateRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_CONTEXT_RESTORE_API_STATE_OWNER_UNIT,
        runtime_id: str = CODEWORKER_CONTEXT_RESTORE_API_STATE_RUNTIME_ID,
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
        compact_restore_report: Any,
        restore_integration_report: Any,
        context_security_snapshot: Any,
        model_stream_reports: Sequence[Any],
        event_records: Sequence[Any],
        projection_payload: Mapping[str, Any] | None = None,
    ) -> ContextRestoreApiStateReport:
        compact_map = _as_mapping(to_jsonable(compact_restore_report))
        integration_map = _as_mapping(to_jsonable(restore_integration_report))
        security_map = _as_mapping(to_jsonable(context_security_snapshot))
        model_maps = [_as_mapping(to_jsonable(report)) for report in model_stream_reports]
        projection_map = projection_payload if isinstance(projection_payload, Mapping) else {}
        phase_counts = _phase_counts(event_records)
        nodes = tuple(
            node
            for node in (
                self._compact_report_node(compact_map),
                self._restore_contract_node(compact_map),
                self._restore_application_node(integration_map),
                self._security_snapshot_node(security_map, integration_map),
                self._model_envelope_node(model_maps),
                self._api_projection_node(projection_map),
                *self._event_phase_nodes(event_records),
            )
            if node is not None
        )
        edges = tuple(self._edges(nodes))
        checks = tuple(
            self._checks(
                nodes=nodes,
                edges=edges,
                phase_counts=phase_counts,
                compact_map=compact_map,
                integration_map=integration_map,
                model_maps=model_maps,
                projection_map=projection_map,
            )
        )
        findings = [finding for check in checks if (finding := check.finding()) is not None]
        if self.disabled:
            findings.append(
                ContextRestoreApiStateFinding(
                    code="CONTEXT_RESTORE_API_STATE_RUNTIME_DISABLED",
                    severity=ContextRestoreApiStateSeverity.BLOCKER,
                    surface=ContextRestoreApiStateSurface.API_PROJECTION,
                    message="Context restore API state runtime is disabled.",
                )
            )
        return ContextRestoreApiStateReport(
            report_id=new_id("context_restore_api_state"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            nodes=nodes,
            edges=edges,
            checks=checks,
            findings=tuple(findings),
            phase_counts=phase_counts,
            source_decisions=default_context_restore_api_state_source_decisions(),
            disabled=self.disabled,
        )

    def event_for_report(
        self,
        report: ContextRestoreApiStateReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        phase: str = "codeworker_context_restore_api_state",
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
                    "context_restore_api_state": report.to_dict(),
                }
            },
        )

    def metadata(self, report: ContextRestoreApiStateReport | None = None) -> dict[str, str]:
        if report is None:
            return {
                "context_restore_api_state_ok": str(not self.disabled).lower(),
                "context_restore_api_state_status": str(
                    ContextRestoreApiStateStatus.DISABLED if self.disabled else ContextRestoreApiStateStatus.READY
                ),
                "context_restore_api_state_runtime_id": self.runtime_id,
            }
        return report.metadata()

    def _compact_report_node(self, compact_map: Mapping[str, Any]) -> ContextRestoreCustodyNode:
        boundary = _as_mapping(compact_map.get("boundary"))
        return ContextRestoreCustodyNode(
            node_id="compact_report",
            kind=ContextRestoreCustodyKind.COMPACT_REPORT,
            label="Compact restore report",
            present=bool(compact_map),
            segment_count=len(_as_list(compact_map.get("preserved_segments"))),
            event_phase="compact_restore_report",
            artifact_id=str(boundary.get("artifact_id") or ""),
            metadata={
                "report_id": compact_map.get("report_id", ""),
                "status": compact_map.get("status", ""),
                "ok": compact_map.get("ok", ""),
                "compact_needed": compact_map.get("compact_needed", ""),
            },
        )

    def _restore_contract_node(self, compact_map: Mapping[str, Any]) -> ContextRestoreCustodyNode:
        contract = _as_mapping(compact_map.get("restore_contract"))
        segments = _as_list(contract.get("restore_segments"))
        segment_security = _segment_security_summary(segments)
        return ContextRestoreCustodyNode(
            node_id="restore_contract",
            kind=ContextRestoreCustodyKind.RESTORE_CONTRACT,
            label="Next turn restore contract",
            present=bool(contract),
            external=segment_security["external_count"] > 0,
            trust_level=segment_security["trust_levels"],
            source_provenance=segment_security["source_provenance"],
            secret_redaction_state=segment_security["redaction_states"],
            segment_count=len(segments),
            event_phase="next_turn_restore_contract",
            artifact_id=str(contract.get("compact_artifact_id") or ""),
            metadata={
                "contract_id": contract.get("contract_id", ""),
                "restore_segment_count": contract.get("restore_segment_count", len(segments)),
                "has_mcp_segments": segment_security["external_count"] > 0,
                "retrieval_scopes": segment_security["retrieval_scopes"],
                "code_index_sources": segment_security["code_index_sources"],
            },
        )

    def _restore_application_node(self, integration_map: Mapping[str, Any]) -> ContextRestoreCustodyNode:
        application = _latest_mapping(_as_list(integration_map.get("applications")))
        security = _as_mapping(application.get("security_snapshot"))
        model_message_count = _safe_int(application.get("model_message_count"))
        if model_message_count == 0:
            model_message_count = len(_as_list(application.get("model_messages"))) or len(
                _as_list(application.get("messages"))
            )
        segment_count = _safe_int(application.get("segment_count"))
        if segment_count == 0:
            segment_count = len(_as_list(application.get("messages"))) or len(_as_list(application.get("blocks")))
        return ContextRestoreCustodyNode(
            node_id="restore_application",
            kind=ContextRestoreCustodyKind.RESTORE_APPLICATION,
            label="Applied restore contract",
            present=bool(application),
            external=_truthy(application.get("external_untrusted_messages")) or _truthy(security.get("external")),
            trust_level=str(_security_snapshot_summary(security).get("trust_levels") or application.get("trust_level") or ""),
            source_provenance=str(application.get("source_provenance") or ""),
            secret_redaction_state=str(
                _security_snapshot_summary(security).get("redaction_states")
                or application.get("secret_redaction_state")
                or ""
            ),
            segment_count=segment_count,
            model_message_count=model_message_count,
            event_phase="codeworker_restore_context_applied",
            metadata={
                "application_id": application.get("application_id", ""),
                "contract_id": application.get("contract_id", ""),
                "ok": application.get("ok", ""),
                "status": application.get("status", ""),
                "context_block_count": application.get("context_block_count", ""),
                "redacted_messages": application.get("redacted_messages", ""),
                "untrusted_messages": application.get("untrusted_messages", ""),
            },
        )

    def _security_snapshot_node(
        self,
        security_map: Mapping[str, Any],
        integration_map: Mapping[str, Any],
    ) -> ContextRestoreCustodyNode:
        application = _latest_mapping(_as_list(integration_map.get("applications")))
        embedded_security = _as_mapping(application.get("security_snapshot"))
        source = security_map if security_map else embedded_security
        summary = _security_snapshot_summary(source)
        return ContextRestoreCustodyNode(
            node_id="context_security_snapshot",
            kind=ContextRestoreCustodyKind.SECURITY_SNAPSHOT,
            label="Context security snapshot",
            present=bool(source),
            external=_truthy(source.get("external_untrusted")) or _truthy(source.get("external")),
            trust_level=str(source.get("trust_levels") or source.get("trust_level") or summary.get("trust_levels") or ""),
            source_provenance=str(source.get("source_provenance") or summary.get("source_provenance") or ""),
            secret_redaction_state=str(
                source.get("redaction_states")
                or source.get("secret_redaction_state")
                or summary.get("redaction_states")
                or ""
            ),
            segment_count=_safe_int(source.get("context_count") or source.get("segment_count")),
            event_phase="codeworker_restore_context_security",
            metadata={
                "snapshot_id": source.get("snapshot_id", ""),
                "ok": source.get("ok", ""),
                "status": source.get("status", ""),
                "verdicts": source.get("verdicts", ""),
                "redactions": source.get("redactions", ""),
                "untrusted": source.get("untrusted", ""),
            },
        )

    def _model_envelope_node(self, model_maps: Sequence[Mapping[str, Any]]) -> ContextRestoreCustodyNode:
        envelope = {}
        restore_message_count = 0
        latest_stream = model_maps[-1] if model_maps else {}
        for model_map in model_maps:
            candidate = _as_mapping(model_map.get("envelope"))
            metadata = _as_mapping(candidate.get("metadata"))
            count = _safe_int(metadata.get("restore_model_message_count"))
            if count > 0:
                envelope = candidate
                restore_message_count = count
        if not envelope:
            envelope = _as_mapping(latest_stream.get("envelope"))
            restore_message_count = _safe_int(_as_mapping(envelope.get("metadata")).get("restore_model_message_count"))
        metadata = _as_mapping(envelope.get("metadata"))
        return ContextRestoreCustodyNode(
            node_id="model_envelope",
            kind=ContextRestoreCustodyKind.MODEL_ENVELOPE,
            label="Model request envelope",
            present=bool(envelope),
            model_message_count=restore_message_count,
            event_phase="model_stream_report",
            metadata={
                "model": envelope.get("model", ""),
                "turn_index": envelope.get("turn_index", ""),
                "restore_application_id": metadata.get("restore_application_id", ""),
                "restore_contract_id": metadata.get("restore_contract_id", ""),
                "restore_context_block_count": metadata.get("restore_context_block_count", ""),
                "stream_status": latest_stream.get("status", ""),
                "stream_ok": latest_stream.get("ok", ""),
            },
        )

    def _api_projection_node(self, projection_map: Mapping[str, Any]) -> ContextRestoreCustodyNode:
        restore_state = _as_mapping(projection_map.get("restore_state"))
        compact_state = _as_mapping(projection_map.get("compact_state"))
        session = _as_mapping(projection_map.get("session"))
        return ContextRestoreCustodyNode(
            node_id="api_projection",
            kind=ContextRestoreCustodyKind.API_PROJECTION,
            label="Task CodeWorker API projection",
            present=bool(projection_map),
            segment_count=_safe_int(compact_state.get("restore_segment_count")),
            model_message_count=_safe_int(restore_state.get("model_message_count")),
            event_phase="codeworker_task_api_projection",
            metadata={
                "projection_id": projection_map.get("projection_id", ""),
                "task_id": projection_map.get("task_id", ""),
                "session_id": session.get("session_id", ""),
                "restore_applied": restore_state.get("applied", ""),
                "restore_status": restore_state.get("status", ""),
                "compact_state_status": compact_state.get("status", ""),
            },
        )

    def _event_phase_nodes(self, event_records: Sequence[Any]) -> tuple[ContextRestoreCustodyNode, ...]:
        tracked = {
            "compact_restore_report",
            "compact_restore_contract_pending",
            "next_turn_restore_contract",
            "codeworker_restore_context_applied",
            "codeworker_restore_context_security",
            "model_stream_report",
            "codeworker_task_api_projection",
        }
        nodes: list[ContextRestoreCustodyNode] = []
        seen: set[str] = set()
        for event in event_records:
            event_map = _event_view(event)
            query_session = _as_mapping(_as_mapping(event_map.get("payload")).get("query_session"))
            phase = str(query_session.get("phase") or "")
            if not phase or phase not in tracked or phase in seen:
                continue
            seen.add(phase)
            nodes.append(
                ContextRestoreCustodyNode(
                    node_id=f"event_{phase}",
                    kind=ContextRestoreCustodyKind.SESSION_EVENT,
                    label=f"Observed phase {phase}",
                    present=True,
                    event_phase=phase,
                    event_id=str(event_map.get("event_id") or ""),
                    metadata={
                        "phase": phase,
                        "event_type": event_map.get("event_type", ""),
                        "created_at": event_map.get("created_at", ""),
                    },
                )
            )
        return tuple(nodes)

    def _edges(self, nodes: Sequence[ContextRestoreCustodyNode]) -> tuple[ContextRestoreCustodyEdge, ...]:
        node_ids = {node.node_id for node in nodes if node.present}
        return (
            _edge("compact_report", "restore_contract", ContextRestoreCustodyEdgeKind.PRODUCES, node_ids),
            _edge("restore_contract", "restore_application", ContextRestoreCustodyEdgeKind.APPLIES, node_ids),
            _edge(
                "restore_application",
                "context_security_snapshot",
                ContextRestoreCustodyEdgeKind.CLASSIFIES,
                node_ids,
            ),
            _edge("restore_application", "model_envelope", ContextRestoreCustodyEdgeKind.INJECTS, node_ids),
            _edge("model_envelope", "api_projection", ContextRestoreCustodyEdgeKind.PROJECTS, node_ids),
            _edge("event_compact_restore_report", "compact_report", ContextRestoreCustodyEdgeKind.OBSERVES, node_ids),
            _edge(
                "event_codeworker_restore_context_applied",
                "restore_application",
                ContextRestoreCustodyEdgeKind.OBSERVES,
                node_ids,
            ),
            _edge("event_model_stream_report", "model_envelope", ContextRestoreCustodyEdgeKind.OBSERVES, node_ids),
        )

    def _checks(
        self,
        *,
        nodes: Sequence[ContextRestoreCustodyNode],
        edges: Sequence[ContextRestoreCustodyEdge],
        phase_counts: Mapping[str, int],
        compact_map: Mapping[str, Any],
        integration_map: Mapping[str, Any],
        model_maps: Sequence[Mapping[str, Any]],
        projection_map: Mapping[str, Any],
    ) -> tuple[ContextRestoreIntegrityCheck, ...]:
        contract = _first_node(nodes, ContextRestoreCustodyKind.RESTORE_CONTRACT)
        application = _first_node(nodes, ContextRestoreCustodyKind.RESTORE_APPLICATION)
        security = _first_node(nodes, ContextRestoreCustodyKind.SECURITY_SNAPSHOT)
        model = _first_node(nodes, ContextRestoreCustodyKind.MODEL_ENVELOPE)
        projection = _first_node(nodes, ContextRestoreCustodyKind.API_PROJECTION)
        has_contract = bool(contract and contract.present)
        has_application = bool(application and application.present)
        has_model_restore_messages = bool(model and model.present and model.model_message_count > 0)
        required_edge_kinds = {
            ContextRestoreCustodyEdgeKind.PRODUCES,
            ContextRestoreCustodyEdgeKind.APPLIES,
            ContextRestoreCustodyEdgeKind.INJECTS,
        }
        if projection is not None and projection.present:
            required_edge_kinds.add(ContextRestoreCustodyEdgeKind.PROJECTS)
        path_connected = {
            edge.kind: edge.connected
            for edge in edges
            if edge.kind in required_edge_kinds
        }
        return (
            ContextRestoreIntegrityCheck(
                check_id="contract_segments_present",
                surface=ContextRestoreApiStateSurface.COMPACT_CONTRACT,
                label="Compact restore report produced a next-turn restore contract with segments.",
                passed=has_contract and bool(contract and contract.segment_count > 0),
                severity=ContextRestoreApiStateSeverity.BLOCKER,
                details="No restore contract segments were available for next-turn application.",
                metadata={"compact_report_id": compact_map.get("report_id", "")},
            ),
            ContextRestoreIntegrityCheck(
                check_id="application_matches_contract",
                surface=ContextRestoreApiStateSurface.RESTORE_APPLICATION,
                label="Restore integration applied the pending contract.",
                passed=has_application and bool(application and application.model_message_count > 0),
                severity=ContextRestoreApiStateSeverity.BLOCKER,
                details="Restore integration did not apply the contract into context blocks and model messages.",
                metadata={"restore_integration_report_id": integration_map.get("report_id", "")},
            ),
            ContextRestoreIntegrityCheck(
                check_id="security_metadata_present",
                surface=ContextRestoreApiStateSurface.CONTEXT_SECURITY,
                label="Restored context carries provenance, trust and redaction metadata.",
                passed=bool(security and security.present and security.has_security_metadata),
                severity=ContextRestoreApiStateSeverity.ERROR,
                details="Restored context security metadata is missing or not projected.",
            ),
            ContextRestoreIntegrityCheck(
                check_id="restore_messages_enter_model",
                surface=ContextRestoreApiStateSurface.MODEL_ENVELOPE,
                label="Restored messages entered a real model stream envelope.",
                passed=has_model_restore_messages,
                severity=ContextRestoreApiStateSeverity.BLOCKER,
                details="No model stream envelope reported restored model messages.",
                metadata={"model_stream_report_count": len(model_maps)},
            ),
            ContextRestoreIntegrityCheck(
                check_id="api_projection_observes_state",
                surface=ContextRestoreApiStateSurface.API_PROJECTION,
                label="Task API projection can expose restore and compact state.",
                passed=not projection_map or bool(projection and projection.present),
                severity=ContextRestoreApiStateSeverity.ERROR,
                details="Projection payload was requested but the API projection node was absent.",
            ),
            ContextRestoreIntegrityCheck(
                check_id="event_phases_observed",
                surface=ContextRestoreApiStateSurface.EVENT_LOG,
                label="Event log contains compact, restore and model stream phases.",
                passed=all(
                    int(phase_counts.get(phase, 0)) > 0
                    for phase in (
                        "compact_restore_report",
                        "codeworker_restore_context_applied",
                        "model_stream_report",
                    )
                ),
                severity=ContextRestoreApiStateSeverity.ERROR,
                details="One or more required event phases were not observed in the event log.",
                metadata={phase: phase_counts.get(phase, 0) for phase in phase_counts},
            ),
            ContextRestoreIntegrityCheck(
                check_id="custody_edges_connected",
                surface=ContextRestoreApiStateSurface.EVENT_LOG,
                label="Custody graph connects compact contract through model envelope and API projection.",
                passed=all(path_connected.values()) if path_connected else False,
                severity=ContextRestoreApiStateSeverity.ERROR,
                details="The restore custody graph is missing one or more connected edges.",
                metadata={str(kind): str(value).lower() for kind, value in path_connected.items()},
            ),
        )


def context_restore_api_state_metadata(report: ContextRestoreApiStateReport | None) -> dict[str, str]:
    if report is None:
        return {
            "context_restore_api_state_ok": "false",
            "context_restore_api_state_status": "missing",
            "context_restore_api_state_report_id": "",
        }
    return report.metadata()


def default_context_restore_api_state_source_decisions() -> tuple[dict[str, str], ...]:
    return (
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/compact/compact.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_context_restore_api_state.py",
            "decision": "zyra_module_migrated",
            "capability": "compact restore contract custody from boundary to next-turn application",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/query.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_context_restore_api_state.py",
            "decision": "zyra_module_migrated",
            "capability": "restored messages must enter model stream envelope before tool loop continuation",
        },
        {
            "source_repo": "opencode",
            "source_path": "packages/opencode/src/session/**",
            "target_path": "packages/runtime/zyra_runtime/codeworker_context_restore_api_state.py",
            "decision": "adapter_encapsulated",
            "capability": "task event log phases are treated as route projection custody evidence",
        },
    )


def _edge(
    source: str,
    target: str,
    kind: ContextRestoreCustodyEdgeKind,
    node_ids: set[str],
) -> ContextRestoreCustodyEdge:
    return ContextRestoreCustodyEdge(
        edge_id=f"{source}->{target}",
        kind=kind,
        source_node_id=source,
        target_node_id=target,
        label=f"{source} {kind} {target}",
        present=source in node_ids and target in node_ids,
    )


def _first_node(
    nodes: Sequence[ContextRestoreCustodyNode],
    kind: ContextRestoreCustodyKind,
) -> ContextRestoreCustodyNode | None:
    for node in nodes:
        if node.kind == kind:
            return node
    return None


def _phase_counts(event_records: Sequence[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in event_records:
        event_map = _event_view(event)
        phase = str(_as_mapping(_as_mapping(event_map.get("payload")).get("query_session")).get("phase") or "")
        if phase:
            counts[phase] = counts.get(phase, 0) + 1
    return counts


def _event_view(event: Any) -> dict[str, Any]:
    if isinstance(event, EventRecord):
        return to_jsonable(event)
    if isinstance(event, Mapping):
        return dict(event)
    data = to_jsonable(event)
    return data if isinstance(data, dict) else {}


def _segment_security_summary(segments: Sequence[Any]) -> dict[str, Any]:
    trust_levels: set[str] = set()
    provenance: set[str] = set()
    redactions: set[str] = set()
    retrieval_scopes: set[str] = set()
    code_index_sources: set[str] = set()
    external_count = 0
    for segment in segments:
        segment_map = _as_mapping(segment)
        metadata = _as_mapping(segment_map.get("metadata"))
        trust = str(metadata.get("trust_level") or segment_map.get("trust_level") or "")
        prov = str(metadata.get("source_provenance") or segment_map.get("source_provenance") or "")
        redact = str(metadata.get("secret_redaction_state") or segment_map.get("secret_redaction_state") or "")
        scope = str(metadata.get("retrieval_scope") or segment_map.get("retrieval_scope") or "")
        code_index = str(metadata.get("code_index_source") or segment_map.get("code_index_source") or "")
        if trust:
            trust_levels.add(trust)
        if prov:
            provenance.add(prov)
        if redact:
            redactions.add(redact)
        if scope:
            retrieval_scopes.add(scope)
        if code_index:
            code_index_sources.add(code_index)
        if _truthy(metadata.get("external")) or trust in {"external_untrusted", "untrusted"}:
            external_count += 1
    return {
        "trust_levels": ",".join(sorted(trust_levels)),
        "source_provenance": ",".join(sorted(provenance)),
        "redaction_states": ",".join(sorted(redactions)),
        "retrieval_scopes": ",".join(sorted(retrieval_scopes)),
        "code_index_sources": ",".join(sorted(code_index_sources)),
        "external_count": external_count,
    }


def _security_snapshot_summary(snapshot: Mapping[str, Any]) -> dict[str, str]:
    trust_levels: set[str] = set()
    provenance: set[str] = set()
    redactions: set[str] = set()
    for verdict in _as_list(snapshot.get("verdicts")):
        verdict_map = _as_mapping(verdict)
        metadata = _as_mapping(verdict_map.get("metadata"))
        trust = str(verdict_map.get("trust_level") or metadata.get("trust_level") or "")
        redaction = str(
            verdict_map.get("secret_state")
            or verdict_map.get("secret_redaction_state")
            or metadata.get("secret_redaction_state")
            or ""
        )
        provenance_map = _as_mapping(verdict_map.get("provenance"))
        source = str(
            metadata.get("source_provenance")
            or provenance_map.get("kind")
            or verdict_map.get("source_provenance")
            or ""
        )
        if trust:
            trust_levels.add(trust)
        if redaction:
            redactions.add(redaction)
        if source:
            provenance.add(source)
    return {
        "trust_levels": ",".join(sorted(trust_levels)),
        "redaction_states": ",".join(sorted(redactions)),
        "source_provenance": ",".join(sorted(provenance)),
    }


def _latest_mapping(values: Sequence[Any]) -> Mapping[str, Any]:
    for value in reversed(values):
        if isinstance(value, Mapping):
            return value
    return {}


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


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}
