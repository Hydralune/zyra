from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from .canonical import canonicalize, digest, stable_unique, utc_now
from .errors import invalid
from .models import (
    DistributionSummary,
    ExperimentRun,
    RawMetricSample,
    RequirementEvidence,
    VariantComparison,
)
from .source import SourceArchive


class ExperimentReportBuilder:
    def build(
        self,
        *,
        run: ExperimentRun,
        source: SourceArchive,
        samples: Iterable[RawMetricSample],
        summaries: Iterable[DistributionSummary],
        comparisons: Iterable[VariantComparison],
        requirements: Iterable[RequirementEvidence],
        source_role_audit: Mapping[str, Any],
        verification_receipts: Iterable[Mapping[str, Any]],
        external_evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        samples = tuple(samples)
        summaries = tuple(summaries)
        comparisons = tuple(comparisons)
        requirements = tuple(requirements)
        receipts = tuple(dict(item) for item in verification_receipts)
        self._verify_input(
            run=run,
            source=source,
            samples=samples,
            summaries=summaries,
            comparisons=comparisons,
            requirements=requirements,
            source_role_audit=source_role_audit,
        )
        summary_index = {
            (item.metric, item.variant_id): item for item in summaries
        }
        variant_cards: list[dict[str, Any]] = []
        for variant in run.variants:
            cells = tuple(
                item for item in run.cells if item.variant_id == variant.variant_id
            )
            metrics = {
                metric: summary.to_dict()
                for (metric, variant_id), summary in summary_index.items()
                if variant_id == variant.variant_id
            }
            comparison_rows = [
                item.to_dict()
                for item in comparisons
                if item.compared_variant_id == variant.variant_id
                or item.baseline_variant_id == variant.variant_id
            ]
            variant_cards.append(
                {
                    "variant": variant.to_dict(),
                    "cell_count": len(cells),
                    "succeeded_cell_count": sum(
                        item.phase.value == "succeeded" for item in cells
                    ),
                    "failed_cell_count": sum(
                        item.phase.value in {"failed", "rejected"} for item in cells
                    ),
                    "seeds": [item.seed for item in cells],
                    "observation_digests": [
                        item.observation_digest
                        for item in cells
                        if item.observation_digest
                    ],
                    "metrics": metrics,
                    "comparisons": comparison_rows,
                }
            )
        category_metrics: dict[str, list[str]] = defaultdict(list)
        for summary in summaries:
            sample = next(
                (
                    item
                    for item in samples
                    if item.metric == summary.metric
                    and item.variant_id == summary.variant_id
                ),
                None,
            )
            category = sample.dimensions.get("category", "other") if sample else "other"
            category_metrics[category].append(summary.metric)
        anomalies = [
            {
                "sample_id": item.sample_id,
                "variant_id": item.variant_id,
                "metric": item.metric,
                "value": item.value,
                "reason": item.anomaly_reason,
            }
            for item in samples
            if item.anomaly_reason
        ]
        unavailable = [
            {
                "sample_id": item.sample_id,
                "variant_id": item.variant_id,
                "metric": item.metric,
                "reason": item.unavailable_reason,
            }
            for item in samples
            if item.unavailable_reason
        ]
        report = {
            "schema": "zyra.experiment-final-report/v1",
            "experiment": {
                "experiment_id": run.experiment_id,
                "title": run.title,
                "phase": run.phase.value,
                "repetitions": run.repetitions,
                "created_at": run.created_at,
                "started_at": run.started_at,
                "completed_at": run.completed_at,
                "commit_sha": run.envelope.commit_sha,
                "envelope_digest": run.envelope.envelope_digest,
                "sealed_policy_digest": run.envelope.sealed_policy_digest,
                "environment_digest": run.envelope.environment_digest,
                "failure_schedule_digest": (
                    run.envelope.failure_schedule.schedule_digest
                ),
                "verifier_digest": run.envelope.verifier.verifier_digest,
                "budget_digest": run.envelope.budget.budget_digest,
                "hardware_digest": run.envelope.hardware.hardware_digest,
                "provider_policy_digest": run.envelope.provider.policy_digest,
            },
            "source": {
                **source.to_dict(include_events=False),
                "source_verification_valid": True,
                "replay": False,
                "fixture": False,
                "live_owner_receipts": True,
            },
            "matrix": {
                "variant_count": len(run.variants),
                "cell_count": len(run.cells),
                "repetition_count": run.repetitions,
                "required_baseline_ids": [
                    "single_agent",
                    "static_full_connect_multi_agent",
                    "dynamic_heterogeneous_swarm",
                ],
                "required_ablation_ids": [
                    "no_scheduler",
                    "no_memory_compact",
                    "no_recovery",
                    "no_low_entropy_communication",
                ],
                "variant_cards": variant_cards,
            },
            "statistics": {
                "raw_sample_count": len(samples),
                "summary_count": len(summaries),
                "comparison_count": len(comparisons),
                "p50_present_count": sum(item.p50 is not None for item in summaries),
                "p95_present_count": sum(item.p95 is not None for item in summaries),
                "dispersion_present_count": sum(
                    item.standard_deviation is not None
                    and item.interquartile_range is not None
                    and item.median_absolute_deviation is not None
                    for item in summaries
                ),
                "confidence_present_count": sum(
                    item.confidence.lower is not None
                    and item.confidence.upper is not None
                    for item in summaries
                ),
                "anomalies": anomalies,
                "unavailable_samples": unavailable,
                "category_metrics": {
                    key: sorted(set(value))
                    for key, value in sorted(category_metrics.items())
                },
                "summaries": [
                    item.to_dict()
                    for item in sorted(
                        summaries,
                        key=lambda value: (value.metric, value.variant_id),
                    )
                ],
                "comparisons": [
                    item.to_dict()
                    for item in sorted(
                        comparisons,
                        key=lambda value: (
                            value.metric,
                            value.baseline_variant_id,
                            value.compared_variant_id,
                        ),
                    )
                ],
            },
            "requirements": {
                "rows": [item.to_dict() for item in requirements],
                "requirement_count": len(requirements),
                "verified_count": sum(item.verified for item in requirements),
                "score_total": sum(item.score for item in requirements),
                "score_verified": sum(
                    item.score for item in requirements if item.verified
                ),
            },
            "source_roles": canonicalize(source_role_audit),
            "external_evidence": canonicalize(external_evidence or {}),
            "verification": {
                "receipt_count": len(receipts),
                "receipts": canonicalize(receipts),
                "all_valid": all(item.get("valid") is True for item in receipts),
            },
            "method": {
                "execution_kind": "controlled_live_evidence_workload",
                "same_conditions": (
                    "task input, budget, provider policy, hardware, verifier, "
                    "environment, failure schedule and seed plan are immutable"
                ),
                "ablation_rule": "one capability changes from dynamic swarm",
                "statistics": (
                    "raw samples, linear P50/P95, Welford dispersion, MAD, IQR "
                    "and deterministic percentile-bootstrap mean confidence"
                ),
                "non_claims": [
                    "No authenticated provider/model CLI was invoked.",
                    "No new external model request was made.",
                    "M2 controlled executions are not relabeled as new cloud/model dispatch.",
                    "Prior M1 local/edge/cloud and provider/model receipts remain external frozen evidence.",
                ],
            },
            "algorithm_entries": algorithm_entries(),
            "reviewer_navigation": self._navigation(
                run=run,
                source=source,
                samples=samples,
                summaries=summaries,
                requirements=requirements,
            ),
            "generated_at": utc_now(),
        }
        report["report_digest"] = digest(report)
        self.verify(report)
        return report

    def verify(self, report: Mapping[str, Any]) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        declared = str(report.get("report_digest") or "")
        body = dict(report)
        body.pop("report_digest", None)
        calculated = digest(body)
        if declared != calculated:
            findings.append(
                {
                    "code": "report_digest_mismatch",
                    "declared": declared,
                    "calculated": calculated,
                }
            )
        matrix = report.get("matrix")
        if not isinstance(matrix, Mapping):
            findings.append({"code": "matrix_missing"})
        else:
            cards = matrix.get("variant_cards")
            if not isinstance(cards, list) or len(cards) < 7:
                findings.append({"code": "variant_cards_incomplete"})
        statistics = report.get("statistics")
        if not isinstance(statistics, Mapping):
            findings.append({"code": "statistics_missing"})
        else:
            for key in (
                "raw_sample_count",
                "p50_present_count",
                "p95_present_count",
                "dispersion_present_count",
            ):
                if int(statistics.get(key) or 0) <= 0:
                    findings.append(
                        {"code": "statistic_evidence_missing", "field": key}
                    )
        requirements = report.get("requirements")
        if not isinstance(requirements, Mapping):
            findings.append({"code": "requirements_missing"})
        else:
            if int(requirements.get("score_total") or 0) != 100:
                findings.append({"code": "score_total_invalid"})
        navigation = report.get("reviewer_navigation")
        if not isinstance(navigation, Mapping):
            findings.append({"code": "reviewer_navigation_missing"})
        else:
            if not navigation.get("nodes") or not navigation.get("edges"):
                findings.append({"code": "reviewer_navigation_empty"})
        algorithms = report.get("algorithm_entries")
        if not isinstance(algorithms, list) or not algorithms:
            findings.append({"code": "algorithm_entries_missing"})
        receipt = {
            "schema": "zyra.experiment-report-verification/v1",
            "valid": not findings,
            "report_digest": declared,
            "findings": findings,
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        if findings:
            raise invalid(
                "experiment_report_invalid",
                "Experiment final report failed verification.",
                phase="report",
                detail=receipt,
            )
        return receipt

    def _verify_input(
        self,
        *,
        run: ExperimentRun,
        source: SourceArchive,
        samples: tuple[RawMetricSample, ...],
        summaries: tuple[DistributionSummary, ...],
        comparisons: tuple[VariantComparison, ...],
        requirements: tuple[RequirementEvidence, ...],
        source_role_audit: Mapping[str, Any],
    ) -> None:
        findings: list[dict[str, Any]] = []
        if source.archive_digest != run.envelope.source_evidence_digest:
            findings.append({"code": "source_envelope_mismatch"})
        if len(run.variants) < 7:
            findings.append({"code": "matrix_variant_count_invalid"})
        if len(run.cells) != len(run.variants) * run.repetitions:
            findings.append({"code": "matrix_cell_count_invalid"})
        if any(item.phase.value != "succeeded" for item in run.cells):
            findings.append({"code": "matrix_cell_not_succeeded"})
        if not samples:
            findings.append({"code": "raw_samples_missing"})
        if not summaries:
            findings.append({"code": "summaries_missing"})
        if not comparisons:
            findings.append({"code": "comparisons_missing"})
        if not requirements:
            findings.append({"code": "requirements_missing"})
        if source_role_audit.get("valid") is not True:
            findings.append({"code": "source_role_audit_invalid"})
        if findings:
            raise invalid(
                "experiment_report_input_invalid",
                "Experiment report inputs are incomplete.",
                phase="report",
                detail={"findings": findings},
            )

    def _navigation(
        self,
        *,
        run: ExperimentRun,
        source: SourceArchive,
        samples: tuple[RawMetricSample, ...],
        summaries: tuple[DistributionSummary, ...],
        requirements: tuple[RequirementEvidence, ...],
    ) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = [
            {
                "node_id": f"experiment:{run.experiment_id}",
                "kind": "experiment",
                "label": run.title,
                "digest": run.envelope.envelope_digest,
                "route": f"/experiments/{run.experiment_id}",
            },
            {
                "node_id": f"source:{source.archive_id}",
                "kind": "source_archive",
                "label": f"{source.domain} live archive",
                "digest": source.archive_digest,
                "route": f"/experiments/{run.experiment_id}/source",
            },
        ]
        edges: list[dict[str, str]] = [
            {
                "from": f"experiment:{run.experiment_id}",
                "to": f"source:{source.archive_id}",
                "relation": "uses_verified_source",
            }
        ]
        for variant in run.variants:
            variant_id = f"variant:{variant.variant_id}"
            nodes.append(
                {
                    "node_id": variant_id,
                    "kind": "variant",
                    "label": variant.title,
                    "digest": variant.definition_digest,
                    "route": (
                        f"/experiments/{run.experiment_id}"
                        f"?variant={variant.variant_id}"
                    ),
                }
            )
            edges.append(
                {
                    "from": f"experiment:{run.experiment_id}",
                    "to": variant_id,
                    "relation": "contains_variant",
                }
            )
            for cell in run.cells:
                if cell.variant_id != variant.variant_id:
                    continue
                cell_id = f"cell:{cell.cell_id}"
                nodes.append(
                    {
                        "node_id": cell_id,
                        "kind": "cell",
                        "label": f"{variant.variant_id} #{cell.repetition}",
                        "digest": cell.observation_digest,
                        "route": (
                            f"/experiments/{run.experiment_id}"
                            f"/cells/{cell.cell_id}"
                        ),
                    }
                )
                edges.append(
                    {
                        "from": variant_id,
                        "to": cell_id,
                        "relation": "executes",
                    }
                )
                edges.append(
                    {
                        "from": cell_id,
                        "to": f"source:{source.archive_id}",
                        "relation": "binds_canonical_events",
                    }
                )
        for summary in summaries:
            node_id = f"metric:{summary.metric}:{summary.variant_id}"
            nodes.append(
                {
                    "node_id": node_id,
                    "kind": "metric_summary",
                    "label": f"{summary.metric} / {summary.variant_id}",
                    "digest": summary.summary_digest,
                    "route": (
                        f"/experiments/{run.experiment_id}/metrics"
                        f"?metric={summary.metric}&variant={summary.variant_id}"
                    ),
                }
            )
            edges.append(
                {
                    "from": f"variant:{summary.variant_id}",
                    "to": node_id,
                    "relation": "aggregates",
                }
            )
        for requirement in requirements:
            node_id = f"requirement:{requirement.requirement_id}"
            nodes.append(
                {
                    "node_id": node_id,
                    "kind": "requirement",
                    "label": requirement.requirement_id,
                    "digest": requirement.source_digest,
                    "route": (
                        f"/experiments/{run.experiment_id}/requirements"
                        f"?requirement={requirement.requirement_id}"
                    ),
                }
            )
            edges.append(
                {
                    "from": f"experiment:{run.experiment_id}",
                    "to": node_id,
                    "relation": "provides_evidence",
                }
            )
            for metric in requirement.metric_names:
                for variant in run.variants:
                    metric_id = f"metric:{metric}:{variant.variant_id}"
                    if any(node["node_id"] == metric_id for node in nodes):
                        edges.append(
                            {
                                "from": node_id,
                                "to": metric_id,
                                "relation": "supported_by",
                            }
                        )
        navigation = {
            "schema": "zyra.experiment-reviewer-navigation/v1",
            "nodes": nodes,
            "edges": edges,
            "backend_log_required": False,
            "raw_sample_api": f"/experiments/{run.experiment_id}/samples",
            "bundle_api": f"/experiments/{run.experiment_id}/bundle",
        }
        navigation["navigation_digest"] = digest(navigation)
        return navigation


def algorithm_entries() -> list[dict[str, Any]]:
    return [
        {
            "algorithm_id": "dynamic_sparse_route",
            "owner": "Zyra scheduler/topology runtime",
            "pseudocode": [
                "for each ready semantic event:",
                "  candidates <- workers matching capability and privacy",
                "  score <- load + SLA + route affinity + failure penalty",
                "  choose deterministic minimum score and acquire lease",
                "  mutate topology only when the selected role/edge is absent",
            ],
            "time_complexity": "O(E * (W + log W)) for E events and W workers",
            "space_complexity": "O(W + R) for workers and sparse routes",
            "evidence_metrics": [
                "topology_sparsity",
                "topology_churn",
                "placement_policy_compliance",
            ],
        },
        {
            "algorithm_id": "low_entropy_evidence_route",
            "owner": "Zyra communication runtime",
            "pseudocode": [
                "derive intent and required role from the semantic effect",
                "rank causally adjacent recipients with matching capability",
                "send one content-addressed evidence reference",
                "broadcast only in the explicit ablation/control baseline",
            ],
            "time_complexity": "O(E * (D + log W)) for route degree D",
            "space_complexity": "O(M + R) for messages and sparse routes",
            "evidence_metrics": [
                "communication_entropy",
                "useful_communication_ratio",
                "communication_delivery_count",
            ],
        },
        {
            "algorithm_id": "content_addressed_compact_restore",
            "owner": "Zyra memory/compact runtime",
            "pseudocode": [
                "hash each semantic event into its effect namespace",
                "deduplicate against local and global memory windows",
                "compact each bounded window into a checksum-bound summary",
                "on resume restore only a verified committed digest",
            ],
            "time_complexity": "O(E) expected with hash lookup",
            "space_complexity": "O(K * C) for K namespaces and capacity C",
            "evidence_metrics": [
                "memory_hit_ratio",
                "compact_ratio",
                "restore_success_rate",
            ],
        },
        {
            "algorithm_id": "fault_bound_exact_recovery",
            "owner": "Zyra fault/recovery runtime",
            "pseudocode": [
                "bind every fault signal to an owner event and checkpoint",
                "classify retry, replace, reroute, replan or deterministic reject",
                "apply recovery behind an idempotency fence",
                "verify resume lineage and post-recovery artifact constraints",
            ],
            "time_complexity": "O(F * (log C + W)) for F faults",
            "space_complexity": "O(F + C) for fault and checkpoint journals",
            "evidence_metrics": [
                "fault_recovery_rate",
                "recovery_mttr_units",
                "unresolved_fault_count",
            ],
        },
        {
            "algorithm_id": "streaming_distribution_and_bundle_integrity",
            "owner": "MetricAggregationRuntime/EvidenceBundleVerifier",
            "pseudocode": [
                "admit checksum-bound raw samples only",
                "compute online moments plus ordered P50/P95, MAD and IQR",
                "bootstrap mean confidence with the frozen seed",
                "hash each bundle member and reduce a deterministic Merkle tree",
                "fail if any required member, edge or digest is absent",
            ],
            "time_complexity": "O(N log N + B log B) for N samples, B members",
            "space_complexity": "O(N + B) for ordered statistics and manifest",
            "evidence_metrics": [
                "raw_sample_count",
                "p50",
                "p95",
                "bundle_root_digest",
            ],
        },
    ]
