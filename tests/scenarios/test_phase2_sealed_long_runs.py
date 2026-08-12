from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from zyra_evaluation.policy_benchmark.long_run_validator import (
    IndependentTransitionValidator,
    SealedLongRunValidator,
    canonical_digest,
)
from zyra_evaluation.policy_benchmark.sealed_mechanisms import (
    SealedMechanismEvidenceRuntime,
)
from zyra_evaluation.policy_benchmark.sealed_long_run import (
    SealedLongRunError,
    SealedLongRunRunner,
    _SealedInlineProductionPolicy,
    _evidence_digest,
    _json,
)
from zyra_evaluation.policy_benchmark.sealed_physical import (
    SealedPhysicalDispatchRuntime,
    SealedPhysicalDispatchError,
    _free_port_block,
    _receipt_evidence,
    _release_port_block,
    _sealed_route_projection,
)
from zyra_evaluation.scenario_runner.live_models import TierKind, TierObservation
from zyra_evaluation.scenario_runner.errors import ScenarioRunnerError
from zyra_evaluation.scenario_runner.dual_domain import CanonicalEventBuilder
from zyra_evaluation.scenario_runner.research_delivery import (
    LiveHttpSourceAcquirer,
)
from zyra_orchestration.deployment.errors import DispatchRejected
from zyra_orchestration.deployment.node_runtime import DeploymentNodeRuntime
from zyra_orchestration.deployment.provider_dispatch import (
    DEEPSEEK_MODEL_ID,
    DEEPSEEK_PROVIDER_ID,
    GLM_52_MODEL_ID,
    KIMI_MODEL_ID,
    KIMI_PROVIDER_ID,
    MARKER_MAXIMUM_OUTPUT_TOKENS,
    ZHIPU_PROVIDER_ID,
    _LIVE_PROFILES,
    _marker_dispatch_request,
)
from zyra_runtime.provider_control_plane import (
    CredentialRegistration,
    IntegrationDefinition,
    ModelCapabilities,
    ModelDefinition,
    ProviderControlPlaneClient,
    ProviderDefinition,
    ProviderProtocol,
    RouteConstraints,
    RouteRequest,
)


ROOT = Path(__file__).resolve().parents[2]


def test_sealed_physical_port_blocks_are_reserved_until_release() -> None:
    first = _free_port_block()
    second = _free_port_block()
    try:
        assert first != second
    finally:
        _release_port_block(first)
        _release_port_block(second)

    reused = _free_port_block()
    try:
        assert reused == first
    finally:
        _release_port_block(reused)


def test_sealed_validator_accepts_extended_true_production_checks() -> None:
    required = {
        "candidate_set": True,
        "resource_decision": True,
        "lease": True,
        "attempt": True,
        "physical_receipt": True,
        "real_execution": True,
    }

    assert SealedLongRunValidator._production_control_checks_ready(
        {
            **required,
            "loopx_pre_control_topology": True,
            "loopx_pre_control_permission": True,
        }
    )
    assert not SealedLongRunValidator._production_control_checks_ready(
        {**required, "loopx_pre_control_topology": False}
    )
    assert not SealedLongRunValidator._production_control_checks_ready(
        {key: value for key, value in required.items() if key != "lease"}
    )


def test_sealed_validator_binds_physical_custody_to_scenario_and_call_uri() -> None:
    receipt = {
        "digest": "receipt-digest",
        "placement_decision_id": "decision-id",
        "lease_id": "lease-id",
        "physical_attempt_id": "attempt-id",
    }
    validation = {
        "schema": "zyra.physical-dispatch-validation/v1",
        "receipt_digest": "receipt-digest",
        "location": "cloud",
        "real_gate_closed": True,
        "blockers": [],
        "checks": {"cloud_live_request": True},
    }
    decision = {
        "decision_id": "decision-id",
        "run_id": "sealed-scenario-id",
        "task_id": "task-id",
    }
    lease = {"fence_token_persisted": False}
    acquired_lease = {"lease_id": "lease-id"}
    attempt = {"attempt_id": "attempt-id", "lease_id": "lease-id"}
    completion = {
        "lease_id": "lease-id",
        "attempt_id": "attempt-id",
        "outcome": "succeeded",
        "backend_receipt_ref": "deployment-dispatch://dispatch-id",
    }
    call = {
        "ref_id": "dispatch-id",
        "uri": "deployment-dispatch://dispatch-id",
    }
    values = {
        "receipt": receipt,
        "validation": validation,
        "decision": decision,
        "lease": lease,
        "acquired_lease": acquired_lease,
        "attempt": attempt,
        "completion": completion,
        "call": call,
        "location": "cloud",
        "scenario_run_id": "sealed-scenario-id",
        "task_id": "task-id",
    }

    assert SealedLongRunValidator._physical_custody_binding_ready(**values)
    assert not SealedLongRunValidator._physical_custody_binding_ready(
        **{**values, "scenario_run_id": "canonical-owner-run-id"}
    )
    assert not SealedLongRunValidator._physical_custody_binding_ready(
        **{
            **values,
            "completion": {
                **completion,
                "backend_receipt_ref": call["ref_id"],
            },
        }
    )


