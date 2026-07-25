from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from zyra_evaluation.experiment_runtime import (
    EvidenceArchiveLoader,
    ExperimentMatrixRuntime,
    ExperimentStore,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from run_m2_s05_03_experiments import _envelope, _external_evidence


SOFTWARE_ARCHIVE = (
    ROOT
    / "docs"
    / "reviews"
    / "evidence"
    / "M2-S05-02"
    / "software-causal-archive.zip"
)
RESEARCH_ARCHIVE = (
    ROOT
    / "docs"
    / "reviews"
    / "evidence"
    / "M2-S05-02"
    / "research-causal-archive.zip"
)
TEST_COMMIT = "b3387c626fc587936c24777ce108eac6f962283e"


def experiment_request(
    *,
    archive_path: Path = SOFTWARE_ARCHIVE,
    seeds: tuple[int, ...] = (7, 17, 29),
    title: str = "M2-S05-03 behavior test",
) -> dict[str, Any]:
    source = EvidenceArchiveLoader(allowed_roots=(ROOT,)).load(archive_path)
    return {
        "title": title,
        "source_archive_path": str(archive_path),
        "envelope": _envelope(
            source=source,
            commit=TEST_COMMIT,
            seeds=list(seeds),
            scenario_name=f"test-{source.domain}",
        ),
        "external_evidence": _external_evidence(TEST_COMMIT),
        "screenshot_index": {
            "screenshots": [],
            "note": "Behavior test; rendering is verified by the Web build.",
        },
        "requested_by": "pytest",
    }


def experiment_runtime(
    tmp_path: Path,
    *,
    suffix: str = "formal",
    enable_ablation_verifier: bool = True,
    enable_metric_aggregator: bool = True,
    enable_evidence_verifier: bool = True,
) -> ExperimentMatrixRuntime:
    return ExperimentMatrixRuntime(
        project_root=ROOT,
        store=ExperimentStore(tmp_path / f"{suffix}.sqlite3"),
        artifact_root=tmp_path / f"{suffix}-artifacts",
        allowed_source_roots=(ROOT, tmp_path),
        maximum_workers=2,
        enable_ablation_verifier=enable_ablation_verifier,
        enable_metric_aggregator=enable_metric_aggregator,
        enable_evidence_verifier=enable_evidence_verifier,
        auto_reconcile=True,
    )
