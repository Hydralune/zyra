from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from zyra_evaluation.live_benchmark import (
    BenchmarkStore,
    DeploymentEvidenceVerifier,
    MetricCatalog,
    MetricDefinition,
    StatisticalEvaluator,
    create_campaign,
    default_variants,
    validate_variants,
    verify_campaign_plan,
)
from zyra_evaluation.live_benchmark.canonical import BenchmarkValidationError, digest
from zyra_evaluation.live_benchmark.models import (
    MetricDirection,
    RawSample,
    SampleStatus,
)
from zyra_evaluation.live_benchmark.semantic_steps import SemanticStepVerifier


COMMIT = "a" * 40
SHA = "b" * 64


def campaign_request() -> dict:
    return {
        "campaign_id": "campaign-test",
        "commit_sha": COMMIT,
        "sealed_policy_digest": digest("sealed"),
        "environment_digest": digest("environment"),
        "hardware_digest": digest("hardware"),
        "deployment_digest": digest("deployment"),
        "provider_policy_digest": digest("providers"),
        "verifier_digest": digest("verifier"),
        "failure_schedule_digest": digest("faults"),
        "budget_digest": digest("budget"),
        "source_evidence_digest": digest("source"),
        "seeds": [117, 311, 911],
        "domains": ["software-delivery", "cross-source-research"],
        "input_revisions": {
            "software-delivery": ["software-r1", "software-r2", "software-r3"],
            "cross-source-research": ["research-r1", "research-r2", "research-r3"],
        },
        "task_family_digests": {
            "software-delivery": digest("software-family"),
            "cross-source-research": digest("research-family"),
        },
        "minimum_effective_steps": 4,
        "required_long_run_steps": 2000,
    }


def test_formal_plan_is_complete_paired_and_one_capability_ablated() -> None:
    campaign = create_campaign(campaign_request())
    receipt = verify_campaign_plan(campaign)

    assert receipt["valid"] is True
    assert receipt["cell_count"] == 42
    assert receipt["domain_count"] == 2
    assert receipt["variant_count"] == 7
    assert receipt["repetition_count"] == 3

    variants = list(default_variants())
    broken = replace(
        variants[-1],
        capabilities=replace(variants[-1].capabilities, recovery=False),
    )
    with pytest.raises(
        BenchmarkValidationError,
        match="incomplete or unfair",
    ) as raised:
        validate_variants((*variants[:-1], broken))
    assert raised.value.code == "benchmark-variant-catalog-invalid"


def events(run_id: str) -> list[dict]:
    result = []
    definitions = (
        ("task-created", "state-mutation", {"state": "running"}),
        ("tool-completed", "tool", {"tool_call_id": "tool-1"}),
        ("verification-completed", "verification", {"verification_id": "verify-1"}),
        ("artifact-published", "artifact", {"artifact_id": "artifact-1"}),
    )
    for index, (event_type, effect, mutation) in enumerate(definitions, start=1):
        result.append(
            {
                "event_id": f"event-{index}",
                "run_id": run_id,
                "sequence": index,
                "event_type": event_type,
                "effect": effect,
                "payload": {},
                "mutation": mutation,
                "causal_parent_ids": [] if index == 1 else [f"event-{index - 1}"],
            }
        )
    return result


def test_semantic_step_verifier_rejects_inflation() -> None:
    verifier = SemanticStepVerifier()
    selected = events("run-test")
    receipt = verifier.verify(
        selected,
        declared_effective_event_ids=[item["event_id"] for item in selected],
        minimum_effective_steps=4,
        run_id="run-test",
    )
    assert receipt["effective_step_count"] == 4

    selected[1]["event_type"] = "heartbeat"
    with pytest.raises(BenchmarkValidationError) as raised:
        verifier.verify(
            selected,
            declared_effective_event_ids=[item["event_id"] for item in selected],
            minimum_effective_steps=3,
            run_id="run-test",
        )
    assert raised.value.code == "benchmark-semantic-steps-invalid"
    assert "effective-step-inflation" in str(raised.value.detail)


def protected_observation(tier: str, index: int) -> dict:
    return {
        "observation_id": f"tier-{tier}",
        "tier": tier,
        "endpoint": (
            "local://device/runtime"
            if tier == "device"
            else (
                "https://benchmark-cloud.example/runtime"
                if tier == "cloud"
                else f"https://203.0.113.{index}/runtime"
            )
        ),
        "runtime_id": f"runtime-{tier}",
        "process_id": f"process-{tier}",
        "isolation_id": f"isolation-{tier}",
        "simulated": False,
        "handshake_ok": True,
        "task_success": True,
        "evidence_mode": "protected-prior",
        "prior_receipt_id": f"M1-{tier}",
        "prior_receipt_digest": digest(("prior", tier)),
        "prior_receipt_still_valid": True,
        "started_at": "2026-07-27T00:00:00Z",
        "completed_at": "2026-07-27T00:00:01Z",
        "request_digest": digest(("request", tier)),
        "response_digest": digest(("response", tier)),
    }


def provider_observation(index: int) -> dict:
    return {
        "observation_id": f"provider-{index}",
        "provider_id": f"provider-{index}",
        "model_id": f"model-{index}",
        "request_id": f"request-{index}",
        "request_digest": digest(("provider-request", index)),
        "response_digest": digest(("provider-response", index)),
        "evidence_mode": "protected-prior",
        "prior_receipt_id": f"M1-provider-{index}",
        "prior_receipt_digest": digest(("prior-provider", index)),
        "prior_receipt_still_valid": True,
        "simulated": False,
        "authenticated": True,
        "response_status": 200,
        "tool_call_ids": [f"tool-{index}"],
        "tool_result_ids": [f"tool-{index}"],
        "latency_ms": 20 + index,
        "cost_usd": 0.01,
    }


