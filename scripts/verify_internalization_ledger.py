from __future__ import annotations

import argparse
import json
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

from zyra_integrations import (
    InternalizationLedgerAuditor,
    build_line_count_report,
    line_count_payload,
    load_project_ledger,
    minimum_effective_lines_for_unit,
    project_ledger_path,
)
from zyra_integrations.ledger_audit import REQUIRED_SOURCE_REPOS


COUNTED_PATHS = ["apps", "packages", "tests", "scripts", "vendor-runtimes", "skills"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Zyra internalization ledger readiness.")
    parser.add_argument("--base", default="", help="Base commit for effective line-count numstat.")
    parser.add_argument("--cached", action="store_true", help="Count staged changes against --base instead of committed HEAD.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-warning", action="store_true")
    parser.add_argument("--unit", default="", help="Execution unit budget, e.g. M1-01A.")
    parser.add_argument("--minimum-effective-lines", type=int, default=0)
    parser.add_argument("--fail-on-shortfall", action="store_true")
    args = parser.parse_args()

    ledger = load_project_ledger(ROOT, bootstrap=True)
    report = InternalizationLedgerAuditor(ROOT, strict=True).audit(ledger)
    repos = set(ledger.summary().by_source_repo)
    missing_repos = sorted(REQUIRED_SOURCE_REPOS - repos)
    minimum_effective_lines = args.minimum_effective_lines or minimum_effective_lines_for_unit(args.unit)
    line_count = (
        build_line_count_report(
            ROOT,
            base=args.base,
            cached=args.cached,
            minimum_effective_lines=minimum_effective_lines,
        )
        if args.base
        else None
    )
    payload = {
        "ok": (
            report.ok
            and not missing_repos
            and (not args.fail_on_warning or report.warning_count == 0)
            and (not args.fail_on_shortfall or line_count is None or line_count.ok)
        ),
        "ledger_path": str(project_ledger_path(ROOT)),
        "seed_entry_count": len(ledger),
        "required_source_repos": sorted(REQUIRED_SOURCE_REPOS),
        "missing_source_repos": missing_repos,
        "unit": args.unit,
        "audit": {
            "ok": report.ok,
            "disposition": str(report.disposition),
            "error_count": report.error_count,
            "blocker_count": report.blocker_count,
            "warning_count": report.warning_count,
            "finding_count": report.finding_count,
        },
        "line_count": line_count_payload(line_count) if line_count else None,
        "counted_paths": COUNTED_PATHS,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"ledger={payload['ledger_path']}")
        print(f"entries={payload['seed_entry_count']} missing_repos={missing_repos}")
        print(f"audit_ok={report.ok} errors={report.error_count} blockers={report.blocker_count} warnings={report.warning_count}")
        if line_count:
            print(
                f"effective_added={line_count.effective_added} raw_added={line_count.raw_added} "
                f"excluded_added={line_count.excluded_added} minimum={line_count.minimum_effective_lines} "
                f"line_count_ok={line_count.ok}"
            )

    if not payload["ok"]:
        raise SystemExit(1)
if __name__ == "__main__":
    main()
