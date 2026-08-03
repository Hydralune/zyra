from __future__ import annotations

import pytest

from zyra_orchestration.deployment.errors import DispatchRejected
from zyra_orchestration.deployment.models import (
    DeploymentProfile,
    Sensitivity,
    Workload,
)
from zyra_orchestration.deployment.node_runtime import DeploymentNodeRuntime
from zyra_orchestration.deployment.profiles import default_profile_policies
from zyra_orchestration.goal_contracts import (
    direct_response_contract,
    goal_contract_matches_projection,
    validate_direct_response,
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


def test_physical_code_worker_emits_verified_user_response(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    goal = "测试，收到请回复ok"
    contract = direct_response_contract(goal)
    assert contract is not None
    result = runtime._phase2_operator_adapter(
        operator_ref="worker:local-code-worker@1",
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

    assert result["adapter_id"] == "worker.local-code-worker.direct-response"
    assert result["domain_result"]["kind"] == "direct_response"
    assert result["domain_result"]["goal_contract_satisfied"] is True
    assert result["domain_artifact"]["kind"] == "text"
    assert result["domain_artifact"]["content"] == "ok"
    assert sorted(result["contract_outputs"]) == [
        "artifact_refs",
        "usage",
        "worker_result",
    ]


def test_physical_adapter_rejects_stale_goal_contract(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    goal = "收到请回复ok"
    with pytest.raises(
        DispatchRejected,
        match="stale or mismatched goal contract",
    ):
        runtime._phase2_operator_adapter(
            operator_ref="worker:local-code-worker@1",
            operator={
                "operator_type": "worker",
                "output_contract": ["artifact_refs", "usage", "worker_result"],
            },
            operator_runtime="CodeWorkerRuntime",
            goal=goal,
            goal_contract={"schema": "zyra.direct-response-contract/v1"},
            layer_index=1,
            workload=_workload(),
        )
