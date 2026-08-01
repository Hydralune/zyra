from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from zyra_productization.release.worktree import require_worktree_boundary


ROOT = Path(__file__).resolve().parents[2]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def prepare_manifest(
    *,
    template_path: Path,
    output_path: Path,
    target_commit: str,
    base_commit: str,
    evidence_root: str,
) -> dict[str, Any]:
    resolved_target = _git("rev-parse", f"{target_commit}^{{commit}}")
    resolved_base = _git("rev-parse", f"{base_commit}^{{commit}}")
    head = _git("rev-parse", "HEAD")
    if resolved_target != head:
        raise RuntimeError(
            "final sealed manifest must be prepared from the checked-out target: "
            f"head={head}, target={resolved_target}"
        )
    require_worktree_boundary(ROOT, expected_head=resolved_target)

    manifest = _load_json(template_path)
    if manifest.get("schema") != "zyra.phase2-sealed-long-run-manifest/v1":
        raise ValueError("unsupported sealed manifest schema")
    # The frozen scenario runner remains the P2-S06-02 evidence mechanism. This
    # final manifest revalidates those same scenarios on the P2-S06-03 target.
    if manifest.get("slice") != "P2-S06-02":
        raise ValueError("the final revalidation template must retain P2-S06-02")

    manifest["manifest_id"] = f"p2-s06-03-final-{resolved_target[:12]}"
    manifest["candidate_commit"] = resolved_target
    manifest["base_commit"] = resolved_base
    manifest["evidence_root"] = evidence_root

    frozen = manifest.get("frozen_files")
    if not isinstance(frozen, dict) or not frozen:
        raise ValueError("sealed manifest frozen_files must be a non-empty object")
    manifest["frozen_files"] = {
        relative: _sha256(ROOT / relative)
        for relative in sorted(str(item) for item in frozen)
    }

    output_relative = output_path.resolve().relative_to(ROOT.resolve()).as_posix()
    if not output_relative.startswith((".tmp/", "docs/evidence/")):
        raise ValueError("final sealed manifest output must be generated evidence")
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite final manifest: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bind the frozen P2 sealed scenarios to the final release target."
    )
    parser.add_argument("--target-commit", required=True)
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument(
        "--template",
        default="config/phase2/sealed-long-run-manifest.json",
    )
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()

    manifest = prepare_manifest(
        template_path=(ROOT / arguments.template).resolve(),
        output_path=(ROOT / arguments.output).resolve(),
        target_commit=arguments.target_commit,
        base_commit=arguments.base_commit,
        evidence_root=arguments.evidence_root,
    )
    print(
        json.dumps(
            {
                "manifest_id": manifest["manifest_id"],
                "candidate_commit": manifest["candidate_commit"],
                "evidence_root": manifest["evidence_root"],
                "frozen_file_count": len(manifest["frozen_files"]),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
