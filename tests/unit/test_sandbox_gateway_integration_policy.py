from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package in ROOT.joinpath("packages").iterdir():
    if package.is_dir() and str(package) not in sys.path:
        sys.path.insert(0, str(package))

from zyra_runtime import LocalArtifactStore  # noqa: E402
from zyra_runtime.sandbox_gateway import (  # noqa: E402
    GatewayCommandEnvelope,
    OperationKind,
    ProvenanceKind,
    TrustLevel,
)
from zyra_runtime.sandbox_gateway.integration_audit import (  # noqa: E402
    GatewayIntegrationAuditor,
)
from zyra_runtime.sandbox_gateway.integration_custody import (  # noqa: E402
    assert_integration_source_custody,
)
from zyra_runtime.sandbox_gateway.integration_factory import (  # noqa: E402
    build_gateway_runtime_bundle,
)
from zyra_runtime.sandbox_gateway.integration_models import content_digest  # noqa: E402
from zyra_runtime.sandbox_gateway.integration_remote import (  # noqa: E402
    GatewayDispatchAttestor,
)
from zyra_workspace import (  # noqa: E402
    WorkspaceEditPort,
    WorkspaceManagerConfig,
    WorkspaceManagerRuntime,
)


class SandboxGatewayIntegrationPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = WorkspaceManagerRuntime(
            WorkspaceManagerConfig(
                state_root=self.root / "state",
                data_root=self.root / "data",
            )
        )
        created = self.manager.create_for_task(
            run_id="run-policy",
            task_id="task-policy",
            session_id="session-policy",
            worker_id="CodeWorkerRuntime",
            idempotency_key="create-policy",
        )
        self.port = WorkspaceEditPort(
            self.manager,
            created.access,
            worker_id="CodeWorkerRuntime",
            run_id="run-policy",
            task_id="task-policy",
            node_id="node-policy",
            artifact_store=LocalArtifactStore(self.root / "artifacts"),
        )
        self.workspace = self.manager.internal_task_root(created.access)
        self.bundle = build_gateway_runtime_bundle(
            workspace_root=self.workspace,
            artifact_root=self.root / "artifacts",
            worker_id="CodeWorkerRuntime",
            workspace_edit_port=self.port,
            runtime_services={
                "sandbox_gateway_required": True,
                "sandbox_gateway_allowed_hosts": ("example.com",),
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_structured_command_and_interpreter_policy(self) -> None:
        executable, argv, environment, cwd = self.bundle.policy_runtime.command_from_arguments(
            {
                "executable": "python",
                "argv": ["-c", "print('safe')"],
                "environment": {"NO_COLOR": "1"},
                "cwd": ".",
            }
        )
        envelope = GatewayCommandEnvelope(
            command_id="command-policy-1",
            session_id="session-policy",
            run_id="run-policy",
            task_id="task-policy",
            worker_id="CodeWorkerRuntime",
            executable=executable,
            argv=argv,
            cwd=cwd,
            environment=environment,
            operation=OperationKind.COMMAND,
            tool_use_id="tool-policy-1",
        )
        decision = self.bundle.policy_runtime.evaluate_command(envelope)
        self.assertTrue(decision.requires_permission)
        self.assertFalse(decision.hard_denied)
        with self.assertRaisesRegex(ValueError, "shell operators"):
            self.bundle.policy_runtime.command_from_arguments(
                {"command": "python -c pass && echo bypass"}
            )
        with self.assertRaisesRegex(ValueError, "CredentialRelay"):
            self.bundle.policy_runtime.command_from_arguments(
                {
                    "executable": "python",
                    "argv": ["-V"],
                    "environment": {"API_TOKEN": "secret"},
                }
            )

    def test_url_path_and_source_role_boundaries(self) -> None:
        denied = self.bundle.policy_runtime.evaluate_url("https://127.0.0.1/private")
        self.assertFalse(denied.allowed)
        allowed = self.bundle.policy_runtime.evaluate_url(
            "https://example.com/path",
            allowed_hosts=("example.com",),
        )
        self.assertTrue(allowed.allowed)
        for path in ("../escape", "C:/escape", "//server/share", "safe/../escape"):
            with self.assertRaises(ValueError, msg=path):
                self.bundle.policy_runtime.assert_path(path)
        custody = assert_integration_source_custody()
        self.assertEqual(custody["canonical_gateway_owner"], "SandboxGatewayRuntime")
        self.assertEqual(custody["second_gateway_count"], 0)

    def test_dispatch_attestation_detects_workspace_tampering(self) -> None:
        attestor = GatewayDispatchAttestor.for_workspace(
            workspace_root=self.workspace,
            artifact_root=self.root / "dispatch-artifacts",
        )
        envelope = {
            "envelope_id": "dispatch-policy-1",
            "run_id": "run-policy",
            "task_id": "task-policy",
            "runtime_worker": "CodeWorkerRuntime",
            "backend": "local-process",
            "location": "local",
            "sandbox": "process-isolation",
            "gateway": "zyra-sandbox-gateway",
            "workspace_root": str(self.workspace),
            "artifact_root": str(self.root / "dispatch-artifacts"),
            "owner_epoch": self.port.current_access().owner_epoch,
            "backend_generation": 1,
        }
        attestation = attestor.attest(envelope)
        self.assertTrue(attestor.validate(envelope, attestation.receipt))
        self.assertFalse(
            attestor.validate(
                {**envelope, "backend_generation": 2},
                attestation.receipt,
            )
        )

    def test_static_audit_detects_raw_process_and_external_source(self) -> None:
        project = self.root / "audit-project"
        source = project / "packages" / "unsafe.py"
        source.parent.mkdir(parents=True)
        source.write_text(
            "import subprocess\nsubprocess.run(['cmd'])\nSOURCE='../OpenHands'\n",
            encoding="utf-8",
        )
        report = GatewayIntegrationAuditor(project).audit(("packages",))
        codes = {item.code for item in report.findings}
        self.assertIn("gateway_primitive_bypass", codes)
        self.assertIn("external_source_runtime_dependency", codes)
        self.assertFalse(report.passed)


if __name__ == "__main__":
    unittest.main()
