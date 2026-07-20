from __future__ import annotations

from typing import Any

from .transport import ProviderControlPlaneProcess


class ProviderCompatV1Port:
    """Read-only 02D/V1 projection: no default resolution and no write route."""

    def __init__(self, process: ProviderControlPlaneProcess) -> None:
        self.process = process

    def snapshot(self) -> dict[str, Any]:
        value = dict(self.process.request("catalog.compat_v1") or {})
        if value.get("writable") is not False or value.get("defaultModel") is not None:
            raise RuntimeError("provider V1 compatibility projection violated read-only contract")
        return value
