from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any


DEFAULT_GENERATED_UNTRACKED_ROOTS = (
    ".tmp",
    ".zyra",
    "docs/evidence/phase2",
    "docs/reviews/evidence/phase2",
)


class WorktreeBoundaryError(RuntimeError):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode:
        raise WorktreeBoundaryError(
            f"git {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    return completed.stdout


def _status_entries(root: Path) -> tuple[tuple[str, str], ...]:
    """Return porcelain entries without Git's C-style path quoting.

    ``-z`` is required here: a quoted non-ASCII path can look as though the
    leading quote is part of the repository path and escape an otherwise
    allowed generated root.  NUL framing also keeps whitespace and newlines
    in filenames unambiguous.
    """

    completed = subprocess.run(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "-z",
        ],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if completed.returncode:
        raise WorktreeBoundaryError(
            "git status --porcelain=v1 --untracked-files=all -z failed: "
            + completed.stderr.decode("utf-8", errors="replace").strip()
        )
    records = completed.stdout.decode("utf-8", errors="replace").split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2] != " ":
            raise WorktreeBoundaryError("git status returned an invalid porcelain record")
        status = record[:2]
        path = record[3:].replace("\\", "/").rstrip("/")
        entries.append((status, path))
        if "R" in status or "C" in status:
            if index >= len(records) or not records[index]:
                raise WorktreeBoundaryError(
                    "git status returned an incomplete rename/copy record"
                )
            index += 1
    return tuple(entries)


def _inside(path: str, root: str) -> bool:
    selected = path.rstrip("/")
    prefix = root.strip().replace("\\", "/").strip("/")
    return selected == prefix or selected.startswith(prefix + "/")


def inspect_worktree(
    repository_root: str | Path,
    *,
    expected_head: str = "",
    allowed_untracked_roots: Sequence[str] = DEFAULT_GENERATED_UNTRACKED_ROOTS,
) -> dict[str, Any]:
    root = Path(repository_root).resolve()
    head = _git(root, "rev-parse", "HEAD").strip()
    tree = _git(root, "rev-parse", "HEAD^{tree}").strip()
    entries = _status_entries(root)
    allowed = tuple(
        sorted(
            {
                item.strip().replace("\\", "/").strip("/")
                for item in allowed_untracked_roots
                if item.strip()
            }
        )
    )
    generated: list[str] = []
    tracked_dirty: list[str] = []
    unexpected_untracked: list[str] = []
    for status, path in entries:
        entry = f"{status} {path}"
        if status == "??":
            if any(_inside(path, prefix) for prefix in allowed):
                generated.append(path)
            else:
                unexpected_untracked.append(entry)
        else:
            tracked_dirty.append(entry)
    ready = (
        (not expected_head or head == expected_head)
        and not tracked_dirty
        and not unexpected_untracked
    )
    value = {
        "schema": "zyra.release-worktree-boundary/v1",
        "ready": ready,
        "head_commit": head,
        "head_tree": tree,
        "expected_head": expected_head or head,
        "head_matches": not expected_head or head == expected_head,
        "tracked_dirty_entries": tracked_dirty,
        "unexpected_untracked_entries": unexpected_untracked,
        "allowed_generated_roots": list(allowed),
        "ignored_generated_entry_count": len(generated),
        "ignored_generated_paths_digest": _digest(sorted(generated)),
    }
    value["boundary_digest"] = _digest(value)
    return value


def require_worktree_boundary(
    repository_root: str | Path,
    *,
    expected_head: str,
    allowed_untracked_roots: Sequence[str] = DEFAULT_GENERATED_UNTRACKED_ROOTS,
) -> dict[str, Any]:
    receipt = inspect_worktree(
        repository_root,
        expected_head=expected_head,
        allowed_untracked_roots=allowed_untracked_roots,
    )
    if not receipt["ready"]:
        raise WorktreeBoundaryError(
            "release source boundary is dirty or does not match the target"
        )
    return receipt


__all__ = [
    "DEFAULT_GENERATED_UNTRACKED_ROOTS",
    "WorktreeBoundaryError",
    "inspect_worktree",
    "require_worktree_boundary",
]
