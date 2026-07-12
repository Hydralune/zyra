from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from zyra_core import EventRecord, EventType, now_iso

from .prompt_queue import PromptQueueRuntime, RuntimeConcurrencyGuard
from .registry import ControlCommandRegistry
from .schemas import (
    CommandConcurrency,
    CommandMutationScope,
    CommandStatus,
    ControlCommandDescriptor,
    ControlCommandRequest,
    ControlCommandResponse,
    ControlError,
    ControlErrorCode,
    ControlResult,
    PromptQueueEntry,
    QueueEntryKind,
)
from .store import ControlRequestRecord, ControlRequestStore


SUPPORTED_PROTOCOL_VERSION = "zyra.control/v1"


class RuntimeControlHandler(Protocol):
    def __call__(
        self,
        request: ControlCommandRequest,
        descriptor: ControlCommandDescriptor,
        context: "RuntimeControlContext",
    ) -> ControlResult: ...


@dataclass(slots=True)
class RuntimeControlContext:
    """Live state-owner ports used by a command invocation.

    The dispatcher deliberately does not own task, permission, MCP, session,
    provider or subagent state.  A handler is registered only when that owner
    is available, which makes stateful commands fail closed instead of falling
    back to the pre-03D event-only acknowledgement path.
    """

    handlers: dict[str, RuntimeControlHandler] = field(default_factory=dict)
    session_revision: Callable[[str], int] = lambda _session_id: 0
    checkpoint: Callable[[ControlCommandRequest, ControlCommandDescriptor], str] | None = None
    permission_authorize: Callable[[ControlCommandRequest, ControlCommandDescriptor], bool] | None = None
    event_sink: Callable[[EventRecord], None] | None = None
    session_mode: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)

    def handler(self, handler_id: str) -> RuntimeControlHandler | None:
        return self.handlers.get(handler_id)


