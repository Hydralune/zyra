from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class FreezeEvidenceError(ValueError):
    """A deterministic, serializable freeze-evidence failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: str,
        detail: Mapping[str, Any] | None = None,
        blockers: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self.code = _code(code)
        self.phase = _code(phase)
        self.detail = _safe(dict(detail or {}))
        self.blockers = tuple(_safe(dict(item)) for item in blockers)
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "phase": self.phase,
            "detail": self.detail,
            "blockers": list(self.blockers),
        }


def fail(
    code: str,
    message: str,
    *,
    phase: str,
    detail: Mapping[str, Any] | None = None,
    blockers: Sequence[Mapping[str, Any]] = (),
) -> FreezeEvidenceError:
    return FreezeEvidenceError(
        code,
        message,
        phase=phase,
        detail=detail,
        blockers=blockers,
    )


def blocker(code: str, message: str, **detail: Any) -> dict[str, Any]:
    return {
        "code": _code(code),
        "message": str(message),
        "detail": _safe(detail),
    }


def require_no_blockers(
    findings: Sequence[Mapping[str, Any]],
    *,
    code: str,
    message: str,
    phase: str,
    detail: Mapping[str, Any] | None = None,
) -> None:
    if findings:
        raise fail(
            code,
            message,
            phase=phase,
            detail=detail,
            blockers=findings,
        )


def _code(value: str) -> str:
    selected = str(value or "").strip().lower().replace("_", "-")
    if not selected:
        return "invalid"
    return "".join(
        char if char.isalnum() or char in ".-" else "-"
        for char in selected
    )[:128]


def _safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _safe(item)
            for key, item in value.items()
            if not _secret(str(key))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe(item) for item in value]
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, str):
        return value[:4096]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:4096]


def _secret(key: str) -> bool:
    lowered = key.lower()
    return any(
        marker in lowered
        for marker in (
            "authorization",
            "cookie",
            "credential",
            "password",
            "private-key",
            "private_key",
            "secret",
            "token",
        )
    )
