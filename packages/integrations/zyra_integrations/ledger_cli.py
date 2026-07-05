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
from .ledger_matrix import build_unit_matrix
from .ledger_models import InternalizationLedgerEntry, to_jsonable
from .ledger_policy import classify_path, minimum_effective_lines_for_unit
from .ledger_reports import build_full_ledger_report, build_unit_readiness_report
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
            current = InternalizationLedger.load(ledger_path)
            for entry in seed.entries():
                current.upsert(entry)
            current.save(ledger_path)
            payload = {"ledger_path": str(ledger_path), "summary": current.summary().to_dict(), "mutations": [to_jsonable(item) for item in current.mutations]}
        else:
            seed.save(ledger_path)
            payload = {"ledger_path": str(ledger_path), "summary": seed.summary().to_dict()}
        _print_payload(payload, as_json=args.json)
        return 0

    ledger = InternalizationLedger.load(ledger_path) if ledger_path.exists() else load_project_ledger(project_root, bootstrap=True)

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
    if "entries" in payload:
        for entry in payload["entries"]:
            print(f"{entry['ledger_id']} {entry['source_repo']} {entry['owner_unit']} {entry['main_path_status']} {entry['capability_name']}")
        print(f"total={payload['summary']['total_entries']}")
        return
    if "findings" in payload:
        print(f"ok={payload['ok']} disposition={payload['disposition']} findings={payload['finding_count']} errors={payload['error_count']} blockers={payload['blocker_count']}")
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


if __name__ == "__main__":
    raise SystemExit(main())
