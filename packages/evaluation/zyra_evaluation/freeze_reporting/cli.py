from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .errors import FreezeEvidenceError
from .pipeline import FreezeEvidencePipeline, verify_freeze_output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zyra-first-stage-evidence",
        description=(
            "Generate or verify the M3-S03-01 first-stage report, evidence index, "
            "role-aware internalization ledger, and checksum archive."
        ),
    )
    parser.add_argument(
        "--repository-root",
        default=".",
        help="Zyra repository root (default: current directory).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser(
        "build",
        help="Build a new evidence output; reviewed M3-S02B-02 input is mandatory.",
    )
    build.add_argument(
        "--target-commit",
        help="Target implementation commit (default: repository HEAD).",
    )
    build.add_argument(
        "--output",
        help=(
            "New output directory inside the repository (default: "
            "docs/reviews/evidence/M3-S03-01/generated-<commit>)."
        ),
    )

    verify = subparsers.add_parser(
        "verify",
        help="Verify an existing output without trusting its generation receipt.",
    )
    verify.add_argument("--output", required=True, help="Evidence output directory.")
    verify.add_argument(
        "--expected-commit",
        help="Optional commit identity that the output must target.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    root = Path(arguments.repository_root).resolve(strict=True)
    try:
        if arguments.command == "build":
            commit = arguments.target_commit or _head_commit(root)
            output = arguments.output or (
                "docs/reviews/evidence/M3-S03-01/"
                f"generated-{commit[:8]}"
            )
            receipt = FreezeEvidencePipeline(
                root,
                target_commit=commit,
                require_release=True,
            ).build(output)
        else:
            output = Path(arguments.output)
            if not output.is_absolute():
                output = root / output
            receipt = verify_freeze_output(
                output,
                expected_commit=arguments.expected_commit,
            )
        _print(receipt)
        return 0
    except FreezeEvidenceError as error:
        _print(
            {
                "schema": "zyra.first-stage-evidence-cli-error/v1",
                "ok": False,
                "error": error.to_dict(),
            },
            stream=sys.stderr,
        )
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as error:
        _print(
            {
                "schema": "zyra.first-stage-evidence-cli-error/v1",
                "ok": False,
                "error": {
                    "code": "unhandled-input-error",
                    "message": str(error),
                    "phase": "cli",
                },
            },
            stream=sys.stderr,
        )
        return 3


def _head_commit(root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise ValueError(
            completed.stderr.strip() or "could not resolve repository HEAD"
        )
    return completed.stdout.strip()


def _print(value: Any, *, stream: Any = sys.stdout) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        file=stream,
    )


if __name__ == "__main__":
    raise SystemExit(main())
