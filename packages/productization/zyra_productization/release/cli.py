from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .bundle import BenchmarkEvidenceLinker
from .cleanroom import PlatformPlanner
from .errors import ReleaseError
from .runtime import ReleaseRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zyra-release",
        description="Build, verify, install and admit reproducible Zyra releases",
    )
    parser.add_argument(
        "--project-root",
        default=str(Path.cwd()),
        help="clean Zyra project root",
    )
    parser.add_argument(
        "--work-root",
        default="",
        help="temporary release work root",
    )
    parser.add_argument(
        "--output-root",
        default="",
        help="release artifact output root",
    )
    parser.add_argument(
        "--state-root",
        default="",
        help="release transaction state root",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="run release preflight diagnostics")
    doctor.add_argument("--require-clean-git", action="store_true")
    doctor.add_argument("--allow-unhashed-lock", action="store_true")
    doctor.add_argument("--no-tools", action="store_true")
    doctor.add_argument(
        "--deep",
        action="store_true",
        help="verify the pinned LoopX package, import, CLI and workspace boundary",
    )

    build = commands.add_parser("build", help="build a deterministic release bundle")
    build.add_argument("--release-id", required=True)
    build.add_argument("--expected-commit", default="")
    build.add_argument("--benchmark-commit", default="")
    build.add_argument("--format", choices=["tar.gz", "zip"], default="tar.gz")
    build.add_argument("--allow-unhashed-lock", action="store_true")

    verify = commands.add_parser("verify", help="verify an existing release bundle")
    verify.add_argument("archive")

    install = commands.add_parser(
        "install",
        help="transactionally install a verified release",
    )
    install.add_argument("archive")
    install.add_argument("--install-root", required=True)
    install.add_argument("--idempotency-key", required=True)
    install.add_argument("--release-id", required=True)
    install.add_argument("--manifest-digest", required=True)
    install.add_argument("--migration-version", type=int, default=1)

    uninstall = commands.add_parser(
        "uninstall",
        help="remove product-owned paths from a committed install",
    )
    uninstall.add_argument("transaction_id")
    uninstall.add_argument("--purge-state", action="store_true")

    lifecycle = commands.add_parser(
        "lifecycle",
        help="start, stop or inspect the installed Zyra deployment",
    )
    lifecycle.add_argument(
        "action",
        choices=["start", "stop", "restart", "status", "doctor", "health"],
    )
    lifecycle.add_argument("--no-build-web", action="store_true")
    lifecycle.add_argument("--no-short-task", action="store_true")

    health = commands.add_parser(
        "health",
        help="run semantic readiness against the installed deployment",
    )
    health.add_argument("--no-short-task", action="store_true")

    migrate = commands.add_parser(
        "migrate",
        help="advance a committed installation through the migration registry",
    )
    migrate.add_argument("transaction_id")
    migrate.add_argument("--target-version", type=int, required=True)

    rollback = commands.add_parser(
        "rollback",
        help="transactionally restore the release active before an install",
    )
    rollback.add_argument("transaction_id")
    rollback.add_argument("--target-version", type=int, default=0)

    clean_install = commands.add_parser(
        "clean-install",
        help="exercise an isolated build/install/lifecycle/uninstall",
    )
    clean_install.add_argument("archive")
    clean_install.add_argument("--expected-commit", required=True)
    clean_install.add_argument("--output", required=True)
    clean_install.add_argument("--offline", action="store_true")
    clean_install.add_argument("--no-product-lifecycle", action="store_true")

    plan = commands.add_parser(
        "platform-plan",
        help="emit Windows or Linux install/lifecycle commands",
    )
    plan.add_argument("platform", choices=["windows", "linux"])
    plan.add_argument("--architecture", default="")
    plan.add_argument("--python", default="")
    plan.add_argument("--offline", action="store_true")

    benchmark = commands.add_parser(
        "benchmark-link",
        help="bind formal benchmark and protected deployment evidence",
    )
    benchmark.add_argument("--expected-benchmark-commit", default="")
    benchmark.add_argument("--report", default="")
    benchmark.add_argument("--deployment", default="")

    ci = commands.add_parser(
        "ci",
        help="run the fail-closed release gate matrix",
    )
    ci.add_argument("archive")
    ci.add_argument("--expected-commit", required=True)
    ci.add_argument("--python", default="")
    ci.add_argument("--bun", default="")
    ci.add_argument("--maximum-parallel", type=int, default=2)
    ci.add_argument(
        "--python-regression-receipt",
        default="",
        help=(
            "reuse an exact-target Phase 2 final-regression receipt instead "
            "of executing the identical Python suite again"
        ),
    )
    ci.add_argument(
        "--python-regression-receipt-sha256",
        default="",
        help="out-of-band SHA-256 anchor for the reused regression receipt",
    )
    return parser


