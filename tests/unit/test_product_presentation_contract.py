from __future__ import annotations

from apps.api.zyra_api.product_presentation import (
    PRODUCT_PRESENTATION_SCHEMA,
    project_product_presentation,
)


def test_generic_agent_message_is_not_admitted_as_assistant_content() -> None:
    assert (
        project_product_presentation(
            {
                "eventId": "event_noise",
                "eventType": "runtime.agent.message",
                "summary": "must not become assistant text",
                "identity": {"taskId": "task_1"},
                "inline": {
                    "legacy_event_type": "system_notice",
                    "workspace_event": {"lifecycle_state": "ready"},
                },
            }
        )
        is None
    )


def test_physical_worker_tool_history_requires_exact_task_and_result_commitment() -> None:
    query = {
        "canonical_owner": "typescript", "task_id": "task_1",
        "phase": "tool_call_started", "tool_call_id": "call_1", "tool_name": "file_write",
        "arguments": {"secret": "must-not-leak"},
    }
    event = {"eventType": "runtime.agent.message", "identity": {"taskId": "task_1"}, "inline": {"query_session": query}}
    assert project_product_presentation(event) == {
        "schema": PRODUCT_PRESENTATION_SCHEMA, "kind": "tool", "phase": "started",
        "identity": "call_1", "label": "file_write", "severity": "info",
    }
    query["phase"] = "tool_call_completed"
    assert project_product_presentation(event) is None
    query["tool_result_commitment"] = {
        "schema": "zyra.public-tool-result-commitment/v1", "tool_call_id": "call_1", "ok": True,
    }
    assert project_product_presentation(event)["phase"] == "completed"
    assert "must-not-leak" not in str(project_product_presentation(event))
    query["tool_result_commitment"]["ok"] = False
    assert project_product_presentation(event)["phase"] == "failed"
    query["task_id"] = "task_foreign"
    assert project_product_presentation(event) is None


def test_text_stream_projects_versioned_assistant_content_without_runtime_fields() -> None:
    started = project_product_presentation(
        {
            "eventId": "event_text_started",
            "eventType": "runtime.text.started",
            "identity": {"taskId": "task_1"},
            "inline": {
                "stream_id": "answer_1",
                "assistant_message_id": "message_1",
                "content_digest": "must-not-leak",
            },
        }
    )
    delta = project_product_presentation(
        {
            "eventId": "event_text_delta",
            "eventType": "runtime.text.delta",
            "identity": {"taskId": "task_1"},
            "inline": {
                "stream_id": "answer_1",
                "assistant_message_id": "message_1",
                "presentation_text": "你好\n\n**world**\x1b[2J",
                "content_digest": "must-not-leak",
            },
        }
    )
    assert started == {
        "schema": PRODUCT_PRESENTATION_SCHEMA,
        "kind": "assistant",
        "phase": "started",
        "identity": "message_1",
        "label": "Assistant",
        "severity": "info",
        "streamId": "answer_1",
    }
    assert delta == {
        "schema": PRODUCT_PRESENTATION_SCHEMA,
        "kind": "assistant",
        "phase": "delta",
        "identity": "message_1",
        "label": "Assistant",
        "severity": "info",
        "streamId": "answer_1",
        "text": "你好\n\n**world**",
    }
    assert "must-not-leak" not in str(started)
    assert "must-not-leak" not in str(delta)


def test_execution_and_dispatch_have_stable_user_level_projection() -> None:
    started = project_product_presentation(
        {
            "eventId": "event_started",
            "eventType": "runtime.agent.message",
            "identity": {"taskId": "task_1"},
            "inline": {
                "schema": "zyra.task-execution-started/v1",
                "started_at": "2026-09-01T00:00:00Z",
                "owner_token": "must-not-leak",
            },
        }
    )
    dispatched = project_product_presentation(
        {
            "eventId": "event_dispatch",
            "eventType": "runtime.backend.dispatch.requested",
            "identity": {"taskId": "task_1", "workerId": "provider-code-worker"},
            "inline": {"backend_id": "local-sandbox-gateway"},
        }
    )
    assert started == {
        "schema": PRODUCT_PRESENTATION_SCHEMA,
        "kind": "activity",
        "phase": "started",
        "identity": "task-execution:task_1",
        "label": "Task execution",
        "severity": "info",
        "category": "execution",
        "startedAt": "2026-09-01T00:00:00Z",
    }
    assert dispatched == {
        "schema": PRODUCT_PRESENTATION_SCHEMA,
        "kind": "worker",
        "phase": "dispatched",
        "identity": "provider-code-worker",
        "label": "provider-code-worker",
        "severity": "info",
        "summary": "backend local-sandbox-gateway",
    }
    assert "must-not-leak" not in str(started)


