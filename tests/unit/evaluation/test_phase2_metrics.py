from __future__ import annotations

import pytest

from zyra_evaluation.policy_benchmark import (
    COMMUNICATION_RECEIPTS,
    FIRST_STAGE_METRIC_COMPATIBILITY,
    PHASE2_METRIC_SPECS,
    InMemoryCanonicalReceiptResolver,
    MetricStatus,
    Phase2MetricEngine,
    Phase2MetricError,
    Phase2MetricReportBuilder,
    RunMetricInput,
    metric_spec_registry_payload,
    requirement_metric_map,
)


def _observation(
    source: str,
    target: str,
    index: int,
    *,
    cost: float = 0.01,
) -> dict[str, object]:
    return {
        "schema_version": "zyra.agentprune-communication-outcome/v1",
        "observation_id": f"observation-{index}",
        "run_id": "run-metrics",
        "task_id": "task-metrics",
        "window_id": "window-a",
        "completed_at": "2026-07-30T09:00:00Z",
        "edge_id": f"edge-{source}-{target}",
        "source_node_id": source,
        "target_node_id": target,
        "edge_type": "spatial" if index % 2 else "temporal",
        "round_index": 1,
        "message_id": f"message-{index}",
        "payload_digest": f"{index:064x}",
        "delivered": True,
        "delivery_receipt_ref": f"delivery-{index}",
        "usage_receipt_ref": f"usage-{index}",
        "message_bytes": 100,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "cost_usd": cost,
        "evidence_refs": [f"evidence-{index}"],
        "utilized_evidence_refs": [f"evidence-{index}"],
        "artifact_refs": [f"artifact-{index}"],
        "verifier_result": "passed",
        "permission_result": "allowed",
        "causal_refs": [f"delivery-{index}", f"usage-{index}"],
    }


def _input(
    observations: tuple[object, ...] = (),
    *,
    task_succeeded: bool = True,
    disconnected: tuple[str, ...] = (),
) -> RunMetricInput:
    return RunMetricInput(
        run_id="run-metrics",
        task_id="task-metrics",
        scenario_id="scenario-metrics",
        mechanism_profile="phase2_strongest_v1",
        receipt_resolver=InMemoryCanonicalReceiptResolver(
            {COMMUNICATION_RECEIPTS: observations},
            disconnected=disconnected,
        ),
        task_succeeded=task_succeeded,
        effective_transition_count=2,
    )


def test_metric_registry_defines_fixed_anti_gaming_semantics() -> None:
    assert len(PHASE2_METRIC_SPECS) >= 50
    assert set(requirement_metric_map()) >= {
        "REQ-COMM-01",
        "REQ-TOPO-01",
        "REQ-MEM-01",
        "REQ-EDGE-01",
        "REQ-TRACE-01",
    }
    assert FIRST_STAGE_METRIC_COMPATIBILITY["topology.normalized_churn"]
    for metric_id, spec in PHASE2_METRIC_SPECS.items():
        assert metric_id == spec.metric_id
        assert spec.numerator
        assert spec.denominator
        assert spec.unit
        assert spec.aggregation_unit
        assert spec.minimum_sample_size >= 1
        assert spec.requirement_ids
        assert spec.evidence_contracts
        assert spec.empty_semantics.value
        assert spec.missing_semantics.value
        assert spec.failed_task_semantics.value
    registry = metric_spec_registry_payload()
    assert registry["history_rewrite"] is False
    assert len(registry["digest"]) == 64


def test_empty_and_disconnected_receipts_never_report_success() -> None:
    empty = Phase2MetricEngine().evaluate_run(_input())
    assert all(
        item.status is not MetricStatus.OBSERVED
        for metric_id, item in empty.metrics.items()
        if metric_id.startswith("communication.")
    )
    disconnected = Phase2MetricEngine().evaluate_run(
        _input(disconnected=(COMMUNICATION_RECEIPTS,))
    )
    assert disconnected.metrics["communication.useful_message_ratio"].status is MetricStatus.FAILED
    assert (
        "canonical_receipt_resolver_unavailable"
        in disconnected.metrics["communication.useful_message_ratio"].reasons
    )


def test_normalized_entropy_is_comparable_across_graph_sizes() -> None:
    small = tuple(
        _observation(source, target, index)
        for index, (source, target) in enumerate(
            (("a", "b"), ("a", "c"), ("b", "a"), ("b", "c"), ("c", "a"), ("c", "b")),
            start=1,
        )
    )
    large_pairs = [
        (source, target)
        for source in ("a", "b", "c", "d", "e")
        for target in ("a", "b", "c", "d", "e")
        if source != target
    ]
    large = tuple(
        _observation(source, target, index)
        for index, (source, target) in enumerate(large_pairs, start=1)
    )
    engine = Phase2MetricEngine()
    small_value = engine.evaluate_run(_input(small)).metrics[
        "communication.normalized_entropy"
    ].value
    large_value = engine.evaluate_run(_input(large)).metrics[
        "communication.normalized_entropy"
    ].value
    assert small_value == pytest.approx(1.0)
    assert large_value == pytest.approx(1.0)


def test_report_separates_failed_low_cost_run_from_optimization() -> None:
    successful = _input((_observation("a", "b", 1, cost=0.5),))
    failed = RunMetricInput(
        run_id="run-failed",
        task_id="task-failed",
        scenario_id=successful.scenario_id,
        mechanism_profile=successful.mechanism_profile,
        receipt_resolver=InMemoryCanonicalReceiptResolver(
            {COMMUNICATION_RECEIPTS: (_observation("a", "b", 2, cost=0.0001),)}
        ),
        task_succeeded=False,
        effective_transition_count=1,
    )
    report = Phase2MetricReportBuilder().build((successful, failed))
    failed_run = next(item for item in report.runs if item.run_id == "run-failed")
    failed_metric = failed_run.metrics["communication.delivered_cost_usd"]
    assert failed_metric.status is MetricStatus.FAILED
    assert failed_metric.eligible_for_optimization is False
    assert report.aggregate.failed_run_count == 1
    assert report.aggregate.successful_run_count == 1


def test_duplicate_import_is_idempotent_but_conflicting_identity_fails() -> None:
    observation = _observation("a", "b", 1)
    engine = Phase2MetricEngine()
    once = engine.evaluate_run(_input((observation,)))
    twice = engine.evaluate_run(_input((observation, observation)))
    assert once.digest == twice.digest

    first = {"report_digest": "shared", "mechanism_id": "arg", "status": "evidence_only"}
    second = {"report_digest": "shared", "mechanism_id": "arg", "status": "deterministic_ready"}
    value = RunMetricInput(
        run_id="run-conflict",
        task_id="task-conflict",
        scenario_id="scenario-conflict",
        mechanism_profile="phase2_strongest_v1",
        receipt_resolver=InMemoryCanonicalReceiptResolver(
            {"readiness_reports": (first, second)}
        ),
        task_succeeded=True,
        effective_transition_count=0,
    )
    with pytest.raises(Phase2MetricError, match="inconsistent canonical digests"):
        engine.evaluate_run(value)


def test_inconsistent_supplied_digest_fails_closed() -> None:
    observation = _observation("a", "b", 1)
    observation["digest"] = "0" * 64
    with pytest.raises(Phase2MetricError) as captured:
        Phase2MetricEngine().evaluate_run(_input((observation,)))
    assert captured.value.code == "metric_receipt_digest_mismatch"