def run(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    project_root = Path(arguments.project_root).resolve()
    runtime = ReleaseRuntime(
        project_root,
        work_root=(
            Path(arguments.work_root).resolve()
            if arguments.work_root
            else None
        ),
        output_root=(
            Path(arguments.output_root).resolve()
            if arguments.output_root
            else None
        ),
        state_root=(
            Path(arguments.state_root).resolve()
            if arguments.state_root
            else None
        ),
    )
    try:
        result = _dispatch(runtime, arguments)
    except ReleaseError as error:
        _print(error.to_dict())
        return 2
    except KeyboardInterrupt:
        _print(
            {
                "schema": "zyra.release-cli-error/v1",
                "code": "release_interrupted",
                "message": "Release operation was interrupted.",
                "retryable": True,
                "fallback": False,
            }
        )
        return 130
    except BaseException as error:
        _print(
            {
                "schema": "zyra.release-cli-error/v1",
                "code": "release_unexpected_error",
                "message": str(error),
                "type": type(error).__name__,
                "retryable": False,
                "fallback": False,
            }
        )
        return 3
    _print(result)
    return 0 if result.get("ready", True) is True else 1


def _dispatch(
    runtime: ReleaseRuntime,
    arguments: argparse.Namespace,
) -> dict[str, Any]:
    command = arguments.command
    if command == "doctor":
        return runtime.doctor(
            require_clean_git=arguments.require_clean_git,
            require_hashes=not arguments.allow_unhashed_lock,
            require_tools=not arguments.no_tools,
            deep=arguments.deep,
        )
    if command == "build":
        return runtime.build(
            release_id=arguments.release_id,
            expected_commit=arguments.expected_commit or None,
            archive_format=arguments.format,
            require_python_hashes=not arguments.allow_unhashed_lock,
            benchmark_expected_commit=arguments.benchmark_commit or None,
        )
    if command == "verify":
        return runtime.verify(Path(arguments.archive))
    if command == "install":
        return runtime.install(
            Path(arguments.archive),
            install_root=Path(arguments.install_root),
            idempotency_key=arguments.idempotency_key,
            release_id=arguments.release_id,
            manifest_digest=arguments.manifest_digest,
            migration_version=arguments.migration_version,
        )
    if command == "uninstall":
        return runtime.uninstall(
            arguments.transaction_id,
            purge_state=arguments.purge_state,
        )
    if command == "lifecycle":
        return runtime.lifecycle(
            arguments.action,
            build_web=not arguments.no_build_web,
            include_short_task=not arguments.no_short_task,
        )
    if command == "health":
        return runtime.lifecycle(
            "health",
            include_short_task=not arguments.no_short_task,
        )
    if command == "migrate":
        return runtime.migrate(
            arguments.transaction_id,
            target_version=arguments.target_version,
        )
    if command == "rollback":
        return runtime.rollback(
            arguments.transaction_id,
            target_version=arguments.target_version,
        )
    if command == "clean-install":
        return runtime.clean_install(
            Path(arguments.archive),
            expected_commit=arguments.expected_commit,
            output=Path(arguments.output),
            offline=arguments.offline,
            run_product_lifecycle=not arguments.no_product_lifecycle,
        )
    if command == "platform-plan":
        return {
            "schema": "zyra.release-platform-plan/v1",
            "ready": True,
            "plan": PlatformPlanner(runtime.project_root).plan(
                arguments.platform,
                architecture=arguments.architecture or None,
                python=arguments.python or None,
                offline=arguments.offline,
            ).to_dict(),
        }
    if command == "benchmark-link":
        return BenchmarkEvidenceLinker(runtime.project_root).link(
            expected_commit=arguments.expected_benchmark_commit or None,
            report_path=Path(arguments.report) if arguments.report else None,
            deployment_path=(
                Path(arguments.deployment)
                if arguments.deployment
                else None
            ),
        )
    if command == "ci":
        return runtime.ci(
            archive=Path(arguments.archive),
            expected_commit=arguments.expected_commit,
            python=arguments.python or None,
            bun=arguments.bun or None,
            maximum_parallel=arguments.maximum_parallel,
            python_regression_receipt=(
                Path(arguments.python_regression_receipt)
                if arguments.python_regression_receipt
                else None
            ),
            python_regression_receipt_sha256=(
                arguments.python_regression_receipt_sha256
            ),
        )
    raise AssertionError(f"unhandled release command: {command}")


def _print(value: Any) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()


__all__ = ["build_parser", "main", "run"]
