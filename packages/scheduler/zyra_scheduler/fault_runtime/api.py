from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any

from zyra_core import TaskState

from .contracts import ContinuationMode, InjectionKind, runtime_id
from .injection import FaultInjectionRuntime
from .runtime import FaultRuntimeApplication, WatchdogRuntimeError


@dataclass(frozen=True, slots=True)
class FaultApiResponse:
    status: HTTPStatus
    body: Mapping[str, Any]
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "body", dict(self.body))
        object.__setattr__(self, "headers", dict(self.headers))


class FaultApiError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: HTTPStatus = HTTPStatus.BAD_REQUEST,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = dict(details or {})

    def response(self) -> FaultApiResponse:
        return FaultApiResponse(
            status=self.status,
            body={
                "schema": "zyra.fault-api-error/v1",
                "error": self.code,
                "message": str(self),
                "details": self.details,
            },
        )


_KIND_ALIASES = {
    "worker": InjectionKind.WORKER_LOST,
    "worker_lost": InjectionKind.WORKER_LOST,
    "worker-lost": InjectionKind.WORKER_LOST,
    "tool": InjectionKind.TOOL_TIMEOUT,
    "tool_timeout": InjectionKind.TOOL_TIMEOUT,
    "tool-timeout": InjectionKind.TOOL_TIMEOUT,
    "browser": InjectionKind.BROWSER_CRASH,
    "browser_crash": InjectionKind.BROWSER_CRASH,
    "browser-crash": InjectionKind.BROWSER_CRASH,
    "model": InjectionKind.MODEL_FAILURE,
    "model_failure": InjectionKind.MODEL_FAILURE,
    "model-failure": InjectionKind.MODEL_FAILURE,
    "provider_failure": InjectionKind.MODEL_FAILURE,
    "workspace": InjectionKind.WORKSPACE_CORRUPT,
    "workspace_corrupt": InjectionKind.WORKSPACE_CORRUPT,
    "workspace-corrupt": InjectionKind.WORKSPACE_CORRUPT,
}

_TARGET_KEYS = {
    "session_id",
    "node_id",
    "attempt_id",
    "tool_call_id",
    "tool_name",
    "worker_id",
    "backend_id",
    "provider_id",
    "workspace_id",
    "browser_session_id",
    "mcp_server_id",
    "subagent_task_id",
    "source_state_revision",
}

_PARAMETER_KEYS = {
    "deadline_ms",
    "elapsed_ms",
    "exit_code",
    "status_code",
    "reason_code",
    "scenario",
}


def parse_injection_command(raw: str) -> dict[str, Any]:
    """Parse a bounded `/inject` argument string without identity inference.

    Every critical identity must appear as an explicit ``name=value`` token.
    Free-form summaries, exception text, and positional values are rejected.
    """

    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError as error:
        raise FaultApiError("invalid_injection_syntax", str(error)) from error
    if tokens and tokens[0] == "/inject":
        tokens = tokens[1:]
    if not tokens:
        raise FaultApiError(
            "missing_injection_kind",
            "fault injection requires one of worker_lost, tool_timeout, browser_crash, model_failure, workspace_corrupt",
        )
    kind = _KIND_ALIASES.get(tokens[0].strip().lower())
    if kind is None:
        raise FaultApiError("unknown_injection_kind", f"unsupported injection kind: {tokens[0]}")
    target: dict[str, Any] = {}
    parameters: dict[str, Any] = {}
    continuation = ContinuationMode.AUTO.value
    idempotency_key = ""
    for token in tokens[1:]:
        if "=" not in token:
            raise FaultApiError(
                "positional_injection_value_rejected",
                f"injection values must use explicit name=value syntax: {token}",
                details={"critical_ref_source": "structured_refs_only"},
            )
        key, value = token.split("=", 1)
        key = key.strip().lower().replace("-", "_")
        value = value.strip()
        if not value:
            raise FaultApiError("empty_injection_value", f"injection field is empty: {key}")
        if key in _TARGET_KEYS:
            target[key] = int(value) if key == "source_state_revision" else value
        elif key in _PARAMETER_KEYS:
            parameters[key] = int(value) if key.endswith("_ms") or key.endswith("_code") else value
        elif key == "continuation":
            try:
                continuation = ContinuationMode(value).value
            except ValueError as error:
                raise FaultApiError("invalid_continuation", f"unsupported continuation: {value}") from error
        elif key == "idempotency_key":
            idempotency_key = value
        else:
            raise FaultApiError("unknown_injection_field", f"unsupported injection field: {key}")
    return {
        "kind": kind.value,
        "target": target,
        "parameters": parameters,
        "continuation": continuation,
        "idempotency_key": idempotency_key,
    }


