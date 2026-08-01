from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PRODUCTIZATION_ROOT = ROOT / "packages" / "productization"
if str(PRODUCTIZATION_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCTIZATION_ROOT))

from zyra_productization.release.phase2_freeze import inspect_release_archive
from zyra_productization.release.worktree import inspect_worktree


def _archive(
    path: Path,
    *,
    release_member: str = "zyra-release/packages/core/module.py",
    wheel_member: str = "zyra_core/module.py",
    sbom_path: str = "packages/core/module.py",
    source_commit: str = "a" * 40,
) -> Path:
    wheel_buffer = io.BytesIO()
    with zipfile.ZipFile(wheel_buffer, "w") as wheel:
        wheel.writestr(wheel_member, b"pass\n")
    sbom = json.dumps(
        {
            "bomFormat": "CycloneDX",
            "components": [{"name": "zyra", "path": sbom_path}],
        }
    ).encode()
    release_manifest = json.dumps(
        {
            "schema": "zyra.release-manifest/v1",
            "release_id": "unit-test",
            "source_commit": source_commit,
        }
    ).encode()
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in (
            (release_member, b"pass\n"),
            (
                "zyra-release/release/wheels/zyra.whl",
                wheel_buffer.getvalue(),
            ),
            ("zyra-release/release/sbom.cdx.json", sbom),
            ("zyra-release/release/manifest.json", release_manifest),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return path


def test_release_archive_audit_accepts_clean_source_wheel_and_sbom(
    tmp_path: Path,
) -> None:
    result = inspect_release_archive(_archive(tmp_path / "release.tar.gz"))

    assert result["ready"] is True
    assert result["source_archive_vendor_root_count"] == 0
    assert result["release_vendor_root_count"] == 0
    assert result["wheel_vendor_root_count"] == 0
    assert result["sbom_vendor_root_count"] == 0
    assert result["release_manifest"]["source_commit"] == "a" * 40


def test_release_archive_audit_rejects_vendor_roots_in_every_boundary(
    tmp_path: Path,
) -> None:
    result = inspect_release_archive(
        _archive(
            tmp_path / "release.tar.gz",
            release_member="zyra-release/vendor/source.py",
            wheel_member="vendor/runtime.py",
            sbom_path="vendor/component.py",
        )
    )

    assert result["ready"] is False
    assert result["release_vendor_root_count"] == 1
    assert result["wheel_vendor_root_count"] == 1
    assert result["sbom_vendor_root_count"] == 1


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_worktree_boundary_allows_generated_evidence_but_rejects_source(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Zyra Test")
    _git(tmp_path, "config", "user.email", "zyra@example.invalid")
    source = tmp_path / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "source.py")
    _git(tmp_path, "commit", "-m", "baseline")
    head = _git(tmp_path, "rev-parse", "HEAD")

    generated = tmp_path / "docs" / "evidence" / "receipt.json"
    generated.parent.mkdir(parents=True)
    generated.write_text("{}\n", encoding="utf-8")
    allowed = inspect_worktree(tmp_path, expected_head=head)
    assert allowed["ready"] is True
    assert allowed["ignored_generated_entry_count"] == 1

    source.write_text("VALUE = 2\n", encoding="utf-8")
    dirty = inspect_worktree(tmp_path, expected_head=head)
    assert dirty["ready"] is False
    assert dirty["tracked_dirty_entries"]


def test_worktree_boundary_rejects_untracked_source(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Zyra Test")
    _git(tmp_path, "config", "user.email", "zyra@example.invalid")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("tracked\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "baseline")
    head = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "unexpected.py").write_text("pass\n", encoding="utf-8")

    receipt = inspect_worktree(tmp_path, expected_head=head)
    assert receipt["ready"] is False
    assert receipt["unexpected_untracked_entries"] == ["?? unexpected.py"]
