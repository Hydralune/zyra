from __future__ import annotations

import json
import hashlib
import time
import os
import queue
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Mapping, Sequence

from zyra_core import ArtifactKind, EventRecord, EventType, to_jsonable
from zyra_runtime.query_session import QuerySession, QueryStreamEventType, StopReason
from zyra_runtime.typescript_runtime_host import (
    ClaudeQueryEngineConfig,
    ClaudeQueryEngineResult,
)
from zyra_runtime.executor import ToolExecutionContext, ToolExecutor
from zyra_runtime.permission import (
    PermissionEvaluationRequest,
    PermissionMode,
    ToolIdentity,
)
from zyra_runtime.permission.runtime import (
    PermissionRuntimeConfig,
    ToolPermissionRuntime,
)
from zyra_runtime.tools import ToolCall, ToolResult

from .code_worker_bridge import code_worker_entrypoint
from .subagents.typescript_port import TypeScriptAgentDurablePort


RUNTIME_PROTOCOL_VERSION = "zyra.claude-runtime.v1"
TYPESCRIPT_RUNTIME_ID = "zyra-typescript-claude-runtime"


class TypeScriptRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _ProcessLineReader:
    def __init__(self, stream: Any) -> None:
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self._thread.start()

    def get(self, timeout: float) -> str | None:
        try:
            return self._lines.get(timeout=max(0.01, timeout))
        except queue.Empty as error:
            raise TypeScriptRuntimeError(
                "typescript_runtime_timeout",
                "TypeScript runtime did not produce a protocol frame before the deadline.",
            ) from error

    def _read(self, stream: Any) -> None:
        try:
            for line in iter(stream.readline, ""):
                self._lines.put(line)
        finally:
            self._lines.put(None)


