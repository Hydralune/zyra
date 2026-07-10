from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from zyra_core import ArtifactKind, ArtifactRef, new_id, now_iso, to_jsonable

from .artifacts import LocalArtifactStore
from .tools import ToolCall, ToolRegistry, ToolResult, ToolSpec


class ToolAccessMode(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    SHELL = "shell"
    ARTIFACT_WRITE = "artifact_write"
    CONTROL = "control"
    UNKNOWN = "unknown"


class ToolBatchExecutionMode(StrEnum):
    CONCURRENT_READ_ONLY = "concurrent_read_only"
    SERIAL_READ_ONLY = "serial_read_only"
    SERIAL_NON_READ_ONLY = "serial_non_read_only"


class ToolFailureKind(StrEnum):
    SCHEMA_ERROR = "schema_error"
    PERMISSION_DENIED = "permission_denied"
    PERMISSION_REQUIRED = "permission_required"
    TIMEOUT = "timeout"
    RUNTIME_ERROR = "runtime_error"
    NON_ZERO_EXIT = "non_zero_exit"
    BUDGET_EXCEEDED = "budget_exceeded"
    CONFLICT_GUARD = "conflict_guard"
    UNKNOWN_TOOL = "unknown_tool"


class ToolSignalSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


@dataclass(frozen=True, slots=True)
class ToolSchemaViolation:
    field: str
    message: str
    expected: str = ""

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ToolLoopRequest:
    run_id: str
    task_id: str
    node_id: str | None
    worker_request_id: str
    turn_index: int
    step_index: int
    tool_name: str
    arguments: dict[str, Any]
    call: ToolCall
    access_mode: ToolAccessMode
    read_only: bool
    concurrency_safe: bool
    mutates_workspace: bool
    conflict_key: str
    source_path: str
    schema_errors: list[ToolSchemaViolation] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return not self.schema_errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.call.tool_call_id,
            "tool_name": self.tool_name,
            "turn_index": self.turn_index,
            "step_index": self.step_index,
            "access_mode": str(self.access_mode),
            "read_only": self.read_only,
            "concurrency_safe": self.concurrency_safe,
            "mutates_workspace": self.mutates_workspace,
            "conflict_key": self.conflict_key,
            "source_path": self.source_path,
            "schema_errors": [item.to_dict() for item in self.schema_errors],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolLoopBatch:
    batch_index: int
    requests: list[ToolLoopRequest]
    execution_mode: ToolBatchExecutionMode
    conflict_keys: list[str]
    conflict_protected: bool = False

    @property
    def read_only(self) -> bool:
        return all(request.read_only for request in self.requests)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_index": self.batch_index,
            "execution_mode": str(self.execution_mode),
            "tool_count": len(self.requests),
            "tool_names": [request.tool_name for request in self.requests],
            "tool_call_ids": [request.call.tool_call_id for request in self.requests],
            "read_only": self.read_only,
            "conflict_keys": list(self.conflict_keys),
            "conflict_protected": self.conflict_protected,
        }


@dataclass(frozen=True, slots=True)
class ToolLoopPlan:
    run_id: str
    task_id: str
    worker_request_id: str
    turn_index: int
    requests: list[ToolLoopRequest]
    batches: list[ToolLoopBatch]
    read_only_count: int
    write_count: int
    schema_error_count: int
    conflict_protected_count: int
    max_read_only_concurrency: int
    source_contract: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_request_id": self.worker_request_id,
            "turn_index": self.turn_index,
            "tool_count": len(self.requests),
            "batch_count": len(self.batches),
            "read_only_count": self.read_only_count,
            "write_count": self.write_count,
            "schema_error_count": self.schema_error_count,
            "conflict_protected_count": self.conflict_protected_count,
            "max_read_only_concurrency": self.max_read_only_concurrency,
            "requests": [request.to_dict() for request in self.requests],
            "batches": [batch.to_dict() for batch in self.batches],
            "source_contract": to_jsonable(self.source_contract),
        }


