from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID


class ToolContinuationMode(StrEnum):
    INLINE_RESULT = "inline_result"
    EXTERNALIZED_RESULT = "externalized_result"
    ERROR_RESULT = "error_result"
    PERMISSION_HANDOFF = "permission_handoff"
    MISSING_RESULT = "missing_result"


class ToolContinuationStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class ToolContinuationFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class ToolContinuationFinding:
    code: str
    severity: ToolContinuationFindingSeverity
    message: str
    tool_call_id: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolContinuationFindingSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "blocking": self.blocking,
            "message": self.message,
            "tool_call_id": self.tool_call_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolContinuationPair:
    pair_id: str
    tool_call_id: str
    tool_name: str
    turn_index: int
    step_index: int
    ok: bool
    error: str
    mode: ToolContinuationMode
    result_message_appended: bool
    context_modifier_count: int
    artifact_refs: tuple[str, ...]
    budget_applied: bool
    permission_handoff: bool
    bounded_summary: str
    continuation_payload_chars: int
    source_path: str = "packages/runtime/zyra_runtime/tool_runtime_continuation.py"
    upstream_signal: str = "assistant tool_use -> tool_result -> continuation"
    created_at: str = field(default_factory=now_iso)

    @property
    def continuation_ready(self) -> bool:
        if self.mode == ToolContinuationMode.MISSING_RESULT:
            return False
        if self.permission_handoff:
            return True
        return self.result_message_appended

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair_id": self.pair_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "turn_index": self.turn_index,
            "step_index": self.step_index,
            "ok": self.ok,
            "error": self.error,
            "mode": str(self.mode),
            "result_message_appended": self.result_message_appended,
            "context_modifier_count": self.context_modifier_count,
            "artifact_refs": list(self.artifact_refs),
            "budget_applied": self.budget_applied,
            "permission_handoff": self.permission_handoff,
            "bounded_summary": self.bounded_summary,
            "continuation_payload_chars": self.continuation_payload_chars,
            "continuation_ready": self.continuation_ready,
            "source_path": self.source_path,
            "upstream_signal": self.upstream_signal,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ToolContinuationReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    pairs: tuple[ToolContinuationPair, ...]
    findings: tuple[ToolContinuationFinding, ...]
    context_snapshot_count: int
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ToolContinuationStatus:
        if any(finding.blocking for finding in self.findings):
            return ToolContinuationStatus.BLOCKED
        if self.findings:
            return ToolContinuationStatus.DEGRADED
        return ToolContinuationStatus.READY

    @property
    def pair_count(self) -> int:
        return len(self.pairs)

    @property
    def ready_count(self) -> int:
        return sum(1 for pair in self.pairs if pair.continuation_ready)

    @property
    def externalized_count(self) -> int:
        return sum(1 for pair in self.pairs if pair.mode == ToolContinuationMode.EXTERNALIZED_RESULT)

    @property
    def permission_handoff_count(self) -> int:
        return sum(1 for pair in self.pairs if pair.permission_handoff)

    @property
    def missing_message_count(self) -> int:
        return sum(1 for pair in self.pairs if not pair.result_message_appended and not pair.permission_handoff)

    @property
    def artifact_ref_count(self) -> int:
        return sum(len(pair.artifact_refs) for pair in self.pairs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "pair_count": self.pair_count,
            "ready_count": self.ready_count,
            "externalized_count": self.externalized_count,
            "permission_handoff_count": self.permission_handoff_count,
            "missing_message_count": self.missing_message_count,
            "artifact_ref_count": self.artifact_ref_count,
            "context_snapshot_count": self.context_snapshot_count,
            "pairs": [pair.to_dict() for pair in self.pairs],
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_continuation_report_id": self.report_id,
            "tool_continuation_owner_unit": self.owner_unit,
            "tool_continuation_ok": str(self.ok).lower(),
            "tool_continuation_status": str(self.status),
            "tool_continuation_pairs": str(self.pair_count),
            "tool_continuation_ready": str(self.ready_count),
            "tool_continuation_externalized": str(self.externalized_count),
            "tool_continuation_permission_handoffs": str(self.permission_handoff_count),
            "tool_continuation_missing_messages": str(self.missing_message_count),
            "tool_continuation_artifact_refs": str(self.artifact_ref_count),
            "tool_continuation_findings": str(len(self.findings)),
        }


