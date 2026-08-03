from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INTEGRATIONS_ROOT = ROOT / "packages" / "integrations"
if str(INTEGRATIONS_ROOT) not in sys.path:
    sys.path.insert(0, str(INTEGRATIONS_ROOT))

from zyra_integrations.source_provenance import (  # noqa: E402
    BundledSourceProvenance,
    source_workspace_root,
)


def test_bundled_source_provenance_is_complete_and_immutable() -> None:
    provenance = BundledSourceProvenance(ROOT)

    receipt = provenance.verify()

    assert receipt["ready"] is True
    assert receipt["repository_count"] == 7
    assert receipt["source_file_count"] >= 125
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
