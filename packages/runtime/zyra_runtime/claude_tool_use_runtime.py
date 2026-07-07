from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import ArtifactRef, new_id, now_iso, to_jsonable

from .tool_loop import (
    ToolAccessMode,
    ToolBudgetDecision,
    ToolFailureKind,
    ToolFailureSignal,
    ToolLoopBatch,
    ToolLoopPlan,
    ToolLoopRequest,
    ToolSchemaViolation,
    ToolSignalSeverity,
)
from .tools import ToolResult


class ClaudeToolUseStage(StrEnum):
    PLANNED = "planned"
    PERMISSION_CHECKED = "permission_checked"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    EXTERNALIZED = "externalized"
    WATCHDOG_SIGNALED = "watchdog_signaled"
    SKIPPED = "skipped"


class ClaudeToolSemanticEffectKind(StrEnum):
    READ_WORKSPACE = "read_workspace"
    WRITE_WORKSPACE = "write_workspace"
    EDIT_WORKSPACE = "edit_workspace"
    RUN_SHELL = "run_shell"
    READ_BROWSER = "read_browser"
    SEARCH_EVIDENCE = "search_evidence"
    WRITE_ARTIFACT = "write_artifact"
    READ_CHECKPOINT = "read_checkpoint"
    READ_TRACE = "read_trace"
    UNKNOWN = "unknown"


class ClaudeToolResultBlockKind(StrEnum):
    CONTENT = "content"
    ERROR = "error"
    ARTIFACT_REF = "artifact_ref"
    BUDGET_REF = "budget_ref"
    PERMISSION = "permission"
    WATCHDOG = "watchdog"


class ClaudeToolUseRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ClaudeToolSemanticEffect:
    kind: ClaudeToolSemanticEffectKind
    subject: str
    mutates_workspace: bool
    reads_workspace: bool
    requires_permission: bool
    risk: ClaudeToolUseRisk
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ClaudeToolUseEnvelope:
    tool_use_id: str
    tool_name: str
    arguments: dict[str, Any]
    turn_index: int
    batch_index: int
    step_index: int
    access_mode: str
    read_only: bool
    conflict_key: str
    schema_errors: list[ToolSchemaViolation] = field(default_factory=list)
    semantic_effects: list[ClaudeToolSemanticEffect] = field(default_factory=list)
    source_path: str = "packages/runtime/zyra_runtime/tool_loop.py"
    upstream_source_path: str = "src/services/tools/toolOrchestration.ts"
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return not self.schema_errors

    @property
    def mutates_workspace(self) -> bool:
        return any(effect.mutates_workspace for effect in self.semantic_effects)

    @property
    def max_risk(self) -> ClaudeToolUseRisk:
        ranks = {
            ClaudeToolUseRisk.LOW: 0,
            ClaudeToolUseRisk.MEDIUM: 1,
            ClaudeToolUseRisk.HIGH: 2,
            ClaudeToolUseRisk.BLOCKED: 3,
        }
        selected = ClaudeToolUseRisk.LOW
        for effect in self.semantic_effects:
            if ranks[effect.risk] > ranks[selected]:
                selected = effect.risk
        return selected

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_use_id": self.tool_use_id,
            "tool_name": self.tool_name,
            "arguments": to_jsonable(self.arguments),
            "turn_index": self.turn_index,
            "batch_index": self.batch_index,
            "step_index": self.step_index,
            "access_mode": self.access_mode,
            "read_only": self.read_only,
            "conflict_key": self.conflict_key,
            "valid": self.valid,
            "mutates_workspace": self.mutates_workspace,
            "max_risk": str(self.max_risk),
            "schema_errors": [item.to_dict() for item in self.schema_errors],
            "semantic_effects": [item.to_dict() for item in self.semantic_effects],
            "source_path": self.source_path,
            "upstream_source_path": self.upstream_source_path,
            "created_at": self.created_at,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ClaudeToolResultBlock:
    block_id: str
    tool_use_id: str
    tool_name: str
    kind: ClaudeToolResultBlockKind
    ok: bool
    text: str
    artifact_ids: list[str] = field(default_factory=list)
    error: str | None = None
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return len(self.text)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ClaudeToolUseTraceEntry:
    trace_id: str
    tool_use_id: str
    tool_name: str
    stage: ClaudeToolUseStage
    ok: bool
    message: str
    created_at: str = field(default_factory=now_iso)
    result_block_id: str = ""
    signal_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ClaudeToolUseBatchDigest:
    turn_index: int
    batch_index: int
    execution_mode: str
    tool_use_ids: list[str]
    tool_names: list[str]
    read_only: bool
    mutates_workspace: bool
    conflict_protected: bool
    schema_error_count: int
    failure_count: int
    budget_externalization_count: int
    result_chars: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ClaudeToolRuntimeStats:
    planned_count: int
    completed_count: int
    failed_count: int
    schema_error_count: int
    permission_denied_count: int
    budget_externalization_count: int
    watchdog_signal_count: int
    artifact_ref_count: int
    mutating_count: int
    read_only_count: int
    total_result_chars: int
    high_risk_count: int
    blocked_risk_count: int

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


