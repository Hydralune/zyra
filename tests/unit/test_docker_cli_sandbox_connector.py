from __future__ import annotations

import subprocess
import shutil
import sys
import tarfile
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
from zyra_runtime.sandbox_gateway.models import ProcessOutput, ProcessResult  # noqa: E402
from zyra_orchestration.deployment import code_worker_adapter  # noqa: E402
from zyra_workers.typescript_claude_runtime import (  # noqa: E402
    _typescript_runtime_timeout_seconds,
)


class DockerCliSandboxConnectorTests(unittest.TestCase):
    def test_git_workspace_state_detects_changes_to_an_already_dirty_file(self) -> None:
        git = shutil.which("git")
        if git is None:
            self.skipTest("git is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            target = workspace / "tracked.txt"
            target.write_text("base", encoding="utf-8")
            commands = (
                ("init",),
                ("add", "tracked.txt"),
                ("-c", "user.name=Zyra Test", "-c", "user.email=test@zyra.local", "commit", "-m", "base"),
            )
            for arguments in commands:
                completed = subprocess.run(
                    [git, *arguments],
                    cwd=workspace,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
            target.write_text("first dirty value", encoding="utf-8")
            before = code_worker_adapter._workspace_state_snapshot(workspace)
            target.write_text("second dirty value", encoding="utf-8")
            after = code_worker_adapter._workspace_state_snapshot(workspace)

            self.assertEqual(before["mode"], "git-head-diff")
            self.assertNotEqual(before["digest"], after["digest"])

    def test_git_workspace_state_detects_ignored_delivery_but_not_dependency_cache(
        self,
    ) -> None:
        git = shutil.which("git")
        if git is None:
            self.skipTest("git is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            workspace.joinpath(".gitignore").write_text(
                "submission/\nnode_modules/\n",
                encoding="utf-8",
            )
            for arguments in (
                ("init",),
                ("add", ".gitignore"),
                (
                    "-c",
                    "user.name=Zyra Test",
                    "-c",
                    "user.email=test@zyra.local",
                    "commit",
                    "-m",
                    "base",
                ),
            ):
                completed = subprocess.run(
                    [git, *arguments],
                    cwd=workspace,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
            delivery = workspace / "submission" / "manifest.json"
            delivery.parent.mkdir()
            delivery.write_text('{"status":"draft"}\n', encoding="utf-8")
            dependency = workspace / "node_modules" / "package" / "cache.bin"
            dependency.parent.mkdir(parents=True)
            dependency.write_bytes(b"before")

            before = code_worker_adapter._workspace_state_snapshot(workspace)
            delivery.write_text('{"status":"ready"}\n', encoding="utf-8")
            after_delivery = code_worker_adapter._workspace_state_snapshot(workspace)
            dependency.write_bytes(b"after")
            after_cache = code_worker_adapter._workspace_state_snapshot(workspace)

            self.assertEqual(before["mode"], "git-head-diff")
            self.assertEqual(before["ignored_count"], 1)
            self.assertNotEqual(before["digest"], after_delivery["digest"])
            self.assertEqual(after_delivery["digest"], after_cache["digest"])

    def test_delivery_driving_benchmark_shell_records_real_host_workspace_change(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_root = root / "data"
            workspace = data_root / "task-1"
            sync_root = root / "sync"
            workspace.mkdir(parents=True)
            sync_root.mkdir()
            target = workspace / "tracked.txt"
            target.write_text("before", encoding="utf-8")
            binding = {
                "container": "task-main-1",
                "container_ref_digest": "digest",
                "workdir": "/app",
                "docker_executable": "docker-test",
                "workspace_data_root": data_root,
                "sync_root": sync_root,
                "canonical_host_workspace": workspace,
            }
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
                executable="python",
                argv=("-c", "write"),
                metadata={"progressive_delivery_driving_shell": True},
            )
            session = BackendSession(
                session_id="session-1",
                backend_id=connector.backend_id,
                execution_root=root,
                generation=1,
                prepared_at=0.0,
            )

            def fake_execute(*_args, **_kwargs):
                target.write_text("after", encoding="utf-8")
                return ProcessResult(
                    command_id=envelope.command_id,
                    termination=ProcessTermination.EXITED,
                    return_code=0,
                    started_at=1.0,
                    finished_at=2.0,
                    output=ProcessOutput(stdout=b"changed"),
                    backend_id=connector.backend_id,
                    metadata={"connector": "docker-cli"},
                )

            with (
                patch.object(
                    code_worker_adapter,
                    "_push_benchmark_workspace_delta",
                    return_value={
                        "written_count": 0,
                        "deleted_count": 0,
                        "written_path_digests": [],
                        "deleted_path_digests": [],
                    },
                ),
                patch.object(
                    DockerCliSandboxConnector,
                    "execute",
                    side_effect=fake_execute,
                ),
            ):
                result = connector.execute(session, envelope, CancellationToken())

            self.assertTrue(result.metadata["workspace_mutation_committed"])
            self.assertEqual(result.metadata["workspace_state_mode"], "bounded-manifest")
            self.assertNotEqual(
                result.metadata["workspace_state_before_digest"],
                result.metadata["workspace_state_after_digest"],
            )

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

    def test_benchmark_network_grant_is_scoped_to_container_loopback(self) -> None:
        services = code_worker_adapter._benchmark_network_gateway_services()

        self.assertEqual(
            services,
            {
                "sandbox_gateway_allow_public_http": True,
                "sandbox_gateway_allow_loopback_network": True,
                "sandbox_gateway_default_command_network_profile": "public",
            },
        )
        self.assertNotIn("sandbox_gateway_allow_private_network", services)

    def test_physical_worker_preserves_a_full_agent_work_phase(self) -> None:
        self.assertEqual(
            code_worker_adapter._code_worker_query_context_budget_chars({}),
            400_000,
        )
        self.assertEqual(
            code_worker_adapter._code_worker_query_context_budget_chars(
                {"query_context_budget_chars": 720_000}
            ),
            720_000,
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

    def test_long_horizon_provider_retry_bounds_are_configurable(self) -> None:
        with patch.dict(
            code_worker_adapter.os.environ,
            {
                "ZYRA_MODEL_API_TIMEOUT_SECONDS": "210",
                "ZYRA_MODEL_STREAM_TOTAL_TIMEOUT_SECONDS": "240",
                "ZYRA_API_RETRY_MAX_ATTEMPTS": "2",
                "ZYRA_MAX_LENGTH_CONTINUATIONS": "2",
            },
            clear=False,
        ):
            constraints = code_worker_adapter._benchmark_runtime_constraints(
                {"benchmark_long_horizon": True}
            )

        self.assertEqual(constraints["model_api_timeout_seconds"], 210.0)
        self.assertEqual(constraints["model_api_timeout_milliseconds"], 210_000)
        self.assertEqual(constraints["model_stream_total_timeout_seconds"], 240.0)
        self.assertEqual(
            constraints["model_stream_total_timeout_milliseconds"],
            240_000,
        )
        self.assertEqual(constraints["api_retry_max_attempts"], 2)
        self.assertEqual(constraints["max_length_continuations"], 2)

    def test_benchmark_command_budget_preserves_agent_closeout_time(self) -> None:
        self.assertEqual(
            code_worker_adapter._benchmark_command_timeout_budget({}),
            (300.0, 3_600.0),
        )
        self.assertEqual(
            code_worker_adapter._benchmark_command_timeout_budget(
                {"reasoning_timeout_seconds": 840}
            ),
            (300.0, 672.0),
        )
        self.assertEqual(
            code_worker_adapter._benchmark_command_timeout_budget(
                {"reasoning_timeout_seconds": 30}
            ),
            (15.0, 15.0),
        )

    def test_benchmark_deadline_propagates_active_closeout_budget(self) -> None:
        context = {
            "reasoning_timeout_seconds": 3_480,
            "external_deadline_epoch_ms": 9_999_999_999_999,
            "benchmark_closeout_reserve_seconds": 660,
            "benchmark_agent_closeout_reserve_seconds": 600,
        }
        constraints = code_worker_adapter._benchmark_runtime_constraints(context)
        self.assertEqual(constraints["external_deadline_epoch_ms"], 9_999_999_999_999)
        self.assertEqual(constraints["benchmark_closeout_reserve_seconds"], 660.0)
        self.assertEqual(
            code_worker_adapter._benchmark_command_timeout_budget(context),
            (300.0, 2_880.0),
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
            connector.raw_command_argv(envelope),
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
        managed = connector.command_argv(envelope)
        self.assertEqual(managed[:8], [
            "docker-test",
            "exec",
            "--workdir",
            "/app/repo/src",
            "--env",
            "NO_COLOR=1",
            "task-main-1",
            "sh",
        ])
        self.assertEqual(managed[-3:], ["git", "status", "--short"])
        self.assertIn("setsid", managed[9])

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

    def test_deadline_race_accepts_a_naturally_reaped_docker_command(self) -> None:
        connector = DockerCliSandboxConnector(
            container="task-main-1",
            workdir="/app",
            docker_executable="docker-test",
        )

        class ReapedProcess:
            @staticmethod
            def poll() -> int:
                return 0

        self.assertTrue(
            connector._settled_during_deadline_race(
                ReapedProcess(),  # type: ignore[arg-type]
                tree_result={
                    "stopped": True,
                    "graceful_requested": False,
                    "forced": False,
                    "return_code": 0,
                },
                container_termination={
                    "stopped": False,
                    "pid_observed": False,
                    "read_error": "",
                },
            )
        )

    def test_deadline_race_keeps_unverified_terminated_command_fail_closed(self) -> None:
        connector = DockerCliSandboxConnector(
            container="task-main-1",
            workdir="/app",
            docker_executable="docker-test",
        )

        class TerminatedProcess:
            @staticmethod
            def poll() -> int:
                return -15

        self.assertFalse(
            connector._settled_during_deadline_race(
                TerminatedProcess(),  # type: ignore[arg-type]
                tree_result={
                    "stopped": True,
                    "graceful_requested": True,
                    "forced": False,
                    "return_code": -15,
                },
                container_termination={
                    "stopped": False,
                    "pid_observed": False,
                    "read_error": "",
                },
            )
        )

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

            def fake_archive(_binding, archive_path, **_kwargs):
                source = root / "archive-source"
                source.joinpath(".git").mkdir(parents=True)
                source.joinpath(".git", "HEAD").write_text(
                    "ref: refs/heads/master\n", encoding="utf-8"
                )
                source.joinpath("site.txt").write_text("live", encoding="utf-8")
                with tarfile.open(archive_path, "w") as archive:
                    archive.add(source, arcname=".")

            with patch.object(
                code_worker_adapter,
                "_run_benchmark_docker_archive",
                side_effect=fake_archive,
            ):
                code_worker_adapter._pull_benchmark_workspace(binding, workspace)
            self.assertFalse((workspace / "stale.txt").exists())
            self.assertEqual((workspace / "site.txt").read_text(encoding="utf-8"), "live")
            self.assertFalse((workspace / ".git").exists())

    def test_container_pull_streams_a_dereferenced_tar_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "workspace.tar"
            binding = {
                "container": "task-main-1",
                "workdir": "/app",
                "docker_executable": "docker-test",
            }
            completed = subprocess.CompletedProcess([], 0, None, b"")
            with patch(
                "zyra_orchestration.deployment.code_worker_adapter.subprocess.run",
                return_value=completed,
            ) as run:
                code_worker_adapter._run_benchmark_docker_archive(
                    binding,
                    archive_path,
                    timeout_seconds=300.0,
                )
        self.assertEqual(
            run.call_args.args[0],
            [
                "docker-test",
                "exec",
                "task-main-1",
                "tar",
                "-chf",
                "-",
                "-C",
                "/app",
                "--exclude=./.runtime/docker-config",
                "--exclude=./.runtime/cache",
                "--exclude=./.runtime/temp",
                "--exclude=./.runtime/tmp",
                "--exclude=./.runtime/venv",
                "--exclude=./.git",
                "--exclude=./.cache",
                "--exclude=*/.cache",
                "--exclude=./.mypy_cache",
                "--exclude=*/.mypy_cache",
                "--exclude=./.nox",
                "--exclude=*/.nox",
                "--exclude=./.pytest_cache",
                "--exclude=*/.pytest_cache",
                "--exclude=./.ruff_cache",
                "--exclude=*/.ruff_cache",
                "--exclude=./.tox",
                "--exclude=*/.tox",
                "--exclude=./.venv",
                "--exclude=*/.venv",
                "--exclude=./__pycache__",
                "--exclude=*/__pycache__",
                "--exclude=./node_modules",
                "--exclude=*/node_modules",
                "--exclude=./venv",
                "--exclude=*/venv",
                "--exclude=./*.egg-info",
                "--exclude=*/*.egg-info",
                ".",
            ],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 300.0)

    def test_container_pull_omits_runtime_dependencies_from_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_root = root / "data"
            workspace = data_root / "task-1"
            sync_root = root / "sync"
            workspace.mkdir(parents=True)
            sync_root.mkdir()
            binding = {
                "container": "task-main-1",
                "container_ref_digest": "digest",
                "workdir": "/app",
                "docker_executable": "docker-test",
                "workspace_data_root": data_root,
                "sync_root": sync_root,
            }

            def fake_archive(_binding, archive_path, **_kwargs):
                source = root / "archive-source"
                source.joinpath(".runtime", "venv").mkdir(parents=True)
                source.joinpath(".runtime", "venv", "large.bin").write_bytes(b"x")
                source.joinpath("node_modules", "package").mkdir(parents=True)
                source.joinpath("node_modules", "package", "large.bin").write_bytes(b"x")
                source.joinpath(".git").mkdir()
                source.joinpath(".git", "index").write_bytes(b"git")
                source.joinpath(".runtime", "simulation-result.json").write_text(
                    "{}", encoding="utf-8"
                )
                with tarfile.open(archive_path, "w") as archive:
                    archive.add(source, arcname=".")

            with patch.object(
                code_worker_adapter,
                "_run_benchmark_docker_archive",
                side_effect=fake_archive,
            ):
                code_worker_adapter._pull_benchmark_workspace(binding, workspace)

            self.assertFalse(workspace.joinpath(".runtime", "venv").exists())
            self.assertFalse(workspace.joinpath("node_modules").exists())
            self.assertFalse(workspace.joinpath(".git").exists())
            self.assertTrue(
                workspace.joinpath(
                    ".runtime", "simulation-result.json"
                ).is_file()
            )

    def test_targeted_pull_copies_only_requested_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_root = root / "data"
            workspace = data_root / "task-1"
            sync_root = root / "sync"
            workspace.mkdir(parents=True)
            sync_root.mkdir()
            binding = {
                "container": "task-main-1",
                "container_ref_digest": "digest",
                "workdir": "/app",
                "docker_executable": "docker-test",
                "workspace_data_root": data_root,
                "sync_root": sync_root,
            }
            calls: list[tuple[str, ...]] = []

            def fake_docker(_binding, argv, **_kwargs):
                calls.append(argv)
                if argv[0] == "cp":
                    Path(argv[-1]).write_text("current", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, b"", b"")

            with patch.object(
                code_worker_adapter,
                "_run_benchmark_docker",
                side_effect=fake_docker,
            ):
                code_worker_adapter._pull_benchmark_workspace_paths(
                    binding,
                    workspace,
                    ("services/api.py",),
                )

            self.assertEqual(
                workspace.joinpath("services", "api.py").read_text(encoding="utf-8"),
                "current",
            )
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1][0], "cp")
            self.assertEqual(calls[1][1], "task-main-1:/app/services/api.py")

    def test_targeted_pull_preserves_identity_when_container_bytes_are_unchanged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_root = root / "data"
            workspace = data_root / "task-1"
            sync_root = root / "sync"
            target = workspace / "services" / "api.py"
            target.parent.mkdir(parents=True)
            sync_root.mkdir()
            target.write_text("unchanged", encoding="utf-8")
            before = target.stat()
            binding = {
                "container": "task-main-1",
                "container_ref_digest": "digest",
                "workdir": "/app",
                "docker_executable": "docker-test",
                "workspace_data_root": data_root,
                "sync_root": sync_root,
            }

            def fake_docker(_binding, argv, **_kwargs):
                if argv[0] == "cp":
                    Path(argv[-1]).write_text("unchanged", encoding="utf-8")
                return subprocess.CompletedProcess(argv, 0, b"", b"")

            with patch.object(
                code_worker_adapter,
                "_run_benchmark_docker",
                side_effect=fake_docker,
            ):
                code_worker_adapter._pull_benchmark_workspace_paths(
                    binding,
                    workspace,
                    ("services/api.py",),
                )

            after = target.stat()
            self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")
            self.assertEqual(after.st_ino, before.st_ino)
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

    def test_container_pull_rejects_an_unresolved_symlink_before_replacing_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_root = root / "data"
            workspace = data_root / "task-1"
            sync_root = root / "sync"
            workspace.mkdir(parents=True)
            sync_root.mkdir()
            workspace.joinpath("preserved.txt").write_text("safe", encoding="utf-8")
            binding = {
                "container": "task-main-1",
                "container_ref_digest": "digest",
                "workdir": "/app",
                "docker_executable": "docker-test",
                "workspace_data_root": data_root,
                "sync_root": sync_root,
            }

            def fake_archive(_binding, archive_path, **_kwargs):
                with tarfile.open(archive_path, "w") as archive:
                    link = tarfile.TarInfo("venv/bin/python")
                    link.type = tarfile.SYMTYPE
                    link.linkname = "python3"
                    archive.addfile(link)

            with patch.object(
                code_worker_adapter,
                "_run_benchmark_docker_archive",
                side_effect=fake_archive,
            ):
                with self.assertRaisesRegex(RuntimeError, "unsafe or unresolved"):
                    code_worker_adapter._pull_benchmark_workspace(binding, workspace)
            self.assertEqual(
                workspace.joinpath("preserved.txt").read_text(encoding="utf-8"),
                "safe",
            )

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

    def test_benchmark_shell_pushes_before_execution_and_defers_pull(self) -> None:
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
            self.assertEqual(calls, ["push", "execute"])
            self.assertEqual(mirror.report()["ordering"], "live-serialized")

    def test_benchmark_file_port_pulls_before_read_and_pushes_after_apply(self) -> None:
        calls: list[str] = []

        class Mirror:
            def __init__(self) -> None:
                import threading

                self.guard = threading.RLock()

            def pull_paths_from_container(self, paths) -> None:
                calls.append(f"pull:{','.join(paths)}")

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
            mutation = type("Mutation", (), {"logical_path": "tracked.txt"})()
            self.assertIs(port.apply((mutation,)), apply_result)

        self.assertEqual(
            calls,
            [
                "pull:tracked.txt",
                "read",
                "pull:tracked.txt",
                "apply",
                "push",
            ],
        )

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
