from __future__ import annotations

from zyra_orchestration.deployment.models import (
    DeploymentProfile,
    Sensitivity,
    Workload,
)
from zyra_orchestration.deployment.node_runtime import DeploymentNodeRuntime
from zyra_orchestration.deployment.profiles import default_profile_policies
from zyra_orchestration.goal_contracts import (
    direct_response_contract,
    goal_delivery_contract,
    goal_contract_matches_projection,
    validate_direct_response,
    validate_goal_delivery,
)


def _workload() -> Workload:
    return Workload(
        workload_id="workload_direct_response",
        task_id="task_direct_response",
        run_id="run_direct_response",
        operation="phase2-operator-execution",
        payload={},
        sensitivity=Sensitivity.INTERNAL,
        complexity=1,
        latency_sla_ms=1_000,
        cpu_units=1,
        memory_mb=16,
    )


def _runtime(tmp_path) -> DeploymentNodeRuntime:
    return DeploymentNodeRuntime(
        node_id="device-test",
        generation_id="generation-test",
        policy=default_profile_policies()[DeploymentProfile.DEVICE],
        data_root=tmp_path,
        credential_presence={},
    )


def test_extracts_bounded_explicit_reply_contracts() -> None:
    chinese = direct_response_contract("测试，收到请回复ok")
    quoted = direct_response_contract('只回复“已经收到”')
    english = direct_response_contract("When ready, respond with OK.")

    assert chinese is not None and chinese.expected_response == "ok"
    assert quoted is not None and quoted.expected_response == "已经收到"
    assert english is not None and english.expected_response == "OK"
    assert direct_response_contract("请回复一份完整报告") is None
    assert direct_response_contract("分析仓库并修复测试") is None


def test_verification_is_exact_and_projection_bound() -> None:
    contract = direct_response_contract("收到请回复ok")
    assert contract is not None
    assert validate_direct_response("收到请回复ok", " ok ")["passed"] is True
    assert validate_direct_response("收到请回复ok", "任务完成")["passed"] is False
    assert goal_contract_matches_projection(
        "收到请回复ok",
        contract.to_dict(),
    ) is True
    stale = contract.to_dict()
    stale["expected_response"] = "not-ok"
    assert goal_contract_matches_projection("收到请回复ok", stale) is False


def test_physical_code_worker_uses_typescript_provider_tool_loop(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)
    goal = "测试，收到请回复ok"
    contract = direct_response_contract(goal)
    assert contract is not None

    def execute(**_kwargs):
        return {
            "provider_call": {
                "provider_called": True,
                "task_execution_verified": True,
                "prompt_goal_bound": True,
                "synthetic_usage": False,
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 3,
                    "total_tokens": 15,
                },
                "calls": [{"request_id": "provider-request-1"}],
            },
            "execution_evidence": {"tool_call_count": 0},
            "workspace_delta": {
                "created": [],
                "modified": [],
                "deleted": [],
                "changed": [],
            },
            "final_text": "ok",
            "runtime_events": [],
            "runtime_artifacts": [],
        }

    monkeypatch.setattr(
        "zyra_orchestration.deployment.code_worker_adapter.execute_code_worker_operator",
        execute,
    )
    result = runtime._phase2_operator_adapter(
        payload={"run_id": "run_direct_response"},
        operator_ref="worker:provider-code-worker@1",
        operator={
            "operator_type": "worker",
            "output_contract": ["artifact_refs", "usage", "worker_result"],
        },
        operator_runtime="CodeWorkerRuntime",
        goal=goal,
        goal_contract=contract.to_dict(),
        layer_index=1,
        workload=_workload(),
    )

    assert (
        result["adapter_id"]
        == "worker.code-worker.typescript-provider-tool-loop"
    )
    assert result["domain_result"]["kind"] == "code_worker_execution"
    assert result["provider_call"]["provider_called"] is True
    assert result["contract_outputs"]["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 3,
        "total_tokens": 15,
        "provider_called": True,
        "synthetic": False,
    }
    assert sorted(result["contract_outputs"]) == [
        "artifact_refs",
        "usage",
        "worker_result",
    ]


