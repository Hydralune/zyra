from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Sequence
from urllib.parse import urlparse

from zyra_scheduler.backend_registry.models import (
    BackendDispatchEnvelope,
    BackendFailureKind,
    BackendKind,
    BackendLocation,
    BackendRecoveryIntent,
    canonical_json,
    checksum,
)
from zyra_scheduler.backend_registry.transport import BackendTransportFrame


class RemoteDispatchState(str, Enum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECONCILE_REQUIRED = "reconcile_required"


@dataclass(frozen=True, slots=True)
class BackendServiceConfig:
    backend_id: str
    runtime_worker: str
    host: str = "127.0.0.1"
    port: int = 0
    maximum_concurrency: int = 4
    maximum_request_bytes: int = 16 * 1024 * 1024
    maximum_result_bytes: int = 32 * 1024 * 1024
    request_poll_seconds: float = 0.02
    shutdown_timeout_seconds: float = 10.0
    expose_error_tracebacks: bool = False
    capabilities: tuple[str, ...] = ()
    workspace_roots: tuple[str, ...] = ()
    artifact_roots: tuple[str, ...] = ()
    generation: str = field(default_factory=lambda: f"backend_generation_{uuid.uuid4().hex}")

    def __post_init__(self) -> None:
        if not self.backend_id.strip():
            raise ValueError("backend service id is required")
        if not self.runtime_worker.strip():
            raise ValueError("backend service runtime worker is required")
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("embedded backend service may bind only to loopback")
        if self.port < 0 or self.port > 65535:
            raise ValueError("backend service port is invalid")
        if self.maximum_concurrency < 1:
            raise ValueError("backend service concurrency must be positive")
        if self.maximum_request_bytes < 1024:
            raise ValueError("backend service request budget is too small")
        if self.maximum_result_bytes < 1024:
            raise ValueError("backend service result budget is too small")


@dataclass(frozen=True, slots=True)
class RemoteDispatchRequest:
    envelope: BackendDispatchEnvelope
    operation: str
    payload: Mapping[str, Any]
    input_digest: str
    headers: Mapping[str, str]

    @classmethod
    def from_wire(
        cls,
        value: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> RemoteDispatchRequest:
        if value.get("schema") != "zyra.backend-transport-request/v1":
            raise RemoteDispatchRejected(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "backend_protocol",
                "unsupported backend transport request schema",
            )
        raw_envelope = value.get("envelope")
        if not isinstance(raw_envelope, Mapping):
            raise RemoteDispatchRejected(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "backend_protocol",
                "backend dispatch envelope is required",
            )
        envelope = decode_envelope(raw_envelope)
        operation = str(value.get("operation") or "")
        if not operation.strip():
            raise RemoteDispatchRejected(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "backend_protocol",
                "backend operation is required",
            )
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            raise RemoteDispatchRejected(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "backend_protocol",
                "backend operation payload must be an object",
            )
        input_digest = str(value.get("input_digest") or "")
        expected_digest = checksum(
            {
                "m0_execution_ref": envelope.m0_execution_ref,
                "operation": operation,
                "payload": dict(payload),
            }
        )
        if input_digest != expected_digest:
            raise RemoteDispatchRejected(
                HTTPStatus.CONFLICT,
                "backend_protocol",
                "backend request input digest mismatch",
            )
        return cls(
            envelope=envelope,
            operation=operation,
            payload=dict(payload),
            input_digest=input_digest,
            headers=safe_request_headers(headers or {}),
        )


@dataclass(frozen=True, slots=True)
class RemoteOperationContext:
    envelope: BackendDispatchEnvelope
    cancellation: threading.Event
    deadline_at: float
    emit: Callable[[str, Mapping[str, Any], bool], BackendTransportFrame]

    @property
    def cancelled(self) -> bool:
        return self.cancellation.is_set()

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_at - time.time())

    def check_cancelled(self) -> None:
        if self.cancelled:
            raise RemoteOperationCancelled("remote backend operation was cancelled")
        if self.remaining_seconds() <= 0:
            self.cancellation.set()
            raise RemoteOperationTimedOut("remote backend operation exceeded turn deadline")


@dataclass(frozen=True, slots=True)
class RemoteDispatchResult:
    result: Mapping[str, Any]
    frames: tuple[BackendTransportFrame, ...]
    state: RemoteDispatchState
    started_at: float
    completed_at: float
    output_observed: bool
    result_digest: str
    receipt_id: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema": "zyra.backend-transport-response/v1",
            "status": self.state.value,
            "result": dict(self.result),
            "frames": [frame.to_wire() for frame in self.frames],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "output_observed": self.output_observed,
            "result_digest": self.result_digest,
            "transport_receipt_id": self.receipt_id,
        }


