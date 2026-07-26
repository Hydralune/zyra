from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for package in (
    ROOT / "packages" / "evaluation",
    ROOT / "packages" / "integrations",
):
    if str(package) not in sys.path:
        sys.path.insert(0, str(package))

from zyra_evaluation.freeze_audit.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
