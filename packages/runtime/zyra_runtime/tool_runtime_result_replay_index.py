from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .tool_runtime_budget_chain import ToolBudgetChainReport
from .tool_runtime_continuation_packet import ToolContinuationPacketReport
from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID
from .tool_runtime_output_store import ToolOutputStoreSnapshot
from .tool_runtime_result_context import ToolResultContextReport


class ToolResultReplayEntryKind(StrEnum):
    RECEIPT_RAW_RESULT = "receipt_raw_result"
    RECEIPT_BOUNDED_RESULT = "receipt_bounded_result"
    RESULT_CONTEXT_MESSAGE = "result_context_message"
    RESULT_CONTEXT_ARTIFACT = "result_context_artifact"
    OUTPUT_STORE_ENTRY = "output_store_entry"
    BUDGET_CHAIN_NODE = "budget_chain_node"
    CONTINUATION_PACKET_ITEM = "continuation_packet_item"


class ToolResultReplayStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    EMPTY = "empty"
    BLOCKED = "blocked"


class ToolResultReplaySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ToolResultReplaySurface(StrEnum):
    RECEIPT = "receipt"
    RESULT_CONTEXT = "result_context"
    OUTPUT_STORE = "output_store"
    BUDGET_CHAIN = "budget_chain"
    CONTINUATION_PACKET = "continuation_packet"


