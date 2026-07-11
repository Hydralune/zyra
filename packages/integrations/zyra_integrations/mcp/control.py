from __future__ import annotations

"""Typed MCP control operations shared by HTTP, commands and workers.

This runtime owns no MCP state. It validates operations, obtains authorization
for every external action, and delegates to the existing MCP domain owners.
"""

import hashlib
import json
import shlex
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, TypeAlias, runtime_checkable

from zyra_core import new_id, now_iso

from .events import sanitize_mcp_value
from .models import JsonValue, stable_digest, to_json_value


class McpControlError(RuntimeError):
    pass


class McpControlValidationError(McpControlError, ValueError):
    pass


class McpControlAuthorizationRequired(McpControlError, PermissionError):
    def __init__(
        self,
        action: str,
        *,
        target: str = "",
        reason: str = "mcp_control_authorization_required",
    ) -> None:
        super().__init__(reason)
        self.action = action
        self.target = target
        self.reason = reason


class McpControlDenied(McpControlAuthorizationRequired):
    pass


class McpControlDisabled(McpControlError):
    pass


class McpControlAction(StrEnum):
    STATUS = "status"
    HEALTH = "health"
    SERVERS = "servers"
    SERVER = "server"
    CATALOG = "catalog"
    TOOLS = "tools"
    RESOURCES = "resources"
    PROMPTS = "prompts"
    ELICITATIONS = "elicitations"
    TASKS = "tasks"
    CONNECT = "connect"
    DISCONNECT = "disconnect"
    RECONNECT = "reconnect"
    REFRESH = "refresh"
    DISABLE = "disable"
    ENABLE = "enable"
    APPROVE = "approve"
    REJECT = "reject"
    AUTH_REFRESH = "auth_refresh"
    AUTH_REVOKE = "auth_revoke"
    RESOURCE_READ = "resource_read"
    PROMPT_GET = "prompt_get"
    ELICITATION_RESOLVE = "elicitation_resolve"
    TASK_POLL = "task_poll"
    TASK_RESULT = "task_result"
    TASK_CANCEL = "task_cancel"
    BOOTSTRAP = "bootstrap"

    @property
    def external_effect(self) -> bool:
        return self in EXTERNAL_ACTIONS

    @property
    def mutation(self) -> bool:
        return self in MUTATING_ACTIONS

    @property
    def observation(self) -> bool:
        return not self.external_effect


EXTERNAL_ACTIONS = frozenset(
    {
        McpControlAction.CONNECT,
        McpControlAction.DISCONNECT,
        McpControlAction.RECONNECT,
        McpControlAction.REFRESH,
        McpControlAction.DISABLE,
        McpControlAction.ENABLE,
        McpControlAction.APPROVE,
        McpControlAction.REJECT,
        McpControlAction.AUTH_REFRESH,
        McpControlAction.AUTH_REVOKE,
        McpControlAction.RESOURCE_READ,
        McpControlAction.PROMPT_GET,
        McpControlAction.ELICITATION_RESOLVE,
        McpControlAction.TASK_POLL,
        McpControlAction.TASK_RESULT,
        McpControlAction.TASK_CANCEL,
        McpControlAction.BOOTSTRAP,
    }
)
MUTATING_ACTIONS = frozenset(
    {
        McpControlAction.CONNECT,
        McpControlAction.DISCONNECT,
        McpControlAction.RECONNECT,
        McpControlAction.REFRESH,
        McpControlAction.DISABLE,
        McpControlAction.ENABLE,
        McpControlAction.APPROVE,
        McpControlAction.REJECT,
        McpControlAction.AUTH_REFRESH,
        McpControlAction.AUTH_REVOKE,
        McpControlAction.ELICITATION_RESOLVE,
        McpControlAction.TASK_CANCEL,
        McpControlAction.BOOTSTRAP,
    }
)


