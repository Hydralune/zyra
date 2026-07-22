from __future__ import annotations

from enum import StrEnum
from typing import Any


class FaultRuntimeErrorCode(StrEnum):
    INVALID_OBSERVATION = "invalid_observation"
    OBSERVER_NOT_REGISTERED = "observer_not_registered"
    OBSERVER_NOT_RUNNING = "observer_not_running"
    OBSERVER_DISABLED = "observer_disabled"
    OBSERVER_REVISION_CONFLICT = "observer_revision_conflict"
    OBSERVER_SOURCE_INACTIVE = "observer_source_inactive"
    CLASSIFICATION_REJECTED = "classification_rejected"
    UNSUPPORTED_INJECTION = "unsupported_injection"
    INVALID_INJECTION_TARGET = "invalid_injection_target"
    INJECTION_REVISION_CONFLICT = "injection_revision_conflict"
    INJECTION_ALREADY_TERMINAL = "injection_already_terminal"
    PROJECTION_FAILED = "projection_failed"
    STATE_STORE_CONFLICT = "state_store_conflict"
    REQUIREMENT_CHANGE_NOT_FAULT = "requirement_change_not_fault"


class FaultRuntimeError(RuntimeError):
    def __init__(
        self,
        code: FaultRuntimeErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.code.value,
            "message": str(self),
            "details": dict(self.details),
            "fallback": False,
        }


class ObserverStateConflict(FaultRuntimeError):
    def __init__(self, observer_id: str, expected: int, actual: int) -> None:
        super().__init__(
            FaultRuntimeErrorCode.OBSERVER_REVISION_CONFLICT,
            f"observer {observer_id} revision conflict: expected {expected}, actual {actual}",
            details={"observer_id": observer_id, "expected_revision": expected, "actual_revision": actual},
        )


class InjectionStateConflict(FaultRuntimeError):
    def __init__(self, injection_id: str, expected: int, actual: int) -> None:
        super().__init__(
            FaultRuntimeErrorCode.INJECTION_REVISION_CONFLICT,
            f"injection {injection_id} revision conflict: expected {expected}, actual {actual}",
            details={"injection_id": injection_id, "expected_revision": expected, "actual_revision": actual},
        )
