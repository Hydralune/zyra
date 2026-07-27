from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from .canonical import digest, identity, invalid, mapping, require_digest, utc_now
from .metrics import MetricCatalog
from .models import Campaign, CellResult, RawSample


COMPETITION_REQUIREMENTS = {
    "REQ-COMP-001": ("works-completeness", 6),
    "REQ-COMP-002": ("works-completeness", 8),
    "REQ-COMP-003": ("works-completeness", 6),
    "REQ-COMP-004": ("works-completeness", 6),
    "REQ-COMP-005": ("works-completeness", 6),
    "REQ-COMP-006": ("works-completeness", 5),
    "REQ-COMP-007": ("works-completeness", 3),
    "REQ-APP-001": ("application-innovation", 7),
    "REQ-APP-002": ("application-innovation", 5),
    "REQ-APP-003": ("application-innovation", 5),
    "REQ-APP-004": ("application-innovation", 4),
    "REQ-APP-005": ("application-innovation", 4),
    "REQ-TECH-001": ("technical-innovation", 8),
    "REQ-TECH-002": ("technical-innovation", 5),
    "REQ-TECH-003": ("technical-innovation", 4),
    "REQ-TECH-004": ("technical-innovation", 3),
    "REQ-PERF-001": ("performance-efficiency", 6),
    "REQ-PERF-002": ("performance-efficiency", 5),
    "REQ-PERF-003": ("performance-efficiency", 4),
}
EXPECTED_SCORE = 100


