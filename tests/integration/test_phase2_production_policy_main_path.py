from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from apps.api.zyra_api import main as api
from zyra_core import EventRecord, EventType, PlanNodeStatus, now_iso
from zyra_orchestration import ensure_default_graph, run_task_graph
from zyra_orchestration.topology_policy import production as production_policy
from zyra_orchestration.topology_policy.contracts import (
    FrozenDict,
    PhysicalDispatchReceipt,
    canonical_digest,
)
from zyra_orchestration.topology_policy.production import (
    Phase2StrongestProductionBridge,
)
from zyra_orchestration.topology_policy.pruning import (
    CommunicationEdgeType,
)
from zyra_symbolic import TopologyRouter
from zyra_scheduler import (
    OperatorLayerProposal,
    PhysicalDispatchReceiptValidator,
)


LIVE_PROVIDER_REQUIRED = pytest.mark.skipif(
    os.environ.get("ZYRA_RUN_LIVE_PROVIDER_TESTS") != "1",
    reason=(
        "set ZYRA_RUN_LIVE_PROVIDER_TESTS=1 only with explicit authorization "
        "to send the test goal to the configured paid provider"
    ),
)


def _assert_physical_graph_binding_terminal(state) -> None:
    pool_api = api.get_worker_pool_api()
    projection = state.metadata["worker_pool"]
    graph = pool_api.graph_custody.current(
        state.metadata["dynamic_graph_id"]
    )
    execute_node = next(
        node
        for node in graph.nodes
        if node.physical_attempt_ref == projection["attempt_id"]
        and node.worker_lease_ref == projection["lease_id"]
    )
    assert execute_node.terminal is True
    assert any(
        item["attempt_id"] == projection["attempt_id"]
        and item["lease_id"] == projection["lease_id"]
        and item["committed"] is True
        for item in state.metadata["worker_pool_graph_terminal_history"]
    )


def test_loopx_pre_control_denial_stops_before_graph_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, created = api.make_task_created_event(
        "Deny LoopX control before any canonical graph mutation."
    )
    denied = {
        "schema": "zyra.phase2-policy-permission-receipt/v1",
        "run_id": state.run_id,
        "task_id": state.task_id,
        "decision_id": "decision-loopx-denied",
        "canonical_owner": "typescript.PermissionCoordinator",
        "effect": "deny",
        "allowed_permissions": [],
    }
    denied["receipt_digest"] = canonical_digest(denied)
    monkeypatch.setattr(
        api,
        "_phase2_permission_decision",
        lambda *_args, **_kwargs: denied,
    )
    monkeypatch.setattr(
        api,
        "get_worker_pool_api",
        lambda: (_ for _ in ()).throw(
            AssertionError("graph owner must not run after permission denial")
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="canonical permission owner denied LoopX pre-control",
    ):
        api.prepare_phase2_loopx_pre_control(
            state,
            causation_id=created.event_id,
        )


def test_loopx_pre_control_replays_valid_task_bound_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _ = api.make_task_created_event("Replay valid LoopX pre-control.")
    permission = {
        "decision_id": "decision-loopx-replay",
        "canonical_owner": "typescript.PermissionCoordinator",
        "effect": "allow",
        "allowed_permissions": ["graph.write", "worker.dispatch"],
        "valid_until": (
            datetime.now(UTC) + timedelta(minutes=5)
        ).isoformat().replace("+00:00", "Z"),
    }
    permission["receipt_digest"] = canonical_digest(permission)
    canonical_commit = {"receipt": {"status": "committed"}}
    receipt = {
        "schema": "zyra.phase2-production-loopx-pre-control/v1",
        "run_id": state.run_id,
        "task_id": state.task_id,
        "canonical_commit": canonical_commit,
        "canonical_commit_digest": canonical_digest(canonical_commit),
        "permission_receipt": permission,
        "continuation": {"allowed": True},
        "checks": {"canonical_commit": True, "permission_allowed": True},
    }
    receipt["receipt_digest"] = canonical_digest(receipt)
    state.metadata["phase2_loopx_pre_control"] = dict(receipt)
    monkeypatch.setattr(
        api,
        "_phase2_permission_decision",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("valid pre-control must replay before owner mutation")
        ),
    )

    replayed = api.prepare_phase2_loopx_pre_control(
        state,
        causation_id="replayed-cause",
    )

    assert replayed == receipt

    expired_permission = dict(permission)
    expired_permission["valid_until"] = "2000-01-01T00:00:00Z"
    expired_permission.pop("receipt_digest")
    expired_permission["receipt_digest"] = canonical_digest(
        expired_permission
    )
    expired = dict(receipt)
    expired["permission_receipt"] = expired_permission
    expired.pop("receipt_digest")
    expired["receipt_digest"] = canonical_digest(expired)
    state.metadata["phase2_loopx_pre_control"] = expired
    monkeypatch.setattr(
        api,
        "_phase2_permission_decision",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("fresh permission requested")
        ),
    )
    with pytest.raises(RuntimeError, match="fresh permission requested"):
        api.prepare_phase2_loopx_pre_control(
            state,
            causation_id="stale-replay-cause",
        )


@pytest.mark.parametrize(
    ("location", "backend_kind", "expected"),
    (
        ("local", "local_process", "local_process"),
        ("edge", "isolated_process", "isolated_process"),
        ("cloud", "cloud_model", "cloud_model"),
    ),
)
def test_dynamic_physical_manifest_preserves_real_backend_kind(
    location: str,
    backend_kind: str,
    expected: str,
) -> None:
    selected = api._scheduler_backend_from_physical_manifest(
        SimpleNamespace(
            location=SimpleNamespace(value=location),
            backend_kinds=(backend_kind,),
        )
    )

    assert selected.value == expected


@pytest.mark.parametrize(
    ("location", "backend_kinds"),
    (
        ("edge", ()),
        ("local", ("sandbox_gateway",)),
        ("edge", ("sandbox_gateway",)),
        ("edge", ("local_process",)),
        ("cloud", ("cloud_model", "isolated_process")),
        ("edge", ("simulated_edge",)),
    ),
)
def test_dynamic_physical_manifest_rejects_ambiguous_or_false_backend(
    location: str,
    backend_kinds: tuple[str, ...],
) -> None:
    with pytest.raises(RuntimeError, match="dynamic physical manifest"):
        api._scheduler_backend_from_physical_manifest(
            SimpleNamespace(
                location=SimpleNamespace(value=location),
                backend_kinds=backend_kinds,
            )
        )


def test_partial_communication_outcome_coverage_requires_new_window() -> None:
    candidates = (
        SimpleNamespace(edge_id="edge-a"),
        SimpleNamespace(edge_id="edge-b"),
    )
    partial = (SimpleNamespace(edge_id="edge-a"),)
    complete = (
        SimpleNamespace(edge_id="edge-a"),
        SimpleNamespace(edge_id="edge-b"),
    )

    assert Phase2StrongestProductionBridge._communication_coverage_complete(
        candidates=candidates,
        observations=partial,
    ) is False
    assert Phase2StrongestProductionBridge._communication_coverage_complete(
        candidates=candidates,
        observations=complete,
    ) is True

    assert Phase2StrongestProductionBridge._communication_outcome_is_prior_and_fresh(
        completed_at="2026-08-01T00:59:59Z",
        completed_before="2026-08-01T01:00:00Z",
        maximum_age_seconds=3600,
    ) is True
    assert Phase2StrongestProductionBridge._communication_outcome_is_prior_and_fresh(
        completed_at="2026-07-31T23:59:59Z",
        completed_before="2026-08-01T01:00:00Z",
        maximum_age_seconds=3600,
    ) is False
    assert Phase2StrongestProductionBridge._communication_outcome_is_prior_and_fresh(
        completed_at="2026-08-01T01:00:00Z",
        completed_before="2026-08-01T01:00:00Z",
        maximum_age_seconds=3600,
    ) is False


def test_completion_failures_exclude_adaptive_depth_only_diagnostics() -> None:
    conditions = {
        "final_verifier_passed": True,
        "required_artifacts_complete": True,
        "confidence_and_cost_benefit": False,
    }
    required = {
        "final_verifier_passed",
        "required_artifacts_complete",
    }

    assert production_policy._failed_completion_conditions(
        conditions,
        required,
    ) == ()
    assert production_policy._failed_completion_conditions(
        {**conditions, "final_verifier_passed": False},
        required,
    ) == ("final_verifier_passed",)


def _communication_candidate() -> SimpleNamespace:
    return SimpleNamespace(
        edge_id="edge-communication-window",
        source_node_id="node-source",
        target_node_id="node-target",
        edge_type=SimpleNamespace(value="spatial"),
    )


def _communication_window(state, identifier: str) -> EventRecord:
    return EventRecord(
        event_id=identifier,
        run_id=state.run_id,
        task_id=state.task_id,
        event_type=EventType.RESOURCE_DECISION,
        payload={
            "schema": "zyra.phase2-temporal-handoff-receipt/v1",
            "acknowledged": True,
            "checkpoint_ref": f"checkpoint:{identifier}",
        },
    )