class TypeScriptClaudeQueryEngine:
    """Canonical TypeScript query runtime with a narrow Python side-effect host.

    This preserves the QueryEngine factory protocol expected by CodeWorker while
    refusing to invoke the historical Python query loop as a fallback.
    """

    def __init__(
        self,
        context: ToolExecutionContext,
        config: ClaudeQueryEngineConfig | None = None,
    ) -> None:
        self.context = context
        self.config = config or ClaudeQueryEngineConfig()
        self.project_root = Path(self.config.project_root or Path.cwd()).resolve()
        self.entrypoint = code_worker_entrypoint(self.project_root)
        self._host_events: list[EventRecord] = []
        self._host_artifacts: list[Any] = []
        self._latest_runtime_checkpoint: dict[str, Any] = {}
        self._tool_effect_receipts: dict[str, dict[str, Any]] = {}
        self._tool_effect_receipts_lock = threading.RLock()
        self._last_tool_batch_evidence: dict[str, Any] = {}

    def run(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        turns: Sequence[Any],
        request_messages: Sequence[Any] = (),
        request_metadata: Mapping[str, Any] | None = None,
    ) -> ClaudeQueryEngineResult:
        session_id = self._session_id(run_id, request_metadata)
        projection = QuerySession(
            run_id=run_id,
            task_id=task_id,
            worker_request_id=worker_request_id,
            node_id=node_id,
            session_id=session_id,
            source_contract={
                "source": "zyra-typescript-runtime",
                "runtime_id": TYPESCRIPT_RUNTIME_ID,
                "canonical_owner": "typescript",
                "projection_owner": "python-noncanonical",
            },
            metadata={
                "canonical_runtime_owner": "typescript",
                "runtime_protocol": RUNTIME_PROTOCOL_VERSION,
            },
        )
        disabled_component = next(
            (
                component
                for disabled, component in (
                    (self.config.disable_tool_permission_runtime, "ToolPermissionRuntime"),
                    (self.config.disable_permission_rule_store, "PermissionRuleStore"),
                    (self.config.disable_permission_request_queue, "PermissionRequestQueue"),
                    (self.config.disable_permission_decision_log, "PermissionDecisionLog"),
                )
                if disabled
            ),
            "",
        )
        if disabled_component:
            self._host_events.append(
                EventRecord(
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    event_type=EventType.NODE_FAILED,
                    payload={
                        "query_session": {
                            "phase": "tool_loop_foundation_disabled",
                            "session_id": session_id,
                            "canonical_owner": "typescript",
                            "disabled_component": disabled_component,
                        }
                    },
                )
            )
            failed = self._failed_result(
                error=TypeScriptRuntimeError(
                    "tool_loop_foundation_disabled",
                    f"Required durable permission component is disabled: {disabled_component}",
                ),
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                session_id=session_id,
                projection=projection,
            )
            failed.metadata["tool_foundation_disabled_component"] = disabled_component
            return failed
        try:
            return self._run_process(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                worker_request_id=worker_request_id,
                session_id=session_id,
                turns=turns,
                request_messages=request_messages,
                request_metadata=request_metadata or {},
                projection=projection,
            )
        except TypeScriptRuntimeError as error:
            return self._failed_result(
                error=error,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                session_id=session_id,
                projection=projection,
            )
        except Exception as error:  # noqa: BLE001 - process boundary fails closed.
            return self._failed_result(
                error=TypeScriptRuntimeError(
                    "typescript_runtime_process_failed",
                    f"{type(error).__name__}: {error}",
                ),
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                session_id=session_id,
                projection=projection,
            )

    def _run_process(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        session_id: str,
        turns: Sequence[Any],
        request_messages: Sequence[Any],
        request_metadata: Mapping[str, Any],
        projection: QuerySession,
    ) -> ClaudeQueryEngineResult:
        constraints = dict(self.config.runtime_constraints)
        if (
            os.environ.get("ZYRA_DISABLE_TYPESCRIPT_RUNTIME") == "1"
            or constraints.get("disable_typescript_runtime") is True
        ):
            raise TypeScriptRuntimeError(
                "typescript_runtime_disabled",
                "The canonical TypeScript query runtime is disabled.",
            )
        command, transport = self._runtime_command()
        timeout_seconds = min(
            600.0,
            max(1.0, float(constraints.get("typescript_runtime_timeout_seconds") or 120.0)),
        )
        process = subprocess.Popen(
            command,
            cwd=self.project_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=self._runtime_environment(),
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            raise TypeScriptRuntimeError(
                "typescript_runtime_process_failed",
                "TypeScript runtime process pipes were not created.",
            )
        reader = _ProcessLineReader(process.stdout)
        deadline = time.monotonic() + timeout_seconds
        outbound_sequence = 1
        inbound_sequence = 1
        raw_restored_runtime_state = self.config.restored_runtime_state or {}
        nested_session_snapshot = raw_restored_runtime_state.get("session_snapshot")
        restored_runtime_state = (
            dict(nested_session_snapshot)
            if isinstance(nested_session_snapshot, Mapping)
            else dict(raw_restored_runtime_state)
        )
        durable_checkpoint = self._load_incremental_checkpoint(session_id)
        provided_revision = int(
            restored_runtime_state.get("revision")
            or dict(restored_runtime_state.get("typescript_runtime_snapshot") or {}).get("revision")
            or 0
        )
        durable_revision = int(durable_checkpoint.get("revision") or 0)
        if durable_checkpoint and durable_revision >= provided_revision:
            restored_runtime_state.update(durable_checkpoint)
        self._latest_runtime_checkpoint = dict(
            restored_runtime_state.get("typescript_runtime_snapshot") or restored_runtime_state
        )
        self._tool_effect_receipts = {
            str(key): dict(value)
            for key, value in dict(restored_runtime_state.get("tool_effect_receipts") or {}).items()
            if isinstance(value, Mapping)
        }
        restored_session_id = str(restored_runtime_state.get("session_id") or "")
        if restored_session_id and restored_session_id != session_id:
            restored_runtime_state.pop("permission_runtime", None)
            restored_runtime_state.pop("permission_continuation", None)
            restored_runtime_state.pop("permission_continuation_payloads", None)
        restored_permission_value = restored_runtime_state.get("permission_runtime")
        restored_mode_value = (
            dict(restored_permission_value.get("mode") or {}).get("mode")
            if isinstance(restored_permission_value, Mapping)
            else ""
        )
        permission_runtime = self._permission_runtime(
            session_id=session_id,
            restored_state=restored_runtime_state,
        )
        from zyra_runtime.typescript_runtime_host import (
            permission_continuation_payloads,
            reconcile_permission_continuation_payloads,
            synchronize_permission_continuations,
        )
        from zyra_runtime.permission.continuation import PermissionContinuationRuntime

        continuation_payloads = permission_continuation_payloads(restored_runtime_state)
        permission_continuation = PermissionContinuationRuntime(
            permission_runtime.state_store,
            session_id=session_id,
            payload_resolver=lambda record: continuation_payloads.get(
                record.payload_locator
            ),
            disabled=self.config.disable_permission_continuation_runtime,
        )
        restored_continuation = restored_runtime_state.get("permission_continuation")
        if isinstance(restored_continuation, Mapping) and restored_continuation:
            permission_continuation.restore(restored_continuation)
        synchronize_permission_continuations(
            permission_continuation,
            permission_runtime,
            continuation_payloads,
            payload_tombstoner=self.config.permission_continuation_payload_tombstoner,
        )
        reconcile_permission_continuation_payloads(
            permission_continuation,
            continuation_payloads,
            payload_tombstoner=self.config.permission_continuation_payload_tombstoner,
        )
        continuation_sequence = [
            int(permission_continuation.snapshot().get("session_sequence") or 0)
        ]
        pending_typescript_settlements: dict[str, Any] = {}
        raw_agent_state_root = (
            constraints.get("typescriptAgentStatePath")
            or constraints.get("typescript_agent_state_path")
            or self.context.artifact_store.root / ".subagents"
        )
        agent_port = TypeScriptAgentDurablePort(
            raw_agent_state_root,
            workspace_root=self.context.workspace_root,
            event_sink=self._host_events.append,
        )
        self._host_events.append(
            EventRecord(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "query_session": {
                        "phase": "permission_runtime_attached",
                        "canonical_owner": "typescript",
                        "session_id": session_id,
                        "worker_request_id": worker_request_id,
                        "permission_runtime": {
                            "policy_owner": "typescript",
                            "durable_state_owner": "python",
                            "mode": str(permission_runtime.mode_runtime.mode),
                            "python_policy_fallback": False,
                        },
                        "permission_mode_reconciliation": {
                            "changed": bool(restored_mode_value)
                            and str(restored_mode_value)
                            != str(permission_runtime.mode_runtime.mode),
                            "from_mode": str(
                                restored_mode_value or self.config.permission_mode
                            ),
                            "to_mode": str(permission_runtime.mode_runtime.mode),
                        },
                    }
                },
            )
        )
        executor = ToolExecutor(
            self.context,
            permission_authority=permission_runtime,
        )
        self._write_frame(
            process,
            run_id=run_id,
            sequence=outbound_sequence,
            kind="run.start",
            payload={
                "task_id": task_id,
                "node_id": node_id,
                "worker_request_id": worker_request_id,
                "session_id": session_id,
                "messages": to_jsonable(list(request_messages)),
                "turns": to_jsonable(list(turns)),
                "tools": [to_jsonable(spec) for spec in self.context.registry.list()],
                "config": self._typescript_config(permission_runtime),
                "session_seed": to_jsonable(self.config.session_seed or {}),
                "context_snapshot": to_jsonable(self.config.context_snapshot or {}),
                "restored_state": to_jsonable(restored_runtime_state),
                "metadata": to_jsonable(dict(request_metadata)),
            },
        )
        outbound_sequence += 1
        if constraints.get("kill_typescript_runtime_after_start") is True:
            process.kill()

        accepted = self._read_frame(
            reader,
            process,
            run_id=run_id,
            expected_sequence=inbound_sequence,
            deadline=deadline,
        )
        inbound_sequence += 1
        if accepted["kind"] != "run.accepted":
            self._terminate(process)
            raise TypeScriptRuntimeError(
                "typescript_runtime_protocol_error",
                "TypeScript runtime did not acknowledge run.start.",
            )

        result_payload: dict[str, Any] | None = None
        projection_error = ""
        while result_payload is None:
            frame = self._read_frame(
                reader,
                process,
                run_id=run_id,
                expected_sequence=inbound_sequence,
                deadline=deadline,
            )
            inbound_sequence += 1
            kind = str(frame["kind"])
            correlation_id = str(frame.get("correlation_id") or "")
            payload = dict(frame.get("payload") or {})
            if kind == "runtime.event":
                self._host_events.append(
                    self._event_record(
                        run_id=run_id,
                        task_id=task_id,
                        node_id=node_id,
                        parent_session_id=session_id,
                        payload=payload,
                    )
                )
                payload_session_id = str(payload.get("session_id") or "")
                is_child_event = bool(
                    payload_session_id and payload_session_id != session_id
                )
                if not is_child_event and not projection_error:
                    try:
                        self._project_event(projection, payload)
                    except Exception as error:  # noqa: BLE001 - noncanonical compatibility projection.
                        projection_error = f"{type(error).__name__}: {error}"
                continue
            if kind == "runtime.checkpoint":
                checkpoint = dict(payload.get("snapshot") or {})
                checkpoint["tool_effect_receipts"] = to_jsonable(self._tool_effect_receipts)
                checkpoint["tool_batch_evidence"] = to_jsonable(self._last_tool_batch_evidence)
                checkpoint["permission_runtime"] = to_jsonable(permission_runtime.snapshot())
                checkpoint["permission_continuation"] = to_jsonable(permission_continuation.snapshot())
                checkpoint["permission_continuation_payloads"] = to_jsonable(continuation_payloads)
                self._latest_runtime_checkpoint = checkpoint
                self._persist_incremental_checkpoint(session_id, checkpoint)
                self._write_frame(
                    process,
                    run_id=run_id,
                    sequence=outbound_sequence,
                    kind="runtime.checkpoint.result",
                    payload={"accepted": True},
                    correlation_id=correlation_id,
                )
                outbound_sequence += 1
                continue
            if kind == "tool.batch.request":
                batch_started_at = time.perf_counter()
                raw_requests = list(payload.get("requests") or [])
                request_payloads = [dict(item) for item in raw_requests if isinstance(item, Mapping)]
                if len(request_payloads) != len(raw_requests):
                    raise TypeScriptRuntimeError("typescript_runtime_protocol_error", "tool batch contains a non-object request")

                def execute_one(item: Mapping[str, Any]) -> dict[str, Any]:
                    return self._execute_tool_request(
                        payload=item,
                        run_id=run_id,
                        task_id=task_id,
                        node_id=node_id,
                        worker_request_id=worker_request_id,
                        session_id=session_id,
                        executor=executor,
                        permission_runtime=permission_runtime,
                        permission_continuation=permission_continuation,
                        continuation_payloads=continuation_payloads,
                        continuation_sequence=continuation_sequence,
                        pending_typescript_settlements=pending_typescript_settlements,
                        tool_effect_receipts=self._tool_effect_receipts,
                        receipt_lock=self._tool_effect_receipts_lock,
                    )

                execution_mode = str(payload.get("execution_mode") or "serial_non_read_only")
                timeout_seconds = max(0.001, min(600.0, float(payload.get("timeout_ms") or 30000) / 1000.0))
                if execution_mode == "concurrent_read_only" and len(request_payloads) > 1:
                    configured_concurrency = int(
                        self.config.runtime_constraints.get("max_read_only_concurrency") or 10
                    )
                    pool = ThreadPoolExecutor(
                        max_workers=min(len(request_payloads), max(1, configured_concurrency))
                    )
                    futures = [pool.submit(execute_one, item) for item in request_payloads]
                    results: list[dict[str, Any]] = []
                    batch_deadline = time.monotonic() + timeout_seconds
                    for index, future in enumerate(futures):
                        remaining = max(0.001, batch_deadline - time.monotonic())
                        try:
                            results.append(future.result(timeout=remaining))
                        except FutureTimeoutError:
                            future.cancel()
                            tool_call_id = str(request_payloads[index].get("tool_call_id") or "")
                            results.append({
                                "tool_call_id": tool_call_id,
                                "ok": False,
                                "summary": "Read-only tool exceeded the batch deadline",
                                "output": {},
                                "artifacts": [],
                                "error": "tool_execution_timeout",
                                "metadata": {"late_result_fenced": "true", "execution_mode": execution_mode},
                            })
                    pool.shutdown(wait=False, cancel_futures=True)
                else:
                    results = [execute_one(item) for item in request_payloads]
                self._last_tool_batch_evidence = {
                    "batch_id": str(payload.get("batch_id") or ""),
                    "execution_mode": execution_mode,
                    "request_count": len(request_payloads),
                    "elapsed_ms": round((time.perf_counter() - batch_started_at) * 1000.0, 3),
                    "result_order": [str(item.get("tool_call_id") or "") for item in results],
                }
                self._write_frame(
                    process,
                    run_id=run_id,
                    sequence=outbound_sequence,
                    kind="tool.batch.result",
                    payload={"batch_id": str(payload.get("batch_id") or ""), "results": results},
                    correlation_id=correlation_id,
                )
                outbound_sequence += 1
                continue
            if kind == "tool.request":
                tool_result = self._execute_tool_request(
                    payload=payload,
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    worker_request_id=worker_request_id,
                    session_id=session_id,
                    executor=executor,
                    permission_runtime=permission_runtime,
                    permission_continuation=permission_continuation,
                    continuation_payloads=continuation_payloads,
                    continuation_sequence=continuation_sequence,
                    pending_typescript_settlements=pending_typescript_settlements,
                    tool_effect_receipts=self._tool_effect_receipts,
                    receipt_lock=self._tool_effect_receipts_lock,
                )
                self._write_frame(
                    process,
                    run_id=run_id,
                    sequence=outbound_sequence,
                    kind="tool.result",
                    payload=tool_result,
                    correlation_id=correlation_id,
                )
                outbound_sequence += 1
                continue
            if kind == "tool.settle":
                settlement = self._settle_typescript_capability(
                    payload=payload,
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    worker_request_id=worker_request_id,
                    permission_continuation=permission_continuation,
                    continuation_payloads=continuation_payloads,
                    pending_settlements=pending_typescript_settlements,
                )
                self._write_frame(
                    process,
                    run_id=run_id,
                    sequence=outbound_sequence,
                    kind="tool.settle.result",
                    payload=settlement,
                    correlation_id=correlation_id,
                )
                outbound_sequence += 1
                continue
            if kind == "agent.mutate":
                mutation = agent_port.handle(
                    payload,
                    run_id=run_id,
                    parent_task_id=task_id,
                    parent_session_id=session_id,
                )
                self._write_frame(
                    process,
                    run_id=run_id,
                    sequence=outbound_sequence,
                    kind="agent.mutate.result",
                    payload=mutation,
                    correlation_id=correlation_id,
                )
                outbound_sequence += 1
                continue
            if kind == "artifact.request":
                artifact = self._write_artifact(
                    payload=payload,
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                )
                self._write_frame(
                    process,
                    run_id=run_id,
                    sequence=outbound_sequence,
                    kind="artifact.result",
                    payload={"artifact": to_jsonable(artifact)},
                    correlation_id=correlation_id,
                )
                outbound_sequence += 1
                continue
            if kind == "runtime.error":
                self._terminate(process)
                raise TypeScriptRuntimeError(
                    str(payload.get("code") or "typescript_runtime_process_failed"),
                    str(payload.get("message") or "TypeScript runtime reported an error."),
                )
            if kind == "run.result":
                if pending_typescript_settlements:
                    for pending_tool_call_id in list(pending_typescript_settlements):
                        self._settle_typescript_capability(
                            payload={
                                "tool_call_id": pending_tool_call_id,
                                "ok": False,
                                "error": "typescript_capability_settlement_missing",
                            },
                            run_id=run_id,
                            task_id=task_id,
                            node_id=node_id,
                            worker_request_id=worker_request_id,
                            permission_continuation=permission_continuation,
                            continuation_payloads=continuation_payloads,
                            pending_settlements=pending_typescript_settlements,
                        )
                    self._terminate(process)
                    raise TypeScriptRuntimeError(
                        "typescript_runtime_protocol_error",
                        "TypeScript runtime completed with unsettled capability executions.",
                    )
                result_payload = dict(payload.get("result") or {})
                break
            self._terminate(process)
            raise TypeScriptRuntimeError(
                "typescript_runtime_protocol_error",
                f"Unexpected TypeScript runtime frame: {kind}",
            )

        process.stdin.close()
        try:
            exit_code = process.wait(timeout=max(1.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            self._terminate(process)
            raise TypeScriptRuntimeError(
                "typescript_runtime_timeout",
                "TypeScript runtime did not exit after run.result.",
            ) from error
        stderr = process.stderr.read().strip()
        if exit_code != 0:
            raise TypeScriptRuntimeError(
                "typescript_runtime_process_failed",
                stderr or f"TypeScript runtime exited with status {exit_code}.",
            )

        ok = result_payload.get("ok") is True
        stopped_reason = str(result_payload.get("stoppedReason") or "") or None
        if projection.active_turn is not None:
            projection.end_turn(
                ok=ok,
                stop_reason=self._stop_reason(stopped_reason),
                error=None if ok else stopped_reason,
                metadata={"canonical_owner": "typescript"},
            )
        projection.complete_session(
            ok=ok,
            stop_reason=self._stop_reason(stopped_reason, complete=ok),
            metadata={
                "canonical_owner": "typescript",
                "typescript_runtime_id": TYPESCRIPT_RUNTIME_ID,
                "typescript_runtime_snapshot": dict(result_payload.get("sessionSnapshot") or {}),
                "projection_error": projection_error,
            },
        )
        session_snapshot = to_jsonable(projection.snapshot())
        session_snapshot["typescript_runtime_snapshot"] = to_jsonable(
            dict(result_payload.get("sessionSnapshot") or {})
        )
        session_snapshot["permission_runtime"] = to_jsonable(
            permission_runtime.snapshot()
        )
        session_snapshot["permission_continuation"] = to_jsonable(
            permission_continuation.snapshot()
        )
        session_snapshot["permission_continuation_payloads"] = to_jsonable(
            continuation_payloads
        )
        session_snapshot["tool_effect_receipts"] = to_jsonable(self._tool_effect_receipts)
        session_snapshot["tool_batch_evidence"] = to_jsonable(self._last_tool_batch_evidence)
        session_snapshot["runtime_state"] = {
            "schema_version": 1,
            "query_session_id": session_id,
            "query_session_resume_token": str(
                session_snapshot.get("resume_token") or session_id
            ),
            "session_snapshot": {
                key: value
                for key, value in session_snapshot.items()
                if key != "runtime_state"
            },
            "permission_runtime": session_snapshot["permission_runtime"],
            "permission_continuation": session_snapshot[
                "permission_continuation"
            ],
            "permission_continuation_payloads": session_snapshot[
                "permission_continuation_payloads"
            ],
        }
        snapshot_artifact = self.context.artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content=json.dumps(
                session_snapshot,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            title="CodeWorker QuerySession Snapshot",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".json",
            producer_node_id=node_id,
        )
        self._host_artifacts.append(snapshot_artifact)
        transcript_lines = [
            json.dumps(
                {
                    "type": "session_metadata",
                    "session_id": session_id,
                    "canonical_runtime_owner": "typescript",
                    "runtime_protocol": RUNTIME_PROTOCOL_VERSION,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        ]
        for event in self._host_events:
            event_payload = getattr(event, "payload", {})
            query_event = (
                event_payload.get("query_session")
                if isinstance(event_payload, Mapping)
                else None
            )
            if not isinstance(query_event, Mapping):
                continue
            transcript_lines.append(
                json.dumps(
                    {
                        "type": "runtime_event",
                        "event_type": str(query_event.get("phase") or "runtime_event"),
                        "canonical_runtime_owner": "typescript",
                        **to_jsonable(dict(query_event)),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        transcript_artifact = self.context.artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content="\n".join(transcript_lines) + "\n",
            title="CodeWorker TypeScript Runtime Transcript",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".jsonl",
            producer_node_id=node_id,
        )
        self._host_artifacts.append(transcript_artifact)
        snapshot_stats = dict(session_snapshot.get("stats") or {})
        snapshot_consistency = dict(session_snapshot.get("consistency") or {})
        metadata = {
            str(key): str(value)
            for key, value in dict(result_payload.get("metadata") or {}).items()
        }
        metadata.update(
            {
                "loop": "zyra_typescript_query_engine_runtime",
                "canonical_runtime_owner": "typescript",
                "typescript_runtime_id": TYPESCRIPT_RUNTIME_ID,
                "runtime_protocol": RUNTIME_PROTOCOL_VERSION,
                "runtime_transport": transport,
                "python_query_engine_fallback": "false",
                "python_session_projection_canonical": "false",
                "query_session_id": session_id,
                "projection_error": projection_error,
                "query_session_snapshot_artifact_id": snapshot_artifact.artifact_id,
                "query_session_transcript_artifact_id": transcript_artifact.artifact_id,
                "query_session_checkpoint_ready": str(
                    snapshot_consistency.get("ok") is True
                ).lower(),
                "query_session_turns": str(
                    snapshot_stats.get("turn_count")
                    or result_payload.get("turnCount")
                    or 0
                ),
                "query_session_consistent": str(
                    snapshot_consistency.get("ok") is True
                ).lower(),
                "query_session_resume_token": str(
                    session_snapshot.get("resume_token") or session_id
                ),
                "permission_continuation_pending": str(
                    len(permission_continuation.pending())
                ),
                "permission_continuation_payload_count": str(
                    len(continuation_payloads)
                ),
                "permission_continuation_suspended": str(
                    stopped_reason == "permission_suspended"
                ).lower(),
            }
        )
        metadata.update(permission_runtime.metadata())
        metadata.update(
            {
                "canonical_permission_owner": "typescript",
                "python_policy_fallback": "false",
                "permission_mode_from_state_owner": str(
                    permission_runtime.mode_runtime.mode
                ),
            }
        )
        if self.config.permission_extension_registry is not None:
            metadata.update(self.config.permission_extension_registry.metadata())
        result_artifacts = [
            self._artifact_from_mapping(item)
            for item in list(result_payload.get("artifacts") or [])
            if isinstance(item, Mapping)
        ]
        return ClaudeQueryEngineResult(
            ok=ok,
            event_records=list(self._host_events),
            artifacts=self._dedupe_artifacts([*self._host_artifacts, *result_artifacts]),
            step_summaries=[
                str(item) for item in list(result_payload.get("stepSummaries") or [])
            ],
            turn_count=int(result_payload.get("turnCount") or 0),
            tool_call_count=int(result_payload.get("toolCallCount") or 0),
            context_compaction_count=int(
                result_payload.get("contextCompactionCount") or 0
            ),
            stopped_reason=stopped_reason,
            session_snapshot=session_snapshot,
            metadata=metadata,
        )

    def _checkpoint_path(self, session_id: str) -> Path:
        identity = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        root = Path(self.context.artifact_store.root).resolve() / ".runtime-checkpoints"
        root.mkdir(parents=True, exist_ok=True)
        return root / f"typescript-e01-{identity}.json"

    def _load_incremental_checkpoint(self, session_id: str) -> dict[str, Any]:
        path = self._checkpoint_path(session_id)
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise TypeScriptRuntimeError(
                "typescript_runtime_checkpoint_corrupt",
                f"Cannot restore the durable TypeScript checkpoint: {error}",
            ) from error
        if not isinstance(value, dict) or str(value.get("session_id") or "") != session_id:
            raise TypeScriptRuntimeError(
                "typescript_runtime_checkpoint_identity",
                "Durable TypeScript checkpoint does not match the logical session.",
            )
        return value

    def _persist_incremental_checkpoint(
        self,
        session_id: str,
        checkpoint: Mapping[str, Any],
    ) -> None:
        path = self._checkpoint_path(session_id)
        staged = path.with_suffix(path.suffix + ".tmp")
        payload = dict(checkpoint)
        payload["session_id"] = session_id
        encoded = json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True)
        staged.write_text(encoded, encoding="utf-8")
        os.replace(staged, path)

    def _runtime_command(self) -> tuple[list[str], str]:
        if not self.entrypoint.exists():
            raise TypeScriptRuntimeError(
                "typescript_runtime_unavailable",
                f"TypeScript runtime entrypoint is missing: {self.entrypoint}",
            )
        configured_bun = str(os.environ.get("ZYRA_BUN_EXECUTABLE") or "").strip()
        local_bun = self.project_root / "node_modules" / "bun" / "bin" / (
            "bun.exe" if os.name == "nt" else "bun"
        )
        bun = configured_bun or shutil.which("bun") or (
            str(local_bun) if local_bun.is_file() else ""
        )
        if bun and Path(bun).is_file():
            return [bun, str(self.entrypoint), "--stdio"], "bun"
        raise TypeScriptRuntimeError(
            "typescript_runtime_unavailable",
            "Bun 1.2.15 is required for the canonical TypeScript runtime; "
            "set ZYRA_BUN_EXECUTABLE or install the locked project dependency.",
        )

    def _runtime_environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment.pop("NODE_PATH", None)
        environment["ZYRA_TYPESCRIPT_RUNTIME_OWNER"] = "canonical"
        environment["ZYRA_TYPESCRIPT_RUNTIME_PROTOCOL"] = RUNTIME_PROTOCOL_VERSION
        return environment

    def _typescript_config(
        self,
        permission_runtime: ToolPermissionRuntime,
    ) -> dict[str, Any]:
        runtime_constraints = dict(self.config.runtime_constraints)
        runtime_constraints.setdefault("workspaceRoot", str(self.context.workspace_root))
        runtime_constraints.setdefault("projectRoot", str(self.project_root))
        return {
            "maxTurns": self.config.max_turns,
            "maxToolResultChars": self.config.max_tool_result_chars,
            "maxTurnToolResultChars": self.config.max_turn_tool_result_chars,
            "maxQueryContextChars": self.config.max_query_context_chars,
            "continueOnError": self.config.continue_on_error,
            "maxReadOnlyConcurrency": self.config.max_read_only_concurrency,
            "emitToolUseSummaries": self.config.emit_tool_use_summaries,
            "allowEmptyTurns": self.config.allow_empty_turns,
            "modelName": self.config.model_name,
            "runtimeConstraints": to_jsonable(runtime_constraints),
            "controlCommands": to_jsonable(list(self.config.control_commands)),
            "permissionPolicy": to_jsonable(
                permission_runtime.typescript_policy_snapshot()
            ),
        }

    def _permission_runtime(
        self,
        *,
        session_id: str,
        restored_state: Mapping[str, Any],
    ) -> ToolPermissionRuntime:
        state_path = Path(
            self.config.permission_state_path
            or self.project_root / ".zyra-runtime" / "permission-state.json"
        )
        state_path.parent.mkdir(parents=True, exist_ok=True)
        restored_permission = restored_state.get("permission_runtime")
        restored_snapshot = (
            dict(restored_permission)
            if isinstance(restored_permission, Mapping) and not state_path.exists()
            else None
        )
        seed = dict(self.config.session_seed or {})
        foundation_metadata = dict(self.config.session_foundation_metadata or {})
        custody_fingerprint = str(
            seed.get("custody_fingerprint")
            or foundation_metadata.get("custody_fingerprint")
            or foundation_metadata.get("permission_session_custody_fingerprint")
            or ""
        )
        return ToolPermissionRuntime.for_session(
            session_id=session_id,
            state_path=state_path,
            config=PermissionRuntimeConfig(
                mode=self.config.permission_mode,
                approval_ttl_seconds=self.config.permission_approval_ttl_seconds,
                execution_grant_ttl_seconds=self.config.permission_execution_grant_ttl_seconds,
                bypass_available=self.config.permission_bypass_available,
                auto_available=self.config.permission_auto_available,
                interactive=self.config.permission_interactive,
                headless=self.config.permission_headless,
                disabled=self.config.disable_tool_permission_runtime,
                disable_rule_store=self.config.disable_permission_rule_store,
                disable_request_queue=self.config.disable_permission_request_queue,
                disable_decision_log=self.config.disable_permission_decision_log,
            ),
            restored_snapshot=restored_snapshot,
            workspace_root=self.context.workspace_root,
            custody_fingerprint=custody_fingerprint,
        )

    def _execute_tool_request(
        self,
        *,
        payload: Mapping[str, Any],
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        session_id: str,
        executor: ToolExecutor,
        permission_runtime: ToolPermissionRuntime,
        permission_continuation: Any,
        continuation_payloads: dict[str, dict[str, Any]],
        continuation_sequence: list[int],
        pending_typescript_settlements: dict[str, Any],
        tool_effect_receipts: dict[str, dict[str, Any]],
        receipt_lock: threading.RLock,
    ) -> dict[str, Any]:
        tool_name = str(payload.get("tool_name") or "")
        tool_call_id = str(payload.get("tool_call_id") or "")
        arguments = dict(payload.get("arguments") or {})
        raw_metadata = dict(payload.get("metadata") or {})
        metadata = {str(key): str(value) for key, value in raw_metadata.items()}
        decision_payload = dict(payload.get("permission_decision") or {})
        permission_only = bool(payload.get("permission_only"))
        execution_owner = str(payload.get("execution_owner") or "python-tool-executor")
        request_digest = hashlib.sha256(
            json.dumps(
                {"tool_name": tool_name, "arguments": arguments},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with receipt_lock:
            cached = tool_effect_receipts.get(tool_call_id)
            if cached is not None:
                if str(cached.get("request_digest") or "") != request_digest:
                    return {
                        "tool_call_id": tool_call_id,
                        "ok": False,
                        "summary": "Stable tool call id was reused with a different payload",
                        "output": {},
                        "artifacts": [],
                        "error": "tool_effect_identity_conflict",
                        "metadata": {"effect_replay_fenced": "true"},
                    }
                replayed = dict(cached.get("result") or {})
                replayed_metadata = dict(replayed.get("metadata") or {})
                replayed_metadata["effect_replay_fenced"] = "true"
                replayed_metadata["effect_replayed"] = "false"
                replayed["metadata"] = replayed_metadata
                return replayed
        spec = self.context.registry.get(tool_name)
        if spec is None and not (permission_only and execution_owner.startswith("typescript-")):
            return to_jsonable(
                ToolResult(
                    tool_call_id=tool_call_id,
                    ok=False,
                    summary=f"Unknown tool: {tool_name}",
                    error="unknown_tool",
                    metadata={"canonical_request_owner": "typescript"},
                )
            )
        binding = dict(decision_payload.get("request_binding") or {})
        namespace = str(binding.get("namespace") or "builtin")
        server_id = str(binding.get("server_id") or "")
        version = str(binding.get("version") or "")
        schema_digest = str(binding.get("schema_digest") or "")
        metadata.update(
            {
                "tool_namespace": namespace,
                "namespace": namespace,
                "server_id": server_id,
                "server_name": server_id,
                "tool_version": version,
                "canonical_request_owner": "typescript",
                "canonical_permission_owner": "typescript",
                "execution_owner": execution_owner,
            }
        )
        call = ToolCall(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            tool_name=tool_name,
            arguments=arguments,
            tool_call_id=tool_call_id,
            metadata=metadata,
        )
        permission_request = PermissionEvaluationRequest(
            run_id=run_id,
            task_id=task_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            turn_id=str(payload.get("turn_index") or ""),
            node_id=node_id,
            tool_use_id=tool_call_id,
            tool_identity=ToolIdentity(
                namespace=namespace,
                name=tool_name,
                server_id=server_id,
                version=version,
                schema_digest=schema_digest,
            ),
            arguments=arguments,
            operation=(
                str(spec.metadata.get("access_mode") or "execute")
                if spec is not None
                else str(dict(decision_payload.get("metadata") or {}).get("operation") or "execute")
            ),
            mode=self._permission_mode(self.config.permission_mode),
            workspace_root=str(self.context.workspace_root),
            interactive=self.config.permission_interactive,
            headless=self.config.permission_headless,
            requires_interaction=tool_name in {"shell", "browser"},
            attributes={
                "tool_source": spec.source if spec is not None else execution_owner,
                "tool_metadata": dict(spec.metadata) if spec is not None else {},
                "canonical_request_owner": "typescript",
            },
            metadata={
                "batch_id": str(payload.get("batch_id") or ""),
                "execution_mode": str(payload.get("execution_mode") or ""),
            },
        )
        continuation_fence = self._permission_continuation_fence(
            permission_continuation,
            permission_request,
        )
        if continuation_fence:
            fenced_record = next(
                (
                    record
                    for record in permission_continuation.records()
                    if record.tool_use_id == permission_request.tool_use_id
                    or (
                        record.tool_identity.namespace
                        == permission_request.tool_identity.namespace
                        and record.tool_identity.name
                        == permission_request.tool_identity.name
                        and record.arguments_digest
                        == permission_request.arguments_digest
                    )
                ),
                None,
            )
            if fenced_record is not None:
                self._host_events.append(
                    self._permission_continuation_event(
                        continuation_fence,
                        run_id=run_id,
                        task_id=task_id,
                        node_id=node_id,
                        worker_request_id=worker_request_id,
                        record=fenced_record,
                    )
                )
            return to_jsonable(
                ToolResult(
                    tool_call_id=tool_call_id,
                    ok=False,
                    summary=(
                        "A consumed approval has an unknown execution outcome"
                        if continuation_fence == "permission_continuation_outcome_unknown"
                        else (
                            "The exact permission continuation replay was rejected"
                            if continuation_fence
                            == "permission_continuation_replay_rejected"
                            else "The exact permission continuation was denied"
                        )
                    ),
                    error=continuation_fence,
                    metadata={
                        "permission_effect": "deny",
                        "canonical_permission_owner": "typescript",
                        "continuation_fence": "true",
                    },
                )
            )
        continuation_claim = None
        approval = permission_runtime._approved_request_for(permission_request)
        if approval is not None:
            try:
                continuation_claim = self._claim_permission_continuation(
                    permission_continuation,
                    continuation_payloads,
                    approval.request_id,
                    worker_request_id=worker_request_id,
                )
            except Exception as exc:
                from zyra_runtime.permission.continuation import (
                    PermissionContinuationAlreadyClaimed,
                )

                error = (
                    "permission_continuation_claim_in_progress"
                    if isinstance(exc, PermissionContinuationAlreadyClaimed)
                    else "permission_continuation_replay_rejected"
                )
                return to_jsonable(
                    ToolResult(
                        tool_call_id=tool_call_id,
                        ok=False,
                        summary=f"Permission continuation claim rejected: {exc}",
                        error=error,
                        metadata={
                            "permission_effect": "deny",
                            "canonical_permission_owner": "typescript",
                            "continuation_fence": "true",
                        },
                    )
                )
            self._host_events.append(
                self._permission_continuation_event(
                    "permission_continuation_claimed",
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    worker_request_id=worker_request_id,
                    record=continuation_claim.record,
                )
            )
        try:
            receipt = permission_runtime.commit_typescript_decision(
                permission_request,
                decision_payload,
            )
        except Exception as exc:
            return to_jsonable(
                ToolResult(
                    tool_call_id=tool_call_id,
                    ok=False,
                    summary=f"TypeScript permission receipt rejected: {exc}",
                    error="permission_binding_rejected",
                    metadata={
                        "permission_effect": "deny",
                        "canonical_permission_owner": "typescript",
                        "python_policy_fallback": "false",
                    },
                )
            )
        self._host_events.extend(receipt.events)
        if receipt.execution_grant is None:
            effect = str(receipt.decision.effect)
            result = ToolResult(
                tool_call_id=tool_call_id,
                ok=False,
                summary=str(receipt.decision.reason),
                output={
                    "pending_request": (
                        receipt.pending_request.to_dict()
                        if receipt.pending_request is not None
                        else None
                    )
                },
                error=(
                    "permission_approval_required"
                    if effect == "ask"
                    else "permission_denied"
                ),
                metadata={
                    "permission_effect": effect,
                    "permission_decision_id": receipt.decision.decision_id,
                    "permission_reason_code": receipt.decision.reason_code,
                    "permission_abort_loop": str(receipt.abort_loop).lower(),
                    "human_intervention_count": str(
                        receipt.decision.metadata.get("human_intervention_count", 0)
                    ),
                    "raw_approved_argument_ignored": str(
                        "approved" in arguments
                    ).lower(),
                    "canonical_request_owner": "typescript",
                    "canonical_permission_owner": "typescript",
                    "python_policy_fallback": "false",
                },
            )
            if receipt.pending_request is not None:
                parked = self._park_permission_continuation(
                    permission_continuation,
                    continuation_payloads,
                    receipt.pending_request,
                    arguments,
                    turn_id=str(payload.get("turn_index") or ""),
                    batch_index=int(payload.get("batch_index") or 0),
                    step_index=int(payload.get("step_index") or 0),
                    tool_name=tool_name,
                    continuation_sequence=continuation_sequence,
                )
                self._host_events.append(
                    self._permission_continuation_event(
                        "permission_continuation_parked",
                        run_id=run_id,
                        task_id=task_id,
                        node_id=node_id,
                        worker_request_id=worker_request_id,
                        record=parked,
                    )
                )
            return to_jsonable(result)
        if permission_only:
            accepted = permission_runtime.validate_and_consume(
                call,
                receipt.execution_grant,
                self.context,
            )
            self._host_events.extend(
                permission_runtime.drain_execution_events(tool_call_id)
            )
            if accepted:
                pending_typescript_settlements[tool_call_id] = continuation_claim
            elif continuation_claim is not None:
                terminal = self._complete_permission_continuation(
                    permission_continuation,
                    continuation_payloads,
                    continuation_claim,
                    ok=accepted,
                    failure_code="permission_grant_rejected",
                )
                self._host_events.append(
                    self._permission_continuation_event(
                        "permission_continuation_finished"
                        if accepted
                        else "permission_continuation_failed",
                        run_id=run_id,
                        task_id=task_id,
                        node_id=node_id,
                        worker_request_id=worker_request_id,
                        record=terminal,
                    )
                )
                if terminal.payload_locator in continuation_payloads:
                    self._host_events.append(
                        self._permission_continuation_event(
                            "permission_continuation_cleanup_deferred",
                            run_id=run_id,
                            task_id=task_id,
                            node_id=node_id,
                            worker_request_id=worker_request_id,
                            record=terminal,
                        )
                    )
            return to_jsonable(
                ToolResult(
                    tool_call_id=tool_call_id,
                    ok=accepted,
                    summary=(
                        "TypeScript capability permission committed"
                        if accepted
                        else "TypeScript capability grant consumption failed"
                    ),
                    output={
                        "permission_committed": accepted,
                        "execution_owner": execution_owner,
                    },
                    error=None if accepted else "permission_grant_rejected",
                    metadata={
                        "permission_effect": "allow" if accepted else "deny",
                        "permission_decision_id": receipt.decision.decision_id,
                        "permission_commit_only": "true",
                        "canonical_permission_owner": "typescript",
                        "canonical_capability_owner": execution_owner,
                        "python_policy_fallback": "false",
                    },
                )
            )
        result = executor.execute(call, permission_grant=receipt.execution_grant)
        encoded_result = to_jsonable(result)
        with receipt_lock:
            tool_effect_receipts[tool_call_id] = {
                "request_digest": request_digest,
                "result": encoded_result,
            }
            checkpoint = dict(self._latest_runtime_checkpoint)
            checkpoint["tool_effect_receipts"] = to_jsonable(tool_effect_receipts)
            self._persist_incremental_checkpoint(session_id, checkpoint)
        self._host_events.extend(permission_runtime.drain_execution_events(tool_call_id))
        if (
            continuation_claim is not None
            and result.ok
            and bool(
                self.config.runtime_constraints.get(
                    "simulate_typescript_host_loss_after_tool_side_effect"
                )
            )
        ):
            record = continuation_claim.record
            terminal = permission_continuation.fail(
                record.request_id,
                claim_id=record.claim_id,
                failure_code="approval_consumed_outcome_unknown",
                expected_record_revision=record.revision,
                metadata={
                    "canonical_policy_owner": "typescript",
                    "execution_outcome_unknown": True,
                    "permission_guard_reentered": True,
                    "execution_grant_required": True,
                    "failure_injection": "typescript_host_loss_after_side_effect",
                },
            )
            self._host_events.append(
                self._permission_continuation_event(
                    "permission_continuation_outcome_unknown",
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    worker_request_id=worker_request_id,
                    record=terminal,
                )
            )
            return to_jsonable(
                ToolResult(
                    tool_call_id=tool_call_id,
                    ok=False,
                    summary="Tool side effect completed but the TypeScript host receipt was lost",
                    artifacts=result.artifacts,
                    error="permission_continuation_execution_ambiguous",
                    metadata={
                        "permission_effect": "allow",
                        "canonical_permission_owner": "typescript",
                        "execution_outcome_unknown": "true",
                    },
                )
            )
        if continuation_claim is not None:
            terminal = self._complete_permission_continuation(
                permission_continuation,
                continuation_payloads,
                continuation_claim,
                ok=bool(result.ok),
                failure_code=str(result.error or "tool_execution_failed"),
            )
            self._host_events.append(
                self._permission_continuation_event(
                    "permission_continuation_finished"
                    if result.ok
                    else "permission_continuation_failed",
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    worker_request_id=worker_request_id,
                    record=terminal,
                )
            )
            if terminal.payload_locator in continuation_payloads:
                self._host_events.append(
                    self._permission_continuation_event(
                        "permission_continuation_cleanup_deferred",
                        run_id=run_id,
                        task_id=task_id,
                        node_id=node_id,
                        worker_request_id=worker_request_id,
                        record=terminal,
                    )
                )
        self._host_artifacts.extend(result.artifacts)
        return encoded_result

    def _settle_typescript_capability(
        self,
        *,
        payload: Mapping[str, Any],
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        permission_continuation: Any,
        continuation_payloads: dict[str, dict[str, Any]],
        pending_settlements: dict[str, Any],
    ) -> dict[str, Any]:
        tool_call_id = str(payload.get("tool_call_id") or "")
        if not tool_call_id or tool_call_id not in pending_settlements:
            return {
                "accepted": False,
                "tool_call_id": tool_call_id,
                "error": "unknown_or_duplicate_capability_settlement",
            }
        continuation_claim = pending_settlements.pop(tool_call_id)
        ok = payload.get("ok") is True
        settled = continuation_claim is not None
        if continuation_claim is not None:
            terminal = self._complete_permission_continuation(
                permission_continuation,
                continuation_payloads,
                continuation_claim,
                ok=ok,
                failure_code=str(
                    payload.get("error") or "typescript_capability_execution_failed"
                ),
            )
            self._host_events.append(
                self._permission_continuation_event(
                    "permission_continuation_finished"
                    if ok
                    else "permission_continuation_failed",
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    worker_request_id=worker_request_id,
                    record=terminal,
                )
            )
            if terminal.payload_locator in continuation_payloads:
                self._host_events.append(
                    self._permission_continuation_event(
                        "permission_continuation_cleanup_deferred",
                        run_id=run_id,
                        task_id=task_id,
                        node_id=node_id,
                        worker_request_id=worker_request_id,
                        record=terminal,
                    )
                )
        return {
            "accepted": True,
            "tool_call_id": tool_call_id,
            "settled": settled,
            "ok": ok,
            "canonical_capability_owner": "typescript",
        }

    def _park_permission_continuation(
        self,
        continuation: Any,
        payloads: dict[str, dict[str, Any]],
        pending: Any,
        arguments: Mapping[str, Any],
        *,
        turn_id: str,
        batch_index: int,
        step_index: int,
        tool_name: str,
        continuation_sequence: list[int],
    ) -> Any:
        from zyra_runtime.permission.canonical import (
            arguments_digest,
            canonical_arguments_json,
        )
        from zyra_runtime.permission.continuation import PermissionContinuationReplay

        locator = "query-session:" + arguments_digest(
            {
                "request_id": pending.request_id,
                "session_id": pending.session_id,
                "tool_use_id": pending.tool_use_id,
                "request_fingerprint": pending.request_fingerprint,
            }
        )
        try:
            existing = continuation.get(pending.request_id)
        except KeyError:
            existing = None
        if existing is None:
            continuation_sequence[0] += 1
            sequence = continuation_sequence[0]
        else:
            sequence = existing.session_sequence
            if existing.payload_locator != locator:
                raise RuntimeError("permission continuation locator mismatch")
        replay_payload = {
            "session_id": pending.session_id,
            "task_id": pending.task_id,
            "run_id": pending.run_id,
            "tool_use_id": pending.tool_use_id,
            "tool_identity": pending.tool_identity.to_dict(),
            "arguments": to_jsonable(dict(arguments)),
            "arguments_digest": pending.arguments_digest,
            "request_fingerprint": pending.request_fingerprint,
            "scope": pending.scope.to_dict(),
            "payload_locator": locator,
            "session_sequence": sequence,
            "source": "CodeWorkerSessionStore.runtime_state",
            "metadata": {
                "request_id": pending.request_id,
                "permission_guard_required": True,
                "canonical_policy_owner": "typescript",
            },
        }
        PermissionContinuationReplay.from_value(replay_payload, source="typescript_runtime")
        current = payloads.get(locator)
        if current is not None:
            if canonical_arguments_json(current) != canonical_arguments_json(replay_payload):
                raise RuntimeError("permission continuation payload collision")
        else:
            if self.config.permission_continuation_payload_writer is not None:
                self.config.permission_continuation_payload_writer(
                    pending.request_id,
                    locator,
                    replay_payload,
                )
            payloads[locator] = replay_payload
        if existing is None:
            existing = continuation.park(
                pending,
                payload_locator=locator,
                session_sequence=sequence,
                metadata={
                    "turn_id": turn_id,
                    "batch_index": batch_index,
                    "step_index": step_index,
                    "tool_name": tool_name,
                    "payload_owner": "CodeWorkerSessionStore.permission_continuation_wal",
                    "payload_write_ahead": self.config.permission_continuation_payload_writer
                    is not None,
                    "raw_arguments_persisted_in_permission_state": False,
                    "canonical_policy_owner": "typescript",
                },
            )
        return existing

    @staticmethod
    def _permission_continuation_fence(
        continuation: Any,
        request: PermissionEvaluationRequest,
    ) -> str:
        for record in continuation.records():
            if (
                record.tool_use_id == request.tool_use_id
                and record.request_fingerprint != request.request_fingerprint
                and str(record.resolution_effect or "") == "allow"
            ):
                return "permission_continuation_replay_rejected"
            same_action = (
                record.tool_identity.namespace == request.tool_identity.namespace
                and record.tool_identity.name == request.tool_identity.name
                and record.tool_identity.server_id == request.tool_identity.server_id
                and record.arguments_digest == request.arguments_digest
            )
            if not same_action:
                continue
            if record.failure_code == "approval_consumed_outcome_unknown":
                return "permission_continuation_outcome_unknown"
            if str(record.resolution_effect or "") == "deny":
                return "permission_denied"
        return ""

    @staticmethod
    def _claim_permission_continuation(
        continuation: Any,
        payloads: Mapping[str, Mapping[str, Any]],
        request_id: str,
        *,
        worker_request_id: str,
    ) -> Any:
        record = continuation.get(request_id)
        replay = payloads.get(record.payload_locator)
        if replay is None:
            raise RuntimeError("permission continuation replay payload is missing")
        return continuation.prepare_resume(
            request_id,
            replay,
            claimant=worker_request_id,
            idempotency_key=f"typescript:{worker_request_id}:{record.tool_use_id}",
            expected_record_revision=record.revision,
            payload_resolver=lambda _: replay,
        )

    def _complete_permission_continuation(
        self,
        continuation: Any,
        payloads: dict[str, dict[str, Any]],
        claim: Any,
        *,
        ok: bool,
        failure_code: str,
    ) -> Any:
        record = claim.record
        if ok:
            terminal = continuation.complete(
                record.request_id,
                claim_id=record.claim_id,
                expected_record_revision=record.revision,
                metadata={
                    "canonical_policy_owner": "typescript",
                    "execution_outcome_unknown": False,
                    "permission_guard_reentered": True,
                    "execution_grant_required": True,
                },
            )
        else:
            terminal = continuation.fail(
                record.request_id,
                claim_id=record.claim_id,
                failure_code=failure_code,
                expected_record_revision=record.revision,
                metadata={
                    "canonical_policy_owner": "typescript",
                    "execution_outcome_unknown": False,
                    "permission_guard_reentered": True,
                    "execution_grant_required": True,
                },
            )
        if self.config.permission_continuation_payload_tombstoner is not None:
            try:
                self.config.permission_continuation_payload_tombstoner(
                    terminal.request_id,
                    terminal.payload_locator,
                    "permission_execution_completed" if ok else failure_code,
                )
            except Exception:
                return terminal
        payloads.pop(terminal.payload_locator, None)
        return terminal

    @staticmethod
    def _permission_continuation_event(
        phase: str,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        record: Any,
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "phase": phase,
                    "canonical_owner": "typescript",
                    "session_id": record.session_id,
                    "worker_request_id": worker_request_id,
                    "request_id": record.request_id,
                    "continuation_id": record.continuation_id,
                    "continuation_revision": record.revision,
                    "continuation_phase": str(record.phase),
                    "tool_call_id": record.tool_use_id,
                    "payload_locator": record.payload_locator,
                    "session_sequence": record.session_sequence,
                }
            },
        )

    def _workspace_safety_state(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        raw_path = arguments.get("path") or arguments.get("file_path")
        if raw_path is None:
            local = tool_name not in {"shell", "browser", "web_search"}
            return {
                "workspace_root": str(self.context.workspace_root),
                "workspace_scoped": local,
                "path_validated": local,
                "read_before_write": True,
                "baseline_current": True,
                "bounded_change": True,
                "external_egress": not local,
            }
        root = Path(self.context.workspace_root).resolve()
        candidate = Path(str(raw_path))
        target = (
            (root / candidate).resolve()
            if not candidate.is_absolute()
            else candidate.resolve()
        )
        try:
            target.relative_to(root)
            inside = True
        except ValueError:
            inside = False
        try:
            target.stat()
            baseline_current = True
        except FileNotFoundError:
            baseline_current = True
        except OSError:
            baseline_current = False
        return {
            "workspace_root": str(root),
            "workspace_scoped": inside,
            "path_validated": inside,
            "read_before_write": inside,
            "baseline_current": baseline_current,
            "bounded_change": inside,
            "cross_repository": not inside,
            "explicit_path_unsafe": not inside,
        }

    def _write_artifact(
        self,
        *,
        payload: Mapping[str, Any],
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> Any:
        kind_value = str(payload.get("kind") or "structured_data")
        try:
            kind = ArtifactKind(kind_value)
        except ValueError:
            kind = ArtifactKind.STRUCTURED_DATA
        artifact = self.context.artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content=str(payload.get("content") or ""),
            title=str(payload.get("title") or "TypeScript runtime artifact"),
            kind=kind,
            extension=str(payload.get("extension") or ".txt"),
            producer_node_id=node_id,
        )
        self._host_artifacts.append(artifact)
        return artifact

    def _event_record(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        parent_session_id: str,
        payload: Mapping[str, Any],
    ) -> EventRecord:
        phase = str(payload.get("phase") or "runtime_event")
        payload_session_id = str(payload.get("session_id") or "")
        is_child_event = bool(
            payload_session_id and payload_session_id != parent_session_id
        )
        session_domain = (
            "agent_child_query_session" if is_child_event else "query_session"
        )
        event_payload: dict[str, Any] = {
            session_domain: dict(payload),
            "typescript_runtime": {
                "runtime_id": TYPESCRIPT_RUNTIME_ID,
                "protocol": RUNTIME_PROTOCOL_VERSION,
                "canonical_owner": "typescript",
                "phase": phase,
                "scope": "agent_child" if is_child_event else "parent_session",
                "parent_session_id": parent_session_id,
                "effective_session_id": payload_session_id or parent_session_id,
            },
        }
        tool_result = payload.get("tool_result")
        if isinstance(tool_result, Mapping):
            event_payload[
                "agent_child_tool_result" if is_child_event else "tool_result"
            ] = dict(tool_result)
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=(
                EventType.BUDGET_UPDATED
                if phase in {"tool_result_budget_exceeded", "context_compacted"}
                else EventType.AGENT_MESSAGE
            ),
            payload=event_payload,
        )

    def _project_event(
        self,
        projection: QuerySession,
        payload: Mapping[str, Any],
    ) -> None:
        phase = str(payload.get("phase") or "")
        metadata = {
            **dict(payload),
            "canonical_owner": "typescript",
            "python_projection_canonical": False,
        }
        if phase == "turn_started":
            if projection.active_turn is None:
                projection.start_turn(
                    int(payload.get("turn_index") or 0),
                    user_content=str(payload.get("user_content") or ""),
                    metadata=metadata,
                )
            return
        if phase == "stream_request_start":
            projection.start_stream_request(metadata=metadata)
            return
        if phase in {
            "tool_batch_started",
            "tool_batch_completed",
            "tool_use_summary",
        }:
            projection.record_batch_event(QueryStreamEventType(phase), metadata=metadata)
            return
        if phase == "tool_call_started":
            projection.record_tool_call(
                tool_call_id=str(payload.get("tool_call_id") or ""),
                tool_name=str(payload.get("tool_name") or ""),
                metadata=metadata,
            )
            return
        if phase == "tool_call_completed":
            result = dict(payload.get("tool_result") or {})
            projection.record_tool_result(
                tool_call_id=str(result.get("tool_call_id") or payload.get("tool_call_id") or ""),
                tool_name=str(payload.get("tool_name") or ""),
                summary=str(result.get("summary") or ""),
                ok=result.get("ok") is True,
                error=str(result.get("error") or "") or None,
                artifacts=[
                    str(item.get("artifact_id") or "")
                    for item in list(result.get("artifacts") or [])
                    if isinstance(item, Mapping) and item.get("artifact_id")
                ],
                metadata=metadata,
            )
            return
        if phase == "context_compacted":
            projection.record_context_compaction(
                artifact_id=str(payload.get("artifact_id") or ""),
                metadata=metadata,
            )
            return
        if phase == "turn_completed" and projection.active_turn is not None:
            ok = payload.get("ok") is True
            projection.end_turn(
                ok=ok,
                stop_reason=StopReason.END_TURN if ok else StopReason.TOOL_ERROR,
                error=str(payload.get("error") or "") or None,
                metadata=metadata,
            )

    def _failed_result(
        self,
        *,
        error: TypeScriptRuntimeError,
        run_id: str,
        task_id: str,
        node_id: str | None,
        session_id: str,
        projection: QuerySession,
    ) -> ClaudeQueryEngineResult:
        if projection.active_turn is not None:
            projection.end_turn(
                ok=False,
                stop_reason=StopReason.STREAM_ERROR,
                error=error.code,
                metadata={"canonical_owner": "typescript"},
            )
        projection.complete_session(
            ok=False,
            stop_reason=StopReason.STREAM_ERROR,
            metadata={
                "canonical_owner": "typescript",
                "typescript_runtime_error": error.code,
            },
        )
        event = EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.NODE_FAILED,
            payload={
                "query_session": {
                    "phase": "session_failed",
                    "session_id": session_id,
                    "canonical_owner": "typescript",
                    "runtime_id": TYPESCRIPT_RUNTIME_ID,
                    "error": error.code,
                    "message": str(error),
                },
                "typescript_runtime": {
                    "runtime_id": TYPESCRIPT_RUNTIME_ID,
                    "protocol": RUNTIME_PROTOCOL_VERSION,
                    "canonical_owner": "typescript",
                    "fallback_used": False,
                },
            },
        )
        failed_snapshot = to_jsonable(projection.snapshot())
        if self._latest_runtime_checkpoint:
            failed_snapshot["typescript_runtime_snapshot"] = to_jsonable(
                self._latest_runtime_checkpoint
            )
        failed_snapshot["tool_effect_receipts"] = to_jsonable(
            self._tool_effect_receipts
        )
        return ClaudeQueryEngineResult(
            ok=False,
            event_records=[*self._host_events, event],
            artifacts=self._dedupe_artifacts(self._host_artifacts),
            step_summaries=[],
            turn_count=0,
            tool_call_count=0,
            context_compaction_count=0,
            stopped_reason=error.code,
            session_snapshot=failed_snapshot,
            metadata={
                "loop": "zyra_typescript_query_engine_runtime",
                "canonical_runtime_owner": "typescript",
                "typescript_runtime_id": TYPESCRIPT_RUNTIME_ID,
                "runtime_protocol": RUNTIME_PROTOCOL_VERSION,
                "python_query_engine_fallback": "false",
                "typescript_runtime_error": error.code,
                "query_session_id": session_id,
            },
        )

    def _read_frame(
        self,
        reader: _ProcessLineReader,
        process: subprocess.Popen[str],
        *,
        run_id: str,
        expected_sequence: int,
        deadline: float,
    ) -> dict[str, Any]:
        line = reader.get(deadline - time.monotonic())
        if line is None:
            stderr = process.stderr.read().strip() if process.stderr is not None else ""
            raise TypeScriptRuntimeError(
                "typescript_runtime_process_failed",
                stderr or "TypeScript runtime closed stdout before completing the run.",
            )
        try:
            frame = json.loads(line)
        except json.JSONDecodeError as error:
            self._terminate(process)
            raise TypeScriptRuntimeError(
                "typescript_runtime_protocol_error",
                "TypeScript runtime emitted non-JSON output.",
            ) from error
        if not isinstance(frame, dict):
            raise TypeScriptRuntimeError(
                "typescript_runtime_protocol_error",
                "TypeScript runtime frame must be an object.",
            )
        if frame.get("protocol") != RUNTIME_PROTOCOL_VERSION:
            raise TypeScriptRuntimeError(
                "typescript_runtime_protocol_error",
                "TypeScript runtime protocol version mismatch.",
            )
        if frame.get("run_id") != run_id:
            raise TypeScriptRuntimeError(
                "typescript_runtime_protocol_error",
                "TypeScript runtime frame run_id mismatch.",
            )
        sequence = frame.get("sequence")
        if sequence != expected_sequence:
            reason = (
                "duplicate"
                if isinstance(sequence, int) and sequence < expected_sequence
                else "out_of_order"
            )
            raise TypeScriptRuntimeError(
                "typescript_runtime_protocol_error",
                f"TypeScript runtime {reason} frame: expected {expected_sequence}, got {sequence}.",
            )
        if not isinstance(frame.get("payload"), dict):
            raise TypeScriptRuntimeError(
                "typescript_runtime_protocol_error",
                "TypeScript runtime frame payload must be an object.",
            )
        return frame

    @staticmethod
    def _write_frame(
        process: subprocess.Popen[str],
        *,
        run_id: str,
        sequence: int,
        kind: str,
        payload: Mapping[str, Any],
        correlation_id: str = "",
    ) -> None:
        if process.stdin is None or process.poll() is not None:
            stderr = process.stderr.read().strip() if process.stderr is not None else ""
            raise TypeScriptRuntimeError(
                "typescript_runtime_process_failed",
                stderr or "TypeScript runtime process is not writable.",
            )
        frame = {
            "protocol": RUNTIME_PROTOCOL_VERSION,
            "run_id": run_id,
            "sequence": sequence,
            "kind": kind,
            "correlation_id": correlation_id,
            "payload": to_jsonable(dict(payload)),
        }
        try:
            process.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            stderr = process.stderr.read().strip() if process.stderr is not None else ""
            raise TypeScriptRuntimeError(
                "typescript_runtime_process_failed",
                stderr or "TypeScript runtime process disconnected while receiving a frame.",
            ) from error

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def _session_id(
        self,
        run_id: str,
        request_metadata: Mapping[str, Any] | None,
    ) -> str:
        seed = dict(self.config.session_seed or {})
        metadata = dict(request_metadata or {})
        return str(
            seed.get("session_id")
            or metadata.get("query_session_id")
            or f"codesession_{run_id}"
        )

    @staticmethod
    def _permission_mode(value: str) -> PermissionMode:
        aliases = {
            "acceptEdits": PermissionMode.ACCEPT_EDITS,
            "accept_edits": PermissionMode.ACCEPT_EDITS,
            "dontAsk": PermissionMode.DONT_ASK,
            "dont_ask": PermissionMode.DONT_ASK,
            "bypassPermissions": PermissionMode.BYPASS,
            "bypass": PermissionMode.BYPASS,
        }
        if value in aliases:
            return aliases[value]
        try:
            return PermissionMode(value)
        except ValueError:
            return PermissionMode.DEFAULT

    @staticmethod
    def _stop_reason(value: str | None, *, complete: bool = False) -> StopReason:
        if complete:
            return StopReason.SESSION_COMPLETED
        return {
            "max_turns_exceeded": StopReason.MAX_TURNS_EXCEEDED,
            "user_cancelled": StopReason.USER_CANCELLED,
            "model_error": StopReason.STREAM_ERROR,
            "typescript_runtime_timeout": StopReason.STREAM_ERROR,
            "typescript_runtime_process_failed": StopReason.STREAM_ERROR,
        }.get(str(value or ""), StopReason.TOOL_ERROR)

    @staticmethod
    def _artifact_from_mapping(value: Mapping[str, Any]) -> Any:
        from zyra_core import ArtifactRef

        try:
            kind = ArtifactKind(str(value.get("kind") or "file"))
        except ValueError:
            kind = ArtifactKind.FILE
        return ArtifactRef(
            artifact_id=str(value.get("artifact_id") or ""),
            kind=kind,
            uri=str(value.get("uri") or ""),
            title=str(value.get("title") or ""),
            producer_node_id=value.get("producer_node_id"),
            created_at=str(value.get("created_at") or ""),
            metadata=dict(value.get("metadata") or {}),
        )

    @staticmethod
    def _dedupe_artifacts(values: Sequence[Any]) -> list[Any]:
        selected: list[Any] = []
        seen: set[str] = set()
        for value in values:
            artifact_id = str(getattr(value, "artifact_id", "") or "")
            if not artifact_id or artifact_id in seen:
                continue
            seen.add(artifact_id)
            selected.append(value)
        return selected
