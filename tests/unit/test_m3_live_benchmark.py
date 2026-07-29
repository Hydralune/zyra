from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from zyra_evaluation.live_benchmark import (
    BenchmarkStore,
    CurrentCampaignEvidenceVerifier,
    CurrentTierDispatchRunner,
    DeploymentEvidenceVerifier,
    LiveBenchmarkFreezeGate,
    MetricCatalog,
    MetricDefinition,
    ProtectedDeploymentEvidenceLoader,
    StatisticalEvaluator,
    create_campaign,
    default_variants,
    validate_variants,
    verify_campaign_fault_coverage,
    verify_campaign_metric_completeness,
    verify_campaign_plan,
    verify_campaign_run_uniqueness,
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
    protected_tier = "local" if tier == "device" else tier
    protected_fact = {
        "tier": protected_tier,
        "protocol": f"protocol-{protected_tier}",
        "transport": f"transport-{protected_tier}",
        "simulated": False,
    }
    if tier == "edge":
        protected_fact.update({"loopback": False, "isolated_process": True})
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
        "protected_fact": protected_fact,
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
        "protected_fact": {
            "provider_id": f"provider-{index}",
            "model_id": f"model-{index}",
            "wire_dialect": f"dialect-{index}",
            "request_path": f"/v1/path-{index}",
            "tool_call_and_result": True,
            "simulated": False,
        },
        "current_request_made": False,
        "no_new_provider_call": True,
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
            "evidence_mode": "live",
            "fresh": True,
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
            "evidence_mode": "live",
            "fresh": True,
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
    completeness = verify_campaign_metric_completeness(
        samples,
        expected_cell_ids=[item.cell_id for item in samples],
        catalog=catalog,
    )
    assert completeness["valid"] is True
    assert len(completeness["receipt_digest"]) == 64


def test_protected_m1_evidence_is_exact_and_tamper_fails_closed(
    tmp_path: Path,
) -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "reviews"
        / "evidence"
        / "M1-08-and-M1-exit-review-2026-07-23.json"
    )
    bundle = ProtectedDeploymentEvidenceLoader().load(source)
    assert {item["tier"] for item in bundle.tiers} == {
        "local",
        "edge",
        "cloud",
    }
    assert {item["provider_id"] for item in bundle.providers} == {
        "anthropic",
        "openai",
    }
    assert bundle.to_dict()["no_new_provider_call"] is True

    tampered = tmp_path / "tampered-m1.json"
    tampered.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(BenchmarkValidationError) as raised:
        ProtectedDeploymentEvidenceLoader().load(tampered)
    assert raised.value.code == "benchmark-protected-evidence-changed"


def test_freeze_gate_rejects_provider_receipts_separate_from_formal_cases() -> None:
    evidence = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "reviews"
        / "evidence"
        / "M3-S02A-02"
        / "formal-live-95fcf7ae"
    )

    with pytest.raises(BenchmarkValidationError) as raised:
        LiveBenchmarkFreezeGate().verify(
            evidence,
            expected_commit=(
                "95fcf7aeaed5b5ec80fb2f7178b97fbdf8adbeb6"
            ),
        )

    assert raised.value.code == "benchmark-freeze-admission-failed"
    codes = {
        finding["code"]
        for finding in raised.value.detail["findings"]
    }
    assert {
        "current-provider-evidence-missing",
        "current-model-request-missing",
        "current-provider-count-insufficient",
        "current-model-count-insufficient",
        "current-tier-evidence-incomplete",
        "case-deployment-evidence-separated",
    }.issubset(codes)


def current_sources() -> list[dict]:
    output = []
    for domain in ("software-delivery", "cross-source-research"):
        for repetition in range(1, 4):
            case_id = f"{domain}:r{repetition:02d}"
            safe_case_id = case_id.replace(":", "-")
            output.append(
                {
                    "domain": domain,
                    "repetition": repetition,
                    "outcome_digest": digest((case_id, "outcome")),
                    "source": {
                        "scenario_run_id": f"scenario-{safe_case_id}",
                        "owner_run_id": f"owner-{safe_case_id}",
                        "task_id": f"task-{safe_case_id}",
                        "archive_digest": digest((case_id, "archive")),
                    },
                }
            )
    return output


