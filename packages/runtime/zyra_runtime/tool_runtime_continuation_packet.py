from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .tool_runtime_budget_chain import ToolBudgetChainReport
from .tool_runtime_execution_timeline import ToolExecutionTimelineReport
from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID
from .tool_runtime_permission_checkpoint import ToolPermissionCheckpointReport
from .tool_runtime_result_context import ToolResultContextReport
from .tool_runtime_session_bridge import ToolSessionBridgeReport


class ToolContinuationPacketStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    EMPTY = "empty"
    BLOCKED = "blocked"


class ToolContinuationPacketItemKind(StrEnum):
    TOOL_RESULT_MESSAGE = "tool_result_message"
    ARTIFACT_POINTER = "artifact_pointer"
    BUDGET_SUMMARY = "budget_summary"
    PERMISSION_CHECKPOINT = "permission_checkpoint"
    TIMELINE_SUMMARY = "timeline_summary"
    SESSION_BRIDGE_SUMMARY = "session_bridge_summary"


class ToolContinuationPacketSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ToolContinuationPacketSurface(StrEnum):
    RESULT_CONTEXT = "result_context"
    ARTIFACT = "artifact"
    BUDGET = "budget"
    PERMISSION = "permission"
    TIMELINE = "timeline"
    SESSION_BRIDGE = "session_bridge"
    PACKET = "packet"


