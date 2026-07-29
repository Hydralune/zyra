from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from zyra_core import EventType
from zyra_orchestration.topology_policy import (
    ActivationEvidence,
    MechanismLifecycle,
    MechanismRegistration,
    MechanismRegistry,
    NoPolicyTrainingValidator,
    PolicyRegistryError,
    ReadinessStage,
    ReadinessStatus,
    ResolutionPurpose,
    StrongestProfileActivationGate,
    ValidationManifest,
    canonical_digest,
)


ROOT = Path(__file__).resolve().parents[3]
FAMILY = "topology_policy"


def _record_mapping(
    *,
    version: str,
    lifecycle: str,
    activation_state: str,
    stage: str = "implementation_validated",
    status: str = "deterministic_ready",
    profile_id: str | None = None,
    baseline: bool = False,
) -> dict[str, object]:
    configuration = {
        "profile_id": profile_id or version,
        "version": version,
        "training_allowed": False,
    }
    readiness = (
        []
        if baseline
        else [
            {
                "mechanism_id": "arg_designer",
                "stage": stage,
                "status": status,
                "report_ref": (
                    "pending:test"
                    if status == "unavailable"
                    else "test/readiness.json"
                ),
                "report_digest": (
                    "" if status == "unavailable" else "1" * 64
                ),
            }
        ]
    )
    return {
        "family": FAMILY,
        "version": version,
        "profile_id": profile_id or version,
        "lifecycle": lifecycle,
        "activation_state": activation_state,
        "schema_version": "v1",
        "schema_digest": "2" * 64,
        "source_digest": "3" * 64,
        "configuration": configuration,
        "config_digest": canonical_digest(configuration),
        "implementation_commit": "4" * 40,
        "evidence_commit": "5" * 40,
        "required_mechanisms": [] if baseline else ["arg_designer"],
        "readiness": readiness,
        "rollback_family": FAMILY,
        "rollback_version": "baseline-v1",
        "timeout_seconds": 1.0,
        "no_policy_training": {
            "runtime_entry_points": [],
            "datasets": [],
            "checkpoints": [],
            "mutable_learned_parameters": [],
            "dependencies": [],
        },
        "hard_gate_ids": [] if baseline else ["success", "safety"],
        "audit_gate_ids": (
            [] if baseline else ["owner", "path", "dependency", "rollback"]
        ),
    }


def _record(**kwargs: object) -> MechanismRegistration:
    return MechanismRegistration.from_mapping(_record_mapping(**kwargs))


def _registry(
    *records: MechanismRegistration,
) -> MechanismRegistry:
    baseline = _record(
        version="baseline-v1",
        lifecycle="baseline",
        activation_state="active",
        baseline=True,
        profile_id="phase1_deterministic_baseline",
    )
    return MechanismRegistry(
        records=[baseline, *records],
        active_versions={FAMILY: baseline.version},
        fallback_family=FAMILY,
        fallback_version=baseline.version,
    )


def _manifest() -> ValidationManifest:
    return ValidationManifest(
        manifest_id="manifest-1",
        scenario_id="scenario-1",
        isolated=True,
        purpose="preflight",
    )


def _activation_evidence(
    record: MechanismRegistration,
    *,
    gate_override: dict[str, bool] | None = None,
) -> ActivationEvidence:
    hard_gates = {gate: True for gate in record.hard_gate_ids}
    hard_gates.update(gate_override or {})
    return ActivationEvidence.from_mapping(
        {
            "evidence_id": "activation-evidence-1",
            "family": record.family,
            "version": record.version,
            "schema_digest": record.schema_digest,
            "config_digest": record.config_digest,
            "source_digest": record.source_digest,
            "implementation_commit": record.implementation_commit,
            "evidence_commit": record.evidence_commit,
            "readiness_digest": record.readiness_digest,
            "hard_gates": hard_gates,
            "audits": {gate: True for gate in record.audit_gate_ids},
            "rollback_family": record.rollback_family,
            "rollback_version": record.rollback_version,
            "no_policy_training_passed": True,
        }
    )