def _persist_legacy_unscoped_communication_outcome(
    state,
    candidate: SimpleNamespace,
    cause_event: EventRecord,
) -> str:
    api.persist_events(api.get_store(), [cause_event])
    edge_type = candidate.edge_type.value
    message_payload = {
        "schema": "zyra.phase2-topology-coordination-message/v1",
        "run_id": state.run_id,
        "task_id": state.task_id,
        "edge_id": candidate.edge_id,
        "source_node_id": candidate.source_node_id,
        "target_node_id": candidate.target_node_id,
        "edge_type": edge_type,
        "intent": "topology_coordination",
        "state_delta": {
            "handoff_ref": cause_event.event_id,
            "candidate_edge_id": candidate.edge_id,
        },
        "evidence_refs": [cause_event.event_id],
        "artifact_refs": [],
    }
    message_digest = canonical_digest(message_payload)
    message_id = "event_phase2_message_" + canonical_digest(
        (state.run_id, candidate.edge_id, cause_event.event_id)
    )[:24]
    api.persist_events(
        api.get_store(),
        [
            EventRecord(
                event_id=message_id,
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    **message_payload,
                    "payload_digest": message_digest,
                },
            )
        ],
    )
    outcome_id = "event_agentprune_outcome_" + canonical_digest(
        (message_id, message_digest, True)
    )[:24]
    completed_at = now_iso()
    legacy = {
        "schema_version": "zyra.agentprune-communication-outcome/v1",
        "observation_id": "observation:" + outcome_id,
        "window_id": "topology-route:" + cause_event.event_id,
        "completed_at": completed_at,
        "edge_id": candidate.edge_id,
        "source_node_id": candidate.source_node_id,
        "target_node_id": candidate.target_node_id,
        "edge_type": edge_type,
        "message_id": message_id,
        "payload_digest": message_digest,
        "delivered": True,
        "delivery_receipt_ref": message_id,
    }
    legacy["digest"] = canonical_digest(legacy)
    api.persist_events(
        api.get_store(),
        [
            EventRecord(
                event_id=outcome_id,
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.RESOURCE_DECISION,
                created_at=completed_at,
                payload=legacy,
            )
        ],
    )
    return outcome_id


def test_communication_outcome_recorder_refreshes_edge_in_new_window() -> None:
    state, _ = api.make_task_created_event(
        "Refresh a stable communication edge with a fresh actual window."
    )
    candidate = _communication_candidate()

    first = api._record_phase2_communication_outcomes(
        state,
        (candidate,),
        _communication_window(state, "event-window-first"),
    )
    second = api._record_phase2_communication_outcomes(
        state,
        (candidate,),
        _communication_window(state, "event-window-second"),
    )

    assert len(first) == 1
    assert len(second) == 2
    assert {item["window_id"] for item in second} == {
        "topology-route:event-window-first",
        "topology-route:event-window-second",
    }
    assert len({item["message_id"] for item in second}) == 2
    assert all(item["run_id"] == state.run_id for item in second)
    assert all(item["task_id"] == state.task_id for item in second)


def test_scoped_outcome_replaces_legacy_identity_in_same_window() -> None:
    state, _ = api.make_task_created_event(
        "Replace an unscoped legacy communication outcome without collision."
    )
    candidate = _communication_candidate()
    cause_event = _communication_window(state, "event-window-legacy")
    legacy_id = _persist_legacy_unscoped_communication_outcome(
        state,
        candidate,
        cause_event,
    )
    assert api._phase2_communication_outcomes(state) == ()

    refreshed = api._record_phase2_communication_outcomes(
        state,
        (candidate,),
        cause_event,
    )

    assert len(refreshed) == 1
    assert refreshed[0]["run_id"] == state.run_id
    assert refreshed[0]["task_id"] == state.task_id
    assert refreshed[0]["observation_id"] != "observation:" + legacy_id


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    (
        ("foreign_scope", "scope mismatch"),
        ("missing_delivery", "delivery receipt is missing"),
        ("edge_binding", "outcome/message binding mismatch"),
    ),
)
def test_communication_outcome_provider_rejects_forged_binding(
    mutation: str,
    expected_error: str,
) -> None:
    state, _ = api.make_task_created_event(
        "Reject forged communication outcome bindings."
    )
    recorded = api._record_phase2_communication_outcomes(
        state,
        (_communication_candidate(),),
        _communication_window(state, "event-window-valid"),
    )
    forged = dict(recorded[0])
    forged["completed_at"] = now_iso()
    forged_event_id = f"event-forged-{mutation}"
    forged["observation_id"] = "observation:" + forged_event_id
    if mutation == "foreign_scope":
        forged["run_id"] = "run-foreign"
        forged["task_id"] = "task-foreign"
    elif mutation == "missing_delivery":
        forged["message_id"] = "event-message-does-not-exist"
        forged["delivery_receipt_ref"] = forged["message_id"]
    else:
        forged["source_node_id"] = "node-foreign-source"
    forged.pop("digest", None)
    forged["digest"] = canonical_digest(forged)
    api.persist_events(
        api.get_store(),
        [
            EventRecord(
                event_id=forged_event_id,
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.RESOURCE_DECISION,
                created_at=forged["completed_at"],
                payload=forged,
            )
        ],
    )

    with pytest.raises(RuntimeError, match=expected_error):
        api._phase2_communication_outcomes(state)


@pytest.mark.parametrize(
    ("raw_override", "expected_error"),
    (
        ({"run_id": "run-foreign"}, "scope differs"),
        ({"source_node_id": "node-foreign"}, "identity and endpoints differ"),
    ),
)
def test_communication_projection_does_not_launder_receipt_identity(
    raw_override: dict[str, str],
    expected_error: str,
) -> None:
    state, _ = api.make_task_created_event(
        "Reject communication receipt identity laundering."
    )
    candidate = SimpleNamespace(
        edge_id="edge-bound",
        source_node_id="node-source",
        target_node_id="node-target",
        edge_type=CommunicationEdgeType.SPATIAL,
    )
    raw = {
        "run_id": state.run_id,
        "task_id": state.task_id,
        "edge_id": candidate.edge_id,
        "source_node_id": candidate.source_node_id,
        "target_node_id": candidate.target_node_id,
        "edge_type": candidate.edge_type.value,
    }
    raw.update(raw_override)
    bridge = object.__new__(Phase2StrongestProductionBridge)
    bridge.communication_outcome_provider = lambda _state: (raw,)

    with pytest.raises(RuntimeError, match=expected_error):
        bridge._communication_observations(
            state=state,
            candidates=(candidate,),
            current_window_id="topology-route:event-current-window",
            completed_before="2026-08-01T01:00:00Z",
            maximum_age_seconds=3600,
        )


def test_communication_projection_requires_exact_edge_and_prior_window() -> None:
    state, _ = api.make_task_created_event(
        "Consume only exact-edge receipts from a completed prior window."
    )
    candidate = SimpleNamespace(
        edge_id="edge-current",
        source_node_id="node-source",
        target_node_id="node-target",
        edge_type=CommunicationEdgeType.SPATIAL,
    )
    raw = {
        "run_id": state.run_id,
        "task_id": state.task_id,
        "edge_id": "edge-old-different-semantics",
        "source_node_id": candidate.source_node_id,
        "target_node_id": candidate.target_node_id,
        "edge_type": candidate.edge_type.value,
        "window_id": "topology-route:event-prior-window",
    }
    bridge = object.__new__(Phase2StrongestProductionBridge)
    bridge.communication_outcome_provider = lambda _state: (raw,)
    assert bridge._communication_observations(
        state=state,
        candidates=(candidate,),
        current_window_id="topology-route:event-current-window",
        completed_before="2026-08-01T01:00:00Z",
        maximum_age_seconds=3600,
    ) == ()

    recorded_candidate = _communication_candidate()
    cause_event = _communication_window(state, "event-same-window")
    recorded = api._record_phase2_communication_outcomes(
        state,
        (recorded_candidate,),
        cause_event,
    )
    bridge.communication_outcome_provider = lambda _state: recorded
    assert bridge._communication_observations(
        state=state,
        candidates=(recorded_candidate,),
        current_window_id="topology-route:event-same-window",
        completed_before="2099-01-01T00:00:00Z",
        maximum_age_seconds=0,
    ) == ()
    assert len(
        bridge._communication_observations(
            state=state,
            candidates=(recorded_candidate,),
            current_window_id="topology-route:event-next-window",
            completed_before="2099-01-01T00:00:00Z",
            maximum_age_seconds=0,
        )
    ) == 1


@LIVE_PROVIDER_REQUIRED
def test_direct_response_goal_completes_with_exact_answer_and_bound_receipt(
) -> None:
    state, created = api.make_task_created_event("测试，收到请回复ok")
    workspace = api.get_workspace_manager().create_for_task(
        run_id=state.run_id,
        task_id=state.task_id,
        session_id=f"task:{state.task_id}",
        worker_id="task-runtime",
        idempotency_key=f"direct-response:{state.task_id}",
        causation_id=created.event_id,
    )
    state.metadata["workspace_ref"] = workspace.projection.to_dict()
    ensure_default_graph(state)

    events = run_task_graph(
        state,
        execution_context=api.graph_execution_context(),
    )

    assert state.status is PlanNodeStatus.COMPLETED
    assert state.metadata["interaction_mode"] == "direct_response"
    assert state.metadata["final_answer"] == "ok"
    assert state.metadata["goal_contract_verification"]["passed"] is True
    assert len(state.artifacts) == 1
    artifact = state.artifacts[0]
    assert artifact.title == "Physical CodeWorker delivery manifest"
    artifact_payload = json.loads(b"".join(
        api.get_worker_pool_api().artifact_store.iter_bytes(artifact)
    ).decode("utf-8"))
    assert artifact_payload["final_text"] == "ok"
    assert artifact_payload["provider_request_ids"]
    assert artifact_payload["workspace"]["workspace_id"] == (
        workspace.projection.workspace_id
    )
    assert state.metadata["delivery"] == {
        "schema": "zyra.task-workspace-delivery/v1",
        "workspace_id": workspace.projection.workspace_id,
        "created_paths": [],
        "modified_paths": [],
        "deleted_paths": [],
        "changed_paths": [],
        "physical_location_redacted": True,
    }

    receipt = state.metadata["worker_pool_receipt"]
    signals = receipt["physical_dispatch_receipt"]["payload"]["input_signals"]
    assert signals["operator_adapter_id"] == (
        "worker.code-worker.typescript-provider-tool-loop"
    )
    assert signals["domain_result"]["kind"] == "code_worker_execution"
    assert signals["final_text"] == "ok"
    provider = receipt["physical_dispatch_receipt"]["payload"][
        "provider_evidence"
    ]
    assert provider["task_execution_verified"] is True
    assert provider["prompt_goal_bound"] is True
    assert provider["synthetic_usage"] is False
    assert receipt["physical_dispatch_validation"]["real_gate_closed"] is True
    final_verifier = next(
        item
        for item in reversed(state.decisions)
        if item.decision_type == "final_verifier"
    )
    assert final_verifier.selected == "passed"
    assert all(item["passed"] for item in final_verifier.checks)
    assert any(
        item.event_type is EventType.EVALUATION
        and item.payload.get("passed") is True
        for item in events
    )


