from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Iterable, Mapping, Sequence

from .models import BackendDefinition, BackendKind, checksum


class RemoteBackendControlError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        backend_id: str,
        http_status: int | None = None,
        retryable: bool = False,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.backend_id = backend_id
        self.http_status = http_status
        self.retryable = retryable
        self.detail = dict(detail or {})

    def safe_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "backend_id": self.backend_id,
            "http_status": self.http_status,
            "retryable": self.retryable,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True, slots=True)
class RemoteDispatchStatus:
    dispatch_id: str
    envelope_id: str
    run_id: str
    task_id: str
    turn_id: str
    state: str
    provider_route_id: str
    provider_route_checksum: str
    m0_execution_ref: str
    deadline_at: float
    accepted_at: float
    updated_at: float
    output_observed: bool
    revision: int
    result_digest: str | None
    error: Mapping[str, Any] | None

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> RemoteDispatchStatus:
        required = {
            "dispatch_id",
            "envelope_id",
            "run_id",
            "task_id",
            "turn_id",
            "state",
            "provider_route_id",
            "provider_route_checksum",
            "m0_execution_ref",
        }
        missing = sorted(name for name in required if not str(value.get(name) or "").strip())
        if missing:
            raise ValueError(f"remote dispatch status is missing fields: {', '.join(missing)}")
        state = str(value["state"])
        if state not in {
            "accepted",
            "running",
            "cancelling",
            "succeeded",
            "failed",
            "cancelled",
            "reconcile_required",
        }:
            raise ValueError(f"remote dispatch status is invalid: {state}")
        error = value.get("error")
        if error is not None and not isinstance(error, Mapping):
            raise ValueError("remote dispatch error must be an object or null")
        return cls(
            dispatch_id=str(value["dispatch_id"]),
            envelope_id=str(value["envelope_id"]),
            run_id=str(value["run_id"]),
            task_id=str(value["task_id"]),
            turn_id=str(value["turn_id"]),
            state=state,
            provider_route_id=str(value["provider_route_id"]),
            provider_route_checksum=str(value["provider_route_checksum"]),
            m0_execution_ref=str(value["m0_execution_ref"]),
            deadline_at=float(value.get("deadline_at") or 0.0),
            accepted_at=float(value.get("accepted_at") or 0.0),
            updated_at=float(value.get("updated_at") or 0.0),
            output_observed=bool(value.get("output_observed", False)),
            revision=int(value.get("revision") or 0),
            result_digest=(str(value["result_digest"]) if value.get("result_digest") else None),
            error=None if error is None else dict(error),
        )

    @property
    def terminal(self) -> bool:
        return self.state in {
            "succeeded",
            "failed",
            "cancelled",
            "reconcile_required",
        }

    @property
    def requires_reconcile(self) -> bool:
        return self.state == "reconcile_required" or (
            self.output_observed and self.state in {"failed", "cancelled"}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "envelope_id": self.envelope_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "turn_id": self.turn_id,
            "state": self.state,
            "provider_route_id": self.provider_route_id,
            "provider_route_checksum": self.provider_route_checksum,
            "m0_execution_ref": self.m0_execution_ref,
            "deadline_at": self.deadline_at,
            "accepted_at": self.accepted_at,
            "updated_at": self.updated_at,
            "output_observed": self.output_observed,
            "revision": self.revision,
            "result_digest": self.result_digest,
            "error": None if self.error is None else dict(self.error),
        }


@dataclass(frozen=True, slots=True)
class RemoteBackendHealth:
    backend_id: str
    ok: bool
    accepting: bool
    generation: str
    runtime_worker: str
    active_dispatches: int
    terminal_dispatches: int
    maximum_concurrency: int
    operations: tuple[str, ...]
    capabilities: tuple[str, ...]
    checked_at: float
    latency_milliseconds: float

    @classmethod
    def from_wire(
        cls,
        backend_id: str,
        value: Mapping[str, Any],
        *,
        latency_milliseconds: float,
    ) -> RemoteBackendHealth:
        actual_backend = str(value.get("backend_id") or "")
        if actual_backend != backend_id:
            raise ValueError("remote health backend identity mismatch")
        return cls(
            backend_id=backend_id,
            ok=bool(value.get("ok", False)),
            accepting=bool(value.get("accepting", False)),
            generation=str(value.get("generation") or ""),
            runtime_worker=str(value.get("runtime_worker") or ""),
            active_dispatches=int(value.get("active_dispatches") or 0),
            terminal_dispatches=int(value.get("terminal_dispatches") or 0),
            maximum_concurrency=int(value.get("maximum_concurrency") or 0),
            operations=_string_tuple(value.get("operations")),
            capabilities=_string_tuple(value.get("capabilities")),
            checked_at=float(value.get("checked_at") or time.time()),
            latency_milliseconds=max(0.0, latency_milliseconds),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "ok": self.ok,
            "accepting": self.accepting,
            "generation": self.generation,
            "runtime_worker": self.runtime_worker,
            "active_dispatches": self.active_dispatches,
            "terminal_dispatches": self.terminal_dispatches,
            "maximum_concurrency": self.maximum_concurrency,
            "operations": list(self.operations),
            "capabilities": list(self.capabilities),
            "checked_at": self.checked_at,
            "latency_milliseconds": self.latency_milliseconds,
        }


