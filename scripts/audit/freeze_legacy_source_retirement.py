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
    freeze_retirement_manifest,
    write_retirement_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Freeze legacy source-pool provenance from immutable Git objects."
    )
    parser.add_argument("--base", default="HEAD")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT
        / "docs"
        / "reviews"
        / "evidence"
        / "P2-S02A-01"
        / "legacy-source-pool-retirement-manifest.json",
    )
    arguments = parser.parse_args()
    manifest = freeze_retirement_manifest(ROOT, base_revision=arguments.base)
    output = write_retirement_manifest(arguments.output, manifest)
    print(
        json.dumps(
            {
                "ok": True,
                "base_commit": manifest["frozen_source"]["commit"],
                "base_tree": manifest["frozen_source"]["tree"],
                "manifest_digest": manifest["manifest_digest"],
                "output": str(output),
                "root_file_count": sum(
                    item["file_count"] for item in manifest["retired_roots"]
                ),
                "root_total_bytes": sum(
                    item["total_bytes"] for item in manifest["retired_roots"]
                ),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
