from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from zyra_core import EventRecord, EventType, create_task_state
from zyra_memory import SQLiteStore
from zyra_orchestration.deployment.code_worker_adapter import (
    _WorkspaceLeaseHeartbeat,
    _canonical_evidence_readers,
    _execution_prompt,
    _governed_final_response,
    _physical_permission_session_id,
    _physical_resource_runtime_constraints,
    _provider_failure_summary,
    _provider_prompt_bindings,
    execute_code_worker_operator,
)
from zyra_orchestration.goal_contracts import (
    direct_response_contract,
    goal_delivery_contract,
)
from zyra_runtime.provider_control_plane import (
    CredentialRegistration,
    IntegrationDefinition,
    ModelCapabilities,
    ModelDefinition,
    ProviderControlPlaneClient,
    ProviderDefinition,
    ProviderProtocol,
    ProviderRouteBindingRuntime,
)
from zyra_workspace import WorkspaceManagerConfig, WorkspaceManagerRuntime


ROOT = Path(__file__).resolve().parents[2]


def test_physical_evidence_readers_bind_fork_calls_to_parent_task(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    database_path = tmp_path / "zyra.sqlite3"
    store = SQLiteStore(database_path)
    store.initialize()
    state = create_task_state("Inspect the canonical parent evidence.")
    event = EventRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        node_id=state.root_node_id,
        event_type=EventType.SYSTEM_NOTICE,
        payload={"marker": "parent-only"},
    )
    store.save_checkpoint(state)
    store.append_event(event)

    event_reader, checkpoint_reader = _canonical_evidence_readers(
        {
            "canonical_state_database_path": str(database_path),
        },
        artifact_root=artifact_root,
        parent_task_id=state.task_id,
    )

    events = event_reader(f"{state.task_id}:skill:trace-summary:child")
    checkpoint = checkpoint_reader(
        f"{state.task_id}:skill:verification:child"
    )
    assert [item["payload"]["marker"] for item in events] == ["parent-only"]
    assert checkpoint is not None
    assert checkpoint["task_id"] == state.task_id


def test_physical_evidence_readers_reject_database_outside_state_owner(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError,
        match="canonical state database is not owned by the task artifact root",
    ):
        _canonical_evidence_readers(
            {
                "canonical_state_database_path": str(
                    tmp_path / "unrelated" / "zyra.sqlite3"
                ),
            },
            artifact_root=tmp_path / "artifacts",
            parent_task_id="task-parent",
        )


def test_nonbenchmark_physical_deadline_propagates_closeout_budget() -> None:
    constraints = _physical_resource_runtime_constraints(
        {
            "external_deadline_epoch_ms": 8_765_432_109_876,
            "closeout_reserve_seconds": 37.5,
            "reasoning_timeout_seconds": 240,
        }
    )

    assert constraints == {
        "external_deadline_epoch_ms": 8_765_432_109_876,
        "closeout_reserve_seconds": 37.5,
    }
    assert "benchmark_physical_dispatch" not in constraints


def test_physical_deadline_without_explicit_reserve_derives_bounded_closeout() -> None:
    constraints = _physical_resource_runtime_constraints(
        {
            "external_deadline_epoch_ms": 7_654_321_098_765,
            "reasoning_timeout_seconds": 125,
        }
    )

    assert constraints["external_deadline_epoch_ms"] == 7_654_321_098_765
    assert constraints["closeout_reserve_seconds"] == 90.0


def _provider_runtime_event(
    *,
    phase: str,
    request_id: str,
    initial_prompt_digest: str = "",
    messages_digest: str = "",
    provider_request_digest: str = "",
    run_id: str = "run-provider-binding",
    task_id: str = "task-provider-binding",
    session_id: str = "provider-session",
    worker_request_id: str = "worker-provider-binding",
    runtime_lineage: dict[str, object] | None = None,
) -> dict[str, object]:
    query: dict[str, object] = {
        "phase": phase,
        "run_id": run_id,
        "task_id": task_id,
        "session_id": session_id,
        "worker_request_id": worker_request_id,
    }
    if runtime_lineage is not None:
        query["runtime_lineage"] = runtime_lineage
    if phase == "model_request_prepared":
        query["provider_request"] = {
            "request_id": request_id,
            "messages_digest": messages_digest,
            "initial_user_message_digest": initial_prompt_digest,
        }
    else:
        query["model_stream"] = {
            "request_id": request_id,
            "ok": True,
            "provider_request_digest": provider_request_digest,
        }
    return {"payload": {"query_session": query}}