@LIVE_PROVIDER_REQUIRED
def test_file_delivery_goal_writes_exact_workspace_file_and_bound_receipt(
) -> None:
    goal = "建一个 smoke.txt 文件，内容是一行 ZYRA_SMOKE_OK"
    state, created = api.make_task_created_event(goal)
    workspace_manager = api.get_workspace_manager()
    session_id = f"task:{state.task_id}"
    workspace = workspace_manager.create_for_task(
        run_id=state.run_id,
        task_id=state.task_id,
        session_id=session_id,
        worker_id="task-runtime",
        idempotency_key=f"file-delivery:{state.task_id}",
        causation_id=created.event_id,
    )
    state.metadata["query_session_id"] = session_id
    state.metadata["workspace_ref"] = workspace.projection.to_dict()
    ensure_default_graph(state)

    events = run_task_graph(
        state,
        execution_context=api.graph_execution_context(),
    )

    verifier_access = workspace_manager.acquire_for_worker(
        task_id=state.task_id,
        session_id=session_id,
        worker_id=f"live-verifier:{state.task_id}",
    )
    smoke = workspace_manager.internal_task_root(verifier_access) / "smoke.txt"
    observed = smoke.read_text(encoding="utf-8")
    assert observed in {"ZYRA_SMOKE_OK", "ZYRA_SMOKE_OK\n", "ZYRA_SMOKE_OK\r\n"}
    assert state.status is PlanNodeStatus.COMPLETED
    assert state.metadata["delivery"]["workspace_id"] == (
        workspace.projection.workspace_id
    )
    assert state.metadata["delivery"]["created_paths"] == ["smoke.txt"]
    assert state.metadata["delivery"]["changed_paths"] == ["smoke.txt"]

    receipt = state.metadata["worker_pool_receipt"]
    physical = receipt["physical_dispatch_receipt"]["payload"]
    signals = physical["input_signals"]
    provider = physical["provider_evidence"]
    assert signals["operator_adapter_id"] == (
        "worker.code-worker.typescript-provider-tool-loop"
    )
    assert signals["workspace_delta"]["created"] == ["smoke.txt"]
    assert provider["provider_called"] is True
    assert provider["task_execution_verified"] is True
    assert provider["prompt_goal_bound"] is True
    assert provider["synthetic_usage"] is False
    assert provider["calls"]
    assert receipt["physical_dispatch_validation"]["real_gate_closed"] is True
    final_verifier = next(
        item
        for item in reversed(state.decisions)
        if item.decision_type == "final_verifier"
    )
    assert final_verifier.selected == "passed"
    assert all(item["passed"] for item in final_verifier.checks)
    assert any(
        item.event_type is EventType.EVALUATION
        and item.payload.get("passed") is True
        for item in events
    )


