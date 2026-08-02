from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
PRODUCTIZATION_ROOT = ROOT / "packages" / "productization"
if str(PRODUCTIZATION_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCTIZATION_ROOT))

from zyra_productization.release.worktree import (
    WorktreeBoundaryError,
    inspect_worktree,
    require_worktree_boundary,
)

P2_BASE_COMMIT = "e207b46ca690171139a718b8b85d808cb5a79c1e"
LOOPX_CROSS_VERSION_BASE_COMMIT = "3c4d1092187b1777468cad0ce2a772244d012197"
PYTHON_TEST_POLICY_PATH = ROOT / "config" / "release-python-tests.json"
FIRST_STAGE_FREEZE_ROOT = (
    ROOT / "docs" / "reviews" / "evidence" / "M3-S03-02" / "final-freeze"
)
TYPESCRIPT_RUNTIME_TEST_ROOTS = (
    "packages/commands/test",
    "packages/integrations/claude-mcp/test",
    "packages/memory/curator-state-machine/test",
    "packages/memory/retrieval-algorithms/test",
    "packages/memory/skill-memory-runtime/test",
    "packages/runtime/claude-runtime/test",
    "packages/runtime/provider-control-plane/test",
    "packages/runtime/runtime-event-spine/test",
    "packages/runtime/sandbox-gateway-control/test",
)
FINAL_REGRESSION_COMMAND_IDS = (
    "python-full-regression",
    "typescript-runtime-regression",
    "typescript-typecheck",
    "web-typecheck",
    "web-tests",
    "web-build",
    "phase1-m1",
    "phase1-m2",
    "phase1-m3",
    "phase1-final-freeze",
    "phase2-policy-contracts",
    "internalization-ledger",
    "loopx-offline-runtime",
    "loopx-cross-version-restart",
)
RESUME_RERUN_GATE_IDS = (
    "phase2-policy-contracts",
    "internalization-ledger",
    "loopx-cross-version-restart",
)
RESUME_ALLOWED_PATHS = (
    "packages/productization/zyra_productization/release/phase2_freeze.py",
    "scripts/audit/verify_phase2_freeze.py",
    "scripts/release/run_phase2_final_regression.py",
    "scripts/release/verify_loopx_cross_version_upgrade.py",
    "tests/unit/productization/test_phase2_final_regression.py",
    "tests/unit/productization/test_phase2_freeze_audit.py",
)
RESUME_REQUIRED_FIRST_COMMIT = "363e011ff899e76cdb2c16e7246294f333cb5b9f"
RESUME_REMEDIATION_TESTS = (
    "tests/unit/productization/test_phase2_final_regression.py",
    "tests/unit/productization/test_phase2_freeze_audit.py",
)
SUPPLEMENT_SOURCE_TARGET_COMMIT = "7995b64e8fd48028f1e12d4c60a23f6df4784a2e"
SUPPLEMENT_SOURCE_RECEIPT_SHA256 = (
    "b8d9ff131cd1206044160a7cea590de1ea6bdd668e8e428fceb70fef143c4df4"
)
SUPPLEMENT_REQUIRED_FIRST_COMMIT = (
    "751dbe2c2aff2172ad3bd82946486d09ca415f3c"
)
SUPPLEMENT_FIRST_ALLOWED_PATHS = (
    "packages/evaluation/zyra_evaluation/policy_benchmark/sealed_physical.py",
    "packages/productization/zyra_productization/release/phase2_freeze.py",
    "scripts/release/run_phase2_final_regression.py",
    "tests/scenarios/test_phase2_sealed_long_runs.py",
    "tests/unit/productization/test_phase2_final_regression.py",
    "tests/unit/productization/test_phase2_freeze_audit.py",
)
SUPPLEMENT_ALLOWED_PATHS = (
    "packages/orchestration/zyra_orchestration/topology_policy/production.py",
    "packages/productization/zyra_productization/release/phase2_freeze.py",
    "scripts/release/run_phase2_final_regression.py",
    "tests/integration/test_phase2_production_policy_main_path.py",
    "tests/unit/productization/test_phase2_final_regression.py",
    "tests/unit/productization/test_phase2_freeze_audit.py",
)
SUPPLEMENT_REMEDIATION_TESTS = (
    "tests/scenarios/test_phase2_sealed_long_runs.py",
    "tests/unit/test_deployment_profiles_runtime.py",
    "tests/unit/orchestration/test_agentprune_optimizer.py",
    "tests/integration/test_spatial_temporal_pruning.py",
    "tests/integration/test_phase2_production_policy_main_path.py",
    "tests/unit/productization/test_phase2_final_regression.py",
    "tests/unit/productization/test_phase2_freeze_audit.py",
)
SUPPLEMENT_RERUN_GATE_IDS = (
    "phase2-policy-contracts",
    "internalization-ledger",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _git(*arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _canonical_digest(value: Mapping[str, Any], field: str) -> str:
    unsigned = dict(value)
    unsigned.pop(field, None)
    payload = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _embedded_digest_ready(value: Mapping[str, Any], field: str) -> bool:
    observed = str(value.get(field) or "")
    return bool(observed) and hmac.compare_digest(
        observed,
        _canonical_digest(value, field),
    )


def _load_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"JSON receipt must be an object: {path}")
    return value


def _receipt_member(root: Path, value: Any) -> Path:
    raw = str(value or "")
    if not raw or Path(raw).is_absolute():
        raise ValueError("receipt member must be a relative path")
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError("receipt member escapes its evidence root") from error
    if not candidate.is_file():
        raise ValueError(f"receipt member is missing: {raw}")
    return candidate


def _boundary_ready(
    value: Mapping[str, Any],
    *,
    target: str,
    target_tree: str,
) -> bool:
    return (
        value.get("schema") == "zyra.release-worktree-boundary/v1"
        and value.get("ready") is True
        and value.get("head_commit") == target
        and value.get("expected_head") == target
        and value.get("head_tree") == target_tree
        and value.get("head_matches") is True
        and value.get("tracked_dirty_entries") in ([], ())
        and value.get("unexpected_untracked_entries") in ([], ())
        and _embedded_digest_ready(value, "boundary_digest")
    )


def _git_common_dir(checkout: Path) -> Path:
    checkout = checkout.resolve()
    raw = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    selected = Path(raw)
    return (
        selected.resolve()
        if selected.is_absolute()
        else (checkout / selected).resolve()
    )


def _trusted_git_common_dir() -> Path:
    return _git_common_dir(ROOT)


def _loopx_base_boundary(checkout: Path) -> dict[str, Any]:
    checkout = checkout.resolve()
    trusted_common_dir = _trusted_git_common_dir()
    common_dir = _git_common_dir(checkout)
    if common_dir != trusted_common_dir:
        rejected: dict[str, Any] = {
            "schema": "zyra.loopx-cross-version-base-boundary/v1",
            "ready": False,
            "checkout": str(checkout),
            "git_common_dir": str(common_dir),
            "trusted_git_common_dir": str(trusted_common_dir),
            "trusted_common_dir_matches": False,
            "rejected_before_worktree_commands": True,
        }
        rejected["boundary_digest"] = _canonical_digest(
            rejected,
            "boundary_digest",
        )
        return rejected
    head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    expected_tree = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "rev-parse",
            f"{LOOPX_CROSS_VERSION_BASE_COMMIT}^{{tree}}",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    toplevel = Path(
        subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
    ).resolve()
    status = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
        ],
        check=True,
        capture_output=True,
    ).stdout
    flags = subprocess.run(
        ["git", "-C", str(checkout), "ls-files", "-v", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    unsafe_flags = tuple(
        entry.decode("utf-8", errors="replace")
        for entry in flags.split(b"\0")
        if entry and not entry.startswith(b"H ")
    )
    index = subprocess.run(
        ["git", "-C", str(checkout), "ls-files", "-s", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    object_format = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "--show-object-format"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    blob_manifest = hashlib.sha256()
    blob_mismatches: list[str] = []
    unsupported_entries: list[str] = []
    tracked_paths: list[str] = []
    expected_blobs: list[str] = []
    tracked_count = 0
    for entry in index.split(b"\0"):
        if not entry:
            continue
        metadata, separator, path_bytes = entry.partition(b"\t")
        parts = metadata.split()
        path = path_bytes.decode("utf-8", errors="surrogateescape")
        if separator != b"\t" or len(parts) != 3:
            unsupported_entries.append(path or "<invalid-index-entry>")
            continue
        mode, expected_blob, stage = (
            parts[0].decode("ascii", errors="replace"),
            parts[1].decode("ascii", errors="replace"),
            parts[2].decode("ascii", errors="replace"),
        )
        tracked_count += 1
        blob_manifest.update(path_bytes)
        blob_manifest.update(b"\0" + parts[0] + b"\0" + parts[1] + b"\n")
        candidate = checkout / path
        if (
            mode not in {"100644", "100755"}
            or stage != "0"
            or not candidate.is_file()
            or "\n" in path
            or "\r" in path
        ):
            unsupported_entries.append(path)
            continue
        tracked_paths.append(path)
        expected_blobs.append(expected_blob)
    canonical_hashes = subprocess.run(
        ["git", "-C", str(checkout), "hash-object", "--stdin-paths"],
        check=True,
        input="".join(f"{path}\n" for path in tracked_paths),
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.splitlines()
    if len(canonical_hashes) != len(tracked_paths):
        unsupported_entries.append("<canonical-hash-count-mismatch>")
    else:
        blob_mismatches.extend(
            path
            for path, expected_blob, actual_blob in zip(
                tracked_paths,
                expected_blobs,
                canonical_hashes,
                strict=True,
            )
            if actual_blob != expected_blob
        )
    sparse = subprocess.run(
        ["git", "-C", str(checkout), "config", "--bool", "core.sparseCheckout"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip().casefold() == "true"
    ready = (
        head == LOOPX_CROSS_VERSION_BASE_COMMIT
        and tree == expected_tree
        and toplevel == checkout
        and object_format == "sha1"
        and not status
        and not unsafe_flags
        and not sparse
        and not blob_mismatches
        and not unsupported_entries
    )
    value: dict[str, Any] = {
        "schema": "zyra.loopx-cross-version-base-boundary/v1",
        "ready": ready,
        "checkout": str(checkout),
        "git_common_dir": str(common_dir),
        "trusted_git_common_dir": str(trusted_common_dir),
        "trusted_common_dir_matches": common_dir == trusted_common_dir,
        "rejected_before_worktree_commands": False,
        "head_commit": head,
        "head_tree": tree,
        "repository_toplevel": str(toplevel),
        "checkout_is_repository_toplevel": toplevel == checkout,
        "object_format": object_format,
        "tracked_untracked_and_ignored_clean": not status,
        "status_sha256": hashlib.sha256(status).hexdigest(),
        "status_entry_count": 0 if not status else status.count(b"\0"),
        "index_flags_sha256": hashlib.sha256(flags).hexdigest(),
        "unsafe_index_flag_count": len(unsafe_flags),
        "unsafe_index_flags": list(unsafe_flags[:20]),
        "sparse_checkout": sparse,
        "tracked_file_count": tracked_count,
        "tracked_blob_manifest_sha256": blob_manifest.hexdigest(),
        "tracked_blob_mismatch_count": len(blob_mismatches),
        "tracked_blob_mismatches": blob_mismatches[:20],
        "unsupported_index_entry_count": len(unsupported_entries),
        "unsupported_index_entries": unsupported_entries[:20],
    }
    value["boundary_digest"] = _canonical_digest(value, "boundary_digest")
    return value


def _require_disjoint_roots(first: Path, second: Path) -> None:
    first = first.resolve()
    second = second.resolve()
    for candidate, root in ((first, second), (second, first)):
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        raise ValueError(
            f"final-regression roots must be disjoint: {first} and {second}"
        )


def _resolve_bun() -> str:
    executable = "bun.exe" if os.name == "nt" else "bun"
    local = ROOT / "node_modules" / ".bin" / executable
    if local.is_file():
        return str(local)
    discovered = shutil.which("bun")
    if discovered:
        return discovered
    raise ValueError(
        "bun is required for the final regression and was not found on PATH "
        "or in node_modules/.bin"
    )


def _source_python_path() -> str:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        pyproject = tomllib.load(stream)
    entries = (
        pyproject.get("tool", {})
        .get("setuptools", {})
        .get("packages", {})
        .get("find", {})
        .get("where", ())
    )
    if not isinstance(entries, list) or not entries:
        raise ValueError("project Python package roots are missing")
    roots = tuple(
        (ROOT / entry).resolve()
        for entry in entries
        if isinstance(entry, str) and (ROOT / entry).is_dir()
    )
    if len(roots) != len(entries):
        raise ValueError("a declared project Python package root is missing")
    return os.pathsep.join(str(path) for path in roots)


def _python_test_policy() -> dict[str, Any]:
    try:
        payload = json.loads(PYTHON_TEST_POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"release Python test policy is unavailable: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("release Python test policy must be an object")
    if payload.get("schema") != "zyra.release-python-test-policy/v1":
        raise ValueError("release Python test policy schema is unsupported")
    for field in ("roots", "ignore_files", "deselect_nodeids"):
        values = payload.get(field)
        if (
            not isinstance(values, list)
            or any(not isinstance(item, str) or not item.strip() for item in values)
            or len(values) != len(set(values))
        ):
            raise ValueError(f"release Python test policy field is invalid: {field}")
    if not str(payload.get("debt_owner") or "").strip():
        raise ValueError("release Python test policy has no debt owner")
    if len(str(payload.get("reason") or "").strip()) < 24:
        raise ValueError("release Python test policy has no bounded rationale")
    for relative in (*payload["roots"], *payload["ignore_files"]):
        candidate = (ROOT / relative).resolve()
        try:
            candidate.relative_to(ROOT)
        except ValueError as error:
            raise ValueError(
                f"release Python test policy path escapes the target: {relative}"
            ) from error
        if not candidate.exists():
            raise ValueError(
                f"release Python test policy path is missing: {relative}"
            )
    for nodeid in payload["deselect_nodeids"]:
        test_path, separator, _selection = nodeid.partition("::")
        if not separator or not (ROOT / test_path).is_file():
            raise ValueError(
                f"release Python test policy node id is invalid: {nodeid}"
            )
    return payload


def _python_test_arguments() -> tuple[str, ...]:
    policy = _python_test_policy()
    arguments: list[str] = []
    arguments.extend(
        f"--ignore={ROOT / relative}" for relative in policy["ignore_files"]
    )
    arguments.extend(
        f"--deselect={nodeid}" for nodeid in policy["deselect_nodeids"]
    )
    arguments.extend(str(ROOT / relative) for relative in policy["roots"])
    return tuple(arguments)


def _command_specs(
    *,
    output_root: Path,
    python: str,
    bun: str,
    loopx_base_checkout: Path,
    target_commit: str,
) -> tuple[tuple[str, tuple[str, ...], float], ...]:
    basetemp = output_root / "pytest"
    return (
        (
            "python-full-regression",
            (
                python,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(basetemp),
                *_python_test_arguments(),
            ),
            7200,
        ),
        (
            "typescript-runtime-regression",
            (bun, "test", *TYPESCRIPT_RUNTIME_TEST_ROOTS),
            3600,
        ),
        ("typescript-typecheck", (bun, "run", "typecheck"), 1200),
        ("web-typecheck", (bun, "run", "typecheck:web"), 1200),
        ("web-tests", (bun, "run", "test:web"), 1800),
        ("web-build", (bun, "run", "build:web"), 1800),
        ("phase1-m1", (python, "scripts/verify_m1.py"), 1800),
        ("phase1-m2", (python, "scripts/verify_m2.py"), 1800),
        ("phase1-m3", (python, "scripts/verify_m3.py"), 1800),
        (
            "phase1-final-freeze",
            (
                python,
                "scripts/verify_first_stage.py",
                "--output",
                str(FIRST_STAGE_FREEZE_ROOT),
            ),
            1800,
        ),
        (
            "phase2-policy-contracts",
            (
                python,
                "scripts/verify_phase2_policy_contracts.py",
                "--target-commit",
                target_commit,
                "--require-strongest-active",
                "--output",
                str(output_root / "phase2-policy-contracts.json"),
            ),
            300,
        ),
        (
            "internalization-ledger",
            (
                python,
                "scripts/verify_internalization_ledger.py",
                "--base",
                P2_BASE_COMMIT,
                "--json",
            ),
            600,
        ),
        (
            "loopx-offline-runtime",
            (python, "scripts/release/verify_loopx_runtime.py"),
            600,
        ),
        (
            "loopx-cross-version-restart",
            (
                python,
                "scripts/release/verify_loopx_cross_version_upgrade.py",
                "--base-checkout",
                str(loopx_base_checkout.resolve()),
                "--target-checkout",
                str(ROOT),
                "--workspace",
                str(output_root / "loopx-cross-version-workspace"),
                "--output",
                str(output_root / "loopx-cross-version-upgrade.json"),
            ),
            900,
        ),
    )


def _resume_target_delta(*, source_target: str, target_commit: str) -> dict[str, Any]:
    commit_chain = (RESUME_REQUIRED_FIRST_COMMIT, target_commit)
    expected_parents = (source_target, RESUME_REQUIRED_FIRST_COMMIT)
    for commit, expected_parent in zip(commit_chain, expected_parents, strict=True):
        parents = _git("rev-list", "--parents", "-n", "1", commit).split()
        if parents != [commit, expected_parent]:
            raise ValueError("resume target is not the exact two-commit remediation chain")
    allowed = set(RESUME_ALLOWED_PATHS)
    status_lines = tuple(
        line
        for line in _git(
            "diff",
            "--name-status",
            "--no-renames",
            source_target,
            target_commit,
        ).splitlines()
        if line
    )
    if not status_lines:
        raise ValueError("resume target has no remediation delta")
    changed_paths: list[str] = []
    blob_transitions: list[dict[str, str]] = []
    for line in status_lines:
        status, separator, path = line.partition("\t")
        if separator != "\t" or status != "M" or path not in allowed:
            raise ValueError(f"resume target contains a forbidden change: {line}")
        source_entry = _git("ls-tree", source_target, "--", path).split()
        target_entry = _git("ls-tree", target_commit, "--", path).split()
        if (
            len(source_entry) < 3
            or len(target_entry) < 3
            or source_entry[0] != target_entry[0]
            or source_entry[0] not in {"100644", "100755"}
            or source_entry[1] != "blob"
            or target_entry[1] != "blob"
        ):
            raise ValueError(f"resume target changed the file mode or object type: {path}")
        changed_paths.append(path)
        blob_transitions.append(
            {
                "path": path,
                "mode": source_entry[0],
                "source_blob": source_entry[2],
                "target_blob": target_entry[2],
            }
        )
    if set(changed_paths) != set(RESUME_ALLOWED_PATHS) or len(changed_paths) != len(
        RESUME_ALLOWED_PATHS
    ):
        raise ValueError("resume target must change every required control-plane file")
    diff = subprocess.run(
        ["git", "diff", "--binary", "--no-renames", source_target, target_commit],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    previous = source_target
    commit_path_changes: list[dict[str, Any]] = []
    for commit in commit_chain:
        commit_statuses = tuple(
            line
            for line in _git(
                "diff",
                "--name-status",
                "--no-renames",
                previous,
                commit,
            ).splitlines()
            if line
        )
        if not commit_statuses:
            raise ValueError("resume remediation commit has no delta")
        segment_transitions: list[dict[str, str]] = []
        for line in commit_statuses:
            status, separator, path = line.partition("\t")
            if separator != "\t" or status != "M" or path not in allowed:
                raise ValueError(f"resume remediation commit contains a forbidden change: {line}")
            parent_entry = _git("ls-tree", previous, "--", path).split()
            commit_entry = _git("ls-tree", commit, "--", path).split()
            if (
                len(parent_entry) < 3
                or len(commit_entry) < 3
                or parent_entry[0] != commit_entry[0]
                or parent_entry[0] not in {"100644", "100755"}
                or parent_entry[1] != "blob"
                or commit_entry[1] != "blob"
            ):
                raise ValueError(
                    "resume remediation commit changed the file mode or object type: "
                    + path
                )
            segment_transitions.append(
                {
                    "path": path,
                    "mode": parent_entry[0],
                    "source_blob": parent_entry[2],
                    "target_blob": commit_entry[2],
                }
            )
        segment_diff = subprocess.run(
            ["git", "diff", "--binary", "--no-renames", previous, commit],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
        commit_path_changes.append(
            {
                "commit": commit,
                "parent": previous,
                "change_statuses": list(commit_statuses),
                "blob_transitions": segment_transitions,
                "diff_sha256": hashlib.sha256(segment_diff).hexdigest(),
            }
        )
        previous = commit
    allowlist_payload = json.dumps(
        RESUME_ALLOWED_PATHS,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return {
        "source_target_commit": source_target,
        "source_target_tree": _git("rev-parse", f"{source_target}^{{tree}}"),
        "target_commit": target_commit,
        "target_tree": _git("rev-parse", f"{target_commit}^{{tree}}"),
        "direct_single_parent": False,
        "linear_single_parent_chain": True,
        "required_first_commit": RESUME_REQUIRED_FIRST_COMMIT,
        "commit_count": len(commit_chain),
        "commit_chain": list(commit_chain),
        "commit_path_changes": commit_path_changes,
        "changed_paths": changed_paths,
        "change_statuses": list(status_lines),
        "blob_transitions": blob_transitions,
        "allowed_paths": list(RESUME_ALLOWED_PATHS),
        "allowlist_sha256": hashlib.sha256(allowlist_payload).hexdigest(),
        "diff_sha256": hashlib.sha256(diff).hexdigest(),
        "production_or_configuration_changed": False,
        "rename_symlink_or_submodule_changed": False,
    }


def _supplement_target_delta(*, target_commit: str) -> dict[str, Any]:
    commit_chain = (SUPPLEMENT_REQUIRED_FIRST_COMMIT, target_commit)
    expected_parents = (
        SUPPLEMENT_SOURCE_TARGET_COMMIT,
        SUPPLEMENT_REQUIRED_FIRST_COMMIT,
    )
    for commit, expected_parent in zip(
        commit_chain,
        expected_parents,
        strict=True,
    ):
        parents = _git("rev-list", "--parents", "-n", "1", commit).split()
        if parents != [commit, expected_parent]:
            raise ValueError(
                "supplement target is not the exact two-commit remediation chain"
            )

    segment_allowlists = (
        SUPPLEMENT_FIRST_ALLOWED_PATHS,
        SUPPLEMENT_ALLOWED_PATHS,
    )
    commit_path_changes: list[dict[str, Any]] = []
    previous = SUPPLEMENT_SOURCE_TARGET_COMMIT
    for commit, allowed_paths in zip(
        commit_chain,
        segment_allowlists,
        strict=True,
    ):
        status_lines = tuple(
            line
            for line in _git(
                "diff",
                "--name-status",
                "--no-renames",
                previous,
                commit,
            ).splitlines()
            if line
        )
        allowed = set(allowed_paths)
        segment_paths: list[str] = []
        segment_transitions: list[dict[str, str]] = []
        for line in status_lines:
            status, separator, path = line.partition("\t")
            if separator != "\t" or status != "M" or path not in allowed:
                raise ValueError(
                    f"supplement target contains a forbidden change: {line}"
                )
            source_entry = _git("ls-tree", previous, "--", path).split()
            target_entry = _git("ls-tree", commit, "--", path).split()
            if (
                len(source_entry) < 3
                or len(target_entry) < 3
                or source_entry[0] != target_entry[0]
                or source_entry[0] not in {"100644", "100755"}
                or source_entry[1] != "blob"
                or target_entry[1] != "blob"
            ):
                raise ValueError(
                    "supplement target changed the file mode or object type: "
                    + path
                )
            segment_paths.append(path)
            segment_transitions.append(
                {
                    "path": path,
                    "mode": source_entry[0],
                    "source_blob": source_entry[2],
                    "target_blob": target_entry[2],
                }
            )
        if set(segment_paths) != allowed or len(segment_paths) != len(allowed):
            raise ValueError(
                "supplement target must change every bounded remediation file"
            )
        segment_diff = subprocess.run(
            ["git", "diff", "--binary", "--no-renames", previous, commit],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
        commit_path_changes.append(
            {
                "commit": commit,
                "parent": previous,
                "change_statuses": list(status_lines),
                "blob_transitions": segment_transitions,
                "allowed_paths": list(allowed_paths),
                "diff_sha256": hashlib.sha256(segment_diff).hexdigest(),
            }
        )
        previous = commit

    cumulative_allowed_paths = tuple(
        dict.fromkeys(
            (*SUPPLEMENT_FIRST_ALLOWED_PATHS, *SUPPLEMENT_ALLOWED_PATHS)
        )
    )
    status_lines = tuple(
        line
        for line in _git(
            "diff",
            "--name-status",
            "--no-renames",
            SUPPLEMENT_SOURCE_TARGET_COMMIT,
            target_commit,
        ).splitlines()
        if line
    )
    changed_paths: list[str] = []
    blob_transitions: list[dict[str, str]] = []
    for line in status_lines:
        status, separator, path = line.partition("\t")
        if (
            separator != "\t"
            or status != "M"
            or path not in cumulative_allowed_paths
        ):
            raise ValueError(
                f"supplement target contains a forbidden cumulative change: {line}"
            )
        source_entry = _git(
            "ls-tree", SUPPLEMENT_SOURCE_TARGET_COMMIT, "--", path
        ).split()
        target_entry = _git("ls-tree", target_commit, "--", path).split()
        if (
            len(source_entry) < 3
            or len(target_entry) < 3
            or source_entry[0] != target_entry[0]
            or source_entry[0] not in {"100644", "100755"}
            or source_entry[1] != "blob"
            or target_entry[1] != "blob"
        ):
            raise ValueError(
                "supplement target changed the cumulative mode or object type: "
                + path
            )
        changed_paths.append(path)
        blob_transitions.append(
            {
                "path": path,
                "mode": source_entry[0],
                "source_blob": source_entry[2],
                "target_blob": target_entry[2],
            }
        )
    if set(changed_paths) != set(cumulative_allowed_paths):
        raise ValueError("supplement cumulative remediation delta is incomplete")
    diff = subprocess.run(
        [
            "git",
            "diff",
            "--binary",
            "--no-renames",
            SUPPLEMENT_SOURCE_TARGET_COMMIT,
            target_commit,
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    allowlist_payload = json.dumps(
        cumulative_allowed_paths,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return {
        "source_target_commit": SUPPLEMENT_SOURCE_TARGET_COMMIT,
        "source_target_tree": _git(
            "rev-parse",
            f"{SUPPLEMENT_SOURCE_TARGET_COMMIT}^{{tree}}",
        ),
        "target_commit": target_commit,
        "target_tree": _git("rev-parse", f"{target_commit}^{{tree}}"),
        "direct_single_parent": False,
        "linear_single_parent_chain": True,
        "required_first_commit": SUPPLEMENT_REQUIRED_FIRST_COMMIT,
        "commit_count": len(commit_chain),
        "commit_chain": list(commit_chain),
        "commit_path_changes": commit_path_changes,
        "changed_paths": changed_paths,
        "change_statuses": list(status_lines),
        "blob_transitions": blob_transitions,
        "allowed_paths": list(cumulative_allowed_paths),
        "allowlist_sha256": hashlib.sha256(allowlist_payload).hexdigest(),
        "diff_sha256": hashlib.sha256(diff).hexdigest(),
        "production_or_configuration_changed": True,
        "bounded_production_change": True,
        "rename_symlink_or_submodule_changed": False,
    }


def _validate_resume_source(
    *,
    source_receipt: Path,
    source_receipt_sha256: str,
    target_commit: str,
    bun: str,
) -> tuple[Mapping[str, Any], dict[str, Any], dict[str, Mapping[str, Any]]]:
    expected_sha = source_receipt_sha256.strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ValueError("resume source receipt requires a full external SHA-256 anchor")
    source_receipt = source_receipt.resolve()
    try:
        source_receipt.relative_to(ROOT)
    except ValueError as error:
        raise ValueError("resume source receipt must be inside the Zyra workspace") from error
    actual_sha = _sha256(source_receipt)
    if not hmac.compare_digest(actual_sha, expected_sha):
        raise ValueError("resume source receipt SHA-256 does not match its anchor")
    value = _load_json(source_receipt)
    if (
        value.get("schema") != "zyra.phase2-final-regression/v1"
        or value.get("ready") is not False
        or not _embedded_digest_ready(value, "receipt_digest")
    ):
        raise ValueError("resume source is not the immutable failed v1 receipt")
    source_target = str(value.get("target_commit") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", source_target):
        raise ValueError("resume source target is not a full commit")
    delta = _resume_target_delta(
        source_target=source_target,
        target_commit=target_commit,
    )
    commands = value.get("commands")
    if not isinstance(commands, Sequence) or isinstance(commands, (str, bytes)):
        raise ValueError("resume source command list is invalid")
    command_values = tuple(item for item in commands if isinstance(item, Mapping))
    if len(command_values) != len(commands):
        raise ValueError("resume source contains a non-object command")
    command_ids = tuple(str(item.get("command_id") or "") for item in command_values)
    if command_ids != FINAL_REGRESSION_COMMAND_IDS:
        raise ValueError("resume source command order or coverage is invalid")
    failed = tuple(item for item in command_values if item.get("ready") is not True)
    if (
        len(failed) != 1
        or failed[0].get("command_id") != "loopx-cross-version-restart"
        or int(failed[0].get("returncode") or 0) == 0
        or int(value.get("passed_count") or 0) != 13
        or int(value.get("failed_count") or 0) != 1
    ):
        raise ValueError("resume source must have exactly the historical LoopX invocation failure")
    expected_source = dict(
        (command_id, argv)
        for command_id, argv, _timeout in _command_specs(
            output_root=source_receipt.parent,
            python=sys.executable,
            bun=bun,
            loopx_base_checkout=ROOT,
            target_commit=source_target,
        )
    )
    inherited: dict[str, Mapping[str, Any]] = {}
    for item in command_values:
        command_id = str(item["command_id"])
        argv = tuple(str(part) for part in item.get("argv") or ())
        expected_argv = expected_source[command_id]
        if command_id == "loopx-cross-version-restart":
            expected_argv = expected_argv[:2]
        if argv != expected_argv or Path(str(item.get("cwd") or "")).resolve() != ROOT:
            raise ValueError(f"resume source command provenance mismatch: {command_id}")
        for field in ("stdout", "stderr"):
            member = _receipt_member(source_receipt.parent, item.get(field))
            if _sha256(member) != item.get(f"{field}_sha256"):
                raise ValueError(f"resume source log digest mismatch: {command_id}:{field}")
        if command_id != "loopx-cross-version-restart" and (
            item.get("ready") is not True or int(item.get("returncode") or 0) != 0
        ):
            raise ValueError(f"resume source attempted to inherit a failed gate: {command_id}")
        if command_id not in RESUME_RERUN_GATE_IDS:
            inherited[command_id] = item
    failed_stderr = _receipt_member(
        source_receipt.parent,
        failed[0].get("stderr"),
    ).read_text(encoding="utf-8", errors="replace")
    if "--base-checkout, --target-checkout, --workspace and --output are required" not in failed_stderr:
        raise ValueError("resume source failure is not the bounded missing-argument defect")
    policy = value.get("python_test_policy")
    if (
        not isinstance(policy, Mapping)
        or policy.get("path") != PYTHON_TEST_POLICY_PATH.relative_to(ROOT).as_posix()
        or policy.get("sha256") != _sha256(PYTHON_TEST_POLICY_PATH)
        or value.get("p2_base_commit") != P2_BASE_COMMIT
    ):
        raise ValueError("resume source Python test policy changed")
    explicit_environment = value.get("explicit_environment")
    expected_environment = {
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "BUN_INSTALL_CACHE_DIR": str(source_receipt.parent / "cache" / "bun"),
        "PIP_CACHE_DIR": str(source_receipt.parent / "cache" / "pip"),
        "UV_CACHE_DIR": str(source_receipt.parent / "cache" / "uv"),
        "ZYRA_STATE_ROOT": str(source_receipt.parent / "state"),
        "PYTHONPATH": _source_python_path(),
    }
    if not isinstance(explicit_environment, Mapping) or dict(
        explicit_environment
    ) != expected_environment:
        raise ValueError("resume source explicit environment changed")
    source_tree = str(delta["source_target_tree"])
    before = value.get("worktree_boundary_before")
    after = value.get("worktree_boundary_after")
    if (
        not isinstance(before, Mapping)
        or not isinstance(after, Mapping)
        or not _boundary_ready(before, target=source_target, target_tree=source_tree)
        or not _boundary_ready(after, target=source_target, target_tree=source_tree)
    ):
        raise ValueError("resume source clean boundaries are invalid")
    return value, delta, inherited


def _validate_supplement_source(
    *,
    source_receipt: Path,
    source_receipt_sha256: str,
    loopx_base_checkout: Path,
    bun: str,
) -> tuple[Mapping[str, Any], dict[str, Mapping[str, Any]]]:
    expected_sha = source_receipt_sha256.strip().casefold()
    if expected_sha != SUPPLEMENT_SOURCE_RECEIPT_SHA256:
        raise ValueError("supplement source receipt is not the frozen v2 anchor")
    source_receipt = source_receipt.resolve()
    try:
        source_receipt.relative_to(ROOT)
    except ValueError as error:
        raise ValueError(
            "supplement source receipt must be inside the Zyra workspace"
        ) from error
    if not hmac.compare_digest(_sha256(source_receipt), expected_sha):
        raise ValueError("supplement source receipt SHA-256 does not match its anchor")
    value = _load_json(source_receipt)
    target_tree = _git(
        "rev-parse",
        f"{SUPPLEMENT_SOURCE_TARGET_COMMIT}^{{tree}}",
    )
    logical_value = value.get("logical_gates")
    logical = (
        tuple(item for item in logical_value if isinstance(item, Mapping))
        if isinstance(logical_value, Sequence)
        and not isinstance(logical_value, (str, bytes))
        else ()
    )
    if (
        value.get("schema") != "zyra.phase2-final-regression-resume/v1"
        or value.get("ready") is not True
        or value.get("target_commit") != SUPPLEMENT_SOURCE_TARGET_COMMIT
        or value.get("target_tree") != target_tree
        or not _embedded_digest_ready(value, "receipt_digest")
        or int(value.get("passed_count") or 0) != len(FINAL_REGRESSION_COMMAND_IDS)
        or int(value.get("failed_count") or 0) != 0
        or value.get("duplicate_heavy_execution_avoided") is not True
        or tuple(str(item.get("command_id") or "") for item in logical)
        != FINAL_REGRESSION_COMMAND_IDS
        or not all(item.get("ready") is True for item in logical)
    ):
        raise ValueError("supplement source v2 receipt integrity is invalid")
    inner_path = Path(str(value.get("source_receipt") or ""))
    inner, expected_delta, inherited = _validate_resume_source(
        source_receipt=inner_path,
        source_receipt_sha256=str(value.get("source_receipt_sha256") or ""),
        target_commit=SUPPLEMENT_SOURCE_TARGET_COMMIT,
        bun=bun,
    )
    if (
        value.get("source_receipt_digest") != inner.get("receipt_digest")
        or value.get("source_target_commit") != inner.get("target_commit")
        or value.get("target_delta") != expected_delta
    ):
        raise ValueError("supplement source v2 inheritance binding is invalid")
    source_loopx_base = Path(str(value.get("loopx_base_checkout") or ".")).resolve()
    if source_loopx_base != loopx_base_checkout.resolve():
        raise ValueError("supplement source LoopX base checkout changed")
    source_boundary_before = value.get("worktree_boundary_before")
    source_boundary_after = value.get("worktree_boundary_after")
    if (
        not isinstance(source_boundary_before, Mapping)
        or not isinstance(source_boundary_after, Mapping)
        or not _boundary_ready(
            source_boundary_before,
            target=SUPPLEMENT_SOURCE_TARGET_COMMIT,
            target_tree=target_tree,
        )
        or not _boundary_ready(
            source_boundary_after,
            target=SUPPLEMENT_SOURCE_TARGET_COMMIT,
            target_tree=target_tree,
        )
    ):
        raise ValueError("supplement source v2 clean boundaries are invalid")
    reruns_value = value.get("rerun_commands")
    reruns = (
        tuple(item for item in reruns_value if isinstance(item, Mapping))
        if isinstance(reruns_value, Sequence)
        and not isinstance(reruns_value, (str, bytes))
        else ()
    )
    expected_specs = {
        command_id: argv
        for command_id, argv, _timeout in _resume_command_specs(
            output_root=source_receipt.parent,
            target_commit=SUPPLEMENT_SOURCE_TARGET_COMMIT,
            loopx_base_checkout=loopx_base_checkout,
        )
    }
    if tuple(str(item.get("command_id") or "") for item in reruns) != tuple(
        expected_specs
    ):
        raise ValueError("supplement source v2 rerun coverage is invalid")
    rerun_by_id: dict[str, Mapping[str, Any]] = {}
    for item in reruns:
        command_id = str(item.get("command_id") or "")
        argv = tuple(str(part) for part in item.get("argv") or ())
        if (
            argv != expected_specs[command_id]
            or Path(str(item.get("cwd") or "")).resolve() != ROOT
            or item.get("ready") is not True
            or int(item.get("returncode") or 0) != 0
        ):
            raise ValueError(
                f"supplement source v2 rerun provenance is invalid: {command_id}"
            )
        for field in ("stdout", "stderr"):
            member = _receipt_member(source_receipt.parent, item.get(field))
            if _sha256(member) != item.get(f"{field}_sha256"):
                raise ValueError(
                    f"supplement source v2 log digest mismatch: {command_id}:{field}"
                )
        rerun_by_id[command_id] = item
    logical_by_id = {str(item.get("command_id") or ""): item for item in logical}
    for command_id in FINAL_REGRESSION_COMMAND_IDS:
        item = logical_by_id[command_id]
        if command_id in RESUME_RERUN_GATE_IDS:
            rerun = rerun_by_id[command_id]
            if (
                item.get("source") != "rerun"
                or item.get("stdout_sha256") != rerun.get("stdout_sha256")
                or item.get("stderr_sha256") != rerun.get("stderr_sha256")
            ):
                raise ValueError(
                    f"supplement source v2 logical rerun mismatch: {command_id}"
                )
        else:
            parent = inherited[command_id]
            if (
                item.get("source") != "inherited"
                or item.get("source_target_commit") != inner.get("target_commit")
                or item.get("source_receipt_sha256")
                != value.get("source_receipt_sha256")
                or item.get("source_stdout_sha256") != parent.get("stdout_sha256")
                or item.get("source_stderr_sha256") != parent.get("stderr_sha256")
            ):
                raise ValueError(
                    f"supplement source v2 logical inheritance mismatch: {command_id}"
                )
    loopx_path = _receipt_member(source_receipt.parent, value.get("loopx_result"))
    if (
        _sha256(loopx_path) != value.get("loopx_result_sha256")
        or value.get("loopx_result_ready") is not True
        or not _loopx_resume_result_ready(
            loopx_path,
            target_commit=SUPPLEMENT_SOURCE_TARGET_COMMIT,
            loopx_base_checkout=loopx_base_checkout,
        )
    ):
        raise ValueError("supplement source v2 LoopX result is invalid")
    return value, logical_by_id


def _resume_command_specs(
    *,
    output_root: Path,
    target_commit: str,
    loopx_base_checkout: Path,
) -> tuple[tuple[str, tuple[str, ...], float], ...]:
    full = {
        command_id: (argv, timeout)
        for command_id, argv, timeout in _command_specs(
            output_root=output_root,
            python=sys.executable,
            bun=_resolve_bun(),
            loopx_base_checkout=loopx_base_checkout,
            target_commit=target_commit,
        )
    }
    remediation = (
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        str(output_root / "remediation-pytest"),
        *(str(ROOT / relative) for relative in RESUME_REMEDIATION_TESTS),
    )
    return (
        ("remediation-targeted-python", remediation, 600),
        *(
            (command_id, full[command_id][0], full[command_id][1])
            for command_id in RESUME_RERUN_GATE_IDS
        ),
    )


def _supplement_command_specs(
    *,
    output_root: Path,
    target_commit: str,
    loopx_base_checkout: Path,
) -> tuple[tuple[str, tuple[str, ...], float], ...]:
    full = {
        command_id: (argv, timeout)
        for command_id, argv, timeout in _command_specs(
            output_root=output_root,
            python=sys.executable,
            bun=_resolve_bun(),
            loopx_base_checkout=loopx_base_checkout,
            target_commit=target_commit,
        )
    }
    remediation = (
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        str(output_root / "supplement-pytest"),
        *(str(ROOT / relative) for relative in SUPPLEMENT_REMEDIATION_TESTS),
    )
    return (
        ("supplement-targeted-python", remediation, 900),
        *(
            (command_id, full[command_id][0], full[command_id][1])
            for command_id in SUPPLEMENT_RERUN_GATE_IDS
        ),
    )


def _run_commands(
    *,
    specs: Sequence[tuple[str, tuple[str, ...], float]],
    output_root: Path,
    environment: Mapping[str, str],
) -> list[dict[str, Any]]:
    logs = output_root / "logs"
    logs.mkdir()
    commands: list[dict[str, Any]] = []
    for command_id, command, timeout in specs:
        started = time.monotonic()
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        stdout_path = logs / f"{command_id}.stdout.log"
        stderr_path = logs / f"{command_id}.stderr.log"
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        commands.append(
            {
                "command_id": command_id,
                "argv": list(command),
                "cwd": str(ROOT),
                "returncode": completed.returncode,
                "ready": completed.returncode == 0,
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                "stdout": stdout_path.relative_to(output_root).as_posix(),
                "stdout_sha256": _sha256(stdout_path),
                "stderr": stderr_path.relative_to(output_root).as_posix(),
                "stderr_sha256": _sha256(stderr_path),
                "stdout_tail": completed.stdout[-4096:],
                "stderr_tail": completed.stderr[-4096:],
            }
        )
        if completed.returncode:
            break
    return commands


def _loopx_resume_result_ready(
    path: Path,
    *,
    target_commit: str,
    loopx_base_checkout: Path,
) -> bool:
    if not path.is_file():
        return False
    value = _load_json(path)
    invariants = value.get("invariants")
    restarts = value.get("target_restarts")
    baseline = value.get("baseline")
    phases = (
        (baseline, loopx_base_checkout.resolve()),
        *((item, ROOT) for item in restarts),
    ) if (
        isinstance(baseline, Mapping)
        and isinstance(restarts, Sequence)
        and not isinstance(restarts, (str, bytes))
        and len(restarts) == 2
        and all(isinstance(item, Mapping) for item in restarts)
    ) else ()
    isolation_paths: list[Path] = []
    isolation_ready = len(phases) == 3
    for phase, expected_checkout in phases:
        isolation = phase.get("deployment_state_isolation")
        if not isinstance(isolation, Mapping):
            isolation_ready = False
            continue
        isolation_path = Path(str(isolation.get("path") or ".")).resolve()
        expected_checkout = expected_checkout.resolve()
        isolation_ready = isolation_ready and (
            isolation.get("strategy") == "owned_checkout_temporary_directory"
            and isolation.get("cleaned") is True
            and Path(str(isolation.get("checkout") or ".")).resolve()
            == expected_checkout
            and expected_checkout in isolation_path.parents
            and not isolation_path.exists()
        )
        isolation_paths.append(isolation_path)
    isolation_ready = isolation_ready and len(isolation_paths) == 3 and len(
        {os.path.normcase(str(item)) for item in isolation_paths}
    ) == 3
    return (
        value.get("schema") == "zyra.loopx-cross-version-upgrade/v1"
        and value.get("ready") is True
        and value.get("base_commit") == LOOPX_CROSS_VERSION_BASE_COMMIT
        and value.get("target_commit") == target_commit
        and isinstance(restarts, Sequence)
        and not isinstance(restarts, (str, bytes))
        and len(restarts) == 2
        and all(
            isinstance(item, Mapping) and item.get("commit") == target_commit
            for item in restarts
        )
        and isinstance(invariants, Mapping)
        and invariants.get("semantic_state_preserved") is True
        and invariants.get("cursor_monotonic") is True
        and invariants.get("duplicate_claim") is False
        and invariants.get("duplicate_spend") is False
        and invariants.get("duplicate_interaction") is False
        and invariants.get("duplicate_canonical_commit") is False
        and invariants.get("historical_install_preserved_and_ignored") is True
        and invariants.get("new_install_or_extraction") is False
        and invariants.get("independent_target_restart_count") == 2
        and invariants.get("checkout_runtime_state_cleaned") is True
        and invariants.get("checkout_runtime_state_paths_distinct") is True
        and isolation_ready
    )


def resume_regression(
    *,
    target_commit: str,
    output_root: Path,
    loopx_base_checkout: Path,
    source_receipt: Path,
    source_receipt_sha256: str,
) -> dict[str, Any]:
    if _head() != target_commit:
        raise ValueError("resume target commit does not match HEAD")
    boundary_before = require_worktree_boundary(ROOT, expected_head=target_commit)
    output_root = output_root.resolve()
    loopx_base_checkout = loopx_base_checkout.resolve()
    _require_disjoint_roots(output_root, loopx_base_checkout)
    loopx_base_boundary_before = _loopx_base_boundary(loopx_base_checkout)
    if loopx_base_boundary_before["ready"] is not True:
        raise ValueError("LoopX resume base checkout is not frozen and clean")
    bun = _resolve_bun()
    source, delta, inherited = _validate_resume_source(
        source_receipt=source_receipt,
        source_receipt_sha256=source_receipt_sha256,
        target_commit=target_commit,
        bun=bun,
    )
    output_root.mkdir(parents=True, exist_ok=False)
    environment = {
        **os.environ,
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "BUN_INSTALL_CACHE_DIR": str(output_root / "cache" / "bun"),
        "PIP_CACHE_DIR": str(output_root / "cache" / "pip"),
        "UV_CACHE_DIR": str(output_root / "cache" / "uv"),
        "ZYRA_STATE_ROOT": str(output_root / "state"),
        "PYTHONPATH": _source_python_path(),
    }
    specs = _resume_command_specs(
        output_root=output_root,
        target_commit=target_commit,
        loopx_base_checkout=loopx_base_checkout,
    )
    rerun_commands = _run_commands(
        specs=specs,
        output_root=output_root,
        environment=environment,
    )
    rerun_by_id = {str(item["command_id"]): item for item in rerun_commands}
    logical_gates: list[dict[str, Any]] = []
    for command_id in FINAL_REGRESSION_COMMAND_IDS:
        if command_id in RESUME_RERUN_GATE_IDS:
            item = rerun_by_id.get(command_id, {})
            logical_gates.append(
                {
                    "command_id": command_id,
                    "source": "rerun",
                    "ready": item.get("ready") is True,
                    "rerun_command_id": command_id,
                    "stdout_sha256": item.get("stdout_sha256"),
                    "stderr_sha256": item.get("stderr_sha256"),
                }
            )
        else:
            item = inherited[command_id]
            logical_gates.append(
                {
                    "command_id": command_id,
                    "source": "inherited",
                    "ready": True,
                    "source_target_commit": source.get("target_commit"),
                    "source_receipt_sha256": source_receipt_sha256.casefold(),
                    "source_stdout_sha256": item.get("stdout_sha256"),
                    "source_stderr_sha256": item.get("stderr_sha256"),
                }
            )
    boundary_after = inspect_worktree(ROOT, expected_head=target_commit)
    loopx_base_boundary_after = _loopx_base_boundary(loopx_base_checkout)
    target_tree = _git("rev-parse", f"{target_commit}^{{tree}}")
    loopx_output = output_root / "loopx-cross-version-upgrade.json"
    loopx_ready = _loopx_resume_result_ready(
        loopx_output,
        target_commit=target_commit,
        loopx_base_checkout=loopx_base_checkout,
    )
    all_reruns_passed = len(rerun_commands) == len(specs) and all(
        item.get("ready") is True for item in rerun_commands
    )
    ready = (
        all_reruns_passed
        and loopx_ready
        and loopx_base_boundary_after == loopx_base_boundary_before
        and loopx_base_boundary_after["ready"] is True
        and all(item["ready"] for item in logical_gates)
        and _boundary_ready(
            boundary_after,
            target=target_commit,
            target_tree=target_tree,
        )
    )
    value: dict[str, Any] = {
        "schema": "zyra.phase2-final-regression-resume/v1",
        "slice_id": "P2-S06-03",
        "ready": ready,
        "target_commit": target_commit,
        "target_tree": target_tree,
        "p2_base_commit": P2_BASE_COMMIT,
        "source_receipt": str(source_receipt.resolve()),
        "source_receipt_sha256": source_receipt_sha256.casefold(),
        "source_receipt_digest": source.get("receipt_digest"),
        "source_target_commit": source.get("target_commit"),
        "source_target_tree": delta["source_target_tree"],
        "target_delta": delta,
        "loopx_cross_version_base_commit": LOOPX_CROSS_VERSION_BASE_COMMIT,
        "loopx_base_checkout": str(loopx_base_checkout),
        "loopx_base_boundary_before": loopx_base_boundary_before,
        "loopx_base_boundary_after": loopx_base_boundary_after,
        "loopx_result": loopx_output.relative_to(output_root).as_posix(),
        "loopx_result_sha256": _sha256(loopx_output) if loopx_output.is_file() else "",
        "loopx_result_ready": loopx_ready,
        "python_test_policy": {
            "path": PYTHON_TEST_POLICY_PATH.relative_to(ROOT).as_posix(),
            "sha256": _sha256(PYTHON_TEST_POLICY_PATH),
        },
        "logical_gates": logical_gates,
        "rerun_commands": rerun_commands,
        "remediation_command_ids": ["remediation-targeted-python"],
        "passed_count": sum(item["ready"] for item in logical_gates),
        "failed_count": sum(not item["ready"] for item in logical_gates),
        "worktree_boundary_before": boundary_before,
        "worktree_boundary_after": boundary_after,
        "explicit_environment": {
            key: environment[key]
            for key in (
                "PYTHONNOUSERSITE",
                "PYTHONDONTWRITEBYTECODE",
                "PIP_DISABLE_PIP_VERSION_CHECK",
                "BUN_INSTALL_CACHE_DIR",
                "PIP_CACHE_DIR",
                "UV_CACHE_DIR",
                "ZYRA_STATE_ROOT",
                "PYTHONPATH",
            )
        },
        "duplicate_heavy_execution_avoided": True,
    }
    value["receipt_digest"] = _canonical_digest(value, "receipt_digest")
    (output_root / "final-regression-resume.json").write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return value


def supplement_regression(
    *,
    target_commit: str,
    output_root: Path,
    loopx_base_checkout: Path,
    source_receipt: Path,
    source_receipt_sha256: str,
) -> dict[str, Any]:
    if _head() != target_commit:
        raise ValueError("supplement target commit does not match HEAD")
    boundary_before = require_worktree_boundary(ROOT, expected_head=target_commit)
    output_root = output_root.resolve()
    loopx_base_checkout = loopx_base_checkout.resolve()
    _require_disjoint_roots(output_root, loopx_base_checkout)
    loopx_base_boundary_before = _loopx_base_boundary(loopx_base_checkout)
    if loopx_base_boundary_before["ready"] is not True:
        raise ValueError("LoopX supplement base checkout is not frozen and clean")
    bun = _resolve_bun()
    source, source_logical = _validate_supplement_source(
        source_receipt=source_receipt,
        source_receipt_sha256=source_receipt_sha256,
        loopx_base_checkout=loopx_base_checkout,
        bun=bun,
    )
    delta = _supplement_target_delta(target_commit=target_commit)
    output_root.mkdir(parents=True, exist_ok=False)
    environment = {
        **os.environ,
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "BUN_INSTALL_CACHE_DIR": str(output_root / "cache" / "bun"),
        "PIP_CACHE_DIR": str(output_root / "cache" / "pip"),
        "UV_CACHE_DIR": str(output_root / "cache" / "uv"),
        "ZYRA_STATE_ROOT": str(output_root / "state"),
        "PYTHONPATH": _source_python_path(),
    }
    specs = _supplement_command_specs(
        output_root=output_root,
        target_commit=target_commit,
        loopx_base_checkout=loopx_base_checkout,
    )
    rerun_commands = _run_commands(
        specs=specs,
        output_root=output_root,
        environment=environment,
    )
    rerun_by_id = {str(item["command_id"]): item for item in rerun_commands}
    logical_gates: list[dict[str, Any]] = []
    for command_id in FINAL_REGRESSION_COMMAND_IDS:
        if command_id in SUPPLEMENT_RERUN_GATE_IDS:
            item = rerun_by_id.get(command_id, {})
            logical_gates.append(
                {
                    "command_id": command_id,
                    "source": "rerun",
                    "ready": item.get("ready") is True,
                    "rerun_command_id": command_id,
                    "stdout_sha256": item.get("stdout_sha256"),
                    "stderr_sha256": item.get("stderr_sha256"),
                }
            )
        else:
            item = source_logical[command_id]
            logical_gates.append(
                {
                    "command_id": command_id,
                    "source": "inherited_v2",
                    "ready": True,
                    "source_target_commit": SUPPLEMENT_SOURCE_TARGET_COMMIT,
                    "source_receipt_sha256": source_receipt_sha256.casefold(),
                    "source_gate_digest": _canonical_digest(
                        item,
                        "source_gate_digest",
                    ),
                }
            )
    boundary_after = inspect_worktree(ROOT, expected_head=target_commit)
    loopx_base_boundary_after = _loopx_base_boundary(loopx_base_checkout)
    target_tree = _git("rev-parse", f"{target_commit}^{{tree}}")
    all_reruns_passed = len(rerun_commands) == len(specs) and all(
        item.get("ready") is True for item in rerun_commands
    )
    ready = (
        all_reruns_passed
        and loopx_base_boundary_after == loopx_base_boundary_before
        and loopx_base_boundary_after["ready"] is True
        and all(item["ready"] for item in logical_gates)
        and _boundary_ready(
            boundary_after,
            target=target_commit,
            target_tree=target_tree,
        )
    )
    value: dict[str, Any] = {
        "schema": "zyra.phase2-final-regression-supplement/v1",
        "slice_id": "P2-S06-03",
        "ready": ready,
        "target_commit": target_commit,
        "target_tree": target_tree,
        "p2_base_commit": P2_BASE_COMMIT,
        "source_receipt": str(source_receipt.resolve()),
        "source_receipt_sha256": source_receipt_sha256.casefold(),
        "source_receipt_digest": source.get("receipt_digest"),
        "source_target_commit": SUPPLEMENT_SOURCE_TARGET_COMMIT,
        "source_target_tree": delta["source_target_tree"],
        "target_delta": delta,
        "loopx_cross_version_base_commit": LOOPX_CROSS_VERSION_BASE_COMMIT,
        "loopx_base_checkout": str(loopx_base_checkout),
        "loopx_base_boundary_before": loopx_base_boundary_before,
        "loopx_base_boundary_after": loopx_base_boundary_after,
        "loopx_result_ready": source.get("loopx_result_ready") is True,
        "source_loopx_result_sha256": source.get("loopx_result_sha256"),
        "python_test_policy": {
            "path": PYTHON_TEST_POLICY_PATH.relative_to(ROOT).as_posix(),
            "sha256": _sha256(PYTHON_TEST_POLICY_PATH),
        },
        "logical_gates": logical_gates,
        "rerun_commands": rerun_commands,
        "remediation_command_ids": ["supplement-targeted-python"],
        "passed_count": sum(item["ready"] for item in logical_gates),
        "failed_count": sum(not item["ready"] for item in logical_gates),
        "worktree_boundary_before": boundary_before,
        "worktree_boundary_after": boundary_after,
        "explicit_environment": {
            key: environment[key]
            for key in (
                "PYTHONNOUSERSITE",
                "PYTHONDONTWRITEBYTECODE",
                "PIP_DISABLE_PIP_VERSION_CHECK",
                "BUN_INSTALL_CACHE_DIR",
                "PIP_CACHE_DIR",
                "UV_CACHE_DIR",
                "ZYRA_STATE_ROOT",
                "PYTHONPATH",
            )
        },
        "duplicate_heavy_execution_avoided": True,
    }
    value["receipt_digest"] = _canonical_digest(value, "receipt_digest")
    (output_root / "final-regression-supplement.json").write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return value


def run_regression(
    *,
    target_commit: str,
    output_root: Path,
    loopx_base_checkout: Path,
) -> dict[str, Any]:
    observed = _head()
    if observed != target_commit:
        raise ValueError(
            f"target commit mismatch: expected {target_commit}, observed {observed}"
        )
    boundary_before = require_worktree_boundary(
        ROOT,
        expected_head=target_commit,
    )
    output_root = output_root.resolve()
    loopx_base_checkout = loopx_base_checkout.resolve()
    _require_disjoint_roots(output_root, loopx_base_checkout)
    loopx_base_boundary_before = _loopx_base_boundary(loopx_base_checkout)
    if loopx_base_boundary_before["ready"] is not True:
        raise ValueError("LoopX cross-version base checkout is not frozen and clean")
    output_root.mkdir(parents=True, exist_ok=False)
    logs = output_root / "logs"
    logs.mkdir()
    bun = _resolve_bun()
    environment = {
        **os.environ,
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "BUN_INSTALL_CACHE_DIR": str(output_root / "cache" / "bun"),
        "PIP_CACHE_DIR": str(output_root / "cache" / "pip"),
        "UV_CACHE_DIR": str(output_root / "cache" / "uv"),
        "ZYRA_STATE_ROOT": str(output_root / "state"),
        "PYTHONPATH": _source_python_path(),
    }
    commands: list[dict[str, Any]] = []
    for command_id, command, timeout in _command_specs(
        output_root=output_root,
        python=sys.executable,
        bun=bun,
        loopx_base_checkout=loopx_base_checkout,
        target_commit=target_commit,
    ):
        started = time.monotonic()
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        stdout_path = logs / f"{command_id}.stdout.log"
        stderr_path = logs / f"{command_id}.stderr.log"
        stdout_path.write_text(completed.stdout, encoding="utf-8")
        stderr_path.write_text(completed.stderr, encoding="utf-8")
        commands.append(
            {
                "command_id": command_id,
                "argv": list(command),
                "cwd": str(ROOT),
                "returncode": completed.returncode,
                "ready": completed.returncode == 0,
                "duration_ms": round(
                    (time.monotonic() - started) * 1000,
                    3,
                ),
                "stdout": stdout_path.relative_to(output_root).as_posix(),
                "stdout_sha256": _sha256(stdout_path),
                "stderr": stderr_path.relative_to(output_root).as_posix(),
                "stderr_sha256": _sha256(stderr_path),
                "stdout_tail": completed.stdout[-4096:],
                "stderr_tail": completed.stderr[-4096:],
            }
        )
        if completed.returncode:
            break
    boundary_after = inspect_worktree(ROOT, expected_head=target_commit)
    loopx_base_boundary_after = _loopx_base_boundary(loopx_base_checkout)
    ready = (
        len(commands)
        == len(
            _command_specs(
                output_root=output_root,
                python=sys.executable,
                bun=bun,
                loopx_base_checkout=loopx_base_checkout,
                target_commit=target_commit,
            )
        )
        and all(item["ready"] for item in commands)
        and boundary_after["ready"]
        and loopx_base_boundary_after == loopx_base_boundary_before
        and loopx_base_boundary_after["ready"] is True
    )
    value: dict[str, Any] = {
        "schema": "zyra.phase2-final-regression/v1",
        "slice_id": "P2-S06-03",
        "ready": ready,
        "target_commit": target_commit,
        "p2_base_commit": P2_BASE_COMMIT,
        "loopx_cross_version_base_commit": LOOPX_CROSS_VERSION_BASE_COMMIT,
        "loopx_base_checkout": str(loopx_base_checkout),
        "loopx_base_boundary_before": loopx_base_boundary_before,
        "loopx_base_boundary_after": loopx_base_boundary_after,
        "python_test_policy": {
            "path": PYTHON_TEST_POLICY_PATH.relative_to(ROOT).as_posix(),
            "sha256": _sha256(PYTHON_TEST_POLICY_PATH),
            "debt_owner": _python_test_policy()["debt_owner"],
            "reason": _python_test_policy()["reason"],
            "ignored_file_count": len(_python_test_policy()["ignore_files"]),
            "deselected_nodeid_count": len(
                _python_test_policy()["deselect_nodeids"]
            ),
        },
        "typescript_runtime_test_roots": list(TYPESCRIPT_RUNTIME_TEST_ROOTS),
        "explicit_environment": {
            key: environment[key]
            for key in (
                "PYTHONNOUSERSITE",
                "PYTHONDONTWRITEBYTECODE",
                "PIP_DISABLE_PIP_VERSION_CHECK",
                "BUN_INSTALL_CACHE_DIR",
                "PIP_CACHE_DIR",
                "UV_CACHE_DIR",
                "ZYRA_STATE_ROOT",
                "PYTHONPATH",
            )
        },
        "commands": commands,
        "worktree_boundary_before": boundary_before,
        "worktree_boundary_after": boundary_after,
        "passed_count": sum(item["ready"] for item in commands),
        "failed_count": sum(not item["ready"] for item in commands),
    }
    unsigned = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    value["receipt_digest"] = hashlib.sha256(unsigned).hexdigest()
    receipt = output_root / "final-regression.json"
    receipt.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Phase 2 final target regression and freeze receipts."
    )
    parser.add_argument("--target-commit", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--loopx-base-checkout", required=True)
    parser.add_argument("--resume-receipt", default="")
    parser.add_argument("--resume-receipt-sha256", default="")
    parser.add_argument("--supplement-receipt", default="")
    parser.add_argument("--supplement-receipt-sha256", default="")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.resume_receipt and arguments.supplement_receipt:
            raise ValueError("resume and supplement receipts are mutually exclusive")
        if arguments.supplement_receipt:
            if arguments.resume_receipt_sha256:
                raise ValueError(
                    "--resume-receipt-sha256 requires --resume-receipt"
                )
            result = supplement_regression(
                target_commit=arguments.target_commit,
                output_root=ROOT / arguments.output_root,
                loopx_base_checkout=Path(arguments.loopx_base_checkout),
                source_receipt=Path(arguments.supplement_receipt),
                source_receipt_sha256=arguments.supplement_receipt_sha256,
            )
        elif arguments.resume_receipt:
            if arguments.supplement_receipt_sha256:
                raise ValueError(
                    "--supplement-receipt-sha256 requires --supplement-receipt"
                )
            result = resume_regression(
                target_commit=arguments.target_commit,
                output_root=ROOT / arguments.output_root,
                loopx_base_checkout=Path(arguments.loopx_base_checkout),
                source_receipt=Path(arguments.resume_receipt),
                source_receipt_sha256=arguments.resume_receipt_sha256,
            )
        else:
            if (
                arguments.resume_receipt_sha256
                or arguments.supplement_receipt_sha256
            ):
                raise ValueError(
                    "receipt SHA-256 requires its matching receipt option"
                )
            result = run_regression(
                target_commit=arguments.target_commit,
                output_root=ROOT / arguments.output_root,
                loopx_base_checkout=Path(arguments.loopx_base_checkout),
            )
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        WorktreeBoundaryError,
    ) as error:
        print(
            json.dumps(
                {
                    "schema": "zyra.phase2-final-regression-error/v1",
                    "ready": False,
                    "error": str(error),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(run())