def test_provider_prompt_binding_survives_same_runtime_context_compaction() -> None:
    expected = "sha256:original-task-prompt"
    events = [
        _provider_runtime_event(
            phase="model_request_prepared",
            request_id="request-0",
            initial_prompt_digest=expected,
            messages_digest="sha256:messages-0",
        ),
        _provider_runtime_event(
            phase="model_stream_report",
            request_id="request-0",
            provider_request_digest="sha256:provider-request-0",
        ),
        _provider_runtime_event(
            phase="model_request_prepared",
            request_id="request-1",
            initial_prompt_digest="sha256:compaction-or-restore-prompt",
            messages_digest="sha256:messages-1",
        ),
        _provider_runtime_event(
            phase="model_stream_report",
            request_id="request-1",
            provider_request_digest="sha256:provider-request-1",
        ),
    ]

    bindings = _provider_prompt_bindings(
        runtime_events=events,
        expected_initial_prompt_digest=expected,
    )

    assert bindings["request-0"]["goal_present"] is True
    assert bindings["request-0"]["goal_binding"] == "initial_prompt"
    assert bindings["request-1"]["goal_present"] is True
    assert bindings["request-1"]["goal_binding"] == "runtime_continuation"
    assert bindings["request-1"]["provider_request_digest"] == (
        "sha256:provider-request-1"
    )


def test_provider_prompt_binding_rejects_different_runtime_identity() -> None:
    expected = "sha256:original-task-prompt"
    events = [
        _provider_runtime_event(
            phase="model_request_prepared",
            request_id="request-0",
            initial_prompt_digest=expected,
            messages_digest="sha256:messages-0",
        ),
        _provider_runtime_event(
            phase="model_request_prepared",
            request_id="request-foreign",
            initial_prompt_digest="sha256:foreign-prompt",
            messages_digest="sha256:foreign-messages",
            session_id="foreign-session",
        ),
    ]

    bindings = _provider_prompt_bindings(
        runtime_events=events,
        expected_initial_prompt_digest=expected,
    )

    assert bindings["request-0"]["goal_present"] is True
    assert bindings["request-foreign"]["goal_present"] is False
    assert bindings["request-foreign"]["goal_binding"] == "unbound"


@pytest.mark.parametrize(
    ("relation", "child_session"),
    [("skill", "provider-session"), ("agent", "agent-session")],
)
def test_provider_prompt_binding_accepts_explicit_runtime_descendant(
    relation: str,
    child_session: str,
) -> None:
    expected = "sha256:original-task-prompt"
    lineage = {
        "schema": "zyra.runtime-lineage/v1",
        "relation": relation,
        "relation_id": f"{relation}-invocation",
        "parent_run_id": "run-provider-binding",
        "parent_task_id": "task-provider-binding",
        "parent_session_id": "provider-session",
        "parent_worker_request_id": "worker-provider-binding",
    }
    child_identity = {
        "task_id": f"task-{relation}-child",
        "session_id": child_session,
        "worker_request_id": f"worker-{relation}-child",
        "runtime_lineage": lineage,
    }
    events = [
        _provider_runtime_event(
            phase="model_request_prepared",
            request_id="request-root",
            initial_prompt_digest=expected,
            messages_digest="sha256:messages-root",
        ),
        _provider_runtime_event(
            phase="model_stream_report",
            request_id="request-root",
            provider_request_digest="sha256:provider-request-root",
        ),
        _provider_runtime_event(
            phase="model_request_prepared",
            request_id="request-child",
            initial_prompt_digest="sha256:child-prompt",
            messages_digest="sha256:messages-child",
            **child_identity,
        ),
        _provider_runtime_event(
            phase="model_stream_report",
            request_id="request-child",
            provider_request_digest="sha256:provider-request-child",
            **child_identity,
        ),
    ]

    bindings = _provider_prompt_bindings(
        runtime_events=events,
        expected_initial_prompt_digest=expected,
    )

    assert bindings["request-child"]["goal_present"] is True
    assert bindings["request-child"]["goal_binding"] == (
        f"authorized_{relation}_descendant"
    )
    assert bindings["request-child"]["runtime_identity_verified"] is True
    assert bindings["request-child"]["provider_request_digest"] == (
        "sha256:provider-request-child"
    )