@dataclass(frozen=True, slots=True)
class ToolResultReplayEntry:
    entry_id: str
    kind: ToolResultReplayEntryKind
    replay_key: str
    tool_call_id: str = ""
    tool_name: str = ""
    artifact_id: str = ""
    payload_ref: str = ""
    chars: int = 0
    checksum: str = ""
    next_turn_visible: bool = False
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def externalized(self) -> bool:
        return bool(self.artifact_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "kind": str(self.kind),
            "replay_key": self.replay_key,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "artifact_id": self.artifact_id,
            "payload_ref": self.payload_ref,
            "chars": self.chars,
            "checksum": self.checksum,
            "externalized": self.externalized,
            "next_turn_visible": self.next_turn_visible,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolResultReplayFinding:
    code: str
    severity: ToolResultReplaySeverity
    surface: ToolResultReplaySurface
    message: str
    tool_call_id: str = ""
    replay_key: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolResultReplaySeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "tool_call_id": self.tool_call_id,
            "replay_key": self.replay_key,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolResultReplayIndexReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    entries: tuple[ToolResultReplayEntry, ...]
    findings: tuple[ToolResultReplayFinding, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ToolResultReplayStatus:
        if any(finding.blocking for finding in self.findings):
            return ToolResultReplayStatus.BLOCKED
        if not self.entries:
            return ToolResultReplayStatus.EMPTY
        if self.findings:
            return ToolResultReplayStatus.DEGRADED
        return ToolResultReplayStatus.READY

    @property
    def tool_call_count(self) -> int:
        return len({entry.tool_call_id for entry in self.entries if entry.tool_call_id})

    @property
    def artifact_count(self) -> int:
        return len({entry.artifact_id for entry in self.entries if entry.artifact_id})

    @property
    def visible_count(self) -> int:
        return sum(1 for entry in self.entries if entry.next_turn_visible)

    @property
    def externalized_count(self) -> int:
        return sum(1 for entry in self.entries if entry.externalized)

    def entries_for_tool_call(self, tool_call_id: str) -> tuple[ToolResultReplayEntry, ...]:
        return tuple(entry for entry in self.entries if entry.tool_call_id == tool_call_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.tool_result_replay_index.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "entry_count": len(self.entries),
            "tool_call_count": self.tool_call_count,
            "artifact_count": self.artifact_count,
            "visible_count": self.visible_count,
            "externalized_count": self.externalized_count,
            "findings": [finding.to_dict() for finding in self.findings],
            "entries": [entry.to_dict() for entry in self.entries],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_result_replay_index_report_id": self.report_id,
            "tool_result_replay_index_owner_unit": self.owner_unit,
            "tool_result_replay_index_runtime_id": self.runtime_id,
            "tool_result_replay_index_ok": str(self.ok).lower(),
            "tool_result_replay_index_status": str(self.status),
            "tool_result_replay_index_entries": str(len(self.entries)),
            "tool_result_replay_index_tool_calls": str(self.tool_call_count),
            "tool_result_replay_index_artifacts": str(self.artifact_count),
            "tool_result_replay_index_visible": str(self.visible_count),
            "tool_result_replay_index_externalized": str(self.externalized_count),
            "tool_result_replay_index_findings": str(len(self.findings)),
        }


class ToolResultReplayIndexRuntime:
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
        output_store_snapshot: ToolOutputStoreSnapshot | None,
        result_context_report: ToolResultContextReport | None,
        budget_chain_report: ToolBudgetChainReport | None,
        continuation_packet_report: ToolContinuationPacketReport | None,
    ) -> ToolResultReplayIndexReport:
        entries: list[ToolResultReplayEntry] = []
        entries.extend(self._receipt_entries(receipts))
        entries.extend(self._output_store_entries(output_store_snapshot))
        entries.extend(self._result_context_entries(result_context_report))
        entries.extend(self._budget_chain_entries(budget_chain_report))
        entries.extend(self._continuation_packet_entries(continuation_packet_report))
        entries = _dedupe_entries(entries)
        findings = self._findings(entries, receipts, result_context_report, continuation_packet_report)
        return ToolResultReplayIndexReport(
            report_id=new_id("toolreplayindex"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            entries=tuple(entries),
            findings=tuple(findings),
        )

    def event_for_report(
        self,
        report: ToolResultReplayIndexReport,
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
                    "phase": "tool_result_replay_index",
                    "tool_result_replay_index": report.to_dict(),
                }
            },
        )

    def _receipt_entries(self, receipts: Sequence[Mapping[str, Any]]) -> list[ToolResultReplayEntry]:
        entries: list[ToolResultReplayEntry] = []
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                continue
            request = receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}
            raw_result = receipt.get("raw_result") if isinstance(receipt.get("raw_result"), Mapping) else {}
            bounded_result = receipt.get("bounded_result") if isinstance(receipt.get("bounded_result"), Mapping) else {}
            decision = receipt.get("budget_decision") if isinstance(receipt.get("budget_decision"), Mapping) else {}
            tool_call_id = str(request.get("tool_call_id") or bounded_result.get("tool_call_id") or raw_result.get("tool_call_id") or "")
            tool_name = str(request.get("tool_name") or "")
            entries.append(
                self._entry(
                    ToolResultReplayEntryKind.RECEIPT_RAW_RESULT,
                    replay_key=f"receipt:{tool_call_id}:raw",
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    payload=raw_result,
                    payload_ref="receipt.raw_result",
                    metadata={"ok": str(raw_result.get("ok") is True).lower(), "source": "ToolExecutionReceipt"},
                )
            )
            entries.append(
                self._entry(
                    ToolResultReplayEntryKind.RECEIPT_BOUNDED_RESULT,
                    replay_key=f"receipt:{tool_call_id}:bounded",
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    artifact_id=str(decision.get("artifact_id") or _full_output_artifact_id(bounded_result) or ""),
                    payload=bounded_result,
                    payload_ref="receipt.bounded_result",
                    next_turn_visible=True,
                    metadata={
                        "ok": str(bounded_result.get("ok") is True).lower(),
                        "budget_applied": str(decision.get("applied") is True).lower(),
                    },
                )
            )
        return entries

    def _output_store_entries(self, snapshot: ToolOutputStoreSnapshot | None) -> list[ToolResultReplayEntry]:
        if snapshot is None:
            return []
        payload = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        entries = payload.get("entries") if isinstance(payload, Mapping) and isinstance(payload.get("entries"), Sequence) else ()
        output: list[ToolResultReplayEntry] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            tool_call_id = str(entry.get("tool_call_id") or "")
            output.append(
                self._entry(
                    ToolResultReplayEntryKind.OUTPUT_STORE_ENTRY,
                    replay_key=f"output_store:{tool_call_id}:{entry.get('entry_id') or entry.get('artifact_id') or 'inline'}",
                    tool_call_id=tool_call_id,
                    tool_name=str(entry.get("tool_name") or ""),
                    artifact_id=str(entry.get("artifact_id") or ""),
                    payload=entry,
                    payload_ref="tool_output_store.entries",
                    next_turn_visible=True,
                    metadata={"kind": str(entry.get("kind") or ""), "source": "ToolOutputStoreRuntime"},
                )
            )
        return output

    def _result_context_entries(self, report: ToolResultContextReport | None) -> list[ToolResultReplayEntry]:
        if report is None:
            return []
        output: list[ToolResultReplayEntry] = []
        for projection in report.projections:
            projection_payload = projection.to_dict() if hasattr(projection, "to_dict") else projection
            if not isinstance(projection_payload, Mapping):
                continue
            tool_call_id = str(projection_payload.get("tool_call_id") or "")
            output.append(
                self._entry(
                    ToolResultReplayEntryKind.RESULT_CONTEXT_MESSAGE,
                    replay_key=f"result_context:{tool_call_id}:{projection_payload.get('projection_id') or 'message'}",
                    tool_call_id=tool_call_id,
                    tool_name=str(projection_payload.get("tool_name") or ""),
                    payload=projection_payload,
                    payload_ref="tool_result_context.projections",
                    next_turn_visible=projection_payload.get("visible_to_next_turn") is True,
                    metadata={
                        "kind": str(projection_payload.get("kind") or ""),
                        "externalized": str(projection_payload.get("externalized") is True).lower(),
                    },
                )
            )
            for artifact_id in _artifact_ids_from_projection(projection_payload):
                output.append(
                    self._entry(
                        ToolResultReplayEntryKind.RESULT_CONTEXT_ARTIFACT,
                        replay_key=f"result_context:{tool_call_id}:artifact:{artifact_id}",
                        tool_call_id=tool_call_id,
                        tool_name=str(projection_payload.get("tool_name") or ""),
                        artifact_id=artifact_id,
                        payload={"artifact_id": artifact_id, "projection_id": projection_payload.get("projection_id")},
                        payload_ref="tool_result_context.artifact_ids",
                        next_turn_visible=True,
                        metadata={"projection_id": str(projection_payload.get("projection_id") or "")},
                    )
                )
        return output

    def _budget_chain_entries(self, report: ToolBudgetChainReport | None) -> list[ToolResultReplayEntry]:
        if report is None:
            return []
        output: list[ToolResultReplayEntry] = []
        for node in report.nodes:
            payload = node.to_dict() if hasattr(node, "to_dict") else node
            if not isinstance(payload, Mapping):
                continue
            artifact_id = str(payload.get("artifact_id") or payload.get("payload_ref") or "")
            output.append(
                self._entry(
                    ToolResultReplayEntryKind.BUDGET_CHAIN_NODE,
                    replay_key=f"budget_chain:{payload.get('tool_call_id') or ''}:{payload.get('node_id') or ''}",
                    tool_call_id=str(payload.get("tool_call_id") or ""),
                    tool_name=str(payload.get("tool_name") or ""),
                    artifact_id=artifact_id if artifact_id.startswith("artifact") else "",
                    payload=payload,
                    payload_ref="tool_budget_chain.nodes",
                    next_turn_visible=str(payload.get("kind") or "") in {"bounded_result", "artifact_pointer", "result_context"},
                    metadata={"kind": str(payload.get("kind") or "")},
                )
            )
        return output

    def _continuation_packet_entries(self, report: ToolContinuationPacketReport | None) -> list[ToolResultReplayEntry]:
        if report is None:
            return []
        output: list[ToolResultReplayEntry] = []
        for item in report.items:
            payload = item.to_dict() if hasattr(item, "to_dict") else item
            if not isinstance(payload, Mapping):
                continue
            output.append(
                self._entry(
                    ToolResultReplayEntryKind.CONTINUATION_PACKET_ITEM,
                    replay_key=f"continuation:{payload.get('tool_call_id') or ''}:{payload.get('item_id') or ''}",
                    tool_call_id=str(payload.get("tool_call_id") or ""),
                    artifact_id=str(payload.get("artifact_id") or ""),
                    payload=payload,
                    payload_ref="tool_continuation_packet.items",
                    next_turn_visible=payload.get("required_for_next_turn") is True,
                    metadata={"kind": str(payload.get("kind") or ""), "section": str(payload.get("section") or "")},
                )
            )
        return output

    def _entry(
        self,
        kind: ToolResultReplayEntryKind,
        *,
        replay_key: str,
        payload: Any,
        tool_call_id: str = "",
        tool_name: str = "",
        artifact_id: str = "",
        payload_ref: str = "",
        next_turn_visible: bool = False,
        metadata: Mapping[str, str] | None = None,
    ) -> ToolResultReplayEntry:
        encoded = _stable_json(payload)
        return ToolResultReplayEntry(
            entry_id=new_id("toolreplayentry"),
            kind=kind,
            replay_key=replay_key,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            artifact_id=artifact_id,
            payload_ref=payload_ref,
            chars=len(encoded),
            checksum=hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16],
            next_turn_visible=next_turn_visible,
            metadata={str(k): str(v) for k, v in dict(metadata or {}).items()},
        )

    def _findings(
        self,
        entries: Sequence[ToolResultReplayEntry],
        receipts: Sequence[Mapping[str, Any]],
        result_context_report: ToolResultContextReport | None,
        continuation_packet_report: ToolContinuationPacketReport | None,
    ) -> list[ToolResultReplayFinding]:
        findings: list[ToolResultReplayFinding] = []
        receipt_tool_ids = _receipt_tool_ids(receipts)
        if receipt_tool_ids and not entries:
            findings.append(
                ToolResultReplayFinding(
                    code="RESULT_REPLAY_INDEX_EMPTY_WITH_RECEIPTS",
                    severity=ToolResultReplaySeverity.BLOCKER,
                    surface=ToolResultReplaySurface.RECEIPT,
                    message="Tool receipts exist but no replay index entries were built.",
                )
            )
        by_tool: dict[str, list[ToolResultReplayEntry]] = {}
        for entry in entries:
            if entry.tool_call_id:
                by_tool.setdefault(entry.tool_call_id, []).append(entry)
        for tool_call_id in sorted(receipt_tool_ids):
            tool_entries = by_tool.get(tool_call_id, [])
            if not tool_entries:
                findings.append(
                    ToolResultReplayFinding(
                        code="RESULT_REPLAY_INDEX_TOOL_CALL_MISSING",
                        severity=ToolResultReplaySeverity.BLOCKER,
                        surface=ToolResultReplaySurface.RECEIPT,
                        message="A receipt tool call does not have any replay index entry.",
                        tool_call_id=tool_call_id,
                    )
                )
                continue
            if not any(entry.next_turn_visible for entry in tool_entries):
                findings.append(
                    ToolResultReplayFinding(
                        code="RESULT_REPLAY_INDEX_NEXT_TURN_VISIBILITY_MISSING",
                        severity=ToolResultReplaySeverity.BLOCKER,
                        surface=ToolResultReplaySurface.CONTINUATION_PACKET,
                        message="A receipt tool call has no replay entry visible to the next turn.",
                        tool_call_id=tool_call_id,
                    )
                )
        if result_context_report is None and receipt_tool_ids:
            findings.append(
                ToolResultReplayFinding(
                    code="RESULT_REPLAY_INDEX_CONTEXT_REPORT_MISSING",
                    severity=ToolResultReplaySeverity.BLOCKER,
                    surface=ToolResultReplaySurface.RESULT_CONTEXT,
                    message="Tool receipts exist but ToolResultContextReport is absent.",
                )
            )
        if continuation_packet_report is None and receipt_tool_ids:
            findings.append(
                ToolResultReplayFinding(
                    code="RESULT_REPLAY_INDEX_CONTINUATION_PACKET_MISSING",
                    severity=ToolResultReplaySeverity.ERROR,
                    surface=ToolResultReplaySurface.CONTINUATION_PACKET,
                    message="Tool receipts exist but continuation packet was not built.",
                )
            )
        duplicate_keys = sorted(key for key, count in _key_counts(entries).items() if count > 1)
        for key in duplicate_keys[:20]:
            findings.append(
                ToolResultReplayFinding(
                    code="RESULT_REPLAY_INDEX_DUPLICATE_KEY",
                    severity=ToolResultReplaySeverity.WARNING,
                    surface=ToolResultReplaySurface.RECEIPT,
                    message="A replay key was emitted more than once before dedupe.",
                    replay_key=key,
                )
            )
        return findings


