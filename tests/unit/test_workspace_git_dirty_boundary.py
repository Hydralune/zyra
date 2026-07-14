from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT / "packages" / "workspace"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from zyra_workspace import (  # noqa: E402
    DirtyOwnershipStore,
    GitQuery,
    WorkspaceDirtyOwner,
    WorkspaceDirtyStateRuntime,
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceGitBoundary,
    WorkspacePathSafetyPolicy,
)


@unittest.skipUnless(shutil.which("git"), "git is required for workspace dirty-state behavior tests")
class WorkspaceGitDirtyBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "workspace"
        self.repository.mkdir()
        self._git(self.repository, "init")
        self._git(self.repository, "config", "user.email", "workspace@example.invalid")
        self._git(self.repository, "config", "user.name", "Workspace Test")
        (self.repository / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        self._git(self.repository, "add", "tracked.txt")
        self._git(self.repository, "commit", "-m", "baseline")
        self.ownership = DirtyOwnershipStore(self.root / "state" / "dirty.json")
        self.policy = WorkspacePathSafetyPolicy(service_root=self.root)
        self.runtime = WorkspaceDirtyStateRuntime(
            workspace_id="workspace-1",
            workspace_root=self.repository,
            ownership_store=self.ownership,
            path_policy=self.policy,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_read_only_boundary_preserves_head_index_and_worktree(self) -> None:
        boundary = WorkspaceGitBoundary(self.repository)
        head_before = self._git(self.repository, "rev-parse", "HEAD").strip()
        index_before = _sha256(self.repository / ".git" / "index")
        tracked_before = (self.repository / "tracked.txt").read_bytes()

        self.assertTrue(boundary.is_repository())
        self.assertEqual(boundary.head_commit(), head_before)
        self.assertTrue(boundary.tree_hash())
        self.assertEqual(boundary.tracked_files(), ("tracked.txt",))
        boundary.status_porcelain()
        boundary.diff()

        self.assertEqual(self._git(self.repository, "rev-parse", "HEAD").strip(), head_before)
        self.assertEqual(_sha256(self.repository / ".git" / "index"), index_before)
        self.assertEqual((self.repository / "tracked.txt").read_bytes(), tracked_before)

    def test_arbitrary_subcommand_and_config_injection_are_rejected(self) -> None:
        with self.assertRaises(WorkspaceError) as mutation:
            GitQuery(operation="reset", arguments=("--hard",))
        self.assertEqual(mutation.exception.detail.code, WorkspaceErrorCode.GIT_QUERY_REJECTED)

        boundary = WorkspaceGitBoundary(self.repository)
        with self.assertRaises(WorkspaceError) as injection:
            boundary.query(GitQuery(operation="status", arguments=("-c", "core.pager=evil")))
        self.assertEqual(injection.exception.detail.code, WorkspaceErrorCode.GIT_QUERY_REJECTED)

    def test_dirty_baseline_captures_modified_and_untracked_content(self) -> None:
        (self.repository / "tracked.txt").write_text("user edit\n", encoding="utf-8")
        (self.repository / "untracked.txt").write_text("user untracked\n", encoding="utf-8")

        baseline = self.runtime.capture_baseline(owner_epoch=1)
        by_path = {item.path: item for item in baseline.dirty_paths}

        self.assertEqual(by_path["tracked.txt"].owner, WorkspaceDirtyOwner.USER_BASELINE)
        self.assertEqual(by_path["untracked.txt"].owner, WorkspaceDirtyOwner.USER_BASELINE)
        self.assertTrue(by_path["tracked.txt"].current_hash)
        self.assertTrue(by_path["untracked.txt"].current_hash)
        self.assertEqual(len(self.ownership.list("workspace-1")), 2)
        self.runtime.assert_user_baseline_preserved(baseline)

        (self.repository / "tracked.txt").write_text("agent clobber\n", encoding="utf-8")
        with self.assertRaises(WorkspaceError) as conflict:
            self.runtime.assert_user_baseline_preserved(baseline)
        self.assertEqual(conflict.exception.detail.code, WorkspaceErrorCode.DIRTY_STATE_CONFLICT)

    def test_agent_cannot_claim_user_baseline_dirt(self) -> None:
        (self.repository / "tracked.txt").write_text("user edit\n", encoding="utf-8")
        self.runtime.capture_baseline(owner_epoch=1)

        with self.assertRaises(WorkspaceError) as conflict:
            self.runtime.claim_agent_write(
                "tracked.txt",
                worker_id="worker-a",
                operation_id="operation-a",
            )
        self.assertEqual(conflict.exception.detail.code, WorkspaceErrorCode.DIRTY_STATE_CONFLICT)

    def test_nested_repository_is_a_separate_typed_boundary(self) -> None:
        nested = self.repository / "components" / "nested"
        nested.mkdir(parents=True)
        self._git(nested, "init")
        self._git(nested, "config", "user.email", "nested@example.invalid")
        self._git(nested, "config", "user.name", "Nested Test")
        (nested / "nested.txt").write_text("nested baseline\n", encoding="utf-8")
        self._git(nested, "add", "nested.txt")
        self._git(nested, "commit", "-m", "nested baseline")
        (nested / "nested.txt").write_text("nested dirty\n", encoding="utf-8")

        repositories = self.runtime.discover_repositories()
        roots = {item.relative_root: item for item in repositories}

        self.assertIn(".", roots)
        self.assertIn("components/nested", roots)
        self.assertEqual(roots["components/nested"].parent_repository_id, roots["."].repository_id)
        state = self.runtime.scan()
        nested_record = next(item for item in state.paths if item.path == "components/nested/nested.txt")
        self.assertEqual(nested_record.nested_repository_id, roots["components/nested"].repository_id)

    def test_repository_escape_is_rejected(self) -> None:
        boundary = WorkspaceGitBoundary(self.repository)
        with self.assertRaises(WorkspaceError) as escape:
            boundary.query(
                GitQuery(
                    operation="status",
                    relative_repository="../outside",
                )
            )
        self.assertEqual(escape.exception.detail.code, WorkspaceErrorCode.GIT_QUERY_REJECTED)

    @staticmethod
    def _git(repository: Path, *arguments: str) -> str:
        completed = subprocess.run(
            [shutil.which("git") or "git", *arguments],
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr.decode("utf-8", errors="replace"))
        return completed.stdout.decode("utf-8", errors="replace")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
