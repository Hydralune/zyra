from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import ArtifactRef, EventRecord, EventType, new_id, now_iso, to_jsonable

from .runtime_budget_state import (
    CODEWORKER_API_FOUNDATION_RUNTIME_ID,
    M1_02D_OWNER_UNIT,
    RuntimeBudgetPressure,
    RuntimeBudgetState,
)


class ContextBudgetStatus(StrEnum):
    READY = "ready"
    WATCH = "watch"
    COMPACT_NEEDED = "compact_needed"
    OVER_LIMIT = "over_limit"
    BLOCKED = "blocked"


class CompactRestoreStatus(StrEnum):
    READY = "ready"
    COMPACT_NEEDED = "compact_needed"
    COMPACTED = "compacted"
    RESTORE_READY = "restore_ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    DISABLED = "disabled"


class CompactRestoreSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class CompactRestoreSurface(StrEnum):
    CONTEXT_USAGE = "context_usage"
    COMPACT_CANDIDATE = "compact_candidate"
    COMPACT_BOUNDARY = "compact_boundary"
    PRESERVED_SEGMENT = "preserved_segment"
    RESTORE_SEGMENT = "restore_segment"
    MCP_INSTRUCTIONS = "mcp_instructions"
    SKILL_MEMORY = "skill_memory"
    TOOL_STATE = "tool_state"
    NEXT_TURN = "next_turn"
    BUDGET_STATE = "budget_state"


class RestoreSegmentKind(StrEnum):
    FILE_ATTACHMENT = "file_attachment"
    TOOL_RESULT = "tool_result"
    ACTIVE_PLAN = "active_plan"
    INVOKED_SKILL = "invoked_skill"
    MCP_INSTRUCTION_DELTA = "mcp_instruction_delta"
    DEFERRED_TOOL = "deferred_tool"
    ASYNC_AGENT = "async_agent"
    AGENT_LISTING = "agent_listing"
    PERMISSION_STATE = "permission_state"
    BUDGET_STATE = "budget_state"
    CONTEXT_SUMMARY = "context_summary"


