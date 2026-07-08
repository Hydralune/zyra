from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID


class ToolEffectFingerprintPartKind(StrEnum):
    RECEIPTS = "receipts"
    RESULT_CONTEXT = "result_context"
    BUDGET_CHAIN = "budget_chain"
    PERMISSION_CHECKPOINT = "permission_checkpoint"
    CONTINUATION_PACKET = "continuation_packet"
    REPLAY_INDEX = "replay_index"
    SOURCE_EFFECTS = "source_effects"
    READINESS_MATRIX = "readiness_matrix"
    SEMANTIC_EFFECTS = "semantic_effects"


class ToolEffectFingerprintStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    EMPTY = "empty"
    BLOCKED = "blocked"


class ToolEffectFingerprintSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class ToolEffectFingerprintPart:
    part_id: str
    kind: ToolEffectFingerprintPartKind
    present: bool
    ok: bool
    fingerprint: str = ""
    report_id: str = ""
    status: str = ""
    item_count: int = 0
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "part_id": self.part_id,
            "kind": str(self.kind),
            "present": self.present,
            "ok": self.ok,
            "fingerprint": self.fingerprint,
            "report_id": self.report_id,
            "status": self.status,
            "item_count": self.item_count,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolEffectFingerprintFinding:
    code: str
    severity: ToolEffectFingerprintSeverity
    message: str
    part_kind: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolEffectFingerprintSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "message": self.message,
            "part_kind": self.part_kind,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolEffectFingerprintReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    fingerprint: str
    parts: tuple[ToolEffectFingerprintPart, ...]
    findings: tuple[ToolEffectFingerprintFinding, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ToolEffectFingerprintStatus:
        if any(finding.blocking for finding in self.findings):
            return ToolEffectFingerprintStatus.BLOCKED
        if not self.parts:
            return ToolEffectFingerprintStatus.EMPTY
        if self.findings:
            return ToolEffectFingerprintStatus.DEGRADED
        return ToolEffectFingerprintStatus.READY

    @property
    def present_count(self) -> int:
        return sum(1 for part in self.parts if part.present)

    @property
    def ready_count(self) -> int:
        return sum(1 for part in self.parts if part.present and part.ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.tool_effect_fingerprint.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "fingerprint": self.fingerprint,
            "part_count": len(self.parts),
            "present_count": self.present_count,
            "ready_count": self.ready_count,
            "findings": [finding.to_dict() for finding in self.findings],
            "parts": [part.to_dict() for part in self.parts],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_effect_fingerprint_report_id": self.report_id,
            "tool_effect_fingerprint_owner_unit": self.owner_unit,
            "tool_effect_fingerprint_runtime_id": self.runtime_id,
            "tool_effect_fingerprint_ok": str(self.ok).lower(),
            "tool_effect_fingerprint_status": str(self.status),
            "tool_effect_fingerprint_value": self.fingerprint,
            "tool_effect_fingerprint_parts": str(len(self.parts)),
            "tool_effect_fingerprint_present": str(self.present_count),
            "tool_effect_fingerprint_ready": str(self.ready_count),
            "tool_effect_fingerprint_findings": str(len(self.findings)),
        }


class ToolEffectFingerprintRuntime:
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
        reports: Mapping[str, Any],
    ) -> ToolEffectFingerprintReport:
        parts = [self._receipt_part(receipts)]
        for kind in (
            ToolEffectFingerprintPartKind.RESULT_CONTEXT,
            ToolEffectFingerprintPartKind.BUDGET_CHAIN,
            ToolEffectFingerprintPartKind.PERMISSION_CHECKPOINT,
            ToolEffectFingerprintPartKind.CONTINUATION_PACKET,
            ToolEffectFingerprintPartKind.REPLAY_INDEX,
            ToolEffectFingerprintPartKind.SOURCE_EFFECTS,
            ToolEffectFingerprintPartKind.READINESS_MATRIX,
            ToolEffectFingerprintPartKind.SEMANTIC_EFFECTS,
        ):
            parts.append(self._report_part(kind, reports.get(str(kind))))
        findings = tuple(self._findings(parts, receipt_count=len(receipts)))
        fingerprint = _fingerprint([part.to_dict() for part in parts if part.present])
        return ToolEffectFingerprintReport(
            report_id=new_id("toolfingerprint"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            fingerprint=fingerprint,
            parts=tuple(parts),
            findings=findings,
        )

    def event_for_report(
        self,
        report: ToolEffectFingerprintReport,
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
                    "phase": "tool_effect_fingerprint",
                    "tool_effect_fingerprint": report.to_dict(),
                }
            },
        )

    def _receipt_part(self, receipts: Sequence[Mapping[str, Any]]) -> ToolEffectFingerprintPart:
        payload = [
            {
                "tool_call_id": str((receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}).get("tool_call_id") or ""),
                "tool_name": str((receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}).get("tool_name") or ""),
                "ok": bool((receipt.get("bounded_result") if isinstance(receipt.get("bounded_result"), Mapping) else {}).get("ok")),
                "budget_applied": bool((receipt.get("budget_decision") if isinstance(receipt.get("budget_decision"), Mapping) else {}).get("applied")),
            }
            for receipt in receipts
            if isinstance(receipt, Mapping)
        ]
        return ToolEffectFingerprintPart(
            part_id=new_id("toolfingerprintpart"),
            kind=ToolEffectFingerprintPartKind.RECEIPTS,
            present=bool(payload),
            ok=True,
            fingerprint=_fingerprint(payload),
            status="ready" if payload else "empty",
            item_count=len(payload),
            metadata={"source": "ToolExecutionReceipt"},
        )

    def _report_part(self, kind: ToolEffectFingerprintPartKind, report: Any) -> ToolEffectFingerprintPart:
        payload = _payload(report)
        present = bool(payload)
        ok = _ok(payload, report) if present else False
        count = _count(payload)
        return ToolEffectFingerprintPart(
            part_id=new_id("toolfingerprintpart"),
            kind=kind,
            present=present,
            ok=ok,
            fingerprint=_fingerprint(payload) if present else "",
            report_id=str(payload.get("report_id") or ""),
            status=str(payload.get("status") or ("ready" if ok else "missing")),
            item_count=count,
            metadata={key: str(value) for key, value in payload.items() if key.endswith("_count") or key in {"status", "ok"}},
        )

    def _findings(
        self,
        parts: Sequence[ToolEffectFingerprintPart],
        *,
        receipt_count: int,
    ) -> list[ToolEffectFingerprintFinding]:
        findings: list[ToolEffectFingerprintFinding] = []
        if receipt_count and not any(part.kind == ToolEffectFingerprintPartKind.RECEIPTS and part.present for part in parts):
            findings.append(
                ToolEffectFingerprintFinding(
                    code="TOOL_EFFECT_FINGERPRINT_RECEIPTS_MISSING",
                    severity=ToolEffectFingerprintSeverity.BLOCKER,
                    message="Receipt count was non-zero but no receipt fingerprint part was produced.",
                    part_kind=str(ToolEffectFingerprintPartKind.RECEIPTS),
                )
            )
        required_when_receipts = {
            ToolEffectFingerprintPartKind.RESULT_CONTEXT,
            ToolEffectFingerprintPartKind.BUDGET_CHAIN,
            ToolEffectFingerprintPartKind.CONTINUATION_PACKET,
            ToolEffectFingerprintPartKind.REPLAY_INDEX,
            ToolEffectFingerprintPartKind.READINESS_MATRIX,
        }
        for part in parts:
            if receipt_count and part.kind in required_when_receipts and not part.present:
                findings.append(
                    ToolEffectFingerprintFinding(
                        code="TOOL_EFFECT_FINGERPRINT_REQUIRED_PART_MISSING",
                        severity=ToolEffectFingerprintSeverity.BLOCKER,
                        message="A required fingerprint part is absent for a run with tool receipts.",
                        part_kind=str(part.kind),
                    )
                )
            elif part.present and not part.ok:
                findings.append(
                    ToolEffectFingerprintFinding(
                        code="TOOL_EFFECT_FINGERPRINT_PART_NOT_READY",
                        severity=ToolEffectFingerprintSeverity.WARNING,
                        message="A fingerprinted report part is present but not ready.",
                        part_kind=str(part.kind),
                        metadata={"status": part.status, "report_id": part.report_id},
                    )
                )
        return findings


