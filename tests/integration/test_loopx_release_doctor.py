from __future__ import annotations

import json
import zipfile
from pathlib import Path, PurePosixPath

from zyra_productization.release import ReleaseRuntime
from zyra_productization.release.bundle import ReleaseBundleBuilder


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_COMMIT = "09e99cdc5ed9cf3a935ccc327f7261110e6c7d1b"


def test_release_deep_doctor_verifies_embedded_identity_cli_and_resources() -> None:
    report = ReleaseRuntime(PROJECT_ROOT).doctor(
        deep=True,
        require_tools=False,
    )
    assert report["ready"] is True
    loopx = next(
        item for item in report["checks"] if item["check"] == "loopx-pinned-runtime"
    )
    assert loopx["ready"] is True
    details = loopx["details"]
    assert details["version"] == "0.2.13"
    assert (
        details["source_commit"]
        == "a2c072d412d90839132e1cf39c23dd431c394175"
    )
    assert details["archive_fallback"] is False
    assert {item["check"] for item in details["checks"]} >= {
        "package-identity",
        "embedded-source-integrity",
        "embedded-runtime-layout",
        "archive-entry-retired",
        "import-and-cli-entry",
        "extension-skill-resource-discovery",
    }


def test_release_bundle_contains_embedded_source_manifest_and_no_loopx_archives(
    tmp_path: Path,
) -> None:
    receipt = ReleaseBundleBuilder(
        PROJECT_ROOT,
        tmp_path / "output",
    ).build(
        release_id="loopx-embedded-release-test",
        archive_format="zip",
        benchmark_expected_commit=BENCHMARK_COMMIT,
    )
    assert receipt["ready"] is True
    assert receipt["loopx"]["version"] == "0.2.13"
    assert receipt["loopx"]["profiles"] == ["pinned_embedded_source"]
    archive = Path(receipt["archive"])
    verification = ReleaseBundleBuilder(
        PROJECT_ROOT,
        tmp_path / "verify",
    ).verify(archive)
    assert verification["ready"] is True
    assert verification["loopx"]["ready"] is True
    assert {
        "loopx_package_lock",
        "loopx_source_manifest",
        "loopx_doctor_metadata",
    }.issubset(verification["verified_artifacts"])
    assert "loopx_wheel" not in verification["verified_artifacts"]
    assert "loopx_source_bundle" not in verification["verified_artifacts"]

    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert any(
            name.endswith(
                "/packages/integrations/loopx_runtime/loopx/__init__.py"
            )
            for name in names
        )
        assert any(
            name.endswith(
                "/packages/integrations/loopx_runtime/SOURCE-MANIFEST.json"
            )
            for name in names
        )
        loopx_archives = [
            name
            for name in names
            if name.casefold().endswith((".whl", ".tar.gz"))
            and PurePosixPath(name).name.casefold().startswith("loopx-")
        ]
        assert all("/provenance/loopx/" in name.casefold() for name in loopx_archives)
        assert not any(
            "/packages/integrations/loopx_runtime/" in name.casefold()
            for name in loopx_archives
        )
        manifest_name = next(
            name for name in names if name.endswith("/release/manifest.json")
        )
        payload_prefix = manifest_name.removesuffix("release/manifest.json")
        manifest = json.loads(bundle.read(manifest_name))
        sbom = json.loads(bundle.read(payload_prefix + "release/sbom.cdx.json"))
    assert manifest["locks"]["loopx"]["version"] == "0.2.13"
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
    assert (
        properties["zyra:migration-mode"]
        == "pinned_embedded_source_integration"
    )
    assert properties["zyra:line-bucket"] == "runtime-assets/vendor-like"
