from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

from zyra_evaluation.m1_hardening import cli as hardening_cli
from zyra_evaluation.m1_hardening.benchmark import BenchmarkAnalyzer
from zyra_evaluation.m1_hardening.cleanroom import (
    CleanroomBoundaryScanner,
    CleanEnvironment,
    CleanroomVerifier,
    default_cleanroom_commands,
)
from zyra_evaluation.m1_hardening.contracts import (
    EvidencePointer,
    GateResult,
    GateStatus,
)
from zyra_evaluation.m1_hardening.cross_scenario import (
    CausalPartition,
    CrossScenarioConsistencyGate,
    EventProjection,
    TopologyAdversarialGate,
)
from zyra_evaluation.m1_hardening.disable import DisableProbeRunner, ProbeStatus
from zyra_evaluation.m1_hardening.evidence_admission import (
    AdmissionPolicy,
    AdmissionStatus,
    EvidenceAdmissionController,
    EvidenceEnvelopeFactory,
    EvidenceKind,
    EvidenceOrigin,
)
from zyra_evaluation.m1_hardening.exit_gate import ExitBundleBuilder, ExitDecision, ExitPolicy
from zyra_evaluation.m1_hardening.handoff import (
    HandoffBuilder,
    HandoffGate,
    HandoffStore,
    default_handoff_surfaces,
)
from zyra_evaluation.m1_hardening.integration_service import (
    IntegrationOptions,
    M1IntegrationService,
)
from zyra_evaluation.m1_hardening.line_audit import EffectiveLineAuditor
from zyra_evaluation.m1_hardening.integration_scenarios import (
    ProviderFailoverProbeServer,
    default_integration_scenarios,
    scenario_catalog_gate,
)
from zyra_evaluation.m1_hardening.live_evidence import (
    EndpointEvidenceGate,
    ProviderWireEvidenceGate,
    WireDialect,
)
from zyra_evaluation.m1_hardening.managed_provider import (
    CapturedLine,
    ManagedProviderProbe,
    ManagedProviderSpec,
    ProcessCapture,
)
from zyra_evaluation.m1_hardening.owner_matrix import OwnerMatrix, REQUIRED_DISABLE_CAPABILITIES
from zyra_evaluation.m1_hardening.owner_probes import (
    EnvironmentOwnerDisconnectProbe,
    OwnerDisableContract,
    OwnerProbeCatalog,
)
from zyra_evaluation.m1_hardening.service import AuditOptions


ROOT = Path(__file__).resolve().parents[2]
BASELINE = "8" * 40
TARGET = "9" * 40


def _provider_capture(values: list[dict[str, object]]) -> ProcessCapture:
    lines = tuple(
        CapturedLine(
            sequence=index,
            timestamp=f"2026-07-23T00:00:{index:02d}Z",
            raw=json.dumps(value, sort_keys=True),
            value=value,
        )
        for index, value in enumerate(values)
    )
    return ProcessCapture(
        command_id="managed-provider-test",
        started_at="2026-07-23T00:00:00Z",
        completed_at="2026-07-23T00:00:09Z",
        exit_code=0,
        lines=lines,
        stderr_digest="0" * 64,
        stderr_bytes=0,
        executable_version="test-cli 1.0",
    )


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
    assert len(probes.probe_ids()) == 18
    assert probes.validate() == ()
    assert owner_gate.status is GateStatus.PASSED, owner_gate.to_dict()
    assert owner_gate.metrics["required_capability_count"] == 18
    mapped = {
        disconnect.capability
        for definition in definitions
        for disconnect in definition.disconnects
    }
    assert mapped == set(REQUIRED_DISABLE_CAPABILITIES)
    assert all(
        disconnect.exercise_step_id in {request.step_id for request in definition.requests}
        for definition in definitions
        for disconnect in definition.disconnects
    )
    failover = next(
        item for item in definitions if item.kind.value == "api-stream-provider-failover"
    )
    failover_request = next(
        item for item in failover.requests if item.step_id == "provider-stall-worker"
    )
    assert failover_request.payload["model_transport"] == "http_sse"
    assert failover_request.payload["api_retry_max_attempts"] == 4
    assert {
        "retry-fallback-selected",
        "failover-model-changed",
        "failover-used",
    }.issubset({item.assertion_id for item in failover.assertions})


