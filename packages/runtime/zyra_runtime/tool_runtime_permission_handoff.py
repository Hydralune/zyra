from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .permissions import JsonPermissionStore, PermissionRequestStatus
from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID


class ToolPermissionQuestionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    SKIPPED = "skipped"


class ToolPermissionReplyEffect(StrEnum):
    APPROVE_ONCE = "approve_once"
    APPROVE_ALWAYS = "approve_always"
    DENY_ONCE = "deny_once"
    DENY_ALWAYS = "deny_always"
    NO_REPLY = "no_reply"


class ToolPermissionHandoffSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ToolPermissionHandoffSurface(StrEnum):
    RECEIPT = "receipt"
    CONTEXT = "context"
    STORE = "permission_store"
    EVENT = "event"
    REPLY = "reply"


@dataclass(frozen=True, slots=True)
class ToolPermissionQuestion:
    question_id: str
    tool_call_id: str
    tool_name: str
    operation: str
    subject: str
    reason: str
    status: ToolPermissionQuestionStatus
    permission_request_id: str = ""
    reply_options: tuple[ToolPermissionReplyEffect, ...] = (
        ToolPermissionReplyEffect.APPROVE_ONCE,
        ToolPermissionReplyEffect.APPROVE_ALWAYS,
        ToolPermissionReplyEffect.DENY_ONCE,
        ToolPermissionReplyEffect.DENY_ALWAYS,
    )
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def pending(self) -> bool:
        return self.status == ToolPermissionQuestionStatus.PENDING

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "operation": self.operation,
            "subject": self.subject,
            "reason": self.reason,
            "status": str(self.status),
            "permission_request_id": self.permission_request_id,
            "reply_options": [str(option) for option in self.reply_options],
            "pending": self.pending,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolPermissionReply:
    question_id: str
    effect: ToolPermissionReplyEffect
    create_rule: bool = False
    replied_at: str = field(default_factory=now_iso)
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def approving(self) -> bool:
        return self.effect in {ToolPermissionReplyEffect.APPROVE_ONCE, ToolPermissionReplyEffect.APPROVE_ALWAYS}

    @property
    def denying(self) -> bool:
        return self.effect in {ToolPermissionReplyEffect.DENY_ONCE, ToolPermissionReplyEffect.DENY_ALWAYS}

    @property
    def persistent(self) -> bool:
        return self.effect in {ToolPermissionReplyEffect.APPROVE_ALWAYS, ToolPermissionReplyEffect.DENY_ALWAYS} or self.create_rule

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "effect": str(self.effect),
            "create_rule": self.create_rule,
            "approving": self.approving,
            "denying": self.denying,
            "persistent": self.persistent,
            "replied_at": self.replied_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolPermissionResolution:
    question: ToolPermissionQuestion
    reply: ToolPermissionReply
    store_request_id: str
    store_status: str
    ok: bool
    error: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question.to_dict(),
            "reply": self.reply.to_dict(),
            "store_request_id": self.store_request_id,
            "store_status": self.store_status,
            "ok": self.ok,
            "error": self.error,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolPermissionHandoffFinding:
    code: str
    severity: ToolPermissionHandoffSeverity
    surface: ToolPermissionHandoffSurface
    message: str
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolPermissionHandoffSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolPermissionHandoffReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    questions: tuple[ToolPermissionQuestion, ...]
    resolutions: tuple[ToolPermissionResolution, ...] = ()
    findings: tuple[ToolPermissionHandoffFinding, ...] = ()
    created_at: str = field(default_factory=now_iso)

    @property
    def pending_count(self) -> int:
        return sum(1 for question in self.questions if question.pending)

    @property
    def resolved_count(self) -> int:
        return len(self.resolutions)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "pending_count": self.pending_count,
            "resolved_count": self.resolved_count,
            "ok": self.ok,
            "questions": [question.to_dict() for question in self.questions],
            "resolutions": [resolution.to_dict() for resolution in self.resolutions],
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_permission_handoff_report_id": self.report_id,
            "tool_permission_handoff_owner_unit": self.owner_unit,
            "tool_permission_handoff_ok": str(self.ok).lower(),
            "tool_permission_handoff_questions": str(len(self.questions)),
            "tool_permission_handoff_pending": str(self.pending_count),
            "tool_permission_handoff_resolved": str(self.resolved_count),
            "tool_permission_handoff_findings": str(len(self.findings)),
        }


