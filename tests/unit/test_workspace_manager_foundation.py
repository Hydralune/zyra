from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT / "packages" / "workspace"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from zyra_workspace import (  # noqa: E402
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceKind,
    WorkspaceLifecycleState,
    WorkspaceManagerConfig,
    WorkspaceManagerRuntime,
    WorkspaceQuota,
)


class WorkspaceManagerFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = WorkspaceManagerConfig(
            state_root=self.root / "state",
            data_root=self.root / "data",
            lease_ttl_seconds=300,
        )
        self.runtime = WorkspaceManagerRuntime(self.config)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create(self, *, worker_id: str = "worker-a", quota: WorkspaceQuota | None = None):
        return self.runtime.create_for_task(
            run_id="run-1",
            task_id="task-1",
            session_id="session-1",
            worker_id=worker_id,
            quota=quota,
            idempotency_key="create-task-1",
        )

    def test_create_crosses_ready_barrier_and_public_projection_redacts_host_path(self) -> None:
        result = self.create()
        projection = result.projection.to_dict()

        self.assertTrue(result.created)
        self.assertTrue(result.ready_barrier_passed)
        self.assertEqual(projection["lifecycle_state"], "open")
        self.assertTrue(projection["physical_location_redacted"])
        self.assertEqual({item["kind"] for item in projection["mounts"]}, {"task", "artifact", "download", "temp"})
        serialized = json.dumps(result.to_public_dict(), sort_keys=True)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("relative_root", serialized)
        physical = self.runtime.internal_task_root(result.access)
        self.assertTrue(physical.is_dir())
        self.assertTrue((physical.parent / ".zyra-workspace.json").is_file())

    def test_idempotent_create_returns_existing_binding(self) -> None:
        first = self.create()
        second = self.create()

        self.assertEqual(first.projection.workspace_id, second.projection.workspace_id)
        self.assertFalse(second.created)
        self.assertEqual(len(self.runtime.store.list_bindings()), 1)

    def test_read_before_write_and_stale_base_hash_are_enforced(self) -> None:
        result = self.create()
        with self.assertRaises(WorkspaceError) as missing_read:
            self.runtime.backend.write_after_read(
                result.access,
                mount_kind=WorkspaceKind.TASK,
                path="src/main.py",
                content=b"print('new')\n",
            )
        self.assertEqual(missing_read.exception.detail.code, WorkspaceErrorCode.READ_REQUIRED)

        absent = self.runtime.backend.read(
            result.access,
            mount_kind=WorkspaceKind.TASK,
            path="src/main.py",
        )
        self.assertEqual(absent.record.file_identity, "absent")
        written = self.runtime.backend.write_after_read(
            result.access,
            mount_kind=WorkspaceKind.TASK,
            path="src/main.py",
            content=b"print('one')\n",
        )
        self.assertTrue(written.created)

        binding = self.runtime.store.require_binding(result.projection.workspace_id)
        task_root = self.runtime.backend.mount_root(binding, WorkspaceKind.TASK)
        target = task_root / "src" / "main.py"
        self.runtime.backend.read(
            result.access,
            mount_kind=WorkspaceKind.TASK,
            path="src/main.py",
        )
        target.write_bytes(b"external mutation\n")
        with self.assertRaises(WorkspaceError) as stale:
            self.runtime.backend.write_after_read(
                result.access,
                mount_kind=WorkspaceKind.TASK,
                path="src/main.py",
                content=b"print('two')\n",
            )
        self.assertIn(
            stale.exception.detail.code,
            {
                WorkspaceErrorCode.BASE_HASH_STALE,
                WorkspaceErrorCode.BASE_MTIME_STALE,
                WorkspaceErrorCode.FILE_IDENTITY_CHANGED,
            },
        )
        self.assertEqual(target.read_bytes(), b"external mutation\n")

    def test_path_escape_symlink_and_mount_write_policy_are_rejected(self) -> None:
        result = self.create()
        for unsafe in ("../outside", "/absolute", "C:\\outside", "\\\\server\\share", "con.txt"):
            with self.subTest(path=unsafe):
                with self.assertRaises(WorkspaceError):
                    self.runtime.backend.read(
                        result.access,
                        mount_kind=WorkspaceKind.TASK,
                        path=unsafe,
                    )

        self.runtime.backend.read(
            result.access,
            mount_kind=WorkspaceKind.ARTIFACT,
            path="result.txt",
        )
        with self.assertRaises(WorkspaceError) as artifact_write:
            self.runtime.backend.write_after_read(
                result.access,
                mount_kind=WorkspaceKind.ARTIFACT,
                path="result.txt",
                content=b"not canonical",
            )
        self.assertEqual(artifact_write.exception.detail.code, WorkspaceErrorCode.MOUNT_BOUNDARY_VIOLATION)

    def test_quota_reservation_prevents_oversized_write_before_mutation(self) -> None:
        quota = WorkspaceQuota(
            max_bytes=8,
            max_files=2,
            max_directories=4,
            max_single_file_bytes=8,
            max_path_depth=8,
            max_snapshots=2,
            max_snapshot_bytes=32,
        )
        result = self.create(quota=quota)
        self.runtime.backend.read(result.access, mount_kind=WorkspaceKind.TASK, path="large.bin")
        with self.assertRaises(WorkspaceError) as exceeded:
            self.runtime.backend.write_after_read(
                result.access,
                mount_kind=WorkspaceKind.TASK,
                path="large.bin",
                content=b"123456789",
            )
        self.assertEqual(exceeded.exception.detail.code, WorkspaceErrorCode.SINGLE_FILE_LIMIT_EXCEEDED)
        binding = self.runtime.store.require_binding(result.projection.workspace_id)
        self.assertFalse((self.runtime.backend.mount_root(binding, WorkspaceKind.TASK) / "large.bin").exists())

    def test_lease_transfer_fences_old_worker_and_invalidates_read_epoch(self) -> None:
        created = self.create(worker_id="worker-a")
        self.runtime.backend.read(created.access, mount_kind=WorkspaceKind.TASK, path="shared.txt")
        replacement = self.runtime.acquire_for_worker(
            task_id="task-1",
            session_id="session-1",
            worker_id="worker-b",
        )

        self.assertGreater(replacement.owner_epoch, created.access.owner_epoch)
        with self.assertRaises(WorkspaceError) as stale:
            self.runtime.backend.read(created.access, mount_kind=WorkspaceKind.TASK, path="shared.txt")
        self.assertIn(
            stale.exception.detail.code,
            {WorkspaceErrorCode.OWNER_EPOCH_STALE, WorkspaceErrorCode.LEASE_REVOKED},
        )
        reread = self.runtime.backend.read(replacement, mount_kind=WorkspaceKind.TASK, path="shared.txt")
        self.assertEqual(reread.record.owner_epoch, replacement.owner_epoch)

    def test_snapshot_restore_verifies_and_restores_content(self) -> None:
        created = self.create()
        workspace_id = created.projection.workspace_id
        self.runtime.backend.read(created.access, mount_kind=WorkspaceKind.TASK, path="answer.txt")
        self.runtime.backend.write_after_read(
            created.access,
            mount_kind=WorkspaceKind.TASK,
            path="answer.txt",
            content=b"version one",
        )
        snapshot = self.runtime.snapshot(workspace_id, include_dirty_state=False)

        edit_handle = self.runtime.acquire_for_worker(
            task_id="task-1",
            session_id="session-1",
            worker_id="editor",
        )
        self.runtime.backend.read(edit_handle, mount_kind=WorkspaceKind.TASK, path="answer.txt")
        self.runtime.backend.write_after_read(
            edit_handle,
            mount_kind=WorkspaceKind.TASK,
            path="answer.txt",
            content=b"version two",
        )
        restore = self.runtime.restore(workspace_id, snapshot.snapshot_id)
        verify_handle = self.runtime.acquire_for_worker(
            task_id="task-1",
            session_id="session-1",
            worker_id="verifier",
        )
        content = self.runtime.backend.read(
            verify_handle,
            mount_kind=WorkspaceKind.TASK,
            path="answer.txt",
        ).content

        self.assertEqual(content, b"version one")
        self.assertEqual(restore.restored_bytes, len(b"version one"))
        self.assertGreater(verify_handle.owner_epoch, edit_handle.owner_epoch)

    def test_corrupt_snapshot_fails_before_mutating_live_workspace(self) -> None:
        created = self.create()
        workspace_id = created.projection.workspace_id
        self.runtime.backend.read(created.access, mount_kind=WorkspaceKind.TASK, path="safe.txt")
        self.runtime.backend.write_after_read(
            created.access,
            mount_kind=WorkspaceKind.TASK,
            path="safe.txt",
            content=b"snapshot content",
        )
        snapshot = self.runtime.snapshot(workspace_id, include_dirty_state=False)
        handle = self.runtime.acquire_for_worker(
            task_id="task-1",
            session_id="session-1",
            worker_id="mutator",
        )
        self.runtime.backend.read(handle, mount_kind=WorkspaceKind.TASK, path="safe.txt")
        self.runtime.backend.write_after_read(
            handle,
            mount_kind=WorkspaceKind.TASK,
            path="safe.txt",
            content=b"live content",
        )
        file_entry = next(item for item in snapshot.entries if item.kind.value == "file")
        blob = self.runtime.snapshot_runtime.blob_root / file_entry.content_hash[:2] / file_entry.content_hash
        blob.write_bytes(b"corrupt")

        with self.assertRaises(WorkspaceError) as corrupt:
            self.runtime.restore(workspace_id, snapshot.snapshot_id)
        self.assertEqual(corrupt.exception.detail.code, WorkspaceErrorCode.SNAPSHOT_CORRUPT)
        binding = self.runtime.store.require_binding(workspace_id)
        target = self.runtime.backend.mount_root(binding, WorkspaceKind.TASK) / "safe.txt"
        self.assertEqual(target.read_bytes(), b"live content")
        self.assertEqual(binding.lifecycle_state, WorkspaceLifecycleState.RECOVERY_REQUIRED)

    def test_restart_recovers_binding_and_rotates_unavailable_fence_token(self) -> None:
        created = self.create()
        workspace_id = created.projection.workspace_id
        restarted = WorkspaceManagerRuntime(self.config)
        recoveries = restarted.recover_on_startup()
        access = restarted.acquire_for_worker(
            task_id="task-1",
            session_id="session-1",
            worker_id="after-restart",
        )

        self.assertEqual(recoveries, ())
        self.assertEqual(access.workspace_id, workspace_id)
        self.assertGreater(access.owner_epoch, created.access.owner_epoch)
        self.assertTrue(restarted.internal_task_root(access).is_dir())

    def test_cleanup_is_archive_before_delete_and_receipted(self) -> None:
        created = self.create()
        workspace_id = created.projection.workspace_id
        self.runtime.backend.read(created.access, mount_kind=WorkspaceKind.TASK, path="deliverable.txt")
        self.runtime.backend.write_after_read(
            created.access,
            mount_kind=WorkspaceKind.TASK,
            path="deliverable.txt",
            content=b"deliverable",
        )
        receipt = self.runtime.cleanup(workspace_id)

        binding = self.runtime.store.require_binding(workspace_id)
        self.assertTrue(receipt.ok)
        self.assertTrue(receipt.snapshot_id)
        self.assertEqual(binding.lifecycle_state, WorkspaceLifecycleState.DELETED)
        self.assertFalse(self.runtime.backend.physical_root(binding).exists())
        self.assertEqual(self.runtime.snapshot_runtime.load(receipt.snapshot_id).workspace_id, workspace_id)

    def test_directory_quota_is_checked_before_parent_creation(self) -> None:
        quota = WorkspaceQuota(
            max_bytes=1024,
            max_files=8,
            max_directories=4,
            max_single_file_bytes=1024,
        )
        created = self.create(quota=quota)
        self.runtime.backend.read(
            created.access,
            mount_kind=WorkspaceKind.TASK,
            path="nested/output.txt",
        )

        with self.assertRaises(WorkspaceError) as exceeded:
            self.runtime.backend.write_after_read(
                created.access,
                mount_kind=WorkspaceKind.TASK,
                path="nested/output.txt",
                content=b"blocked",
            )

        self.assertEqual(exceeded.exception.detail.code, WorkspaceErrorCode.QUOTA_EXCEEDED)
        self.assertFalse((self.runtime.internal_task_root(created.access) / "nested").exists())

    def test_disabled_local_backend_fails_without_global_directory_fallback(self) -> None:
        disabled_root = self.root / "disabled"
        runtime = WorkspaceManagerRuntime(
            WorkspaceManagerConfig(
                state_root=disabled_root / "state",
                data_root=disabled_root / "data",
                local_enabled=False,
            )
        )
        with self.assertRaises(WorkspaceError) as disabled:
            runtime.create_for_task(run_id="run-x", task_id="task-x", session_id="session-x")
        self.assertEqual(disabled.exception.detail.code, WorkspaceErrorCode.DISABLED)
        self.assertFalse((disabled_root / "data").exists())
        self.assertIsNone(runtime.health()["fallback_backend"])


if __name__ == "__main__":
    unittest.main()