class ClaudeToolUseRuntime:
    """Maps Zyra tool execution into Claude-Code-style tool-use semantics."""

    def __init__(self, *, runtime_source: str, runtime_id: str, owner_unit: str = "M1-02A") -> None:
        self.runtime_source = runtime_source
        self.runtime_id = runtime_id
        self.owner_unit = owner_unit
        self._envelopes: dict[str, ClaudeToolUseEnvelope] = {}
        self._result_blocks: list[ClaudeToolResultBlock] = []
        self._trace: list[ClaudeToolUseTraceEntry] = []
        self._batch_digests: list[ClaudeToolUseBatchDigest] = []
        self._signals: list[ToolFailureSignal] = []
        self._budget_decisions: list[ToolBudgetDecision] = []

    @property
    def envelopes(self) -> list[ClaudeToolUseEnvelope]:
        return list(self._envelopes.values())

    @property
    def result_blocks(self) -> list[ClaudeToolResultBlock]:
        return list(self._result_blocks)

    @property
    def trace(self) -> list[ClaudeToolUseTraceEntry]:
        return list(self._trace)

    @property
    def batch_digests(self) -> list[ClaudeToolUseBatchDigest]:
        return list(self._batch_digests)

    def register_plan(self, plan: ToolLoopPlan) -> list[ClaudeToolUseEnvelope]:
        envelopes: list[ClaudeToolUseEnvelope] = []
        for request in plan.requests:
            envelope = self.envelope_from_request(request, batch_index=_batch_index_for_request(plan.batches, request))
            self._envelopes[envelope.tool_use_id] = envelope
            envelopes.append(envelope)
            self._trace.append(
                ClaudeToolUseTraceEntry(
                    trace_id=new_id("tooltrace"),
                    tool_use_id=envelope.tool_use_id,
                    tool_name=envelope.tool_name,
                    stage=ClaudeToolUseStage.PLANNED,
                    ok=envelope.valid,
                    message="Tool use planned" if envelope.valid else "Tool use has schema errors",
                    metadata={
                        "turn_index": envelope.turn_index,
                        "step_index": envelope.step_index,
                        "schema_error_count": len(envelope.schema_errors),
                        "source_path": envelope.source_path,
                    },
                )
            )
        return envelopes

    def mark_started(self, request: ToolLoopRequest, *, batch_index: int) -> ClaudeToolUseTraceEntry:
        envelope = self._envelope_for_request(request, batch_index=batch_index)
        entry = ClaudeToolUseTraceEntry(
            trace_id=new_id("tooltrace"),
            tool_use_id=envelope.tool_use_id,
            tool_name=envelope.tool_name,
            stage=ClaudeToolUseStage.STARTED,
            ok=True,
            message="Tool execution started",
            metadata={
                "turn_index": envelope.turn_index,
                "batch_index": batch_index,
                "step_index": envelope.step_index,
                "access_mode": envelope.access_mode,
                "read_only": envelope.read_only,
                "conflict_key": envelope.conflict_key,
            },
        )
        self._trace.append(entry)
        return entry

    def record_result(
        self,
        request: ToolLoopRequest,
        result: ToolResult,
        *,
        batch_index: int,
        budget_decision: ToolBudgetDecision | None = None,
        failure_signal: ToolFailureSignal | None = None,
    ) -> ClaudeToolResultBlock:
        envelope = self._envelope_for_request(request, batch_index=batch_index)
        if budget_decision is not None and budget_decision.applied:
            self._budget_decisions.append(budget_decision)
        if failure_signal is not None:
            self._signals.append(failure_signal)
        block = self.result_block_from_result(envelope, result, budget_decision=budget_decision, failure_signal=failure_signal)
        self._result_blocks.append(block)
        stage = ClaudeToolUseStage.COMPLETED if result.ok else ClaudeToolUseStage.FAILED
        if budget_decision is not None and budget_decision.applied:
            stage = ClaudeToolUseStage.EXTERNALIZED
        if failure_signal is not None and not result.ok:
            stage = ClaudeToolUseStage.WATCHDOG_SIGNALED
        self._trace.append(
            ClaudeToolUseTraceEntry(
                trace_id=new_id("tooltrace"),
                tool_use_id=envelope.tool_use_id,
                tool_name=envelope.tool_name,
                stage=stage,
                ok=result.ok,
                message=result.summary,
                result_block_id=block.block_id,
                signal_id=failure_signal.signal_id if failure_signal else "",
                metadata={
                    "error": result.error,
                    "artifact_ids": [artifact.artifact_id for artifact in result.artifacts],
                    "budget_applied": bool(budget_decision and budget_decision.applied),
                    "permission_effect": str(result.metadata.get("permission_effect") or ""),
                },
            )
        )
        return block

    def record_batch_digest(
        self,
        batch: ToolLoopBatch,
        results: Sequence[ToolResult],
        *,
        budget_decisions: Sequence[ToolBudgetDecision | None] = (),
    ) -> ClaudeToolUseBatchDigest:
        requests = list(batch.requests)
        envelopes = [self._envelope_for_request(request, batch_index=batch.batch_index) for request in requests]
        digest = ClaudeToolUseBatchDigest(
            turn_index=requests[0].turn_index if requests else 0,
            batch_index=batch.batch_index,
            execution_mode=str(batch.execution_mode),
            tool_use_ids=[item.tool_use_id for item in envelopes],
            tool_names=[item.tool_name for item in envelopes],
            read_only=batch.read_only,
            mutates_workspace=any(item.mutates_workspace for item in envelopes),
            conflict_protected=batch.conflict_protected,
            schema_error_count=sum(len(item.schema_errors) for item in envelopes),
            failure_count=sum(1 for item in results if not item.ok),
            budget_externalization_count=sum(1 for item in budget_decisions if item is not None and item.applied),
            result_chars=sum(tool_result_chars(item) for item in results),
            metadata={
                "conflict_keys": list(batch.conflict_keys),
                "runtime_source": self.runtime_source,
                "runtime_id": self.runtime_id,
            },
        )
        self._batch_digests.append(digest)
        return digest

    def result_block_from_result(
        self,
        envelope: ClaudeToolUseEnvelope,
        result: ToolResult,
        *,
        budget_decision: ToolBudgetDecision | None = None,
        failure_signal: ToolFailureSignal | None = None,
    ) -> ClaudeToolResultBlock:
        if failure_signal is not None:
            kind = ClaudeToolResultBlockKind.WATCHDOG
        elif budget_decision is not None and budget_decision.applied:
            kind = ClaudeToolResultBlockKind.BUDGET_REF
        elif not result.ok:
            kind = ClaudeToolResultBlockKind.ERROR
        elif result.artifacts:
            kind = ClaudeToolResultBlockKind.ARTIFACT_REF
        else:
            kind = ClaudeToolResultBlockKind.CONTENT
        text = self._result_text(envelope, result, budget_decision=budget_decision, failure_signal=failure_signal)
        return ClaudeToolResultBlock(
            block_id=new_id("toolblock"),
            tool_use_id=envelope.tool_use_id,
            tool_name=envelope.tool_name,
            kind=kind,
            ok=result.ok,
            text=text,
            artifact_ids=[artifact.artifact_id for artifact in result.artifacts],
            error=result.error,
            metadata={
                "runtime_source": self.runtime_source,
                "runtime_id": self.runtime_id,
                "owner_unit": self.owner_unit,
                "semantic_effects": [effect.to_dict() for effect in envelope.semantic_effects],
                "budget": budget_decision.to_dict() if budget_decision else None,
                "signal": failure_signal.to_dict() if failure_signal else None,
                "permission_effect": str(result.metadata.get("permission_effect") or ""),
            },
        )

    def envelope_from_request(self, request: ToolLoopRequest, *, batch_index: int) -> ClaudeToolUseEnvelope:
        return ClaudeToolUseEnvelope(
            tool_use_id=request.call.tool_call_id,
            tool_name=request.tool_name,
            arguments=dict(request.arguments),
            turn_index=request.turn_index,
            batch_index=batch_index,
            step_index=request.step_index,
            access_mode=str(request.access_mode),
            read_only=request.read_only,
            conflict_key=request.conflict_key,
            schema_errors=list(request.schema_errors),
            semantic_effects=semantic_effects_for_request(request),
            source_path=request.source_path,
            upstream_source_path=str(request.metadata.get("upstream_source_path") or _upstream_source_for_tool(request.tool_name)),
            metadata={
                "runtime_source": self.runtime_source,
                "runtime_id": self.runtime_id,
                "node_id": str(request.node_id or ""),
                "worker_request_id": request.worker_request_id,
                **dict(request.metadata),
            },
        )

    def stats(self) -> ClaudeToolRuntimeStats:
        envelopes = self.envelopes
        blocks = self.result_blocks
        failed_blocks = [block for block in blocks if not block.ok]
        permission_denied = [
            block
            for block in failed_blocks
            if str(block.error or "").lower() in {"permission_denied", "permission_required"}
            or str(block.metadata.get("permission_effect") or "").endswith("deny")
        ]
        return ClaudeToolRuntimeStats(
            planned_count=len(envelopes),
            completed_count=sum(1 for block in blocks if block.ok),
            failed_count=len(failed_blocks),
            schema_error_count=sum(len(item.schema_errors) for item in envelopes),
            permission_denied_count=len(permission_denied),
            budget_externalization_count=sum(1 for item in self._budget_decisions if item.applied),
            watchdog_signal_count=len(self._signals),
            artifact_ref_count=sum(len(block.artifact_ids) for block in blocks),
            mutating_count=sum(1 for item in envelopes if item.mutates_workspace),
            read_only_count=sum(1 for item in envelopes if item.read_only),
            total_result_chars=sum(block.chars for block in blocks),
            high_risk_count=sum(1 for item in envelopes if item.max_risk == ClaudeToolUseRisk.HIGH),
            blocked_risk_count=sum(1 for item in envelopes if item.max_risk == ClaudeToolUseRisk.BLOCKED),
        )

    def metadata(self) -> dict[str, str]:
        stats = self.stats()
        return {
            "tool_runtime_planned": str(stats.planned_count),
            "tool_runtime_completed": str(stats.completed_count),
            "tool_runtime_failed": str(stats.failed_count),
            "tool_runtime_schema_errors": str(stats.schema_error_count),
            "tool_runtime_permission_denied": str(stats.permission_denied_count),
            "tool_runtime_budget_externalizations": str(stats.budget_externalization_count),
            "tool_runtime_watchdog_signals": str(stats.watchdog_signal_count),
            "tool_runtime_artifact_refs": str(stats.artifact_ref_count),
            "tool_runtime_mutating": str(stats.mutating_count),
            "tool_runtime_read_only": str(stats.read_only_count),
            "tool_runtime_result_chars": str(stats.total_result_chars),
            "tool_runtime_high_risk": str(stats.high_risk_count),
            "tool_runtime_blocked_risk": str(stats.blocked_risk_count),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": "zyra.claude.tool_use_runtime.v1",
            "runtime_source": self.runtime_source,
            "runtime_id": self.runtime_id,
            "owner_unit": self.owner_unit,
            "stats": self.stats().to_dict(),
            "envelopes": [item.to_dict() for item in self.envelopes],
            "result_blocks": [item.to_dict() for item in self.result_blocks],
            "trace": [item.to_dict() for item in self.trace],
            "batch_digests": [item.to_dict() for item in self.batch_digests],
            "signals": [item.to_dict() for item in self._signals],
            "budget_decisions": [item.to_dict() for item in self._budget_decisions],
        }

    def assert_semantic_effects(self, *, require_mutation_for_writes: bool = True) -> list[str]:
        errors: list[str] = []
        for envelope in self.envelopes:
            if envelope.access_mode in {str(ToolAccessMode.WORKSPACE_WRITE), str(ToolAccessMode.SHELL)}:
                if require_mutation_for_writes and not envelope.mutates_workspace:
                    errors.append(f"{envelope.tool_use_id}:{envelope.tool_name} missing mutating semantic effect")
            if envelope.schema_errors and envelope.valid:
                errors.append(f"{envelope.tool_use_id}:{envelope.tool_name} schema errors not reflected in envelope validity")
        return errors

    def _envelope_for_request(self, request: ToolLoopRequest, *, batch_index: int) -> ClaudeToolUseEnvelope:
        existing = self._envelopes.get(request.call.tool_call_id)
        if existing is not None:
            return existing
        envelope = self.envelope_from_request(request, batch_index=batch_index)
        self._envelopes[envelope.tool_use_id] = envelope
        return envelope

    def _result_text(
        self,
        envelope: ClaudeToolUseEnvelope,
        result: ToolResult,
        *,
        budget_decision: ToolBudgetDecision | None,
        failure_signal: ToolFailureSignal | None,
    ) -> str:
        payload = {
            "type": "tool_result",
            "tool_use_id": envelope.tool_use_id,
            "tool_name": envelope.tool_name,
            "ok": result.ok,
            "summary": result.summary,
            "error": result.error,
            "output": result.output,
            "artifact_ids": [artifact.artifact_id for artifact in result.artifacts],
            "semantic_effects": [effect.to_dict() for effect in envelope.semantic_effects],
            "budget": budget_decision.to_dict() if budget_decision else None,
            "watchdog_signal": failure_signal.to_dict() if failure_signal else None,
            "runtime_source": self.runtime_source,
        }
        return json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True)


