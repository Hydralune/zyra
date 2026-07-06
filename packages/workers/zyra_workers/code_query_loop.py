from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from zyra_core import ArtifactKind, ArtifactRef, EventRecord, EventType, to_jsonable
from zyra_runtime import (
    QuerySession,
    QueryStreamEventType,
    StopReason,
    ToolCall,
    ToolExecutionContext,
    ToolExecutor,
    ToolResult,
    snapshot_checkpoint_metadata,
    tool_result_event,
)


READ_ONLY_TOOL_NAMES = {"file_read", "web_search", "browser", "trace", "checkpoint"}


@dataclass(frozen=True, slots=True)
class CodeQueryLoopConfig:
    max_turns: int | None = None
    max_tool_result_chars: int = 8000
    max_query_context_chars: int = 32000
    continue_on_error: bool = False
    query_contract: dict[str, Any] | None = None
    session_contract: dict[str, Any] | None = None
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
    session_snapshot: dict[str, Any] = field(default_factory=dict)
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
        ok = True
        stopped_reason: str | None = None
        tool_call_count = 0
        contract = self.config.query_contract or {}
        session_contract = self.config.session_contract or {}
        orchestration = contract.get("toolOrchestration") if isinstance(contract.get("toolOrchestration"), dict) else {}
        source_files = contract.get("sourceFiles") if isinstance(contract.get("sourceFiles"), list) else []
        session = QuerySession(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            worker_request_id=worker_request_id,
            source_contract={
                "query_contract": contract,
                "session_contract": session_contract,
            },
        )
        session_id = session.session_id

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
                    "resume_token": session.resume_token,
                },
            )
        )
        for turn_index, turn in enumerate(turns, start=1):
            if turn_index > max_turns:
                ok = False
                stopped_reason = "max_turns_exceeded"
                session.record_error(
                    error=stopped_reason,
                    stop_reason=StopReason.MAX_TURNS_EXCEEDED,
                    metadata={"turn_index": turn_index, "max_turns": max_turns},
                )
                event_records.append(
                    _query_lifecycle_event(
                        run_id,
                        task_id,
                        node_id,
                        worker_request_id,
                        session_id=session_id,
                        phase="session_stopped",
                        payload={
                            "turn_index": turn_index,
                            "stopped_reason": stopped_reason,
                            "resume_token": session.resume_token,
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
                        phase="error",
                        payload={
                            "turn_index": turn_index,
                            "error": stopped_reason,
                            "stop_reason": str(StopReason.MAX_TURNS_EXCEEDED),
                            "resume_token": session.resume_token,
                        },
                    )
                )
                break
            executed_turn_count += 1
            turn_started_at_count = tool_call_count
            turn_state = session.start_turn(
                turn_index,
                user_content=_turn_user_content(turn_index, turn),
                metadata={"planned_tool_calls": len(turn), "source_path": "src/QueryEngine.ts"},
            )
            event_records.append(
                _query_lifecycle_event(
                    run_id,
                    task_id,
                    node_id,
                    worker_request_id,
                    session_id=session_id,
                    phase="turn_started",
                    payload={
                        "turn_index": turn_index,
                        "turn_id": turn_state.turn_id,
                        "planned_tool_calls": len(turn),
                        "resume_token": session.resume_token,
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
                    phase="turn_start",
                    payload={
                        "turn_index": turn_index,
                        "turn_id": turn_state.turn_id,
                        "planned_tool_calls": len(turn),
                        "resume_token": session.resume_token,
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
                    phase="message_delta",
                    payload={
                        "turn_index": turn_index,
                        "turn_id": turn_state.turn_id,
                        "role": "user",
                        "delta": _turn_user_content(turn_index, turn),
                        "resume_token": session.resume_token,
                    },
                )
            )
            session.start_stream_request(
                turn_id=turn_state.turn_id,
                metadata={
                    "turn_index": turn_index,
                    "source_path": "src/query.ts",
                    "contract_source": str(contract.get("source") or ""),
                },
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
                        "turn_id": turn_state.turn_id,
                        "source_path": "src/query.ts",
                        "contract_source": str(contract.get("source") or ""),
                        "resume_token": session.resume_token,
                    },
                )
            )
            session.start_assistant_message(
                turn_id=turn_state.turn_id,
                metadata={"turn_index": turn_index, "source_path": "src/query.ts"},
            )
            assistant_delta = f"Executing structured CodeWorker turn {turn_index} with {len(turn)} planned tool call(s)."
            session.append_assistant_delta(
                assistant_delta,
                metadata={"turn_index": turn_index, "planned_tool_calls": len(turn)},
            )
            event_records.append(
                _query_lifecycle_event(
                    run_id,
                    task_id,
                    node_id,
                    worker_request_id,
                    session_id=session_id,
                    phase="message_delta",
                    payload={
                        "turn_index": turn_index,
                        "turn_id": turn_state.turn_id,
                        "role": "assistant",
                        "delta": assistant_delta,
                        "resume_token": session.resume_token,
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
                session.record_batch_event(
                    QueryStreamEventType.TOOL_BATCH_STARTED,
                    turn_id=turn_state.turn_id,
                    metadata={
                        "turn_index": turn_index,
                        "batch_index": batch_index,
                        "tool_count": len(batch),
                        "execution_mode": execution_mode,
                    },
                )
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
                            "turn_id": turn_state.turn_id,
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
                                "turn_id": turn_state.turn_id,
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
                                "turn_id": turn_state.turn_id,
                                "batch_index": batch_index,
                                "step_index": planned.step_index,
                                "tool_call_id": planned.call.tool_call_id,
                                "tool_name": planned.call.tool_name,
                                "ok": bounded_result.ok,
                                "error": bounded_result.error,
                                "artifact_ids": [artifact.artifact_id for artifact in bounded_result.artifacts],
                                "resume_token": session.resume_token,
                            },
                        )
                    )
                    artifacts.extend(bounded_result.artifacts)
                    tool_call_count += 1
                    step_summary = f"turn {turn_index}.{planned.step_index} {planned.call.tool_name}: {bounded_result.summary}"
                    step_summaries.append(step_summary)
                    artifact_ids = [artifact.artifact_id for artifact in bounded_result.artifacts]
                    session.record_tool_result(
                        tool_call_id=planned.call.tool_call_id,
                        tool_name=planned.call.tool_name,
                        summary=bounded_result.summary,
                        ok=bounded_result.ok,
                        error=bounded_result.error,
                        artifacts=artifact_ids,
                        turn_id=turn_state.turn_id,
                        metadata={
                            "turn_index": turn_index,
                            "batch_index": batch_index,
                            "step_index": planned.step_index,
                        },
                    )
                    event_records.append(
                        _query_lifecycle_event(
                            run_id,
                            task_id,
                            node_id,
                            worker_request_id,
                            session_id=session_id,
                            phase="message_delta",
                            payload={
                                "turn_index": turn_index,
                                "turn_id": turn_state.turn_id,
                                "role": "tool",
                                "tool_call_id": planned.call.tool_call_id,
                                "tool_name": planned.call.tool_name,
                                "delta": bounded_result.summary,
                                "ok": bounded_result.ok,
                                "error": bounded_result.error,
                                "resume_token": session.resume_token,
                            },
                        )
                    )
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
                        session.record_context_compaction(
                            artifact_id=artifact.artifact_id,
                            metadata={
                                "turn_index": turn_index,
                                "batch_index": batch_index,
                                "step_index": planned.step_index,
                                "context_chars": context_chars,
                            },
                        )
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
                                    "turn_id": turn_state.turn_id,
                                    "batch_index": batch_index,
                                    "step_index": planned.step_index,
                                    "context_chars": context_chars,
                                    "artifact_id": artifact.artifact_id,
                                    "resume_token": session.resume_token,
                                },
                            )
                        )
                        context_entries = []
                        context_chars = 0
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
                        event_records.append(
                            _query_lifecycle_event(
                                run_id,
                                task_id,
                                node_id,
                                worker_request_id,
                                session_id=session_id,
                                phase="error",
                                payload={
                                    "turn_index": turn_index,
                                    "turn_id": turn_state.turn_id,
                                    "tool_call_id": planned.call.tool_call_id,
                                    "tool_name": planned.call.tool_name,
                                    "error": stopped_reason,
                                    "stop_reason": str(StopReason.TOOL_ERROR),
                                    "resume_token": session.resume_token,
                                },
                            )
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
                        event_records.append(
                            _query_lifecycle_event(
                                run_id,
                                task_id,
                                node_id,
                                worker_request_id,
                                session_id=session_id,
                                phase="continue",
                                payload={
                                    "turn_index": turn_index,
                                    "turn_id": turn_state.turn_id,
                                    "tool_call_id": planned.call.tool_call_id,
                                    "tool_name": planned.call.tool_name,
                                    "reason": str(StopReason.CONTINUE_REQUESTED),
                                    "error": bounded_result.error,
                                    "resume_token": session.resume_token,
                                },
                            )
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
                            "tool_count": len(batch),
                            "summary": batch_summaries,
                        },
                    )
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
                                "turn_id": turn_state.turn_id,
                                "batch_index": batch_index,
                                "execution_mode": execution_mode,
                                "tool_count": len(batch),
                                "source_path": "src/query.ts",
                                "summary": batch_summaries,
                                "resume_token": session.resume_token,
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
                            "turn_id": turn_state.turn_id,
                            "batch_index": batch_index,
                            "tool_count": len(batch),
                            "execution_mode": execution_mode,
                            "ok": all(item["ok"] for item in batch_summaries),
                            "stopped_reason": stopped_reason,
                            "resume_token": session.resume_token,
                        },
                    )
                )
                session.record_batch_event(
                    QueryStreamEventType.TOOL_BATCH_COMPLETED,
                    turn_id=turn_state.turn_id,
                    metadata={
                        "turn_index": turn_index,
                        "batch_index": batch_index,
                        "tool_count": len(batch),
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
                        "turn_id": turn_state.turn_id,
                        "ok": ok,
                        "tool_calls": tool_call_count - turn_started_at_count,
                        "stopped_reason": stopped_reason,
                        "resume_token": session.resume_token,
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
                    phase="turn_end",
                    payload={
                        "turn_index": turn_index,
                        "turn_id": turn_state.turn_id,
                        "ok": ok or self.config.continue_on_error,
                        "tool_calls": tool_call_count - turn_started_at_count,
                        "stopped_reason": stopped_reason,
                        "resume_token": session.resume_token,
                    },
                )
            )
            if not ok and not self.config.continue_on_error:
                break

        session.complete_session(
            ok=ok,
            stop_reason=StopReason.SESSION_COMPLETED if ok else StopReason.TOOL_ERROR,
            metadata={
                "tool_call_count": tool_call_count,
                "turn_count": executed_turn_count,
                "context_compaction_count": compaction_count,
                "stopped_reason": stopped_reason,
            },
        )
        session_snapshot = session.snapshot_payload(
            include_transcript=True,
            metadata={
                "tool_call_count": tool_call_count,
                "turn_count": executed_turn_count,
                "context_compaction_count": compaction_count,
                "stopped_reason": stopped_reason,
            },
        )
        snapshot_artifact = self.context.artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content=json.dumps(session_snapshot, ensure_ascii=False, indent=2, sort_keys=True),
            title=f"CodeWorker query session snapshot {session_id}",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".json",
            producer_node_id=node_id,
        )
        transcript_artifact = self.context.artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content=session.to_jsonl(),
            title=f"CodeWorker query session transcript {session_id}",
            kind=ArtifactKind.TRACE,
            extension=".jsonl",
            producer_node_id=node_id,
        )
        artifacts.extend([snapshot_artifact, transcript_artifact])
        event_records.append(
            _query_lifecycle_event(
                run_id,
                task_id,
                node_id,
                worker_request_id,
                session_id=session_id,
                phase="query_session_snapshot",
                payload={
                    "snapshot_artifact_id": snapshot_artifact.artifact_id,
                    "transcript_artifact_id": transcript_artifact.artifact_id,
                    "resume_token": session.resume_token,
                    "consistency": session_snapshot.get("consistency", {}),
                    "stats": session_snapshot.get("stats", {}),
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
                phase="session_completed",
                payload={
                    "ok": ok,
                    "tool_call_count": tool_call_count,
                    "turn_count": executed_turn_count,
                    "context_compaction_count": compaction_count,
                    "stopped_reason": stopped_reason,
                    "resume_token": session.resume_token,
                },
            )
        )
        session_metadata = snapshot_checkpoint_metadata(session_snapshot)
        return CodeQueryLoopResult(
            ok=ok,
            event_records=event_records,
            artifacts=artifacts,
            step_summaries=step_summaries,
            turn_count=executed_turn_count,
            tool_call_count=tool_call_count,
            context_compaction_count=compaction_count,
            stopped_reason=stopped_reason,
            session_snapshot=session_snapshot,
            metadata={
                "loop": "claude_code_query_engine_contract_loop",
                "query_session_id": session_id,
                "query_session_resume_token": session.resume_token,
                "query_session_snapshot_artifact_id": snapshot_artifact.artifact_id,
                "query_session_transcript_artifact_id": transcript_artifact.artifact_id,
                **session_metadata,
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


def _turn_user_content(turn_index: int, turn: list[dict[str, Any]]) -> str:
    explicit_prompts = [
        str(step.get("prompt") or step.get("user_message") or "")
        for step in turn
        if isinstance(step, dict) and (step.get("prompt") or step.get("user_message"))
    ]
    if explicit_prompts:
        return "\n".join(explicit_prompts)
    tool_names = [
        str(step.get("tool_name") or step.get("tool") or "unknown_tool")
        for step in turn
        if isinstance(step, dict)
    ]
    return f"Structured CodeWorker query turn {turn_index}: execute {', '.join(tool_names) or 'no tools'}."


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
