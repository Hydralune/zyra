from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import ArtifactKind, ArtifactRef, EventRecord, EventType, to_jsonable

from .claude_control_commands import (
    ClaudeControlCommandRuntime,
    ClaudeControlRuntimeState,
    control_metadata,
)
from .claude_context_window import (
    ClaudeContextBudget,
    ClaudeContextCompactionReason,
    ClaudeContextWindowManager,
)
from .claude_query_plan import query_turns_from_constraints as planned_query_turns_from_constraints
from .claude_runtime_contracts import (
    PRODUCTIZED_CONTRACT_SOURCE,
    ClaudeRuntimeContractBundle,
    build_productized_claude_runtime_contracts,
)
from .claude_runtime_state import ClaudeRuntimeStateLedger
from .claude_session_lifecycle import ClaudeSessionLifecycleRuntime, session_artifact_metadata
from .claude_tool_use_runtime import ClaudeToolUseRuntime
from .executor import ToolExecutionContext, ToolExecutor, tool_result_event
from .query_session import QuerySession, QueryStreamEventType, StopReason, snapshot_checkpoint_metadata
from .tool_loop import (
    ToolFailureSignal,
    ToolLoopRequest,
    ToolLoopScheduler,
    ToolResultBudgeter,
    tool_failure_signal_from_result,
    watchdog_signal_payload,
)
from .tools import ToolResult


@dataclass(frozen=True, slots=True)
class ClaudeQueryEngineConfig:
    max_turns: int | None = None
    max_tool_result_chars: int = 8000
    max_query_context_chars: int = 32000
    continue_on_error: bool = False
    max_read_only_concurrency: int = 10
    emit_tool_use_summaries: bool = True
    runtime_contracts: ClaudeRuntimeContractBundle | None = None
    trace_title: str = "CodeWorker Runtime Trace"
    context_artifact_title: str = "CodeWorker context compaction"
    allow_empty_turns: bool = False
    control_commands: Sequence[Any] = field(default_factory=tuple)
    project_root: str | Path | None = None

    @property
    def contracts(self) -> ClaudeRuntimeContractBundle:
        return self.runtime_contracts or build_productized_claude_runtime_contracts()


@dataclass(frozen=True, slots=True)
class ClaudeQueryEngineResult:
    ok: bool
    event_records: list[EventRecord]
    artifacts: list[ArtifactRef]
    step_summaries: list[str]
    turn_count: int
    tool_call_count: int
    context_compaction_count: int = 0
    stopped_reason: str | None = None
    session_snapshot: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeContextEntry:
    turn_index: int
    batch_index: int
    step_index: int
    tool_name: str
    tool_call_id: str
    summary: str
    ok: bool
    chars: int

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


