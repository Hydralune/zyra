from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .models import (
    BackendDefinition,
    BackendDispatchEnvelope,
    BackendDispatchError,
    BackendFailureKind,
    BackendKind,
    BackendRecoveryIntent,
    canonical_json,
    checksum,
)
from .remote_control import RemoteBackendControlClient


class BackendTransportError(BackendDispatchError):
    """A backend transport failure with bytes/output and cancellation evidence."""


@dataclass(frozen=True, slots=True)
class BackendTransportRequest:
    envelope: BackendDispatchEnvelope
    operation: str
    payload: Mapping[str, Any]
    headers: Mapping[str, str] = field(default_factory=dict)
    input_digest: str = ""

    def __post_init__(self) -> None:
        if not self.operation.strip():
            raise ValueError("backend transport operation is required")
        digest = self.input_digest or checksum(
            {
                "envelope_id": self.envelope.envelope_id,
                "operation": self.operation,
                "payload": dict(self.payload),
            }
        )
        object.__setattr__(self, "input_digest", digest)

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema": "zyra.backend-transport-request/v1",
            "envelope": self.envelope.to_dict(),
            "operation": self.operation,
            "payload": dict(self.payload),
            "headers": _safe_headers(self.headers),
            "input_digest": self.input_digest,
        }