class ToolPermissionHandoffRuntime:
    def __init__(
        self,
        *,
        permission_store: JsonPermissionStore | None = None,
        owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID,
    ) -> None:
        self.permission_store = permission_store
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        receipts: Sequence[Mapping[str, Any]],
        context_snapshots: Sequence[Mapping[str, Any]],
    ) -> ToolPermissionHandoffReport:
        questions = self.questions_from_receipts(receipts, context_snapshots=context_snapshots)
        findings = [
            *self._receipt_findings(receipts, questions),
            *self._context_findings(context_snapshots, questions),
            *self._store_findings(questions),
        ]
        return ToolPermissionHandoffReport(
            report_id=new_id("toolpermhandoff"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            questions=tuple(questions),
            findings=tuple(findings),
        )

    def questions_from_receipts(
        self,
        receipts: Sequence[Mapping[str, Any]],
        *,
        context_snapshots: Sequence[Mapping[str, Any]] = (),
    ) -> list[ToolPermissionQuestion]:
        context_handoffs = _context_handoff_index(context_snapshots)
        questions: list[ToolPermissionQuestion] = []
        seen: set[str] = set()
        for receipt in receipts:
            question = self._question_from_receipt(receipt, context_handoffs)
            if question is None:
                continue
            if question.tool_call_id in seen:
                continue
            seen.add(question.tool_call_id)
            questions.append(question)
        return questions

    def resolve(
        self,
        report: ToolPermissionHandoffReport,
        reply: ToolPermissionReply,
    ) -> ToolPermissionResolution:
        question = next((item for item in report.questions if item.question_id == reply.question_id), None)
        if question is None:
            return ToolPermissionResolution(
                question=_missing_question(reply.question_id),
                reply=reply,
                store_request_id="",
                store_status="",
                ok=False,
                error="permission_question_not_found",
            )
        if self.permission_store is None:
            return ToolPermissionResolution(
                question=question,
                reply=reply,
                store_request_id=question.permission_request_id,
                store_status="",
                ok=False,
                error="permission_store_missing",
            )
        if not question.permission_request_id:
            return ToolPermissionResolution(
                question=question,
                reply=reply,
                store_request_id="",
                store_status="",
                ok=False,
                error="permission_request_id_missing",
            )
        if reply.effect == ToolPermissionReplyEffect.NO_REPLY:
            return ToolPermissionResolution(
                question=question,
                reply=reply,
                store_request_id=question.permission_request_id,
                store_status=str(ToolPermissionQuestionStatus.PENDING),
                ok=True,
                metadata={"no_reply": "true"},
            )
        status = PermissionRequestStatus.APPROVED if reply.approving else PermissionRequestStatus.DENIED
        resolved = self.permission_store.resolve_request(
            question.permission_request_id,
            status,
            create_rule=reply.persistent,
        )
        if resolved is None:
            return ToolPermissionResolution(
                question=question,
                reply=reply,
                store_request_id=question.permission_request_id,
                store_status="",
                ok=False,
                error="permission_store_request_missing",
            )
        return ToolPermissionResolution(
            question=ToolPermissionQuestion(
                question_id=question.question_id,
                tool_call_id=question.tool_call_id,
                tool_name=question.tool_name,
                operation=question.operation,
                subject=question.subject,
                reason=question.reason,
                status=ToolPermissionQuestionStatus.APPROVED if reply.approving else ToolPermissionQuestionStatus.DENIED,
                permission_request_id=question.permission_request_id,
                reply_options=question.reply_options,
                created_at=question.created_at,
                metadata=question.metadata,
            ),
            reply=reply,
            store_request_id=resolved.request_id,
            store_status=str(resolved.status),
            ok=True,
            metadata={
                "persistent_rule_created": str(reply.persistent).lower(),
                "effect": str(reply.effect),
            },
        )

    def report_with_resolutions(
        self,
        report: ToolPermissionHandoffReport,
        resolutions: Sequence[ToolPermissionResolution],
    ) -> ToolPermissionHandoffReport:
        resolution_by_question = {resolution.question.question_id: resolution for resolution in resolutions if resolution.ok}
        questions = []
        for question in report.questions:
            resolution = resolution_by_question.get(question.question_id)
            questions.append(resolution.question if resolution is not None else question)
        findings = list(report.findings)
        for resolution in resolutions:
            if not resolution.ok:
                findings.append(
                    _finding(
                        "TOOL_PERMISSION_RESOLUTION_FAILED",
                        ToolPermissionHandoffSeverity.ERROR,
                        ToolPermissionHandoffSurface.REPLY,
                        "Permission reply could not be applied to the store.",
                        question_id=resolution.reply.question_id,
                        error=resolution.error,
                    )
                )
        return ToolPermissionHandoffReport(
            report_id=report.report_id,
            owner_unit=report.owner_unit,
            runtime_id=report.runtime_id,
            session_id=report.session_id,
            worker_request_id=report.worker_request_id,
            questions=tuple(questions),
            resolutions=tuple(resolutions),
            findings=tuple(findings),
            created_at=report.created_at,
        )

    def event_for_report(
        self,
        report: ToolPermissionHandoffReport,
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
                    "phase": "tool_permission_handoff",
                    "permission_handoff": report.to_dict(),
                }
            },
        )

    def _question_from_receipt(
        self,
        receipt: Mapping[str, Any],
        context_handoffs: Mapping[str, Mapping[str, Any]],
    ) -> ToolPermissionQuestion | None:
        result = receipt.get("bounded_result") if isinstance(receipt.get("bounded_result"), Mapping) else {}
        raw_result = receipt.get("raw_result") if isinstance(receipt.get("raw_result"), Mapping) else {}
        error = str(result.get("error") or raw_result.get("error") or "")
        metadata = result.get("metadata") if isinstance(result.get("metadata"), Mapping) else {}
        permission_effect = str(metadata.get("permission_effect") or "")
        if error not in {"permission_required", "permission_denied"} and permission_effect not in {"ask", "deny"}:
            return None
        request = receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}
        tool_call_id = str(request.get("tool_call_id") or result.get("tool_call_id") or "")
        handoff = context_handoffs.get(tool_call_id, {})
        status = ToolPermissionQuestionStatus.PENDING if error == "permission_required" else ToolPermissionQuestionStatus.DENIED
        return ToolPermissionQuestion(
            question_id=new_id("toolpermq"),
            tool_call_id=tool_call_id,
            tool_name=str(request.get("tool_name") or handoff.get("tool_name") or ""),
            operation=str(metadata.get("operation") or _operation_for_tool(request)),
            subject=str(metadata.get("subject") or _subject_for_request(request)),
            reason=str(metadata.get("permission_reason") or handoff.get("reason") or error),
            status=status,
            permission_request_id=str(metadata.get("permission_request_id") or handoff.get("permission_request_id") or ""),
            metadata={
                "permission_effect": permission_effect,
                "error": error,
                "handoff_source": "receipt+ToolUseContext",
            },
        )

    def _receipt_findings(
        self,
        receipts: Sequence[Mapping[str, Any]],
        questions: Sequence[ToolPermissionQuestion],
    ) -> list[ToolPermissionHandoffFinding]:
        findings: list[ToolPermissionHandoffFinding] = []
        denied_or_required = 0
        for receipt in receipts:
            result = receipt.get("bounded_result") if isinstance(receipt.get("bounded_result"), Mapping) else {}
            error = str(result.get("error") or "")
            if error in {"permission_required", "permission_denied"}:
                denied_or_required += 1
        if denied_or_required != len(questions):
            findings.append(
                _finding(
                    "TOOL_PERMISSION_QUESTION_COUNT_MISMATCH",
                    ToolPermissionHandoffSeverity.ERROR,
                    ToolPermissionHandoffSurface.RECEIPT,
                    "Permission receipt count and projected question count differ.",
                    receipt_permission_count=str(denied_or_required),
                    question_count=str(len(questions)),
                )
            )
        return findings

    def _context_findings(
        self,
        context_snapshots: Sequence[Mapping[str, Any]],
        questions: Sequence[ToolPermissionQuestion],
    ) -> list[ToolPermissionHandoffFinding]:
        findings: list[ToolPermissionHandoffFinding] = []
        if questions and not context_snapshots:
            findings.append(
                _finding(
                    "TOOL_PERMISSION_CONTEXT_MISSING",
                    ToolPermissionHandoffSeverity.BLOCKER,
                    ToolPermissionHandoffSurface.CONTEXT,
                    "Permission questions exist but ToolUseContext snapshots were not retained.",
                )
            )
        handoffs = _context_handoff_index(context_snapshots)
        for question in questions:
            if question.tool_call_id not in handoffs:
                findings.append(
                    _finding(
                        "TOOL_PERMISSION_CONTEXT_HANDOFF_MISSING",
                        ToolPermissionHandoffSeverity.WARNING,
                        ToolPermissionHandoffSurface.CONTEXT,
                        "A permission question has no matching ToolUseContext permission handoff.",
                        tool_call_id=question.tool_call_id,
                    )
                )
        return findings

    def _store_findings(self, questions: Sequence[ToolPermissionQuestion]) -> list[ToolPermissionHandoffFinding]:
        findings: list[ToolPermissionHandoffFinding] = []
        if any(question.pending for question in questions) and self.permission_store is None:
            findings.append(
                _finding(
                    "TOOL_PERMISSION_STORE_MISSING",
                    ToolPermissionHandoffSeverity.BLOCKER,
                    ToolPermissionHandoffSurface.STORE,
                    "Pending permission questions cannot be resolved because no JsonPermissionStore is attached.",
                )
            )
        for question in questions:
            if question.pending and not question.permission_request_id:
                findings.append(
                    _finding(
                        "TOOL_PERMISSION_REQUEST_ID_MISSING",
                        ToolPermissionHandoffSeverity.BLOCKER,
                        ToolPermissionHandoffSurface.STORE,
                        "Pending permission question has no store request id.",
                        tool_call_id=question.tool_call_id,
                    )
                )
        return findings