def test_provider_prompt_binding_rejects_forged_descendant_and_foreign_report() -> None:
    expected = "sha256:original-task-prompt"
    forged_lineage = {
        "schema": "zyra.runtime-lineage/v1",
        "relation": "skill",
        "relation_id": "forged-invocation",
        "parent_run_id": "run-provider-binding",
        "parent_task_id": "different-parent",
        "parent_session_id": "provider-session",
        "parent_worker_request_id": "worker-provider-binding",
    }
    events = [
        _provider_runtime_event(
            phase="model_request_prepared",
            request_id="request-root",
            initial_prompt_digest=expected,
            messages_digest="sha256:messages-root",
        ),
        _provider_runtime_event(
            phase="model_request_prepared",
            request_id="request-forged",
            initial_prompt_digest="sha256:child-prompt",
            messages_digest="sha256:messages-forged",
            task_id="task-forged-child",
            worker_request_id="worker-forged-child",
            runtime_lineage=forged_lineage,
        ),
        _provider_runtime_event(
            phase="model_stream_report",
            request_id="request-root",
            provider_request_digest="sha256:foreign-report",
            session_id="foreign-session",
        ),
    ]

    bindings = _provider_prompt_bindings(
        runtime_events=events,
        expected_initial_prompt_digest=expected,
    )

    assert bindings["request-forged"]["goal_present"] is False
    assert bindings["request-forged"]["goal_binding"] == "unbound"
    assert "provider_request_digest" not in bindings["request-root"]


def test_workspace_lease_heartbeat_keeps_long_worker_access_alive(
    tmp_path: Path,
) -> None:
    manager = WorkspaceManagerRuntime(
        WorkspaceManagerConfig(
            state_root=tmp_path / "workspace-state",
            data_root=tmp_path / "workspace-data",
            lease_ttl_seconds=0.15,
        )
    )
    created = manager.create_for_task(
        run_id="run-heartbeat",
        task_id="task-heartbeat",
        session_id="session-heartbeat",
        worker_id="worker-heartbeat",
    )

    class EditPort:
        def current_access(self):
            return created.access

    heartbeat = _WorkspaceLeaseHeartbeat(
        manager,
        EditPort(),  # type: ignore[arg-type]
        lease_ttl_seconds=0.15,
        interval_seconds=0.03,
    )
    heartbeat.start()
    time.sleep(0.45)
    heartbeat.stop()
    heartbeat.raise_if_failed()

    assert manager.internal_task_root(created.access).is_dir()
    lease = manager.store.get_lease(created.access.lease_id)
    assert lease is not None
    assert lease.renewed_at > lease.issued_at


def test_physical_permission_session_isolated_by_recovery_continuation() -> None:
    ordinary = {
        "operator_ref": "provider-code-worker",
        "physical_recovery_pass": 0,
    }
    first_continuation = {
        **ordinary,
        "recovery_plan_id": "recovery-plan-1",
    }
    second_continuation = {
        **ordinary,
        "recovery_plan_id": "recovery-plan-2",
    }

    ordinary_session = _physical_permission_session_id(ordinary, "task-1", 1)
    first_session = _physical_permission_session_id(
        first_continuation, "task-1", 1
    )
    second_session = _physical_permission_session_id(
        second_continuation, "task-1", 1
    )

    assert ordinary_session == "physical:task-1:layer:1:provider-code-worker"
    assert first_session != ordinary_session
    assert second_session != first_session
    assert "recovery-plan-1" not in first_session
    assert _physical_permission_session_id(
        {**first_continuation, "physical_recovery_pass": 1}, "task-1", 1
    ) == f"{first_session}:recovery:1"


