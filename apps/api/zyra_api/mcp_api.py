from __future__ import annotations

"""HTTP-neutral control-plane adapter for :class:`McpClientRuntime`.

The API server deliberately owns HTTP parsing while the MCP runtime owns
state and behavior.  This module is the narrow seam between them: callers
pass normalized path parts and JSON values and receive an HTTP-compatible
``(status, body, headers)`` tuple.  It never opens a second MCP state store,
never exposes credential material, and never reports a constant health
result.
"""

import hashlib
import ipaddress
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from http import HTTPStatus
from pathlib import Path
from typing import Any, Protocol, TypeAlias, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from zyra_integrations.mcp.auth import (
    AuthCancelled,
    AuthConfigurationError,
    AuthError,
    AuthNeedsInteraction,
    AuthNetworkError,
    AuthStepUpRequired,
)
from zyra_integrations.mcp.capabilities import (
    McpCapabilityError,
    McpCapabilityRuntimeDisabled,
)
from zyra_integrations.mcp.config import (
    McpConfigError,
    McpConfigNotFound,
    McpConfigValidationError,
    McpEnterpriseExclusiveError,
    McpEnvironmentExpansionError,
    McpProjectApprovalError,
    McpSourceGenerationConflict,
)
from zyra_integrations.mcp.connection import (
    McpConnectionError,
    McpConnectionRuntimeDisabled,
    McpNeedsAuthentication,
    McpServerNotConnected,
    McpServerNotFound,
)
from zyra_integrations.mcp.elicitation import (
    McpElicitationConflict,
    McpElicitationError,
    McpElicitationNotFound,
)
from zyra_integrations.mcp.models import McpModelError
from zyra_integrations.mcp.output import McpOutputError, McpOutputRuntimeDisabled
from zyra_integrations.mcp.runtime import (
    McpClientRuntime,
    McpClientRuntimeDisabled,
    McpClientRuntimeError,
)
from zyra_integrations.mcp.store import (
    McpStateConflict,
    McpStateCorrupt,
    McpStateDisabled,
    McpStateLockTimeout,
    McpStateSerializationError,
    McpStateStoreError,
)


JsonBody: TypeAlias = dict[str, Any]
Headers: TypeAlias = dict[str, str]
McpApiResult: TypeAlias = tuple[HTTPStatus, JsonBody, Headers]


# This manifest is the public HTTP contract implemented by ``McpApiFacade``.
# The API server delegates normalized paths to the facade, so keeping the
# contract beside the dispatcher lets documentation, reachability audits, and
# future UI clients inspect the same boundary without executing the server.
MCP_API_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/mcp"),
    ("GET", "/mcp/health"),
    ("GET", "/mcp/config"),
    ("GET", "/mcp/servers"),
    ("GET", "/mcp/servers/{server_id}"),
    ("GET", "/mcp/catalog"),
    ("GET", "/mcp/tools"),
    ("GET", "/mcp/resources"),
    ("GET", "/mcp/prompts"),
    ("GET", "/mcp/elicitations"),
    ("POST", "/mcp/servers"),
    ("POST", "/mcp/servers/{server_id}/connect"),
    ("POST", "/mcp/servers/{server_id}/disconnect"),
    ("POST", "/mcp/servers/{server_id}/reconnect"),
    ("POST", "/mcp/servers/{server_id}/refresh"),
    ("POST", "/mcp/servers/{server_id}/disable"),
    ("POST", "/mcp/servers/{server_id}/approve"),
    ("POST", "/mcp/servers/{server_id}/reject"),
    ("POST", "/mcp/servers/{server_id}/auth/install"),
    ("POST", "/mcp/servers/{server_id}/auth/revoke"),
    ("POST", "/mcp/auth/{server_id}/install"),
    ("POST", "/mcp/auth/{server_id}/revoke"),
    ("POST", "/mcp/resources/read"),
    ("POST", "/mcp/prompts/get"),
    ("POST", "/mcp/elicitations/resolve"),
)


_NO_STORE_HEADERS: Headers = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "X-Zyra-MCP-State-Owner": "McpClientRuntime",
}

_PERMISSION_CONTEXT_FIELDS = frozenset(
    {
        "run_id",
        "task_id",
        "node_id",
        "session_id",
        "worker_request_id",
        "tool_use_id",
        "permission_session_custody_token",
    }
)


class McpApiValidationError(ValueError):
    """A request-shape error whose message is safe to return to a caller."""


