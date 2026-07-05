from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "commands",
    ROOT / "packages" / "orchestration",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "workers",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "symbolic",
    ROOT / "packages" / "evaluation",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_evaluation import run_m2_scenarios


def main() -> None:
    output_root = ROOT / "tmp" / "m2-scenarios"
    report = run_m2_scenarios(ROOT, output_root)
    print(json.dumps({"passed": report["passed"], "scenario_count": report["scenario_count"], "output_root": str(output_root)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