def current_campaign_evidence(sources: list[dict]) -> dict:
    cases = []
    for source_receipt in sources:
        domain = source_receipt["domain"]
        repetition = source_receipt["repetition"]
        case_id = f"{domain}:r{repetition:02d}"
        source = source_receipt["source"]
        binding = {
            "case_id": case_id,
            "domain": domain,
            "repetition": repetition,
            "source_run_id": source["scenario_run_id"],
            "owner_run_id": source["owner_run_id"],
            "task_id": source["task_id"],
            "source_archive_digest": source["archive_digest"],
            "source_outcome_digest": source_receipt["outcome_digest"],
        }
        tiers = []
        for tier, endpoint in (
            ("device", f"process://{1000 + repetition}"),
            ("edge", f"tcp://203.0.113.{10 + repetition}:4100"),
            ("cloud", "https://api.provider.example/v1"),
        ):
            tiers.append(
                {
                    "observation_id": f"{case_id}:{tier}",
                    **binding,
                    "run_id": binding["source_run_id"],
                    "tier": tier,
                    "fresh": True,
                    "current_dispatch": True,
                    "handshake_ok": True,
                    "task_success": True,
                    "simulated": False,
                    "dispatch_status": "succeeded",
                    "request_digest": digest((case_id, tier, "request")),
                    "response_digest": digest((case_id, tier, "response")),
                    "isolation_id": f"isolation-{case_id}-{tier}",
                    "runtime_id": f"runtime-{case_id}-{tier}",
                    "process_id": f"process-{case_id}-{tier}",
                    "endpoint": endpoint,
                    "isolated_process": True,
                    "loopback": False,
                }
            )
        providers = []
        for index in (1, 2):
            provider_id = f"provider-{index}"
            model_id = f"model-{index}"
            tool_call_id = f"tool-{case_id}-{index}"
            binding_token = hashlib.sha256(
                "\n".join(
                    (
                        "campaign-current",
                        case_id,
                        binding["source_run_id"],
                        binding["source_archive_digest"],
                        binding["source_outcome_digest"],
                        provider_id,
                        model_id,
                    )
                ).encode("utf-8")
            ).hexdigest()[:24]
            providers.append(
                {
                    "observation_id": f"{case_id}:{provider_id}",
                    **binding,
                    "run_id": binding["source_run_id"],
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "live": True,
                    "fresh": True,
                    "authenticated": True,
                    "external_model_request": True,
                    "simulated": False,
                    "credential_material_persisted": False,
                    "http_status": 200,
                    "tool_call_ids": [tool_call_id],
                    "tool_result_ids": [tool_call_id],
                    "tool_name": "bind_formal_case",
                    "request_id": f"request-{case_id}-{index}",
                    "request_digest": digest((case_id, index, "request")),
                    "response_digest": digest((case_id, index, "response")),
                    "binding_token_digest": digest(binding_token),
                    "tool_result_digest": digest(
                        {
                            "tool_call_id": tool_call_id,
                            "tool_name": "bind_formal_case",
                            "accepted": True,
                            "binding_token": binding_token,
                            "source_archive_digest": binding[
                                "source_archive_digest"
                            ],
                            "source_outcome_digest": binding[
                                "source_outcome_digest"
                            ],
                        }
                    ),
                }
            )
        cases.append(
            {
                **binding,
                "tier_observations": tiers,
                "provider_observations": providers,
            }
        )
    evidence = {
        "schema": "zyra.m3-current-campaign-evidence/v1",
        "status": "passed",
        "campaign_id": "campaign-current",
        "implementation_commit": COMMIT,
        "case_count": len(cases),
        "provider_request_count": len(cases) * 2,
        "provider_usage_by_provider": {},
        "tier_dispatch_case_count": len(cases),
        "current_provider_ids": ["provider-1", "provider-2"],
        "current_model_ids": ["model-1", "model-2"],
        "current_tier_ids": ["device", "edge", "cloud"],
        "external_model_request_made": True,
        "authenticated_provider_runtime_invoked": True,
        "protected_prior_receipts_only": False,
        "same_run_as_formal_cases": True,
        "credential_material_persisted": False,
        "human_intervention_count": 0,
        "operator_intervention_count": 0,
        "cases": cases,
    }
    evidence["receipt_digest"] = digest(evidence)
    return evidence


def test_current_provider_and_tier_evidence_is_bound_to_every_formal_case() -> None:
    sources = current_sources()
    evidence = current_campaign_evidence(sources)
    receipt = CurrentCampaignEvidenceVerifier().verify(
        evidence,
        campaign_id="campaign-current",
        implementation_commit=COMMIT,
        sources=sources,
    )

    assert receipt["valid"] is True
    assert receipt["case_count"] == 6
    assert receipt["provider_request_count"] == 12
    assert receipt["tier_ids"] == ["device", "edge", "cloud"]

    shared_response_digest = evidence["cases"][0]["provider_observations"][0][
        "response_digest"
    ]
    evidence["cases"][1]["provider_observations"][0][
        "response_digest"
    ] = shared_response_digest
    evidence["receipt_digest"] = digest(
        {key: value for key, value in evidence.items() if key != "receipt_digest"}
    )
    duplicate_response_receipt = CurrentCampaignEvidenceVerifier().verify(
        evidence,
        campaign_id="campaign-current",
        implementation_commit=COMMIT,
        sources=sources,
    )
    assert duplicate_response_receipt["valid"] is True

    evidence["cases"][0]["provider_observations"][0][
        "source_archive_digest"
    ] = digest("wrong-source")
    evidence["receipt_digest"] = digest(
        {key: value for key, value in evidence.items() if key != "receipt_digest"}
    )
    with pytest.raises(BenchmarkValidationError) as raised:
        CurrentCampaignEvidenceVerifier().verify(
            evidence,
            campaign_id="campaign-current",
            implementation_commit=COMMIT,
            sources=sources,
        )
    assert raised.value.code == "benchmark-current-campaign-evidence-invalid"
    assert "provider-source-binding-mismatch" in str(raised.value.detail)


