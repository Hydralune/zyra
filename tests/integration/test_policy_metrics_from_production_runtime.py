from __future__ import annotations

import copy
import json

import pytest

from apps.api.zyra_api import main as api
from apps.api.zyra_api.policy_api import (
    FilesystemPolicyMetricReportProvider,
    PolicyMetricApi,
    RuntimePolicyEvidenceSource,
)
from zyra_core import EventRecord, EventType, PlanNodeStatus
from zyra_evaluation.policy_benchmark import (
    PHYSICAL_DISPATCH_RECEIPTS,
    CanonicalRuntimeReceiptResolver,
    MetricStatus,
    Phase2MetricError,
    Phase2MetricReportBuilder,
    RunMetricInput,
    canonical_digest,
    write_phase2_metric_report,
)
from zyra_orchestration import ensure_default_graph, run_task_graph
from zyra_memory import MemoryLayer, MemoryRecord


def _production_run():
    state, created = api.make_task_created_event(
        "Create policy-metrics-artifact.txt containing exactly one line: "
        "PHASE2_POLICY_METRICS_OK. Use only file_write and file_read; do not "
        "run shell commands or browse."
    )
    obligations = tuple(
        sorted(
            {
                *state.constraints.requirements,
                *state.constraints.success_criteria,
            }
        )
    )
    requirement_revision = "requirements:" + canonical_digest(obligations)
    memory_event = EventRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        event_id="event-policy-metrics-critical-fact",
        event_type=EventType.AGENT_MESSAGE,
        node_id=state.root_node_id,
        payload={"fact": "signed release checksum", "value": "sha256:r1"},
    )
    memory_content = {
        "fact": "signed release checksum",
        "value": "sha256:r1",
        "requirement_revision": requirement_revision,
    }
    api.persist_events(api.get_store(), [created, memory_event])
    api.get_store().save_memory_records(
        [
            MemoryRecord(
                memory_id="memory-policy-metrics-critical-fact",
                run_id=state.run_id,
                task_id=state.task_id,
                layer=MemoryLayer.SEMANTIC,
                source_type="event_log",
                source_id=memory_event.event_id,
                node_id=state.root_node_id,
                summary="The signed release checksum is sha256:r1.",
                content=memory_content,
                evidence_ids=[memory_event.event_id],
                score=1.0,
                metadata={
                    "continuity_status": "active",
                    "continuity_version": f"{requirement_revision}/fact-v1",
                    "content_digest": canonical_digest(memory_content),
                    "requirement_revision": requirement_revision,
                },
            )
        ]
    )
    workspace = api.get_workspace_manager().create_for_task(
        run_id=state.run_id,
        task_id=state.task_id,
        session_id=f"task:{state.task_id}",
        worker_id="task-runtime",
        idempotency_key=f"policy-metrics-production:{state.task_id}",
        causation_id=created.event_id,
    )
    state.metadata["workspace_ref"] = workspace.projection.to_dict()
    ensure_default_graph(state)
    events = run_task_graph(
        state,
        execution_context=api.graph_execution_context(),
    )
    api.persist_events(api.get_store(), list(events))
    assert state.status is PlanNodeStatus.COMPLETED, json.dumps(
        [
            dict(event.payload)
            for event in events
            if event.event_type is EventType.SYSTEM_NOTICE
            and event.payload.get("error_metadata")
        ],
        default=str,
        sort_keys=True,
    )
    readiness = json.loads(
        (
            api.PROJECT_ROOT
            / "docs"
            / "release"
            / "phase2"
            / "activation-readiness.json"
        ).read_text(encoding="utf-8")
    )
    return state, created, memory_event, events, readiness


def _resolver(state, created, memory_event, events, readiness, *, disconnected=()):
    return CanonicalRuntimeReceiptResolver(
        run_id=state.run_id,
        task_id=state.task_id,
        events=(created, memory_event, *events),
        canonical_events=tuple(api.get_store().task_events(state.task_id)),
        communication_receipts=api._phase2_communication_outcomes(state),
        physical_dispatch_receipts=tuple(
            state.metadata["physical_dispatch_receipts"]
        ),
        symbolic_bundles=tuple(state.metadata["phase2_symbolic_bundles"]),
        readiness_report=readiness,
        decision_records=tuple(state.decisions),
        disconnected=disconnected,
    )


