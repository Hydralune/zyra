from __future__ import annotations

import json
from pathlib import Path

from zyra_evaluation.m1_hardening.catalog import M1CapabilityCatalog, M1CustodyCatalog
from zyra_evaluation.m1_hardening.contracts import GateStatus
from zyra_evaluation.m1_hardening.coverage import SourceToTargetCoverageReport
from zyra_evaluation.m1_hardening.custody import M1StateCustodyMap
from zyra_evaluation.m1_hardening.disable import DisableModuleProbe, FunctionDisableProbe
from zyra_evaluation.m1_hardening.dependency import M1InternalizationGate
from zyra_evaluation.m1_hardening.evidence_graph import CausalEvidenceGraphGate
from zyra_evaluation.m1_hardening.langgraph import LangGraphBoundaryGate
from zyra_evaluation.m1_hardening.progress import LongHorizonProgressLedger, transition_event
from zyra_evaluation.m1_hardening.probe_catalog import GraphCustodyDisconnectProbe
from zyra_evaluation.m1_hardening.reporting import GatePolicyEngine, default_gate_policies
from zyra_evaluation.m1_hardening.store import HardeningReportStore, ReportIntegrityError


ROOT = Path(__file__).resolve().parents[2]


def test_internalization_gate_keeps_external_workspace_detection_separate_from_provenance(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "zyra"
    package_root = project_root / "packages"
    package_root.mkdir(parents=True)
    external = tmp_path / "claude-code-best" / "src"
    (package_root / "absolute_source.py").write_text(
        "from pathlib import Path\n"
        f"SOURCE = Path({str(external)!r})\n",
        encoding="utf-8",
    )

    report = M1InternalizationGate(project_root).evaluate(
        scan_roots=("packages",),
    )

    assert "internalization.workspace_external_path" in {
        finding.code for finding in report.findings
    }
    assert report.metrics["source_workspace"] == str(project_root / "provenance")
    assert report.metrics["external_workspace_roots"] == [str(tmp_path)]


def test_role_aware_coverage_and_custody_catalog_resolve_real_entries() -> None:
    catalog = M1CapabilityCatalog(ROOT, source_workspace=ROOT)
    coverage = SourceToTargetCoverageReport(ROOT, source_workspace=ROOT).evaluate(
        catalog.items(),
        required_capabilities=catalog.required_capabilities(),
        known_scenarios=(
            "m1-foundation-query-permission-control",
            "m1-query-mcp",
            "m1-skill-memory-compact",
            "m1-subagent-worker-recovery",
            "m1-api-stream-fallback",
            "m1-control-session-mutation",
        ),
        known_disable_probes=catalog.known_disable_probe_ids(),
    )
    assert coverage.status is GateStatus.PASSED
    assert coverage.blocker_count == 0
    assert coverage.error_count == 0
    assert coverage.metrics["capability_count"] >= 18
    omp = [item for item in coverage.metrics["items"] if item["source_repository"] == "oh-my-pi"]
    assert {
        item["capability_id"]
        for item in omp
        if item["role"] in {"primary", "supplementary"}
    } == {
        "memory-curator",
        "memory-retrieval",
        "provider-control",
        "subagent-runtime",
        "worker-pool",
        "workspace-gateway",
    }
    assert all(not item["capability_id"].startswith("omp-decision-") for item in omp)

    custody_catalog = M1CustodyCatalog()
    custody = M1StateCustodyMap(ROOT).evaluate(
        custody_catalog.entries(),
        required_families=custody_catalog.required_families(),
    )
    assert custody.status is GateStatus.PASSED
    assert custody.metrics["state_family_count"] >= 12
    owners = {entry["state_family"]: entry["canonical_owner"] for entry in custody.metrics["entries"]}
    assert owners["graph_topology"] == "GraphStateStore"
    assert owners["permission"] == "PermissionJournal"


def test_langgraph_negative_boundary_executes_dynamic_adversarial_probes(tmp_path: Path) -> None:
    gate = LangGraphBoundaryGate(ROOT, artifact_root=tmp_path).evaluate(run_dynamic_probes=True)
    assert gate.status is GateStatus.PASSED
    assert gate.blocker_count == 0
    probes = gate.metrics["probes"]
    assert len(probes) >= 7
    assert all(item["ok"] for item in probes)
    probe_ids = {item["probe_id"] for item in probes}
    assert "runtime-topology-mutation" in probe_ids
    assert "nested-alias-isolation" in probe_ids
    assert "deterministic-permutation" in probe_ids
    assert "explicit-write-conflict" in probe_ids
    assert "side-effect-idempotency-fence" in probe_ids
    assert "codeworker-loop-cohesion" in probe_ids


def test_disable_probe_fails_closed_and_restores_owner() -> None:
    state = {"enabled": True, "revision": 1}

    def capture() -> dict[str, object]:
        return dict(state)

    def exercise() -> dict[str, object]:
        if not state["enabled"]:
            return {
                "ok": False,
                "error": "owner_disabled",
                "metadata": {"canonical_runtime_owner": "unit-owner", "fallback": False},
            }
        return {
            "ok": True,
            "value": "real-owner-result",
            "metadata": {"canonical_runtime_owner": "unit-owner", "fallback": False},
        }

    def disable() -> dict[str, object]:
        state["enabled"] = False
        state["revision"] = int(state["revision"]) + 1
        return {"disabled": True, "revision": state["revision"]}

    def restore(captured: dict[str, object]) -> dict[str, object]:
        state.update(captured)
        return {"restored": True, "revision": state["revision"]}

    suite = DisableModuleProbe()
    suite.register(
        FunctionDisableProbe(
            probe_id="disable-unit-owner",
            capability="unit-owner",
            capture_fn=capture,
            exercise_fn=exercise,
            disable_fn=disable,
            restore_fn=restore,
            expected_error_codes=("owner_disabled",),
        )
    )
    result = suite.evaluate(required_capabilities=("unit-owner",), execute=True)
    assert result.status is GateStatus.PASSED
    assert result.metrics["executed_count"] == 1
    execution = result.metrics["executions"][0]
    assert execution["expected_failure_observed"] is True
    assert execution["fallback_masked"] is False
    assert state == {"enabled": True, "revision": 1}


def test_default_graph_custody_probe_disconnects_real_owner_and_restores(tmp_path: Path) -> None:
    suite = DisableModuleProbe()
    suite.register(GraphCustodyDisconnectProbe(tmp_path))
    result = suite.evaluate(required_capabilities=("graph-custody",), execute=True)
    assert result.status is GateStatus.PASSED
    execution = result.metrics["executions"][0]
    assert execution["error_code"] == "graph_custody_disabled"
    assert execution["fallback_masked"] is False
    assert execution["restore_receipt"]["restored_owner"] == "GraphStateStore.initialize"


def test_progress_ledger_counts_only_semantic_revision_mutations() -> None:
    events = [
        transition_event(
            event_id=f"event-{index}",
            run_id="run-1",
            task_id="task-1",
            event_type="state_mutated",
            semantic_family="artifact" if index % 2 else "route",
            semantic_key=f"key-{index}",
            before_revision=index,
            after_revision=index + 1,
            before={"value": index},
            after={"value": index + 1},
            causation_id=f"cause-{index}",
            action_id=f"action-{index // 2}",
        )
        for index in range(2_000)
    ]
    events.extend(
        [
            {
                "event_id": "heartbeat-1",
                "run_id": "run-1",
                "task_id": "task-1",
                "event_type": "heartbeat",
                "payload": {"before_revision": 2000, "after_revision": 2001},
            },
            {
                "event_id": "noop-1",
                "run_id": "run-1",
                "task_id": "task-1",
                "event_type": "state_mutated",
                "payload": {
                    "before_revision": 2001,
                    "after_revision": 2002,
                    "before": {"same": True},
                    "after": {"same": True},
                    "causation_id": "cause-noop",
                    "action_id": "action-noop",
                },
            },
        ]
    )
    gate = LongHorizonProgressLedger().evaluate(events, run_id="run-1", final_completion=True)
    assert gate.status is GateStatus.PASSED
    assert gate.metrics["effective_transition_count"] == 2_000
    assert gate.metrics["effective_action_count"] == 1_000
    assert gate.metrics["transition_threshold_met"] is True
    assert gate.metrics["action_threshold_met"] is True


def test_causal_evidence_detects_semantic_chains_and_revision_failures() -> None:
    events = [
        {
            "event_id": "permission-1",
            "event_type": "permission_decision",
            "run_id": "run-1",
            "task_id": "task-1",
            "sequence": 1,
            "payload": {"before_revision": 0, "after_revision": 1, "decision_id": "decision-1"},
        },
        {
            "event_id": "tool-1",
            "event_type": "tool_started",
            "run_id": "run-1",
            "task_id": "task-1",
            "sequence": 2,
            "causation_id": "permission-1",
            "payload": {"before_revision": 1, "after_revision": 2, "tool_call_id": "call-1"},
        },
        {
            "event_id": "artifact-1",
            "event_type": "artifact_written",
            "run_id": "run-1",
            "task_id": "task-1",
            "sequence": 3,
            "causation_id": "tool-1",
            "payload": {"before_revision": 2, "after_revision": 3, "artifact_id": "artifact-a"},
        },
    ]
    gate = CausalEvidenceGraphGate().evaluate(events, final_completion=False)
    assert gate.blocker_count == 0
    assert gate.error_count == 0
    chains = gate.metrics["analysis"]["semantic_chains"]["observed"]
    assert chains["permission_to_tool"] is True
    assert chains["tool_to_artifact"] is True

    events[2]["payload"]["after_revision"] = 2
    broken = CausalEvidenceGraphGate().evaluate(events, final_completion=False)
    assert broken.status is GateStatus.BLOCKED
    assert any(finding.code == "causal.revision_not_advanced" for finding in broken.findings)


def test_report_store_is_idempotent_and_detects_tampering(tmp_path: Path) -> None:
    from zyra_evaluation.m1_hardening.contracts import GateResult, HardeningReport
    from zyra_evaluation.m1_hardening.reporting import EvidenceLinkAuditor

    gate = GateResult("source-to-target-coverage", GateStatus.PASSED, "covered").finish()
    report = HardeningReport(
        report_id="report-1",
        baseline_commit="44da53a",
        project_root=str(ROOT),
        gates=[gate],
    )
    policy = GatePolicyEngine(default_gate_policies())
    disposition = policy.evaluate(report)
    evidence = EvidenceLinkAuditor().evaluate(report)
    store = HardeningReportStore(tmp_path)
    record = store.persist(report, disposition=disposition, evidence_audit=evidence)
    again = store.persist(report, disposition=disposition, evidence_audit=evidence)
    assert again.content_digest == record.content_digest
    assert store.verify_chain()["valid"] is True

    path = tmp_path / record.json_path
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["baseline_commit"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        store.load("report-1", verify=True)
    except ReportIntegrityError:
        pass
    else:
        raise AssertionError("tampered report must fail integrity verification")
