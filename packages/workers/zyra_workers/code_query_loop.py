from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from zyra_core import ArtifactKind, ArtifactRef, EventRecord, EventType, new_id, to_jsonable
from zyra_runtime import ToolCall, ToolExecutionContext, ToolExecutor, ToolResult, tool_result_event


READ_ONLY_TOOL_NAMES = {"file_read", "web_search", "browser", "trace", "checkpoint"}


@dataclass(frozen=True, slots=True)
class CodeQueryLoopConfig:
    max_turns: int | None = None
    max_tool_result_chars: int = 8000
    max_query_context_chars: int = 32000
    continue_on_error: bool = False
    query_contract: dict[str, Any] | None = None
    max_read_only_concurrency: int = 10
    emit_tool_use_summaries: bool = True


@dataclass(frozen=True, slots=True)
class CodeQueryLoopResult:
    ok: bool
    event_records: list[EventRecord]
    artifacts: list[ArtifactRef]
    step_summaries: list[str]
    turn_count: int
    tool_call_count: int
    context_compaction_count: int = 0
    stopped_reason: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _PlannedToolCall:
    step_index: int
    call: ToolCall
    read_only: bool


class CodeQueryLoop:
    """Contract-backed CodeWorker loop aligned with Claude Code's QueryEngine.

    M2 still receives model/planner-provided tool calls from the upstream
    harness, but session lifecycle, tool batching, budget handling, compaction
    events, and trace metadata are driven by the vendored QueryEngine contract.
    """

    def __init__(self, context: ToolExecutionContext, config: CodeQueryLoopConfig) -> None:
        self.context = context
        self.config = config

    def run(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        turns: list[list[dict[str, Any]]],
    ) -> CodeQueryLoopResult:
        executor = ToolExecutor(self.context)
        event_records: list[EventRecord] = []
        artifacts: list[ArtifactRef] = []
        step_summaries: list[str] = []
        context_entries: list[dict[str, Any]] = []
        context_chars = 0
        compaction_count = 0
        tool_use_summary_count = 0
        executed_turn_count = 0
        session_id = new_id("codesession")
        ok = True
        stopped_reason: str | None = None
        tool_call_count = 0
        contract = self.config.query_contract or {}
        orchestration = contract.get("toolOrchestration") if isinstance(contract.get("toolOrchestration"), dict) else {}
        source_files = contract.get("sourceFiles") if isinstance(contract.get("sourceFiles"), list) else []

        max_turns = self.config.max_turns or len(turns)
        event_records.append(
            _query_lifecycle_event(
                run_id,
                task_id,
                node_id,
                worker_request_id,
                session_id=session_id,
                phase="session_started",
                payload={
                    "max_turns": max_turns,
                    "tool_result_budget_chars": self.config.max_tool_result_chars,
                    "query_context_budget_chars": self.config.max_query_context_chars,
                },
            )
        )
        for turn_index, turn in enumerate(turns, start=1):
            if turn_index > max_turns:
                ok = False
                stopped_reason = "max_turns_exceeded"
                event_records.append(
                    _query_lifecycle_event(
                        run_id,
                        task_id,
                        node_id,
                        worker_request_id,
                        session_id=session_id,
                        phase="session_stopped",
                        payload={"turn_index": turn_index, "stopped_reason": stopped_reason},
                    )
                )
                break
            executed_turn_count += 1
            turn_started_at_count = tool_call_count
            event_records.append(
                _query_lifecycle_event(
                    run_id,
                    task_id,
                    node_id,
                    worker_request_id,
                    session_id=session_id,
                    phase="turn_started",
                    payload={"turn_index": turn_index, "planned_tool_calls": len(turn)},
                )
            )
            event_records.append(
                _query_lifecycle_event(
                    run_id,
                    task_id,
                    node_id,
                    worker_request_id,
                    session_id=session_id,
                    phase="stream_request_start",
                    payload={
                        "turn_index": turn_index,
                        "source_path": "src/query.ts",
                        "contract_source": str(contract.get("source") or ""),
                    },
                )
            )
            planned_calls = [
                self._planned_tool_call(
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    worker_request_id=worker_request_id,
                    turn_index=turn_index,
                    step_index=step_index,
                    step=step,
                )
                for step_index, step in enumerate(turn, start=1)
            ]
            for batch_index, batch in enumerate(self._partition_tool_calls(planned_calls), start=1):
                execution_mode = _batch_execution_mode(batch)
                event_records.append(
                    _query_lifecycle_event(
                        run_id,
                        task_id,
                        node_id,
                        worker_request_id,
                        session_id=session_id,
                        phase="tool_batch_started",
                        payload={
                            "turn_index": turn_index,
                            "batch_index": batch_index,
                            "tool_count": len(batch),
                            "execution_mode": execution_mode,
                            "source_path": "src/services/tools/toolOrchestration.ts",
                            "tool_names": [planned.call.tool_name for planned in batch],
                            "read_only": str(all(planned.read_only for planned in batch)).lower(),
                        },
                    )
                )
                for planned in batch:
                    event_records.append(
                        _query_lifecycle_event(
                            run_id,
                            task_id,
                            node_id,
                            worker_request_id,
                            session_id=session_id,
                            phase="tool_call_started",
                            payload={
                                "turn_index": turn_index,
                                "batch_index": batch_index,
                                "step_index": planned.step_index,
                                "tool_call_id": planned.call.tool_call_id,
                                "tool_name": planned.call.tool_name,
                                "read_only": str(planned.read_only).lower(),
                            },
                        )
                    )
                raw_results = self._execute_batch(executor, batch)
                batch_summaries: list[dict[str, Any]] = []
                for planned, result in zip(batch, raw_results, strict=True):
                    bounded_result = self._apply_tool_result_budget(planned.call, result)
                    event_records.append(tool_result_event(planned.call, bounded_result))
                    event_records.append(
                        _query_lifecycle_event(
                            run_id,
                            task_id,
                            node_id,
                            worker_request_id,
                            session_id=session_id,
                            phase="tool_call_completed",
                            payload={
                                "turn_index": turn_index,
                                "batch_index": batch_index,
                                "step_index": planned.step_index,
                                "tool_call_id": planned.call.tool_call_id,
                                "tool_name": planned.call.tool_name,
                                "ok": bounded_result.ok,
                                "error": bounded_result.error,
                                "artifact_ids": [artifact.artifact_id for artifact in bounded_result.artifacts],
                            },
                        )
                    )
                    artifacts.extend(bounded_result.artifacts)
                    tool_call_count += 1
                    step_summary = f"turn {turn_index}.{planned.step_index} {planned.call.tool_name}: {bounded_result.summary}"
                    step_summaries.append(step_summary)
                    result_chars = len(json.dumps(to_jsonable(bounded_result.output), ensure_ascii=False, sort_keys=True))
                    context_entries.append(
                        {
                            "turn_index": turn_index,
                            "batch_index": batch_index,
                            "step_index": planned.step_index,
                            "tool_name": planned.call.tool_name,
                            "summary": bounded_result.summary,
                            "ok": bounded_result.ok,
                            "error": bounded_result.error,
                            "result_chars": result_chars,
                            "artifact_ids": [artifact.artifact_id for artifact in bounded_result.artifacts],
                        }
                    )
                    batch_summaries.append(
                        {
                            "step_index": planned.step_index,
                            "tool_name": planned.call.tool_name,
                            "ok": bounded_result.ok,
                            "summary": bounded_result.summary,
                            "error": bounded_result.error,
                        }
                    )
                    context_chars += result_chars
                    if context_chars > self.config.max_query_context_chars:
                        artifact = self.context.artifact_store.write_text(
                            run_id=run_id,
                            task_id=task_id,
                            content=json.dumps(
                                {
                                    "session_id": session_id,
                                    "reason": "query_context_budget_exceeded",
                                    "context_chars": context_chars,
                                    "entries": context_entries,
                                },
                                ensure_ascii=False,
                                indent=2,
                            ),
                            title=f"CodeWorker query context compact {session_id}",
                            kind=ArtifactKind.STRUCTURED_DATA,
                            extension=".json",
                            producer_node_id=node_id,
                        )
                        artifacts.append(artifact)
                        compaction_count += 1
                        event_records.append(
                            _query_lifecycle_event(
                                run_id,
                                task_id,
                                node_id,
                                worker_request_id,
                                session_id=session_id,
                                phase="context_compacted",
                                payload={
                                    "turn_index": turn_index,
                                    "batch_index": batch_index,
                                    "step_index": planned.step_index,
                                    "context_chars": context_chars,
                                    "artifact_id": artifact.artifact_id,
                                },
                            )
                        )
                        context_entries = []
                        context_chars = 0
                    if not bounded_result.ok and not self.config.continue_on_error and stopped_reason is None:
                        ok = False
                        stopped_reason = bounded_result.error or "tool_step_failed"
                if self.config.emit_tool_use_summaries:
                    tool_use_summary_count += 1
                    event_records.append(
                        _query_lifecycle_event(
                            run_id,
                            task_id,
                            node_id,
                            worker_request_id,
                            session_id=session_id,
                            phase="tool_use_summary",
                            payload={
                                "turn_index": turn_index,
                                "batch_index": batch_index,
                                "execution_mode": execution_mode,
                                "tool_count": len(batch),
                                "source_path": "src/query.ts",
                                "summary": batch_summaries,
                            },
                        )
                    )
                event_records.append(
                    _query_lifecycle_event(
                        run_id,
                        task_id,
                        node_id,
                        worker_request_id,
                        session_id=session_id,
                        phase="tool_batch_completed",
                        payload={
                            "turn_index": turn_index,
                            "batch_index": batch_index,
                            "tool_count": len(batch),
                            "execution_mode": execution_mode,
                            "ok": all(item["ok"] for item in batch_summaries),
                            "stopped_reason": stopped_reason,
                        },
                    )
                )
                if not ok and not self.config.continue_on_error:
                    break
            event_records.append(
                _query_lifecycle_event(
                    run_id,
                    task_id,
                    node_id,
                    worker_request_id,
                    session_id=session_id,
                    phase="turn_completed",
                    payload={
                        "turn_index": turn_index,
                        "ok": ok,
                        "tool_calls": tool_call_count - turn_started_at_count,
                        "stopped_reason": stopped_reason,
                    },
                )
            )
            if not ok and not self.config.continue_on_error:
                break

        event_records.append(
            _query_lifecycle_event(
                run_id,
                task_id,
                node_id,
                worker_request_id,
                session_id=session_id,
                phase="session_completed",
                payload={
                    "ok": ok,
                    "tool_call_count": tool_call_count,
                    "turn_count": executed_turn_count,
                    "context_compaction_count": compaction_count,
                    "stopped_reason": stopped_reason,
                },
            )
        )
        return CodeQueryLoopResult(
            ok=ok,
            event_records=event_records,
            artifacts=artifacts,
            step_summaries=step_summaries,
            turn_count=executed_turn_count,
            tool_call_count=tool_call_count,
            context_compaction_count=compaction_count,
            stopped_reason=stopped_reason,
            metadata={
                "loop": "claude_code_query_engine_contract_loop",
                "query_session_id": session_id,
                "max_turns": str(max_turns),
                "tool_result_budget_chars": str(self.config.max_tool_result_chars),
                "query_context_budget_chars": str(self.config.max_query_context_chars),
                "context_compactions": str(compaction_count),
                "query_engine_contract_source": str(contract.get("source") or ""),
                "query_engine_contract_files": ",".join(str(item) for item in source_files[:12]),
                "tool_orchestration_read_only_concurrent": str(orchestration.get("readOnlyConcurrent") is True).lower(),
                "tool_orchestration_write_serial": str(orchestration.get("writeSerial") is True).lower(),
                "max_read_only_concurrency": str(self.config.max_read_only_concurrency),
                "tool_use_summaries": str(tool_use_summary_count),
            },
        )

    def _planned_tool_call(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        turn_index: int,
        step_index: int,
        step: dict[str, Any],
    ) -> _PlannedToolCall:
        tool_name = str(step.get("tool_name") or step.get("tool") or "")
        arguments = step.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        read_only = tool_name in READ_ONLY_TOOL_NAMES
        call = ToolCall(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            tool_name=tool_name,
            arguments=arguments,
            metadata={
                "worker_request_id": worker_request_id,
                "turn_index": str(turn_index),
                "step_index": str(step_index),
                "read_only": str(read_only).lower(),
            },
        )
        return _PlannedToolCall(step_index=step_index, call=call, read_only=read_only)

    def _partition_tool_calls(self, planned_calls: list[_PlannedToolCall]) -> list[list[_PlannedToolCall]]:
        batches: list[list[_PlannedToolCall]] = []
        for planned in planned_calls:
            if planned.read_only and batches and all(item.read_only for item in batches[-1]):
                if len(batches[-1]) < max(1, self.config.max_read_only_concurrency):
                    batches[-1].append(planned)
                else:
                    batches.append([planned])
            else:
                batches.append([planned])
        return batches

    def _execute_batch(self, executor: ToolExecutor, batch: list[_PlannedToolCall]) -> list[ToolResult]:
        if len(batch) > 1 and all(planned.read_only for planned in batch):
            max_workers = min(max(1, self.config.max_read_only_concurrency), len(batch))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                return list(pool.map(lambda planned: executor.execute(planned.call), batch))
        return [executor.execute(planned.call) for planned in batch]

    def _apply_tool_result_budget(self, call: ToolCall, result: ToolResult) -> ToolResult:
        payload = json.dumps(to_jsonable(result.output), ensure_ascii=False, sort_keys=True)
        if len(payload) <= self.config.max_tool_result_chars:
            return result
        artifact = self.context.artifact_store.write_text(
            run_id=call.run_id,
            task_id=call.task_id,
            content=payload,
            title=f"tool_result:{call.tool_name}:{call.tool_call_id}",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".json",
            producer_node_id=call.node_id,
        )
        return ToolResult(
            tool_call_id=result.tool_call_id,
            ok=result.ok,
            summary=f"{result.summary} (tool result output stored as artifact)",
            output={
                "truncated": True,
                "output_preview": payload[: self.config.max_tool_result_chars],
                "full_output_artifact_id": artifact.artifact_id,
            },
            artifacts=[*result.artifacts, artifact],
            error=result.error,
            completed_at=result.completed_at,
            metadata={**result.metadata, "tool_result_budget_applied": "true"},
        )