@dataclass(frozen=True, slots=True)
class McpControlContext:
    run_id: str = ""
    task_id: str = ""
    node_id: str = ""
    session_id: str = ""
    worker_request_id: str = ""
    tool_use_id: str = ""
    actor_id: str = ""
    cause_event_id: str = ""
    trace_id: str = ""
    operation_id: str = field(default_factory=lambda: new_id("mcpop"))

    def __post_init__(self) -> None:
        values = (
            self.run_id,
            self.task_id,
            self.node_id,
            self.session_id,
            self.worker_request_id,
            self.tool_use_id,
            self.actor_id,
            self.cause_event_id,
            self.trace_id,
            self.operation_id,
        )
        if any(len(str(value)) > 4096 for value in values):
            raise McpControlValidationError("MCP control identity exceeds maximum length")
        if bool(self.run_id) != bool(self.task_id):
            raise McpControlValidationError("run_id and task_id must be supplied together")

    @property
    def partitioned(self) -> bool:
        return bool(self.run_id and self.task_id)

    def event_context(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "run_id": self.run_id,
                "task_id": self.task_id,
                "node_id": self.node_id,
                "session_id": self.session_id,
                "worker_request_id": self.worker_request_id,
                "cause_event_id": self.cause_event_id,
            }.items()
            if value
        }

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "tool_use_id": self.tool_use_id,
            "actor_id": self.actor_id,
            "cause_event_id": self.cause_event_id,
            "trace_id": self.trace_id,
            "operation_id": self.operation_id,
            "partitioned": self.partitioned,
        }


@dataclass(frozen=True, slots=True)
class McpControlRequest:
    action: McpControlAction | str
    target: str = ""
    arguments: Mapping[str, Any] = field(default_factory=dict)
    context: McpControlContext = field(default_factory=McpControlContext)
    request_id: str = field(default_factory=lambda: new_id("mcpcontrol"))
    idempotency_key: str = ""
    expected_revision: int | None = None
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not isinstance(self.action, McpControlAction):
            try:
                object.__setattr__(
                    self,
                    "action",
                    McpControlAction(str(self.action).strip().casefold()),
                )
            except ValueError as error:
                raise McpControlValidationError(
                    f"unsupported MCP control action: {self.action}"
                ) from error
        target = str(self.target or "").strip()
        if len(target) > 4096:
            raise McpControlValidationError("MCP control target exceeds maximum length")
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "arguments", canonical_arguments(self.arguments))
        if self.expected_revision is not None and self.expected_revision < 0:
            raise McpControlValidationError("expected_revision cannot be negative")
        if not self.request_id or len(self.request_id) > 512:
            raise McpControlValidationError("request_id is invalid")
        if len(self.idempotency_key) > 1024:
            raise McpControlValidationError("idempotency_key exceeds maximum length")
        self.validate_shape()

    @property
    def fingerprint(self) -> str:
        return stable_digest(
            {
                "action": str(self.action),
                "target": self.target,
                "arguments": self.arguments,
                "run_id": self.context.run_id,
                "task_id": self.context.task_id,
                "session_id": self.context.session_id,
                "worker_request_id": self.context.worker_request_id,
                "tool_use_id": self.context.tool_use_id,
                "expected_revision": self.expected_revision,
            }
        )

    def validate_shape(self) -> None:
        no_target = {
            McpControlAction.STATUS,
            McpControlAction.HEALTH,
            McpControlAction.SERVERS,
            McpControlAction.CATALOG,
            McpControlAction.TOOLS,
            McpControlAction.RESOURCES,
            McpControlAction.PROMPTS,
            McpControlAction.ELICITATIONS,
            McpControlAction.TASKS,
            McpControlAction.BOOTSTRAP,
        }
        if self.action not in no_target and not self.target:
            raise McpControlValidationError(f"{self.action} requires a target")
        if self.action is McpControlAction.RESOURCE_READ:
            required_argument(self.arguments, "uri")
        elif self.action is McpControlAction.PROMPT_GET:
            required_argument(self.arguments, "name")
            mapping_argument(self.arguments, "arguments", default={})
        elif self.action is McpControlAction.ELICITATION_RESOLVE:
            for name in ("session_id", "request_id", "action", "idempotency_key"):
                required_argument(self.arguments, name)
            nonnegative_argument(self.arguments, "expected_revision", required=True)
        elif self.action in {
            McpControlAction.TASK_POLL,
            McpControlAction.TASK_RESULT,
            McpControlAction.TASK_CANCEL,
        }:
            required_argument(self.arguments, "task_handle")

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "request_id": self.request_id,
            "action": str(self.action),
            "target": self.target,
            "arguments": to_json_value(sanitize_mcp_value(self.arguments)),
            "context": self.context.safe_dict(),
            "idempotency_key_present": bool(self.idempotency_key),
            "expected_revision": self.expected_revision,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class McpControlAuthorization:
    allowed: bool
    decision_id: str = ""
    request_id: str = ""
    grant_id: str = ""
    reason_code: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def validate(self, request: McpControlRequest) -> None:
        if not self.allowed:
            raise McpControlDenied(
                str(request.action),
                target=request.target,
                reason=self.reason_code or "mcp_control_denied",
            )
        if request.action.external_effect and (not self.decision_id or not self.grant_id):
            raise McpControlDenied(
                str(request.action),
                target=request.target,
                reason="mcp_control_authorization_incomplete",
            )

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "allowed": self.allowed,
            "decision_id": self.decision_id,
            "request_id": self.request_id,
            "grant_id": self.grant_id,
            "reason_code": self.reason_code,
            "evidence": to_json_value(sanitize_mcp_value(self.evidence)),
        }


