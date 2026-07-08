from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .permissions import JsonPermissionStore, PermissionRequestStatus
from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID
from .tool_runtime_permission_handoff import ToolPermissionHandoffReport
from .tool_runtime_permission_session import ToolPermissionSessionReport


class ToolPermissionCheckpointStatus(StrEnum):
    READY = "ready"
    PENDING = "pending"
    EMPTY = "empty"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class ToolPermissionCheckpointKind(StrEnum):
    STORE_REQUEST = "store_request"
    HANDOFF_QUESTION = "handoff_question"
    SESSION_RECORD = "session_record"
    RECEIPT_PERMISSION = "receipt_permission"
    REPLAY_ACTION = "replay_action"


class ToolPermissionCheckpointAction(StrEnum):
    WAIT_FOR_REPLY = "wait_for_reply"
    REPLAY_AFTER_APPROVAL = "replay_after_approval"
    SKIP_AFTER_DENIAL = "skip_after_denial"
    NO_ACTION = "no_action"


class ToolPermissionCheckpointSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ToolPermissionCheckpointSurface(StrEnum):
    STORE = "store"
    HANDOFF = "handoff"
    SESSION = "session"
    RECEIPT = "receipt"
    REPLAY = "replay"


@dataclass(frozen=True, slots=True)
class ToolPermissionCheckpointEntry:
    entry_id: str
    kind: ToolPermissionCheckpointKind
    tool_call_id: str
    tool_name: str = ""
    permission_request_id: str = ""
    status: str = ""
    action: ToolPermissionCheckpointAction = ToolPermissionCheckpointAction.NO_ACTION
    subject: str = ""
    reason: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    @property
    def pending(self) -> bool:
        return self.status.endswith("pending") or self.action == ToolPermissionCheckpointAction.WAIT_FOR_REPLY

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "kind": str(self.kind),
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "permission_request_id": self.permission_request_id,
            "status": self.status,
            "action": str(self.action),
            "subject": self.subject,
            "reason": self.reason,
            "pending": self.pending,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ToolPermissionCheckpointFinding:
    code: str
    severity: ToolPermissionCheckpointSeverity
    surface: ToolPermissionCheckpointSurface
    message: str
    tool_call_id: str = ""
    permission_request_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolPermissionCheckpointSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "tool_call_id": self.tool_call_id,
            "permission_request_id": self.permission_request_id,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolPermissionCheckpointReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    entries: tuple[ToolPermissionCheckpointEntry, ...]
    findings: tuple[ToolPermissionCheckpointFinding, ...]
    store_path: str = ""
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ToolPermissionCheckpointStatus:
        if any(finding.blocking for finding in self.findings):
            return ToolPermissionCheckpointStatus.BLOCKED
        if any(entry.pending for entry in self.entries):
            return ToolPermissionCheckpointStatus.PENDING
        if not self.entries:
            return ToolPermissionCheckpointStatus.EMPTY
        if self.findings:
            return ToolPermissionCheckpointStatus.DEGRADED
        return ToolPermissionCheckpointStatus.READY

    @property
    def pending_count(self) -> int:
        return sum(1 for entry in self.entries if entry.pending)

    @property
    def store_request_count(self) -> int:
        return sum(1 for entry in self.entries if entry.kind == ToolPermissionCheckpointKind.STORE_REQUEST)

    @property
    def replay_action_count(self) -> int:
        return sum(1 for entry in self.entries if entry.kind == ToolPermissionCheckpointKind.REPLAY_ACTION)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.tool_permission_checkpoint.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "store_path": self.store_path,
            "ok": self.ok,
            "status": str(self.status),
            "entry_count": len(self.entries),
            "pending_count": self.pending_count,
            "store_request_count": self.store_request_count,
            "replay_action_count": self.replay_action_count,
            "entries": [entry.to_dict() for entry in self.entries],
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_permission_checkpoint_report_id": self.report_id,
            "tool_permission_checkpoint_owner_unit": self.owner_unit,
            "tool_permission_checkpoint_runtime_id": self.runtime_id,
            "tool_permission_checkpoint_ok": str(self.ok).lower(),
            "tool_permission_checkpoint_status": str(self.status),
            "tool_permission_checkpoint_entries": str(len(self.entries)),
            "tool_permission_checkpoint_pending": str(self.pending_count),
            "tool_permission_checkpoint_store_requests": str(self.store_request_count),
            "tool_permission_checkpoint_replay_actions": str(self.replay_action_count),
            "tool_permission_checkpoint_findings": str(len(self.findings)),
            "tool_permission_checkpoint_store_path": self.store_path,
        }


