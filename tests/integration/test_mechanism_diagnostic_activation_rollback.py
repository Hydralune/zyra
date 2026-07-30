from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from zyra_core import ArtifactKind, append_event, create_task_state, read_events
from zyra_evaluation.policy_benchmark import attribute_runtime_receipt
from zyra_orchestration import ensure_default_graph
from zyra_orchestration.graph_custody import (
    GraphStateCustody,
    GraphStateStore,
)
from zyra_orchestration.topology_policy import (
    ActivationEvidence,
    DiagnosticMutationError,
    MechanismRegistration,
    MechanismRegistry,
    PolicyActivationRuntime,
    ResolutionPurpose,
    StrongestProfileActivationGate,
    TopologyPolicyRuntime,
    ValidationManifest,
    canonical_digest,
)
from zyra_runtime import LocalArtifactStore
from zyra_scheduler import ResourceScheduler


ROOT = Path(__file__).resolve().parents[2]
FAMILY = "topology_policy"


def _record(
    *,
    version: str,
    lifecycle: str,
    activation_state: str,
    stage: str = "implementation_validated",
    status: str = "deterministic_ready",
    profile_id: str | None = None,
    baseline: bool = False,
    timeout_seconds: float = 1.0,
) -> MechanismRegistration:
    configuration = {
        "profile_id": profile_id or version,
        "version": version,
        "training_allowed": False,
    }
    mechanism_ids = (
        "loopx",
        "arg_designer",
        "card",
        "agentprune",
        "maas",
    )
    readiness = (
        []
        if baseline
        else [
            {
                "mechanism_id": mechanism_id,
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
            for mechanism_id in mechanism_ids
        ]
    )
    return MechanismRegistration.from_mapping(
        {
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
            "required_mechanisms": [] if baseline else list(mechanism_ids),
            "readiness": readiness,
            "rollback_family": FAMILY,
            "rollback_version": "baseline-v1",
            "timeout_seconds": timeout_seconds,
            "no_policy_training": {
                "runtime_entry_points": [],
                "datasets": [],
                "checkpoints": [],
                "mutable_learned_parameters": [],
                "dependencies": [],
            },
            "hard_gate_ids": [] if baseline else ["success", "safety"],
            "audit_gate_ids": (
                []
                if baseline
                else ["owner", "path", "dependency", "rollback"]
            ),
        }
    )


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
        purpose="scenario",
    )


def _activation_evidence(
    record: MechanismRegistration,
) -> ActivationEvidence:
    return ActivationEvidence.from_mapping(
        {
            "evidence_id": "activation-evidence",
            "family": record.family,
            "version": record.version,
            "schema_digest": record.schema_digest,
            "config_digest": record.config_digest,
            "source_digest": record.source_digest,
            "implementation_commit": record.implementation_commit,
            "evidence_commit": record.evidence_commit,
            "readiness_digest": record.readiness_digest,
            "hard_gates": {gate: True for gate in record.hard_gate_ids},
            "audits": {gate: True for gate in record.audit_gate_ids},
            "rollback_family": record.rollback_family,
            "rollback_version": record.rollback_version,
            "no_policy_training_passed": True,
        }
    )


def _owners() -> dict[str, object]:
    return {
        "graph_revision": 7,
        "worker_route": "",
        "lease_count": 0,
        "side_effect_count": 0,
    }


def _baseline_executor(
    owner_state: dict[str, object],
    calls: list[str],
):
    def execute(snapshot: object) -> dict[str, object]:
        calls.append("baseline_execute")
        owner_state["worker_route"] = "CodeWorkerRuntime"
        owner_state["lease_count"] = int(owner_state["lease_count"]) + 1
        owner_state["side_effect_count"] = (
            int(owner_state["side_effect_count"]) + 1
        )
        return {
            "selected_worker": "CodeWorkerRuntime",
            "snapshot_digest": canonical_digest(snapshot),
            "executed": True,
        }

    return execute


def _baseline_reference(calls: list[str]):
    def reference(snapshot: object) -> dict[str, object]:
        calls.append("baseline_reference")
        return {
            "selected_worker": "CodeWorkerRuntime",
            "snapshot_digest": canonical_digest(snapshot),
            "executed": False,
        }

    return reference


