from __future__ import annotations

from collections.abc import Mapping
from typing import Any


RECOVERY_BY_CODE: dict[str, str] = {
    "loopx_package_lock_missing": (
        "Restore config/loopx/package-lock.json from the same Zyra release."
    ),
    "loopx_package_lock_invalid": (
        "Replace the package lock and embedded manifest with copies from the "
        "same checksum-verified Zyra release."
    ),
    "loopx_embedded_source_missing": (
        "Restore packages/integrations/loopx_runtime from the pinned Zyra release."
    ),
    "loopx_embedded_source_tampered": (
        "Discard this checkout or release and restore the checksum-verified "
        "embedded LoopX source tree."
    ),
    "loopx_source_manifest_mismatch": (
        "Regenerate the embedded source only from the exact approved v0.2.13 tag "
        "object and peeled source commit."
    ),
    "loopx_version_mismatch": (
        "Restore the v0.2.13 embedded source and matching package lock."
    ),
    "loopx_runtime_origin_mismatch": (
        "Remove user-level LoopX path overrides and run the Zyra-bundled runtime."
    ),
    "loopx_runtime_unavailable": (
        "Restore the embedded runtime; archive and user-level fallbacks are disabled."
    ),
    "loopx_cli_entry_failed": (
        "Restore the embedded source and rerun doctor --deep from the Zyra release."
    ),
    "loopx_resource_missing": (
        "Restore the complete embedded extension, skill, template and package data."
    ),
    "loopx_python_incompatible": (
        "Use CPython 3.12 or newer for Zyra (LoopX itself requires 3.11 or newer)."
    ),
    "loopx_python_probe_failed": (
        "Select an executable Python interpreter and rerun the LoopX doctor."
    ),
    "loopx_retired_install_path": (
        "The historical .zyra/loopx/install directory is ignored. Preserve it "
        "for recovery evidence or remove it manually after independent backup."
    ),
}


class LoopXRuntimeError(RuntimeError):
    """Fail-closed embedded LoopX runtime, manifest, or doctor error."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: Mapping[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = dict(details or {})
        self.retryable = retryable

    @property
    def recovery(self) -> str:
        return RECOVERY_BY_CODE.get(
            self.code,
            "Inspect the LoopX doctor receipt and restore the matching Zyra release.",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.loopx-runtime-error/v1",
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "recovery": self.recovery,
            "details": self.details,
        }


# Historical callers import this name from the retired install facade.
LoopXInstallError = LoopXRuntimeError

__all__ = ["LoopXInstallError", "LoopXRuntimeError", "RECOVERY_BY_CODE"]