@dataclass(frozen=True, slots=True)
class ContextUsageSample:
    sample_id: str
    session_id: str
    worker_request_id: str
    active_chars: int
    active_limit_chars: int
    total_chars: int
    active_blocks: int
    compacted_blocks: int
    restored_blocks: int
    tool_chars: int
    user_chars: int
    assistant_chars: int
    artifact_refs: int
    pressure: ContextBudgetStatus
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def over_limit_chars(self) -> int:
        return max(0, self.active_chars - self.active_limit_chars)

    @property
    def ratio(self) -> float:
        if self.active_limit_chars <= 0:
            return 1.0 if self.active_chars > 0 else 0.0
        return max(0.0, self.active_chars / self.active_limit_chars)

    @property
    def compact_needed(self) -> bool:
        return self.pressure in {
            ContextBudgetStatus.COMPACT_NEEDED,
            ContextBudgetStatus.OVER_LIMIT,
            ContextBudgetStatus.BLOCKED,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "active_chars": self.active_chars,
            "active_limit_chars": self.active_limit_chars,
            "total_chars": self.total_chars,
            "active_blocks": self.active_blocks,
            "compacted_blocks": self.compacted_blocks,
            "restored_blocks": self.restored_blocks,
            "tool_chars": self.tool_chars,
            "user_chars": self.user_chars,
            "assistant_chars": self.assistant_chars,
            "artifact_refs": self.artifact_refs,
            "over_limit_chars": self.over_limit_chars,
            "ratio": round(self.ratio, 6),
            "pressure": str(self.pressure),
            "compact_needed": self.compact_needed,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CompactCandidate:
    candidate_id: str
    reason: str
    active_chars: int
    active_limit_chars: int
    projected_after_chars: int
    candidate_block_ids: tuple[str, ...]
    protected_block_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    forced: bool = False
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def compact_needed(self) -> bool:
        return self.forced or self.active_chars > self.active_limit_chars or bool(self.candidate_block_ids)

    @property
    def candidate_count(self) -> int:
        return len(self.candidate_block_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "reason": self.reason,
            "active_chars": self.active_chars,
            "active_limit_chars": self.active_limit_chars,
            "projected_after_chars": self.projected_after_chars,
            "over_limit_chars": max(0, self.active_chars - self.active_limit_chars),
            "candidate_block_ids": list(self.candidate_block_ids),
            "protected_block_ids": list(self.protected_block_ids),
            "artifact_ids": list(self.artifact_ids),
            "forced": self.forced,
            "compact_needed": self.compact_needed,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CompactBoundary:
    boundary_id: str
    reason: str
    before_chars: int
    after_chars: int
    compacted_chars: int
    compacted_block_ids: tuple[str, ...]
    summary_block_id: str = ""
    artifact_id: str = ""
    applied: bool = False
    turn_index: int = 0
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    @property
    def reduced_chars(self) -> int:
        return max(0, self.before_chars - self.after_chars)

    def to_dict(self) -> dict[str, Any]:
        return {
            "boundary_id": self.boundary_id,
            "reason": self.reason,
            "before_chars": self.before_chars,
            "after_chars": self.after_chars,
            "compacted_chars": self.compacted_chars,
            "reduced_chars": self.reduced_chars,
            "compacted_block_ids": list(self.compacted_block_ids),
            "summary_block_id": self.summary_block_id,
            "artifact_id": self.artifact_id,
            "applied": self.applied,
            "turn_index": self.turn_index,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class PreservedContextSegment:
    segment_id: str
    block_id: str
    role: str
    state: str
    priority: int
    chars: int
    source_kind: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    artifact_ids: tuple[str, ...] = ()
    reason: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "block_id": self.block_id,
            "role": self.role,
            "state": self.state,
            "priority": self.priority,
            "chars": self.chars,
            "source_kind": self.source_kind,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "artifact_ids": list(self.artifact_ids),
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RestoreSegment:
    segment_id: str
    kind: RestoreSegmentKind
    label: str
    content: str = ""
    artifact_id: str = ""
    source_id: str = ""
    required: bool = True
    budget_chars: int = 0
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return len(self.content)

    @property
    def available(self) -> bool:
        return bool(self.content or self.artifact_id or not self.required)

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "kind": str(self.kind),
            "label": self.label,
            "content": self.content,
            "artifact_id": self.artifact_id,
            "source_id": self.source_id,
            "required": self.required,
            "available": self.available,
            "chars": self.chars,
            "budget_chars": self.budget_chars,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class NextTurnRestoreContract:
    contract_id: str
    session_id: str
    worker_request_id: str
    boundary_id: str
    restore_segments: tuple[RestoreSegment, ...]
    preserved_segments: tuple[PreservedContextSegment, ...]
    compact_artifact_id: str = ""
    resume_token: str = ""
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return all(segment.available for segment in self.restore_segments if segment.required)

    @property
    def restore_segment_count(self) -> int:
        return len(self.restore_segments)

    @property
    def restored_chars(self) -> int:
        return sum(segment.chars for segment in self.restore_segments)

    @property
    def required_segment_count(self) -> int:
        return sum(1 for segment in self.restore_segments if segment.required)

    @property
    def missing_required_segments(self) -> int:
        return sum(1 for segment in self.restore_segments if segment.required and not segment.available)

    def messages(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if self.boundary_id:
            messages.append(
                {
                    "role": "system",
                    "content": f"Context compact boundary {self.boundary_id} restored for next CodeWorker turn.",
                    "metadata": {
                        "kind": "compact_restore_boundary",
                        "boundary_id": self.boundary_id,
                        "compact_artifact_id": self.compact_artifact_id,
                    },
                }
            )
        for segment in self.restore_segments:
            if not segment.available:
                continue
            messages.append(
                {
                    "role": "system" if segment.kind != RestoreSegmentKind.TOOL_RESULT else "tool",
                    "content": segment.content or f"Restore artifact {segment.artifact_id}",
                    "metadata": {
                        "kind": str(segment.kind),
                        "segment_id": segment.segment_id,
                        "artifact_id": segment.artifact_id,
                        "source_id": segment.source_id,
                    },
                }
            )
        return messages

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.next_turn_restore_contract.v1",
            "contract_id": self.contract_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "boundary_id": self.boundary_id,
            "compact_artifact_id": self.compact_artifact_id,
            "resume_token": self.resume_token,
            "ok": self.ok,
            "restore_segment_count": self.restore_segment_count,
            "required_segment_count": self.required_segment_count,
            "missing_required_segments": self.missing_required_segments,
            "restored_chars": self.restored_chars,
            "restore_segments": [segment.to_dict() for segment in self.restore_segments],
            "preserved_segments": [segment.to_dict() for segment in self.preserved_segments],
            "messages": self.messages(),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class CompactRestoreFinding:
    code: str
    severity: CompactRestoreSeverity
    surface: CompactRestoreSurface
    message: str
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == CompactRestoreSeverity.BLOCKER

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
class ContextBudgetReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    usage: ContextUsageSample
    candidate: CompactCandidate | None
    findings: tuple[CompactRestoreFinding, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> ContextBudgetStatus:
        if any(finding.blocking for finding in self.findings):
            return ContextBudgetStatus.BLOCKED
        if self.candidate and self.candidate.compact_needed:
            return ContextBudgetStatus.COMPACT_NEEDED
        return self.usage.pressure

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.context_budget_runtime.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "usage": self.usage.to_dict(),
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "context_budget_report_id": self.report_id,
            "context_budget_owner_unit": self.owner_unit,
            "context_budget_runtime_id": self.runtime_id,
            "context_budget_ok": str(self.ok).lower(),
            "context_budget_status": str(self.status),
            "context_budget_active_chars": str(self.usage.active_chars),
            "context_budget_active_limit_chars": str(self.usage.active_limit_chars),
            "context_budget_over_limit_chars": str(self.usage.over_limit_chars),
            "context_budget_compact_needed": str(self.usage.compact_needed or bool(self.candidate and self.candidate.compact_needed)).lower(),
            "context_budget_candidate_blocks": str(self.candidate.candidate_count if self.candidate else 0),
            "context_budget_findings": str(len(self.findings)),
        }


@dataclass(frozen=True, slots=True)
class CompactRestoreReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    context_budget: ContextBudgetReport
    boundary: CompactBoundary | None
    preserved_segments: tuple[PreservedContextSegment, ...]
    restore_contract: NextTurnRestoreContract | None
    findings: tuple[CompactRestoreFinding, ...]
    source_decisions: tuple[dict[str, str], ...]
    disabled: bool = False
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return (
            not self.disabled
            and self.context_budget.ok
            and not any(finding.blocking for finding in self.findings)
            and (self.restore_contract.ok if self.restore_contract else True)
        )

    @property
    def status(self) -> CompactRestoreStatus:
        if self.disabled:
            return CompactRestoreStatus.DISABLED
        if any(finding.blocking for finding in self.findings) or not self.context_budget.ok:
            return CompactRestoreStatus.BLOCKED
        if self.restore_contract and not self.restore_contract.ok:
            return CompactRestoreStatus.DEGRADED
        if self.boundary and self.boundary.applied:
            return CompactRestoreStatus.RESTORE_READY if self.restore_contract else CompactRestoreStatus.COMPACTED
        if self.context_budget.status == ContextBudgetStatus.COMPACT_NEEDED:
            return CompactRestoreStatus.COMPACT_NEEDED
        if self.findings:
            return CompactRestoreStatus.DEGRADED
        return CompactRestoreStatus.READY

    @property
    def compact_needed(self) -> bool:
        return self.status == CompactRestoreStatus.COMPACT_NEEDED or self.context_budget.status == ContextBudgetStatus.COMPACT_NEEDED

    @property
    def boundary_id(self) -> str:
        return self.boundary.boundary_id if self.boundary else ""

    @property
    def restore_contract_id(self) -> str:
        return self.restore_contract.contract_id if self.restore_contract else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.compact_restore_runtime.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "disabled": self.disabled,
            "compact_needed": self.compact_needed,
            "context_budget": self.context_budget.to_dict(),
            "boundary": self.boundary.to_dict() if self.boundary else None,
            "preserved_segments": [segment.to_dict() for segment in self.preserved_segments],
            "restore_contract": self.restore_contract.to_dict() if self.restore_contract else None,
            "findings": [finding.to_dict() for finding in self.findings],
            "source_decisions": [dict(item) for item in self.source_decisions],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        restore = self.restore_contract
        return {
            "compact_restore_report_id": self.report_id,
            "compact_restore_owner_unit": self.owner_unit,
            "compact_restore_runtime_id": self.runtime_id,
            "compact_restore_ok": str(self.ok).lower(),
            "compact_restore_status": str(self.status),
            "compact_restore_disabled": str(self.disabled).lower(),
            "compact_restore_compact_needed": str(self.compact_needed).lower(),
            "compact_restore_boundary_id": self.boundary_id,
            "compact_restore_boundary_applied": str(bool(self.boundary and self.boundary.applied)).lower(),
            "compact_restore_artifact_id": self.boundary.artifact_id if self.boundary else "",
            "compact_restore_preserved_segments": str(len(self.preserved_segments)),
            "compact_restore_contract_id": self.restore_contract_id,
            "compact_restore_segments": str(restore.restore_segment_count if restore else 0),
            "compact_restore_missing_segments": str(restore.missing_required_segments if restore else 0),
            "compact_restore_findings": str(len(self.findings) + len(self.context_budget.findings)),
            **self.context_budget.metadata(),
        }


class ContextBudgetRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_OWNER_UNIT,
        runtime_id: str = CODEWORKER_API_FOUNDATION_RUNTIME_ID,
        disabled: bool = False,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.disabled = disabled

    def build_report(
        self,
        *,
        context_window_snapshot: Mapping[str, Any],
        budget_state: RuntimeBudgetState,
        force_compact: bool = False,
        reason: str = "budget_threshold",
    ) -> ContextBudgetReport:
        stats = _as_mapping(context_window_snapshot.get("stats"))
        budget = _as_mapping(context_window_snapshot.get("budget"))
        session_id = str(context_window_snapshot.get("session_id") or budget_state.session_id)
        worker_request_id = str(context_window_snapshot.get("worker_request_id") or budget_state.worker_request_id)
        active_chars = _safe_int(stats.get("active_chars"))
        active_limit = _safe_int(budget.get("active_limit") or budget.get("max_chars") or budget_state.context_limit_chars)
        usage_pressure = _context_pressure(active_chars=active_chars, active_limit=active_limit, disabled=self.disabled)
        usage = ContextUsageSample(
            sample_id=new_id("ctx_usage"),
            session_id=session_id,
            worker_request_id=worker_request_id,
            active_chars=active_chars,
            active_limit_chars=active_limit,
            total_chars=_safe_int(stats.get("total_chars")),
            active_blocks=_safe_int(stats.get("active_blocks")),
            compacted_blocks=_safe_int(stats.get("compacted_blocks")),
            restored_blocks=_safe_int(stats.get("restored_blocks")),
            tool_chars=_safe_int(stats.get("tool_chars")),
            user_chars=_safe_int(stats.get("user_chars")),
            assistant_chars=_safe_int(stats.get("assistant_chars")),
            artifact_refs=_safe_int(stats.get("artifact_refs")),
            pressure=usage_pressure,
            metadata={"reason": reason},
        )
        budget_state.record_context_usage(active_chars=active_chars, source="ContextBudgetRuntime")
        findings: list[CompactRestoreFinding] = []
        if self.disabled:
            findings.append(
                CompactRestoreFinding(
                    code="CONTEXT_BUDGET_RUNTIME_DISABLED",
                    severity=CompactRestoreSeverity.BLOCKER,
                    surface=CompactRestoreSurface.CONTEXT_USAGE,
                    message="ContextBudgetRuntime is disabled; compact restore cannot prove context usage.",
                )
            )
        blocks = _blocks(context_window_snapshot)
        active_blocks = [block for block in blocks if str(block.get("state") or "").endswith("active") or str(block.get("state") or "") in {"active", "pinned", "restored"}]
        candidates = _candidate_blocks(active_blocks, active_limit=active_limit, force_compact=force_compact)
        protected = _protected_blocks(active_blocks, candidates)
        artifact_ids = _block_artifact_ids(candidates)
        candidate: CompactCandidate | None = None
        if usage.compact_needed or force_compact or candidates:
            projected_after = max(0, active_chars - sum(_safe_int(block.get("chars")) for block in candidates))
            candidate = CompactCandidate(
                candidate_id=new_id("compact_candidate"),
                reason="forced" if force_compact else reason,
                active_chars=active_chars,
                active_limit_chars=active_limit,
                projected_after_chars=projected_after,
                candidate_block_ids=tuple(str(block.get("block_id") or "") for block in candidates if block.get("block_id")),
                protected_block_ids=tuple(str(block.get("block_id") or "") for block in protected if block.get("block_id")),
                artifact_ids=artifact_ids,
                forced=force_compact,
                metadata={"candidate_policy": "oldest_low_priority_active_blocks"},
            )
        if usage.pressure == ContextBudgetStatus.OVER_LIMIT and not candidates:
            findings.append(
                CompactRestoreFinding(
                    code="CONTEXT_OVER_LIMIT_WITHOUT_CANDIDATES",
                    severity=CompactRestoreSeverity.WARNING,
                    surface=CompactRestoreSurface.COMPACT_CANDIDATE,
                    message="Context is over limit but no compactable candidate block was found.",
                )
            )
        return ContextBudgetReport(
            report_id=new_id("ctx_budget"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            usage=usage,
            candidate=candidate,
            findings=tuple(findings),
        )


class CompactRestoreRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_OWNER_UNIT,
        runtime_id: str = CODEWORKER_API_FOUNDATION_RUNTIME_ID,
        disabled: bool = False,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.disabled = disabled
        self.context_budget_runtime = ContextBudgetRuntime(
            owner_unit=owner_unit,
            runtime_id=runtime_id,
            disabled=disabled,
        )

    def build_report(
        self,
        *,
        context_window_snapshot: Mapping[str, Any],
        budget_state: RuntimeBudgetState,
        tool_result_context_report: Any = None,
        budget_chain_report: Any = None,
        constraints: Mapping[str, Any] | None = None,
        resume_token: str = "",
        workspace_root: str | Path | None = None,
    ) -> CompactRestoreReport:
        constraints = dict(constraints or {})
        force_compact = _truthy(constraints.get("force_compact_restore")) or _truthy(constraints.get("force_context_compact"))
        context_budget = self.context_budget_runtime.build_report(
            context_window_snapshot=context_window_snapshot,
            budget_state=budget_state,
            force_compact=force_compact,
            reason=str(constraints.get("compact_reason") or "budget_threshold"),
        )
        findings: list[CompactRestoreFinding] = []
        if self.disabled:
            findings.append(
                CompactRestoreFinding(
                    code="COMPACT_RESTORE_RUNTIME_DISABLED",
                    severity=CompactRestoreSeverity.BLOCKER,
                    surface=CompactRestoreSurface.COMPACT_BOUNDARY,
                    message="CompactRestoreRuntime is disabled; next-turn restore contract cannot be generated.",
                )
            )
        if not budget_state.ok:
            findings.append(
                CompactRestoreFinding(
                    code="RUNTIME_BUDGET_STATE_NOT_READY",
                    severity=CompactRestoreSeverity.BLOCKER,
                    surface=CompactRestoreSurface.BUDGET_STATE,
                    message="CompactRestoreRuntime requires RuntimeBudgetState custody before building restore contract.",
                )
            )
        boundary = self._latest_boundary(context_window_snapshot, context_budget)
        preserved = self._preserved_segments(context_window_snapshot, candidate=context_budget.candidate)
        restore_segments = self._restore_segments(
            context_window_snapshot=context_window_snapshot,
            tool_result_context_report=tool_result_context_report,
            budget_chain_report=budget_chain_report,
            constraints=constraints,
            boundary=boundary,
            budget_state=budget_state,
            workspace_root=workspace_root,
        )
        restore_contract: NextTurnRestoreContract | None = None
        if boundary is not None or context_budget.candidate is not None or restore_segments:
            restore_contract = NextTurnRestoreContract(
                contract_id=new_id("restore_contract"),
                session_id=context_budget.session_id,
                worker_request_id=context_budget.worker_request_id,
                boundary_id=boundary.boundary_id if boundary else "",
                restore_segments=tuple(restore_segments),
                preserved_segments=tuple(preserved),
                compact_artifact_id=boundary.artifact_id if boundary else "",
                resume_token=resume_token,
            )
            if not restore_contract.ok:
                findings.append(
                    CompactRestoreFinding(
                        code="NEXT_TURN_RESTORE_CONTRACT_MISSING_REQUIRED_SEGMENT",
                        severity=CompactRestoreSeverity.BLOCKER,
                        surface=CompactRestoreSurface.NEXT_TURN,
                        message="Next-turn restore contract is missing one or more required restore segments.",
                    )
                )
        if boundary is not None:
            budget_state.record_compact_boundary(
                boundary_id=boundary.boundary_id,
                reason=boundary.reason,
                before_chars=boundary.before_chars,
                after_chars=boundary.after_chars,
                artifact_id=boundary.artifact_id,
                turn_index=boundary.turn_index,
            )
        if restore_contract is not None:
            budget_state.record_next_turn_restore(
                restore_contract_id=restore_contract.contract_id,
                segment_count=restore_contract.restore_segment_count,
                restored_chars=restore_contract.restored_chars,
            )
        if context_budget.candidate and context_budget.candidate.compact_needed and boundary is None:
            findings.append(
                CompactRestoreFinding(
                    code="COMPACT_NEEDED_WITHOUT_APPLIED_BOUNDARY",
                    severity=CompactRestoreSeverity.WARNING,
                    surface=CompactRestoreSurface.COMPACT_BOUNDARY,
                    message="Context requires a compact boundary; current report carries a compact-needed state for the next runtime pass.",
                )
            )
        return CompactRestoreReport(
            report_id=new_id("compact_restore"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=context_budget.session_id,
            worker_request_id=context_budget.worker_request_id,
            context_budget=context_budget,
            boundary=boundary,
            preserved_segments=tuple(preserved),
            restore_contract=restore_contract,
            findings=tuple(findings),
            source_decisions=default_compact_restore_source_decisions(),
            disabled=self.disabled,
        )

    def event_for_report(
        self,
        report: CompactRestoreReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        phase: str = "compact_restore_report",
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
                    "compact_restore": report.to_dict(),
                }
            },
        )

    def boundary_event(
        self,
        report: CompactRestoreReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> EventRecord | None:
        if report.boundary is None and not report.compact_needed:
            return None
        phase = "compact_boundary_created" if report.boundary and report.boundary.applied else "compact_needed"
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
                    "compact_boundary": report.boundary.to_dict() if report.boundary else {},
                    "context_budget": report.context_budget.to_dict(),
                    "compact_restore_report_id": report.report_id,
                }
            },
        )

    def restore_event(
        self,
        report: CompactRestoreReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> EventRecord | None:
        if report.restore_contract is None:
            return None
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": report.session_id,
                    "worker_request_id": report.worker_request_id,
                    "phase": "next_turn_restore_contract",
                    "next_turn_restore_contract": report.restore_contract.to_dict(),
                    "compact_restore_report_id": report.report_id,
                }
            },
        )

    def _latest_boundary(
        self,
        context_window_snapshot: Mapping[str, Any],
        context_budget: ContextBudgetReport,
    ) -> CompactBoundary | None:
        compactions = [
            item for item in _as_list(context_window_snapshot.get("compactions")) if isinstance(item, Mapping)
        ]
        applied = [item for item in compactions if item.get("applied") is True]
        if applied:
            latest = applied[-1]
            artifact = _as_mapping(latest.get("artifact"))
            summary = _as_mapping(latest.get("summary_block"))
            boundary = CompactBoundary(
                boundary_id=new_id("compact_boundary"),
                reason=str(latest.get("reason") or "budget_exceeded"),
                before_chars=_safe_int(latest.get("before_chars")),
                after_chars=_safe_int(latest.get("after_chars")),
                compacted_chars=_safe_int(latest.get("compacted_chars")),
                compacted_block_ids=tuple(str(item) for item in _as_list(latest.get("compacted_block_ids"))),
                summary_block_id=str(summary.get("block_id") or ""),
                artifact_id=str(artifact.get("artifact_id") or ""),
                applied=True,
                turn_index=_safe_int(_as_mapping(latest.get("metadata")).get("turn_index")),
                metadata={"source": "ClaudeContextWindowManager.maybe_compact"},
            )
            return boundary
        candidate = context_budget.candidate
        if candidate is None:
            return None
        return CompactBoundary(
            boundary_id=new_id("compact_boundary"),
            reason=candidate.reason,
            before_chars=candidate.active_chars,
            after_chars=candidate.projected_after_chars,
            compacted_chars=max(0, candidate.active_chars - candidate.projected_after_chars),
            compacted_block_ids=candidate.candidate_block_ids,
            applied=False,
            metadata={"source": "ContextBudgetRuntime.compact_needed_state"},
        )

    def _preserved_segments(
        self,
        context_window_snapshot: Mapping[str, Any],
        *,
        candidate: CompactCandidate | None,
    ) -> list[PreservedContextSegment]:
        candidate_ids = set(candidate.candidate_block_ids if candidate else ())
        blocks = _blocks(context_window_snapshot)
        active = [block for block in blocks if str(block.get("state") or "") in {"active", "pinned", "restored"}]
        preserved_blocks = [
            block
            for block in active
            if str(block.get("block_id") or "") not in candidate_ids
            and (
                _safe_int(block.get("priority")) >= 700
                or str(block.get("role") or "").endswith("system")
                or block in active[-5:]
            )
        ]
        segments: list[PreservedContextSegment] = []
        for block in preserved_blocks[:24]:
            source = _as_mapping(block.get("source"))
            segments.append(
                PreservedContextSegment(
                    segment_id=new_id("preserved"),
                    block_id=str(block.get("block_id") or ""),
                    role=str(block.get("role") or ""),
                    state=str(block.get("state") or ""),
                    priority=_safe_int(block.get("priority")),
                    chars=_safe_int(block.get("chars")),
                    source_kind=str(source.get("source_kind") or ""),
                    tool_call_id=str(block.get("tool_call_id") or ""),
                    tool_name=str(block.get("tool_name") or ""),
                    artifact_ids=tuple(str(item) for item in _as_list(block.get("artifact_ids"))),
                    reason="recent_or_high_priority",
                    metadata={"created_at": str(block.get("created_at") or "")},
                )
            )
        return segments

    def _restore_segments(
        self,
        *,
        context_window_snapshot: Mapping[str, Any],
        tool_result_context_report: Any,
        budget_chain_report: Any,
        constraints: Mapping[str, Any],
        boundary: CompactBoundary | None,
        budget_state: RuntimeBudgetState,
        workspace_root: str | Path | None,
    ) -> list[RestoreSegment]:
        segments: list[RestoreSegment] = []
        if boundary and boundary.artifact_id:
            segments.append(
                RestoreSegment(
                    segment_id=new_id("restore"),
                    kind=RestoreSegmentKind.CONTEXT_SUMMARY,
                    label="compact-summary",
                    content=f"Compact summary artifact {boundary.artifact_id} preserves {len(boundary.compacted_block_ids)} block(s).",
                    artifact_id=boundary.artifact_id,
                    source_id=boundary.boundary_id,
                    budget_chars=280,
                    metadata={
                        "applied": str(boundary.applied).lower(),
                        "source_provenance": "compact_summary",
                        "trust_level": "workspace",
                        "secret_redaction_state": "clean",
                        "source_ref": boundary.artifact_id,
                        "retrieval_query": "compact boundary summary",
                        "retrieval_scope": "query_session_context_window",
                        "retrieval_budget": "280",
                        "code_index_source": "false",
                        "source_path": "packages/runtime/zyra_runtime/compact_restore_runtime.py",
                        "upstream_source_path": "src/services/compact/compact.ts",
                    },
                )
            )
        for projection in getattr(tool_result_context_report, "projections", ()) or ():
            content = ""
            session_message = getattr(projection, "session_message", None)
            if session_message is not None:
                content = str(getattr(session_message, "content", "") or "")
            artifact_ids = tuple(str(item) for item in getattr(projection, "artifact_ids", ()) or ())
            externalized = str(getattr(projection, "externalized_artifact_id", "") or "")
            artifact_id = externalized or (artifact_ids[0] if artifact_ids else "")
            if not content and artifact_id:
                content = f"Tool result artifact {artifact_id} remains available for next turn."
            if not content and not artifact_id:
                continue
            segments.append(
                RestoreSegment(
                    segment_id=new_id("restore"),
                    kind=RestoreSegmentKind.TOOL_RESULT,
                    label=str(getattr(projection, "tool_name", "") or "tool_result"),
                    content=_clip(content, 1200),
                    artifact_id=artifact_id,
                    source_id=str(getattr(projection, "tool_call_id", "") or ""),
                    required=False,
                    budget_chars=_safe_int(getattr(projection, "inline_chars", 0)),
                    metadata={
                        "projection_id": str(getattr(projection, "projection_id", "") or ""),
                        "budget_applied": str(bool(getattr(projection, "budget_applied", False))).lower(),
                        "source_provenance": "tool_result",
                        "trust_level": "tool_output",
                        "secret_redaction_state": "clean",
                        "source_ref": str(getattr(projection, "tool_call_id", "") or artifact_id),
                        "retrieval_query": str(getattr(projection, "tool_name", "") or "tool_result"),
                        "retrieval_scope": "tool_result_context_report",
                        "retrieval_budget": str(_safe_int(getattr(projection, "inline_chars", 0))),
                        "code_index_source": "false",
                        "source_path": "packages/runtime/zyra_runtime/tool_runtime_result_context.py",
                        "upstream_source_path": "src/utils/toolResultStorage.ts",
                    },
                )
            )
        for path in _string_list(constraints.get("restore_files") or constraints.get("file_restore_paths")):
            file_restore = _workspace_file_restore_payload(path, workspace_root=workspace_root)
            segments.append(
                RestoreSegment(
                    segment_id=new_id("restore"),
                    kind=RestoreSegmentKind.FILE_ATTACHMENT,
                    label=path,
                    content=file_restore["content"],
                    source_id=path,
                    required=True,
                    budget_chars=_safe_int(file_restore["metadata"].get("retrieval_budget"), len(path)),
                    metadata={
                        "source_provenance": "workspace_file",
                        "trust_level": "workspace",
                        "secret_redaction_state": "clean",
                        "source_ref": path,
                        "retrieval_query": path,
                        "retrieval_scope": "workspace_file_restore",
                        **file_restore["metadata"],
                        "code_index_source": "true",
                        "source_path": path,
                        "upstream_source_path": "src/services/compact/sessionMemoryCompact.ts",
                    },
                )
            )
        plan = constraints.get("active_plan") or constraints.get("restore_plan")
        if plan:
            segments.append(
                RestoreSegment(
                    segment_id=new_id("restore"),
                    kind=RestoreSegmentKind.ACTIVE_PLAN,
                    label="active-plan",
                    content=_clip(str(plan), 2000),
                    source_id="constraints.active_plan",
                    required=True,
                    budget_chars=len(str(plan)),
                    metadata={
                        "source_provenance": "active_plan",
                        "trust_level": "workspace",
                        "secret_redaction_state": "clean",
                        "source_ref": "constraints.active_plan",
                        "retrieval_query": "active_plan",
                        "retrieval_scope": "worker_request_constraints",
                        "retrieval_budget": str(len(str(plan))),
                        "code_index_source": "false",
                        "source_path": "packages/runtime/zyra_runtime/compact_restore_runtime.py",
                        "upstream_source_path": "src/QueryEngine.ts",
                    },
                )
            )
        for skill in _string_list(constraints.get("invoked_skills") or constraints.get("restore_skills")):
            segments.append(
                RestoreSegment(
                    segment_id=new_id("restore"),
                    kind=RestoreSegmentKind.INVOKED_SKILL,
                    label=skill,
                    content=f"Invoked skill memory retained for next turn: {skill}",
                    source_id=skill,
                    required=False,
                    budget_chars=len(skill),
                    metadata={
                        "source_provenance": "invoked_skill",
                        "trust_level": "workspace",
                        "secret_redaction_state": "clean",
                        "source_ref": skill,
                        "retrieval_query": skill,
                        "retrieval_scope": "skill_memory",
                        "retrieval_budget": str(len(skill)),
                        "code_index_source": "false",
                        "source_path": "packages/runtime/zyra_runtime/compact_restore_runtime.py",
                        "upstream_source_path": "src/tools/SkillTool",
                    },
                )
            )
        for delta in _mcp_deltas(constraints):
            segments.append(
                RestoreSegment(
                    segment_id=new_id("restore"),
                    kind=RestoreSegmentKind.MCP_INSTRUCTION_DELTA,
                    label=str(delta.get("server") or "mcp"),
                    content=_clip(str(delta.get("instructions") or delta), 1800),
                    source_id=str(delta.get("server") or ""),
                    required=False,
                    budget_chars=len(str(delta.get("instructions") or "")),
                    metadata={
                        "delta_id": str(delta.get("id") or ""),
                        "source_provenance": "mcp_instruction",
                        "trust_level": "external_untrusted",
                        "secret_redaction_state": "clean",
                        "source_ref": str(delta.get("server") or "mcp"),
                        "retrieval_query": str(delta.get("server") or "mcp"),
                        "retrieval_scope": "mcp_instruction_delta",
                        "retrieval_budget": str(len(str(delta.get("instructions") or ""))),
                        "code_index_source": "false",
                        "external": "true",
                        "source_path": "packages/runtime/zyra_runtime/compact_restore_runtime.py",
                        "upstream_source_path": "src/services/mcpClient.ts",
                    },
                )
            )
        for tool in _string_list(constraints.get("deferred_tools")):
            segments.append(
                RestoreSegment(
                    segment_id=new_id("restore"),
                    kind=RestoreSegmentKind.DEFERRED_TOOL,
                    label=tool,
                    content=f"Deferred tool remains pending after compact: {tool}",
                    source_id=tool,
                    required=False,
                    budget_chars=len(tool),
                    metadata={
                        "source_provenance": "deferred_tool",
                        "trust_level": "trusted_system",
                        "secret_redaction_state": "clean",
                        "source_ref": tool,
                        "retrieval_query": tool,
                        "retrieval_scope": "deferred_tool_state",
                        "retrieval_budget": str(len(tool)),
                        "code_index_source": "false",
                        "source_path": "packages/runtime/zyra_runtime/compact_restore_runtime.py",
                        "upstream_source_path": "src/services/tools/toolOrchestration.ts",
                    },
                )
            )
        budget_snapshot = budget_state.snapshot()
        segments.append(
            RestoreSegment(
                segment_id=new_id("restore"),
                kind=RestoreSegmentKind.BUDGET_STATE,
                label="runtime-budget-state",
                content=f"Runtime budget state {budget_snapshot.snapshot_id}: context={budget_snapshot.metadata().get('runtime_budget_state_context_used_chars')} retries={budget_snapshot.retry_count}.",
                source_id=budget_snapshot.snapshot_id,
                required=True,
                budget_chars=320,
                metadata={
                    "status": str(budget_snapshot.status),
                    "highest_pressure": str(budget_snapshot.highest_pressure),
                    "source_provenance": "runtime_budget",
                    "trust_level": "trusted_system",
                    "secret_redaction_state": "clean",
                    "source_ref": budget_snapshot.snapshot_id,
                    "retrieval_query": "runtime_budget_state",
                    "retrieval_scope": "RuntimeBudgetState",
                    "retrieval_budget": "320",
                    "code_index_source": "false",
                    "source_path": "packages/runtime/zyra_runtime/runtime_budget_state.py",
                    "upstream_source_path": "opencode/packages/opencode/src/session",
                },
            )
        )
        return segments


