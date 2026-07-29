from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from zyra_evaluation.policy_benchmark import (
    CAUSAL_LINKS,
    FieldObservation,
    InputFieldContract,
    MechanismAuditSample,
    MechanismContract,
    MechanismReadinessConfig,
    evaluate_mechanism,
    run_no_training_audit,
)


ROOT = Path(__file__).resolve().parents[3]
NEGATIVE_EVIDENCE = frozenset(
    {
        "mechanism_disable",
        "baseline_fallback",
        "stale_or_corrupt_fail_closed",
        "projector_reject",
        "mechanism_exception",
    }
)


def _field(*, required: bool = True) -> InputFieldContract:
    return InputFieldContract(
        field_id="task_phase",
        canonical_owner="ClaudeRuntimeCore",
        owner_domain="task_session_attempt",
        decision_timing="strictly before proposal",
        evidence_key="event.task_created.phase",
        max_age_seconds=3600,
        minimum_confidence=1.0,
        missingness="fail_closed" if required else "exclude_optional",
        required=required,
    )


def _contract() -> MechanismContract:
    return MechanismContract(
        mechanism_id="arg_designer",
        display_name="ARG",
        decision_event_type="topology_mutation",
        semantic_effect="joint topology proposal",
        deterministic_decision_contract="fixed input produces fixed proposal",
        causal_receipt_contract=CAUSAL_LINKS,
        fallback=MappingProxyType(
            {
                "on_missing": "phase1_deterministic_baseline",
                "silent_fallback_allowed": False,
            }
        ),
        required_inputs=(_field(),),
        optional_inputs=(),
        required_scenarios=("normal",),
        required_failure_paths=(),
        allowed_follow_up_scope=("typed proposal",),
        instrumentation_requirements=("proposal receipt",),
    )


def _observation(
    *,
    sequence: int = 0,
    value_digest: str = "a" * 64,
    owner: str = "ClaudeRuntimeCore",
    corrupt: bool = False,
    observed_at: str = "2026-07-29T00:00:00Z",
) -> FieldObservation:
    return FieldObservation(
        field_id="task_phase",
        canonical_owner=owner,
        value_digest=value_digest,
        evidence_ref="archive.zip#event:task-created",
        observed_at=observed_at,
        observed_sequence=sequence,
        confidence=1.0,
        corrupt=corrupt,
    )


def _sample(
    *,
    run_id: str = "run-1",
    observations: tuple[FieldObservation, ...] = (),
    causal_links: frozenset[str] = frozenset(CAUSAL_LINKS),
    source_digest: str = "b" * 64,
) -> MechanismAuditSample:
    return MechanismAuditSample(
        independent_run_id=run_id,
        source_digest=source_digest,
        domain="software-delivery",
        decision_at="2026-07-29T00:00:10Z",
        decision_sequence=10,
        observations=MappingProxyType({"task_phase": observations}),
        scenarios=frozenset({"normal"}),
        failure_paths=frozenset(),
        causal_links=causal_links,
        baseline_causal_chain_complete=True,
        negative_evidence=NEGATIVE_EVIDENCE,
        raw_event_count=100,
        derived_run_count=7,
    )


def test_duplicate_cells_from_one_source_run_do_not_raise_readiness() -> None:
    sample = _sample(observations=())
    result = evaluate_mechanism(_contract(), [sample, sample])

    assert result["status"] == "unavailable"
    assert result["volume"]["independent_source_run_count"] == 1
    assert result["volume"]["discarded_duplicate_source_run_count"] == 1
    assert result["volume"]["derived_formal_run_count"] == 7
    assert result["field_coverage"][0]["coverage_ratio"] == 0.0


def test_decision_or_later_observation_is_future_information() -> None:
    sample = _sample(observations=(_observation(sequence=10),))
    result = evaluate_mechanism(_contract(), [sample])
    coverage = result["field_coverage"][0]

    assert result["status"] == "unavailable"
    assert coverage["available_count"] == 0
    assert coverage["future_information_count"] == 1
    assert coverage["missing_count"] == 1