@dataclass(frozen=True, slots=True)
class BackendTransportFrame:
    sequence: int
    kind: str
    created_at: float
    payload: Mapping[str, Any]
    output_observed: bool
    digest: str

    @classmethod
    def from_wire(cls, value: Mapping[str, Any], *, expected_sequence: int) -> BackendTransportFrame:
        sequence = int(value.get("sequence", expected_sequence))
        if sequence != expected_sequence:
            raise ValueError(
                f"backend transport frame sequence mismatch: expected {expected_sequence}, got {sequence}"
            )
        kind = str(value.get("kind") or "")
        if kind not in {
            "accepted",
            "progress",
            "stdout",
            "stderr",
            "artifact",
            "result",
            "error",
            "cancelled",
            "heartbeat",
        }:
            raise ValueError(f"unsupported backend transport frame kind: {kind}")
        payload = dict(value.get("payload") or {})
        body = {
            "sequence": sequence,
            "kind": kind,
            "created_at": float(value.get("created_at", time.time())),
            "payload": payload,
            "output_observed": bool(
                value.get(
                    "output_observed",
                    kind in {"stdout", "artifact", "result"},
                )
            ),
        }
        supplied = str(value.get("digest") or "")
        expected = checksum(body)
        if not supplied:
            raise ValueError("backend transport frame digest is required")
        if supplied != expected:
            raise ValueError("backend transport frame digest mismatch")
        return cls(**body, digest=expected)

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BackendTransportResponse:
    request_id: str
    backend_id: str
    status: str
    result: Mapping[str, Any]
    frames: tuple[BackendTransportFrame, ...]
    started_at: float
    completed_at: float
    request_bytes: int
    response_bytes: int
    output_observed: bool
    transport_receipt_id: str
    input_digest: str
    result_digest: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    # In-process workers may return rich Python values.  They are deliberately
    # kept outside the durable/wire result so hashes, events, and remote
    # transports never depend on a non-serializable object graph.
    native_value: Any = field(default=None, repr=False, compare=False)

    def to_wire(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "backend_id": self.backend_id,
            "status": self.status,
            "result": dict(self.result),
            "frames": [frame.to_wire() for frame in self.frames],
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "request_bytes": self.request_bytes,
            "response_bytes": self.response_bytes,
            "output_observed": self.output_observed,
            "transport_receipt_id": self.transport_receipt_id,
            "input_digest": self.input_digest,
            "result_digest": self.result_digest,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class BackendHealthProbe:
    backend_id: str
    ok: bool
    status: str
    latency_milliseconds: float
    checked_at: float
    transport: str
    detail: Mapping[str, Any] = field(default_factory=dict)


class DispatchCancellation:
    """Cooperative token plus transport-specific interrupt callbacks."""

    def __init__(self, *, dispatch_id: str, deadline_at: float) -> None:
        self.dispatch_id = dispatch_id
        self.deadline_at = deadline_at
        self._event = threading.Event()
        self._lock = threading.RLock()
        self._callbacks: dict[str, Callable[[str], None]] = {}
        self._reason = ""
        self._requested_at: float | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    @property
    def requested_at(self) -> float | None:
        with self._lock:
            return self._requested_at

    def remaining_seconds(self, *, now: float | None = None) -> float:
        return max(0.0, self.deadline_at - (time.time() if now is None else now))

    def throw_if_cancelled(self) -> None:
        if self.cancelled:
            raise BackendTransportError(
                BackendFailureKind.DISPATCH_ABORTED,
                self.reason or "backend dispatch cancelled",
                retryable=False,
                recovery_intent=BackendRecoveryIntent.NONE,
                detail={"dispatch_id": self.dispatch_id, "cancel_requested_at": self.requested_at},
            )
        if self.remaining_seconds() <= 0:
            self.cancel("turn deadline elapsed")
            raise BackendTransportError(
                BackendFailureKind.TURN_TIMEOUT,
                "backend dispatch deadline elapsed",
                retryable=True,
                recovery_intent=BackendRecoveryIntent.CHANGE_BACKEND,
                detail={"dispatch_id": self.dispatch_id, "deadline_at": self.deadline_at},
            )

    def register(self, name: str, callback: Callable[[str], None]) -> Callable[[], None]:
        if not name.strip():
            raise ValueError("cancellation callback name is required")
        with self._lock:
            if self.cancelled:
                callback(self._reason)
                return lambda: None
            self._callbacks[name] = callback

        def unregister() -> None:
            with self._lock:
                self._callbacks.pop(name, None)

        return unregister

    def cancel(self, reason: str) -> bool:
        reason = reason.strip() or "backend dispatch cancelled"
        callbacks: list[Callable[[str], None]]
        with self._lock:
            if self.cancelled:
                return False
            self._reason = reason
            self._requested_at = time.time()
            self._event.set()
            callbacks = list(self._callbacks.values())
        for callback in callbacks:
            try:
                callback(reason)
            except Exception:
                # Cancellation is best effort per callback, but the token is
                # authoritative and prevents any result from being committed.
                continue
        return True

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


class BackendTransport(ABC):
    kind: BackendKind

    @abstractmethod
    def probe(self, definition: BackendDefinition) -> BackendHealthProbe:
        raise NotImplementedError

    @abstractmethod
    def dispatch(
        self,
        definition: BackendDefinition,
        request: BackendTransportRequest,
        cancellation: DispatchCancellation,
    ) -> BackendTransportResponse:
        raise NotImplementedError


class InProcessBackendTransport(BackendTransport):
    kind = BackendKind.LOCAL_PROCESS

    def __init__(self, operation: Callable[[BackendDispatchEnvelope], Any]) -> None:
        self.operation = operation

    def probe(self, definition: BackendDefinition) -> BackendHealthProbe:
        started = time.monotonic()
        ok = bool(definition.enabled and callable(self.operation))
        return BackendHealthProbe(
            backend_id=definition.backend_id,
            ok=ok,
            status="healthy" if ok else "unavailable",
            latency_milliseconds=(time.monotonic() - started) * 1_000,
            checked_at=time.time(),
            transport="in_process",
            detail={"callable": callable(self.operation)},
        )

    def dispatch(
        self,
        definition: BackendDefinition,
        request: BackendTransportRequest,
        cancellation: DispatchCancellation,
    ) -> BackendTransportResponse:
        cancellation.throw_if_cancelled()
        started_at = time.time()
        value = self.operation(request.envelope)
        if cancellation.cancelled:
            raise BackendTransportError(
                BackendFailureKind.TURN_TIMEOUT,
                cancellation.reason or "in-process turn deadline elapsed after completion",
                retryable=False,
                recovery_intent=BackendRecoveryIntent.RECONCILE,
                backend_id=definition.backend_id,
                lease_id=request.envelope.backend_lease_id,
                provider_route_id=request.envelope.provider_route_id or "",
                output_observed=True,
                detail={"in_process_operation_could_not_be_preempted": True},
            )
        completed_at = time.time()
        result = _mapping_result(value)
        frame = _frame(1, "result", result, output_observed=True)
        return _response(
            definition,
            request,
            status="succeeded",
            result=result,
            frames=(frame,),
            started_at=started_at,
            completed_at=completed_at,
            request_bytes=len(canonical_json(request.to_wire()).encode("utf-8")),
            response_bytes=len(canonical_json(result).encode("utf-8")),
            metadata={"transport": "in_process"},
            native_value=value,
        )


class LocalProcessBackendTransport(BackendTransport):
    kind = BackendKind.LOCAL_PROCESS

    def __init__(self, *, maximum_response_bytes: int = 64 * 1024 * 1024) -> None:
        self.maximum_response_bytes = maximum_response_bytes

    def probe(self, definition: BackendDefinition) -> BackendHealthProbe:
        started = time.monotonic()
        command = self._command(definition)
        executable = _resolve_executable(command[0]) if command else None
        ok = executable is not None
        return BackendHealthProbe(
            backend_id=definition.backend_id,
            ok=ok,
            status="healthy" if ok else "unavailable",
            latency_milliseconds=(time.monotonic() - started) * 1_000,
            checked_at=time.time(),
            transport="local_process",
            detail={
                "executable": executable or "",
                "command_configured": bool(command),
            },
        )

    def dispatch(
        self,
        definition: BackendDefinition,
        request: BackendTransportRequest,
        cancellation: DispatchCancellation,
    ) -> BackendTransportResponse:
        cancellation.throw_if_cancelled()
        command = self._command(definition)
        if not command:
            raise _unavailable(definition, "local process backend has no command")
        executable = _resolve_executable(command[0])
        if executable is None:
            raise _unavailable(definition, f"local process executable not found: {command[0]}")
        command = (executable, *command[1:])
        workspace = Path(request.envelope.workspace_root).resolve()
        if not workspace.is_dir():
            raise BackendTransportError(
                BackendFailureKind.WORKSPACE_UNAVAILABLE,
                f"local process workspace does not exist: {workspace}",
                retryable=True,
                recovery_intent=BackendRecoveryIntent.CHANGE_BACKEND,
                backend_id=definition.backend_id,
                lease_id=request.envelope.backend_lease_id,
            )
        started_at = time.time()
        process = subprocess.Popen(
            command,
            cwd=str(workspace),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            env=_sanitized_process_environment(definition),
            creationflags=_process_creation_flags(),
        )
        unregister = cancellation.register(
            f"process:{process.pid}",
            lambda _reason: _terminate_process_tree(process),
        )
        payload = (canonical_json(request.to_wire()) + "\n").encode("utf-8")
        try:
            stdout, stderr = process.communicate(
                payload,
                timeout=max(0.001, cancellation.remaining_seconds()),
            )
        except subprocess.TimeoutExpired as error:
            cancellation.cancel("local process turn timed out")
            _terminate_process_tree(process)
            stdout, stderr = process.communicate(timeout=5)
            raise BackendTransportError(
                BackendFailureKind.TURN_TIMEOUT,
                "local process backend exceeded turn deadline",
                retryable=not bool(stdout),
                recovery_intent=(
                    BackendRecoveryIntent.CHANGE_BACKEND
                    if not stdout
                    else BackendRecoveryIntent.RECONCILE
                ),
                backend_id=definition.backend_id,
                lease_id=request.envelope.backend_lease_id,
                provider_route_id=request.envelope.provider_route_id or "",
                output_observed=bool(stdout),
                detail={"pid": process.pid, "stderr": _safe_text(stderr)},
            ) from error
        finally:
            unregister()
        cancellation.throw_if_cancelled()
        if len(stdout) > self.maximum_response_bytes:
            raise BackendTransportError(
                BackendFailureKind.BACKEND_PROTOCOL,
                "local process response exceeded configured byte budget",
                retryable=False,
                recovery_intent=BackendRecoveryIntent.NONE,
                backend_id=definition.backend_id,
                detail={"response_bytes": len(stdout), "maximum_bytes": self.maximum_response_bytes},
            )
        if process.returncode != 0:
            raise BackendTransportError(
                BackendFailureKind.EXECUTION_FAILED,
                f"local process backend exited with code {process.returncode}",
                retryable=process.returncode in {75, 111},
                recovery_intent=(
                    BackendRecoveryIntent.CHANGE_BACKEND
                    if process.returncode in {75, 111}
                    else BackendRecoveryIntent.NONE
                ),
                backend_id=definition.backend_id,
                lease_id=request.envelope.backend_lease_id,
                output_observed=bool(stdout),
                detail={"exit_code": process.returncode, "stderr": _safe_text(stderr)},
            )
        frames, result = _decode_process_response(stdout)
        return _response(
            definition,
            request,
            status="succeeded",
            result=result,
            frames=frames,
            started_at=started_at,
            completed_at=time.time(),
            request_bytes=len(payload),
            response_bytes=len(stdout),
            metadata={
                "transport": "local_process",
                "pid": process.pid,
                "exit_code": process.returncode,
                "stderr": _safe_text(stderr),
            },
        )

    @staticmethod
    def _command(definition: BackendDefinition) -> tuple[str, ...]:
        return tuple(str(item) for item in definition.command if str(item).strip())


class DockerBackendTransport(BackendTransport):
    kind = BackendKind.DOCKER

    def __init__(
        self,
        *,
        docker_executable: str = "docker",
        maximum_response_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self.docker_executable = docker_executable
        self.maximum_response_bytes = maximum_response_bytes

    def probe(self, definition: BackendDefinition) -> BackendHealthProbe:
        started = time.monotonic()
        executable = _resolve_executable(self.docker_executable)
        if executable is None:
            return BackendHealthProbe(
                backend_id=definition.backend_id,
                ok=False,
                status="unavailable",
                latency_milliseconds=(time.monotonic() - started) * 1_000,
                checked_at=time.time(),
                transport="docker",
                detail={"reason": "docker executable not found"},
            )
        try:
            completed = subprocess.run(
                [executable, "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                timeout=definition.limits.health_timeout_seconds,
                check=False,
                env=_minimal_host_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return BackendHealthProbe(
                backend_id=definition.backend_id,
                ok=False,
                status="unavailable",
                latency_milliseconds=(time.monotonic() - started) * 1_000,
                checked_at=time.time(),
                transport="docker",
                detail={"reason": f"{type(error).__name__}: {error}"},
            )
        ok = completed.returncode == 0
        return BackendHealthProbe(
            backend_id=definition.backend_id,
            ok=ok,
            status="healthy" if ok else "unavailable",
            latency_milliseconds=(time.monotonic() - started) * 1_000,
            checked_at=time.time(),
            transport="docker",
            detail={
                "server_version": _safe_text(completed.stdout),
                "stderr": _safe_text(completed.stderr),
            },
        )

    def dispatch(
        self,
        definition: BackendDefinition,
        request: BackendTransportRequest,
        cancellation: DispatchCancellation,
    ) -> BackendTransportResponse:
        cancellation.throw_if_cancelled()
        if not definition.docker_image:
            raise _unavailable(definition, "docker backend has no image")
        executable = _resolve_executable(self.docker_executable)
        if executable is None:
            raise _unavailable(definition, "docker executable is unavailable")
        workspace = Path(request.envelope.workspace_root).resolve()
        artifact_root = Path(request.envelope.artifact_root).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        artifact_root.mkdir(parents=True, exist_ok=True)
        container_name = _safe_container_name(
            f"zyra-{definition.backend_id}-{request.envelope.envelope_id}"
        )
        command = [
            executable,
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            str(definition.metadata.get("docker_network") or "none"),
            "--read-only",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--pids-limit",
            str(int(definition.metadata.get("docker_pids_limit") or 256)),
            "--memory",
            f"{definition.limits.memory_megabytes or 1024}m",
            "--cpus",
            str(max(0.1, (definition.limits.cpu_millicores or 1000) / 1000)),
            "--mount",
            f"type=bind,src={workspace},dst=/workspace",
            "--mount",
            f"type=bind,src={artifact_root},dst=/artifacts",
            "--workdir",
            "/workspace",
            "--env",
            "ZYRA_BACKEND_PROTOCOL=zyra.backend-transport-request/v1",
            definition.docker_image,
            *tuple(definition.command),
        ]
        started_at = time.time()
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            env=_minimal_host_environment(),
            creationflags=_process_creation_flags(),
        )

        def cancel_container(_reason: str) -> None:
            try:
                subprocess.run(
                    [executable, "kill", container_name],
                    capture_output=True,
                    timeout=5,
                    check=False,
                    env=_minimal_host_environment(),
                )
            finally:
                _terminate_process_tree(process)

        unregister = cancellation.register(f"docker:{container_name}", cancel_container)
        payload = (canonical_json(request.to_wire()) + "\n").encode("utf-8")
        try:
            stdout, stderr = process.communicate(
                payload,
                timeout=max(0.001, cancellation.remaining_seconds()),
            )
        except subprocess.TimeoutExpired as error:
            cancellation.cancel("docker turn timed out")
            cancel_container("docker turn timed out")
            stdout, stderr = process.communicate(timeout=5)
            raise BackendTransportError(
                BackendFailureKind.TURN_TIMEOUT,
                "docker backend exceeded turn deadline",
                retryable=not bool(stdout),
                recovery_intent=(
                    BackendRecoveryIntent.CHANGE_BACKEND
                    if not stdout
                    else BackendRecoveryIntent.RECONCILE
                ),
                backend_id=definition.backend_id,
                lease_id=request.envelope.backend_lease_id,
                output_observed=bool(stdout),
                detail={"container_name": container_name, "stderr": _safe_text(stderr)},
            ) from error
        finally:
            unregister()
        cancellation.throw_if_cancelled()
        if process.returncode != 0:
            raise BackendTransportError(
                BackendFailureKind.EXECUTION_FAILED,
                f"docker backend exited with code {process.returncode}",
                retryable=process.returncode in {125, 137, 143},
                recovery_intent=BackendRecoveryIntent.CHANGE_BACKEND,
                backend_id=definition.backend_id,
                lease_id=request.envelope.backend_lease_id,
                output_observed=bool(stdout),
                detail={"container_name": container_name, "stderr": _safe_text(stderr)},
            )
        if len(stdout) > self.maximum_response_bytes:
            raise BackendTransportError(
                BackendFailureKind.BACKEND_PROTOCOL,
                "docker backend response exceeded byte budget",
                retryable=False,
                recovery_intent=BackendRecoveryIntent.NONE,
                backend_id=definition.backend_id,
            )
        frames, result = _decode_process_response(stdout)
        return _response(
            definition,
            request,
            status="succeeded",
            result=result,
            frames=frames,
            started_at=started_at,
            completed_at=time.time(),
            request_bytes=len(payload),
            response_bytes=len(stdout),
            metadata={
                "transport": "docker",
                "container_name": container_name,
                "image": definition.docker_image,
                "exit_code": process.returncode,
            },
        )


class HttpBackendTransport(BackendTransport):
    def __init__(
        self,
        kind: BackendKind,
        *,
        maximum_response_bytes: int = 64 * 1024 * 1024,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        if kind not in {BackendKind.EDGE_HTTP, BackendKind.CLOUD_HTTP}:
            raise ValueError("HTTP backend transport requires edge_http or cloud_http kind")
        self.kind = kind
        self.maximum_response_bytes = maximum_response_bytes
        self.opener = opener or urllib.request.build_opener(_NoCrossHostRedirectHandler())

    def probe(self, definition: BackendDefinition) -> BackendHealthProbe:
        started = time.monotonic()
        url = definition.health_endpoint or _join_url(definition.endpoint or "", "/health")
        try:
            _enforce_endpoint_policy(definition, url)
            request = urllib.request.Request(
                url,
                headers={
                    "accept": "application/json",
                    "user-agent": "Zyra-BackendRegistry/1",
                },
                method="GET",
            )
            with self.opener.open(request, timeout=definition.limits.health_timeout_seconds) as response:
                body = response.read(min(self.maximum_response_bytes, 1024 * 1024))
                status = int(getattr(response, "status", 200))
            value = _decode_json_object(body, context="backend health") if body else {}
            ok = 200 <= status < 300 and bool(value.get("ok", True))
            detail = {
                "http_status": status,
                "capabilities": list(value.get("capabilities") or ()),
                "generation": value.get("generation"),
            }
        except Exception as error:
            ok = False
            detail = {"reason": f"{type(error).__name__}: {error}"}
        return BackendHealthProbe(
            backend_id=definition.backend_id,
            ok=ok,
            status="healthy" if ok else "unavailable",
            latency_milliseconds=(time.monotonic() - started) * 1_000,
            checked_at=time.time(),
            transport=self.kind.value,
            detail=detail,
        )

    def dispatch(
        self,
        definition: BackendDefinition,
        request: BackendTransportRequest,
        cancellation: DispatchCancellation,
    ) -> BackendTransportResponse:
        cancellation.throw_if_cancelled()
        endpoint = _join_url(definition.endpoint or "", "/v1/dispatch")
        _enforce_endpoint_policy(definition, endpoint)
        payload = canonical_json(request.to_wire()).encode("utf-8")
        headers = {
            "accept": "application/x-ndjson, application/json",
            "content-type": "application/json",
            "idempotency-key": request.envelope.idempotency_key,
            "x-zyra-envelope-id": request.envelope.envelope_id,
            "x-zyra-backend-lease-id": request.envelope.backend_lease_id,
            "x-zyra-provider-route-ref": request.envelope.provider_route_id or "",
            "x-zyra-m0-execution-ref": request.envelope.m0_execution_ref,
            "user-agent": "Zyra-BackendRegistry/1",
            **_safe_headers(request.headers),
        }
        http_request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
        started_at = time.time()
        response_holder: list[Any] = []
        remote_control = RemoteBackendControlClient(definition)

        def interrupt_remote(reason: str) -> None:
            try:
                remote_control.cancel_envelope(request.envelope.envelope_id, reason)
            finally:
                _close_all(response_holder)

        unregister = cancellation.register(
            f"http:{request.envelope.envelope_id}",
            interrupt_remote,
        )
        try:
            response = self.opener.open(
                http_request,
                timeout=max(
                    0.001,
                    min(
                        definition.limits.connect_timeout_seconds,
                        cancellation.remaining_seconds(),
                    ),
                ),
            )
            response_holder.append(response)
            status = int(getattr(response, "status", 200))
            content_type = str(response.headers.get("content-type") or "").lower()
            if status < 200 or status >= 300:
                body = response.read(min(self.maximum_response_bytes, 1024 * 1024))
                raise _http_failure(definition, request.envelope, status, body, response.headers)
            if "application/x-ndjson" in content_type or "text/event-stream" in content_type:
                frames, result, response_bytes = self._read_frames(
                    response,
                    cancellation,
                    sse="text/event-stream" in content_type,
                )
            else:
                body = _read_bounded(response, self.maximum_response_bytes, cancellation)
                value = _decode_json_object(body, context="backend response")
                frames, result = _decode_response_object(value)
                response_bytes = len(body)
            cancellation.throw_if_cancelled()
            return _response(
                definition,
                request,
                status="succeeded",
                result=result,
                frames=frames,
                started_at=started_at,
                completed_at=time.time(),
                request_bytes=len(payload),
                response_bytes=response_bytes,
                metadata={
                    "transport": self.kind.value,
                    "endpoint": _redact_url(endpoint),
                    "http_status": status,
                    "response_headers": _safe_response_headers(response.headers),
                },
            )
        except urllib.error.HTTPError as error:
            body = error.read(min(self.maximum_response_bytes, 1024 * 1024))
            raise _http_failure(
                definition,
                request.envelope,
                int(error.code),
                body,
                error.headers,
            ) from error
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            cancellation.throw_if_cancelled()
            reason = str(getattr(error, "reason", error))
            timeout = "timed out" in reason.lower() or isinstance(error, TimeoutError)
            raise BackendTransportError(
                BackendFailureKind.BACKEND_TIMEOUT if timeout else BackendFailureKind.BACKEND_UNAVAILABLE,
                f"{self.kind.value} backend request failed: {reason}",
                retryable=True,
                recovery_intent=BackendRecoveryIntent.CHANGE_BACKEND,
                backend_id=definition.backend_id,
                lease_id=request.envelope.backend_lease_id,
                provider_route_id=request.envelope.provider_route_id or "",
                detail={"endpoint": _redact_url(endpoint)},
            ) from error
        finally:
            unregister()
            _close_all(response_holder)

    def _read_frames(
        self,
        response: Any,
        cancellation: DispatchCancellation,
        *,
        sse: bool,
    ) -> tuple[tuple[BackendTransportFrame, ...], dict[str, Any], int]:
        frames: list[BackendTransportFrame] = []
        response_bytes = 0
        result: dict[str, Any] | None = None
        sse_data: list[str] = []
        while True:
            cancellation.throw_if_cancelled()
            line = response.readline()
            if not line:
                break
            response_bytes += len(line)
            if response_bytes > self.maximum_response_bytes:
                raise BackendTransportError(
                    BackendFailureKind.BACKEND_PROTOCOL,
                    "HTTP backend stream exceeded response byte budget",
                    retryable=False,
                    recovery_intent=BackendRecoveryIntent.NONE,
                    output_observed=any(item.output_observed for item in frames),
                )
            text = line.decode("utf-8", errors="strict").rstrip("\r\n")
            if sse:
                if text.startswith("data:"):
                    sse_data.append(text[5:].lstrip())
                    continue
                if text:
                    continue
                if not sse_data:
                    continue
                text = "\n".join(sse_data)
                sse_data.clear()
            elif not text:
                continue
            if text == "[DONE]":
                break
            value = json.loads(text)
            if not isinstance(value, Mapping):
                raise ValueError("backend stream frame must be a JSON object")
            frame = BackendTransportFrame.from_wire(value, expected_sequence=len(frames) + 1)
            frames.append(frame)
            if frame.kind == "result":
                result = dict(frame.payload)
            elif frame.kind == "error":
                raise _remote_error(frame, output_observed=any(item.output_observed for item in frames))
            elif frame.kind == "cancelled":
                raise BackendTransportError(
                    BackendFailureKind.DISPATCH_ABORTED,
                    str(frame.payload.get("reason") or "remote backend cancelled dispatch"),
                    retryable=False,
                    recovery_intent=BackendRecoveryIntent.NONE,
                    output_observed=any(item.output_observed for item in frames),
                )
        if result is None:
            raise BackendTransportError(
                BackendFailureKind.BACKEND_PROTOCOL,
                "HTTP backend stream ended without terminal result frame",
                retryable=not any(item.output_observed for item in frames),
                recovery_intent=(
                    BackendRecoveryIntent.CHANGE_BACKEND
                    if not any(item.output_observed for item in frames)
                    else BackendRecoveryIntent.RECONCILE
                ),
                output_observed=any(item.output_observed for item in frames),
            )
        return tuple(frames), result, response_bytes


class BackendTransportRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._factories: dict[BackendKind, Callable[..., BackendTransport]] = {
            BackendKind.LOCAL_PROCESS: lambda **kwargs: LocalProcessBackendTransport(**kwargs),
            BackendKind.DOCKER: lambda **kwargs: DockerBackendTransport(**kwargs),
            BackendKind.EDGE_HTTP: lambda **kwargs: HttpBackendTransport(BackendKind.EDGE_HTTP, **kwargs),
            BackendKind.CLOUD_HTTP: lambda **kwargs: HttpBackendTransport(BackendKind.CLOUD_HTTP, **kwargs),
        }

    def register(
        self,
        kind: BackendKind,
        factory: Callable[..., BackendTransport],
        *,
        replace: bool = False,
    ) -> None:
        with self._lock:
            if kind in self._factories and not replace:
                raise ValueError(f"backend transport factory already registered: {kind.value}")
            self._factories[kind] = factory

    def create(
        self,
        definition: BackendDefinition,
        *,
        in_process_operation: Callable[[BackendDispatchEnvelope], Any] | None = None,
    ) -> BackendTransport:
        # dispatch_callable is an explicit compatibility port: the supplied
        # callable is the selected local execution implementation.  Remote
        # and subprocess dispatches enter through dispatch_payload and never
        # provide this value.
        if definition.kind is BackendKind.LOCAL_PROCESS and in_process_operation is not None:
            return InProcessBackendTransport(in_process_operation)
        if (
            definition.kind is BackendKind.LOCAL_PROCESS
            and str(definition.metadata.get("execution_mode") or "") == "in_process_callable"
        ):
            if in_process_operation is None:
                raise _unavailable(definition, "in-process backend requires a worker operation")
        with self._lock:
            factory = self._factories.get(definition.kind)
        if factory is None:
            raise _unavailable(definition, f"no transport registered for {definition.kind.value}")
        return factory()

    def disconnect(self, kind: BackendKind) -> None:
        with self._lock:
            self._factories.pop(kind, None)

    def available_kinds(self) -> tuple[BackendKind, ...]:
        with self._lock:
            return tuple(sorted(self._factories, key=lambda item: item.value))


class _NoCrossHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str):
        source = urllib.parse.urlparse(req.full_url)
        target = urllib.parse.urlparse(newurl)
        if (source.scheme, source.hostname, source.port) != (
            target.scheme,
            target.hostname,
            target.port,
        ):
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                "cross-host backend redirect rejected",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _response(
    definition: BackendDefinition,
    request: BackendTransportRequest,
    *,
    status: str,
    result: Mapping[str, Any],
    frames: tuple[BackendTransportFrame, ...],
    started_at: float,
    completed_at: float,
    request_bytes: int,
    response_bytes: int,
    metadata: Mapping[str, Any],
    native_value: Any = None,
) -> BackendTransportResponse:
    result_body = dict(result)
    return BackendTransportResponse(
        request_id=request.envelope.envelope_id,
        backend_id=definition.backend_id,
        status=status,
        result=result_body,
        frames=frames,
        started_at=started_at,
        completed_at=completed_at,
        request_bytes=request_bytes,
        response_bytes=response_bytes,
        output_observed=any(frame.output_observed for frame in frames),
        transport_receipt_id=f"backend_receipt_{uuid.uuid4().hex}",
        input_digest=request.input_digest,
        result_digest=checksum(result_body),
        metadata=dict(metadata),
        native_value=native_value,
    )


def _frame(
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


def _decode_process_response(
    stdout: bytes,
) -> tuple[tuple[BackendTransportFrame, ...], dict[str, Any]]:
    frames: list[BackendTransportFrame] = []
    result: dict[str, Any] | None = None
    for raw_line in stdout.splitlines():
        if not raw_line.strip():
            continue
        value = json.loads(raw_line.decode("utf-8", errors="strict"))
        if not isinstance(value, Mapping):
            raise ValueError("local backend response line must be a JSON object")
        if value.get("schema") == "zyra.backend-transport-response/v1":
            decoded_frames, decoded_result = _decode_response_object(value)
            for frame in decoded_frames:
                normalized = BackendTransportFrame.from_wire(
                    {**frame.to_wire(), "sequence": len(frames) + 1},
                    expected_sequence=len(frames) + 1,
                )
                frames.append(normalized)
            result = decoded_result
            continue
        frame = BackendTransportFrame.from_wire(value, expected_sequence=len(frames) + 1)
        frames.append(frame)
        if frame.kind == "result":
            result = dict(frame.payload)
        elif frame.kind == "error":
            raise _remote_error(frame, output_observed=any(item.output_observed for item in frames))
    if result is None:
        raise BackendTransportError(
            BackendFailureKind.BACKEND_PROTOCOL,
            "local backend exited without terminal result",
            retryable=not any(frame.output_observed for frame in frames),
            recovery_intent=(
                BackendRecoveryIntent.CHANGE_BACKEND
                if not any(frame.output_observed for frame in frames)
                else BackendRecoveryIntent.RECONCILE
            ),
            output_observed=any(frame.output_observed for frame in frames),
        )
    return tuple(frames), result


def _decode_response_object(
    value: Mapping[str, Any],
) -> tuple[tuple[BackendTransportFrame, ...], dict[str, Any]]:
    if value.get("schema") not in {None, "zyra.backend-transport-response/v1"}:
        raise ValueError("unsupported backend transport response schema")
    raw_frames = value.get("frames") or ()
    if not isinstance(raw_frames, Sequence) or isinstance(raw_frames, (str, bytes, bytearray)):
        raise ValueError("backend response frames must be an array")
    frames = tuple(
        BackendTransportFrame.from_wire(item, expected_sequence=index)
        for index, item in enumerate(raw_frames, start=1)
        if isinstance(item, Mapping)
    )
    result = value.get("result")
    if not isinstance(result, Mapping):
        result_frame = next((frame for frame in reversed(frames) if frame.kind == "result"), None)
        result = {} if result_frame is None else result_frame.payload
    if not frames:
        frames = (_frame(1, "result", dict(result), output_observed=True),)
    return frames, dict(result)


def _remote_error(frame: BackendTransportFrame, *, output_observed: bool) -> BackendTransportError:
    raw_kind = str(frame.payload.get("kind") or BackendFailureKind.EXECUTION_FAILED.value)
    try:
        kind = BackendFailureKind(raw_kind)
    except ValueError:
        kind = BackendFailureKind.EXECUTION_FAILED
    raw_intent = str(frame.payload.get("recovery_intent") or BackendRecoveryIntent.NONE.value)
    try:
        intent = BackendRecoveryIntent(raw_intent)
    except ValueError:
        intent = BackendRecoveryIntent.NONE
    return BackendTransportError(
        kind,
        str(frame.payload.get("message") or "remote backend execution failed"),
        retryable=bool(frame.payload.get("retryable", False)) and not output_observed,
        recovery_intent=BackendRecoveryIntent.RECONCILE if output_observed else intent,
        output_observed=output_observed,
        detail={key: value for key, value in frame.payload.items() if key != "message"},
    )


def _http_failure(
    definition: BackendDefinition,
    envelope: BackendDispatchEnvelope,
    status: int,
    body: bytes,
    headers: Any,
) -> BackendTransportError:
    text = body.decode("utf-8", errors="replace")[:4_000]
    retry_after = _retry_after_seconds(headers)
    if status in {408, 504}:
        kind = BackendFailureKind.BACKEND_TIMEOUT
    elif status in {409, 425, 429}:
        kind = BackendFailureKind.BACKEND_CAPACITY
    elif status in {502, 503} or status >= 500:
        kind = BackendFailureKind.BACKEND_UNAVAILABLE
    elif status == 422 and "workspace" in text.lower():
        kind = BackendFailureKind.WORKSPACE_CORRUPT
    else:
        kind = BackendFailureKind.BACKEND_PROTOCOL
    retryable = kind in {
        BackendFailureKind.BACKEND_TIMEOUT,
        BackendFailureKind.BACKEND_CAPACITY,
        BackendFailureKind.BACKEND_UNAVAILABLE,
        BackendFailureKind.WORKSPACE_CORRUPT,
    }
    return BackendTransportError(
        kind,
        f"{definition.kind.value} backend returned HTTP {status}: {_redact_text(text)}",
        retryable=retryable,
        recovery_intent=(
            BackendRecoveryIntent.REBUILD_WORKSPACE
            if kind is BackendFailureKind.WORKSPACE_CORRUPT
            else BackendRecoveryIntent.CHANGE_BACKEND
            if retryable
            else BackendRecoveryIntent.NONE
        ),
        backend_id=definition.backend_id,
        lease_id=envelope.backend_lease_id,
        provider_route_id=envelope.provider_route_id or "",
        detail={"http_status": status, "retry_after_seconds": retry_after},
    )


def _read_bounded(
    response: Any,
    maximum_bytes: int,
    cancellation: DispatchCancellation,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        cancellation.throw_if_cancelled()
        chunk = response.read(min(64 * 1024, maximum_bytes - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum_bytes:
            raise BackendTransportError(
                BackendFailureKind.BACKEND_PROTOCOL,
                "backend response exceeded configured byte budget",
                retryable=False,
                recovery_intent=BackendRecoveryIntent.NONE,
            )
    return b"".join(chunks)


def _decode_json_object(body: bytes, *, context: str) -> dict[str, Any]:
    value = json.loads(body.decode("utf-8", errors="strict"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be a JSON object")
    return dict(value)


def _enforce_endpoint_policy(definition: BackendDefinition, url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BackendTransportError(
            BackendFailureKind.BACKEND_PROTOCOL,
            "backend endpoint must be absolute HTTP(S)",
            retryable=False,
            recovery_intent=BackendRecoveryIntent.NONE,
            backend_id=definition.backend_id,
        )
    allowed_hosts = {
        str(item).lower()
        for item in definition.metadata.get("allowed_hosts", ())
        if str(item).strip()
    }
    endpoint_host = urllib.parse.urlparse(definition.endpoint or "").hostname
    if endpoint_host:
        allowed_hosts.add(endpoint_host.lower())
    if parsed.hostname.lower() not in allowed_hosts:
        raise BackendTransportError(
            BackendFailureKind.BACKEND_PROTOCOL,
            f"backend endpoint host is not allowed: {parsed.hostname}",
            retryable=False,
            recovery_intent=BackendRecoveryIntent.NONE,
            backend_id=definition.backend_id,
        )
    if definition.location.value == "cloud" and parsed.scheme != "https":
        allow_loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"} and bool(
            definition.metadata.get("allow_insecure_loopback")
        )
        if not allow_loopback:
            raise BackendTransportError(
                BackendFailureKind.BACKEND_PROTOCOL,
                "cloud backend requires HTTPS",
                retryable=False,
                recovery_intent=BackendRecoveryIntent.NONE,
                backend_id=definition.backend_id,
            )


def _join_url(base: str, path: str) -> str:
    parsed = urllib.parse.urlparse(base)
    if not parsed.scheme or not parsed.netloc:
        return base
    base_path = parsed.path.rstrip("/")
    target_path = path if path.startswith("/") else f"/{path}"
    return urllib.parse.urlunparse(parsed._replace(path=f"{base_path}{target_path}"))


def _resolve_executable(value: str) -> str | None:
    candidate = Path(value).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        return str(candidate.resolve()) if candidate.is_file() else None
    return shutil.which(value)


def _sanitized_process_environment(definition: BackendDefinition) -> dict[str, str]:
    env = _minimal_host_environment()
    env.update(
        {
            "ZYRA_BACKEND_ID": definition.backend_id,
            "ZYRA_BACKEND_KIND": definition.kind.value,
            "ZYRA_BACKEND_LOCATION": definition.location.value,
            "ZYRA_BACKEND_PROTOCOL": "zyra.backend-transport-request/v1",
        }
    )
    allowed = tuple(str(item) for item in definition.metadata.get("environment_allowlist", ()))
    for name in allowed:
        if name in os.environ and not _looks_secret_name(name):
            env[name] = os.environ[name]
    return env


def _minimal_host_environment() -> dict[str, str]:
    allowed = (
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
    )
    return {name: os.environ[name] for name in allowed if name in os.environ}


def _looks_secret_name(name: str) -> bool:
    upper = name.upper()
    return any(token in upper for token in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))


def _process_creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass


def _safe_headers(value: Mapping[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        name = str(raw_name).strip().lower()
        if not name or "\n" in name or "\r" in name:
            raise ValueError("invalid backend request header name")
        if name in {"authorization", "proxy-authorization", "cookie", "set-cookie"}:
            raise ValueError(f"backend transport request may not forward credential header: {name}")
        text = str(raw_value)
        if "\n" in text or "\r" in text:
            raise ValueError("invalid backend request header value")
        result[name] = text
    return result


def _safe_response_headers(headers: Any) -> dict[str, str]:
    allowed = {"content-type", "content-length", "date", "server", "retry-after", "x-request-id"}
    return {
        str(name).lower(): str(value)[:500]
        for name, value in headers.items()
        if str(name).lower() in allowed
    }


def _retry_after_seconds(headers: Any) -> float | None:
    raw = headers.get("retry-after") if headers is not None else None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _safe_text(value: bytes | str | None, *, limit: int = 4_000) -> str:
    if value is None:
        return ""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    return _redact_text(text)[:limit]


def _redact_text(value: str) -> str:
    text = value
    for prefix in ("sk-", "key-", "token-", "bearer "):
        start = 0
        lower = text.lower()
        while True:
            index = lower.find(prefix, start)
            if index < 0:
                break
            end = index + len(prefix)
            while end < len(text) and (text[end].isalnum() or text[end] in "_-."):
                end += 1
            text = text[:index] + "[REDACTED]" + text[end:]
            lower = text.lower()
            start = index + len("[REDACTED]")
    return text


def _redact_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    hostname = parsed.hostname or ""
    if parsed.port is not None:
        hostname = f"{hostname}:{parsed.port}"
    return urllib.parse.urlunparse(
        parsed._replace(netloc=hostname, path="/[OPAQUE]", params="", query="", fragment="")
    )


def _close_all(values: Iterable[Any]) -> None:
    for value in tuple(values):
        try:
            value.close()
        except Exception:
            continue


def _mapping_result(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "worker_result"):
        return {
            "result_type": type(value).__name__,
            "worker_result": asdict(value.worker_result),
            "event_records": [
                asdict(item) if hasattr(item, "__dataclass_fields__") else item
                for item in getattr(value, "event_records", ())
            ],
        }
    try:
        canonical_json(value)
    except (TypeError, ValueError):
        return {"result_type": type(value).__name__, "representation": repr(value)[:4096]}
    return {"value": value}


def _unavailable(definition: BackendDefinition, message: str) -> BackendTransportError:
    return BackendTransportError(
        BackendFailureKind.BACKEND_UNAVAILABLE,
        message,
        retryable=True,
        recovery_intent=BackendRecoveryIntent.CHANGE_BACKEND,
        backend_id=definition.backend_id,
    )


def _safe_container_name(value: str) -> str:
    normalized = "".join(character.lower() if character.isalnum() else "-" for character in value)
    normalized = "-".join(part for part in normalized.split("-") if part)
    return normalized[:63] or f"zyra-{uuid.uuid4().hex[:12]}"


__all__ = [
    "BackendHealthProbe",
    "BackendTransport",
    "BackendTransportError",
    "BackendTransportFrame",
    "BackendTransportRegistry",
    "BackendTransportRequest",
    "BackendTransportResponse",
    "DispatchCancellation",
    "DockerBackendTransport",
    "HttpBackendTransport",
    "InProcessBackendTransport",
    "LocalProcessBackendTransport",
]