def semantic_effects_for_request(request: ToolLoopRequest) -> list[ClaudeToolSemanticEffect]:
    tool_name = request.tool_name
    args = dict(request.arguments)
    if tool_name == "file_read":
        return [
            ClaudeToolSemanticEffect(
                kind=ClaudeToolSemanticEffectKind.READ_WORKSPACE,
                subject=str(args.get("path") or ""),
                mutates_workspace=False,
                reads_workspace=True,
                requires_permission=True,
                risk=ClaudeToolUseRisk.LOW,
                metadata={"access_mode": str(request.access_mode)},
            )
        ]
    if tool_name == "file_write":
        return [
            ClaudeToolSemanticEffect(
                kind=ClaudeToolSemanticEffectKind.WRITE_WORKSPACE,
                subject=str(args.get("path") or ""),
                mutates_workspace=True,
                reads_workspace=False,
                requires_permission=True,
                risk=ClaudeToolUseRisk.MEDIUM,
                metadata={"content_chars": len(str(args.get("content") or ""))},
            )
        ]
    if tool_name == "file_edit":
        return [
            ClaudeToolSemanticEffect(
                kind=ClaudeToolSemanticEffectKind.EDIT_WORKSPACE,
                subject=str(args.get("path") or ""),
                mutates_workspace=True,
                reads_workspace=True,
                requires_permission=True,
                risk=ClaudeToolUseRisk.MEDIUM,
                metadata={"replace_all": bool(args.get("replace_all"))},
            )
        ]
    if tool_name == "shell":
        return [
            ClaudeToolSemanticEffect(
                kind=ClaudeToolSemanticEffectKind.RUN_SHELL,
                subject=str(args.get("command") or ""),
                mutates_workspace=True,
                reads_workspace=True,
                requires_permission=True,
                risk=ClaudeToolUseRisk.HIGH if not args.get("approved") else ClaudeToolUseRisk.MEDIUM,
                metadata={"approved": bool(args.get("approved")), "timeout_seconds": args.get("timeout_seconds")},
            )
        ]
    if tool_name == "browser":
        return [
            ClaudeToolSemanticEffect(
                kind=ClaudeToolSemanticEffectKind.READ_BROWSER,
                subject=str(args.get("url") or "inline_html"),
                mutates_workspace=False,
                reads_workspace=False,
                requires_permission=bool(args.get("allow_network")),
                risk=ClaudeToolUseRisk.MEDIUM if args.get("allow_network") else ClaudeToolUseRisk.LOW,
                metadata={"allow_network": bool(args.get("allow_network"))},
            )
        ]
    if tool_name == "web_search":
        return [
            ClaudeToolSemanticEffect(
                kind=ClaudeToolSemanticEffectKind.SEARCH_EVIDENCE,
                subject=str(args.get("query") or args.get("url") or ""),
                mutates_workspace=False,
                reads_workspace=True,
                requires_permission=bool(args.get("allow_network")),
                risk=ClaudeToolUseRisk.MEDIUM if args.get("allow_network") else ClaudeToolUseRisk.LOW,
                metadata={"max_results": args.get("max_results"), "allow_network": bool(args.get("allow_network"))},
            )
        ]
    if tool_name == "artifact_write":
        return [
            ClaudeToolSemanticEffect(
                kind=ClaudeToolSemanticEffectKind.WRITE_ARTIFACT,
                subject=str(args.get("title") or "artifact"),
                mutates_workspace=False,
                reads_workspace=False,
                requires_permission=False,
                risk=ClaudeToolUseRisk.LOW,
                metadata={"content_chars": len(str(args.get("content") or "")), "kind": str(args.get("kind") or "")},
            )
        ]
    if tool_name == "checkpoint":
        return [
            ClaudeToolSemanticEffect(
                kind=ClaudeToolSemanticEffectKind.READ_CHECKPOINT,
                subject="checkpoint",
                mutates_workspace=False,
                reads_workspace=False,
                requires_permission=False,
                risk=ClaudeToolUseRisk.LOW,
                metadata={"write_artifact": bool(args.get("write_artifact"))},
            )
        ]
    if tool_name == "trace":
        return [
            ClaudeToolSemanticEffect(
                kind=ClaudeToolSemanticEffectKind.READ_TRACE,
                subject=str(args.get("event_type") or "all"),
                mutates_workspace=False,
                reads_workspace=False,
                requires_permission=False,
                risk=ClaudeToolUseRisk.LOW,
                metadata={"limit": args.get("limit"), "write_artifact": bool(args.get("write_artifact"))},
            )
        ]
    return [
        ClaudeToolSemanticEffect(
            kind=ClaudeToolSemanticEffectKind.UNKNOWN,
            subject=tool_name,
            mutates_workspace=not request.read_only,
            reads_workspace=request.read_only,
            requires_permission=True,
            risk=ClaudeToolUseRisk.BLOCKED if request.schema_errors else ClaudeToolUseRisk.MEDIUM,
            metadata={"access_mode": str(request.access_mode)},
        )
    ]