def test_diagnostic_uses_same_snapshot_without_graph_route_lease_or_side_effect_diff() -> None:
    diagnostic = _record(
        version="diagnostic-v1",
        lifecycle="diagnostic",
        activation_state="read_only",
        status="evidence_only",
    )
    registry = _registry(diagnostic)
    diagnostic_owners = _owners()
    baseline_owners = _owners()
    diagnostic_calls: list[str] = []
    baseline_calls: list[str] = []
    snapshot = {"task": "same-input", "graph_revision": 7}

    runtime = TopologyPolicyRuntime(
        registry,
        baseline_executor=_baseline_executor(
            diagnostic_owners,
            diagnostic_calls,
        ),
        baseline_reference=_baseline_reference(diagnostic_calls),
        owner_probe=lambda: dict(diagnostic_owners),
    )
    diagnostic_result = runtime.execute(
        run_id="run-diagnostic",
        task_id="task-1",
        input_snapshot=snapshot,
        purpose=ResolutionPurpose.DIAGNOSTIC,
        version=diagnostic.version,
        mechanism_executor=lambda invocation: {
            "proposal": "read-only",
            "input_snapshot_digest": canonical_digest(
                invocation.input_snapshot
            ),
            "execution_allowed": invocation.execution_allowed,
        },
    )

    baseline_runtime = TopologyPolicyRuntime(
        registry,
        baseline_executor=_baseline_executor(
            baseline_owners,
            baseline_calls,
        ),
        owner_probe=lambda: dict(baseline_owners),
    )
    baseline_result = baseline_runtime.execute(
        run_id="run-baseline",
        task_id="task-1",
        input_snapshot=snapshot,
    )

    assert diagnostic_owners == baseline_owners
    assert diagnostic_calls == ["baseline_execute"]
    assert baseline_calls == ["baseline_execute"]
    assert (
        diagnostic_result.execution_receipt.actual_decision_digest
        == baseline_result.execution_receipt.actual_decision_digest
    )
    assert diagnostic_result.diagnostic_receipt is not None
    assert diagnostic_result.diagnostic_receipt.executed is False
    assert diagnostic_result.diagnostic_receipt.actual_outcome_recorded is False
    assert diagnostic_result.diagnostic_receipt.no_effect_verified is True
    assert (
        diagnostic_result.diagnostic_receipt.input_snapshot_digest
        == baseline_result.execution_receipt.input_snapshot_digest
    )
    attribution = attribute_runtime_receipt(
        diagnostic_result.execution_receipt.to_dict(),
        diagnostic_receipt=diagnostic_result.diagnostic_receipt.to_dict(),
    )
    assert attribution.attribution_class == (
        "baseline_with_read_only_diagnostic"
    )
    assert not attribution.strongest_success_eligible


def test_diagnostic_no_effect_guard_is_disconnect_to_fail_mutation_evidence() -> None:
    diagnostic = _record(
        version="diagnostic-v1",
        lifecycle="diagnostic",
        activation_state="read_only",
        status="evidence_only",
    )
    registry = _registry(diagnostic)
    owner_state = _owners()
    events = []
    runtime = TopologyPolicyRuntime(
        registry,
        baseline_executor=_baseline_executor(owner_state, []),
        owner_probe=lambda: dict(owner_state),
        admit_event=events.append,
    )

    def illegal_diagnostic(invocation: object) -> dict[str, object]:
        owner_state["graph_revision"] = 8
        return {"proposal": "illegal-mutation"}

    with pytest.raises(
        DiagnosticMutationError,
        match="changed canonical owner state",
    ):
        runtime.execute(
            run_id="run-mutation",
            task_id="task-1",
            input_snapshot={"graph_revision": 7},
            purpose=ResolutionPurpose.DIAGNOSTIC,
            version=diagnostic.version,
            mechanism_executor=illegal_diagnostic,
        )
    assert events[-1].payload["event_name"] == "phase2.mechanism_degraded"
    assert events[-1].payload["failure_class"] == (
        "diagnostic_no_effect_guard"
    )
    assert events[-1].payload["owner_diff"]["graph_revision"] == {
        "before": 7,
        "after": 8,
    }


