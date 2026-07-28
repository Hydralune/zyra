"""Command-line entry points for final-freeze operators and automation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .common import FinalFreezeError, load_json, write_json
from .critical_review import CriticalReviewEngine
from .handoff import HandoffLedger
from .orchestrator import (
    COMPONENT_SPECS,
    FinalFreezeIdentity,
    FinalFreezeOrchestrator,
    FinalFreezeVerifier,
)
from .schedule import ReleaseSchedule
from .submission import SubmissionCandidateVerifier


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zyra-final-freeze",
        description=(
            "Build and verify the Zyra first-stage final-freeze decision."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    review = subcommands.add_parser(
        "critical-review",
        help="review the M3-03 increment and inherited evidence receipts",
    )
    review.add_argument("--repository-root", type=Path, required=True)
    review.add_argument("--evidence-output", type=Path, required=True)
    review.add_argument("--expected-evidence-commit")
    review.add_argument("--stage-baseline-commit", required=True)
    review.add_argument("--review-target-commit", required=True)
    review.add_argument("--output", type=Path)

    verify = subcommands.add_parser(
        "verify",
        help="independently verify a final-freeze output directory",
    )
    verify.add_argument("--output-directory", type=Path, required=True)
    verify.add_argument("--output", type=Path)

    submission = subcommands.add_parser(
        "verify-submission",
        help="independently verify a submission candidate directory",
    )
    submission.add_argument("--candidate-directory", type=Path, required=True)
    submission.add_argument("--output", type=Path)

    schedule = subcommands.add_parser(
        "schedule",
        help="emit the frozen 2026 submission schedule",
    )
    schedule.add_argument("--now")
    schedule.add_argument("--output", type=Path)

    handoff = subcommands.add_parser(
        "classify-handoff",
        help="classify residuals into blocker, CI, or optimization work",
    )
    handoff.add_argument("--input", type=Path, required=True)
    handoff.add_argument("--output", type=Path)

    finalize = subcommands.add_parser(
        "finalize",
        help="compose component receipts into a final-freeze output",
    )
    finalize.add_argument("--identity", type=Path, required=True)
    finalize.add_argument("--components-directory", type=Path, required=True)
    finalize.add_argument("--output-directory", type=Path, required=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "critical-review":
            result = _critical_review(arguments)
        elif arguments.command == "verify":
            result = FinalFreezeVerifier().verify(
                arguments.output_directory
            )
        elif arguments.command == "verify-submission":
            result = SubmissionCandidateVerifier().verify(
                arguments.candidate_directory
            )
        elif arguments.command == "schedule":
            result = ReleaseSchedule.default().evaluate(now=arguments.now)
        elif arguments.command == "classify-handoff":
            source = load_json(arguments.input, label="residuals")
            residuals = source.get("residuals", [])
            result = HandoffLedger().build(residuals)
        elif arguments.command == "finalize":
            result = _finalize(arguments)
        else:
            parser.error(f"unsupported command: {arguments.command}")
            return 2
        output = getattr(arguments, "output", None)
        if output is not None:
            write_json(output, result)
        else:
            _print_json(result)
        return 0 if result.get("valid", not result.get("blocking", True)) else 1
    except FinalFreezeError as error:
        _print_json(error.to_dict())
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as error:
        _print_json(
            {
                "schema": "zyra.final-freeze.cli-error.v1",
                "valid": False,
                "error": type(error).__name__,
                "message": str(error),
            }
        )
        return 2


def _critical_review(arguments: argparse.Namespace) -> dict[str, Any]:
    return CriticalReviewEngine(
        arguments.repository_root,
        arguments.evidence_output,
        expected_evidence_commit=arguments.expected_evidence_commit,
        stage_baseline_commit=arguments.stage_baseline_commit,
        review_target_commit=arguments.review_target_commit,
    ).review()


def _finalize(arguments: argparse.Namespace) -> dict[str, Any]:
    identity = FinalFreezeIdentity.from_dict(
        load_json(arguments.identity, label="freeze_identity")
    )
    component_root = arguments.components_directory.resolve()
    components: dict[str, dict[str, Any]] = {}
    for name, spec in COMPONENT_SPECS.items():
        components[name] = load_json(
            component_root / spec["filename"],
            label=f"component.{name}",
        )
    receipt = FinalFreezeOrchestrator().write(
        arguments.output_directory,
        identity,
        components,
    )
    return {
        "schema": "zyra.final-freeze.cli-finalize.v1",
        "valid": True,
        "output_directory": str(arguments.output_directory),
        "generation_receipt": receipt,
    }


def _print_json(value: Any) -> None:
    sys.stdout.write(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