class ZyraClaudeQueryEngine:
    """Zyra-owned QueryEngine port for structured CodeWorker turns.

    The upstream Claude Code QueryEngine combines model streaming, tool
    orchestration, permission handling, session persistence, compact boundaries,
    and SDK result shaping. Zyra's default CodeWorker path keeps the same
    responsibilities but binds them to Zyra stores: QuerySession for transcript
    state, ToolLoopScheduler for batching, ToolExecutor for permission-gated
    tool calls, LocalArtifactStore for large outputs and snapshots, and
    EventRecord for runtime observability.
    """

    def __init__(self, context: ToolExecutionContext, config: ClaudeQueryEngineConfig | None = None) -> None:
        self.context = context
        self.config = config or ClaudeQueryEngineConfig()
        self.contracts = self.config.contracts

    def run(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        turns: Sequence[Sequence[Mapping[str, Any]]],
        request_messages: Sequence[Any] = (),
        request_metadata: Mapping[str, str] | None = None,
    ) -> ClaudeQueryEngineResult:
        normalized_turns = _normalize_turns(turns)
        if not normalized_turns and not self.config.allow_empty_turns:
            return self._missing_plan_result(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                worker_request_id=worker_request_id,
                request_messages=request_messages,
            )

        executor = ToolExecutor(self.context)
        scheduler = ToolLoopScheduler(
            self.context.registry,
            max_read_only_concurrency=max(1, self.config.max_read_only_concurrency),
            source_contract=self.contracts.tool_loop_contract,
        )
        budgeter = ToolResultBudgeter(max_chars=max(1, self.config.max_tool_result_chars))
        session = QuerySession(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            worker_request_id=worker_request_id,
            source_contract={
                "runtime_id": self.contracts.runtime_id,
                "contract_source": self.contracts.contract_source,
                "query_contract": self.contracts.query_contract,
                "session_contract": self.contracts.session_contract,
                "tool_loop_contract": self.contracts.tool_loop_contract,
            },
            metadata={
                "contract_source": self.contracts.contract_source,
                "runtime_id": self.contracts.runtime_id,
                **dict(request_metadata or {}),
            },
        )

        event_records: list[EventRecord] = []
        artifacts: list[ArtifactRef] = []
        step_summaries: list[str] = []
        context_entries: list[RuntimeContextEntry] = []
        failure_signals: list[ToolFailureSignal] = []
        context_window = ClaudeContextWindowManager(
            budget=ClaudeContextBudget(
                max_chars=self.config.max_query_context_chars,
                reserve_chars=0,
                min_recent_blocks=1,
            ),
            runtime_source=self.contracts.contract_source,
            runtime_id=self.contracts.runtime_id,
            session_id=session.session_id,
            request_id=worker_request_id,
        )
        context_window.seed_request_messages(request_messages)
        tool_runtime = ClaudeToolUseRuntime(
            runtime_source=self.contracts.contract_source,
            runtime_id=self.contracts.runtime_id,
            owner_unit=str(self.contracts.tool_loop_contract.get("ownerUnit") or "M1-02A"),
        )
        session_lifecycle = ClaudeSessionLifecycleRuntime(
            artifact_store=self.context.artifact_store,
            runtime_source=self.contracts.contract_source,
            runtime_id=self.contracts.runtime_id,
            owner_unit=str(self.contracts.session_contract.get("ownerUnit") or "M1-02A"),
        )
        state_ledger = ClaudeRuntimeStateLedger(
            runtime_id=self.contracts.runtime_id,
            runtime_source=self.contracts.contract_source,
            session_id=session.session_id,
        )
        state_ledger.record_session_started(session.session_id, worker_request_id=worker_request_id)
        context_chars = context_window.active_chars
        compaction_count = 0
        tool_use_summary_count = 0
        tool_budget_externalization_count = 0
        tool_failure_signal_count = 0
        tool_schema_error_count = 0
        conflict_protected_count = 0
        executed_turn_count = 0
        tool_call_count = 0
        ok = True
        stopped_reason: str | None = None
        max_turns = self.config.max_turns or len(normalized_turns)

        self._append_lifecycle(
            event_records,
            session,
            run_id,
            task_id,
            node_id,
            worker_request_id,
            "session_started",
            {
                "runtime_id": self.contracts.runtime_id,
                "contract_source": self.contracts.contract_source,
                "max_turns": max_turns,
                "tool_result_budget_chars": self.config.max_tool_result_chars,
                "query_context_budget_chars": self.config.max_query_context_chars,
                "resume_token": session.resume_token,
            },
        )
        self._append_lifecycle(
            event_records,
            session,
            run_id,
            task_id,
            node_id,
            worker_request_id,
            "permission_runtime_attached",
            {
                "permission": self._permission_runtime_payload(),
                "source_path": "packages/runtime/zyra_runtime/permissions.py",
                "upstream_source_path": "src/cli/src/utils/permissions/*",
                "resume_token": session.resume_token,
            },
        )

        for turn_index, turn in enumerate(normalized_turns, start=1):
            if turn_index > max_turns:
                ok = False
                stopped_reason = "max_turns_exceeded"
                session.record_error(
                    error=stopped_reason,
                    stop_reason=StopReason.MAX_TURNS_EXCEEDED,
                    metadata={"turn_index": turn_index, "max_turns": max_turns},
                )
                self._append_lifecycle(
                    event_records,
                    session,
                    run_id,
                    task_id,
                    node_id,
                    worker_request_id,
                    "session_stopped",
                    {"turn_index": turn_index, "stopped_reason": stopped_reason, "resume_token": session.resume_token},
                )
                self._append_lifecycle(
                    event_records,
                    session,
                    run_id,
                    task_id,
                    node_id,
                    worker_request_id,
                    "error",
                    {
                        "turn_index": turn_index,
                        "error": stopped_reason,
                        "stop_reason": str(StopReason.MAX_TURNS_EXCEEDED),
                        "resume_token": session.resume_token,
                    },
                )
                break

            executed_turn_count += 1
            turn_started_at_count = tool_call_count
            user_content = _turn_user_content(turn_index, turn)
            context_window.record_turn_prompt(
                turn_index=turn_index,
                text=user_content,
                metadata={"planned_tool_calls": len(turn)},
            )
            turn_state = session.start_turn(
                turn_index,
                user_content=user_content,
                metadata={
                    "planned_tool_calls": len(turn),
                    "source_path": "packages/runtime/zyra_runtime/claude_query_engine_runtime.py",
                    "upstream_source_path": "src/QueryEngine.ts",
                },
            )
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "turn_started",
                {
                    "turn_index": turn_index,
                    "turn_id": turn_state.turn_id,
                    "planned_tool_calls": len(turn),
                    "resume_token": session.resume_token,
                },
            )
            state_ledger.record_turn(turn_state.turn_id, turn_index=turn_index)
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "turn_start",
                {
                    "turn_index": turn_index,
                    "turn_id": turn_state.turn_id,
                    "planned_tool_calls": len(turn),
                    "resume_token": session.resume_token,
                },
            )
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "message_delta",
                {
                    "turn_index": turn_index,
                    "turn_id": turn_state.turn_id,
                    "role": "user",
                    "delta": user_content,
                    "resume_token": session.resume_token,
                },
            )
            session.start_stream_request(
                turn_id=turn_state.turn_id,
                metadata={
                    "turn_index": turn_index,
                    "source_path": "packages/runtime/zyra_runtime/claude_query_engine_runtime.py",
                    "upstream_source_path": "src/query.ts",
                    "contract_source": self.contracts.contract_source,
                },
            )
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "stream_request_start",
                {
                    "turn_index": turn_index,
                    "turn_id": turn_state.turn_id,
                    "source_path": "packages/runtime/zyra_runtime/claude_query_engine_runtime.py",
                    "upstream_source_path": "src/query.ts",
                    "contract_source": self.contracts.contract_source,
                    "resume_token": session.resume_token,
                },
            )
            session.start_assistant_message(
                turn_id=turn_state.turn_id,
                metadata={
                    "turn_index": turn_index,
                    "source_path": "packages/runtime/zyra_runtime/claude_query_engine_runtime.py",
                },
            )
            assistant_delta = f"Executing Zyra-owned Claude Code turn {turn_index} with {len(turn)} planned tool call(s)."
            context_window.record_assistant_delta(
                turn_index=turn_index,
                text=assistant_delta,
                metadata={"planned_tool_calls": len(turn)},
            )
            session.append_assistant_delta(
                assistant_delta,
                metadata={"turn_index": turn_index, "planned_tool_calls": len(turn)},
            )
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "message_delta",
                {
                    "turn_index": turn_index,
                    "turn_id": turn_state.turn_id,
                    "role": "assistant",
                    "delta": assistant_delta,
                    "resume_token": session.resume_token,
                },
            )

            tool_loop_plan = scheduler.plan_turn(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                worker_request_id=worker_request_id,
                turn_index=turn_index,
                steps=turn,
            )
            tool_schema_error_count += tool_loop_plan.schema_error_count
            conflict_protected_count += tool_loop_plan.conflict_protected_count
            tool_runtime.register_plan(tool_loop_plan)
            session.record_batch_event(QueryStreamEventType.TOOL_LOOP_PLAN, turn_id=turn_state.turn_id, metadata=tool_loop_plan.to_dict())
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "tool_loop_plan",
                {
                    "turn_index": turn_index,
                    "turn_id": turn_state.turn_id,
                    "tool_count": len(tool_loop_plan.requests),
                    "batch_count": len(tool_loop_plan.batches),
                    "read_only_count": tool_loop_plan.read_only_count,
                    "write_count": tool_loop_plan.write_count,
                    "schema_error_count": tool_loop_plan.schema_error_count,
                    "conflict_protected_count": tool_loop_plan.conflict_protected_count,
                    "source_path": "packages/runtime/zyra_runtime/tool_loop.py",
                    "upstream_source_path": "src/services/tools/toolOrchestration.ts",
                    "resume_token": session.resume_token,
                },
            )

            for batch in tool_loop_plan.batches:
                batch_index = batch.batch_index
                batch_requests = batch.requests
                execution_mode = str(batch.execution_mode)
                session.record_batch_event(
                    QueryStreamEventType.TOOL_BATCH_STARTED,
                    turn_id=turn_state.turn_id,
                    metadata={
                        "turn_index": turn_index,
                        "batch_index": batch_index,
                        "tool_count": len(batch_requests),
                        "execution_mode": execution_mode,
                        "conflict_keys": batch.conflict_keys,
                        "conflict_protected": batch.conflict_protected,
                    },
                )
                self._append_lifecycle(
                    event_records,
                    session,
                    run_id,
                    task_id,
                    node_id,
                    worker_request_id,
                    "tool_batch_started",
                    {
                        "turn_index": turn_index,
                        "turn_id": turn_state.turn_id,
                        "batch_index": batch_index,
                        "tool_count": len(batch_requests),
                        "execution_mode": execution_mode,
                        "source_path": "packages/runtime/zyra_runtime/tool_loop.py",
                        "upstream_source_path": "src/services/tools/toolOrchestration.ts",
                        "tool_names": [planned.call.tool_name for planned in batch_requests],
                        "read_only": str(all(planned.read_only for planned in batch_requests)).lower(),
                        "conflict_keys": batch.conflict_keys,
                        "conflict_protected": str(batch.conflict_protected).lower(),
                    },
                )
                for planned in batch_requests:
                    tool_runtime.mark_started(planned, batch_index=batch_index)
                    session.record_tool_call(
                        tool_call_id=planned.call.tool_call_id,
                        tool_name=planned.call.tool_name,
                        turn_id=turn_state.turn_id,
                        metadata={
                            "turn_index": turn_index,
                            "batch_index": batch_index,
                            "step_index": planned.step_index,
                            "read_only": planned.read_only,
                        },
                    )
                    self._append_lifecycle(
                        event_records,
                        session,
                        run_id,
                        task_id,
                        node_id,
                        worker_request_id,
                        "tool_call_started",
                        {
                            "turn_index": turn_index,
                            "turn_id": turn_state.turn_id,
                            "batch_index": batch_index,
                            "step_index": planned.step_index,
                            "tool_call_id": planned.call.tool_call_id,
                            "tool_name": planned.call.tool_name,
                            "read_only": str(planned.read_only).lower(),
                            "access_mode": str(planned.access_mode),
                            "conflict_key": planned.conflict_key,
                            "schema_error_count": len(planned.schema_errors),
                        },
                    )

                raw_results = self._execute_batch(executor, batch_requests, scheduler)
                batch_summaries: list[dict[str, Any]] = []
                bounded_results_for_batch: list[ToolResult] = []
                budget_decisions_for_batch: list[Any] = []
                for planned, result in zip(batch_requests, raw_results, strict=True):
                    bounded_result, budget_decision = budgeter.apply(
                        request=planned,
                        result=result,
                        artifact_store=self.context.artifact_store,
                    )
                    bounded_results_for_batch.append(bounded_result)
                    budget_decisions_for_batch.append(budget_decision if budget_decision.applied else None)
                    artifacts.extend(bounded_result.artifacts)
                    observed_signal: ToolFailureSignal | None = None
                    if budget_decision.applied:
                        tool_budget_externalization_count += 1
                        budget_signal = tool_failure_signal_from_result(
                            planned,
                            bounded_result,
                            budget_decision=budget_decision,
                        )
                        if budget_signal is not None:
                            observed_signal = budget_signal
                            failure_signals.append(budget_signal)
                            tool_failure_signal_count += 1
                            self._append_tool_signal_events(
                                event_records,
                                session,
                                run_id=run_id,
                                task_id=task_id,
                                node_id=node_id,
                                worker_request_id=worker_request_id,
                                session_id=session.session_id,
                                turn_id=turn_state.turn_id,
                                turn_index=turn_index,
                                batch_index=batch_index,
                                step_index=planned.step_index,
                                signal=budget_signal,
                            )
                        self._append_lifecycle(
                            event_records,
                            session,
                            run_id,
                            task_id,
                            node_id,
                            worker_request_id,
                            "tool_result_budget_exceeded",
                            {
                                "turn_index": turn_index,
                                "turn_id": turn_state.turn_id,
                                "batch_index": batch_index,
                                "step_index": planned.step_index,
                                "tool_call_id": planned.call.tool_call_id,
                                "tool_name": planned.call.tool_name,
                                "budget": budget_decision.to_dict(),
                                "resume_token": session.resume_token,
                            },
                        )

                    signal = tool_failure_signal_from_result(planned, bounded_result)
                    if signal is not None:
                        observed_signal = signal
                        failure_signals.append(signal)
                        tool_failure_signal_count += 1
                        self._append_tool_signal_events(
                            event_records,
                            session,
                            run_id=run_id,
                            task_id=task_id,
                            node_id=node_id,
                            worker_request_id=worker_request_id,
                            session_id=session.session_id,
                            turn_id=turn_state.turn_id,
                            turn_index=turn_index,
                            batch_index=batch_index,
                            step_index=planned.step_index,
                            signal=signal,
                        )

                    result_block = tool_runtime.record_result(
                        planned,
                        bounded_result,
                        batch_index=batch_index,
                        budget_decision=budget_decision if budget_decision.applied else None,
                        failure_signal=observed_signal,
                    )
                    state_ledger.record_tool_result(
                        tool_call_id=planned.call.tool_call_id,
                        tool_name=planned.call.tool_name,
                        ok=bounded_result.ok,
                        artifact_ids=[artifact.artifact_id for artifact in bounded_result.artifacts],
                        turn_id=turn_state.turn_id,
                        error=bounded_result.error,
                    )
                    session.record_tool_result(
                        tool_call_id=planned.call.tool_call_id,
                        tool_name=planned.call.tool_name,
                        summary=bounded_result.summary,
                        ok=bounded_result.ok,
                        error=bounded_result.error,
                        artifacts=[artifact.artifact_id for artifact in bounded_result.artifacts],
                        turn_id=turn_state.turn_id,
                        metadata={
                            "turn_index": turn_index,
                            "batch_index": batch_index,
                            "step_index": planned.step_index,
                            "permission_effect": str(bounded_result.metadata.get("permission_effect") or ""),
                            "runtime_source": PRODUCTIZED_CONTRACT_SOURCE,
                            "tool_result_block_id": result_block.block_id,
                            "tool_result_block_kind": str(result_block.kind),
                        },
                    )
                    event_records.append(tool_result_event(planned.call, bounded_result))
                    self._append_lifecycle(
                        event_records,
                        session,
                        run_id,
                        task_id,
                        node_id,
                        worker_request_id,
                        "tool_call_completed",
                        {
                            "turn_index": turn_index,
                            "turn_id": turn_state.turn_id,
                            "batch_index": batch_index,
                            "step_index": planned.step_index,
                            "tool_call_id": planned.call.tool_call_id,
                            "tool_name": planned.call.tool_name,
                            "ok": bounded_result.ok,
                            "error": bounded_result.error,
                            "artifact_ids": [artifact.artifact_id for artifact in bounded_result.artifacts],
                            "permission_effect": str(bounded_result.metadata.get("permission_effect") or ""),
                            "resume_token": session.resume_token,
                        },
                    )

                    tool_call_count += 1
                    result_chars = _result_chars(bounded_result)
                    summary_row = {
                        "turn_index": turn_index,
                        "batch_index": batch_index,
                        "step_index": planned.step_index,
                        "tool_call_id": planned.call.tool_call_id,
                        "tool_name": planned.call.tool_name,
                        "ok": bounded_result.ok,
                        "summary": bounded_result.summary,
                        "error": bounded_result.error,
                    }
                    batch_summaries.append(summary_row)
                    step_summaries.append(_format_step_summary(summary_row, execution_mode))
                    context_entries.append(
                        RuntimeContextEntry(
                            turn_index=turn_index,
                            batch_index=batch_index,
                            step_index=planned.step_index,
                            tool_name=planned.call.tool_name,
                            tool_call_id=planned.call.tool_call_id,
                            summary=bounded_result.summary,
                            ok=bounded_result.ok,
                            chars=result_chars,
                        )
                    )
                    context_window.record_tool_result(
                        planned=planned,
                        result=bounded_result,
                        result_chars=result_chars,
                        turn_index=turn_index,
                        batch_index=batch_index,
                        metadata={"tool_result_block_id": result_block.block_id},
                    )
                    context_chars = context_window.active_chars

                    if context_window.active_chars > self.config.max_query_context_chars:
                        compaction = context_window.maybe_compact(
                            artifact_store=self.context.artifact_store,
                            run_id=run_id,
                            task_id=task_id,
                            producer_node_id=node_id,
                            reason=ClaudeContextCompactionReason.BUDGET_EXCEEDED,
                        )
                        if compaction.applied and compaction.artifact is not None:
                            artifact = compaction.artifact
                            artifacts.append(artifact)
                            compaction_count += 1
                            session.record_context_compaction(
                                artifact_id=artifact.artifact_id,
                                metadata={
                                    "turn_index": turn_index,
                                    "batch_index": batch_index,
                                    "step_index": planned.step_index,
                                    "context_chars": compaction.before_chars,
                                    "after_chars": compaction.after_chars,
                                    "budget_chars": self.config.max_query_context_chars,
                                    "runtime_source": PRODUCTIZED_CONTRACT_SOURCE,
                                    "compacted_block_ids": compaction.compacted_block_ids,
                                },
                            )
                            self._append_lifecycle(
                                event_records,
                                session,
                                run_id,
                                task_id,
                                node_id,
                                worker_request_id,
                                "context_compacted",
                                {
                                    "turn_index": turn_index,
                                    "turn_id": turn_state.turn_id,
                                    "artifact_id": artifact.artifact_id,
                                    "context_chars": compaction.before_chars,
                                    "after_chars": compaction.after_chars,
                                    "budget_chars": self.config.max_query_context_chars,
                                    "resume_token": session.resume_token,
                                },
                            )
                            context_entries = []
                            context_chars = context_window.active_chars
                            state_ledger.record_context_compaction(
                                artifact=artifact,
                                before_chars=compaction.before_chars,
                                after_chars=compaction.after_chars,
                            )

                    if not bounded_result.ok and not self.config.continue_on_error and stopped_reason is None:
                        ok = False
                        stopped_reason = bounded_result.error or "tool_step_failed"
                        session.record_error(
                            error=stopped_reason,
                            stop_reason=StopReason.TOOL_ERROR,
                            metadata={
                                "turn_index": turn_index,
                                "batch_index": batch_index,
                                "step_index": planned.step_index,
                                "tool_call_id": planned.call.tool_call_id,
                                "tool_name": planned.call.tool_name,
                            },
                        )
                        self._append_lifecycle(
                            event_records,
                            session,
                            run_id,
                            task_id,
                            node_id,
                            worker_request_id,
                            "error",
                            {
                                "turn_index": turn_index,
                                "turn_id": turn_state.turn_id,
                                "tool_call_id": planned.call.tool_call_id,
                                "tool_name": planned.call.tool_name,
                                "error": stopped_reason,
                                "stop_reason": str(StopReason.TOOL_ERROR),
                                "resume_token": session.resume_token,
                            },
                        )
                    elif not bounded_result.ok and self.config.continue_on_error:
                        session.record_continue(
                            reason=StopReason.CONTINUE_REQUESTED,
                            error=bounded_result.error or "tool_step_failed",
                            metadata={
                                "turn_index": turn_index,
                                "batch_index": batch_index,
                                "step_index": planned.step_index,
                                "tool_call_id": planned.call.tool_call_id,
                                "tool_name": planned.call.tool_name,
                            },
                        )
                        self._append_lifecycle(
                            event_records,
                            session,
                            run_id,
                            task_id,
                            node_id,
                            worker_request_id,
                            "continue",
                            {
                                "turn_index": turn_index,
                                "turn_id": turn_state.turn_id,
                                "tool_call_id": planned.call.tool_call_id,
                                "tool_name": planned.call.tool_name,
                                "reason": str(StopReason.CONTINUE_REQUESTED),
                                "error": bounded_result.error,
                                "resume_token": session.resume_token,
                            },
                        )

                batch_digest = tool_runtime.record_batch_digest(
                    batch,
                    bounded_results_for_batch,
                    budget_decisions=budget_decisions_for_batch,
                )
                session.record_batch_event(
                    QueryStreamEventType.TOOL_BATCH_COMPLETED,
                    turn_id=turn_state.turn_id,
                    metadata={
                        "turn_index": turn_index,
                        "batch_index": batch_index,
                        "tool_use_digest": batch_digest.to_dict(),
                    },
                )
                if self.config.emit_tool_use_summaries:
                    tool_use_summary_count += 1
                    session.record_batch_event(
                        QueryStreamEventType.TOOL_USE_SUMMARY,
                        turn_id=turn_state.turn_id,
                        metadata={
                            "turn_index": turn_index,
                            "batch_index": batch_index,
                            "execution_mode": execution_mode,
                            "tool_count": len(batch_requests),
                            "summary": batch_summaries,
                        },
                    )
                    self._append_lifecycle(
                        event_records,
                        session,
                        run_id,
                        task_id,
                        node_id,
                        worker_request_id,
                        "tool_use_summary",
                        {
                            "turn_index": turn_index,
                            "turn_id": turn_state.turn_id,
                            "batch_index": batch_index,
                            "execution_mode": execution_mode,
                            "tool_count": len(batch_requests),
                            "source_path": "packages/runtime/zyra_runtime/claude_query_engine_runtime.py",
                            "upstream_source_path": "src/query.ts",
                            "summary": batch_summaries,
                            "resume_token": session.resume_token,
                        },
                    )
                self._append_lifecycle(
                    event_records,
                    session,
                    run_id,
                    task_id,
                    node_id,
                    worker_request_id,
                    "tool_batch_completed",
                    {
                        "turn_index": turn_index,
                        "turn_id": turn_state.turn_id,
                        "batch_index": batch_index,
                        "tool_count": len(batch_requests),
                        "execution_mode": execution_mode,
                        "ok": all(item["ok"] for item in batch_summaries),
                        "stopped_reason": stopped_reason,
                        "resume_token": session.resume_token,
                    },
                )
                session.record_batch_event(
                    QueryStreamEventType.TOOL_BATCH_COMPLETED,
                    turn_id=turn_state.turn_id,
                    metadata={
                        "turn_index": turn_index,
                        "batch_index": batch_index,
                        "tool_count": len(batch_requests),
                        "execution_mode": execution_mode,
                        "ok": all(item["ok"] for item in batch_summaries),
                        "stopped_reason": stopped_reason,
                    },
                )
                if not ok and not self.config.continue_on_error:
                    break

            session.end_turn(
                ok=ok or self.config.continue_on_error,
                stop_reason=StopReason.END_TURN if ok or self.config.continue_on_error else StopReason.TOOL_ERROR,
                error=stopped_reason,
                metadata={
                    "turn_index": turn_index,
                    "tool_calls": tool_call_count - turn_started_at_count,
                    "stopped_reason": stopped_reason,
                },
            )
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "turn_completed",
                {
                    "turn_index": turn_index,
                    "turn_id": turn_state.turn_id,
                    "ok": ok,
                    "tool_calls": tool_call_count - turn_started_at_count,
                    "stopped_reason": stopped_reason,
                    "resume_token": session.resume_token,
                },
            )
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "turn_end",
                {
                    "turn_index": turn_index,
                    "turn_id": turn_state.turn_id,
                    "ok": ok or self.config.continue_on_error,
                    "tool_calls": tool_call_count - turn_started_at_count,
                    "stopped_reason": stopped_reason,
                    "resume_token": session.resume_token,
                },
            )
            state_ledger.record_turn(turn_state.turn_id, turn_index=turn_index, completed=True, ok=ok)
            if not ok and not self.config.continue_on_error:
                break

        session.complete_session(
            ok=ok,
            stop_reason=StopReason.SESSION_COMPLETED if ok else StopReason.TOOL_ERROR,
            metadata={
                "tool_call_count": tool_call_count,
                "turn_count": executed_turn_count,
                "context_compaction_count": compaction_count,
                "tool_budget_externalization_count": tool_budget_externalization_count,
                "tool_failure_signal_count": tool_failure_signal_count,
                "tool_schema_error_count": tool_schema_error_count,
                "conflict_protected_count": conflict_protected_count,
                "stopped_reason": stopped_reason,
                "runtime_source": PRODUCTIZED_CONTRACT_SOURCE,
            },
        )
        session_snapshot = session.snapshot_payload(
            include_transcript=True,
            metadata={
                "tool_call_count": tool_call_count,
                "turn_count": executed_turn_count,
                "context_compaction_count": compaction_count,
                "tool_budget_externalization_count": tool_budget_externalization_count,
                "tool_failure_signal_count": tool_failure_signal_count,
                "tool_schema_error_count": tool_schema_error_count,
                "conflict_protected_count": conflict_protected_count,
                "failure_signals": [signal.to_dict() for signal in failure_signals],
                "stopped_reason": stopped_reason,
                "runtime_source": PRODUCTIZED_CONTRACT_SOURCE,
            },
        )
        artifact_set = session_lifecycle.materialize_session_artifacts(
            session,
            run_id=run_id,
            task_id=task_id,
            producer_node_id=node_id,
            metadata={
                "tool_call_count": tool_call_count,
                "turn_count": executed_turn_count,
                "context_compaction_count": compaction_count,
                "tool_budget_externalization_count": tool_budget_externalization_count,
                "tool_failure_signal_count": tool_failure_signal_count,
                "tool_schema_error_count": tool_schema_error_count,
                "conflict_protected_count": conflict_protected_count,
                "failure_signals": [signal.to_dict() for signal in failure_signals],
                "stopped_reason": stopped_reason,
                "context_window": context_window.snapshot(include_text=False),
                "tool_runtime": tool_runtime.snapshot(),
            },
        )
        snapshot_artifact = artifact_set.snapshot_artifact
        transcript_artifact = artifact_set.transcript_artifact
        artifacts.extend(artifact_set.artifacts)
        state_ledger.record_session_artifacts(
            snapshot_artifact=snapshot_artifact,
            transcript_artifact=transcript_artifact,
            resume_artifact=artifact_set.resume_plan_artifact,
        )
        self._append_lifecycle(
            event_records,
            session,
            run_id,
            task_id,
            node_id,
            worker_request_id,
            "query_session_snapshot",
            {
                "snapshot_artifact_id": snapshot_artifact.artifact_id,
                "transcript_artifact_id": transcript_artifact.artifact_id,
                "resume_plan_artifact_id": artifact_set.resume_plan_artifact.artifact_id if artifact_set.resume_plan_artifact else "",
                "resume_token": session.resume_token,
                "consistency": session_snapshot.get("consistency", {}),
                "stats": session_snapshot.get("stats", {}),
            },
        )
        self._append_lifecycle(
            event_records,
            session,
            run_id,
            task_id,
            node_id,
            worker_request_id,
            "session_completed",
            {
                "ok": ok,
                "tool_call_count": tool_call_count,
                "turn_count": executed_turn_count,
                "context_compaction_count": compaction_count,
                "tool_budget_externalization_count": tool_budget_externalization_count,
                "tool_failure_signal_count": tool_failure_signal_count,
                "stopped_reason": stopped_reason,
                "resume_token": session.resume_token,
            },
        )
        metadata = self._metadata(
            session_snapshot=session_snapshot,
            max_turns=max_turns,
            tool_budget_externalization_count=tool_budget_externalization_count,
            tool_failure_signal_count=tool_failure_signal_count,
            tool_schema_error_count=tool_schema_error_count,
            conflict_protected_count=conflict_protected_count,
            compaction_count=compaction_count,
            tool_use_summary_count=tool_use_summary_count,
        )
        metadata.update(session_artifact_metadata(artifact_set))
        metadata.update(context_window.metadata())
        metadata.update(tool_runtime.metadata())
        metadata.update(session_lifecycle.metadata())
        control_report = None
        if self.config.control_commands:
            control_runtime = ClaudeControlCommandRuntime(
                ClaudeControlRuntimeState(
                    project_root=Path(self.config.project_root or self.context.workspace_root),
                    workspace_root=self.context.workspace_root,
                    artifact_store=self.context.artifact_store,
                    runtime_source=self.contracts.contract_source,
                    runtime_id=self.contracts.runtime_id,
                    context_window=context_window,
                    tool_runtime=tool_runtime,
                    session_lifecycle=session_lifecycle,
                    permission_store=self.context.permission_store,
                    event_reader=self.context.event_reader,
                    checkpoint_reader=self.context.checkpoint_reader,
                    metadata={
                        "session_id": session.session_id,
                        "worker_request_id": worker_request_id,
                    },
                )
            )
            control_report = control_runtime.run_commands(
                list(self.config.control_commands),
                run_id=run_id,
                task_id=task_id,
                producer_node_id=node_id,
            )
            artifacts.extend(control_report.artifacts)
            for control_result in control_report.results:
                state_ledger.record_control_result(
                    command_id=control_result.command_id,
                    command_name=str(control_result.name),
                    ok=control_result.ok,
                    artifact=control_result.artifact,
                )
                context_window.record_control_result(
                    command_name=str(control_result.name),
                    text=control_result.summary,
                    ok=control_result.ok,
                    metadata={"command_id": control_result.command_id},
                )
                self._append_lifecycle(
                    event_records,
                    session,
                    run_id,
                    task_id,
                    node_id,
                    worker_request_id,
                    "control_command",
                    {
                        "command_id": control_result.command_id,
                        "command_name": str(control_result.name),
                        "status": str(control_result.status),
                        "ok": control_result.ok,
                        "artifact_id": control_result.artifact.artifact_id if control_result.artifact else "",
                    },
                )
        metadata.update(control_metadata(control_report))
        metadata.update(state_ledger.metadata())
        return ClaudeQueryEngineResult(
            ok=ok,
            event_records=event_records,
            artifacts=artifacts,
            step_summaries=step_summaries,
            turn_count=executed_turn_count,
            tool_call_count=tool_call_count,
            context_compaction_count=compaction_count,
            stopped_reason=stopped_reason,
            session_snapshot=session_snapshot,
            metadata=metadata,
        )

    def _missing_plan_result(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        request_messages: Sequence[Any],
    ) -> ClaudeQueryEngineResult:
        event = EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": "",
                    "worker_request_id": worker_request_id,
                    "phase": "missing_tool_plan",
                    "runtime_id": self.contracts.runtime_id,
                    "message_count": len(request_messages),
                }
            },
        )
        return ClaudeQueryEngineResult(
            ok=False,
            event_records=[event],
            artifacts=[],
            step_summaries=[],
            turn_count=0,
            tool_call_count=0,
            stopped_reason="missing_tool_plan",
            metadata={
                **self.contracts.metadata(),
                "loop": "zyra_claude_query_engine_runtime",
                "query_contract_source": self.contracts.contract_source,
                "query_turns": "0",
                "tool_steps": "0",
            },
        )

    def _execute_batch(
        self,
        executor: ToolExecutor,
        batch: list[ToolLoopRequest],
        scheduler: ToolLoopScheduler,
    ) -> list[ToolResult]:
        def execute_one(planned: ToolLoopRequest) -> ToolResult:
            if not planned.valid:
                return scheduler.schema_error_result(planned)
            return executor.execute(planned.call)

        if len(batch) > 1 and all(planned.read_only and planned.concurrency_safe for planned in batch):
            max_workers = min(max(1, self.config.max_read_only_concurrency), len(batch))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                return list(pool.map(execute_one, batch))
        return [execute_one(planned) for planned in batch]

    def _write_context_compaction_artifact(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        session: QuerySession,
        context_entries: Sequence[RuntimeContextEntry],
        context_chars: int,
    ) -> ArtifactRef:
        payload = {
            "runtime_source": PRODUCTIZED_CONTRACT_SOURCE,
            "runtime_id": self.contracts.runtime_id,
            "session_id": session.session_id,
            "resume_token": session.resume_token,
            "budget_chars": self.config.max_query_context_chars,
            "context_chars": context_chars,
            "entries": [entry.to_dict() for entry in context_entries],
        }
        return self.context.artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            title=self.config.context_artifact_title,
            kind=ArtifactKind.TRACE,
            extension=".json",
            producer_node_id=node_id,
        )

    def _append_lifecycle(
        self,
        event_records: list[EventRecord],
        session: QuerySession,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        phase: str,
        payload: Mapping[str, Any],
    ) -> None:
        event_records.append(
            EventRecord(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "query_session": {
                        "session_id": session.session_id,
                        "worker_request_id": worker_request_id,
                        "phase": phase,
                        **dict(payload),
                    }
                },
            )
        )

    def _append_tool_signal_events(
        self,
        event_records: list[EventRecord],
        session: QuerySession,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        session_id: str,
        turn_id: str,
        turn_index: int,
        batch_index: int,
        step_index: int,
        signal: ToolFailureSignal,
    ) -> None:
        payload = {
            "turn_index": turn_index,
            "turn_id": turn_id,
            "batch_index": batch_index,
            "step_index": step_index,
            "signal": signal.to_dict(),
        }
        session.record_batch_event(QueryStreamEventType.TOOL_FAILURE_SIGNAL, turn_id=turn_id, metadata=payload)
        event_records.append(
            EventRecord(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "query_session": {
                        "session_id": session_id,
                        "worker_request_id": worker_request_id,
                        "phase": "tool_failure_signal",
                        **payload,
                    }
                },
            )
        )
        watchdog_payload = {
            "turn_index": turn_index,
            "turn_id": turn_id,
            "batch_index": batch_index,
            "step_index": step_index,
            **watchdog_signal_payload(signal),
        }
        session.record_batch_event(QueryStreamEventType.WATCHDOG_SIGNAL, turn_id=turn_id, metadata=watchdog_payload)
        event_records.append(
            EventRecord(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "query_session": {
                        "session_id": session_id,
                        "worker_request_id": worker_request_id,
                        "phase": "watchdog_signal",
                        **watchdog_payload,
                    }
                },
            )
        )

    def _permission_runtime_payload(self) -> dict[str, Any]:
        return {
            "permission_policy": type(self.context.permission_policy).__name__,
            "permission_store": type(self.context.permission_store).__name__ if self.context.permission_store else "",
            "default_effect": str(getattr(self.context.permission_policy, "default_effect", "")),
            "workspace_root": str(self.context.workspace_root),
            "runtime_source": PRODUCTIZED_CONTRACT_SOURCE,
            "state_owner": "ToolPermissionPolicy",
        }

    def _metadata(
        self,
        *,
        session_snapshot: Mapping[str, Any],
        max_turns: int,
        tool_budget_externalization_count: int,
        tool_failure_signal_count: int,
        tool_schema_error_count: int,
        conflict_protected_count: int,
        compaction_count: int,
        tool_use_summary_count: int,
    ) -> dict[str, str]:
        session_metadata = snapshot_checkpoint_metadata(session_snapshot)
        orchestration = self.contracts.query_contract.get("toolOrchestration")
        if not isinstance(orchestration, dict):
            orchestration = {}
        return {
            **self.contracts.metadata(),
            "loop": "zyra_claude_query_engine_runtime",
            "query_session_id": session_metadata["query_session_id"],
            "query_session_resume_token": session_metadata["query_session_resume_token"],
            **session_metadata,
            "max_turns": str(max_turns),
            "tool_result_budget_chars": str(self.config.max_tool_result_chars),
            "tool_result_externalizations": str(tool_budget_externalization_count),
            "tool_failure_signals": str(tool_failure_signal_count),
            "tool_schema_errors": str(tool_schema_error_count),
            "tool_conflict_protected": str(conflict_protected_count),
            "query_context_budget_chars": str(self.config.max_query_context_chars),
            "context_compactions": str(compaction_count),
            "query_engine_contract_source": self.contracts.contract_source,
            "query_contract_source": self.contracts.contract_source,
            "session_contract_source": self.contracts.contract_source,
            "tool_loop_contract_source": self.contracts.contract_source,
            "tool_loop_contract_owner_unit": str(self.contracts.tool_loop_contract.get("ownerUnit") or ""),
            "tool_loop_contract_inventory_exists": str(self.contracts.tool_loop_contract.get("inventoryExists") is True).lower(),
            "tool_orchestration_read_only_concurrent": str(orchestration.get("readOnlyConcurrent") is True).lower(),
            "tool_orchestration_write_serial": str(orchestration.get("writeSerial") is True).lower(),
            "max_read_only_concurrency": str(self.config.max_read_only_concurrency),
            "tool_use_summaries": str(tool_use_summary_count),
            "sidecar_contracts_used": "false",
        }