@dataclass(frozen=True, slots=True)
class ToolBudgetDecision:
    applied: bool
    original_chars: int
    budget_chars: int
    preview_chars: int = 0
    artifact_id: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ToolFailureSignal:
    signal_id: str
    tool_call_id: str
    tool_name: str
    kind: ToolFailureKind
    severity: ToolSignalSeverity
    message: str
    retryable: bool
    watchdog_route: str
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


class ToolLoopScheduler:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        max_read_only_concurrency: int = 10,
        source_contract: Mapping[str, Any] | None = None,
    ) -> None:
        self.registry = registry
        self.max_read_only_concurrency = max(1, max_read_only_concurrency)
        self.source_contract = dict(source_contract or {})

    def plan_turn(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        turn_index: int,
        steps: list[dict[str, Any]],
    ) -> ToolLoopPlan:
        requests = [
            self._request_from_step(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                worker_request_id=worker_request_id,
                turn_index=turn_index,
                step_index=step_index,
                step=step,
            )
            for step_index, step in enumerate(steps, start=1)
        ]
        batches = self._partition(requests)
        scheduled_requests = [request for batch in batches for request in batch.requests]
        conflict_protected = sum(1 for request in scheduled_requests if request.metadata.get("conflict_protected") == "true")
        return ToolLoopPlan(
            run_id=run_id,
            task_id=task_id,
            worker_request_id=worker_request_id,
            turn_index=turn_index,
            requests=scheduled_requests,
            batches=batches,
            read_only_count=sum(1 for request in requests if request.read_only),
            write_count=sum(1 for request in requests if not request.read_only),
            schema_error_count=sum(len(request.schema_errors) for request in requests),
            conflict_protected_count=conflict_protected,
            max_read_only_concurrency=self.max_read_only_concurrency,
            source_contract=dict(self.source_contract),
        )

    def schema_error_result(self, request: ToolLoopRequest) -> ToolResult:
        return ToolResult(
            tool_call_id=request.call.tool_call_id,
            ok=False,
            summary=f"{request.tool_name or 'unknown_tool'} schema validation failed",
            output={
                "schema_errors": [item.to_dict() for item in request.schema_errors],
                "tool_name": request.tool_name,
                "step_index": request.step_index,
            },
            error="schema_error",
            metadata={
                "failure_kind": str(ToolFailureKind.SCHEMA_ERROR),
                "schema_error_count": str(len(request.schema_errors)),
            },
        )

    def validate_arguments(self, tool_name: str, arguments: Any) -> tuple[ToolSchemaViolation, ...]:
        """Revalidate post-hook arguments before an execution grant is used."""

        return tuple(self._validate_step(tool_name, arguments, self.registry.get(tool_name)))

    def _request_from_step(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        turn_index: int,
        step_index: int,
        step: dict[str, Any],
    ) -> ToolLoopRequest:
        tool_name = str(step.get("tool_name") or step.get("tool") or "")
        raw_arguments = step.get("arguments")
        arguments = dict(raw_arguments) if isinstance(raw_arguments, dict) else {}
        spec = self.registry.get(tool_name)
        schema_errors = self._validate_step(tool_name, raw_arguments, spec)
        access_mode = _access_mode(tool_name, arguments, spec)
        read_only = access_mode == ToolAccessMode.READ_ONLY
        concurrency_safe = read_only and _metadata_bool(spec, "concurrency_safe", default=True)
        mutates_workspace = access_mode in {ToolAccessMode.WORKSPACE_WRITE, ToolAccessMode.SHELL}
        conflict_key = _conflict_key(tool_name, arguments, access_mode)
        step_metadata = step.get("metadata") if isinstance(step.get("metadata"), Mapping) else {}
        metadata = {
            "worker_request_id": worker_request_id,
            "turn_index": str(turn_index),
            "step_index": str(step_index),
            "read_only": str(read_only).lower(),
            "access_mode": str(access_mode),
            "concurrency_safe": str(concurrency_safe).lower(),
            "conflict_key": conflict_key,
            **{str(key): str(value) for key, value in step_metadata.items()},
        }
        if spec is not None:
            metadata["tool_source"] = spec.source
            metadata.update({str(key): str(value) for key, value in spec.metadata.items()})
        call = ToolCall(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            tool_name=tool_name,
            arguments=arguments,
            tool_call_id=str(step.get("tool_call_id") or step.get("id") or new_id("toolcall")),
            metadata=metadata,
        )
        return ToolLoopRequest(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            worker_request_id=worker_request_id,
            turn_index=turn_index,
            step_index=step_index,
            tool_name=tool_name,
            arguments=arguments,
            call=call,
            access_mode=access_mode,
            read_only=read_only,
            concurrency_safe=concurrency_safe,
            mutates_workspace=mutates_workspace,
            conflict_key=conflict_key,
            source_path=str(spec.metadata.get("source_path") or "") if spec else "",
            schema_errors=schema_errors,
            metadata=metadata,
        )

    def _validate_step(self, tool_name: str, raw_arguments: Any, spec: ToolSpec | None) -> list[ToolSchemaViolation]:
        errors: list[ToolSchemaViolation] = []
        if not tool_name:
            errors.append(ToolSchemaViolation("tool_name", "tool_name is required", "non-empty string"))
        if spec is None:
            errors.append(ToolSchemaViolation("tool_name", f"unknown tool: {tool_name}", "registered tool"))
            return errors
        if not isinstance(raw_arguments, dict):
            errors.append(ToolSchemaViolation("arguments", "arguments must be an object", "object"))
            return errors
        schema = spec.input_schema if isinstance(spec.input_schema, dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for field_name in required:
            if field_name not in raw_arguments:
                errors.append(ToolSchemaViolation(str(field_name), "required field is missing", _schema_type(properties.get(field_name))))
        for field_name, value in raw_arguments.items():
            expected = _schema_type(properties.get(field_name))
            if expected and not _value_matches_type(value, expected):
                errors.append(ToolSchemaViolation(str(field_name), f"expected {expected}", expected))
        return errors

    def _partition(self, requests: list[ToolLoopRequest]) -> list[ToolLoopBatch]:
        batches: list[list[ToolLoopRequest]] = []
        seen_mutating_keys: set[str] = set()
        for request in requests:
            if not request.read_only and request.conflict_key:
                if request.conflict_key in seen_mutating_keys:
                    request = _with_request_metadata(request, {"conflict_protected": "true"})
                seen_mutating_keys.add(request.conflict_key)
            if request.read_only and request.concurrency_safe and batches and all(item.read_only and item.concurrency_safe for item in batches[-1]):
                if len(batches[-1]) < self.max_read_only_concurrency:
                    batches[-1].append(request)
                    continue
            batches.append([request])
        output: list[ToolLoopBatch] = []
        for index, batch_requests in enumerate(batches, start=1):
            read_only = all(request.read_only for request in batch_requests)
            mode = (
                ToolBatchExecutionMode.CONCURRENT_READ_ONLY
                if read_only and len(batch_requests) > 1
                else ToolBatchExecutionMode.SERIAL_READ_ONLY
                if read_only
                else ToolBatchExecutionMode.SERIAL_NON_READ_ONLY
            )
            output.append(
                ToolLoopBatch(
                    batch_index=index,
                    requests=batch_requests,
                    execution_mode=mode,
                    conflict_keys=sorted({request.conflict_key for request in batch_requests if request.conflict_key}),
                    conflict_protected=any(request.metadata.get("conflict_protected") == "true" for request in batch_requests),
                )
            )
        return output


class ToolResultBudgeter:
    def __init__(self, *, max_chars: int) -> None:
        self.max_chars = max(1, max_chars)

    def apply(
        self,
        *,
        request: ToolLoopRequest,
        result: ToolResult,
        artifact_store: LocalArtifactStore,
    ) -> tuple[ToolResult, ToolBudgetDecision]:
        payload = json.dumps(to_jsonable(result.output), ensure_ascii=False, sort_keys=True)
        if len(payload) <= self.max_chars:
            return result, ToolBudgetDecision(False, len(payload), self.max_chars)
        artifact = artifact_store.write_text(
            run_id=request.run_id,
            task_id=request.task_id,
            content=payload,
            title=f"tool_result:{request.tool_name}:{request.call.tool_call_id}",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".json",
            producer_node_id=request.node_id,
        )
        bounded = ToolResult(
            tool_call_id=result.tool_call_id,
            ok=result.ok,
            summary=f"{result.summary} (tool result output stored as artifact)",
            output={
                "truncated": True,
                "output_preview": payload[: self.max_chars],
                "original_chars": len(payload),
                "budget_chars": self.max_chars,
                "full_output_artifact_id": artifact.artifact_id,
            },
            artifacts=[*result.artifacts, artifact],
            error=result.error,
            completed_at=result.completed_at,
            metadata={
                **result.metadata,
                "tool_result_budget_applied": "true",
                "tool_result_original_chars": str(len(payload)),
                "tool_result_budget_chars": str(self.max_chars),
            },
        )
        return bounded, ToolBudgetDecision(
            True,
            len(payload),
            self.max_chars,
            preview_chars=self.max_chars,
            artifact_id=artifact.artifact_id,
            reason="tool_result_budget_exceeded",
        )


def tool_failure_signal_from_result(
    request: ToolLoopRequest,
    result: ToolResult,
    *,
    budget_decision: ToolBudgetDecision | None = None,
) -> ToolFailureSignal | None:
    if budget_decision is not None and budget_decision.applied:
        return ToolFailureSignal(
            signal_id=new_id("toolsignal"),
            tool_call_id=request.call.tool_call_id,
            tool_name=request.tool_name,
            kind=ToolFailureKind.BUDGET_EXCEEDED,
            severity=ToolSignalSeverity.WARNING,
            message="tool result exceeded inline budget and was externalized",
            retryable=False,
            watchdog_route="artifact_externalized",
            metadata={
                "artifact_id": budget_decision.artifact_id,
                "original_chars": str(budget_decision.original_chars),
                "budget_chars": str(budget_decision.budget_chars),
            },
        )
    if result.ok:
        return None
    kind = _failure_kind(result.error)
    severity = ToolSignalSeverity.ERROR
    retryable = kind in {ToolFailureKind.TIMEOUT, ToolFailureKind.RUNTIME_ERROR, ToolFailureKind.NON_ZERO_EXIT}
    if kind in {ToolFailureKind.PERMISSION_DENIED, ToolFailureKind.PERMISSION_REQUIRED, ToolFailureKind.SCHEMA_ERROR}:
        retryable = False
    return ToolFailureSignal(
        signal_id=new_id("toolsignal"),
        tool_call_id=request.call.tool_call_id,
        tool_name=request.tool_name,
        kind=kind,
        severity=severity,
        message=result.summary,
        retryable=retryable,
        watchdog_route=_watchdog_route(kind),
        metadata={
            "error": str(result.error or ""),
            "access_mode": str(request.access_mode),
            "conflict_key": request.conflict_key,
            **{str(key): str(value) for key, value in result.metadata.items()},
        },
    )


def watchdog_signal_payload(signal: ToolFailureSignal) -> dict[str, Any]:
    return {
        "watchdog_signal": {
            "signal_id": signal.signal_id,
            "tool_call_id": signal.tool_call_id,
            "tool_name": signal.tool_name,
            "kind": str(signal.kind),
            "severity": str(signal.severity),
            "retryable": signal.retryable,
            "route": signal.watchdog_route,
            "message": signal.message,
            "metadata": dict(signal.metadata),
        }
    }


def _access_mode(tool_name: str, arguments: Mapping[str, Any], spec: ToolSpec | None) -> ToolAccessMode:
    if spec is not None and spec.metadata.get("access_mode"):
        configured = spec.metadata["access_mode"]
        try:
            return ToolAccessMode(configured)
        except ValueError:
            pass
    if tool_name in {"file_read", "web_search", "browser"}:
        return ToolAccessMode.READ_ONLY
    if tool_name in {"checkpoint", "trace"}:
        return ToolAccessMode.ARTIFACT_WRITE if arguments.get("write_artifact") is True else ToolAccessMode.READ_ONLY
    if tool_name in {"file_write", "file_edit"}:
        return ToolAccessMode.WORKSPACE_WRITE
    if tool_name == "shell":
        return ToolAccessMode.SHELL
    if tool_name == "artifact_write":
        return ToolAccessMode.ARTIFACT_WRITE
    if spec is None:
        return ToolAccessMode.UNKNOWN
    return ToolAccessMode.CONTROL


def _conflict_key(tool_name: str, arguments: Mapping[str, Any], access_mode: ToolAccessMode) -> str:
    if tool_name in {"file_read", "file_write", "file_edit"}:
        path = str(arguments.get("path") or "")
        return f"{access_mode}:{Path(path).as_posix()}" if path else str(access_mode)
    if tool_name == "shell":
        return "shell:workspace"
    if access_mode == ToolAccessMode.ARTIFACT_WRITE:
        return "artifact:task"
    return str(access_mode)


def _metadata_bool(spec: ToolSpec | None, key: str, *, default: bool) -> bool:
    if spec is None:
        return default
    value = spec.metadata.get(key)
    if value is None:
        return default
    return str(value).lower() == "true"


def _schema_type(schema_fragment: Any) -> str:
    if not isinstance(schema_fragment, dict):
        return ""
    value = schema_fragment.get("type")
    return str(value) if value is not None else ""


def _value_matches_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected in {"integer", "number"}:
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def _with_request_metadata(request: ToolLoopRequest, metadata: Mapping[str, str]) -> ToolLoopRequest:
    merged = {**request.metadata, **dict(metadata)}
    call = ToolCall(
        run_id=request.call.run_id,
        task_id=request.call.task_id,
        node_id=request.call.node_id,
        tool_name=request.call.tool_name,
        arguments=request.call.arguments,
        tool_call_id=request.call.tool_call_id,
        created_at=request.call.created_at,
        metadata={**request.call.metadata, **dict(metadata)},
    )
    return ToolLoopRequest(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        worker_request_id=request.worker_request_id,
        turn_index=request.turn_index,
        step_index=request.step_index,
        tool_name=request.tool_name,
        arguments=request.arguments,
        call=call,
        access_mode=request.access_mode,
        read_only=request.read_only,
        concurrency_safe=request.concurrency_safe,
        mutates_workspace=request.mutates_workspace,
        conflict_key=request.conflict_key,
        source_path=request.source_path,
        schema_errors=request.schema_errors,
        metadata=merged,
    )


def _failure_kind(error: str | None) -> ToolFailureKind:
    normalized = str(error or "").lower()
    if normalized in {"schema_error", "unknown_tool"}:
        return ToolFailureKind.SCHEMA_ERROR if normalized == "schema_error" else ToolFailureKind.UNKNOWN_TOOL
    if normalized == "permission_denied":
        return ToolFailureKind.PERMISSION_DENIED
    if normalized == "permission_required":
        return ToolFailureKind.PERMISSION_REQUIRED
    if normalized in {"timeoutexpired", "tool_timeout", "timeout"}:
        return ToolFailureKind.TIMEOUT
    if normalized == "non_zero_exit":
        return ToolFailureKind.NON_ZERO_EXIT
    return ToolFailureKind.RUNTIME_ERROR


def _watchdog_route(kind: ToolFailureKind) -> str:
    if kind == ToolFailureKind.TIMEOUT:
        return "retry_or_background"
    if kind in {ToolFailureKind.PERMISSION_DENIED, ToolFailureKind.PERMISSION_REQUIRED}:
        return "permission_runtime"
    if kind == ToolFailureKind.SCHEMA_ERROR:
        return "repair_tool_arguments"
    if kind == ToolFailureKind.BUDGET_EXCEEDED:
        return "artifact_externalized"
    return "recovery_planner"