def query_turns_from_constraints(constraints: dict[str, Any]) -> list[list[dict[str, Any]]]:
    query_turns = constraints.get("query_turns")
    if isinstance(query_turns, list):
        turns: list[list[dict[str, Any]]] = []
        for turn in query_turns:
            if isinstance(turn, dict):
                steps = turn.get("tool_calls") or turn.get("steps")
            else:
                steps = turn
            if isinstance(steps, list):
                turns.append([dict(item) for item in steps if isinstance(item, dict)])
        return [turn for turn in turns if turn]

    tool_plan = constraints.get("tool_plan")
    if isinstance(tool_plan, list):
        return [[dict(item) for item in tool_plan if isinstance(item, dict)]]
    return []


def _batch_execution_mode(batch: list[_PlannedToolCall]) -> str:
    if all(planned.read_only for planned in batch):
        return "concurrent_read_only" if len(batch) > 1 else "serial_read_only"
    return "serial_non_read_only"


def _query_lifecycle_event(
    run_id: str,
    task_id: str,
    node_id: str | None,
    worker_request_id: str,
    *,
    session_id: str,
    phase: str,
    payload: dict[str, Any],
) -> EventRecord:
    return EventRecord(
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        event_type=EventType.AGENT_MESSAGE,
        payload={
            "query_session": {
                "session_id": session_id,
                "worker_request_id": worker_request_id,
                "phase": phase,
                **payload,
            }
        },
    )
