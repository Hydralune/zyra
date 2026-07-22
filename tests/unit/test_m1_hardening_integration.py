from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

from zyra_evaluation.m1_hardening.benchmark import BenchmarkAnalyzer
from zyra_evaluation.m1_hardening.cleanroom import (
    CleanroomBoundaryScanner,
    default_cleanroom_commands,
)
from zyra_evaluation.m1_hardening.contracts import GateStatus
from zyra_evaluation.m1_hardening.evidence_admission import (
    AdmissionPolicy,
    AdmissionStatus,
    EvidenceAdmissionController,
    EvidenceEnvelopeFactory,
    EvidenceKind,
    EvidenceOrigin,
)
from zyra_evaluation.m1_hardening.exit_gate import ExitBundleBuilder, ExitDecision, ExitPolicy
from zyra_evaluation.m1_hardening.handoff import HandoffBuilder, HandoffStore
from zyra_evaluation.m1_hardening.integration_scenarios import (
    default_integration_scenarios,
    scenario_catalog_gate,
)
from zyra_evaluation.m1_hardening.live_evidence import EndpointEvidenceGate
from zyra_evaluation.m1_hardening.owner_matrix import OwnerMatrix
from zyra_evaluation.m1_hardening.owner_probes import (
    EnvironmentOwnerDisconnectProbe,
    OwnerDisableContract,
    OwnerProbeCatalog,
)


ROOT = Path(__file__).resolve().parents[2]
BASELINE = "8" * 40
TARGET = "9" * 40


def test_six_scenario_catalog_and_owner_matrix_resolve_real_product_owners() -> None:
    definitions = default_integration_scenarios()
    catalog_gate = scenario_catalog_gate()
    probes = OwnerProbeCatalog()
    owner_gate = OwnerMatrix(ROOT).evaluate(
        registered_probe_ids=probes.probe_ids(),
        scenario_ids=[item.scenario_id for item in definitions],
        final_completion=False,
    )

    assert catalog_gate.status is GateStatus.PASSED, catalog_gate.to_dict()
    assert len(definitions) == 6
    assert len({item.kind for item in definitions}) == 6
    assert len(probes.probe_ids()) == 17
    assert probes.validate() == ()
    assert owner_gate.status is GateStatus.PASSED, owner_gate.to_dict()
    assert owner_gate.metrics["required_capability_count"] == 17


def test_environment_disconnect_probe_disables_real_process_flag_and_restores() -> None:
    flag = "ZYRA_TEST_M1_OWNER_DISABLED"
    os.environ.pop(flag, None)
    contract = OwnerDisableContract(
        probe_id="disable-code-index",
        capability="code-index",
        environment_flags=(flag,),
        expected_error_codes=("test_owner_disabled",),
    )

    def exercise() -> dict[str, object]:
        disabled = os.environ.get(flag) == "1"
        return {
            "ok": not disabled,
            "error": "test_owner_disabled" if disabled else "",
            "canonical_owner": "code-index",
            "fallback": False,
        }

    probe = EnvironmentOwnerDisconnectProbe(contract, exercise)
    captured = probe.capture()
    baseline = probe.exercise()
    disabled = probe.disable()
    failure = probe.exercise()
    restored = probe.restore(captured)

    assert baseline["ok"] is True
    assert disabled["ok"] is True
    assert failure == {
        "ok": False,
        "error": "test_owner_disabled",
        "canonical_owner": "code-index",
        "fallback": False,
    }
    assert restored["ok"] is True
    assert flag not in os.environ


def test_cleanroom_scanner_rejects_sibling_runtime_paths_without_self_matching(tmp_path: Path) -> None:
    package = tmp_path / "packages" / "runtime"
    package.mkdir(parents=True)
    clean = package / "runtime.py"
    clean.write_text("import subprocess\nsubprocess.run(['python', '-V'])\n", encoding="utf-8")

    residuals, links, references, files, _ = CleanroomBoundaryScanner(tmp_path).scan()
    assert residuals == []
    assert links == []
    assert references == []
    assert files == 1

    forbidden = package / "forbidden.py"
    forbidden.write_text(
        "import subprocess\nsubprocess.run(['../claude-code-best/bin/runtime'])\n",
        encoding="utf-8",
    )
    _, _, references, _, _ = CleanroomBoundaryScanner(tmp_path).scan()
    assert any(item["kind"] == "python-external-process-or-path" for item in references)


def test_cleanroom_scanner_excludes_committed_history_but_uses_controlled_python(tmp_path: Path) -> None:
    evidence = tmp_path / "docs" / "reviews" / "evidence" / "historical.log"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("historical command log", encoding="utf-8")
    vendor = tmp_path / "vendor" / "source" / "example.py"
    vendor.parent.mkdir(parents=True)
    vendor.write_text("subprocess.run(['../claude-code-best/bin/runtime'])", encoding="utf-8")

    residuals, _, references, _, _ = CleanroomBoundaryScanner(tmp_path).scan()
    assert residuals == []
    assert references == []
    commands = default_cleanroom_commands()
    assert all(Path(command.argv[0]).is_absolute() for command in commands)
    assert len({command.argv[0] for command in commands}) == 1