@dataclass(slots=True)
class RemoteDispatchRecord:
    dispatch_id: str
    idempotency_key: str
    envelope_id: str
    run_id: str
    task_id: str
    turn_id: str
    provider_route_id: str
    provider_route_checksum: str
    m0_execution_ref: str
    deadline_at: float
    state: RemoteDispatchState
    accepted_at: float
    updated_at: float
    cancellation: threading.Event
    future: Future[RemoteDispatchResult] | None = None
    result: RemoteDispatchResult | None = None
    error: Mapping[str, Any] | None = None
    output_observed: bool = False
    revision: int = 1

    def safe_dict(self) -> dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "idempotency_key": self.idempotency_key,
            "envelope_id": self.envelope_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "turn_id": self.turn_id,
            "provider_route_id": self.provider_route_id,
            "provider_route_checksum": self.provider_route_checksum,
            "m0_execution_ref": self.m0_execution_ref,
            "deadline_at": self.deadline_at,
            "state": self.state.value,
            "accepted_at": self.accepted_at,
            "updated_at": self.updated_at,
            "output_observed": self.output_observed,
            "revision": self.revision,
            "result_digest": None if self.result is None else self.result.result_digest,
            "error": None if self.error is None else dict(self.error),
        }


class RemoteDispatchRejected(RuntimeError):
    def __init__(
        self,
        status: HTTPStatus,
        kind: str,
        message: str,
        *,
        retryable: bool = False,
        recovery_intent: BackendRecoveryIntent = BackendRecoveryIntent.NONE,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.retryable = retryable
        self.recovery_intent = recovery_intent
        self.detail = dict(detail or {})

    def safe_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": str(self),
            "retryable": self.retryable,
            "recovery_intent": self.recovery_intent.value,
            "detail": dict(self.detail),
        }


class RemoteOperationCancelled(RuntimeError):
    pass


class RemoteOperationTimedOut(TimeoutError):
    pass


RemoteOperation = Callable[[RemoteOperationContext, Mapping[str, Any]], Mapping[str, Any] | Any]


class BackendOperationRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._operations: dict[str, RemoteOperation] = {}

    def register(self, name: str, operation: RemoteOperation, *, replace: bool = False) -> None:
        normalized = normalize_operation_name(name)
        if not callable(operation):
            raise TypeError("backend operation must be callable")
        with self._lock:
            if normalized in self._operations and not replace:
                raise ValueError(f"backend operation already registered: {normalized}")
            self._operations[normalized] = operation

    def unregister(self, name: str) -> bool:
        normalized = normalize_operation_name(name)
        with self._lock:
            return self._operations.pop(normalized, None) is not None

    def require(self, name: str) -> RemoteOperation:
        normalized = normalize_operation_name(name)
        with self._lock:
            operation = self._operations.get(normalized)
        if operation is None:
            raise RemoteDispatchRejected(
                HTTPStatus.NOT_FOUND,
                "operation_not_found",
                f"backend operation is not registered: {normalized}",
            )
        return operation

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._operations))


