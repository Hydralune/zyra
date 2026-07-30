from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PRODUCTIZATION = ROOT / "packages" / "productization"
if str(PRODUCTIZATION) not in sys.path:
    sys.path.insert(0, str(PRODUCTIZATION))

from zyra_productization.release.phase2_freeze import (
    Phase2FreezeAuditor,
    Phase2FreezeError,
)


def _path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit one immutable Phase 2 release/evidence target."
    )
    parser.add_argument("--target-commit", required=True)
    parser.add_argument("--release-root", required=True)
    parser.add_argument("--sealed-root", required=True)
    parser.add_argument("--preflight-root", required=True)
    parser.add_argument("--regression-receipt", required=True)
    parser.add_argument("--custody-report", required=True)
    parser.add_argument("--contract-report", required=True)
    parser.add_argument("--output-root", required=True)
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = Phase2FreezeAuditor(ROOT).audit(
            target_commit=arguments.target_commit,
            release_root=_path(arguments.release_root),
            sealed_root=_path(arguments.sealed_root),
            preflight_root=_path(arguments.preflight_root),
            regression_receipt=_path(arguments.regression_receipt),
            custody_report=_path(arguments.custody_report),
            contract_report=_path(arguments.contract_report),
            output_root=_path(arguments.output_root),
        )
    except (OSError, Phase2FreezeError, ValueError) as error:
        print(
            json.dumps(
                {
                    "schema": "zyra.phase2-final-freeze-error/v1",
                    "verdict": "BLOCKED",
                    "ready": False,
                    "error": str(error),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(run())
