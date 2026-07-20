from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [ROOT / "packages" / "core", ROOT / "packages" / "runtime"]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_runtime.provider_control_plane import (
    IntegrationDefinition,
    ModelCapabilities,
    ModelDefinition,
    ProviderControlPlaneClient,
    ProviderControlPlanePortError,
    ProviderDefinition,
    ProviderProtocol,
)


class ProviderControlPlanePortTests(unittest.TestCase):
    def test_process_port_reaches_typescript_owner_and_read_only_v1(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with ProviderControlPlaneClient(
                project_root=ROOT,
                database_path=Path(tmpdir) / "provider.sqlite3",
            ) as client:
                health = client.health()
                self.assertEqual(health["stateOwner"], "typescript.ProviderControlPlaneStore")
                self.assertTrue(health["providerFallbackOwned"])
                self.assertFalse(health["backendFallbackOwned"])

                integration = IntegrationDefinition(
                    integration_id="test-integration",
                    display_name="Test",
                    kind="anonymous",
                )
                provider = ProviderDefinition(
                    provider_id="test-provider",
                    display_name="Test Provider",
                    integration_id="test-integration",
                    status="active",
                    base_url="http://127.0.0.1:9",
                    protocol=ProviderProtocol.OPENAI_CHAT,
                )
                model = ModelDefinition(
                    provider_id="test-provider",
                    model_id="test-model",
                    display_name="Test Model",
                    family="test",
                    released_at=1,
                    capabilities=ModelCapabilities(),
                )
                client.integrations.upsert(integration)
                client.catalog.upsert_provider(provider)
                client.catalog.upsert_model(model)

                snapshot = client.catalog.snapshot()
                self.assertEqual(snapshot["revision"], 3)
                self.assertEqual(snapshot["providers"][0]["providerId"], "test-provider")
                compat = client.compat_v1.snapshot()
                self.assertFalse(compat["writable"])
                self.assertIsNone(compat["defaultModel"])
                self.assertEqual(compat["catalogRevision"], 3)

    def test_process_port_rejects_inline_secret_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with ProviderControlPlaneClient(
                project_root=ROOT,
                database_path=Path(tmpdir) / "provider.sqlite3",
            ) as client:
                with self.assertRaises(ProviderControlPlanePortError) as raised:
                    client.process.request(
                        "credential.register",
                        {
                            "credential": {
                                "providerId": "p",
                                "integrationId": "i",
                                "accountId": "a",
                                "secretRef": "env://KEY",
                                "fingerprint": "sha256:0000000000000000",
                                "apiKey": "must-never-cross-rpc",
                            }
                        },
                    )
                self.assertEqual(raised.exception.code, "provider_inline_secret_rejected")


if __name__ == "__main__":
    unittest.main()