def test_execution_error_whitelists_message_and_never_copies_secret_fields() -> None:
    projected = project_product_presentation(
        {
            "eventId": "event_error",
            "eventType": "runtime.agent.message",
            "identity": {"taskId": "task_1"},
            "inline": {
                "schema": "zyra.task-execution-error/v1",
                "error": "provider_unavailable",
                "message": "Provider is unavailable. Authorization: Bearer secret-value-123456",
                "retryable": True,
                "authorization": "Bearer secret-value",
            },
        }
    )
    assert projected is not None
    assert projected["kind"] == "issue"
    assert projected["code"] == "provider_unavailable"
    assert projected["retryable"] is True
    assert "secret-value" not in str(projected)


def test_tool_output_projection_admits_only_bounded_stdout_stderr_descriptors() -> None:
    projected = project_product_presentation(
        {
            "eventId": "event_tool_done",
            "eventType": "runtime.tool.succeeded",
            "identity": {"taskId": "task_1", "toolCallId": "tool_call_1"},
            "inline": {
                "tool_name": "tests",
                "presentation_summary": "12 tests passed",
                "authorization": "Bearer must-not-leak",
            },
            "artifactRefs": [
                {
                    "artifactId": "artifact_stdout",
                    "title": "Test stdout",
                    "mediaType": "text/plain",
                    "sizeBytes": 10 * 1024 * 1024,
                    "digest": "secret-digest",
                    "metadata": {
                        "source_path": "source_payload.stdout",
                        "authorization": "Bearer must-not-leak",
                    },
                },
                {
                    "artifactId": "artifact_stderr",
                    "title": "Test stderr",
                    "mediaType": "text/plain",
                    "sizeBytes": 42,
                    "metadata": {"source_path": "source_payload.stderr"},
                },
                {
                    "artifactId": "artifact_result",
                    "title": "Generic result",
                    "mediaType": "application/json",
                    "metadata": {"source_path": "source_payload.result"},
                },
            ],
        }
    )
    assert projected is not None
    assert projected["outputArtifacts"] == [
        {
            "artifactId": "artifact_stdout",
            "stream": "stdout",
            "title": "Test stdout",
            "mediaType": "text/plain",
            "sizeBytes": 10 * 1024 * 1024,
        },
        {
            "artifactId": "artifact_stderr",
            "stream": "stderr",
            "title": "Test stderr",
            "mediaType": "text/plain",
            "sizeBytes": 42,
        },
    ]
    assert projected["artifactIds"] == [
        "artifact_stdout",
        "artifact_stderr",
        "artifact_result",
    ]
    assert "source_path" not in str(projected)
    assert "secret-digest" not in str(projected)
    assert "must-not-leak" not in str(projected)


def test_local_worker_tool_failure_and_recovery_have_explicit_non_task_impact() -> None:
    tool = project_product_presentation(
        {
            "eventId": "event_tool_failed",
            "eventType": "runtime.tool.failed",
            "identity": {"taskId": "task_1", "toolCallId": "tool_call_failed"},
            "inline": {
                "tool_name": "tests",
                "error_code": "exit_1",
                "presentation_summary": "one test failed",
                "authorization": "Bearer must-not-leak",
            },
        }
    )
    worker = project_product_presentation(
        {
            "eventId": "event_worker_failed",
            "eventType": "runtime.node.failed",
            "identity": {"taskId": "task_1", "workerId": "worker-a"},
            "inline": {
                "reason": "worker heartbeat timeout",
                "secret": "must-not-leak",
            },
        }
    )
    recovered = project_product_presentation(
        {
            "eventId": "event_recovery_completed",
            "eventType": "runtime.recovery.completed",
            "identity": {"taskId": "task_1"},
            "inline": {
                "recovery_id": "recovery-1",
                "strategy": "replace_worker",
                "status": "completed",
            },
        }
    )

    assert tool is not None
    assert tool["kind"] == "tool"
    assert tool["phase"] == "failed"
    assert tool["impact"] == "local"
    assert tool["code"] == "exit_1"
    assert worker is not None
    assert worker["kind"] == "worker"
    assert worker["phase"] == "failed"
    assert worker["impact"] == "local"
    assert worker["severity"] == "warning"
    assert recovered is not None
    assert recovered["kind"] == "activity"
    assert recovered["phase"] == "completed"
    assert recovered["impact"] == "local"
    assert recovered["category"] == "recovery"
    assert "must-not-leak" not in str(tool)
    assert "must-not-leak" not in str(worker)


def test_task_execution_error_is_explicitly_task_scoped() -> None:
    projected = project_product_presentation(
        {
            "eventId": "event_task_error",
            "eventType": "runtime.agent.message",
            "identity": {"taskId": "task_1"},
            "inline": {
                "schema": "zyra.task-execution-error/v1",
                "error": "provider_unavailable",
                "message": "Task execution could not continue.",
            },
        }
    )
    assert projected is not None
    assert projected["kind"] == "issue"
    assert projected["impact"] == "task"
