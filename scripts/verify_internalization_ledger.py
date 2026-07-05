from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations import InternalizationLedgerAuditor, load_project_ledger, project_ledger_path
from zyra_integrations.ledger_audit import REQUIRED_SOURCE_REPOS


COUNTED_PATHS = ["apps", "packages", "tests", "scripts", "vendor-runtimes", "skills"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Zyra internalization ledger readiness.")
    parser.add_argument("--base", default="", help="Base commit for effective line-count numstat.")
    parser.add_argument("--cached", action="store_true", help="Count staged changes against --base instead of committed HEAD.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-warning", action="store_true")
    args = parser.parse_args()

    ledger = load_project_ledger(ROOT, bootstrap=True)
    report = InternalizationLedgerAuditor(ROOT, strict=True).audit(ledger)
    repos = set(ledger.summary().by_source_repo)
    missing_repos = sorted(REQUIRED_SOURCE_REPOS - repos)
    numstat = diff_numstat(args.base, cached=args.cached) if args.base else {"base": "", "cached": args.cached, "added": 0, "deleted": 0, "files": []}
    payload = {
        "ok": report.ok and not missing_repos and (not args.fail_on_warning or report.warning_count == 0),
        "ledger_path": str(project_ledger_path(ROOT)),
        "seed_entry_count": len(ledger),
        "required_source_repos": sorted(REQUIRED_SOURCE_REPOS),
        "missing_source_repos": missing_repos,
        "audit": {
            "ok": report.ok,
            "disposition": str(report.disposition),
            "error_count": report.error_count,
            "blocker_count": report.blocker_count,
            "warning_count": report.warning_count,
            "finding_count": report.finding_count,
        },
        "numstat": numstat,
        "counted_paths": COUNTED_PATHS,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"ledger={payload['ledger_path']}")
        print(f"entries={payload['seed_entry_count']} missing_repos={missing_repos}")
        print(f"audit_ok={report.ok} errors={report.error_count} blockers={report.blocker_count} warnings={report.warning_count}")
        if args.base:
            print(f"added={numstat['added']} deleted={numstat['deleted']} files={len(numstat['files'])}")

    if not payload["ok"]:
        raise SystemExit(1)


def diff_numstat(base: str, *, cached: bool = False) -> dict[str, Any]:
    command = ["git", "diff", "--numstat"]
    if cached:
        command.append("--cached")
    command.extend([base])
    if not cached:
        command.append("HEAD")
    command.extend(["--", *COUNTED_PATHS])
    completed = subprocess.run(command, cwd=ROOT, check=True, text=True, capture_output=True)
    files: list[dict[str, Any]] = []
    added = 0
    deleted = 0
    for line in completed.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add_text, del_text, path = parts[0], parts[1], parts[2]
        add_count = int(add_text) if add_text.isdigit() else 0
        del_count = int(del_text) if del_text.isdigit() else 0
        added += add_count
        deleted += del_count
        files.append({"path": path, "added": add_count, "deleted": del_count})
    return {"base": base, "cached": cached, "added": added, "deleted": deleted, "files": files}


if __name__ == "__main__":
    main()