def tool_permission_handoff_metadata(report: ToolPermissionHandoffReport | None) -> dict[str, str]:
    if report is None:
        return {
            "tool_permission_handoff_questions": "0",
            "tool_permission_handoff_ok": "true",
        }
    return report.metadata()


def render_tool_permission_handoff_markdown(report: ToolPermissionHandoffReport) -> str:
    question_lines = [
        f"- `{question.tool_name}` `{question.tool_call_id}`: `{question.status}` {question.reason}"
        for question in report.questions
    ]
    finding_lines = [
        f"- `{finding.code}` [{finding.severity}/{finding.surface}]: {finding.message}"
        for finding in report.findings
    ]
    return "\n".join(
        [
            "## Tool Permission Handoff",
            "",
            f"- owner_unit: `{report.owner_unit}`",
            f"- ok: `{str(report.ok).lower()}`",
            f"- pending: `{report.pending_count}`",
            f"- resolved: `{report.resolved_count}`",
            "",
            "### Questions",
            "",
            *(question_lines or ["- no permission questions"]),
            "",
            "### Findings",
            "",
            *(finding_lines or ["- no findings"]),
        ]
    )


def _context_handoff_index(context_snapshots: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    handoffs: dict[str, Mapping[str, Any]] = {}
    for snapshot in context_snapshots:
        raw_handoffs = snapshot.get("permission_handoffs")
        if not isinstance(raw_handoffs, list):
            continue
        for handoff in raw_handoffs:
            if not isinstance(handoff, Mapping):
                continue
            tool_call_id = str(handoff.get("tool_call_id") or "")
            if tool_call_id:
                handoffs[tool_call_id] = handoff
    return handoffs


def _operation_for_tool(request: Mapping[str, Any]) -> str:
    tool_name = str(request.get("tool_name") or "")
    if tool_name in {"file_write", "file_edit"}:
        return "write"
    if tool_name == "file_read":
        return "read"
    if tool_name == "shell":
        return "shell"
    return "unknown"


def _subject_for_request(request: Mapping[str, Any]) -> str:
    arguments = request.get("arguments") if isinstance(request.get("arguments"), Mapping) else {}
    if "path" in arguments:
        return str(arguments.get("path") or "")
    if "command" in arguments:
        return str(arguments.get("command") or "")
    return ""


def _missing_question(question_id: str) -> ToolPermissionQuestion:
    return ToolPermissionQuestion(
        question_id=question_id,
        tool_call_id="",
        tool_name="",
        operation="",
        subject="",
        reason="missing question",
        status=ToolPermissionQuestionStatus.SKIPPED,
    )


def _finding(
    code: str,
    severity: ToolPermissionHandoffSeverity,
    surface: ToolPermissionHandoffSurface,
    message: str,
    **metadata: str,
) -> ToolPermissionHandoffFinding:
    return ToolPermissionHandoffFinding(
        code=code,
        severity=severity,
        surface=surface,
        message=message,
        metadata={str(key): str(value) for key, value in metadata.items()},
    )