class McpApiPolicyError(PermissionError):
    """A deployment policy rejected an MCP control-plane mutation."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class McpApiAuthorizationError(PermissionError):
    """The 03A permission runtime did not authorize an exact mutation."""

    def __init__(
        self,
        code: str = "mcp_permission_required",
        *,
        status: HTTPStatus = HTTPStatus.FORBIDDEN,
        retryable: bool = True,
        permission: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.permission = dict(permission or {})


@dataclass(frozen=True, slots=True)
class McpMutationRequest:
    """Secret-safe, exact permission identity for one HTTP mutation."""

    action: str
    server_id: str
    arguments: dict[str, Any]
    actor_id: str = ""


@dataclass(frozen=True, slots=True)
class McpMutationAuthorization:
    """Successful authorization evidence returned by a 03A-backed gate."""

    allowed: bool
    decision_id: str = ""
    request_id: str = ""
    grant_id: str = ""
    custody_fingerprint: str = ""
    reason_code: str = ""
    events: tuple[Mapping[str, Any], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "allowed": bool(self.allowed),
            "decision_id": self.decision_id,
            "request_id": self.request_id,
            "grant_id": self.grant_id,
            "custody_fingerprint": self.custody_fingerprint,
            "reason_code": self.reason_code,
            "events": [_safe_value(item) for item in self.events],
            "metadata": _safe_value(dict(self.metadata)),
            "exact_one_shot": True,
        }


@runtime_checkable
class McpMutationAuthorizer(Protocol):
    def __call__(self, request: McpMutationRequest) -> McpMutationAuthorization: ...


class McpMutationPolicy:
    """Deployment-owned allowlists enforced before permission evaluation.

    A human or standing permission may authorize an action, but it cannot
    widen this deployment boundary.  Enterprise-owned sources are immutable
    through the ordinary HTTP facade.  STDIO is an exact, absolute executable
    path with empty arguments/environment.  HTTP is an exact public
    IP-literal URL.  The facade performs no DNS resolution, so a permission
    cannot be widened by PATH lookup, DNS rebinding, or a validation/use race.
    """

    def __init__(
        self,
        *,
        allowed_scopes: Sequence[str] = ("dynamic",),
        stdio_commands: Sequence[str] = (),
        http_endpoints: Sequence[str] = (),
        http_hosts: Sequence[str] = (),
        allow_insecure_http: bool = False,
        resolver: Any | None = None,
    ) -> None:
        # ``resolver`` and ``http_hosts`` are retained only as constructor
        # compatibility inputs.  Resolvers are never called; host/domain-only
        # entries cannot become effective endpoints.
        del resolver
        self.allowed_scopes = frozenset(
            str(value).strip().casefold() for value in allowed_scopes if str(value).strip()
        )
        self.configured_stdio_command_count = len(
            tuple(value for value in stdio_commands if str(value).strip())
        )
        self.stdio_commands = tuple(
            dict.fromkeys(
                selected
                for selected in (_canonical_executable_path(value) for value in stdio_commands)
                if selected
            )
        )
        self.allow_insecure_http = bool(allow_insecure_http)
        configured_endpoints = tuple(http_endpoints) + tuple(http_hosts)
        self.configured_http_endpoint_count = len(
            tuple(value for value in configured_endpoints if str(value).strip())
        )
        self.http_endpoints = tuple(
            dict.fromkeys(
                selected
                for selected in (
                    _canonical_public_ip_endpoint(
                        value,
                        allow_insecure_http=self.allow_insecure_http,
                    )
                    for value in configured_endpoints
                )
                if selected
            )
        )

    @classmethod
    def from_environment(cls) -> "McpMutationPolicy":
        endpoints = (
            *_environment_list("ZYRA_MCP_HTTP_ENDPOINT_ALLOWLIST", ()),
            # Legacy host entries remain ineffective unless they are already
            # complete IP-literal URLs; domains are never resolved.
            *_environment_list("ZYRA_MCP_HTTP_HOST_ALLOWLIST", ()),
        )
        return cls(
            allowed_scopes=_environment_list("ZYRA_MCP_API_ALLOWED_SCOPES", ("dynamic",)),
            stdio_commands=_environment_list("ZYRA_MCP_STDIO_COMMAND_ALLOWLIST", ()),
            http_endpoints=endpoints,
            allow_insecure_http=_environment_bool("ZYRA_MCP_HTTP_ALLOW_INSECURE", False),
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "schema": "zyra.mcp-mutation-policy.v1",
            "allowed_scopes": sorted(self.allowed_scopes),
            "enterprise_sources_mutable": False,
            "stdio": {
                "mode": "absolute_canonical_executable_exact",
                "path_lookup": False,
                "args_policy": "empty_only",
                "env_policy": "empty_only",
                "configured_entries": self.configured_stdio_command_count,
                "effective_entries": len(self.stdio_commands),
            },
            "http": {
                "mode": "public_ip_literal_url_exact",
                "dns_resolution": False,
                "domain_names_allowed": False,
                "redirects_allowed_by_transport": False,
                "configured_entries": self.configured_http_endpoint_count,
                "effective_entries": len(self.http_endpoints),
                "insecure_http_allowed": self.allow_insecure_http,
            },
        }

    def validate_add(
        self,
        *,
        source_id: str,
        scope: str,
        config: Mapping[str, Any],
    ) -> None:
        selected_scope = str(scope or "dynamic").strip().casefold()
        selected_source = str(source_id or "manual:dynamic").strip().casefold()
        if selected_scope == "enterprise" or selected_source.startswith("enterprise"):
            raise McpApiPolicyError("enterprise_source_read_only")
        if selected_scope not in self.allowed_scopes:
            raise McpApiPolicyError("scope_not_allowlisted")
        transport = str(config.get("transport") or "").strip().casefold()
        command = _optional_text(config.get("command"))
        url = _optional_text(config.get("url"))
        if not transport:
            transport = "stdio" if command and not url else "streamable_http" if url else "in_process"
        if transport == "stdio":
            self._validate_stdio(
                command,
                args=config.get("args"),
                env=config.get("env"),
            )
        elif transport in {"streamable_http", "http", "sse"}:
            self._validate_http(url)
        elif transport != "in_process":
            raise McpApiPolicyError("transport_not_allowlisted")

    def validate_existing(self, record: Any) -> None:
        config = getattr(record, "config", None)
        provenance = getattr(record, "provenance", None)
        scopes = {
            str(value).strip().casefold()
            for value in (
                getattr(record, "scope", ""),
                getattr(config, "scope", ""),
                getattr(provenance, "scope", ""),
                getattr(provenance, "source_kind", ""),
            )
            if str(value).strip()
        }
        source_ids = {
            str(value).strip().casefold()
            for value in (
                getattr(record, "source_id", ""),
                getattr(record, "source_path", ""),
                getattr(config, "source_path", ""),
                getattr(provenance, "source_id", ""),
                getattr(provenance, "source_path", ""),
            )
            if str(value).strip()
        }
        if "enterprise" in scopes or any(
            value.startswith("enterprise") for value in source_ids
        ):
            raise McpApiPolicyError("enterprise_source_read_only")
        effective_scope = next(
            (
                value
                for value in (
                    str(getattr(config, "scope", "") or "").strip().casefold(),
                    str(getattr(record, "scope", "") or "").strip().casefold(),
                    str(getattr(provenance, "scope", "") or "").strip().casefold(),
                )
                if value
            ),
            "",
        )
        if effective_scope and effective_scope not in self.allowed_scopes:
            raise McpApiPolicyError("scope_not_allowlisted")
        transport = str(getattr(config, "transport", "") or "").strip().casefold()
        if transport == "stdio":
            self._validate_stdio(
                str(getattr(config, "command", "") or ""),
                args=getattr(config, "args", ()),
                env=getattr(config, "env", {}),
            )
        elif transport in {"streamable_http", "http", "sse"}:
            self._validate_http(str(getattr(config, "url", "") or ""))

    def _validate_stdio(self, command: str, *, args: Any = (), env: Any = None) -> None:
        if _nonempty_invocation_value(args):
            raise McpApiPolicyError("stdio_arguments_require_exact_template")
        if _nonempty_invocation_value(env):
            raise McpApiPolicyError("stdio_environment_require_exact_template")
        candidate = _canonical_executable_path(command)
        if not candidate or not self.stdio_commands:
            raise McpApiPolicyError("stdio_command_not_allowlisted")
        if candidate not in self.stdio_commands:
            raise McpApiPolicyError("stdio_command_not_allowlisted")

    def _validate_http(self, url: str) -> None:
        if not url:
            raise McpApiPolicyError("http_target_not_allowlisted")
        candidate = _canonical_public_ip_endpoint(
            url,
            allow_insecure_http=self.allow_insecure_http,
            raise_reason=True,
        )
        if not self.http_endpoints or candidate not in self.http_endpoints:
            raise McpApiPolicyError("http_target_not_allowlisted")


class McpApiFacade:
    """Route normalized MCP control-plane operations to one runtime instance."""

    def __init__(
        self,
        runtime: McpClientRuntime,
        *,
        mutation_authorizer: McpMutationAuthorizer | None = None,
        mutation_policy: McpMutationPolicy | None = None,
    ) -> None:
        self.runtime = runtime
        self.mutation_authorizer = mutation_authorizer
        self.mutation_policy = mutation_policy or McpMutationPolicy.from_environment()

    def handle_get(
        self,
        parts: Sequence[str],
        query: Mapping[str, Any] | None = None,
    ) -> McpApiResult | None:
        route = _mcp_route(parts)
        if route is None:
            return None
        parameters = _flatten_query(query or {})
        try:
            if route in {(), ("health",)}:
                diagnostics = _safe_value(self.runtime.diagnostics())
                if not isinstance(diagnostics, dict):
                    raise RuntimeError("MCP diagnostics returned an invalid shape")
                healthy = bool(diagnostics.get("ok", False))
                return _response(
                    HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "schema": "zyra.mcp-api.health.v1",
                        "ok": healthy,
                        "runtime": diagnostics,
                        "mutation_policy": self.mutation_policy.diagnostics(),
                    },
                )

            if route == ("config",):
                snapshot = self.runtime.config_store.public_snapshot()
                return _response(
                    HTTPStatus.OK,
                    {
                        "schema": "zyra.mcp-api.config.v1",
                        "config": _safe_value(snapshot),
                        "mutation_policy": self.mutation_policy.diagnostics(),
                    },
                )

            if route == ("servers",):
                return _response(
                    HTTPStatus.OK,
                    self._servers_body(
                        include_inactive=_query_bool(parameters, "include_inactive", default=True),
                        server_id=parameters.get("server_id", ""),
                    ),
                )

            if len(route) == 2 and route[0] == "servers":
                server = self._server_body(route[1])
                if server is None:
                    raise McpConfigNotFound(route[1])
                return _response(
                    HTTPStatus.OK,
                    {"schema": "zyra.mcp-api.server.v1", "server": server},
                )

            if route == ("catalog",):
                return _response(
                    HTTPStatus.OK,
                    {
                        "schema": "zyra.mcp-api.catalog.v1",
                        "catalog": _safe_value(self.runtime.catalog.safe_dict()),
                    },
                )

            if route in {("tools",), ("resources",), ("prompts",)}:
                capability = route[0]
                return _response(
                    HTTPStatus.OK,
                    self._capability_body(capability, server_id=parameters.get("server_id", "")),
                )

            if route == ("elicitations",):
                include_terminal = _query_bool(parameters, "include_terminal", default=False)
                records = self.runtime.elicitation_queue.list(
                    server_id=parameters.get("server_id", ""),
                    session_id=parameters.get("session_id", ""),
                    include_terminal=include_terminal,
                )
                return _response(
                    HTTPStatus.OK,
                    {
                        "schema": "zyra.mcp-api.elicitations.v1",
                        "elicitations": [_safe_value(item) for item in records],
                        "count": len(records),
                        "include_terminal": include_terminal,
                    },
                    sensitive=True,
                )
        except Exception as error:  # noqa: BLE001 - domain errors are mapped at this boundary.
            return _error_response(error)
        return None

    def handle_post(
        self,
        parts: Sequence[str],
        payload: Mapping[str, Any] | None,
        actor_id: str,
    ) -> McpApiResult | None:
        route = _mcp_route(parts)
        if route is None:
            return None
        if not isinstance(payload, Mapping):
            return _error_response(McpApiValidationError("JSON request body must be an object."))
        value = dict(payload)
        try:
            if route == ("servers",):
                mutation = self._server_add_mutation(value, actor_id=actor_id)
                authorization = self._authorize_mutation(mutation)
                return _with_authorization(self._add_server(value), authorization)

            if len(route) == 3 and route[0] == "servers":
                mutation = self._server_action_mutation(
                    route[1], route[2], value, actor_id=actor_id
                )
                authorization = self._authorize_mutation(mutation)
                return _with_authorization(
                    self._server_action(route[1], route[2], value, actor_id=actor_id),
                    authorization,
                )

            if route == ("resources", "read"):
                server_id = _required_text(value, "server_id")
                uri = _required_text(value, "uri")
                receipt = self.runtime.read_resource(server_id, uri, **_event_context(value))
                return _response(
                    HTTPStatus.OK,
                    {
                        "schema": "zyra.mcp-api.resource-read.v1",
                        "server_id": server_id,
                        "uri": uri,
                        "resource": _safe_value(receipt),
                    },
                    sensitive=True,
                )

            if route == ("prompts", "get"):
                server_id = _required_text(value, "server_id")
                name = _required_text(value, "name")
                arguments = _mapping(value.get("arguments"), "arguments", default={})
                receipt = self.runtime.get_prompt(server_id, name, arguments)
                return _response(
                    HTTPStatus.OK,
                    {
                        "schema": "zyra.mcp-api.prompt-get.v1",
                        "server_id": server_id,
                        "name": name,
                        "prompt": _safe_value(receipt),
                    },
                    sensitive=True,
                )

            if route == ("elicitations", "resolve"):
                mutation = self._elicitation_mutation(value, actor_id=actor_id)
                authorization = self._authorize_mutation(mutation)
                return _with_authorization(
                    self._resolve_elicitation(value, actor_id=actor_id),
                    authorization,
                )

            auth_route = _auth_route(route)
            if auth_route is not None:
                server_id, action = auth_route
                mutation = self._auth_mutation(server_id, action, value, actor_id=actor_id)
                authorization = self._authorize_mutation(mutation)
                return _with_authorization(
                    self._auth_action(server_id, action, value),
                    authorization,
                )
        except Exception as error:  # noqa: BLE001 - domain errors are mapped at this boundary.
            return _error_response(error)
        return None

    def _authorize_mutation(
        self,
        request: McpMutationRequest,
    ) -> McpMutationAuthorization:
        if self.mutation_authorizer is None:
            raise McpApiAuthorizationError("mcp_permission_required")
        authorization = self.mutation_authorizer(request)
        if not isinstance(authorization, McpMutationAuthorization):
            raise McpApiAuthorizationError(
                "mcp_permission_invalid",
                retryable=False,
            )
        if not authorization.allowed:
            raise McpApiAuthorizationError(
                authorization.reason_code or "mcp_permission_denied",
                permission=authorization.safe_dict(),
            )
        if not authorization.decision_id or not authorization.grant_id:
            raise McpApiAuthorizationError(
                "mcp_permission_invalid",
                retryable=False,
            )
        return authorization

    def _server_add_values(
        self,
        payload: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any], str, str, int | None]:
        name = _required_text(payload, "name")
        nested = payload.get("config")
        if nested is None:
            excluded = {
                "name",
                "source_id",
                "scope",
                "expected_revision",
                *_PERMISSION_CONTEXT_FIELDS,
            }
            config = {key: item for key, item in payload.items() if key not in excluded}
        else:
            config = _mapping(nested, "config")
        if not config:
            raise McpApiValidationError("config must contain an MCP transport configuration.")
        source_id = _optional_text(payload.get("source_id")) or "manual:dynamic"
        scope = _optional_text(payload.get("scope")) or "dynamic"
        expected_revision = _optional_int(payload.get("expected_revision"), "expected_revision")
        return name, config, source_id, scope, expected_revision

    def _server_add_mutation(
        self,
        payload: Mapping[str, Any],
        *,
        actor_id: str,
    ) -> McpMutationRequest:
        name, config, source_id, scope, expected_revision = self._server_add_values(payload)
        self.mutation_policy.validate_add(source_id=source_id, scope=scope, config=config)
        server_id = _optional_text(config.get("server_id")) or name
        return McpMutationRequest(
            action="mcp.server.add",
            server_id=server_id,
            actor_id=actor_id,
            arguments={
                "name": name,
                "server_id": server_id,
                "source_id": source_id,
                "scope": scope,
                "expected_revision": expected_revision,
                "config": _permission_config(config),
            },
        )

    def _server_action_mutation(
        self,
        identifier: str,
        action: str,
        payload: Mapping[str, Any],
        *,
        actor_id: str,
    ) -> McpMutationRequest:
        selected_action = str(action).casefold()
        if selected_action not in {
            "connect",
            "disconnect",
            "reconnect",
            "refresh",
            "disable",
            "approve",
            "reject",
        }:
            raise McpApiValidationError("unsupported MCP server action.")
        config_name, server_id, record = self._server_record(identifier)
        self.mutation_policy.validate_existing(record)
        arguments: dict[str, Any] = {
            "action": selected_action,
            "server_name": config_name,
            "server_id": server_id,
            **_event_context(payload),
        }
        if selected_action == "refresh":
            arguments["refresh_kinds"] = list(
                _string_sequence(
                    payload.get("refresh_kinds"),
                    "refresh_kinds",
                    default=("tools", "resources", "resource_templates", "prompts"),
                )
            )
        elif selected_action == "disable":
            arguments["disabled"] = _bool_value(
                payload.get("disabled"), default=True, field="disabled"
            )
        elif selected_action in {"approve", "reject"}:
            arguments.update(
                {
                    "actor_id": actor_id,
                    "reason": _optional_text(payload.get("reason")) or selected_action,
                    "source_id": _optional_text(payload.get("source_id")),
                    "expected_revision": _optional_int(
                        payload.get("expected_revision"), "expected_revision"
                    ),
                }
            )
        return McpMutationRequest(
            action=f"mcp.server.{selected_action}",
            server_id=server_id,
            actor_id=actor_id,
            arguments=arguments,
        )

    def _auth_mutation(
        self,
        identifier: str,
        action: str,
        payload: Mapping[str, Any],
        *,
        actor_id: str,
    ) -> McpMutationRequest:
        config_name, server_id, record = self._server_record(identifier)
        self.mutation_policy.validate_existing(record)
        selected_action = str(action).casefold()
        arguments: dict[str, Any] = {
            "action": selected_action,
            "server_name": config_name,
            "server_id": server_id,
            **_event_context(payload),
        }
        if selected_action == "install":
            tokens = _mapping(payload.get("tokens", payload), "tokens")
            if not _optional_text(tokens.get("access_token")):
                raise McpApiValidationError("tokens.access_token is required.")
            arguments["token_fields"] = sorted(str(key) for key in tokens)
            arguments["tokens_digest"] = _canonical_digest(tokens)
        elif selected_action != "revoke":
            raise McpApiValidationError("unsupported MCP auth action.")
        return McpMutationRequest(
            action=f"mcp.auth.{selected_action}",
            server_id=server_id,
            actor_id=actor_id,
            arguments=arguments,
        )

    def _elicitation_mutation(
        self,
        payload: Mapping[str, Any],
        *,
        actor_id: str,
    ) -> McpMutationRequest:
        server_identifier = _required_text(payload, "server_id")
        _, server_id, record = self._server_record(server_identifier)
        self.mutation_policy.validate_existing(record)
        content = _mapping(payload.get("content"), "content", default={})
        arguments = {
            "server_id": server_id,
            "session_id": _required_text(payload, "session_id"),
            "request_id": _required_text(payload, "request_id"),
            "expected_revision": _required_int(payload, "expected_revision"),
            "action": _required_text(payload, "action"),
            "content_digest": _canonical_digest(content),
            "idempotency_key": _required_text(payload, "idempotency_key"),
            **_event_context(payload),
        }
        return McpMutationRequest(
            action="mcp.elicitation.resolve",
            server_id=server_id,
            actor_id=actor_id,
            arguments=arguments,
        )

    def _add_server(self, payload: Mapping[str, Any]) -> McpApiResult:
        name, config, source_id, scope, expected_revision = self._server_add_values(payload)
        kwargs: dict[str, Any] = {
            "source_id": source_id,
            "scope": scope,
        }
        if expected_revision is not None:
            kwargs["expected_revision"] = expected_revision
        created = self.runtime.add_server(name, config, **kwargs)
        return _response(
            HTTPStatus.CREATED,
            {
                "schema": "zyra.mcp-api.server-created.v1",
                "server": _safe_value(created),
                "config_revision": int(self.runtime.config_store.revision),
            },
            sensitive=True,
        )

    def _server_action(
        self,
        identifier: str,
        action: str,
        payload: Mapping[str, Any],
        *,
        actor_id: str,
    ) -> McpApiResult:
        if not identifier:
            raise McpApiValidationError("server identifier is required.")
        action = action.casefold()
        config_name, server_id = self._server_identity(identifier)
        context = _event_context(payload)
        if action == "connect":
            result = self.runtime.connect_server(config_name, **context)
            body = _safe_value(result)
        elif action == "disconnect":
            result = self.runtime.disconnect_server(server_id, **context)
            body = _safe_value(result)
        elif action == "reconnect":
            result = self.runtime.reconnect_server(server_id, **context)
            body = _safe_value(result)
        elif action == "refresh":
            kinds = _string_sequence(
                payload.get("refresh_kinds"),
                "refresh_kinds",
                default=("tools", "resources", "resource_templates", "prompts"),
            )
            result = self.runtime.refresh_server(server_id, refresh_kinds=kinds, **context)
            body = _safe_value(result)
        elif action == "disable":
            disabled = _bool_value(payload.get("disabled"), default=True, field="disabled")
            # The state document returned by set_server_disabled contains
            # internal config-source material.  It is intentionally discarded.
            self.runtime.set_server_disabled(config_name, disabled, **context)
            body = {
                "disabled": disabled,
                "server": self._server_body(config_name),
            }
        elif action in {"approve", "reject"}:
            if not actor_id.strip():
                raise McpApiValidationError("actor_id is required for server approval changes.")
            kwargs: dict[str, Any] = {
                "actor": actor_id.strip(),
                "reason": _optional_text(payload.get("reason")) or action,
            }
            source_id = _optional_text(payload.get("source_id"))
            if source_id:
                kwargs["source_id"] = source_id
            expected_revision = _optional_int(payload.get("expected_revision"), "expected_revision")
            if expected_revision is not None:
                kwargs["expected_revision"] = expected_revision
            if action == "approve":
                self.runtime.approve_server(config_name, **kwargs)
            else:
                self.runtime.reject_server(config_name, **kwargs)
            body = {
                "approval": action + "d" if action == "approve" else "rejected",
                "server": self._server_body(config_name),
            }
        else:
            raise McpApiValidationError("unsupported MCP server action.")

        return _response(
            HTTPStatus.OK,
            {
                "schema": "zyra.mcp-api.server-action.v1",
                "action": action,
                "server_id": server_id,
                "result": body,
            },
            sensitive=True,
        )

    def _auth_action(
        self,
        server_id: str,
        action: str,
        payload: Mapping[str, Any],
    ) -> McpApiResult:
        config_name, canonical_server_id = self._server_identity(server_id)
        if action == "install":
            raw_tokens = payload.get("tokens", payload)
            tokens = _mapping(raw_tokens, "tokens")
            if not _optional_text(tokens.get("access_token")):
                raise McpApiValidationError("tokens.access_token is required.")
            # McpClientRuntime resolves auth configuration through the config
            # store, whose primary key is the configured name.  The response
            # still exposes the durable server_id used by transport/catalog.
            outcome = self.runtime.install_auth_tokens(config_name, tokens)
            status = HTTPStatus.CREATED
        elif action == "revoke":
            outcome = self.runtime.revoke_auth(config_name)
            status = HTTPStatus.OK
        else:
            raise McpApiValidationError("unsupported MCP auth action.")
        # Runtime outcomes are already credential-safe.  The request payload is
        # never reflected, and no-store is mandatory even for revoke results.
        return _response(
            status,
            {
                "schema": "zyra.mcp-api.auth-action.v1",
                "action": action,
                "server_id": canonical_server_id,
                "auth": _safe_value(outcome),
                "secret_values_included": False,
            },
            sensitive=True,
        )

    def _resolve_elicitation(
        self,
        payload: Mapping[str, Any],
        *,
        actor_id: str,
    ) -> McpApiResult:
        if not actor_id.strip():
            raise McpApiValidationError("actor_id is required to resolve an elicitation.")
        idempotency_key = _required_text(payload, "idempotency_key")
        content = _mapping(payload.get("content"), "content", default={})
        context = _event_context(payload)
        # session_id is part of the elicitation's exact durable identity, not
        # merely optional event context; do not pass it twice.
        context.pop("session_id", None)
        receipt = self.runtime.resolve_elicitation(
            server_id=_required_text(payload, "server_id"),
            session_id=_required_text(payload, "session_id"),
            request_id=_required_text(payload, "request_id"),
            expected_revision=_required_int(payload, "expected_revision"),
            action=_required_text(payload, "action"),
            content=content,
            actor_id=actor_id.strip(),
            idempotency_key=idempotency_key,
            **context,
        )
        return _response(
            HTTPStatus.OK,
            {
                "schema": "zyra.mcp-api.elicitation-resolution.v1",
                "elicitation": _safe_value(receipt),
            },
            sensitive=True,
        )

    def _servers_body(self, *, include_inactive: bool, server_id: str = "") -> JsonBody:
        configured = self.runtime.config_store.list_servers(include_inactive=include_inactive)
        connections = {
            snapshot.server_id: _safe_value(snapshot)
            for snapshot in self.runtime.connection_runtime.snapshots()
        }
        catalog = {snapshot.server_id: snapshot for snapshot in self.runtime.catalog.list()}
        servers: list[dict[str, Any]] = []
        for name in sorted(configured):
            record = configured[name]
            if server_id and server_id not in {name, record.server_id}:
                continue
            item = _safe_value(record)
            if not isinstance(item, dict):
                item = {"name": name, "server_id": record.server_id}
            item["connection"] = connections.get(record.server_id)
            snapshot = catalog.get(record.server_id)
            item["catalog"] = _safe_value(snapshot) if snapshot is not None else None
            servers.append(item)
        return {
            "schema": "zyra.mcp-api.servers.v1",
            "servers": servers,
            "count": len(servers),
            "include_inactive": include_inactive,
        }

    def _server_body(self, identifier: str) -> dict[str, Any] | None:
        body = self._servers_body(include_inactive=True, server_id=identifier)
        servers = body["servers"]
        return servers[0] if isinstance(servers, list) and servers else None

    def _server_identity(self, identifier: str) -> tuple[str, str]:
        name, server_id, _ = self._server_record(identifier)
        return name, server_id

    def _server_record(self, identifier: str) -> tuple[str, str, Any]:
        configured = self.runtime.config_store.list_servers(include_inactive=True)
        direct = configured.get(identifier)
        if direct is not None:
            return identifier, direct.server_id, direct
        for name, record in configured.items():
            if record.server_id == identifier:
                return name, record.server_id, record
        raise McpConfigNotFound(identifier)

    def _capability_body(self, capability: str, *, server_id: str = "") -> JsonBody:
        key = capability
        items: list[Any] = []
        templates: list[Any] = []
        generations: dict[str, int] = {}
        for snapshot in self.runtime.catalog.list():
            if server_id and snapshot.server_id != server_id:
                continue
            generations[snapshot.server_id] = int(snapshot.generation)
            items.extend(_safe_value(item) for item in getattr(snapshot, key))
            if capability == "resources":
                templates.extend(_safe_value(item) for item in snapshot.resource_templates)
        body: JsonBody = {
            "schema": f"zyra.mcp-api.{capability}.v1",
            capability: items,
            "count": len(items),
            "catalog_generations": generations,
        }
        if capability == "resources":
            body["resource_templates"] = templates
            body["resource_template_count"] = len(templates)
        return body


def handle_get(
    runtime: McpClientRuntime,
    parts: Sequence[str],
    query: Mapping[str, Any] | None = None,
) -> McpApiResult | None:
    """Functional entry point for handlers that do not retain a facade."""

    return McpApiFacade(runtime).handle_get(parts, query)


def handle_post(
    runtime: McpClientRuntime,
    parts: Sequence[str],
    payload: Mapping[str, Any] | None,
    actor_id: str,
    *,
    mutation_authorizer: McpMutationAuthorizer | None = None,
    mutation_policy: McpMutationPolicy | None = None,
) -> McpApiResult | None:
    """Functional entry point mirroring :meth:`McpApiFacade.handle_post`."""

    return McpApiFacade(
        runtime,
        mutation_authorizer=mutation_authorizer,
        mutation_policy=mutation_policy,
    ).handle_post(parts, payload, actor_id)


def _mcp_route(parts: Sequence[str]) -> tuple[str, ...] | None:
    normalized = tuple(str(part).strip() for part in parts if str(part).strip())
    if not normalized or normalized[0].casefold() != "mcp":
        return None
    return normalized[1:]


def _auth_route(route: tuple[str, ...]) -> tuple[str, str] | None:
    if (
        len(route) == 3
        and route[0].casefold() == "auth"
        and route[2].casefold() in {"install", "revoke"}
    ):
        return route[1], route[2].casefold()
    if (
        len(route) == 4
        and route[0].casefold() == "servers"
        and route[2].casefold() == "auth"
        and route[3].casefold() in {"install", "revoke"}
    ):
        return route[1], route[3].casefold()
    return None


def _response(
    status: HTTPStatus,
    body: JsonBody,
    *,
    sensitive: bool = False,
) -> McpApiResult:
    headers = dict(_NO_STORE_HEADERS) if sensitive else {
        "Cache-Control": "no-cache, max-age=0",
        "X-Zyra-MCP-State-Owner": "McpClientRuntime",
    }
    return status, body, headers


def _with_authorization(
    result: McpApiResult,
    authorization: McpMutationAuthorization,
) -> McpApiResult:
    status, body, headers = result
    return status, {**body, "permission": authorization.safe_dict()}, headers


def _error_response(error: Exception) -> McpApiResult:
    if isinstance(error, McpApiAuthorizationError):
        body = _error_body(
            error.code,
            "An exact one-shot MCP mutation permission is required.",
            retryable=error.retryable,
        )
        if error.permission:
            body["permission"] = _safe_value(error.permission)
        return _response(error.status, body, sensitive=True)
    if isinstance(error, McpApiPolicyError):
        return _response(
            HTTPStatus.FORBIDDEN,
            {
                **_error_body(
                    "mcp_deployment_policy_denied",
                    "The MCP deployment policy denied this mutation.",
                    retryable=False,
                ),
                "reason_code": error.reason_code,
            },
            sensitive=True,
        )
    if isinstance(error, McpApiValidationError):
        return _response(
            HTTPStatus.BAD_REQUEST,
            _error_body("invalid_request", str(error), retryable=False),
            sensitive=True,
        )
    if isinstance(error, (McpConfigNotFound, McpServerNotFound, McpElicitationNotFound)):
        return _response(
            HTTPStatus.NOT_FOUND,
            _error_body("mcp_not_found", "The requested MCP object was not found.", retryable=False),
            sensitive=True,
        )
    if isinstance(error, (McpNeedsAuthentication, AuthNeedsInteraction, AuthStepUpRequired)):
        return _response(
            HTTPStatus.UNAUTHORIZED,
            _error_body("mcp_auth_required", "The MCP server requires authentication.", retryable=True),
            sensitive=True,
        )
    if isinstance(error, AuthCancelled):
        return _response(
            HTTPStatus.CONFLICT,
            _error_body("mcp_auth_cancelled", "The MCP authentication operation was cancelled.", retryable=True),
            sensitive=True,
        )
    if isinstance(
        error,
        (
            McpStateConflict,
            McpSourceGenerationConflict,
            McpProjectApprovalError,
            McpEnterpriseExclusiveError,
            McpElicitationConflict,
            McpServerNotConnected,
        ),
    ):
        return _response(
            HTTPStatus.CONFLICT,
            _error_body("mcp_state_conflict", "The MCP operation conflicts with current state.", retryable=True),
            sensitive=True,
        )
    if isinstance(
        error,
        (
            McpClientRuntimeDisabled,
            McpConnectionRuntimeDisabled,
            McpCapabilityRuntimeDisabled,
            McpOutputRuntimeDisabled,
            McpStateDisabled,
        ),
    ):
        return _response(
            HTTPStatus.SERVICE_UNAVAILABLE,
            _error_body("mcp_runtime_disabled", "The MCP runtime is disabled.", retryable=False),
            sensitive=True,
        )
    if isinstance(error, (AuthNetworkError, McpStateLockTimeout)):
        return _response(
            HTTPStatus.SERVICE_UNAVAILABLE,
            _error_body("mcp_dependency_unavailable", "An MCP dependency is temporarily unavailable.", retryable=True),
            sensitive=True,
        )
    if isinstance(
        error,
        (
            McpModelError,
            McpConfigValidationError,
            McpEnvironmentExpansionError,
            AuthConfigurationError,
        ),
    ):
        return _response(
            HTTPStatus.BAD_REQUEST,
            _error_body("mcp_validation_failed", "The MCP request failed validation.", retryable=False),
            sensitive=True,
        )
    if isinstance(error, (McpStateCorrupt, McpStateSerializationError)):
        return _response(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            _error_body("mcp_state_unavailable", "MCP runtime state is unavailable.", retryable=True),
            sensitive=True,
        )
    if isinstance(
        error,
        (
            AuthError,
            McpConfigError,
            McpConnectionError,
            McpCapabilityError,
            McpElicitationError,
            McpOutputError,
            McpClientRuntimeError,
            McpStateStoreError,
        ),
    ):
        return _response(
            HTTPStatus.BAD_GATEWAY,
            _error_body("mcp_operation_failed", "The MCP runtime rejected the operation.", retryable=True),
            sensitive=True,
        )
    return _response(
        HTTPStatus.INTERNAL_SERVER_ERROR,
        _error_body("mcp_internal_error", "The MCP control operation failed.", retryable=True),
        sensitive=True,
    )


def _error_body(code: str, message: str, *, retryable: bool) -> JsonBody:
    return {
        "schema": "zyra.mcp-api.error.v1",
        "ok": False,
        "error": code,
        "message": message,
        "retryable": retryable,
        "secret_values_included": False,
    }


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, Mapping):
        return {str(key): _safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_value(item) for item in value]
    safe_dict = getattr(value, "safe_dict", None)
    if callable(safe_dict):
        return _safe_value(safe_dict())
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _safe_value(to_dict())
    return {"type": type(value).__name__, "safe_projection_available": False}


def _flatten_query(query: Mapping[str, Any]) -> dict[str, str]:
    flattened: dict[str, str] = {}
    for key, value in query.items():
        if isinstance(value, (list, tuple)):
            selected = value[-1] if value else ""
        else:
            selected = value
        flattened[str(key)] = "" if selected is None else str(selected)
    return flattened


def _query_bool(query: Mapping[str, str], key: str, *, default: bool) -> bool:
    if key not in query:
        return default
    return _bool_value(query[key], default=default, field=key)


def _bool_value(value: Any, *, default: bool, field: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise McpApiValidationError(f"{field} must be a boolean value.")


def _required_text(payload: Mapping[str, Any], field: str) -> str:
    value = _optional_text(payload.get(field))
    if not value:
        raise McpApiValidationError(f"{field} is required.")
    if len(value) > 4096:
        raise McpApiValidationError(f"{field} exceeds the maximum length.")
    return value


def _optional_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (Mapping, list, tuple, set)):
        return ""
    return str(value).strip()


def _required_int(payload: Mapping[str, Any], field: str) -> int:
    value = _optional_int(payload.get(field), field)
    if value is None:
        raise McpApiValidationError(f"{field} is required.")
    return value


def _optional_int(value: Any, field: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise McpApiValidationError(f"{field} must be an integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise McpApiValidationError(f"{field} must be an integer.") from error
    if parsed < 0:
        raise McpApiValidationError(f"{field} cannot be negative.")
    return parsed


def _mapping(value: Any, field: str, *, default: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if value is None and default is not None:
        return dict(default)
    if not isinstance(value, Mapping):
        raise McpApiValidationError(f"{field} must be an object.")
    return dict(value)


def _string_sequence(value: Any, field: str, *, default: Sequence[str]) -> tuple[str, ...]:
    if value is None:
        return tuple(default)
    if isinstance(value, str):
        values = [item.strip() for item in value.split(",")]
    elif isinstance(value, Sequence):
        values = [_optional_text(item) for item in value]
    else:
        raise McpApiValidationError(f"{field} must be an array or comma-separated string.")
    selected = tuple(item for item in values if item)
    if not selected:
        raise McpApiValidationError(f"{field} cannot be empty.")
    return selected


def _event_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    for key in ("run_id", "task_id", "node_id", "session_id"):
        value = _optional_text(payload.get(key))
        if value:
            context[key] = value
    return context


def _canonical_digest(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise McpApiValidationError("mutation values must be canonical JSON.") from error
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _permission_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Build an exact but credential-safe permission projection."""

    url = _optional_text(config.get("url"))
    safe_url = ""
    if url:
        try:
            parsed = urlsplit(url)
            safe_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        except ValueError:
            safe_url = "<invalid>"
    env = config.get("env") if isinstance(config.get("env"), Mapping) else {}
    headers = config.get("headers") if isinstance(config.get("headers"), Mapping) else {}
    args = config.get("args") if isinstance(config.get("args"), Sequence) and not isinstance(
        config.get("args"), (str, bytes, bytearray)
    ) else ()
    return {
        "transport": _optional_text(config.get("transport")) or "in_process",
        "server_id": _optional_text(config.get("server_id")),
        "command": _optional_text(config.get("command")),
        "args_count": len(args),
        "args_digest": _canonical_digest(list(args)),
        "url": safe_url,
        "url_digest": _canonical_digest(url),
        "env_names": sorted(str(key) for key in env),
        "env_digest": _canonical_digest(env),
        "header_names": sorted(str(key) for key in headers),
        "headers_digest": _canonical_digest(headers),
        "config_digest": _canonical_digest(config),
    }