def test_current_tier_runner_uses_isolated_device_and_edge_processes(
    tmp_path: Path,
) -> None:
    source = current_sources()[0]
    case = {
        "case_id": "software-delivery:r01",
        "domain": source["domain"],
        "repetition": source["repetition"],
        "source_run_id": source["source"]["scenario_run_id"],
        "owner_run_id": source["source"]["owner_run_id"],
        "task_id": source["source"]["task_id"],
        "source_archive_digest": source["source"]["archive_digest"],
        "source_outcome_digest": source["outcome_digest"],
    }
    receipt = CurrentTierDispatchRunner(
        project_root=Path(__file__).resolve().parents[2],
        state_root=tmp_path / "current-tiers",
    ).run([case])

    assert receipt["status"] == "passed"
    observations = receipt["cases"][0]["tier_observations"]
    assert {item["tier"] for item in observations} == {"device", "edge"}
    assert len({item["process_id"] for item in observations}) == 2
    assert all(item["dispatch_status"] == "succeeded" for item in observations)
    assert next(item for item in observations if item["tier"] == "edge")[
        "endpoint"
    ].startswith("tcp://")


def test_campaign_fault_coverage_is_union_and_measures_no_recovery() -> None:
    enabled = {
        "fault_receipt": {
            "kind_counts": {
                "exception": 1,
                "worker-loss": 1,
                "provider-failure": 1,
                "edge-disconnect": 1,
            }
        },
        "requirement_change_receipt": {
            "kind_counts": {"scope-change": 1}
        },
        "recovery_expected": True,
        "final_delivery_succeeded": True,
    }
    disabled = {
        "fault_receipt": {
            "kind_counts": {
                "tool-failure": 1,
                "node-loss": 1,
                "provider-failure": 1,
                "network-failure": 1,
            }
        },
        "requirement_change_receipt": {
            "kind_counts": {"acceptance-criteria-change": 1}
        },
        "recovery_expected": False,
        "final_delivery_succeeded": False,
    }
    receipt = verify_campaign_fault_coverage((enabled, disabled))
    assert receipt["valid"] is True
    assert receipt["recovery_modes"] == {"disabled": 1, "enabled": 1}
    assert receipt["expected_no_recovery_failure_count"] == 1

    disabled["fault_receipt"]["kind_counts"].pop("network-failure")
    with pytest.raises(BenchmarkValidationError) as raised:
        verify_campaign_fault_coverage((enabled, disabled))
    assert raised.value.code == "benchmark-campaign-fault-coverage-invalid"


def test_campaign_uniqueness_shares_only_paired_source_block() -> None:
    variants = [item.variant_id for item in default_variants()]
    receipts = []
    for domain in ("software-delivery", "cross-source-research"):
        for repetition in range(1, 4):
            source = f"{domain}-source-{repetition}"
            archive = digest((domain, repetition, "archive"))
            revision = f"{domain}-revision-{repetition}"
            for variant in variants:
                identity = f"{domain}-{repetition}-{variant}"
                receipts.append(
                    {
                        "run_id": f"run-{identity}",
                        "live_receipt_digest": digest((identity, "live")),
                        "variant_execution_digest": digest(
                            (identity, "variant-execution")
                        ),
                        "semantic_step_receipt": {
                            "event_stream_digest": digest((identity, "events"))
                        },
                        "domain": domain,
                        "repetition": repetition,
                        "source_run_id": source,
                        "source_archive_digest": archive,
                        "input_revision": revision,
                        "variant_id": variant,
                    }
                )
    receipt = verify_campaign_run_uniqueness(receipts)
    assert receipt["valid"] is True
    assert receipt["run_count"] == 42
    assert receipt["source_run_count"] == 6

    receipts[0]["source_run_id"] = "unexpected-second-source"
    with pytest.raises(BenchmarkValidationError) as raised:
        verify_campaign_run_uniqueness(receipts)
    assert raised.value.code == "benchmark-run-uniqueness-invalid"
