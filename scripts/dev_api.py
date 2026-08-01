from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (
    ROOT,
    ROOT / "apps" / "api",
    ROOT / "packages" / "code_index",
    ROOT / "packages" / "commands",
    ROOT / "packages" / "core",
    ROOT / "packages" / "evaluation",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "integrations" / "loopx_runtime",
    ROOT / "packages" / "memory",
    ROOT / "packages" / "orchestration",
    ROOT / "packages" / "productization",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "scheduler",
    ROOT / "packages" / "skills",
    ROOT / "packages" / "symbolic",
    ROOT / "packages" / "workers",
    ROOT / "packages" / "workspace",
)
for source_root in reversed(SOURCE_ROOTS):
    rendered = str(source_root)
    if rendered not in sys.path:
        sys.path.insert(0, rendered)

from apps.api.zyra_api.main import run


if __name__ == "__main__":
    run()
