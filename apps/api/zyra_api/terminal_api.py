from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Mapping

from zyra_workers.terminal import (
    TerminalControlRequest,
    TerminalCreateRequest,
    TerminalError,
    TerminalSessionRegistry,
)


TERMINAL_API_SCHEMA = "zyra.terminal-api.v1"
TERMINAL_PROTOCOL = "zyra.terminal.v1"


@dataclass(frozen=True, slots=True)
class TerminalApiResponse:
    status: HTTPStatus
    body: dict[str, Any]
    headers: Mapping[str, str]


def _required_text(
    payload: Mapping[str, Any],
    key: str,
    *,
    maximum: int = 512,
) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise TerminalError(
            f"terminal_{key}_required",
            f"Terminal {key.replace('_', ' ')} is required.",
            status=400,
        )
    if len(value) > maximum:
        raise TerminalError(
            f"terminal_{key}_oversized",
            f"Terminal {key.replace('_', ' ')} is too long.",
            status=400,
        )
    return value


def _optional_text(
    payload: Mapping[str, Any],
    key: str,
    *,
    maximum: int,
    default: str = "",
) -> str:
    value = str(payload.get(key) or default).strip()
    if len(value) > maximum:
        raise TerminalError(
            f"terminal_{key}_oversized",
            f"Terminal {key.replace('_', ' ')} is too long.",
            status=400,
        )
    return value


def _integer(
    payload: Mapping[str, Any],
    key: str,
    *,
    minimum: int,
    maximum: int,
    default: int,
) -> int:
    try:
        value = int(payload.get(key, default))
    except (TypeError, ValueError) as error:
        raise TerminalError(
            f"terminal_{key}_invalid",
            f"Terminal {key.replace('_', ' ')} must be an integer.",
            status=400,
        ) from error
    if not minimum <= value <= maximum:
        raise TerminalError(
            f"terminal_{key}_invalid",
            (
                f"Terminal {key.replace('_', ' ')} must be in "
                f"{minimum}..{maximum}."
            ),
            status=400,
        )
    return value


def _boolean(payload: Mapping[str, Any], key: str, *, default: bool = False) -> bool:
    raw = payload.get(key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and raw in {0, 1}:
        return bool(raw)
    if isinstance(raw, str):
        selected = raw.strip().casefold()
        if selected in {"1", "true", "yes", "on"}:
            return True
        if selected in {"0", "false", "no", "off", ""}:
            return False
    raise TerminalError(
        f"terminal_{key}_invalid",
        f"Terminal {key.replace('_', ' ')} must be a boolean.",
        status=400,
    )


def _environment(payload: Mapping[str, Any]) -> dict[str, str]:
    raw = payload.get("environment")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping) or len(raw) > 64:
        raise TerminalError(
            "terminal_environment_invalid",
            "Terminal environment must be an object with at most 64 entries.",
            status=400,
        )
    result: dict[str, str] = {}
    total = 0
    for key, value in raw.items():
        name = str(key).strip()
        material = str(value)
        if not name or len(name) > 128 or "\x00" in name:
            raise TerminalError(
                "terminal_environment_name_invalid",
                "Terminal environment contains an invalid variable name.",
                status=400,
            )
        if len(material) > 16_384 or "\x00" in material:
            raise TerminalError(
                "terminal_environment_value_invalid",
                "Terminal environment contains an invalid variable value.",
                status=400,
            )
        total += len(name.encode("utf-8")) + len(material.encode("utf-8"))
        if total > 64 * 1_024:
            raise TerminalError(
                "terminal_environment_budget_exceeded",
                "Terminal environment exceeds its byte budget.",
                status=413,
            )
        result[name] = material
    return result


