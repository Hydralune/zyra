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
