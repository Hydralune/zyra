"""Verify a frozen first-stage output or its submission candidate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EVALUATION_PACKAGE = REPOSITORY_ROOT / "packages" / "evaluation"
if str(EVALUATION_PACKAGE) not in sys.path:
    sys.path.insert(0, str(EVALUATION_PACKAGE))

from zyra_evaluation.final_freeze.orchestrator import FinalFreezeVerifier
from zyra_evaluation.final_freeze.submission import (
    SubmissionCandidateVerifier,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Independently verify Zyra first-stage frozen evidence."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            REPOSITORY_ROOT
            / "docs"
            / "reviews"
            / "evidence"
            / "M3-S03-02"
            / "final-freeze"
        ),
    )
    parser.add_argument(
        "--submission",
        action="store_true",
        help="verify a submission candidate instead of final-freeze output",
    )
    arguments = parser.parse_args()
    if arguments.submission:
        receipt = SubmissionCandidateVerifier().verify(arguments.output)
    else:
        receipt = FinalFreezeVerifier().verify(arguments.output)
    sys.stdout.write(
        json.dumps(
            receipt,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    sys.stdout.write("\n")
    return 0 if receipt["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