def failure_signal_from_result_block(block: ClaudeToolResultBlock) -> ToolFailureSignal | None:
    if block.ok:
        return None
    error = str(block.error or "").lower()
    if "permission" in error:
        kind = ToolFailureKind.PERMISSION_DENIED
        severity = ToolSignalSeverity.ERROR
        retryable = False
        route = "permission_runtime"
    elif "timeout" in error:
        kind = ToolFailureKind.TIMEOUT
        severity = ToolSignalSeverity.WARNING
        retryable = True
        route = "watchdog_retry"
    elif "schema" in error or "missing" in error:
        kind = ToolFailureKind.SCHEMA_ERROR
        severity = ToolSignalSeverity.ERROR
        retryable = False
        route = "query_engine_schema_repair"
    else:
        kind = ToolFailureKind.RUNTIME_ERROR
        severity = ToolSignalSeverity.ERROR
        retryable = True
        route = "fault_recovery"
    return ToolFailureSignal(
        signal_id=new_id("toolsignal"),
        tool_call_id=block.tool_use_id,
        tool_name=block.tool_name,
        kind=kind,
        severity=severity,
        message=block.text[:400],
        retryable=retryable,
        watchdog_route=route,
        metadata={"result_block_id": block.block_id, "source": "claude_tool_use_runtime"},
    )


