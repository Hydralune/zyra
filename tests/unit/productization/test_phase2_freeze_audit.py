from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path

from zyra_productization.release.phase2_freeze import inspect_release_archive


def _archive(
    path: Path,
    *,
    release_member: str = "zyra-release/packages/core/module.py",
    wheel_member: str = "zyra_core/module.py",
    sbom_path: str = "packages/core/module.py",
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
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in (
            (release_member, b"pass\n"),
            (
                "zyra-release/release/wheels/zyra.whl",
                wheel_buffer.getvalue(),
            ),
            ("zyra-release/release/sbom.cdx.json", sbom),
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