def test_delivery_contract_verifies_requested_file_and_real_provider(tmp_path) -> None:
    goal = '创建 smoke.txt 文件，内容是一行“ZYRA_SMOKE_OK”。'
    contract = goal_delivery_contract(goal)
    provider = {
        "provider_called": True,
        "task_execution_verified": True,
        "prompt_goal_bound": True,
        "synthetic_usage": False,
        "calls": [{"request_id": "provider-request-1"}],
    }
    missing = validate_goal_delivery(
        goal,
        projection=contract.to_dict(),
        workspace_root=tmp_path,
        workspace_delta={"created": ["smoke.txt"]},
        final_response="已完成",
        provider_evidence=provider,
    )
    assert missing["passed"] is False
    assert missing["checks"]["required_paths_present"] is False

    (tmp_path / "smoke.txt").write_text("ZYRA_SMOKE_OK\n", encoding="utf-8")
    passed = validate_goal_delivery(
        goal,
        projection=contract.to_dict(),
        workspace_root=tmp_path,
        workspace_delta={"created": ["smoke.txt"]},
        final_response="已完成",
        provider_evidence=provider,
    )
    assert passed["passed"] is True

    (tmp_path / "smoke.txt").write_text("ZYRA_SMOKE_OK\n\n", encoding="utf-8")
    extra_line = validate_goal_delivery(
        goal,
        projection=contract.to_dict(),
        workspace_root=tmp_path,
        workspace_delta={"created": ["smoke.txt"]},
        final_response="已完成",
        provider_evidence=provider,
    )
    assert extra_line["checks"]["expected_file_contents_match"] is False


def test_delivery_contract_recognizes_plain_chinese_create_file_wording() -> None:
    contract = goal_delivery_contract(
        "建一个 smoke.txt 文件，内容是一行指定文字。"
    )
    assert contract.workspace_mutation_required is True
    assert contract.required_paths == ("smoke.txt",)
    assert contract.expected_file_contents == (("smoke.txt", "指定文字"),)


def test_delivery_contract_does_not_invent_paths_from_container_paths_emails_or_versions() -> None:
    contract = goal_delivery_contract(
        "Create `/app/meeting_scheduled.ics` for alice@example.com with VERSION:2.0."
    )
    assert contract.workspace_mutation_required is True
    assert contract.required_paths == ()

    relative = goal_delivery_contract(
        "Create meeting_scheduled.ics for alice@example.com with VERSION:2.0."
    )
    assert relative.required_paths == ("meeting_scheduled.ics",)

    hyphenated_absolute = goal_delivery_contract(
        "Create /workspace/deadline-proof.txt and verify it."
    )
    assert hyphenated_absolute.workspace_mutation_required is True
    assert hyphenated_absolute.required_paths == ()


def test_delivery_contract_rejects_synthetic_provider_usage(tmp_path) -> None:
    goal = "分析当前任务并给出结论"
    verification = validate_goal_delivery(
        goal,
        projection=goal_delivery_contract(goal).to_dict(),
        workspace_root=tmp_path,
        workspace_delta={},
        final_response="结论",
        provider_evidence={
            "provider_called": True,
            "task_execution_verified": True,
            "prompt_goal_bound": True,
            "synthetic_usage": True,
            "calls": [{"request_id": "fake"}],
        },
    )
    assert verification["passed"] is False
    assert (
        verification["checks"]["provider_reasoning_executed"] is False
    )


def test_delivery_contract_requires_mutation_for_implementation_goals() -> None:
    for goal in (
        "修复执行层并完成验证",
        "Implement the requested runtime fix",
        "Refactor the worker lifecycle",
    ):
        contract = goal_delivery_contract(goal)
        assert contract.workspace_mutation_required is True


def test_deployment_secret_gate_allows_usage_and_credential_references() -> None:
    DeploymentNodeRuntime._reject_secret_payload(
        {
            "model_output_token_limit": 8192,
            "prompt_tokens": 12,
            "completion_tokens": 3,
            "provider_credential_id": "credential-ref-1",
            "provider_credential_environment_name": "ZAI_API_KEY",
            "provider_credential_fingerprint": "sha256:reference-only",
            "credential_material_persisted": False,
        }
    )