@dataclass(frozen=True, slots=True)
class RemoteControlReceipt:
    backend_id: str
    action: str
    effective: bool
    requested_at: float
    completed_at: float
    statuses: tuple[RemoteDispatchStatus, ...] = ()
    health: RemoteBackendHealth | None = None
    response_digest: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "action": self.action,
            "effective": self.effective,
            "requested_at": self.requested_at,
            "completed_at": self.completed_at,
            "statuses": [status.to_dict() for status in self.statuses],
            "health": None if self.health is None else self.health.to_dict(),
            "response_digest": self.response_digest,
            "metadata": dict(self.metadata),
        }


class RemoteBackendControlClient:
    """Typed, bounded control client for Zyra-owned edge/cloud workers."""

    def __init__(
        self,
        definition: BackendDefinition,
        *,
        opener: urllib.request.OpenerDirector | None = None,
        maximum_response_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if definition.kind not in {BackendKind.EDGE_HTTP, BackendKind.CLOUD_HTTP}:
            raise ValueError("remote control requires an edge_http or cloud_http backend")
        if maximum_response_bytes < 1_024:
            raise ValueError("remote control response budget must be at least 1024 bytes")
        self.definition = definition
        self.maximum_response_bytes = maximum_response_bytes
        self.opener = opener or urllib.request.build_opener(_SameOriginRedirectHandler())
        self.base_endpoint = _validated_base_endpoint(definition)

    def health(self) -> RemoteBackendHealth:
        started = time.monotonic()
        value = self._request(
            "GET",
            "/health",
            timeout=self.definition.limits.health_timeout_seconds,
            allow_not_ok=True,
        )
        return RemoteBackendHealth.from_wire(
            self.definition.backend_id,
            value,
            latency_milliseconds=(time.monotonic() - started) * 1_000,
        )

    def dispatches(self) -> tuple[RemoteDispatchStatus, ...]:
        value = self._request("GET", "/v1/dispatches")
        items = value.get("dispatches")
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
            raise self._protocol_error("remote dispatch listing is not an array")
        statuses = tuple(
            RemoteDispatchStatus.from_wire(_mapping(item, "remote dispatch status"))
            for item in items
        )
        identities = [item.dispatch_id for item in statuses]
        if len(set(identities)) != len(identities):
            raise self._protocol_error("remote dispatch listing contains duplicate identities")
        return statuses

    def status(self, dispatch_id: str) -> RemoteDispatchStatus:
        dispatch_id = _identifier(dispatch_id, "dispatch_id")
        value = self._request("GET", f"/v1/dispatches/{urllib.parse.quote(dispatch_id, safe='')}")
        return RemoteDispatchStatus.from_wire(_mapping(value.get("dispatch"), "remote dispatch"))

    def cancel_dispatch(self, dispatch_id: str, reason: str) -> RemoteControlReceipt:
        dispatch_id = _identifier(dispatch_id, "dispatch_id")
        return self._dispatch_action(
            "cancel_dispatch",
            f"/v1/dispatches/{urllib.parse.quote(dispatch_id, safe='')}/cancel",
            reason,
        )

    def cancel_envelope(self, envelope_id: str, reason: str) -> RemoteControlReceipt:
        envelope_id = _identifier(envelope_id, "envelope_id")
        return self._dispatch_action(
            "cancel_envelope",
            f"/v1/envelopes/{urllib.parse.quote(envelope_id, safe='')}/cancel",
            reason,
        )

    def drain(self, reason: str) -> RemoteControlReceipt:
        requested_at = time.time()
        value = self._request("POST", "/v1/control/drain", {"reason": _reason(reason)})
        statuses = self._decode_statuses(value.get("active_dispatches"), "active_dispatches")
        completed_at = time.time()
        return RemoteControlReceipt(
            backend_id=self.definition.backend_id,
            action="drain",
            effective=True,
            requested_at=requested_at,
            completed_at=completed_at,
            statuses=statuses,
            response_digest=checksum(value),
            metadata={"interrupted_count": len(statuses)},
        )

    def resume(self) -> RemoteControlReceipt:
        requested_at = time.time()
        value = self._request("POST", "/v1/control/resume", {})
        health = RemoteBackendHealth.from_wire(
            self.definition.backend_id,
            value,
            latency_milliseconds=(time.time() - requested_at) * 1_000,
        )
        return RemoteControlReceipt(
            backend_id=self.definition.backend_id,
            action="resume",
            effective=health.accepting,
            requested_at=requested_at,
            completed_at=time.time(),
            health=health,
            response_digest=checksum(value),
        )

    def wait_terminal(
        self,
        dispatch_id: str,
        *,
        deadline_at: float,
        poll_seconds: float = 0.05,
    ) -> RemoteDispatchStatus:
        if poll_seconds <= 0:
            raise ValueError("remote dispatch poll interval must be positive")
        while True:
            status = self.status(dispatch_id)
            if status.terminal:
                return status
            if time.time() >= deadline_at:
                raise RemoteBackendControlError(
                    "remote_control_timeout",
                    "remote dispatch did not become terminal before deadline",
                    backend_id=self.definition.backend_id,
                    retryable=not status.output_observed,
                    detail={
                        "dispatch_id": status.dispatch_id,
                        "state": status.state,
                        "output_observed": status.output_observed,
                    },
                )
            time.sleep(min(poll_seconds, max(0.001, deadline_at - time.time())))

    def _dispatch_action(self, action: str, path: str, reason: str) -> RemoteControlReceipt:
        requested_at = time.time()
        value = self._request("POST", path, {"reason": _reason(reason)})
        status = RemoteDispatchStatus.from_wire(_mapping(value.get("dispatch"), "remote dispatch"))
        completed_at = time.time()
        return RemoteControlReceipt(
            backend_id=self.definition.backend_id,
            action=action,
            effective=status.state in {"cancelling", "cancelled", "reconcile_required"},
            requested_at=requested_at,
            completed_at=completed_at,
            statuses=(status,),
            response_digest=checksum(value),
            metadata={
                "output_observed": status.output_observed,
                "requires_reconcile": status.requires_reconcile,
            },
        )

    def _decode_statuses(self, value: Any, name: str) -> tuple[RemoteDispatchStatus, ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise self._protocol_error(f"remote {name} is not an array")
        return tuple(
            RemoteDispatchStatus.from_wire(_mapping(item, "remote dispatch status"))
            for item in value
        )

    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        allow_not_ok: bool = False,
    ) -> dict[str, Any]:
        url = _control_url(self.base_endpoint, path)
        encoded = None if body is None else json.dumps(
            dict(body),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "accept": "application/json",
            "cache-control": "no-store",
            "user-agent": "Zyra-BackendControl/1",
        }
        if encoded is not None:
            headers["content-type"] = "application/json"
        request = urllib.request.Request(url, data=encoded, headers=headers, method=method)
        try:
            with self.opener.open(
                request,
                timeout=timeout or self.definition.limits.connect_timeout_seconds,
            ) as response:
                raw = _read_bounded(response, self.maximum_response_bytes)
                status = int(getattr(response, "status", HTTPStatus.OK))
        except urllib.error.HTTPError as error:
            raw = _read_bounded(error, self.maximum_response_bytes)
            raise self._response_error(int(error.code), raw, url) from error
        except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout, OSError) as error:
            reason = str(getattr(error, "reason", error))
            raise RemoteBackendControlError(
                "remote_control_unavailable",
                f"remote backend control request failed: {reason}",
                backend_id=self.definition.backend_id,
                retryable=True,
                detail={"endpoint": _redact_url(url), "method": method},
            ) from error
        if status < 200 or status >= 300:
            raise self._response_error(status, raw, url)
        try:
            value = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise self._protocol_error("remote backend control returned invalid JSON") from error
        if not isinstance(value, Mapping):
            raise self._protocol_error("remote backend control response must be an object")
        if value.get("ok") is False and not allow_not_ok:
            raise self._response_error(status, raw, url)
        return dict(value)

    def _response_error(self, status: int, raw: bytes, url: str) -> RemoteBackendControlError:
        value: Mapping[str, Any] = {}
        try:
            decoded = json.loads(raw.decode("utf-8", errors="strict"))
            if isinstance(decoded, Mapping):
                value = decoded
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        error = value.get("error")
        detail = dict(error) if isinstance(error, Mapping) else {}
        code = str(detail.get("kind") or detail.get("code") or "remote_control_rejected")
        message = str(detail.get("message") or f"remote backend control returned HTTP {status}")
        return RemoteBackendControlError(
            code,
            message,
            backend_id=self.definition.backend_id,
            http_status=status,
            retryable=status in {408, 425, 429, 500, 502, 503, 504},
            detail={"endpoint": _redact_url(url), "remote": detail},
        )

    def _protocol_error(self, message: str) -> RemoteBackendControlError:
        return RemoteBackendControlError(
            "remote_control_protocol_error",
            message,
            backend_id=self.definition.backend_id,
            retryable=False,
        )