class BackendDispatchServiceRuntime:
    def __init__(
        self,
        config: BackendServiceConfig,
        operations: BackendOperationRegistry,
    ) -> None:
        self.config = config
        self.operations = operations
        self._lock = threading.RLock()
        self._capacity = threading.BoundedSemaphore(config.maximum_concurrency)
        self._executor = ThreadPoolExecutor(
            max_workers=config.maximum_concurrency,
            thread_name_prefix=f"zyra-{config.backend_id}",
        )
        self._records: dict[str, RemoteDispatchRecord] = {}
        self._idempotency: dict[str, str] = {}
        self._accepting = True
        self._started_at = time.time()

    def health(self) -> dict[str, Any]:
        with self._lock:
            active = sum(
                1
                for record in self._records.values()
                if record.state in {
                    RemoteDispatchState.ACCEPTED,
                    RemoteDispatchState.RUNNING,
                    RemoteDispatchState.CANCELLING,
                }
            )
            terminal = len(self._records) - active
            accepting = self._accepting
        return {
            "ok": accepting,
            "schema": "zyra.backend-dispatch-service-health/v1",
            "backend_id": self.config.backend_id,
            "runtime_worker": self.config.runtime_worker,
            "generation": self.config.generation,
            "capabilities": list(self.config.capabilities),
            "operations": list(self.operations.names()),
            "maximum_concurrency": self.config.maximum_concurrency,
            "active_dispatches": active,
            "terminal_dispatches": terminal,
            "accepting": accepting,
            "started_at": self._started_at,
            "checked_at": time.time(),
        }

    def submit(self, request: RemoteDispatchRequest) -> RemoteDispatchRecord:
        self._validate_request(request)
        operation = self.operations.require(request.operation)
        with self._lock:
            if not self._accepting:
                raise RemoteDispatchRejected(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    BackendFailureKind.BACKEND_UNAVAILABLE.value,
                    "backend service is draining",
                    retryable=True,
                    recovery_intent=BackendRecoveryIntent.CHANGE_BACKEND,
                )
            existing_id = self._idempotency.get(request.envelope.idempotency_key)
            if existing_id is not None:
                existing = self._records[existing_id]
                if existing.envelope_id != request.envelope.envelope_id:
                    raise RemoteDispatchRejected(
                        HTTPStatus.CONFLICT,
                        BackendFailureKind.LEASE_CONFLICT.value,
                        "idempotency key was reused with another envelope",
                    )
                return existing
            if not self._capacity.acquire(blocking=False):
                raise RemoteDispatchRejected(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    BackendFailureKind.BACKEND_CAPACITY.value,
                    "backend service concurrency is exhausted",
                    retryable=True,
                    recovery_intent=BackendRecoveryIntent.CHANGE_BACKEND,
                )
            now = time.time()
            record = RemoteDispatchRecord(
                dispatch_id=f"remote_dispatch_{uuid.uuid4().hex}",
                idempotency_key=request.envelope.idempotency_key,
                envelope_id=request.envelope.envelope_id,
                run_id=request.envelope.run_id,
                task_id=request.envelope.task_id,
                turn_id=request.envelope.turn_id,
                provider_route_id=request.envelope.provider_route_id or "",
                provider_route_checksum=request.envelope.provider_route_checksum,
                m0_execution_ref=request.envelope.m0_execution_ref,
                deadline_at=request.envelope.deadline_at,
                state=RemoteDispatchState.ACCEPTED,
                accepted_at=now,
                updated_at=now,
                cancellation=threading.Event(),
            )
            self._records[record.dispatch_id] = record
            self._idempotency[record.idempotency_key] = record.dispatch_id
            record.future = self._executor.submit(self._execute, record, request, operation)
            return record

    def await_result(self, dispatch_id: str) -> RemoteDispatchResult:
        record = self.require(dispatch_id)
        future = record.future
        if future is None:
            raise RuntimeError("remote dispatch future was not created")
        remaining = max(0.001, record_deadline(record, self.config.request_poll_seconds) - time.time())
        try:
            return future.result(timeout=remaining)
        except FutureTimeoutError as error:
            self.cancel(dispatch_id, "remote service wait exceeded envelope deadline")
            raise RemoteDispatchRejected(
                HTTPStatus.GATEWAY_TIMEOUT,
                BackendFailureKind.BACKEND_TIMEOUT.value,
                "remote backend turn timed out",
                retryable=not record.output_observed,
                recovery_intent=(
                    BackendRecoveryIntent.RECONCILE
                    if record.output_observed
                    else BackendRecoveryIntent.CHANGE_BACKEND
                ),
                detail={"dispatch_id": dispatch_id, "output_observed": record.output_observed},
            ) from error

    def dispatch(self, request: RemoteDispatchRequest) -> RemoteDispatchResult:
        record = self.submit(request)
        if record.result is not None:
            return record.result
        if record.error is not None:
            raise rejected_from_record(record)
        return self.await_result(record.dispatch_id)

    def cancel(self, dispatch_id: str, reason: str) -> RemoteDispatchRecord:
        if not reason.strip():
            raise ValueError("remote dispatch cancel reason is required")
        with self._lock:
            record = self.require(dispatch_id)
            if record.state in {
                RemoteDispatchState.SUCCEEDED,
                RemoteDispatchState.FAILED,
                RemoteDispatchState.CANCELLED,
                RemoteDispatchState.RECONCILE_REQUIRED,
            }:
                return record
            record.state = RemoteDispatchState.CANCELLING
            record.updated_at = time.time()
            record.revision += 1
            record.cancellation.set()
            return record

    def cancel_by_envelope(self, envelope_id: str, reason: str) -> RemoteDispatchRecord:
        with self._lock:
            record = next(
                (item for item in self._records.values() if item.envelope_id == envelope_id),
                None,
            )
        if record is None:
            raise KeyError(f"remote dispatch envelope not found: {envelope_id}")
        return self.cancel(record.dispatch_id, reason)

    def require(self, dispatch_id: str) -> RemoteDispatchRecord:
        with self._lock:
            record = self._records.get(dispatch_id)
        if record is None:
            raise KeyError(f"remote dispatch not found: {dispatch_id}")
        return record

    def records(
        self,
        *,
        run_id: str = "",
        task_id: str = "",
        active_only: bool = False,
    ) -> tuple[RemoteDispatchRecord, ...]:
        with self._lock:
            records = tuple(self._records.values())
        active_states = {
            RemoteDispatchState.ACCEPTED,
            RemoteDispatchState.RUNNING,
            RemoteDispatchState.CANCELLING,
        }
        return tuple(
            record
            for record in records
            if (not run_id or record.run_id == run_id)
            and (not task_id or record.task_id == task_id)
            and (not active_only or record.state in active_states)
        )

    def drain(self, reason: str = "backend service drain requested") -> tuple[RemoteDispatchRecord, ...]:
        with self._lock:
            self._accepting = False
            active_ids = [
                record.dispatch_id
                for record in self._records.values()
                if record.state in {
                    RemoteDispatchState.ACCEPTED,
                    RemoteDispatchState.RUNNING,
                    RemoteDispatchState.CANCELLING,
                }
            ]
        for dispatch_id in active_ids:
            self.cancel(dispatch_id, reason)
        return self.records(active_only=True)

    def resume(self) -> None:
        with self._lock:
            self._accepting = True

    def close(self) -> None:
        self.drain("backend service shutdown")
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _execute(
        self,
        record: RemoteDispatchRecord,
        request: RemoteDispatchRequest,
        operation: RemoteOperation,
    ) -> RemoteDispatchResult:
        started = time.time()
        frames: list[BackendTransportFrame] = []

        def emit(kind: str, payload: Mapping[str, Any], output_observed: bool) -> BackendTransportFrame:
            frame = create_frame(len(frames) + 1, kind, payload, output_observed=output_observed)
            frames.append(frame)
            with self._lock:
                record.output_observed = record.output_observed or output_observed
                record.updated_at = time.time()
                record.revision += 1
            return frame

        context = RemoteOperationContext(
            envelope=request.envelope,
            cancellation=record.cancellation,
            deadline_at=request.envelope.deadline_at,
            emit=emit,
        )
        with self._lock:
            record.state = RemoteDispatchState.RUNNING
            record.updated_at = started
            record.revision += 1
        try:
            context.check_cancelled()
            value = operation(context, request.payload)
            context.check_cancelled()
            result = json_result(value)
            encoded = canonical_json(result).encode("utf-8")
            if len(encoded) > self.config.maximum_result_bytes:
                raise RemoteDispatchRejected(
                    HTTPStatus.INSUFFICIENT_STORAGE,
                    BackendFailureKind.BACKEND_PROTOCOL.value,
                    "backend result exceeded response byte budget",
                )
            if not frames or frames[-1].kind != "result":
                emit("result", result, True)
            completed = time.time()
            response = RemoteDispatchResult(
                result=result,
                frames=tuple(frames),
                state=RemoteDispatchState.SUCCEEDED,
                started_at=started,
                completed_at=completed,
                output_observed=any(frame.output_observed for frame in frames),
                result_digest=checksum(result),
                receipt_id=f"remote_receipt_{uuid.uuid4().hex}",
            )
            with self._lock:
                record.state = RemoteDispatchState.SUCCEEDED
                record.result = response
                record.output_observed = response.output_observed
                record.updated_at = completed
                record.revision += 1
            return response
        except RemoteOperationCancelled as error:
            return self._terminal_error(record, frames, started, error, cancelled=True)
        except RemoteOperationTimedOut as error:
            return self._terminal_error(record, frames, started, error, timed_out=True)
        except BaseException as error:
            if isinstance(error, RemoteDispatchRejected):
                rejected = error
            else:
                rejected = RemoteDispatchRejected(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    BackendFailureKind.EXECUTION_FAILED.value,
                    str(error) or type(error).__name__,
                    retryable=False,
                    recovery_intent=(
                        BackendRecoveryIntent.RECONCILE
                        if record.output_observed
                        else BackendRecoveryIntent.NONE
                    ),
                    detail={
                        "exception_type": type(error).__name__,
                        **(
                            {"traceback": traceback.format_exc(limit=20)}
                            if self.config.expose_error_tracebacks
                            else {}
                        ),
                    },
                )
            self._store_error(record, rejected)
            raise rejected
        finally:
            self._capacity.release()

    def _terminal_error(
        self,
        record: RemoteDispatchRecord,
        frames: Sequence[BackendTransportFrame],
        started: float,
        error: BaseException,
        *,
        cancelled: bool = False,
        timed_out: bool = False,
    ) -> RemoteDispatchResult:
        observed = record.output_observed or any(frame.output_observed for frame in frames)
        state = (
            RemoteDispatchState.RECONCILE_REQUIRED
            if observed
            else RemoteDispatchState.CANCELLED
            if cancelled
            else RemoteDispatchState.FAILED
        )
        failure_kind = (
            BackendFailureKind.DISPATCH_ABORTED
            if cancelled
            else BackendFailureKind.BACKEND_TIMEOUT
            if timed_out
            else BackendFailureKind.EXECUTION_FAILED
        )
        recovery = (
            BackendRecoveryIntent.RECONCILE
            if observed
            else BackendRecoveryIntent.CHANGE_BACKEND
            if timed_out
            else BackendRecoveryIntent.NONE
        )
        frame = create_frame(
            len(frames) + 1,
            "error",
            {
                "kind": failure_kind.value,
                "message": str(error),
                "retryable": timed_out and not observed,
                "recovery_intent": recovery.value,
                "output_observed": observed,
            },
            output_observed=observed,
        )
        result = {
            "ok": False,
            "failure_kind": failure_kind.value,
            "recovery_intent": recovery.value,
            "output_observed": observed,
        }
        response = RemoteDispatchResult(
            result=result,
            frames=tuple([*frames, frame]),
            state=state,
            started_at=started,
            completed_at=time.time(),
            output_observed=observed,
            result_digest=checksum(result),
            receipt_id=f"remote_receipt_{uuid.uuid4().hex}",
        )
        with self._lock:
            record.state = state
            record.result = response
            record.output_observed = observed
            record.updated_at = response.completed_at
            record.revision += 1
        return response

    def _store_error(self, record: RemoteDispatchRecord, error: RemoteDispatchRejected) -> None:
        with self._lock:
            record.state = (
                RemoteDispatchState.RECONCILE_REQUIRED
                if record.output_observed
                else RemoteDispatchState.FAILED
            )
            record.error = error.safe_dict()
            record.updated_at = time.time()
            record.revision += 1

    def _validate_request(self, request: RemoteDispatchRequest) -> None:
        envelope = request.envelope
        if envelope.backend_id != self.config.backend_id:
            raise RemoteDispatchRejected(
                HTTPStatus.CONFLICT,
                BackendFailureKind.LEASE_CONFLICT.value,
                "dispatch envelope targets another backend",
            )
        if envelope.runtime_worker != self.config.runtime_worker:
            raise RemoteDispatchRejected(
                HTTPStatus.CONFLICT,
                BackendFailureKind.BACKEND_PROTOCOL.value,
                "dispatch envelope runtime worker mismatch",
            )
        if envelope.deadline_at <= time.time():
            raise RemoteDispatchRejected(
                HTTPStatus.GATEWAY_TIMEOUT,
                BackendFailureKind.TURN_TIMEOUT.value,
                "dispatch envelope deadline already elapsed",
                retryable=True,
                recovery_intent=BackendRecoveryIntent.CHANGE_BACKEND,
            )
        validate_root(envelope.workspace_root, self.config.workspace_roots, "workspace")
        validate_root(envelope.artifact_root, self.config.artifact_roots, "artifact")


