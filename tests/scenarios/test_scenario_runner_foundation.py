from __future__ import annotations

from copy import deepcopy
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
for package_path in (
    ROOT / "packages" / "core",
    ROOT / "packages" / "evaluation",
):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


from zyra_evaluation.scenario_runner import (  # noqa: E402
    EffectiveStepClassifier,
    EvidenceCollector,
    OwnerExecutionResult,
    ScenarioRegistry,
    ScenarioRunStore,
    SealedPolicyRuntime,
    SourceRoleAuditor,
)
from zyra_evaluation.scenario_runner.canonical import digest  # noqa: E402
from zyra_evaluation.scenario_runner.effective_steps import (  # noqa: E402
    require_effect_coverage,
)
from zyra_evaluation.scenario_runner.metrics import (  # noqa: E402
    ScenarioMetricCollector,
)
from zyra_evaluation.scenario_runner.models import (  # noqa: E402
    ScenarioRun,
    StepEffect,
)
from zyra_evaluation.scenario_runner.preflight import (  # noqa: E402
    CleanStateInspector,
)
from zyra_evaluation.scenario_runner.registry import (  # noqa: E402
    build_configuration,
)


def configuration(tmp_path: Path, *, input_text: str = "brand new input"):
    registry = ScenarioRegistry.defaults()
    paths = {
        "database": str(tmp_path / "owner.sqlite3"),
        "cache": str(tmp_path / "cache"),
        "index": str(tmp_path / "index"),
        "artifact": str(tmp_path / "artifacts"),
        "build": str(tmp_path / "build"),
    }
    value = build_configuration(
        registry,
        {
            "input": input_text,
            "seed": 7,
            "mode": "sealed",
        },
        project_root=str(ROOT),
        default_preflight_paths=paths,
    )
    return registry, value


def event(
    sequence: int,
    event_type: str,
    *,
    run_id: str = "run_owner",
    task_id: str = "task_owner",
    semantic_effect: str = "",
    causation_id: str = "",
    payload: dict | None = None,
) -> dict:
    event_id = f"event_{sequence}"
    return {
        "event_id": event_id,
        "event_type": event_type,
        "run_id": run_id,
        "task_id": task_id,
        "node_id": "root",
        "causation_id": causation_id,
        "created_at": f"2026-07-25T00:00:{sequence:02d}.000Z",
        "payload": {
            **(payload or {}),
            **({"semantic_effect": semantic_effect} if semantic_effect else {}),
        },
        "metadata": {
            "stage": semantic_effect or event_type.split("_", 1)[0],
            **({"semantic_effect": semantic_effect} if semantic_effect else {}),
        },
    }


def semantic_events() -> list[dict]:
    values = [
        event(1, "task_created", payload={"state": {"status": "created"}}),
        event(2, "backend_route", semantic_effect="route", causation_id="event_1"),
        event(
            3,
            "memory_curator_committed",
            semantic_effect="memory",
            causation_id="event_2",
        ),
        event(
            4,
            "permission_decision",
            semantic_effect="permission",
            causation_id="event_3",
        ),
        event(5, "failure_injected", causation_id="event_4"),
        event(6, "recovery_applied", semantic_effect="recovery", causation_id="event_5"),
        event(7, "artifact_written", causation_id="event_6"),
        event(8, "verification", semantic_effect="verification", causation_id="event_7"),
    ]
    return values


def test_registry_configuration_and_clean_preflight_are_digest_bound(tmp_path: Path) -> None:
    registry, selected = configuration(tmp_path)
    catalog = registry.catalog()

    assert catalog["registry_digest"]
    assert selected.definition_digest == catalog["definitions"][0]["definition_digest"]
    assert selected.expected_policy_digest == selected.policy.policy_digest
    assert {item.kind.value for item in selected.preflight_targets} == {
        "database",
        "cache",
        "index",
        "artifact",
        "build",
    }

    inspector = CleanStateInspector(input_seen=lambda _: False)
    receipt = inspector.require_formal_admission("scenario_clean", selected)

    assert receipt.clean is True
    assert receipt.new_input is True
    assert receipt.receipt_digest
    assert all(item.clean for item in receipt.checks)


def test_dirty_root_and_replayed_input_fail_closed(tmp_path: Path) -> None:
    _, selected = configuration(tmp_path)
    artifact = tmp_path / "artifacts"
    artifact.mkdir()
    (artifact / "stale.json").write_text("stale", encoding="utf-8")
    inspector = CleanStateInspector(input_seen=lambda _: False)

    with pytest.raises(Exception) as dirty:
        inspector.require_formal_admission("scenario_dirty", selected)
    assert getattr(dirty.value, "code", "") == "scenario_preflight_dirty"

    (artifact / "stale.json").unlink()
    inspector = CleanStateInspector(input_seen=lambda _: True)
    with pytest.raises(Exception) as replay:
        inspector.require_formal_admission("scenario_replay", selected)
    assert getattr(replay.value, "code", "") == "scenario_input_replayed"