def tool_result_chars(result: ToolResult) -> int:
    return len(json.dumps(to_jsonable(result.output), ensure_ascii=False, sort_keys=True)) + len(result.summary)


def summarize_tool_blocks(blocks: Iterable[ClaudeToolResultBlock], *, max_lines: int = 24) -> list[str]:
    lines: list[str] = []
    for block in list(blocks)[:max_lines]:
        status = "ok" if block.ok else f"error={block.error or 'tool_error'}"
        artifact_suffix = f", artifacts={len(block.artifact_ids)}" if block.artifact_ids else ""
        lines.append(f"- {block.tool_name} `{block.tool_use_id}` {status}, chars={block.chars}{artifact_suffix}")
    return lines


def tool_runtime_from_snapshot(snapshot: Mapping[str, Any]) -> ClaudeToolUseRuntime:
    runtime = ClaudeToolUseRuntime(
        runtime_source=str(snapshot.get("runtime_source") or "zyra-claude-productized"),
        runtime_id=str(snapshot.get("runtime_id") or "zyra-claude-code-productized-runtime"),
        owner_unit=str(snapshot.get("owner_unit") or "M1-02A"),
    )
    for item in _as_list(snapshot.get("envelopes")):
        if isinstance(item, Mapping):
            envelope = _envelope_from_dict(item)
            runtime._envelopes[envelope.tool_use_id] = envelope
    for item in _as_list(snapshot.get("result_blocks")):
        if isinstance(item, Mapping):
            runtime._result_blocks.append(_result_block_from_dict(item))
    return runtime


