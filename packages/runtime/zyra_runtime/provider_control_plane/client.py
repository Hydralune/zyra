from __future__ import annotations

from pathlib import Path
from typing import Any

from .catalog import ProviderCatalogPort
from .compat_v1 import ProviderCompatV1Port
from .credentials import ProviderCredentialPort
from .integrations import ProviderIntegrationPort
from .models import ProviderDispatchRequest
from .routing import ProviderRoutingPort
from .store import ProviderControlPlaneStorePort
from .transport import ProviderControlPlaneProcess


class ProviderControlPlaneClient:
    def __init__(
        self,
        *,
        project_root: str | Path,
        database_path: str | Path,
        request_timeout_seconds: float = 180.0,
    ) -> None:
        self.process = ProviderControlPlaneProcess(
            project_root=project_root,
            database_path=database_path,
            request_timeout_seconds=request_timeout_seconds,
        )
        self.store = ProviderControlPlaneStorePort(self.process)
        self.catalog = ProviderCatalogPort(self.process)
        self.integrations = ProviderIntegrationPort(self.process)
        self.credentials = ProviderCredentialPort(self.process)
        self.routing = ProviderRoutingPort(self.process)
        self.compat_v1 = ProviderCompatV1Port(self.process)

    def health(self) -> dict[str, Any]:
        return self.process.health()

    def dispatch(self, request: ProviderDispatchRequest) -> dict[str, Any]:
        return dict(self.process.request("dispatch", {"request": request.to_wire()}) or {})

    def install_configured_profiles(self) -> dict[str, Any]:
        """Install only live profiles whose environment references are present.

        Secret bytes stay in the provider process environment; the RPC result
        contains only credential identifiers, versions, fingerprints and
        ``env://`` references.
        """

        return dict(self.process.request("profiles.install_configured") or {})

    def close(self) -> None:
        self.process.close()

    def __enter__(self) -> ProviderControlPlaneClient:
        self.process.__enter__()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
