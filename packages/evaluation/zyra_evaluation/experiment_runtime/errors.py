from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any


@dataclass(frozen=True, slots=True)
class ExperimentError(RuntimeError):
    code: str
    message: str
    status: int = int(HTTPStatus.UNPROCESSABLE_ENTITY)
    phase: str = "experiment"
    retryable: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message

    def response(self) -> dict[str, Any]:
        return {
            "schema": "zyra.experiment-error/v1",
            "ok": False,
            "error": self.code,
            "message": self.message,
            "status": self.status,
            "phase": self.phase,
            "retryable": self.retryable,
            "fallback": False,
            "detail": dict(self.detail),
        }


def invalid(
    code: str,
    message: str,
    *,
    phase: str = "validation",
    detail: dict[str, Any] | None = None,
) -> ExperimentError:
    return ExperimentError(
        code=code,
        message=message,
        status=int(HTTPStatus.UNPROCESSABLE_ENTITY),
        phase=phase,
        detail=dict(detail or {}),
    )


def conflict(
    code: str,
    message: str,
    *,
    phase: str = "lifecycle",
    retryable: bool = False,
    detail: dict[str, Any] | None = None,
) -> ExperimentError:
    return ExperimentError(
        code=code,
        message=message,
        status=int(HTTPStatus.CONFLICT),
        phase=phase,
        retryable=retryable,
        detail=dict(detail or {}),
    )


def unavailable(
    code: str,
    message: str,
    *,
    phase: str = "runtime",
    retryable: bool = True,
    detail: dict[str, Any] | None = None,
) -> ExperimentError:
    return ExperimentError(
        code=code,
        message=message,
        status=int(HTTPStatus.SERVICE_UNAVAILABLE),
        phase=phase,
        retryable=retryable,
        detail=dict(detail or {}),
    )


def not_found(
    code: str,
    message: str,
    *,
    phase: str = "lookup",
    detail: dict[str, Any] | None = None,
) -> ExperimentError:
    return ExperimentError(
        code=code,
        message=message,
        status=int(HTTPStatus.NOT_FOUND),
        phase=phase,
        detail=dict(detail or {}),
    )


def forbidden(
    code: str,
    message: str,
    *,
    phase: str = "policy",
    detail: dict[str, Any] | None = None,
) -> ExperimentError:
    return ExperimentError(
        code=code,
        message=message,
        status=int(HTTPStatus.FORBIDDEN),
        phase=phase,
        detail=dict(detail or {}),
    )