def _envelope_from_dict(data: Mapping[str, Any]) -> ClaudeToolUseEnvelope:
    return ClaudeToolUseEnvelope(
        tool_use_id=str(data.get("tool_use_id") or new_id("toolcall")),
        tool_name=str(data.get("tool_name") or ""),
        arguments=dict(data.get("arguments") or {}),
        turn_index=_safe_int(data.get("turn_index"), default=0),
        batch_index=_safe_int(data.get("batch_index"), default=0),
        step_index=_safe_int(data.get("step_index"), default=0),
        access_mode=str(data.get("access_mode") or ""),
        read_only=bool(data.get("read_only")),
        conflict_key=str(data.get("conflict_key") or ""),
        schema_errors=[],
        semantic_effects=[
            _effect_from_dict(item)
            for item in _as_list(data.get("semantic_effects"))
            if isinstance(item, Mapping)
        ],
        source_path=str(data.get("source_path") or ""),
        upstream_source_path=str(data.get("upstream_source_path") or ""),
        created_at=str(data.get("created_at") or now_iso()),
        metadata=dict(data.get("metadata") or {}),
    )


def _effect_from_dict(data: Mapping[str, Any]) -> ClaudeToolSemanticEffect:
    return ClaudeToolSemanticEffect(
        kind=_enum_or_default(ClaudeToolSemanticEffectKind, data.get("kind"), ClaudeToolSemanticEffectKind.UNKNOWN),
        subject=str(data.get("subject") or ""),
        mutates_workspace=bool(data.get("mutates_workspace")),
        reads_workspace=bool(data.get("reads_workspace")),
        requires_permission=bool(data.get("requires_permission")),
        risk=_enum_or_default(ClaudeToolUseRisk, data.get("risk"), ClaudeToolUseRisk.MEDIUM),
        metadata=dict(data.get("metadata") or {}),
    )