class RuntimeControlDispatcher:
    """Durable, idempotent control-command admission and dispatch runtime."""

    def __init__(
        self,
        *,
        registry: ControlCommandRegistry,
        request_store: ControlRequestStore,
        prompt_queue: PromptQueueRuntime,
        mutation_guard: RuntimeConcurrencyGuard | None = None,
        disabled: bool = False,
    ) -> None:
        self.registry = registry
        self.request_store = request_store
        self.prompt_queue = prompt_queue
        self.mutation_guard = mutation_guard or RuntimeConcurrencyGuard()
        self.disabled = disabled

    def submit(
        self,
        request: ControlCommandRequest,
        context: RuntimeControlContext,
        *,
        allow_queue: bool = True,
    ) -> ControlCommandResponse:
        self._require_enabled()
        received = self.request_store.receive(request)
        if received.status.terminal and received.response is not None:
            return received.response
        if received.request.request_id != request.request_id:
            if received.response is not None:
                return received.response
            return self._response_from_record(received)
        self._emit(request, EventType.COMMAND_REQUESTED, "received", context)

        validation = self._validate(request, context)
        if validation is not None:
            return self._finish_error(request, received, validation, context)
        descriptor = self.registry.require(request.canonical_name)
        current = self.request_store.transition(
            request.request_id,
            CommandStatus.VALIDATED,
            expected_revision=received.revision,
            metadata={
                "handler_id": descriptor.handler_id,
                "mutation_scope": descriptor.mutation_scope.value,
                "registry_generation": self.registry.generation,
            },
        )
        self._emit(request, EventType.COMMAND_VALIDATED, "validated", context, descriptor=descriptor)

        if self._must_queue(request, descriptor) and not self.mutation_guard.reserve(request.session_id, request.request_id):
            if not allow_queue:
                return self._finish_error(
                    request,
                    current,
                    ControlError(ControlErrorCode.REVISION_CONFLICT, "session is busy and command queueing is disabled", retryable=True),
                    context,
                )
            entry = self.prompt_queue.enqueue(
                PromptQueueEntry(
                    session_id=request.session_id,
                    kind=QueueEntryKind.CONTROL,
                    payload={"request": request.to_dict()},
                    priority=request.priority,
                    origin=request.origin,
                    target_subagent_task_id=request.target_subagent_task_id,
                    parse_slash=False,
                    is_meta=True,
                    idempotency_key=request.idempotency_key,
                    expected_revision=request.expected_session_revision,
                    metadata={"command_name": request.canonical_name},
                )
            )
            self.request_store.transition(
                request.request_id,
                CommandStatus.QUEUED,
                expected_revision=current.revision,
                queue_id=entry.queue_id,
            )
            self._emit(request, EventType.COMMAND_QUEUED, "queued", context, descriptor=descriptor, extra={"queue_id": entry.queue_id})
            return ControlCommandResponse(
                request_id=request.request_id,
                command_id=request.command_id,
                status=CommandStatus.QUEUED,
                registry_generation=self.registry.generation,
                result=ControlResult(
                    display_text=f"{request.canonical_name} queued",
                    followup_queue_id=entry.queue_id,
                    data={"queue": entry.safe_dict()},
                ),
                metadata={"durable": True, "executed": False},
            )

        guard_reserved = self._must_queue(request, descriptor)
        try:
            return self._execute(request, descriptor, context, current)
        finally:
            if guard_reserved:
                self.mutation_guard.release(request.session_id, request.request_id)

    def drain_one(
        self,
        *,
        session_id: str,
        context: RuntimeControlContext,
        target_subagent_task_id: str = "",
    ) -> ControlCommandResponse | None:
        self._require_enabled()
        entry = self.prompt_queue.reserve_next(
            session_id=session_id,
            target_subagent_task_id=target_subagent_task_id,
        )
        if entry is None:
            return None
        self.prompt_queue.start(entry.queue_id, entry.claim_token)
        request = ControlCommandRequest.from_dict(dict(entry.payload.get("request") or {}))
        if not self.mutation_guard.reserve(session_id, request.request_id):
            self.prompt_queue.fail(entry.queue_id, entry.claim_token, error="session mutation guard is busy")
            return self._finish_error(
                request,
                self.request_store.get(request.request_id),
                ControlError(ControlErrorCode.REVISION_CONFLICT, "session mutation guard is busy", retryable=True),
                context,
            )
        try:
            descriptor = self.registry.get(request.canonical_name)
            if descriptor is None:
                response = self._finish_error(
                    request,
                    self.request_store.get(request.request_id),
                    ControlError(ControlErrorCode.UNKNOWN_COMMAND, request.canonical_name),
                    context,
                )
                self.prompt_queue.fail(entry.queue_id, entry.claim_token, error=response.summary)
                return response
            record = self.request_store.transition(
                request.request_id,
                CommandStatus.DISPATCHED,
                metadata={"queue_id": entry.queue_id},
            )
            response = self._execute(request, descriptor, context, record, already_dispatched=True)
            if response.ok:
                self.prompt_queue.complete(entry.queue_id, entry.claim_token, metadata={"request_id": request.request_id})
            else:
                self.prompt_queue.fail(entry.queue_id, entry.claim_token, error=response.summary)
            return response
        finally:
            self.mutation_guard.release(session_id, request.request_id)

    def cancel(self, request_id: str, *, reason: str, context: RuntimeControlContext) -> ControlCommandResponse:
        record = self.request_store.get(request_id)
        if record.queue_id:
            self.prompt_queue.cancel(queue_id=record.queue_id)
        cancelled = self.request_store.cancel(request_id, reason=reason)
        self._emit(cancelled.request, EventType.COMMAND_CANCELLED, "cancelled", context, extra={"reason": reason})
        return cancelled.response or self._response_from_record(cancelled)

    def _execute(
        self,
        request: ControlCommandRequest,
        descriptor: ControlCommandDescriptor,
        context: RuntimeControlContext,
        record: ControlRequestRecord,
        *,
        already_dispatched: bool = False,
    ) -> ControlCommandResponse:
        revision_before = context.session_revision(request.session_id)
        if request.expected_session_revision is not None and request.expected_session_revision != revision_before:
            return self._finish_error(
                request,
                record,
                ControlError(
                    ControlErrorCode.REVISION_CONFLICT,
                    "session revision changed before command execution",
                    retryable=True,
                    details={"expected": request.expected_session_revision, "actual": revision_before},
                ),
                context,
            )
        if not already_dispatched:
            record = self.request_store.transition(request.request_id, CommandStatus.DISPATCHED, expected_revision=record.revision)
        checkpoint_ref = ""
        if descriptor.mutation_scope != CommandMutationScope.READ_ONLY:
            if context.checkpoint is None:
                return self._finish_error(
                    request,
                    record,
                    ControlError(ControlErrorCode.STATE_OWNER_UNAVAILABLE, "mutating command requires a checkpoint owner"),
                    context,
                )
            checkpoint_ref = context.checkpoint(request, descriptor)
            if not checkpoint_ref:
                return self._finish_error(
                    request,
                    record,
                    ControlError(ControlErrorCode.INVARIANT_VIOLATION, "checkpoint owner returned an empty checkpoint reference"),
                    context,
                )
        record = self.request_store.transition(request.request_id, CommandStatus.RUNNING, expected_revision=record.revision)
        if self._must_queue(request, descriptor):
            self.mutation_guard.start(request.session_id, request.request_id)
        self._emit(request, EventType.COMMAND_STARTED, "running", context, descriptor=descriptor, extra={"checkpoint_ref": checkpoint_ref})
        handler = context.handler(descriptor.handler_id)
        if handler is None:
            return self._finish_error(
                request,
                record,
                ControlError(
                    ControlErrorCode.STATE_OWNER_UNAVAILABLE,
                    f"state owner handler is unavailable: {descriptor.handler_id}",
                    details={"handler_id": descriptor.handler_id},
                ),
                context,
                checkpoint_ref=checkpoint_ref,
            )
        started_at = now_iso()
        try:
            result = handler(request, descriptor, context)
            if not isinstance(result, ControlResult):
                raise TypeError("control handler must return ControlResult")
            if checkpoint_ref and not result.checkpoint_ref:
                result = ControlResult(
                    display_text=result.display_text,
                    data=copy.deepcopy(result.data),
                    artifact_refs=result.artifact_refs,
                    checkpoint_ref=checkpoint_ref,
                    ui_surface=copy.deepcopy(result.ui_surface),
                    followup_queue_id=result.followup_queue_id,
                    usage=copy.deepcopy(result.usage),
                    metadata=copy.deepcopy(result.metadata),
                )
            revision_after = context.session_revision(request.session_id)
            response = ControlCommandResponse(
                request_id=request.request_id,
                command_id=request.command_id,
                status=CommandStatus.SUCCEEDED,
                registry_generation=self.registry.generation,
                result=result,
                revision_before=revision_before,
                revision_after=revision_after,
                started_at=started_at,
                metadata={"handler_id": descriptor.handler_id, "durable": True},
            )
            self.request_store.complete(request.request_id, response)
            self._emit(request, EventType.COMMAND_SUCCEEDED, "succeeded", context, descriptor=descriptor, extra={"revision_after": revision_after})
            return response
        except Exception as exc:
            return self._finish_error(
                request,
                self.request_store.get(request.request_id),
                ControlError(ControlErrorCode.HANDLER_FAILED, str(exc), details={"exception_type": type(exc).__name__}),
                context,
                checkpoint_ref=checkpoint_ref,
                revision_before=revision_before,
                started_at=started_at,
            )

    def _validate(self, request: ControlCommandRequest, context: RuntimeControlContext) -> ControlError | None:
        if request.protocol_version != SUPPORTED_PROTOCOL_VERSION:
            return ControlError(ControlErrorCode.UNSUPPORTED_VERSION, f"unsupported control protocol: {request.protocol_version}")
        descriptor = self.registry.get(request.canonical_name)
        if descriptor is None:
            return ControlError(ControlErrorCode.UNKNOWN_COMMAND, f"unknown command: {request.canonical_name}")
        if request.registry_generation not in {0, self.registry.generation}:
            return ControlError(
                ControlErrorCode.REGISTRY_STALE,
                "command registry changed after request construction",
                retryable=True,
                details={"request_generation": request.registry_generation, "current_generation": self.registry.generation},
            )
        if not descriptor.availability.enabled:
            return ControlError(ControlErrorCode.COMMAND_DISABLED, descriptor.availability.reason or "command is disabled")
        if not descriptor.availability.allows(origin=request.origin, session_mode=context.session_mode):
            return ControlError(ControlErrorCode.COMMAND_UNAVAILABLE, "command is unavailable in this session mode or origin")
        if not descriptor.exposure.allows(request.origin):
            return ControlError(ControlErrorCode.UNSAFE_ORIGIN, f"command is not exposed to {request.origin.value}")
        argument_error = _validate_arguments(request.arguments, descriptor.argument_schema)
        if argument_error:
            return ControlError(ControlErrorCode.ARGUMENT_INVALID, argument_error)
        if descriptor.mutation_scope != CommandMutationScope.READ_ONLY:
            if context.permission_authorize is None:
                return ControlError(ControlErrorCode.PERMISSION_DENIED, "mutating command requires an explicit permission authority")
            if not context.permission_authorize(request, descriptor):
                return ControlError(ControlErrorCode.PERMISSION_DENIED, f"permission denied for {descriptor.permission_action}")
        return None

    @staticmethod
    def _must_queue(request: ControlCommandRequest, descriptor: ControlCommandDescriptor) -> bool:
        return descriptor.concurrency != CommandConcurrency.READ_ONLY_PARALLEL or bool(request.target_subagent_task_id)

    def _finish_error(
        self,
        request: ControlCommandRequest,
        record: ControlRequestRecord,
        error: ControlError,
        context: RuntimeControlContext,
        *,
        checkpoint_ref: str = "",
        revision_before: int | None = None,
        started_at: str = "",
    ) -> ControlCommandResponse:
        status = CommandStatus.DENIED if error.code == ControlErrorCode.PERMISSION_DENIED else CommandStatus.CONFLICT if error.code in {ControlErrorCode.DUPLICATE_CONFLICT, ControlErrorCode.REVISION_CONFLICT, ControlErrorCode.REGISTRY_STALE} else CommandStatus.FAILED
        response = ControlCommandResponse(
            request_id=request.request_id,
            command_id=request.command_id,
            status=status,
            registry_generation=self.registry.generation,
            result=ControlResult(checkpoint_ref=checkpoint_ref),
            error=error,
            revision_before=revision_before,
            revision_after=context.session_revision(request.session_id),
            started_at=started_at or record.started_at,
            metadata={"durable": True, "event_only_fallback": False},
        )
        if record.status == CommandStatus.CONFLICT and record.response is not None:
            return record.response
        if not record.status.terminal:
            self.request_store.complete(request.request_id, response)
        event_type = EventType.COMMAND_CANCELLED if status == CommandStatus.CANCELLED else EventType.COMMAND_FAILED
        self._emit(request, event_type, status.value, context, extra={"error": error.to_dict()})
        return response

    @staticmethod
    def _response_from_record(record: ControlRequestRecord) -> ControlCommandResponse:
        if record.response is not None:
            return record.response
        return ControlCommandResponse(
            request_id=record.request.request_id,
            command_id=record.request.command_id,
            status=record.status,
            registry_generation=record.request.registry_generation,
            metadata={"replayed": True},
        )

    @staticmethod
    def _emit(
        request: ControlCommandRequest,
        event_type: EventType,
        phase: str,
        context: RuntimeControlContext,
        *,
        descriptor: ControlCommandDescriptor | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        if context.event_sink is None:
            return
        context.event_sink(EventRecord(
            run_id=request.run_id,
            task_id=request.task_id,
            event_type=event_type,
            payload={
                "schema": "zyra.control-command-lifecycle/v1",
                "phase": phase,
                "request_id": request.request_id,
                "command_id": request.command_id,
                "command": request.canonical_name,
                "session_id": request.session_id,
                "target_subagent_task_id": request.target_subagent_task_id,
                "handler_id": descriptor.handler_id if descriptor else "",
                "origin": request.origin.value,
                **copy.deepcopy(dict(extra or {})),
            },
        ))

    def _require_enabled(self) -> None:
        if self.disabled:
            raise RuntimeError("RuntimeControlDispatcher is disabled")


def _validate_arguments(arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> str:
    if str(schema.get("type") or "object") != "object":
        return "only object command argument schemas are supported"
    required = tuple(str(item) for item in schema.get("required") or ())
    for key in required:
        if key not in arguments:
            return f"missing required argument: {key}"
    properties = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
    for key, value in arguments.items():
        rule = properties.get(key)
        if not isinstance(rule, Mapping):
            continue
        expected = str(rule.get("type") or "")
        if expected == "string" and not isinstance(value, str):
            return f"argument {key} must be a string"
        if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            return f"argument {key} must be an integer"
        if expected == "boolean" and not isinstance(value, bool):
            return f"argument {key} must be a boolean"
        if expected == "array" and not isinstance(value, (list, tuple)):
            return f"argument {key} must be an array"
        if expected == "object" and not isinstance(value, Mapping):
            return f"argument {key} must be an object"
    return ""
