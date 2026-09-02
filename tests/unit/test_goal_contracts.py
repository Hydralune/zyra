from __future__ import annotations

import pytest

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
    independent_role_evidence_satisfied,
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
    assert direct_response_contract("请读取 proof.txt，并只回复其中的内容。") is None
    assert direct_response_contract("读取结果后只回复文件内容") is None


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


def test_role_separation_accepts_inline_analysis_with_distinct_fork_roles() -> None:
    required = {
        "codebase-analysis",
        "code-change",
        "failure-recovery",
        "verification",
    }
    invocations = [
        {
            "name": "codebase-analysis",
            "execution_mode": "inline",
            "child_task_id": "inline-analysis",
        },
        {
            "name": "code-change",
            "execution_mode": "inline",
            "child_task_id": "inline-change",
        },
        {
            "name": "failure-recovery",
            "execution_mode": "fork",
            "child_task_id": "child-recovery",
        },
        {
            "name": "verification",
            "execution_mode": "fork",
            "child_task_id": "child-verifier",
        },
    ]

    assert independent_role_evidence_satisfied(required, invocations) is True


def test_role_separation_rejects_repeated_children_in_one_role() -> None:
    invocations = [
        {
            "name": "verification",
            "execution_mode": "fork",
            "child_task_id": "child-verifier-one",
        },
        {
            "name": "verification",
            "execution_mode": "fork",
            "child_task_id": "child-verifier-two",
        },
    ]

    assert (
        independent_role_evidence_satisfied(
            {"codebase-analysis", "verification"},
            invocations,
        )
        is False
    )


def test_role_separation_keeps_forked_material_reviewer_pairing() -> None:
    invocations = [
        {
            "name": "failure-recovery",
            "execution_mode": "fork",
            "child_task_id": "child-recovery",
        },
        {
            "name": "verification",
            "execution_mode": "fork",
            "child_task_id": "child-verifier",
        },
    ]

    assert (
        independent_role_evidence_satisfied(
            {"pdf-analysis", "failure-recovery", "verification"},
            invocations,
        )
        is False
    )


@pytest.mark.parametrize(
    ("required", "invocations", "expected"),
    (
        (
            {"codebase-analysis", "failure-recovery", "verification"},
            (
                ("verification", "fork", "review-91"),
                ("codebase-analysis", "inline", "analysis-local"),
                ("failure-recovery", "fork", "recovery-27"),
            ),
            True,
        ),
        (
            {"codebase-analysis", "verification"},
            (
                ("verification", "fork", "shared-child"),
                ("failure-recovery", "fork", "shared-child"),
            ),
            False,
        ),
        (
            {"codebase-analysis", "verification"},
            (
                ("failure-recovery", "fork", "recovery-a"),
                ("failure-recovery", "fork", "recovery-b"),
            ),
            False,
        ),
        (
            {"web-research", "failure-recovery", "verification"},
            (
                ("verification", "fork", "review-web"),
                ("web-research", "fork", "source-web"),
                ("failure-recovery", "fork", "recovery-web"),
            ),
            True,
        ),
    ),
    ids=(
        "inline-material-order-and-identifiers-vary",
        "one-child-cannot-claim-two-roles",
        "required-verifier-must-execute",
        "fork-capable-material-keeps-review-pairing",
    ),
)
def test_role_separation_is_identity_and_order_independent(
    required: set[str],
    invocations: tuple[tuple[str, str, str], ...],
    expected: bool,
) -> None:
    records = [
        {
            "name": name,
            "execution_mode": execution_mode,
            "child_task_id": child_task_id,
        }
        for name, execution_mode, child_task_id in invocations
    ]

    assert independent_role_evidence_satisfied(required, records) is expected


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
            "execution_evidence": {
                "tool_call_count": 1,
                "obligation_evidence": {
                    "schema": "zyra.runtime-obligation-evidence/v1",
                    "successful_skill_invocations": [
                        {
                            "name": "verification",
                            "execution_mode": "fork",
                            "child_task_id": "child-verifier",
                        }
                    ],
                },
            },
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
    assert result["domain_result"]["obligation_evidence"] == (
        result["execution_evidence"]["obligation_evidence"]
    )
    assert result["domain_result"]["obligation_evidence"][
        "successful_skill_invocations"
    ][0]["child_task_id"] == "child-verifier"
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