@LIVE_PROVIDER_REQUIRED
def test_api_composition_root_runs_strongest_and_binds_scheduler_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, created = api.make_task_created_event(
        "Implement a code artifact and verify the result."
    )
    workspace = api.get_workspace_manager().create_for_task(
        run_id=state.run_id,
        task_id=state.task_id,
        session_id=f"task:{state.task_id}",
        worker_id="task-runtime",
        idempotency_key=f"production-main:{state.task_id}",
        causation_id=created.event_id,
    )
    state.metadata["workspace_ref"] = workspace.projection.to_dict()
    ensure_default_graph(state)
    context = api.graph_execution_context()
    events = run_task_graph(state, execution_context=context)
    route_events = [
        item
        for item in events
        if item.event_type is EventType.TOPOLOGY_ROUTE
        and "used_baseline" in (item.payload.get("topology_policy") or {})
    ]
    assert route_events, [
        {
            "type": str(item.event_type),
            "keys": sorted(item.payload),
            "schema": item.payload.get("schema"),
            "status": item.payload.get("status"),
            "message": item.payload.get("message"),
            "policy_keys": sorted(
                (item.payload.get("topology_policy") or {}).keys()
            ),
        }
        for item in events
        if str(item.event_type) in {"topology_route", "system_notice"}
    ]
    event = route_events[-1]
    decision = [
        item
        for item in state.decisions
        if item.decision_type == "worker_route"
    ][-1]

    policy = event.payload["topology_policy"]
    assert "physical_placement" in policy, {
        "topology_error": state.metadata.get("topology_policy_error"),
        "candidate_types": [
            item.get("edge_type")
            for item in policy.get("communication_candidate_edges") or ()
        ],
        "outcome_types": [
            item.get("edge_type")
            for item in api._phase2_communication_outcomes(state)
        ],
        "scheduler_error": state.metadata.get("resource_scheduler_error"),
        "policy": policy,
        "degraded_reason": policy.get("execution_receipt", {}).get(
            "degraded_reason"
        ),
        "topology_degraded_reason": policy.get("topology_result", {}).get(
            "degraded_reason"
        ),
    }
    binding = policy["physical_placement"]
    assert route_events[0].payload["topology_policy"]["used_baseline"] is True
    assert route_events[0].payload["topology_policy"]["reroute_required"] is True
    assert policy["used_baseline"] is False, repr(
        {
            "degraded_reason": policy.get("degraded_reason"),
            "execution_degraded_reason": (
                policy.get("execution_receipt") or {}
            ).get("degraded_reason"),
            "topology_degraded_reason": (
                policy.get("topology_result") or {}
            ).get("degraded_reason"),
            "topology_result": policy.get("topology_result"),
            "outcome_count": policy.get("communication_outcome_count"),
            "topology_error": state.metadata.get("topology_policy_error"),
            "candidate_types": [
                item.get("edge_type")
                for item in policy.get("communication_candidate_edges") or ()
            ],
            "outcome_types": [
                item.get("edge_type")
                for item in api._phase2_communication_outcomes(state)
            ],
            "graph_metadata": dict(
                api.get_worker_pool_api().graph_custody.current(
                    str(state.metadata.get("dynamic_graph_id"))
                ).metadata
            ),
        }
    )
    assert policy["committed"] is True
    prior_outcomes = api._phase2_communication_outcomes(state)
    current_policy_source = policy["topology_result"]["scheduler_causal_refs"][0]
    current_policy_created_at = policy["topology_result"]["decision_receipt"][
        "created_at"
    ]
    assert prior_outcomes
    assert all(
        item["window_id"] != f"topology-route:{current_policy_source}"
        and item["completed_at"] < current_policy_created_at
        for item in prior_outcomes
    )
    assert policy["operator_selection"]["mode"] == "default", {
        "degraded": policy["operator_selection"].get("degraded"),
        "degraded_reason": policy["operator_selection"].get(
            "degraded_reason"
        ),
        "readiness": policy["operator_selection"].get("readiness"),
    }
    assert policy["operator_candidate_set"]["candidate_set_digest"]
    assert decision.metadata["operator_candidate_contract_consumed"] == "true"
    assert decision.metadata["physical_lease_ref"] == binding["lease_id"]
    assert binding["resource_decision_id"] == (
        event.payload["resource_decision"]["decision_id"]
    )
    assert binding["topology_commit_id"]
    assert binding["physical_binding_commit_id"]
    assert binding["topology_commit_id"] != binding[
        "physical_binding_commit_id"
    ]
    assert binding["causation_order"] == [
        "operator_candidate_set",
        "resource_decision",
        "worker_lease",
        "physical_attempt",
    ]
    lease = api.get_worker_pool_api().pool.store.require_lease(
        binding["lease_id"]
    )
    assert lease.task_id == state.task_id
    assert lease.run_id == state.run_id
    assert lease.terminal is True, [
        {
            "type": str(item.event_type),
            "error": item.payload.get("error"),
            "message": item.payload.get("message"),
            "error_code": item.payload.get("error_code"),
            "node_details": (
                (item.payload.get("error_metadata") or {})
                .get("node_error", {})
                .get("details", {})
            ),
        }
        for item in events
        if item.event_type in {EventType.SYSTEM_NOTICE, EventType.NODE_FAILED}
    ]
    assert "worker_pool_receipt" in state.metadata, repr([
        {
            "type": str(item.event_type),
            "error": item.payload.get("error"),
            "error_code": item.payload.get("error_code"),
            "failed_execution_checks": (
                item.payload.get("error_metadata") or {}
            ).get("failed_execution_checks"),
            "failure_outcome": (
                (item.payload.get("error_metadata") or {}).get(
                    "physical_execution_failure_receipt"
                )
                or {}
            ).get("outcome"),
            "physical_dispatch_error": (
                item.payload.get("error_metadata") or {}
            ).get("physical_dispatch_error"),
        }
        for item in events
        if item.event_type in {EventType.SYSTEM_NOTICE, EventType.NODE_FAILED}
    ])
    physical_receipt = state.metadata["worker_pool_receipt"]
    assert physical_receipt["outcome"] == "succeeded"
    assert physical_receipt["receipt_id"]
    dispatch_receipt = physical_receipt["physical_dispatch_receipt"]
    dispatch_validation = physical_receipt["physical_dispatch_validation"]
    dispatch_payload = dispatch_receipt["payload"]
    dispatch_signals = dispatch_payload["input_signals"]
    assert dispatch_receipt["schema_version"] == (
        "zyra.physical-dispatch-receipt/v2"
    )
    assert dispatch_receipt["payload"]["simulated"] is False
    assert dispatch_receipt["payload"]["semantic_only"] is False
    assert dispatch_receipt["payload"]["physical_identity"]["location"] == "cloud"
    assert dispatch_signals["workload_operation"] == "phase2-operator-execution"
    assert dispatch_signals["operator_ref"]
    assert dispatch_signals["layer_index"] == 1
    assert dispatch_payload["placement_decision_id"] == binding[
        "resource_decision_id"
    ]
    assert dispatch_payload["lease_id"] == binding["lease_id"]
    assert dispatch_payload["physical_attempt_id"] == binding["attempt_id"]
    assert dispatch_signals["worker_id"] == binding["worker_id"]
    assert dispatch_signals["leased_worker_process_identity"] == (
        binding["worker_process_identity"]
    )
    assert dispatch_payload["physical_identity"]["failure_boundary_id"] == (
        binding["worker_process_identity"]
    )
    assert dispatch_signals["domain_effect_performed"] is True
    assert dispatch_signals["output_contract_fulfilled"] is True
    assert dispatch_signals["operator_adapter_id"] == (
        "worker.code-worker.typescript-provider-tool-loop"
    )
    execution_body = dispatch_signals["operator_execution_body"]
    assert sorted(execution_body["output_contract"]) == sorted(
        dispatch_signals["contract_outputs"]
    )
    assert str(dispatch_signals["operator_execution_digest"]).removeprefix(
        "sha256:"
    ) == canonical_digest(execution_body)
    assert physical_receipt["metadata"][
        "physical_dispatch_receipt_digest"
    ] == dispatch_receipt["digest"]
    assert dispatch_validation["real_gate_closed"] is True
    assert not dispatch_validation["blockers"]
    pool_api = api.get_worker_pool_api()
    valid_replay = pool_api.finalize_task(
        state,
        success=True,
        summary="verify exact physical receipt replay",
    )
    assert valid_replay is not None
    assert valid_replay["physical_dispatch_receipt"]["digest"] == (
        dispatch_receipt["digest"]
    )
    assert valid_replay["physical_dispatch_policy_artifact_ref"] == (
        physical_receipt["physical_dispatch_policy_artifact_ref"]
    )
    canonical_base = next(
        item.to_dict()
        for item in pool_api.pool.store.receipts_for_task(state.task_id)
        if item.receipt_id == valid_replay["receipt_id"]
    )
    state.metadata.pop("worker_pool_receipt")
    artifact_restored = pool_api.finalize_task(
        state,
        success=True,
        summary="restore absent TaskState receipt from canonical artifact bytes",
    )
    assert artifact_restored is not None
    assert artifact_restored["physical_dispatch_receipt"] == dispatch_receipt
    assert artifact_restored["physical_dispatch_validation"] == (
        dispatch_validation
    )

    with monkeypatch.context() as missing_artifact:
        state.metadata.pop("worker_pool_receipt", None)

        def reject_missing_artifact(*_args, **_kwargs):
            raise FileNotFoundError("controlled missing policy artifact")

        missing_artifact.setattr(
            pool_api.artifact_store,
            "verify",
            reject_missing_artifact,
        )
        with pytest.raises(RuntimeError, match="artifact verification failed"):
            pool_api.finalize_task(
                state,
                success=True,
                summary="reject missing canonical artifact bytes",
            )

    with monkeypatch.context() as corrupt_artifact:
        state.metadata.pop("worker_pool_receipt", None)
        corrupt_artifact.setattr(
            pool_api.artifact_store,
            "iter_bytes",
            lambda *_args, **_kwargs: iter((b"{}",)),
        )
        with pytest.raises(RuntimeError, match="artifact verification failed"):
            pool_api.finalize_task(
                state,
                success=True,
                summary="reject corrupt canonical artifact bytes",
            )

    state.metadata["worker_pool_receipt"] = dict(valid_replay)
    forged_policy_artifact = dict(
        valid_replay["physical_dispatch_policy_artifact_ref"]
    )
    forged_policy_artifact.update(
        {
            "ref_id": "artifact_forged_cross_task",
            "uri": "artifact://nonexistent/cross-task",
        }
    )
    state.metadata["worker_pool_receipt"] = {
        **dict(valid_replay),
        "physical_dispatch_policy_artifact_ref": forged_policy_artifact,
    }
    with pytest.raises(RuntimeError, match="canonical execution custody"):
        pool_api.finalize_task(
            state,
            success=True,
            summary="reject forged policy artifact custody",
        )

    wrong_validation = dict(dispatch_validation)
    wrong_validation["real_gate_closed"] = False
    state.metadata["worker_pool_receipt"] = {
        **dict(physical_receipt),
        "physical_dispatch_validation": wrong_validation,
    }
    with pytest.raises(RuntimeError, match="canonical binding"):
        pool_api.finalize_task(
            state,
            success=True,
            summary="reject wrong physical validation replay",
        )

    parsed_dispatch = PhysicalDispatchReceipt.from_dict(dispatch_receipt)
    cross_lease_dispatch = replace(
        parsed_dispatch,
        lease_id="lease_cross_attempt_replay",
    )
    state.metadata["worker_pool_receipt"] = {
        **dict(physical_receipt),
        "physical_dispatch_receipt": cross_lease_dispatch.to_dict(),
        "physical_dispatch_validation": (
            PhysicalDispatchReceiptValidator()
            .validate(cross_lease_dispatch)
            .to_dict()
        ),
    }
    with pytest.raises(RuntimeError, match="canonical binding"):
        pool_api.finalize_task(
            state,
            success=True,
            summary="reject cross-lease physical receipt replay",
        )
    state.metadata["worker_pool_receipt"] = dict(valid_replay)
    original_binding = dict(state.metadata["operator_placement_binding"])
    state.metadata["operator_placement_binding"] = {
        **original_binding,
        "worker_process_identity": "tampered-process-identity",
    }
    with pytest.raises(RuntimeError, match="canonical binding"):
        pool_api.finalize_task(
            state,
            success=True,
            summary="reject tampered historical placement binding",
        )
    state.metadata["operator_placement_binding"] = original_binding
    state.metadata["worker_pool_receipt"] = dict(valid_replay)
    worker_store = pool_api.pool.store
    historical_worker = worker_store.require_worker(binding["worker_id"])
    worker_store.upsert_worker_generation(
        replace(
            historical_worker,
            generation=historical_worker.generation + 1,
            version=historical_worker.version + 1,
            process_identity="replacement-process-identity",
            endpoint="local://replacement-generation",
        )
    )
    generation_replay = pool_api.finalize_task(
        state,
        success=True,
        summary="accept exact receipt after worker generation replacement",
    )
    assert generation_replay is not None
    assert generation_replay["physical_dispatch_receipt"]["digest"] == (
        dispatch_receipt["digest"]
    )
    assert generation_replay["physical_dispatch_policy_artifact_ref"] == (
        physical_receipt["physical_dispatch_policy_artifact_ref"]
    )
    assert str(state.status) == "completed", {
        "nodes": {
            item.metadata.get("stage"): {
                "status": str(item.status),
                "error": item.metadata.get("worker_error"),
                "summary": item.metadata.get("result_summary"),
            }
            for item in state.plan_nodes.values()
        }
    }
    assert state.artifacts
    assert state.artifacts[0].kind.value == "structured_data"
    assert state.artifacts[0].metadata["domain_result_kind"] == (
        "code_worker_execution"
    )
    assert state.artifacts[0].metadata[
        "operator_output_contract_fulfilled"
    ] is True
    assert state.metadata["delivery"]["workspace_id"] == (
        workspace.projection.workspace_id
    )
    assert state.metadata["delivery"]["schema"] == (
        "zyra.task-workspace-delivery/v1"
    )
    assert all(
        item.metadata.get("physical_call_ref")
        and item.metadata.get("physical_receipt_digest")
        == dispatch_receipt["digest"]
        and "execution-summary.md" not in item.uri
        for item in state.artifacts
    )
    physical_operator_events = [
        item
        for item in events
        if item.payload.get("schema")
        == "zyra.production-physical-operator-executed/v1"
    ]
    assert len(physical_operator_events) == 1
    assert physical_operator_events[0].payload["operator_ref"] == (
        dispatch_signals["operator_ref"]
    )
    layers = state.metadata["phase2_operator_execution_layers"]
    assert len(layers) == 1
    assert layers[0]["operator_ref"] == dispatch_signals["operator_ref"]
    assert layers[0]["operator_adapter_id"] == dispatch_signals[
        "operator_adapter_id"
    ]
    assert layers[0]["domain_result_kind"] == "code_worker_execution"
    assert layers[0]["physical_dispatch_receipt_digest"] == (
        dispatch_receipt["digest"]
    )
    assert any(
        item.payload.get("schema")
        == "zyra.production-execution-placement-gate/v1"
        for item in events
    )
    completion_gate = next(
        item.payload
        for item in events
        if item.payload.get("schema")
        == "zyra.production-adaptive-depth-completion-gate/v1"
    )
    assert completion_gate["failed_conditions"] == []
    assert completion_gate["adaptive_depth_failed_conditions"] == [
        "confidence_and_cost_benefit"
    ]
    assert completion_gate["decision"] == "continue"
    assert completion_gate["remaining_operator_count"] == 0
    assert completion_gate["early_exit_enabled"] is True
    assert completion_gate["final_verifier_receipt_ref"].startswith(
        "final-verifier://"
    )
    assert completion_gate["physical_execution_receipt_ref"] == (
        physical_receipt["receipt_id"]
    )
    assert any(
        item.decision_type == "operator_execution"
        for item in state.decisions
    )
    assert any(
        item.decision_type == "final_verifier"
        for item in state.decisions
    )
    outcomes = api._phase2_communication_outcomes(state)
    assert outcomes
    assert all(item["delivered"] is True for item in outcomes)
    assert all(not item["utilized_evidence_refs"] for item in outcomes)
    assert all(item["verifier_result"] == "not_run" for item in outcomes)


