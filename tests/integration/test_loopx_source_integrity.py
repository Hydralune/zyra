from __future__ import annotations

import json
import shutil
from pathlib import Path

from zyra_integrations.loopx.runtime import LoopXDoctor, LoopXPackageLock
from zyra_integrations.loopx.runtime.manifest import source_manifest_digest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
EMBEDDED_RELATIVE = Path("packages") / "integrations" / "loopx_runtime"


def _isolated_runtime(tmp_path: Path) -> Path:
    root = tmp_path / "isolated"
    embedded = root / EMBEDDED_RELATIVE
    embedded.parent.mkdir(parents=True)
    shutil.copytree(PROJECT_ROOT / EMBEDDED_RELATIVE, embedded)
    lock_source = PROJECT_ROOT / "config" / "loopx" / "package-lock.json"
    lock_target = root / "config" / "loopx" / "package-lock.json"
    lock_target.parent.mkdir(parents=True)
    shutil.copyfile(lock_source, lock_target)
    return root


def test_source_file_resource_manifest_and_version_tamper_fail_closed(
    tmp_path: Path,
) -> None:
    root = _isolated_runtime(tmp_path)
    embedded = root / EMBEDDED_RELATIVE

    source = embedded / "loopx" / "event_sourced_state.py"
    original_source = source.read_bytes()
    source.write_bytes(original_source + b"\n# tamper\n")
    report = LoopXDoctor(root).run(deep=False)
    assert report["ready"] is False
    assert any(
        blocker["code"] == "loopx_embedded_source_tampered"
        for blocker in report["blockers"]
    )
    source.write_bytes(original_source)

    extension = embedded / "loopx" / "extensions" / "lark" / "extension.toml"
    original_extension = extension.read_bytes()
    extension.unlink()
    report = LoopXDoctor(root).run(deep=True)
    assert report["ready"] is False
    assert any(
        blocker["code"] == "loopx_embedded_source_tampered"
        for blocker in report["blockers"]
    )
    extension.write_bytes(original_extension)

    injected = embedded / "loopx" / "injected_runtime.py"
    injected.write_text("INJECTED = True\n", encoding="utf-8")
    report = LoopXDoctor(root).run(deep=True)
    assert report["ready"] is False
    blocker = next(
        item
        for item in report["blockers"]
        if item["code"] == "loopx_embedded_source_tampered"
    )
    assert blocker["details"]["details"]["changed"] == [
        {"path": "loopx/injected_runtime.py", "reason": "unexpected"}
    ]
    injected.unlink()

    manifest_path = embedded / "SOURCE-MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "0.2.14"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report = LoopXDoctor(root).run(deep=False)
    assert report["ready"] is False
    assert report["blockers"][0]["code"] == "loopx_source_manifest_mismatch"


def test_manifest_inventory_covers_runtime_extensions_skills_and_tests() -> None:
    lock = LoopXPackageLock.load(PROJECT_ROOT)
    manifest = lock.source_manifest
    assert manifest["file_count"] == 1925
    assert len(manifest["packages"]) >= 40
    assert set(manifest["entry_points"]) == {
        "loopx",
        "loopx-lark-provider",
        "loopx-openviking-semantic-preference",
    }
    assert {
        "loopx/extensions/lark/extension.toml",
        "loopx/extensions/openviking_periodic_report/extension.toml",
        "loopx/extensions/openviking_semantic_preference/extension.toml",
        "packages/loopx-finance-value-discovery/extension.toml",
    }.issubset(manifest["extensions"])
    assert "skills/loopx-project/SKILL.md" in manifest["skills"]
    assert (
        "loopx/capabilities/auto_research/worker_skill/SKILL.md"
        in manifest["package_data"]
    )
    assert len(manifest["upstream_tests"]) == 125
    exclusions = {item["pattern"]: item for item in manifest["exclusions"]}
    assert exclusions[".github/**"]["excluded_file_count"] == 7
    assert exclusions["AGENTS.md"]["excluded_file_count"] == 1


def test_tree_digest_is_path_separator_order_and_platform_stable() -> None:
    records = [
        {
            "path": "loopx/a.py",
            "sha256": "a" * 64,
            "size": 10,
            "executable": False,
        },
        {
            "path": "skills/demo/SKILL.md",
            "sha256": "b" * 64,
            "size": 20,
            "executable": True,
        },
    ]
    windows_reversed = [
        {**item, "path": str(item["path"]).replace("/", "\\")}
        for item in reversed(records)
    ]
    assert source_manifest_digest(records) == source_manifest_digest(
        windows_reversed
    )
