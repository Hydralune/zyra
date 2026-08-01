from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVALUATION_ROOT = PROJECT_ROOT / "packages" / "evaluation"
if str(EVALUATION_ROOT) not in sys.path:
    sys.path.insert(0, str(EVALUATION_ROOT))
PRODUCTIZATION_ROOT = PROJECT_ROOT / "packages" / "productization"
if str(PRODUCTIZATION_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCTIZATION_ROOT))

from zyra_evaluation.policy_benchmark.preflight import (  # noqa: E402
    StrongestPreflightError,
    StrongestPreflightRunner,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the frozen, local-first phase2_strongest_v1 preflight. "
            "The command never activates the default resolver."
        )
    )
    parser.add_argument(
        "--manifest",
        default="config/phase2/strongest-preflight.json",
        help="Repository-relative frozen preflight manifest.",
    )
    parser.add_argument(
        "--output",
        default="",
        help=(
            "Repository-relative immutable output directory. Defaults to "
            "docs/evidence/phase2/preflight/<preflight_id>."
        ),
    )
    parser.add_argument(
        "--implementation-commit",
        default="",
        help="Runner/config commit; defaults to the current Git HEAD.",
    )
    return parser


def _repository_path(value: str, *, directory: bool = False) -> Path:
    selected = Path(value)
    target = (
        selected.resolve()
        if selected.is_absolute()
        else (PROJECT_ROOT / selected).resolve()
    )
    try:
        target.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise StrongestPreflightError(
            "preflight-cli-path-outside-repository",
            f"Path must remain inside the Zyra repository: {value}.",
        ) from exc
    if not directory and not target.is_file():
        raise StrongestPreflightError(
            "preflight-cli-input-missing",
            f"Required file is missing: {value}.",
        )
    return target


def _head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


def run(arguments: list[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    manifest_path = _repository_path(options.manifest)
    runner = StrongestPreflightRunner.from_manifest(
        PROJECT_ROOT,
        manifest_path,
    )
    output = (
        _repository_path(options.output, directory=True)
        if options.output
        else PROJECT_ROOT
        / "docs"
        / "evidence"
        / "phase2"
        / "preflight"
        / runner.manifest.preflight_id
    )
    result = runner.run(
        implementation_commit=options.implementation_commit or _head(),
        output_directory=output,
    )
    summary = {
        "schema": "zyra.strongest-preflight-cli-summary/v1",
        "preflight_id": runner.manifest.preflight_id,
        "status": result.report["status"],
        "preflight_report_digest": result.report["report_digest"],
        "readiness_report_digest": result.readiness_report["report_digest"],
        "activation_report_digest": result.activation_report[
            "activation_report_digest"
        ],
        "activation_conclusion": result.activation_report["conclusion"],
        "sealed_run_admission_eligible": result.activation_report[
            "sealed_run_admission_eligible"
        ],
        "normal_resolver_after": result.report["resolver_after"],
        "output": output.relative_to(PROJECT_ROOT).as_posix(),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.passed else 2


def main() -> None:
    try:
        raise SystemExit(run())
    except (
        OSError,
        StrongestPreflightError,
        subprocess.CalledProcessError,
    ) as exc:
        print(
            json.dumps(
                {
                    "schema": "zyra.strongest-preflight-cli-error/v1",
                    "status": "blocked",
                    "error": str(exc),
                    "error_code": getattr(
                        exc,
                        "code",
                        type(exc).__name__,
                    ),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
