from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from .canonical import digest, now, require_mapping, require_sequence
from .contracts import REQUIRED_ABLATION_VARIANTS
from .errors import blocker, require_no_blockers
from .inputs import FreezeInputSet


class AblationMaterialBuilder:
    """Projects raw samples and paired statistics without inventing conclusions."""

    def __init__(self, inputs: FreezeInputSet) -> None:
        self.inputs = inputs

    def build(self) -> dict[str, Any]:
        statistics = self.inputs.document("benchmark-statistics")
        samples_document = self.inputs.document("benchmark-raw-samples")
        index = self.inputs.document("benchmark-index")
        runs = [
            require_mapping(item, "benchmark index run")
            for item in require_sequence(index.get("runs"), "benchmark index runs")
        ]
        samples_value = samples_document.get("samples")
        if samples_value is None:
            samples_value = samples_document.get("raw_samples")
        samples = require_sequence(samples_value, "raw samples")
        run_matrix = self._run_matrix(runs)
        distributions = self._distributions(statistics)
        comparisons = self._comparisons(statistics)
        material = {
            "schema": "zyra.first-stage-ablation-material/v1",
            "variants": sorted(run_matrix),
            "run_matrix": run_matrix,
            "raw_sample_count": len(samples),
            "raw_sample_path": self.inputs.relative_path("benchmark-raw-samples"),
            "raw_sample_sha256": self.inputs.digests["benchmark-raw-samples"],
            "statistical_path": self.inputs.relative_path("benchmark-statistics"),
            "statistical_sha256": self.inputs.digests["benchmark-statistics"],
            "distributions": distributions,
            "comparisons": comparisons,
            "bootstrap_iterations": statistics.get("bootstrap_iterations"),
            "generated_at": now(),
        }
        material["verification"] = self.verify(material)
        material["material_digest"] = digest(material)
        return material

    def verify(self, material: Mapping[str, Any]) -> dict[str, Any]:
        selected = require_mapping(material, "ablation material")
        findings: list[dict[str, Any]] = []
        variants = set(str(item) for item in selected.get("variants") or [])
        missing = sorted(set(REQUIRED_ABLATION_VARIANTS) - variants)
        if missing:
            findings.append(
                blocker(
                    "ablation-variants-missing",
                    "Ablation material omits required variants.",
                    missing=missing,
                )
            )
        matrix = require_mapping(selected.get("run_matrix"), "ablation run matrix")
        for variant in REQUIRED_ABLATION_VARIANTS:
            entry = require_mapping(matrix.get(variant), f"{variant} run matrix")
            if int(entry.get("run_count") or 0) < 6:
                findings.append(
                    blocker(
                        "ablation-run-count-insufficient",
                        "Ablation variant requires both domains and three repetitions.",
                        variant=variant,
                        run_count=entry.get("run_count"),
                    )
                )
            if sorted(entry.get("domains") or []) != [
                "cross-source-research",
                "software-delivery",
            ]:
                findings.append(
                    blocker(
                        "ablation-domain-coverage-incomplete",
                        "Ablation variant does not cover both formal domains.",
                        variant=variant,
                        domains=entry.get("domains"),
                    )
                )
        if int(selected.get("raw_sample_count") or 0) < 1:
            findings.append(
                blocker(
                    "ablation-raw-samples-missing",
                    "Ablation material has no raw samples.",
                )
            )
        if not selected.get("distributions"):
            findings.append(
                blocker(
                    "ablation-distributions-missing",
                    "Ablation material has no P50/P95 distributions.",
                )
            )
        if not selected.get("comparisons"):
            findings.append(
                blocker(
                    "ablation-comparisons-missing",
                    "Ablation material has no paired comparisons.",
                )
            )
        if int(selected.get("bootstrap_iterations") or 0) < 1_000:
            findings.append(
                blocker(
                    "ablation-bootstrap-insufficient",
                    "Ablation statistics use too few bootstrap iterations.",
                    iterations=selected.get("bootstrap_iterations"),
                )
            )
        require_no_blockers(
            findings,
            code="ablation-material-invalid",
            message="Baseline and ablation material is incomplete.",
            phase="material",
        )
        receipt = {
            "schema": "zyra.first-stage-ablation-verification/v1",
            "valid": True,
            "variant_count": len(variants),
            "raw_sample_count": selected.get("raw_sample_count"),
            "distribution_count": len(selected.get("distributions") or []),
            "comparison_count": len(selected.get("comparisons") or []),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    @staticmethod
    def _run_matrix(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        grouped = defaultdict(list)
        for run in runs:
            grouped[str(run.get("variant_id") or "")].append(run)
        return {
            variant: {
                "run_count": len(items),
                "domains": sorted({str(item.get("domain") or "") for item in items}),
                "repetitions": sorted(
                    {int(item.get("repetition") or 0) for item in items}
                ),
                "run_ids": sorted(str(item.get("run_id") or "") for item in items),
            }
            for variant, items in sorted(grouped.items())
        }

    @staticmethod
    def _distributions(statistics: Mapping[str, Any]) -> list[dict[str, Any]]:
        values = statistics.get("distributions") or []
        result = []
        for value in require_sequence(values, "statistical distributions"):
            item = require_mapping(value, "statistical distribution")
            if item.get("p50") is None or item.get("p95") is None:
                continue
            result.append(
                {
                    "domain": item.get("domain"),
                    "variant_id": item.get("variant_id"),
                    "metric_id": item.get("metric_id"),
                    "sample_count": item.get("sample_count"),
                    "p50": item.get("p50"),
                    "p95": item.get("p95"),
                    "unit": item.get("unit"),
                }
            )
        return result

    @staticmethod
    def _comparisons(statistics: Mapping[str, Any]) -> list[dict[str, Any]]:
        values = require_sequence(
            statistics.get("comparisons") or [],
            "statistical comparisons",
        )
        return [
            {
                key: item.get(key)
                for key in (
                    "domain",
                    "metric_id",
                    "baseline_variant",
                    "candidate_variant",
                    "pair_count",
                    "missing_pair_count",
                    "mean_delta",
                    "median_delta",
                    "p95_absolute_delta",
                    "relative_change",
                    "confidence_low",
                    "confidence_high",
                    "effect_direction",
                    "verdict",
                    "unit",
                )
            }
            for item in (
                require_mapping(value, "statistical comparison")
                for value in values
            )
        ]