def _result_block_from_dict(data: Mapping[str, Any]) -> ClaudeToolResultBlock:
    return ClaudeToolResultBlock(
        block_id=str(data.get("block_id") or new_id("toolblock")),
        tool_use_id=str(data.get("tool_use_id") or ""),
        tool_name=str(data.get("tool_name") or ""),
        kind=_enum_or_default(ClaudeToolResultBlockKind, data.get("kind"), ClaudeToolResultBlockKind.CONTENT),
        ok=bool(data.get("ok")),
        text=str(data.get("text") or ""),
        artifact_ids=[str(item) for item in _as_list(data.get("artifact_ids"))],
        error=None if data.get("error") is None else str(data.get("error")),
        created_at=str(data.get("created_at") or now_iso()),
        metadata=dict(data.get("metadata") or {}),
    )


def _batch_index_for_request(batches: Sequence[ToolLoopBatch], request: ToolLoopRequest) -> int:
    for batch in batches:
        if any(item.call.tool_call_id == request.call.tool_call_id for item in batch.requests):
            return batch.batch_index
    return 0


def _upstream_source_for_tool(tool_name: str) -> str:
    mapping = {
        "file_read": "src/tools/FileReadTool",
        "file_write": "src/tools/FileWriteTool",
        "file_edit": "src/tools/FileEditTool",
        "shell": "src/tools/BashTool",
        "browser": "browser_use/tools/registry",
        "web_search": "src/tools/WebSearchTool",
        "artifact_write": "zyra_runtime.artifacts",
        "checkpoint": "zyra_runtime.session",
        "trace": "zyra_core.event_log",
    }
    return mapping.get(tool_name, "src/Tool.ts")


def _enum_or_default(enum_type: type[Any], value: Any, default: Any) -> Any:
    try:
        return enum_type(str(value))
    except ValueError:
        return default


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