class ToolPermissionCheckpointRuntime:
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
        permission_store: JsonPermissionStore | None,
        handoff_report: ToolPermissionHandoffReport | None,
        permission_session_report: ToolPermissionSessionReport | None,
        receipts: Sequence[Mapping[str, Any]],
    ) -> ToolPermissionCheckpointReport:
        entries: list[ToolPermissionCheckpointEntry] = []
        entries.extend(self._store_entries(permission_store))
        entries.extend(self._handoff_entries(handoff_report))
        entries.extend(self._session_entries(permission_session_report))
        entries.extend(self._receipt_entries(receipts))
        entries.extend(self._replay_entries(entries))
        findings = self._findings(entries, permission_store, handoff_report, permission_session_report)
        return ToolPermissionCheckpointReport(
            report_id=new_id("toolpermcheckpoint"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            entries=tuple(entries),
            findings=tuple(findings),
            store_path=str(permission_store.path) if permission_store is not None else "",
        )

    def event_for_report(
        self,
        report: ToolPermissionCheckpointReport,
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
                    "phase": "tool_permission_checkpoint",
                    "tool_permission_checkpoint": report.to_dict(),
                }
            },
        )

    def _store_entries(self, permission_store: JsonPermissionStore | None) -> list[ToolPermissionCheckpointEntry]:
        if permission_store is None:
            return []
        entries: list[ToolPermissionCheckpointEntry] = []
        for request in permission_store.list_requests():
            entries.append(
                ToolPermissionCheckpointEntry(
                    entry_id=new_id("toolpermchkentry"),
                    kind=ToolPermissionCheckpointKind.STORE_REQUEST,
                    tool_call_id=request.tool_call_id,
                    permission_request_id=request.request_id,
                    status=str(request.status),
                    action=_action_for_status(request.status),
                    subject=request.subject,
                    reason=request.reason,
                    metadata={
                        "operation": str(request.operation),
                        "created_at": request.created_at,
                        "resolved_at": str(request.resolved_at or ""),
                    },
                )
            )
        return entries

    def _handoff_entries(self, report: ToolPermissionHandoffReport | None) -> list[ToolPermissionCheckpointEntry]:
        if report is None:
            return []
        entries: list[ToolPermissionCheckpointEntry] = []
        for question in report.questions:
            entries.append(
                ToolPermissionCheckpointEntry(
                    entry_id=new_id("toolpermchkentry"),
                    kind=ToolPermissionCheckpointKind.HANDOFF_QUESTION,
                    tool_call_id=question.tool_call_id,
                    tool_name=question.tool_name,
                    permission_request_id=question.permission_request_id,
                    status=str(question.status),
                    action=ToolPermissionCheckpointAction.WAIT_FOR_REPLY if question.pending else ToolPermissionCheckpointAction.NO_ACTION,
                    subject=question.subject,
                    reason=question.reason,
                    metadata={"question_id": question.question_id},
                )
            )
        return entries

    def _session_entries(self, report: ToolPermissionSessionReport | None) -> list[ToolPermissionCheckpointEntry]:
        if report is None:
            return []
        entries: list[ToolPermissionCheckpointEntry] = []
        for record in report.records:
            entries.append(
                ToolPermissionCheckpointEntry(
                    entry_id=new_id("toolpermchkentry"),
                    kind=ToolPermissionCheckpointKind.SESSION_RECORD,
                    tool_call_id=record.tool_call_id,
                    tool_name=record.tool_name,
                    permission_request_id=record.permission_request_id,
                    status=record.status,
                    action=ToolPermissionCheckpointAction.WAIT_FOR_REPLY if record.pending else ToolPermissionCheckpointAction.NO_ACTION,
                    subject=record.metadata.get("subject", ""),
                    reason=record.metadata.get("reason", ""),
                    metadata={"record_id": record.record_id, "record_kind": str(record.kind)},
                )
            )
        return entries

    def _receipt_entries(self, receipts: Sequence[Mapping[str, Any]]) -> list[ToolPermissionCheckpointEntry]:
        entries: list[ToolPermissionCheckpointEntry] = []
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                continue
            request = receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}
            result = receipt.get("bounded_result") if isinstance(receipt.get("bounded_result"), Mapping) else {}
            metadata = result.get("metadata") if isinstance(result.get("metadata"), Mapping) else {}
            error = str(result.get("error") or "")
            permission_effect = str(metadata.get("permission_effect") or "")
            if error not in {"permission_required", "permission_denied"} and permission_effect not in {"ask", "deny"}:
                continue
            entries.append(
                ToolPermissionCheckpointEntry(
                    entry_id=new_id("toolpermchkentry"),
                    kind=ToolPermissionCheckpointKind.RECEIPT_PERMISSION,
                    tool_call_id=str(request.get("tool_call_id") or result.get("tool_call_id") or ""),
                    tool_name=str(request.get("tool_name") or ""),
                    permission_request_id=str(metadata.get("permission_request_id") or ""),
                    status=error or permission_effect,
                    action=ToolPermissionCheckpointAction.WAIT_FOR_REPLY if error == "permission_required" else ToolPermissionCheckpointAction.SKIP_AFTER_DENIAL,
                    subject=str(metadata.get("permission_subject") or ""),
                    reason=str(metadata.get("permission_reason") or ""),
                    metadata={"permission_effect": permission_effect, "error": error},
                )
            )
        return entries

    def _replay_entries(self, entries: Sequence[ToolPermissionCheckpointEntry]) -> list[ToolPermissionCheckpointEntry]:
        output: list[ToolPermissionCheckpointEntry] = []
        by_tool: dict[str, list[ToolPermissionCheckpointEntry]] = {}
        for entry in entries:
            if entry.tool_call_id:
                by_tool.setdefault(entry.tool_call_id, []).append(entry)
        for tool_call_id, grouped in sorted(by_tool.items()):
            pending = any(entry.pending for entry in grouped)
            denied = any(entry.status.endswith("denied") or entry.status == "permission_denied" for entry in grouped)
            approved = any(entry.status.endswith("approved") for entry in grouped)
            if pending:
                action = ToolPermissionCheckpointAction.WAIT_FOR_REPLY
            elif approved:
                action = ToolPermissionCheckpointAction.REPLAY_AFTER_APPROVAL
            elif denied:
                action = ToolPermissionCheckpointAction.SKIP_AFTER_DENIAL
            else:
                action = ToolPermissionCheckpointAction.NO_ACTION
            output.append(
                ToolPermissionCheckpointEntry(
                    entry_id=new_id("toolpermchkentry"),
                    kind=ToolPermissionCheckpointKind.REPLAY_ACTION,
                    tool_call_id=tool_call_id,
                    tool_name=next((entry.tool_name for entry in grouped if entry.tool_name), ""),
                    permission_request_id=next((entry.permission_request_id for entry in grouped if entry.permission_request_id), ""),
                    status=str(action),
                    action=action,
                    metadata={"source_entry_count": str(len(grouped))},
                )
            )
        return output

    def _findings(
        self,
        entries: Sequence[ToolPermissionCheckpointEntry],
        permission_store: JsonPermissionStore | None,
        handoff_report: ToolPermissionHandoffReport | None,
        permission_session_report: ToolPermissionSessionReport | None,
    ) -> list[ToolPermissionCheckpointFinding]:
        findings: list[ToolPermissionCheckpointFinding] = []
        pending_entries = [entry for entry in entries if entry.pending]
        if pending_entries and permission_store is None:
            findings.append(
                ToolPermissionCheckpointFinding(
                    code="PERMISSION_CHECKPOINT_PENDING_WITHOUT_STORE",
                    severity=ToolPermissionCheckpointSeverity.BLOCKER,
                    surface=ToolPermissionCheckpointSurface.STORE,
                    message="Pending permission entries require a JsonPermissionStore checkpoint.",
                )
            )
        handoff_ids = {question.permission_request_id for question in (handoff_report.questions if handoff_report else ()) if question.permission_request_id}
        store_ids = {entry.permission_request_id for entry in entries if entry.kind == ToolPermissionCheckpointKind.STORE_REQUEST}
        missing = sorted(handoff_ids - store_ids)
        for request_id in missing:
            findings.append(
                ToolPermissionCheckpointFinding(
                    code="PERMISSION_CHECKPOINT_STORE_REQUEST_MISSING",
                    severity=ToolPermissionCheckpointSeverity.BLOCKER,
                    surface=ToolPermissionCheckpointSurface.STORE,
                    message="A handoff permission request id was not found in the permission store checkpoint.",
                    permission_request_id=request_id,
                )
            )
        if handoff_report and handoff_report.questions and permission_session_report and not permission_session_report.records:
            findings.append(
                ToolPermissionCheckpointFinding(
                    code="PERMISSION_CHECKPOINT_SESSION_RECORD_MISSING",
                    severity=ToolPermissionCheckpointSeverity.BLOCKER,
                    surface=ToolPermissionCheckpointSurface.SESSION,
                    message="Permission handoff questions did not produce session checkpoint records.",
                )
            )
        return findings