class ToolContinuationRuntime:
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
    ) -> ToolContinuationReport:
        context_index = _ContextIndex.from_snapshots(context_snapshots)
        pairs = tuple(self._pairs(receipts, context_index))
        findings = tuple(self._findings(pairs, receipts))
        return ToolContinuationReport(
            report_id=new_id("toolcontinuation"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            pairs=pairs,
            findings=findings,
            context_snapshot_count=len(context_snapshots),
        )

    def event_for_report(
        self,
        report: ToolContinuationReport,
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
                    "phase": "tool_continuation_report",
                    "tool_continuation_report": report.to_dict(),
                }
            },
        )

    def _pairs(self, receipts: Sequence[Mapping[str, Any]], context_index: "_ContextIndex") -> list[ToolContinuationPair]:
        pairs: list[ToolContinuationPair] = []
        for receipt in receipts:
            request = receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}
            result = receipt.get("bounded_result") if isinstance(receipt.get("bounded_result"), Mapping) else {}
            decision = receipt.get("budget_decision") if isinstance(receipt.get("budget_decision"), Mapping) else {}
            budget_applied = decision.get("applied") is True
            tool_call_id = str(request.get("tool_call_id") or result.get("tool_call_id") or "")
            tool_name = str(request.get("tool_name") or "")
            error = str(result.get("error") or "")
            ok = result.get("ok") is True
            permission_handoff = error in {"permission_required", "permission_denied"} or context_index.has_permission(tool_call_id)
            mode = _continuation_mode(ok=ok, error=error, budget_applied=budget_applied, permission_handoff=permission_handoff)
            modifier_count = context_index.modifier_count(tool_call_id)
            artifacts = tuple(context_index.artifacts_for(tool_call_id) or _artifact_ids_from_result(result))
            pairs.append(
                ToolContinuationPair(
                    pair_id=new_id("toolpair"),
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    turn_index=_safe_int(request.get("turn_index")),
                    step_index=_safe_int(request.get("step_index")),
                    ok=ok,
                    error=error,
                    mode=mode,
                    result_message_appended=context_index.has_message(tool_call_id),
                    context_modifier_count=modifier_count,
                    artifact_refs=artifacts,
                    budget_applied=budget_applied,
                    permission_handoff=permission_handoff,
                    bounded_summary=str(result.get("summary") or ""),
                    continuation_payload_chars=_payload_chars(result),
                )
            )
        return pairs

    def _findings(
        self,
        pairs: Sequence[ToolContinuationPair],
        receipts: Sequence[Mapping[str, Any]],
    ) -> list[ToolContinuationFinding]:
        findings: list[ToolContinuationFinding] = []
        if receipts and not pairs:
            findings.append(
                ToolContinuationFinding(
                    code="TOOL_CONTINUATION_RECEIPTS_WITHOUT_PAIRS",
                    severity=ToolContinuationFindingSeverity.BLOCKER,
                    message="Tool receipts were present but no continuation pairs could be built.",
                )
            )
            return findings
        for pair in pairs:
            if not pair.tool_call_id:
                findings.append(
                    ToolContinuationFinding(
                        code="TOOL_CONTINUATION_PAIR_MISSING_TOOL_CALL_ID",
                        severity=ToolContinuationFindingSeverity.BLOCKER,
                        message="A continuation pair lacks a tool call id.",
                    )
                )
            if not pair.result_message_appended and not pair.permission_handoff:
                findings.append(
                    ToolContinuationFinding(
                        code="TOOL_RESULT_NOT_APPENDED_TO_CONTEXT",
                        severity=ToolContinuationFindingSeverity.BLOCKER,
                        message="A tool result receipt did not produce a message_append context modifier.",
                        tool_call_id=pair.tool_call_id,
                        metadata={"tool_name": pair.tool_name, "mode": str(pair.mode)},
                    )
                )
            if pair.budget_applied and not pair.artifact_refs:
                findings.append(
                    ToolContinuationFinding(
                        code="TOOL_BUDGET_CONTINUATION_WITHOUT_ARTIFACT_REF",
                        severity=ToolContinuationFindingSeverity.BLOCKER,
                        message="A budget-shaped tool result lacks an artifact reference for continuation.",
                        tool_call_id=pair.tool_call_id,
                        metadata={"tool_name": pair.tool_name},
                    )
                )
            if pair.mode == ToolContinuationMode.ERROR_RESULT and not pair.error:
                findings.append(
                    ToolContinuationFinding(
                        code="TOOL_ERROR_CONTINUATION_WITHOUT_ERROR_CODE",
                        severity=ToolContinuationFindingSeverity.WARNING,
                        message="A failed continuation pair lacks an error code.",
                        tool_call_id=pair.tool_call_id,
                    )
                )
        return findings