def test_diagnostic_mutation_cannot_hide_behind_executor_exception() -> None:
    diagnostic = _record(
        version="diagnostic-exception-mutation",
        lifecycle="diagnostic",
        activation_state="read_only",
        status="evidence_only",
    )
    registry = _registry(diagnostic)
    owner_state = _owners()
    events = []
    runtime = TopologyPolicyRuntime(
        registry,
        baseline_executor=_baseline_executor(owner_state, []),
        owner_probe=lambda: dict(owner_state),
        admit_event=events.append,
    )

    def mutating_failure(invocation: object) -> dict[str, object]:
        owner_state["lease_count"] += 1
        raise RuntimeError("failure after forbidden mutation")

    with pytest.raises(
        DiagnosticMutationError,
        match="changed canonical owner state",
    ):
        runtime.execute(
            run_id="run-diagnostic-mutating-failure",
            task_id="task-1",
            input_snapshot={"graph_revision": 7},
            purpose=ResolutionPurpose.DIAGNOSTIC,
            version=diagnostic.version,
            mechanism_executor=mutating_failure,
        )

    assert events[-1].payload["failure_class"] == (
        "diagnostic_no_effect_guard"
    )
    assert events[-1].payload["owner_diff"]["lease_count"] == {
        "before": 1,
        "after": 2,
    }


def test_diagnostic_guard_observes_real_graph_scheduler_and_artifact_owners(
    tmp_path: Path,
) -> None:
    diagnostic = _record(
        version="diagnostic-real-owner",
        lifecycle="diagnostic",
        activation_state="read_only",
        status="evidence_only",
    )
    registry = _registry(diagnostic)
    graph_store = GraphStateStore(tmp_path / "graph.sqlite3")
    graph_store.initialize()
    custody = GraphStateCustody(graph_store)
    custody.create(graph_id_value="graph-real", run_id="run-real")
    task = create_task_state("Patch Python code and run tests.")
    ensure_default_graph(task)
    execute_node = next(
        node
        for node in task.plan_nodes.values()
        if node.metadata.get("stage") == "execute"
    )
    scheduler = ResourceScheduler()
    artifacts = LocalArtifactStore(tmp_path / "artifacts")

    def baseline(snapshot: object) -> dict[str, object]:
        decision = scheduler.decide(task, node=execute_node)
        scheduler.attach_decision_to_state(
            task,
            decision,
            node=execute_node,
        )
        artifact = artifacts.write_text(
            run_id=task.run_id,
            task_id=task.task_id,
            content="baseline side effect",
            title="baseline-output",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".txt",
        )
        task.artifacts.append(artifact)
        return {
            "selected_worker": decision.selected_worker,
            "selected_manifest_id": decision.selected_manifest_id,
            "artifact_id": artifact.artifact_id,
        }

    def owner_probe() -> dict[str, object]:
        current = custody.current("graph-real")
        resource = task.metadata.get("last_resource_decision")
        selected = (
            str(resource.get("selected_worker") or "")
            if isinstance(resource, dict)
            else ""
        )
        return {
            "graph_revision": current.revision,
            "worker_route": selected,
            "lease_count": 0,
            "side_effect_count": len(task.artifacts),
        }

    runtime = TopologyPolicyRuntime(
        registry,
        baseline_executor=baseline,
        owner_probe=owner_probe,
    )
    result = runtime.execute(
        run_id=task.run_id,
        task_id=task.task_id,
        input_snapshot={
            "graph": custody.current("graph-real").to_dict(),
            "task_id": task.task_id,
        },
        purpose=ResolutionPurpose.DIAGNOSTIC,
        version=diagnostic.version,
        mechanism_executor=lambda invocation: {
            "proposal": "read-only-real-owner-check",
            "execution_allowed": invocation.execution_allowed,
        },
    )

    assert result.diagnostic_receipt is not None
    assert result.diagnostic_receipt.no_effect_verified
    assert custody.current("graph-real").revision == 0
    assert task.metadata["last_resource_decision"]["selected_worker"]
    assert len(task.artifacts) == 1


