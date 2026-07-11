from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .ledger_audit import InternalizationLedgerAuditor, filter_findings
from .ledger_accounting import accounting_markdown, build_accounting_report
from .ledger_events import append_jsonl_event, audit_event_payload
from .ledger_gate import build_completion_gate_report
from .ledger_linecount import build_line_count_report, line_count_payload
from .ledger_line_buckets import build_line_bucket_report, line_bucket_payload
from .ledger_matrix import build_unit_matrix
from .ledger_models import InternalizationLedgerEntry, to_jsonable
from .ledger_acceptance import acceptance_payload, build_acceptance_report
from .ledger_boundary import boundary_payload, build_clean_boundary_report
from .ledger_cleanroom import build_cleanroom_report, cleanroom_payload
from .ledger_evidence_graph import build_evidence_graph_report, evidence_graph_payload
from .ledger_mutation_consistency import build_mutation_consistency_report, mutation_consistency_payload
from .ledger_persistence import AtomicLedgerStore, persistence_payload
from .ledger_policy_matrix import build_policy_matrix_report, policy_matrix_payload
from .ledger_policy import classify_path, minimum_effective_lines_for_unit
from .ledger_reports import build_full_ledger_report, build_unit_readiness_report
from .ledger_reachability import build_reachability_report, reachability_payload
from .ledger_schema_contract import build_schema_contract_report, schema_contract_payload
from .ledger_semantics import build_semantic_effect_report, semantic_payload
from .ledger_state_custody import build_state_custody_report, state_custody_payload
from .ledger_source_scan import build_source_scan_report
from .ledger_snapshots import (
    build_snapshot,
    diff_snapshots,
    list_snapshots,
    load_snapshot,
    save_snapshot,
    save_snapshot_to_default_dir,
)
from .ledger_store import (
    InternalizationLedger,
    load_project_ledger,
    load_seed_ledger,
    parse_query,
    project_ledger_path,
    save_project_ledger,
)
from .ledger_test_quality import build_test_quality_report, test_quality_payload
from .ledger_unit_review import build_unit_review_report, unit_review_markdown, unit_review_payload
from .ledger_workflow import LedgerAdvanceRequest, LedgerWorkflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Zyra internalization ledger CLI")
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[3]))
    parser.add_argument("--ledger-path", default="")
    subcommands = parser.add_subparsers(dest="command", required=True)

    list_parser = subcommands.add_parser("list", help="List ledger entries")
    _add_query_args(list_parser)
    list_parser.add_argument("--json", action="store_true")

    show_parser = subcommands.add_parser("show", help="Show one ledger entry")
    show_parser.add_argument("ledger_id")
    show_parser.add_argument("--json", action="store_true")

    audit_parser = subcommands.add_parser("audit", help="Run ledger audit")
    audit_parser.add_argument("--strict", action="store_true", default=False)
    audit_parser.add_argument("--write-event", action="store_true", default=False)
    audit_parser.add_argument("--event-log", default="")
    audit_parser.add_argument("--fail-on-error", action="store_true", default=False)
    audit_parser.add_argument("--severity", default="")
    audit_parser.add_argument("--source-repo", default="")
    audit_parser.add_argument("--owner-unit", default="")
    audit_parser.add_argument("--code", default="")
    audit_parser.add_argument("--json", action="store_true")

    seed_parser = subcommands.add_parser("seed", help="Write the bundled seed ledger to the project ledger path")
    seed_parser.add_argument("--merge", action="store_true", default=False)
    seed_parser.add_argument("--json", action="store_true")

    export_parser = subcommands.add_parser("export", help="Export ledger as JSON-compatible YAML or JSON")
    export_parser.add_argument("--output", default="")
    export_parser.add_argument("--format", choices=["json", "yaml"], default="json")
    _add_query_args(export_parser)

    upsert_parser = subcommands.add_parser("upsert", help="Upsert one ledger entry from a JSON file")
    upsert_parser.add_argument("entry_json")
    upsert_parser.add_argument("--json", action="store_true")

    readiness_parser = subcommands.add_parser("readiness", help="Show execution-unit readiness")
    readiness_parser.add_argument("--owner-unit", "--unit", dest="owner_unit", default="")
    readiness_parser.add_argument("--base", default="")
    readiness_parser.add_argument("--cached", action="store_true", default=False)
    readiness_parser.add_argument("--json", action="store_true")

    report_parser = subcommands.add_parser("report", help="Build full ledger coverage/debt/readiness report")
    report_parser.add_argument("--owner-unit", "--unit", dest="owner_unit", default="")
    report_parser.add_argument("--base", default="")
    report_parser.add_argument("--cached", action="store_true", default=False)
    report_parser.add_argument("--json", action="store_true")

    accounting_parser = subcommands.add_parser("accounting", help="Build source/unit/target internalization accounting report")
    accounting_parser.add_argument("--owner-unit", "--unit", dest="owner_unit", default="")
    accounting_parser.add_argument("--no-entries", action="store_true", default=False)
    accounting_parser.add_argument("--fail-on-error", action="store_true", default=False)
    accounting_parser.add_argument("--markdown", action="store_true", default=False)
    accounting_parser.add_argument("--json", action="store_true")

    matrix_parser = subcommands.add_parser("matrix", help="Show execution-unit dependency and ledger coverage matrix")
    matrix_parser.add_argument("--json", action="store_true")

    gate_parser = subcommands.add_parser("gate", help="Run the execution-unit completion gate")
    gate_parser.add_argument("--owner-unit", "--unit", dest="owner_unit", required=True)
    gate_parser.add_argument("--base", default="")
    gate_parser.add_argument("--cached", action="store_true", default=False)
    gate_parser.add_argument("--minimum-effective-lines", type=int, default=0)
    gate_parser.add_argument("--include-source-scan", action="store_true", default=False)
    gate_parser.add_argument("--source-root", default="")
    gate_parser.add_argument("--fail-on-error", action="store_true", default=False)
    gate_parser.add_argument("--json", action="store_true")

    source_scan_parser = subcommands.add_parser("source-scan", help="Verify source evidence, target paths, and forbidden parent repo dependencies")
    source_scan_parser.add_argument("--source-root", default="")
    source_scan_parser.add_argument("--include-tests", action="store_true", default=False)
    source_scan_parser.add_argument("--json", action="store_true")

    linecount_parser = subcommands.add_parser("linecount", help="Run strict effective line-count classification")
    linecount_parser.add_argument("--base", required=True)
    linecount_parser.add_argument("--head", default="HEAD")
    linecount_parser.add_argument("--cached", action="store_true", default=False)
    linecount_parser.add_argument("--owner-unit", "--unit", dest="owner_unit", default="")
    linecount_parser.add_argument("--minimum-effective-lines", type=int, default=0)
    linecount_parser.add_argument("--fail-on-shortfall", action="store_true", default=False)
    linecount_parser.add_argument("--json", action="store_true")

    buckets_parser = subcommands.add_parser("buckets", help="Run strict production/test/data/vendor/mock line-count buckets")
    buckets_parser.add_argument("--base", required=True)
    buckets_parser.add_argument("--head", default="HEAD")
    buckets_parser.add_argument("--cached", action="store_true", default=False)
    buckets_parser.add_argument("--owner-unit", "--unit", dest="owner_unit", default="")
    buckets_parser.add_argument("--minimum-effective-lines", type=int, default=0)
    buckets_parser.add_argument("--fail-on-shortfall", action="store_true", default=False)
    buckets_parser.add_argument("--json", action="store_true")

    boundary_parser = subcommands.add_parser("boundary", help="Audit clean-submission boundary and parent source repo dependencies")
    boundary_parser.add_argument("--no-tests", action="store_true", default=False)
    boundary_parser.add_argument("--include-cache", action="store_true", default=False)
    boundary_parser.add_argument("--roots", default="")
    boundary_parser.add_argument("--fail-on-error", action="store_true", default=False)
    boundary_parser.add_argument("--json", action="store_true")

    reachability_parser = subcommands.add_parser("reachability", help="Verify ledger main-path API/CLI/event/runtime/test reachability")
    reachability_parser.add_argument("--owner-unit", "--unit", dest="owner_unit", default="")
    reachability_parser.add_argument("--no-entries", action="store_true", default=False)
    reachability_parser.add_argument("--fail-on-error", action="store_true", default=False)
    reachability_parser.add_argument("--json", action="store_true")

    acceptance_parser = subcommands.add_parser("acceptance", help="Build the M1-01A anti-fake-internalization acceptance report")
    acceptance_parser.add_argument("--owner-unit", "--unit", dest="owner_unit", default="M1-01A")
    acceptance_parser.add_argument("--base", default="")
    acceptance_parser.add_argument("--cached", action="store_true", default=False)
    acceptance_parser.add_argument("--include-entries", action="store_true", default=False)
    acceptance_parser.add_argument("--strict-audit", action="store_true", default=False)
    acceptance_parser.add_argument("--boundary-roots", default="")
    acceptance_parser.add_argument("--fail-on-error", action="store_true", default=False)
    acceptance_parser.add_argument("--json", action="store_true")

    persistence_parser = subcommands.add_parser("persistence", help="Show atomic ledger revision and mutation journal state")
    persistence_parser.add_argument("--json", action="store_true")

    cleanroom_parser = subcommands.add_parser("cleanroom", help="Build clean-directory verification plan and blockers")
    cleanroom_parser.add_argument("--source-root", default="")
    cleanroom_parser.add_argument("--no-source-scan", action="store_true", default=False)
    cleanroom_parser.add_argument("--roots", default="")
    cleanroom_parser.add_argument("--fail-on-error", action="store_true", default=False)
    cleanroom_parser.add_argument("--json", action="store_true")

    semantics_parser = subcommands.add_parser("semantic-effects", help="Run semantic effect probes for anti-fake internalization")
    semantics_parser.add_argument("--fail-on-error", action="store_true", default=False)
    semantics_parser.add_argument("--json", action="store_true")

    test_quality_parser = subcommands.add_parser("test-quality", help="Audit ledger test entries for behavior coverage")
    test_quality_parser.add_argument("--owner-unit", "--unit", dest="owner_unit", default="")
    test_quality_parser.add_argument("--include-entries", action="store_true", default=False)
    test_quality_parser.add_argument("--fail-on-error", action="store_true", default=False)
    test_quality_parser.add_argument("--json", action="store_true")

    schema_parser = subcommands.add_parser("schema-contract", help="Audit ledger schema contract and roundtrip behavior")
    schema_parser.add_argument("--owner-unit", "--unit", dest="owner_unit", default="")
    schema_parser.add_argument("--include-entries", action="store_true", default=False)
    schema_parser.add_argument("--fail-on-error", action="store_true", default=False)
    schema_parser.add_argument("--json", action="store_true")

    graph_parser = subcommands.add_parser("evidence-graph", help="Build source-to-target evidence graph")
    graph_parser.add_argument("--owner-unit", "--unit", dest="owner_unit", default="")
    graph_parser.add_argument("--include-nodes", action="store_true", default=False)
    graph_parser.add_argument("--fail-on-error", action="store_true", default=False)
    graph_parser.add_argument("--json", action="store_true")

    custody_parser = subcommands.add_parser("state-custody", help="Audit Zyra-owned state custody map")
    custody_parser.add_argument("--fail-on-error", action="store_true", default=False)
    custody_parser.add_argument("--json", action="store_true")

    mutation_parser = subcommands.add_parser("mutation-consistency", help="Run mutation, journal, and event causality probes")
    mutation_parser.add_argument("--fail-on-error", action="store_true", default=False)
    mutation_parser.add_argument("--json", action="store_true")

    policy_matrix_parser = subcommands.add_parser("policy-matrix", help="Audit lifecycle/status/strategy policy matrix")
    policy_matrix_parser.add_argument("--owner-unit", "--unit", dest="owner_unit", default="")
    policy_matrix_parser.add_argument("--include-decisions", action="store_true", default=False)
    policy_matrix_parser.add_argument("--fail-on-error", action="store_true", default=False)
    policy_matrix_parser.add_argument("--json", action="store_true")

    unit_review_parser = subcommands.add_parser("unit-review", help="Build M1-01A execution-unit self-review matrix")
    unit_review_parser.add_argument("--owner-unit", "--unit", dest="owner_unit", default="M1-01A")
    unit_review_parser.add_argument("--base", default="")
    unit_review_parser.add_argument("--cached", action="store_true", default=False)
    unit_review_parser.add_argument("--minimum-effective-lines", type=int, default=10000)
    unit_review_parser.add_argument("--include-reports", action="store_true", default=False)
    unit_review_parser.add_argument("--boundary-roots", default="")
    unit_review_parser.add_argument("--markdown", action="store_true", default=False)
    unit_review_parser.add_argument("--fail-on-error", action="store_true", default=False)
    unit_review_parser.add_argument("--json", action="store_true")

    classify_parser = subcommands.add_parser("classify-path", help="Classify paths for effective line-count policy")
    classify_parser.add_argument("paths", nargs="+")
    classify_parser.add_argument("--json", action="store_true")

    snapshot_parser = subcommands.add_parser("snapshot", help="Create an audit/readiness/line-count snapshot")
    snapshot_parser.add_argument("--label", default="")
    snapshot_parser.add_argument("--owner-unit", "--unit", dest="owner_unit", default="")
    snapshot_parser.add_argument("--base", default="")
    snapshot_parser.add_argument("--output", default="")
    snapshot_parser.add_argument("--no-entries", action="store_true", default=False)
    snapshot_parser.add_argument("--json", action="store_true")

    snapshots_parser = subcommands.add_parser("snapshots", help="List stored ledger snapshots")
    snapshots_parser.add_argument("--json", action="store_true")

    diff_parser = subcommands.add_parser("diff", help="Diff two stored ledger snapshots")
    diff_parser.add_argument("base_snapshot")
    diff_parser.add_argument("head_snapshot")
    diff_parser.add_argument("--json", action="store_true")

    advance_parser = subcommands.add_parser("advance", help="Advance one ledger entry through the guarded lifecycle")
    advance_parser.add_argument("ledger_id")
    advance_parser.add_argument("--lifecycle", default="")
    advance_parser.add_argument("--status", "--main-path-status", dest="main_path_status", default="")
    advance_parser.add_argument("--reason", required=True)
    advance_parser.add_argument("--note", default="")
    advance_parser.add_argument("--effective-lines", type=int, default=0)
    advance_parser.add_argument("--actor", default="cli")
    advance_parser.add_argument("--force", action="store_true", default=False)
    advance_parser.add_argument("--no-event", action="store_true", default=False)
    advance_parser.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    project_root = Path(args.project_root).resolve()
    ledger_path = Path(args.ledger_path) if args.ledger_path else project_ledger_path(project_root)
    if not ledger_path.is_absolute():
        ledger_path = project_root / ledger_path

    if args.command == "seed":
        seed = load_seed_ledger()
        if args.merge and ledger_path.exists():
            current = InternalizationLedger.load(ledger_path, normalize_current_policy=True)
            for entry in seed.entries():
                current.upsert(entry)
            current.save(ledger_path)
            payload = {"ledger_path": str(ledger_path), "summary": current.summary().to_dict(), "mutations": [to_jsonable(item) for item in current.mutations]}
        else:
            seed.save(ledger_path)
            payload = {"ledger_path": str(ledger_path), "summary": seed.summary().to_dict()}
        _print_payload(payload, as_json=args.json)
        return 0

    ledger = (
        InternalizationLedger.load(ledger_path, normalize_current_policy=True)
        if ledger_path.exists()
        else load_project_ledger(project_root, bootstrap=True)
    )

    if args.command == "list":
        query = parse_query(_args_to_query_params(args))
        entries = [entry.to_dict() for entry in ledger.query(query)]
        payload = {"summary": ledger.summary().to_dict(), "entries": entries}
        _print_payload(payload, as_json=args.json)
        return 0

    if args.command == "show":
        entry = ledger.get(args.ledger_id)
        if entry is None:
            print(f"Unknown ledger entry: {args.ledger_id}", file=sys.stderr)
            return 2
        _print_payload(entry.to_dict(), as_json=args.json)
        return 0

    if args.command == "audit":
        report = InternalizationLedgerAuditor(project_root, strict=args.strict).audit(ledger)
        filtered_findings = filter_findings(
            report.findings,
            severity=args.severity,
            source_repo=args.source_repo,
            owner_unit=args.owner_unit,
            code=args.code,
        )
        payload = report.to_dict()
        if any([args.severity, args.source_repo, args.owner_unit, args.code]):
            payload["findings"] = [finding.to_dict() for finding in filtered_findings]
            payload["filtered_finding_count"] = len(filtered_findings)
        if args.write_event:
            event_log = Path(args.event_log) if args.event_log else project_root / "tmp" / "events.jsonl"
            append_jsonl_event(audit_event_payload(report, trigger="cli"), event_log)
            payload["event_log"] = str(event_log)
            payload["event_written"] = True
        _print_payload(payload, as_json=args.json)
        if args.fail_on_error and not report.ok:
            return 1
        return 0

    if args.command == "export":
        query = parse_query(_args_to_query_params(args))
        export_ledger = InternalizationLedger(ledger.query(query))
        output_payload = export_ledger.to_dict()
        text = json.dumps(output_payload, ensure_ascii=False, indent=2, sort_keys=True)
        if args.output:
            output = Path(args.output)
            if not output.is_absolute():
                output = project_root / output
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text + "\n", encoding="utf-8")
            print(str(output))
        else:
            print(text)
        return 0

    if args.command == "upsert":
        entry_path = Path(args.entry_json)
        if not entry_path.is_absolute():
            entry_path = project_root / entry_path
        entry = InternalizationLedgerEntry.from_dict(json.loads(entry_path.read_text(encoding="utf-8")))
        validation_errors = entry.validate()
        if validation_errors:
            _print_payload({"ok": False, "errors": validation_errors}, as_json=args.json)
            return 1
        mutation = ledger.upsert(entry)
        ledger.save(ledger_path)
        _print_payload({"mutation": to_jsonable(mutation), "ledger_path": str(ledger_path)}, as_json=args.json)
        return 0

    if args.command == "readiness":
        audit = InternalizationLedgerAuditor(project_root, strict=True).audit(ledger)
        line_count = _line_count_for_optional_base(project_root, args.base, args.cached, args.owner_unit)
        report = build_unit_readiness_report(
            project_root,
            ledger,
            owner_unit=args.owner_unit,
            audit_report=audit,
            line_count_report=line_count,
        )
        _print_payload(report.to_dict(), as_json=args.json)
        return 0

    if args.command == "report":
        line_count = _line_count_for_optional_base(project_root, args.base, args.cached, args.owner_unit)
        payload = build_full_ledger_report(
            project_root,
            ledger,
            owner_unit=args.owner_unit,
            line_count_report=line_count,
        )
        _print_payload(payload, as_json=args.json)
        return 0

    if args.command == "accounting":
        report = build_accounting_report(
            project_root,
            ledger,
            owner_unit=args.owner_unit,
            include_entries=not args.no_entries,
        )
        if args.markdown:
            print(accounting_markdown(report))
            return 1 if args.fail_on_error and not report.ok else 0
        payload = report.to_dict()
        _print_payload(payload, as_json=args.json)
        if args.fail_on_error and not report.ok:
            return 1
        return 0

    if args.command == "matrix":
        _print_payload(build_unit_matrix(ledger).to_dict(), as_json=args.json)
        return 0

    if args.command == "gate":
        minimum = args.minimum_effective_lines or minimum_effective_lines_for_unit(args.owner_unit)
        source_root = Path(args.source_root).resolve() if args.source_root else project_root.parent
        payload = build_completion_gate_report(
            project_root,
            ledger,
            owner_unit=args.owner_unit,
            base_commit=args.base,
            cached=args.cached,
            minimum_effective_lines=minimum,
            source_root=source_root,
            include_source_scan=args.include_source_scan,
        ).to_dict()
        _print_payload(payload, as_json=args.json)
        if args.fail_on_error and not payload.get("ok"):
            return 1
        return 0

    if args.command == "source-scan":
        source_root = Path(args.source_root).resolve() if args.source_root else project_root.parent
        payload = build_source_scan_report(
            project_root,
            source_root,
            ledger,
            include_tests=args.include_tests,
        ).to_dict()
        _print_payload(payload, as_json=args.json)
        return 0

    if args.command == "linecount":
        minimum = args.minimum_effective_lines or minimum_effective_lines_for_unit(args.owner_unit)
        report = build_line_count_report(
            project_root,
            base=args.base,
            head=args.head,
            cached=args.cached,
            minimum_effective_lines=minimum,
        )
        payload = line_count_payload(report)
        _print_payload(payload, as_json=args.json)
        if args.fail_on_shortfall and not report.ok:
            return 1
        return 0

    if args.command == "buckets":
        minimum = args.minimum_effective_lines or minimum_effective_lines_for_unit(args.owner_unit)
        report = build_line_bucket_report(
            project_root,
            base=args.base,
            head=args.head,
            cached=args.cached,
            minimum_effective_lines=minimum,
        )
        payload = line_bucket_payload(report)
        _print_payload(payload, as_json=args.json)
        if args.fail_on_shortfall and not report.ok:
            return 1
        return 0

    if args.command == "boundary":
        report = build_clean_boundary_report(
            project_root,
            include_tests=not args.no_tests,
            include_cache=args.include_cache,
            scan_roots=_csv_list(args.roots),
        )
        payload = boundary_payload(report)
        _print_payload(payload, as_json=args.json)
        if args.fail_on_error and not report.ok:
            return 1
        return 0

    if args.command == "reachability":
        report = build_reachability_report(
            project_root,
            ledger,
            owner_unit=args.owner_unit,
            include_entries=not args.no_entries,
        )
        payload = reachability_payload(report)
        _print_payload(payload, as_json=args.json)
        if args.fail_on_error and not report.ok:
            return 1
        return 0

    if args.command == "acceptance":
        line_count = _line_count_for_optional_base(project_root, args.base, args.cached, args.owner_unit)
        audit = InternalizationLedgerAuditor(project_root, strict=args.strict_audit).audit(ledger)
        boundary = build_clean_boundary_report(
            project_root,
            include_tests=True,
            include_cache=False,
            scan_roots=_csv_list(args.boundary_roots),
        )
        reachability = build_reachability_report(
            project_root,
            ledger,
            owner_unit=args.owner_unit,
            include_entries=args.include_entries,
            strict_audit=args.strict_audit,
        )
        report = build_acceptance_report(
            project_root,
            ledger,
            owner_unit=args.owner_unit,
            audit_report=audit,
            line_count_report=line_count,
            reachability_report=reachability,
            boundary_report=boundary,
            include_entries=args.include_entries,
        )
        payload = acceptance_payload(report)
        _print_payload(payload, as_json=args.json)
        if args.fail_on_error and not report.ok:
            return 1
        return 0

    if args.command == "persistence":
        payload = persistence_payload(AtomicLedgerStore(project_root, ledger_path=ledger_path))
        _print_payload(payload, as_json=args.json)
        return 0

    if args.command == "cleanroom":
        source_root = Path(args.source_root).resolve() if args.source_root else None
        report = build_cleanroom_report(
            project_root,
            ledger,
            source_root=source_root,
            include_source_scan=not args.no_source_scan,
            scan_roots=_csv_list(args.roots),
        )
        payload = cleanroom_payload(report)
        _print_payload(payload, as_json=args.json)
        if args.fail_on_error and not report.ok:
            return 1
        return 0

    if args.command == "semantic-effects":
        report = build_semantic_effect_report(project_root)
        payload = semantic_payload(report)
        _print_payload(payload, as_json=args.json)
        if args.fail_on_error and not report.ok:
            return 1
        return 0

    if args.command == "test-quality":
        report = build_test_quality_report(
            project_root,
            ledger,
            owner_unit=args.owner_unit,
            include_entries=args.include_entries,
        )
        payload = test_quality_payload(report)
        _print_payload(payload, as_json=args.json)
        if args.fail_on_error and not report.ok:
            return 1
        return 0

    if args.command == "schema-contract":
        report = build_schema_contract_report(
            ledger,
            owner_unit=args.owner_unit,
            include_entries=args.include_entries,
        )
        payload = schema_contract_payload(report)
        _print_payload(payload, as_json=args.json)
        if args.fail_on_error and not report.ok:
            return 1
        return 0

    if args.command == "evidence-graph":
        report = build_evidence_graph_report(
            project_root,
            ledger,
            owner_unit=args.owner_unit,
            include_nodes=args.include_nodes,
        )
        payload = evidence_graph_payload(report)
        _print_payload(payload, as_json=args.json)
        if args.fail_on_error and not report.ok:
            return 1
        return 0

    if args.command == "state-custody":
        report = build_state_custody_report(project_root)
        payload = state_custody_payload(report)
        _print_payload(payload, as_json=args.json)
        if args.fail_on_error and not report.ok:
            return 1
        return 0

    if args.command == "mutation-consistency":
        report = build_mutation_consistency_report(project_root)
        payload = mutation_consistency_payload(report)
        _print_payload(payload, as_json=args.json)
        if args.fail_on_error and not report.ok:
            return 1
        return 0

    if args.command == "policy-matrix":
        report = build_policy_matrix_report(
            ledger,
            owner_unit=args.owner_unit,
            include_decisions=args.include_decisions,
        )
        payload = policy_matrix_payload(report)
        _print_payload(payload, as_json=args.json)
        if args.fail_on_error and not report.ok:
            return 1
        return 0

    if args.command == "unit-review":
        report = build_unit_review_report(
            project_root,
            ledger,
            owner_unit=args.owner_unit,
            base_commit=args.base,
            cached=args.cached,
            minimum_effective_lines=args.minimum_effective_lines,
            include_reports=args.include_reports,
            boundary_roots=_csv_list(args.boundary_roots) or None,
        )
        if args.markdown:
            print(unit_review_markdown(report))
        else:
            _print_payload(unit_review_payload(report), as_json=args.json)
        if args.fail_on_error and not report.ok:
            return 1
        return 0

    if args.command == "classify-path":
        payload = {"paths": [classify_path(path).to_dict() for path in args.paths]}
        _print_payload(payload, as_json=args.json)
        return 0

    if args.command == "snapshot":
        snapshot = build_snapshot(
            project_root,
            ledger,
            label=args.label,
            owner_unit=args.owner_unit,
            base_commit=args.base,
            include_entries=not args.no_entries,
            metadata={"trigger": "cli"},
        )
        if args.output:
            output = Path(args.output)
            if not output.is_absolute():
                output = project_root / output
            path = save_snapshot(snapshot, output)
        else:
            path = save_snapshot_to_default_dir(project_root, snapshot)
        payload = {"snapshot": snapshot.to_dict(), "path": str(path)}
        _print_payload(payload, as_json=args.json)
        return 0

    if args.command == "snapshots":
        _print_payload({"snapshots": list_snapshots(project_root)}, as_json=args.json)
        return 0

    if args.command == "diff":
        base_snapshot = load_snapshot(_resolve_project_path(project_root, args.base_snapshot))
        head_snapshot = load_snapshot(_resolve_project_path(project_root, args.head_snapshot))
        payload = diff_snapshots(base_snapshot, head_snapshot).to_dict()
        _print_payload(payload, as_json=args.json)
        return 0

    if args.command == "advance":
        request = LedgerAdvanceRequest.from_dict(
            {
                "ledger_id": args.ledger_id,
                "lifecycle": args.lifecycle,
                "main_path_status": args.main_path_status,
                "reason": args.reason,
                "note": args.note,
                "effective_lines": args.effective_lines,
                "actor": args.actor,
                "write_event": not args.no_event,
                "force": args.force,
            }
        )
        result = LedgerWorkflow(project_root, ledger).advance(request)
        if result.mutation is not None:
            save_project_ledger(project_root, ledger)
        _print_payload(result.to_dict(), as_json=args.json)
        return 0 if result.ok else 1

    parser.print_help()
    return 2


