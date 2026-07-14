from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
for package in (ROOT / "packages" / "workspace", ROOT / "packages" / "runtime", ROOT / "packages" / "core"):
    if str(package) not in sys.path:
        sys.path.insert(0, str(package))

from zyra_runtime import LocalArtifactStore  # noqa: E402
from zyra_workspace import (  # noqa: E402
    IntegrationOperation,
    IsolationState,
    MutationKind,
    WorkspaceEditPort,
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceHandoffRuntime,
    WorkspaceIsolationRuntime,
    WorkspaceKind,
    WorkspaceLifecycleState,
    WorkspaceManagerConfig,
    WorkspaceManagerRuntime,
    WorkspaceMutation,
    WorkspaceRebindRuntime,
)


class WorkspaceManagerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = WorkspaceManagerRuntime(
            WorkspaceManagerConfig(
                state_root=self.root / "state",
                data_root=self.root / "data",
                lease_ttl_seconds=300,
            )
        )
        self.created = self.manager.create_for_task(
            run_id="run-integration",
            task_id="task-integration",
            session_id="session-integration",
            worker_id="CodeWorkerRuntime",
            idempotency_key="create-integration",
        )
        self.artifacts = LocalArtifactStore(self.root / "artifacts")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def port(self, access=None, *, worker_id: str = "CodeWorkerRuntime") -> WorkspaceEditPort:
        return WorkspaceEditPort(
            self.manager,
            access or self.created.access,
            worker_id=worker_id,
            run_id="run-integration",
            task_id="task-integration",
            node_id="node-integration",
            artifact_store=self.artifacts,
        )

    def test_patch_transaction_publishes_artifact_rotates_epoch_and_restores_snapshot(self) -> None:
        port = self.port()
        old_access = port.current_access()
        write = port.write_text(
            "src/main.py",
            "print('one')\n",
            publish_artifact=True,
            idempotency_key="write-main-one",
        )
        self.assertTrue(write.ok)
        self.assertEqual(write.transaction.phase.value, "committed")
        self.assertEqual(write.transaction.path_results[0].disposition, "created")
        self.assertEqual(len(write.publications), 1)
        self.assertTrue(write.publications[0].artifact_uri.startswith("artifact://"))
        self.assertGreater(write.access.owner_epoch, old_access.owner_epoch)
        committed_epoch = write.access.owner_epoch
        replay = port.write_text(
            "src/main.py",
            "print('one')\n",
            publish_artifact=True,
            idempotency_key="write-main-one",
        )
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(replay.transaction.transaction_id, write.transaction.transaction_id)
        self.assertEqual(replay.access.owner_epoch, committed_epoch)
        with self.assertRaises(WorkspaceError) as stale:
            self.manager.backend.read(old_access, mount_kind=WorkspaceKind.TASK, path="src/main.py")
        self.assertEqual(stale.exception.code, WorkspaceErrorCode.OWNER_EPOCH_STALE)

        edit = port.edit_text(
            "src/main.py",
            old="one",
            new="two",
            idempotency_key="edit-main-two",
        )
        self.assertTrue(edit.ok)
        self.assertEqual(port.read_text("src/main.py").text(), "print('two')\n")
        self.assertTrue(
            any(item.path == "src/main.py" for item in self.manager.ownership_store.list(self.created.projection.workspace_id))
        )

        before_restore = self.manager.snapshot(self.created.projection.workspace_id)
        port.adopt_access(
            self.manager.acquire_for_worker(
                task_id="task-integration",
                session_id="session-integration",
                worker_id="CodeWorkerRuntime",
            )
        )
        port.write_text("src/main.py", "corrupt\n", idempotency_key="corrupt-main")
        self.manager.restore(self.created.projection.workspace_id, before_restore.snapshot_id)
        restored_access = self.manager.acquire_for_worker(
            task_id="task-integration",
            session_id="session-integration",
            worker_id="CodeWorkerRuntime",
        )
        restored_port = self.port(restored_access)
        self.assertEqual(restored_port.read_text("src/main.py").text(), "print('two')\n")
        public = json.dumps(write.to_public_dict(), sort_keys=True)
        self.assertNotIn(str(self.root), public)
        self.assertNotIn("relative_root", public)

    def test_multi_path_preflight_and_injected_second_write_roll_back_without_partial_tree(self) -> None:
        port = self.port()
        first = port.read_bytes("a.txt")
        second = port.read_bytes("b.txt")
        mutations = (
            WorkspaceMutation(
                mutation_id="mutation-a",
                kind=MutationKind.WRITE_BYTES,
                logical_path="a.txt",
                content=b"alpha",
                read_evidence_id=first.evidence.evidence_id,
                expected_absent=True,
            ),
            WorkspaceMutation(
                mutation_id="mutation-b",
                kind=MutationKind.WRITE_BYTES,
                logical_path="b.txt",
                content=b"beta",
                read_evidence_id=second.evidence.evidence_id,
                expected_absent=True,
            ),
        )
        original = self.manager.backend.write_after_read
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected second write failure")
            return original(*args, **kwargs)

        with mock.patch.object(self.manager.backend, "write_after_read", side_effect=fail_second):
            with self.assertRaises(WorkspaceError):
                port.apply(
                    mutations,
                    evidence=(first.evidence, second.evidence),
                    idempotency_key="two-file-rollback",
                )
        binding = self.manager.store.require_binding(self.created.projection.workspace_id)
        access = self.manager.acquire_for_worker(
            task_id=binding.task_id,
            session_id=binding.session_id,
            worker_id="CodeWorkerRuntime",
        )
        verify = self.port(access)
        self.assertFalse(verify.read_bytes("a.txt").exists)
        self.assertFalse(verify.read_bytes("b.txt").exists)
        transactions = self.manager.integration_store.list_transactions(binding.workspace_id)
        self.assertEqual(transactions[-1].phase.value, "rolled_back")
        self.assertEqual(self.manager.store.get_usage(binding.workspace_id).reserved_bytes, 0)
        self.assertEqual(self.manager.ownership_store.list(binding.workspace_id), ())

        with self.assertRaises(WorkspaceError):
            verify.write_text("../escape.txt", "escape")
        self.assertFalse((self.root / "escape.txt").exists())

    def test_gateway_disable_store_disable_and_path_policy_disconnect_fail_before_mutation(self) -> None:
        disabled_port = WorkspaceEditPort(
            self.manager,
            self.created.access,
            worker_id="CodeWorkerRuntime",
            run_id="run-integration",
            task_id="task-integration",
            disabled=True,
        )
        with self.assertRaises(WorkspaceError) as disabled:
            disabled_port.write_text("disabled.txt", "no")
        self.assertEqual(disabled.exception.code, WorkspaceErrorCode.DISABLED)

        self.manager.integration_store.disabled = True
        with self.assertRaises(WorkspaceError) as store_disabled:
            self.port().write_text("store-disabled.txt", "no")
        self.assertEqual(store_disabled.exception.code, WorkspaceErrorCode.DISABLED)
        self.manager.integration_store.disabled = False

        port = self.port()
        with mock.patch.object(
            self.manager.backend.path_policy,
            "validate_logical_path",
            side_effect=WorkspaceError(
                WorkspaceErrorCode.DISABLED,
                "path safety disconnected",
                operation="test",
            ),
        ):
            with self.assertRaises(WorkspaceError) as path_disabled:
                port.write_text("path-disabled.txt", "no")
        self.assertEqual(path_disabled.exception.code, WorkspaceErrorCode.DISABLED)
        task_root = self.manager.internal_task_root(self.created.access)
        self.assertFalse((task_root / "disabled.txt").exists())
        self.assertFalse((task_root / "store-disabled.txt").exists())
        self.assertFalse((task_root / "path-disabled.txt").exists())

    def test_isolated_child_merge_preserves_user_wip_and_duplicate_merge_is_idempotent(self) -> None:
        root = self.manager.internal_task_root(self.created.access)
        _git(root, "init")
        _git(root, "config", "user.email", "zyra@example.invalid")
        _git(root, "config", "user.name", "Zyra Test")
        (root / "base.txt").write_text("base\n", encoding="utf-8")
        (root / "nested").mkdir()
        (root / "nested" / "nested-base.txt").write_text("nested base\n", encoding="utf-8")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "base")
        (root / "user-wip.txt").write_text("user untracked\n", encoding="utf-8")
        (root / "base.txt").write_text("base + user wip\n", encoding="utf-8")

        nested = root / "nested"
        _git(nested, "init")
        _git(nested, "config", "user.email", "zyra@example.invalid")
        _git(nested, "config", "user.name", "Zyra Test")
        _git(nested, "add", ".")
        _git(nested, "commit", "-m", "nested base")

        isolation = WorkspaceIsolationRuntime(self.manager, artifact_store=self.artifacts)
        prepared = isolation.prepare(
            self.created.access,
            parent_worker_id="CodeWorkerRuntime",
            child_worker_id="AgentTool-child",
            idempotency_key="prepare-child",
        )
        child = isolation.child_edit_port(prepared.isolation_id, run_id="run-integration")
        child.write_text("agent.txt", "agent change\n", idempotency_key="child-agent")
        child.write_text("nested/agent-nested.txt", "nested agent\n", idempotency_key="child-nested")
        merged = isolation.merge(
            self.created.access,
            prepared.isolation_id,
            idempotency_key="merge-child",
        )
        self.assertTrue(merged.ok)
        self.assertEqual((root / "base.txt").read_text(encoding="utf-8"), "base + user wip\n")
        self.assertEqual((root / "user-wip.txt").read_text(encoding="utf-8"), "user untracked\n")
        self.assertEqual((root / "agent.txt").read_text(encoding="utf-8"), "agent change\n")
        self.assertEqual((root / "nested" / "agent-nested.txt").read_text(encoding="utf-8"), "nested agent\n")
        record = self.manager.integration_store.require_isolation(prepared.isolation_id)
        outcomes = record.metadata["nested_repository_outcomes"]
        self.assertTrue(any(item["repository_root"] == "nested" for item in outcomes))
        replay = isolation.merge(self.created.access, prepared.isolation_id, idempotency_key="merge-child")
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual(replay.transaction_id, merged.transaction_id)

    def test_isolation_conflict_keeps_parent_and_child_queryable(self) -> None:
        port = self.port()
        created = port.write_text("conflict.txt", "base\n", idempotency_key="create-conflict")
        parent_access = created.access
        isolation = WorkspaceIsolationRuntime(self.manager, artifact_store=self.artifacts)
        prepared = isolation.prepare(
            parent_access,
            parent_worker_id="CodeWorkerRuntime",
            child_worker_id="conflicting-child",
            idempotency_key="prepare-conflict",
        )
        child = isolation.child_edit_port(prepared.isolation_id, run_id="run-integration")
        child.write_text("conflict.txt", "child\n", idempotency_key="child-conflict")
        root = self.manager.internal_task_root(parent_access)
        (root / "conflict.txt").write_text("parent concurrent\n", encoding="utf-8")
        result = isolation.merge(parent_access, prepared.isolation_id, idempotency_key="merge-conflict")
        self.assertFalse(result.ok)
        self.assertEqual(result.state, IsolationState.CONFLICTED)
        self.assertEqual((root / "conflict.txt").read_text(encoding="utf-8"), "parent concurrent\n")
        self.assertEqual(len(result.conflicts), 1)
        queried = self.manager.integration_store.list_conflicts(
            self.created.projection.workspace_id,
            isolation_id=prepared.isolation_id,
        )
        self.assertEqual(queried[0].logical_path, "conflict.txt")
        replay = isolation.merge(parent_access, prepared.isolation_id, idempotency_key="merge-conflict")
        self.assertTrue(replay.idempotent_replay)

    def test_authorized_shell_runs_in_child_workspace_and_merges_only_its_delta(self) -> None:
        parent_root = self.manager.internal_task_root(self.created.access)
        (parent_root / "user-wip.txt").write_text("keep user state\n", encoding="utf-8")
        isolation = WorkspaceIsolationRuntime(self.manager, artifact_store=self.artifacts)

        shell = isolation.run_shell_and_merge(
            self.created.access,
            command="echo isolated-shell>shell-created.txt",
            parent_worker_id="CodeWorkerRuntime",
            child_worker_id="CodeWorkerRuntime-shell",
            timeout_seconds=15,
            causation_id="tool-call-shell",
        )

        self.assertTrue(shell.ok)
        self.assertEqual(shell.returncode, 0)
        self.assertIsNotNone(shell.merge)
        self.assertTrue(shell.merge.ok)
        self.assertEqual((parent_root / "shell-created.txt").read_text(encoding="utf-8").strip(), "isolated-shell")
        self.assertEqual((parent_root / "user-wip.txt").read_text(encoding="utf-8"), "keep user state\n")
        child_binding = self.manager.store.require_binding(
            str(shell.isolation.metadata["child_workspace_id"])
        )
        self.assertEqual(child_binding.lifecycle_state, WorkspaceLifecycleState.DELETED)

    def test_rebind_switches_location_atomically_fences_old_access_and_redacts_envelope(self) -> None:
        port = self.port()
        written = port.write_text("state.txt", "portable\n", idempotency_key="portable-state")
        old_access = written.access
        old_binding = self.manager.store.require_binding(old_access.workspace_id)
        old_root = self.manager.backend.physical_root(old_binding)
        runtime = WorkspaceRebindRuntime(self.manager)
        runtime.register_endpoint("local-secondary", relative_root="endpoint-secondary")
        result = runtime.rebind(
            old_access,
            target_endpoint_id="local-secondary",
            worker_id="CodeWorkerRuntime",
            idempotency_key="rebind-secondary",
            artifact_refs=("artifact://portable",),
            event_refs=("event://portable",),
        )
        self.assertTrue(result.ok)
        latest = self.manager.store.require_binding(old_access.workspace_id)
        self.assertEqual(latest.metadata["endpoint_id"], "local-secondary")
        self.assertGreater(latest.owner_epoch, old_access.owner_epoch)
        self.assertFalse(old_root.exists())
        with self.assertRaises(WorkspaceError) as stale:
            self.manager.backend.read(old_access, mount_kind=WorkspaceKind.TASK, path="state.txt")
        self.assertEqual(stale.exception.code, WorkspaceErrorCode.OWNER_EPOCH_STALE)
        verify = self.port(result.access)
        self.assertEqual(verify.read_text("state.txt").text(), "portable\n")
        serialized = json.dumps(result.to_public_dict(), sort_keys=True)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("relative_root", serialized)

        handoff = WorkspaceHandoffRuntime(self.manager)
        current_access = verify.current_access()
        envelope = handoff.issue(
            current_access,
            audience="BrowserWorker",
            operations=(IntegrationOperation.READ,),
        )
        serialized_envelope = handoff.serialize(envelope)
        self.assertNotIn(str(self.root), serialized_envelope)
        self.assertNotIn(current_access.fence_token, serialized_envelope)
        consumed = handoff.consume(
            envelope,
            audience="BrowserWorker",
            required_operation=IntegrationOperation.READ,
        )
        self.assertEqual(consumed.workspace_id, current_access.workspace_id)
        with self.assertRaises(WorkspaceError) as replayed:
            handoff.verify(envelope, audience="BrowserWorker")
        self.assertEqual(replayed.exception.code, WorkspaceErrorCode.OWNER_EPOCH_STALE)

    def test_rebind_pre_cas_failure_rolls_back_to_source_and_restart_finishes_cleanup(self) -> None:
        port = self.port()
        written = port.write_text("rollback.txt", "source\n", idempotency_key="rebind-rollback-source")
        old_binding = self.manager.store.require_binding(written.access.workspace_id)
        old_location = old_binding.location
        runtime = WorkspaceRebindRuntime(self.manager)
        runtime.register_endpoint("broken-secondary", relative_root="endpoint-broken")
        with mock.patch.object(
            self.manager.snapshot_runtime,
            "restore",
            side_effect=WorkspaceError(
                WorkspaceErrorCode.RESTORE_FAILED,
                "injected target restore failure",
                operation="test_rebind",
            ),
        ):
            with self.assertRaises(WorkspaceError):
                runtime.rebind(
                    written.access,
                    target_endpoint_id="broken-secondary",
                    worker_id="CodeWorkerRuntime",
                    idempotency_key="rebind-broken",
                )
        latest = self.manager.store.require_binding(written.access.workspace_id)
        self.assertEqual(latest.location, old_location)
        self.assertEqual(latest.lifecycle_state, WorkspaceLifecycleState.OPEN)
        access = self.manager.acquire_for_worker(
            task_id=latest.task_id,
            session_id=latest.session_id,
            worker_id="CodeWorkerRuntime",
        )
        self.assertEqual(self.port(access).read_text("rollback.txt").text(), "source\n")
        self.assertEqual(self.manager.integration_store.list_rebinds(latest.workspace_id)[-1].state.value, "rolled_back")

        snapshot = self.manager.snapshot(latest.workspace_id)
        cleaning = self.manager._transition(  # noqa: SLF001 - crash-phase test fixture.
            self.manager.store.require_binding(latest.workspace_id),
            WorkspaceLifecycleState.CLEANING,
            write_frozen_reason="injected_cleanup_interrupt",
            active_snapshot_id=snapshot.snapshot_id,
        )
        self.assertEqual(cleaning.lifecycle_state, WorkspaceLifecycleState.CLEANING)
        restarted = WorkspaceManagerRuntime(self.manager.config)
        recoveries = restarted.recover_on_startup()
        self.assertTrue(any(item.reason == "interrupted_cleanup_resumed" for item in recoveries))
        self.assertEqual(
            restarted.store.require_binding(latest.workspace_id).lifecycle_state,
            WorkspaceLifecycleState.DELETED,
        )


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


if __name__ == "__main__":
    unittest.main()