def test_validation_is_explicit_and_normal_runtime_falls_back_once() -> None:
    candidate = _record(
        version="candidate-v1",
        lifecycle="diagnostic",
        activation_state="read_only",
    )
    registry = _registry(candidate)
    registry.enter_validation(
        FAMILY,
        candidate.version,
        manifest=_manifest(),
    )
    owner_state = _owners()
    calls: list[str] = []
    events = []
    runtime = TopologyPolicyRuntime(
        registry,
        baseline_executor=_baseline_executor(owner_state, calls),
        baseline_reference=_baseline_reference(calls),
        owner_probe=lambda: dict(owner_state),
        admit_event=events.append,
    )

    normal = runtime.execute(
        run_id="run-normal",
        task_id="task-1",
        input_snapshot={"task": "normal"},
        purpose=ResolutionPurpose.NORMAL,
        version=candidate.version,
        mechanism_executor=lambda invocation: {"should_not_execute": True},
    )
    assert calls == ["baseline_execute"]
    assert normal.execution_receipt.degraded is True
    assert normal.execution_receipt.outcome_attribution == "degraded_baseline"
    assert events[-1].payload["failure_class"] == (
        "registry_resolution:validation-normal-selection-forbidden"
    )

    calls.clear()
    validation = runtime.execute(
        run_id="run-validation",
        task_id="task-1",
        input_snapshot={"task": "isolated-validation"},
        purpose=ResolutionPurpose.VALIDATION,
        version=candidate.version,
        validation_manifest=_manifest(),
        mechanism_executor=lambda invocation: {
            "mechanism": candidate.version,
            "execution_allowed": invocation.execution_allowed,
        },
    )
    assert calls == ["baseline_reference"]
    assert validation.execution_receipt.outcome_attribution == (
        "isolated_validation"
    )
    assert validation.execution_receipt.fallback_executed is False
    assert validation.execution_receipt.strongest_success_eligible is False


@pytest.mark.parametrize(
    "failure_kind",
    ["exception", "timeout"],
)
def test_mechanism_failure_writes_explicit_degraded_event_and_executes_baseline_once(
    failure_kind: str,
) -> None:
    candidate = _record(
        version=f"candidate-{failure_kind}",
        lifecycle="diagnostic",
        activation_state="read_only",
        timeout_seconds=0.001 if failure_kind == "timeout" else 1.0,
    )
    registry = _registry(candidate)
    registry.enter_validation(
        FAMILY,
        candidate.version,
        manifest=_manifest(),
    )
    owner_state = _owners()
    calls: list[str] = []
    events = []
    runtime = TopologyPolicyRuntime(
        registry,
        baseline_executor=_baseline_executor(owner_state, calls),
        baseline_reference=_baseline_reference(calls),
        owner_probe=lambda: dict(owner_state),
        admit_event=events.append,
    )

    def fail(invocation: object) -> dict[str, object]:
        if failure_kind == "timeout":
            time.sleep(0.01)
            return {"late": True}
        raise RuntimeError("mechanism exploded")

    result = runtime.execute(
        run_id=f"run-{failure_kind}",
        task_id="task-1",
        input_snapshot={"task": "fallback"},
        purpose=ResolutionPurpose.VALIDATION,
        version=candidate.version,
        validation_manifest=_manifest(),
        mechanism_executor=fail,
    )

    assert calls == ["baseline_reference", "baseline_execute"]
    assert owner_state["side_effect_count"] == 1
    assert result.execution_receipt.degraded
    assert result.execution_receipt.fallback_executed
    assert not result.execution_receipt.strongest_success_eligible
    assert events[-1].payload["event_name"] == "phase2.mechanism_degraded"
    assert events[-1].payload["silent_fallback"] is False
    attribution = attribute_runtime_receipt(
        result.execution_receipt.to_dict()
    )
    assert attribution.attribution_class == "degraded_baseline"
    assert not attribution.strongest_success_eligible