def test_physical_adapter_does_not_promote_incomplete_code_worker(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = _runtime(tmp_path)

    def execute(**_kwargs):
        return {
            "provider_call": {
                "provider_called": True,
                "task_execution_verified": True,
                "prompt_goal_bound": True,
                "synthetic_usage": False,
                "usage": {},
                "calls": [{"request_id": "provider-request-partial"}],
            },
            "execution_evidence": {"tool_call_count": 1},
            "workspace_delta": {
                "created": ["partial.txt"],
                "modified": [],
                "deleted": [],
                "changed": ["partial.txt"],
            },
            "final_text": "",
            "execution_outcome": "needs_verification",
            "runtime_terminal_error": {"worker_error": "completion_stop_exhausted"},
            "workspace_effect_observed": True,
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
            "output_contract": ["artifact_refs", "verification", "worker_result"],
        },
        operator_runtime="CodeWorkerRuntime",
        goal="Create partial.txt and finish the requested package.",
        goal_contract=None,
        layer_index=1,
        workload=_workload(),
    )

    assert result["execution_outcome"] == "needs_verification"
    assert result["workspace_effect_observed"] is True
    assert result["contract_outputs"]["worker_result"]["ok"] is False
    assert result["contract_outputs"]["verification"]["passed"] is False


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
    assert contract.verification_required is False
    assert contract.required_paths == ("smoke.txt",)
    assert contract.expected_file_contents == (("smoke.txt", "指定文字"),)

    strict = goal_delivery_contract(
        "在当前目录创建 proof.txt，文件内容必须严格为 "
        "ZYRA_LOCAL_EXECUTOR_OK（末尾可以有一个换行），完成后简短说明。"
    )
    assert strict.expected_file_contents == (
        ("proof.txt", "ZYRA_LOCAL_EXECUTOR_OK"),
    )
    assert strict.verification_required is False


def test_delivery_contract_keeps_behavioral_verification_for_source_changes() -> None:
    contract = goal_delivery_contract(
        "创建 app.py，文件内容严格为 print('ok')。"
    )
    assert contract.workspace_mutation_required is True
    assert contract.verification_required is True


def test_delivery_contract_preserves_declared_directory_scope_for_file_list() -> None:
    contract = goal_delivery_contract(
        "工作区根目录的 task-contract.json 给出约束。\n"
        "请在 `submission/` 中交付：\n"
        "- manifest.json\n"
        "- architecture.md\n"
        "- release-notes.md"
    )

    assert contract.required_paths == (
        "submission/manifest.json",
        "submission/architecture.md",
        "submission/release-notes.md",
    )


def test_delivery_contract_maps_known_app_workspace_paths_without_inventing_other_paths() -> None:
    contract = goal_delivery_contract(
        "Create `/app/meeting_scheduled.ics` for alice@example.com with VERSION:2.0."
    )
    assert contract.workspace_mutation_required is True
    assert contract.required_paths == ("meeting_scheduled.ics",)

    relative = goal_delivery_contract(
        "Create meeting_scheduled.ics for alice@example.com with VERSION:2.0."
    )
    assert relative.required_paths == ("meeting_scheduled.ics",)

    hyphenated_absolute = goal_delivery_contract(
        "Create /workspace/deadline-proof.txt and verify it."
    )
    assert hyphenated_absolute.workspace_mutation_required is True
    assert hyphenated_absolute.required_paths == ()


