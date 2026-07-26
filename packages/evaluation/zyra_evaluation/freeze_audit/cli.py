from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .catalog import DEFAULT_CATALOG_PATH
from .engine import FreezeAuditEngine
from .model import AuditMode, CatalogError, RuleSwitches, stable_json


DEFAULT_SLICE_BASELINE = "ce799c7fe02d1f5fc1832bbbff1e76b5a73e8caa"
DEFAULT_PARENT_BASELINE = "1d19ea39a8313091dcfbc00f78c79fdfdeab7cf4"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zyra-freeze-audit",
        description=(
            "Audit Zyra state-owner uniqueness, default CLI/API/Web/worker "
            "reachability, event-to-mutation causality, semantic-port and opaque "
            "risk evidence, effective-line buckets, and competition requirement "
            "bindings."
        ),
    )
    parser.add_argument(
        "--project-root",
        default=str(Path(__file__).resolve().parents[4]),
        help="Zyra Git repository root.",
    )
    parser.add_argument(
        "--mode",
        choices=[item.value for item in AuditMode],
        default=AuditMode.CANDIDATE.value,
        help=(
            "inventory emits all findings with exit 0; candidate fails on "
            "blockers; freeze also fails on unresolved errors."
        ),
    )
    parser.add_argument("--catalog", default=DEFAULT_CATALOG_PATH)
    parser.add_argument(
        "--requirement-matrix",
        default="",
        help=(
            "Optional authoritative requirement matrix. The workspace-root "
            "matrix is auto-detected when omitted."
        ),
    )
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument(
        "--baseline-revision",
        default=DEFAULT_SLICE_BASELINE,
        help="Frozen slice baseline used for effective-code classification.",
    )
    parser.add_argument(
        "--parent-baseline-revision",
        default=DEFAULT_PARENT_BASELINE,
        help="Frozen M3-01A parent baseline used for cumulative closure.",
    )
    parser.add_argument("--slice-minimum", type=int, default=4500)
    parser.add_argument("--parent-minimum", type=int, default=9000)
    parser.add_argument(
        "--source-receipt",
        default="",
        help=(
            "Read a full M3-S01A-01 receipt instead of running its protected "
            "source/dependency/custody engine."
        ),
    )
    parser.add_argument(
        "--no-source-scan",
        action="store_true",
        help="Do not run M3-S01A-01; requires --source-receipt.",
    )
    parser.add_argument(
        "--no-vendor-scan",
        action="store_true",
        help="Skip vendor trees in the source-risk input scan.",
    )
    parser.add_argument(
        "--disable-rule",
        action="append",
        default=[],
        choices=list(RuleSwitches.__dataclass_fields__),
        help=(
            "Disable one rule group for mutation testing. Disabled rules cannot "
            "produce release evidence."
        ),
    )
    parser.add_argument("--receipt", default="")
    parser.add_argument("--summary-output", default="")
    parser.add_argument("--work-queue", default="")
    parser.add_argument("--downstream-directory", default="")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--full-json", action="store_true")
    return parser


def switches_from_args(arguments: argparse.Namespace) -> RuleSwitches:
    switches = RuleSwitches()
    for name in arguments.disable_rule:
        switches = switches.disabled(name)
    return switches


def run(arguments: argparse.Namespace) -> int:
    if arguments.no_source_scan and not arguments.source_receipt:
        raise ValueError("--no-source-scan requires --source-receipt")
    engine = FreezeAuditEngine(
        arguments.project_root,
        mode=arguments.mode,
        catalog_path=arguments.catalog,
        requirement_matrix_path=arguments.requirement_matrix or None,
        revision=arguments.revision,
        baseline_revision=arguments.baseline_revision,
        parent_baseline_revision=arguments.parent_baseline_revision,
        slice_minimum=arguments.slice_minimum,
        parent_minimum=arguments.parent_minimum,
        source_receipt_path=arguments.source_receipt or None,
        run_source_custody=not arguments.no_source_scan,
        include_vendor=not arguments.no_vendor_scan,
        switches=switches_from_args(arguments),
    )
    result = engine.run()
    if arguments.receipt:
        result.write_receipt(arguments.receipt)
    if arguments.summary_output:
        result.write_summary(arguments.summary_output)
    if arguments.work_queue:
        result.write_work_queue(arguments.work_queue)
    if arguments.downstream_directory:
        result.write_downstream_directory(arguments.downstream_directory)
    if arguments.full_json:
        print(stable_json(result.receipt()))
    elif arguments.json:
        print(stable_json(result.summary()))
    else:
        print_human_summary(result.summary())
    return result.exit_code


def print_human_summary(summary: dict[str, object]) -> None:
    policy = summary["policy"]
    if not isinstance(policy, dict):
        raise TypeError("summary policy must be an object")
    print(
        "freeze_audit "
        f"mode={summary['mode']} valid={str(summary['valid']).lower()} "
        f"release_ready={str(summary['release_ready']).lower()}"
    )
    print(f"revision={summary['revision']}")
    print(
        f"findings={policy['findings']} blockers={policy['blockers']} "
        f"errors={policy['errors']} warnings={policy['warnings']}"
    )
    queue = policy["work_queue"]
    if not isinstance(queue, dict):
        raise TypeError("summary work queue must be an object")
    print(
        f"work_items={queue['items']} blocking_items={queue['blocking']} "
        f"owners={queue['by_owner_unit']}"
    )
    sections = summary["sections"]
    if not isinstance(sections, dict):
        raise TypeError("summary sections must be an object")
    for name, raw in sections.items():
        if not isinstance(raw, dict):
            continue
        print(
            f"section={name} valid={str(raw['valid']).lower()} "
            f"findings={raw['findings']} digest={raw['digest']}"
        )
    print(f"receipt_digest={summary['receipt_digest']}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return run(arguments)
    except (
        OSError,
        ValueError,
        TypeError,
        CatalogError,
        json.JSONDecodeError,
    ) as exc:
        if arguments.json or arguments.full_json:
            print(
                stable_json(
                    {
                        "schema": "zyra.freeze-audit-cli-error/v1",
                        "valid": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
            )
        else:
            print(f"freeze audit failed: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