def _environment_list(name: str, default: Sequence[str]) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return tuple(default)
    selected = raw.strip()
    if not selected:
        return ()
    if selected.startswith("["):
        try:
            parsed = json.loads(selected)
        except json.JSONDecodeError:
            return ()
        if not isinstance(parsed, list):
            return ()
        return tuple(str(item).strip() for item in parsed if str(item).strip())
    return tuple(item.strip() for item in selected.split(",") if item.strip())


def _environment_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _canonical_executable_path(value: Any) -> str:
    selected = str(value or "").strip()
    if not selected:
        return ""
    try:
        path = Path(selected)
        if not path.is_absolute():
            return ""
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return ""
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return ""
    return os.path.normcase(str(resolved))


def _nonempty_invocation_value(value: Any) -> bool:
    if value is None:
        return False
    try:
        return len(value) > 0
    except TypeError:
        return True


def _canonical_public_ip_endpoint(
    value: Any,
    *,
    allow_insecure_http: bool,
    raise_reason: bool = False,
) -> str:
    def reject(reason: str) -> str:
        if raise_reason:
            raise McpApiPolicyError(reason)
        return ""

    selected = str(value or "").strip()
    if not selected:
        return reject("http_target_invalid")
    try:
        parsed = urlsplit(selected)
        port = parsed.port
    except ValueError:
        return reject("http_target_invalid")
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        return reject("http_target_invalid")
    if scheme == "http" and not allow_insecure_http:
        return reject("insecure_http_denied")
    host = parsed.hostname or ""
    if not host or parsed.username or parsed.password or parsed.fragment or "%" in host:
        return reject("http_target_invalid")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return reject("http_dns_names_denied")
    if not _public_ip(address):
        return reject("http_loopback_or_private_denied")
    canonical_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    netloc = canonical_host if port is None else f"{canonical_host}:{port}"
    return urlunsplit((scheme, netloc, parsed.path, parsed.query, ""))


def _public_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(address.is_global) and not any(
        (
            address.is_loopback,
            address.is_private,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


__all__ = [
    "MCP_API_ROUTES",
    "McpApiAuthorizationError",
    "McpApiFacade",
    "McpApiPolicyError",
    "McpApiValidationError",
    "McpMutationAuthorization",
    "McpMutationAuthorizer",
    "McpMutationPolicy",
    "McpMutationRequest",
    "handle_get",
    "handle_post",
]