def test_delivery_contract_uses_objective_workspace_evidence_over_model_wording(
    tmp_path,
) -> None:
    goal = (
        "A video is located at /app/video.mp4. Transcribe all chess moves and "
        "create a file /app/solution.txt containing the result."
    )
    contract = goal_delivery_contract(goal)
    provider = {
        "provider_called": True,
        "task_execution_verified": True,
        "prompt_goal_bound": True,
        "synthetic_usage": False,
        "calls": [{"request_id": "provider-request-1"}],
    }
    (tmp_path / "video.mp4").write_bytes(b"input-video")
    incomplete = validate_goal_delivery(
        goal,
        projection=contract.to_dict(),
        workspace_root=tmp_path,
        workspace_delta={"created": ["frames/001.png"]},
        final_response=(
            "The task deadline has been reached. I was unable to complete the "
            "transcription and could not create /app/solution.txt."
        ),
        provider_evidence=provider,
    )

    assert contract.required_paths == ("solution.txt",)
    assert incomplete["passed"] is False
    assert incomplete["checks"]["required_paths_present"] is False
    assert incomplete["schema"] == "zyra.goal-delivery-verification/v2"
    assert "required_paths_present" in incomplete["decision"]["decisive_failures"]

    (tmp_path / "solution.txt").write_text("1. e4 e5\n", encoding="utf-8")
    complete = validate_goal_delivery(
        goal,
        projection=contract.to_dict(),
        workspace_root=tmp_path,
        workspace_delta={"created": ["solution.txt"]},
        final_response=(
            "I am not sure the transcription was completed or that the requested "
            "file is ready."
        ),
        provider_evidence=provider,
    )
    assert complete["passed"] is True
    assert complete["decision"]["decisive_failures"] == []
    assert complete["evidence"][-1] == {
        "tier": 4,
        "source": "model_final_response",
        "name": "final_response_present",
        "passed": True,
        "decisive": True,
    }


def test_delivery_contract_ignores_commands_inputs_and_schema_references() -> None:
    contract = goal_delivery_contract(
        "工作区根目录的 `task-contract.json` 给出约束。\n"
        "请创建 `work/build_submission.py`，并通过 `python tools/hailanctl.py` 验收。\n"
        "按 `schemas/plan.v1.schema.json` 保存 "
        "`submission/normalized/operational/plan-initial.json`。\n"
        "请在 `submission/` 中交付：\n"
        "- manifest.json\n"
        "- report.md"
    )

    assert contract.required_paths == (
        "work/build_submission.py",
        "submission/normalized/operational/plan-initial.json",
        "submission/manifest.json",
        "submission/report.md",
    )
    assert contract.expected_file_contents == ()


def test_delivery_contract_rejects_model_claim_without_artifact(tmp_path) -> None:
    goal = "Create result.txt with file content 'ready'."
    contract = goal_delivery_contract(goal)
    verification = validate_goal_delivery(
        goal,
        projection=contract.to_dict(),
        workspace_root=tmp_path,
        workspace_delta={},
        final_response="Completed successfully.",
        provider_evidence={
            "provider_called": True,
            "task_execution_verified": True,
            "prompt_goal_bound": True,
            "synthetic_usage": False,
            "calls": [{"request_id": "provider-request"}],
        },
    )

    assert verification["passed"] is False
    assert verification["checks"]["required_paths_present"] is False
    assert verification["checks"]["workspace_mutation_observed"] is False


def test_delivery_contract_rejects_artifact_when_deterministic_content_check_fails(
    tmp_path,
) -> None:
    goal = "Create result.txt with file content 'ready'."
    contract = goal_delivery_contract(goal)
    (tmp_path / "result.txt").write_text("wrong\n", encoding="utf-8")
    verification = validate_goal_delivery(
        goal,
        projection=contract.to_dict(),
        workspace_root=tmp_path,
        workspace_delta={"created": ["result.txt"]},
        final_response="Completed successfully.",
        provider_evidence={
            "provider_called": True,
            "task_execution_verified": True,
            "prompt_goal_bound": True,
            "synthetic_usage": False,
            "calls": [{"request_id": "provider-request"}],
        },
    )

    assert verification["passed"] is False
    assert verification["checks"]["required_paths_present"] is True
    assert verification["checks"]["expected_file_contents_match"] is False


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


def test_delivery_contract_compiles_explicit_runtime_obligations() -> None:
    contract = goal_delivery_contract(
        "先用 LoopX 建立目标、待办、claim、gate 和证据计划，并由不同角色独立复核。\n"
        "使用 `pdf-analysis` 和 `web-research`；使用记忆/trace 摘要。\n"
        "调用 `report-writing` 和 `verification`。\n"
        "编写并运行一个可重复的脚本。\n"
        "请在 `deliverables/` 中交付：source_index.json、reproduce.ps1。"
    )

    assert contract.required_skills == (
        "pdf-analysis",
        "report-writing",
        "trace-summary",
        "verification",
        "web-research",
    )
    assert contract.required_executed_paths == ("deliverables/reproduce.ps1",)
    assert contract.provenance_index_paths == (
        "deliverables/source_index.json",
    )
    assert contract.loopx_required is True
    assert contract.role_separation_required is True