def test_inline_policy_persists_every_returned_production_event_before_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_values = [
        SimpleNamespace(
            event_id="event-topology",
            payload={
                "topology_policy": {
                    "used_baseline": False,
                    "committed": True,
                    "operator_candidate_set": {
                        "candidate_set_digest": "candidate-digest",
                        "input_snapshot_digest": "operator-policy-digest",
                    },
                    "physical_placement": {
                        "candidate_set_digest": "candidate-digest",
                        "resource_decision_id": "resource-decision",
                        "lease_id": "lease-id",
                        "attempt_id": "attempt-id",
                    },
                    "loopx_pre_control": {
                        "receipt_digest": "loopx-pre-digest",
                        "permission_receipt_digest": "loopx-permission-digest",
                        "consumed_before_topology": True,
                        "topology_policy_input_digest": "topology-policy-digest",
                        "operator_policy_input_digest": "operator-policy-digest",
                    },
                    "permission_receipt": {
                        "effect": "allow",
                        "receipt_digest": "permission-digest",
                    },
                    "topology_result": {
                        "composition": {
                            "policy_input_digest": "topology-policy-digest",
                            "proposal": {
                                "payload": {
                                    "operations": [],
                                }
                            },
                            "layers": [],
                            "projection_differences": [],
                        },
                        "decision_receipt": {
                            "digest": "decision-digest",
                            "payload": {
                                "decision_id": "decision-id",
                                "graph_commit": {
                                    "commit_id": "commit-id",
                                },
                            },
                        },
                    },
                },
            },
        ),
        SimpleNamespace(
            event_id="event-completion",
            payload={
                "schema": "zyra.production-adaptive-depth-completion-gate/v1",
                "hard_conditions_passed": True,
                "continuity_receipt": {
                    "payload": {"continuity_result": "passed"},
                },
                "symbolic_bundle_policy_artifact_ref": {
                    "digest": "symbolic-digest",
                },
                "final_verifier_receipt_ref": "verifier-ref",
                "physical_execution_receipt_ref": "physical-ref",
            },
        ),
    ]
    persisted: dict[str, dict] = {}
    calls: list[str] = []

    class Store:
        def save_checkpoint(self, _state: object) -> None:
            calls.append("checkpoint")

        def task_events(self, _task_id: str) -> list[dict]:
            calls.append("readback")
            return list(persisted.values())

    store = Store()

    class Api:
        @staticmethod
        def graph_execution_context() -> object:
            return object()

        @staticmethod
        def get_store() -> Store:
            return store

        @staticmethod
        def persist_events(_store: Store, events: list[object]) -> None:
            calls.append("persist")
            for event in events:
                persisted[str(event.event_id)] = {
                    "event_id": str(event.event_id),
                    "payload": dict(event.payload),
                }

    state = SimpleNamespace(
        run_id="run-inline",
        task_id="task-inline",
        user_goal="sealed goal",
        metadata={
            "phase2_operator_execution_layers": [
                {
                    "operator_ref": "operator-ref",
                    "resource_decision_id": "resource-decision",
                    "lease_id": "lease-id",
                    "attempt_id": "attempt-id",
                    "physical_dispatch_receipt_digest": "physical-digest",
                    "operator_execution_digest": "operator-digest",
                    "canonical_artifact_ids": ["artifact-id"],
                }
            ],
            "worker_pool_receipt": {
                "receipt_id": "worker-receipt",
                "physical_dispatch_receipt": {
                    "digest": "physical-digest",
                    "payload": {
                        "placement_decision_id": "resource-decision",
                        "lease_id": "lease-id",
                        "physical_attempt_id": "attempt-id",
                        "simulated": False,
                        "semantic_only": False,
                    },
                },
                "physical_dispatch_validation": {"real_gate_closed": True},
            },
            "operator_placement_binding": {
                "candidate_set_digest": "candidate-digest",
                "loopx_pre_control_digest": "loopx-pre-digest",
                "loopx_topology_policy_input_digest": "topology-policy-digest",
                "loopx_operator_policy_input_digest": "operator-policy-digest",
            },
        },
    )
    policy = _SealedInlineProductionPolicy(
        api_main=Api(),
        owner=SimpleNamespace(state=state),
        project_root=ROOT,
        state_root=tmp_path,
    )
    fake_loopx = {
        "schema": "zyra.phase2-production-loopx-chain/v1",
        "pre_control": {"receipt_digest": "loopx-pre-digest"},
        "checks": {},
        "chain_digest": "loopx-digest",
    }
    fake_pre_control = {
        "schema": "zyra.phase2-production-loopx-pre-control/v1",
        "receipt_digest": "loopx-pre-digest",
        "permission_receipt": {
            "receipt_digest": "loopx-permission-digest",
        },
    }
    monkeypatch.setattr(
        policy,
        "_loopx_pre_control",
        lambda **_kwargs: fake_pre_control,
    )
    monkeypatch.setattr(
        policy,
        "_loopx_chain",
        lambda **_kwargs: fake_loopx,
    )
    monkeypatch.setattr(
        "zyra_orchestration.run_task_graph",
        lambda *_args, **_kwargs: list(event_values),
    )

    receipt = policy.execute(
        owner_context={
            "run_id": "run-inline",
            "task_id": "task-inline",
            "task_created_event_id": "event-root",
        },
        configuration=None,
        goal="sealed goal",
    )

    assert tuple(persisted) == ("event-topology", "event-completion")
    assert calls[:5] == [
        "readback",
        "checkpoint",
        "persist",
        "checkpoint",
        "readback",
    ]
    assert receipt["production_event_count"] == 2
    assert policy.require_bundle()["production_mechanism_chain"]["loopx"] == (
        fake_loopx
    )


