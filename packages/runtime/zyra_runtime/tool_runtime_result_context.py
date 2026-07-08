from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID
from .tool_runtime_output_store import ToolOutputStoreSnapshot
from .tool_runtime_session_bridge import ToolSessionBridgeReport


class ToolResultContextKind(StrEnum):
    INLINE = "inline"
    BUDGETED_ARTIFACT = "budgeted_artifact"
    PERMISSION_REQUIRED = "permission_required"
    SCHEMA_ERROR = "schema_error"
    TOOL_ERROR = "tool_error"


class ToolResultContextStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    EMPTY = "empty"
    BLOCKED = "blocked"


class ToolResultContextFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ToolResultContextSurface(StrEnum):
    RECEIPT = "receipt"
    SESSION_MESSAGE = "session_message"
    CONTEXT_WINDOW = "context_window"
    ARTIFACT = "artifact"
    BUDGET = "budget"
    BRIDGE = "bridge"


@dataclass(frozen=True, slots=True)
class ToolResultContextFinding:
    code: str
    severity: ToolResultContextFindingSeverity
    surface: ToolResultContextSurface
    message: str
    tool_call_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolResultContextFindingSeverity.BLOCKER

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
class ToolResultSessionMessage:
    message_id: str
    role: str
    content_type: str
    tool_call_id: str
    tool_name: str
    content: str
    artifact_ids: tuple[str, ...]
    budget_applied: bool
    error: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    @property
    def visible_to_next_turn(self) -> bool:
        return bool(self.content or self.artifact_ids or self.error)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": self.role,
            "content_type": self.content_type,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "content": self.content,
            "artifact_ids": list(self.artifact_ids),
            "budget_applied": self.budget_applied,
            "error": self.error,
            "visible_to_next_turn": self.visible_to_next_turn,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ToolResultContextProjection:
    projection_id: str
    kind: ToolResultContextKind
    tool_call_id: str
    tool_name: str
    ok: bool
    turn_index: int
    batch_index: int
    step_index: int
    inline_chars: int
    original_chars: int
    budget_chars: int
    budget_applied: bool
    raw_output_blocked: bool
    artifact_ids: tuple[str, ...]
    externalized_artifact_id: str
    session_message: ToolResultSessionMessage
    output_store_entry_id: str = ""
    permission_required: bool = False
    error: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    @property
    def externalized(self) -> bool:
        return bool(self.externalized_artifact_id or self.artifact_ids and self.budget_applied)

    @property
    def visible_to_next_turn(self) -> bool:
        return self.session_message.visible_to_next_turn

    def to_dict(self) -> dict[str, Any]:
        return {
            "projection_id": self.projection_id,
            "kind": str(self.kind),
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "ok": self.ok,
            "turn_index": self.turn_index,
            "batch_index": self.batch_index,
            "step_index": self.step_index,
            "inline_chars": self.inline_chars,
            "original_chars": self.original_chars,
            "budget_chars": self.budget_chars,
            "budget_applied": self.budget_applied,
            "raw_output_blocked": self.raw_output_blocked,
            "artifact_ids": list(self.artifact_ids),
            "externalized_artifact_id": self.externalized_artifact_id,
            "externalized": self.externalized,
            "visible_to_next_turn": self.visible_to_next_turn,
            "permission_required": self.permission_required,
            "error": self.error,
            "output_store_entry_id": self.output_store_entry_id,
            "session_message": self.session_message.to_dict(),
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ToolResultContextReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    projections: tuple[ToolResultContextProjection, ...]
    findings: tuple[ToolResultContextFinding, ...]
    bridge_report_id: str = ""
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ToolResultContextStatus:
        if any(finding.blocking for finding in self.findings):
            return ToolResultContextStatus.BLOCKED
        if not self.projections:
            return ToolResultContextStatus.EMPTY
        if self.findings:
            return ToolResultContextStatus.DEGRADED
        return ToolResultContextStatus.READY

    @property
    def projection_count(self) -> int:
        return len(self.projections)

    @property
    def appended_message_count(self) -> int:
        return sum(1 for item in self.projections if item.session_message.visible_to_next_turn)

    @property
    def budgeted_count(self) -> int:
        return sum(1 for item in self.projections if item.budget_applied)

    @property
    def raw_output_blocked_count(self) -> int:
        return sum(1 for item in self.projections if item.raw_output_blocked)

    @property
    def artifact_ref_count(self) -> int:
        refs: set[str] = set()
        for item in self.projections:
            refs.update(item.artifact_ids)
            if item.externalized_artifact_id:
                refs.add(item.externalized_artifact_id)
        return len(refs)

    @property
    def permission_required_count(self) -> int:
        return sum(1 for item in self.projections if item.permission_required)

    @property
    def error_count(self) -> int:
        return sum(1 for item in self.projections if not item.ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.tool_result_context.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "bridge_report_id": self.bridge_report_id,
            "ok": self.ok,
            "status": str(self.status),
            "projection_count": self.projection_count,
            "appended_message_count": self.appended_message_count,
            "budgeted_count": self.budgeted_count,
            "raw_output_blocked_count": self.raw_output_blocked_count,
            "artifact_ref_count": self.artifact_ref_count,
            "permission_required_count": self.permission_required_count,
            "error_count": self.error_count,
            "projections": [item.to_dict() for item in self.projections],
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_result_context_report_id": self.report_id,
            "tool_result_context_owner_unit": self.owner_unit,
            "tool_result_context_runtime_id": self.runtime_id,
            "tool_result_context_ok": str(self.ok).lower(),
            "tool_result_context_status": str(self.status),
            "tool_result_context_projections": str(self.projection_count),
            "tool_result_context_appended_messages": str(self.appended_message_count),
            "tool_result_context_budgeted": str(self.budgeted_count),
            "tool_result_context_raw_output_blocked": str(self.raw_output_blocked_count),
            "tool_result_context_artifact_refs": str(self.artifact_ref_count),
            "tool_result_context_permission_required": str(self.permission_required_count),
            "tool_result_context_errors": str(self.error_count),
            "tool_result_context_findings": str(len(self.findings)),
            "tool_result_context_bridge_report_id": self.bridge_report_id,
        }


class ToolResultContextRuntime:
    """Projects bounded tool results back into session/context state."""

    def __init__(
        self,
        *,
        owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID,
        max_inline_chars: int = 800,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.max_inline_chars = max(1, int(max_inline_chars))

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        receipts: Sequence[Mapping[str, Any]],
        context_snapshots: Sequence[Mapping[str, Any]] = (),
        output_store_snapshot: ToolOutputStoreSnapshot | None = None,
        session_bridge_report: ToolSessionBridgeReport | None = None,
    ) -> ToolResultContextReport:
        projections: list[ToolResultContextProjection] = []
        findings: list[ToolResultContextFinding] = []
        store_entries = {}
        if output_store_snapshot is not None:
            store_entries = {entry.tool_call_id: entry for entry in output_store_snapshot.entries}
        for receipt in receipts:
            projection = self._projection(receipt, store_entries=store_entries)
            projections.append(projection)
            findings.extend(self._projection_findings(projection))
        findings.extend(self._context_findings(projections, context_snapshots))
        findings.extend(self._bridge_findings(projections, session_bridge_report))
        return ToolResultContextReport(
            report_id=new_id("toolresultctx"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            projections=tuple(projections),
            findings=tuple(findings),
            bridge_report_id=session_bridge_report.report_id if session_bridge_report is not None else "",
        )

    def event_for_report(
        self,
        report: ToolResultContextReport,
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
                    "phase": "tool_result_context_projected",
                    "tool_result_context": report.to_dict(),
                }
            },
        )

    def message_events(
        self,
        report: ToolResultContextReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> list[EventRecord]:
        events: list[EventRecord] = []
        for projection in report.projections:
            events.append(
                EventRecord(
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    event_type=EventType.AGENT_MESSAGE,
                    payload={
                        "query_session": {
                            "session_id": report.session_id,
                            "worker_request_id": report.worker_request_id,
                            "phase": "tool_result_session_appended",
                            "tool_result_context_projection_id": projection.projection_id,
                            "tool_result_message": projection.session_message.to_dict(),
                        }
                    },
                )
            )
        return events

    def _projection(
        self,
        receipt: Mapping[str, Any],
        *,
        store_entries: Mapping[str, Any],
    ) -> ToolResultContextProjection:
        request = receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}
        result = receipt.get("bounded_result") if isinstance(receipt.get("bounded_result"), Mapping) else {}
        decision = receipt.get("budget_decision") if isinstance(receipt.get("budget_decision"), Mapping) else {}
        output = result.get("output") if isinstance(result.get("output"), Mapping) else {}
        metadata = result.get("metadata") if isinstance(result.get("metadata"), Mapping) else {}
        tool_call_id = str(request.get("tool_call_id") or result.get("tool_call_id") or "")
        tool_name = str(request.get("tool_name") or "")
        ok = result.get("ok") is True
        error = str(result.get("error") or "")
        budget_applied = decision.get("applied") is True or str(metadata.get("tool_result_budget_applied") or "").lower() == "true"
        original_chars = _int(decision.get("original_chars") or output.get("original_chars") or metadata.get("tool_result_original_chars"))
        budget_chars = _int(decision.get("budget_chars") or output.get("budget_chars") or metadata.get("tool_result_budget_chars"))
        artifact_ids = tuple(_artifact_ids(result, output, decision))
        externalized_artifact_id = str(decision.get("artifact_id") or output.get("full_output_artifact_id") or "")
        inline_text = _bounded_inline_text(output, result, max_chars=self.max_inline_chars)
        inline_chars = len(inline_text)
        permission_required = error == "permission_required" or str(metadata.get("permission_effect") or "") == "ask"
        kind = _projection_kind(ok=ok, error=error, budget_applied=budget_applied, permission_required=permission_required)
        raw_output_blocked = bool(budget_applied and externalized_artifact_id and original_chars > inline_chars)
        store_entry = store_entries.get(tool_call_id)
        store_entry_id = str(getattr(store_entry, "entry_id", "") or "")
        message = ToolResultSessionMessage(
            message_id=new_id("toolresultmsg"),
            role="user",
            content_type="tool_result",
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            content=inline_text,
            artifact_ids=artifact_ids,
            budget_applied=budget_applied,
            error=error,
            metadata={
                "runtime_id": self.runtime_id,
                "owner_unit": self.owner_unit,
                "budget_applied": str(budget_applied).lower(),
                "externalized_artifact_id": externalized_artifact_id,
                "raw_output_blocked": str(raw_output_blocked).lower(),
                "output_store_entry_id": store_entry_id,
            },
        )
        return ToolResultContextProjection(
            projection_id=new_id("toolresultprojection"),
            kind=kind,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            ok=ok,
            turn_index=_int(request.get("turn_index")),
            batch_index=_batch_index(receipt),
            step_index=_int(request.get("step_index")),
            inline_chars=inline_chars,
            original_chars=original_chars,
            budget_chars=budget_chars,
            budget_applied=budget_applied,
            raw_output_blocked=raw_output_blocked,
            artifact_ids=artifact_ids,
            externalized_artifact_id=externalized_artifact_id,
            session_message=message,
            output_store_entry_id=store_entry_id,
            permission_required=permission_required,
            error=error,
            metadata={
                "tool_result_shaping_source": str(metadata.get("tool_result_shaping_source") or ""),
                "tool_result_untrusted_envelope": str(metadata.get("tool_result_untrusted_envelope") or ""),
                "receipt_executor": str(receipt.get("executor_name") or ""),
            },
        )

    def _projection_findings(self, projection: ToolResultContextProjection) -> list[ToolResultContextFinding]:
        findings: list[ToolResultContextFinding] = []
        if projection.budget_applied and not projection.externalized_artifact_id:
            findings.append(
                ToolResultContextFinding(
                    code="BUDGETED_RESULT_WITHOUT_EXTERNALIZED_ARTIFACT",
                    severity=ToolResultContextFindingSeverity.ERROR,
                    surface=ToolResultContextSurface.BUDGET,
                    message="A budgeted tool result did not expose an externalized artifact id.",
                    tool_call_id=projection.tool_call_id,
                )
            )
        if not projection.visible_to_next_turn:
            findings.append(
                ToolResultContextFinding(
                    code="TOOL_RESULT_NOT_VISIBLE_TO_NEXT_TURN",
                    severity=ToolResultContextFindingSeverity.BLOCKER,
                    surface=ToolResultContextSurface.SESSION_MESSAGE,
                    message="A tool result projection produced no message content, artifact reference or error.",
                    tool_call_id=projection.tool_call_id,
                )
            )
        return findings

    def _context_findings(
        self,
        projections: Sequence[ToolResultContextProjection],
        context_snapshots: Sequence[Mapping[str, Any]],
    ) -> list[ToolResultContextFinding]:
        findings: list[ToolResultContextFinding] = []
        if not projections:
            return findings
        message_modifier_count = 0
        budget_entries = 0
        artifact_refs: set[str] = set()
        for snapshot in context_snapshots:
            if not isinstance(snapshot, Mapping):
                continue
            modifiers = snapshot.get("modifier_log") if isinstance(snapshot.get("modifier_log"), Sequence) else ()
            for modifier in modifiers:
                if not isinstance(modifier, Mapping):
                    continue
                if str(modifier.get("kind") or "").endswith("message_append"):
                    message_modifier_count += 1
            ledger = snapshot.get("budget_ledger") if isinstance(snapshot.get("budget_ledger"), Sequence) else ()
            budget_entries += len(ledger)
            refs = snapshot.get("artifact_refs") if isinstance(snapshot.get("artifact_refs"), Sequence) else ()
            artifact_refs.update(str(ref) for ref in refs if ref)
        if message_modifier_count < len(projections):
            findings.append(
                ToolResultContextFinding(
                    code="CONTEXT_MESSAGE_APPEND_COUNT_BELOW_RESULTS",
                    severity=ToolResultContextFindingSeverity.WARNING,
                    surface=ToolResultContextSurface.CONTEXT_WINDOW,
                    message="ToolUseContext message append modifiers are fewer than projected tool results.",
                    metadata={
                        "message_modifier_count": str(message_modifier_count),
                        "projection_count": str(len(projections)),
                    },
                )
            )
        if any(item.budget_applied for item in projections) and budget_entries == 0:
            findings.append(
                ToolResultContextFinding(
                    code="BUDGETED_RESULT_MISSING_CONTEXT_LEDGER",
                    severity=ToolResultContextFindingSeverity.ERROR,
                    surface=ToolResultContextSurface.BUDGET,
                    message="Budgeted tool results did not leave a budget ledger entry in ToolUseContext.",
                )
            )
        projected_artifacts = {ref for item in projections for ref in item.artifact_ids}
        if projected_artifacts and not projected_artifacts.issubset(artifact_refs):
            findings.append(
                ToolResultContextFinding(
                    code="PROJECTED_ARTIFACTS_NOT_ALL_IN_CONTEXT",
                    severity=ToolResultContextFindingSeverity.WARNING,
                    surface=ToolResultContextSurface.ARTIFACT,
                    message="Some projected tool artifact refs were not present in ToolUseContext snapshots.",
                    metadata={
                        "projected_artifacts": ",".join(sorted(projected_artifacts)),
                        "context_artifacts": ",".join(sorted(artifact_refs)),
                    },
                )
            )
        return findings

    def _bridge_findings(
        self,
        projections: Sequence[ToolResultContextProjection],
        session_bridge_report: ToolSessionBridgeReport | None,
    ) -> list[ToolResultContextFinding]:
        if session_bridge_report is None:
            return []
        expected = session_bridge_report.valid_tool_use_count
        if expected and len(projections) < expected:
            return [
                ToolResultContextFinding(
                    code="SESSION_BRIDGE_TOOL_USE_WITHOUT_RESULT_PROJECTION",
                    severity=ToolResultContextFindingSeverity.BLOCKER,
                    surface=ToolResultContextSurface.BRIDGE,
                    message="Assistant tool_use blocks exceeded projected tool_result messages.",
                    metadata={"expected": str(expected), "projected": str(len(projections))},
                )
            ]
        return []


