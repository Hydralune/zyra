from __future__ import annotations

"""Durable MCP long-running task lifecycle (SEP-1686/SEP-2663 style)."""

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from zyra_core import EventRecord, EventType, new_id, now_iso

from .models import JsonValue, McpTaskOptions, McpTaskSnapshot, McpTaskStatus, redact_value


class McpTaskError(RuntimeError):
    pass


class McpTaskOutcomeUnknown(McpTaskError):
    """The create request may have reached the server; never replay it."""


class McpTaskCancelled(McpTaskError):
    pass


class McpTaskTimedOut(McpTaskError):
    pass


class McpTaskRequestPort(Protocol):
    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]: ...


class McpTaskStatePort(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class McpTaskExecutionReceipt:
    execution_id: str
    server_id: str
    tool_name: str
    task_id: str
    result: Mapping[str, JsonValue]
    snapshots: tuple[McpTaskSnapshot, ...]
    tool_call_count: int
    poll_count: int
    reconnect_count: int
    cancel_count: int
    outcome_unknown: bool = False
    started_at: str = field(default_factory=now_iso)
    completed_at: str = field(default_factory=now_iso)

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "execution_id": self.execution_id,
            "server_id": self.server_id,
            "tool_name": self.tool_name,
            "task_id": self.task_id,
            "result": redact_value(self.result),
            "snapshots": [item.to_dict() for item in self.snapshots],
            "tool_call_count": self.tool_call_count,
            "poll_count": self.poll_count,
            "reconnect_count": self.reconnect_count,
            "cancel_count": self.cancel_count,
            "outcome_unknown": self.outcome_unknown,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


class McpTaskLifecycleRuntime:
    """Own create/poll/result/cancel and restart-safe task handles.

    A tool create call is never automatically replayed.  Once a task id is
    known, transport reconnect may repeat only idempotent ``tasks/get`` and
    ``tasks/result`` requests for that same id.
    """

    def __init__(
        self,
        *,
        options: McpTaskOptions | None = None,
        state_store: McpTaskStatePort | None = None,
        reconnect: Callable[[str], McpTaskRequestPort | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        event_sink: Callable[[EventRecord], None] | None = None,
        disabled: bool = False,
    ) -> None:
        self.options = options or McpTaskOptions()
        self.state_store = state_store
        self.reconnect = reconnect
        self.clock = clock
        self.sleeper = sleeper
        self.event_sink = event_sink
        self.disabled = disabled
        self._locks: dict[tuple[str, str], threading.RLock] = {}
        self._lock = threading.RLock()
        self._cancelled: set[tuple[str, str]] = set()

    def execute_tool(
        self,
        *,
        request: McpTaskRequestPort,
        server_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        use_task: bool,
        run_id: str = "",
        task_id: str = "",
        node_id: str | None = None,
        deadline_seconds: float | None = None,
    ) -> McpTaskExecutionReceipt:
        if self.disabled:
            raise McpTaskError("McpTaskLifecycleRuntime is disabled")
        started = self.clock()
        started_at = now_iso()
        params: dict[str, Any] = {"name": tool_name, "arguments": dict(arguments)}
        if use_task:
            params["task"] = self.options.to_task_metadata()
        try:
            initial = request.request("tools/call", params)
        except Exception as error:  # noqa: BLE001
            # The transport cannot prove whether the server accepted a
            # side-effecting create.  Replaying would risk duplicate effects.
            raise McpTaskOutcomeUnknown(
                f"tools/call outcome is unknown; refusing replay ({type(error).__name__})"
            ) from error

        if not isinstance(initial, Mapping):
            raise McpTaskError("tools/call returned a non-object result")
        raw_task = initial.get("task")
        if not isinstance(raw_task, Mapping):
            return McpTaskExecutionReceipt(
                execution_id=new_id("mcptaskexec"),
                server_id=server_id,
                tool_name=tool_name,
                task_id="",
                result=redact_value(initial),
                snapshots=(),
                tool_call_count=1,
                poll_count=0,
                reconnect_count=0,
                cancel_count=0,
                started_at=started_at,
            )

        snapshot = McpTaskSnapshot.from_result(server_id, tool_name, initial, revision=1)
        with self._task_lock(server_id, snapshot.task_id):
            self._persist(snapshot)
            snapshots = [snapshot]
            polls = 0
            reconnects = 0
            cancels = 0
            consecutive_transport_errors = 0
            while not snapshot.status.terminal:
                if self._is_cancelled(server_id, snapshot.task_id):
                    cancels += self._cancel_remote(request, snapshot)
                    raise McpTaskCancelled(f"MCP task {snapshot.task_id} was cancelled")
                timeout = deadline_seconds or self.options.max_wait_seconds
                if timeout is not None and self.clock() - started >= timeout:
                    if self.options.cancel_remote_on_local_cancel:
                        cancels += self._cancel_remote(request, snapshot)
                    raise McpTaskTimedOut(f"MCP task {snapshot.task_id} exceeded {timeout} seconds")

                self.sleeper(self.options.clamp_poll_interval(snapshot.poll_interval_ms))
                try:
                    status_result = request.request(
                        "tasks/get",
                        {"taskId": snapshot.task_id},
                    )
                    consecutive_transport_errors = 0
                except Exception as error:  # noqa: BLE001
                    consecutive_transport_errors += 1
                    if consecutive_transport_errors > 1 or self.reconnect is None:
                        if self.options.cancel_remote_on_local_cancel:
                            cancels += self._cancel_remote(request, snapshot, suppress=True)
                        raise McpTaskError(
                            f"tasks/get failed after reconnect for {snapshot.task_id}"
                        ) from error
                    replacement = self.reconnect(server_id)
                    if replacement is not None:
                        if not hasattr(replacement, "request"):
                            raise McpTaskError("reconnect returned an invalid MCP task request port")
                        request = replacement
                    reconnects += 1
                    continue
                if not isinstance(status_result, Mapping):
                    cancels += self._cancel_remote(request, snapshot, suppress=True)
                    raise McpTaskError("tasks/get returned a non-object result")
                next_snapshot = McpTaskSnapshot.from_result(
                    server_id,
                    tool_name,
                    status_result,
                    revision=snapshot.revision + 1,
                )
                if next_snapshot.task_id != snapshot.task_id:
                    cancels += self._cancel_remote(request, snapshot, suppress=True)
                    raise McpTaskError("tasks/get changed task identity")
                snapshot = replace(
                    next_snapshot,
                    created_at=snapshots[0].created_at,
                    updated_at=now_iso(),
                )
                snapshots.append(snapshot)
                polls += 1
                self._persist(snapshot)
                self._emit(snapshot, run_id=run_id, task_id=task_id, node_id=node_id)

            if snapshot.status is McpTaskStatus.COMPLETED:
                try:
                    result = request.request("tasks/result", {"taskId": snapshot.task_id})
                except Exception as error:  # noqa: BLE001
                    if self.reconnect is None:
                        raise McpTaskError("tasks/result failed") from error
                    replacement = self.reconnect(server_id)
                    if replacement is not None:
                        if not hasattr(replacement, "request"):
                            raise McpTaskError("reconnect returned an invalid MCP task request port")
                        request = replacement
                    reconnects += 1
                    result = request.request("tasks/result", {"taskId": snapshot.task_id})
                if not isinstance(result, Mapping):
                    raise McpTaskError("tasks/result returned a non-object result")
            else:
                result = {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": snapshot.error_message or f"MCP task ended with status {snapshot.status}",
                        }
                    ],
                    "task": snapshot.to_dict(),
                }
            return McpTaskExecutionReceipt(
                execution_id=new_id("mcptaskexec"),
                server_id=server_id,
                tool_name=tool_name,
                task_id=snapshot.task_id,
                result=redact_value(result),
                snapshots=tuple(snapshots),
                tool_call_count=1,
                poll_count=polls,
                reconnect_count=reconnects,
                cancel_count=cancels,
                started_at=started_at,
            )

    def resume(
        self,
        *,
        request: McpTaskRequestPort,
        snapshot: McpTaskSnapshot,
        run_id: str = "",
        task_id: str = "",
        node_id: str | None = None,
        deadline_seconds: float | None = None,
    ) -> McpTaskExecutionReceipt:
        """Resume a known task id without reissuing ``tools/call``."""

        if snapshot.status.terminal:
            raise McpTaskError("cannot resume a terminal MCP task")
        shim = _ResumeTaskPort(request, snapshot)
        # execute_tool consumes the shim's synthetic initial task response; it
        # never sends tools/call to the remote transport.
        receipt = self.execute_tool(
            request=shim,
            server_id=snapshot.server_id,
            tool_name=snapshot.tool_name,
            arguments={},
            use_task=True,
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            deadline_seconds=deadline_seconds,
        )
        return replace(receipt, tool_call_count=0)

    def cancel(self, server_id: str, task_id: str) -> None:
        with self._lock:
            self._cancelled.add((server_id, task_id))

    def clear_cancel(self, server_id: str, task_id: str) -> None:
        with self._lock:
            self._cancelled.discard((server_id, task_id))

    def _is_cancelled(self, server_id: str, task_id: str) -> bool:
        with self._lock:
            return (server_id, task_id) in self._cancelled

    def _task_lock(self, server_id: str, task_id: str) -> threading.RLock:
        with self._lock:
            return self._locks.setdefault((server_id, task_id), threading.RLock())

    def _cancel_remote(
        self,
        request: McpTaskRequestPort,
        snapshot: McpTaskSnapshot,
        *,
        suppress: bool = False,
    ) -> int:
        if snapshot.status.terminal:
            return 0
        try:
            request.request("tasks/cancel", {"taskId": snapshot.task_id})
        except Exception:
            if not suppress:
                raise
        return 1

    def _persist(self, snapshot: McpTaskSnapshot) -> None:
        if self.state_store is None:
            return
        key = f"tasks.{snapshot.server_id}.{snapshot.task_id}"
        try:
            self.state_store.set(key, snapshot.to_dict())
        except TypeError:
            self.state_store.set(key, snapshot.to_dict(), expected_revision=None)

    def _emit(
        self,
        snapshot: McpTaskSnapshot,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> None:
        if self.event_sink is None or not run_id or not task_id:
            return
        event_type = getattr(EventType, "MCP_TASK_UPDATED", EventType.SYSTEM_NOTICE)
        self.event_sink(
            EventRecord(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                event_type=event_type,
                payload={
                    "mcp_runtime": {
                        "schema": "zyra.mcp-task-event.v1",
                        "runtime_id": "McpTaskLifecycleRuntime",
                        "server_id": snapshot.server_id,
                        "task": snapshot.to_dict(),
                    }
                },
            )
        )


class _ResumeTaskPort:
    def __init__(self, delegate: McpTaskRequestPort, snapshot: McpTaskSnapshot) -> None:
        self.delegate = delegate
        self.snapshot = snapshot
        self._initial_sent = False

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        if method == "tools/call" and not self._initial_sent:
            self._initial_sent = True
            return {"task": self.snapshot.to_dict()}
        return self.delegate.request(method, params, timeout_seconds=timeout_seconds)


__all__ = [
    "McpTaskCancelled",
    "McpTaskError",
    "McpTaskExecutionReceipt",
    "McpTaskLifecycleRuntime",
    "McpTaskOutcomeUnknown",
    "McpTaskRequestPort",
    "McpTaskTimedOut",
]