def compact_restore_metadata(report: CompactRestoreReport | None) -> dict[str, str]:
    if report is None:
        return {
            "compact_restore_ok": "false",
            "compact_restore_status": "missing",
            "compact_restore_report_id": "",
        }
    return report.metadata()


def render_compact_restore_markdown(report: CompactRestoreReport) -> str:
    lines = [
        "# Compact Restore Runtime",
        "",
        f"- owner_unit: {report.owner_unit}",
        f"- runtime_id: {report.runtime_id}",
        f"- status: {report.status}",
        f"- ok: {str(report.ok).lower()}",
        f"- compact_needed: {str(report.compact_needed).lower()}",
        f"- boundary_id: {report.boundary_id}",
        f"- restore_contract_id: {report.restore_contract_id}",
        "",
        "## Context Budget",
        f"- active_chars: {report.context_budget.usage.active_chars}",
        f"- active_limit_chars: {report.context_budget.usage.active_limit_chars}",
        f"- pressure: {report.context_budget.usage.pressure}",
        "",
        "## Preserved Segments",
    ]
    for segment in report.preserved_segments[:20]:
        lines.append(f"- {segment.role} {segment.block_id} chars={segment.chars} reason={segment.reason}")
    lines.extend(["", "## Restore Segments"])
    if report.restore_contract is not None:
        for segment in report.restore_contract.restore_segments:
            lines.append(f"- {segment.kind} {segment.label} required={str(segment.required).lower()} chars={segment.chars}")
    lines.extend(["", "## Findings"])
    all_findings = [*report.context_budget.findings, *report.findings]
    if all_findings:
        for finding in all_findings:
            lines.append(f"- {finding.severity} {finding.code}: {finding.message}")
    else:
        lines.append("- none")
    return "\n".join(lines)