class TerminalApiService:
    """Typed HTTP façade around the gateway-owned terminal registry."""

    def __init__(self, registry: TerminalSessionRegistry) -> None:
        self.registry = registry

    def list(self, *, task_id: str, include_closed: bool) -> TerminalApiResponse:
        sessions = self.registry.list(task_id, include_closed=include_closed)
        return TerminalApiResponse(
            HTTPStatus.OK,
            {
                "schema": TERMINAL_API_SCHEMA,
                "protocol": TERMINAL_PROTOCOL,
                "task_id": task_id,
                "terminals": [item.to_json() for item in sessions],
                "count": len(sessions),
                "canonical_owner": "zyra_workers.terminal.TerminalSessionRegistry",
            },
            self._headers(),
        )

    def get(self, *, task_id: str, terminal_id: str) -> TerminalApiResponse:
        projection = self.registry.get(task_id, terminal_id).projection()
        return TerminalApiResponse(
            HTTPStatus.OK,
            {
                "schema": TERMINAL_API_SCHEMA,
                "terminal": projection.to_json(),
                "canonical_owner": "zyra_workers.terminal.TerminalSessionRegistry",
            },
            self._headers(),
        )

    def create(
        self,
        *,
        task_id: str,
        run_id: str,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> TerminalApiResponse:
        request = TerminalCreateRequest(
            task_id=task_id,
            run_id=run_id,
            session_id=_required_text(payload, "session_id"),
            worker_id=_required_text(payload, "worker_id"),
            command_id=_required_text(payload, "command_id"),
            tool_call_id=_required_text(payload, "tool_call_id"),
            span_id=_required_text(payload, "span_id"),
            actor_id=actor_id,
            command=_required_text(payload, "command", maximum=32_768),
            title=_optional_text(payload, "title", maximum=256, default="Terminal"),
            cwd=_optional_text(payload, "cwd", maximum=2_048, default="."),
            shell=_optional_text(payload, "shell", maximum=2_048),
            rows=_integer(payload, "rows", minimum=2, maximum=500, default=24),
            cols=_integer(payload, "cols", minimum=2, maximum=1_000, default=80),
            sealed=_boolean(payload, "sealed"),
            competition_mode=_optional_text(
                payload,
                "competition_mode",
                maximum=64,
                default="interactive",
            ),
            permission_permit_id=_optional_text(
                payload,
                "permission_permit_id",
                maximum=512,
            ),
            environment=_environment(payload),
            correlation_id=_optional_text(payload, "correlation_id", maximum=512),
            causation_id=_optional_text(payload, "causation_id", maximum=512),
        )
        projection = self.registry.create(request)
        status = (
            HTTPStatus.CREATED
            if projection.status.phase.value == "running"
            else HTTPStatus.ACCEPTED
            if projection.status.phase.value == "permission_pending"
            else HTTPStatus.FORBIDDEN
        )
        return TerminalApiResponse(
            status,
            {
                "schema": TERMINAL_API_SCHEMA,
                "terminal": projection.to_json(),
                "canonical_owner": "zyra_workers.terminal.TerminalSessionRegistry",
            },
            self._headers(),
        )

    def ticket(
        self,
        *,
        task_id: str,
        terminal_id: str,
        run_id: str,
        payload: Mapping[str, Any],
        origin: str,
    ) -> TerminalApiResponse:
        projection = self.registry.ticket(
            task_id=task_id,
            terminal_id=terminal_id,
            run_id=run_id,
            session_id=_required_text(payload, "session_id"),
            origin=origin,
            cursor=_integer(
                payload,
                "cursor",
                minimum=0,
                maximum=9_007_199_254_740_991,
                default=0,
            ),
            protocol=_optional_text(
                payload,
                "protocol",
                maximum=64,
                default=TERMINAL_PROTOCOL,
            ),
        )
        return TerminalApiResponse(
            HTTPStatus.OK,
            {
                "schema": TERMINAL_API_SCHEMA,
                "terminal": projection.to_json(),
                "ticket": projection.ticket,
                "expires_at": projection.ticket_expires_at,
                "socket_path": projection.socket_path,
            },
            self._headers(),
        )

    def kill(
        self,
        *,
        task_id: str,
        terminal_id: str,
        run_id: str,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> TerminalApiResponse:
        request = TerminalControlRequest(
            task_id=task_id,
            run_id=run_id,
            terminal_id=terminal_id,
            session_id=_required_text(payload, "session_id"),
            worker_id=_required_text(payload, "worker_id"),
            tool_call_id=_required_text(payload, "tool_call_id"),
            span_id=_required_text(payload, "span_id"),
            actor_id=actor_id,
            action="kill",
            sequence=_integer(
                payload,
                "sequence",
                minimum=1,
                maximum=9_007_199_254_740_991,
                default=1,
            ),
            sealed=_boolean(payload, "sealed"),
            competition_mode=_optional_text(
                payload,
                "competition_mode",
                maximum=64,
                default="interactive",
            ),
            reason=_optional_text(payload, "reason", maximum=2_048),
            permission_permit_id=_optional_text(
                payload,
                "permission_permit_id",
                maximum=512,
            ),
        )
        result = self.registry.control(request)
        projection = self.registry.get(task_id, terminal_id).projection()
        return TerminalApiResponse(
            HTTPStatus.OK if result.get("accepted") else HTTPStatus.ACCEPTED,
            {
                "schema": TERMINAL_API_SCHEMA,
                "terminal_id": terminal_id,
                "terminal": projection.to_json(),
                "result": result,
            },
            self._headers(),
        )

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "Cache-Control": "no-store, max-age=0",
            "X-Zyra-Terminal-State-Owner": (
                "zyra_workers.terminal.TerminalSessionRegistry"
            ),
            "X-Zyra-Permission-State-Owner": "typescript.PermissionCoordinator",
        }
