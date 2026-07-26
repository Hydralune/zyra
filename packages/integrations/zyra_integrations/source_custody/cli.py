from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .engine import (
    DEFAULT_CATALOG_PATH,
    DEFAULT_LEDGER_PATH,
    DEFAULT_NOTICE_PATH,
    DEFAULT_PROCESS_CATALOG_PATH,
    DEFAULT_VENDOR_MAP_PATH,
    SourceCustodyEngine,
)
from .model import RuleSwitches, stable_json
from .policy import AuditMode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zyra-source-custody",
        description=(
            "Audit Zyra source roles, dependencies, processes, package custody, "
            "LangGraph boundaries, opaque artifacts, and M3 freeze readiness."
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
            "inventory always emits findings with exit 0; candidate fails on "
            "release blockers; freeze also fails on unresolved errors."
        ),
    )
    parser.add_argument("--catalog", default=DEFAULT_CATALOG_PATH)
    parser.add_argument("--process-catalog", default=DEFAULT_PROCESS_CATALOG_PATH)
    parser.add_argument("--vendor-map", default=DEFAULT_VENDOR_MAP_PATH)
    parser.add_argument("--notice", default=DEFAULT_NOTICE_PATH)
    parser.add_argument("--ledger", default=DEFAULT_LEDGER_PATH)
    parser.add_argument("--revision", default="")
    parser.add_argument("--baseline-revision", default="")
    parser.add_argument(
        "--receipt",
        default="",
        help="Write the complete checksum-bound JSON receipt.",
    )
    parser.add_argument(
        "--summary-output",
        default="",
        help="Write a compact JSON audit summary.",
    )
    parser.add_argument(
        "--work-queue",
        default="",
        help="Write the actionable M3-01B work queue.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the compact summary as JSON.",
    )
    parser.add_argument(
        "--full-json",
        action="store_true",
        help="Print the full receipt as JSON.",
    )
    parser.add_argument(
        "--no-vendor-scan",
        action="store_true",
        help="Skip vendor trees in inventory; freeze use should not set this.",
    )
    parser.add_argument(
        "--similarity-threshold",
        type=float,
        default=0.86,
        help="Token-winnowing vendor similarity threshold (0.5..1.0).",
    )
    parser.add_argument(
        "--disable-rule",
        action="append",
        default=[],
        choices=list(RuleSwitches.__dataclass_fields__),
        help=(
            "Disable one rule group for mutation testing. Do not use a disabled "
            "rule set as release evidence."
        ),
    )
    return parser


def switches_from_args(arguments: argparse.Namespace) -> RuleSwitches:
    switches = RuleSwitches()
    for name in arguments.disable_rule:
        switches = switches.disabled(name)
    return switches


def run(arguments: argparse.Namespace) -> int:
    switches = switches_from_args(arguments)
    engine = SourceCustodyEngine(
        arguments.project_root,
        mode=arguments.mode,
        catalog_path=arguments.catalog,
        process_catalog_path=arguments.process_catalog,
        vendor_map_path=arguments.vendor_map,
        notice_path=arguments.notice,
        ledger_path=arguments.ledger,
        revision=arguments.revision,
        baseline_revision=arguments.baseline_revision,
        switches=switches,
        include_vendor=not arguments.no_vendor_scan,
        similarity_threshold=arguments.similarity_threshold,
    )
    result = engine.run()
    if arguments.receipt:
        result.write_receipt(arguments.receipt)
    if arguments.summary_output:
        result.write_summary(arguments.summary_output)
    if arguments.work_queue:
        result.write_work_queue(arguments.work_queue)
    if arguments.full_json:
        print(stable_json(result.receipt()))
    elif arguments.json:
        print(stable_json(result.summary()))
    else:
        print_human_summary(result.summary())
    return result.exit_code


def print_human_summary(summary: dict[str, object]) -> None:
    policy = summary["policy"]
    assert isinstance(policy, dict)
    print(
        "source_custody "
        f"mode={summary['mode']} valid={str(summary['valid']).lower()} "
        f"release_ready={str(summary['release_ready']).lower()}"
    )
    print(
        f"revision={summary['revision'] or 'unresolved'} "
        f"baseline={summary['baseline_revision'] or 'not-specified'}"
    )
    print(
        f"findings={policy['findings']} blockers={policy['blockers']} "
        f"errors={policy['errors']} warnings={policy['warnings']}"
    )
    queue = policy["work_queue"]
    assert isinstance(queue, dict)
    print(f"m3_01b_queue={queue['items']} blocking_items={queue['blocking']}")
    sections = summary["sections"]
    assert isinstance(sections, dict)
    for name, raw in sections.items():
        assert isinstance(raw, dict)
        print(
            f"section={name} valid={str(raw['valid']).lower()} "
            f"findings={raw['finding_count']}"
        )
    print(f"receipt_digest={summary['receipt_digest']}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return run(arguments)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        if arguments.json or arguments.full_json:
            print(
                stable_json(
                    {
                        "schema": "zyra.source-custody-cli-error/v1",
                        "valid": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
            )
        else:
            print(f"source custody audit failed: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