def test_inline_policy_binds_actual_production_mechanisms_and_rejects_tamper(
    tmp_path: Path,
) -> None:
    from apps.api.zyra_api import main as api_main
    from zyra_orchestration import ensure_default_graph

    state, created = api_main.make_task_created_event(
        "Implement a code artifact and verify the result."
    )
    workspace = api_main.get_workspace_manager().create_for_task(
        run_id=state.run_id,
        task_id=state.task_id,
        session_id=f"task:{state.task_id}",
        worker_id="task-runtime",
        idempotency_key=f"sealed-inline-test:{state.task_id}",
        causation_id=created.event_id,
    )
    state.metadata["workspace_ref"] = workspace.projection.to_dict()
    ensure_default_graph(state)
    api_main.persist_events(api_main.get_store(), [created])
    policy = _SealedInlineProductionPolicy(
        api_main=api_main,
        owner=SimpleNamespace(state=state),
        project_root=ROOT,
        state_root=tmp_path / "mechanisms",
    )
    policy.execute(
        owner_context={
            "run_id": state.run_id,
            "task_id": state.task_id,
            "task_created_event_id": created.event_id,
        },
        configuration=None,
        goal=state.user_goal,
    )
    bundle = policy.require_bundle()
    chain = bundle["production_mechanism_chain"]
    control = bundle["production_control"]
    owner_events = api_main.get_store().task_events(state.task_id)

    assert SealedLongRunValidator._production_chain_blockers(
        chain,
        control=control,
        owner_events=owner_events,
        run_id=state.run_id,
        task_id=state.task_id,
    ) == []
    assert set(chain["topology"]["operation_kinds"]) == {
        "add_edge",
        "add_node",
    }
    assert all(chain["loopx"]["checks"].values())
    assert chain["loopx"]["pre_control_digest"] == chain["loopx"][
        "pre_control"
    ]["receipt_digest"]
    assert chain["loopx"]["checks"]["restart_runtime_replaced"] is True
    assert chain["loopx"]["restart_runtime_identity_before"] != chain[
        "loopx"
    ]["restart_runtime_identity_after"]
    assert chain["topology"]["loopx_pre_control_consumption"][
        "consumed_before_topology"
    ] is True
    pre_control = chain["loopx"]["pre_control"]
    pre_permission = pre_control["permission_receipt"]
    pre_permission_unsigned = dict(pre_permission)
    pre_permission_digest = pre_permission_unsigned.pop("receipt_digest")
    assert "typescript" in pre_permission["canonical_owner"].casefold()
    assert pre_permission["effect"] == "allow"
    assert {"graph.write", "worker.dispatch"}.issubset(
        pre_permission["allowed_permissions"]
    )
    assert pre_permission_digest == canonical_digest(
        pre_permission_unsigned
    )
    assert pre_control["validation"]["permission_receipt_id"] == (
        pre_permission["decision_id"]
    )
    assert chain["topology"]["loopx_pre_control_consumption"][
        "permission_receipt_digest"
    ] == pre_permission_digest

    tampered = json.loads(json.dumps(chain))
    tampered["loopx"]["checks"]["restart_recovered"] = False
    loopx_unsigned = dict(tampered["loopx"])
    loopx_unsigned.pop("chain_digest")
    tampered["loopx"]["chain_digest"] = canonical_digest(loopx_unsigned)
    chain_unsigned = dict(tampered)
    chain_unsigned.pop("chain_digest")
    tampered["chain_digest"] = canonical_digest(chain_unsigned)
    tampered_control = dict(control)
    tampered_control["production_mechanism_chain_digest"] = tampered[
        "chain_digest"
    ]
    control_unsigned = dict(tampered_control)
    control_unsigned.pop("receipt_digest")
    tampered_control["receipt_digest"] = canonical_digest(control_unsigned)
    blockers = SealedLongRunValidator._production_chain_blockers(
        tampered,
        control=tampered_control,
        owner_events=owner_events,
        run_id=state.run_id,
        task_id=state.task_id,
    )
    assert "production_loopx_chain_binding" in blockers


