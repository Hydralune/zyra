from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID
from .tool_runtime_permission_handoff import (
    ToolPermissionHandoffReport,
    ToolPermissionQuestion,
    ToolPermissionQuestionStatus,
    ToolPermissionResolution,
)


class ToolPermissionSessionRecordKind(StrEnum):
    QUESTION_APPENDED = "question_appended"
    RESOLUTION_APPENDED = "resolution_appended"
    PENDING_BLOCKER = "pending_blocker"
    STORE_MISSING = "store_missing"


class ToolPermissionSessionStatus(StrEnum):
    READY = "ready"
    PENDING = "pending"
    DEGRADED = "degraded"
    EMPTY = "empty"
    BLOCKED = "blocked"


class ToolPermissionSessionFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ToolPermissionSessionSurface(StrEnum):
    HANDOFF = "handoff"
    SESSION_MESSAGE = "session_message"
    PERMISSION_STORE = "permission_store"
    REPLY = "reply"
    EVENT = "event"


@dataclass(frozen=True, slots=True)
class ToolPermissionSessionFinding:
    code: str
    severity: ToolPermissionSessionFindingSeverity
    surface: ToolPermissionSessionSurface
    message: str
    question_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolPermissionSessionFindingSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "question_id": self.question_id,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolPermissionSessionRecord:
    record_id: str
    kind: ToolPermissionSessionRecordKind
    question_id: str
    tool_call_id: str
    tool_name: str
    status: str
    permission_request_id: str = ""
    session_message_role: str = "assistant"
    session_message_type: str = "permission_question"
    content: str = ""
    reply_options: tuple[str, ...] = ()
    resolution_effect: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    @property
    def pending(self) -> bool:
        return self.kind == ToolPermissionSessionRecordKind.QUESTION_APPENDED and self.status.endswith("pending")

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "kind": str(self.kind),
            "question_id": self.question_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "permission_request_id": self.permission_request_id,
            "session_message_role": self.session_message_role,
            "session_message_type": self.session_message_type,
            "content": self.content,
            "reply_options": list(self.reply_options),
            "resolution_effect": self.resolution_effect,
            "pending": self.pending,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ToolPermissionSessionReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    handoff_report_id: str
    records: tuple[ToolPermissionSessionRecord, ...]
    findings: tuple[ToolPermissionSessionFinding, ...] = ()
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ToolPermissionSessionStatus:
        if any(finding.blocking for finding in self.findings):
            return ToolPermissionSessionStatus.BLOCKED
        if self.pending_count:
            return ToolPermissionSessionStatus.PENDING
        if self.findings:
            return ToolPermissionSessionStatus.DEGRADED
        if not self.records:
            return ToolPermissionSessionStatus.EMPTY
        return ToolPermissionSessionStatus.READY

    @property
    def question_count(self) -> int:
        return sum(1 for record in self.records if record.kind == ToolPermissionSessionRecordKind.QUESTION_APPENDED)

    @property
    def pending_count(self) -> int:
        return sum(1 for record in self.records if record.pending)

    @property
    def resolution_count(self) -> int:
        return sum(1 for record in self.records if record.kind == ToolPermissionSessionRecordKind.RESOLUTION_APPENDED)

    @property
    def blocker_count(self) -> int:
        return sum(1 for record in self.records if record.kind == ToolPermissionSessionRecordKind.PENDING_BLOCKER)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.tool_permission_session.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "handoff_report_id": self.handoff_report_id,
            "ok": self.ok,
            "status": str(self.status),
            "question_count": self.question_count,
            "pending_count": self.pending_count,
            "resolution_count": self.resolution_count,
            "blocker_count": self.blocker_count,
            "records": [record.to_dict() for record in self.records],
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_permission_session_report_id": self.report_id,
            "tool_permission_session_owner_unit": self.owner_unit,
            "tool_permission_session_runtime_id": self.runtime_id,
            "tool_permission_session_ok": str(self.ok).lower(),
            "tool_permission_session_status": str(self.status),
            "tool_permission_session_questions": str(self.question_count),
            "tool_permission_session_pending": str(self.pending_count),
            "tool_permission_session_resolutions": str(self.resolution_count),
            "tool_permission_session_blockers": str(self.blocker_count),
            "tool_permission_session_findings": str(len(self.findings)),
            "tool_permission_session_handoff_report_id": self.handoff_report_id,
        }