@dataclass(frozen=True, slots=True)
class ToolContinuationPacketItem:
    item_id: str
    kind: ToolContinuationPacketItemKind
    key: str
    value: dict[str, Any]
    tool_call_id: str = ""
    token_estimate: int = 0
    required_for_next_turn: bool = True
    metadata: dict[str, str] = field(default_factory=dict)

    def to_message(self) -> dict[str, Any]:
        return {
            "role": "tool" if self.kind == ToolContinuationPacketItemKind.TOOL_RESULT_MESSAGE else "system",
            "content": self.value,
            "metadata": {
                "continuation_item_id": self.item_id,
                "continuation_kind": str(self.kind),
                "tool_call_id": self.tool_call_id,
                **dict(self.metadata),
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "kind": str(self.kind),
            "key": self.key,
            "value": to_jsonable(self.value),
            "tool_call_id": self.tool_call_id,
            "token_estimate": self.token_estimate,
            "required_for_next_turn": self.required_for_next_turn,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolContinuationPacketFinding:
    code: str
    severity: ToolContinuationPacketSeverity
    surface: ToolContinuationPacketSurface
    message: str
    item_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolContinuationPacketSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "item_id": self.item_id,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolContinuationPacketReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    items: tuple[ToolContinuationPacketItem, ...]
    findings: tuple[ToolContinuationPacketFinding, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ToolContinuationPacketStatus:
        if any(finding.blocking for finding in self.findings):
            return ToolContinuationPacketStatus.BLOCKED
        if not self.items:
            return ToolContinuationPacketStatus.EMPTY
        if self.findings:
            return ToolContinuationPacketStatus.DEGRADED
        return ToolContinuationPacketStatus.READY

    @property
    def message_count(self) -> int:
        return sum(1 for item in self.items if item.kind == ToolContinuationPacketItemKind.TOOL_RESULT_MESSAGE)

    @property
    def artifact_count(self) -> int:
        return sum(1 for item in self.items if item.kind == ToolContinuationPacketItemKind.ARTIFACT_POINTER)

    @property
    def budget_count(self) -> int:
        return sum(1 for item in self.items if item.kind == ToolContinuationPacketItemKind.BUDGET_SUMMARY)

    @property
    def permission_count(self) -> int:
        return sum(1 for item in self.items if item.kind == ToolContinuationPacketItemKind.PERMISSION_CHECKPOINT)

    @property
    def token_estimate(self) -> int:
        return sum(item.token_estimate for item in self.items)

    def to_messages(self) -> list[dict[str, Any]]:
        return [item.to_message() for item in self.items if item.required_for_next_turn]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.tool_continuation_packet.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "item_count": len(self.items),
            "message_count": self.message_count,
            "artifact_count": self.artifact_count,
            "budget_count": self.budget_count,
            "permission_count": self.permission_count,
            "token_estimate": self.token_estimate,
            "items": [item.to_dict() for item in self.items],
            "messages": [to_jsonable(message) for message in self.to_messages()],
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_continuation_packet_report_id": self.report_id,
            "tool_continuation_packet_owner_unit": self.owner_unit,
            "tool_continuation_packet_runtime_id": self.runtime_id,
            "tool_continuation_packet_ok": str(self.ok).lower(),
            "tool_continuation_packet_status": str(self.status),
            "tool_continuation_packet_items": str(len(self.items)),
            "tool_continuation_packet_messages": str(self.message_count),
            "tool_continuation_packet_artifacts": str(self.artifact_count),
            "tool_continuation_packet_budgets": str(self.budget_count),
            "tool_continuation_packet_permissions": str(self.permission_count),
            "tool_continuation_packet_token_estimate": str(self.token_estimate),
            "tool_continuation_packet_findings": str(len(self.findings)),
        }


class ToolContinuationPacketRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID,
        max_item_chars: int = 1200,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.max_item_chars = max(1, int(max_item_chars))

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        result_context_report: ToolResultContextReport | None,
        budget_chain_report: ToolBudgetChainReport | None,
        permission_checkpoint_report: ToolPermissionCheckpointReport | None,
        timeline_report: ToolExecutionTimelineReport | None,
        session_bridge_report: ToolSessionBridgeReport | None,
    ) -> ToolContinuationPacketReport:
        items: list[ToolContinuationPacketItem] = []
        items.extend(self._result_items(result_context_report))
        items.extend(self._budget_items(budget_chain_report))
        items.extend(self._permission_items(permission_checkpoint_report))
        items.extend(self._timeline_items(timeline_report))
        items.extend(self._session_bridge_items(session_bridge_report))
        findings = self._findings(items, result_context_report, budget_chain_report, permission_checkpoint_report)
        return ToolContinuationPacketReport(
            report_id=new_id("toolcontpacket"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            items=tuple(items),
            findings=tuple(findings),
        )

    def event_for_report(
        self,
        report: ToolContinuationPacketReport,
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
                    "phase": "tool_continuation_packet",
                    "tool_continuation_packet": report.to_dict(),
                }
            },
        )

    def _result_items(self, report: ToolResultContextReport | None) -> list[ToolContinuationPacketItem]:
        if report is None:
            return []
        items: list[ToolContinuationPacketItem] = []
        for projection in report.projections:
            message = projection.session_message.to_dict()
            content = {
                "tool_call_id": projection.tool_call_id,
                "tool_name": projection.tool_name,
                "ok": projection.ok,
                "error": projection.error,
                "content": _truncate(str(message.get("content") or ""), self.max_item_chars),
                "artifact_ids": list(projection.artifact_ids),
                "externalized_artifact_id": projection.externalized_artifact_id,
                "budget_applied": projection.budget_applied,
            }
            items.append(
                ToolContinuationPacketItem(
                    item_id=new_id("toolcontitem"),
                    kind=ToolContinuationPacketItemKind.TOOL_RESULT_MESSAGE,
                    key=f"tool_result:{projection.tool_call_id}",
                    value=content,
                    tool_call_id=projection.tool_call_id,
                    token_estimate=_token_estimate(content),
                    required_for_next_turn=True,
                    metadata={"projection_id": projection.projection_id},
                )
            )
            for artifact_id in projection.artifact_ids:
                items.append(
                    ToolContinuationPacketItem(
                        item_id=new_id("toolcontitem"),
                        kind=ToolContinuationPacketItemKind.ARTIFACT_POINTER,
                        key=f"artifact:{artifact_id}",
                        value={
                            "artifact_id": artifact_id,
                            "tool_call_id": projection.tool_call_id,
                            "tool_name": projection.tool_name,
                            "externalized": projection.externalized,
                            "raw_output_blocked": projection.raw_output_blocked,
                        },
                        tool_call_id=projection.tool_call_id,
                        token_estimate=16,
                        required_for_next_turn=projection.externalized or projection.raw_output_blocked,
                    )
                )
        return items

    def _budget_items(self, report: ToolBudgetChainReport | None) -> list[ToolContinuationPacketItem]:
        if report is None:
            return []
        if report.budgeted_count == 0:
            return [
                ToolContinuationPacketItem(
                    item_id=new_id("toolcontitem"),
                    kind=ToolContinuationPacketItemKind.BUDGET_SUMMARY,
                    key="budget:clean",
                    value={"budgeted_count": 0, "artifact_count": report.artifact_count, "status": str(report.status)},
                    token_estimate=16,
                    required_for_next_turn=False,
                    metadata={"budget_chain_report_id": report.report_id},
                )
            ]
        items: list[ToolContinuationPacketItem] = []
        for node in report.nodes:
            if node.kind.value != "budget_decision" or not node.externalized:
                continue
            items.append(
                ToolContinuationPacketItem(
                    item_id=new_id("toolcontitem"),
                    kind=ToolContinuationPacketItemKind.BUDGET_SUMMARY,
                    key=f"budget:{node.tool_call_id}",
                    value={
                        "tool_call_id": node.tool_call_id,
                        "tool_name": node.tool_name,
                        "artifact_id": node.artifact_id,
                        "original_chars": node.chars,
                        "reason": node.metadata.get("reason", ""),
                    },
                    tool_call_id=node.tool_call_id,
                    token_estimate=32,
                    required_for_next_turn=True,
                    metadata={"budget_chain_report_id": report.report_id},
                )
            )
        return items

    def _permission_items(self, report: ToolPermissionCheckpointReport | None) -> list[ToolContinuationPacketItem]:
        if report is None:
            return []
        items: list[ToolContinuationPacketItem] = []
        for entry in report.entries:
            if entry.kind.value != "replay_action":
                continue
            items.append(
                ToolContinuationPacketItem(
                    item_id=new_id("toolcontitem"),
                    kind=ToolContinuationPacketItemKind.PERMISSION_CHECKPOINT,
                    key=f"permission:{entry.tool_call_id}",
                    value={
                        "tool_call_id": entry.tool_call_id,
                        "tool_name": entry.tool_name,
                        "permission_request_id": entry.permission_request_id,
                        "status": entry.status,
                        "action": str(entry.action),
                    },
                    tool_call_id=entry.tool_call_id,
                    token_estimate=32,
                    required_for_next_turn=entry.pending,
                    metadata={"permission_checkpoint_report_id": report.report_id},
                )
            )
        return items

    def _timeline_items(self, report: ToolExecutionTimelineReport | None) -> list[ToolContinuationPacketItem]:
        if report is None:
            return []
        return [
            ToolContinuationPacketItem(
                item_id=new_id("toolcontitem"),
                kind=ToolContinuationPacketItemKind.TIMELINE_SUMMARY,
                key="timeline:summary",
                value={
                    "timeline_report_id": report.report_id,
                    "status": str(report.status),
                    "tool_call_count": report.tool_call_count,
                    "batch_count": len(report.batch_windows),
                    "concurrent_batch_count": report.concurrent_batch_count,
                    "serial_batch_count": report.serial_batch_count,
                },
                token_estimate=32,
                required_for_next_turn=False,
            )
        ]

    def _session_bridge_items(self, report: ToolSessionBridgeReport | None) -> list[ToolContinuationPacketItem]:
        if report is None:
            return []
        return [
            ToolContinuationPacketItem(
                item_id=new_id("toolcontitem"),
                kind=ToolContinuationPacketItemKind.SESSION_BRIDGE_SUMMARY,
                key="session_bridge:summary",
                value={
                    "bridge_report_id": report.report_id,
                    "origin": str(report.origin),
                    "valid_tool_use_count": report.valid_tool_use_count,
                    "opencode_tool_use_count": report.opencode_tool_use_count,
                    "fallback_used": report.fallback_used,
                },
                token_estimate=32,
                required_for_next_turn=False,
            )
        ]

    def _findings(
        self,
        items: Sequence[ToolContinuationPacketItem],
        result_context_report: ToolResultContextReport | None,
        budget_chain_report: ToolBudgetChainReport | None,
        permission_checkpoint_report: ToolPermissionCheckpointReport | None,
    ) -> list[ToolContinuationPacketFinding]:
        findings: list[ToolContinuationPacketFinding] = []
        result_count = result_context_report.projection_count if result_context_report is not None else 0
        message_count = sum(1 for item in items if item.kind == ToolContinuationPacketItemKind.TOOL_RESULT_MESSAGE)
        if result_count and message_count < result_count:
            findings.append(
                ToolContinuationPacketFinding(
                    code="TOOL_CONTINUATION_PACKET_MISSING_RESULT_MESSAGES",
                    severity=ToolContinuationPacketSeverity.BLOCKER,
                    surface=ToolContinuationPacketSurface.RESULT_CONTEXT,
                    message="Continuation packet does not include all projected tool result messages.",
                    metadata={"result_projections": str(result_count), "packet_messages": str(message_count)},
                )
            )
        if budget_chain_report is not None and budget_chain_report.budgeted_count and not any(item.kind == ToolContinuationPacketItemKind.BUDGET_SUMMARY and item.required_for_next_turn for item in items):
            findings.append(
                ToolContinuationPacketFinding(
                    code="TOOL_CONTINUATION_PACKET_MISSING_BUDGET_SUMMARY",
                    severity=ToolContinuationPacketSeverity.BLOCKER,
                    surface=ToolContinuationPacketSurface.BUDGET,
                    message="Budgeted results require a next-turn budget summary.",
                )
            )
        if permission_checkpoint_report is not None and permission_checkpoint_report.pending_count and not any(item.kind == ToolContinuationPacketItemKind.PERMISSION_CHECKPOINT and item.required_for_next_turn for item in items):
            findings.append(
                ToolContinuationPacketFinding(
                    code="TOOL_CONTINUATION_PACKET_MISSING_PERMISSION_CHECKPOINT",
                    severity=ToolContinuationPacketSeverity.BLOCKER,
                    surface=ToolContinuationPacketSurface.PERMISSION,
                    message="Pending permission checkpoint was not carried into continuation packet.",
                )
            )
        return findings


