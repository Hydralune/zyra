from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import Any


@dataclass(frozen=True, slots=True)
class ScenarioFault:
    code: str
    message: str
    status: int
    retryable: bool = False
    phase: str = "request"
    detail: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.scenario-error/v1",
            "ok": False,
            "error": self.code,
            "message": self.message,
            "status": self.status,
            "retryable": self.retryable,
            "phase": self.phase,
            "detail": dict(self.detail or {}),
            "fallback": False,
        }


class ScenarioRunnerError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | HTTPStatus = HTTPStatus.BAD_REQUEST,
        retryable: bool = False,
        phase: str = "request",
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.fault = ScenarioFault(
            code=code,
            message=message,
            status=int(status),
            retryable=retryable,
            phase=phase,
            detail=detail,
        )

    @property
    def code(self) -> str:
        return self.fault.code

    @property
    def status(self) -> int:
        return self.fault.status

    def response(self) -> dict[str, Any]:
        return self.fault.to_dict()


def unavailable(code: str, message: str, *, phase: str) -> ScenarioRunnerError:
    return ScenarioRunnerError(
        code,
        message,
        status=HTTPStatus.SERVICE_UNAVAILABLE,
        phase=phase,
    )


def conflict(
    code: str,
    message: str,
    *,
    phase: str,
    detail: dict[str, Any] | None = None,
) -> ScenarioRunnerError:
    return ScenarioRunnerError(
        code,
        message,
        status=HTTPStatus.CONFLICT,
        phase=phase,
        detail=detail,
    )


def not_found(code: str, message: str, *, phase: str = "lookup") -> ScenarioRunnerError:
    return ScenarioRunnerError(
        code,
        message,
        status=HTTPStatus.NOT_FOUND,
        phase=phase,
    )


def invalid(
    code: str,
    message: str,
    *,
    phase: str = "validation",
    detail: dict[str, Any] | None = None,
) -> ScenarioRunnerError:
    return ScenarioRunnerError(
        code,
        message,
        status=HTTPStatus.UNPROCESSABLE_ENTITY,
        phase=phase,
        detail=detail,
    )