def tool_result_context_metadata(report: ToolResultContextReport | None) -> dict[str, str]:
    return report.metadata() if report is not None else {
        "tool_result_context_ok": "false",
        "tool_result_context_projections": "0",
    }


def _artifact_ids(result: Mapping[str, Any], output: Mapping[str, Any], decision: Mapping[str, Any]) -> list[str]:
    refs: list[str] = []
    artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), Sequence) else ()
    for artifact in artifacts:
        if isinstance(artifact, Mapping):
            artifact_id = str(artifact.get("artifact_id") or "")
        else:
            artifact_id = str(getattr(artifact, "artifact_id", "") or "")
        if artifact_id and artifact_id not in refs:
            refs.append(artifact_id)
    for value in (decision.get("artifact_id"), output.get("full_output_artifact_id")):
        artifact_id = str(value or "")
        if artifact_id and artifact_id not in refs:
            refs.append(artifact_id)
    return refs


def _bounded_inline_text(output: Mapping[str, Any], result: Mapping[str, Any], *, max_chars: int) -> str:
    if isinstance(output.get("output_preview"), str):
        return output["output_preview"][:max_chars]
    if isinstance(output.get("content"), str):
        return output["content"][:max_chars]
    if isinstance(output.get("stdout"), str):
        return output["stdout"][:max_chars]
    summary = str(result.get("summary") or "")
    if output:
        text = json.dumps(to_jsonable(output), ensure_ascii=False, sort_keys=True)
        return text[:max_chars]
    return summary[:max_chars]


def _projection_kind(
    *,
    ok: bool,
    error: str,
    budget_applied: bool,
    permission_required: bool,
) -> ToolResultContextKind:
    if permission_required:
        return ToolResultContextKind.PERMISSION_REQUIRED
    if error == "schema_error":
        return ToolResultContextKind.SCHEMA_ERROR
    if budget_applied:
        return ToolResultContextKind.BUDGETED_ARTIFACT
    if not ok:
        return ToolResultContextKind.TOOL_ERROR
    return ToolResultContextKind.INLINE


def _batch_index(receipt: Mapping[str, Any]) -> int:
    request = receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}
    metadata = request.get("metadata") if isinstance(request.get("metadata"), Mapping) else {}
    return _int(metadata.get("batch_index") or receipt.get("batch_index"))


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