@dataclass(frozen=True, slots=True)
class McpControlResult:
    request_id: str
    action: McpControlAction
    ok: bool
    summary: str
    data: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""
    retryable: bool = False
    changed: bool = False
    authorization: McpControlAuthorization | None = None
    operation_id: str = ""
    started_at: str = field(default_factory=now_iso)
    completed_at: str = field(default_factory=now_iso)

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": "zyra.mcp-control-result.v1",
            "request_id": self.request_id,
            "operation_id": self.operation_id,
            "action": str(self.action),
            "ok": self.ok,
            "summary": self.summary,
            "data": to_json_value(sanitize_mcp_value(self.data)),
            "error": self.error,
            "retryable": self.retryable,
            "changed": self.changed,
            "authorization": self.authorization.safe_dict() if self.authorization else None,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


@runtime_checkable
class McpControlRuntimePort(Protocol):
    disabled: bool

    def diagnostics(self) -> Mapping[str, Any]: ...
    def connect_server(self, name: str, **context: Any) -> Any: ...
    def disconnect_server(self, name: str, **context: Any) -> Any: ...
    def reconnect_server(self, name: str, **context: Any) -> Any: ...
    def refresh_server(
        self,
        name: str,
        *,
        refresh_kinds: Sequence[str],
        **context: Any,
    ) -> Any: ...
    def set_server_disabled(self, name: str, disabled: bool, **context: Any) -> Any: ...
    def approve_server(self, name: str, **context: Any) -> Any: ...
    def reject_server(self, name: str, **context: Any) -> Any: ...
    def refresh_auth(self, server_id: str) -> Mapping[str, Any]: ...
    def revoke_auth(self, server_id: str) -> Mapping[str, Any]: ...
    def read_resource(self, server_id: str, uri: str, **context: Any) -> Mapping[str, Any]: ...
    def get_prompt(
        self,
        server_id: str,
        name: str,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


AuthorizationCallback: TypeAlias = Callable[[McpControlRequest], McpControlAuthorization]


class McpControlParser:
    ALIASES: Mapping[str, McpControlAction] = {
        "": McpControlAction.STATUS,
        "status": McpControlAction.STATUS,
        "health": McpControlAction.HEALTH,
        "list": McpControlAction.SERVERS,
        "servers": McpControlAction.SERVERS,
        "server": McpControlAction.SERVER,
        "catalog": McpControlAction.CATALOG,
        "tools": McpControlAction.TOOLS,
        "resources": McpControlAction.RESOURCES,
        "prompts": McpControlAction.PROMPTS,
        "elicitations": McpControlAction.ELICITATIONS,
        "tasks": McpControlAction.TASKS,
        "connect": McpControlAction.CONNECT,
        "disconnect": McpControlAction.DISCONNECT,
        "reconnect": McpControlAction.RECONNECT,
        "refresh": McpControlAction.REFRESH,
        "disable": McpControlAction.DISABLE,
        "enable": McpControlAction.ENABLE,
        "approve": McpControlAction.APPROVE,
        "reject": McpControlAction.REJECT,
        "auth-refresh": McpControlAction.AUTH_REFRESH,
        "auth_revoke": McpControlAction.AUTH_REVOKE,
        "auth-revoke": McpControlAction.AUTH_REVOKE,
        "read": McpControlAction.RESOURCE_READ,
        "resource-read": McpControlAction.RESOURCE_READ,
        "prompt": McpControlAction.PROMPT_GET,
        "prompt-get": McpControlAction.PROMPT_GET,
        "resolve": McpControlAction.ELICITATION_RESOLVE,
        "task-poll": McpControlAction.TASK_POLL,
        "task-result": McpControlAction.TASK_RESULT,
        "task-cancel": McpControlAction.TASK_CANCEL,
        "bootstrap": McpControlAction.BOOTSTRAP,
    }

    def parse(
        self,
        text: str,
        *,
        context: McpControlContext | None = None,
        request_id: str = "",
        idempotency_key: str = "",
    ) -> McpControlRequest:
        raw = str(text or "").strip()
        if raw.startswith("/mcp"):
            raw = raw[4:].strip()
        try:
            tokens = shlex.split(raw, posix=True)
        except ValueError as error:
            raise McpControlValidationError("invalid /mcp quoting") from error
        verb = tokens.pop(0).casefold() if tokens else ""
        action = self.ALIASES.get(verb)
        if action is None:
            raise McpControlValidationError(f"unknown /mcp operation: {verb}")
        target = ""
        if tokens and not tokens[0].startswith("--"):
            target = tokens.pop(0)
        arguments = self.options(tokens)
        if action is McpControlAction.RESOURCE_READ:
            target = target or str(arguments.pop("server", ""))
            if "uri" not in arguments and "value" in arguments:
                arguments["uri"] = arguments.pop("value")
        elif action is McpControlAction.PROMPT_GET:
            target = target or str(arguments.pop("server", ""))
            if "name" not in arguments and "value" in arguments:
                arguments["name"] = arguments.pop("value")
            if isinstance(arguments.get("arguments"), str):
                arguments["arguments"] = json_object(
                    str(arguments["arguments"]),
                    "arguments",
                )
        elif action is McpControlAction.REFRESH:
            kinds = arguments.get("kinds") or arguments.get("refresh_kinds")
            if isinstance(kinds, str):
                arguments["refresh_kinds"] = [
                    value.strip() for value in kinds.split(",") if value.strip()
                ]
        return McpControlRequest(
            action=action,
            target=target,
            arguments=arguments,
            context=context or McpControlContext(),
            request_id=request_id or new_id("mcpcontrol"),
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def options(tokens: Sequence[str]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        index = 0
        positionals: list[str] = []
        while index < len(tokens):
            token = tokens[index]
            if not token.startswith("--"):
                positionals.append(token)
                index += 1
                continue
            name, separator, inline = token[2:].partition("=")
            name = name.strip().replace("-", "_")
            if not name:
                raise McpControlValidationError("empty /mcp option")
            if separator:
                value: Any = inline
            elif index + 1 < len(tokens) and not tokens[index + 1].startswith("--"):
                index += 1
                value = tokens[index]
            else:
                value = True
            if name in result:
                existing = result[name]
                result[name] = (
                    [*existing, value]
                    if isinstance(existing, list)
                    else [existing, value]
                )
            else:
                result[name] = value
            index += 1
        if positionals:
            result["value"] = " ".join(positionals)
        return result


class McpControlRuntime:
    def __init__(
        self,
        runtime: McpControlRuntimePort,
        *,
        authorizer: AuthorizationCallback | None = None,
        bootstrap_runtime: Any | None = None,
        recovery_runtime: Any | None = None,
        task_runtime: Any | None = None,
        parser: McpControlParser | None = None,
        result_cache_capacity: int = 512,
        disabled: bool = False,
    ) -> None:
        if result_cache_capacity < 0:
            raise ValueError("result_cache_capacity cannot be negative")
        self.runtime = runtime
        self.authorizer = authorizer
        self.bootstrap_runtime = bootstrap_runtime
        self.recovery_runtime = recovery_runtime
        self.task_runtime = task_runtime
        self.parser = parser or McpControlParser()
        self.result_cache_capacity = result_cache_capacity
        self.disabled = disabled
        self._cache: OrderedDict[str, tuple[str, McpControlResult]] = OrderedDict()
        self._inflight: dict[str, tuple[str, threading.Event]] = {}
        self._lock = threading.RLock()

    def execute_text(
        self,
        text: str,
        *,
        context: McpControlContext | None = None,
        authorizer: AuthorizationCallback | None = None,
        request_id: str = "",
        idempotency_key: str = "",
    ) -> McpControlResult:
        try:
            request = self.parser.parse(
                text,
                context=context,
                request_id=request_id,
                idempotency_key=idempotency_key,
            )
            return self.execute(request, authorizer=authorizer)
        except McpControlError as error:
            return McpControlResult(
                request_id=request_id or new_id("mcpcontrol"),
                action=McpControlAction.STATUS,
                ok=False,
                summary=str(error),
                error=error_code(error),
                retryable=isinstance(error, McpControlAuthorizationRequired),
                operation_id=context.operation_id if context else "",
            )

    def execute(
        self,
        request: McpControlRequest,
        *,
        authorizer: AuthorizationCallback | None = None,
    ) -> McpControlResult:
        if self.disabled or bool(getattr(self.runtime, "disabled", False)):
            raise McpControlDisabled("MCP control runtime is disabled")
        cached = self.claim_or_wait(request)
        if cached is not None:
            return cached
        started_at = now_iso()
        authorization: McpControlAuthorization | None = None
        try:
            if request.action.external_effect:
                callback = authorizer or self.authorizer
                if callback is None:
                    raise McpControlAuthorizationRequired(
                        str(request.action),
                        target=request.target,
                    )
                authorization = callback(request)
                if not isinstance(authorization, McpControlAuthorization):
                    raise McpControlDenied(
                        str(request.action),
                        target=request.target,
                        reason="mcp_control_authorizer_invalid",
                    )
                authorization.validate(request)
            data, changed, summary = self.dispatch(request)
            result = McpControlResult(
                request_id=request.request_id,
                action=request.action,
                ok=True,
                summary=summary,
                data=data,
                changed=changed,
                authorization=authorization,
                operation_id=request.context.operation_id,
                started_at=started_at,
            )
        except Exception as error:
            result = McpControlResult(
                request_id=request.request_id,
                action=request.action,
                ok=False,
                summary=safe_error_message(error),
                error=error_code(error),
                retryable=retryable(error),
                authorization=authorization,
                operation_id=request.context.operation_id,
                started_at=started_at,
            )
        self.complete(request, result)
        return result

    def execute_batch(
        self,
        requests: Sequence[McpControlRequest],
        *,
        authorizer: AuthorizationCallback | None = None,
        stop_on_error: bool = True,
    ) -> tuple[McpControlResult, ...]:
        results: list[McpControlResult] = []
        seen: set[str] = set()
        for request in requests:
            if request.request_id in seen:
                result = McpControlResult(
                    request_id=request.request_id,
                    action=request.action,
                    ok=False,
                    summary="duplicate request_id in batch",
                    error="mcp_control_duplicate_request",
                    operation_id=request.context.operation_id,
                )
            else:
                seen.add(request.request_id)
                result = self.execute(request, authorizer=authorizer)
            results.append(result)
            if stop_on_error and not result.ok:
                break
        return tuple(results)

    def dispatch(
        self,
        request: McpControlRequest,
    ) -> tuple[Mapping[str, Any], bool, str]:
        action = request.action
        context = request.context.event_context()
        target = request.target
        arguments = dict(request.arguments)
        if action in {McpControlAction.STATUS, McpControlAction.HEALTH}:
            return self.runtime.diagnostics(), False, "MCP runtime status"
        if action is McpControlAction.SERVERS:
            values = self.runtime.config_store.list_servers(
                include_inactive=bool_value(arguments.get("include_inactive"), True)
            )
            return {
                "servers": [safe_value(item) for _, item in sorted(values.items())],
                "count": len(values),
            }, False, "MCP server inventory"
        if action is McpControlAction.SERVER:
            record = self.runtime.config_store.get_server(target, include_inactive=True)
            if record is None:
                raise McpControlValidationError(f"unknown MCP server: {target}")
            return {"server": safe_value(record)}, False, f"MCP server {target}"
        if action is McpControlAction.CATALOG:
            return {"catalog": safe_value(self.runtime.catalog.safe_dict())}, False, "MCP catalog"
        if action in {
            McpControlAction.TOOLS,
            McpControlAction.RESOURCES,
            McpControlAction.PROMPTS,
        }:
            key = str(action)
            items: list[Any] = []
            generations: dict[str, int] = {}
            templates: list[Any] = []
            for snapshot in self.runtime.catalog.list():
                if target and snapshot.server_id != target:
                    continue
                generations[snapshot.server_id] = int(snapshot.generation)
                items.extend(safe_value(item) for item in getattr(snapshot, key))
                if action is McpControlAction.RESOURCES:
                    templates.extend(
                        safe_value(item) for item in snapshot.resource_templates
                    )
            value: dict[str, Any] = {
                key: items,
                "count": len(items),
                "catalog_generations": generations,
            }
            if templates:
                value["resource_templates"] = templates
            return value, False, f"MCP {key}"
        if action is McpControlAction.ELICITATIONS:
            values = self.runtime.elicitation_queue.list(
                server_id=target,
                session_id=str(arguments.get("session_id") or request.context.session_id),
                include_terminal=bool_value(arguments.get("include_terminal"), False),
            )
            return {
                "elicitations": [safe_value(item) for item in values],
                "count": len(values),
            }, False, "MCP elicitation inventory"
        if action is McpControlAction.TASKS:
            values = self.task_list(target=target, arguments=arguments)
            return {
                "tasks": [safe_value(item) for item in values],
                "count": len(values),
            }, False, "MCP task inventory"
        if action is McpControlAction.CONNECT:
            receipt = self.runtime.connect_server(target, **context)
            return {"connection": safe_value(receipt)}, True, f"Connected MCP server {target}"
        if action is McpControlAction.DISCONNECT:
            receipt = self.runtime.disconnect_server(target, **context)
            return {"connection": safe_value(receipt)}, True, f"Disconnected MCP server {target}"
        if action is McpControlAction.RECONNECT:
            receipt = self.runtime.reconnect_server(target, **context)
            return {"connection": safe_value(receipt)}, True, f"Reconnected MCP server {target}"
        if action is McpControlAction.REFRESH:
            kinds = string_sequence(
                arguments.get("refresh_kinds") or arguments.get("kinds"),
                ("tools", "resources", "resource_templates", "prompts"),
            )
            receipt = self.runtime.refresh_server(
                target,
                refresh_kinds=kinds,
                **context,
            )
            return {"refresh": safe_value(receipt)}, True, f"Refreshed MCP server {target}"
        if action in {McpControlAction.DISABLE, McpControlAction.ENABLE}:
            disabled = action is McpControlAction.DISABLE
            receipt = self.runtime.set_server_disabled(target, disabled, **context)
            verb = "Disabled" if disabled else "Enabled"
            return {
                "disabled": disabled,
                "state": safe_value(receipt),
            }, True, f"{verb} MCP server {target}"
        if action in {McpControlAction.APPROVE, McpControlAction.REJECT}:
            kwargs: dict[str, Any] = {
                "actor": request.context.actor_id or "mcp-control",
                "reason": str(arguments.get("reason") or str(action)),
            }
            if arguments.get("source_id"):
                kwargs["source_id"] = str(arguments["source_id"])
            if request.expected_revision is not None:
                kwargs["expected_revision"] = request.expected_revision
            method = (
                self.runtime.approve_server
                if action is McpControlAction.APPROVE
                else self.runtime.reject_server
            )
            receipt = method(target, **kwargs)
            return {"approval": safe_value(receipt)}, True, f"{action} MCP server {target}"
        if action is McpControlAction.AUTH_REFRESH:
            receipt = self.runtime.refresh_auth(target)
            return {"auth": safe_value(receipt)}, True, f"Refreshed MCP auth for {target}"
        if action is McpControlAction.AUTH_REVOKE:
            receipt = self.runtime.revoke_auth(target)
            return {"auth": safe_value(receipt)}, True, f"Revoked MCP auth for {target}"
        if action is McpControlAction.RESOURCE_READ:
            receipt = self.runtime.read_resource(
                target,
                str(arguments["uri"]),
                **context,
            )
            return {"resource": safe_value(receipt)}, False, f"Read MCP resource from {target}"
        if action is McpControlAction.PROMPT_GET:
            receipt = self.runtime.get_prompt(
                target,
                str(arguments["name"]),
                mapping_argument(arguments, "arguments", default={}),
            )
            return {"prompt": safe_value(receipt)}, False, f"Loaded MCP prompt from {target}"
        if action is McpControlAction.ELICITATION_RESOLVE:
            nested_context = dict(context)
            nested_context.pop("session_id", None)
            receipt = self.runtime.resolve_elicitation(
                server_id=target,
                session_id=str(arguments["session_id"]),
                request_id=str(arguments["request_id"]),
                expected_revision=int(arguments["expected_revision"]),
                action=str(arguments["action"]),
                content=mapping_argument(arguments, "content", default={}),
                actor_id=request.context.actor_id or "mcp-control",
                idempotency_key=str(arguments["idempotency_key"]),
                **nested_context,
            )
            return {"elicitation": safe_value(receipt)}, True, "Resolved MCP elicitation"
        if action in {
            McpControlAction.TASK_POLL,
            McpControlAction.TASK_RESULT,
            McpControlAction.TASK_CANCEL,
        }:
            receipt = self.task_action(action, target, arguments, context)
            changed = action is McpControlAction.TASK_CANCEL
            return {"task": safe_value(receipt)}, changed, f"MCP {action}"
        if action is McpControlAction.BOOTSTRAP:
            if self.bootstrap_runtime is None:
                raise McpControlValidationError("MCP bootstrap runtime is unavailable")
            receipt = self.bootstrap_runtime.bootstrap(
                context=request.context,
                include_servers=string_sequence(arguments.get("servers"), ()),
            )
            return {"bootstrap": safe_value(receipt)}, True, "MCP bootstrap completed"
        raise McpControlValidationError(f"unsupported MCP control action: {action}")

    def task_list(
        self,
        *,
        target: str,
        arguments: Mapping[str, Any],
    ) -> Sequence[Any]:
        owner = self.task_runtime or getattr(
            self.runtime.connection_runtime,
            "task_runtime",
            None,
        )
        if owner is None:
            return ()
        method = getattr(owner, "list", None)
        if not callable(method):
            return ()
        return tuple(
            method(
                server_id=target,
                include_terminal=bool_value(arguments.get("include_terminal"), True),
            )
        )

    def task_action(
        self,
        action: McpControlAction,
        target: str,
        arguments: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> Any:
        owner = self.task_runtime or getattr(
            self.runtime.connection_runtime,
            "task_runtime",
            None,
        )
        if owner is None:
            raise McpControlValidationError("MCP task runtime is unavailable")
        method_name = {
            McpControlAction.TASK_POLL: "poll",
            McpControlAction.TASK_RESULT: "result",
            McpControlAction.TASK_CANCEL: "cancel",
        }[action]
        method = getattr(owner, method_name, None)
        if not callable(method):
            raise McpControlValidationError(
                f"MCP task runtime does not support {method_name}"
            )
        return method(
            server_id=target,
            task_handle=str(arguments["task_handle"]),
            **context,
        )

    def claim_or_wait(self, request: McpControlRequest) -> McpControlResult | None:
        if not request.idempotency_key:
            return None
        key = idempotency_digest(request.idempotency_key)
        while True:
            with self._lock:
                cached = self._cache.get(key)
                if cached is not None:
                    fingerprint, result = cached
                    if fingerprint != request.fingerprint:
                        raise McpControlValidationError(
                            "idempotency key is bound to different MCP arguments"
                        )
                    self._cache.move_to_end(key)
                    return result
                inflight = self._inflight.get(key)
                if inflight is None:
                    self._inflight[key] = (request.fingerprint, threading.Event())
                    return None
                fingerprint, waiter = inflight
                if fingerprint != request.fingerprint:
                    raise McpControlValidationError(
                        "idempotency key collides with an in-flight MCP operation"
                    )
            if not waiter.wait(timeout=30):
                raise McpControlError("timed out waiting for idempotent MCP operation")

    def complete(self, request: McpControlRequest, result: McpControlResult) -> None:
        if not request.idempotency_key:
            return
        key = idempotency_digest(request.idempotency_key)
        with self._lock:
            inflight = self._inflight.pop(key, None)
            if self.result_cache_capacity:
                self._cache[key] = (request.fingerprint, result)
                self._cache.move_to_end(key)
                while len(self._cache) > self.result_cache_capacity:
                    self._cache.popitem(last=False)
            if inflight is not None:
                inflight[1].set()

    def diagnostics(self) -> dict[str, JsonValue]:
        with self._lock:
            cache_size = len(self._cache)
            inflight_size = len(self._inflight)
        return {
            "schema": "zyra.mcp-control-runtime.v1",
            "runtime_id": "McpControlRuntime",
            "owner_unit": "M1-S03B-02",
            "enabled": not self.disabled,
            "state_owner": "McpClientRuntime",
            "permission_owner": "ToolPermissionRuntime",
            "session_owner": "CodeWorkerSessionStore",
            "tool_owner": "ToolRegistryRuntime",
            "event_owner": "canonical Zyra EventStore",
            "supported_actions": [str(action) for action in McpControlAction],
            "external_actions": sorted(str(action) for action in EXTERNAL_ACTIONS),
            "mutation_actions": sorted(str(action) for action in MUTATING_ACTIONS),
            "cached_idempotent_results": cache_size,
            "inflight_idempotent_operations": inflight_size,
            "creates_parallel_store": False,
        }


def control_authorization_from_mapping(
    value: Mapping[str, Any],
) -> McpControlAuthorization:
    return McpControlAuthorization(
        allowed=bool(value.get("allowed")),
        decision_id=str(value.get("decision_id") or ""),
        request_id=str(value.get("request_id") or ""),
        grant_id=str(value.get("grant_id") or ""),
        reason_code=str(value.get("reason_code") or ""),
        evidence=mapping_argument(value, "evidence", default={}),
    )


def canonical_arguments(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise McpControlValidationError("MCP control arguments must be an object")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise McpControlValidationError(
            "MCP control arguments must be canonical JSON"
        ) from error
    if not isinstance(decoded, dict):
        raise McpControlValidationError("MCP control arguments must be an object")
    if len(encoded) > 2_000_000:
        raise McpControlValidationError("MCP control arguments exceed size limit")
    return decoded


def json_object(value: str, field_name: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise McpControlValidationError(f"{field_name} must be JSON") from error
    if not isinstance(decoded, dict):
        raise McpControlValidationError(f"{field_name} must be a JSON object")
    return decoded


def required_argument(arguments: Mapping[str, Any], name: str) -> Any:
    value = arguments.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise McpControlValidationError(f"{name} is required")
    return value


def mapping_argument(
    arguments: Mapping[str, Any],
    name: str,
    *,
    default: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    value = arguments.get(name)
    if value is None and default is not None:
        return dict(default)
    if not isinstance(value, Mapping):
        raise McpControlValidationError(f"{name} must be an object")
    return dict(value)


def nonnegative_argument(
    arguments: Mapping[str, Any],
    name: str,
    *,
    required: bool,
) -> int | None:
    value = arguments.get(name)
    if value is None:
        if required:
            raise McpControlValidationError(f"{name} is required")
        return None
    if isinstance(value, bool):
        raise McpControlValidationError(f"{name} must be an integer")
    try:
        selected = int(value)
    except (TypeError, ValueError) as error:
        raise McpControlValidationError(f"{name} must be an integer") from error
    if selected < 0:
        raise McpControlValidationError(f"{name} cannot be negative")
    return selected


def string_sequence(value: Any, default: Sequence[str]) -> tuple[str, ...]:
    if value is None:
        return tuple(default)
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, Sequence):
        values = value
    else:
        raise McpControlValidationError("value must be a string or array")
    selected = tuple(str(item).strip() for item in values if str(item).strip())
    return tuple(dict.fromkeys(selected))


def bool_value(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    selected = str(value).strip().casefold()
    if selected in {"1", "true", "yes", "on"}:
        return True
    if selected in {"0", "false", "no", "off"}:
        return False
    raise McpControlValidationError("value must be boolean")


def safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): safe_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [safe_value(item) for item in value]
    for name in ("safe_dict", "to_dict"):
        method = getattr(value, name, None)
        if callable(method):
            return safe_value(method())
    return {"type": type(value).__name__, "safe_projection_available": False}


def idempotency_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_error_message(error: Exception) -> str:
    if isinstance(error, McpControlValidationError):
        return str(error)
    if isinstance(error, McpControlAuthorizationRequired):
        return error.reason
    if isinstance(error, McpControlDisabled):
        return "MCP control runtime is disabled"
    return f"MCP {type(error).__name__} operation failed"


def error_code(error: Exception) -> str:
    if isinstance(error, McpControlAuthorizationRequired):
        return error.reason
    if isinstance(error, McpControlValidationError):
        return "mcp_control_invalid"
    if isinstance(error, McpControlDisabled):
        return "mcp_control_disabled"
    name = type(error).__name__.casefold()
    if "auth" in name:
        return "mcp_auth_required"
    if "notfound" in name or "not_found" in name:
        return "mcp_not_found"
    if "conflict" in name or "stale" in name:
        return "mcp_state_conflict"
    if "timeout" in name or "unavailable" in name:
        return "mcp_dependency_unavailable"
    return "mcp_control_failed"


def retryable(error: Exception) -> bool:
    return isinstance(error, McpControlAuthorizationRequired) or any(
        value in type(error).__name__.casefold()
        for value in (
            "timeout",
            "unavailable",
            "needsauthentication",
            "conflict",
        )
    )


__all__ = [
    "AuthorizationCallback",
    "EXTERNAL_ACTIONS",
    "MUTATING_ACTIONS",
    "McpControlAction",
    "McpControlAuthorization",
    "McpControlAuthorizationRequired",
    "McpControlContext",
    "McpControlDenied",
    "McpControlDisabled",
    "McpControlError",
    "McpControlParser",
    "McpControlRequest",
    "McpControlResult",
    "McpControlRuntime",
    "McpControlRuntimePort",
    "McpControlValidationError",
    "control_authorization_from_mapping",
]
