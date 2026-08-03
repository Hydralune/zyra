from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INTEGRATIONS_ROOT = ROOT / "packages" / "integrations"
for package_path in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    INTEGRATIONS_ROOT,
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_integrations.source_provenance import (  # noqa: E402
    BundledSourceProvenance,
    source_workspace_root,
)
from zyra_integrations import build_source_scan_report, load_seed_ledger  # noqa: E402
from zyra_runtime.claude_productization_foundation import (  # noqa: E402
    ClaudeProductizationFoundation,
)


def test_bundled_source_provenance_is_complete_and_immutable() -> None:
    provenance = BundledSourceProvenance(ROOT)

    receipt = provenance.verify()

    assert receipt["ready"] is True
    assert receipt["repository_count"] == 7
    assert receipt["source_file_count"] >= 125
    assert receipt["identity_index_count"] == 1
    assert receipt["source_identity_count"] >= 1_000
    assert provenance.source_file(
        "claude-code-best",
        "src/QueryEngine.ts",
    ).is_file()


def test_bundled_provenance_preserves_source_workspace_layout() -> None:
    workspace = source_workspace_root(ROOT)

    assert workspace == ROOT / "provenance"
    assert (workspace / "claude-code-best" / "src" / "QueryEngine.ts").is_file()
    assert (
        workspace
        / "source-graphs"
        / "claude-code-best"
        / "source-graph.md"
    ).is_file()
    assert (
        workspace
        / "docs"
        / "milestones"
        / "source-graph-realignment-2026-07-08.md"
    ).is_file()


def test_ledger_source_scan_uses_integrity_checked_frozen_identities() -> None:
    report = build_source_scan_report(
        ROOT,
        ROOT / "provenance",
        load_seed_ledger(),
    )

    assert report.identity_verified_source_count >= 1_200
    assert report.provenance_identity_count >= 1_000
    assert report.filesystem_source_count >= 125
    assert report.missing_source_count <= 30
    assert all(
        item.exists_in_workspace or item.provenance_path
        for item in report.source_verifications
        if item.identity_verified
    )


def test_claude_foundation_has_all_declared_bundled_source_inputs() -> None:
    report = ClaudeProductizationFoundation(ROOT).inspect()
    source_inspections = [
        item
        for boundary in report.boundaries
        for item in boundary.source_inspections
    ]

    assert len(source_inspections) == 29
    assert all(item.exists for item in source_inspections)
    assert not any(
        finding.code == "PRIMARY_SOURCE_MISSING" for finding in report.findings
    )
