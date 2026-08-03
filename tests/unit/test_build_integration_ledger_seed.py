from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
for package_path in (
    ROOT / "packages" / "integrations",
    ROOT / "scripts",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from build_integration_ledger_seed import build_seed_entries  # noqa: E402
from zyra_integrations import load_seed_ledger  # noqa: E402


def test_repository_local_seed_build_reemits_canonical_frozen_ledger() -> None:
    rebuilt = build_seed_entries()
    canonical = load_seed_ledger().entries()

    assert len(rebuilt) == len(canonical)
    expected = {
        entry.ledger_id: (
            entry.source_repo,
            entry.source_path,
            entry.capability_name,
            entry.owner_unit,
            entry.lifecycle,
            entry.main_path_status,
        )
        for entry in canonical
    }
    actual = {
        entry.ledger_id: (
            entry.source_repo,
            entry.source_path,
            entry.capability_name,
            entry.owner_unit,
            entry.lifecycle,
            entry.main_path_status,
        )
        for entry in rebuilt
    }
    assert actual == expected