def normalize_injection_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = str(payload.get("raw") or "").strip()
    parsed = parse_injection_command(raw) if raw else {}
    selected = {**parsed, **dict(payload)}
    raw_kind = str(selected.get("kind") or "").strip().lower()
    kind = _KIND_ALIASES.get(raw_kind)
    if kind is None:
        try:
            kind = InjectionKind(raw_kind)
        except ValueError as error:
            raise FaultApiError("unknown_injection_kind", f"unsupported injection kind: {raw_kind}") from error
    target = dict(parsed.get("target") or {})
    explicit_target = selected.get("target")
    if explicit_target is not None:
        if not isinstance(explicit_target, Mapping):
            raise FaultApiError("invalid_injection_target", "target must be an object")
        target.update(dict(explicit_target))
    parameters = dict(parsed.get("parameters") or {})
    explicit_parameters = selected.get("parameters")
    if explicit_parameters is not None:
        if not isinstance(explicit_parameters, Mapping):
            raise FaultApiError("invalid_injection_parameters", "parameters must be an object")
        parameters.update(dict(explicit_parameters))
    return {
        "kind": kind.value,
        "target": target,
        "parameters": parameters,
        "continuation": str(selected.get("continuation") or ContinuationMode.AUTO.value),
        "idempotency_key": str(selected.get("idempotency_key") or ""),
    }


