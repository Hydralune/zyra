from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class BrowserFailureKind(StrEnum):
    CONFIGURATION = "configuration"
    STATE = "state"
    PROFILE = "profile"
    PROCESS = "process"
    DISCOVERY = "discovery"
    TRANSPORT = "transport"
    PROTOCOL = "protocol"
    REQUEST_TIMEOUT = "request_timeout"
    CONNECTION_LOST = "connection_lost"
    TARGET = "target"
    FOCUS = "focus"
    PERMISSION = "permission"
    ARTIFACT = "artifact"
    CONFLICT = "conflict"
    DISABLED = "disabled"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class BrowserFailure:
    code: str
    message: str
    kind: BrowserFailureKind
    retryable: bool = False
    terminal: bool = False
    session_id: str = ""
    operation: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "kind": str(self.kind),
            "retryable": self.retryable,
            "terminal": self.terminal,
            "session_id": self.session_id,
            "operation": self.operation,
            "details": dict(self.details),
        }


class BrowserRuntimeError(RuntimeError):
    kind = BrowserFailureKind.INTERNAL
    code = "browser_runtime_error"
    retryable = False
    terminal = False

    def __init__(
        self,
        message: str,
        *,
        session_id: str = "",
        operation: str = "",
        details: dict[str, Any] | None = None,
        code: str = "",
        retryable: bool | None = None,
        terminal: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.session_id = session_id
        self.operation = operation
        self.details = dict(details or {})
        self.error_code = code or self.code
        self.is_retryable = self.retryable if retryable is None else retryable
        self.is_terminal = self.terminal if terminal is None else terminal

    def failure(self) -> BrowserFailure:
        return BrowserFailure(
            code=self.error_code,
            message=str(self),
            kind=self.kind,
            retryable=self.is_retryable,
            terminal=self.is_terminal,
            session_id=self.session_id,
            operation=self.operation,
            details=self.details,
        )


class BrowserConfigurationError(BrowserRuntimeError):
    kind = BrowserFailureKind.CONFIGURATION
    code = "browser_configuration_invalid"
    terminal = True


class BrowserRuntimeDisabled(BrowserRuntimeError):
    kind = BrowserFailureKind.DISABLED
    code = "browser_runtime_disabled"
    terminal = True


class BrowserStateError(BrowserRuntimeError):
    kind = BrowserFailureKind.STATE
    code = "browser_state_error"


class BrowserStateCorrupt(BrowserStateError):
    code = "browser_state_corrupt"
    terminal = True


class BrowserStateConflict(BrowserStateError):
    kind = BrowserFailureKind.CONFLICT
    code = "browser_state_conflict"
    retryable = True


class BrowserSessionNotFound(BrowserStateError):
    code = "browser_session_not_found"


class BrowserSessionBusy(BrowserStateError):
    code = "browser_session_busy"
    retryable = True


class BrowserProfileError(BrowserRuntimeError):
    kind = BrowserFailureKind.PROFILE
    code = "browser_profile_error"


class BrowserProfileCorrupt(BrowserProfileError):
    code = "browser_profile_corrupt"
    terminal = True


class BrowserProfileLocked(BrowserProfileError):
    code = "browser_profile_locked"
    retryable = True


class BrowserProcessError(BrowserRuntimeError):
    kind = BrowserFailureKind.PROCESS
    code = "browser_process_error"
    retryable = True


class BrowserExecutableNotFound(BrowserProcessError):
    code = "browser_executable_not_found"
    retryable = False
    terminal = True


class BrowserLaunchFailed(BrowserProcessError):
    code = "browser_launch_failed"


class BrowserDiscoveryError(BrowserRuntimeError):
    kind = BrowserFailureKind.DISCOVERY
    code = "browser_discovery_failed"
    retryable = True


class BrowserTransportError(BrowserRuntimeError):
    kind = BrowserFailureKind.TRANSPORT
    code = "browser_transport_error"
    retryable = True


class BrowserConnectionLost(BrowserTransportError):
    kind = BrowserFailureKind.CONNECTION_LOST
    code = "browser_connection_lost"


class BrowserProtocolError(BrowserRuntimeError):
    kind = BrowserFailureKind.PROTOCOL
    code = "browser_protocol_error"


class BrowserRequestTimeout(BrowserRuntimeError):
    kind = BrowserFailureKind.REQUEST_TIMEOUT
    code = "browser_cdp_request_timeout"
    retryable = True


class BrowserTargetError(BrowserRuntimeError):
    kind = BrowserFailureKind.TARGET
    code = "browser_target_error"
    retryable = True


class BrowserTargetDetached(BrowserTargetError):
    code = "browser_target_detached"


class BrowserFocusError(BrowserRuntimeError):
    kind = BrowserFailureKind.FOCUS
    code = "browser_focus_error"
    retryable = True


class BrowserPermissionDenied(BrowserRuntimeError):
    kind = BrowserFailureKind.PERMISSION
    code = "browser_permission_denied"
    terminal = True


class BrowserPermissionPending(BrowserRuntimeError):
    kind = BrowserFailureKind.PERMISSION
    code = "browser_permission_pending"
    retryable = True


class BrowserArtifactError(BrowserRuntimeError):
    kind = BrowserFailureKind.ARTIFACT
    code = "browser_artifact_error"


def classify_browser_error(error: BaseException, *, operation: str = "", session_id: str = "") -> BrowserFailure:
    if isinstance(error, BrowserRuntimeError):
        return error.failure()
    if isinstance(error, TimeoutError):
        return BrowserFailure(
            code="browser_operation_timeout",
            message=str(error) or "browser operation timed out",
            kind=BrowserFailureKind.REQUEST_TIMEOUT,
            retryable=True,
            session_id=session_id,
            operation=operation,
        )
    if isinstance(error, FileNotFoundError):
        return BrowserFailure(
            code="browser_file_not_found",
            message=str(error),
            kind=BrowserFailureKind.CONFIGURATION,
            terminal=True,
            session_id=session_id,
            operation=operation,
        )
    if isinstance(error, PermissionError):
        return BrowserFailure(
            code="browser_filesystem_permission_denied",
            message=str(error),
            kind=BrowserFailureKind.PERMISSION,
            terminal=True,
            session_id=session_id,
            operation=operation,
        )
    return BrowserFailure(
        code=type(error).__name__,
        message=str(error),
        kind=BrowserFailureKind.INTERNAL,
        session_id=session_id,
        operation=operation,
    )


def failure_chain(error: BaseException) -> tuple[BrowserFailure, ...]:
    failures: list[BrowserFailure] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        failures.append(classify_browser_error(current))
        current = current.__cause__ or current.__context__
    return tuple(failures)


def public_error(error: BaseException) -> dict[str, Any]:
    failure = classify_browser_error(error)
    result = failure.to_dict()
    result["causes"] = [item.to_dict() for item in failure_chain(error)[1:]]
    return result