class BenchmarkReportBuilder:
    def __init__(self, metric_catalog: MetricCatalog | None = None) -> None:
        self.metric_catalog = metric_catalog or MetricCatalog()

    def build(
        self,
        *,
        campaign: Campaign,
        results: Sequence[CellResult],
        statistical_evaluation: Mapping[str, Any],
        requirement_evidence: Mapping[str, Sequence[Mapping[str, Any]]],
        predecessor_receipts: Mapping[str, str],
    ) -> dict[str, Any]:
        result_receipt = self._results(campaign, results)
        requirement_receipt = self._requirements(requirement_evidence)
        statistics_receipt = self._statistics(statistical_evaluation, campaign)
        predecessor_receipt = self._predecessors(predecessor_receipts)
        score = sum(
            value[1]
            for requirement_id, value in COMPETITION_REQUIREMENTS.items()
            if requirement_receipt["requirements"][requirement_id]["status"] == "verified"
        )
        report = {
            "schema": "zyra.live-benchmark-report/v1",
            "campaign_id": campaign.campaign_id,
            "campaign_digest": campaign.campaign_digest,
            "commit_sha": campaign.conditions.commit_sha,
            "sealed_policy_digest": campaign.conditions.sealed_policy_digest,
            "condition_digest": campaign.conditions.condition_digest,
            "result_receipt": result_receipt,
            "statistical_receipt": statistics_receipt,
            "requirement_receipt": requirement_receipt,
            "predecessor_receipt": predecessor_receipt,
            "score": {
                "verified": score,
                "maximum": EXPECTED_SCORE,
                "complete": score == EXPECTED_SCORE,
            },
            "environment": {
                "environment_digest": campaign.conditions.environment_digest,
                "hardware_digest": campaign.conditions.hardware_digest,
                "deployment_digest": campaign.conditions.deployment_digest,
                "provider_policy_digest": campaign.conditions.provider_policy_digest,
                "verifier_digest": campaign.conditions.verifier_digest,
                "failure_schedule_digest": campaign.conditions.failure_schedule_digest,
                "budget_digest": campaign.conditions.budget_digest,
            },
            "generated_at": utc_now(),
        }
        if score != EXPECTED_SCORE:
            missing = [
                requirement_id
                for requirement_id, entry in requirement_receipt["requirements"].items()
                if entry["status"] != "verified"
            ]
            raise invalid(
                "benchmark_report_score_incomplete",
                "Formal benchmark does not close the 100-point evidence matrix.",
                phase="report",
                detail={"score": score, "missing": missing},
            )
        report["report_digest"] = digest(report)
        return report

    def evidence_index(
        self,
        *,
        campaign: Campaign,
        results: Sequence[CellResult],
        report: Mapping[str, Any],
        requirement_evidence: Mapping[str, Sequence[Mapping[str, Any]]],
        artifact_members: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        runs = []
        samples = []
        for result in results:
            run = {
                "run_id": result.run_id,
                "cell_id": result.cell.cell_id,
                "domain": result.cell.domain.value,
                "variant_id": result.cell.variant_id,
                "repetition": result.cell.repetition,
                "input_revision": result.cell.input_revision,
                "result_digest": result.result_digest,
                "live_receipt_digest": result.admission_receipt[
                    "live_receipt_digest"
                ],
                "semantic_step_receipt_digest": result.admission_receipt[
                    "semantic_step_receipt"
                ]["receipt_digest"],
                "verifier_receipt_digest": result.verifier_receipt["receipt_digest"],
                "deployment_receipt_digest": result.deployment_receipt[
                    "receipt_digest"
                ],
                "fault_receipt_digest": result.fault_receipt["receipt_digest"],
            }
            runs.append(run)
            samples.extend(
                {
                    "sample_id": item.sample_id,
                    "cell_id": item.cell_id,
                    "run_id": item.run_id,
                    "metric_id": item.metric_id,
                    "sample_digest": item.sample_digest,
                }
                for item in result.samples
            )
        index = {
            "schema": "zyra.100-point-evidence-index/v1",
            "campaign_id": campaign.campaign_id,
            "commit_sha": campaign.conditions.commit_sha,
            "policy_hash": campaign.conditions.sealed_policy_digest,
            "report_digest": require_digest(
                report.get("report_digest"),
                "report digest",
            ),
            "requirements": {
                requirement_id: [
                    {
                        "evidence_id": identity(item.get("evidence_id"), "evidence id"),
                        "kind": str(item.get("kind") or ""),
                        "digest": require_digest(
                            item.get("digest"),
                            f"{requirement_id} evidence digest",
                        ),
                        "run_ids": sorted(
                            identity(value, "evidence run id")
                            for value in item.get("run_ids") or []
                        ),
                        "artifact_ids": sorted(
                            identity(value, "evidence artifact id")
                            for value in item.get("artifact_ids") or []
                        ),
                    }
                    for item in requirement_evidence[requirement_id]
                ]
                for requirement_id in sorted(COMPETITION_REQUIREMENTS)
            },
            "runs": sorted(runs, key=lambda item: item["cell_id"]),
            "samples": sorted(
                samples,
                key=lambda item: (item["cell_id"], item["metric_id"]),
            ),
            "artifacts": sorted(
                [dict(item) for item in artifact_members],
                key=lambda item: str(item.get("path") or ""),
            ),
            "handoff": {
                "owner": "M3-03",
                "ready": True,
                "required_inputs": [
                    "report",
                    "raw-samples",
                    "statistical-evaluation",
                    "100-point-evidence-index",
                    "evidence-manifest",
                ],
            },
            "generated_at": utc_now(),
        }
        index["index_digest"] = digest(index)
        return index

    def verify(
        self,
        report: Mapping[str, Any],
        *,
        campaign: Campaign,
    ) -> dict[str, Any]:
        projection = dict(report)
        declared = projection.pop("report_digest", "")
        observed = digest(projection)
        findings: list[dict[str, Any]] = []
        if declared != observed:
            findings.append({"code": "report-digest-mismatch"})
        if report.get("campaign_id") != campaign.campaign_id:
            findings.append({"code": "report-campaign-mismatch"})
        if report.get("commit_sha") != campaign.conditions.commit_sha:
            findings.append({"code": "report-commit-mismatch"})
        if report.get("sealed_policy_digest") != campaign.conditions.sealed_policy_digest:
            findings.append({"code": "report-policy-mismatch"})
        score = mapping(report.get("score"), "report score")
        if score.get("verified") != EXPECTED_SCORE or score.get("complete") is not True:
            findings.append({"code": "report-score-incomplete"})
        result = mapping(report.get("result_receipt"), "result receipt")
        if result.get("cell_count") != len(campaign.cells):
            findings.append({"code": "report-cell-count-mismatch"})
        statistics = mapping(
            report.get("statistical_receipt"),
            "statistical receipt",
        )
        if statistics.get("valid") is not True:
            findings.append({"code": "report-statistics-invalid"})
        requirements = mapping(
            report.get("requirement_receipt"),
            "requirement receipt",
        )
        if requirements.get("verified_score") != EXPECTED_SCORE:
            findings.append({"code": "report-requirements-incomplete"})
        if findings:
            raise invalid(
                "benchmark_report_invalid",
                "Benchmark report failed self-verification.",
                phase="report",
                detail={"findings": findings},
            )
        receipt = {
            "schema": "zyra.live-benchmark-report-verification/v1",
            "valid": True,
            "campaign_id": campaign.campaign_id,
            "report_digest": observed,
            "score": EXPECTED_SCORE,
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    def _results(
        self,
        campaign: Campaign,
        results: Sequence[CellResult],
    ) -> dict[str, Any]:
        by_cell = {item.cell.cell_id: item for item in results}
        missing = sorted(set(item.cell_id for item in campaign.cells) - set(by_cell))
        unexpected = sorted(set(by_cell) - set(item.cell_id for item in campaign.cells))
        if missing or unexpected or len(by_cell) != len(results):
            raise invalid(
                "benchmark_report_results_incomplete",
                "Report result set is incomplete or duplicated.",
                phase="report",
                detail={"missing": missing, "unexpected": unexpected},
            )
        human = sum(
            int(item.live_receipt.get("human_intervention_count") or 0)
            for item in results
        )
        operator = sum(
            int(item.live_receipt.get("operator_intervention_count") or 0)
            for item in results
        )
        if human or operator:
            raise invalid(
                "benchmark_report_intervention_nonzero",
                "Formal report contains manual intervention.",
                phase="report",
                detail={"human": human, "operator": operator},
            )
        long_runs = [
            item.run_id
            for item in results
            if item.admission_receipt["semantic_step_receipt"][
                "effective_step_count"
            ]
            >= campaign.required_long_run_steps
        ]
        if not long_runs:
            raise invalid(
                "benchmark_report_long_run_missing",
                "No formal run meets the 2,000-transition threshold.",
                phase="report",
            )
        domain_counts = Counter(item.cell.domain.value for item in results)
        variant_counts = Counter(item.cell.variant_id for item in results)
        return {
            "schema": "zyra.live-benchmark-result-summary/v1",
            "valid": True,
            "cell_count": len(results),
            "run_count": len({item.run_id for item in results}),
            "domain_counts": dict(sorted(domain_counts.items())),
            "variant_counts": dict(sorted(variant_counts.items())),
            "human_intervention_count": human,
            "operator_intervention_count": operator,
            "long_run_ids": sorted(long_runs),
            "result_digest": digest([item.to_dict() for item in results]),
        }

    def _requirements(
        self,
        evidence: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> dict[str, Any]:
        missing = sorted(set(COMPETITION_REQUIREMENTS) - set(evidence))
        unexpected = sorted(set(evidence) - set(COMPETITION_REQUIREMENTS))
        if missing or unexpected:
            raise invalid(
                "benchmark_requirement_evidence_matrix_invalid",
                "Requirement evidence matrix is incomplete.",
                phase="report",
                detail={"missing": missing, "unexpected": unexpected},
            )
        entries: dict[str, Any] = {}
        score = 0
        for requirement_id, (dimension, points) in sorted(
            COMPETITION_REQUIREMENTS.items()
        ):
            items = tuple(evidence[requirement_id])
            findings: list[str] = []
            if not items:
                findings.append("evidence-empty")
            kinds: set[str] = set()
            for item in items:
                identity(item.get("evidence_id"), f"{requirement_id} evidence id")
                require_digest(item.get("digest"), f"{requirement_id} evidence digest")
                kind = str(item.get("kind") or "").strip()
                if not kind:
                    findings.append("evidence-kind-missing")
                kinds.add(kind)
                if item.get("verified") is not True:
                    findings.append("evidence-not-verified")
            status = "verified" if not findings else "failed"
            if status == "verified":
                score += points
            entries[requirement_id] = {
                "dimension": dimension,
                "points": points,
                "status": status,
                "evidence_count": len(items),
                "evidence_kinds": sorted(kinds),
                "findings": sorted(set(findings)),
                "evidence_digest": digest(items),
            }
        if score != EXPECTED_SCORE:
            raise invalid(
                "benchmark_requirement_evidence_incomplete",
                "Requirement evidence does not close the formal score.",
                phase="report",
                detail={"score": score},
            )
        return {
            "schema": "zyra.live-benchmark-requirement-evidence/v1",
            "valid": True,
            "verified_score": score,
            "maximum_score": EXPECTED_SCORE,
            "requirements": entries,
            "evidence_digest": digest(evidence),
        }

    def _statistics(
        self,
        value: Mapping[str, Any],
        campaign: Campaign,
    ) -> dict[str, Any]:
        if value.get("valid") is not True:
            raise invalid(
                "benchmark_statistical_evaluation_invalid",
                "Statistical evaluation is not valid.",
                phase="report",
            )
        distributions = value.get("distributions") or []
        comparisons = value.get("comparisons") or []
        required_distribution_count = (
            len(self.metric_catalog.required_ids())
            * len(campaign.domains)
            * len(campaign.variants)
        )
        if len(distributions) != required_distribution_count:
            raise invalid(
                "benchmark_distribution_matrix_incomplete",
                "Statistical distribution matrix is incomplete.",
                phase="report",
                detail={
                    "expected": required_distribution_count,
                    "observed": len(distributions),
                },
            )
        if not comparisons:
            raise invalid(
                "benchmark_comparisons_empty",
                "Statistical evaluation contains no paired comparisons.",
                phase="report",
            )
        return {
            "schema": "zyra.live-benchmark-statistics-summary/v1",
            "valid": True,
            "sample_count": value.get("sample_count"),
            "distribution_count": len(distributions),
            "comparison_count": len(comparisons),
            "confidence_level": value.get("confidence_level"),
            "bootstrap_iterations": value.get("bootstrap_iterations"),
            "statistical_evaluation_digest": require_digest(
                value.get("receipt_digest"),
                "statistical evaluation digest",
            ),
        }

    def _predecessors(self, values: Mapping[str, str]) -> dict[str, Any]:
        required = {
            "m1_exit",
            "m2_live_scenarios",
            "m2_experiment_matrix",
            "m3_regression_hardening",
        }
        missing = sorted(required - set(values))
        if missing:
            raise invalid(
                "benchmark_predecessor_receipts_missing",
                "Benchmark report is missing protected predecessor receipts.",
                phase="report",
                detail={"missing": missing},
            )
        selected = {
            key: require_digest(value, f"{key} predecessor digest")
            for key, value in values.items()
        }
        return {
            "schema": "zyra.live-benchmark-predecessors/v1",
            "valid": True,
            "receipts": dict(sorted(selected.items())),
            "predecessor_digest": digest(selected),
        }