class BackendDispatchHttpServer:
    def __init__(
        self,
        runtime: BackendDispatchServiceRuntime,
    ) -> None:
        self.runtime = runtime
        handler = build_http_handler(runtime)
        self._server = ThreadingHTTPServer(
            (runtime.config.host, runtime.config.port),
            handler,
        )
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def endpoint(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> BackendDispatchHttpServer:
        if self._thread is not None and self._thread.is_alive():
            return self
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"zyra-backend-http-{self.runtime.config.backend_id}",
            daemon=True,
        )
        self._thread.start()
        wait_for_server(self._server.server_address, timeout=5.0)
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=self.runtime.config.shutdown_timeout_seconds)
        self.runtime.close()

    def __enter__(self) -> BackendDispatchHttpServer:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.close()


def build_http_handler(runtime: BackendDispatchServiceRuntime) -> type[BaseHTTPRequestHandler]:
    class BackendHandler(BaseHTTPRequestHandler):
        server_version = "ZyraBackendDispatch/1"

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/health":
                self._json(HTTPStatus.OK, runtime.health())
                return
            if path == "/v1/dispatches":
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "dispatches": [record.safe_dict() for record in runtime.records()],
                    },
                )
                return
            if path.startswith("/v1/dispatches/"):
                dispatch_id = path.rsplit("/", 1)[-1]
                try:
                    record = runtime.require(dispatch_id)
                except KeyError as error:
                    self._error(HTTPStatus.NOT_FOUND, "dispatch_not_found", str(error))
                else:
                    self._json(HTTPStatus.OK, {"ok": True, "dispatch": record.safe_dict()})
                return
            self._error(HTTPStatus.NOT_FOUND, "route_not_found", "backend service route not found")

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                body = self._read_body()
                if path == "/v1/dispatch":
                    request = RemoteDispatchRequest.from_wire(body, headers=dict(self.headers.items()))
                    response = runtime.dispatch(request)
                    self._json(HTTPStatus.OK, response.to_wire())
                    return
                if path.startswith("/v1/dispatches/") and path.endswith("/cancel"):
                    dispatch_id = path.split("/")[-2]
                    record = runtime.cancel(
                        dispatch_id,
                        str(body.get("reason") or "remote cancel API request"),
                    )
                    self._json(HTTPStatus.ACCEPTED, {"ok": True, "dispatch": record.safe_dict()})
                    return
                if path.startswith("/v1/envelopes/") and path.endswith("/cancel"):
                    envelope_id = path.split("/")[-2]
                    record = runtime.cancel_by_envelope(
                        envelope_id,
                        str(body.get("reason") or "remote envelope cancel API request"),
                    )
                    self._json(HTTPStatus.ACCEPTED, {"ok": True, "dispatch": record.safe_dict()})
                    return
                if path == "/v1/control/drain":
                    records = runtime.drain(str(body.get("reason") or "remote drain API request"))
                    self._json(
                        HTTPStatus.ACCEPTED,
                        {"ok": True, "active_dispatches": [record.safe_dict() for record in records]},
                    )
                    return
                if path == "/v1/control/resume":
                    runtime.resume()
                    self._json(HTTPStatus.OK, runtime.health())
                    return
                self._error(HTTPStatus.NOT_FOUND, "route_not_found", "backend service route not found")
            except RemoteDispatchRejected as error:
                self._json(error.status, {"ok": False, "error": error.safe_dict()})
            except KeyError as error:
                self._error(HTTPStatus.NOT_FOUND, "dispatch_not_found", str(error))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                self._error(HTTPStatus.BAD_REQUEST, "invalid_request", str(error))

        def _read_body(self) -> dict[str, Any]:
            raw_length = self.headers.get("content-length")
            if raw_length is None:
                raise ValueError("content-length header is required")
            length = int(raw_length)
            if length < 0 or length > runtime.config.maximum_request_bytes:
                raise RemoteDispatchRejected(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "request_too_large",
                    "backend request exceeded byte budget",
                )
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("backend request body ended early")
            value = json.loads(raw.decode("utf-8", errors="strict"))
            if not isinstance(value, Mapping):
                raise ValueError("backend request body must be an object")
            return dict(value)

        def _error(self, status: HTTPStatus, kind: str, message: str) -> None:
            self._json(status, {"ok": False, "error": {"kind": kind, "message": message}})

        def _json(self, status: HTTPStatus, value: Mapping[str, Any]) -> None:
            body = canonical_json(value).encode("utf-8")
            self.send_response(int(status))
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.send_header("x-zyra-backend-generation", runtime.config.generation)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return BackendHandler