def test_production_registry_loads_frozen_baseline_and_blocks_unready_profiles() -> None:
    registry = MechanismRegistry.load(ROOT)

    assert registry.resolve(FAMILY).profile_id == "phase1_deterministic_baseline"
    strongest = registry.get(FAMILY, "phase2_strongest_v1")
    assert strongest.lifecycle is MechanismLifecycle.VALIDATION
    assert strongest.activation_state == "blocked_pending_readiness"
    assert all(
        item.status is ReadinessStatus.UNAVAILABLE
        for item in strongest.readiness
    )
    with pytest.raises(
        PolicyRegistryError,
        match="has not entered validation",
    ):
        registry.resolve(
            FAMILY,
            purpose=ResolutionPurpose.VALIDATION,
            version=strongest.version,
            validation_manifest=_manifest(),
        )
    with pytest.raises(
        PolicyRegistryError,
        match="Unavailable mechanisms must use baseline",
    ):
        registry.resolve(
            FAMILY,
            purpose=ResolutionPurpose.DIAGNOSTIC,
            version="phase2_diagnostic_v1",
        )


def test_duplicate_family_version_is_rejected_for_same_or_different_digest() -> None:
    candidate = _record(
        version="candidate-v1",
        lifecycle="diagnostic",
        activation_state="read_only",
    )
    registry = _registry(candidate)

    with pytest.raises(PolicyRegistryError, match="only be registered once"):
        registry.register(candidate)
    changed = replace(
        candidate,
        source_digest="9" * 64,
    )
    with pytest.raises(PolicyRegistryError, match="only be registered once"):
        registry.register(changed)


def test_lifecycle_state_matrix_rejects_incompatible_registration() -> None:
    mapping = _record_mapping(
        version="invalid-diagnostic-state",
        lifecycle="diagnostic",
        activation_state="active",
    )

    with pytest.raises(
        PolicyRegistryError,
        match="lifecycle and activation state are incompatible",
    ):
        MechanismRegistration.from_mapping(mapping)

    registry = _registry()
    with pytest.raises(
        PolicyRegistryError,
        match="frozen baseline cannot enter validation",
    ):
        registry.enter_validation(
            FAMILY,
            "baseline-v1",
            manifest=_manifest(),
        )


def test_readiness_transition_matrix_enforces_validation_and_diagnostic_modes() -> None:
    input_ready = _record(
        version="input-ready",
        lifecycle="diagnostic",
        activation_state="read_only",
        stage="input_precheck",
    )
    implementation_ready = _record(
        version="implementation-ready",
        lifecycle="diagnostic",
        activation_state="read_only",
    )
    evidence_only = _record(
        version="evidence-only",
        lifecycle="diagnostic",
        activation_state="read_only",
        status="evidence_only",
        stage="implementation_validated",
    )
    unavailable = _record(
        version="unavailable",
        lifecycle="diagnostic",
        activation_state="read_only",
        status="unavailable",
        stage="input_precheck",
    )
    registry = _registry(
        input_ready,
        implementation_ready,
        evidence_only,
        unavailable,
    )

    with pytest.raises(
        PolicyRegistryError,
        match="implementation_validated",
    ):
        registry.enter_validation(
            FAMILY,
            input_ready.version,
            manifest=_manifest(),
        )
    with pytest.raises(
        PolicyRegistryError,
        match="input_precheck-only",
    ):
        registry.resolve(
            FAMILY,
            purpose=ResolutionPurpose.DIAGNOSTIC,
            version=input_ready.version,
        )
    transition = registry.enter_validation(
        FAMILY,
        implementation_ready.version,
        manifest=_manifest(),
    )
    assert transition.action == "enter_validation"
    assert (
        registry.resolve(
            FAMILY,
            purpose=ResolutionPurpose.VALIDATION,
            version=implementation_ready.version,
            validation_manifest=_manifest(),
        ).activation_state
        == "validation_ready"
    )
    assert (
        registry.resolve(
            FAMILY,
            purpose=ResolutionPurpose.DIAGNOSTIC,
            version=evidence_only.version,
        ).version
        == evidence_only.version
    )
    with pytest.raises(PolicyRegistryError, match="implementation_validated"):
        registry.enter_validation(
            FAMILY,
            evidence_only.version,
            manifest=_manifest(),
        )
    with pytest.raises(
        PolicyRegistryError,
        match="must use baseline",
    ):
        registry.resolve(
            FAMILY,
            purpose=ResolutionPurpose.DIAGNOSTIC,
            version=unavailable.version,
        )


