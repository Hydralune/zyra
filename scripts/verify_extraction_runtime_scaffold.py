from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations.extraction_acceptance import assert_m1_01b_acceptance, build_m1_01b_acceptance_report


def main() -> None:
    base = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else ""
    report = build_m1_01b_acceptance_report(ROOT, base_commit=base)
    assert_m1_01b_acceptance(report)
    print(json.dumps(report.summary(), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