class RemoteBackendControlFanout:
    def __init__(self, definitions: Iterable[BackendDefinition]) -> None:
        self._clients = {
            definition.backend_id: RemoteBackendControlClient(definition)
            for definition in definitions
            if definition.kind in {BackendKind.EDGE_HTTP, BackendKind.CLOUD_HTTP}
        }

    def health(self) -> tuple[RemoteBackendHealth, ...]:
        outputs: list[RemoteBackendHealth] = []
        for backend_id in sorted(self._clients):
            outputs.append(self._clients[backend_id].health())
        return tuple(outputs)

    def drain(self, backend_ids: Iterable[str], reason: str) -> tuple[RemoteControlReceipt, ...]:
        return self._apply(backend_ids, lambda client: client.drain(reason))

    def resume(self, backend_ids: Iterable[str]) -> tuple[RemoteControlReceipt, ...]:
        return self._apply(backend_ids, lambda client: client.resume())

    def cancel_envelope(
        self,
        backend_id: str,
        envelope_id: str,
        reason: str,
    ) -> RemoteControlReceipt:
        return self.require(backend_id).cancel_envelope(envelope_id, reason)

    def require(self, backend_id: str) -> RemoteBackendControlClient:
        try:
            return self._clients[backend_id]
        except KeyError as error:
            raise KeyError(f"remote backend control client not found: {backend_id}") from error

    def _apply(self, backend_ids: Iterable[str], operation: Any) -> tuple[RemoteControlReceipt, ...]:
        outputs: list[RemoteControlReceipt] = []
        seen: set[str] = set()
        for backend_id in backend_ids:
            if backend_id in seen:
                continue
            seen.add(backend_id)
            outputs.append(operation(self.require(backend_id)))
        return tuple(outputs)


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Any,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> Any:
        source = urllib.parse.urlparse(request.full_url)
        target = urllib.parse.urlparse(new_url)
        if (source.scheme, source.hostname, source.port) != (
            target.scheme,
            target.hostname,
            target.port,
        ):
            raise urllib.error.HTTPError(
                request.full_url,
                code,
                "cross-origin remote backend control redirect rejected",
                headers,
                file_pointer,
            )
        return super().redirect_request(
            request,
            file_pointer,
            code,
            message,
            headers,
            new_url,
        )


