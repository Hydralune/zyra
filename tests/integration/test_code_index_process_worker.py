from __future__ import annotations

import tempfile
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

from zyra_code_index import (
    CodeIndexConsumer,
    CodeIndexIntegrationRuntime,
    CodeIndexQuery,
    CodeIndexRuntime,
    CodeIndexRuntimeRegistry,
    CodeIndexWorkerProcessSupervisor,
)
from zyra_workspace import WorkspaceManagerConfig, WorkspaceManagerRuntime


class CodeIndexProcessWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = WorkspaceManagerConfig(
            state_root=self.root / "workspace-state",
            data_root=self.root / "workspace-data",
            lease_ttl_seconds=300,
        )
        self.manager = WorkspaceManagerRuntime(self.config)
        created = self.manager.create_for_task(
            run_id="run-process",
            task_id="task-process",
            session_id="session-process",
            worker_id="CodeWorkerRuntime",
            idempotency_key="create-process-workspace",
        )
        self.access = created.access
        workspace = self.manager.internal_task_root(self.access)
        (workspace / "src").mkdir()
        (workspace / "tests").mkdir()
        (workspace / "src" / "process.py").write_text(
            "def durable_publish():\n    return 'worker process fenced publication'\n",
            encoding="utf-8",
        )
        (workspace / "tests" / "test_process.py").write_text(
            "from src.process import durable_publish\n\ndef test_process():\n    assert durable_publish()\n",
            encoding="utf-8",
        )
        self.runtime = CodeIndexRuntime.from_workspace_manager(
            self.manager,
            self.access,
            index_path=self.root / "code-index.sqlite3",
        )
        self.supervisor = CodeIndexWorkerProcessSupervisor(
            identity=self.runtime.source.identity,
            index_db=self.runtime.store.path,
            workspace_state_root=self.config.state_root,
            workspace_data_root=self.config.data_root,
            project_root=Path(__file__).resolve().parents[2],
            lease_ttl_seconds=2.0,
            heartbeat_interval_seconds=0.2,
            timeout_seconds=30.0,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_productized_process_publishes_and_restart_queries_current_revision(self) -> None:
        integration = CodeIndexIntegrationRuntime(
            self.runtime,
            process_worker=lambda workspace_id, maximum_jobs: self.supervisor.drain(
                maximum_jobs=maximum_jobs
            ).outcomes,
        )
        admission = integration.admit_initial(process=True)
        self.assertTrue(admission.published)
        self.assertEqual(admission.worker_outcomes[0].status, "ready")

        status = self.supervisor.status()
        self.assertTrue(status.ok)
        self.assertGreater(status.pid, 0)
        self.assertFalse(status.to_dict()["external_source_process"])
        self.assertFalse(status.to_dict()["physical_workspace_root_persisted"])

        restarted_runtime = CodeIndexRuntime.from_workspace_manager(
            self.manager,
            self.access,
            index_path=self.runtime.store.path,
        )
        restarted = CodeIndexIntegrationRuntime(
            restarted_runtime,
            process_worker=lambda workspace_id, maximum_jobs: self.supervisor.drain(
                maximum_jobs=maximum_jobs
            ).outcomes,
        )
        selection = restarted.select(
            CodeIndexQuery(
                task_id="task-process",
                text="durable fenced publication",
                consumer=CodeIndexConsumer.CODE_WORKER_CONTEXT,
                worker_request_id="after-process-restart",
            )
        )
        self.assertIn("src/process.py", selection.selected_files)
        self.assertIn("tests/test_process.py", selection.selected_tests)

    def test_registry_reuses_worker_access_without_rotating_workspace_owner(self) -> None:
        before = self.manager.project(self.access.workspace_id)
        public_before = self.access.to_public_dict()
        registry = CodeIndexRuntimeRegistry(
            self.manager,
            index_root=self.root / "registry-index",
            worker_id="must-not-take-custody",
        )
        runtime = registry.runtime_for_access(self.access)
        after = self.manager.project(self.access.workspace_id)
        public_after = self.access.to_public_dict()
        self.assertEqual(before.owner_epoch, after.owner_epoch)
        self.assertEqual(before.binding_revision, after.binding_revision)
        self.assertEqual(before.lease_id, after.lease_id)
        self.assertEqual(public_before, public_after)
        self.assertEqual(
            runtime.source.identity.revision,
            self.runtime.source.identity.revision,
        )
        self.assertEqual(
            self.manager.internal_task_root(self.access),
            Path(runtime.source.identity.root),
        )

    def test_killed_builder_is_swept_and_republished_by_a_new_process(self) -> None:
        supervisor = CodeIndexWorkerProcessSupervisor(
            identity=self.runtime.source.identity,
            index_db=self.runtime.store.path,
            workspace_state_root=self.config.state_root,
            workspace_data_root=self.config.data_root,
            project_root=Path(__file__).resolve().parents[2],
            lease_ttl_seconds=0.3,
            heartbeat_interval_seconds=0.05,
            timeout_seconds=30.0,
        )
        integration = CodeIndexIntegrationRuntime(
            self.runtime,
            process_worker=lambda workspace_id, maximum_jobs: supervisor.drain(
                maximum_jobs=maximum_jobs
            ).outcomes,
        )
        admitted = integration.admit_initial(process=False)
        phase_file = self.root / "worker-phase.json"
        release_file = self.root / "never-release"
        command = [
            sys.executable,
            "-m",
            "zyra_code_index.worker_cli",
            "--index-db",
            str(self.runtime.store.path),
            "--workspace-state-root",
            str(self.config.state_root),
            "--workspace-data-root",
            str(self.config.data_root),
            "--workspace-id",
            self.runtime.source.identity.workspace_id,
            "--source-revision",
            self.runtime.source.identity.revision,
            "--backend-id",
            self.runtime.source.identity.backend_id,
            "--worker-id",
            "doomed-code-index-worker",
            "--lease-ttl",
            "0.3",
            "--heartbeat-interval",
            "0.05",
            "--once",
            "--phase-file",
            str(phase_file),
            "--pause-phase",
            "building",
            "--release-file",
            str(release_file),
            "--pause-timeout",
            "30",
        ]
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[2],
            env=supervisor._environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        deadline = time.monotonic() + 20.0
        observed = {}
        while time.monotonic() < deadline:
            if phase_file.exists():
                observed = json.loads(phase_file.read_text(encoding="utf-8"))
                if observed.get("phase") == "building":
                    break
            time.sleep(0.025)
        if observed.get("phase") != "building":
            if process.poll() is None:
                process.kill()
            stdout, stderr = process.communicate(timeout=10.0)
            self.fail(
                "worker did not reach building phase; "
                f"observed={observed!r}; stdout={stdout[-1000:]!r}; stderr={stderr[-2000:]!r}"
            )
        process.kill()
        process.communicate(timeout=10.0)
        self.assertNotEqual(process.returncode, 0)

        time.sleep(0.4)
        sweep = supervisor.sweep_once()
        self.assertEqual(sweep.sweep["count"], 1)
        self.assertIn(admitted.job.job_id, sweep.sweep["stale_job_ids"])
        recovered = supervisor.drain(maximum_jobs=4)
        self.assertTrue(any(outcome["status"] == "ready" for outcome in recovered.outcomes))
        selection = integration.select(
            CodeIndexQuery(
                task_id="task-process",
                text="fenced publication",
                consumer=CodeIndexConsumer.RECOVERY,
                request_id="after-kill-recovery",
            )
        )
        self.assertIn("src/process.py", selection.selected_files)


if __name__ == "__main__":
    unittest.main()