def default_compact_restore_source_decisions() -> tuple[dict[str, str], ...]:
    return (
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/compact/compact.ts",
            "target_path": "packages/runtime/zyra_runtime/compact_restore_runtime.py",
            "decision": "zyra_module_migrated",
            "capability": "durable compact boundary and post-compact restore contract",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/compact/sessionMemoryCompact.ts",
            "target_path": "packages/runtime/zyra_runtime/compact_restore_runtime.py",
            "decision": "zyra_module_migrated",
            "capability": "message-pair-safe compact state and restored segment metadata",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/compact/postCompactCleanup.ts",
            "target_path": "packages/runtime/zyra_runtime/compact_restore_runtime.py",
            "decision": "zyra_module_migrated",
            "capability": "next-turn cleanup and restoration of file/skill/MCP/tool state attachments",
        },
        {
            "source_repo": "opencode",
            "source_path": "packages/opencode/src/session/*",
            "target_path": "packages/runtime/zyra_runtime/runtime_budget_state.py",
            "decision": "adapter_encapsulated",
            "capability": "session context epoch and usage state custody adapted into RuntimeBudgetState",
        },
    )


def _context_pressure(*, active_chars: int, active_limit: int, disabled: bool) -> ContextBudgetStatus:
    if disabled or active_limit <= 0:
        return ContextBudgetStatus.BLOCKED
    if active_chars > active_limit:
        return ContextBudgetStatus.OVER_LIMIT
    ratio = active_chars / active_limit if active_limit else 0.0
    if ratio >= 0.92:
        return ContextBudgetStatus.COMPACT_NEEDED
    if ratio >= 0.76:
        return ContextBudgetStatus.WATCH
    return ContextBudgetStatus.READY


