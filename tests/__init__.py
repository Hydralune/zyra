"""Zyra test package with the monorepo's product packages on ``sys.path``."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package in (
    "core",
    "runtime",
    "integrations",
    "workers",
    "skills",
    "commands",
    "memory",
    "scheduler",
):
    path = ROOT / "packages" / package
    if path.exists() and str(path) not in sys.path:
        sys.path.insert(0, str(path))
