from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .tool_loop import ToolLoopBatch, ToolLoopPlan, ToolLoopRequest
from .tool_runtime_foundation import (
    TOOL_LOOP_FOUNDATION_OWNER_UNIT,
    TOOL_LOOP_FOUNDATION_RUNTIME_ID,
    ToolRegistryMaterialization,
)


class ToolSettlementStatus(StrEnum):
    SETTLED = "settled"
    WARNING = "warning"
    STALE = "stale"
    BLOCKED = "blocked"


class ToolSettlementSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ToolSettlementSurface(StrEnum):
    MATERIALIZATION = "materialization"
    PLAN = "plan"
    BATCH = "batch"
    RECEIPT = "receipt"
    FILTER = "filter"
    CONCURRENCY = "concurrency"


@dataclass(frozen=True, slots=True)
class ToolSettlementFinding:
    code: str
    severity: ToolSettlementSeverity
    surface: ToolSettlementSurface
    message: str
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolSettlementSeverity.BLOCKER

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
class ToolSettlementPlanCoverage:
    materialization_id: str
    generation: int
    active_tool_count: int
    filtered_tool_count: int
    planned_tool_count: int
    planned_active_count: int
    planned_filtered_count: int
    planned_unknown_count: int
    schema_error_count: int
    conflict_protected_count: int
    duplicate_tool_call_ids: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.planned_unknown_count == 0 and self.planned_filtered_count == 0 and not self.duplicate_tool_call_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "materialization_id": self.materialization_id,
            "generation": self.generation,
            "active_tool_count": self.active_tool_count,
            "filtered_tool_count": self.filtered_tool_count,
            "planned_tool_count": self.planned_tool_count,
            "planned_active_count": self.planned_active_count,
            "planned_filtered_count": self.planned_filtered_count,
            "planned_unknown_count": self.planned_unknown_count,
            "schema_error_count": self.schema_error_count,
            "conflict_protected_count": self.conflict_protected_count,
            "duplicate_tool_call_ids": list(self.duplicate_tool_call_ids),
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class ToolSettlementBatchCoverage:
    batch_count: int
    concurrent_batch_count: int
    serial_batch_count: int
    max_batch_size: int
    read_only_requests: int
    mutating_requests: int
    unsafe_concurrent_batches: int
    duplicate_batch_indexes: tuple[int, ...] = ()

    @property
    def ok(self) -> bool:
        return self.unsafe_concurrent_batches == 0 and not self.duplicate_batch_indexes

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_count": self.batch_count,
            "concurrent_batch_count": self.concurrent_batch_count,
            "serial_batch_count": self.serial_batch_count,
            "max_batch_size": self.max_batch_size,
            "read_only_requests": self.read_only_requests,
            "mutating_requests": self.mutating_requests,
            "unsafe_concurrent_batches": self.unsafe_concurrent_batches,
            "duplicate_batch_indexes": list(self.duplicate_batch_indexes),
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class ToolSettlementReceiptCoverage:
    expected_receipts: int
    observed_receipts: int
    missing_tool_call_ids: tuple[str, ...] = ()
    extra_tool_call_ids: tuple[str, ...] = ()
    duplicate_receipt_tool_call_ids: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.missing_tool_call_ids and not self.extra_tool_call_ids and not self.duplicate_receipt_tool_call_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_receipts": self.expected_receipts,
            "observed_receipts": self.observed_receipts,
            "missing_tool_call_ids": list(self.missing_tool_call_ids),
            "extra_tool_call_ids": list(self.extra_tool_call_ids),
            "duplicate_receipt_tool_call_ids": list(self.duplicate_receipt_tool_call_ids),
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class ToolSettlementReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    status: ToolSettlementStatus
    worker_request_id: str
    session_id: str
    turn_id: str
    turn_index: int
    plan_coverage: ToolSettlementPlanCoverage
    batch_coverage: ToolSettlementBatchCoverage
    receipt_coverage: ToolSettlementReceiptCoverage | None = None
    findings: tuple[ToolSettlementFinding, ...] = ()
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return self.status in {ToolSettlementStatus.SETTLED, ToolSettlementStatus.WARNING} and not any(
            finding.blocking for finding in self.findings
        )

    @property
    def blocking_codes(self) -> tuple[str, ...]:
        return tuple(finding.code for finding in self.findings if finding.blocking)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "status": str(self.status),
            "ok": self.ok,
            "worker_request_id": self.worker_request_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "turn_index": self.turn_index,
            "plan_coverage": self.plan_coverage.to_dict(),
            "batch_coverage": self.batch_coverage.to_dict(),
            "receipt_coverage": self.receipt_coverage.to_dict() if self.receipt_coverage else None,
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_settlement_report_id": self.report_id,
            "tool_settlement_status": str(self.status),
            "tool_settlement_ok": str(self.ok).lower(),
            "tool_settlement_findings": str(len(self.findings)),
            "tool_settlement_blockers": ",".join(self.blocking_codes),
            "tool_settlement_planned_tools": str(self.plan_coverage.planned_tool_count),
            "tool_settlement_filtered_tools": str(self.plan_coverage.planned_filtered_count),
            "tool_settlement_unknown_tools": str(self.plan_coverage.planned_unknown_count),
            "tool_settlement_batch_count": str(self.batch_coverage.batch_count),
        }


class ToolSettlementRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id

    def settle_plan(
        self,
        *,
        materialization: ToolRegistryMaterialization | Mapping[str, Any],
        plan: ToolLoopPlan,
        session_id: str,
        turn_id: str,
    ) -> ToolSettlementReport:
        materialization_data = materialization.to_dict() if hasattr(materialization, "to_dict") else dict(materialization)
        plan_coverage = self._plan_coverage(materialization_data, plan)
        batch_coverage = self._batch_coverage(plan.batches)
        findings = [
            *self._materialization_findings(materialization_data),
            *self._plan_findings(plan_coverage, plan),
            *self._batch_findings(batch_coverage),
        ]
        return ToolSettlementReport(
            report_id=new_id("toolsettle"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            status=_status_from_findings(findings),
            worker_request_id=plan.worker_request_id,
            session_id=session_id,
            turn_id=turn_id,
            turn_index=plan.turn_index,
            plan_coverage=plan_coverage,
            batch_coverage=batch_coverage,
            findings=tuple(findings),
        )

    def settle_receipts(
        self,
        report: ToolSettlementReport,
        *,
        plan: ToolLoopPlan,
        receipts: Sequence[Mapping[str, Any]],
    ) -> ToolSettlementReport:
        receipt_coverage = self._receipt_coverage(plan.requests, receipts)
        findings = [*report.findings, *self._receipt_findings(receipt_coverage)]
        return ToolSettlementReport(
            report_id=report.report_id,
            owner_unit=report.owner_unit,
            runtime_id=report.runtime_id,
            status=_status_from_findings(findings),
            worker_request_id=report.worker_request_id,
            session_id=report.session_id,
            turn_id=report.turn_id,
            turn_index=report.turn_index,
            plan_coverage=report.plan_coverage,
            batch_coverage=report.batch_coverage,
            receipt_coverage=receipt_coverage,
            findings=tuple(findings),
            created_at=report.created_at,
        )

    def event_for_report(self, report: ToolSettlementReport, *, run_id: str, task_id: str, node_id: str | None) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": report.session_id,
                    "worker_request_id": report.worker_request_id,
                    "phase": "tool_registry_settled",
                    "settlement": report.to_dict(),
                }
            },
        )

    def _plan_coverage(
        self,
        materialization: Mapping[str, Any],
        plan: ToolLoopPlan,
    ) -> ToolSettlementPlanCoverage:
        active = set(str(item) for item in materialization.get("active_tool_names") or [])
        filtered = set(str(item) for item in materialization.get("filtered_tool_names") or [])
        planned_active = 0
        planned_filtered = 0
        planned_unknown = 0
        seen_ids: set[str] = set()
        duplicate_ids: list[str] = []
        for request in plan.requests:
            if request.tool_name in active:
                planned_active += 1
            elif request.tool_name in filtered:
                planned_filtered += 1
            else:
                planned_unknown += 1
            if request.call.tool_call_id in seen_ids:
                duplicate_ids.append(request.call.tool_call_id)
            seen_ids.add(request.call.tool_call_id)
        return ToolSettlementPlanCoverage(
            materialization_id=str(materialization.get("materialization_id") or ""),
            generation=_safe_int(materialization.get("generation")),
            active_tool_count=len(active),
            filtered_tool_count=len(filtered),
            planned_tool_count=len(plan.requests),
            planned_active_count=planned_active,
            planned_filtered_count=planned_filtered,
            planned_unknown_count=planned_unknown,
            schema_error_count=plan.schema_error_count,
            conflict_protected_count=plan.conflict_protected_count,
            duplicate_tool_call_ids=tuple(sorted(set(duplicate_ids))),
        )

    def _batch_coverage(self, batches: Sequence[ToolLoopBatch]) -> ToolSettlementBatchCoverage:
        concurrent = 0
        serial = 0
        unsafe_concurrent = 0
        read_only_requests = 0
        mutating_requests = 0
        max_batch_size = 0
        seen_indexes: set[int] = set()
        duplicate_indexes: list[int] = []
        for batch in batches:
            if batch.batch_index in seen_indexes:
                duplicate_indexes.append(batch.batch_index)
            seen_indexes.add(batch.batch_index)
            max_batch_size = max(max_batch_size, len(batch.requests))
            if len(batch.requests) > 1:
                concurrent += 1
            else:
                serial += 1
            if len(batch.requests) > 1 and not all(request.read_only and request.concurrency_safe for request in batch.requests):
                unsafe_concurrent += 1
            for request in batch.requests:
                if request.read_only:
                    read_only_requests += 1
                else:
                    mutating_requests += 1
        return ToolSettlementBatchCoverage(
            batch_count=len(batches),
            concurrent_batch_count=concurrent,
            serial_batch_count=serial,
            max_batch_size=max_batch_size,
            read_only_requests=read_only_requests,
            mutating_requests=mutating_requests,
            unsafe_concurrent_batches=unsafe_concurrent,
            duplicate_batch_indexes=tuple(sorted(set(duplicate_indexes))),
        )

    def _receipt_coverage(
        self,
        requests: Sequence[ToolLoopRequest],
        receipts: Sequence[Mapping[str, Any]],
    ) -> ToolSettlementReceiptCoverage:
        expected = [request.call.tool_call_id for request in requests]
        observed: list[str] = []
        for receipt in receipts:
            request = receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}
            bounded = receipt.get("bounded_result") if isinstance(receipt.get("bounded_result"), Mapping) else {}
            observed.append(str(request.get("tool_call_id") or bounded.get("tool_call_id") or ""))
        duplicate_observed = _duplicates(observed)
        expected_set = set(expected)
        observed_set = set(item for item in observed if item)
        return ToolSettlementReceiptCoverage(
            expected_receipts=len(expected),
            observed_receipts=len(observed),
            missing_tool_call_ids=tuple(sorted(expected_set.difference(observed_set))),
            extra_tool_call_ids=tuple(sorted(observed_set.difference(expected_set))),
            duplicate_receipt_tool_call_ids=tuple(sorted(duplicate_observed)),
        )

    def _materialization_findings(self, materialization: Mapping[str, Any]) -> list[ToolSettlementFinding]:
        findings: list[ToolSettlementFinding] = []
        if not materialization.get("materialization_id"):
            findings.append(
                _finding(
                    "TOOL_SETTLEMENT_MATERIALIZATION_ID_MISSING",
                    ToolSettlementSeverity.BLOCKER,
                    ToolSettlementSurface.MATERIALIZATION,
                    "Tool settlement cannot proceed without a materialization id.",
                )
            )
        if materialization.get("owner_unit") != self.owner_unit:
            findings.append(
                _finding(
                    "TOOL_SETTLEMENT_OWNER_MISMATCH",
                    ToolSettlementSeverity.BLOCKER,
                    ToolSettlementSurface.MATERIALIZATION,
                    "Tool materialization owner does not match settlement runtime owner.",
                    expected_owner=self.owner_unit,
                    actual_owner=str(materialization.get("owner_unit") or ""),
                )
            )
        return findings

    def _plan_findings(
        self,
        coverage: ToolSettlementPlanCoverage,
        plan: ToolLoopPlan,
    ) -> list[ToolSettlementFinding]:
        findings: list[ToolSettlementFinding] = []
        if coverage.planned_filtered_count:
            findings.append(
                _finding(
                    "TOOL_SETTLEMENT_FILTERED_TOOL_PLANNED",
                    ToolSettlementSeverity.BLOCKER,
                    ToolSettlementSurface.FILTER,
                    "A tool filtered out of the materialized registry was still planned for execution.",
                    planned_filtered_count=str(coverage.planned_filtered_count),
                )
            )
        if coverage.planned_unknown_count:
            findings.append(
                _finding(
                    "TOOL_SETTLEMENT_UNKNOWN_TOOL_PLANNED",
                    ToolSettlementSeverity.ERROR,
                    ToolSettlementSurface.PLAN,
                    "A planned tool was not present in active or filtered materialization entries.",
                    planned_unknown_count=str(coverage.planned_unknown_count),
                )
            )
        if coverage.duplicate_tool_call_ids:
            findings.append(
                _finding(
                    "TOOL_SETTLEMENT_DUPLICATE_TOOL_CALL_ID",
                    ToolSettlementSeverity.BLOCKER,
                    ToolSettlementSurface.PLAN,
                    "A tool loop plan contains duplicate tool call ids.",
                    duplicate_tool_call_ids=",".join(coverage.duplicate_tool_call_ids),
                )
            )
        planned_ids = [request.call.tool_call_id for request in plan.requests]
        batch_ids = [request.call.tool_call_id for batch in plan.batches for request in batch.requests]
        missing_from_batches = set(planned_ids).difference(batch_ids)
        if missing_from_batches:
            findings.append(
                _finding(
                    "TOOL_SETTLEMENT_REQUEST_NOT_BATCHED",
                    ToolSettlementSeverity.BLOCKER,
                    ToolSettlementSurface.BATCH,
                    "A planned request is not present in any execution batch.",
                    missing_tool_call_ids=",".join(sorted(missing_from_batches)),
                )
            )
        return findings

    def _batch_findings(self, coverage: ToolSettlementBatchCoverage) -> list[ToolSettlementFinding]:
        findings: list[ToolSettlementFinding] = []
        if coverage.unsafe_concurrent_batches:
            findings.append(
                _finding(
                    "TOOL_SETTLEMENT_UNSAFE_CONCURRENT_BATCH",
                    ToolSettlementSeverity.BLOCKER,
                    ToolSettlementSurface.CONCURRENCY,
                    "A concurrent batch contains a mutating or non-concurrency-safe tool.",
                    unsafe_concurrent_batches=str(coverage.unsafe_concurrent_batches),
                )
            )
        if coverage.duplicate_batch_indexes:
            findings.append(
                _finding(
                    "TOOL_SETTLEMENT_DUPLICATE_BATCH_INDEX",
                    ToolSettlementSeverity.BLOCKER,
                    ToolSettlementSurface.BATCH,
                    "Tool loop plan contains duplicate batch indexes.",
                    duplicate_batch_indexes=",".join(str(item) for item in coverage.duplicate_batch_indexes),
                )
            )
        return findings

    def _receipt_findings(self, coverage: ToolSettlementReceiptCoverage) -> list[ToolSettlementFinding]:
        findings: list[ToolSettlementFinding] = []
        if coverage.missing_tool_call_ids:
            findings.append(
                _finding(
                    "TOOL_SETTLEMENT_RECEIPT_MISSING",
                    ToolSettlementSeverity.BLOCKER,
                    ToolSettlementSurface.RECEIPT,
                    "A planned tool call has no execution receipt.",
                    missing_tool_call_ids=",".join(coverage.missing_tool_call_ids),
                )
            )
        if coverage.extra_tool_call_ids:
            findings.append(
                _finding(
                    "TOOL_SETTLEMENT_RECEIPT_EXTRA",
                    ToolSettlementSeverity.BLOCKER,
                    ToolSettlementSurface.RECEIPT,
                    "An execution receipt exists for an unplanned tool call.",
                    extra_tool_call_ids=",".join(coverage.extra_tool_call_ids),
                )
            )
        if coverage.duplicate_receipt_tool_call_ids:
            findings.append(
                _finding(
                    "TOOL_SETTLEMENT_RECEIPT_DUPLICATE",
                    ToolSettlementSeverity.BLOCKER,
                    ToolSettlementSurface.RECEIPT,
                    "A tool call has duplicate execution receipts.",
                    duplicate_tool_call_ids=",".join(coverage.duplicate_receipt_tool_call_ids),
                )
            )
        return findings