def _blocks(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [item for item in _as_list(snapshot.get("blocks")) if isinstance(item, Mapping)]


def _candidate_blocks(blocks: Sequence[Mapping[str, Any]], *, active_limit: int, force_compact: bool) -> list[Mapping[str, Any]]:
    if not blocks:
        return []
    compactable = [
        block
        for block in blocks
        if _safe_int(block.get("priority")) < 900
        and str(block.get("role") or "") not in {"system", "control", "ClaudeContextBlockRole.SYSTEM", "ClaudeContextBlockRole.CONTROL"}
    ]
    compactable.sort(key=lambda block: (_safe_int(block.get("priority")), str(block.get("created_at") or ""), str(block.get("block_id") or "")))
    total = sum(_safe_int(block.get("chars")) for block in blocks)
    target_reduce = max(0, total - active_limit)
    if force_compact and target_reduce <= 0:
        target_reduce = max(1, int(total * 0.25))
    selected: list[Mapping[str, Any]] = []
    selected_chars = 0
    for block in compactable:
        selected.append(block)
        selected_chars += _safe_int(block.get("chars"))
        if selected_chars >= target_reduce:
            break
    return selected


def _protected_blocks(
    blocks: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    candidate_ids = {str(block.get("block_id") or "") for block in candidates}
    protected = [
        block
        for block in blocks
        if str(block.get("block_id") or "") not in candidate_ids
        and (_safe_int(block.get("priority")) >= 700 or block in blocks[-5:])
    ]
    return protected[:24]


def _block_artifact_ids(blocks: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    ids: list[str] = []
    for block in blocks:
        for artifact_id in _as_list(block.get("artifact_ids")):
            value = str(artifact_id or "")
            if value and value not in ids:
                ids.append(value)
    return tuple(ids)


def _mcp_deltas(constraints: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = constraints.get("mcp_instruction_deltas") or constraints.get("restore_mcp_instruction_deltas")
    if isinstance(raw, Mapping):
        return [
            {"server": str(key), "instructions": str(value), "id": str(key)}
            for key, value in raw.items()
        ]
    if isinstance(raw, list):
        return [item if isinstance(item, Mapping) else {"server": str(item), "instructions": str(item)} for item in raw]
    if raw:
        return [{"server": "mcp", "instructions": str(raw)}]
    return []


def _string_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value)]


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "force"}


def _clip(text: str, max_chars: int) -> str:
    value = str(text)
    if len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - 3)] + "..."