def _analysis_receipt(
    unit_id: str,
    *,
    byte_start: int,
    byte_end: int,
    source_locator: str = "source.py",
) -> dict:
    receipt = {
        "schema": "zyra.analysis-unit-owner-receipt/v1",
        "owner": "SQLiteStore.MemoryRecord",
        "run_id": "run-analysis",
        "task_id": "task-analysis",
        "work_unit_id": unit_id,
        "memory_id": f"memory-{unit_id}",
        "kind": "source-analysis",
        "input_digest": "a" * 64,
        "output_digest": "b" * 64,
        "byte_start": byte_start,
        "byte_end": byte_end,
        "analysis_index": 1,
        "source_locator": source_locator,
        "content_digest": "c" * 64,
        "committed": True,
        "readback_verified": True,
    }
    receipt["receipt_digest"] = canonical_digest(receipt)
    return receipt


def _owner_bound_event(sequence: int, previous: str) -> tuple[dict, dict]:
    event_id = f"event-{sequence}"
    source = {
        "event_id": event_id,
        "event_type": "canonical_state_mutation",
        "run_id": "run-owner",
        "task_id": "task-owner",
        "sequence": sequence,
        "causation_id": previous,
        "payload": {
            "semantic_effect": "state_mutation",
            "mutation": {"revision": sequence, "state": f"v{sequence}"},
            "causation_id": previous,
        },
        "metadata": {"stage": "owner-test"},
    }
    persisted = {
        "event_id": event_id,
        "event_type": "task.updated",
        "run_id": "run-owner",
        "task_id": "task-owner",
        "payload": {"canonical_revision": sequence},
    }
    receipt = {
        "schema": "zyra.canonical-event-owner-receipt/v1",
        "owner": "SQLiteStore.EventRecord",
        "run_id": "run-owner",
        "task_id": "task-owner",
        "event_id": event_id,
        "source_event_type": source["event_type"],
        "persisted_event_type": persisted["event_type"],
        "source_event_digest": canonical_digest(source),
        "persisted_event_digest": canonical_digest(persisted),
        "persisted_payload_digest": canonical_digest(persisted["payload"]),
    }
    receipt["receipt_digest"] = canonical_digest(receipt)
    source["owner_receipt"] = receipt
    return source, persisted


def test_independent_validator_rejects_two_thousand_unowned_claims() -> None:
    events = []
    previous = ""
    for sequence in range(1, 2_001):
        event = _owner_bound_event(sequence, previous)[0]
        event.pop("owner_receipt")
        events.append(event)
        previous = event["event_id"]
    result = IndependentTransitionValidator().validate(
        events,
        run_id="run-owner",
        task_id="task-owner",
    )
    assert result.valid_count == 0
    assert len(result.invalid) == 2_000
    assert all(
        "owner_receipt_missing" in item["reason_codes"]
        for item in result.invalid
    )