def test_policy_input_clock_advances_past_same_millisecond_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed_at = "2026-08-02T06:53:16.192Z"
    monkeypatch.setattr(production_policy, "now_iso", lambda: completed_at)

    cutoff = Phase2StrongestProductionBridge._causal_policy_input_created_at(
        observations=(
            {
                "run_id": "run-clock",
                "task_id": "task-clock",
                "completed_at": completed_at,
            },
        ),
        run_id="run-clock",
        task_id="task-clock",
    )

    assert cutoff == "2026-08-02T06:53:16.193Z"
    assert (
        Phase2StrongestProductionBridge
        ._communication_outcome_is_prior_and_fresh(
            completed_at=completed_at,
            completed_before=cutoff,
            maximum_age_seconds=60,
        )
        is True
    )


def test_policy_input_clock_rejects_future_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        production_policy,
        "now_iso",
        lambda: "2026-08-02T06:53:16.192Z",
    )

    with pytest.raises(
        production_policy.Phase2ProductionPolicyError,
        match="timestamp is in the future",
    ):
        Phase2StrongestProductionBridge._causal_policy_input_created_at(
            observations=(
                {
                    "run_id": "run-clock",
                    "task_id": "task-clock",
                    "completed_at": "2026-08-02T06:53:17.192Z",
                },
            ),
            run_id="run-clock",
            task_id="task-clock",
        )


@LIVE_PROVIDER_REQUIRED
def test_sealed_semantic_health_goal_consumes_loopx_before_physical_route() -> None:
    state, created = api.make_task_created_event(
        "Produce a concise deployment readiness artifact, preserve the result "
        "in memory, and verify it through the default task graph."
    )
    state.metadata.update(
        {
            "sealed": True,
            "sealed_autonomous": True,
            "competition_mode": "sealed_autonomous",
        }
    )
    workspace = api.get_workspace_manager().create_for_task(
        run_id=state.run_id,
        task_id=state.task_id,
        session_id=f"deployment-health:{state.task_id}",
        worker_id="task-runtime",
        idempotency_key=f"sealed-semantic-health:{state.task_id}",
        causation_id=created.event_id,
    )
    state.metadata["workspace_ref"] = workspace.projection.to_dict()
    ensure_default_graph(state)
    pool_api = api.get_worker_pool_api()
    pool_api.ensure_default_local_worker()
    pool_api.ensure_task_graph(state)
    pre_control = api.prepare_phase2_loopx_pre_control(
        state,
        causation_id=created.event_id,
    )

    events = run_task_graph(
        state,
        execution_context=api.graph_execution_context(),
    )

    route_events = [
        item
        for item in events
        if item.event_type is EventType.TOPOLOGY_ROUTE
    ]
    final_policy = route_events[-1].payload["topology_policy"]
    loopx = final_policy["loopx_pre_control"]
    assert pre_control["receipt_digest"] == loopx["receipt_digest"]
    assert loopx["consumed_before_topology"] is True
    assert loopx["topology_policy_input_digest"]
    assert loopx["operator_policy_input_digest"]
    assert final_policy["committed"] is True
    assert final_policy["used_baseline"] is False
    assert final_policy["operator_candidate_set"]["candidate_set_digest"]
    assert final_policy["physical_placement"]["lease_id"]
    cutoff = final_policy["topology_result"]["decision_receipt"]["created_at"]
    assert all(
        item["completed_at"] < cutoff
        for item in api._phase2_communication_outcomes(state)
    )
    assert str(state.status) == "completed"


def test_api_composition_root_missing_outcome_mutation_is_explicit_baseline() -> None:
    state, created = api.make_task_created_event(
        "Implement a code artifact and verify the result."
    )
    ensure_default_graph(state)
    context = api.graph_execution_context()
    bridge = context.topology_policy_trigger
    route_node = next(
        item
        for item in state.plan_nodes.values()
        if item.metadata.get("stage") == "route"
    )

    _, event = TopologyRouter(
        resource_scheduler=context.resource_scheduler,
        topology_policy_trigger=bridge,
    ).route(state, node=route_node, cause_event=created)

    policy = event.payload["topology_policy"]
    assert policy["used_baseline"] is True
    assert policy["committed"] is False
    assert policy["operator_candidate_set"] is None
    assert (
        policy["operator_candidate_set_absent_reason"]
        == "baseline_profile_selected"
    )
    assert policy["physical_placement"]["candidate_set_digest"] == ""


def test_placement_without_candidate_set_fails_closed_outside_baseline() -> None:
    state, created = api.make_task_created_event(
        "Implement a code artifact and verify the result."
    )
    ensure_default_graph(state)
    context = api.graph_execution_context()
    bridge = context.topology_policy_trigger
    route_node = next(
        item
        for item in state.plan_nodes.values()
        if item.metadata.get("stage") == "route"
    )

    _, event = TopologyRouter(
        resource_scheduler=context.resource_scheduler,
        topology_policy_trigger=bridge,
    ).route(state, node=route_node, cause_event=created)
    policy = dict(event.payload["topology_policy"])
    assert policy["operator_candidate_set"] is None

    # A strongest-profile run whose topology commit was rejected reaches
    # placement with the same empty constraint the baseline profile carries
    # legitimately.  Only the baseline is entitled to place without one.
    decision = context.resource_scheduler.decide(
        state,
        node=route_node,
        cause_event=created,
        operator_input=None,
    )
    for reason in (
        "topology_strongest_not_committed",
        "operator_policy_selected_no_proposal",
        "",
    ):
        lost = {**policy, "operator_candidate_set_absent_reason": reason}
        with pytest.raises(
            production_policy.Phase2ProductionPolicyError,
            match="no MaAS candidate set outside the baseline profile",
        ):
            bridge.bind_resource_decision(state, route_node, decision, lost)