def query_turns_from_constraints(constraints: Mapping[str, Any]) -> list[list[dict[str, Any]]]:
    return planned_query_turns_from_constraints(constraints)


def _normalize_turns(turns: Sequence[Sequence[Mapping[str, Any]]]) -> list[list[dict[str, Any]]]:
    normalized: list[list[dict[str, Any]]] = []
    for turn in turns:
        steps = [dict(step) for step in turn if isinstance(step, Mapping)]
        if steps:
            normalized.append(steps)
    return normalized


def _turn_user_content(turn_index: int, turn: Sequence[Mapping[str, Any]]) -> str:
    explicit_prompts = [
        str(step.get("prompt") or step.get("user_message") or "")
        for step in turn
        if isinstance(step, Mapping) and (step.get("prompt") or step.get("user_message"))
    ]
    if explicit_prompts:
        return "\n".join(explicit_prompts)
    tool_names = [str(step.get("tool_name") or step.get("tool") or "unknown_tool") for step in turn if isinstance(step, Mapping)]
    return f"Structured CodeWorker query turn {turn_index}: execute {', '.join(tool_names) or 'no tools'}."


def _request_message_chars(messages: Sequence[Any]) -> int:
    total = 0
    for message in messages:
        total += len(json.dumps(to_jsonable(message), ensure_ascii=False, sort_keys=True))
    return total


def _result_chars(result: ToolResult) -> int:
    return len(json.dumps(to_jsonable(result.output), ensure_ascii=False, sort_keys=True))


def _format_step_summary(summary: Mapping[str, Any], execution_mode: str) -> str:
    ok = "ok" if summary.get("ok") else f"error={summary.get('error') or 'tool_error'}"
    return (
        f"- turn {summary.get('turn_index')} batch {summary.get('batch_index')} "
        f"step {summary.get('step_index')} `{summary.get('tool_name')}` "
        f"({execution_mode}): {ok}; {summary.get('summary')}"
    )