def test_deployment_verifier_binds_tiers_models_privacy_and_failover() -> None:
    routes = [
        {
            "route_id": "route-1",
            "tier": "device",
            "privacy_class": "restricted",
            "sla_class": "interactive",
            "provider_id": "provider-1",
            "model_id": "model-1",
            "reason": "private input",
            "selected_at": "2026-07-27T00:00:00Z",
            "privacy_compliant": True,
            "sla_compliant": True,
        },
        {
            "route_id": "route-2",
            "tier": "cloud",
            "privacy_class": "public",
            "sla_class": "recovery",
            "provider_id": "provider-2",
            "model_id": "model-2",
            "reason": "provider failover",
            "selected_at": "2026-07-27T00:00:01Z",
            "privacy_compliant": True,
            "sla_compliant": True,
        },
    ]
    value = {
        "tier_observations": [
            protected_observation("device", 1),
            protected_observation("edge", 2),
            protected_observation("cloud", 3),
        ],
        "provider_observations": [
            provider_observation(1),
            provider_observation(2),
        ],
        "route_decisions": routes,
        "failovers": [
            {
                "failover_id": "failover-1",
                "route_before": "route-1",
                "route_after": "route-2",
                "reason": "provider-failure",
                "successful": True,
                "delivery_resumed": True,
                "detected_at": "2026-07-27T00:00:00Z",
                "resumed_at": "2026-07-27T00:00:01Z",
            }
        ],
        "disconnect_degradation": {
            "event_id": "disconnect-1",
            "observed": True,
            "safe": True,
            "route_before": "route-1",
            "route_after": "route-2",
            "delivery_resumed": True,
            "browser_required": False,
            "relabeled_as_cloud": False,
        },
    }
    receipt = DeploymentEvidenceVerifier().verify(value, run_id="run-test")
    assert receipt["valid"] is True
    assert receipt["tier_receipt"]["tiers"] == ["device", "edge", "cloud"]
    assert len(receipt["provider_receipt"]["provider_ids"]) == 2

    value["provider_observations"][1]["prior_receipt_still_valid"] = False
    with pytest.raises(BenchmarkValidationError) as raised:
        DeploymentEvidenceVerifier().verify(value, run_id="run-test")
    assert raised.value.code == "benchmark-provider-evidence-invalid"


def test_store_lease_completion_and_journal_tamper_detection(tmp_path: Path) -> None:
    campaign = create_campaign(campaign_request())
    store = BenchmarkStore(tmp_path / "store")
    store.create(campaign.to_dict())
    store.transition(campaign.campaign_id, "running")
    cell = campaign.cells[0]
    lease = store.lease(campaign.campaign_id, cell.cell_id, worker_id="worker-1")
    store.start_cell(
        campaign.campaign_id,
        cell.cell_id,
        lease_id=lease["lease_id"],
        worker_id="worker-1",
    )
    result = {
        "cell": cell.to_dict(),
        "run_id": "run-1",
        "phase": "admitted",
        "live_receipt": {},
        "admission_receipt": {},
        "verifier_receipt": {},
        "deployment_receipt": {},
        "fault_receipt": {},
        "samples": [],
        "started_at": "2026-07-27T00:00:00Z",
        "completed_at": "2026-07-27T00:00:01Z",
        "failure": {},
    }
    store.complete_cell(
        campaign.campaign_id,
        cell.cell_id,
        lease_id=lease["lease_id"],
        worker_id="worker-1",
        result=result,
    )
    assert store.verify_journal(campaign.campaign_id)["valid"] is True

    journal = tmp_path / "store" / campaign.campaign_id / "journal.jsonl"
    journal.write_text(
        journal.read_text(encoding="utf-8").replace(
            "campaign-created",
            "campaign-tampered",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkValidationError) as raised:
        store.verify_journal(campaign.campaign_id)
    assert raised.value.code == "benchmark-journal-entry-tampered"


def test_statistics_report_p50_p95_confidence_and_paired_deltas() -> None:
    definition = MetricDefinition(
        metric_id="quality.score",
        title="Quality score",
        unit="ratio",
        direction=MetricDirection.HIGHER_IS_BETTER,
        required=True,
        minimum=0,
        maximum=1,
    )
    catalog = MetricCatalog((definition,))
    samples = []
    for domain in ("software-delivery", "cross-source-research"):
        for variant_index, variant in enumerate(
            item.variant_id for item in default_variants()
        ):
            for repetition in range(1, 4):
                samples.append(
                    RawSample(
                        sample_id=f"sample-{domain}-{variant}-{repetition}",
                        campaign_id="campaign-test",
                        cell_id=f"cell-{domain}-{variant}-{repetition}",
                        run_id=f"run-{domain}-{variant}-{repetition}",
                        domain=__import__(
                            "zyra_evaluation.live_benchmark.models",
                            fromlist=["DomainKind"],
                        ).DomainKind(domain),
                        variant_id=variant,
                        repetition=repetition,
                        seed=repetition,
                        metric_id="quality.score",
                        value=0.9 - variant_index * 0.03 + repetition * 0.001,
                        unit="ratio",
                        status=SampleStatus.OBSERVED,
                        observed_at="2026-07-27T00:00:00Z",
                        evidence_digest=SHA,
                    )
                )
    receipt = StatisticalEvaluator(
        catalog,
        bootstrap_iterations=200,
    ).evaluate(samples)
    assert receipt["valid"] is True
    assert receipt["distribution_count"] == 14
    assert receipt["comparison_count"] == 12
    assert all("p50" in item and "p95" in item for item in receipt["distributions"])