def test_independent_validator_recomputes_owner_snapshot_and_rejects_tamper() -> None:
    first, persisted_first = _owner_bound_event(1, "")
    second, persisted_second = _owner_bound_event(2, first["event_id"])
    valid = IndependentTransitionValidator().validate(
        (first, second),
        run_id="run-owner",
        task_id="task-owner",
        owner_events=(persisted_first, persisted_second),
    )
    assert valid.valid_count == 2
    persisted_second["payload"]["canonical_revision"] = 99
    invalid = IndependentTransitionValidator().validate(
        (first, second),
        run_id="run-owner",
        task_id="task-owner",
        owner_events=(persisted_first, persisted_second),
    )
    assert invalid.valid_count == 1
    assert "owner_event_digest" in invalid.invalid[0]["reason_codes"]


def test_work_units_reject_overlapping_owner_backed_source_ranges() -> None:
    builder = CanonicalEventBuilder(
        run_id="run-analysis",
        task_id="task-analysis",
    )
    with pytest.raises(ScenarioRunnerError) as blocked:
        builder.work_units(
            (
                _analysis_receipt("unit-1", byte_start=0, byte_end=100),
                _analysis_receipt("unit-2", byte_start=50, byte_end=150),
            ),
            worker_id="worker-analysis",
            provider_id="zyra-local",
            stage="source-index",
        )
    assert blocked.value.code == "live_work_unit_not_distinct"


def test_validator_binds_analysis_range_to_persisted_memory_content() -> None:
    persisted_content = {
        "work_unit_id": "unit-1",
        "input_digest": "a" * 64,
        "output_digest": "b" * 64,
        "byte_start": 0,
        "byte_end": 100,
        "source_locator": "persisted.py",
    }
    memory = {
        "memory_id": "memory-unit-1",
        "run_id": "run-analysis",
        "task_id": "task-analysis",
        "source_id": "unit-1",
        "content": persisted_content,
    }
    unit = _analysis_receipt(
        "unit-1",
        byte_start=0,
        byte_end=100,
        source_locator="claimed.py",
    )
    unit["content_digest"] = canonical_digest(persisted_content)
    unsigned_unit = dict(unit)
    unsigned_unit.pop("receipt_digest")
    unit["receipt_digest"] = canonical_digest(unsigned_unit)
    event = {
        "event_id": "event-analysis-1",
        "event_type": "analysis_unit_indexed",
        "run_id": "run-analysis",
        "task_id": "task-analysis",
        "sequence": 1,
        "causation_id": "",
        "payload": {
            "semantic_effect": "memory",
            "mutation": {"owner_receipt": unit, "state": "owner-indexed"},
            "causation_id": "",
        },
        "metadata": {"stage": "source-index"},
    }
    owner_receipt = {
        "schema": "zyra.canonical-analysis-event-owner-receipt/v1",
        "owner": "SQLiteStore.MemoryRecord",
        "run_id": "run-analysis",
        "task_id": "task-analysis",
        "event_id": event["event_id"],
        "source_event_type": event["event_type"],
        "source_event_digest": canonical_digest(event),
        "memory_id": memory["memory_id"],
        "analysis_receipt_digest": unit["receipt_digest"],
        "persisted_memory_digest": canonical_digest(memory),
        "persisted_content_digest": canonical_digest(persisted_content),
    }
    owner_receipt["receipt_digest"] = canonical_digest(owner_receipt)
    event["owner_receipt"] = owner_receipt
    result = IndependentTransitionValidator().validate(
        (event,),
        run_id="run-analysis",
        task_id="task-analysis",
        owner_analysis_records=(memory,),
    )
    assert result.valid_count == 0
    assert "analysis_memory_content_binding" in result.invalid[0]["reason_codes"]


