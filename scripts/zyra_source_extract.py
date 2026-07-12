from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "workers",
    ROOT / "packages" / "skills",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations.source_extraction import (  # noqa: E402
    OverwritePolicy,
    SourceExtractor,
    claude_code_m1_01b_plan,
    claude_code_m1_02a_plan,
    claude_code_m1_02b_plan,
    claude_code_m1_02c_plan,
    write_productized_runtime_files,
    write_runtime_scaffold_files,
)
from zyra_integrations.extraction_acceptance import build_m1_01b_acceptance_report, m1_01b_acceptance_payload  # noqa: E402
from zyra_integrations.extraction_rules import build_rule_report_for_plan  # noqa: E402
from zyra_integrations.reference_crosswalk import (  # noqa: E402
    build_claude_code_reference_crosswalk,
    write_claude_code_reference_crosswalk,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Zyra source extraction and runtime scaffold CLI")
    parser.add_argument("--project-root", default=str(ROOT))
    parser.add_argument("--source-workspace-root", default=str(ROOT.parent))
    subcommands = parser.add_subparsers(dest="command", required=True)

    pilot = subcommands.add_parser("pilot-claude-code", help="Run the M1-01B Claude Code pilot extraction")
    pilot.add_argument("--dry-run", action="store_true")
    pilot.add_argument("--overwrite-policy", choices=[item.value for item in OverwritePolicy], default=OverwritePolicy.IF_CHANGED.value)
    pilot.add_argument("--write-ledger", action="store_true")
    pilot.add_argument("--update-seed", action="store_true")
    pilot.add_argument("--write-scaffold", action="store_true")
    pilot.add_argument("--json", action="store_true")

    productized = subcommands.add_parser("productize-claude-code", help="Run the M1-02A Claude Code productized extraction")
    productized.add_argument("--dry-run", action="store_true")
    productized.add_argument("--overwrite-policy", choices=[item.value for item in OverwritePolicy], default=OverwritePolicy.IF_CHANGED.value)
    productized.add_argument("--write-ledger", action="store_true")
    productized.add_argument("--update-seed", action="store_true")
    productized.add_argument("--write-scaffold", action="store_true")
    productized.add_argument("--write-crosswalk", action="store_true")
    productized.add_argument("--json", action="store_true")

    query_session = subcommands.add_parser(
        "productize-claude-query-session",
        help="Run the M1-02B Claude Code query/session lifecycle extraction",
    )
    query_session.add_argument("--dry-run", action="store_true")
    query_session.add_argument(
        "--overwrite-policy",
        choices=[item.value for item in OverwritePolicy],
        default=OverwritePolicy.IF_CHANGED.value,
    )
    query_session.add_argument("--write-ledger", action="store_true")
    query_session.add_argument("--update-seed", action="store_true")
    query_session.add_argument("--json", action="store_true")

    tool_loop = subcommands.add_parser(
        "productize-claude-tool-loop-budget",
        help="Run the M1-02C Claude Code tool loop and result budget extraction",
    )
    tool_loop.add_argument("--dry-run", action="store_true")
    tool_loop.add_argument(
        "--overwrite-policy",
        choices=[item.value for item in OverwritePolicy],
        default=OverwritePolicy.IF_CHANGED.value,
    )
    tool_loop.add_argument("--write-ledger", action="store_true")
    tool_loop.add_argument("--update-seed", action="store_true")
    tool_loop.add_argument("--json", action="store_true")

    smoke = subcommands.add_parser("smoke", help="Verify the M1-01B runtime extraction scaffold")
    smoke.add_argument("--json", action="store_true")
    rules = subcommands.add_parser("audit-rules", help="Audit the M1-01B extraction allowlist/exclude rules")
    rules.add_argument("--json", action="store_true")
    acceptance = subcommands.add_parser("m1-01b-acceptance", help="Run the M1-01B extraction/runtime scaffold acceptance probe")
    acceptance.add_argument("--base", default="")
    acceptance.add_argument("--json", action="store_true")
    productized_smoke = subcommands.add_parser("productized-smoke", help="Verify the M1-02A productized Claude Code runtime")
    productized_smoke.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    project_root = Path(args.project_root).resolve()
    source_workspace_root = Path(args.source_workspace_root).resolve()

    if args.command == "pilot-claude-code":
        if args.write_scaffold and not args.dry_run:
            write_runtime_scaffold_files(project_root)
        plan = claude_code_m1_01b_plan(
            project_root=project_root,
            source_workspace_root=source_workspace_root,
            dry_run=args.dry_run,
            overwrite_policy=OverwritePolicy(args.overwrite_policy),
        )
        extractor = SourceExtractor(plan)
        report = extractor.run()
        ledger_payload = None
        if args.write_ledger and not args.dry_run and report.ok:
            ledger_payload = extractor.upsert_ledger_entries(report, project_ledger=True, seed_ledger=args.update_seed)
        payload = {
            "report": report.to_dict(),
            "ledger": ledger_payload,
        }
        _print_payload(payload, as_json=args.json)
        return 0 if report.ok else 1

    if args.command == "productize-claude-code":
        if args.write_scaffold and not args.dry_run:
            write_productized_runtime_files(project_root)
        plan = claude_code_m1_02a_plan(
            project_root=project_root,
            source_workspace_root=source_workspace_root,
            dry_run=args.dry_run,
            overwrite_policy=OverwritePolicy(args.overwrite_policy),
        )
        extractor = SourceExtractor(plan)
        report = extractor.run()
        ledger_payload = None
        crosswalk_payload = None
        if args.write_crosswalk and not args.dry_run and report.ok:
            crosswalk_path = write_claude_code_reference_crosswalk(
                project_root=project_root,
                source_workspace_root=source_workspace_root,
                target_mount=plan.target_mount,
            )
            crosswalk_report = build_claude_code_reference_crosswalk(
                project_root=project_root,
                source_workspace_root=source_workspace_root,
                target_mount=plan.target_mount,
            )
            crosswalk_payload = {
                "path": str(crosswalk_path.relative_to(project_root)),
                "summary": crosswalk_report.summary(),
            }
        if args.write_ledger and not args.dry_run and report.ok:
            ledger_payload = extractor.upsert_ledger_entries(report, project_ledger=True, seed_ledger=args.update_seed)
        payload = {
            "report": report.to_dict(),
            "ledger": ledger_payload,
            "reference_crosswalk": crosswalk_payload,
        }
        _print_payload(payload, as_json=args.json)
        return 0 if report.ok and (crosswalk_payload is not None or not args.write_crosswalk or args.dry_run) else 1

    if args.command == "productize-claude-query-session":
        plan = claude_code_m1_02b_plan(
            project_root=project_root,
            source_workspace_root=source_workspace_root,
            dry_run=args.dry_run,
            overwrite_policy=OverwritePolicy(args.overwrite_policy),
        )
        extractor = SourceExtractor(plan)
        report = extractor.run()
        ledger_payload = None
        if args.write_ledger and not args.dry_run and report.ok:
            ledger_payload = extractor.upsert_ledger_entries(report, project_ledger=True, seed_ledger=args.update_seed)
        payload = {
            "report": report.to_dict(),
            "ledger": ledger_payload,
        }
        _print_payload(payload, as_json=args.json)
        return 0 if report.ok else 1

    if args.command == "productize-claude-tool-loop-budget":
        plan = claude_code_m1_02c_plan(
            project_root=project_root,
            source_workspace_root=source_workspace_root,
            dry_run=args.dry_run,
            overwrite_policy=OverwritePolicy(args.overwrite_policy),
        )
        extractor = SourceExtractor(plan)
        report = extractor.run()
        ledger_payload = None
        if args.write_ledger and not args.dry_run and report.ok:
            ledger_payload = extractor.upsert_ledger_entries(report, project_ledger=True, seed_ledger=args.update_seed)
        payload = {
            "report": report.to_dict(),
            "ledger": ledger_payload,
        }
        _print_payload(payload, as_json=args.json)
        return 0 if report.ok else 1

    if args.command == "smoke":
        payload = smoke_payload(project_root)
        _print_payload(payload, as_json=args.json)
        return 0 if payload["ok"] else 1

    if args.command == "audit-rules":
        plan = claude_code_m1_01b_plan(project_root=project_root, source_workspace_root=source_workspace_root, dry_run=True)
        payload = build_rule_report_for_plan(plan)
        _print_payload(payload, as_json=args.json)
        return 0 if payload["summary"]["ok"] else 1

    if args.command == "m1-01b-acceptance":
        report = build_m1_01b_acceptance_report(
            project_root,
            source_workspace_root=source_workspace_root,
            base_commit=args.base,
        )
        payload = m1_01b_acceptance_payload(report)
        _print_payload(payload, as_json=args.json)
        return 0 if report.ok else 1

    if args.command == "productized-smoke":
        payload = productized_smoke_payload(project_root)
        _print_payload(payload, as_json=args.json)
        return 0 if payload["ok"] else 1

    parser.print_help()
    return 2


def smoke_payload(project_root: Path) -> dict:
    runtime_root = project_root / "vendor-runtimes" / "claude-code-runtime"
    manifest = runtime_root / "src" / "zyra-pilot-manifest.mjs"
    inventory = runtime_root / "metadata" / "source_inventory.json"
    copied_root = runtime_root / "pilot" / "claude-code-best"
    copied_files = [path for path in copied_root.rglob("*") if path.is_file()] if copied_root.exists() else []
    source_files = [path for path in copied_files if path.suffix.lower() in {".ts", ".tsx", ".js", ".mjs"}]
    return {
        "ok": manifest.exists() and inventory.exists() and len(source_files) > 0,
        "runtime_root": str(runtime_root),
        "manifest_exists": manifest.exists(),
        "inventory_exists": inventory.exists(),
        "copied_file_count": len(copied_files),
        "source_file_count": len(source_files),
    }


def productized_smoke_payload(project_root: Path) -> dict:
    runtime_root = project_root / "vendor-runtimes" / "claude-code-runtime"
    manifest = runtime_root / "src" / "zyra-productized-manifest.mjs"
    inventory = runtime_root / "metadata" / "productized_source_inventory.json"
    crosswalk = runtime_root / "metadata" / "reference_crosswalk.json"
    copied_root = runtime_root / "productized" / "claude-code-best"
    copied_files = [path for path in copied_root.rglob("*") if path.is_file()] if copied_root.exists() else []
    source_files = [path for path in copied_files if path.suffix.lower() in {".ts", ".tsx", ".js", ".mjs"}]
    crosswalk_payload = {}
    if crosswalk.exists():
        crosswalk_payload = json.loads(crosswalk.read_text(encoding="utf-8"))
    inventory_payload = {}
    if inventory.exists():
        inventory_payload = json.loads(inventory.read_text(encoding="utf-8"))
    inventory_summary = inventory_payload.get("summary", {})
    vendor_like_line_count = _count_source_lines(source_files)
    legacy_inventory_effective = inventory_summary.get("effective_line_count", 0)
    return {
        "ok": (
            manifest.exists()
            and inventory.exists()
            and crosswalk.exists()
            and len(source_files) >= 80
            and bool(crosswalk_payload.get("ok", False))
        ),
        "runtime_root": str(runtime_root),
        "manifest_exists": manifest.exists(),
        "inventory_exists": inventory.exists(),
        "crosswalk_exists": crosswalk.exists(),
        "copied_file_count": len(copied_files),
        "source_file_count": len(source_files),
        "effective_line_count": 0,
        "legacy_inventory_effective_line_count": legacy_inventory_effective,
        "vendor_like_line_count": vendor_like_line_count,
        "line_count_policy": "vendor-runtime source pool is vendor_like/review until Zyra-owned adapter glue is bucketed separately",
        "upstream_type_stub_count": inventory_summary.get("upstream_type_stub_count", 0),
        "upstream_type_stub_line_count": inventory_summary.get("upstream_type_stub_line_count", 0),
        "crosswalk_summary": crosswalk_payload.get("summary", {}),
    }


def _count_source_lines(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        try:
            total += len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
    return total


def _print_payload(payload: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if "report" in payload:
        summary = payload["report"]["summary"]
        print(
            f"ok={payload['report']['ok']} copied={summary['copied_count']} skipped={summary['skipped_count']} "
            f"excluded={summary['excluded_count']} effective_lines={summary['effective_line_count']} "
            f"ledger_upserts={summary['ledger_upsert_count']}"
        )
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