def test_normal_resolver_cannot_select_validation_even_with_explicit_version() -> None:
    candidate = _record(
        version="candidate-v1",
        lifecycle="validation",
        activation_state="validation_ready",
    )
    registry = _registry(candidate)

    with pytest.raises(
        PolicyRegistryError,
        match="normal run cannot select a validation",
    ):
        registry.resolve(
            FAMILY,
            purpose=ResolutionPurpose.NORMAL,
            version=candidate.version,
        )


def test_default_lifecycle_cannot_bypass_activation_ready_enforcement() -> None:
    illegal_default = _record(
        version="illegal-default",
        lifecycle="default",
        activation_state="active",
        stage="implementation_validated",
        profile_id="phase2_strongest_v1",
    )
    baseline = _record(
        version="baseline-v1",
        lifecycle="baseline",
        activation_state="active",
        baseline=True,
        profile_id="phase1_deterministic_baseline",
    )

    with pytest.raises(
        PolicyRegistryError,
        match="activation_ready",
    ):
        MechanismRegistry(
            records=[baseline, illegal_default],
            active_versions={FAMILY: illegal_default.version},
            fallback_family=FAMILY,
            fallback_version=baseline.version,
        )
    registry = _registry()
    with pytest.raises(
        PolicyRegistryError,
        match="cannot be registered active",
    ):
        registry.register(illegal_default)

    wrong_profile_default = _record(
        version="wrong-profile-default",
        lifecycle="default",
        activation_state="active",
        stage="activation_ready",
        profile_id="not-the-strongest-profile",
    )
    with pytest.raises(
        PolicyRegistryError,
        match="only permitted default profile",
    ):
        MechanismRegistry(
            records=[baseline, wrong_profile_default],
            active_versions={FAMILY: wrong_profile_default.version},
            fallback_family=FAMILY,
            fallback_version=baseline.version,
        )


def test_activation_requires_readiness_all_hard_gates_audits_and_rollback() -> None:
    candidate = _record(
        version="strongest-v1",
        lifecycle="diagnostic",
        activation_state="read_only",
        stage="activation_ready",
        profile_id="phase2_strongest_v1",
    )
    registry = _registry(candidate)
    registry.enter_validation(
        FAMILY,
        candidate.version,
        manifest=_manifest(),
    )
    ready = registry.get(FAMILY, candidate.version)
    gate = StrongestProfileActivationGate()

    blocked = gate.evaluate(
        registry,
        family=FAMILY,
        version=ready.version,
        evidence=_activation_evidence(
            ready,
            gate_override={"safety": False},
        ),
    )
    assert not blocked.eligible
    assert "hard_gate:safety" in blocked.blockers
    with pytest.raises(
        PolicyRegistryError,
        match="Strongest activation failed",
    ):
        gate.activate(
            registry,
            family=FAMILY,
            version=ready.version,
            evidence=_activation_evidence(
                ready,
                gate_override={"safety": False},
            ),
        )

    decision, transition = gate.activate(
        registry,
        family=FAMILY,
        version=ready.version,
        evidence=_activation_evidence(ready),
    )
    assert decision.eligible
    assert transition.action == "activate_default"
    assert registry.resolve(FAMILY).version == ready.version
    assert registry.get(FAMILY, "baseline-v1").activation_state == "standby"


def test_new_runs_follow_activation_and_rollback_while_existing_pin_replays() -> None:
    candidate = _record(
        version="strongest-v1",
        lifecycle="diagnostic",
        activation_state="read_only",
        stage="activation_ready",
        profile_id="phase2_strongest_v1",
    )
    registry = _registry(candidate)
    baseline_pin = registry.pin("run-before", FAMILY)
    registry.enter_validation(
        FAMILY,
        candidate.version,
        manifest=_manifest(),
    )
    ready = registry.get(FAMILY, candidate.version)
    gate = StrongestProfileActivationGate()
    gate.activate(
        registry,
        family=FAMILY,
        version=ready.version,
        evidence=_activation_evidence(ready),
    )

    strongest_pin = registry.pin("run-after-activation", FAMILY)
    assert strongest_pin.version == ready.version
    assert registry.resolve_pinned(baseline_pin).profile_id == (
        "phase1_deterministic_baseline"
    )

    transition = registry.rollback(FAMILY)
    assert transition.action == "rollback"
    assert registry.pin("run-after-rollback", FAMILY).version == "baseline-v1"
    assert registry.get(FAMILY, "baseline-v1").activation_state == "active"
    assert registry.resolve_pinned(strongest_pin).version == ready.version

    registry.retire(FAMILY, ready.version)
    assert registry.resolve_pinned(strongest_pin).version == ready.version
    compatibility = registry.check_compatibility(
        strongest_pin,
        for_new_run=True,
    )
    assert not compatibility.compatible
    assert "retired versions cannot serve new runs" in compatibility.reasons


