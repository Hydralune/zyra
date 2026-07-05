from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "integrations",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations.ledger_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