class FaultRuntimeApiService:
    """HTTP-independent task route service for watchdog and injection state."""

    def __init__(self, runtime: FaultRuntimeApplication) -> None:
        self.runtime = runtime
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.runtime.close()
            self.closed = True

    def route_get(
        self,
        parts: tuple[str, ...],
        *,
        task_state: TaskState | None,
    ) -> FaultApiResponse | None:
        if len(parts) != 3 or parts[0] != "tasks" or parts[2] != "faults":
            return None
        if task_state is None:
            return FaultApiResponse(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
        try:
            snapshot = dict(self.runtime.snapshot(task_id=task_state.task_id))
            snapshot["api"] = self.contract()
            return FaultApiResponse(
                HTTPStatus.OK,
                snapshot,
                headers={"Cache-Control": "no-store"},
            )
        finally:
            self.runtime.store.release_connection()

    def route_post(
        self,
        parts: tuple[str, ...],
        payload: Mapping[str, Any],
        *,
        task_state: TaskState | None,
        requested_by: str = "api-user",
    ) -> FaultApiResponse | None:
        if len(parts) not in {4, 5} or parts[0] != "tasks" or parts[2] != "faults":
            return None
        if task_state is None:
            return FaultApiResponse(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
        action = "/".join(parts[3:])
        try:
            if action == "inject":
                return self._inject(payload, task_state=task_state, requested_by=requested_by)
            if action == "observers":
                return self._observer_control(payload)
            if action == "sources/bind":
                result = self.runtime.integration.bind_source(task_state, payload)
                return FaultApiResponse(HTTPStatus.CREATED, result.to_dict(), headers={"Cache-Control": "no-store"})
            if action == "sources/observe":
                result = self.runtime.integration.observe_source(task_state, payload)
                return FaultApiResponse(HTTPStatus.OK, result.to_dict(), headers={"Cache-Control": "no-store"})
            if action == "runtime-events":
                result = self.runtime.integration.ingest_typescript_event(task_state, payload)
                return FaultApiResponse(HTTPStatus.ACCEPTED, result.to_dict(), headers={"Cache-Control": "no-store"})
            if action == "observations":
                result = self.runtime.integration.observations.ingest(task_state, payload)
                return FaultApiResponse(HTTPStatus.ACCEPTED, result.to_dict(), headers={"Cache-Control": "no-store"})
            if action == "handoffs/dispatch":
                result = self.runtime.integration.dispatch_handoffs(
                    str(payload.get("consumer_id") or ""),
                    task_id=task_state.task_id,
                    limit=int(payload.get("limit") or 20),
                    now_ms=(int(payload["now_ms"]) if payload.get("now_ms") is not None else None),
                )
                return FaultApiResponse(HTTPStatus.OK, result.to_dict(), headers={"Cache-Control": "no-store"})
        except FaultApiError as error:
            return error.response()
        except WatchdogRuntimeError as error:
            return FaultApiResponse(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "schema": "zyra.fault-api-error/v1",
                    "error": error.code,
                    "message": str(error),
                    "fallback": False,
                },
            )
        except (TypeError, ValueError, KeyError, RuntimeError) as error:
            return FaultApiResponse(
                HTTPStatus.BAD_REQUEST,
                {
                    "schema": "zyra.fault-api-error/v1",
                    "error": "invalid_fault_request",
                    "message": str(error),
                },
            )
        finally:
            self.runtime.store.release_connection()
        return FaultApiResponse(HTTPStatus.NOT_FOUND, {"error": "fault_action_not_found", "action": action})

    def release_idle_connection(self) -> None:
        self.runtime.store.release_connection()

    def command_inject(
        self,
        raw: str,
        *,
        state: TaskState,
        requested_by: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        payload = parse_injection_command(raw)
        payload["idempotency_key"] = payload.get("idempotency_key") or idempotency_key
        response = self._inject(payload, task_state=state, requested_by=requested_by)
        if response.status >= HTTPStatus.BAD_REQUEST:
            raise FaultApiError(
                str(response.body.get("error") or "fault_injection_failed"),
                str(response.body.get("message") or "fault injection failed"),
                status=response.status,
                details=dict(response.body),
            )
        return response.body

    def _inject(
        self,
        payload: Mapping[str, Any],
        *,
        task_state: TaskState,
        requested_by: str,
    ) -> FaultApiResponse:
        normalized = normalize_injection_payload(payload)
        request = FaultInjectionRuntime.request_from_mapping(
            normalized,
            state=task_state,
            requested_by=requested_by,
            default_idempotency_key=runtime_id("fault-api-idem"),
        )
        receipt = self.runtime.inject(request, task_state=task_state)
        status = HTTPStatus.OK if receipt.duplicate else HTTPStatus.CREATED
        if not receipt.ok:
            status = HTTPStatus.CONFLICT
        return FaultApiResponse(
            status,
            {
                **receipt.to_dict(),
                "task_projection": self.runtime.writer.task_projection(task_state.task_id),
                "state_saved_by_caller": True,
            },
            headers={"Cache-Control": "no-store", "Idempotency-Key": request.idempotency_key},
        )

    def _observer_control(self, payload: Mapping[str, Any]) -> FaultApiResponse:
        observer_id = str(payload.get("observer_id") or "").strip()
        action = str(payload.get("action") or "").strip().lower()
        if not observer_id:
            raise FaultApiError("missing_observer_id", "observer control requires observer_id")
        if action == "disable":
            value = self.runtime.watchdog.disable_observer(
                observer_id,
                reason=str(payload.get("reason") or "disabled through fault API"),
            )
        elif action in {"enable", "start"}:
            value = self.runtime.watchdog.enable_observer(observer_id)
        else:
            raise FaultApiError("invalid_observer_action", f"unsupported observer action: {action}")
        return FaultApiResponse(
            HTTPStatus.OK,
            {
                "schema": "zyra.watchdog-observer-control/v1",
                "observer": value,
                "injection_observer_changed": False,
            },
        )

    @staticmethod
    def contract() -> dict[str, Any]:
        return {
            "schema": "zyra.fault-runtime-api/v1",
            "routes": [
                {"method": "GET", "path": "/tasks/{task_id}/faults"},
                {"method": "POST", "path": "/tasks/{task_id}/faults/inject"},
                {"method": "POST", "path": "/tasks/{task_id}/faults/observers"},
                {"method": "POST", "path": "/tasks/{task_id}/faults/sources/bind"},
                {"method": "POST", "path": "/tasks/{task_id}/faults/sources/observe"},
                {"method": "POST", "path": "/tasks/{task_id}/faults/runtime-events"},
                {"method": "POST", "path": "/tasks/{task_id}/faults/observations"},
                {"method": "POST", "path": "/tasks/{task_id}/faults/handoffs/dispatch"},
            ],
            "command": "/inject <kind> name=value...",
            "idempotency": "run_id + task_id + idempotency_key",
            "critical_ref_source": "structured target object or explicit name=value tokens only",
            "observer_disable_is_independent_from_injection": True,
            "external_resource_mutation": False,
        }


__all__ = [
    "FaultApiError",
    "FaultApiResponse",
    "FaultRuntimeApiService",
    "normalize_injection_payload",
    "parse_injection_command",
]
