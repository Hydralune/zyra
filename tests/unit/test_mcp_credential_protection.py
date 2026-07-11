from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
for package_path in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "runtime",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


from zyra_integrations.mcp.credentials import (  # noqa: E402
    CredentialCorrupt,
    CredentialProtectionUnavailable,
    CredentialReference,
    FileCredentialVault,
    IdentitySecretCodec,
    WindowsDpapiSecretCodec,
    _default_secret_codec,
)
from zyra_integrations.mcp.runtime import McpClientRuntime  # noqa: E402


class McpCredentialProtectionTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows DPAPI behavior")
    def test_default_file_vault_uses_dpapi_and_rejects_ciphertext_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "credentials"
            vault = FileCredentialVault(root)
            self.assertIsInstance(vault.codec, WindowsDpapiSecretCodec)
            reference = CredentialReference(
                "mcp",
                "server-a",
                "mcp-config:sha256:" + "a" * 64,
            )
            token = "dpapi-token-that-must-not-be-base64-plaintext"
            vault.put(reference, {"access_token": token})

            path = next(root.glob("*.credential"))
            document = json.loads(path.read_text(encoding="utf-8"))
            protected = base64.b64decode(document["payload"], validate=True)
            self.assertEqual(document["codec"], "windows-dpapi-current-user-v1")
            self.assertNotIn(token.encode("utf-8"), protected)
            self.assertNotIn(token, protected.decode("latin-1"))
            with vault.get(reference) as envelope:
                self.assertEqual(envelope.reveal_text("access_token"), token)

            # Recompute the outer SHA-256 after flipping ciphertext.  This
            # proves DPAPI authentication, not merely the document checksum,
            # detects the modification.
            tampered = bytearray(protected)
            tampered[-1] ^= 0x01
            document["payload"] = base64.b64encode(tampered).decode("ascii")
            document["payload_digest"] = "sha256:" + hashlib.sha256(tampered).hexdigest()
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(CredentialCorrupt):
                vault.get(reference)

    def test_identity_codec_requires_explicit_development_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            vault = FileCredentialVault(
                Path(directory) / "credentials",
                codec=IdentitySecretCodec(),
            )
            reference = CredentialReference(
                "mcp",
                "dev-server",
                "mcp-config:sha256:" + "b" * 64,
            )
            vault.put(reference, {"access_token": "explicit-dev-token"})
            document = json.loads(next(vault.root.glob("*.credential")).read_text(encoding="utf-8"))
            self.assertEqual(document["codec"], "identity-insecure-dev-v1")
            self.assertIn(b"explicit-dev-token", base64.b64decode(document["payload"]))

    def test_unavailable_platform_is_lazy_fail_closed_for_credential_free_runtime(self) -> None:
        unavailable = _default_secret_codec(platform_name="posix")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Simulate the non-Windows default without changing pathlib's
            # process-wide platform selection.
            with patch(
                "zyra_integrations.mcp.credentials._default_secret_codec",
                return_value=unavailable,
            ):
                runtime = McpClientRuntime.from_paths(
                    state_path=root / "mcp-state.json",
                    artifact_root=root / "artifacts",
                    credential_root=root / "credentials",
                )
            diagnostics = runtime.diagnostics()
            self.assertTrue(diagnostics["enabled"])
            runtime.add_server(
                "local",
                {"server_id": "local", "transport": "in_process"},
            )
            with self.assertRaises(CredentialProtectionUnavailable):
                runtime.install_auth_tokens(
                    "local",
                    {"access_token": "must-fail-closed"},
                )


if __name__ == "__main__":
    unittest.main()
