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
