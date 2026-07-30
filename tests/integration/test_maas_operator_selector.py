from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from zyra_core import create_task_state
from zyra_orchestration.topology_policy import (
    ContractHeader,
    EnvironmentSnapshot,
    FrozenDict,
    GraphSnapshotRef,
    MechanismEvidenceReadinessReportRef,
    PolicyBudget,
    PolicyInputSnapshot,
    PolicyNodeSnapshot,
    canonical_digest,
)
from zyra_scheduler import (
    MaasOperatorPolicyRuntime,
    OperatorCatalog,
    OperatorCatalogError,
    OperatorProfile,
    OperatorSelectorConfig,
    OperatorType,
    ResourceScheduler,
)


ROOT = Path(__file__).resolve().parents[2]
NOW = "2026-07-30T08:00:00Z"


def _header(
    contract_id: str,
    mechanism_id: str,
    *,
    configuration_digest: str = "",
) -> ContractHeader:
    return ContractHeader(
        contract_id=contract_id,
        created_at=NOW,
        source_event_id="event-maas-trigger",
        correlation_id="correlation-maas",
        causation_id="commit-topology-1",
        mechanism_id=mechanism_id,
        mechanism_version="v1",
        input_version="v1",
        idempotency_key=f"idempotency:{contract_id}",
        configuration_digest=configuration_digest,
    )


def _policy_input(
    *,
    run_id: str = "run-maas",
    task_id: str = "task-maas",
    stage: str = "input_precheck",
    status: str = "deterministic_ready",
    phase: str = "execution",
    obligations: tuple[str, ...] = (
        "implement the requested artifact",
        "verify the requested artifact",
    ),
    remaining_tokens: int = 20_000,
    remaining_cost_usd: float = 10.0,
    remaining_time_ms: int = 180_000,
    max_fan_out: int = 4,
    allowed_permissions: tuple[str, ...] = (
        "worker.dispatch",
        "tool:trace",
        "model.invoke",
        "skill.invoke",
        "agent.spawn",
        "permission_mode:default",
    ),
    allowed_placements: tuple[str, ...] = ("local", "edge", "cloud"),
    privacy_class: str = "internal",
    commit_id: str = "commit-topology-1",
) -> PolicyInputSnapshot:
    report_digest = canonical_digest(("maas-readiness", stage, status))
    return PolicyInputSnapshot(
        header=_header("policy-input-maas", "PolicyInputSnapshotBuilder"),
        run_id=run_id,
        task_id=task_id,
        phase=phase,
        requirement_revision="requirement-r2",
        graph=GraphSnapshotRef(
            graph_id="graph-maas",
            run_id=run_id,
            revision=7,
            signature=canonical_digest(("graph-maas", 7, commit_id)),
            commit_id=commit_id,
        ),
        nodes=(
            PolicyNodeSnapshot(
                node_id="node-implement",
                role="implementer",
                capabilities=("code-change", "artifact-production"),
            ),
            PolicyNodeSnapshot(
                node_id="node-verify",
                role="verifier",
                capabilities=("verification", "testing"),
                dependencies=("node-implement",),
            ),
        ),
        registered_roles=("implementer", "verifier"),
        registered_capabilities=(
            "code-change",
            "artifact-production",
            "verification",
            "testing",
        ),
        unresolved_obligations=obligations,
        registry_versions=FrozenDict(
            {
                "graph_state_custody": "v1",
                "worker_pool": "v1",
                "tool_registry": "v1",
                "model_registry": "v1",
            }
        ),
        environment=EnvironmentSnapshot(
            header=_header("environment-maas", "EnvironmentSnapshotBuilder"),
            observed_at=NOW,
            observations=(),
        ),
        memory_refs=(),
        readiness_refs=(
            MechanismEvidenceReadinessReportRef(
                header=_header("readiness-maas", "maas"),
                report_ref="urn:zyra:readiness:maas",
                report_digest=report_digest,
                readiness_stage=stage,
                status=status,
            ),
        ),
        budget=PolicyBudget(
            remaining_tokens=remaining_tokens,
            remaining_cost_usd=remaining_cost_usd,
            remaining_time_ms=remaining_time_ms,
            max_communication_bytes=64_000,
            max_fan_out=max_fan_out,
            max_topology_churn=8,
            minimum_dwell_seconds=0,
        ),
        allowed_permissions=allowed_permissions,
        allowed_placements=allowed_placements,
        privacy_class=privacy_class,
        last_topology_change_at="2026-07-30T07:00:00Z",
    )