def test_rollback_can_target_only_a_previously_activated_version() -> None:
    first = _record(
        version="strongest-v1",
        lifecycle="diagnostic",
        activation_state="read_only",
        stage="activation_ready",
        profile_id="phase2_strongest_v1",
    )
    second = _record(
        version="strongest-v2",
        lifecycle="diagnostic",
        activation_state="read_only",
        stage="activation_ready",
        profile_id="phase2_strongest_v1",
    )
    never_activated = _record(
        version="strongest-v3",
        lifecycle="diagnostic",
        activation_state="read_only",
        stage="activation_ready",
        profile_id="phase2_strongest_v1",
    )
    registry = _registry(first, second, never_activated)
    gate = StrongestProfileActivationGate()

    registry.enter_validation(FAMILY, first.version, manifest=_manifest())
    ready_first = registry.get(FAMILY, first.version)
    gate.activate(
        registry,
        family=FAMILY,
        version=first.version,
        evidence=_activation_evidence(ready_first),
    )
    registry.enter_validation(FAMILY, second.version, manifest=_manifest())
    ready_second = registry.get(FAMILY, second.version)
    gate.activate(
        registry,
        family=FAMILY,
        version=second.version,
        evidence=_activation_evidence(ready_second),
    )
    second_pin = registry.pin("run-on-v2", FAMILY)

    with pytest.raises(
        PolicyRegistryError,
        match="previously activated",
    ):
        registry.rollback(FAMILY, target_version=never_activated.version)
    assert registry.resolve(FAMILY).version == second.version

    transition = registry.rollback(FAMILY, target_version=first.version)
    assert transition.after_active_version == first.version
    assert registry.pin("run-after-versioned-rollback", FAMILY).version == (
        first.version
    )
    assert registry.resolve_pinned(second_pin).version == second.version


def test_pin_detects_digest_drift_without_requiring_current_registry_revision() -> None:
    registry = _registry()
    pin = registry.pin("run-1", FAMILY)

    incompatible = registry.check_compatibility(
        replace(pin, config_digest="9" * 64),
        for_new_run=False,
    )
    assert not incompatible.compatible
    assert "config digest differs" in incompatible.reasons


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime_entry_points", ["policy/train.py"]),
        ("datasets", ["policy-training-dataset"]),
        ("checkpoints", ["checkpoint.bin"]),
        ("mutable_learned_parameters", ["weights"]),
        ("dependencies", ["torch==2.0"]),
    ],
)
def test_no_policy_training_rejects_registry_artifacts(
    field: str,
    value: list[str],
) -> None:
    mapping = _record_mapping(
        version=f"blocked-{field}",
        lifecycle="diagnostic",
        activation_state="read_only",
    )
    declaration = mapping["no_policy_training"]
    assert isinstance(declaration, dict)
    declaration[field] = value

    with pytest.raises(PolicyRegistryError, match="training|Training"):
        MechanismRegistration.from_mapping(mapping)


def test_no_policy_training_rejects_release_paths_and_dependencies() -> None:
    validator = NoPolicyTrainingValidator()

    with pytest.raises(PolicyRegistryError, match="Training paths"):
        validator.validate_release_inputs(
            ROOT,
            release_paths=["packages/policy/train_selector.py"],
            dependencies=[],
        )
    with pytest.raises(PolicyRegistryError, match="Training dependencies"):
        validator.validate_release_inputs(
            ROOT,
            release_paths=["packages/policy/runtime.py"],
            dependencies=["tensorflow>=2"],
        )


def test_registry_transition_emits_through_existing_event_contract() -> None:
    candidate = _record(
        version="candidate-v1",
        lifecycle="diagnostic",
        activation_state="read_only",
    )
    registry = _registry()
    transition = registry.register(candidate)
    event = transition.to_event(run_id="run-1", task_id="task-1")

    assert event.event_type is EventType.SYSTEM_NOTICE
    assert event.payload["event_name"] == "phase2.policy_registry.register"
    assert event.payload["transition_digest"] == transition.digest