def tool_result_replay_index_metadata(report: ToolResultReplayIndexReport | None) -> dict[str, str]:
    if report is None:
        return {"tool_result_replay_index_ok": "false", "tool_result_replay_index_entries": "0"}
    return report.metadata()


def assert_tool_result_replay_index_ready(report: ToolResultReplayIndexReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.findings if finding.blocking)
    raise AssertionError(f"tool result replay index blocked: {blockers or 'unknown'}")


def render_tool_result_replay_index_markdown(report: ToolResultReplayIndexReport) -> str:
    lines = [
        "## Tool Result Replay Index",
        "",
        f"- status: `{report.status}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- entries: `{len(report.entries)}`",
        f"- tool_calls: `{report.tool_call_count}`",
        f"- artifacts: `{report.artifact_count}`",
        f"- visible: `{report.visible_count}`",
        "",
        "### Findings",
        "",
    ]
    if report.findings:
        lines.extend(f"- `{finding.code}` [{finding.severity}]: {finding.message}" for finding in report.findings)
    else:
        lines.append("- no findings")
    return "\n".join(lines)


def _dedupe_entries(entries: Sequence[ToolResultReplayEntry]) -> list[ToolResultReplayEntry]:
    seen: set[tuple[str, str, str]] = set()
    output: list[ToolResultReplayEntry] = []
    for entry in entries:
        key = (entry.kind, entry.replay_key, entry.checksum)
        if key in seen:
            continue
        seen.add(key)
        output.append(entry)
    return output


def _key_counts(entries: Sequence[ToolResultReplayEntry]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.replay_key] = counts.get(entry.replay_key, 0) + 1
    return counts


def _receipt_tool_ids(receipts: Sequence[Mapping[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            continue
        request = receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}
        bounded = receipt.get("bounded_result") if isinstance(receipt.get("bounded_result"), Mapping) else {}
        tool_call_id = str(request.get("tool_call_id") or bounded.get("tool_call_id") or "")
        if tool_call_id:
            ids.add(tool_call_id)
    return ids


def _artifact_ids_from_projection(projection: Mapping[str, Any]) -> list[str]:
    value = projection.get("artifact_ids")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value if str(item)]


def _full_output_artifact_id(result: Mapping[str, Any]) -> str:
    output = result.get("output") if isinstance(result.get("output"), Mapping) else {}
    return str(output.get("full_output_artifact_id") or "")


def _stable_json(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