def tool_permission_checkpoint_metadata(report: ToolPermissionCheckpointReport | None) -> dict[str, str]:
    if report is None:
        return {"tool_permission_checkpoint_ok": "false", "tool_permission_checkpoint_entries": "0"}
    return report.metadata()


def assert_tool_permission_checkpoint_ready(report: ToolPermissionCheckpointReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.findings if finding.blocking)
    raise AssertionError(f"tool permission checkpoint blocked: {blockers or 'unknown'}")


def render_tool_permission_checkpoint_markdown(report: ToolPermissionCheckpointReport) -> str:
    lines = [
        "## Tool Permission Checkpoint",
        "",
        f"- status: `{report.status}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- entries: `{len(report.entries)}`",
        f"- pending: `{report.pending_count}`",
        f"- store_requests: `{report.store_request_count}`",
        "",
        "### Replay Actions",
        "",
    ]
    for entry in report.entries:
        if entry.kind == ToolPermissionCheckpointKind.REPLAY_ACTION:
            lines.append(f"- `{entry.tool_call_id}` -> `{entry.action}`")
    lines.extend(["", "### Findings", ""])
    if report.findings:
        lines.extend(f"- `{finding.code}` [{finding.severity}]: {finding.message}" for finding in report.findings)
    else:
        lines.append("- no findings")
    return "\n".join(lines)


def _action_for_status(status: PermissionRequestStatus) -> ToolPermissionCheckpointAction:
    if status == PermissionRequestStatus.PENDING:
        return ToolPermissionCheckpointAction.WAIT_FOR_REPLY
    if status == PermissionRequestStatus.APPROVED:
        return ToolPermissionCheckpointAction.REPLAY_AFTER_APPROVAL
    if status == PermissionRequestStatus.DENIED:
        return ToolPermissionCheckpointAction.SKIP_AFTER_DENIAL
    return ToolPermissionCheckpointAction.NO_ACTION