def test_sealed_policy_converts_ask_and_unknown_to_deny_replan(tmp_path: Path) -> None:
    _, selected = configuration(tmp_path)
    runtime = SealedPolicyRuntime(selected.policy)

    assert runtime.verify_configuration(selected) == selected.policy.policy_digest
    allowed = runtime.evaluate(
        {"action_id": "allow", "action": "task.create", "effect": "allow"}
    )
    asked = runtime.evaluate(
        {"action_id": "ask", "action": "tool.unknown", "effect": "ask"}
    )
    unknown = runtime.evaluate(
        {"action_id": "unknown", "action": "network.elsewhere", "effect": "allow"}
    )

    assert allowed.final_effect == "allow"
    assert asked.final_effect == "deny"
    assert asked.recovery_action == "replan"
    assert unknown.final_effect == "deny"
    assert unknown.recovery_action == "replan"
    assert runtime.human_intervention_count == 0


def test_manual_attempt_is_recorded_without_incrementing_human_count(tmp_path: Path) -> None:
    _, selected = configuration(tmp_path)
    runtime = SealedPolicyRuntime(selected.policy)
    runtime.record_operator_attempt(
        actor_id="operator",
        action="operator.steer",
        reason="attempted manual mutation",
    )

    assert runtime.human_intervention_count == 0
    assert runtime.operator_intervention_attempt_count == 1
    with pytest.raises(Exception) as failure:
        runtime.assert_formal_invariants()
    assert getattr(failure.value, "code", "") == "scenario_sealed_invariant_failed"


def test_effective_step_classifier_rejects_inflation_and_requires_causation(
    tmp_path: Path,
) -> None:
    _, selected = configuration(tmp_path)
    events = semantic_events()
    events.insert(
        2,
        event(
            20,
            "worker_heartbeat",
            causation_id="event_2",
            payload={"no_op": True},
        ),
    )
    events.insert(
        3,
        event(
            21,
            "ui_repaint",
            causation_id="event_20",
            payload={"semantic_mutation": False},
        ),
    )
    classifier = EffectiveStepClassifier(profile=selected.profile)
    batch = classifier.classify_all(
        events,
        expected_run_id="run_owner",
        expected_task_id="task_owner",
    )

    assert len(batch.admitted) == 8
    assert len(batch.excluded) == 2
    assert {item.effect for item in batch.admitted} == {
        StepEffect.STATE_MUTATION,
        StepEffect.ROUTE,
        StepEffect.MEMORY,
        StepEffect.PERMISSION,
        StepEffect.FAULT,
        StepEffect.RECOVERY,
        StepEffect.ARTIFACT,
        StepEffect.VERIFICATION,
    }
    receipt = require_effect_coverage(
        batch,
        expected_effects=registry_effects(),
        minimum_steps=8,
        maximum_steps=100,
    )
    assert receipt["valid"] is True

    invalid_classifier = EffectiveStepClassifier(profile=selected.profile)
    invalid_batch = invalid_classifier.classify_all(
        [
            event(
                9,
                "artifact_written",
                run_id="run_owner",
                task_id="task_owner",
            )
        ],
        expected_run_id="run_owner",
        expected_task_id="task_owner",
    )
    assert invalid_batch.invalid[0].reason_code == "invalid.causation_missing"


def test_effective_step_disable_has_no_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, selected = configuration(tmp_path)
    monkeypatch.setenv("ZYRA_SCENARIO_EFFECTIVE_STEP_DISABLED", "1")
    classifier = EffectiveStepClassifier(profile=selected.profile)
    with pytest.raises(Exception) as failure:
        classifier.classify_all(semantic_events())
    assert getattr(failure.value, "code", "") == (
        "scenario_effective_step_classifier_disabled"
    )


