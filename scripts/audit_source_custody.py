from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages" / "integrations"
if str(PACKAGE) not in sys.path:
    sys.path.insert(0, str(PACKAGE))

from zyra_integrations.source_custody.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