def test_loopback_remote_lane_requires_real_physical_boundary() -> None:
    base = {
        "observation_id": "edge-real",
        "tier": TierKind.EDGE,
        "endpoint": "http://127.0.0.1:51000",
        "endpoint_id": "edge-node",
        "runtime_id": "edge-generation",
        "process_id": "12345",
        "isolation_id": "edge-failure-boundary",
        "request_id": "edge-request",
        "route_id": "edge-route",
        "lease_id": "edge-lease",
        "artifact_ids": ("edge-artifact",),
        "started_at": "2026-07-30T00:00:00Z",
        "completed_at": "2026-07-30T00:00:01Z",
        "request_digest": "a" * 64,
        "response_digest": "b" * 64,
        "handshake_ok": True,
        "heartbeat_ok": True,
        "task_success": True,
        "simulated": False,
        "loopback": True,
    }
    TierObservation(
        **base,
        metadata={
            "remote_boundary": "isolated-process",
            "failure_boundary_id": "edge-failure-boundary",
            "independent_process": True,
            "physical_dispatch_validation": {"real_gate_closed": True},
        },
    ).validate()
    with pytest.raises(ScenarioRunnerError, match="loopback"):
        TierObservation(**base, metadata={}).validate()


def test_physical_receipt_contract_envelope_is_flattened_for_evidence() -> None:
    flattened = _receipt_evidence(
        {
            "contract_id": "physical-dispatch:test",
            "created_at": "2026-07-30T00:00:00Z",
            "digest": "a" * 64,
            "payload": {
                "physical_attempt_id": "attempt-test",
                "physical_identity": {"location": "local"},
                "simulated": False,
                "semantic_only": False,
            },
        }
    )
    assert flattened["physical_identity"]["location"] == "local"
    assert flattened["digest"] == "a" * 64
    assert flattened["contract_id"] == "physical-dispatch:test"
    with pytest.raises(
        SealedPhysicalDispatchError,
        match="contract payload is missing",
    ):
        _receipt_evidence({"digest": "b" * 64})


@pytest.mark.parametrize(
    ("provider_id", "model_id", "expected_endpoint"),
    (
        (
            ZHIPU_PROVIDER_ID,
            GLM_52_MODEL_ID,
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        ),
        (
            DEEPSEEK_PROVIDER_ID,
            DEEPSEEK_MODEL_ID,
            "https://api.deepseek.com/chat/completions",
        ),
        (
            KIMI_PROVIDER_ID,
            KIMI_MODEL_ID,
            "https://api.moonshot.cn/v1/chat/completions",
        ),
    ),
)
def test_sealed_provider_projection_uses_profile_canonical_https_endpoint(
    provider_id: str,
    model_id: str,
    expected_endpoint: str,
) -> None:
    profile = _LIVE_PROFILES[(provider_id, model_id)]
    providers = SealedPhysicalDispatchRuntime._providers(
        (
            {
                "physical_attempt_id": "attempt-local",
                "physical_identity": {
                    "location": "local",
                    "endpoint": "local://terminal",
                },
                "call_receipt": {
                    "ref_id": "call-local",
                    "digest": "a" * 64,
                },
                "placement_decision_id": "decision-local",
                "input_signals": {
                    "payload_digest": "b" * 64,
                    "call_started_at": "2026-08-02T00:00:00Z",
                },
                "completed_at": "2026-08-02T00:00:01Z",
                "digest": "c" * 64,
            },
            {
                "physical_attempt_id": "attempt-cloud",
                "physical_identity": {"location": "cloud"},
                "call_receipt": {
                    "ref_id": "call-cloud",
                    "digest": "d" * 64,
                },
                "placement_decision_id": "decision-cloud",
                "input_signals": {
                    "call_started_at": "2026-08-02T00:00:00Z",
                },
                "completed_at": "2026-08-02T00:00:01Z",
                "digest": "e" * 64,
                "provider_evidence": {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "endpoint": profile.endpoint,
                    "endpoint_host": profile.endpoint_host,
                    "endpoint_path": profile.endpoint_path,
                    "request_id": "request-cloud",
                    "provider_attempt_id": "provider-attempt-cloud",
                    "credential_ref": "env://ZAI_API_KEY",
                    "live": True,
                    "http_status": 200,
                    "payload_digest": "f" * 64,
                    "cost_usd": 0.001,
                    "latency_ms": 12,
                },
            },
        ),
        (),
    )

    assert profile.endpoint == expected_endpoint
    assert providers[1]["endpoint"] == expected_endpoint


