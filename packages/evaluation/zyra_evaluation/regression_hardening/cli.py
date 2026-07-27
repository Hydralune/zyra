from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .contracts import ContractError
from .freeze_gate import RegressionFreezeGate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zyra-regression-hardening",
        description=(
            "Inspect the M3 default-path/security regression registry or "
            "admit an exact-revision suite receipt into the freeze gate."
        ),
    )
    parser.add_argument(
        "--project-root",
        default=str(Path(__file__).resolve().parents[4]),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify = subparsers.add_parser(
        "verify-receipt",
        help="verify a completed generated-input regression suite receipt",
    )
    verify.add_argument("receipt")
    verify.add_argument("--revision", default="HEAD")
    verify.add_argument("--prerequisite", default="")
    verify.add_argument("--live-matrix", default="")
    verify.add_argument("--secret-canary", action="append", default=[])
    verify.add_argument("--output", default="")

    live = subparsers.add_parser(
        "run-live",
        help="run real CLI/API/Web/worker generated-input scenarios",
    )
    live.add_argument("--output", required=True)
    live.add_argument("--python", default=sys.executable)
    live.add_argument("--bun", default="")
    live.add_argument("--temporary-parent", default="")
    live.add_argument("--maximum-workers", type=int, default=1)
    live.add_argument("--shard-index", type=int, default=0)
    live.add_argument("--shard-count", type=int, default=1)
    live.add_argument("--scenario", action="append", default=[])

    schema = subparsers.add_parser(
        "schema",
        help="print required case and receipt admission contract",
    )
    schema.add_argument("--compact", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        root = Path(args.project_root).resolve()
        if args.command == "verify-receipt":
            gate = RegressionFreezeGate(
                root,
                prerequisite_path=args.prerequisite or None,
                live_matrix_path=args.live_matrix or None,
                secret_canaries=tuple(args.secret_canary),
            )
            report = gate.verify_file(
                args.receipt,
                expected_revision=args.revision,
            )
            payload = report.to_dict()
            if args.output:
                _atomic_json(args.output, payload)
            _print_json(payload)
            return 0 if report.valid else 2
        if args.command == "run-live":
            from .live_matrix import (
                LiveRegressionMatrixRunner,
                default_live_registry,
            )

            registry = default_live_registry(
                root,
                python_executable=args.python,
                bun_executable=args.bun or None,
            )
            runner = LiveRegressionMatrixRunner(
                root,
                registry,
                temporary_parent=args.temporary_parent or None,
                maximum_workers=args.maximum_workers,
            )
            receipt = runner.run(
                args.output,
                shard_index=args.shard_index,
                shard_count=args.shard_count,
                scenario_ids=tuple(args.scenario),
            )
            _print_json(
                {
                    "schema": "zyra.m3-live-regression-cli-result/v1",
                    "passed": receipt.passed,
                    "receipt_path": str(Path(args.output).resolve()),
                    "receipt_digest": receipt.digest,
                    "revision": receipt.revision,
                    "case_count": len(receipt.cases),
                }
            )
            return 0 if receipt.passed else 2
        if args.command == "schema":
            from .freeze_gate import REQUIRED_CASES

            payload = {
                "schema": "zyra.m3-regression-hardening-cli-schema/v1",
                "suite_schema": "zyra.m3-regression-hardening/v1/suite-receipt",
                "required_cases": list(REQUIRED_CASES),
                "requirements": {
                    "exact_revision": True,
                    "sealed_registry_digest": True,
                    "generated_input_digest_per_case": True,
                    "passing_assertions_and_observations": True,
                    "integrity_bound_artifact_per_case": True,
                    "minimum_mutation_assertions": 20,
                    "minimum_security_observations": 2,
                    "m3_01_prerequisite": True,
                    "positive_mock_fixture_legacy_fallback": False,
                },
            }
            _print_json(payload, compact=args.compact)
            return 0
        parser.error(f"unsupported command: {args.command}")
    except (ContractError, OSError, ValueError, TypeError) as error:
        _print_json(
            {
                "schema": "zyra.m3-regression-hardening-cli-error/v1",
                "valid": False,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        )
        return 3
    return 3


def _atomic_json(path: str | Path, payload: Any) -> None:
    from .engine_io import atomic_json_write

    atomic_json_write(path, payload)


def _print_json(payload: Any, *, compact: bool = False) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=None if compact else 2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