def tool_settlement_metadata(reports: Sequence[ToolSettlementReport]) -> dict[str, str]:
    if not reports:
        return {
            "tool_settlement_reports": "0",
            "tool_settlement_all_ok": "true",
        }
    blockers = [code for report in reports for code in report.blocking_codes]
    return {
        "tool_settlement_reports": str(len(reports)),
        "tool_settlement_all_ok": str(all(report.ok for report in reports)).lower(),
        "tool_settlement_blockers": ",".join(blockers),
        "tool_settlement_total_planned_tools": str(sum(report.plan_coverage.planned_tool_count for report in reports)),
        "tool_settlement_total_batches": str(sum(report.batch_coverage.batch_count for report in reports)),
        "tool_settlement_total_findings": str(sum(len(report.findings) for report in reports)),
    }


def render_tool_settlement_markdown(reports: Sequence[ToolSettlementReport]) -> str:
    lines = [
        "## Tool Registry Settlement",
        "",
        f"- report_count: `{len(reports)}`",
        f"- all_ok: `{str(all(report.ok for report in reports)).lower()}`",
        "",
        "### Reports",
        "",
    ]
    for report in reports:
        lines.append(
            f"- turn `{report.turn_index}` `{report.status}`: "
            f"{report.plan_coverage.planned_tool_count} tool(s), {report.batch_coverage.batch_count} batch(es)"
        )
        for finding in report.findings:
            lines.append(f"  - `{finding.code}` [{finding.severity}/{finding.surface}]: {finding.message}")
    if not reports:
        lines.append("- no settlement reports")
    return "\n".join(lines)


def _status_from_findings(findings: Sequence[ToolSettlementFinding]) -> ToolSettlementStatus:
    if any(finding.blocking for finding in findings):
        return ToolSettlementStatus.BLOCKED
    if any(finding.severity == ToolSettlementSeverity.ERROR for finding in findings):
        return ToolSettlementStatus.STALE
    if any(finding.severity == ToolSettlementSeverity.WARNING for finding in findings):
        return ToolSettlementStatus.WARNING
    return ToolSettlementStatus.SETTLED


def _finding(
    code: str,
    severity: ToolSettlementSeverity,
    surface: ToolSettlementSurface,
    message: str,
    **metadata: str,
) -> ToolSettlementFinding:
    return ToolSettlementFinding(
        code=code,
        severity=severity,
        surface=surface,
        message=message,
        metadata={str(key): str(value) for key, value in metadata.items()},
    )


def _duplicates(values: Sequence[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if not value:
            continue
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
