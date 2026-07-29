from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

from zyra_integrations.loopx.install import LoopXDoctor, LoopXPackageLock
from zyra_productization.release import ReleaseRuntime
from zyra_productization.release.bundle import ReleaseBundleBuilder


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_COMMIT = "09e99cdc5ed9cf3a935ccc327f7261110e6c7d1b"


def _copy_packaged_loopx(destination: Path) -> Path:
    lock = LoopXPackageLock.load(PROJECT_ROOT)
    lock_target = destination / "config" / "loopx" / "package-lock.json"
    lock_target.parent.mkdir(parents=True)
    shutil.copyfile(lock.path, lock_target)
    for artifact in lock.artifacts.values():
        source = lock.resolve_artifact(artifact)
        target = destination / artifact.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return destination


def test_release_deep_doctor_verifies_loopx_identity_cli_and_write_boundary() -> None:
    report = ReleaseRuntime(PROJECT_ROOT).doctor(
        deep=True,
        require_tools=False,
    )
    assert report["ready"] is True
    assert report["mode"] == "deep"
    loopx = next(
        item for item in report["checks"] if item["check"] == "loopx-pinned-runtime"
    )
    assert loopx["ready"] is True
    details = loopx["details"]
    assert details["version"] == "0.2.4"
    assert (
        details["source_commit"]
        == "8e79843704a40d8069a9cab4ede6edc6d29f671b"
    )
    assert {item["check"] for item in details["checks"]} >= {
        "package-identity",
        "offline-artifacts",
        "profile-boundary",
        "release-independence",
        "import-and-cli-entry",
    }


def test_loopx_doctor_fails_closed_for_wheel_and_manifest_tampering(
    tmp_path: Path,
) -> None:
    wheel_root = _copy_packaged_loopx(tmp_path / "wheel-tamper")
    wheel_lock = LoopXPackageLock.load(wheel_root)
    wheel = wheel_lock.resolve_artifact(wheel_lock.artifacts["wheel"])
    data = bytearray(wheel.read_bytes())
    data[len(data) // 2] ^= 0x01
    wheel.write_bytes(data)
    wheel_report = LoopXDoctor(wheel_root).run(deep=True)
    assert wheel_report["ready"] is False
    assert any(
        blocker["code"] == "loopx_artifact_tampered"
        for blocker in wheel_report["blockers"]
    )

    manifest_root = _copy_packaged_loopx(tmp_path / "manifest-tamper")
    manifest_path = manifest_root / "config" / "loopx" / "package-lock.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["package"]["version"] = "0.2.5"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_report = LoopXDoctor(manifest_root).run(deep=False)
    assert manifest_report["ready"] is False
    assert manifest_report["blockers"][0]["code"] == "loopx_package_lock_invalid"


def test_release_bundle_contains_loopx_lock_assets_doctor_metadata_and_sbom(
    tmp_path: Path,
) -> None:
    receipt = ReleaseBundleBuilder(
        PROJECT_ROOT,
        tmp_path / "output",
    ).build(
        release_id="loopx-release-test",
        archive_format="zip",
        benchmark_expected_commit=BENCHMARK_COMMIT,
    )
    assert receipt["ready"] is True
    assert receipt["loopx"]["version"] == "0.2.4"
    assert receipt["loopx"]["profiles"] == [
        "linux_wsl_upstream_semantics",
        "windows_release_offline_wheel",
    ]
    archive = Path(receipt["archive"])
    verification = ReleaseBundleBuilder(
        PROJECT_ROOT,
        tmp_path / "verify",
    ).verify(archive)
    assert verification["ready"] is True
    assert verification["loopx"]["ready"] is True
    assert {
        "loopx_package_lock",
        "loopx_wheel",
        "loopx_source_bundle",
        "loopx_doctor_metadata",
    }.issubset(verification["verified_artifacts"])

    with zipfile.ZipFile(archive) as bundle:
        manifest_name = next(
            name for name in bundle.namelist() if name.endswith("/release/manifest.json")
        )
        payload_prefix = manifest_name.removesuffix("release/manifest.json")
        manifest = json.loads(bundle.read(manifest_name))
        sbom = json.loads(bundle.read(payload_prefix + "release/sbom.cdx.json"))
    assert manifest["locks"]["loopx"]["version"] == "0.2.4"
    assert manifest["locks"]["loopx"]["source_digest"] == receipt["loopx"][
        "source_digest"
    ]
    loopx_component = next(
        item for item in sbom["components"] if item["name"] == "loopx"
    )
    properties = {
        item["name"]: item["value"] for item in loopx_component["properties"]
    }
    assert properties["zyra:source-role"] == "supplementary_implementation"
    assert properties["zyra:line-bucket"] == "runtime-assets/vendor-like"
