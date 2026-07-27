from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class ReleaseError(RuntimeError):
    code = "release_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or type(self).code
        self.retryable = (
            type(self).retryable if retryable is None else bool(retryable)
        )
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.release-error/v1",
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
            "fallback": False,
        }


class BoundaryViolation(ReleaseError):
    code = "release_boundary_violation"


class LockViolation(ReleaseError):
    code = "release_lock_violation"


class IntegrityViolation(ReleaseError):
    code = "release_integrity_violation"


class InventoryViolation(ReleaseError):
    code = "release_inventory_violation"


class ConfigurationViolation(ReleaseError):
    code = "release_configuration_violation"


class TransactionConflict(ReleaseError):
    code = "release_transaction_conflict"


class MigrationFailure(ReleaseError):
    code = "release_migration_failure"


class InstallationFailure(ReleaseError):
    code = "release_installation_failure"


class CleanroomFailure(ReleaseError):
    code = "release_cleanroom_failure"


class GateFailure(ReleaseError):
    code = "release_gate_failure"


__all__ = [
    "BoundaryViolation",
    "CleanroomFailure",
    "ConfigurationViolation",
    "GateFailure",
    "InstallationFailure",
    "IntegrityViolation",
    "InventoryViolation",
    "LockViolation",
    "MigrationFailure",
    "ReleaseError",
    "TransactionConflict",
]