def _profile(
    operator_id: str,
    *,
    capabilities: tuple[str, ...] = ("code-change",),
    permissions: tuple[str, ...] = ("worker.dispatch",),
    locations: tuple[str, ...] = ("local",),
    privacy: tuple[str, ...] = ("internal",),
    health: str = "healthy",
    verifier_contracts: tuple[str, ...] = ("artifact_verifier",),
    evidence_contract: tuple[str, ...] = ("operator_receipt",),
    cold_start: bool = False,
    confidence: float = 1.0,
    estimated_tokens: int = 100,
    estimated_cost_usd: float = 0.0,
    estimated_latency_ms: int = 50,
    operator_type: OperatorType = OperatorType.WORKER,
) -> OperatorProfile:
    return OperatorProfile(
        operator_id=operator_id,
        operator_type=operator_type,
        version="1",
        display_name=operator_id,
        description=f"{operator_id} code change artifact verification",
        capabilities=capabilities,
        input_contract=("task",),
        output_contract=("artifact",),
        required_permissions=permissions,
        allowed_locations=locations,
        allowed_privacy_classes=privacy,
        estimated_tokens=estimated_tokens,
        estimated_cost_usd=estimated_cost_usd,
        estimated_latency_ms=estimated_latency_ms,
        health_status=health,
        available_capacity=1,
        verifier_contracts=verifier_contracts,
        minimum_evidence_contract=evidence_contract,
        cold_start=cold_start,
        confidence=confidence,
        outcome_count=0 if cold_start else 5,
        source_registry="test-registry",
        source_registry_version="v1",
        source_ref=f"urn:test:{operator_id}",
        metadata=FrozenDict(),
    )


def _catalog(
    profiles: tuple[OperatorProfile, ...],
    *,
    generation: int = 1,
) -> OperatorCatalog:
    return OperatorCatalog(
        entries=profiles,
        source_versions=FrozenDict({"test-registry": f"v{generation}"}),
        generation=generation,
        built_at=NOW,
    )


def _runtime() -> MaasOperatorPolicyRuntime:
    config = OperatorSelectorConfig.load(
        ROOT / "config" / "phase2" / "maas-operator-selector.json"
    )
    return MaasOperatorPolicyRuntime(config=config)


def test_filters_are_hard_and_fixed_input_is_deterministic() -> None:
    catalog = _catalog(
        (
            _profile("worker:valid"),
            _profile("worker:missing-capability", capabilities=("research",)),
            _profile("worker:permission-denied", permissions=("secret.use",)),
            _profile("worker:privacy-denied", privacy=("public",)),
            _profile("worker:unhealthy", health="unavailable"),
            _profile("worker:no-verifier-contract", verifier_contracts=()),
        )
    )
    policy_input = _policy_input()
    runtime = _runtime()

    first = runtime.execute(
        policy_input=policy_input,
        query="Implement a code change and produce an artifact",
        catalog=catalog,
        required_capabilities=("code-change",),
        explicit_validation=True,
    )
    second = runtime.execute(
        policy_input=policy_input,
        query="Implement a code change and produce an artifact",
        catalog=catalog,
        required_capabilities=("code-change",),
        explicit_validation=True,
    )

    assert first.mode == "validation"
    assert first.proposal is not None
    assert second.proposal is not None
    assert first.proposal.digest == second.proposal.digest
    assert first.proposal.to_dict() == second.proposal.to_dict()
    assert [item.operator_id for item in first.proposal.candidates] == [
        "worker:valid"
    ]
    verdicts = {
        item.operator_id: item for item in first.proposal.filter_verdicts
    }
    assert "missing required capability" in verdicts[
        "worker:missing-capability"
    ].reasons
    assert "required permission not allowed" in verdicts[
        "worker:permission-denied"
    ].reasons
    assert "privacy class not allowed" in verdicts[
        "worker:privacy-denied"
    ].reasons
    assert "operator health unavailable" in verdicts[
        "worker:unhealthy"
    ].reasons
    assert any(
        "verifier evidence contract missing" in reason
        for reason in verdicts["worker:no-verifier-contract"].reasons
    )


