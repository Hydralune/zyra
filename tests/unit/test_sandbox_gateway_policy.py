from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "workspace",
):
    if str(package) not in sys.path:
        sys.path.insert(0, str(package))

from zyra_runtime.sandbox_gateway import (  # noqa: E402
    ArtifactProvenance,
    CommandEffect,
    FileArtifactRequest,
    GatewayCommandEnvelope,
    GatewayFilePolicy,
    ProvenanceKind,
    ProvenancePolicy,
    SecretRedactor,
    StructuredCommandPolicy,
    TrustLevel,
    canonical_logical_path,
)
from zyra_runtime.sandbox_gateway.errors import SandboxGatewayError  # noqa: E402
from zyra_runtime.sandbox_gateway.network_policy import (  # noqa: E402
    NetworkPolicy,
    NetworkProfile,
)


def command(
    executable: str,
    argv: tuple[str, ...] = (),
    *,
    network_profile: str = "offline",
    metadata: dict | None = None,
) -> GatewayCommandEnvelope:
    return GatewayCommandEnvelope.build(
        session_id="session-policy",
        run_id="run-policy",
        task_id="task-policy",
        worker_id="CodeWorkerRuntime",
        executable=executable,
        argv=argv,
        tool_use_id="tool-policy",
        network_profile=network_profile,
        metadata=metadata,
    )