def _add_query_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-repo", default="")
    parser.add_argument("--owner-unit", default="")
    parser.add_argument("--milestone", default="")
    parser.add_argument("--lifecycle", default="")
    parser.add_argument("--status", default="")
    parser.add_argument("--strategy", default="")
    parser.add_argument("--target", default="")
    parser.add_argument("--q", default="")
    parser.add_argument("--tag", default="")
    parser.add_argument("--limit", type=int, default=200)


def _args_to_query_params(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "source_repo": getattr(args, "source_repo", ""),
        "owner_unit": getattr(args, "owner_unit", ""),
        "milestone": getattr(args, "milestone", ""),
        "lifecycle": getattr(args, "lifecycle", ""),
        "status": getattr(args, "status", ""),
        "strategy": getattr(args, "strategy", ""),
        "target": getattr(args, "target", ""),
        "q": getattr(args, "q", ""),
        "tag": getattr(args, "tag", ""),
        "limit": getattr(args, "limit", 200),
    }


def _print_payload(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    entries = payload.get("entries")
    if (
        isinstance(entries, list)
        and (not entries or all(
            isinstance(entry, dict)
            and {"ledger_id", "source_repo", "owner_unit", "main_path_status", "capability_name"}.issubset(entry)
            for entry in entries
        ))
        and "findings" not in payload
    ):
        for entry in entries:
            print(f"{entry['ledger_id']} {entry['source_repo']} {entry['owner_unit']} {entry['main_path_status']} {entry['capability_name']}")
        print(f"total={payload.get('summary', {}).get('total_entries', len(entries))}")
        return
    if "findings" in payload:
        finding_count = payload.get("finding_count", len(payload.get("findings", [])))
        error_count = payload.get("error_count", 0)
        blocker_count = payload.get("blocker_count", 0)
        owner_unit = payload.get("owner_unit", "")
        prefix = f"unit={owner_unit} " if owner_unit else ""
        print(
            f"{prefix}ok={payload.get('ok')} disposition={payload.get('disposition')} "
            f"findings={finding_count} errors={error_count} blockers={blocker_count}"
        )
        for finding in payload["findings"][:50]:
            print(f"{finding['severity']} {finding['code']} {finding.get('ledger_id', '')}: {finding['message']}")
        return
    if "effective_added" in payload and "minimum_effective_lines" in payload:
        print(
            f"effective_added={payload['effective_added']} raw_added={payload.get('raw_added', 0)} "
            f"excluded_added={payload.get('excluded_added', 0)} minimum={payload['minimum_effective_lines']} ok={payload.get('ok')}"
        )
        return
    if "ready_for_advance" in payload and "owner_unit" in payload:
        print(
            f"unit={payload['owner_unit']} entries={payload['total_entries']} ready={payload['ready_for_advance']} "
            f"blocked={payload['blocked_entries']} effective_added={payload.get('effective_added', 0)} ok={payload.get('ok')}"
        )
        return
    if "summary" in payload and "source_accounts" in payload and "unit_accounts" in payload:
        summary = payload["summary"]
        print(
            f"ok={summary['ok']} entries={summary['total_entries']} sources={summary['source_repo_count']} "
            f"units={summary['owner_unit_count']} targets={summary['target_path_count']} "
            f"errors={summary['error_count']} warnings={summary['warning_count']}"
        )
        for finding in payload.get("findings", [])[:25]:
            print(f"{finding['severity']} {finding['code']} {finding.get('ledger_id') or finding.get('target_path', '')}: {finding['message']}")
        return
    if "snapshot" in payload and "path" in payload:
        print(payload["path"])
        return
    if "snapshots" in payload:
        for snapshot in payload["snapshots"]:
            print(f"{snapshot['snapshot_id']} {snapshot['created_at']} {snapshot['label']} {snapshot['path']}")
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _resolve_project_path(project_root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root / path


def _line_count_for_optional_base(project_root: Path, base: str, cached: bool, owner_unit: str) -> Any:
    if not base:
        return None
    return build_line_count_report(
        project_root,
        base=base,
        cached=cached,
        minimum_effective_lines=minimum_effective_lines_for_unit(owner_unit),
    )


def _csv_list(value: str) -> list[str] | None:
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or None


if __name__ == "__main__":
    raise SystemExit(main())
