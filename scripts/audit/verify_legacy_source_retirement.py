from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_root in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
):
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))

from zyra_integrations.legacy_source_retirement import (  # noqa: E402
    load_retirement_manifest,
    verify_retirement_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Independently verify the P2 legacy source-pool retirement."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT
        / "docs"
        / "reviews"
        / "evidence"
        / "P2-S02A-01"
        / "legacy-source-pool-retirement-manifest.json",
    )
    parser.add_argument("--target", default="HEAD")
    parser.add_argument("--no-worktree-check", action="store_true")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    report = verify_retirement_manifest(
        ROOT,
        load_retirement_manifest(arguments.manifest),
        target_revision=arguments.target,
        check_worktree=not arguments.no_worktree_check,
    )
    payload = report.to_dict()
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
