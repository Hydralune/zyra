from __future__ import annotations

from typing import Any

from .models import CredentialRegistration
from .transport import ProviderControlPlaneProcess


class ProviderCredentialPort:
    def __init__(self, process: ProviderControlPlaneProcess) -> None:
        self.process = process

    def register(self, credential: CredentialRegistration) -> dict[str, Any]:
        return dict(self.process.request("credential.register", {"credential": credential.to_wire()}) or {})

    def rotate(
        self,
        credential_id: str,
        *,
        expected_version: int,
        update: CredentialRegistration,
    ) -> dict[str, Any]:
        return dict(
            self.process.request(
                "credential.rotate",
                {
                    "credentialId": credential_id,
                    "expectedVersion": expected_version,
                    "update": update.to_wire(),
                },
            )
            or {}
        )

    def get(self, credential_id: str) -> dict[str, Any]:
        return dict(self.process.request("credential.get", {"credentialId": credential_id}) or {})

    def list(self, *, provider_id: str = "") -> list[dict[str, Any]]:
        payload = {"providerId": provider_id} if provider_id else {}
        values = self.process.request("credential.list", payload)
        return [dict(item) for item in values or () if isinstance(item, dict)]

    def revoke(self, credential_id: str, *, expected_version: int) -> dict[str, Any]:
        return dict(self.process.request("credential.revoke", {
            "credentialId": credential_id,
            "expectedVersion": expected_version,
        }) or {})

    def block(
        self,
        credential_id: str,
        *,
        expected_version: int,
        reason: str,
    ) -> dict[str, Any]:
        return dict(self.process.request("credential.block", {
            "credentialId": credential_id,
            "expectedVersion": expected_version,
            "reason": reason,
        }) or {})
