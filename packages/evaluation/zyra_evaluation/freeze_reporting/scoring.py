from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from .canonical import digest, require_mapping, require_sequence, require_text
from .contracts import (
    REQUIRED_REFERENCE_KINDS,
    SCORE_DIMENSIONS,
    SCORE_ITEMS,
)
from .errors import blocker, require_no_blockers


class ScoreMatrixVerifier:
    """Fail-closed verifier for the final 40/25/20/15 evidence matrix."""

    def verify(self, index: Mapping[str, Any]) -> dict[str, Any]:
        selected = require_mapping(index, "100-point evidence index")
        entries = require_mapping(selected.get("requirements"), "score requirements")
        findings: list[dict[str, Any]] = []
        expected_ids = set(SCORE_ITEMS)
        observed_ids = set(entries)
        missing = sorted(expected_ids - observed_ids)
        unexpected = sorted(observed_ids - expected_ids)
        if missing:
            findings.append(
                blocker(
                    "score-items-missing",
                    "Evidence index omits required score items.",
                    requirement_ids=missing,
                )
            )
        if unexpected:
            findings.append(
                blocker(
                    "score-items-unexpected",
                    "Evidence index contains unknown score items.",
                    requirement_ids=unexpected,
                )
            )
        dimension_verified = defaultdict(int)
        kind_totals: Counter[str] = Counter()
        verified_score = 0
        requirement_receipts: dict[str, Any] = {}
        for requirement_id in sorted(expected_ids & observed_ids):
            entry = require_mapping(entries[requirement_id], requirement_id)
            receipt, entry_findings = self._verify_entry(requirement_id, entry)
            findings.extend(entry_findings)
            requirement_receipts[requirement_id] = receipt
            if not entry_findings:
                dimension, points, _title = SCORE_ITEMS[requirement_id]
                dimension_verified[dimension] += points
                verified_score += points
                kind_totals.update(receipt["reference_kind_counts"])
        for dimension, maximum in SCORE_DIMENSIONS.items():
            observed = dimension_verified[dimension]
            if observed != maximum:
                findings.append(
                    blocker(
                        "score-dimension-incomplete",
                        "Score dimension does not close its required points.",
                        dimension=dimension,
                        expected=maximum,
                        observed=observed,
                    )
                )
        declared = require_mapping(selected.get("score"), "score summary")
        if declared.get("maximum") != 100:
            findings.append(
                blocker(
                    "score-maximum-invalid",
                    "Evidence index maximum must be 100.",
                    observed=declared.get("maximum"),
                )
            )
        if declared.get("verified") != verified_score:
            findings.append(
                blocker(
                    "score-verified-mismatch",
                    "Evidence index verified score is inconsistent.",
                    declared=declared.get("verified"),
                    observed=verified_score,
                )
            )
        if declared.get("complete") is not (verified_score == 100):
            findings.append(
                blocker(
                    "score-complete-flag-invalid",
                    "Evidence index completion flag is inconsistent.",
                    verified=verified_score,
                )
            )
        projection = dict(selected)
        declared_digest = projection.pop("index_digest", "")
        observed_digest = digest(projection)
        if declared_digest != observed_digest:
            findings.append(
                blocker(
                    "score-index-digest-mismatch",
                    "Evidence index embedded digest is invalid.",
                    declared=declared_digest,
                    observed=observed_digest,
                )
            )
        require_no_blockers(
            findings,
            code="score-evidence-incomplete",
            message="100-point evidence index contains blocking findings.",
            phase="score",
            detail={"verified_score": verified_score},
        )
        receipt = {
            "schema": "zyra.freeze-score-verification/v1",
            "valid": True,
            "verified_score": verified_score,
            "maximum_score": 100,
            "dimension_scores": dict(sorted(dimension_verified.items())),
            "reference_kind_counts": dict(sorted(kind_totals.items())),
            "requirements": requirement_receipts,
            "index_digest": observed_digest,
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    def diagnose(self, index: Mapping[str, Any]) -> dict[str, Any]:
        try:
            receipt = self.verify(index)
        except Exception as error:
            if hasattr(error, "to_dict"):
                return {
                    "schema": "zyra.freeze-score-diagnostic/v1",
                    "valid": False,
                    "error": error.to_dict(),
                }
            raise
        return {
            "schema": "zyra.freeze-score-diagnostic/v1",
            "valid": True,
            "receipt": receipt,
        }

    def _verify_entry(
        self,
        requirement_id: str,
        entry: Mapping[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        findings: list[dict[str, Any]] = []
        dimension, points, title = SCORE_ITEMS[requirement_id]
        if entry.get("requirement_id") != requirement_id:
            findings.append(
                blocker(
                    "score-requirement-id-mismatch",
                    "Score entry requirement identifier is inconsistent.",
                    expected=requirement_id,
                    observed=entry.get("requirement_id"),
                )
            )
        if entry.get("dimension") != dimension:
            findings.append(
                blocker(
                    "score-entry-dimension-mismatch",
                    "Score entry dimension is inconsistent.",
                    requirement_id=requirement_id,
                    expected=dimension,
                    observed=entry.get("dimension"),
                )
            )
        if entry.get("points") != points:
            findings.append(
                blocker(
                    "score-entry-points-mismatch",
                    "Score entry point value is inconsistent.",
                    requirement_id=requirement_id,
                    expected=points,
                    observed=entry.get("points"),
                )
            )
        if entry.get("title") != title:
            findings.append(
                blocker(
                    "score-entry-title-mismatch",
                    "Score entry title is inconsistent.",
                    requirement_id=requirement_id,
                )
            )
        if entry.get("status") != "verified":
            findings.append(
                blocker(
                    "score-entry-not-verified",
                    "Score entry is not verified.",
                    requirement_id=requirement_id,
                    status=entry.get("status"),
                )
            )
        references = [
            require_mapping(item, f"{requirement_id} reference")
            for item in require_sequence(
                entry.get("references"),
                f"{requirement_id} references",
            )
        ]
        reference_ids: set[str] = set()
        kind_counts: Counter[str] = Counter()
        for reference in references:
            reference_id = str(reference.get("reference_id") or "")
            kind = str(reference.get("kind") or "")
            if reference_id in reference_ids:
                findings.append(
                    blocker(
                        "score-reference-duplicate",
                        "Score entry duplicates an evidence reference.",
                        requirement_id=requirement_id,
                        reference_id=reference_id,
                    )
                )
            reference_ids.add(reference_id)
            kind_counts[kind] += 1
            if reference.get("resolved") is not True:
                findings.append(
                    blocker(
                        "score-reference-unresolved",
                        "Score entry contains an unresolved evidence reference.",
                        requirement_id=requirement_id,
                        reference_id=reference_id,
                        kind=kind,
                    )
                )
            if not reference.get("sha256"):
                findings.append(
                    blocker(
                        "score-reference-checksum-missing",
                        "Score evidence reference lacks a checksum.",
                        requirement_id=requirement_id,
                        reference_id=reference_id,
                    )
                )
        for kind in REQUIRED_REFERENCE_KINDS:
            if kind_counts[kind] < 1:
                findings.append(
                    blocker(
                        "score-reference-kind-missing",
                        "Score entry lacks a required evidence class.",
                        requirement_id=requirement_id,
                        kind=kind,
                    )
                )
        receipt = {
            "requirement_id": requirement_id,
            "dimension": dimension,
            "points": points,
            "reference_count": len(references),
            "reference_kind_counts": dict(sorted(kind_counts.items())),
            "valid": not findings,
        }
        return receipt, findings


def score_summary(requirements: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    verified = 0
    dimensions = defaultdict(int)
    for requirement_id, entry in requirements.items():
        if entry.get("status") != "verified" or requirement_id not in SCORE_ITEMS:
            continue
        dimension, points, _title = SCORE_ITEMS[requirement_id]
        verified += points
        dimensions[dimension] += points
    return {
        "verified": verified,
        "maximum": 100,
        "complete": verified == 100,
        "dimensions": {
            dimension: {
                "verified": dimensions[dimension],
                "maximum": maximum,
                "complete": dimensions[dimension] == maximum,
            }
            for dimension, maximum in SCORE_DIMENSIONS.items()
        },
    }
