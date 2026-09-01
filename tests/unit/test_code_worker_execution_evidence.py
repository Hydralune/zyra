from __future__ import annotations

from types import SimpleNamespace

from zyra_workers.code_worker_runtime import _execution_evidence


def test_execution_evidence_preserves_openai_cached_prompt_tokens() -> None:
    result = SimpleNamespace(
        session_snapshot={},
        event_records=(
            SimpleNamespace(
                payload={
                    "query_session": {
                        "phase": "model_stream_report",
                        "model_stream": {
                            "provider_request_id": "provider-request-1",
                            "provider": "zhipu",
                            "model": "glm-5.2",
                            "route_id": "route-1",
                            "transport": "provider_control_plane",
                            "status": 200,
                            "ok": True,
                            "usage": {
                                "prompt_tokens": 1_000,
                                "completion_tokens": 25,
                                "total_tokens": 1_025,
                                "prompt_tokens_details": {
                                    "cached_tokens": 800,
                                },
                            },
                        },
                    }
                }
            ),
        ),
    )

    evidence = _execution_evidence(result)

    assert evidence["provider_called"] is True
    assert evidence["usage"]["input_tokens"] == 1_000
    assert evidence["usage"]["output_tokens"] == 25
    assert evidence["usage"]["cache_read_input_tokens"] == 800
    assert evidence["provider_calls"][0]["usage"]["cache_read_input_tokens"] == 800


def test_execution_evidence_preserves_runtime_obligation_receipts() -> None:
    obligation = {
        "schema": "zyra.runtime-obligation-evidence/v1",
        "successful_skill_invocations": [
            {
                "name": "verification",
                "execution_mode": "fork",
                "child_task_id": "child-verifier",
            }
        ],
        "successful_executed_paths": [
            {"path": "deliverables/reproduce.ps1"}
        ],
        "workspace_mutation_count": 2,
    }
    result = SimpleNamespace(
        session_snapshot={
            "typescript_runtime_snapshot": {
                "modelIteration": {"finalText": "done"},
                "obligationEvidence": obligation,
            }
        },
        event_records=(),
        tool_call_count=2,
        turn_count=1,
        artifacts=(),
    )

    evidence = _execution_evidence(result)

    assert evidence["obligation_evidence"] == obligation


def test_execution_evidence_projects_redacted_verification_command_receipts() -> None:
    obligation = {
        "schema": "zyra.runtime-obligation-evidence/v1",
        "successful_skill_invocations": [],
        "successful_executed_paths": [],
        "verification_command_receipts": [
            {
                "schema": "zyra.verification-command-receipt/v1",
                "tool_call_id": "tool-wait",
                "originating_tool_call_id": "tool-shell",
                "scope": "shell:pytest:tests/unit",
                "status": "failed",
                "exit_code": 1,
                "workspace_mutation_count": 3,
            }
        ],
        "workspace_mutation_count": 3,
    }
    result = SimpleNamespace(
        session_snapshot={
            "typescript_runtime_snapshot": {
                "modelIteration": {"finalText": "done"},
                "obligationEvidence": obligation,
                "e01Runtime": {
                    "query": {
                        "toolCalls": [
                            {
                                "toolCallId": "tool-shell",
                                "name": "shell",
                                "arguments": {
                                    "command": (
                                        "python -m pytest tests/unit "
                                        "--token sk-secret-value "
                                        "C:\\Users\\libin\\private\\case.py"
                                    )
                                },
                            }
                        ]
                    }
                },
            }
        },
        event_records=(),
        tool_call_count=2,
        turn_count=1,
        artifacts=(),
    )

    evidence = _execution_evidence(result)

    receipt = evidence["obligation_evidence"][
        "verification_command_receipts"
    ][0]
    assert receipt["status"] == "failed"
    assert receipt["exit_code"] == 1
    assert "tests/unit" in receipt["command"]
    assert "sk-secret-value" not in receipt["command"]
    assert "libin" not in receipt["command"]
    assert "[REDACTED]" in receipt["command"]