class SandboxGatewayPolicyTests(unittest.TestCase):
    def test_read_only_git_is_allowed_but_destructive_git_is_denied(self) -> None:
        policy = StructuredCommandPolicy()

        read_only = policy.evaluate(command("git", ("status", "--short")))
        destructive = policy.evaluate(command("git", ("reset", "--hard", "HEAD")))

        self.assertEqual(read_only.effect, CommandEffect.ALLOW)
        self.assertTrue(read_only.eligible_for_sealed_auto_allow)
        self.assertEqual(destructive.effect, CommandEffect.DENY)
        self.assertIn(
            "git.destructive",
            {item.code for item in destructive.evidence},
        )

    def test_lost_commit_diagnostics_distinguish_reads_from_local_recovery(self) -> None:
        policy = StructuredCommandPolicy()

        reflog = policy.evaluate(command("git", ("reflog", "--all", "-30")))
        lost_found = policy.evaluate(command("git", ("fsck", "--lost-found")))

        self.assertEqual(reflog.effect, CommandEffect.ALLOW)
        self.assertTrue(reflog.eligible_for_sealed_auto_allow)
        self.assertIn("git.read_only", {item.code for item in reflog.evidence})
        self.assertEqual(lost_found.effect, CommandEffect.ASK)
        self.assertFalse(lost_found.eligible_for_sealed_auto_allow)
        self.assertIn(
            "git.local_mutation",
            {item.code for item in lost_found.evidence},
        )

    def test_shell_redirection_and_encoded_powershell_fail_closed(self) -> None:
        policy = StructuredCommandPolicy()

        redirected = policy.evaluate(
            command("cmd.exe", ("/c", "echo unsafe > bypass.txt"))
        )
        encoded = policy.evaluate(
            command("pwsh", ("-EncodedCommand", "ZQBjAGgAbwA="))
        )

        self.assertEqual(redirected.effect, CommandEffect.DENY)
        self.assertIn(
            "shell.redirection",
            {item.code for item in redirected.evidence},
        )
        self.assertEqual(encoded.effect, CommandEffect.DENY)
        self.assertIn(
            "shell.powershell_dynamic",
            {item.code for item in encoded.evidence},
        )

    def test_sealed_mode_denies_ask_and_caller_bypass_has_no_authority(self) -> None:
        policy = StructuredCommandPolicy()
        decision = policy.evaluate(
            command(
                "unregistered-tool",
                ("--read",),
                metadata={"permission_bypass": True, "auto_approve": True},
            ),
            sealed=True,
        )

        self.assertEqual(decision.effect, CommandEffect.DENY)
        codes = {item.code for item in decision.evidence}
        self.assertIn("executable.unknown", codes)
        self.assertIn("command.untrusted_override_ignored", codes)
        self.assertIn("sealed.ask_denied", codes)

    def test_network_profile_binds_hosts_and_offline_denies(self) -> None:
        network = NetworkPolicy(
            [
                NetworkProfile(
                    profile_id="docs",
                    allowed_schemes=frozenset({"https"}),
                    allowed_hosts=frozenset({"docs.example.test"}),
                    require_approval=True,
                )
            ]
        )
        policy = StructuredCommandPolicy(network_policy=network)

        offline = policy.evaluate(
            command("curl", ("https://docs.example.test/page",))
        )
        allowed = policy.evaluate(
            command(
                "curl",
                ("https://docs.example.test/page",),
                network_profile="docs",
            )
        )
        denied = policy.evaluate(
            command(
                "curl",
                ("https://collector.example.test/upload",),
                network_profile="docs",
            )
        )

        self.assertEqual(offline.effect, CommandEffect.DENY)
        self.assertEqual(allowed.effect, CommandEffect.ASK)
        self.assertEqual(denied.effect, CommandEffect.DENY)

    def test_paths_reject_traversal_unc_drive_and_reserved_devices(self) -> None:
        for value in (
            "../outside.txt",
            r"\\server\share\payload",
            r"C:\outside.txt",
            "nested/NUL",
        ):
            with self.subTest(value=value), self.assertRaises(SandboxGatewayError):
                canonical_logical_path(value)
        self.assertEqual(
            canonical_logical_path(r"nested\safe.txt"),
            "nested/safe.txt",
        )

    def test_untrusted_sources_cannot_write_policy_or_active_content(self) -> None:
        policy = GatewayFilePolicy(
            provenance_policy=ProvenancePolicy(
                executable_from_untrusted=False,
                archive_from_untrusted=False,
            )
        )
        provenance = ArtifactProvenance.build(
            kind=ProvenanceKind.WEB,
            trust=TrustLevel.UNTRUSTED,
            source_id="https://example.test/instructions",
            untrusted_instructions=True,
        )
        control = FileArtifactRequest.build(
            session_id="session-policy",
            logical_path=".codex/settings.json",
            content='{"permission":"allow"}',
            content_type="application/json",
            provenance=provenance,
        )
        executable = FileArtifactRequest.build(
            session_id="session-policy",
            logical_path="downloads/install.ps1",
            content="Start-Process unsafe",
            content_type="text/plain",
            provenance=provenance,
            executable_allowed=True,
        )

        control_decision = policy.inspect(control)
        executable_decision = policy.inspect(executable)

        self.assertFalse(control_decision.allowed)
        self.assertTrue(control_decision.quarantine)
        self.assertIn(
            "provenance.untrusted_control_write",
            {item.code for item in control_decision.findings},
        )
        self.assertTrue(executable_decision.quarantine)

    def test_redaction_covers_nested_keys_terminal_tokens_and_urls(self) -> None:
        secret = "sk-live-ThisIsASecretValue123456"
        redactor = SecretRedactor(known_secrets=(secret,))
        report = redactor.redact_value(
            {
                "authorization": f"Bearer {secret}",
                "nested": {
                    "message": f"connect https://user:{secret}@example.test/",
                    "api_token": secret,
                },
            },
            source="test",
        )
        serialized = json.dumps(report.value, sort_keys=True)

        self.assertTrue(report.changed)
        self.assertNotIn(secret, serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_mutating_approved_envelope_changes_command_identity(self) -> None:
        original = command("rg", ("needle", "."))
        mutated = replace(original, argv=("different", "."))
        policy = StructuredCommandPolicy()

        with self.assertRaises(ValueError):
            policy.assert_unchanged(original, mutated)


if __name__ == "__main__":
    unittest.main()