def test_evidence_admission_accepts_digest_bound_runtime_evidence_and_rejects_tampering() -> None:
    policy = AdmissionPolicy(
        required_kinds=(EvidenceKind.SCENARIO,),
        require_chain_for_kinds=(),
    )
    factory = EvidenceEnvelopeFactory()
    envelope = factory.create(
        evidence_id="scenario-live-1",
        kind=EvidenceKind.SCENARIO,
        origin=EvidenceOrigin.LIVE_RUNTIME,
        producer="integration-test",
        content={"ok": True, "artifact_id": "artifact-1"},
        baseline_commit=BASELINE,
        target_commit=TARGET,
        run_id="run-1",
        task_id="task-1",
    )
    receipts, gate = EvidenceAdmissionController(policy).admit(
        [envelope], expected_target_commit=TARGET, final_completion=True
    )
    assert gate.status is GateStatus.PASSED, gate.to_dict()
    assert receipts[0].status is AdmissionStatus.ADMITTED

    tampered = replace(envelope, content={"ok": False, "artifact_id": "artifact-1"})
    receipts, gate = EvidenceAdmissionController(policy).admit(
        [tampered], expected_target_commit=TARGET, final_completion=True
    )
    assert receipts[0].status is AdmissionStatus.REJECTED
    assert gate.status is GateStatus.BLOCKED
    assert "content_digest_mismatch" in receipts[0].reasons


def test_live_tier_gate_never_accepts_loopback_as_edge_or_cloud() -> None:
    observations = [
        {
            "tier": tier,
            "endpoint": "http://127.0.0.1:9000/task",
            "endpoint_id": f"{tier}-endpoint",
            "runtime_id": f"{tier}-runtime",
            "process_id": f"{tier}-process",
            "host_id": f"{tier}-host",
            "isolation_id": f"{tier}-isolation",
            "request_id": f"{tier}-request",
            "route_id": f"{tier}-route",
            "lease_id": f"{tier}-lease",
            "artifact_ids": [f"{tier}-artifact"],
            "started_at": "2026-07-23T00:00:00Z",
            "completed_at": "2026-07-23T00:00:01Z",
            "request_digest": "a" * 64,
            "response_digest": "b" * 64,
            "handshake_ok": True,
            "heartbeat_ok": True,
            "task_success": True,
            "protocol": "zyra-worker-v1",
            "transport": "http",
        }
        for tier in ("local", "edge", "cloud")
    ]
    gate = EndpointEvidenceGate().evaluate(observations, final_completion=True)
    assert gate.status is GateStatus.BLOCKED
    assert any("loopback" in finding.code for finding in gate.findings)


def test_benchmark_admission_counts_mutations_and_excludes_heartbeat() -> None:
    events = [
        {
            "event_id": "event-1",
            "event_type": "artifact_written",
            "run_id": "run-1",
            "task_id": "task-1",
            "sequence": 1,
            "causation_id": "cause-1",
            "payload": {
                "action_id": "action-1",
                "before_revision": 0,
                "after_revision": 1,
                "before": {"artifact": None},
                "after": {"artifact": "a"},
                "artifact_id": "a",
            },
        },
        {
            "event_id": "event-2",
            "event_type": "route_failover",
            "run_id": "run-1",
            "task_id": "task-1",
            "sequence": 2,
            "causation_id": "cause-1",
            "payload": {
                "action_id": "action-1",
                "before_revision": 1,
                "after_revision": 2,
                "before": {"route": "edge"},
                "after": {"route": "cloud"},
                "route_id": "cloud",
            },
        },
        {
            "event_id": "heartbeat-1",
            "event_type": "heartbeat",
            "run_id": "run-1",
            "task_id": "task-1",
            "sequence": 3,
            "payload": {"before_revision": 2, "after_revision": 3},
        },
    ]
    snapshot = BenchmarkAnalyzer().analyze(events, run_id="run-1")
    assert len(snapshot.effective_transitions) == 2
    assert len(snapshot.effective_actions) == 1
    assert snapshot.exclusions["excluded_event_family"] == 1
    assert {item.semantic_family.value for item in snapshot.effective_transitions} == {
        "artifact",
        "route",
    }


def test_handoff_store_detects_post_persist_tampering(tmp_path: Path) -> None:
    contract = HandoffBuilder().build(
        baseline_commit=BASELINE,
        target_commit=TARGET,
        surfaces=(),
        source_chains=(),
        state_custody=(),
        scenario_status={},
        gates=(),
        cleanroom_digest="c" * 64,
        report_id="report-1",
    )
    store = HandoffStore(tmp_path)
    path = store.persist(contract)
    assert store.load(path)["target_commit"] == TARGET

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["target_commit"] = BASELINE
    path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        store.load(path)
    except ValueError as error:
        assert "digest mismatch" in str(error)
    else:
        raise AssertionError("tampered handoff must fail digest verification")


def test_exit_policy_stays_blocked_without_external_evidence_and_line_proof() -> None:
    bundle = ExitBundleBuilder().build(
        baseline_commit=BASELINE,
        implementation_commit=TARGET,
        evidence_commit="",
        line_evidence=(),
        gates=(),
        scenario_status={},
        executed_disable_capabilities=(),
        cleanroom_commit=TARGET,
        cleanroom_digest="c" * 64,
        state_custody_digest="d" * 64,
        source_coverage_digest="e" * 64,
        handoff_digest="f" * 64,
        unresolved_requirements=("real-edge-cloud-evidence-not-admitted",),
    )
    gate = ExitPolicy().evaluate(bundle)
    assert bundle.decision is ExitDecision.BLOCKED
    assert gate.status is GateStatus.BLOCKED
    assert any(finding.code == "exit.requirement_unresolved" for finding in gate.findings)