def test_production_receipts_drive_report_and_read_only_evidence_api(
    tmp_path,
) -> None:
    state, created, memory_event, events, readiness = _production_run()
    resolver = _resolver(state, created, memory_event, events, readiness)
    report = Phase2MetricReportBuilder().build(
        (
            RunMetricInput(
                run_id=state.run_id,
                task_id=state.task_id,
                scenario_id="production-main-path",
                mechanism_profile="phase2_strongest_v1",
                receipt_resolver=resolver,
                task_succeeded=True,
                effective_transition_count=(
                    resolver.canonical_transition_count()
                ),
            ),
        )
    )
    run = report.runs[0]
    assert run.source_admission["canonical_owner"] == (
        "RuntimeEventSpine+TaskState+ArtifactStore"
    )
    assert run.metrics[
        "dispatch.local_real_receipt_completeness"
    ].status is MetricStatus.OBSERVED
    assert run.metrics[
        "dispatch.local_real_receipt_completeness"
    ].value == 1.0
    assert run.metrics[
        "continuity.critical_fact_recall"
    ].status is MetricStatus.OBSERVED
    assert run.metrics["continuity.critical_fact_recall"].value == 1.0
    assert run.metrics["continuity.provenance_coverage"].value == 1.0
    report_id = "production-runtime-report"
    write_phase2_metric_report(
        report,
        root=tmp_path,
        report_id=report_id,
        run_id=state.run_id,
        task_id=state.task_id,
        admit_event=lambda event: api.persist_events(api.get_store(), [event]),
    )
    evidence_api = PolicyMetricApi(
        FilesystemPolicyMetricReportProvider(tmp_path),
        evidence_source=RuntimePolicyEvidenceSource(
            api.get_runtime_event_api(),
            api.artifact_root_path(),
        ),
    )
    response = evidence_api.route_get(
        ("policy", "evidence"),
        {
            "run_id": state.run_id,
            "task_id": state.task_id,
            "report_id": report_id,
            "limit": 200,
        },
    )
    assert response is not None and response.status == 200
    assert response.body["metric_report"]["status"] == "verified"
    transitions = response.body["transitions"]
    kinds = {item["contract_kind"] for item in transitions}
    assert {
        "topology_proposal_artifact",
        "policy_decision_receipt",
        "policy_outcome",
        "physical_dispatch_receipt",
        "memory_continuity_receipt",
        "neuro_symbolic_evidence_bundle",
    }.issubset(kinds)
    assert all(
        ref["route"]
        for item in transitions
        if item["integrity"] == "verified"
        for ref in item["causal_refs"]
    )
    physical = next(
        item
        for item in transitions
        if item["contract_kind"] == "physical_dispatch_receipt"
    )
    assert physical["execution"] == "real"
    assert physical["integrity"] == "verified"
    assert physical["details"]["physical_validation"][
        "real_gate_closed"
    ] is True
    direct_report = evidence_api.route_get(
        ("policy", "metrics", "reports", report_id),
        {},
    )
    assert direct_report is not None and direct_report.status == 200

    report_path = tmp_path / f"{report_id}.json"
    forged_report = json.loads(report_path.read_text(encoding="utf-8"))
    forged_run = forged_report["run_reports"][0]
    forged_metric = forged_run["metrics"][
        "dispatch.local_real_receipt_completeness"
    ]
    forged_metric["value"] = 0.123456
    forged_metric["numerator"] = 123456
    forged_report["aggregate_report"]["metrics"][
        "dispatch.local_real_receipt_completeness"
    ]["value"] = 0.123456
    unsigned_run = dict(forged_run)
    unsigned_run.pop("digest", None)
    forged_run["digest"] = canonical_digest(unsigned_run)
    unsigned_report = dict(forged_report)
    unsigned_report.pop("digest", None)
    forged_report["digest"] = canonical_digest(unsigned_report)
    report_path.write_text(
        json.dumps(forged_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    forged_response = evidence_api.route_get(
        ("policy", "metrics", "reports", report_id),
        {},
    )
    assert forged_response is not None and forged_response.status == 409
    assert forged_response.body["error"] == (
        "metric_report_source_owner_unresolved"
    )

    disconnected = _resolver(
        state,
        created,
        memory_event,
        events,
        readiness,
        disconnected=(PHYSICAL_DISPATCH_RECEIPTS,),
    )
    disconnected_report = Phase2MetricReportBuilder().build(
        (
            RunMetricInput(
                run_id=state.run_id,
                task_id=state.task_id,
                scenario_id="production-main-path",
                mechanism_profile="phase2_strongest_v1",
                receipt_resolver=disconnected,
                task_succeeded=True,
                effective_transition_count=(
                    disconnected.canonical_transition_count()
                ),
            ),
        )
    )
    assert disconnected_report.runs[0].metrics[
        "dispatch.causal_chain_completeness"
    ].status is MetricStatus.FAILED

    mutated = copy.deepcopy(state.metadata["physical_dispatch_receipts"][0])
    mutated["payload"]["input_signals"]["leased_worker_endpoint"] = (
        "http://127.0.0.1:1"
    )
    unsigned = dict(mutated)
    unsigned.pop("digest", None)
    mutated["digest"] = canonical_digest(unsigned)
    with pytest.raises(Phase2MetricError) as captured:
        CanonicalRuntimeReceiptResolver(
            run_id=state.run_id,
            task_id=state.task_id,
            events=(created, memory_event, *events),
            canonical_events=tuple(api.get_store().task_events(state.task_id)),
            communication_receipts=api._phase2_communication_outcomes(state),
            physical_dispatch_receipts=(mutated,),
            symbolic_bundles=tuple(state.metadata["phase2_symbolic_bundles"]),
            readiness_report=readiness,
            decision_records=tuple(state.decisions),
        ).resolve(PHYSICAL_DISPATCH_RECEIPTS)
    assert captured.value.code in {
        "metric_receipt_contract_invalid",
        "metric_physical_dispatch_gate_open",
    }

    resolver = _resolver(
        state,
        created,
        memory_event,
        events,
        readiness,
    )
    with pytest.raises(Phase2MetricError) as transition_error:
        Phase2MetricReportBuilder().build(
            (
                RunMetricInput(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    scenario_id="production-main-path",
                    mechanism_profile="phase2_strongest_v1",
                    receipt_resolver=resolver,
                    task_succeeded=True,
                    effective_transition_count=9_999,
                ),
            )
        )
    assert transition_error.value.code == "metric_transition_count_not_canonical"