class ToolPermissionSessionRuntime:
    """Projects permission handoff questions into session-visible messages."""

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
        handoff_report: ToolPermissionHandoffReport,
        require_store_link: bool = True,
    ) -> ToolPermissionSessionReport:
        records: list[ToolPermissionSessionRecord] = []
        findings: list[ToolPermissionSessionFinding] = []
        for question in handoff_report.questions:
            records.append(self._question_record(question))
            if question.pending:
                records.append(self._pending_record(question))
            if require_store_link and not question.permission_request_id:
                records.append(self._store_missing_record(question))
                findings.append(
                    ToolPermissionSessionFinding(
                        code="PERMISSION_QUESTION_WITHOUT_STORE_REQUEST",
                        severity=ToolPermissionSessionFindingSeverity.ERROR,
                        surface=ToolPermissionSessionSurface.PERMISSION_STORE,
                        message="A permission question did not include a persisted permission request id.",
                        question_id=question.question_id,
                    )
                )
        for resolution in handoff_report.resolutions:
            records.append(self._resolution_record(resolution))
            if not resolution.ok:
                findings.append(
                    ToolPermissionSessionFinding(
                        code="PERMISSION_RESOLUTION_FAILED",
                        severity=ToolPermissionSessionFindingSeverity.ERROR,
                        surface=ToolPermissionSessionSurface.REPLY,
                        message=resolution.error or "Permission resolution failed.",
                        question_id=resolution.question.question_id,
                    )
                )
        for finding in handoff_report.findings:
            severity = (
                ToolPermissionSessionFindingSeverity.BLOCKER
                if finding.blocking
                else ToolPermissionSessionFindingSeverity.WARNING
                if str(finding.severity).endswith("warning")
                else ToolPermissionSessionFindingSeverity.INFO
            )
            findings.append(
                ToolPermissionSessionFinding(
                    code=f"HANDOFF_{finding.code}",
                    severity=severity,
                    surface=ToolPermissionSessionSurface.HANDOFF,
                    message=finding.message,
                    metadata=finding.metadata,
                )
            )
        return ToolPermissionSessionReport(
            report_id=new_id("toolpermsession"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            handoff_report_id=handoff_report.report_id,
            records=tuple(records),
            findings=tuple(findings),
        )

    def event_for_report(
        self,
        report: ToolPermissionSessionReport,
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
                    "phase": "tool_permission_session_bridge",
                    "tool_permission_session": report.to_dict(),
                }
            },
        )

    def record_events(
        self,
        report: ToolPermissionSessionReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> list[EventRecord]:
        events: list[EventRecord] = []
        for record in report.records:
            if record.kind == ToolPermissionSessionRecordKind.QUESTION_APPENDED:
                phase = "tool_permission_question_appended"
            elif record.kind == ToolPermissionSessionRecordKind.RESOLUTION_APPENDED:
                phase = "tool_permission_resolution_appended"
            elif record.kind == ToolPermissionSessionRecordKind.PENDING_BLOCKER:
                phase = "tool_permission_pending_blocker"
            else:
                phase = "tool_permission_store_missing"
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
                            "phase": phase,
                            "tool_permission_session_record": record.to_dict(),
                        }
                    },
                )
            )
        return events

    def _question_record(self, question: ToolPermissionQuestion) -> ToolPermissionSessionRecord:
        return ToolPermissionSessionRecord(
            record_id=new_id("toolpermrecord"),
            kind=ToolPermissionSessionRecordKind.QUESTION_APPENDED,
            question_id=question.question_id,
            tool_call_id=question.tool_call_id,
            tool_name=question.tool_name,
            status=str(question.status),
            permission_request_id=question.permission_request_id,
            content=_question_content(question),
            reply_options=tuple(str(option) for option in question.reply_options),
            metadata={
                "operation": question.operation,
                "subject": question.subject,
                "reason": question.reason,
                "source": "opencode permission question handoff",
            },
        )

    def _pending_record(self, question: ToolPermissionQuestion) -> ToolPermissionSessionRecord:
        return ToolPermissionSessionRecord(
            record_id=new_id("toolpermrecord"),
            kind=ToolPermissionSessionRecordKind.PENDING_BLOCKER,
            question_id=question.question_id,
            tool_call_id=question.tool_call_id,
            tool_name=question.tool_name,
            status=str(ToolPermissionQuestionStatus.PENDING),
            permission_request_id=question.permission_request_id,
            session_message_type="permission_pending",
            content=f"Tool {question.tool_name} is blocked pending permission for {question.subject}.",
            metadata={"reason": question.reason, "blocks_tool_side_effect": "true"},
        )

    def _store_missing_record(self, question: ToolPermissionQuestion) -> ToolPermissionSessionRecord:
        return ToolPermissionSessionRecord(
            record_id=new_id("toolpermrecord"),
            kind=ToolPermissionSessionRecordKind.STORE_MISSING,
            question_id=question.question_id,
            tool_call_id=question.tool_call_id,
            tool_name=question.tool_name,
            status="store_missing",
            session_message_type="permission_store_missing",
            content=f"Permission question {question.question_id} could not be linked to the permission store.",
            metadata={"blocks_reply_resolution": "true"},
        )

    def _resolution_record(self, resolution: ToolPermissionResolution) -> ToolPermissionSessionRecord:
        return ToolPermissionSessionRecord(
            record_id=new_id("toolpermrecord"),
            kind=ToolPermissionSessionRecordKind.RESOLUTION_APPENDED,
            question_id=resolution.question.question_id,
            tool_call_id=resolution.question.tool_call_id,
            tool_name=resolution.question.tool_name,
            status=resolution.store_status,
            permission_request_id=resolution.store_request_id,
            session_message_type="permission_resolution",
            content=f"Permission for {resolution.question.tool_name} resolved as {resolution.store_status}.",
            resolution_effect=str(resolution.reply.effect),
            metadata={**dict(resolution.metadata), "ok": str(resolution.ok).lower(), "error": resolution.error},
        )


def tool_permission_session_metadata(report: ToolPermissionSessionReport | None) -> dict[str, str]:
    return report.metadata() if report is not None else {
        "tool_permission_session_ok": "false",
        "tool_permission_session_questions": "0",
        "tool_permission_session_pending": "0",
    }


def _question_content(question: ToolPermissionQuestion) -> str:
    operation = question.operation or "execute"
    subject = question.subject or question.tool_name
    reason = f" Reason: {question.reason}" if question.reason else ""
    return f"Allow tool {question.tool_name} to {operation} {subject}?{reason}"