def _workspace_file_restore_payload(path: str, *, workspace_root: str | Path | None) -> dict[str, Any]:
    metadata: dict[str, str] = {
        "restore_file_path": path,
        "restore_file_status": "path_only",
        "restore_file_sha256": "",
        "restore_file_size_bytes": "0",
        "restore_file_preview_chars": "0",
        "retrieval_budget": str(len(path)),
    }
    if workspace_root is None or str(workspace_root) == "":
        return {
            "content": f"Restore file attachment path: {path}",
            "metadata": metadata,
        }
    try:
        root = Path(workspace_root).resolve()
        candidate = (root / path).resolve()
        candidate.relative_to(root)
    except (OSError, ValueError):
        metadata["restore_file_status"] = "outside_workspace"
        return {
            "content": f"Restore file attachment rejected outside workspace: {path}",
            "metadata": metadata,
        }
    if not candidate.is_file():
        metadata["restore_file_status"] = "missing"
        return {
            "content": f"Restore file attachment missing in workspace: {path}",
            "metadata": metadata,
        }
    digest = hashlib.sha256()
    size_bytes = 0
    preview = b""
    try:
        with candidate.open("rb") as handle:
            while True:
                chunk = handle.read(8192)
                if not chunk:
                    break
                if len(preview) < 4096:
                    preview += chunk[: max(0, 4096 - len(preview))]
                size_bytes += len(chunk)
                digest.update(chunk)
    except OSError:
        metadata["restore_file_status"] = "unreadable"
        return {
            "content": f"Restore file attachment unreadable in workspace: {path}",
            "metadata": metadata,
        }
    preview_text = _clip(preview.decode("utf-8", errors="replace"), 1600)
    metadata.update(
        {
            "restore_file_status": "available",
            "restore_file_sha256": digest.hexdigest(),
            "restore_file_size_bytes": str(size_bytes),
            "restore_file_preview_chars": str(len(preview_text)),
            "retrieval_budget": str(len(preview_text) + 160),
        }
    )
    return {
        "content": (
            f"Workspace file restore: {path}\n"
            f"size_bytes: {size_bytes}\n"
            f"sha256: {digest.hexdigest()}\n"
            "preview:\n"
            f"{preview_text}"
        ),
        "metadata": metadata,
    }
