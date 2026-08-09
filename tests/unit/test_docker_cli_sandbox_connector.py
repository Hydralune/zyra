from __future__ import annotations

import subprocess
import sys
import tempfile
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
    GatewayCommandEnvelope,
    GatewaySessionRecord,
    ProcessTermination,
)
from zyra_orchestration.deployment import code_worker_adapter  # noqa: E402


class DockerCliSandboxConnectorTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
