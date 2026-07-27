from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .evidence_gate import DeploymentEvidenceGate
from .errors import DeploymentError
from .models import DeploymentProfile
from .orchestrator import DeploymentOrchestrator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zyra-deploy",
        description="Zyra semantic deployment supervisor",
    )
    parser.add_argument(
        "--project-root",
        default=str(Path.cwd()),
        help="Zyra repository root",
    )
    parser.add_argument(
        "--state-root",
        default="",
        help="deployment state root (defaults to tmp/deployment)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start", help="start API, Web and all profiles")
    start.add_argument("--no-build-web", action="store_true")
    start.add_argument("--restart", action="store_true")
    subparsers.add_parser("stop", help="stop supervised processes")
    subparsers.add_parser("restart", help="restart supervised processes")
    subparsers.add_parser("status", help="show process and profile status")
    doctor = subparsers.add_parser("doctor", help="run deployment diagnostics")
    doctor.add_argument("--check", action="append", default=[])
    doctor.add_argument("--fail-fast", action="store_true")
    health = subparsers.add_parser("health", help="run semantic readiness")
    health.add_argument("--no-short-task", action="store_true")
    health.add_argument("--probe", action="append", default=[])
    exercise = subparsers.add_parser("exercise", help="run profile placement exercise")
    exercise.add_argument("--task-id", required=True)
    exercise.add_argument("--run-id", required=True)
    fault = subparsers.add_parser("fault", help="inject a bounded profile fault")
    fault.add_argument("profile", choices=[item.value for item in DeploymentProfile])
    fault.add_argument(
        "kind",
        choices=[
            "network-loss",
            "provider-failure",
            "latency",
            "fail-next",
            "crash-next",
            "clear",
        ],
    )
    fault.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=True)
    fault.add_argument("--latency-ms", type=int, default=0)
    fault.add_argument("--count", type=int, default=1)
    gate = subparsers.add_parser("verify-evidence", help="verify exact-revision evidence")
    gate.add_argument("--expected-commit", default="")
    return parser


def run(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    root = Path(arguments.project_root).resolve()
    state_root = Path(arguments.state_root).resolve() if arguments.state_root else None
    try:
        if arguments.command == "verify-evidence":
            result = DeploymentEvidenceGate(root).verify(
                expected_commit=arguments.expected_commit or None
            )
        else:
            orchestrator = DeploymentOrchestrator(
                root,
                state_root=state_root,
            )
            if arguments.command == "start":
                result = orchestrator.start(
                    build_web=not arguments.no_build_web,
                    restart=arguments.restart,
                )
            elif arguments.command == "stop":
                result = orchestrator.stop()
            elif arguments.command == "restart":
                result = orchestrator.restart()
            elif arguments.command == "status":
                result = orchestrator.status()
            elif arguments.command == "doctor":
                result = orchestrator.doctor().run(
                    selected=tuple(arguments.check),
                    fail_fast=arguments.fail_fast,
                )
            elif arguments.command == "health":
                result = orchestrator.semantic_health(
                    include_short_task=not arguments.no_short_task,
                    selected=tuple(arguments.probe),
                )
            elif arguments.command == "exercise":
                result = orchestrator.exercise_profiles(
                    task_id=arguments.task_id,
                    run_id=arguments.run_id,
                )
            elif arguments.command == "fault":
                result = orchestrator.inject_fault(
                    DeploymentProfile(arguments.profile),
                    {
                        "kind": arguments.kind,
                        "enabled": arguments.enabled,
                        "latency_ms": arguments.latency_ms,
                        "count": arguments.count,
                    },
                )
            else:
                raise AssertionError(f"unhandled command: {arguments.command}")
    except DeploymentError as error:
        print(
            json.dumps(
                error.to_dict(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    except BaseException as error:
        print(
            json.dumps(
                {
                    "schema": "zyra.deployment-cli-error/v1",
                    "error": type(error).__name__,
                    "message": str(error),
                    "fallback": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 3
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ready", result.get("stopped", True)) is True else 1


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