def tool_continuation_packet_metadata(report: ToolContinuationPacketReport | None) -> dict[str, str]:
    if report is None:
        return {"tool_continuation_packet_ok": "false", "tool_continuation_packet_items": "0"}
    return report.metadata()


def assert_tool_continuation_packet_ready(report: ToolContinuationPacketReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.findings if finding.blocking)
    raise AssertionError(f"tool continuation packet blocked: {blockers or 'unknown'}")


def render_tool_continuation_packet_markdown(report: ToolContinuationPacketReport) -> str:
    lines = [
        "## Tool Continuation Packet",
        "",
        f"- status: `{report.status}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- items: `{len(report.items)}`",
        f"- messages: `{report.message_count}`",
        f"- artifacts: `{report.artifact_count}`",
        f"- permissions: `{report.permission_count}`",
        "",
        "### Items",
        "",
    ]
    for item in report.items:
        lines.append(f"- `{item.kind}` `{item.key}` required `{str(item.required_for_next_turn).lower()}`")
    lines.extend(["", "### Findings", ""])
    if report.findings:
        lines.extend(f"- `{finding.code}` [{finding.severity}]: {finding.message}" for finding in report.findings)
    else:
        lines.append("- no findings")
    return "\n".join(lines)


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


def _token_estimate(value: Mapping[str, Any]) -> int:
    return max(1, len(str(to_jsonable(value))) // 4)
