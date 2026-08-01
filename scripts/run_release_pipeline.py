from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTIZATION_ROOT = PROJECT_ROOT / "packages" / "productization"
if str(PRODUCTIZATION_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCTIZATION_ROOT))

from zyra_productization.release.bundle import assert_clean_git, source_revision
from zyra_productization.release.errors import ReleaseError
from zyra_productization.release.integrity import sha256_file, stable_digest
from zyra_productization.release.runtime import ReleaseRuntime
from zyra_productization.release.submission import ReproducibilityVerifier


PIPELINE_SCHEMA = "zyra.release-pipeline/v1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build two byte-identical Zyra release bundles and optionally run "
            "the fail-closed release CI graph."
        )
    )
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--expected-commit", default="")
    parser.add_argument(
        "--benchmark-commit",
        default="95fcf7aeaed5b5ec80fb2f7178b97fbdf8adbeb6",
    )
    parser.add_argument(
        "--output-root",
        default="dist/release-pipeline",
    )
    parser.add_argument(
        "--format",
        choices=("tar.gz", "zip"),
        default="tar.gz",
    )
    parser.add_argument("--skip-ci", action="store_true")
    parser.add_argument("--maximum-parallel", type=int, default=2)
    parser.add_argument(
        "--python-regression-receipt",
        default="",
        help=(
            "exact-target Phase 2 final-regression receipt to reuse for the "
            "release CI Python gate"
        ),
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="development-only; release admission still records the revision",
    )
    return parser


class ReleasePipeline:
    def __init__(
        self,
        project_root: Path,
        output_root: Path,
    ) -> None:
        self.project_root = project_root.resolve()
        self.output_root = output_root.resolve()

    def run(
        self,
        *,
        release_id: str,
        expected_commit: str,
        benchmark_commit: str,
        archive_format: str,
        run_ci: bool,
        maximum_parallel: int,
        require_clean: bool,
        python_regression_receipt: Path | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        git_receipt = (
            assert_clean_git(self.project_root)
            if require_clean
            else {
                "schema": "zyra.release-git-receipt/v1",
                "ready": True,
                "revision": source_revision(self.project_root),
                "dirty_check_skipped": True,
            }
        )
        revision = str(git_receipt["revision"])
        if expected_commit and expected_commit != revision:
            raise ReleaseError(
                "Release pipeline revision does not match the requested commit.",
                code="release_pipeline_revision_mismatch",
                details={
                    "expected": expected_commit,
                    "actual": revision,
                },
            )
        expected_commit = expected_commit or revision
        first_root = self.output_root / "reproducibility-a"
        second_root = self.output_root / "reproducibility-b"
        ci_root = self.output_root / "admission"
        for path in (first_root, second_root, ci_root):
            path.mkdir(parents=True, exist_ok=True)
        first = ReleaseRuntime(
            self.project_root,
            output_root=first_root,
            state_root=self.output_root / "state-a",
        ).build(
            release_id=release_id,
            expected_commit=expected_commit,
            archive_format=archive_format,
            benchmark_expected_commit=benchmark_commit,
        )
        second = ReleaseRuntime(
            self.project_root,
            output_root=second_root,
            state_root=self.output_root / "state-b",
        ).build(
            release_id=release_id,
            expected_commit=expected_commit,
            archive_format=archive_format,
            benchmark_expected_commit=benchmark_commit,
        )
        first_archive = Path(str(first["archive"]))
        second_archive = Path(str(second["archive"]))
        reproducibility = ReproducibilityVerifier().compare(
            first_archive,
            second_archive,
            expected_commit=expected_commit,
        )
        promoted = self.output_root / first_archive.name
        shutil.copyfile(first_archive, promoted)
        promoted_digest = sha256_file(promoted)
        if promoted_digest != str(first["archive_sha256"]):
            raise ReleaseError(
                "Promoted release archive changed after reproducibility check.",
                code="release_pipeline_promotion_digest",
                details={
                    "expected": first["archive_sha256"],
                    "actual": promoted_digest,
                },
            )
        verification = ReleaseRuntime(
            self.project_root,
            output_root=ci_root,
            state_root=self.output_root / "state-ci",
        ).verify(promoted)
        ci: Mapping[str, Any] | None = None
        if run_ci:
            ci = ReleaseRuntime(
                self.project_root,
                output_root=ci_root,
                state_root=self.output_root / "state-ci",
            ).ci(
                archive=promoted,
                expected_commit=expected_commit,
                maximum_parallel=maximum_parallel,
                python_regression_receipt=python_regression_receipt,
            )
        ready = (
            reproducibility.get("ready") is True
            and verification.get("ready") is True
            and (ci is None or ci.get("ready") is True)
        )
        result: dict[str, Any] = {
            "schema": PIPELINE_SCHEMA,
            "ready": ready,
            "release_id": release_id,
            "source_commit": expected_commit,
            "benchmark_commit": benchmark_commit,
            "archive": str(promoted),
            "archive_sha256": promoted_digest,
            "archive_size": promoted.stat().st_size,
            "git": git_receipt,
            "first_build": self._portable_build(first),
            "second_build": self._portable_build(second),
            "reproducibility": reproducibility,
            "verification": verification,
            "ci": dict(ci) if ci is not None else None,
            "ci_executed": ci is not None,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }
        result["digest"] = stable_digest(result)
        self._write_report(result)
        if not ready:
            raise ReleaseError(
                "Release pipeline did not satisfy every enabled gate.",
                code="release_pipeline_not_ready",
                details=result,
            )
        return result

    def _write_report(self, value: Mapping[str, Any]) -> None:
        path = self.output_root / "pipeline-report.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _portable_build(value: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: item
            for key, item in value.items()
            if key
            not in {
                "archive",
                "python_lock",
                "javascript_lock",
                "boundary",
                "benchmark",
            }
        }


def run(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    pipeline = ReleasePipeline(
        PROJECT_ROOT,
        (PROJECT_ROOT / arguments.output_root),
    )
    try:
        result = pipeline.run(
            release_id=arguments.release_id,
            expected_commit=arguments.expected_commit,
            benchmark_commit=arguments.benchmark_commit,
            archive_format=arguments.format,
            run_ci=not arguments.skip_ci,
            maximum_parallel=arguments.maximum_parallel,
            require_clean=not arguments.allow_dirty,
            python_regression_receipt=(
                Path(arguments.python_regression_receipt)
                if arguments.python_regression_receipt
                else None
            ),
        )
    except ReleaseError as error:
        print(
            json.dumps(
                error.to_dict(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