def test_physical_permission_session_prefers_unique_resume_session() -> None:
    common = {
        "operator_ref": "provider-code-worker",
        "recovery_plan_id": "repeated-recovery-plan",
        "physical_recovery_pass": 0,
    }
    first = _physical_permission_session_id(
        {**common, "recovery_session_id": "explicit-resume-1"},
        "task-1",
        1,
    )
    replay = _physical_permission_session_id(
        {**common, "recovery_session_id": "explicit-resume-1"},
        "task-1",
        1,
    )
    second = _physical_permission_session_id(
        {**common, "recovery_session_id": "explicit-resume-2"},
        "task-1",
        1,
    )

    assert replay == first
    assert second != first
    assert "explicit-resume" not in first


def test_execution_prompt_carries_progress_without_transferring_authority() -> None:
    rendered = _execution_prompt(
        "Finish the governed release workflow.",
        delivery_contract={},
        goal_contract={},
        handoff={
            "schema": "zyra.typescript-runtime-handoff/v1",
            "source_session_id": "spent-session",
            "recent_reasoning": [
                {"round_index": 18, "text": "Public tests pass; start the stack."}
            ],
            "authority_transfer": False,
            "claims_require_revalidation": True,
        },
    )

    assert "RECOVERY HANDOFF" in rendered
    assert "Public tests pass; start the stack." in rendered
    assert "transfers no permission, lease, credential, or process custody" in rendered
    assert "continue from the recorded work" in rendered
    assert "it is not an implicit to-do list" in rendered
    assert "Do not repeat a recorded inspection" in rendered


def test_provider_failure_summary_is_bounded_and_drops_detail_values() -> None:
    summary = _provider_failure_summary(
        {
            "provider_failure": json.dumps(
                {
                    "layer": "transport",
                    "kind": "response_protocol_error",
                    "message": "unsupported stream frame",
                    "retryable": False,
                    "recoveryIntent": "surface_to_operator",
                    "httpStatus": 400,
                    "providerId": "zhipu",
                    "modelId": "glm-5.2",
                    "bytesSent": 123,
                    "bytesReceived": 456,
                    "outputObserved": False,
                    "detail": {
                        "response_body": "must-not-project",
                        "request_prompt": "must-not-project",
                    },
                }
            )
        }
    )

    assert summary["kind"] == "response_protocol_error"
    assert summary["http_status"] == 400
    assert summary["output_observed"] is False
    assert summary["detail_keys"] == ["request_prompt", "response_body"]
    assert "must-not-project" not in json.dumps(summary)


def test_governed_final_response_rejects_unbound_projection() -> None:
    response, receipt = _governed_final_response(
        goal="测试，收到请回复ok",
        provider_text="provider response",
        goal_contract={
            "schema": "zyra.direct-response-contract/v1",
            "kind": "direct_response",
            "expected_response": "forged",
            "match_mode": "exact_trimmed",
        },
    )

    assert response == "provider response"
    assert receipt["applicable"] is False
    assert receipt["contract_bound"] is False
    assert receipt["projected"] is False