@pytest.mark.parametrize(
    ("goal", "expected"),
    (
        (
            "请在 `deliverables/` 中交付 change_report.md，并保留清晰的变更轨迹。",
            {"report-writing", "trace-summary"},
        ),
        (
            "Deliver `output/audit-report.json` and preserve the execution trace.",
            {"report-writing", "trace-summary"},
        ),
        (
            "生成 `notes/summary.md`，记录诊断追踪和验证结论。",
            {"trace-summary"},
        ),
    ),
    ids=("chinese-change-report", "english-audit-report", "trace-with-non-report"),
)
def test_delivery_contract_infers_skills_from_typed_outputs(
    goal: str,
    expected: set[str],
) -> None:
    contract = goal_delivery_contract(goal)

    assert set(contract.required_skills) == expected


def test_delivery_contract_does_not_infer_skills_from_input_mentions() -> None:
    contract = goal_delivery_contract(
        "Inspect the existing `inputs/prior-report.json`; do not save or retain a trace."
    )

    assert contract.required_paths == ()
    assert contract.required_skills == ()


def test_delivery_contract_compiles_generic_ordered_mutation_policy() -> None:
    goals = (
        (
            "`inputs/widget-kit/` is a small library. Work in an isolated copy. "
            "Run the existing tests and save the baseline before editing. "
            "Do not modify existing tests; new regression tests are allowed.",
            "inputs/widget-kit",
        ),
        (
            "`fixtures/ledger-core/` 是待修复库，请在隔离副本中完成。"
            "运行现有测试并保存基线；不得修改既有测试的业务断言。",
            "fixtures/ledger-core",
        ),
    )

    for goal, source_root in goals:
        policy = goal_delivery_contract(goal).to_dict()["mutation_policy"]
        assert policy == {
            "schema": "zyra.task-mutation-policy/v1",
            "enabled": True,
            "protected_source_roots": [source_root],
            "required_pre_mutation_evidence": ["existing_test_baseline"],
            "protect_existing_test_files": True,
            "inherit_across_execution_lineage": True,
        }


def test_delivery_contract_does_not_invent_mutation_policy() -> None:
    policy = goal_delivery_contract(
        "Inspect `inputs/widget-kit/` and explain the architecture."
    ).to_dict()["mutation_policy"]

    assert policy["enabled"] is False
    assert policy["protected_source_roots"] == []
    assert policy["required_pre_mutation_evidence"] == []
    assert policy["protect_existing_test_files"] is False


def test_provenance_index_requires_digest_and_extraction_method(tmp_path) -> None:
    goal = "请在 `deliverables/` 中交付 source_index.json。"
    contract = goal_delivery_contract(goal)
    target = tmp_path / "deliverables" / "source_index.json"
    target.parent.mkdir()
    target.write_text(
        '{"inputs":[{"path":"inputs/a.csv","role":"data"}]}',
        encoding="utf-8",
    )
    provider = {
        "provider_called": True,
        "task_execution_verified": True,
        "prompt_goal_bound": True,
        "synthetic_usage": False,
        "calls": [{"request_id": "provider-request"}],
    }
    invalid = validate_goal_delivery(
        goal,
        projection=contract.to_dict(),
        workspace_root=tmp_path,
        workspace_delta={"created": ["deliverables/source_index.json"]},
        final_response="完成",
        provider_evidence=provider,
    )
    assert invalid["checks"]["provenance_indexes_valid"] is False

    target.write_text(
        "{\"inputs\":[{\"path\":\"inputs/a.csv\","
        "\"sha256\":\"" + "a" * 64 + "\","
        "\"extraction_method\":\"csv parser\"}]}",
        encoding="utf-8",
    )
    valid = validate_goal_delivery(
        goal,
        projection=contract.to_dict(),
        workspace_root=tmp_path,
        workspace_delta={"created": ["deliverables/source_index.json"]},
        final_response="完成",
        provider_evidence=provider,
    )
    assert valid["checks"]["provenance_indexes_valid"] is True


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