def test_phase_obligation_and_budget_change_breadth_and_depth() -> None:
    profiles = tuple(
        _profile(
            f"worker:operator-{index}",
            capabilities=(
                "code-change",
                "artifact-production",
                "verification" if index % 2 else "testing",
            ),
        )
        for index in range(10)
    )
    catalog = _catalog(profiles)
    runtime = _runtime()
    low = runtime.execute(
        policy_input=_policy_input(
            obligations=("produce artifact",),
            remaining_tokens=1_000,
            remaining_time_ms=10_000,
            max_fan_out=1,
        ),
        query="Produce artifact",
        catalog=catalog,
        explicit_validation=True,
    )
    high = runtime.execute(
        policy_input=_policy_input(
            phase="verification",
            obligations=(
                "implement artifact",
                "test artifact",
                "review artifact",
                "verify evidence",
                "publish result",
            ),
            remaining_tokens=50_000,
            remaining_time_ms=300_000,
            max_fan_out=4,
        ),
        query="Implement test review verify and publish the artifact",
        catalog=catalog,
        verifier_necessary=True,
        explicit_validation=True,
    )

    assert low.proposal is not None
    assert high.proposal is not None
    assert (low.proposal.expected_breadth, low.proposal.expected_depth) == (1, 1)
    assert high.proposal.expected_breadth > low.proposal.expected_breadth
    assert high.proposal.expected_depth > low.proposal.expected_depth
    assert high.proposal.requirement_revision == low.proposal.requirement_revision
    assert high.proposal.context_digest != low.proposal.context_digest


def test_cold_start_can_rank_but_requires_conservative_reserve() -> None:
    established = _profile("worker:established", confidence=1.0)
    cold = _profile(
        "worker:cold",
        cold_start=True,
        confidence=0.55,
        estimated_tokens=900,
    )
    catalog = _catalog((established, cold))
    runtime = _runtime()
    ample = runtime.execute(
        policy_input=_policy_input(
            remaining_tokens=20_000,
            remaining_time_ms=180_000,
        ),
        query="Code change artifact",
        catalog=catalog,
        explicit_validation=True,
    )
    tight = runtime.execute(
        policy_input=_policy_input(
            remaining_tokens=1_000,
            remaining_time_ms=10_000,
            max_fan_out=2,
        ),
        query="Code change artifact",
        catalog=catalog,
        explicit_validation=True,
    )

    assert ample.proposal is not None
    assert any(item.operator_id == "worker:cold" for item in ample.proposal.candidates)
    assert tight.proposal is not None
    assert all(item.operator_id != "worker:cold" for item in tight.proposal.candidates)
    cold_verdict = next(
        item
        for item in tight.proposal.filter_verdicts
        if item.operator_id == "worker:cold"
    )
    assert "cold-start token reserve unavailable" in cold_verdict.reasons


def test_readiness_modes_and_scheduler_input_preserve_placement_owner() -> None:
    state = create_task_state("Implement and verify a local code artifact.")
    catalog = _catalog((_profile("worker:maas-candidate"),))
    runtime = _runtime()
    validation_input = _policy_input(
        run_id=state.run_id,
        task_id=state.task_id,
    )
    validation = runtime.execute(
        policy_input=validation_input,
        query=state.user_goal,
        catalog=catalog,
        required_capabilities=("code-change",),
        explicit_validation=True,
    )
    baseline_scheduler = ResourceScheduler().decide(state)
    selected_scheduler = ResourceScheduler().decide(
        state,
        operator_input=validation.scheduler_input,
    )

    assert validation.mode == "validation"
    assert validation.scheduler_input is not None
    assert validation.readiness.placement_change_allowed is False
    assert validation.physical_attempt_created is False
    assert selected_scheduler.selected_manifest_id == baseline_scheduler.selected_manifest_id
    assert selected_scheduler.metadata["operator_candidate_contract_consumed"] is True
    assert (
        selected_scheduler.metadata["operator_candidate_contract_effect"]
        == "selection_input_only_pending_P2-S04-03"
    )
    assert selected_scheduler.metadata["placement_owner"] == "ResourceScheduler"

    diagnostic = runtime.execute(
        policy_input=_policy_input(
            run_id=state.run_id,
            task_id=state.task_id,
            status="evidence_only",
        ),
        query=state.user_goal,
        catalog=catalog,
        required_capabilities=("code-change",),
    )
    assert diagnostic.mode == "diagnostic"
    assert diagnostic.proposal is not None
    assert diagnostic.scheduler_input is None
    assert diagnostic.baseline_decision["route_owner"] == "ResourceScheduler"
    with pytest.raises(
        ValueError,
        match="diagnostic operator ranking cannot enter ResourceScheduler",
    ):
        ResourceScheduler().decide(
            state,
            operator_input=replace(
                validation.scheduler_input,
                diagnostic_only=True,
            ),
        )

    unavailable = runtime.execute(
        policy_input=_policy_input(
            run_id=state.run_id,
            task_id=state.task_id,
            status="unavailable",
        ),
        query=state.user_goal,
        catalog=catalog,
    )
    disabled = runtime.execute(
        policy_input=validation_input,
        query=state.user_goal,
        catalog=catalog,
        enabled=False,
        explicit_validation=True,
    )
    assert unavailable.mode == "baseline"
    assert unavailable.proposal is None
    assert disabled.mode == "baseline"
    assert disabled.proposal is None
    disabled_decision = ResourceScheduler().decide(state)
    assert disabled_decision.selected_manifest_id == selected_scheduler.selected_manifest_id
    assert disabled_decision.metadata["operator_candidate_contract_consumed"] is False