def test_physical_code_worker_runs_model_tool_observation_model_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []
    authorization_seen: list[bool] = []
    physical_call_returned = threading.Event()
    streamed_assistant_events: list[tuple[int, dict[str, object], bool]] = []

    def capture_runtime_event(
        payload: dict[str, object],
        *,
        transport_sequence: int,
    ) -> None:
        if payload.get("schema") != "zyra.provider-assistant-presentation/v1":
            return
        streamed_assistant_events.append(
            (
                transport_sequence,
                dict(payload),
                physical_call_returned.is_set(),
            )
        )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(
                self.rfile.read(int(self.headers.get("content-length") or 0)).decode(
                    "utf-8"
                )
            )
            requests.append(body)
            authorization_seen.append(
                self.headers.get("authorization") == "Bearer loop-secret"
            )
            serialized_request = json.dumps(body, ensure_ascii=False)
            if "收到请回复ok" in serialized_request:
                event = {
                    "choices": [
                        {
                            # Exercise governed delivery when a real provider
                            # adds punctuation despite an exact response
                            # contract in its bound prompt.
                            "delta": {"content": "ok."},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 50,
                        "completion_tokens": 1,
                        "total_tokens": 51,
                    },
                }
            elif any(
                isinstance(message, dict)
                and message.get("role") == "tool"
                for message in body.get("messages") or ()
            ):
                event = {
                    "choices": [
                        {
                            "delta": {
                                "content": "Created and verified smoke.txt."
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 140,
                        "completion_tokens": 9,
                        "total_tokens": 149,
                    },
                }
            else:
                event = {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-write-smoke",
                                        "type": "function",
                                        "function": {
                                            "name": "file_write",
                                            "arguments": json.dumps(
                                                {
                                                    "path": "smoke.txt",
                                                    "content": "ZYRA_SMOKE_OK\n",
                                                }
                                            ),
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 110,
                        "completion_tokens": 18,
                        "total_tokens": 128,
                    },
                }
            response = (
                "data: "
                + json.dumps(event, separators=(",", ":"))
                + "\n\ndata: [DONE]\n\n"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for name in ("ZAI_API_KEY", "DEEPSEEK_API_KEY", "KIMI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ZYRA_LOOP_TEST_API_KEY", "loop-secret")

    run_id = "run-physical-reasoning-loop"
    task_id = "task-physical-reasoning-loop"
    workspace_session_id = "session-physical-reasoning-loop"
    artifact_root = tmp_path / "artifacts"
    workspace_config = WorkspaceManagerConfig(
        state_root=tmp_path / "workspace-state",
        data_root=tmp_path / "workspace-data",
    )
    manager = WorkspaceManagerRuntime(workspace_config)
    manager.create_for_task(
        run_id=run_id,
        task_id=task_id,
        session_id=workspace_session_id,
        worker_id="test-setup",
        idempotency_key="create-physical-reasoning-loop",
    )
    provider_database = artifact_root / ".provider-control-plane" / "provider.sqlite3"
    try:
        with ProviderControlPlaneClient(
            project_root=ROOT,
            database_path=provider_database,
        ) as client:
            client.integrations.upsert(
                IntegrationDefinition(
                    integration_id="loop-test-bearer",
                    display_name="Loop Test Bearer",
                    kind="bearer",
                    env_names=("ZYRA_LOOP_TEST_API_KEY",),
                    authorization_scheme="Bearer",
                )
            )
            client.catalog.upsert_provider(
                ProviderDefinition(
                    provider_id="loop-test-provider",
                    display_name="Loop Test Provider",
                    integration_id="loop-test-bearer",
                    status="active",
                    base_url=f"http://127.0.0.1:{server.server_address[1]}",
                    protocol=ProviderProtocol.OPENAI_CHAT,
                    allowed_hosts=("127.0.0.1",),
                )
            )
            client.catalog.upsert_model(
                ModelDefinition(
                    provider_id="loop-test-provider",
                    model_id="loop-test-model",
                    display_name="Loop Test Model",
                    family="loop-test",
                    capabilities=ModelCapabilities(tools=True, streaming=True),
                    endpoint_path="/chat/completions",
                    protocol=ProviderProtocol.OPENAI_CHAT,
                )
            )
            client.credentials.register(
                CredentialRegistration(
                    credential_id="loop-test-credential",
                    integration_id="loop-test-bearer",
                    provider_id="loop-test-provider",
                    account_id="loop-test",
                    secret_ref="env://ZYRA_LOOP_TEST_API_KEY",
                    fingerprint=(
                        "sha256:"
                        + hashlib.sha256(b"loop-secret").hexdigest()[:16]
                    ),
                    allowed_models=("loop-test-model",),
                )
            )
        route = ProviderRouteBindingRuntime(
            project_root=ROOT,
            database_path=provider_database,
            allow_explicit_sim_bootstrap=False,
        ).bind(
            run_id=run_id,
            task_id=task_id,
            node_id="cloud-code-worker-test",
            session_id="provider-session-physical-reasoning-loop",
            turn_id="provider-turn-physical-reasoning-loop",
            purpose="code",
            preferred_provider_id="loop-test-provider",
            preferred_model_id="loop-test-model",
            require_tools=True,
            require_streaming=True,
        )
        result = execute_code_worker_operator(
            payload={
                "run_id": run_id,
                "task_id": task_id,
                "goal": (
                    "Create smoke.txt in the governed workspace containing "
                    "exactly one line: ZYRA_SMOKE_OK"
                ),
                "layer_index": 1,
                "operator_ref": "code-worker",
                "code_worker_context": {
                    "project_root": str(ROOT),
                    "artifact_root": str(artifact_root),
                    "canonical_state_database_path": str(
                        tmp_path / "zyra.sqlite3"
                    ),
                    "model_id": "loop-test-model",
                    "max_turns": 6,
                    "workspace_manager": {
                        "state_root": str(workspace_config.state_root),
                        "data_root": str(workspace_config.data_root),
                        "session_id": workspace_session_id,
                        "local_enabled": True,
                        "default_backend_id": workspace_config.default_backend_id,
                        "lease_ttl_seconds": workspace_config.lease_ttl_seconds,
                        "reservation_ttl_seconds": (
                            workspace_config.reservation_ttl_seconds
                        ),
                        "max_receipts": workspace_config.max_receipts,
                    },
                    "provider_constraints": route.runtime_constraints(
                        database_path=provider_database
                    ),
                },
            },
            node_id="cloud-code-worker-test",
            node_data_root=tmp_path / "node",
            runtime_event_sink=capture_runtime_event,
        )
        physical_call_returned.set()

        direct_task_id = "task-physical-direct-response"
        manager.create_for_task(
            run_id=run_id,
            task_id=direct_task_id,
            session_id=workspace_session_id,
            worker_id="test-setup",
            idempotency_key="create-physical-direct-response",
        )
        direct_route = ProviderRouteBindingRuntime(
            project_root=ROOT,
            database_path=provider_database,
            allow_explicit_sim_bootstrap=False,
        ).bind(
            run_id=run_id,
            task_id=direct_task_id,
            node_id="cloud-code-worker-test",
            session_id="provider-session-physical-direct-response",
            turn_id="provider-turn-physical-direct-response",
            purpose="code",
            preferred_provider_id="loop-test-provider",
            preferred_model_id="loop-test-model",
            require_tools=True,
            require_streaming=True,
        )
        direct_goal = "测试，收到请回复ok"
        response_contract = direct_response_contract(direct_goal)
        assert response_contract is not None
        direct_result = execute_code_worker_operator(
            payload={
                "run_id": run_id,
                "task_id": direct_task_id,
                "goal": direct_goal,
                "layer_index": 1,
                "operator_ref": "code-worker",
                "goal_contract": response_contract.to_dict(),
                "delivery_contract": goal_delivery_contract(
                    direct_goal
                ).to_dict(),
                "code_worker_context": {
                    "project_root": str(ROOT),
                    "artifact_root": str(artifact_root),
                    "canonical_state_database_path": str(
                        tmp_path / "zyra.sqlite3"
                    ),
                    "model_id": "loop-test-model",
                    "max_turns": 3,
                    "workspace_manager": {
                        "state_root": str(workspace_config.state_root),
                        "data_root": str(workspace_config.data_root),
                        "session_id": workspace_session_id,
                        "local_enabled": True,
                        "default_backend_id": workspace_config.default_backend_id,
                        "lease_ttl_seconds": workspace_config.lease_ttl_seconds,
                        "reservation_ttl_seconds": (
                            workspace_config.reservation_ttl_seconds
                        ),
                        "max_receipts": workspace_config.max_receipts,
                    },
                    "provider_constraints": direct_route.runtime_constraints(
                        database_path=provider_database
                    ),
                },
            },
            node_id="cloud-code-worker-test",
            node_data_root=tmp_path / "node",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    access = manager.acquire_for_worker(
        task_id=task_id,
        session_id=workspace_session_id,
        worker_id="test-verifier",
    )
    smoke = manager.internal_task_root(access) / "smoke.txt"
    assert smoke.read_text(encoding="utf-8") == "ZYRA_SMOKE_OK\n"
    assert (
        manager.internal_task_root(access)
        / ".zyra"
        / "skills"
        / "zyra-bundled"
        / "pdf-analysis"
        / "SKILL.md"
    ).is_file()
    assert len(requests) == 3
    assert all(authorization_seen)
    assert "ZYRA_SMOKE_OK" in json.dumps(requests[0])
    second_messages = requests[1]["messages"]
    assert isinstance(second_messages, list)
    assert any(
        isinstance(message, dict)
        and message.get("role") == "tool"
        and message.get("tool_call_id") == "call-write-smoke"
        and "Committed smoke.txt" in str(message.get("content") or "")
        for message in second_messages
    )
    assert result["workspace_delta"]["created"] == ["smoke.txt"]
    assert result["final_text"] == "Created and verified smoke.txt."
    assert result["provider_call"]["provider_called"] is True
    assert result["provider_call"]["task_execution_verified"] is True
    assert result["provider_call"]["prompt_goal_bound"] is True
    assert result["provider_call"]["external_model_request"] is False
    assert result["provider_call"]["live"] is False
    assert result["provider_call"]["synthetic_usage"] is False
    assert result["provider_call"]["usage"]["input_tokens"] == 250
    assert result["provider_call"]["usage"]["output_tokens"] == 27
    streamed_phases = [
        str(payload.get("phase") or "")
        for _sequence, payload, _after_return in streamed_assistant_events
    ]
    assert {
        "assistant_text_started",
        "assistant_text_delta",
        "assistant_text_ended",
    }.issubset(streamed_phases), streamed_phases
    assert all(
        after_return is False
        for _sequence, _payload, after_return in streamed_assistant_events
    )
    assert any(
        payload.get("content") == "Created and verified smoke.txt."
        for _sequence, payload, _after_return in streamed_assistant_events
    )
    assert all(
        len(str(payload.get("content") or "").encode("utf-8")) <= 1_024
        for _sequence, payload, _after_return in streamed_assistant_events
    )
    assistant_stream_json = json.dumps(
        [payload for _sequence, payload, _after_return in streamed_assistant_events],
        ensure_ascii=False,
    )
    assert '"arguments"' not in assistant_stream_json
    assert '"tool_calls"' not in assistant_stream_json
    assert '"thinking"' not in assistant_stream_json
    public_phases = {
        str(event.get("payload", {}).get("query_session", {}).get("phase") or "")
        for event in result["runtime_events"]
    }
    assert "model_stream_frame" not in public_phases
    assert "message_delta" not in public_phases
    assert {
        "assistant_text_started",
        "assistant_text_ended",
    }.issubset(public_phases), sorted(public_phases)
    assert "assistant_text_delta" not in public_phases
    public_event_json = json.dumps(result["runtime_events"], ensure_ascii=False)
    assert '"user_content"' not in public_event_json
    assert '"messages"' not in public_event_json
    assert '"tool_result"' not in public_event_json
    assert '"worker_result"' not in public_event_json
    assert "tool_result_commitment" in public_event_json
    assert "worker_result_commitment" in public_event_json
    assert all(
        item["provider_request_digest"]
        and item["provider_request_digest_verified"] is True
        and item["runtime_provider_request_digest"]
        == item["provider_request_digest"]
        and item["prompt_messages_digest"]
        and item["prompt_goal_bound"] is True
        for item in result["provider_call"]["calls"]
    )
    prepared_requests = [
        event["payload"]["query_session"]["provider_request"]
        for event in result["runtime_events"]
        if isinstance(event.get("payload"), dict)
        and isinstance(event["payload"].get("query_session"), dict)
        and event["payload"]["query_session"].get("phase")
        == "model_request_prepared"
    ]
    assert prepared_requests
    assert all(
        "messages" not in prepared
        and "tools" not in prepared
        and prepared.get("messages_digest")
        and prepared.get("initial_user_message_digest")
        and prepared.get("tools_digest")
        for prepared in prepared_requests
    )
    assert "loop-secret" not in json.dumps(requests)
    assert direct_result["final_text"] == "ok"
    assert direct_result["execution_evidence"]["final_response_projection"] == {
        "schema": "zyra.governed-final-response/v1",
        "applicable": True,
        "contract_bound": True,
        "match_mode": "exact_trimmed",
        "expected_response_digest": (
            "sha256:" + hashlib.sha256(b"ok").hexdigest()
        ),
        "provider_response_digest": (
            "sha256:" + hashlib.sha256(b"ok.").hexdigest()
        ),
        "provider_exact": False,
        "projected": True,
        "projection_authority": "compiled-explicit-direct-response-contract",
    }
    assert direct_result["provider_call"]["provider_called"] is True
    assert direct_result["provider_call"]["prompt_goal_bound"] is True
    assert direct_result["workspace_delta"]["changed"] == []
    assert "entire final response must be exactly" in json.dumps(
        requests[2]
    )