def test_provider_failover_probe_exercises_real_http_outage_then_sse_recovery() -> None:
    probe = ProviderFailoverProbeServer().start()
    try:
        statuses: list[int] = []
        for attempt in range(4):
            request = urllib.request.Request(
                probe.base_url + "/chat/completions",
                data=json.dumps({"model": f"model-{attempt}"}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    statuses.append(response.status)
                    body = response.read().decode("utf-8")
            except urllib.error.HTTPError as error:
                statuses.append(error.code)
    finally:
        probe.stop()

    receipt = probe.receipt()
    assert statuses == [529, 529, 529, 200]
    assert "data: [DONE]" in body
    assert receipt["bounded_capacity_outage_observed"] is True
    assert receipt["recovered_stream_observed"] is True
    assert receipt["models"] == ["model-0", "model-1", "model-2", "model-3"]


def test_cross_scenario_distinguishes_external_correlation_from_event_parent() -> None:
    external = EventProjection.from_mapping(
        {
            "event_id": "event_child",
            "run_id": "run-1",
            "task_id": "task-1",
            "event_type": "control_command",
            "sequence": 1,
            "causation_id": "controlreq_external",
            "payload": {},
        },
        0,
    )
    missing_event_parent = EventProjection.from_mapping(
        {
            "event_id": "event_orphan",
            "run_id": "run-1",
            "task_id": "task-1",
            "event_type": "agent_message",
            "sequence": 2,
            "causation_id": "event_missing",
            "payload": {},
        },
        1,
    )
    result = GateResult(
        gate_id="test-cross-causation",
        status=GateStatus.NOT_RUN,
        summary="test",
    )

    CrossScenarioConsistencyGate._partition_findings(
        (CausalPartition("run-1", "task-1", [external]),),
        result,
    )
    assert result.findings == []

    CrossScenarioConsistencyGate._partition_findings(
        (CausalPartition("run-1", "task-1", [external, missing_event_parent]),),
        result,
    )
    assert [item.code for item in result.findings] == ["cross.causation_orphan"]


def test_cross_cutting_support_requires_traceable_gates_and_material_code_index() -> None:
    scenario = SimpleNamespace(
        disconnect_evidence=(
            {
                "probe_id": "disable-code-index",
                "status": "passed",
                "expected_failure_observed": True,
                "material_difference": True,
                "fallback_masked": False,
                "difference": {"semantic_change": True},
            },
        )
    )
    supporting = tuple(
        GateResult(
            gate_id=gate_id,
            status=GateStatus.PASSED,
            summary="passed",
            evidence=[
                EvidencePointer(
                    kind="foundation_audit_execution",
                    location=f"audit-{gate_id}",
                    summary="executed",
                )
            ],
        )
        for gate_id in (
            "patch-git",
            "deny-policy",
            "secrets-prompt-injection",
            "code-index",
        )
    )
    result = GateResult(
        gate_id="test-cross-cutting",
        status=GateStatus.NOT_RUN,
        summary="test",
    )

    CrossScenarioConsistencyGate._cross_cutting_findings(
        (scenario,),
        supporting,
        result,
        final_completion=True,
    )

    assert result.findings == []
    assert result.metrics["code_index_material_difference"] is True


def test_foundation_aggregate_adds_execution_evidence_for_metric_only_gate() -> None:
    child = GateResult(
        gate_id="patch-git",
        status=GateStatus.PASSED,
        summary="child",
        metrics={"runtime": "observed"},
    )
    audit = SimpleNamespace(
        report=SimpleNamespace(
            gates=(child,),
            report_id="audit-1",
            scenario_id="scenario-1",
            task_id="task-1",
            run_id="run-1",
            generated_at="2026-07-23T00:00:00Z",
        )
    )

    aggregate = M1IntegrationService._aggregate_foundation_gates((audit,))[0]

    assert aggregate.status is GateStatus.PASSED
    assert len(aggregate.evidence) == 1
    assert aggregate.evidence[0].kind == "foundation_audit_execution"


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
    execution = DisableProbeRunner().run(probe)

    assert execution.status is ProbeStatus.PASSED, execution.to_dict()
    assert execution.baseline["ok"] is True
    assert execution.disabled == {
        "ok": False,
        "error": "test_owner_disabled",
        "canonical_owner": "code-index",
        "fallback": False,
    }
    assert execution.restore_receipt["ok"] is True
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

    forbidden.write_text(
        "from pathlib import Path\nPath('../browser-use/runtime')\n",
        encoding="utf-8",
    )
    _, _, references, _, _ = CleanroomBoundaryScanner(tmp_path).scan()
    assert any(item["kind"] == "python-external-process-or-path" for item in references)

    detector = package / "detector.py"
    detector.write_text(
        "def inspect(value):\n    return value.startswith('g:/agent-zoo/')\n",
        encoding="utf-8",
    )
    _, _, references, _, _ = CleanroomBoundaryScanner(tmp_path).scan()
    assert not any(item["path"].endswith("detector.py") for item in references)


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


def test_cleanroom_exposes_only_package_manager_locked_bun(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    extracted = tmp_path / "extracted"
    bun = source / "node_modules" / "bun" / "bin" / ("bun.exe" if os.name == "nt" else "bun")
    bun.parent.mkdir(parents=True)
    bun.write_text("toolchain-placeholder", encoding="utf-8")
    extracted.mkdir()
    (extracted / "package.json").write_text(
        json.dumps({"packageManager": "bun@1.2.15"}),
        encoding="utf-8",
    )
    verifier = CleanroomVerifier(source)

    class Completed:
        returncode = 0
        stdout = "1.2.15\n"

    monkeypatch.setattr("zyra_evaluation.m1_hardening.cleanroom.subprocess.run", lambda *args, **kwargs: Completed())
    environment = verifier._controlled_toolchain_environment(extracted)

    assert environment["ZYRA_BUN_EXECUTABLE"] == str(bun.resolve())
    assert environment["ZYRA_BUN_LOCKED_VERSION"] == "1.2.15"
    assert environment["PATH"].split(os.pathsep)[0] == str(bun.parent.resolve())


def test_cleanroom_materializes_committed_workspace_packages_without_install(tmp_path: Path) -> None:
    package = tmp_path / "packages" / "memory" / "runtime"
    package.mkdir(parents=True)
    (tmp_path / "package.json").write_text(
        json.dumps({"workspaces": ["packages/memory/runtime"]}),
        encoding="utf-8",
    )
    (package / "package.json").write_text(
        json.dumps({"name": "@zyra/memory-runtime", "exports": {".": "./src/index.ts"}}),
        encoding="utf-8",
    )
    source = package / "src" / "index.ts"
    source.parent.mkdir()
    source.write_text("export const owner = 'zyra';\n", encoding="utf-8")

    materialized = CleanroomVerifier._materialize_workspace_packages(tmp_path)

    target = tmp_path / "node_modules" / "@zyra" / "memory-runtime"
    assert materialized == ["@zyra/memory-runtime"]
    assert (target / "package.json").is_file()
    assert (target / "src" / "index.ts").read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_clean_environment_keeps_process_temporary_state_inside_archive(tmp_path: Path) -> None:
    environment = CleanEnvironment.build(tmp_path)

    temporary = (tmp_path / ".cleanroom-tmp").resolve()
    assert temporary.is_dir()
    assert Path(environment["TEMP"]).resolve() == temporary
    assert Path(environment["TMP"]).resolve() == temporary
    assert Path(environment["TMPDIR"]).resolve() == temporary


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


def test_final_integration_automatically_binds_all_required_evidence_kinds() -> None:
    options = IntegrationOptions(
        baseline_commit=BASELINE,
        implementation_commit=TARGET,
        final_completion=True,
        execute_disconnects=True,
        run_cleanroom=True,
        line_evidence=(
            {
                "slice_id": "M1-S08-02",
                "effective_added_lines": 7100,
                "minimum_required": 7000,
                "source_pool_lines": 0,
            },
        ),
        tier_observations=(
            {"tier": "local"},
            {"tier": "edge"},
            {"tier": "cloud"},
        ),
        provider_observations=(
            {"provider_id": "anthropic", "dialect": "anthropic-compatible"},
            {"provider_id": "openai", "dialect": "openai-compatible"},
        ),
    )
    scenario = SimpleNamespace(
        scenario_id="m1-integration-live",
        passed=True,
        disconnect_evidence=(
            {
                "probe_id": "disable-owner-live",
                "capability": "runtime-owner",
                "status": "passed",
                "expected_failure_observed": True,
                "fallback_masked": False,
            },
        ),
        digest=lambda: "1" * 64,
    )
    benchmark_events = (
        {
            "event_id": "event-1",
            "run_id": "run-live",
            "task_id": "task-live",
            "event_type": "artifact_committed",
        },
        {
            "event_id": "event-2",
            "run_id": "run-live",
            "task_id": "task-live",
            "event_type": "verification_completed",
        },
    )
    benchmark_gate = GateResult(
        gate_id="m1-long-horizon-benchmark",
        status=GateStatus.PASSED,
        summary="live benchmark",
        metrics={
            "effective_action_count": 1000,
            "effective_transition_count": 2000,
            "snapshot_digest": "2" * 64,
            "child_status": {"sealed-autonomy": "passed"},
        },
    )
    cleanroom = SimpleNamespace(
        target_commit=TARGET,
        commands=(SimpleNamespace(ok=True),),
        to_dict=lambda: {"content_digest": "3" * 64},
    )
    handoff = SimpleNamespace(
        report_id="handoff-live",
        surfaces=(object(),),
        source_chains=(object(),),
        to_dict=lambda: {"content_digest": "4" * 64},
    )
    custody_gate = GateResult(
        gate_id="m1-state-custody",
        status=GateStatus.PASSED,
        summary="custody",
        metrics={"owner_count": 9},
    )
    coverage_gate = GateResult(
        gate_id="source-to-target-coverage",
        status=GateStatus.PASSED,
        summary="coverage",
        metrics={"covered_count": 12},
    )
    report = SimpleNamespace(
        gate=lambda gate_id: (
            custody_gate if gate_id == "m1-state-custody" else coverage_gate
        )
    )
    audit = SimpleNamespace(report=report)

    envelopes = M1IntegrationService._automatic_evidence_envelopes(
        integration_run_id="m1-integration-live-run",
        benchmark_run_id="run-live",
        options=options,
        scenarios=(scenario,),
        benchmark_events=benchmark_events,
        benchmark_gate=benchmark_gate,
        cleanroom=cleanroom,
        handoff=handoff,
        base_audits=(audit,),
    )
    receipts, gate = EvidenceAdmissionController().admit(
        envelopes,
        expected_target_commit=TARGET,
        final_completion=True,
    )

    assert {item.kind for item in envelopes} == set(EvidenceKind)
    assert len(receipts) == len(EvidenceKind)
    assert all(item.status is AdmissionStatus.ADMITTED for item in receipts)
    assert gate.status is GateStatus.PASSED, gate.to_dict()


def test_cli_integration_delegates_orchestration_to_owner_process(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class OwnerProcessTransport:
        def __init__(self, base_url: str, *, timeout_seconds: float) -> None:
            captured["base_url"] = base_url
            captured["timeout_seconds"] = timeout_seconds

        def post(self, path: str, payload: Mapping[str, Any]):
            captured["path"] = path
            captured["payload"] = payload
            return (
                200,
                {
                    "schema": "zyra.m1-integration-outcome/v1",
                    "accepted": True,
                    "run_id": "m1-owner-process-test",
                },
            )

    monkeypatch.setattr(hardening_cli, "HttpScenarioTransport", OwnerProcessTransport)
    monkeypatch.setattr(
        hardening_cli,
        "_git_identity",
        lambda _root, expression: (
            "1" * 40 if expression == "HEAD" else str(expression)
        ),
    )
    args = SimpleNamespace(
        scenario_timeout=120.0,
        integration_timeout=3600.0,
        sealed_policy_json="",
        line_evidence_json="",
        tier_evidence_json="",
        provider_evidence_json="",
        evidence_envelopes_json="",
        benchmark_events_json="",
        implementation_commit="HEAD",
        evidence_commit="",
        baseline="HEAD",
        scenario=["m1-integration-query-session-context-tool"],
        no_scenarios=False,
        execute_disconnects=True,
        final_completion=False,
        run_cleanroom=False,
        benchmark_run_id="",
        unresolved_requirement=[],
        no_persist=True,
        base_url="http://127.0.0.1:8010",
    )

    result = hardening_cli._integration(SimpleNamespace(root=ROOT), args)

    assert result == 0
    assert captured["path"] == "/hardening/m1/integration"
    assert captured["timeout_seconds"] == 3600.0
    payload = captured["payload"]
    assert isinstance(payload, Mapping)
    assert payload["execute_disconnects"] is True
    assert payload["scenario_ids"] == ["m1-integration-query-session-context-tool"]


def test_zero_mock_fixture_line_bucket_is_not_fixture_provenance() -> None:
    policy = AdmissionPolicy(
        required_kinds=(EvidenceKind.LINE_AUDIT,),
        require_chain_for_kinds=(),
    )
    envelope = EvidenceEnvelopeFactory().create(
        evidence_id="line-audit-live-1",
        kind=EvidenceKind.LINE_AUDIT,
        origin=EvidenceOrigin.LOCAL_AUDITOR,
        producer="effective-line-auditor",
        content={"slice_id": "M1-S08-02", "mock_fixture": 0, "effective_production": 7001},
        baseline_commit=BASELINE,
        target_commit=TARGET,
    )

    receipts, gate = EvidenceAdmissionController(policy).admit(
        [envelope], expected_target_commit=TARGET, final_completion=True
    )

    assert receipts[0].status is AdmissionStatus.ADMITTED
    assert gate.status is GateStatus.PASSED, gate.to_dict()

    fixture = EvidenceEnvelopeFactory().create(
        evidence_id="line-audit-fixture-1",
        kind=EvidenceKind.LINE_AUDIT,
        origin=EvidenceOrigin.LOCAL_AUDITOR,
        producer="effective-line-auditor",
        content={"artifact_path": "tests/fixtures/recorded_trace.json"},
        baseline_commit=BASELINE,
        target_commit=TARGET,
    )
    receipts, gate = EvidenceAdmissionController(policy).admit(
        [fixture], expected_target_commit=TARGET, final_completion=True
    )
    assert receipts[0].status is AdmissionStatus.REJECTED
    assert "fixture_evidence_rejected" in receipts[0].reasons
    assert gate.status is GateStatus.BLOCKED


def test_default_handoff_exposes_canonical_event_surface() -> None:
    surfaces = default_handoff_surfaces(("scenario:evidence",))

    assert {item.kind for item in surfaces} == HandoffGate.REQUIRED_SURFACE_KINDS
    event = next(item for item in surfaces if item.kind == "event")
    assert event.owner == "RuntimeEventSpine"
    assert event.state_family == "runtime_event"
    assert event.event_types


def test_topology_gate_accepts_runtime_pending_then_committed_mutation() -> None:
    events = (
        {
            "event_id": "topology-pending-1",
            "run_id": "run-topology",
            "task_id": "task-topology",
            "event_type": "topology_write_pending",
            "sequence": 1,
            "payload": {
                "semantic_key": "topology.node.dynamic-verifier",
                "commit_state": "pending",
                "runtime_created": True,
                "outside_precompiled_set": True,
                "after": {"node_id": "dynamic-verifier", "status": "pending"},
            },
        },
        {
            "event_id": "topology-committed-1",
            "run_id": "run-topology",
            "task_id": "task-topology",
            "event_type": "topology_node_added",
            "sequence": 2,
            "causation_id": "topology-pending-1",
            "payload": {
                "semantic_key": "topology.node.dynamic-verifier",
                "commit_state": "committed",
                "runtime_created": True,
                "outside_precompiled_set": True,
                "after": {"node_id": "dynamic-verifier", "status": "active"},
            },
        },
    )

    gate = TopologyAdversarialGate().evaluate(events, final_completion=True)

    assert gate.status is GateStatus.PASSED, gate.to_dict()
    assert gate.metrics["external_mutation_count"] == 2
    assert gate.metrics["pending_count"] == 1
    assert gate.metrics["committed_count"] == 1


def test_line_audit_excludes_only_source_pool_already_inside_explicit_protection() -> None:
    class Diff:
        @staticmethod
        def numstat(baseline: str, head: str = "HEAD"):
            if (baseline, head) == ("baseline", "target"):
                return {"vendor-runtimes/upstream/runtime.py": (100, 0)}
            if (baseline, head) == ("baseline", "target-new"):
                return {"vendor-runtimes/upstream/runtime.py": (101, 0)}
            if (baseline, head) == ("baseline", "protected"):
                return {"vendor-runtimes/upstream/runtime.py": (100, 0)}
            if (baseline, head) == ("protected", "target"):
                return {}
            if (baseline, head) == ("protected", "target-new"):
                return {"vendor-runtimes/upstream/runtime.py": (1, 0)}
            raise AssertionError((baseline, head))

        @staticmethod
        def added_lines(baseline: str, head: str = "HEAD"):
            return {}

        @staticmethod
        def is_ancestor(ancestor: str, descendant: str) -> bool:
            return (ancestor, descendant) in {
                ("baseline", "protected"),
                ("protected", "target"),
                ("protected", "target-new"),
            }

    auditor = EffectiveLineAuditor(ROOT)
    auditor.git = Diff()

    unprotected = auditor.evaluate("baseline", head="target")
    protected = auditor.evaluate(
        "baseline",
        head="target",
        protected_source_pool_commit="protected",
    )
    newly_added = auditor.evaluate(
        "baseline",
        head="target-new",
        protected_source_pool_commit="protected",
    )

    assert unprotected.status is GateStatus.BLOCKED
    assert any(
        item.code == "line-audit.vendor_source_added"
        for item in unprotected.findings
    )
    assert protected.status is GateStatus.PASSED
    assert protected.metrics["protected_vendor_raw_added"] == 100
    assert protected.metrics["unprotected_vendor_raw_added"] == 0
    assert any(
        item.code == "line-audit.protected_source_pool_excluded"
        for item in protected.findings
    )
    assert newly_added.status is GateStatus.BLOCKED
    assert newly_added.metrics["unprotected_vendor_raw_added"] == 1


def test_line_audit_classifies_source_from_audited_revision() -> None:
    class Diff:
        @staticmethod
        def numstat(baseline: str, head: str = "HEAD"):
            assert (baseline, head) == ("baseline", "target")
            return {"packages/runtime/example.py": (1, 0)}

        @staticmethod
        def added_lines(baseline: str, head: str = "HEAD"):
            assert (baseline, head) == ("baseline", "target")
            return {"packages/runtime/example.py": {1}}

        @staticmethod
        def file_text(revision: str, path: str) -> str:
            assert revision == "target"
            assert path == "packages/runtime/example.py"
            return "result = execute_runtime()\n"

    auditor = EffectiveLineAuditor(ROOT)
    auditor.git = Diff()

    gate = auditor.evaluate(
        "baseline",
        head="target",
        minimum_effective_production=1,
    )

    assert gate.status is GateStatus.PASSED, gate.to_dict()
    assert gate.metrics["effective_production_lines"] == 1


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


def test_managed_provider_streams_admit_two_real_dialects_without_header_claims() -> None:
    token = "ZYRA_PROVIDER_TOOL_TEST"
    prompt = "read provider_input.txt"
    probe = ManagedProviderProbe()
    claude = probe.attestation_from_capture(
        ManagedProviderSpec(
            provider_id="anthropic-first-party",
            cli_kind="claude",
            executable="claude",
            model_id="sonnet",
            dialect=WireDialect.ANTHROPIC_COMPATIBLE,
            endpoint="https://api.anthropic.com",
            request_path="/v1/messages",
        ),
        _provider_capture(
            [
                {"type": "system", "subtype": "init", "model": "claude-sonnet-5"},
                {
                    "type": "assistant",
                    "request_id": "anthropic-request-1",
                    "message": {
                        "model": "claude-sonnet-5",
                        "content": [{"type": "tool_use", "id": "tool-claude", "name": "Read"}],
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [{"type": "tool_result", "tool_use_id": "tool-claude"}]
                    },
                },
                {
                    "type": "assistant",
                    "request_id": "anthropic-request-2",
                    "message": {
                        "model": "claude-sonnet-5",
                        "content": [{"type": "text", "text": token}],
                    },
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "result": token,
                    "stop_reason": "end_turn",
                },
            ]
        ),
        expected_token=token,
        prompt=prompt,
        attempt_id="anthropic-attempt",
    )
    codex = probe.attestation_from_capture(
        ManagedProviderSpec(
            provider_id="openai-codex",
            cli_kind="codex",
            executable="codex",
            model_id="gpt-5.6-sol",
            dialect=WireDialect.OPENAI_COMPATIBLE,
            endpoint="https://api.openai.com",
            request_path="/v1/responses",
        ),
        _provider_capture(
            [
                {"type": "thread.started", "thread_id": "openai-thread-1"},
                {
                    "type": "item.started",
                    "item": {"id": "tool-codex", "type": "command_execution"},
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "tool-codex",
                        "type": "command_execution",
                        "status": "completed",
                        "exit_code": 0,
                    },
                },
                {
                    "type": "item.completed",
                    "item": {"id": "answer", "type": "agent_message", "text": token},
                },
                {"type": "turn.completed"},
            ]
        ),
        expected_token=token,
        prompt=prompt,
        attempt_id="openai-attempt",
    )

    gate = ProviderWireEvidenceGate().evaluate(
        [claude, codex],
        final_completion=True,
    )

    assert gate.status is GateStatus.PASSED, gate.to_dict()
    assert gate.metrics["providers"] == ["anthropic-first-party", "openai-codex"]
    assert gate.metrics["dialects"] == ["anthropic-compatible", "openai-compatible"]
    assert all(not item.request_headers for item in (claude, codex))


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


def test_final_foundation_audit_cannot_disable_effective_line_gate() -> None:
    options = AuditOptions(
        baseline_commit=BASELINE,
        final_completion=True,
        include_line_audit=False,
    )

    try:
        options.validate()
    except ValueError as error:
        assert "cannot disable" in str(error)
    else:
        raise AssertionError("final completion must retain the effective line gate")