def test_catalog_version_drift_and_revocation_invalidate_old_proposal() -> None:
    runtime = _runtime()
    policy_input = _policy_input()
    selected = _profile("worker:selected")
    first_catalog = _catalog((selected,), generation=1)
    result = runtime.execute(
        policy_input=policy_input,
        query="Code change artifact",
        catalog=first_catalog,
        explicit_validation=True,
    )
    assert result.proposal is not None

    drifted = _catalog(
        (
            replace(
                selected,
                version="2",
                enabled=False,
                revoked=True,
            ),
            _profile("worker:replacement"),
        ),
        generation=2,
    )
    with pytest.raises(
        OperatorCatalogError,
        match="different catalog version",
    ):
        runtime.validate_proposal(
            proposal=result.proposal,
            catalog=drifted,
            policy_input=policy_input,
        )


def test_missing_committed_topology_fails_closed_to_baseline() -> None:
    result = _runtime().execute(
        policy_input=_policy_input(commit_id=""),
        query="Code change artifact",
        catalog=_catalog((_profile("worker:valid"),)),
        explicit_validation=True,
    )

    assert result.mode == "baseline"
    assert result.proposal is None
    assert result.degraded_reason == "maas_committed_topology_missing"


def test_config_declares_no_training_sampling_or_pretrained_signal() -> None:
    config = OperatorSelectorConfig.load(
        ROOT / "config" / "phase2" / "maas-operator-selector.json"
    )
    assert config.no_policy_training["training_allowed"] is False
    assert config.no_policy_training["sampling_allowed"] is False
    assert config.no_policy_training["pretrained_model_used"] is False
    assert config.no_policy_training["datasets"] == ()
    assert config.no_policy_training["checkpoints"] == ()


def test_repository_runtime_verifies_readiness_report_before_validation(
    tmp_path: Path,
) -> None:
    config = OperatorSelectorConfig.load(
        ROOT / "config" / "phase2" / "maas-operator-selector.json"
    )
    report = {
        "schema": "zyra.mechanism-evidence-readiness-report/v1",
        "generated_at": NOW,
        "readiness_stage": "input_precheck",
        "supersedes_report_digest": config.input_precheck_report_digest,
        "mechanism_statuses": {"maas": "deterministic_ready"},
        "mechanisms": {
            "maas": {
                "readiness_stage": "input_precheck",
                "status": "deterministic_ready",
                "selector_validation": {
                    "passed": True,
                    "mechanism_version": config.mechanism_version,
                    "configuration_digest": config.digest,
                    "catalog_schema_version": config.catalog_schema_version,
                    "proposal_schema_version": config.proposal_schema_version,
                    "scheduler_input_schema_version": (
                        config.scheduler_input_schema_version
                    ),
                    "placement_change_allowed": False,
                    "lease_created": False,
                    "physical_attempt_created": False,
                },
            }
        },
        "no_policy_training_audit": {
            "passed": True,
            "training_sample_count": 0,
        },
    }
    report_digest = canonical_digest(report)
    report["report_digest"] = report_digest
    report_path = tmp_path / "MechanismEvidenceReadinessReport.json"
    report_path.write_text(
        json.dumps(report, sort_keys=True),
        encoding="utf-8",
    )
    policy_input = _policy_input()
    trusted_ref = replace(
        policy_input.readiness_refs[0],
        report_ref=report_path.as_posix(),
        report_digest=report_digest,
    )
    trusted_input = replace(policy_input, readiness_refs=(trusted_ref,))
    runtime = MaasOperatorPolicyRuntime.from_repository(
        ROOT,
        report_path=report_path,
    )

    trusted = runtime.execute(
        policy_input=trusted_input,
        query="Code change artifact",
        catalog=_catalog((_profile("worker:valid"),)),
        explicit_validation=True,
    )
    forged_input = replace(
        policy_input,
        readiness_refs=(
            replace(
                trusted_ref,
                report_digest=canonical_digest("forged-maas-report"),
            ),
        ),
    )
    forged = runtime.execute(
        policy_input=forged_input,
        query="Code change artifact",
        catalog=_catalog((_profile("worker:valid"),)),
        explicit_validation=True,
    )

    assert trusted.mode == "validation"
    assert trusted.scheduler_input is not None
    assert forged.mode == "baseline"
    assert forged.proposal is None
    assert (
        forged.readiness.reason
        == "policy input readiness does not match the verified MaAS report"
    )