def test_evidence_manifest_checksums_events_metrics_and_tamper(tmp_path: Path) -> None:
    _, selected = configuration(tmp_path)
    artifact_root = tmp_path / "artifacts"
    artifact_path = artifact_root / "run" / "task" / "artifact.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text('{"ok":true}', encoding="utf-8")
    import hashlib

    checksum = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    artifacts = (
        {
            "artifact_id": "artifact_owner",
            "kind": "report",
            "uri": str(artifact_path),
            "producer_node_id": "root",
            "metadata": {"sha256": checksum, "revision": "1"},
        },
    )
    events = semantic_events()
    classifier = EffectiveStepClassifier(profile=selected.profile)
    batch = classifier.classify_all(
        events,
        expected_run_id="run_owner",
        expected_task_id="task_owner",
    )
    coverage = require_effect_coverage(
        batch,
        expected_effects=registry_effects(),
        minimum_steps=8,
        maximum_steps=100,
    )
    metrics = ScenarioMetricCollector()
    samples = metrics.collect(
        batch,
        scenario_run_id="scenario_owner",
        owner_run_id="run_owner",
        task_id="task_owner",
        started_at="2026-07-25T00:00:00.000Z",
        completed_at="2026-07-25T00:00:10.000Z",
    )
    owner = OwnerExecutionResult(
        owner_run_id="run_owner",
        task_id="task_owner",
        task={"task_id": "task_owner", "run_id": "run_owner"},
        events=tuple(events),
        artifacts=artifacts,
        owner_receipts=(),
        started_at="2026-07-25T00:00:00.000Z",
        completed_at="2026-07-25T00:00:10.000Z",
    )
    source_audit = SourceRoleAuditor(project_root=ROOT).require_valid()
    policy = SealedPolicyRuntime(selected.policy)
    policy.verify_configuration(selected)
    policy.evaluate_all(
        ScenarioRegistry.defaults()
        .definition(selected.scenario_id, selected.definition_version)
        .planned_actions
    )
    collector = EvidenceCollector(artifact_root=artifact_root)
    manifest = collector.collect(
        scenario_run_id="scenario_owner",
        configuration=selected,
        owner=owner,
        step_batch=batch,
        policy_receipt=policy.assert_formal_invariants(),
        preflight_receipt={
            "scenario_run_id": "scenario_owner",
            "clean": True,
            "new_input": True,
            "input_digest": selected.input_digest,
        },
        metric_samples=samples,
        metric_summary=metrics.summarize(samples),
        coverage_receipt=coverage,
        source_audit=source_audit,
    )

    assert manifest["verification_receipt"]["valid"] is True
    assert manifest["claims"]["human_intervention_count"] == 0
    assert manifest["claims"]["m2_exit_complete"] is False

    tampered = {**manifest, "seed": 999}
    with pytest.raises(Exception) as failure:
        collector.verify(tampered)
    assert getattr(failure.value, "code", "") == (
        "scenario_evidence_verification_failed"
    )

    rebound = deepcopy(manifest)
    rebound.pop("verification_receipt", None)
    rebound["policy_receipt"]["policy_digest"] = "0" * 64
    unsigned = dict(rebound)
    unsigned.pop("manifest_digest", None)
    rebound["manifest_digest"] = digest(unsigned)
    with pytest.raises(Exception) as binding_failure:
        collector.verify(rebound)
    binding_codes = {
        item["code"]
        for item in binding_failure.value.fault.detail["failures"]
    }
    assert "policy_digest_binding_mismatch" in binding_codes


def test_store_persists_transitions_and_reconciles_restart(tmp_path: Path) -> None:
    _, selected = configuration(tmp_path)
    store = ScenarioRunStore(tmp_path / "scenario.sqlite3")
    run = ScenarioRun.create(selected)
    store.create(run)
    admitted = run.evolve(phase=type(run.phase).ADMITTED)
    store.save(
        admitted,
        expected_revision=run.revision,
        reason="test.admitted",
    )
    queued = admitted.evolve(phase=type(run.phase).QUEUED)
    store.save(
        queued,
        expected_revision=admitted.revision,
        reason="test.queued",
    )
    running = queued.evolve(phase=type(run.phase).RUNNING)
    store.save(
        running,
        expected_revision=queued.revision,
        reason="test.running",
    )

    reopened = ScenarioRunStore(tmp_path / "scenario.sqlite3")
    reconciled = reopened.reconcile_interrupted()

    assert reconciled[0].phase.value == "queued"
    assert reconciled[0].failure["code"] == "scenario_process_restarted"
    assert len(reopened.transitions(run.scenario_run_id)) == 5


def registry_effects() -> tuple[StepEffect, ...]:
    return (
        StepEffect.STATE_MUTATION,
        StepEffect.ROUTE,
        StepEffect.MEMORY,
        StepEffect.PERMISSION,
        StepEffect.FAULT,
        StepEffect.RECOVERY,
        StepEffect.ARTIFACT,
        StepEffect.VERIFICATION,
    )
