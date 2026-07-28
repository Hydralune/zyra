from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .canonical import (
    digest,
    file_digest,
    now,
    require_commit,
    require_mapping,
    require_sequence,
    safe_relative_path,
)
from .contracts import SCORE_ITEMS
from .errors import blocker, fail, require_no_blockers
from .inputs import FreezeInputSet
from .links import EvidenceLinkResolver
from .scoring import ScoreMatrixVerifier, score_summary


DEFAULT_ENTRY_CANDIDATES = (
    "scripts/run_m3_s02a02_live_benchmark.py",
    "scripts/run_release_pipeline.py",
    "scripts/verify_m3.py",
)

TEST_CANDIDATES = (
    "tests/unit/test_m3_live_benchmark.py",
    "tests/unit/test_release_productization.py",
    "tests/integration/test_m1_live_evidence_runtime.py",
)


class FreezeEvidenceIndexBuilder:
    """Builds a navigable final score matrix from admitted, immutable inputs."""

    def __init__(
        self,
        inputs: FreezeInputSet,
        *,
        target_commit: str,
    ) -> None:
        self.inputs = inputs
        self.repository_root = inputs.repository_root
        self.target_commit = require_commit(target_commit, "freeze target commit")
        self.resolver = EvidenceLinkResolver(self.repository_root)

    def build(self) -> dict[str, Any]:
        benchmark_index = self.inputs.document("benchmark-index")
        legacy_requirements = require_mapping(
            benchmark_index.get("requirements"),
            "benchmark requirements",
        )
        findings: list[dict[str, Any]] = []
        requirements: dict[str, dict[str, Any]] = {}
        for requirement_id, (dimension, points, title) in sorted(
            SCORE_ITEMS.items()
        ):
            if requirement_id not in legacy_requirements:
                findings.append(
                    blocker(
                        "benchmark-score-source-missing",
                        "Formal benchmark omits a final score requirement.",
                        requirement_id=requirement_id,
                    )
                )
                continue
            source_items = require_sequence(
                legacy_requirements[requirement_id],
                f"{requirement_id} source evidence",
            )
            if not source_items:
                findings.append(
                    blocker(
                        "benchmark-score-source-empty",
                        "Formal benchmark score evidence is empty.",
                        requirement_id=requirement_id,
                    )
                )
                continue
            references = self._references(requirement_id)
            resolved: list[dict[str, Any]] = []
            for reference in references:
                try:
                    receipt = self.resolver.resolve(reference)
                except Exception as error:
                    findings.append(
                        blocker(
                            "freeze-score-link-invalid",
                            "Score evidence reference did not resolve.",
                            requirement_id=requirement_id,
                            error=(
                                error.to_dict()
                                if hasattr(error, "to_dict")
                                else str(error)
                            ),
                        )
                    )
                    continue
                resolved.append({**reference, "resolved": True, "resolution": receipt})
            requirements[requirement_id] = {
                "requirement_id": requirement_id,
                "dimension": dimension,
                "points": points,
                "title": title,
                "status": "verified" if len(resolved) == len(references) else "blocked",
                "source_evidence": [dict(item) for item in source_items],
                "references": resolved,
            }
        require_no_blockers(
            findings,
            code="freeze-score-input-incomplete",
            message="Final score index inputs are incomplete.",
            phase="score",
        )
        report = self.inputs.document("benchmark-report")
        campaign = self.inputs.document("benchmark-campaign")
        input_set = {
            input_id: {
                "path": self.inputs.relative_path(input_id),
                "sha256": self.inputs.digests[input_id],
                "schema": str(document.get("schema") or ""),
            }
            for input_id, document in sorted(self.inputs.documents.items())
        }
        index = {
            "schema": "zyra.first-stage-100-point-evidence-index/v1",
            "target_commit": self.target_commit,
            "formal_benchmark_commit": report.get("commit_sha"),
            "campaign_id": campaign.get("campaign_id"),
            "sealed_policy_hash": campaign.get("conditions", {}).get(
                "sealed_policy_digest",
                benchmark_index.get("policy_hash"),
            ),
            "requirements": requirements,
            "score": score_summary(requirements),
            "inputs": input_set,
            "navigation": self.resolver.graph(requirements),
            "generated_at": now(),
        }
        index["index_digest"] = digest(index)
        ScoreMatrixVerifier().verify(index)
        return index

    def verify(self, index: Mapping[str, Any]) -> dict[str, Any]:
        score_receipt = ScoreMatrixVerifier().verify(index)
        selected = require_mapping(index, "freeze evidence index")
        requirements = require_mapping(
            selected.get("requirements"),
            "freeze score requirements",
        )
        findings: list[dict[str, Any]] = []
        all_references: list[dict[str, Any]] = []
        for requirement_id, entry in requirements.items():
            for reference in require_sequence(
                require_mapping(entry, requirement_id).get("references"),
                f"{requirement_id} references",
            ):
                selected_reference = require_mapping(reference, "evidence reference")
                all_references.append(selected_reference)
                try:
                    self.resolver.resolve(selected_reference)
                except Exception as error:
                    findings.append(
                        blocker(
                            "freeze-index-link-broken",
                            "Stored freeze evidence link no longer resolves.",
                            requirement_id=requirement_id,
                            reference_id=selected_reference.get("reference_id"),
                            error=(
                                error.to_dict()
                                if hasattr(error, "to_dict")
                                else str(error)
                            ),
                        )
                    )
        require_no_blockers(
            findings,
            code="freeze-index-links-invalid",
            message="Final evidence index contains broken links.",
            phase="score",
        )
        kinds = Counter(str(item.get("kind") or "") for item in all_references)
        receipt = {
            "schema": "zyra.first-stage-evidence-index-verification/v1",
            "valid": True,
            "target_commit": selected.get("target_commit"),
            "score": score_receipt["verified_score"],
            "requirement_count": len(requirements),
            "reference_count": len(all_references),
            "reference_kind_counts": dict(sorted(kinds.items())),
            "index_digest": score_receipt["index_digest"],
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    def _references(self, requirement_id: str) -> list[dict[str, Any]]:
        default_entry = self._first_existing(DEFAULT_ENTRY_CANDIDATES)
        test_entry = self._first_existing(TEST_CANDIDATES)
        common = (
            (
                "default-entry",
                default_entry,
                "Default product entry used to create or verify live evidence.",
            ),
            (
                "live-mutation",
                self.inputs.relative_path("benchmark-source-runs"),
                "Live source-run receipt binding canonical mutations.",
            ),
            (
                "artifact",
                self.inputs.relative_path("benchmark-index"),
                "Formal live artifact and run index.",
            ),
            (
                "metric",
                self.inputs.relative_path("benchmark-raw-samples"),
                "Raw metric samples used by statistical evaluation.",
            ),
            (
                "test",
                test_entry,
                "Behavior test covering the admitted live benchmark path.",
            ),
            (
                "config",
                self.inputs.relative_path("benchmark-campaign"),
                "Sealed campaign, environment, model, and verifier configuration.",
            ),
            (
                "commit",
                self.inputs.relative_path("benchmark-metadata"),
                "Implementation and evidence commit identity.",
            ),
            (
                "checksum",
                self.inputs.relative_path("benchmark-manifest"),
                "Hash-bound evidence manifest and Merkle root.",
            ),
        )
        references = []
        for ordinal, (kind, relative, label) in enumerate(common, 1):
            path = self.repository_root / relative
            references.append(
                {
                    "reference_id": (
                        f"{requirement_id.lower()}-{kind}-{ordinal:02d}"
                        .replace("_", "-")
                    ),
                    "kind": kind,
                    "path": safe_relative_path(relative),
                    "sha256": file_digest(path),
                    "label": label,
                    "commit": self.target_commit if kind == "commit" else "",
                }
            )
            if not references[-1]["commit"]:
                references[-1].pop("commit")
        return references

    def _first_existing(self, candidates: Sequence[str]) -> str:
        for relative in candidates:
            if (self.repository_root / relative).is_file():
                return relative
        raise fail(
            "freeze-evidence-entry-missing",
            "No default entry or behavior test exists for evidence navigation.",
            phase="score",
            detail={"candidates": list(candidates)},
        )


def build_evidence_index(
    inputs: FreezeInputSet,
    *,
    target_commit: str,
) -> dict[str, Any]:
    return FreezeEvidenceIndexBuilder(
        inputs,
        target_commit=target_commit,
    ).build()
