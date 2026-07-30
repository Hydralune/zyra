from __future__ import annotations

import json
from pathlib import Path

from apps.api.zyra_api.main import ZYRA_DYNAMIC_API_ROUTES
from apps.api.zyra_api.policy_api import (
    FilesystemPolicyMetricReportProvider,
    PolicyMetricApi,
    RuntimePolicyEvidenceSource,
)
from zyra_core import EventRecord, EventType
from zyra_evaluation.policy_benchmark.contracts import canonical_digest
from zyra_orchestration.topology_policy import (
    ContractHeader,
    FrozenDict,
    GraphSnapshotRef,
    PhysicalDispatchReceipt,
    PolicyEvidencePublisher,
    StableArtifactRef,
    TopologyOperation,
    TopologyOperationKind,
    TopologyProposalArtifact,
)
from zyra_runtime import LocalArtifactStore
from zyra_runtime.runtime_events import (
    RuntimeEventApiFacade,
    RuntimeEventSpineBridge,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = "2026-07-30T12:00:00Z"


def _header(contract_id: str, mechanism: str = "ARG") -> ContractHeader:
    return ContractHeader(
        contract_id=contract_id,
        created_at=NOW,
        source_event_id=f"source-{contract_id}",
        correlation_id="correlation-policy-evidence",
        causation_id=f"cause-{contract_id}",
        mechanism_id=mechanism,
        mechanism_version=f"{mechanism.lower()}-deterministic-v1",
        input_version="zyra.policy-input-snapshot/v1",
        idempotency_key=f"idempotency-{contract_id}",
        configuration_digest="c" * 64,
    )


def _ref(ref_id: str) -> StableArtifactRef:
    return StableArtifactRef(
        ref_id=ref_id,
        uri=f"artifact://{ref_id}",
        digest=canonical_digest({"ref_id": ref_id}),
    )


def _proposal(contract_id: str) -> TopologyProposalArtifact:
    return TopologyProposalArtifact(
        header=_header(contract_id),
        proposal_id=contract_id,
        input_snapshot_digest="a" * 64,
        base_graph=GraphSnapshotRef(
            graph_id="graph-policy-evidence",
            run_id="run-policy-evidence",
            revision=7,
            signature="b" * 64,
            commit_id="commit-before",
        ),
        operations=(
            TopologyOperation(
                kind=TopologyOperationKind.REPLACE_NODE,
                entity_id="worker-execution",
                value=FrozenDict(
                    {
                        "node_id": "worker-recovery",
                        "role": "recovery",
                        "capabilities": ["repair"],
                    }
                ),
                expected_entity_revision=3,
                required_permissions=("graph.mutate",),
                reason="real fault recovery",
            ),
        ),
        expected_outcome=FrozenDict({"recovery": True}),
        alternatives=(),
        reasons=("fault_detected",),
        constraint_assumptions=("permission_current",),
        expires_at="2026-07-30T12:05:00Z",
        fallback_profile="phase1_deterministic_baseline",
    )


def _dispatch(contract_id: str, *, simulated: bool) -> PhysicalDispatchReceipt:
    return PhysicalDispatchReceipt(
        header=_header(contract_id, "MaAS"),
        placement_decision_id=f"placement-{contract_id}",
        alternatives=("local", "edge", "cloud"),
        input_signals=FrozenDict({"privacy": "internal", "load": 0.2}),
        worker_manifest_ref=_ref(f"manifest-{contract_id}"),
        lease_id=f"lease-{contract_id}",
        physical_attempt_id=f"attempt-{contract_id}",
        physical_identity=FrozenDict(
            {
                "location": "edge",
                "worker_id": "worker-edge-1",
                "process_id": 4123,
            }
        ),
        call_receipt=_ref(f"call-{contract_id}"),
        artifact_ref=_ref(f"artifact-{contract_id}"),
        verifier_ref=_ref(f"verifier-{contract_id}"),
        privacy_class="internal",
        allowed_placements=("edge",),
        permission_ref=f"permission-{contract_id}",
        simulated=simulated,
        semantic_only=False,
        placement_reason="resource_scheduler_physical_dispatch",
        runtime_evidence=FrozenDict({"process_alive": True}),
    )


def _api(
    tmp_path: Path,
) -> tuple[
    PolicyMetricApi,
    RuntimeEventSpineBridge,
    LocalArtifactStore,
    Path,
]:
    event_root = tmp_path / "event-owner"
    artifact_root = tmp_path / "artifacts"
    report_root = tmp_path / "reports"
    report_root.mkdir()
    bridge = RuntimeEventSpineBridge.create(
        database_path=event_root / "events.sqlite3",
        artifact_root=event_root / "artifacts",
        workspace_root=PROJECT_ROOT,
    )
    source = RuntimePolicyEvidenceSource(
        RuntimeEventApiFacade(bridge),
        artifact_root,
    )
    return (
        PolicyMetricApi(
            FilesystemPolicyMetricReportProvider(report_root),
            evidence_source=source,
        ),
        bridge,
        LocalArtifactStore(artifact_root),
        report_root,
    )


def test_real_policy_artifacts_project_graph_dispatch_metrics_and_cursor(
    tmp_path: Path,
) -> None:
    api, bridge, artifacts, reports = _api(tmp_path)
    publisher = PolicyEvidencePublisher(
        artifacts,
        admit_event=lambda event: bridge.append_legacy_events([event]),
    )
    try:
        publisher.publish(
            _proposal("proposal-evidence"),
            run_id="run-policy-evidence",
            task_id="task-policy-evidence",
        )
        publisher.publish(
            _dispatch("dispatch-real", simulated=False),
            run_id="run-policy-evidence",
            task_id="task-policy-evidence",
        )
        publisher.publish(
            _dispatch("dispatch-simulated", simulated=True),
            run_id="run-policy-evidence",
            task_id="task-policy-evidence",
        )
        report_body = {
            "schema_version": "zyra.phase2-metric-report/v1",
            "registry_digest": "d" * 64,
            "run_reports": [],
            "scenario_reports": [],
            "mechanism_reports": [],
            "aggregate_report": {
                "metrics": {
                    "dispatch.causal_chain_completeness": {
                        "status": "observed",
                        "value": 1.0,
                        "source_refs": ["e" * 64],
                    }
                }
            },
            "requirement_metrics": {},
            "anti_gaming": {
                "simulated_dispatch_excluded_from_real_numerator": True,
            },
        }
        (reports / "report-real.json").write_text(
            json.dumps(
                {
                    **report_body,
                    "digest": canonical_digest(report_body),
                }
            ),
            encoding="utf-8",
        )

        first = api.route_get(
            ("policy", "evidence"),
            {
                "task_id": "task-policy-evidence",
                "report_id": "report-real",
                "limit": "2",
            },
        )
        assert first is not None
        assert first.status == 200
        assert first.body["has_more"] is True
        assert first.body["metric_report"]["status"] == "verified"
        assert first.body["metric_report"]["digest"]
        proposal = next(
            item
            for item in first.body["transitions"]
            if item["contract_kind"] == "topology_proposal_artifact"
        )
        assert proposal["graph_diff"] == [
            _proposal("proposal-evidence").operations[0].to_dict()
        ]
        assert any(
            item["kind"] == "commit" and item["id"] == "commit-before"
            for item in proposal["causal_refs"]
        )

        second = api.route_get(
            ("policy", "evidence"),
            {
                "task_id": "task-policy-evidence",
                "report_id": "report-real",
                "limit": "2",
                "cursor": first.body["next_cursor"],
            },
        )
        assert second is not None
        dispatches = [
            item
            for item in [*first.body["transitions"], *second.body["transitions"]]
            if item["contract_kind"] == "physical_dispatch_receipt"
        ]
        assert {item["execution"] for item in dispatches} == {"real", "simulated"}
        simulated = next(item for item in dispatches if item["execution"] == "simulated")
        assert simulated["integrity"] == "verified"
        assert any(item["kind"] == "attempt" for item in simulated["causal_refs"])

        wrong_scope = api.route_get(
            ("policy", "evidence"),
            {
                "task_id": "task-policy-evidence",
                "receipt_id": "dispatch-real",
                "limit": "2",
                "cursor": first.body["next_cursor"],
            },
        )
        assert wrong_scope is not None
        assert wrong_scope.status == 409
        assert wrong_scope.body["error"] == "policy_evidence_cursor_scope_mismatch"
        assert ("GET", "/policy/evidence") in ZYRA_DYNAMIC_API_ROUTES
    finally:
        bridge.close()


def test_tamper_and_adapter_disconnect_are_not_rendered_as_success(
    tmp_path: Path,
) -> None:
    api, bridge, artifacts, _ = _api(tmp_path)
    publisher = PolicyEvidencePublisher(
        artifacts,
        admit_event=lambda event: bridge.append_legacy_events([event]),
    )
    published = publisher.publish(
        _proposal("proposal-tampered"),
        run_id="run-policy-evidence",
        task_id="task-policy-evidence",
    )
    artifacts.resolve_path(published.artifact).write_text("{}", encoding="utf-8")
    degraded = api.route_get(
        ("policy", "evidence"),
        {"task_id": "task-policy-evidence"},
    )
    assert degraded is not None
    assert degraded.status == 200
    assert degraded.body["status"] == "degraded"
    assert degraded.body["transitions"][0]["integrity"] == "inconsistent"
    assert degraded.body["transitions"][0]["execution"] == "degraded"

    bridge.close()
    disconnected = api.route_get(
        ("policy", "evidence"),
        {"task_id": "task-policy-evidence"},
    )
    assert disconnected is not None
    assert disconnected.status == 503
    assert disconnected.body["error"] == "policy_evidence_adapter_disconnected"


def test_task_drilldown_cursor_terminates_across_unrelated_global_events(
    tmp_path: Path,
) -> None:
    api, bridge, _, _ = _api(tmp_path)
    try:
        bridge.append_legacy_events(
            [
                EventRecord(
                    run_id="run-policy-focus",
                    task_id="task-policy-focus",
                    event_id="event-loopx-focus",
                    event_type=EventType.ARTIFACT_WRITTEN,
                    payload={
                        "schema": "zyra.loopx-sync-status/v1",
                        "receipt_id": "loopx-focus",
                        "mechanism_version": "0.2.13",
                        "status": "acked",
                    },
                ),
                *[
                    EventRecord(
                        run_id="run-unrelated",
                        task_id="task-unrelated",
                        event_id=f"event-loopx-unrelated-{index}",
                        event_type=EventType.ARTIFACT_WRITTEN,
                        payload={
                            "schema": "zyra.loopx-sync-status/v1",
                            "receipt_id": f"loopx-unrelated-{index}",
                            "mechanism_version": "0.2.13",
                            "status": "acked",
                        },
                    )
                    for index in range(4)
                ],
            ]
        )
        first = api.route_get(
            ("policy", "evidence"),
            {"task_id": "task-policy-focus", "limit": "1"},
        )
        assert first is not None
        assert first.status == 200
        assert first.body["transition_count"] == 1
        assert first.body["has_more"] is True

        final = api.route_get(
            ("policy", "evidence"),
            {
                "task_id": "task-policy-focus",
                "limit": "1",
                "cursor": first.body["next_cursor"],
            },
        )
        assert final is not None
        assert final.status == 200
        assert final.body["transition_count"] == 0
        assert final.body["has_more"] is False
        assert final.body["next_cursor"] == ""
    finally:
        bridge.close()


def test_more_than_two_thousand_real_spine_transitions_stay_incremental(
    tmp_path: Path,
) -> None:
    api, bridge, _, _ = _api(tmp_path)
    try:
        events = [
            EventRecord(
                run_id="run-policy-scale",
                task_id="task-policy-scale",
                event_id=f"event-loopx-scale-{index:04d}",
                event_type=EventType.ARTIFACT_WRITTEN,
                payload={
                    "schema": "zyra.loopx-sync-status/v1",
                    "receipt_id": f"loopx-receipt-{index:04d}",
                    "mechanism_version": "0.2.13",
                    "status": "acked",
                },
            )
            for index in range(2_105)
        ]
        for offset in range(0, len(events), 250):
            bridge.append_legacy_events(events[offset : offset + 250])

        cursor = ""
        observed = 0
        pages = 0
        snapshot_digest = ""
        while True:
            response = api.route_get(
                ("policy", "evidence"),
                {
                    "task_id": "task-policy-scale",
                    "limit": "200",
                    "cursor": cursor,
                },
            )
            assert response is not None
            assert response.status == 200
            assert response.body["transition_count"] <= 200
            if snapshot_digest:
                assert response.body["snapshot_digest"] == snapshot_digest
            snapshot_digest = str(response.body["snapshot_digest"])
            observed += int(response.body["transition_count"])
            pages += 1
            cursor = str(response.body["next_cursor"])
            if not response.body["has_more"]:
                break
        assert observed == 2_105
        assert pages == 11
    finally:
        bridge.close()
