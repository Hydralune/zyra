from __future__ import annotations

import json
import hashlib
import os
import queue
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from zyra_core import ArtifactKind, EventRecord, EventType, to_jsonable
from zyra_runtime.query_session import QuerySession, QueryStreamEventType, StopReason
from zyra_runtime.typescript_runtime_host import (
    ClaudeQueryEngineConfig,
    ClaudeQueryEngineResult,
)
from zyra_runtime.executor import ToolExecutionContext, ToolExecutor
from zyra_runtime.e02_ports import (
    TypeScriptPermissionReceiptError,
    TypeScriptPermissionReceiptPort,
)
from zyra_runtime.permission.models import (
    PermissionEffect,
    PermissionMode,
    PermissionRequestPhase,
    PermissionRequestRecord,
    PermissionScope,
    PermissionScopeKind,
    ToolIdentity,
)
from zyra_runtime.permission.request_queue import PermissionRequestQueue
from zyra_runtime.permission.store import PermissionStateStore
from zyra_runtime.tools import ToolCall, ToolResult

from .code_worker_bridge import code_worker_entrypoint
from .subagents.typescript_port import TypeScriptAgentDurablePort


RUNTIME_PROTOCOL_VERSION = "zyra.claude-runtime.v1"
TYPESCRIPT_RUNTIME_ID = "zyra-typescript-claude-runtime"
_CHECKPOINT_LOCKS: dict[str, threading.RLock] = {}
_CHECKPOINT_LOCKS_GUARD = threading.RLock()


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
        self._checkpoint_revision = 0
        self._runtime_process_epoch = 0
        self._protocol_frame_trace: list[dict[str, Any]] = []
        self._checkpoint_writer_id = hashlib.sha256(
            f"{os.getpid()}:{id(self)}:{time.time_ns()}".encode("utf-8")
        ).hexdigest()

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
                    (self.config.disable_tool_permission_runtime, "E02CapabilityCoordinator"),
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
        raw_restored_runtime_state = self.config.restored_runtime_state or {}
        nested_session_snapshot = raw_restored_runtime_state.get("session_snapshot")
        restored_runtime_state = (
            dict(nested_session_snapshot)
            if isinstance(nested_session_snapshot, Mapping)
            else dict(raw_restored_runtime_state)
        )
        durable_checkpoint = self._load_incremental_checkpoint(session_id)
        provided_revision = int(
            restored_runtime_state.get("host_checkpoint_revision")
            or restored_runtime_state.get("revision")
            or dict(restored_runtime_state.get("typescript_runtime_snapshot") or {}).get("revision")
            or 0
        )
        durable_revision = int(
            durable_checkpoint.get("host_checkpoint_revision")
            or durable_checkpoint.get("revision")
            or 0
        )
        if durable_checkpoint and durable_revision >= provided_revision:
            restored_runtime_state.update(durable_checkpoint)
        if constraints.get("permission_transport_queue_enabled") is True:
            approval_responses = self._host_permission_responses(session_id)
            if approval_responses:
                e02_snapshot = self._find_e02_snapshot(restored_runtime_state)
                if not e02_snapshot:
                    raise TypeScriptRuntimeError(
                        "permission_transport_checkpoint_missing",
                        "A resolved approval has no matching durable TypeScript E02 checkpoint.",
                    )
                restored_runtime_state = {
                    "e02": e02_snapshot,
                    "tool_effect_receipts": to_jsonable(
                        restored_runtime_state.get("tool_effect_receipts") or {}
                    ),
                    "permission_transport_resume": True,
                }
        nested_typescript_snapshot = restored_runtime_state.get(
            "typescript_runtime_snapshot"
        )
        self._latest_runtime_checkpoint = dict(
            restored_runtime_state
            if "e02" in restored_runtime_state
            or "host_checkpoint_revision" in restored_runtime_state
            else nested_typescript_snapshot
            if isinstance(nested_typescript_snapshot, Mapping)
            else restored_runtime_state
        )
        self._runtime_process_epoch = int(
            self._latest_runtime_checkpoint.get("runtime_process_epoch") or 0
        )
        raw_protocol_trace = self._latest_runtime_checkpoint.get(
            "protocol_frame_trace"
        )
        self._protocol_frame_trace = [
            dict(item)
            for item in list(raw_protocol_trace or [])
            if isinstance(item, Mapping)
        ]
        self._tool_effect_receipts = {
            str(key): dict(value)
            for key, value in dict(restored_runtime_state.get("tool_effect_receipts") or {}).items()
            if isinstance(value, Mapping)
        }
        for legacy_key in (
            "permission_runtime",
            "permission_continuation",
            "permission_continuation_payloads",
        ):
            restored_runtime_state.pop(legacy_key, None)
        pending_typescript_settlements: dict[str, str] = {}
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
        receipt_port = TypeScriptPermissionReceiptPort(
            run_id=run_id,
            task_id=task_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            workspace_root=self.context.workspace_root,
        )
        raw_terminal_receipts = restored_runtime_state.get(
            "terminal_result_receipts"
        )
        terminal_receipt = (
            raw_terminal_receipts.get(worker_request_id)
            if isinstance(raw_terminal_receipts, Mapping)
            else None
        )
        if isinstance(terminal_receipt, Mapping):
            recovered_result = terminal_receipt.get("result")
            if (
                str(terminal_receipt.get("run_id") or "") != run_id
                or str(terminal_receipt.get("session_id") or "") != session_id
                or str(terminal_receipt.get("worker_request_id") or "")
                != worker_request_id
                or str(terminal_receipt.get("state") or "")
                != "committed_before_ack"
                or not isinstance(recovered_result, Mapping)
            ):
                raise TypeScriptRuntimeError(
                    "typescript_runtime_terminal_receipt_invalid",
                    "The durable terminal receipt does not match this logical request.",
                )
            self._host_events.append(
                EventRecord(
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    event_type=EventType.AGENT_MESSAGE,
                    payload={
                        "query_session": {
                            "phase": "terminal_result_recovered",
                            "canonical_owner": "typescript",
                            "terminal_id": str(
                                terminal_receipt.get("terminal_id") or ""
                            ),
                            "worker_request_id": worker_request_id,
                            "exactly_once": True,
                        }
                    },
                )
            )
            return self._complete_result(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                session_id=session_id,
                result_payload=dict(recovered_result),
                projection=projection,
                projection_error="",
                receipt_port=receipt_port,
                transport="durable-terminal-receipt",
                terminal_recovered=True,
            )
        self._runtime_process_epoch = max(
            self._runtime_process_epoch,
            int(restored_runtime_state.get("runtime_process_epoch") or 0),
        ) + 1
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
        self._host_events.append(
            EventRecord(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "query_session": {
                        "phase": "e02_receipt_port_attached",
                        "canonical_owner": "typescript",
                        "session_id": session_id,
                        "worker_request_id": worker_request_id,
                        "permission_runtime": {
                            "policy_owner": "typescript",
                            "journal_owner": "E02CapabilityCoordinator",
                            "python_role": "typed-physical-effect-port",
                            "python_policy_fallback": False,
                        },
                    }
                },
            )
        )
        executor = ToolExecutor(self.context, permission_authority=receipt_port)
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
                "config": self._typescript_config(session_id=session_id),
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
        pending_terminal_payload: dict[str, Any] | None = None
        pending_terminal_id = ""
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
                if "e02" in checkpoint and self._latest_runtime_checkpoint:
                    checkpoint = {
                        **self._latest_runtime_checkpoint,
                        "e02": checkpoint["e02"],
                        "checkpointPhase": checkpoint.get("checkpointPhase"),
                        "checkpointEventSequence": max(
                            int(
                                self._latest_runtime_checkpoint.get(
                                    "checkpointEventSequence"
                                )
                                or 0
                            ),
                            int(checkpoint.get("checkpointEventSequence") or 0),
                        ),
                    }
                for host_managed_key in (
                    "runtime_fault_receipts",
                    "terminal_result_receipts",
                ):
                    if host_managed_key in self._latest_runtime_checkpoint:
                        checkpoint[host_managed_key] = to_jsonable(
                            self._latest_runtime_checkpoint[host_managed_key]
                        )
                checkpoint["tool_effect_receipts"] = to_jsonable(self._tool_effect_receipts)
                checkpoint["tool_batch_evidence"] = to_jsonable(self._last_tool_batch_evidence)
                checkpoint["runtime_process_epoch"] = self._runtime_process_epoch
                checkpoint["last_checkpoint_correlation_id"] = correlation_id
                checkpoint = self._persist_incremental_checkpoint(session_id, checkpoint)
                self._latest_runtime_checkpoint = checkpoint
                fault_point = str(constraints.get("typescript_fault_injection") or "")
                final_checkpoint = bool(
                    isinstance(checkpoint.get("e02"), Mapping)
                    and dict(checkpoint["e02"]).get("closing") is True
                )
                if fault_point == "checkpoint_request_before_ack":
                    self._terminate(process)
                    fault_receipts = list(checkpoint.get("runtime_fault_receipts") or [])
                    fault_receipts.append({
                        "point": fault_point,
                        "runtime_process_epoch": self._runtime_process_epoch,
                        "process_pid": process.pid,
                        "process_exit_code": process.poll(),
                        "real_process_kill": True,
                        "checkpoint_correlation_id": correlation_id,
                    })
                    checkpoint["runtime_fault_receipts"] = fault_receipts
                    self._latest_runtime_checkpoint = self._persist_incremental_checkpoint(session_id, checkpoint)
                    raise TypeScriptRuntimeError(
                        "typescript_runtime_fault_injected",
                        f"Killed TypeScript owner at {fault_point} in epoch {self._runtime_process_epoch}.",
                    )
                self._write_frame(
                    process,
                    run_id=run_id,
                    sequence=outbound_sequence,
                    kind="runtime.checkpoint.result",
                    payload={"accepted": True},
                    correlation_id=correlation_id,
                )
                outbound_sequence += 1
                if final_checkpoint and fault_point in {
                    "final_checkpoint_ack_before_terminal",
                    "python_host_terminal_disconnect",
                }:
                    if fault_point == "python_host_terminal_disconnect" and process.stdin is not None:
                        process.stdin.close()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            self._terminate(process)
                    else:
                        self._terminate(process)
                    fault_receipts = list(self._latest_runtime_checkpoint.get("runtime_fault_receipts") or [])
                    fault_receipts.append({
                        "point": fault_point,
                        "runtime_process_epoch": self._runtime_process_epoch,
                        "process_pid": process.pid,
                        "process_exit_code": process.poll(),
                        "real_process_kill": True,
                        "checkpoint_correlation_id": correlation_id,
                    })
                    fault_checkpoint = dict(self._latest_runtime_checkpoint)
                    fault_checkpoint["runtime_fault_receipts"] = fault_receipts
                    self._latest_runtime_checkpoint = self._persist_incremental_checkpoint(
                        session_id, fault_checkpoint
                    )
                    raise TypeScriptRuntimeError(
                        "typescript_runtime_fault_injected",
                        f"Disconnected terminal transition at {fault_point} in epoch {self._runtime_process_epoch}.",
                    )
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
                        receipt_port=receipt_port,
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
                    receipt_port=receipt_port,
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
                                    pending_settlements=pending_typescript_settlements,
                        )
                    self._terminate(process)
                    raise TypeScriptRuntimeError(
                        "typescript_runtime_protocol_error",
                        "TypeScript runtime completed with unsettled capability executions.",
                    )
                terminal_id = str(payload.get("terminal_id") or "")
                terminal_revision = int(payload.get("terminal_revision") or 0)
                selected_result = dict(payload.get("result") or {})
                fault_point = str(constraints.get("typescript_fault_injection") or "")
                if fault_point == "typescript_process_terminal_disconnect":
                    self._terminate(process)
                    fault_checkpoint = dict(self._latest_runtime_checkpoint)
                    fault_receipts = list(fault_checkpoint.get("runtime_fault_receipts") or [])
                    fault_receipts.append({
                        "point": fault_point,
                        "runtime_process_epoch": self._runtime_process_epoch,
                        "process_pid": process.pid,
                        "process_exit_code": process.poll(),
                        "real_process_kill": True,
                        "terminal_id": str(payload.get("terminal_id") or ""),
                    })
                    fault_checkpoint["runtime_fault_receipts"] = fault_receipts
                    self._latest_runtime_checkpoint = self._persist_incremental_checkpoint(
                        session_id, fault_checkpoint
                    )
                    raise TypeScriptRuntimeError(
                        "typescript_runtime_fault_injected",
                        f"Killed TypeScript owner at {fault_point} in epoch {self._runtime_process_epoch}.",
                    )
                if not terminal_id or terminal_revision != 1:
                    self._terminate(process)
                    raise TypeScriptRuntimeError(
                        "typescript_runtime_protocol_error",
                        "TypeScript runtime emitted an invalid terminal result identity.",
                    )
                if pending_terminal_id and (
                    pending_terminal_id != terminal_id
                    or pending_terminal_payload != selected_result
                ):
                    self._terminate(process)
                    raise TypeScriptRuntimeError(
                        "typescript_runtime_terminal_conflict",
                        "TypeScript runtime emitted conflicting terminal results.",
                    )
                pending_terminal_id = terminal_id
                pending_terminal_payload = selected_result
                self._persist_terminal_receipt(
                    session_id=session_id,
                    worker_request_id=worker_request_id,
                    run_id=run_id,
                    terminal_id=terminal_id,
                    result=selected_result,
                )
                if fault_point == "terminal_result_ack_lost":
                    self._terminate(process)
                    fault_checkpoint = dict(self._latest_runtime_checkpoint)
                    fault_receipts = list(fault_checkpoint.get("runtime_fault_receipts") or [])
                    fault_receipts.append({
                        "point": fault_point,
                        "runtime_process_epoch": self._runtime_process_epoch,
                        "process_pid": process.pid,
                        "process_exit_code": process.poll(),
                        "real_process_kill": True,
                        "terminal_id": terminal_id,
                    })
                    fault_checkpoint["runtime_fault_receipts"] = fault_receipts
                    self._latest_runtime_checkpoint = self._persist_incremental_checkpoint(
                        session_id, fault_checkpoint
                    )
                    raise TypeScriptRuntimeError(
                        "typescript_runtime_fault_injected",
                        f"Lost terminal ACK after killing TypeScript owner in epoch {self._runtime_process_epoch}.",
                    )
                try:
                    self._write_frame(
                        process,
                        run_id=run_id,
                        sequence=outbound_sequence,
                        kind="run.result.ack",
                        payload={
                            "accepted": True,
                            "terminal_id": terminal_id,
                            "terminal_revision": terminal_revision,
                            "durable": True,
                        },
                        correlation_id=correlation_id,
                    )
                except Exception:
                    self._terminate(process)
                    raise
                outbound_sequence += 1
                continue
            if kind == "run.closed":
                closed_terminal_id = str(payload.get("terminal_id") or "")
                if (
                    pending_terminal_payload is None
                    or not pending_terminal_id
                    or closed_terminal_id != pending_terminal_id
                    or correlation_id != pending_terminal_id
                ):
                    self._terminate(process)
                    raise TypeScriptRuntimeError(
                        "typescript_runtime_protocol_error",
                        "TypeScript runtime closed without the committed terminal result.",
                    )
                result_payload = pending_terminal_payload
                self._latest_runtime_checkpoint = self._persist_incremental_checkpoint(
                    session_id,
                    self._latest_runtime_checkpoint,
                )
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

        return self._complete_result(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            session_id=session_id,
            result_payload=result_payload,
            projection=projection,
            projection_error=projection_error,
            receipt_port=receipt_port,
            transport=transport,
            terminal_recovered=False,
        )

    def _complete_result(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        session_id: str,
        result_payload: Mapping[str, Any],
        projection: QuerySession,
        projection_error: str,
        receipt_port: TypeScriptPermissionReceiptPort,
        transport: str,
        terminal_recovered: bool,
    ) -> ClaudeQueryEngineResult:
        result_payload = dict(result_payload)
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
        session_snapshot["tool_effect_receipts"] = to_jsonable(self._tool_effect_receipts)
        session_snapshot["tool_batch_evidence"] = to_jsonable(self._last_tool_batch_evidence)
        session_snapshot["host_checkpoint_revision"] = self._checkpoint_revision
        session_snapshot["runtime_process_epoch"] = self._runtime_process_epoch
        session_snapshot["protocol_frame_trace"] = to_jsonable(
            self._protocol_frame_trace
        )
        session_snapshot["runtime_fault_receipts"] = to_jsonable(
            list(self._latest_runtime_checkpoint.get("runtime_fault_receipts") or [])
        )
        raw_terminal_receipts = self._latest_runtime_checkpoint.get(
            "terminal_result_receipts"
        )
        session_snapshot["terminal_result_receipts"] = to_jsonable(
            dict(raw_terminal_receipts)
            if isinstance(raw_terminal_receipts, Mapping)
            else {}
        )
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
                "terminal_result_recovered": str(terminal_recovered).lower(),
                "host_checkpoint_revision": str(self._checkpoint_revision),
                "runtime_process_epoch": str(self._runtime_process_epoch),
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
            }
        )
        metadata.update(receipt_port.metadata())
        metadata.update(
            {
                "canonical_permission_owner": "typescript",
                "python_policy_fallback": "false",
                "typescript_permission_journal_owner": "E02CapabilityCoordinator",
            }
        )
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
            self._checkpoint_revision = 0
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
        self._checkpoint_revision = int(
            value.get("host_checkpoint_revision") or value.get("revision") or 0
        )
        return value

    def _persist_incremental_checkpoint(
        self,
        session_id: str,
        checkpoint: Mapping[str, Any],
    ) -> dict[str, Any]:
        path = self._checkpoint_path(session_id)
        staged = path.with_suffix(path.suffix + ".tmp")
        lock_key = str(path)
        with _CHECKPOINT_LOCKS_GUARD:
            checkpoint_lock = _CHECKPOINT_LOCKS.setdefault(lock_key, threading.RLock())
        with checkpoint_lock:
            current_revision = 0
            if path.exists():
                try:
                    current = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    raise TypeScriptRuntimeError(
                        "typescript_runtime_checkpoint_corrupt",
                        f"Cannot compare-and-swap the durable TypeScript checkpoint: {error}",
                    ) from error
                if not isinstance(current, dict) or str(current.get("session_id") or "") != session_id:
                    raise TypeScriptRuntimeError(
                        "typescript_runtime_checkpoint_identity",
                        "Durable TypeScript checkpoint changed logical session during compare-and-swap.",
                    )
                current_revision = int(
                    current.get("host_checkpoint_revision") or current.get("revision") or 0
                )
            if current_revision != self._checkpoint_revision:
                raise TypeScriptRuntimeError(
                    "typescript_runtime_checkpoint_stale_writer",
                    "Durable TypeScript checkpoint compare-and-swap rejected a stale writer: "
                    f"expected {self._checkpoint_revision}, observed {current_revision}.",
                )
            payload = dict(checkpoint)
            next_revision = current_revision + 1
            payload["session_id"] = session_id
            payload["protocol_frame_trace"] = to_jsonable(
                self._protocol_frame_trace
            )
            payload["host_checkpoint_parent_revision"] = current_revision
            payload["host_checkpoint_revision"] = next_revision
            payload["host_checkpoint_writer_id"] = self._checkpoint_writer_id
            commit_material = {
                key: value
                for key, value in payload.items()
                if key != "host_checkpoint_commit_id"
            }
            payload["host_checkpoint_commit_id"] = hashlib.sha256(
                json.dumps(
                    to_jsonable(commit_material),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            encoded = json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True)
            staged.write_text(encoded, encoding="utf-8")
            os.replace(staged, path)
            self._checkpoint_revision = next_revision
            return payload

    def _persist_terminal_receipt(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        run_id: str,
        terminal_id: str,
        result: Mapping[str, Any],
    ) -> None:
        checkpoint = dict(self._latest_runtime_checkpoint)
        raw_receipts = checkpoint.get("terminal_result_receipts")
        receipts = (
            {str(key): dict(value) for key, value in raw_receipts.items() if isinstance(value, Mapping)}
            if isinstance(raw_receipts, Mapping)
            else {}
        )
        if str(result.get("stoppedReason") or "") == "permission_suspended":
            # ASK is a durable suspension point, not the terminal outcome of
            # the logical worker request.  Its E02 continuation is already in
            # the checkpoint and must remain resumable after external approval.
            receipts.pop(worker_request_id, None)
            checkpoint["terminal_result_receipts"] = receipts
            self._latest_runtime_checkpoint = self._persist_incremental_checkpoint(
                session_id, checkpoint
            )
            return
        existing = receipts.get(worker_request_id)
        if existing and (
            str(existing.get("terminal_id") or "") != terminal_id
            or dict(existing.get("result") or {}) != dict(result)
        ):
            raise TypeScriptRuntimeError(
                "typescript_runtime_terminal_conflict",
                "A different terminal result is already committed for this worker request.",
            )
        if not existing:
            receipts[worker_request_id] = {
                "schema_version": 1,
                "run_id": run_id,
                "session_id": session_id,
                "worker_request_id": worker_request_id,
                "terminal_id": terminal_id,
                "terminal_revision": 1,
                "state": "committed_before_ack",
                "result": to_jsonable(dict(result)),
            }
        checkpoint["terminal_result_receipts"] = receipts
        self._latest_runtime_checkpoint = self._persist_incremental_checkpoint(
            session_id, checkpoint
        )

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

    def _typescript_config(self, *, session_id: str) -> dict[str, Any]:
        runtime_constraints = dict(self.config.runtime_constraints)
        runtime_constraints.setdefault("workspaceRoot", str(self.context.workspace_root))
        runtime_constraints.setdefault("projectRoot", str(self.project_root))
        if runtime_constraints.get("permission_transport_queue_enabled") is True:
            runtime_constraints["hostApprovalResponses"] = self._host_permission_responses(
                session_id
            )
        raw_policy = runtime_constraints.pop(
            "e02PermissionPolicy",
            runtime_constraints.pop("permissionPolicy", {}),
        )
        if raw_policy is not None and not isinstance(raw_policy, Mapping):
            raise TypeScriptRuntimeError(
                "e02_permission_policy_invalid",
                "e02PermissionPolicy must be a JSON object",
            )
        permission_policy = dict(raw_policy or {})
        permission_policy.setdefault("version", "zyra.e02-typescript-permission-policy-input.v1")
        permission_policy.setdefault("canonical_owner", "typescript")
        permission_policy.setdefault("mode", self.config.permission_mode)
        permission_policy.setdefault("mode_revision", 0)
        permission_policy.setdefault("interactive", self.config.permission_interactive)
        permission_policy.setdefault("headless", self.config.permission_headless)
        permission_policy.setdefault("rules", [])
        permission_policy.setdefault("python_policy_fallback", False)
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
            "permissionPolicy": to_jsonable(permission_policy),
        }

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
        receipt_port: TypeScriptPermissionReceiptPort,
        pending_typescript_settlements: dict[str, str],
        tool_effect_receipts: dict[str, dict[str, Any]],
        receipt_lock: threading.RLock,
    ) -> dict[str, Any]:
        tool_name = str(payload.get("tool_name") or "")
        tool_call_id = str(payload.get("tool_call_id") or "")
        arguments = dict(payload.get("arguments") or {})
        decision = dict(payload.get("permission_decision") or {})
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
        effect_key = hashlib.sha256(
            json.dumps(
                {
                    "run_id": run_id,
                    "task_id": task_id,
                    "session_id": session_id,
                    "worker_request_id": worker_request_id,
                    "turn_index": int(payload.get("turn_index") or 0),
                    "step_index": int(payload.get("step_index") or 0),
                    "batch_index": int(payload.get("batch_index") or 0),
                    "tool_name": tool_name,
                    "arguments": arguments,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with receipt_lock:
            cached = tool_effect_receipts.get(tool_call_id)
            if cached is None:
                cached = next(
                    (
                        receipt
                        for receipt in tool_effect_receipts.values()
                        if str(receipt.get("effect_key") or "") == effect_key
                    ),
                    None,
                )
            if cached is not None:
                if str(cached.get("request_digest") or "") != request_digest:
                    return {
                        "tool_call_id": tool_call_id,
                        "ok": False,
                        "summary": "Stable tool call id was reused with a different payload",
                        "output": {},
                        "artifacts": [],
                        "error": "tool_effect_identity_conflict",
                        "metadata": {
                            "canonical_permission_owner": "typescript",
                            "effect_replay_fenced": "true",
                        },
                    }
                replayed = dict(cached.get("result") or {})
                replayed_metadata = dict(replayed.get("metadata") or {})
                replayed_metadata["effect_replay_fenced"] = "true"
                replayed_metadata["effect_replayed"] = "false"
                replayed_metadata["effect_key"] = effect_key
                replayed_metadata["original_tool_call_id"] = str(
                    cached.get("original_tool_call_id") or replayed.get("tool_call_id") or ""
                )
                replayed["tool_call_id"] = tool_call_id
                replayed["metadata"] = replayed_metadata
                return replayed
        raw_binding = decision.get("requestBinding", decision.get("request_binding"))
        binding = dict(raw_binding) if isinstance(raw_binding, Mapping) else {}
        namespace = str(binding.get("namespace") or "builtin")
        server_id = str(binding.get("server_id") or "")
        operation = str(binding.get("operation") or "execute")
        effect = str(decision.get("effect") or "deny")
        if effect != "allow":
            if effect == "ask":
                self._project_permission_request(
                    decision=decision,
                    binding=binding,
                    run_id=run_id,
                    task_id=task_id,
                    worker_request_id=worker_request_id,
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    namespace=namespace,
                    server_id=server_id,
                )
            return to_jsonable(
                ToolResult(
                    tool_call_id=tool_call_id,
                    ok=False,
                    summary=str(decision.get("reason") or "TypeScript permission denied"),
                    output={
                        "continuation_request_id": str(
                            decision.get("continuationRequestId", decision.get("continuation_request_id", "")) or ""
                        ),
                        "recovery_input": decision.get("recoveryInput", decision.get("recovery_input")),
                        "replan_required": bool(
                            decision.get("replanRequired", decision.get("replan_required", False))
                        ),
                    },
                    error="permission_approval_required" if effect == "ask" else "permission_denied",
                    metadata={
                        "permission_effect": effect,
                        "permission_decision_id": str(
                            decision.get("decisionId", decision.get("decision_id", "")) or ""
                        ),
                        "canonical_permission_owner": "typescript",
                        "python_policy_fallback": "false",
                    },
                )
            )
        try:
            permit = receipt_port.accept(
                decision,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                namespace=namespace,
                server_id=server_id,
                operation=operation,
            )
        except TypeScriptPermissionReceiptError as error:
            return to_jsonable(
                ToolResult(
                    tool_call_id=tool_call_id,
                    ok=False,
                    summary=str(error),
                    error=error.code,
                    metadata={
                        "permission_effect": "deny",
                        "canonical_permission_owner": "typescript",
                        "python_policy_fallback": "false",
                        "receipt_validation_failed": "true",
                    },
                )
            )
        if permission_only:
            pending_typescript_settlements[tool_call_id] = permit.decision_id
            return to_jsonable(
                ToolResult(
                    tool_call_id=tool_call_id,
                    ok=True,
                    summary="TypeScript capability receipt accepted by physical host",
                    output={"permission_committed": True, "execution_owner": execution_owner},
                    metadata={
                        "permission_effect": "allow",
                        "permission_decision_id": permit.decision_id,
                        "permission_commit_only": "true",
                        "canonical_permission_owner": "typescript",
                        "canonical_capability_owner": execution_owner,
                        "python_policy_fallback": "false",
                    },
                )
            )
        spec = self.context.registry.get(tool_name)
        if spec is None:
            return to_jsonable(
                ToolResult(
                    tool_call_id=tool_call_id,
                    ok=False,
                    summary=f"Unknown physical tool: {tool_name}",
                    error="unknown_tool",
                    metadata={
                        "canonical_permission_owner": "typescript",
                        "permission_decision_id": permit.decision_id,
                    },
                )
            )
        metadata = {str(key): str(value) for key, value in dict(payload.get("metadata") or {}).items()}
        metadata.update(
            {
                "tool_namespace": namespace,
                "namespace": namespace,
                "server_id": server_id,
                "server_name": server_id,
                "canonical_request_owner": "typescript",
                "canonical_permission_owner": "typescript",
                "execution_owner": execution_owner,
                "e02_receipt_digest": permit.receipt_digest,
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
        result = executor.execute(call, permission_grant=permit)
        encoded_result = to_jsonable(result)
        with receipt_lock:
            tool_effect_receipts[tool_call_id] = {
                "request_digest": request_digest,
                "effect_key": effect_key,
                "original_tool_call_id": tool_call_id,
                "decision_id": permit.decision_id,
                "receipt_digest": permit.receipt_digest,
                "result": encoded_result,
            }
            checkpoint = dict(self._latest_runtime_checkpoint)
            checkpoint["tool_effect_receipts"] = to_jsonable(tool_effect_receipts)
            self._latest_runtime_checkpoint = self._persist_incremental_checkpoint(
                session_id, checkpoint
            )
        self._host_artifacts.extend(result.artifacts)
        return encoded_result

    def _permission_queue(self, session_id: str) -> PermissionRequestQueue:
        if self.config.permission_state_path is None:
            raise TypeScriptRuntimeError(
                "permission_transport_state_missing",
                "The external approval transport requires PermissionStateStore custody.",
            )
        return PermissionRequestQueue(
            PermissionStateStore(Path(self.config.permission_state_path).resolve()),
            session_id,
        )

    @staticmethod
    def _find_e02_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
        pending: list[Mapping[str, Any]] = [value]
        visited: set[int] = set()
        while pending:
            candidate = pending.pop(0)
            identity = id(candidate)
            if identity in visited:
                continue
            visited.add(identity)
            if candidate.get("version") == "zyra.e02-runtime/v1":
                return dict(candidate)
            for child in candidate.values():
                if isinstance(child, Mapping):
                    pending.append(child)
        return {}

    def _host_permission_responses(self, session_id: str) -> list[dict[str, Any]]:
        responses: list[dict[str, Any]] = []
        for record in self._permission_queue(session_id).list(
            phases=(PermissionRequestPhase.RESOLVED,),
        ):
            if record.metadata.get("typescript_transport_projection") is not True:
                continue
            effect = record.resolution_effect
            response_id = str(
                record.metadata.get("resolution_idempotency_key") or ""
            )
            if effect not in {PermissionEffect.ALLOW, PermissionEffect.DENY} or not response_id:
                raise TypeScriptRuntimeError(
                    "permission_transport_resolution_invalid",
                    f"Resolved approval {record.request_id} lacks an exact transport identity.",
                )
            binding = dict(record.metadata.get("typescript_request_binding") or {})
            responses.append(
                {
                    "responseId": response_id,
                    "requestId": record.request_id,
                    "runId": record.run_id,
                    "sessionId": record.session_id,
                    "sessionRevision": int(binding.get("session_revision") or 0),
                    "workerRequestId": record.worker_request_id,
                    "toolCallId": record.tool_use_id,
                    "effect": effect.value,
                    "responder": record.resolved_by,
                    "respondedAt": record.resolved_at,
                    "metadata": {
                        "transport": record.resolution_channel,
                        "python_role": "approval-transport-only",
                        "canonical_policy_owner": "typescript",
                        "permission_request_fingerprint": record.request_fingerprint,
                    },
                }
            )
        return responses

    def _project_permission_request(
        self,
        *,
        decision: Mapping[str, Any],
        binding: Mapping[str, Any],
        run_id: str,
        task_id: str,
        worker_request_id: str,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        namespace: str,
        server_id: str,
    ) -> None:
        if self.config.runtime_constraints.get("permission_transport_queue_enabled") is not True:
            return
        request_id = str(
            decision.get(
                "continuationRequestId",
                decision.get("continuation_request_id", ""),
            )
            or ""
        )
        arguments_digest = str(
            decision.get(
                "finalArgumentsDigest",
                decision.get("final_arguments_digest", ""),
            )
            or ""
        )
        request_fingerprint = str(
            decision.get(
                "requestFingerprint",
                decision.get("request_fingerprint", ""),
            )
            or ""
        )
        if not request_id or not arguments_digest or not request_fingerprint:
            raise TypeScriptRuntimeError(
                "permission_transport_request_invalid",
                "TypeScript ASK decision lacks its continuation identity or digests.",
            )
        queue = self._permission_queue(session_id)
        existing = queue.get(request_id)
        if existing is not None:
            exact_identity = (
                existing.run_id == run_id
                and existing.task_id == task_id
                and existing.worker_request_id == worker_request_id
                and existing.tool_use_id == tool_call_id
                and existing.arguments_digest == arguments_digest
                and existing.request_fingerprint == request_fingerprint
            )
            if not exact_identity:
                raise TypeScriptRuntimeError(
                    "permission_transport_identity_conflict",
                    f"Approval request {request_id} is already bound to another tool call.",
                )
            return
        expires_at = (
            datetime.now(timezone.utc)
            + timedelta(seconds=max(1.0, self.config.permission_approval_ttl_seconds))
        ).isoformat()
        raw_mode = str(decision.get("mode") or self.config.permission_mode or "default")
        if raw_mode == "acceptEdits":
            mode = PermissionMode.ACCEPT_EDITS
        elif raw_mode == "dontAsk":
            mode = PermissionMode.DONT_ASK
        else:
            mode = PermissionMode(raw_mode)
        record = PermissionRequestRecord(
            request_id=request_id,
            session_id=session_id,
            task_id=task_id,
            run_id=run_id,
            worker_request_id=worker_request_id,
            tool_use_id=tool_call_id,
            tool_identity=ToolIdentity(
                namespace=namespace,
                name=tool_name,
                server_id=server_id,
                version=str(binding.get("version") or ""),
                schema_digest=str(binding.get("schema_digest") or ""),
            ),
            arguments_digest=arguments_digest,
            request_fingerprint=request_fingerprint,
            scope=PermissionScope(
                kind=PermissionScopeKind.ACTION,
                session_id=session_id,
                task_id=task_id,
                run_id=run_id,
                workspace_root=str(self.context.workspace_root),
                tool_namespace=namespace,
                tool_name=tool_name,
                server_id=server_id,
                argument_digest=arguments_digest,
                request_fingerprint=request_fingerprint,
            ),
            expires_at=expires_at,
            reason_code=str(
                decision.get("reasonCode", decision.get("reason_code", "typescript_ask"))
                or "typescript_ask"
            ),
            reason=str(decision.get("reason") or "TypeScript permission approval required"),
            rule_snapshot_id=str(
                decision.get("policyDigest", decision.get("policy_digest", "")) or ""
            ),
            mode=mode,
            metadata={
                "typescript_transport_projection": True,
                "canonical_policy_owner": "typescript",
                "python_role": "approval-transport-only",
                "typescript_decision_id": str(
                    decision.get("decisionId", decision.get("decision_id", "")) or ""
                ),
                "typescript_request_binding": to_jsonable(dict(binding)),
                "typescript_policy_revision": int(
                    decision.get("policyRevision", decision.get("policy_revision", 0)) or 0
                ),
                "typescript_mode_revision": int(
                    decision.get("modeRevision", decision.get("mode_revision", 0)) or 0
                ),
            },
        )
        created = queue.create(record)
        queue.mark_delivered(
            request_id,
            expected_request_revision=created.revision,
            channel="typescript-stdio",
        )

    def _settle_typescript_capability(
        self,
        *,
        payload: Mapping[str, Any],
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        pending_settlements: dict[str, str],
    ) -> dict[str, Any]:
        del run_id, task_id, node_id, worker_request_id
        tool_call_id = str(payload.get("tool_call_id") or "")
        decision_id = pending_settlements.pop(tool_call_id, "")
        if not tool_call_id or not decision_id:
            return {
                "accepted": False,
                "tool_call_id": tool_call_id,
                "error": "unknown_or_duplicate_capability_settlement",
            }
        return {
            "accepted": True,
            "tool_call_id": tool_call_id,
            "decision_id": decision_id,
            "ok": payload.get("ok") is True,
            "error": str(payload.get("error") or ""),
            "canonical_permission_owner": "typescript",
            "python_role": "settlement-transport",
        }

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
        failed_snapshot["protocol_frame_trace"] = to_jsonable(
            self._protocol_frame_trace
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
                "typescript_runtime_error_message": str(error),
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
        self._protocol_frame_trace.append(
            {
                "trace_index": len(self._protocol_frame_trace) + 1,
                "direction": "typescript-to-python",
                "runtime_process_epoch": self._runtime_process_epoch,
                "process_pid": process.pid,
                "protocol": str(frame.get("protocol") or ""),
                "run_id": str(frame.get("run_id") or ""),
                "sequence": int(frame.get("sequence") or 0),
                "kind": str(frame.get("kind") or ""),
                "correlation_id": str(frame.get("correlation_id") or ""),
                "payload_digest": hashlib.sha256(
                    json.dumps(
                        to_jsonable(frame.get("payload") or {}),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            }
        )
        return frame

    def _write_frame(
        self,
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
            self._protocol_frame_trace.append(
                {
                    "trace_index": len(self._protocol_frame_trace) + 1,
                    "direction": "python-to-typescript",
                    "runtime_process_epoch": self._runtime_process_epoch,
                    "process_pid": process.pid,
                    "protocol": RUNTIME_PROTOCOL_VERSION,
                    "run_id": run_id,
                    "sequence": sequence,
                    "kind": kind,
                    "correlation_id": correlation_id,
                    "payload_digest": hashlib.sha256(
                        json.dumps(
                            to_jsonable(dict(payload)),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                }
            )
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