def test_activation_and_rollback_only_change_new_runs_and_keep_receipts_replayable(
    tmp_path: Path,
) -> None:
    strongest = _record(
        version="strongest-v1",
        lifecycle="diagnostic",
        activation_state="read_only",
        stage="activation_ready",
        profile_id="phase2_strongest_v1",
    )
    registry = _registry(strongest)
    old_pin = registry.pin("run-old", FAMILY)
    registry.enter_validation(
        FAMILY,
        strongest.version,
        manifest=_manifest(),
    )
    ready = registry.get(FAMILY, strongest.version)
    gate = StrongestProfileActivationGate()
    event_log = tmp_path / "policy-events.jsonl"
    lifecycle = PolicyActivationRuntime(
        registry,
        admit_event=lambda event: append_event(event, event_log),
        gate=gate,
    )
    _, activation = lifecycle.activate_default(
        run_id="activation-control",
        task_id="task-control",
        family=FAMILY,
        version=ready.version,
        evidence=_activation_evidence(ready),
    )
    assert activation.action == "activate_default"
    assert read_events(event_log)[-1]["payload"]["event_name"] == (
        "phase2.policy_registry.activate_default"
    )

    calls: list[str] = []
    owner_state = _owners()
    runtime = TopologyPolicyRuntime(
        registry,
        baseline_executor=_baseline_executor(owner_state, calls),
        baseline_reference=_baseline_reference(calls),
        owner_probe=lambda: dict(owner_state),
    )
    old_run = runtime.execute(
        run_id="run-old",
        task_id="task-1",
        input_snapshot={"task": "old"},
        existing_pin=old_pin,
        mechanism_executor=lambda invocation: {"unexpected": True},
    )
    assert old_run.execution_receipt.outcome_attribution == "baseline"

    new_run = runtime.execute(
        run_id="run-new",
        task_id="task-1",
        input_snapshot={"task": "new"},
        mechanism_executor=lambda invocation: {
            "profile": invocation.pin.profile_id,
            "version": invocation.pin.version,
        },
    )
    assert new_run.pin.version == strongest.version
    assert new_run.execution_receipt.strongest_success_eligible

    rollback = lifecycle.rollback(
        run_id="rollback-control",
        task_id="task-control",
        family=FAMILY,
    )
    assert rollback.action == "rollback"
    assert read_events(event_log)[-1]["payload"]["event_name"] == (
        "phase2.policy_registry.rollback"
    )
    after_rollback = runtime.execute(
        run_id="run-after-rollback",
        task_id="task-1",
        input_snapshot={"task": "after"},
        mechanism_executor=lambda invocation: {"unexpected": True},
    )
    assert after_rollback.pin.version == "baseline-v1"

    continued = runtime.execute(
        run_id="run-new",
        task_id="task-1",
        input_snapshot={"task": "continued"},
        existing_pin=new_run.pin,
        mechanism_executor=lambda invocation: {
            "profile": invocation.pin.profile_id,
            "continued": True,
        },
    )
    assert continued.pin.version == strongest.version
    assert continued.execution_receipt.strongest_success_eligible

    registry.retire(FAMILY, strongest.version)
    assert runtime.replay_receipt(new_run.execution_receipt) == (
        new_run.execution_receipt
    )


@pytest.mark.parametrize(
    ("mutation", "expected_failure"),
    [
        ("outer_digest", "registry-digest-mismatch"),
        ("record_config", "config-digest-mismatch"),
        ("schema", "registry-schema-incompatible"),
        ("report", "readiness-report-missing"),
    ],
)
def test_corrupt_config_schema_or_report_degrades_explicitly_to_frozen_baseline(
    tmp_path: Path,
    mutation: str,
    expected_failure: str,
) -> None:
    config = json.loads(
        (ROOT / "config/phase2/policies.yaml").read_text(encoding="utf-8")
    )
    if mutation == "outer_digest":
        config["registry_revision"] = 999
    elif mutation == "record_config":
        config["records"][0]["configuration"]["tampered"] = True
        config["registry_digest"] = _registry_config_digest(config)
    elif mutation == "schema":
        config["schema"] = "zyra.phase2-policy-registry/v999"
        config["registry_digest"] = _registry_config_digest(config)
    elif mutation == "report":
        config["records"][1]["readiness"][1]["report_ref"] = (
            "docs/reviews/phase2/missing-report.json"
        )
        config["records"][1]["readiness"][1]["report_digest"] = "9" * 64
        config["registry_digest"] = _registry_config_digest(config)
    config_path = tmp_path / "policies.yaml"
    config_path.write_text(
        json.dumps(config, sort_keys=True),
        encoding="utf-8",
    )

    calls: list[str] = []
    owner_state = _owners()
    events = []
    runtime = TopologyPolicyRuntime.from_repository(
        ROOT,
        config_path=config_path,
        baseline_executor=_baseline_executor(owner_state, calls),
        owner_probe=lambda: dict(owner_state),
        admit_event=events.append,
    )
    result = runtime.execute(
        run_id=f"run-{mutation}",
        task_id="task-1",
        input_snapshot={"task": "degraded"},
    )

    assert calls == ["baseline_execute"]
    assert result.execution_receipt.degraded
    assert result.execution_receipt.degraded_reason == (
        f"registry_disconnected:{expected_failure}"
    )
    assert events[-1].payload["failure_class"] == (
        f"registry_disconnected:{expected_failure}"
    )
    assert events[-1].payload["strongest_success_eligible"] is False


def _registry_config_digest(config: dict[str, object]) -> str:
    selected = dict(config)
    selected.pop("registry_digest", None)
    return canonical_digest(selected)