def test_scheduler_failure_after_maas_candidate_set_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _ = api.make_task_created_event(
        "Implement a code artifact and verify the result."
    )
    ensure_default_graph(state)
    context = api.graph_execution_context()

    def fail_scheduler(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("controlled scheduler outage")

    monkeypatch.setattr(context.resource_scheduler, "decide", fail_scheduler)
    with pytest.raises(
        RuntimeError,
        match="ResourceScheduler failed after a MaAS candidate set",
    ):
        run_task_graph(state, execution_context=context)

    assert "operator_placement_binding" not in state.metadata
    assert not state.metadata.get("worker_pool")


def test_acquired_fallback_worker_cannot_impersonate_scheduler_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _ = api.make_task_created_event(
        "Implement a code artifact and verify the result."
    )
    ensure_default_graph(state)
    context = api.graph_execution_context()
    pool_api = api.get_worker_pool_api()
    original_acquire = pool_api.acquire_for_task

    def return_mismatched_worker(*args: object, **kwargs: object) -> object:
        acquisition = original_acquire(*args, **kwargs)
        return replace(
            acquisition,
            worker=replace(
                acquisition.worker,
                worker_id="controlled-fallback-worker",
            ),
        )

    monkeypatch.setattr(pool_api, "acquire_for_task", return_mismatched_worker)
    with pytest.raises(
        RuntimeError,
        match="acquired lease does not exactly match",
    ):
        run_task_graph(state, execution_context=context)

    projection = state.metadata["worker_pool"]
    rejected_lease = pool_api.pool.store.require_lease(projection["lease_id"])
    assert rejected_lease.terminal is True
    _assert_physical_graph_binding_terminal(state)
    assert "operator_placement_binding" not in state.metadata
    assert not state.metadata.get("physical_dispatch_receipts")


def test_physical_execution_rejects_tampered_attempt_worker_backend_binding() -> None:
    state, created = api.make_task_created_event(
        "Implement a code artifact and verify the result."
    )
    workspace = api.get_workspace_manager().create_for_task(
        run_id=state.run_id,
        task_id=state.task_id,
        session_id=f"task:{state.task_id}",
        worker_id="task-runtime",
        idempotency_key=f"production-binding-mutation:{state.task_id}",
        causation_id=created.event_id,
    )
    state.metadata["workspace_ref"] = workspace.projection.to_dict()
    ensure_default_graph(state)
    context = api.graph_execution_context()
    original_runner = context.physical_execution_runner
    assert original_runner is not None

    def tampered_runner(task, node):
        binding = dict(task.metadata["operator_placement_binding"])
        binding.update(
            {
                "attempt_id": "attempt-controlled-tamper",
                "worker_id": "worker-controlled-tamper",
                "backend_id": "backend-controlled-tamper",
            }
        )
        unsigned = dict(binding)
        unsigned.pop("binding_digest", None)
        binding["binding_digest"] = canonical_digest(unsigned)
        task.metadata["operator_placement_binding"] = binding
        return original_runner(task, node)

    events = run_task_graph(
        state,
        execution_context=replace(
            context,
            physical_execution_runner=tampered_runner,
        ),
    )
    failure = next(
        item.payload
        for item in events
        if item.payload.get("summary")
        == "Worker runtime raised an exception."
    )
    assert failure["error_code"] == "phase2_physical_placement_rejected"
    failure_receipt = failure["error_metadata"][
        "physical_execution_failure_receipt"
    ]
    assert failure_receipt["outcome"] == "rejected"
    assert failure_receipt["side_effect_started"] is False
    assert failure_receipt["terminal"] is True
    assert not state.metadata.get("physical_dispatch_receipts")

    lease_id = state.metadata["worker_pool"]["lease_id"]
    lease = api.get_worker_pool_api().pool.store.require_lease(lease_id)
    assert lease.terminal is True
    _assert_physical_graph_binding_terminal(state)


def test_physical_placement_missing_lease_reconciles_graph_and_failure_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _ = api.make_task_created_event(
        "Reject placement after its canonical lease disappears."
    )
    ensure_default_graph(state)
    context = api.graph_execution_context()
    original_runner = context.physical_execution_runner
    assert original_runner is not None
    store = api.get_worker_pool_api().pool.store
    original_get_lease = store.get_lease
    lease_missing = False

    def missing_lease_runner(task, node):
        nonlocal lease_missing
        lease_missing = True
        return original_runner(task, node)

    monkeypatch.setattr(
        store,
        "get_lease",
        lambda lease_id: (
            None if lease_missing else original_get_lease(lease_id)
        ),
    )
    events = run_task_graph(
        state,
        execution_context=replace(
            context,
            physical_execution_runner=missing_lease_runner,
        ),
    )

    failure = next(
        item.payload
        for item in events
        if item.payload.get("error_code")
        == "phase2_physical_placement_rejected"
    )
    receipt = failure["error_metadata"][
        "physical_execution_failure_receipt"
    ]
    assert receipt == state.metadata["physical_execution_failure_receipt"]
    assert receipt["lease_id"] == state.metadata["worker_pool"]["lease_id"]
    assert receipt["terminal"] is False
    _assert_physical_graph_binding_terminal(state)


def test_physical_preflight_failure_closes_started_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _ = api.make_task_created_event(
        "Implement a code artifact and verify the result."
    )
    ensure_default_graph(state)
    context = api.graph_execution_context()
    bridge = context.topology_policy_trigger

    class RejectingPreflightPort:
        receipts: list[object] = []
        validation_reports: list[object] = []

        def prepare(self, execution_context: object) -> None:
            del execution_context
            raise RuntimeError("controlled physical preflight rejection")

    monkeypatch.setattr(
        bridge,
        "physical_dispatch_factory",
        lambda *args, **kwargs: RejectingPreflightPort(),
    )
    events = run_task_graph(state, execution_context=context)

    failures = [
        item.payload
        for item in events
        if item.payload.get("error_code")
    ]
    assert failures, [
        {
            "type": str(item.event_type),
            "schema": item.payload.get("schema"),
            "summary": item.payload.get("summary"),
            "message": item.payload.get("message"),
        }
        for item in events
    ]
    failure = failures[-1]
    assert failure["error_code"] == "phase2_physical_operator_preflight_failed"
    failure_receipt = failure["error_metadata"][
        "physical_execution_failure_receipt"
    ]
    assert failure_receipt["outcome"] == "rejected"
    assert failure_receipt["metadata"]["side_effect_started"] is False
    lease = api.get_worker_pool_api().pool.store.require_lease(
        state.metadata["worker_pool"]["lease_id"]
    )
    attempt = api.get_worker_pool_api().pool.store.require_attempt(
        state.metadata["worker_pool"]["attempt_id"]
    )
    assert lease.terminal is True
    assert attempt.terminal is True
    _assert_physical_graph_binding_terminal(state)
    assert not state.artifacts


@LIVE_PROVIDER_REQUIRED
def test_physical_evidence_publish_failure_requires_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _ = api.make_task_created_event(
        "Execute the physical operator and reject an uncommitted evidence event."
    )
    ensure_default_graph(state)
    context = api.graph_execution_context()
    bridge = context.topology_policy_trigger
    publisher = bridge.evidence_publisher
    assert publisher is not None
    original_admit = publisher.admit_event

    def reject_physical_dispatch_event(event) -> None:
        if event.payload.get("policy_contract_kind") == (
            PhysicalDispatchReceipt.CONTRACT_KIND
        ):
            raise RuntimeError("controlled physical evidence event failure")
        original_admit(event)

    monkeypatch.setattr(publisher, "admit_event", reject_physical_dispatch_event)
    events = run_task_graph(state, execution_context=context)

    failure = next(
        item.payload
        for item in reversed(events)
        if item.payload.get("error_code")
        == "phase2_physical_evidence_publish_failed"
    )
    failure_receipt = failure["error_metadata"][
        "physical_execution_failure_receipt"
    ]
    assert failure_receipt["outcome"] == "failed"
    assert failure_receipt["metadata"]["side_effect_started"] is True
    assert failure_receipt["metadata"]["outcome_unknown"] is True
    assert failure_receipt["metadata"][
        "automatic_execution_retry_allowed"
    ] is False
    assert failure_receipt["metadata"]["reconcile_before_retry"] is True
    assert failure_receipt["metadata"][
        "physical_dispatch_receipt_digest"
    ]
    assert failure["error_metadata"][
        "physical_dispatch_receipt_digest"
    ] == failure_receipt["metadata"][
        "physical_dispatch_receipt_digest"
    ]
    assert "worker_pool_receipt" not in state.metadata
    lease = api.get_worker_pool_api().pool.store.require_lease(
        state.metadata["worker_pool"]["lease_id"]
    )
    assert lease.terminal is True
    _assert_physical_graph_binding_terminal(state)


def test_physical_failure_missing_lease_still_terminalizes_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, _ = api.make_task_created_event(
        "Reject a physical call whose lease disappeared at the failure boundary."
    )
    ensure_default_graph(state)
    context = api.graph_execution_context()
    bridge = context.topology_policy_trigger
    store = api.get_worker_pool_api().pool.store
    original_get_lease = store.get_lease
    lease_missing = False

    class MissingLeasePreflightPort:
        receipts: list[object] = []
        validation_reports: list[object] = []

        def prepare(self, execution_context: object) -> None:
            nonlocal lease_missing
            del execution_context
            lease_missing = True
            raise RuntimeError("controlled lease disappearance")

    monkeypatch.setattr(
        store,
        "get_lease",
        lambda lease_id: (
            None if lease_missing else original_get_lease(lease_id)
        ),
    )
    monkeypatch.setattr(
        bridge,
        "physical_dispatch_factory",
        lambda *args, **kwargs: MissingLeasePreflightPort(),
    )

    events = run_task_graph(state, execution_context=context)

    failure = next(
        item.payload
        for item in reversed(events)
        if item.payload.get("error_code")
    )
    failure_receipt = failure["error_metadata"][
        "physical_execution_failure_receipt"
    ]
    assert failure_receipt["error_code"] == "lease_missing"
    assert failure_receipt["terminal"] is True
    _assert_physical_graph_binding_terminal(state)


def test_disabled_physical_operator_adapter_fails_closed_with_terminal_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZYRA_DISABLE_PHASE2_OPERATOR_ADAPTER", "1")
    state, _ = api.make_task_created_event(
        "Implement a code artifact and verify the result."
    )
    ensure_default_graph(state)
    events = run_task_graph(
        state,
        execution_context=api.graph_execution_context(),
    )

    failures = [
        item.payload for item in events if item.payload.get("error_code")
    ]
    assert failures, [
        {
            "type": str(item.event_type),
            "schema": item.payload.get("schema"),
            "summary": item.payload.get("summary"),
            "message": item.payload.get("message"),
        }
        for item in events
    ]
    failure = failures[-1]
    failure_receipt = failure["error_metadata"][
        "physical_execution_failure_receipt"
    ]
    assert failure_receipt["outcome"] in {"failed", "rejected"}
    assert failure_receipt["metadata"][
        "automatic_execution_retry_allowed"
    ] is (failure_receipt["outcome"] == "rejected")
    lease = api.get_worker_pool_api().pool.store.require_lease(
        state.metadata["worker_pool"]["lease_id"]
    )
    attempt = api.get_worker_pool_api().pool.store.require_attempt(
        state.metadata["worker_pool"]["attempt_id"]
    )
    assert lease.terminal is True
    assert attempt.terminal is True
    _assert_physical_graph_binding_terminal(state)
    assert not state.artifacts


@pytest.mark.parametrize(
    ("mutation", "failed_check"),
    (
        ("payload_digest", "payload_digest_exact"),
        ("execution_digest", "execution_digest_valid"),
        ("contract_outputs", "contract_outputs_valid"),
        ("domain_artifact", "domain_artifact_valid"),
    ),
)
@LIVE_PROVIDER_REQUIRED
def test_tampered_operator_output_fails_closed_after_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    failed_check: str,
) -> None:
    state, _ = api.make_task_created_event(
        "测试，收到请回复ok"
    )
    workspace = api.get_workspace_manager().create_for_task(
        run_id=state.run_id,
        task_id=state.task_id,
        session_id=f"task:{state.task_id}",
        worker_id="task-runtime",
        idempotency_key=f"tampered-output:{state.task_id}",
    )
    state.metadata["workspace_ref"] = workspace.projection.to_dict()
    ensure_default_graph(state)
    context = api.graph_execution_context()
    bridge = context.topology_policy_trigger
    original_factory = bridge.physical_dispatch_factory

    class TamperingPort:
        def __init__(self, delegate: object) -> None:
            self.delegate = delegate

        @property
        def receipts(self) -> tuple[object, ...]:
            return self.delegate.receipts

        @property
        def validation_reports(self) -> tuple[object, ...]:
            return self.delegate.validation_reports

        def prepare(self, execution_context: object) -> None:
            self.delegate.prepare(execution_context)

        def execute(self, execution_context: object) -> object:
            result = self.delegate.execute(execution_context)
            metadata = dict(result.metadata)
            execution_output = dict(metadata["execution_output"])
            if mutation == "payload_digest":
                execution_output["task_payload_digest"] = "0" * 64
            elif mutation == "execution_digest":
                execution_output["operator_execution_digest"] = (
                    "sha256:" + ("0" * 64)
                )
            elif mutation == "contract_outputs":
                contract_outputs = dict(execution_output["contract_outputs"])
                first_key = sorted(contract_outputs)[0]
                contract_outputs[first_key] = {"tampered": True}
                execution_output["contract_outputs"] = contract_outputs
            elif mutation == "domain_artifact":
                domain_artifact = dict(execution_output["domain_artifact"])
                domain_artifact["content"] = (
                    str(domain_artifact["content"]) + "\n# tampered"
                )
                execution_output["domain_artifact"] = domain_artifact
            else:  # pragma: no cover - parametrization is closed above.
                raise AssertionError(mutation)
            metadata["execution_output"] = execution_output
            return replace(result, metadata=FrozenDict(metadata))

    monkeypatch.setattr(
        bridge,
        "physical_dispatch_factory",
        lambda *args, **kwargs: TamperingPort(
            original_factory(*args, **kwargs)
        ),
    )
    events = run_task_graph(state, execution_context=context)

    failures = [
        item.payload for item in events if item.payload.get("error_code")
    ]
    assert failures
    failure = failures[-1]
    assert (
        failure["error_code"]
        == "phase2_physical_operator_output_binding_failed"
    ), json.dumps(failures, ensure_ascii=False, sort_keys=True)
    assert failed_check in failure["error_metadata"][
        "failed_execution_checks"
    ]
    failure_receipt = failure["error_metadata"][
        "physical_execution_failure_receipt"
    ]
    assert failure_receipt["outcome"] == "failed"
    assert failed_check in failure_receipt["metadata"]["cause_metadata"][
        "failed_execution_checks"
    ]
    lease = api.get_worker_pool_api().pool.store.require_lease(
        state.metadata["worker_pool"]["lease_id"]
    )
    attempt = api.get_worker_pool_api().pool.store.require_attempt(
        state.metadata["worker_pool"]["attempt_id"]
    )
    assert lease.terminal is True
    assert attempt.terminal is True
    _assert_physical_graph_binding_terminal(state)
    assert not state.artifacts