def decode_envelope(value: Mapping[str, Any]) -> BackendDispatchEnvelope:
    body = dict(value)
    supplied = str(body.pop("checksum", ""))
    if not supplied or checksum(body) != supplied:
        raise RemoteDispatchRejected(
            HTTPStatus.CONFLICT,
            BackendFailureKind.BACKEND_PROTOCOL.value,
            "backend dispatch envelope checksum mismatch",
        )
    if body.get("schema") != "zyra.backend-dispatch-envelope/v2":
        raise RemoteDispatchRejected(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            BackendFailureKind.BACKEND_PROTOCOL.value,
            "unsupported backend dispatch envelope schema",
        )
    required_refs = {
        "provider_route_id": body.get("provider_route_id"),
        "provider_route_checksum": body.get("provider_route_checksum"),
        "provider_credential_fingerprint": body.get("provider_credential_fingerprint"),
        "provider_transport_id": body.get("provider_transport_id"),
        "m0_execution_ref": body.get("m0_execution_ref"),
    }
    missing = sorted(name for name, item in required_refs.items() if not str(item or "").strip())
    if int(body.get("provider_catalog_revision") or 0) <= 0:
        missing.append("provider_catalog_revision")
    if int(body.get("provider_credential_version") or 0) <= 0:
        missing.append("provider_credential_version")
    if missing:
        raise RemoteDispatchRejected(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            BackendFailureKind.BACKEND_PROTOCOL.value,
            "backend envelope is missing route references",
            detail={"missing_refs": missing},
        )
    try:
        return BackendDispatchEnvelope(
            schema=str(body["schema"]),
            envelope_id=str(body["envelope_id"]),
            run_id=str(body["run_id"]),
            task_id=str(body["task_id"]),
            node_id=(str(body["node_id"]) if body.get("node_id") else None),
            turn_id=str(body["turn_id"]),
            runtime_worker=str(body["runtime_worker"]),
            backend_lease_id=str(body["backend_lease_id"]),
            backend_id=str(body["backend_id"]),
            backend_kind=BackendKind(str(body["backend_kind"])),
            backend_location=BackendLocation(str(body["backend_location"])),
            workspace_root=str(body["workspace_root"]),
            artifact_root=str(body["artifact_root"]),
            provider_route_id=str(body["provider_route_id"]),
            provider_route_checksum=str(body["provider_route_checksum"]),
            provider_catalog_revision=int(body["provider_catalog_revision"]),
            provider_credential_version=int(body["provider_credential_version"]),
            provider_credential_fingerprint=str(body["provider_credential_fingerprint"]),
            provider_transport_id=str(body["provider_transport_id"]),
            m0_execution_ref=str(body["m0_execution_ref"]),
            physical_worker_lease_ref=(
                str(body["physical_worker_lease_ref"])
                if body.get("physical_worker_lease_ref")
                else None
            ),
            idempotency_key=str(body["idempotency_key"]),
            deadline_at=float(body["deadline_at"]),
            attempt=int(body["attempt"]),
            previous_envelope_id=(
                str(body["previous_envelope_id"])
                if body.get("previous_envelope_id")
                else None
            ),
            created_at=float(body["created_at"]),
            metadata=dict(body.get("metadata") or {}),
            checksum=supplied,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RemoteDispatchRejected(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            BackendFailureKind.BACKEND_PROTOCOL.value,
            f"invalid backend dispatch envelope: {error}",
        ) from error


def create_frame(
    sequence: int,
    kind: str,
    payload: Mapping[str, Any],
    *,
    output_observed: bool,
) -> BackendTransportFrame:
    body = {
        "sequence": sequence,
        "kind": kind,
        "created_at": time.time(),
        "payload": dict(payload),
        "output_observed": output_observed,
    }
    return BackendTransportFrame(**body, digest=checksum(body))


def json_result(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        result = dict(value)
    elif value is None or isinstance(value, (str, int, float, bool)):
        result = {"value": value}
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        result = {"items": list(value)}
    else:
        raise TypeError(f"remote backend result is not JSON-compatible: {type(value).__name__}")
    canonical_json(result)
    return result


def validate_root(value: str, allowed: Sequence[str], label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.exists():
        raise RemoteDispatchRejected(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            BackendFailureKind.WORKSPACE_CORRUPT.value,
            f"{label} root does not exist",
            retryable=True,
            recovery_intent=BackendRecoveryIntent.REBUILD_WORKSPACE,
            detail={"root": str(path)},
        )
    if not path.is_dir():
        raise RemoteDispatchRejected(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            BackendFailureKind.WORKSPACE_CORRUPT.value,
            f"{label} root is not a directory",
            retryable=True,
            recovery_intent=BackendRecoveryIntent.REBUILD_WORKSPACE,
        )
    if allowed:
        allowed_paths = tuple(Path(item).expanduser().resolve() for item in allowed)
        if not any(path == root or root in path.parents for root in allowed_paths):
            raise RemoteDispatchRejected(
                HTTPStatus.FORBIDDEN,
                BackendFailureKind.WORKSPACE_CORRUPT.value,
                f"{label} root is outside service custody",
                recovery_intent=BackendRecoveryIntent.REBUILD_WORKSPACE,
            )
    return path


def normalize_operation_name(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("backend operation name is required")
    if len(normalized) > 200:
        raise ValueError("backend operation name is too long")
    if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for character in normalized):
        raise ValueError("backend operation name contains unsupported characters")
    return normalized


def safe_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    allowed = {
        "content-type",
        "accept",
        "idempotency-key",
        "x-zyra-envelope-id",
        "x-zyra-backend-lease-id",
        "x-zyra-provider-route-ref",
        "x-zyra-m0-execution-ref",
        "user-agent",
    }
    result: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = str(raw_name).strip().lower()
        if name in allowed:
            result[name] = str(raw_value)[:4096]
    return result


def rejected_from_record(record: RemoteDispatchRecord) -> RemoteDispatchRejected:
    error = dict(record.error or {})
    return RemoteDispatchRejected(
        HTTPStatus.INTERNAL_SERVER_ERROR,
        str(error.get("kind") or BackendFailureKind.EXECUTION_FAILED.value),
        str(error.get("message") or "remote backend execution failed"),
        retryable=bool(error.get("retryable")),
        recovery_intent=BackendRecoveryIntent(
            str(error.get("recovery_intent") or BackendRecoveryIntent.NONE.value)
        ),
        detail=dict(error.get("detail") or {}),
    )


def record_deadline(record: RemoteDispatchRecord, margin: float) -> float:
    future = record.future
    if future is not None and future.done():
        return time.time() + max(0.001, margin)
    return max(time.time() + max(0.001, margin), record.deadline_at + margin)


def wait_for_server(address: tuple[Any, ...], *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    host, port = str(address[0]), int(address[1])
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.01)
    raise TimeoutError(f"backend dispatch service did not start on {host}:{port}")


__all__ = [
    "BackendDispatchHttpServer",
    "BackendDispatchServiceRuntime",
    "BackendOperationRegistry",
    "BackendServiceConfig",
    "RemoteDispatchRecord",
    "RemoteDispatchRejected",
    "RemoteDispatchRequest",
    "RemoteDispatchResult",
    "RemoteDispatchState",
    "RemoteOperationCancelled",
    "RemoteOperationContext",
    "RemoteOperationTimedOut",
    "build_http_handler",
    "create_frame",
    "decode_envelope",
]
