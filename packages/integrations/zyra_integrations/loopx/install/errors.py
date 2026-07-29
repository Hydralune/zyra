"""Compatibility facade for the retired archive-install error surface."""

from ..runtime.errors import LoopXInstallError, LoopXRuntimeError, RECOVERY_BY_CODE

__all__ = ["LoopXInstallError", "LoopXRuntimeError", "RECOVERY_BY_CODE"]
