from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import re
import secrets
import threading
import time
import traceback
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Generic, TypeVar


_TRACE_ID = contextvars.ContextVar[str]("zyra_product_trace_id", default="")
_SPAN_ID = contextvars.ContextVar[str]("zyra_product_span_id", default="")
_CORRELATION_ID = contextvars.ContextVar[str](
    "zyra_product_correlation_id", default=""
)
_CAUSATION_ID = contextvars.ContextVar[str](
    "zyra_product_causation_id", default=""
)
_SECRET_NAME = re.compile(
    r"(secret|password|passwd|token|authorization|cookie|api[_-]?key|private[_-]?key|credential)",
    re.IGNORECASE,
)
_SECRET_TEXT = re.compile(
    r"(?i)(bearer\s+)[^\s,;]+|(basic\s+)[^\s,;]+|"
    r"((?:secret|password|token|api[_-]?key)\s*[=:]\s*)[^\s,;]+|"
    r"(sk-)[A-Za-z0-9_-]{12,}"
)
_T = TypeVar("_T")


class LifecyclePhase(StrEnum):
    CREATED = "created"
    CONFIGURING = "configuring"
    MIGRATING = "migrating"
    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class Severity(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class ResourceState(StrEnum):
    REGISTERED = "registered"
    STARTED = "started"
    DRAINING = "draining"
    CLOSED = "closed"
    FAILED = "failed"


class LifecycleError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: LifecyclePhase,
        retryable: bool = False,
        owner: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.retryable = retryable
        self.owner = owner
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    process_id: str
    generation_id: str
    pid: int
    started_at_ns: int
    executable_fingerprint: str
    configuration_digest: str

    @classmethod
    def create(
        cls,
        *,
        configuration_digest: str,
        process_id: str | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> ProcessIdentity:
        pid = os.getpid()
        started = clock_ns()
        executable = str(Path(os.sys.executable).resolve())
        material = f"{pid}:{started}:{executable}".encode("utf-8")
        fingerprint = "sha256:" + hashlib.sha256(material).hexdigest()
        return cls(
            process_id=process_id or f"zyra-process-{pid}",
            generation_id="generation-" + secrets.token_urlsafe(18),
            pid=pid,
            started_at_ns=started,
            executable_fingerprint=fingerprint,
            configuration_digest=configuration_digest,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.process-identity/v1",
            "process_id": self.process_id,
            "generation_id": self.generation_id,
            "pid": self.pid,
            "started_at_ns": self.started_at_ns,
            "executable_fingerprint": self.executable_fingerprint,
            "configuration_digest": self.configuration_digest,
        }


@dataclass(frozen=True, slots=True)
class TraceContext:
    trace_id: str
    span_id: str
    parent_span_id: str
    correlation_id: str
    causation_id: str

    @classmethod
    def current(cls) -> TraceContext:
        return cls(
            trace_id=_TRACE_ID.get(),
            span_id=_SPAN_ID.get(),
            parent_span_id="",
            correlation_id=_CORRELATION_ID.get(),
            causation_id=_CAUSATION_ID.get(),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
        }


@dataclass(frozen=True, slots=True)
class ErrorEnvelope:
    error_id: str
    code: str
    message: str
    error_type: str
    phase: str
    owner: str
    retryable: bool
    trace: TraceContext
    details: Mapping[str, Any]
    occurred_at_ns: int

    @classmethod
    def from_exception(
        cls,
        error: BaseException,
        *,
        phase: LifecyclePhase,
        owner: str = "",
        code: str = "",
        retryable: bool | None = None,
        include_stack: bool = False,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> ErrorEnvelope:
        if isinstance(error, LifecycleError):
            selected_code = code or error.code
            selected_owner = owner or error.owner
            selected_retryable = error.retryable if retryable is None else retryable
            details: dict[str, Any] = dict(error.details)
        else:
            selected_code = code or _exception_code(error)
            selected_owner = owner
            selected_retryable = bool(retryable)
            details = {}
        if include_stack:
            frames = traceback.extract_tb(error.__traceback__)[-16:]
            details["stack"] = [
                {
                    "file": Path(frame.filename).name,
                    "line": frame.lineno,
                    "function": frame.name,
                }
                for frame in frames
            ]
        return cls(
            error_id="error-" + secrets.token_urlsafe(18),
            code=selected_code,
            message=_redact_text(str(error))[:2_048],
            error_type=type(error).__name__,
            phase=phase.value,
            owner=selected_owner,
            retryable=selected_retryable,
            trace=TraceContext.current(),
            details=MappingProxyType(_redact_value(details)),
            occurred_at_ns=clock_ns(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.error-envelope/v1",
            "error_id": self.error_id,
            "code": self.code,
            "message": self.message,
            "error_type": self.error_type,
            "phase": self.phase,
            "owner": self.owner,
            "retryable": self.retryable,
            "trace": self.trace.to_dict(),
            "details": _redact_value(self.details),
            "occurred_at_ns": self.occurred_at_ns,
            "credential_material_exposed": False,
        }


@dataclass(frozen=True, slots=True)
class LifecycleRecord:
    sequence: int
    timestamp_ns: int
    severity: Severity
    component: str
    phase: LifecyclePhase
    event: str
    process: ProcessIdentity
    trace: TraceContext
    attributes: Mapping[str, Any]
    error: ErrorEnvelope | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.lifecycle-record/v1",
            "sequence": self.sequence,
            "timestamp_ns": self.timestamp_ns,
            "severity": self.severity.value,
            "component": self.component,
            "phase": self.phase.value,
            "event": self.event,
            "process": self.process.to_dict(),
            "trace": self.trace.to_dict(),
            "attributes": _redact_value(self.attributes),
            "error": self.error.to_dict() if self.error is not None else None,
        }


@dataclass(slots=True)
class ManagedResource(Generic[_T]):
    name: str
    value: _T
    close: Callable[[_T], Any]
    drain: Callable[[_T, float], Any] | None = None
    required: bool = True
    dependencies: tuple[str, ...] = ()
    state: ResourceState = ResourceState.REGISTERED
    failure: ErrorEnvelope | None = None


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    process: ProcessIdentity
    requested_at_ns: int
    completed_at_ns: int
    closed: tuple[str, ...]
    failed: tuple[str, ...]
    errors: tuple[ErrorEnvelope, ...]

    @property
    def clean(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.shutdown-report/v1",
            "process": self.process.to_dict(),
            "requested_at_ns": self.requested_at_ns,
            "completed_at_ns": self.completed_at_ns,
            "clean": self.clean,
            "closed": list(self.closed),
            "failed": list(self.failed),
            "errors": [item.to_dict() for item in self.errors],
        }


class LifecycleRecorder:
    _TRANSITIONS = MappingProxyType(
        {
            LifecyclePhase.CREATED: {
                LifecyclePhase.CONFIGURING,
                LifecyclePhase.FAILED,
            },
            LifecyclePhase.CONFIGURING: {
                LifecyclePhase.MIGRATING,
                LifecyclePhase.STARTING,
                LifecyclePhase.FAILED,
            },
            LifecyclePhase.MIGRATING: {
                LifecyclePhase.STARTING,
                LifecyclePhase.FAILED,
            },
            LifecyclePhase.STARTING: {
                LifecyclePhase.READY,
                LifecyclePhase.FAILED,
            },
            LifecyclePhase.READY: {
                LifecyclePhase.DRAINING,
                LifecyclePhase.FAILED,
            },
            LifecyclePhase.DRAINING: {
                LifecyclePhase.STOPPING,
                LifecyclePhase.FAILED,
            },
            LifecyclePhase.STOPPING: {
                LifecyclePhase.STOPPED,
                LifecyclePhase.FAILED,
            },
            LifecyclePhase.FAILED: {
                LifecyclePhase.DRAINING,
                LifecyclePhase.STOPPING,
                LifecyclePhase.STOPPED,
            },
            LifecyclePhase.STOPPED: set(),
        }
    )

    def __init__(
        self,
        process: ProcessIdentity,
        *,
        component: str,
        log_path: Path | str | None = None,
        maximum_records: int = 16_384,
        clock_ns: Callable[[], int] = time.time_ns,
        sink: Callable[[LifecycleRecord], Any] | None = None,
    ) -> None:
        if not component.strip():
            raise ValueError("component is required")
        if maximum_records < 64:
            raise ValueError("maximum_records must be at least 64")
        self.process = process
        self.component = component.strip()
        self.log_path = Path(log_path).resolve() if log_path is not None else None
        self._clock_ns = clock_ns
        self._sink = sink
        self._records: deque[LifecycleRecord] = deque(maxlen=maximum_records)
        self._resources: dict[str, ManagedResource[Any]] = {}
        self._phase = LifecyclePhase.CREATED
        self._sequence = 0
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._closed = False
        self.emit(
            "process.created",
            severity=Severity.INFO,
            attributes={"configuration_digest": process.configuration_digest},
        )

    @property
    def phase(self) -> LifecyclePhase:
        with self._lock:
            return self._phase

    def transition(
        self,
        target: LifecyclePhase,
        *,
        event: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> LifecycleRecord:
        with self._lock:
            current = self._phase
            if target is current:
                return self.emit(
                    event or f"process.{target.value}.reaffirmed",
                    attributes=attributes,
                )
            allowed = self._TRANSITIONS[current]
            if target not in allowed:
                raise LifecycleError(
                    "lifecycle_transition_invalid",
                    f"invalid lifecycle transition: {current.value} -> {target.value}",
                    phase=current,
                    owner=self.component,
                    details={
                        "current": current.value,
                        "target": target.value,
                        "allowed": sorted(item.value for item in allowed),
                    },
                )
            self._phase = target
            severity = (
                Severity.ERROR if target is LifecyclePhase.FAILED else Severity.INFO
            )
            return self.emit(
                event or f"process.{target.value}",
                severity=severity,
                attributes={
                    "previous_phase": current.value,
                    **dict(attributes or {}),
                },
            )

    def emit(
        self,
        event: str,
        *,
        severity: Severity = Severity.INFO,
        attributes: Mapping[str, Any] | None = None,
        error: ErrorEnvelope | None = None,
        trace: TraceContext | None = None,
    ) -> LifecycleRecord:
        normalized_event = event.strip()
        if not normalized_event or len(normalized_event) > 192:
            raise ValueError("lifecycle event must be between 1 and 192 characters")
        with self._lock:
            if self._closed and normalized_event != "process.stopped":
                raise LifecycleError(
                    "lifecycle_recorder_closed",
                    "lifecycle recorder is closed",
                    phase=self._phase,
                    owner=self.component,
                )
            self._sequence += 1
            record = LifecycleRecord(
                sequence=self._sequence,
                timestamp_ns=self._clock_ns(),
                severity=severity,
                component=self.component,
                phase=self._phase,
                event=normalized_event,
                process=self.process,
                trace=trace or TraceContext.current(),
                attributes=MappingProxyType(_redact_value(dict(attributes or {}))),
                error=error,
            )
            self._records.append(record)
        self._persist(record)
        if self._sink is not None:
            self._sink(record)
        return record

    def record_error(
        self,
        error: BaseException,
        *,
        event: str,
        code: str = "",
        owner: str = "",
        retryable: bool | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> ErrorEnvelope:
        envelope = ErrorEnvelope.from_exception(
            error,
            phase=self.phase,
            owner=owner or self.component,
            code=code,
            retryable=retryable,
        )
        self.emit(
            event,
            severity=Severity.ERROR,
            attributes=attributes,
            error=envelope,
        )
        return envelope

    @contextlib.contextmanager
    def span(
        self,
        name: str,
        *,
        trace_id: str = "",
        correlation_id: str = "",
        causation_id: str = "",
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[TraceContext]:
        parent_trace = _TRACE_ID.get()
        parent_span = _SPAN_ID.get()
        selected_trace = trace_id or parent_trace or _new_trace_id()
        span_id = _new_span_id()
        selected_correlation = (
            correlation_id or _CORRELATION_ID.get() or selected_trace
        )
        selected_causation = causation_id or _CAUSATION_ID.get()
        context = TraceContext(
            trace_id=selected_trace,
            span_id=span_id,
            parent_span_id=parent_span,
            correlation_id=selected_correlation,
            causation_id=selected_causation,
        )
        tokens = (
            (_TRACE_ID, _TRACE_ID.set(selected_trace)),
            (_SPAN_ID, _SPAN_ID.set(span_id)),
            (_CORRELATION_ID, _CORRELATION_ID.set(selected_correlation)),
            (_CAUSATION_ID, _CAUSATION_ID.set(selected_causation)),
        )
        started = self._clock_ns()
        self.emit(
            f"{name}.started",
            severity=Severity.DEBUG,
            attributes=attributes,
            trace=context,
        )
        try:
            yield context
        except BaseException as error:
            envelope = ErrorEnvelope.from_exception(
                error,
                phase=self.phase,
                owner=self.component,
            )
            self.emit(
                f"{name}.failed",
                severity=Severity.ERROR,
                attributes={
                    **dict(attributes or {}),
                    "duration_ns": max(0, self._clock_ns() - started),
                },
                error=envelope,
                trace=context,
            )
            raise
        else:
            self.emit(
                f"{name}.completed",
                severity=Severity.DEBUG,
                attributes={
                    **dict(attributes or {}),
                    "duration_ns": max(0, self._clock_ns() - started),
                },
                trace=context,
            )
        finally:
            for variable, token in reversed(tokens):
                variable.reset(token)

    def register_resource(
        self,
        name: str,
        value: _T,
        *,
        close: Callable[[_T], Any],
        drain: Callable[[_T, float], Any] | None = None,
        required: bool = True,
        dependencies: tuple[str, ...] = (),
    ) -> _T:
        normalized = name.strip()
        if not normalized:
            raise ValueError("resource name is required")
        with self._lock:
            if normalized in self._resources:
                raise LifecycleError(
                    "lifecycle_resource_duplicate",
                    f"resource is already registered: {normalized}",
                    phase=self._phase,
                    owner=self.component,
                )
            missing = [
                dependency
                for dependency in dependencies
                if dependency not in self._resources
            ]
            if missing:
                raise LifecycleError(
                    "lifecycle_resource_dependency_missing",
                    f"resource dependencies are not registered: {missing}",
                    phase=self._phase,
                    owner=self.component,
                    details={"resource": normalized, "missing": missing},
                )
            self._resources[normalized] = ManagedResource(
                name=normalized,
                value=value,
                close=close,
                drain=drain,
                required=required,
                dependencies=tuple(dependencies),
                state=ResourceState.STARTED,
            )
        self.emit(
            "resource.started",
            attributes={
                "resource": normalized,
                "required": required,
                "dependencies": list(dependencies),
            },
        )
        return value

    def readiness(self) -> dict[str, Any]:
        with self._lock:
            resources = {
                name: {
                    "state": resource.state.value,
                    "required": resource.required,
                    "dependencies": list(resource.dependencies),
                    "failure": (
                        resource.failure.to_dict()
                        if resource.failure is not None
                        else None
                    ),
                }
                for name, resource in sorted(self._resources.items())
            }
            blockers = [
                name
                for name, resource in self._resources.items()
                if resource.required and resource.state is not ResourceState.STARTED
            ]
            return {
                "schema": "zyra.lifecycle-readiness/v1",
                "ready": self._phase is LifecyclePhase.READY and not blockers,
                "phase": self._phase.value,
                "process": self.process.to_dict(),
                "blockers": blockers,
                "resources": resources,
                "sequence": self._sequence,
            }

    def shutdown(self, *, timeout_seconds: float) -> ShutdownReport:
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise ValueError("shutdown timeout must be in (0, 300]")
        requested = self._clock_ns()
        deadline = time.monotonic() + timeout_seconds
        with self._lock:
            if self._phase is LifecyclePhase.STOPPED:
                return ShutdownReport(
                    process=self.process,
                    requested_at_ns=requested,
                    completed_at_ns=self._clock_ns(),
                    closed=tuple(
                        name
                        for name, resource in self._resources.items()
                        if resource.state is ResourceState.CLOSED
                    ),
                    failed=tuple(
                        name
                        for name, resource in self._resources.items()
                        if resource.state is ResourceState.FAILED
                    ),
                    errors=tuple(
                        resource.failure
                        for resource in self._resources.values()
                        if resource.failure is not None
                    ),
                )
        if self.phase not in {
            LifecyclePhase.DRAINING,
            LifecyclePhase.STOPPING,
            LifecyclePhase.FAILED,
        }:
            self.transition(LifecyclePhase.DRAINING)
        elif self.phase is LifecyclePhase.FAILED:
            self.transition(LifecyclePhase.DRAINING)
        errors: list[ErrorEnvelope] = []
        ordered = self._shutdown_order()
        for resource in ordered:
            if resource.drain is None or resource.state is not ResourceState.STARTED:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                envelope = ErrorEnvelope.from_exception(
                    TimeoutError("shutdown drain deadline exhausted"),
                    phase=LifecyclePhase.DRAINING,
                    owner=resource.name,
                    code="lifecycle_drain_timeout",
                    retryable=True,
                )
                resource.state = ResourceState.FAILED
                resource.failure = envelope
                errors.append(envelope)
                continue
            try:
                resource.state = ResourceState.DRAINING
                resource.drain(resource.value, remaining)
            except BaseException as error:
                envelope = ErrorEnvelope.from_exception(
                    error,
                    phase=LifecyclePhase.DRAINING,
                    owner=resource.name,
                    code="lifecycle_resource_drain_failed",
                )
                resource.state = ResourceState.FAILED
                resource.failure = envelope
                errors.append(envelope)
                self.emit(
                    "resource.drain.failed",
                    severity=Severity.ERROR,
                    attributes={"resource": resource.name},
                    error=envelope,
                )
            else:
                resource.state = ResourceState.STARTED
                self.emit(
                    "resource.drained",
                    attributes={"resource": resource.name},
                )
        self.transition(LifecyclePhase.STOPPING)
        closed: list[str] = []
        failed: list[str] = []
        for resource in ordered:
            if resource.state is ResourceState.CLOSED:
                closed.append(resource.name)
                continue
            try:
                resource.close(resource.value)
            except BaseException as error:
                envelope = ErrorEnvelope.from_exception(
                    error,
                    phase=LifecyclePhase.STOPPING,
                    owner=resource.name,
                    code="lifecycle_resource_close_failed",
                )
                resource.state = ResourceState.FAILED
                resource.failure = envelope
                errors.append(envelope)
                failed.append(resource.name)
                self.emit(
                    "resource.close.failed",
                    severity=Severity.ERROR,
                    attributes={"resource": resource.name},
                    error=envelope,
                )
            else:
                resource.state = ResourceState.CLOSED
                closed.append(resource.name)
                self.emit(
                    "resource.closed",
                    attributes={"resource": resource.name},
                )
        report = ShutdownReport(
            process=self.process,
            requested_at_ns=requested,
            completed_at_ns=self._clock_ns(),
            closed=tuple(closed),
            failed=tuple(failed),
            errors=tuple(errors),
        )
        self.transition(
            LifecyclePhase.STOPPED,
            attributes={"clean": report.clean, "failed_resources": list(report.failed)},
        )
        with self._lock:
            self._closed = True
        return report

    def records(
        self,
        *,
        after_sequence: int = 0,
        severity: Severity | None = None,
    ) -> tuple[LifecycleRecord, ...]:
        with self._lock:
            return tuple(
                record
                for record in self._records
                if record.sequence > after_sequence
                and (severity is None or record.severity is severity)
            )

    def _shutdown_order(self) -> tuple[ManagedResource[Any], ...]:
        with self._lock:
            resources = dict(self._resources)
        visiting: set[str] = set()
        visited: set[str] = set()
        result: list[ManagedResource[Any]] = []

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                raise LifecycleError(
                    "lifecycle_resource_dependency_cycle",
                    f"resource dependency cycle includes {name}",
                    phase=self.phase,
                    owner=self.component,
                )
            visiting.add(name)
            resource = resources[name]
            for dependency in resource.dependencies:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)
            result.append(resource)

        for name in resources:
            visit(name)
        result.reverse()
        return tuple(result)

    def _persist(self, record: LifecycleRecord) -> None:
        if self.log_path is None:
            return
        payload = (
            json.dumps(
                record.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            with self.log_path.open("ab") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())


def _new_trace_id() -> str:
    return secrets.token_hex(16)


def _new_span_id() -> str:
    return secrets.token_hex(8)


def _exception_code(error: BaseException) -> str:
    name = type(error).__name__
    parts: list[str] = []
    token = ""
    for character in name:
        if character.isupper() and token:
            parts.append(token.casefold())
            token = character
        else:
            token += character
    if token:
        parts.append(token.casefold())
    return "runtime_" + "_".join(parts)


def _redact_text(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        prefix = next(
            (
                group
                for group in match.groups()
                if group is not None
            ),
            "",
        )
        return f"{prefix}[REDACTED]"

    return _SECRET_TEXT.sub(replace, value)


def _redact_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        return "[DEPTH_LIMIT]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in list(value.items())[:256]:
            rendered = str(key)[:128]
            if _SECRET_NAME.search(rendered):
                result[rendered] = "[REDACTED]"
            else:
                result[rendered] = _redact_value(child, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _redact_value(child, depth=depth + 1)
            for child in list(value)[:256]
        ]
    if isinstance(value, str):
        return _redact_text(value[:8_192])
    if isinstance(value, bytes):
        return {
            "bytes": len(value),
            "digest": "sha256:" + hashlib.sha256(value).hexdigest(),
        }
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(repr(value)[:1_024])