@dataclass(frozen=True, slots=True)
class _ContextIndex:
    messages_by_tool: Mapping[str, tuple[Mapping[str, Any], ...]]
    modifiers_by_tool: Mapping[str, tuple[Mapping[str, Any], ...]]
    artifacts_by_tool: Mapping[str, tuple[str, ...]]
    permissions_by_tool: Mapping[str, tuple[Mapping[str, Any], ...]]

    @classmethod
    def from_snapshots(cls, snapshots: Sequence[Mapping[str, Any]]) -> "_ContextIndex":
        messages: dict[str, list[Mapping[str, Any]]] = {}
        modifiers: dict[str, list[Mapping[str, Any]]] = {}
        artifacts: dict[str, list[str]] = {}
        permissions: dict[str, list[Mapping[str, Any]]] = {}
        for snapshot in snapshots:
            for modifier in _iter_mapping_list(snapshot.get("modifier_log")):
                tool_call_id = str(modifier.get("tool_call_id") or "")
                if not tool_call_id:
                    continue
                modifiers.setdefault(tool_call_id, []).append(modifier)
                kind = str(modifier.get("kind") or "")
                value = modifier.get("value") if isinstance(modifier.get("value"), Mapping) else {}
                if kind == "message_append":
                    messages.setdefault(tool_call_id, []).append(value)
                elif kind == "artifact_ref":
                    artifact_id = str(modifier.get("value") or "")
                    if artifact_id:
                        artifacts.setdefault(tool_call_id, []).append(artifact_id)
                elif kind == "permission_handoff":
                    permissions.setdefault(tool_call_id, []).append(value)
            for handoff in _iter_mapping_list(snapshot.get("permission_handoffs")):
                tool_call_id = str(handoff.get("tool_call_id") or "")
                if tool_call_id:
                    permissions.setdefault(tool_call_id, []).append(handoff)
            for budget in _iter_mapping_list(snapshot.get("budget_ledger")):
                tool_call_id = str(budget.get("tool_call_id") or "")
                artifact_id = str(budget.get("artifact_id") or "")
                if tool_call_id and artifact_id:
                    artifacts.setdefault(tool_call_id, []).append(artifact_id)
        return cls(
            messages_by_tool={key: tuple(value) for key, value in messages.items()},
            modifiers_by_tool={key: tuple(value) for key, value in modifiers.items()},
            artifacts_by_tool={key: tuple(dict.fromkeys(value)) for key, value in artifacts.items()},
            permissions_by_tool={key: tuple(value) for key, value in permissions.items()},
        )

    def has_message(self, tool_call_id: str) -> bool:
        return bool(self.messages_by_tool.get(tool_call_id))

    def has_permission(self, tool_call_id: str) -> bool:
        return bool(self.permissions_by_tool.get(tool_call_id))

    def modifier_count(self, tool_call_id: str) -> int:
        return len(self.modifiers_by_tool.get(tool_call_id, ()))

    def artifacts_for(self, tool_call_id: str) -> tuple[str, ...]:
        return self.artifacts_by_tool.get(tool_call_id, ())


def tool_continuation_metadata(report: ToolContinuationReport | None) -> dict[str, str]:
    if report is None:
        return {
            "tool_continuation_ok": "true",
            "tool_continuation_pairs": "0",
        }
    return report.metadata()


def render_tool_continuation_markdown(report: ToolContinuationReport) -> str:
    lines = [
        "## Tool Continuation Runtime",
        "",
        f"- owner_unit: `{report.owner_unit}`",
        f"- status: `{report.status}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- pairs: `{report.pair_count}`",
        f"- ready: `{report.ready_count}`",
        f"- externalized: `{report.externalized_count}`",
        f"- permission_handoffs: `{report.permission_handoff_count}`",
        "",
        "### Findings",
        "",
    ]
    if report.findings:
        lines.extend(f"- `{finding.code}` [{finding.severity}]: {finding.message}" for finding in report.findings)
    else:
        lines.append("- no findings")
    lines.extend(["", "### Pairs", ""])
    for pair in report.pairs:
        lines.append(
            f"- `{pair.tool_call_id}` `{pair.tool_name}`: mode `{pair.mode}`, "
            f"message `{str(pair.result_message_appended).lower()}`, artifacts `{len(pair.artifact_refs)}`"
        )
    return "\n".join(lines)


def _continuation_mode(
    *,
    ok: bool,
    error: str,
    budget_applied: bool,
    permission_handoff: bool,
) -> ToolContinuationMode:
    if permission_handoff:
        return ToolContinuationMode.PERMISSION_HANDOFF
    if budget_applied:
        return ToolContinuationMode.EXTERNALIZED_RESULT
    if ok:
        return ToolContinuationMode.INLINE_RESULT
    if error:
        return ToolContinuationMode.ERROR_RESULT
    return ToolContinuationMode.MISSING_RESULT


def _iter_mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _artifact_ids_from_result(result: Mapping[str, Any]) -> tuple[str, ...]:
    artifact_ids: list[str] = []
    for artifact in result.get("artifacts") or []:
        if isinstance(artifact, Mapping):
            artifact_id = str(artifact.get("artifact_id") or "")
            if artifact_id:
                artifact_ids.append(artifact_id)
    output = result.get("output") if isinstance(result.get("output"), Mapping) else {}
    output_artifact_id = str(output.get("full_output_artifact_id") or "")
    if output_artifact_id:
        artifact_ids.append(output_artifact_id)
    return tuple(dict.fromkeys(artifact_ids))


def _payload_chars(result: Mapping[str, Any]) -> int:
    try:
        return len(str(to_jsonable(result.get("output") or {})))
    except Exception:
        return 0


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
