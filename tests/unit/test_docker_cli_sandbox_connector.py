from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
for package in ROOT.joinpath("packages").iterdir():
    if package.is_dir() and str(package) not in sys.path:
        sys.path.insert(0, str(package))

from zyra_runtime.sandbox_gateway import (  # noqa: E402
    BackendSession,
    CancellationToken,
    DockerCliSandboxConnector,
    DockerSandboxBackend,
    GatewayCommandEnvelope,
    GatewaySessionRecord,
    ProcessTermination,
    SandboxGatewayConfig,
    SandboxGatewayRuntime,
)
from zyra_orchestration.deployment import code_worker_adapter  # noqa: E402
from zyra_workers.typescript_claude_runtime import (  # noqa: E402
    _typescript_runtime_timeout_seconds,
)


class DockerCliSandboxConnectorTests(unittest.TestCase):
    def test_worker_failure_with_workspace_effects_requires_verification(self) -> None:
        self.assertEqual(
            code_worker_adapter._workspace_execution_outcome(
                worker_ok=False,
                workspace_delta={"created": ["answer.txt"]},
            ),
            ("needs_verification", True),
        )
        self.assertEqual(
            code_worker_adapter._workspace_execution_outcome(
                worker_ok=False,
                workspace_delta={"created": [], "modified": [], "deleted": []},
            ),
            ("failed", False),
        )

    def test_explicit_benchmark_reasoning_budget_propagates_without_hidden_caps(self) -> None:
        self.assertEqual(
            code_worker_adapter._code_worker_reasoning_budget(
                {"max_turns": 12, "reasoning_timeout_seconds": 600},
                benchmark_execution=True,
            ),
            (12, 600.0),
        )
        self.assertEqual(
            _typescript_runtime_timeout_seconds(
                {
                    "benchmark_physical_dispatch": True,
                    "typescript_runtime_timeout_seconds": 900,
                }
            ),
            900.0,
        )
        self.assertEqual(
            code_worker_adapter._benchmark_runtime_constraints({}),
            {"benchmark_physical_dispatch": True},
        )

    def test_explicit_long_horizon_budget_uses_the_authoritative_deadline(self) -> None:
        context = {
            "benchmark_long_horizon": True,
            "max_turns": 80,
            "reasoning_timeout_seconds": 2_400,
        }
        self.assertEqual(
            code_worker_adapter._code_worker_reasoning_budget(
                context,
                benchmark_execution=True,
            ),
            (80, 2_400.0),
        )
        self.assertEqual(
            _typescript_runtime_timeout_seconds(
                {
                    **code_worker_adapter._benchmark_runtime_constraints(context),
                    "typescript_runtime_timeout_seconds": 2_400,
                }
            ),
            2_400.0,
        )
        self.assertEqual(
            code_worker_adapter._benchmark_runtime_constraints(context),
            {
                "benchmark_physical_dispatch": True,
                "benchmark_long_horizon": True,
                "model_api_timeout_seconds": 300.0,
                "model_api_timeout_milliseconds": 300_000,
            },
        )
        # The long-horizon marker does not invent a separate internal budget.
        self.assertIsNone(
            _typescript_runtime_timeout_seconds(
                {
                    "benchmark_long_horizon": True,
                }
            )
        )

    def test_production_reasoning_budget_is_open_without_an_explicit_deadline(self) -> None:
        self.assertEqual(
            code_worker_adapter._code_worker_reasoning_budget(
                {"max_turns": 48, "reasoning_timeout_seconds": 900},
                benchmark_execution=False,
            ),
            (48, 900.0),
        )
        self.assertEqual(
            _typescript_runtime_timeout_seconds(
                {"typescript_runtime_timeout_seconds": 900}
            ),
            900.0,
        )
        self.assertEqual(
            code_worker_adapter._code_worker_reasoning_budget(
                {},
                benchmark_execution=False,
            ),
            (None, None),
        )
        self.assertIsNone(_typescript_runtime_timeout_seconds({}))

    def test_rejects_ambiguous_container_and_workdir(self) -> None:
        with self.assertRaisesRegex(ValueError, "container reference"):
            DockerCliSandboxConnector(container="bad container", workdir="/app")
        with self.assertRaisesRegex(ValueError, "absolute container path"):
            DockerCliSandboxConnector(container="task-main-1", workdir="../app")

    def test_builds_argument_vector_without_a_host_shell(self) -> None:
        connector = DockerCliSandboxConnector(
            container="task-main-1",
            workdir="/app/repo",
            docker_executable="docker-test",
        )
        envelope = GatewayCommandEnvelope.build(
            session_id="session-1",
            run_id="run-1",
            task_id="task-1",
            worker_id="worker-1",
            executable="git",
            argv=("status", "--short"),
            cwd="src",
            environment={"NO_COLOR": "1"},
        )
        self.assertEqual(
            connector.command_argv(envelope),
            [
                "docker-test",
                "exec",
                "--workdir",
                "/app/repo/src",
                "--env",
                "NO_COLOR=1",
                "task-main-1",
                "git",
                "status",
                "--short",
            ],
        )

    def test_backend_exposes_the_connectors_redaction_boundary(self) -> None:
        connector = DockerCliSandboxConnector(
            container="task-main-1",
            workdir="/app",
            docker_executable="docker-test",
        )
        with tempfile.TemporaryDirectory() as temporary:
            backend = DockerSandboxBackend(temporary, connector)
        self.assertIs(backend.redactor, connector.redactor)

    def test_backend_reattaches_durable_connector_session_after_restart(self) -> None:
        connector = DockerCliSandboxConnector(
            container="task-main-1",
            workdir="/app",
            docker_executable="docker-test",
        )
        record = GatewaySessionRecord.create(
            session_id="session-1",
            run_id="run-1",
            task_id="task-1",
            workspace_id="workspace-1",
            worker_id="worker-1",
            backend_id=connector.backend_id,
        )
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=b"true\n",
            stderr=b"",
        )
        with tempfile.TemporaryDirectory() as temporary, patch(
            "zyra_runtime.sandbox_gateway.docker_cli_connector.subprocess.run",
            return_value=completed,
        ) as inspect:
            original = DockerSandboxBackend(temporary, connector)
            prepared = original.prepare(record)
            restarted = DockerSandboxBackend(temporary, connector)
            with self.assertRaisesRegex(Exception, "not prepared in this process"):
                restarted.get(record.session_id)
            recovered = restarted.recover(record)

        self.assertEqual(recovered.session_id, prepared.session_id)
        self.assertEqual(recovered.execution_root, prepared.execution_root)
        self.assertTrue(recovered.metadata["recovered"])
        self.assertEqual(inspect.call_count, 2)

    def test_runtime_lazily_recovers_missing_process_local_backend_session(self) -> None:
        connector = DockerCliSandboxConnector(
            container="task-main-1",
            workdir="/app",
            docker_executable="docker-test",
        )
        record = GatewaySessionRecord.create(
            session_id="session-1",
            run_id="run-1",
            task_id="task-1",
            workspace_id="workspace-1",
            worker_id="worker-1",
            backend_id=connector.backend_id,
        )
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=b"true\n",
            stderr=b"",
        )
        with tempfile.TemporaryDirectory() as temporary, patch(
            "zyra_runtime.sandbox_gateway.docker_cli_connector.subprocess.run",
            return_value=completed,
        ):
            original = DockerSandboxBackend(temporary, connector)
            original.prepare(record)
            restarted = DockerSandboxBackend(temporary, connector)
            runtime = object.__new__(SandboxGatewayRuntime)
            runtime.config = SandboxGatewayConfig(state_root=Path(temporary))
            runtime.backend = restarted
            runtime._backend_sessions = {}
            runtime._backend_lock = threading.RLock()
            recovered = runtime._backend_session(record)

        self.assertTrue(recovered.metadata["recovered"])
        self.assertIs(runtime._backend_sessions[record.session_id], recovered)

    def test_prepare_requires_a_live_preexisting_container(self) -> None:
        connector = DockerCliSandboxConnector(
            container="task-main-1",
            workdir="/app",
            docker_executable="docker-test",
        )
        record = GatewaySessionRecord.create(
            session_id="session-1",
            run_id="run-1",
            task_id="task-1",
            workspace_id="workspace-1",
            worker_id="worker-1",
            backend_id=connector.backend_id,
        )
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=b"true\n",
            stderr=b"",
        )
        with patch(
            "zyra_runtime.sandbox_gateway.docker_cli_connector.subprocess.run",
            return_value=completed,
        ) as run:
            metadata = connector.prepare(record)
        self.assertTrue(metadata["container_running_verified"])
        self.assertEqual(metadata["container_lifecycle_owner"], "external-harness")
        self.assertEqual(
            run.call_args.args[0],
            [
                "docker-test",
                "inspect",
                "--format={{.State.Running}}",
                "task-main-1",
            ],
        )

    def test_execute_records_real_subprocess_output_and_identity(self) -> None:
        connector = DockerCliSandboxConnector(
            container="task-main-1",
            workdir="/app",
            docker_executable="docker-test",
        )
        envelope = GatewayCommandEnvelope.build(
            session_id="session-1",
            run_id="run-1",
            task_id="task-1",
            worker_id="worker-1",
            executable="ignored-in-test",
        )
        session = BackendSession(
            session_id="session-1",
            backend_id=connector.backend_id,
            execution_root=Path(tempfile.gettempdir()),
            generation=1,
            prepared_at=0.0,
        )
        real_command = [
            sys.executable,
            "-c",
            "import sys; print('docker-bridge-ok'); print('evidence', file=sys.stderr)",
        ]
        with patch.object(connector, "command_argv", return_value=real_command):
            result = connector.execute(session, envelope, CancellationToken())
        self.assertEqual(result.termination, ProcessTermination.EXITED)
        self.assertEqual(result.return_code, 0)
        self.assertEqual(result.backend_id, connector.backend_id)
        self.assertIn(b"docker-bridge-ok", result.output.stdout)
        self.assertIn(b"evidence", result.output.stderr)
        self.assertEqual(result.metadata["connector"], "docker-cli")

    def test_container_pull_replaces_stale_managed_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_root = root / "data"
            workspace = data_root / "task-1"
            sync_root = root / "sync"
            workspace.mkdir(parents=True)
            sync_root.mkdir()
            (workspace / "stale.txt").write_text("stale", encoding="utf-8")
            binding = {
                "container": "task-main-1",
                "container_ref_digest": "digest",
                "workdir": "/app",
                "docker_executable": "docker-test",
                "workspace_data_root": data_root,
                "sync_root": sync_root,
            }

            def fake_docker(_binding, argv, **_kwargs):
                destination = Path(argv[2])
                destination.joinpath(".git").mkdir()
                destination.joinpath(".git", "HEAD").write_text(
                    "ref: refs/heads/master\n", encoding="utf-8"
                )
                destination.joinpath("site.txt").write_text("live", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, b"", b"")

            with patch.object(
                code_worker_adapter,
                "_run_benchmark_docker",
                side_effect=fake_docker,
            ):
                code_worker_adapter._pull_benchmark_workspace(binding, workspace)
            self.assertFalse((workspace / "stale.txt").exists())
            self.assertEqual((workspace / "site.txt").read_text(encoding="utf-8"), "live")
            self.assertTrue((workspace / ".git" / "HEAD").is_file())

    def test_host_file_delta_is_pushed_with_structured_docker_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_root = root / "data"
            workspace = data_root / "task-1"
            sync_root = root / "sync"
            workspace.mkdir(parents=True)
            sync_root.mkdir()
            (workspace / "changed.txt").write_text("new", encoding="utf-8")
            (workspace / "created.txt").write_text("created", encoding="utf-8")
            before = {
                "changed.txt": {"sha256": "old"},
                "deleted.txt": {"sha256": "gone"},
            }
            after = code_worker_adapter._workspace_manifest(workspace)
            binding = {
                "container": "task-main-1",
                "container_ref_digest": "digest",
                "workdir": "/app",
                "docker_executable": "docker-test",
                "workspace_data_root": data_root,
                "sync_root": sync_root,
            }
            calls: list[tuple[str, ...]] = []

            def record_docker(_binding, argv, **_kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, b"", b"")

            with patch.object(
                code_worker_adapter,
                "_run_benchmark_docker",
                side_effect=record_docker,
            ):
                report = code_worker_adapter._push_benchmark_workspace_delta(
                    binding,
                    workspace,
                    before=before,
                    after=after,
                )
            self.assertEqual(report["written_count"], 2)
            self.assertEqual(report["deleted_count"], 1)
            self.assertIn(
                ("exec", "task-main-1", "rm", "-f", "--", "/app/deleted.txt"),
                calls,
            )
            copied_destinations = {
                call[-1] for call in calls if call and call[0] == "cp"
            }
            self.assertEqual(
                copied_destinations,
                {
                    "task-main-1:/app/changed.txt",
                    "task-main-1:/app/created.txt",
                },
            )

    def test_benchmark_shell_is_ordered_between_mirror_push_and_pull(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_root = root / "data"
            workspace = data_root / "task-1"
            sync_root = root / "sync"
            workspace.mkdir(parents=True)
            sync_root.mkdir()
            (workspace / "tracked.txt").write_text("live", encoding="utf-8")
            binding = {
                "container": "task-main-1",
                "container_ref_digest": "digest",
                "workdir": "/app",
                "docker_executable": "docker-test",
                "workspace_data_root": data_root,
                "sync_root": sync_root,
            }
            calls: list[str] = []

            def fake_push(*_args, **_kwargs):
                calls.append("push")
                return {
                    "written_count": 0,
                    "deleted_count": 0,
                    "written_path_digests": [],
                    "deleted_path_digests": [],
                }

            def fake_pull(*_args, **_kwargs):
                calls.append("pull")

            mirror = code_worker_adapter._BenchmarkWorkspaceMirror(
                binding,
                workspace,
                synced_manifest=code_worker_adapter._workspace_manifest(workspace),
            )
            connector = code_worker_adapter._BenchmarkDockerCliSandboxConnector(
                container="task-main-1",
                workdir="/app",
                docker_executable="docker-test",
                benchmark_mirror=mirror,
            )
            envelope = GatewayCommandEnvelope.build(
                session_id="session-1",
                run_id="run-1",
                task_id="task-1",
                worker_id="worker-1",
                executable="git",
                argv=("status", "--short"),
            )
            session = BackendSession(
                session_id="session-1",
                backend_id=connector.backend_id,
                execution_root=root,
                generation=1,
                prepared_at=0.0,
            )
            expected = object()

            def fake_execute(*_args, **_kwargs):
                calls.append("execute")
                return expected

            with (
                patch.object(
                    code_worker_adapter,
                    "_push_benchmark_workspace_delta",
                    side_effect=fake_push,
                ),
                patch.object(
                    code_worker_adapter,
                    "_pull_benchmark_workspace",
                    side_effect=fake_pull,
                ),
                patch.object(
                    DockerCliSandboxConnector,
                    "execute",
                    side_effect=fake_execute,
                ),
            ):
                result = connector.execute(session, envelope, CancellationToken())

            self.assertIs(result, expected)
            self.assertEqual(calls, ["push", "execute", "pull"])
            self.assertEqual(mirror.report()["ordering"], "live-serialized")

    def test_benchmark_file_port_pulls_before_read_and_pushes_after_apply(self) -> None:
        calls: list[str] = []

        class Mirror:
            def __init__(self) -> None:
                import threading

                self.guard = threading.RLock()

            def pull_from_container(self) -> None:
                calls.append("pull")

            def push_to_container(self) -> None:
                calls.append("push")

        port = object.__new__(code_worker_adapter._BenchmarkWorkspaceEditPort)
        port._benchmark_mirror = Mirror()
        read_result = object()
        apply_result = object()

        with patch.object(
            code_worker_adapter.WorkspaceEditPort,
            "read_bytes",
            side_effect=lambda *_args, **_kwargs: calls.append("read") or read_result,
        ):
            self.assertIs(port.read_bytes("tracked.txt"), read_result)
        with patch.object(
            code_worker_adapter.WorkspaceEditPort,
            "apply",
            side_effect=lambda *_args, **_kwargs: calls.append("apply") or apply_result,
        ):
            self.assertIs(port.apply(()), apply_result)

        self.assertEqual(calls, ["pull", "read", "apply", "push"])

    def test_benchmark_permission_rule_is_exactly_session_and_workspace_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve() / "managed-workspace"
            workspace.mkdir()
            policy = code_worker_adapter._benchmark_permission_policy(
                session_id="physical:task-1:layer:1:worker-1",
                workspace_root=workspace,
                container_ref_digest="sha256:container-ref",
            )

        self.assertEqual(policy["mode"], "acceptEdits")
        self.assertFalse(policy["interactive"])
        self.assertTrue(policy["headless"])
        self.assertFalse(policy["python_policy_fallback"])
        self.assertEqual(len(policy["rules"]), 1)
        rule = policy["rules"][0]
        self.assertEqual(rule["effect"], "allow")
        self.assertEqual(rule["source"], "managed")
        self.assertEqual(rule["tool_pattern"], "shell")
        self.assertEqual(rule["namespace_pattern"], "builtin")
        self.assertEqual(rule["operation_pattern"], "execute")
        self.assertEqual(
            rule["session_pattern"],
            "physical:task-1:layer:1:worker-1",
        )
        self.assertEqual(rule["workspace_pattern"], str(workspace))
        self.assertTrue(
            rule["metadata"]["gateway_hard_denies_remain_authoritative"]
        )


if __name__ == "__main__":
    unittest.main()