@LIVE_PROVIDER_REQUIRED
def test_production_early_exit_disable_is_visible_and_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZYRA_DISABLE_PHASE2_EARLY_EXIT", "1")
    state, created = api.make_task_created_event(
        "Implement a code artifact and verify the result."
    )
    workspace = api.get_workspace_manager().create_for_task(
        run_id=state.run_id,
        task_id=state.task_id,
        session_id=f"task:{state.task_id}",
        worker_id="task-runtime",
        idempotency_key=f"production-main-disabled:{state.task_id}",
        causation_id=created.event_id,
    )
    state.metadata["workspace_ref"] = workspace.projection.to_dict()
    ensure_default_graph(state)

    events = run_task_graph(
        state,
        execution_context=api.graph_execution_context(),
    )
    gate = next((
        item.payload
        for item in events
        if item.payload.get("schema")
        == "zyra.production-adaptive-depth-completion-gate/v1"
    ), None)
    assert gate is not None, {
        "status": str(state.status),
        "nodes": {
            item.metadata.get("stage"): {
                "status": str(item.status),
                "error": item.metadata.get("worker_error"),
            }
            for item in state.plan_nodes.values()
        },
        "schemas": [item.payload.get("schema") for item in events],
        "diagnostics": [
            dict(item.payload)
            for item in events
            if item.payload.get("schema")
            in {
                "zyra.production-independent-final-verifier/v2",
                "zyra.production-completion-gate-error/v1",
                "zyra.production-completion-gate-blocked/v1",
            }
        ],
        "routes": [
            {
                "baseline": (item.payload.get("topology_policy") or {}).get("used_baseline"),
                "reroute": (item.payload.get("topology_policy") or {}).get("reroute_required"),
                "reason": ((item.payload.get("topology_policy") or {}).get("topology_result") or {}).get("degraded_reason"),
                "candidates": len((item.payload.get("topology_policy") or {}).get("communication_candidate_edges") or ()),
                "outcomes": (item.payload.get("topology_policy") or {}).get("communication_outcome_count"),
            }
            for item in events
            if item.event_type is EventType.TOPOLOGY_ROUTE
        ],
        "nodes": {
            item.metadata.get("stage"): {
                "status": str(item.status),
                "error": item.metadata.get("worker_error"),
            }
            for item in state.plan_nodes.values()
        },
    }

    assert gate["early_exit_enabled"] is False
    assert gate["decision"] == "continue"
    assert "early_exit_enabled" in gate[
        "adaptive_depth_failed_conditions"
    ]
    assert gate["remaining_operator_count"] == 0
    assert str(state.status) == "completed", {
        "gate": {
            "failed": gate["failed_conditions"],
            "hard": gate.get("hard_conditions_passed"),
            "remaining": gate["remaining_operator_count"],
        },
        "verifiers": [
            dict(item.payload)
            for item in events
            if item.payload.get("schema")
            == "zyra.production-independent-final-verifier/v2"
        ],
    }


@LIVE_PROVIDER_REQUIRED
def test_independent_final_verifier_failure_blocks_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, created = api.make_task_created_event(
        "Implement a code artifact and verify the result."
    )
    workspace = api.get_workspace_manager().create_for_task(
        run_id=state.run_id,
        task_id=state.task_id,
        session_id=f"task:{state.task_id}",
        worker_id="task-runtime",
        idempotency_key=f"production-verifier-mutation:{state.task_id}",
        causation_id=created.event_id,
    )
    state.metadata["workspace_ref"] = workspace.projection.to_dict()
    ensure_default_graph(state)
    context = api.graph_execution_context()
    verifier_owner = context.final_verifier.__self__

    def fail_artifact_verification(artifact: object) -> object:
        del artifact
        raise ValueError("controlled artifact digest mutation")

    monkeypatch.setattr(
        verifier_owner.artifact_store,
        "verify",
        fail_artifact_verification,
    )
    events = run_task_graph(state, execution_context=context)
    verifier = next(
        item.payload
        for item in events
        if item.payload.get("schema")
        == "zyra.production-independent-final-verifier/v2"
    )
    gate = next(
        item.payload
        for item in events
        if item.payload.get("schema")
        == "zyra.production-adaptive-depth-completion-gate/v1"
    )

    assert verifier["passed"] is False
    assert verifier["checks"]["artifact_owner_verified"] is False
    assert "final_verifier_passed" in gate["failed_conditions"]
    assert gate["hard_conditions_passed"] is False
    assert str(state.status) == "blocked"


