from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PRODUCTIZATION_ROOT = PROJECT_ROOT / "packages" / "productization"
if str(PRODUCTIZATION_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCTIZATION_ROOT))

from zyra_productization.release import Phase2BaselineVerifier


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Verify the immutable Phase 2 baseline and evidence inventory."
    )
    result.add_argument(
        "--manifest",
        default="docs/release/phase2-baseline-manifest.json",
    )
    result.add_argument("--workspace-root", default=str(PROJECT_ROOT))
    result.add_argument("--require-clean", action="store_true")
    result.add_argument("--output", default="")
    return result


def run(arguments: list[str] | None = None) -> int:
    options = parser().parse_args(arguments)
    manifest = Path(options.manifest)
    if not manifest.is_absolute():
        manifest = PROJECT_ROOT / manifest
    receipt = Phase2BaselineVerifier(
        PROJECT_ROOT,
        workspace_root=Path(options.workspace_root),
    ).verify(
        manifest,
        require_clean=options.require_clean,
    )
    encoded = json.dumps(
        receipt,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    if options.output:
        output = Path(options.output)
        if not output.is_absolute():
            output = PROJECT_ROOT / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if receipt["valid"] else 2


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
