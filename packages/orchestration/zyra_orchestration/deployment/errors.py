from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_SECRET_KEYS = {
    "authorization",
    "credential",
    "credential_value",
    "api_key",
    "password",
    "secret",
    "service_token",
    "signature",
    "token",
}


def redact(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "<depth-limit>"
    if isinstance(value, Mapping):
        return {
            str(key): (
                "<redacted>"
                if _secret_key(str(key))
                else redact(child, depth=depth + 1)
            )
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(child, depth=depth + 1) for child in value]
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    text = str(value) if value is not None else None
    if text and (
        text.startswith(("sk-", "Bearer ", "Basic "))
        or "PRIVATE KEY" in text
        or len(text) > 2048
    ):
        return "<redacted>"
    return value


def _secret_key(key: str) -> bool:
    normalized = key.casefold()
    if normalized in _SECRET_KEYS:
        return True
    if "secret" in normalized or "password" in normalized:
        return True
    # Usage counters such as prompt_tokens/total_tokens are evidence, not
    # credentials. Keep singular token-bearing credential fields redacted.
    return "token" in normalized and "tokens" not in normalized


class DeploymentError(RuntimeError):
    status = 400

    def __init__(
        self,
        code: str,
        message: str,
        *,
        operation: str = "",
        profile: str = "",
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.operation = operation
        self.profile = profile
        self.retryable = retryable
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.deployment-error/v1",
            "error": self.code,
            "message": str(self),
            "operation": self.operation,
            "profile": self.profile,
            "retryable": self.retryable,
            "fallback": False,
            "details": redact(self.details),
        }


class ConfigurationRejected(DeploymentError):
    status = 422


class StateConflict(DeploymentError):
    status = 409


class StateCorrupt(DeploymentError):
    status = 500


class PortConflict(DeploymentError):
    status = 409


class ProcessUnavailable(DeploymentError):
    status = 503


class NodeProtocolError(DeploymentError):
    status = 502


class NodeAuthenticationError(DeploymentError):
    status = 401


class NodeReplayRejected(DeploymentError):
    status = 409


class PlacementRejected(DeploymentError):
    status = 422


class PlacementDisabled(DeploymentError):
    status = 503


class DispatchRejected(DeploymentError):
    status = 422


class DispatchTimeout(DeploymentError):
    status = 504


class HandoffRejected(DeploymentError):
    status = 409


class SemanticProbeFailed(DeploymentError):
    status = 503


class SemanticHealthDisabled(DeploymentError):
    status = 503


class DoctorBlocked(DeploymentError):
    status = 503


class CleanStateRejected(DeploymentError):
    status = 409


__all__ = [
    "CleanStateRejected",
    "ConfigurationRejected",
    "DeploymentError",
    "DispatchRejected",
    "DispatchTimeout",
    "DoctorBlocked",
    "HandoffRejected",
    "NodeAuthenticationError",
    "NodeProtocolError",
    "NodeReplayRejected",
    "PlacementDisabled",
    "PlacementRejected",
    "PortConflict",
    "ProcessUnavailable",
    "SemanticHealthDisabled",
    "SemanticProbeFailed",
    "StateConflict",
    "StateCorrupt",
    "redact",
]
