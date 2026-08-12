from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from zyra_orchestration.deployment.code_worker_adapter import (
    _WorkspaceLeaseHeartbeat,
    _execution_prompt,
    _governed_final_response,
    _physical_permission_session_id,
    _provider_failure_summary,
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
        )

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
    public_phases = {
        str(event.get("payload", {}).get("query_session", {}).get("phase") or "")
        for event in result["runtime_events"]
    }
    assert "model_stream_frame" not in public_phases
    assert "message_delta" not in public_phases
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
