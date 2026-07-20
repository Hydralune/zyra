from __future__ import annotations

from typing import Any

from .models import IntegrationDefinition
from .transport import ProviderControlPlaneProcess


class ProviderIntegrationPort:
    def __init__(self, process: ProviderControlPlaneProcess) -> None:
        self.process = process

    def list(self) -> list[dict[str, Any]]:
        values = self.process.request("integration.list")
        return [dict(item) for item in values or () if isinstance(item, dict)]

    def get(self, integration_id: str) -> dict[str, Any]:
        return dict(self.process.request("integration.get", {"integrationId": integration_id}) or {})

    def upsert(
        self,
        integration: IntegrationDefinition,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return dict(self.process.request("integration.upsert", {
            "integration": integration.to_wire(),
            "expectedRevision": expected_revision,
        }) or {})