def test_marker_request_reserves_reasoning_headroom_without_payload_in_message() -> None:
    request = _marker_dispatch_request(
        request_id="request-marker",
        route_id="route-marker",
        run_id="run-marker",
        task_id="task-marker",
        node_id="node-marker",
        marker="ZYRA_PHYSICAL_ABC123",
        idempotency_key="marker-key",
        payload_digest="a" * 64,
    )

    assert request.maximum_output_tokens == MARKER_MAXIMUM_OUTPUT_TOKENS == 512
    assert len(request.messages) == 1
    assert request.messages[0].content.endswith("ZYRA_PHYSICAL_ABC123")
    assert "a" * 64 not in request.messages[0].content


def test_marker_request_external_http_body_excludes_control_plane_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "ZYRA_PHYSICAL_ABC123"
    payload_digest = "9" * 64
    captured: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            captured["body"] = json.loads(body.decode("utf-8"))
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            self.wfile.write(
                (
                    "data: "
                    + json.dumps(
                        {
                            "choices": [
                                {
                                    "delta": {"content": marker},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 8,
                                "completion_tokens": 8,
                                "total_tokens": 16,
                            },
                        }
                    )
                    + "\n\ndata: [DONE]\n\n"
                ).encode("utf-8")
            )

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    api_key_env = "ZYRA_MARKER_BODY_TEST_API_KEY"
    monkeypatch.setenv(api_key_env, "marker-test-secret")
    try:
        with ProviderControlPlaneClient(
            project_root=ROOT,
            database_path=tmp_path / "provider.sqlite3",
        ) as client:
            client.integrations.upsert(
                IntegrationDefinition(
                    integration_id="marker-test-integration",
                    display_name="Marker Test",
                    kind="bearer",
                    env_names=(api_key_env,),
                    authorization_scheme="Bearer",
                )
            )
            client.catalog.upsert_provider(
                ProviderDefinition(
                    provider_id="marker-test-provider",
                    display_name="Marker Test Provider",
                    integration_id="marker-test-integration",
                    status="active",
                    base_url=f"http://127.0.0.1:{server.server_address[1]}",
                    protocol=ProviderProtocol.OPENAI_CHAT,
                    allowed_hosts=("127.0.0.1",),
                )
            )
            client.catalog.upsert_model(
                ModelDefinition(
                    provider_id="marker-test-provider",
                    model_id="marker-test-model",
                    display_name="Marker Test Model",
                    family="marker-test",
                    maximum_output_tokens=4096,
                    capabilities=ModelCapabilities(streaming=True),
                    endpoint_path="/chat/completions",
                    protocol=ProviderProtocol.OPENAI_CHAT,
                )
            )
            client.credentials.register(
                CredentialRegistration(
                    credential_id="marker-test-credential",
                    integration_id="marker-test-integration",
                    provider_id="marker-test-provider",
                    account_id="marker-test",
                    secret_ref=f"env://{api_key_env}",
                    fingerprint=(
                        "sha256:"
                        + hashlib.sha256(b"marker-test-secret").hexdigest()[:16]
                    ),
                    allowed_models=("marker-test-model",),
                    scopes=("chat.completions",),
                )
            )
            route = client.routing.acquire(
                RouteRequest(
                    run_id="run-marker",
                    task_id="task-marker",
                    node_id="node-marker",
                    session_id="physical-session:task-marker",
                    turn_id="request-marker",
                    preferred_provider_id="marker-test-provider",
                    preferred_model_id="marker-test-model",
                    constraints=RouteConstraints(
                        provider_ids=("marker-test-provider",),
                        model_ids=("marker-test-model",),
                        required_scopes=("chat.completions",),
                    ),
                )
            )
            client.dispatch(
                _marker_dispatch_request(
                    request_id="request-marker",
                    route_id=str(route["routeId"]),
                    run_id="run-marker",
                    task_id="task-marker",
                    node_id="node-marker",
                    marker=marker,
                    idempotency_key="marker-key",
                    payload_digest=payload_digest,
                )
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["messages"] == [
        {
            "role": "user",
            "content": (
                "Return exactly the marker below and no other text.\n" + marker
            ),
        }
    ]
    assert body["max_tokens"] == MARKER_MAXIMUM_OUTPUT_TOKENS
    assert "metadata" not in body
    assert payload_digest not in json.dumps(body, sort_keys=True)


def test_sealed_route_projection_binds_route_without_forwarding_fence_token() -> None:
    route = {
        "schema": "zyra.live-scheduler-route/v1",
        "route_id": "route-test",
        "lease_id": "lease-test",
        "fence_token_digest": "a" * 64,
    }

    with pytest.raises(DispatchRejected) as direct:
        DeploymentNodeRuntime._reject_secret_payload({"canonical_route": route})
    assert direct.value.code == "node_secret_payload_rejected"
    assert direct.value.details == {
        "path": "canonical_route.fence_token_digest"
    }

    projection = _sealed_route_projection(route)

    DeploymentNodeRuntime._reject_secret_payload(projection)
    assert projection["canonical_route"] == {
        "schema": "zyra.live-scheduler-route/v1",
        "route_id": "route-test",
        "lease_id": "lease-test",
    }
    assert projection["canonical_route_digest"] == canonical_digest(route)
    assert projection["canonical_route_redacted_fields"] == [
        "fence_token_digest"
    ]
    changed_fence = _sealed_route_projection(
        {**route, "fence_token_digest": "b" * 64}
    )
    assert changed_fence["canonical_route"] == projection["canonical_route"]
    assert changed_fence["canonical_route_digest"] != projection[
        "canonical_route_digest"
    ]
    null_fence = _sealed_route_projection(
        {**route, "fence_token_digest": None}
    )
    assert null_fence["canonical_route_redacted_fields"] == [
        "fence_token_digest"
    ]


def test_live_research_redirects_stay_inside_frozen_host_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acquirer = LiveHttpSourceAcquirer(
        artifact_root=tmp_path / "sources",
        allowed_hostnames=("www.rfc-editor.org",),
    )
    monkeypatch.setattr(
        acquirer,
        "_resolve_addresses",
        lambda host: ["93.184.216.34"],
    )
    assert (
        acquirer._validated_url("https://www.rfc-editor.org/rfc/rfc9110.txt")
        == "https://www.rfc-editor.org/rfc/rfc9110.txt"
    )
    with pytest.raises(ScenarioRunnerError) as blocked:
        acquirer._validated_url("https://redirect.example/rfc9110.txt")
    assert blocked.value.code == "research_host_not_allowed"


def test_sealed_manifest_preflight_rejects_noncanonical_fault_kind() -> None:
    runner = object.__new__(SealedLongRunRunner)
    with pytest.raises(
        SealedLongRunError,
        match="sealed fault schedule kind is invalid: worker_loss",
    ):
        runner._validate_fault_schedules(
            (
                {
                    "failure_schedule": (
                        {"kind": "requirement_change"},
                        {"kind": "worker_loss"},
                    )
                },
            )
        )


def test_final_research_artifact_uses_canonical_kind_after_publication(
    tmp_path: Path,
) -> None:
    randomized_report = tmp_path / "artifact_abcd1234.json"
    randomized_report.write_text('{"valid": true}\n', encoding="utf-8")
    selected = SealedLongRunRunner._final_artifact(
        (
            {
                "kind": "report",
                "path": str(randomized_report),
            },
        ),
        {"domain": "cross_source_research"},
    )
    assert selected == randomized_report.resolve()


def test_actual_mechanisms_cover_restart_attacks_and_disable_paths(
    tmp_path: Path,
) -> None:
    bundle = SealedMechanismEvidenceRuntime(
        project_root=ROOT,
        state_root=tmp_path,
    ).execute(run_id="sealed-mechanism-run", task_id="sealed-mechanism-task")
    assert bundle["continuity"]["verified_transitions"] == [
        "compact_restore",
        "process_restart",
        "handoff",
        "requirement_revision",
    ]
    assert bundle["continuity"]["poisoned_rejected"] is True
    assert bundle["continuity"]["stale_rejected"] is True
    assert bundle["continuity"]["conflicting_rejected"] is True
    assert bundle["topology_operator"]["invalid_proposal_count"] == 3
    assert bundle["topology_operator"]["unsafe_commit_count"] == 0
    assert bundle["loopx"]["restart_recovered"] is True
    assert bundle["loopx"]["claim_conflict_rejected"] is True
    assert bundle["loopx"]["quota_exhaustion_fail_closed"] is True
    assert all(
        value["disabled_changed_outcome"] is True
        for value in bundle["disable_evidence"].values()
    )
    mechanism_path = tmp_path / "mechanism-bundle.json"
    _json(mechanism_path, bundle)
    persisted = json.loads(mechanism_path.read_text(encoding="utf-8"))
    assert persisted["topology_operator"]["canonical_custody_commit"] is True
    assert len(_evidence_digest(bundle["continuity"])) == 64
