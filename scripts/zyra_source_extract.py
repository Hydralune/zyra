from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations.source_extraction import (  # noqa: E402
    OverwritePolicy,
    SourceExtractor,
    claude_code_m1_01b_plan,
    write_runtime_scaffold_files,
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

    smoke = subcommands.add_parser("smoke", help="Verify the M1-01B runtime extraction scaffold")
    smoke.add_argument("--json", action="store_true")
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

    if args.command == "smoke":
        payload = smoke_payload(project_root)
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