def test_broken_causal_chain_downgrades_complete_inputs_to_evidence_only() -> None:
    links = frozenset(set(CAUSAL_LINKS) - {"verification_outcome"})
    sample = _sample(
        observations=(_observation(),),
        causal_links=links,
    )
    result = evaluate_mechanism(_contract(), [sample])

    assert result["required_input_gate_passed"] is True
    assert result["causal_links"]["completeness_ratio"] == 0.0
    assert result["status"] == "evidence_only"
    assert result["diagnostic_boundary"] == {
        "evidence_only_is_read_only": True,
        "may_change_graph_route_lease_or_side_effect": False,
    }


def test_unknown_owner_and_corrupt_values_fail_required_input_gate() -> None:
    wrong_owner = _sample(
        run_id="wrong-owner",
        observations=(_observation(owner="AnotherOwner"),),
    )
    corrupt = _sample(
        run_id="corrupt",
        observations=(_observation(corrupt=True),),
    )
    result = evaluate_mechanism(_contract(), [wrong_owner, corrupt])
    coverage = result["field_coverage"][0]

    assert result["status"] == "unavailable"
    assert coverage["owner_mismatch_count"] == 1
    assert coverage["corrupt_count"] == 1
    assert coverage["coverage_ratio"] == 0.0


def test_stale_required_observation_cannot_enter_ready_status() -> None:
    sample = _sample(
        observations=(
            _observation(observed_at="2026-07-28T20:00:00Z"),
        )
    )
    result = evaluate_mechanism(_contract(), [sample])

    assert result["status"] == "unavailable"
    assert result["field_coverage"][0]["stale_count"] == 1
    assert result["required_input_gate_passed"] is False


def test_conflicting_snapshot_values_fail_determinism_gate() -> None:
    sample = _sample(
        observations=(
            _observation(sequence=1, value_digest="a" * 64),
            _observation(sequence=2, value_digest="c" * 64),
        )
    )
    result = evaluate_mechanism(_contract(), [sample])

    assert result["field_coverage"][0]["conflicting_value_count"] == 1
    assert result["deterministic_input_snapshot_replay"]["match"] is False
    assert result["status"] == "unavailable"


def test_no_training_audit_rejects_injected_training_entry(
    tmp_path: Path,
) -> None:
    configured = MechanismReadinessConfig.load(ROOT)
    training_root = tmp_path / "mechanism"
    training_root.mkdir()
    (training_root / "trainer.py").write_text(
        "def audit_only():\n    return None\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='0'\ndependencies=[]\n",
        encoding="utf-8",
    )
    policy = dict(configured.no_training_audit)
    policy["scan_roots"] = ["mechanism"]
    injected = replace(
        configured,
        no_training_audit=MappingProxyType(policy),
    )

    result = run_no_training_audit(tmp_path, injected)

    assert result["passed"] is False
    assert result["training_sample_count"] == 0
    assert result["findings"] == [
        {
            "code": "training-path-detected",
            "path": "mechanism/trainer.py",
            "detail": "matched forbidden pattern */trainer.py",
        }
    ]


def test_no_training_audit_rejects_injected_training_symbol(
    tmp_path: Path,
) -> None:
    configured = MechanismReadinessConfig.load(ROOT)
    training_root = tmp_path / "mechanism"
    training_root.mkdir()
    (training_root / "policy_runtime.py").write_text(
        "def train_policy():\n    return None\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='0'\ndependencies=[]\n",
        encoding="utf-8",
    )
    policy = dict(configured.no_training_audit)
    policy["scan_roots"] = ["mechanism"]
    injected = replace(
        configured,
        no_training_audit=MappingProxyType(policy),
    )

    result = run_no_training_audit(tmp_path, injected)

    assert result["passed"] is False
    assert result["findings"] == [
        {
            "code": "training-symbol-detected",
            "path": "mechanism/policy_runtime.py",
            "detail": "train_policy",
        }
    ]


def test_no_training_audit_accepts_current_mechanism_boundary() -> None:
    configured = MechanismReadinessConfig.load(ROOT)
    result = run_no_training_audit(ROOT, configured)

    assert result["passed"] is True
    assert result["training_dataset_count"] == 0
    assert result["training_checkpoint_count"] == 0
    assert result["mutable_learned_parameter_count"] == 0
    assert result["findings"] == []
