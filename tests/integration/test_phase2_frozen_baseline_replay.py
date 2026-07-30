from __future__ import annotations

import json
import subprocess
from pathlib import Path

from zyra_productization.release import Phase2BaselineVerifier


ROOT = Path(__file__).resolve().parents[2]


def test_descendant_worktree_replays_references_from_frozen_base_commit() -> None:
    manifest_path = ROOT / "docs" / "release" / "phase2-baseline-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    package_reference = next(
        item
        for item in manifest["references"]
        if item["id"] == "package-manifest"
    )
    frozen_payload = subprocess.run(
        [
            "git",
            "cat-file",
            "blob",
            f"{manifest['identity']['p2_base_commit']}:{package_reference['path']}",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    assert (ROOT / package_reference["path"]).read_bytes() != frozen_payload

    receipt = Phase2BaselineVerifier(ROOT).verify(manifest_path)

    assert receipt["valid"] is True
    assert receipt["finding_count"] == 0
