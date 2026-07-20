from __future__ import annotations

from typing import Any

from .models import ModelDefinition, ProviderDefinition
from .transport import ProviderControlPlaneProcess


class ProviderCatalogPort:
    def __init__(self, process: ProviderControlPlaneProcess) -> None:
        self.process = process

    def snapshot(self) -> dict[str, Any]:
        return dict(self.process.request("catalog.snapshot") or {})

    def providers(self, *, available_only: bool = False) -> list[dict[str, Any]]:
        values = self.process.request("catalog.provider.list", {"availableOnly": available_only})
        return [dict(item) for item in values or () if isinstance(item, dict)]

    def provider(self, provider_id: str) -> dict[str, Any]:
        return dict(self.process.request("catalog.provider.get", {"providerId": provider_id}) or {})

    def upsert_provider(
        self,
        provider: ProviderDefinition,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return dict(self.process.request("catalog.provider.upsert", {
            "provider": provider.to_wire(),
            "expectedRevision": expected_revision,
        }) or {})

    def models(
        self,
        *,
        provider_id: str = "",
        available_only: bool = False,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"availableOnly": available_only}
        if provider_id:
            payload["providerId"] = provider_id
        values = self.process.request("catalog.model.list", payload)
        return [dict(item) for item in values or () if isinstance(item, dict)]

    def model(self, provider_id: str, model_id: str) -> dict[str, Any]:
        return dict(self.process.request("catalog.model.get", {
            "providerId": provider_id,
            "modelId": model_id,
        }) or {})

    def upsert_model(
        self,
        model: ModelDefinition,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return dict(self.process.request("catalog.model.upsert", {
            "model": model.to_wire(),
            "expectedRevision": expected_revision,
        }) or {})
