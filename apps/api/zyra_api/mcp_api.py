from __future__ import annotations

"""HTTP-neutral projection over the TypeScript E02 MCP control port.

HTTP parsing stays in the API process.  Permission evaluation, MCP state,
connections, catalog mutations, execution journals, and capability dispatch
stay behind :class:`E02CapabilityCoordinator` in TypeScript.
"""

from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Any, Protocol, TypeAlias, runtime_checkable

from zyra_integrations.e02_ports import TypeScriptE02PortError


JsonBody: TypeAlias = dict[str, Any]
Headers: TypeAlias = dict[str, str]
McpApiResult: TypeAlias = tuple[HTTPStatus, JsonBody, Headers]


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
    ("POST", "/mcp/reload"),
    ("POST", "/mcp/resources/read"),
    ("POST", "/mcp/prompts/get"),
)


_NO_STORE_HEADERS: Headers = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "X-Zyra-E02-State-Owner": "E02CapabilityCoordinator",
    "X-Zyra-MCP-State-Owner": "McpRuntimeCoordinator",
    "X-Zyra-Python-Decision-Fallback": "false",
}


@runtime_checkable
class TypeScriptMcpProjectionPort(Protocol):
    def mcp_get(
        self,
        parts: Sequence[str],
        query: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...

    def mcp_post(
        self,
        parts: Sequence[str],
        body: Mapping[str, Any] | None = None,
        *,
        actor_id: str = "",
    ) -> Mapping[str, Any]: ...


class McpApiFacade:
    """Translate normalized HTTP values without making any logical decision."""

    def __init__(self, runtime: TypeScriptMcpProjectionPort) -> None:
        if not isinstance(runtime, TypeScriptMcpProjectionPort):
            raise TypeError("McpApiFacade requires a TypeScript E02 projection port")
        self.runtime = runtime

    def handle_get(
        self,
        parts: Sequence[str],
        query: Mapping[str, Any] | None = None,
    ) -> McpApiResult | None:
        normalized = _path(parts)
        if not normalized or normalized[0] != "mcp":
            return None
        try:
            return _projection(self.runtime.mcp_get(normalized, query))
        except TypeScriptE02PortError as error:
            return _error_projection(error)

    def handle_post(
        self,
        parts: Sequence[str],
        payload: Mapping[str, Any] | None,
        actor_id: str = "",
    ) -> McpApiResult | None:
        normalized = _path(parts)
        if not normalized or normalized[0] != "mcp":
            return None
        try:
            return _projection(
                self.runtime.mcp_post(
                    normalized,
                    dict(payload or {}),
                    actor_id=str(actor_id or ""),
                )
            )
        except TypeScriptE02PortError as error:
            return _error_projection(error)


def _path(parts: Sequence[str]) -> tuple[str, ...]:
    return tuple(str(value).strip() for value in parts if str(value).strip())


def _projection(value: Mapping[str, Any]) -> McpApiResult:
    status_value = value.get("status", HTTPStatus.OK)
    try:
        status = HTTPStatus(int(status_value))
    except (TypeError, ValueError):
        status = HTTPStatus.INTERNAL_SERVER_ERROR
    body = dict(value.get("body") or {}) if isinstance(value.get("body"), Mapping) else {}
    headers = dict(_NO_STORE_HEADERS)
    if isinstance(value.get("headers"), Mapping):
        headers.update(
            {
                str(key): str(item)
                for key, item in value["headers"].items()
                if isinstance(key, str) and isinstance(item, (str, int, float, bool))
            }
        )
    body.setdefault("canonical_entrypoint", "E02CapabilityCoordinator.execute")
    body.setdefault("python_fallback", False)
    return status, body, headers


def _error_projection(error: TypeScriptE02PortError) -> McpApiResult:
    code = str(error.code or "e02_api_port_failed")
    status = (
        HTTPStatus.LOCKED
        if code == "e02_api_state_locked"
        else HTTPStatus.SERVICE_UNAVAILABLE
        if code.endswith(("unavailable", "exited", "timeout"))
        else HTTPStatus.CONFLICT
    )
    return (
        status,
        {
            "ok": False,
            "error": code,
            "message": str(error),
            "detail": dict(error.detail),
            "canonical_entrypoint": "E02CapabilityCoordinator.execute",
            "python_fallback": False,
        },
        dict(_NO_STORE_HEADERS),
    )


__all__ = [
    "MCP_API_ROUTES",
    "McpApiFacade",
    "McpApiResult",
    "TypeScriptMcpProjectionPort",
]
