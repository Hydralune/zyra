from __future__ import annotations

from zyra_orchestration.deployment.code_worker_adapter import (
    _public_runtime_events,
    _public_session_projection,
)


def _assistant_event(phase: str, **values: object) -> dict[str, object]:
    return {
        "schema": "zyra.provider-assistant-presentation/v1",
        "phase": phase,
        "delta_kind": "assistant_text",
        "stream_id": "provider:dispatch-1",
        "assistant_message_id": "message:assistant:provider:dispatch-1",
        "provider_sequence": 3,
        **values,
    }


def test_public_projection_admits_only_bounded_redacted_assistant_text() -> None:
    projected = _public_session_projection(
        _assistant_event(
            "assistant_text_delta",
            content="answer api_key=sk-private-provider-key",
            segment_index=4,
        )
    )

    assert projected is not None
    assert projected["phase"] == "assistant_text_delta"
    assert projected["presentation_text"] == "answer api_key=<redacted>"
    assert projected["content_persisted"] is False
    assert "content" not in projected
    assert "sk-private-provider-key" not in str(projected)


def test_public_projection_rejects_private_or_malformed_stream_frames() -> None:
    assert _public_session_projection(
        {"phase": "message_delta", "content": "private model token"}
    ) is None
    assert _public_session_projection(
        {"phase": "model_stream_frame", "content": "private reasoning"}
    ) is None
    assert _public_session_projection(
        _assistant_event(
            "assistant_text_delta",
            content="x" * 1_025,
        )
    ) is None
    assert _public_session_projection(
        _assistant_event(
            "assistant_text_delta",
            content="answer",
            stream_id="",
        )
    ) is None
    malformed_counter = _public_session_projection(
        _assistant_event(
            "assistant_text_delta",
            content="answer",
            segment_index="not-an-integer",
        )
    )
    assert malformed_counter is not None
    assert malformed_counter["segment_index"] == 0


def test_public_event_projection_drops_live_delta_from_durable_worker_result() -> None:
    projected = _public_runtime_events(
        [
            {
                "event_id": "event-assistant-1",
                "task_id": "task-1",
                "run_id": "run-1",
                "payload": {
                    "query_session": _assistant_event(
                        "assistant_text_delta",
                        content="bounded answer",
                        segment_index=7,
                    ),
                    "typescript_runtime": {
                        "runtime_id": "zyra-typescript-claude-runtime",
                        "protocol": "zyra.claude-runtime.v1",
                        "canonical_owner": "typescript",
                        "phase": "assistant_text_delta",
                        "scope": "parent_session",
                        "parent_session_id": "session-1",
                        "effective_session_id": "session-1",
                        "untrusted_extra": "must not cross",
                    },
                },
            }
        ]
    )

    assert projected == []


def test_public_event_projection_keeps_bounded_text_only_for_opted_in_tasks() -> None:
    """A demo task that opts in keeps its answer text; every other task does not."""

    events = [
        {
            "event_id": "event-assistant-1",
            "task_id": "task-1",
            "run_id": "run-1",
            "payload": {
                "query_session": _assistant_event(
                    "assistant_text_delta",
                    content="bounded answer",
                    segment_index=7,
                ),
            },
        }
    ]

    # Default: low-entropy.  The same event that produces nothing above still
    # produces nothing without the explicit opt-in.
    assert _public_runtime_events(events) == []

    projected = _public_runtime_events(events, persist_presentation_text=True)

    assert len(projected) == 1
    session = projected[0]["payload"]["query_session"]
    assert session["presentation_text"] == "bounded answer"
    # The digest custody is untouched and the raw content never crosses.
    assert session["content_persisted"] is False
    assert "content" not in session


def test_public_event_projection_bounds_persisted_text_per_stream() -> None:
    """An over-long answer keeps a bounded prefix and says what was elided."""

    chunk = _assistant_event(
        "assistant_text_delta",
        content="x" * 1_024,
        segment_index=1,
    )
    # 100 KiB of chunks against a 64 KiB per-stream allowance.
    events = [
        {
            "event_id": f"event-assistant-{index}",
            "task_id": "task-1",
            "run_id": "run-1",
            "payload": {"query_session": {**chunk, "assistant_message_id": f"m-{index}"}},
        }
        for index in range(100)
    ]

    projected = _public_runtime_events(events, persist_presentation_text=True)

    retained = [
        event["payload"]["query_session"]
        for event in projected
        if event.get("schema") != "zyra.presentation-retention-summary/v1"
    ]
    kept = [session for session in retained if "presentation_text" in session]
    elided = [session for session in retained if "presentation_text" not in session]
    summaries = [
        event
        for event in projected
        if event.get("schema") == "zyra.presentation-retention-summary/v1"
    ]
    kept_bytes = sum(
        len(session["presentation_text"].encode("utf-8")) for session in kept
    )
    assert kept_bytes == 64 * 1_024
    assert len(elided) == 36
    # Every elided frame still keeps its lifecycle and custody fields, so only
    # the redundant text copy was dropped, not the record of what happened.
    assert all(session["content_persisted"] is False for session in elided)
    assert all(session["stream_id"] == "provider:dispatch-1" for session in elided)
    # The elision is reported rather than silently swallowed.
    assert len(summaries) == 1
    assert summaries[0]["dropped_presentation_bytes"] == 36 * 1_024
    assert summaries[0]["per_stream_limit_bytes"] == 64 * 1_024