def tool_effect_fingerprint_metadata(report: ToolEffectFingerprintReport | None) -> dict[str, str]:
    if report is None:
        return {"tool_effect_fingerprint_ok": "false", "tool_effect_fingerprint_parts": "0"}
    return report.metadata()


def assert_tool_effect_fingerprint_ready(report: ToolEffectFingerprintReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.findings if finding.blocking)
    raise AssertionError(f"tool effect fingerprint blocked: {blockers or 'unknown'}")


def render_tool_effect_fingerprint_markdown(report: ToolEffectFingerprintReport) -> str:
    lines = [
        "## Tool Effect Fingerprint",
        "",
        f"- status: `{report.status}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- fingerprint: `{report.fingerprint}`",
        f"- parts: `{len(report.parts)}`",
        "",
        "### Findings",
        "",
    ]
    if report.findings:
        lines.extend(f"- `{finding.code}` [{finding.severity}]: {finding.message}" for finding in report.findings)
    else:
        lines.append("- no findings")
    return "\n".join(lines)


def _payload(report: Any) -> dict[str, Any]:
    if report is None:
        return {}
    if isinstance(report, Mapping):
        return dict(report)
    if hasattr(report, "to_dict"):
        payload = report.to_dict()
        return dict(payload) if isinstance(payload, Mapping) else {}
    return {}


def _ok(payload: Mapping[str, Any], report: Any) -> bool:
    if payload.get("ok") is not None:
        return bool(payload.get("ok"))
    if report is not None and hasattr(report, "ok"):
        return bool(getattr(report, "ok"))
    return str(payload.get("status") or "") not in {"blocked", "failed", "fail"}


def _count(payload: Mapping[str, Any]) -> int:
    for key in ("entry_count", "item_count", "node_count", "evidence_count", "row_count", "effect_count"):
        try:
            return int(payload.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return 1 if payload else 0


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(to_jsonable(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
