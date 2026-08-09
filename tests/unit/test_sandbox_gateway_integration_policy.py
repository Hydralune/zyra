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
    CallbackCredentialProvider,
    CancellationToken,
    CredentialRelay,
    DockerSandboxBackend,
    GatewayCommandEnvelope,
    GatewaySessionRecord,
    OperationKind,
    ProcessOutput,
    ProcessResult,
    ProcessTermination,
    ProvenanceKind,
    SimulatedSandboxBackend,
    TrustLevel,
)
from zyra_runtime.sandbox_gateway.integration_host import (  # noqa: E402
    GatewayHostProcessRuntime,
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
                "sandbox_gateway_allowed_environment_keys": ("PYTHONPATH",),
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
        pythonpath = self.bundle.policy_runtime.command_from_arguments(
            {
                "executable": "python3",
                "argv": ["-m", "unittest"],
                "environment": {"PYTHONPATH": "src"},
            }
        )
        pythonpath_decision = self.bundle.policy_runtime.evaluate_command(
            GatewayCommandEnvelope(
                command_id="command-policy-pythonpath",
                session_id="session-policy",
                run_id="run-policy",
                task_id="task-policy",
                worker_id="CodeWorkerRuntime",
                executable=pythonpath[0],
                argv=pythonpath[1],
                environment=pythonpath[2],
                cwd=pythonpath[3],
                operation=OperationKind.COMMAND,
                tool_use_id="tool-policy-pythonpath",
            )
        )
        self.assertFalse(pythonpath_decision.hard_denied)
        self.assertNotIn(
            "environment.key_not_allowed",
            {item.code for item in pythonpath_decision.findings},
        )
        script = self.bundle.policy_runtime.command_from_arguments(
            {"command": "sh bin/verify"}
        )
        script_decision = self.bundle.policy_runtime.evaluate_command(
            GatewayCommandEnvelope(
                command_id="command-policy-script",
                session_id="session-policy",
                run_id="run-policy",
                task_id="task-policy",
                worker_id="CodeWorkerRuntime",
                executable=script[0],
                argv=script[1],
                environment=script[2],
                cwd=script[3],
                operation=OperationKind.COMMAND,
                tool_use_id="tool-policy-script",
            )
        )
        self.assertFalse(script_decision.hard_denied)
        self.assertNotIn(
            "shell.command_missing",
            {item.code for item in script_decision.findings},
        )
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

    def test_interactive_host_uses_single_use_scoped_credential_relay(self) -> None:
        relay = CredentialRelay(
            CallbackCredentialProvider(lambda _request: "test-only-secret")
        )
        host = GatewayHostProcessRuntime(
            self.bundle.policy_runtime,
            allowed_roots=(self.workspace,),
        )
        process = host.start_interactive(
            executable=sys.executable,
            argv=(
                "-c",
                "import os; print('present' if os.environ.get('TEST_API_KEY') "
                "== 'test-only-secret' else 'missing', flush=True)",
            ),
            cwd=self.workspace,
            environment={"NO_COLOR": "1"},
            credential_relay=relay,
            credential_environment_name="TEST_API_KEY",
            credential_provider="test-provider",
            credential_scope=("provider:model:dispatch",),
            credential_provenance_ref="route:test",
        )
        assert process.stdout is not None
        self.assertEqual(process.stdout.readline().strip(), "present")
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertTrue(getattr(process, "_zyra_credential_relay_used"))
        self.assertEqual(relay.descriptor()["active_envelopes"], 0)
        host.release_interactive(process)

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

    def test_simulated_and_docker_backends_share_the_canonical_contract(self) -> None:
        simulated = SimulatedSandboxBackend(self.root / "simulated")
        simulated_record = GatewaySessionRecord.create(
            session_id="session-simulated",
            run_id="run-policy",
            task_id="task-policy",
            workspace_id=self.port.workspace_id,
            worker_id="CodeWorkerRuntime",
            backend_id=simulated.backend_id,
        )
        simulated_session = simulated.prepare(simulated_record)
        simulated_envelope = GatewayCommandEnvelope.build(
            session_id=simulated_record.session_id,
            run_id=simulated_record.run_id,
            task_id=simulated_record.task_id,
            worker_id=simulated_record.worker_id,
            executable="python",
            argv=("-V",),
            tool_use_id="simulated-tool",
        )
        simulated_result = simulated.execute(
            simulated_session,
            simulated_envelope,
            CancellationToken(),
        )
        self.assertTrue(simulated_result.ok)
        self.assertFalse(simulated.descriptor()["default"])

        class Connector:
            def prepare(self, record):
                return {"container_id_digest": "sha256:test"}

            def execute(self, session, envelope, cancellation, *, on_chunk=None):
                return ProcessResult(
                    command_id=envelope.command_id,
                    termination=ProcessTermination.EXITED,
                    return_code=0,
                    started_at=1.0,
                    finished_at=2.0,
                    output=ProcessOutput(stdout=b"docker-contract"),
                    backend_id="zyra.docker-sandbox.v1",
                )

            def cleanup(self, session):
                return None

            def cancel(self, command_id, reason):
                return True

        docker = DockerSandboxBackend(self.root / "docker", Connector())
        docker_record = GatewaySessionRecord.create(
            session_id="session-docker",
            run_id="run-policy",
            task_id="task-policy",
            workspace_id=self.port.workspace_id,
            worker_id="CodeWorkerRuntime",
            backend_id=docker.backend_id,
        )
        docker_session = docker.prepare(docker_record)
        docker_envelope = GatewayCommandEnvelope.build(
            session_id=docker_record.session_id,
            run_id=docker_record.run_id,
            task_id=docker_record.task_id,
            worker_id=docker_record.worker_id,
            executable="python",
            argv=("-V",),
            tool_use_id="docker-tool",
        )
        docker_result = docker.execute(docker_session, docker_envelope, CancellationToken())
        self.assertEqual(docker_result.output.stdout, b"docker-contract")
        self.assertTrue(docker.cancel(docker_envelope.command_id, "test"))


if __name__ == "__main__":
    unittest.main()