@LIVE_PROVIDER_REQUIRED
def test_early_exit_disabled_executes_every_available_maas_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZYRA_DISABLE_PHASE2_EARLY_EXIT", "1")
    api.get_worker_pool_api().ensure_default_local_worker(
        worker_id="local-memory-curator"
    )
    state, created = api.make_task_created_event(
        "Implement a code artifact, preserve memory, and verify the result."
    )
    workspace = api.get_workspace_manager().create_for_task(
        run_id=state.run_id,
        task_id=state.task_id,
        session_id=f"task:{state.task_id}",
        worker_id="task-runtime",
        idempotency_key=f"production-full-depth:{state.task_id}",
        causation_id=created.event_id,
    )
    state.metadata["workspace_ref"] = workspace.projection.to_dict()
    ensure_default_graph(state)
    context = api.graph_execution_context()
    bridge = context.topology_policy_trigger
    original_execute = bridge.operator_policy.execute

    def expose_second_executable_layer(*args: object, **kwargs: object) -> object:
        result = original_execute(*args, **kwargs)
        proposal = result.proposal
        assert proposal is not None
        candidates = proposal.candidates
        assert len(candidates) >= 2
        expanded = replace(
            proposal,
            layers=(
                OperatorLayerProposal(
                    layer_index=1,
                    candidates=(candidates[0],),
                    reason="controlled production first layer",
                ),
                OperatorLayerProposal(
                    layer_index=2,
                    candidates=(candidates[1],),
                    reason="controlled production full-depth exposure",
                ),
            ),
            alternatives=tuple(candidates[2:]),
            expected_depth=2,
            expected_breadth=1,
        )
        return replace(
            result,
            proposal=expanded,
            scheduler_input=expanded.scheduler_input().bind_task(
                run_id=kwargs["policy_input"].run_id,
                task_id=kwargs["policy_input"].task_id,
            ),
        )

    monkeypatch.setattr(
        bridge.operator_policy,
        "execute",
        expose_second_executable_layer,
    )
    events = run_task_graph(
        state,
        execution_context=context,
    )
    gates = [
        item.payload
        for item in events
        if item.payload.get("schema")
        == "zyra.production-adaptive-depth-completion-gate/v1"
    ]
    execution_refs = [
        tuple(item.metadata.get("operator_refs") or ())
        for item in state.decisions
        if item.decision_type == "operator_execution"
    ]

    assert len(gates) >= 2, {
        "gates": [
            {
                "remaining": item["remaining_operator_count"],
                "executed": item["executed_operator_refs"],
                "failed": item["failed_conditions"],
            }
            for item in gates
        ],
        "status": str(state.status),
        "execution_node": [
            {
                "status": str(item.status),
                "error": item.metadata.get("worker_error"),
                "summary": item.metadata.get("result_summary"),
            }
            for item in state.plan_nodes.values()
            if item.metadata.get("stage") == "execute"
        ],
        "candidate_sets": [
            (item.payload.get("topology_policy") or {}).get(
                "operator_candidate_set"
            )
            for item in events
            if item.event_type is EventType.TOPOLOGY_ROUTE
        ],
    }
    assert gates[0]["remaining_operator_count"] >= 1
    assert gates[-1]["remaining_operator_count"] == 0
    assert all(item["decision"] == "continue" for item in gates)
    assert all(item["early_exit_enabled"] is False for item in gates)
    assert any(
        item.payload.get("schema")
        == "zyra.production-adaptive-depth-continuation/v1"
        for item in events
    )
    flattened = [ref for group in execution_refs for ref in group]
    assert len(flattened) >= 2
    assert len(flattened) == len(set(flattened))
    layers = state.metadata["phase2_operator_execution_layers"]
    physical_receipts = state.metadata["physical_dispatch_receipts"]
    assert len(layers) == len(physical_receipts) == len(flattened)
    for layer, receipt in zip(layers, physical_receipts, strict=True):
        payload = receipt["payload"]
        signals = payload["input_signals"]
        assert signals["workload_operation"] == "phase2-operator-execution"
        assert signals["operator_ref"] == layer["operator_ref"]
        assert signals["layer_index"] == layer["layer_index"]
        assert payload["placement_decision_id"] == layer[
            "resource_decision_id"
        ]
        assert payload["lease_id"] == layer["lease_id"]
        assert payload["physical_attempt_id"] == layer["attempt_id"]
        assert signals["worker_id"] == layer["worker_id"]
        assert receipt["digest"] == layer[
            "physical_dispatch_receipt_digest"
        ]
    for key in (
        "operator_ref",
        "operator_idempotency_key",
        "resource_decision_id",
        "lease_id",
        "attempt_id",
        "physical_call_ref",
        "physical_dispatch_receipt_digest",
        "physical_node_artifact_ref",
        "worker_pool_receipt_id",
        "layer_digest",
        "operator_adapter_id",
        "domain_result_kind",
        "domain_output_digest",
    ):
        values = [item[key] for item in layers]
        assert len(values) == len(set(values)), {key: values}
    artifact_ids = [
        artifact_id
        for item in layers
        for artifact_id in item["canonical_artifact_ids"]
    ]
    assert len(artifact_ids) == len(set(artifact_ids))
    assert {item["operator_adapter_id"] for item in layers} == {
        "worker.code-worker.typescript-provider-tool-loop",
        "worker.local-memory-curator.continuity",
    }
    assert {item["domain_result_kind"] for item in layers} == {
        "code_worker_execution",
        "memory_continuity",
    }
    memory_layer = next(
        item for item in layers if item["domain_result_kind"] == "memory_continuity"
    )
    assert memory_layer["memory_record_ids"]
    memory_receipt = memory_layer["memory_mutation_receipt"]
    assert memory_receipt["owner"] == "MemoryFabric"
    assert memory_receipt["committed"] is True
    assert memory_receipt["readback_verified"] is True
    assert memory_receipt["committed_record_ids"] == memory_layer[
        "memory_record_ids"
    ]
    receipt_unsigned = dict(memory_receipt)
    receipt_digest = receipt_unsigned.pop("receipt_digest")
    assert receipt_digest == canonical_digest(receipt_unsigned)
    assert all(item["output_contract_fulfilled"] is True for item in layers)
    assert str(state.status) == "completed"


def test_memory_owner_failure_precedes_success_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api.get_worker_pool_api().ensure_default_local_worker(
        worker_id="local-memory-curator"
    )
    state, _ = api.make_task_created_event(
        "Preserve durable task memory and verify continuity."
    )
    ensure_default_graph(state)
    context = api.graph_execution_context()
    bridge = context.topology_policy_trigger
    original_execute = bridge.operator_policy.execute

    def select_memory_operator(*args: object, **kwargs: object) -> object:
        result = original_execute(*args, **kwargs)
        proposal = result.proposal
        assert proposal is not None
        memory_candidate = next(
            item
            for item in proposal.candidates
            if item.operator_id == "worker:local-memory-curator"
        )
        expanded = replace(
            proposal,
            layers=(
                OperatorLayerProposal(
                    layer_index=1,
                    candidates=(memory_candidate,),
                    reason="controlled memory owner failure path",
                ),
            ),
            alternatives=(),
            expected_depth=1,
            expected_breadth=1,
        )
        return replace(
            result,
            proposal=expanded,
            scheduler_input=expanded.scheduler_input().bind_task(
                run_id=kwargs["policy_input"].run_id,
                task_id=kwargs["policy_input"].task_id,
            ),
        )

    monkeypatch.setattr(
        bridge.operator_policy,
        "execute",
        select_memory_operator,
    )

    def reject_memory_commit(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("controlled MemoryFabric owner commit failure")

    monkeypatch.setattr(
        bridge.memory_fabric,
        "refresh_task_memory",
        reject_memory_commit,
    )
    events = run_task_graph(state, execution_context=context)

    failures = [
        item.payload
        for item in events
        if item.payload.get("error_code")
    ]
    failure = next(
        (
            item
            for item in reversed(failures)
            if item.get("error_code")
            == "phase2_memory_owner_commit_failed"
        ),
        None,
    )
    assert failure is not None, repr([
        {
            "error_code": item.get("error_code"),
            "error": item.get("error"),
            "metadata": item.get("error_metadata"),
        }
        for item in failures
    ])
    failure_receipt = failure["error_metadata"][
        "physical_execution_failure_receipt"
    ]
    assert failure_receipt["outcome"] == "failed"
    binding = state.metadata["operator_placement_binding"]
    lease = api.get_worker_pool_api().pool.store.require_lease(
        binding["lease_id"]
    )
    attempt = api.get_worker_pool_api().pool.store.require_attempt(
        binding["attempt_id"]
    )
    assert lease.terminal is True
    assert attempt.terminal is True
    assert not state.metadata.get("phase2_operator_execution_layers")
    assert not state.metadata.get("worker_pool_receipt")
    assert failure["error_metadata"]["reconcile_before_retry"] is True


def test_permission_receipt_is_not_used_as_an_agent_lifetime() -> None:
    """Permission freshness remains independent from an open agent run."""

    assert api.PHASE2_PERMISSION_RECEIPT_VALIDITY >= timedelta(hours=1)
    assert api.PHASE2_PERMISSION_RECEIPT_VALIDITY > timedelta(
        milliseconds=api._BENCHMARK_CLOSEOUT_RESERVE_MS
    )


def test_long_horizon_reasoning_budget_requires_an_external_docker_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZYRA_BENCHMARK_LONG_HORIZON", "true")
    assert api._reasoning_budget_from_environment() == (
        None,
        None,
        0,
        False,
    )

    monkeypatch.setenv("ZYRA_BENCHMARK_DOCKER_CONTAINER", "task-main-1")
    monkeypatch.setenv("ZYRA_BENCHMARK_DOCKER_WORKDIR", "/app/task")
    turns, runtime_seconds, transport_ms, enabled = (
        api._reasoning_budget_from_environment()
    )
    assert enabled is True
    assert turns is None
    assert runtime_seconds is None
    assert transport_ms == 0

    external_deadline = int(time.time() * 1000) + 120_000
    monkeypatch.setenv("ZYRA_EXTERNAL_DEADLINE_EPOCH_MS", str(external_deadline))
    turns, runtime_seconds, transport_ms, enabled = (
        api._reasoning_budget_from_environment()
    )
    assert enabled is True
    assert turns is None
    assert runtime_seconds is not None
    assert 55 <= runtime_seconds <= 60
    assert transport_ms - runtime_seconds * 1_000 == pytest.approx(
        api._PHYSICAL_DISPATCH_RECEIPT_RESERVE_MS,
        abs=100,
    )
    assert 85_000 <= transport_ms <= 90_000
    assert api.PHASE2_PERMISSION_RECEIPT_VALIDITY > timedelta(
        milliseconds=transport_ms
    )


def test_model_output_token_priority_has_one_auditable_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ZYRA_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("ZYRA_DEPLOYMENT_MAX_OUTPUT_TOKENS", raising=False)
    assert api._configured_model_output_tokens() == {
        "schema": "zyra.model-output-token-budget/v1",
        "requested": 16_384,
        "requested_source": "model-catalog-default-request",
    }
    assert api._configured_model_output_tokens(live_model_bound=True) == {
        "schema": "zyra.model-output-token-budget/v1",
        "requested": 131_072,
        "requested_source": "model-catalog-default-request",
    }
    monkeypatch.setenv("ZYRA_DEPLOYMENT_MAX_OUTPUT_TOKENS", "12000")
    assert api._configured_model_output_tokens()["requested_source"] == (
        "deployment-profile"
    )
    monkeypatch.setenv("ZYRA_MAX_OUTPUT_TOKENS", "16384")
    assert api._configured_model_output_tokens() == {
        "schema": "zyra.model-output-token-budget/v1",
        "requested": 16_384,
        "requested_source": "benchmark-environment",
    }
    assert api._configured_model_output_tokens({"max_output_tokens": 32768}) == {
        "schema": "zyra.model-output-token-budget/v1",
        "requested": 32_768,
        "requested_source": "task-explicit",
    }
