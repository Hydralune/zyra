from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .ledger_audit import InternalizationLedgerAuditor, filter_findings
from .ledger_events import append_jsonl_event, audit_event_payload
from .ledger_models import InternalizationLedgerEntry, to_jsonable
from .ledger_store import (
    InternalizationLedger,
    load_project_ledger,
    load_seed_ledger,
    parse_query,
    project_ledger_path,
    save_project_ledger,
)


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
        mutation = ledger.upsert(entry)
        ledger.save(ledger_path)
        _print_payload({"mutation": to_jsonable(mutation), "ledger_path": str(ledger_path)}, as_json=args.json)
        return 0

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
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
