"""Zyra test package with the monorepo's product packages on ``sys.path``."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (
    ROOT / "apps" / "api",
    ROOT / "packages" / "code_index",
    ROOT / "packages" / "commands",
    ROOT / "packages" / "core",
    ROOT / "packages" / "evaluation",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "integrations" / "loopx_runtime",
    (
        ROOT
        / "packages"
        / "integrations"
        / "loopx_runtime"
        / "packages"
        / "loopx-finance-value-discovery"
        / "src"
    ),
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
for path in reversed(SOURCE_ROOTS):
    if path.exists() and str(path) not in sys.path:
        sys.path.insert(0, str(path))