def _validated_base_endpoint(definition: BackendDefinition) -> str:
    endpoint = str(definition.endpoint or "").rstrip("/")
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("remote backend endpoint must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("remote backend endpoint must not contain credentials")
    if parsed.fragment or parsed.query:
        raise ValueError("remote backend endpoint must not contain query or fragment")
    allowed = {str(item).lower() for item in definition.metadata.get("allowed_hosts", ())}
    if allowed and str(parsed.hostname).lower() not in allowed:
        raise ValueError("remote backend endpoint host is outside allowed_hosts")
    return endpoint


def _control_url(base_endpoint: str, path: str) -> str:
    if not path.startswith("/"):
        raise ValueError("remote backend control path must be absolute")
    base = urllib.parse.urlparse(base_endpoint)
    value = urllib.parse.urlparse(f"{base_endpoint}{path}")
    if (base.scheme, base.hostname, base.port) != (value.scheme, value.hostname, value.port):
        raise ValueError("remote backend control URL escaped configured origin")
    return value.geturl()


def _read_bounded(response: Any, maximum_bytes: int) -> bytes:
    body = response.read(maximum_bytes + 1)
    if len(body) > maximum_bytes:
        raise ValueError("remote backend control response exceeded byte budget")
    return body


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(sorted({str(item) for item in value if str(item).strip()}))


def _identifier(value: str, name: str) -> str:
    value = value.strip()
    if not value or any(character in value for character in "/\\?#"):
        raise ValueError(f"{name} is invalid")
    return value


def _reason(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("remote backend control reason is required")
    if len(value) > 4_096:
        raise ValueError("remote backend control reason is too long")
    return value


def _redact_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urllib.parse.urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))


__all__ = [
    "RemoteBackendControlClient",
    "RemoteBackendControlError",
    "RemoteBackendControlFanout",
    "RemoteBackendHealth",
    "RemoteControlReceipt",
    "RemoteDispatchStatus",
]
